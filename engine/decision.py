"""
engine/decision.py
-----------------------------------------------------------------------------
Stage 4 of the pipeline: Pricing Decision.

This is where everything upstream (eligibility, cycle history, the learned
price/performance profile, and product-specific directional reluctance)
gets combined into one of three outputs: INCREASE, DECREASE, or HOLD, plus
a human-readable `reason` string (the spec's "Ability to explain why a
price was changed").

DESIGN PHILOSOPHY -- deterministic rules first:
The spec is explicit that "Initially the system can use deterministic rules
and statistical learning," with room to bring in ML later once enough
history exists. So this is intentionally a transparent, auditable if/else
decision tree over well-named signals -- not a black box -- which also
happens to be exactly what you want to be able to explain in an interview.
Swapping this function's internals for a learned model later doesn't
require touching anything else in the pipeline, because every other stage
only depends on this function's OUTPUT shape (a PricingDecision).

THE CORE IDEA, in one sentence: look at what has actually happened at
nearby prices this product has been sold at before, and only move toward a
price that the data says was (or is likely to be) more profitable --
becoming more cautious about a direction the moment it has a track record
of hurting total profit for THIS SPECIFIC product.
"""

import sqlite3

from config import PricingConfig
from models import PricingDecision
from engine.eligibility import evaluate_eligibility
from engine.history import get_cycle_history
from engine.learning import build_price_performance_profile, get_directional_reluctance, price_bucket_key
from engine.safety import clamp_price_move


def _nearest_bucket(profile: list[dict], current_bucket_key: int, direction: str) -> dict | None:
    """Among the historical price buckets, find the closest one that is
    strictly above (direction='up') or below (direction='down') the CURRENT
    price bucket -- i.e. "the last time we actually tried pricing
    meaningfully higher/lower than where we are now, what happened?"
    Compares by bucket_key (not raw price) so a bucket whose mean price
    happens to sit a few cents on the other side of current_price, but is
    really the SAME percentage-width bucket we're already in, is correctly
    treated as neither up nor down. Returns None if that direction has
    never been explored within the current baseline window."""
    candidates = [
        b for b in profile
        if (b["bucket_key"] > current_bucket_key if direction == "up" else b["bucket_key"] < current_bucket_key)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda b: abs(b["bucket_key"] - current_bucket_key))


def decide_price(conn: sqlite3.Connection, cfg: PricingConfig, product_id: str) -> PricingDecision:
    """
    The main entry point for Stage 4. Always returns a PricingDecision --
    even a HOLD is a full decision record with a reason, because the spec
    requires a "complete history of every recommendation," not just the
    ones that changed something.
    """
    product = conn.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)).fetchone()
    if product is None:
        raise ValueError(f"Unknown product_id: {product_id!r}")

    cycles = get_cycle_history(conn, product_id)
    if not cycles:
        return PricingDecision(
            product_id=product_id, cycle_id=-1, decision="HOLD",
            price_before=product["current_price"], price_after=product["current_price"], pct_change=0.0,
            reason="No completed pricing cycles yet; nothing to evaluate.",
        )
    latest_cycle = cycles[-1]

    # --- Step 1: eligibility gate (hard) ---------------------------------------
    eligibility = evaluate_eligibility(conn, cfg, product_id)
    if not eligibility.eligible:
        return PricingDecision(
            product_id=product_id, cycle_id=latest_cycle.id, decision="HOLD",
            price_before=product["current_price"], price_after=product["current_price"], pct_change=0.0,
            reason=eligibility.reason,
        )

    current_price = product["current_price"]
    cost = product["cost"]

    # --- Step 2: build this product's historical profile (within the current
    # baseline window -- all history, or just post-replenishment cycles if
    # the product is inside a reset window) -------------------------------------
    profile = build_price_performance_profile(conn, product_id, start_cycle_index=eligibility.relevant_cycles_start_index)
    current_bucket_key = price_bucket_key(current_price)
    current_bucket = next((b for b in profile if b["bucket_key"] == current_bucket_key), None)

    reluctance = get_directional_reluctance(conn, cfg, product_id)

    # --- Step 3: recent profit trend, independent of price (catches demand
    # shifts unrelated to our own pricing, e.g. seasonality) --------------------
    relevant_cycles = [c for c in cycles if c.cycle_index >= eligibility.relevant_cycles_start_index]
    recent = relevant_cycles[-2:]
    prior = relevant_cycles[-4:-2] if len(relevant_cycles) >= 4 else []
    recent_avg_profit = sum(c.total_profit for c in recent) / len(recent) if recent else 0.0
    prior_avg_profit = (sum(c.total_profit for c in prior) / len(prior)) if prior else recent_avg_profit
    profit_trend_pct = (
        ((recent_avg_profit - prior_avg_profit) / abs(prior_avg_profit)) * 100 if prior_avg_profit else 0.0
    )

    up_bucket = _nearest_bucket(profile, current_bucket_key, "up")
    down_bucket = _nearest_bucket(profile, current_bucket_key, "down")

    def profit_of(bucket):
        return bucket["avg_profit"] if bucket else None

    # --- Step 4: decide a DIRECTION using historical evidence -------------------
    # "The decision should consider total profit and sales behavior
    # together... a higher margin per unit is not necessarily successful if
    # the resulting reduction in sales causes total profit to fall."
    # That trade-off is exactly why every comparison below is on
    # avg_profit (units x margin together), never on price or units alone.
    direction = None
    strength = "exploratory"   # exploratory | weak | strong -- drives move size, see _select_move_pct
    evidence_notes = []

    current_profit_ref = current_bucket["avg_profit"] if current_bucket else recent_avg_profit

    if up_bucket and profit_of(up_bucket) is not None and profit_of(up_bucket) > current_profit_ref * (1 + cfg.neutral_profit_band_pct / 100):
        direction, strength = "INCREASE", "strong"
        evidence_notes.append(
            f"at ~${up_bucket['price']}, historical avg total profit was ${up_bucket['avg_profit']:.2f} "
            f"over {up_bucket['n_cycles']} cycle(s) vs ~${current_profit_ref:.2f} at the current price band"
        )
    elif down_bucket and profit_of(down_bucket) is not None and profit_of(down_bucket) > current_profit_ref * (1 + cfg.neutral_profit_band_pct / 100):
        direction, strength = "DECREASE", "strong"
        evidence_notes.append(
            f"at ~${down_bucket['price']}, historical avg total profit was ${down_bucket['avg_profit']:.2f} "
            f"over {down_bucket['n_cycles']} cycle(s) vs ~${current_profit_ref:.2f} at the current price band"
        )
    elif not profile or len(profile) <= 1:
        # No meaningful price variation has ever been observed for this
        # product -- there is no elasticity signal to react to yet, so the
        # only responsible move is a small, cautious exploratory step, and
        # only in the direction demand can currently support.
        if latest_cycle.units_sold > 0 and profit_trend_pct >= 0:
            direction, strength = "INCREASE", "exploratory"
            evidence_notes.append("no price variation observed yet; demand is healthy, so testing modest pricing power")
        elif profit_trend_pct < -cfg.neutral_profit_band_pct:
            direction, strength = "DECREASE", "exploratory"
            evidence_notes.append(f"no price variation observed yet; recent profit trend is down {abs(profit_trend_pct):.1f}%")
        else:
            direction = None
            evidence_notes.append("no price variation observed yet and no clear trend either way; holding")
    else:
        # We have SOME profile but neither neighbor beats the current price
        # band -- history suggests we're already close to the locally best
        # price. Only override that with a small corrective move if profit
        # is actively declining right now.
        if profit_trend_pct < -cfg.neutral_profit_band_pct * 2 and down_bucket:
            direction, strength = "DECREASE", "weak"
            evidence_notes.append(f"current price appears near-optimal historically, but recent profit is down {abs(profit_trend_pct):.1f}%")
        else:
            direction = None
            evidence_notes.append("current price already appears near-optimal based on historical performance at nearby prices")

    # --- Step 5: apply learned directional reluctance/confidence ---------------
    if direction and reluctance[direction]["reluctant"]:
        evidence_notes.append(
            f"overridden to HOLD: the last {reluctance[direction]['negative_streak']} {direction.lower()} "
            f"experiment(s) for this product reduced total profit, so the engine is currently reluctant to try that again"
        )
        direction = None
    elif direction and reluctance[direction]["confident"] and strength != "strong":
        strength = "strong"
        evidence_notes.append(
            f"upgraded confidence: the last {reluctance[direction]['positive_streak']} {direction.lower()} "
            f"experiment(s) for this product improved total profit"
        )

    if direction is None:
        return PricingDecision(
            product_id=product_id, cycle_id=latest_cycle.id, decision="HOLD",
            price_before=current_price, price_after=current_price, pct_change=0.0,
            reason="; ".join(evidence_notes) or "No favorable price adjustment identified this cycle.",
        )

    # --- Step 6: choose a magnitude within the configured band -----------------
    move_pct = {
        "strong": cfg.max_price_move_pct,
        "weak": round((cfg.min_price_move_pct + cfg.max_price_move_pct) / 2, 4),
        "exploratory": cfg.min_price_move_pct,
    }[strength]

    candidate_price = round(
        current_price * (1 + move_pct / 100) if direction == "INCREASE" else current_price * (1 - move_pct / 100), 2
    )

    # --- Step 7: hard safety clamp (margin floor + max move) -------------------
    clamped = clamp_price_move(current_price, candidate_price, cost, cfg, product)
    if clamped.blocked:
        return PricingDecision(
            product_id=product_id, cycle_id=latest_cycle.id, decision="HOLD",
            price_before=current_price, price_after=current_price, pct_change=0.0,
            reason=f"Wanted to {direction.lower()} based on: {'; '.join(evidence_notes)}. {clamped.block_reason}",
        )

    safety_note = []
    if clamped.was_clamped_by_margin:
        safety_note.append("reduced move to respect the minimum profit margin floor")
    if clamped.was_clamped_by_max_move:
        safety_note.append(f"capped move at the configured max of {cfg.max_price_move_pct}% per cycle")

    final_decision = "INCREASE" if clamped.final_price > current_price else (
        "DECREASE" if clamped.final_price < current_price else "HOLD"
    )
    if final_decision == "HOLD":
        return PricingDecision(
            product_id=product_id, cycle_id=latest_cycle.id, decision="HOLD",
            price_before=current_price, price_after=current_price, pct_change=0.0,
            reason=f"Wanted to {direction.lower()} based on: {'; '.join(evidence_notes)}, but safety clamps left no meaningful change.",
        )

    reason_parts = [f"{final_decision} based on: {'; '.join(evidence_notes)}"]
    if safety_note:
        reason_parts.append("; ".join(safety_note))
    reason_parts.append(
        f"current margin {((current_price - cost) / current_price * 100):.1f}% -> "
        f"new margin {((clamped.final_price - cost) / clamped.final_price * 100):.1f}%"
    )

    return PricingDecision(
        product_id=product_id, cycle_id=latest_cycle.id, decision=final_decision,
        price_before=current_price, price_after=clamped.final_price, pct_change=clamped.final_pct_change,
        reason=". ".join(reason_parts) + ".",
    )
