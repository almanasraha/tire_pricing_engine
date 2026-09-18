"""
models.py
-----------------------------------------------------------------------------
Small, typed data structures passed between engine modules.

These are NOT database tables (those live in database/schema.sql) -- they're
plain in-memory objects used so functions can return something more
descriptive than a bare tuple or dict. Using @dataclass gives us free
__init__/__repr__ and keeps each function's inputs/outputs self-documenting,
which matters a lot for an interview reviewer skimming this code.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class CycleMetrics:
    """The aggregated performance of one product over one pricing cycle.
    Mirrors a row of pricing_cycles, but as a typed object for engine code."""
    id: int                # the pricing_cycles.id surrogate key (FK target for pricing_decisions.cycle_id)
    product_id: str
    cycle_index: int
    start_date: str
    end_date: str
    price_at_cycle_start: float
    units_sold: int
    revenue: float
    total_profit: float
    avg_unit_cost: Optional[float]

    @property
    def avg_selling_price(self) -> Optional[float]:
        """Revenue-weighted average price actually realized during the cycle.
        Falls back to the cycle's starting price if nothing sold, since
        there's no transaction price to average in that case."""
        if self.units_sold > 0:
            return self.revenue / self.units_sold
        return self.price_at_cycle_start


@dataclass
class EligibilityResult:
    """Whether a product is allowed to receive an automatic price change
    this cycle, and -- importantly for auditability -- WHY.

    The extra diagnostic fields aren't just informational: decision.py reads
    `relevant_cycles_start_index` to know which slice of a product's cycle
    history should count as its "current baseline" (all of it normally, or
    only cycles since the last major replenishment)."""
    eligible: bool
    reason: str
    is_locked: bool = False
    is_post_replenishment_window: bool = False
    is_new_product: bool = False            # hasn't completed min_cycles_before_eligible yet
    is_stale_zero_sales: bool = False       # old enough to have data, but genuinely has none
    cycles_available: int = 0               # cycles completed since creation (or since baseline reset)
    relevant_cycles_start_index: int = 0    # first cycle_index the decision engine should consider "current"


@dataclass
class PricingDecision:
    """The engine's output for one product for one cycle: what to do, and
    the human-readable justification the spec requires ("Ability to explain
    why a price was changed")."""
    product_id: str
    cycle_id: int
    decision: str          # 'INCREASE' | 'DECREASE' | 'HOLD'
    price_before: float
    price_after: float
    pct_change: float
    reason: str
