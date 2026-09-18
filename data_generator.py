"""
data_generator.py
-----------------------------------------------------------------------------
Builds a realistic SYNTHETIC dataset for a tire business and loads it into
the database, standing in for the "data from the company's existing
systems" the spec assumes already exists (POS sales history, inventory
receipts, product catalog). Nothing here is real usawheelstires.com data --
it's a plausible tire catalog with an underlying price-elasticity-of-demand
model per product, so that when the pricing engine later runs against it,
there is genuine, learnable signal (some products truly get less profitable
as price rises, some don't) rather than random noise.

WHY A DEMAND MODEL AND NOT JUST RANDOM NUMBERS:
The whole point of this engine is to detect patterns like "raising this
product's price reduced total profit." If sales were pure random noise,
there would be nothing consistent for the engine to learn, and a demo run
would look identical whether the learning logic worked or was broken. Each
synthetic product instead gets a fixed (hidden, "ground truth") price
elasticity, so a working engine should visibly start avoiding price
increases for the elastic products and grow bolder with the inelastic ones
-- which is exactly what you'd want to show off in an interview.

This module ONLY writes rows directly (bulk INSERTs) rather than going
through engine/ingestion.py's per-row validation+commit path. That's a
deliberate performance choice for seeding thousands of rows at once; real,
live data still always enters through ingestion.py (see api.py). The data
shapes are identical either way.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, timedelta
import math
import random
import sqlite3

from config import PricingConfig
from engine.history import close_due_cycles

# ---------------------------------------------------------------------------
# Tire catalog building blocks (used to synthesize plausible SKUs)
# ---------------------------------------------------------------------------
BRANDS = ["Michelin", "Goodyear", "Bridgestone", "Continental", "Pirelli",
          "Falken", "Hankook", "Cooper", "Kumho", "Yokohama", "Toyo", "Nexen"]

# Each category defines: typical size strings, cost range, target margin
# range (used to pick a starting price), demand range (avg units per 3-day
# cycle at that starting price), and an elasticity range (how sharply demand
# reacts to price -- higher = more price-sensitive / "elastic").
CATEGORIES = {
    "Passenger All-Season": {
        "sizes": ["195/65R15", "205/55R16", "215/60R16", "225/45R17", "215/55R17"],
        "cost_range": (42, 85), "margin_range": (0.30, 0.45),
        "demand_range": (6, 20), "elasticity_range": (1.8, 3.2),
    },
    "Truck/SUV All-Terrain": {
        "sizes": ["265/70R17", "275/65R18", "245/75R16", "285/70R17", "255/70R18"],
        "cost_range": (95, 170), "margin_range": (0.28, 0.42),
        "demand_range": (3, 12), "elasticity_range": (1.3, 2.4),
    },
    "Performance Summer": {
        "sizes": ["225/40R18", "245/35R19", "255/35R20", "235/40R18"],
        "cost_range": (110, 230), "margin_range": (0.25, 0.38),
        "demand_range": (1, 6), "elasticity_range": (0.9, 1.7),
    },
    "Winter/Snow": {
        "sizes": ["205/60R16", "215/55R17", "225/50R18", "235/65R17"],
        "cost_range": (70, 140), "margin_range": (0.30, 0.40),
        "demand_range": (2, 9), "elasticity_range": (1.1, 2.0),
    },
    "Off-Road Mud-Terrain": {
        "sizes": ["33x12.50R17", "285/75R16", "315/70R17", "LT265/75R16"],
        "cost_range": (140, 260), "margin_range": (0.28, 0.40),
        "demand_range": (1, 5), "elasticity_range": (0.8, 1.5),
    },
}

TOTAL_HISTORY_DAYS = 150  # ~50 pricing cycles of history for established products


@dataclass
class SyntheticProduct:
    """Internal bookkeeping while we simulate one product's history. Not a
    database model -- just carries the "ground truth" demand parameters that
    generate() uses to decide how many units sell each day."""
    product_id: str
    sku: str
    name: str
    brand: str
    category: str
    cost: float
    price: float
    inventory: int
    created_at: date
    reference_price: float          # the price at which reference_demand applies
    reference_demand_per_cycle: float
    elasticity: float               # higher = demand reacts more sharply to price changes
    manual_price_steps: list = field(default_factory=list)   # [(day_offset, new_price), ...]
    major_replenishment_day: int = None
    is_dead_stock: bool = False     # old product, ~always zero demand


def _make_product_id(i: int) -> str:
    return f"TIRE-{i:04d}"


def _build_catalog(rng: random.Random, today: date) -> list[SyntheticProduct]:
    products: list[SyntheticProduct] = []
    pid_counter = 1

    def new_established(force_category=None, dead_stock=False, with_manual_steps=False, with_major_replenishment=False):
        nonlocal pid_counter
        category = force_category or rng.choice(list(CATEGORIES.keys()))
        spec = CATEGORIES[category]
        brand = rng.choice(BRANDS)
        size = rng.choice(spec["sizes"])
        cost = round(rng.uniform(*spec["cost_range"]), 2)
        margin = rng.uniform(*spec["margin_range"])
        price = round(cost / (1 - margin), 2)
        demand = 0.15 if dead_stock else rng.uniform(*spec["demand_range"])
        elasticity = rng.uniform(*spec["elasticity_range"])
        starting_inventory = 4 if dead_stock else rng.randint(30, 90)

        p = SyntheticProduct(
            product_id=_make_product_id(pid_counter),
            sku=f"{brand[:3].upper()}-{size.replace('/', '').replace('.', '')}",
            name=f"{brand} {category} {size}",
            brand=brand, category=category, cost=cost, price=price,
            inventory=starting_inventory, created_at=today - timedelta(days=TOTAL_HISTORY_DAYS),
            reference_price=price, reference_demand_per_cycle=demand, elasticity=elasticity,
            is_dead_stock=dead_stock,
        )
        pid_counter += 1

        if with_manual_steps:
            # Simulate 1-2 pre-system manual price adjustments already
            # sitting in the historical record, so the engine has more than
            # one price point to learn from even before it takes over.
            n_steps = rng.choice([1, 2])
            for _ in range(n_steps):
                day_offset = rng.randint(30, TOTAL_HISTORY_DAYS - 20)
                pct = rng.uniform(-0.06, 0.06)
                new_price = round(p.price * (1 + pct), 2) if not p.manual_price_steps else round(
                    p.manual_price_steps[-1][1] * (1 + pct), 2)
                p.manual_price_steps.append((day_offset, new_price))
            p.manual_price_steps.sort(key=lambda t: t[0])

        if with_major_replenishment:
            p.major_replenishment_day = rng.randint(50, TOTAL_HISTORY_DAYS - 30)

        return p

    # 24 established products across the catalog, spread over all categories
    for idx in range(24):
        category = list(CATEGORIES.keys())[idx % len(CATEGORIES)]
        with_steps = idx < 6          # first 6 get pre-system manual price history
        with_replen = idx in (3, 9, 15)  # a few get a deliberate major restock mid-history
        products.append(new_established(force_category=category, with_manual_steps=with_steps,
                                          with_major_replenishment=with_replen))

    # 2 "old dead stock" products: long-established, essentially zero demand.
    # This is what tests "a newly introduced product with zero sales must be
    # distinguished from an older product that has been available for a long
    # period and still has zero sales."
    for _ in range(2):
        products.append(new_established(dead_stock=True))

    # New products at different points on the eligibility boundary:
    #   - brand new today (0 completed cycles)
    #   - 1 completed cycle (still not eligible; needs 3 by default)
    #   - 2 completed cycles (still not eligible)
    #   - exactly 3 completed cycles (just became eligible)
    cycle_len = PricingConfig().cycle_length_days  # default cycle length (3 days) used to place boundary cases
    boundary_specs = [
        ("new_zero_cycles", 0),
        ("new_one_cycle", 1 * cycle_len),
        ("new_two_cycles", 2 * cycle_len),
        ("new_three_cycles", 3 * cycle_len),
    ]
    for label, days_old in boundary_specs:
        category = rng.choice(list(CATEGORIES.keys()))
        spec = CATEGORIES[category]
        brand = rng.choice(BRANDS)
        size = rng.choice(spec["sizes"])
        cost = round(rng.uniform(*spec["cost_range"]), 2)
        margin = rng.uniform(*spec["margin_range"])
        price = round(cost / (1 - margin), 2)
        demand = rng.uniform(*spec["demand_range"])
        elasticity = rng.uniform(*spec["elasticity_range"])
        p = SyntheticProduct(
            product_id=_make_product_id(pid_counter), sku=f"{brand[:3].upper()}-{size.replace('/', '').replace('.', '')}-{label[:3].upper()}",
            name=f"{brand} {category} {size} (New Arrival)", brand=brand, category=category,
            cost=cost, price=price, inventory=rng.randint(20, 50),
            created_at=today - timedelta(days=days_old),
            reference_price=price, reference_demand_per_cycle=demand, elasticity=elasticity,
        )
        pid_counter += 1
        products.append(p)

    return products


def _daily_units_sold(rng: random.Random, p: SyntheticProduct, current_price: float, day_index: int) -> int:
    """
    Ground-truth demand model: expected daily demand follows a standard
    constant-elasticity curve around the product's reference price/demand,

        demand(price) = reference_demand * (reference_price / price) ** elasticity

    then converted from "per cycle" to "per day", given mild random noise,
    and finally sampled from a Poisson distribution (the standard way to
    simulate discrete unit sales / count data).
    """
    if current_price <= 0:
        return 0
    price_ratio = p.reference_price / current_price
    demand_per_cycle = p.reference_demand_per_cycle * (price_ratio ** p.elasticity)
    demand_per_day = max(demand_per_cycle / 3.0, 0.0)

    # Mild day-to-day noise so cycles aren't perfectly smooth (real sales
    # never are), clipped so a bad draw can't go negative.
    noise = max(rng.gauss(1.0, 0.18), 0.0)
    daily_mean = demand_per_day * noise

    if daily_mean <= 0:
        return 0
    return min(_poisson(rng, daily_mean), p.inventory)


def _poisson(rng: random.Random, lam: float) -> int:
    """Knuth's algorithm -- stdlib's `random` module has no built-in Poisson
    sampler, so we implement the standard textbook approach directly rather
    than pulling in numpy just for this one function."""
    if lam <= 0:
        return 0
    l = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= l:
            return k - 1


def get_ground_truth_catalog(seed: int = 42, today: date = None) -> dict:
    """
    Rebuild (deterministically, from the same seed) the "hidden" demand
    parameters -- reference price, reference demand, elasticity -- for
    every synthetic product, WITHOUT touching the database.

    Why this exists: generate_synthetic_dataset() builds this same catalog
    internally to seed history, but then only writes the resulting rows to
    the database -- the ground-truth elasticity parameters themselves are
    not stored anywhere (deliberately: a real system never has access to a
    product's "true" demand curve, only observed history). simulate.py
    needs those hidden parameters to keep generating realistic demand for
    NEW days as the live engine changes prices going forward, so it calls
    this with the same seed/today to deterministically regenerate the exact
    same catalog rather than duplicating the generation logic.
    """
    rng = random.Random(seed)
    today = today or date.today()
    products = _build_catalog(rng, today)
    return {p.product_id: p for p in products}


# Public alias -- simulate.py uses this same ground-truth demand function
# (rather than reimplementing it) so that "how a product actually responds
# to price" is defined in exactly ONE place, whether we're seeding history
# or simulating what happens after the live engine sets a new price.
simulate_daily_demand = _daily_units_sold


def generate_synthetic_dataset(conn: sqlite3.Connection, cfg: PricingConfig, seed: int = 42, today: date = None) -> dict:
    """
    Populate `conn` with a full synthetic tire-business dataset: products,
    day-by-day sales history, inventory receipts, and a few pre-system
    manual price changes. Then rolls that history up into pricing_cycles.

    Returns a small summary dict (counts) useful for printing/logging.
    """
    rng = random.Random(seed)
    today = today or date.today()

    products = _build_catalog(rng, today)

    sales_rows = []       # bulk rows for sales_daily
    receipt_rows = []     # bulk rows for inventory_receipts
    decision_rows = []    # bulk rows for pricing_decisions (pre-system manual changes)

    for p in products:
        current_price = p.price
        current_cost = p.cost
        inventory = p.inventory
        step_queue = list(p.manual_price_steps)  # [(day_offset, new_price), ...]

        day = p.created_at
        day_index = 0
        while day < today:
            # Apply a scheduled manual price step, if this is the day for it.
            while step_queue and step_queue[0][0] == day_index:
                _, new_price = step_queue.pop(0)
                current_price = new_price
            # Apply the scheduled major replenishment, if this is the day for it.
            if p.major_replenishment_day is not None and day_index == p.major_replenishment_day:
                receipt_qty = max(int(inventory * 2.5), 60)  # guarantees is_major under default config
                receipt_rows.append((p.product_id, day.isoformat(), receipt_qty, current_cost, 1))
                inventory += receipt_qty

            # Routine restocking: once inventory gets low, top it back up
            # (this represents ordinary, NON-major replenishment -- keeps
            # products from simply running out mid-history).
            if inventory < 8 and not p.is_dead_stock:
                receipt_qty = rng.randint(30, 70)
                is_major_flag = 1 if inventory <= 0 else 0
                receipt_rows.append((p.product_id, day.isoformat(), receipt_qty, current_cost, is_major_flag))
                inventory += receipt_qty

            units = _daily_units_sold(rng, p, current_price, day_index)
            units = min(units, inventory)
            revenue = round(units * current_price, 2)
            profit = round(units * (current_price - current_cost), 2)
            sales_rows.append((p.product_id, day.isoformat(), units, current_price, current_cost, revenue, profit))
            inventory -= units

            day += timedelta(days=1)
            day_index += 1

        # Final snapshot written to the products table below.
        p.price = current_price
        p.cost = current_cost
        p.inventory = inventory

    # --- Bulk-insert products -------------------------------------------------
    now_iso = today.isoformat()
    conn.executemany(
        """
        INSERT INTO products (product_id, sku, name, brand, category, cost, current_price,
                               current_inventory, created_at, is_locked, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        [
            (p.product_id, p.sku, p.name, p.brand, p.category, p.cost, p.price,
             max(p.inventory, 0), p.created_at.isoformat(), now_iso)
            for p in products
        ],
    )

    conn.executemany(
        """
        INSERT INTO sales_daily (product_id, sale_date, units_sold, unit_price, unit_cost, revenue, profit, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [(pid, d, u, up, uc, rev, prof, now_iso) for (pid, d, u, up, uc, rev, prof) in sales_rows],
    )

    conn.executemany(
        """
        INSERT INTO inventory_receipts (product_id, received_at, quantity, unit_cost, is_major)
        VALUES (?, ?, ?, ?, ?)
        """,
        receipt_rows,
    )
    conn.commit()

    # --- Roll raw sales up into pricing_cycles for every product --------------
    total_cycles = 0
    for p in products:
        closed = close_due_cycles(conn, cfg, p.product_id, as_of=today)
        total_cycles += len(closed)

    # --- Record pre-system manual price changes as pricing_decisions ----------
    # (left with outcome=NULL -- pending -- so engine/learning.py's outcome
    # evaluator picks them up exactly like any other experiment the very
    # first time it runs.)
    for p in products:
        if not p.manual_price_steps:
            continue
        running_price = p.reference_price
        for step_num, (day_offset, new_price) in enumerate(p.manual_price_steps):
            change_date = p.created_at + timedelta(days=day_offset)
            # Find the cycle that was in progress / most recently closed right
            # before this change, to anchor the decision to a real cycle_id.
            cycle_row = conn.execute(
                """
                SELECT id FROM pricing_cycles
                WHERE product_id = ? AND end_date <= ?
                ORDER BY end_date DESC LIMIT 1
                """,
                (p.product_id, change_date.isoformat()),
            ).fetchone()
            if cycle_row is None:
                running_price = new_price
                continue
            pct_change = round((new_price - running_price) / running_price * 100, 3)
            decision_rows.append((
                p.product_id, cycle_row["id"], change_date.isoformat(),
                "INCREASE" if new_price > running_price else "DECREASE",
                running_price, new_price, pct_change,
                "Historical manual pricing adjustment (pre-system, imported from legacy records).",
                1, 1, f"seed:{p.product_id}:{step_num}",
            ))
            running_price = new_price

    if decision_rows:
        conn.executemany(
            """
            INSERT INTO pricing_decisions
                (product_id, cycle_id, decision_time, decision, price_before, price_after,
                 pct_change, reason, is_experiment, executed, execution_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            decision_rows,
        )
        conn.commit()

    return {
        "products": len(products),
        "sales_days": len(sales_rows),
        "inventory_receipts": len(receipt_rows),
        "pricing_cycles": total_cycles,
        "historical_manual_price_changes": len(decision_rows),
    }


if __name__ == "__main__":
    # Running this file directly rebuilds the database from scratch with a
    # fresh synthetic dataset -- the fastest way to reset the demo.
    from database.db import reset_database, get_connection
    from config import get_config

    reset_database()
    conn = get_connection()
    cfg = get_config(conn)
    summary = generate_synthetic_dataset(conn, cfg)
    print("Synthetic dataset generated:")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    conn.close()
