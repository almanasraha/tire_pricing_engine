"""
engine/learning.py
-----------------------------------------------------------------------------
Stages 3 and 6-7 of the pipeline: Historical Learning, Outcome Evaluation,
and Learning. This is the module that turns raw cycle history into the kind
of product-specific understanding the spec describes:

    "When this product was priced around $180, it sold approximately 5
    units per cycle and generated $150 profit. When the price increased to
    $181, volume dropped and total profit fell. Therefore, another increase
    may not be desirable."

Two independent things happen here, and it's worth keeping them mentally
separate:

  1. build_price_performance_profile() -- "what has this product's demand
     curve actually looked like at different prices?" A purely descriptive
     summary of history, no judgment attached.

  2. evaluate_pending_experiments() / get_directional_reluctance() -- "did
     our past ACTIONS (the price changes we chose to make) work out?" This
     is what lets the engine develop caution or confidence about a specific
     direction (raising vs lowering) for a specific product, rather than
     treating every SKU identically.

engine/decision.py consumes the outputs of both when it reasons about what
to do next.
"""

from datetime import datetime
from statistics import mean
import math
import sqlite3

from config import PricingConfig
from engine.history import get_cycle_history

# Bucket width for the price/performance profile, as a PERCENT of price
# rather than a fixed dollar amount. Tire prices in this system range from
# ~$40 to ~$300+, and price moves are themselves percentage-based
# (cfg.min/max_price_move_pct), so a fixed "$1" bucket would be far too fine
# for a $300 product (a single 0.3% move is already ~$1, meaning every move
# lands in a brand-new bucket with zero history -- the engine would never
# accumulate evidence at any price and would oscillate forever chasing a
# "better" neighboring bucket that's actually the same price it just left).
# A percentage-width bucket keeps roughly one bucket per max-sized cycle
# move at ANY price level, so consecutive cycles at a similar price
# genuinely share a bucket, and history can actually accumulate.
PRICE_BUCKET_WIDTH_PCT = 1.0


def price_bucket_key(price: float) -> int:
    """Map a price to a geometric (percentage-width) bucket id. Using a log
    scale means each bucket spans the same PERCENTAGE range at every price
    level (unlike a fixed dollar-width bucket, which would be far too fine
    at high prices and far too coarse at low ones)."""
    if price <= 0:
        return 0
    return round(math.log(price) / math.log(1 + PRICE_BUCKET_WIDTH_PCT / 100))


# =============================================================================
# 1. Descriptive price/performance profile ("the historical profile")
# =============================================================================

def build_price_performance_profile(conn: sqlite3.Connection, product_id: str, start_cycle_index: int = 0) -> list[dict]:
    """
    Group a product's cycle history by price point -- using a percentage-
    width bucket (see PRICE_BUCKET_WIDTH_PCT above) so "$180 vs $181" (the
    spec's own example, on a ~$180 item) separates cleanly while a $300
    item's own ~1% neighboring prices don't fragment into one bucket per
    cycle -- and summarize average performance at each price.

    `start_cycle_index` lets callers restrict this to "cycles since the last
    baseline reset" for a product currently inside its post-replenishment
    window (see engine/eligibility.py), so a major restock's fresh cycles
    aren't blended with stale pre-restock history.

    Returns buckets sorted by price ascending, e.g.:
        [{"price": 180.40, "bucket_key": 1042, "avg_units": 5.1, "avg_profit": 148.3, "n_cycles": 6}, ...]
    `price` is the bucket's mean observed price (for human-readable
    explanations); `bucket_key` is the exact grouping key decision.py uses
    to test "is the current price in this same bucket."
    """
    cycles = [c for c in get_cycle_history(conn, product_id) if c.cycle_index >= start_cycle_index]

    buckets: dict[int, list] = {}
    for c in cycles:
        key = price_bucket_key(c.price_at_cycle_start)
        buckets.setdefault(key, []).append(c)

    profile = []
    for key in sorted(buckets):
        bucket_cycles = buckets[key]
        profile.append({
            "bucket_key": key,
            "price": round(mean(c.price_at_cycle_start for c in bucket_cycles), 2),
            "avg_units": round(mean(c.units_sold for c in bucket_cycles), 2),
            "avg_profit": round(mean(c.total_profit for c in bucket_cycles), 2),
            "n_cycles": len(bucket_cycles),
        })
    return profile


# =============================================================================
# 2. Outcome evaluation for past price-change experiments
# =============================================================================

def _classify_outcome(before_profit: float, after_profit: float, before_units: int, after_units: int,
                       cfg: PricingConfig) -> str:
    """Turn a before/after profit comparison into one of the four outcome
    labels the spec asks for: positive, negative, neutral, or inconclusive."""
    total_units = before_units + after_units
    if total_units < 2:
        # Essentially no sales activity around the change at all -- there is
        # nothing here to reliably attribute to the price change itself.
        return "inconclusive"

    if before_profit == 0 and after_profit == 0:
        return "inconclusive"
    if before_profit == 0:
        # Can't express a meaningful percent change from a zero base; fall
        # back to a plain sign comparison.
        if after_profit > 0:
            return "positive"
        return "neutral" if after_profit == 0 else "negative"

    pct_change = (after_profit - before_profit) / abs(before_profit) * 100
    if abs(pct_change) <= cfg.neutral_profit_band_pct:
        return "neutral"
    return "positive" if pct_change > 0 else "negative"


def evaluate_pending_experiments(conn: sqlite3.Connection, cfg: PricingConfig, product_id: str = None) -> list[dict]:
    """
    Find every executed price-change (an "experiment") that hasn't been
    scored yet, and score any of them for which enough subsequent cycles
    have now completed. This is deliberately decoupled from the moment the
    price change happened -- outcome evaluation runs on ITS OWN schedule
    (whenever this function is called, e.g. once per simulated cycle) and
    only acts once `cfg.experiment_evaluation_cycles` cycles of fresh
    evidence exist. Safe to call as often as you like; already-scored rows
    and not-yet-ready rows are simply skipped.

    Returns a list of dicts describing everything that WAS scored on this
    call, for logging/reporting.
    """
    query = """
        SELECT pd.id, pd.product_id, pd.cycle_id, pd.decision, pd.price_before, pd.price_after
        FROM pricing_decisions pd
        WHERE pd.is_experiment = 1 AND pd.executed = 1 AND pd.outcome IS NULL
    """
    params = ()
    if product_id:
        query += " AND pd.product_id = ?"
        params = (product_id,)
    pending = conn.execute(query, params).fetchall()

    results = []
    for row in pending:
        anchor = conn.execute(
            "SELECT cycle_index FROM pricing_cycles WHERE id = ?", (row["cycle_id"],)
        ).fetchone()
        if anchor is None:
            continue
        anchor_index = anchor["cycle_index"]

        all_cycles = get_cycle_history(conn, row["product_id"])
        prior_cycles = [c for c in all_cycles if c.cycle_index <= anchor_index][-3:]
        subsequent_cycles = [c for c in all_cycles if c.cycle_index > anchor_index][: cfg.experiment_evaluation_cycles]

        if len(subsequent_cycles) < cfg.experiment_evaluation_cycles:
            continue  # not enough evidence has accumulated yet -- still pending

        before_profit = mean(c.total_profit for c in prior_cycles) if prior_cycles else 0.0
        before_units = sum(c.units_sold for c in prior_cycles)
        after_profit = mean(c.total_profit for c in subsequent_cycles)
        after_units = sum(c.units_sold for c in subsequent_cycles)

        outcome = _classify_outcome(before_profit, after_profit, before_units, after_units, cfg)

        conn.execute(
            "UPDATE pricing_decisions SET outcome = ?, outcome_evaluated_at = ? WHERE id = ?",
            (outcome, datetime.utcnow().isoformat(), row["id"]),
        )
        results.append({
            "product_id": row["product_id"], "decision_id": row["id"], "decision": row["decision"],
            "price_before": row["price_before"], "price_after": row["price_after"],
            "before_avg_profit": round(before_profit, 2), "after_avg_profit": round(after_profit, 2),
            "outcome": outcome,
        })

    if results:
        conn.commit()
    return results


# =============================================================================
# 3. Directional reluctance / confidence ("product-specific pricing knowledge")
# =============================================================================

def get_directional_reluctance(conn: sqlite3.Connection, cfg: PricingConfig, product_id: str) -> dict:
    """
    Look at this product's OWN history of evaluated experiments (not other
    products') to decide whether the engine should currently be cautious or
    confident about INCREASING vs DECREASING its price.

    "if repeated price increases for a particular product consistently
    reduce total profit, the engine should become increasingly reluctant to
    increase that product's price. Conversely, if increases maintain sales
    while improving total profit, it should recognize that additional
    pricing power may exist."

    Returns, per direction, the number of CONSECUTIVE most-recent outcomes
    (walking backward from the latest) that were negative or positive --
    a streak is broken by any outcome that isn't the same sign, so a single
    intervening success/failure resets the count. `reluctant` becomes True
    once the negative streak reaches cfg.consecutive_negative_reluctance_threshold.
    """
    reluctance = {}
    for direction in ("INCREASE", "DECREASE"):
        rows = conn.execute(
            """
            SELECT outcome FROM pricing_decisions
            WHERE product_id = ? AND decision = ? AND is_experiment = 1 AND outcome IS NOT NULL
            ORDER BY decision_time DESC
            """,
            (product_id, direction),
        ).fetchall()

        negative_streak = 0
        for r in rows:
            if r["outcome"] == "negative":
                negative_streak += 1
            else:
                break

        positive_streak = 0
        for r in rows:
            if r["outcome"] == "positive":
                positive_streak += 1
            else:
                break

        reluctance[direction] = {
            "negative_streak": negative_streak,
            "positive_streak": positive_streak,
            "reluctant": negative_streak >= cfg.consecutive_negative_reluctance_threshold,
            "confident": positive_streak >= cfg.consecutive_negative_reluctance_threshold,
            "sample_size": len(rows),
        }
    return reluctance
