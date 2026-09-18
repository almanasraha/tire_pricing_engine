"""
tests/test_engine.py
-----------------------------------------------------------------------------
Unit/integration tests for the pricing engine's safety-critical behavior.

Deliberately built on the STANDARD LIBRARY's `unittest` (no pytest) so this
suite runs anywhere Python itself runs, with zero extra dependencies to
install -- appropriate for a small internal engine like this one.

Run with:   python3 -m unittest discover -v
        or: python3 tests/test_engine.py

Each test gets its own throwaway SQLite file (via tempfile), so tests never
interact with each other or with the "real" demo database that
data_generator.py / simulate.py populate.
"""

import os
import tempfile
import unittest
from datetime import date, timedelta

from config import PricingConfig
from database.db import get_connection
from engine.eligibility import evaluate_eligibility
from engine.execution import execute_decision
from engine.learning import evaluate_pending_experiments
from engine.safety import clamp_price_move, min_allowed_price
from models import PricingDecision


class EngineTestCase(unittest.TestCase):
    """Common fixture: a fresh, isolated SQLite database per test."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.db_path)  # get_connection creates the schema fresh
        self.conn = get_connection(self.db_path)
        self.cfg = PricingConfig()  # defaults: 3-day cycle, 15% min margin, 0.3-1% move, 3 cycles required

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    # ---- fixture helpers ----------------------------------------------------

    def _make_product(self, product_id="P1", cost=100.0, price=140.0, inventory=50,
                       created_days_ago=200, locked=0, baseline_reset_days_ago=None,
                       min_margin_override=None, max_move_override=None):
        created_at = (date.today() - timedelta(days=created_days_ago)).isoformat()
        baseline_reset_at = (
            (date.today() - timedelta(days=baseline_reset_days_ago)).isoformat()
            if baseline_reset_days_ago is not None else None
        )
        self.conn.execute(
            """
            INSERT INTO products (product_id, sku, name, brand, category, cost, current_price,
                                   current_inventory, created_at, is_locked, min_margin_pct_override,
                                   max_move_pct_override, baseline_reset_at, updated_at)
            VALUES (?, ?, ?, 'TestBrand', 'TestCategory', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (product_id, f"SKU-{product_id}", f"Test Product {product_id}", cost, price, inventory,
             created_at, locked, min_margin_override, max_move_override, baseline_reset_at, created_at),
        )
        self.conn.commit()

    def _make_cycle(self, product_id, cycle_index, price, units, revenue, profit, days_ago=None):
        """Insert a pricing_cycles row directly (bypassing close_due_cycles) so
        tests can construct precise, controlled scenarios."""
        if days_ago is None:
            days_ago = (50 - cycle_index) * self.cfg.cycle_length_days
        start = date.today() - timedelta(days=days_ago)
        end = start + timedelta(days=self.cfg.cycle_length_days - 1)
        cur = self.conn.execute(
            """
            INSERT INTO pricing_cycles
                (product_id, cycle_index, start_date, end_date, price_at_cycle_start,
                 units_sold, revenue, total_profit, avg_unit_cost, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'complete')
            """,
            (product_id, cycle_index, start.isoformat(), end.isoformat(), price, units, revenue, profit),
        )
        self.conn.commit()
        return cur.lastrowid


# =============================================================================
# Safety: minimum margin floor and maximum move-per-cycle
# =============================================================================

class TestSafety(EngineTestCase):

    def test_min_allowed_price_matches_margin_formula(self):
        # cost=100, 15% margin -> price such that (price-cost)/price == 0.15
        floor = min_allowed_price(cost=100.0, min_margin_pct=15.0)
        self.assertAlmostEqual(floor, 117.65, places=2)
        self.assertAlmostEqual((floor - 100.0) / floor * 100, 15.0, places=1)

    def test_decrease_clamped_by_max_move_before_margin_matters(self):
        self._make_product("P1", cost=100.0, price=150.0)
        product = self.conn.execute("SELECT * FROM products WHERE product_id='P1'").fetchone()
        # Candidate wants to drop to 110, but max_price_move_pct=1.0 only
        # allows moving 1% (=1.50) away from 150 -> should clamp to 148.50,
        # nowhere near the (much lower) margin floor.
        result = clamp_price_move(current_price=150.0, candidate_price=110.0, cost=100.0,
                                   cfg=self.cfg, product_row=product)
        self.assertTrue(result.was_clamped_by_max_move)
        self.assertFalse(result.was_clamped_by_margin)
        self.assertAlmostEqual(result.final_price, 148.5, places=2)
        self.assertFalse(result.blocked)

    def test_decrease_clamped_by_margin_floor(self):
        self._make_product("P1", cost=100.0, price=200.0)
        product = self.conn.execute("SELECT * FROM products WHERE product_id='P1'").fetchone()
        loose_cfg = PricingConfig(max_price_move_pct=20.0, min_margin_pct=45.0)  # floor = 100/0.55 = 181.82
        result = clamp_price_move(current_price=200.0, candidate_price=170.0, cost=100.0,
                                   cfg=loose_cfg, product_row=product)
        self.assertTrue(result.was_clamped_by_margin)
        self.assertAlmostEqual(result.final_price, 181.82, places=2)
        self.assertFalse(result.blocked)

    def test_decrease_fully_blocked_when_already_at_margin_floor(self):
        self._make_product("P1", cost=100.0, price=181.82)  # already sitting at the 45% margin floor
        product = self.conn.execute("SELECT * FROM products WHERE product_id='P1'").fetchone()
        cfg = PricingConfig(max_price_move_pct=20.0, min_margin_pct=45.0)
        result = clamp_price_move(current_price=181.82, candidate_price=160.0, cost=100.0,
                                   cfg=cfg, product_row=product)
        self.assertTrue(result.blocked)
        self.assertIn("margin", result.block_reason.lower())

    def test_increase_never_exceeds_configured_max_move(self):
        self._make_product("P1", cost=50.0, price=100.0)
        product = self.conn.execute("SELECT * FROM products WHERE product_id='P1'").fetchone()
        # Ask for a wildly large increase; the engine must never grant more
        # than cfg.max_price_move_pct in a single cycle, no matter what.
        result = clamp_price_move(current_price=100.0, candidate_price=500.0, cost=50.0,
                                   cfg=self.cfg, product_row=product)
        self.assertLessEqual(result.final_pct_change, self.cfg.max_price_move_pct + 1e-6)
        self.assertTrue(result.was_clamped_by_max_move)


# =============================================================================
# Eligibility: new-product protection, locks, stale zero-sales, replenishment
# =============================================================================

class TestEligibility(EngineTestCase):

    def test_new_product_not_eligible_before_required_cycles(self):
        self._make_product("NEW1", created_days_ago=5)
        for i in range(2):  # only 2 of the required 3 cycles completed
            self._make_cycle("NEW1", i, price=140.0, units=3, revenue=420.0, profit=120.0)
        result = evaluate_eligibility(self.conn, self.cfg, "NEW1")
        self.assertFalse(result.eligible)
        self.assertTrue(result.is_new_product)

    def test_product_eligible_at_exactly_the_required_cycle_count(self):
        self._make_product("NEW2", created_days_ago=10)
        for i in range(self.cfg.min_cycles_before_eligible):  # exactly 3
            self._make_cycle("NEW2", i, price=140.0, units=3, revenue=420.0, profit=120.0)
        result = evaluate_eligibility(self.conn, self.cfg, "NEW2")
        self.assertTrue(result.eligible)

    def test_locked_product_never_eligible_regardless_of_history(self):
        self._make_product("LOCKED1", created_days_ago=300, locked=1)
        for i in range(50):
            self._make_cycle("LOCKED1", i, price=140.0, units=5, revenue=700.0, profit=200.0)
        result = evaluate_eligibility(self.conn, self.cfg, "LOCKED1")
        self.assertFalse(result.eligible)
        self.assertTrue(result.is_locked)

    def test_old_product_with_zero_sales_flagged_stale_not_new(self):
        self._make_product("DEAD1", created_days_ago=300)
        for i in range(50):
            self._make_cycle("DEAD1", i, price=140.0, units=0, revenue=0.0, profit=0.0)
        result = evaluate_eligibility(self.conn, self.cfg, "DEAD1")
        self.assertFalse(result.eligible)
        self.assertTrue(result.is_stale_zero_sales)
        self.assertFalse(result.is_new_product)  # the key distinction the spec calls for

    def test_new_product_with_zero_sales_is_not_flagged_stale(self):
        # Brand new (created 2 days ago), zero sales so far -- this must be
        # read as "too new to know," NOT "established dead stock."
        self._make_product("NEW3", created_days_ago=2)
        result = evaluate_eligibility(self.conn, self.cfg, "NEW3")
        self.assertFalse(result.eligible)
        self.assertTrue(result.is_new_product)
        self.assertFalse(result.is_stale_zero_sales)

    def test_post_replenishment_window_resets_eligibility(self):
        # Long-established product (200 days, 50 cycles of solid history),
        # but a MAJOR replenishment reset the baseline 2 days ago -- only
        # cycles since then should count toward the eligibility requirement.
        self._make_product("REPL1", created_days_ago=200, baseline_reset_days_ago=2)
        for i in range(50):
            self._make_cycle("REPL1", i, price=140.0, units=5, revenue=700.0, profit=200.0)
        result = evaluate_eligibility(self.conn, self.cfg, "REPL1")
        self.assertFalse(result.eligible)
        self.assertTrue(result.is_post_replenishment_window)


# =============================================================================
# Execution: duplicate-execution prevention
# =============================================================================

class TestExecution(EngineTestCase):

    def test_duplicate_decision_for_same_cycle_is_rejected(self):
        self._make_product("DUP1", cost=100.0, price=150.0)
        cycle_id = self._make_cycle("DUP1", 10, price=150.0, units=5, revenue=750.0, profit=250.0)

        decision = PricingDecision(
            product_id="DUP1", cycle_id=cycle_id, decision="DECREASE",
            price_before=150.0, price_after=148.5, pct_change=-1.0, reason="test decrease",
        )

        first = execute_decision(self.conn, decision)
        self.assertTrue(first["executed"])
        self.assertFalse(first["duplicate"])

        second = execute_decision(self.conn, decision)
        self.assertFalse(second["executed"])
        self.assertTrue(second["duplicate"])

        # The price must only have moved ONCE, not twice.
        row = self.conn.execute("SELECT current_price FROM products WHERE product_id='DUP1'").fetchone()
        self.assertAlmostEqual(row["current_price"], 148.5, places=2)

        # And there is exactly one audit row for this cycle's decision.
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM pricing_decisions WHERE product_id='DUP1'"
        ).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_hold_decisions_are_never_treated_as_duplicates(self):
        self._make_product("HOLD1", cost=100.0, price=150.0)
        cycle_a = self._make_cycle("HOLD1", 10, price=150.0, units=5, revenue=750.0, profit=250.0)
        cycle_b = self._make_cycle("HOLD1", 11, price=150.0, units=5, revenue=750.0, profit=250.0)

        hold_a = PricingDecision(product_id="HOLD1", cycle_id=cycle_a, decision="HOLD",
                                  price_before=150.0, price_after=150.0, pct_change=0.0, reason="hold 1")
        hold_b = PricingDecision(product_id="HOLD1", cycle_id=cycle_b, decision="HOLD",
                                  price_before=150.0, price_after=150.0, pct_change=0.0, reason="hold 2")

        r1 = execute_decision(self.conn, hold_a)
        r2 = execute_decision(self.conn, hold_b)
        self.assertFalse(r1["duplicate"])
        self.assertFalse(r2["duplicate"])
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM pricing_decisions WHERE product_id='HOLD1'"
        ).fetchone()["n"]
        self.assertEqual(count, 2)


# =============================================================================
# Learning: experiment outcome classification (positive/negative/neutral/inconclusive)
# =============================================================================

class TestLearning(EngineTestCase):

    def _seed_experiment(self, product_id, prior_profits, subsequent_profits, units_per_cycle=5):
        """Build a product with `prior_profits` cycles before a price change,
        one experiment decision row, and `subsequent_profits` cycles after it."""
        self._make_product(product_id, cost=100.0, price=150.0)
        idx = 0
        for profit in prior_profits:
            self._make_cycle(product_id, idx, price=150.0, units=units_per_cycle,
                              revenue=units_per_cycle * 150.0, profit=profit)
            idx += 1
        anchor_cycle_id = self.conn.execute(
            "SELECT id FROM pricing_cycles WHERE product_id=? ORDER BY cycle_index DESC LIMIT 1", (product_id,)
        ).fetchone()["id"]

        self.conn.execute(
            """
            INSERT INTO pricing_decisions
                (product_id, cycle_id, decision_time, decision, price_before, price_after,
                 pct_change, reason, is_experiment, executed, execution_key)
            VALUES (?, ?, ?, 'INCREASE', 150.0, 151.5, 1.0, 'test experiment', 1, 1, ?)
            """,
            (product_id, anchor_cycle_id, date.today().isoformat(), f"seed:{product_id}"),
        )
        self.conn.commit()

        for profit in subsequent_profits:
            self._make_cycle(product_id, idx, price=151.5, units=units_per_cycle,
                              revenue=units_per_cycle * 151.5, profit=profit)
            idx += 1

    def test_negative_outcome_detected(self):
        self._seed_experiment("EXP_NEG", prior_profits=[200, 210], subsequent_profits=[100, 90])
        results = evaluate_pending_experiments(self.conn, self.cfg, "EXP_NEG")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["outcome"], "negative")

    def test_positive_outcome_detected(self):
        self._seed_experiment("EXP_POS", prior_profits=[200, 210], subsequent_profits=[260, 270])
        results = evaluate_pending_experiments(self.conn, self.cfg, "EXP_POS")
        self.assertEqual(results[0]["outcome"], "positive")

    def test_neutral_outcome_within_band(self):
        # neutral_profit_band_pct defaults to 3.0% -- a ~1% profit change
        # should be classified as noise, not a real effect.
        self._seed_experiment("EXP_NEUT", prior_profits=[200, 200], subsequent_profits=[201, 200])
        results = evaluate_pending_experiments(self.conn, self.cfg, "EXP_NEUT")
        self.assertEqual(results[0]["outcome"], "neutral")

    def test_inconclusive_when_no_sales_signal(self):
        self._seed_experiment("EXP_INC", prior_profits=[0], subsequent_profits=[0, 0], units_per_cycle=0)
        results = evaluate_pending_experiments(self.conn, self.cfg, "EXP_INC")
        self.assertEqual(results[0]["outcome"], "inconclusive")

    def test_pending_experiment_not_evaluated_until_enough_cycles_elapse(self):
        # Only ONE subsequent cycle exists, but cfg requires 2 -- should
        # still be pending (not scored) after this call.
        self._seed_experiment("EXP_PEND", prior_profits=[200, 200], subsequent_profits=[50])
        results = evaluate_pending_experiments(self.conn, self.cfg, "EXP_PEND")
        self.assertEqual(len(results), 0)
        row = self.conn.execute(
            "SELECT outcome FROM pricing_decisions WHERE product_id='EXP_PEND'"
        ).fetchone()
        self.assertIsNone(row["outcome"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
