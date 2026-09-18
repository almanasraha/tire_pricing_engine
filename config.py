"""
config.py
-----------------------------------------------------------------------------
Centralized configuration for the pricing engine.

WHY THIS FILE EXISTS AS ITS OWN MODULE:
The spec says: "The configuration should be centralized so that parameters
such as cycle length, minimum margin, required history, and maximum price
movement can be changed without modifying the underlying program."

We satisfy that two ways at once:
  1. Every tunable lives in ONE place (this file) instead of being scattered
     as magic numbers through the engine code.
  2. Every value can ALSO be overridden at runtime from the `config` table in
     the database (see `get_config()` below), so an operator (or a future
     admin UI / API call) can retune the engine without a code deploy.

If a key is present in the database `config` table, that value wins over the
default below. If not, the default here is used. That's the whole override
mechanism -- deliberately simple so it's easy to reason about and audit.
"""

from dataclasses import dataclass, fields
import sqlite3


@dataclass
class PricingConfig:
    """
    A typed bag of every configurable parameter the engine uses.

    Using a dataclass (rather than a raw dict) means every place in the
    codebase that reads `cfg.min_margin_pct` gets IDE autocomplete and a
    type-checked value, instead of a string key that could be misspelled.
    """

    # --- Cycle definition -----------------------------------------------
    # "Performance should be evaluated in defined periods, initially 3-day
    # pricing cycles."
    cycle_length_days: int = 3

    # --- Price movement limits --------------------------------------------
    # "Price changes should normally be small and controlled. Initial
    # configurable limits are approximately 0.3%-1% per pricing cycle."
    min_price_move_pct: float = 0.3   # smallest change worth making (below this, just HOLD)
    max_price_move_pct: float = 1.0   # hard ceiling on |% change| per cycle, per product

    # --- Profitability floor ------------------------------------------------
    # "The engine must enforce a minimum profit margin and must never
    # recommend a price below the permitted profitability threshold."
    # Expressed as a fraction of price that must be profit: margin = (price - cost) / price
    min_margin_pct: float = 15.0   # percent; e.g. 15.0 means price must be >= cost / (1 - 0.15)

    # --- New-product protection ------------------------------------------
    # "Initially, a product should complete approximately 3 full pricing
    # cycles before becoming eligible for automatic price changes. This
    # must be configurable."
    min_cycles_before_eligible: int = 3

    # How many days of zero sales, for a product that is otherwise old
    # enough to have a track record, before we no longer treat "zero sales"
    # as simply "too new to have data" and instead flag it for review.
    stale_zero_sales_days: int = 30

    # --- Replenishment detection --------------------------------------------
    # "The system should recognize significant inventory receipts and,
    # where appropriate, temporarily allow the product to establish a new
    # performance baseline."
    # A receipt is "major" if it adds at least this many percent on top of
    # the inventory the product had right before the receipt (e.g. 200 means
    # the receipt at least tripled on-hand inventory), OR the product was
    # effectively out of stock (see engine/eligibility.py for the exact rule).
    major_replenishment_pct_increase: float = 200.0
    # After a major replenishment, how many fresh cycles must accumulate
    # before pricing decisions resume trusting the (reset) history again.
    post_replenishment_cycles_required: int = 3

    # --- Experiment / learning evaluation ------------------------------------
    # How many cycles of "subsequent" data to wait for after a price change
    # before judging whether it was positive/negative/neutral/inconclusive.
    experiment_evaluation_cycles: int = 2
    # A profit change smaller than this (as a percent of prior profit) counts
    # as "neutral" rather than positive/negative -- avoids over-reacting to noise.
    neutral_profit_band_pct: float = 3.0
    # If a product has had this many consecutive NEGATIVE outcomes in a given
    # direction (e.g. repeated failed increases), the engine becomes reluctant
    # to try that direction again until conditions change.
    consecutive_negative_reluctance_threshold: int = 2

    # --- Data validation -----------------------------------------------------
    # Guardrails used to reject obviously bad incoming rows before they ever
    # reach the learning/decision logic ("Validation of incoming sales/
    # inventory data").
    max_plausible_daily_units: int = 500   # a single day selling more than this for one SKU is suspect
    max_plausible_unit_price: float = 20000.0

    @classmethod
    def field_names(cls) -> list[str]:
        return [f.name for f in fields(cls)]


DEFAULT_CONFIG = PricingConfig()


def get_config(conn: sqlite3.Connection) -> PricingConfig:
    """
    Build a PricingConfig by layering database overrides on top of the
    hard-coded defaults.

    Why layer instead of require every key in the DB: it means the `config`
    table can start EMPTY (or only contain the one or two values an operator
    actually wants to change) and everything else just falls back to a
    sensible, documented default -- there's no risk of the system breaking
    because someone forgot to seed an obscure key.
    """
    cfg = PricingConfig()  # start from defaults
    rows = conn.execute("SELECT key, value FROM config").fetchall()
    overrides = {key: value for key, value in rows}

    for name in PricingConfig.field_names():
        if name in overrides:
            raw_value = overrides[name]
            current_default = getattr(cfg, name)
            # Cast the stored string back to whatever type the default is
            # (int vs float) so callers don't have to worry about types.
            caster = type(current_default)
            setattr(cfg, name, caster(raw_value))

    return cfg


def set_config_value(conn: sqlite3.Connection, key: str, value, description: str = "") -> None:
    """
    Write (or overwrite) one config override into the database.

    This is the mechanism that fulfills "can be changed without modifying
    the underlying program": calling this (directly, or via the
    PUT /config/{key} API endpoint in api.py) changes engine behavior on the
    next cycle, with no code deploy.
    """
    if key not in PricingConfig.field_names():
        raise ValueError(f"Unknown config key: {key!r}. Valid keys: {PricingConfig.field_names()}")

    conn.execute(
        """
        INSERT INTO config (key, value, description)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                        description = COALESCE(NULLIF(excluded.description, ''), config.description)
        """,
        (key, str(value), description),
    )
    conn.commit()
