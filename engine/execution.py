"""
engine/execution.py
-----------------------------------------------------------------------------
Stage 5 of the pipeline: Price Execution.

Takes the PricingDecision produced by engine/decision.py and (a) always
writes it to the audit log, and (b) if it's an actual change, applies it to
the live `products.current_price`. This is also where two more Safety and
Control bullets get satisfied concretely:

  - "Prevention of duplicate price executions"
  - "Complete history of every recommendation and executed change"

DUPLICATE PREVENTION, concretely: `pricing_decisions.execution_key` is a
UNIQUE column (see database/schema.sql) built as "<product_id>:<cycle_id>".
There is exactly one decision permitted per product per cycle. If
`run_pricing_cycle` is ever called twice for the same product in the same
cycle (a retried request, an overlapping scheduled job, a network retry
from the website), the second INSERT hits SQLite's UNIQUE constraint and
fails BEFORE any price is touched -- so duplicate execution is prevented by
the database itself, not by application logic that could have a bug in it.
Note HOLD decisions intentionally get execution_key = NULL: SQLite (like
standard SQL) treats NULL as distinct from every other NULL for uniqueness
purposes, so any number of HOLD rows can coexist -- only actual price
CHANGES are guarded against duplication.
"""

from datetime import datetime
import sqlite3

from config import PricingConfig
from models import PricingDecision
from engine.decision import decide_price


def execute_decision(conn: sqlite3.Connection, decision: PricingDecision) -> dict:
    """
    Persist one PricingDecision to the audit log, and apply it to the
    product's live price if it's an actual change.

    Returns a summary dict: {"decision_id", "executed", "duplicate"}.
    `duplicate=True` means this exact (product, cycle) decision was already
    recorded previously -- nothing was written or changed on this call.
    """
    is_experiment = decision.decision != "HOLD"
    execution_key = f"{decision.product_id}:{decision.cycle_id}" if is_experiment else None

    try:
        cur = conn.execute(
            """
            INSERT INTO pricing_decisions
                (product_id, cycle_id, decision_time, decision, price_before, price_after,
                 pct_change, reason, is_experiment, executed, execution_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                decision.product_id, decision.cycle_id, datetime.utcnow().isoformat(), decision.decision,
                decision.price_before, decision.price_after, decision.pct_change, decision.reason,
                int(is_experiment), execution_key,
            ),
        )
    except sqlite3.IntegrityError:
        # execution_key already exists -> this (product, cycle) was already
        # decided and (if it was a change) already executed. Refuse to act
        # again rather than silently re-applying a price change.
        conn.rollback()
        return {"decision_id": None, "executed": False, "duplicate": True}

    decision_id = cur.lastrowid
    executed = False

    if is_experiment:
        conn.execute(
            "UPDATE products SET current_price = ?, updated_at = ? WHERE product_id = ?",
            (decision.price_after, datetime.utcnow().isoformat(), decision.product_id),
        )
        conn.execute("UPDATE pricing_decisions SET executed = 1 WHERE id = ?", (decision_id,))
        executed = True

    conn.commit()
    return {"decision_id": decision_id, "executed": executed, "duplicate": False}


def run_pricing_cycle(conn: sqlite3.Connection, cfg: PricingConfig, product_id: str) -> dict:
    """
    The full Pricing Decision -> Price Execution step for ONE product:
    decide, then execute, then report what happened. This is what a
    scheduler (or the API's /pricing/run-cycle endpoint) calls once per
    product per cycle.
    """
    decision = decide_price(conn, cfg, product_id)
    outcome = execute_decision(conn, decision)
    return {
        "product_id": product_id,
        "decision": decision.decision,
        "price_before": decision.price_before,
        "price_after": decision.price_after,
        "pct_change": decision.pct_change,
        "reason": decision.reason,
        **outcome,
    }


def run_pricing_cycle_for_all(conn: sqlite3.Connection, cfg: PricingConfig) -> list[dict]:
    """Run the full decision+execution step for every product currently in
    the system. Products automatically participate simply by existing in
    the `products` table -- no per-product registration or rule setup
    required, per the spec's scale requirements."""
    product_ids = [r["product_id"] for r in conn.execute("SELECT product_id FROM products").fetchall()]
    return [run_pricing_cycle(conn, cfg, pid) for pid in product_ids]
