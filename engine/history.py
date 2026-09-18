"""
engine/history.py
-----------------------------------------------------------------------------
Stage 2 of the pipeline: Performance History.

Rolls raw, day-by-day sales_daily rows up into fixed-length pricing_cycles
(3 days by default). The rest of the engine (eligibility, learning, decision)
never looks at sales_daily directly -- it only ever reasons in terms of
CLOSED cycles. That separation is what the spec means by "The system should
build a historical profile for every product rather than making decisions
based only on the most recent few days": cycles are the atomic unit of
memory, and a product's "historical profile" is just its list of past cycles.

Design note on price tracking: rather than maintaining a separate "price
change log" that has to be joined back against dates, every sales_daily row
already carries the unit_price that was in effect that day (see
engine/ingestion.py). So a cycle's starting price is simply read off the
first day's row in that cycle's window -- one less place for the system to
disagree with itself about what the price actually was on a given day.
"""

from datetime import date, timedelta
import sqlite3

from config import PricingConfig
from models import CycleMetrics


def _product_created_date(conn: sqlite3.Connection, product_id: str) -> date:
    row = conn.execute("SELECT created_at FROM products WHERE product_id = ?", (product_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown product_id: {product_id!r}")
    return date.fromisoformat(row["created_at"][:10])


def _last_closed_cycle_index(conn: sqlite3.Connection, product_id: str) -> int:
    row = conn.execute(
        "SELECT MAX(cycle_index) AS max_idx FROM pricing_cycles WHERE product_id = ?",
        (product_id,),
    ).fetchone()
    return row["max_idx"] if row["max_idx"] is not None else -1


def close_due_cycles(conn: sqlite3.Connection, cfg: PricingConfig, product_id: str, as_of: date = None) -> list[CycleMetrics]:
    """
    Close every pricing cycle for `product_id` whose window has fully
    elapsed as of `as_of` (defaults to today) and that hasn't been closed
    yet. Returns the list of newly-closed cycles (empty if none were due).

    This is safe to call repeatedly/idempotently: a cycle, once closed, is
    never recomputed, and calling this again before the next cycle boundary
    is a no-op.
    """
    as_of = as_of or date.today()
    created = _product_created_date(conn, product_id)
    next_index = _last_closed_cycle_index(conn, product_id) + 1

    newly_closed: list[CycleMetrics] = []

    while True:
        window_start = created + timedelta(days=cfg.cycle_length_days * next_index)
        window_end = window_start + timedelta(days=cfg.cycle_length_days - 1)
        # Only close a cycle once its FULL window (all N days) has elapsed --
        # we never aggregate a partial, still-in-progress cycle, since that
        # would let an incomplete day masquerade as a finished decision point.
        if window_end >= as_of:
            break

        rows = conn.execute(
            """
            SELECT sale_date, units_sold, unit_price, unit_cost, revenue, profit
            FROM sales_daily
            WHERE product_id = ? AND sale_date >= ? AND sale_date <= ?
            ORDER BY sale_date ASC
            """,
            (product_id, window_start.isoformat(), window_end.isoformat()),
        ).fetchall()

        if not rows:
            # No sales_daily rows recorded for this window at all -- e.g. the
            # feed hasn't caught up yet. Stop here rather than fabricating an
            # empty cycle; we'll pick back up next time close_due_cycles runs.
            break

        units_sold = sum(r["units_sold"] for r in rows)
        revenue = round(sum(r["revenue"] for r in rows), 2)
        total_profit = round(sum(r["profit"] for r in rows), 2)
        avg_unit_cost = round(sum(r["unit_cost"] for r in rows) / len(rows), 4)
        price_at_cycle_start = rows[0]["unit_price"]

        # --- Post-replenishment baseline flag -------------------------------
        product = conn.execute(
            "SELECT baseline_reset_at FROM products WHERE product_id = ?", (product_id,)
        ).fetchone()
        is_post_replenishment = 0
        if product["baseline_reset_at"]:
            reset_date = date.fromisoformat(product["baseline_reset_at"][:10])
            if window_start >= reset_date:
                cycles_since_reset = conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM pricing_cycles
                    WHERE product_id = ? AND start_date >= ?
                    """,
                    (product_id, product["baseline_reset_at"][:10]),
                ).fetchone()["n"]
                if cycles_since_reset < cfg.post_replenishment_cycles_required:
                    is_post_replenishment = 1

        cur = conn.execute(
            """
            INSERT INTO pricing_cycles
                (product_id, cycle_index, start_date, end_date, price_at_cycle_start,
                 units_sold, revenue, total_profit, avg_unit_cost, status, is_post_replenishment)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete', ?)
            """,
            (
                product_id, next_index, window_start.isoformat(), window_end.isoformat(),
                price_at_cycle_start, units_sold, revenue, total_profit, avg_unit_cost,
                is_post_replenishment,
            ),
        )
        conn.commit()

        newly_closed.append(CycleMetrics(
            id=cur.lastrowid, product_id=product_id, cycle_index=next_index,
            start_date=window_start.isoformat(), end_date=window_end.isoformat(),
            price_at_cycle_start=price_at_cycle_start, units_sold=units_sold,
            revenue=revenue, total_profit=total_profit, avg_unit_cost=avg_unit_cost,
        ))
        next_index += 1

    return newly_closed


def get_cycle_history(conn: sqlite3.Connection, product_id: str, limit: int = None) -> list[CycleMetrics]:
    """Return every closed cycle for a product, oldest first -- this list IS
    the product's "historical profile" the spec describes."""
    query = """
        SELECT id, product_id, cycle_index, start_date, end_date, price_at_cycle_start,
               units_sold, revenue, total_profit, avg_unit_cost
        FROM pricing_cycles
        WHERE product_id = ? AND status = 'complete'
        ORDER BY cycle_index ASC
    """
    rows = conn.execute(query, (product_id,)).fetchall()
    if limit is not None:
        rows = rows[-limit:]
    return [
        CycleMetrics(
            id=r["id"], product_id=r["product_id"], cycle_index=r["cycle_index"],
            start_date=r["start_date"], end_date=r["end_date"],
            price_at_cycle_start=r["price_at_cycle_start"], units_sold=r["units_sold"],
            revenue=r["revenue"], total_profit=r["total_profit"], avg_unit_cost=r["avg_unit_cost"],
        )
        for r in rows
    ]


def cycles_completed_count(conn: sqlite3.Connection, product_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM pricing_cycles WHERE product_id = ? AND status = 'complete'",
        (product_id,),
    ).fetchone()
    return row["n"]
