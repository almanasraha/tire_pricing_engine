"""
engine/ingestion.py
-----------------------------------------------------------------------------
Stage 1 of the pipeline: Data Collection.

Everything the rest of the system trusts (sales, inventory receipts) enters
the database through the two functions in this file, and ONLY through them.
That's what lets us honor the safeguard "Validation of incoming sales/
inventory data": there is exactly one door, and it's locked to anything that
looks malformed or physically implausible.

In a real deployment, these functions are what the company's POS / inventory
system (or an ETL job pulling from it) would call -- either directly, or via
the `POST /ingest/sales` and `POST /ingest/inventory-receipt` endpoints in
api.py, which are thin wrappers around these same functions.
"""

from datetime import datetime, date
import sqlite3

from config import PricingConfig


class ValidationError(ValueError):
    """Raised when an incoming row fails a sanity check. Deliberately a
    distinct type (not a bare ValueError) so callers -- and the API layer --
    can catch it specifically and turn it into a clean 4xx response instead
    of a generic 500."""


def record_daily_sale(
    conn: sqlite3.Connection,
    cfg: PricingConfig,
    product_id: str,
    sale_date: str,
    units_sold: int,
    unit_price: float,
    unit_cost: float,
) -> int:
    """
    Validate and store one day's sales figure for one product.

    Returns the new sales_daily row id.
    Raises ValidationError if the row is implausible or the product is
    unknown -- we never silently accept bad data, because every downstream
    stage (history, learning, decisions) assumes this table is trustworthy.
    """
    product = conn.execute(
        "SELECT product_id, current_inventory FROM products WHERE product_id = ?",
        (product_id,),
    ).fetchone()
    if product is None:
        raise ValidationError(f"Unknown product_id: {product_id!r}. Products must exist before sales are recorded.")

    if units_sold < 0:
        raise ValidationError(f"units_sold cannot be negative (got {units_sold}).")
    if units_sold > cfg.max_plausible_daily_units:
        raise ValidationError(
            f"units_sold={units_sold} exceeds the plausibility ceiling "
            f"({cfg.max_plausible_daily_units}) for a single SKU/day. "
            f"Rejected as likely a data error (e.g. units mixed up with a different aggregation level)."
        )
    if unit_price < 0 or unit_price > cfg.max_plausible_unit_price:
        raise ValidationError(f"unit_price={unit_price} is outside the plausible range for this system.")
    if unit_cost < 0:
        raise ValidationError(f"unit_cost cannot be negative (got {unit_cost}).")
    try:
        datetime.strptime(sale_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValidationError(f"sale_date must be ISO format YYYY-MM-DD, got {sale_date!r}.") from exc

    # units_sold cannot exceed what was actually on hand -- a classic sign of
    # a duplicated feed or a unit mismatch upstream.
    if units_sold > product["current_inventory"]:
        raise ValidationError(
            f"units_sold={units_sold} exceeds current recorded inventory "
            f"({product['current_inventory']}) for {product_id}. Rejected pending reconciliation."
        )

    revenue = round(units_sold * unit_price, 2)
    profit = round(units_sold * (unit_price - unit_cost), 2)

    cur = conn.execute(
        """
        INSERT INTO sales_daily (product_id, sale_date, units_sold, unit_price, unit_cost, revenue, profit, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (product_id, sale_date, units_sold, unit_price, unit_cost, revenue, profit, datetime.utcnow().isoformat()),
    )

    # Selling stock is the one inventory movement that happens automatically
    # alongside a sale (a receipt is a separate, explicit event below).
    conn.execute(
        "UPDATE products SET current_inventory = current_inventory - ?, updated_at = ? WHERE product_id = ?",
        (units_sold, datetime.utcnow().isoformat(), product_id),
    )
    conn.commit()
    return cur.lastrowid


def record_inventory_receipt(
    conn: sqlite3.Connection,
    cfg: PricingConfig,
    product_id: str,
    received_at: str,
    quantity: int,
    unit_cost: float,
) -> int:
    """
    Validate and store an inventory replenishment event, flagging whether it
    counts as a "major" receipt (see PricingConfig.major_replenishment_pct_increase).

    Flagging happens HERE, at ingestion time, rather than being recomputed
    later, so the "was this major?" judgment is fixed to the inventory level
    that actually existed at the moment of receipt -- not recalculated after
    the fact against whatever inventory happens to be on hand later.
    """
    product = conn.execute(
        "SELECT product_id, current_inventory FROM products WHERE product_id = ?",
        (product_id,),
    ).fetchone()
    if product is None:
        raise ValidationError(f"Unknown product_id: {product_id!r}.")
    if quantity <= 0:
        raise ValidationError(f"quantity must be positive (got {quantity}).")
    if unit_cost < 0:
        raise ValidationError(f"unit_cost cannot be negative (got {unit_cost}).")

    prior_inventory = product["current_inventory"]
    # A receipt counts as "major" if it's a big jump relative to what was on
    # hand -- including the important edge case of restocking a product that
    # had run out (prior_inventory == 0), which is always major by definition
    # since it re-establishes availability from nothing.
    if prior_inventory <= 0:
        is_major = True
    else:
        pct_increase = (quantity / prior_inventory) * 100.0
        is_major = pct_increase >= cfg.major_replenishment_pct_increase

    cur = conn.execute(
        """
        INSERT INTO inventory_receipts (product_id, received_at, quantity, unit_cost, is_major)
        VALUES (?, ?, ?, ?, ?)
        """,
        (product_id, received_at, quantity, unit_cost, int(is_major)),
    )

    update_fields = ["current_inventory = current_inventory + ?", "cost = ?", "updated_at = ?"]
    params = [quantity, unit_cost, datetime.utcnow().isoformat()]
    if is_major:
        # This is what "temporarily allow the product to establish a new
        # performance baseline" means concretely: stamp the reset time so
        # engine/eligibility.py can start a fresh baseline-eligibility clock.
        update_fields.append("baseline_reset_at = ?")
        params.append(received_at)
    params.append(product_id)

    conn.execute(f"UPDATE products SET {', '.join(update_fields)} WHERE product_id = ?", params)
    conn.commit()
    return cur.lastrowid
