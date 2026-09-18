"""
engine/safety.py
-----------------------------------------------------------------------------
Pure, stateless guardrail calculations shared by the decision and execution
stages. Kept separate from engine/decision.py deliberately: these functions
encode the *hard* numeric limits from the spec's "Safety and Control"
section (minimum margin, maximum price movement per cycle) and having them
isolated, dependency-free, and easy to unit test in one place makes it much
easier for a reviewer (or an interviewer) to verify the engine literally
cannot violate them, no matter what the decision logic in decision.py does.

Every function here is pure: given the same inputs, always the same output,
no I/O. That's intentional -- guardrails should be trivially testable.
"""

from dataclasses import dataclass

from config import PricingConfig


def min_allowed_price(cost: float, min_margin_pct: float) -> float:
    """
    The lowest price at which margin = (price - cost) / price still meets
    min_margin_pct. Solving that equation for price gives:

        price = cost / (1 - min_margin_pct / 100)

    e.g. cost=$100, min_margin_pct=15 -> price must be >= $117.65 (a $117.65
    price on a $100 cost yields exactly 15% margin: (117.65-100)/117.65 = 0.15).
    """
    margin_fraction = min_margin_pct / 100.0
    if margin_fraction >= 1.0:
        raise ValueError(f"min_margin_pct must be < 100, got {min_margin_pct}")
    return round(cost / (1 - margin_fraction), 2)


def effective_min_margin_pct(cfg: PricingConfig, product_row) -> float:
    """Per-product override wins over the global default, if one is set."""
    override = product_row["min_margin_pct_override"]
    return override if override is not None else cfg.min_margin_pct


def effective_max_move_pct(cfg: PricingConfig, product_row) -> float:
    override = product_row["max_move_pct_override"]
    return override if override is not None else cfg.max_price_move_pct


@dataclass
class ClampedMove:
    final_price: float
    final_pct_change: float
    was_clamped_by_margin: bool
    was_clamped_by_max_move: bool
    blocked: bool             # True if no valid move remains at all (-> caller should HOLD)
    block_reason: str = ""


def clamp_price_move(current_price: float, candidate_price: float, cost: float,
                      cfg: PricingConfig, product_row) -> ClampedMove:
    """
    Take a *desired* candidate price from the decision logic and force it to
    respect both hard safety limits:

      1. Never below the minimum-margin floor.
      2. Never move by more than the configured max percent per cycle.

    This is the single choke point both limits are enforced through, so a
    bug in decision.py's reasoning can never actually produce an unsafe
    price -- at worst it produces a price that gets clamped back into range,
    or blocked entirely if no safe move exists in the requested direction.
    """
    max_move_pct = effective_max_move_pct(cfg, product_row)
    min_margin_pct = effective_min_margin_pct(cfg, product_row)

    floor_price = min_allowed_price(cost, min_margin_pct)

    # --- 1. Max-move clamp -----------------------------------------------------
    max_delta = current_price * (max_move_pct / 100.0)
    lo_by_move, hi_by_move = current_price - max_delta, current_price + max_delta
    clamped_by_move = min(max(candidate_price, lo_by_move), hi_by_move)
    was_clamped_by_max_move = round(clamped_by_move, 2) != round(candidate_price, 2)

    # --- 2. Margin-floor clamp --------------------------------------------------
    final_price = clamped_by_move
    was_clamped_by_margin = False
    if final_price < floor_price:
        final_price = floor_price
        was_clamped_by_margin = True

    final_price = round(final_price, 2)
    pct_change = round((final_price - current_price) / current_price * 100, 4) if current_price else 0.0

    # If, after clamping, the move rounds to essentially nothing (or would
    # require moving the WRONG direction to satisfy the margin floor, e.g. a
    # decrease that the floor won't allow at all), there is no meaningful
    # price change left to make -- signal the caller to HOLD instead of
    # executing a change of a fraction of a cent.
    blocked = abs(final_price - current_price) < 0.01
    block_reason = ""
    if blocked and candidate_price < current_price and floor_price >= current_price:
        block_reason = (
            f"Minimum margin floor (${floor_price:.2f}, {min_margin_pct:.1f}% margin) is at or above the "
            f"current price (${current_price:.2f}); no room remains to decrease."
        )
    elif blocked:
        block_reason = "Clamped price change is smaller than $0.01 and would not be a meaningful adjustment."

    return ClampedMove(
        final_price=final_price, final_pct_change=pct_change,
        was_clamped_by_margin=was_clamped_by_margin, was_clamped_by_max_move=was_clamped_by_max_move,
        blocked=blocked, block_reason=block_reason,
    )
