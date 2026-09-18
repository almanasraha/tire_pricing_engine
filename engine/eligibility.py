"""
engine/eligibility.py
-----------------------------------------------------------------------------
Gatekeeper stage that runs BEFORE the decision engine ever looks at pricing.
Every one of these safeguards maps directly to a bullet in the spec's
"Safety and Control" and "New Products and Inventory Replenishment" sections:

  - "Minimum historical data before pricing begins"
  - "Protection for new products"
  - "Recognition of major inventory replenishment"
  - "Ability to lock individual products from automatic pricing"
  - distinguishing a brand-new zero-sales product from an old dead-stock one

engine/decision.py calls `evaluate_eligibility()` once per product per cycle
and refuses to make a price change (falls back to HOLD) whenever
`eligible` is False -- eligibility is a hard gate, never a soft suggestion.
"""

from datetime import date, timedelta
import sqlite3

from config import PricingConfig
from models import EligibilityResult
from engine.history import get_cycle_history, cycles_completed_count


def evaluate_eligibility(conn: sqlite3.Connection, cfg: PricingConfig, product_id: str) -> EligibilityResult:
    product = conn.execute(
        "SELECT product_id, is_locked, created_at, baseline_reset_at FROM products WHERE product_id = ?",
        (product_id,),
    ).fetchone()
    if product is None:
        return EligibilityResult(eligible=False, reason=f"Unknown product_id {product_id!r}.")

    # --- 1. Manual lock always wins, no matter what the data says ------------
    if product["is_locked"]:
        return EligibilityResult(
            eligible=False, is_locked=True,
            reason="Product is manually locked from automatic pricing.",
        )

    all_cycles = get_cycle_history(conn, product_id)
    total_cycles_completed = len(all_cycles)

    # --- 2. Major-replenishment baseline reset --------------------------------
    # If a major replenishment happened recently, we evaluate "how much
    # history does this product have" against cycles SINCE the reset, not
    # since the product's original creation -- per spec: "temporarily allow
    # the product to establish a new performance baseline rather than
    # blindly relying on conditions that existed before replenishment."
    relevant_cycles = all_cycles
    is_post_replenishment_window = False
    if product["baseline_reset_at"]:
        reset_date = date.fromisoformat(product["baseline_reset_at"][:10])
        post_reset_cycles = [c for c in all_cycles if date.fromisoformat(c.start_date) >= reset_date]
        if len(post_reset_cycles) < cfg.post_replenishment_cycles_required:
            is_post_replenishment_window = True
            relevant_cycles = post_reset_cycles

    cycles_available = len(relevant_cycles)
    relevant_start_index = relevant_cycles[0].cycle_index if relevant_cycles else total_cycles_completed

    # --- 3. New-product protection --------------------------------------------
    if cycles_available < cfg.min_cycles_before_eligible:
        if is_post_replenishment_window:
            reason = (
                f"Product is within its post-replenishment baseline window: only "
                f"{cycles_available}/{cfg.min_cycles_before_eligible} cycles have completed since the "
                f"major restock on {product['baseline_reset_at'][:10]}. Treating it like a new product "
                f"until a fresh performance baseline is established."
            )
        else:
            reason = (
                f"Product has completed {cycles_available}/{cfg.min_cycles_before_eligible} required "
                f"pricing cycles since introduction ({product['created_at'][:10]}). New products are "
                f"protected from automatic pricing until enough history accumulates."
            )
        return EligibilityResult(
            eligible=False, is_new_product=True, is_post_replenishment_window=is_post_replenishment_window,
            cycles_available=cycles_available, relevant_cycles_start_index=relevant_start_index, reason=reason,
        )

    # --- 4. Distinguish "new with zero sales" from "old dead stock" ----------
    # By this point the product already has enough cycles to be structurally
    # eligible, so a run of zero-sales cycles here means something different:
    # it's not "too new to know," it's "we have real signal and the signal is
    # zero." That's flagged (not silently treated as a normal HOLD) so an
    # operator can see it and decide whether to investigate (dead inventory,
    # wrong price entirely, discontinued item, etc.) rather than the engine
    # nudging price by fractions of a percent forever on phantom data.
    recent_window_days = cfg.stale_zero_sales_days
    cutoff = date.today() - timedelta(days=recent_window_days)
    recent_relevant_cycles = [c for c in relevant_cycles if date.fromisoformat(c.end_date) >= cutoff] or relevant_cycles
    is_stale_zero_sales = cycles_available >= cfg.min_cycles_before_eligible and all(
        c.units_sold == 0 for c in recent_relevant_cycles
    )

    if is_stale_zero_sales:
        return EligibilityResult(
            eligible=False, is_stale_zero_sales=True, is_post_replenishment_window=is_post_replenishment_window,
            cycles_available=cycles_available, relevant_cycles_start_index=relevant_start_index,
            reason=(
                f"Product has {cycles_available} completed cycles with zero units sold in the last "
                f"{recent_window_days} days -- this is an established item with no demand signal, not a "
                f"new product. Flagged for review rather than priced automatically; no reliable price/"
                f"demand relationship can be learned from zero data points."
            ),
        )

    # --- 5. Eligible ------------------------------------------------------------
    return EligibilityResult(
        eligible=True, is_post_replenishment_window=is_post_replenishment_window,
        cycles_available=cycles_available, relevant_cycles_start_index=relevant_start_index,
        reason=(
            f"Eligible: {cycles_available} qualifying cycles of history "
            f"{'since post-replenishment reset' if is_post_replenishment_window else 'since introduction'} "
            f"(>= required {cfg.min_cycles_before_eligible})."
        ),
    )
