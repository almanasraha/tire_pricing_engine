"""
simulate.py
-----------------------------------------------------------------------------
Runs the pricing engine forward through many pricing cycles against the
synthetic tire dataset, so you can SEE the learning behavior the spec
describes actually happen -- not just trust that the code compiles.

WHAT THIS SCRIPT DOES, IN ORDER:
  1. Rebuilds a fresh synthetic dataset (same as running data_generator.py
     directly) -- this is the "before the AI system existed" history.
  2. Repeatedly, for `--cycles` pricing cycles:
       a. Simulates `cycle_length_days` more days of real-world demand for
          every product, where demand responds to WHATEVER PRICE THE ENGINE
          ITSELF chose last cycle (this is the live feedback loop -- the
          engine's own past decisions shape the data it learns from next).
       b. Closes the newly-completed cycle for every product.
       c. Runs a full pricing decision + execution pass for every product.
       d. Evaluates any experiments that now have enough subsequent data.
  3. Prints a report showing how a handful of "hero" products' prices and
     decisions evolved, and renders a price-trajectory chart.

Run it with:  python3 simulate.py --cycles 24
"""

from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta
import random

from config import get_config
from database.db import reset_database, get_connection
from data_generator import generate_synthetic_dataset, get_ground_truth_catalog, simulate_daily_demand
from engine.ingestion import record_daily_sale, record_inventory_receipt
from engine.history import close_due_cycles
from engine.execution import run_pricing_cycle_for_all
from engine.learning import evaluate_pending_experiments

# Hero products chosen to showcase specific behaviors in the printed report:
#   TIRE-0001 -- highly elastic passenger tire, no pre-system price history
#                -> watch the engine explore cautiously and react to what it learns
#   TIRE-0004 -- had one positive and one negative pre-system INCREASE experiment
#                -> watch reluctance kick in after repeated negative signal
#   TIRE-0023 -- low-elasticity performance tire
#                -> watch the engine grow bolder (bigger, more frequent increases)
#   TIRE-0030 -- was exactly at the new-product eligibility boundary (3 cycles)
#                -> watch it become an active, normal participant
HERO_PRODUCTS = ["TIRE-0001", "TIRE-0004", "TIRE-0023", "TIRE-0030"]

# Validated categorical colors from the dataviz skill's default palette
# (references/palette.md, light-mode slots 1-4), used for the price chart.
CHART_COLORS = {
    "TIRE-0001": "#2a78d6",  # blue
    "TIRE-0004": "#eb6834",  # orange
    "TIRE-0023": "#1baf7a",  # aqua
    "TIRE-0030": "#eda100",  # yellow
}


def run_simulation(cycles: int = 24, seed: int = 42):
    reset_database()
    conn = get_connection()
    cfg = get_config(conn)
    today = date.today()

    seed_summary = generate_synthetic_dataset(conn, cfg, seed=seed, today=today)
    print("=== Seeded historical dataset ===")
    for k, v in seed_summary.items():
        print(f"  {k}: {v}")

    ground_truth = get_ground_truth_catalog(seed=seed, today=today)
    live_rng = random.Random(seed + 1)  # a separate RNG stream for the live/forward-simulated phase

    all_product_ids = list(ground_truth.keys())
    price_trajectory = defaultdict(list)   # product_id -> [(cycle_num, price), ...]
    decision_log = []                       # every decision made during the live phase, for the report
    sim_date = today

    print(f"\n=== Running {cycles} live pricing cycles ({cfg.cycle_length_days} days each) ===")
    for cycle_num in range(1, cycles + 1):
        # --- a. Simulate `cycle_length_days` more days of real demand, at
        # whatever price the engine currently has set for each product ------------
        for _ in range(cfg.cycle_length_days):
            for product_id in all_product_ids:
                row = conn.execute(
                    "SELECT current_price, cost, current_inventory FROM products WHERE product_id = ?",
                    (product_id,),
                ).fetchone()
                current_price, cost, inventory = row["current_price"], row["cost"], row["current_inventory"]

                if inventory < 8:
                    receipt_qty = live_rng.randint(30, 70)
                    record_inventory_receipt(conn, cfg, product_id, sim_date.isoformat(), receipt_qty, cost)
                    inventory += receipt_qty

                gt = ground_truth[product_id]
                units = simulate_daily_demand(live_rng, gt, current_price, 0)
                units = min(units, inventory)
                record_daily_sale(conn, cfg, product_id, sim_date.isoformat(), units, current_price, cost)

            sim_date += timedelta(days=1)

        # --- b. Close cycles, c. decide + execute, d. evaluate experiments --------
        for product_id in all_product_ids:
            close_due_cycles(conn, cfg, product_id, as_of=sim_date)
        decisions = run_pricing_cycle_for_all(conn, cfg)
        evaluate_pending_experiments(conn, cfg)

        for d in decisions:
            decision_log.append({"cycle": cycle_num, **d})
            if d["product_id"] in HERO_PRODUCTS:
                price_trajectory[d["product_id"]].append((cycle_num, d["price_after"]))

    return conn, cfg, decision_log, price_trajectory


def print_report(conn, decision_log, price_trajectory):
    print("\n=== Decision totals across the live simulation ===")
    counts = Counter(d["decision"] for d in decision_log)
    for k in ("INCREASE", "DECREASE", "HOLD"):
        print(f"  {k}: {counts.get(k, 0)}")

    print("\n=== Experiment outcomes recorded ===")
    outcome_rows = conn.execute(
        "SELECT outcome, COUNT(*) AS n FROM pricing_decisions WHERE outcome IS NOT NULL GROUP BY outcome"
    ).fetchall()
    for r in outcome_rows:
        print(f"  {r['outcome']}: {r['n']}")

    print("\n=== Hero product trajectories ===")
    for product_id in HERO_PRODUCTS:
        product = conn.execute("SELECT name, current_price FROM products WHERE product_id = ?", (product_id,)).fetchone()
        print(f"\n{product_id} -- {product['name']}")
        rows = [d for d in decision_log if d["product_id"] == product_id]
        if not rows:
            print("  (not yet eligible for any decisions during this simulation window)")
            continue
        for d in rows:
            arrow = {"INCREASE": "^", "DECREASE": "v", "HOLD": "="}[d["decision"]]
            print(f"  cycle {d['cycle']:>2} [{arrow}] ${d['price_before']:.2f} -> ${d['price_after']:.2f}  ({d['reason'][:100]})")
        print(f"  final price: ${product['current_price']:.2f}")

    # Catalog-wide profit signal: average PER-PRODUCT-PER-CYCLE profit
    # during the seeded "before the engine existed" history (start_date <
    # today, i.e. the pre-system manual-pricing regime) vs during the
    # "live, engine-controlled" period (start_date >= today). Comparing by
    # calendar date rather than each product's own relative cycle_index
    # matters here: products were onboarded on different days, so their
    # cycle_index values aren't aligned to the same real-world dates.
    today_str = date.today().isoformat()
    before = conn.execute(
        "SELECT AVG(total_profit) AS avg_profit, COUNT(*) AS n FROM pricing_cycles WHERE start_date < ?",
        (today_str,),
    ).fetchone()
    after = conn.execute(
        "SELECT AVG(total_profit) AS avg_profit, COUNT(*) AS n FROM pricing_cycles WHERE start_date >= ?",
        (today_str,),
    ).fetchone()
    if before["n"] and after["n"]:
        delta_pct = (after["avg_profit"] - before["avg_profit"]) / abs(before["avg_profit"]) * 100
        print(
            f"\n=== Avg profit per product-cycle: pre-system manual pricing "
            f"(${before['avg_profit']:.2f}, n={before['n']}) vs live engine-controlled "
            f"(${after['avg_profit']:.2f}, n={after['n']}) => {delta_pct:+.1f}% ==="
        )


def render_chart(price_trajectory: dict, out_path: str = "simulation_price_chart.png"):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 5), dpi=150)
    fig.patch.set_facecolor("#fcfcfb")
    ax.set_facecolor("#fcfcfb")

    # Hero products span very different absolute price levels ($65 to
    # $330+), so plotting raw dollars on one shared axis would make the
    # smaller products' movements invisible next to the larger ones (see
    # the dataviz skill's anti-patterns: different-scale measures need
    # indexing to a common base, not a shared raw axis). Instead we index
    # every product to its OWN cycle-1 price = 100, so the chart reads as
    # "percent moved from where the live engine started," directly
    # comparable across every product regardless of its dollar price.
    for product_id, points in price_trajectory.items():
        if not points:
            continue
        cycles = [p[0] for p in points]
        base_price = points[0][1]
        indexed = [p[1] / base_price * 100 for p in points]
        ax.plot(cycles, indexed, color=CHART_COLORS.get(product_id, "#52514e"),
                linewidth=2, marker="o", markersize=4, label=product_id)

    ax.axhline(100, color="#d8d7d0", linewidth=1, linestyle="--", zorder=0)
    ax.set_title("Price trajectory under live engine control (indexed to cycle 1 = 100)",
                 color="#0b0b0b", fontsize=13, loc="left")
    ax.set_xlabel("Pricing cycle", color="#52514e")
    ax.set_ylabel("Price index (cycle 1 = 100)", color="#52514e")
    ax.tick_params(colors="#52514e")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color("#d8d7d0")
    ax.grid(axis="y", color="#eceae4", linewidth=1)
    ax.legend(frameon=False, labelcolor="#0b0b0b")

    fig.tight_layout()
    fig.savefig(out_path, facecolor=fig.get_facecolor())
    print(f"\nSaved price trajectory chart to {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulate the pricing engine over many live cycles.")
    parser.add_argument("--cycles", type=int, default=24, help="Number of live pricing cycles to simulate.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility.")
    parser.add_argument("--no-chart", action="store_true", help="Skip rendering the PNG chart.")
    args = parser.parse_args()

    conn, cfg, decision_log, price_trajectory = run_simulation(cycles=args.cycles, seed=args.seed)
    print_report(conn, decision_log, price_trajectory)
    if not args.no_chart:
        render_chart(price_trajectory)
    conn.close()
