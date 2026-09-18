-- =============================================================================
-- schema.sql
-- -----------------------------------------------------------------------------
-- This is the full database schema for the dynamic pricing engine.
--
-- WHY SQLite for the prototype: the spec explicitly says "the prototype may
-- use spreadsheets or similar tools, but the production system should be
-- designed around a proper database and APIs." SQLite gives us a *real* SQL
-- database (so the schema, queries, and constraints are all genuine and would
-- port to Postgres/MySQL almost unchanged) without requiring you to stand up
-- a database server just to run/demo this for an interview. Swapping the
-- connection string in database/db.py for a Postgres URL is the only change
-- needed to move this to production.
--
-- Every table below maps directly to a requirement in the spec (comments
-- point back to the relevant paragraph) so it's easy to explain *why* each
-- column exists, not just what it stores.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- products
-- One row per SKU/product. This is the "master record" the rest of the system
-- hangs off of. Note there is NO per-product pricing *rule* stored here -- the
-- spec is explicit that the system should not require "products or pricing
-- rules to be manually entered individually." All the behavioral rules live
-- in config.py / the config table, applied uniformly (with a couple of
-- optional per-product overrides for real-world exceptions).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    product_id              TEXT PRIMARY KEY,   -- unique product_id, per spec's "Core Concept"
    sku                     TEXT NOT NULL,
    name                    TEXT NOT NULL,
    brand                   TEXT,
    category                TEXT,               -- e.g. "Passenger", "Truck/SUV", "Performance"
    cost                    REAL NOT NULL,       -- current unit cost (what we pay per tire)
    current_price           REAL NOT NULL,       -- current selling price
    current_inventory       INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL,       -- when the product FIRST entered the system --
                                                  -- this is what lets us tell "brand new product,
                                                  -- zero sales" apart from "old product, zero sales"
    is_locked                INTEGER NOT NULL DEFAULT 0,   -- 1 = excluded from automatic pricing (manual override)
    min_margin_pct_override REAL,                -- optional per-product minimum margin; NULL = use global config
    max_move_pct_override   REAL,                -- optional per-product max price move per cycle; NULL = use global config
    baseline_reset_at       TEXT,                -- last time a major replenishment reset this product's
                                                  -- "performance baseline" (see inventory_receipts below)
    updated_at               TEXT NOT NULL
);

-- -----------------------------------------------------------------------------
-- sales_daily
-- Raw, immutable, day-by-day transactional facts as they would arrive from
-- the company's existing POS/e-commerce system. Nothing in this table is ever
-- edited after insert -- it's the ground truth the rest of the system is
-- computed from, which is what makes the whole pipeline auditable.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sales_daily (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id     TEXT NOT NULL REFERENCES products(product_id),
    sale_date      TEXT NOT NULL,     -- ISO date, e.g. "2026-09-14"
    units_sold     INTEGER NOT NULL CHECK (units_sold >= 0),
    unit_price     REAL NOT NULL CHECK (unit_price >= 0),   -- price actually charged that day
    unit_cost      REAL NOT NULL CHECK (unit_cost >= 0),    -- cost basis that day (cost can drift over time)
    revenue        REAL NOT NULL,     -- units_sold * unit_price, stored (not recomputed) so history is stable
    profit         REAL NOT NULL,     -- units_sold * (unit_price - unit_cost)
    ingested_at    TEXT NOT NULL      -- when OUR system received/validated this row (for data-quality auditing)
);
CREATE INDEX IF NOT EXISTS idx_sales_daily_product_date ON sales_daily(product_id, sale_date);

-- -----------------------------------------------------------------------------
-- inventory_receipts
-- Every time inventory is replenished. "is_major" flags a receipt that is
-- large relative to the product's recent inventory levels -- this is what the
-- spec calls "Large inventory replenishments may also change the meaning of
-- historical performance," and it's what triggers a baseline reset so the
-- engine doesn't keep judging new stock against pre-replenishment behavior.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inventory_receipts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id     TEXT NOT NULL REFERENCES products(product_id),
    received_at    TEXT NOT NULL,
    quantity       INTEGER NOT NULL CHECK (quantity > 0),
    unit_cost      REAL NOT NULL,
    is_major       INTEGER NOT NULL DEFAULT 0   -- computed at ingest time, see engine/eligibility.py
);

-- -----------------------------------------------------------------------------
-- pricing_cycles
-- The aggregated performance of ONE product over ONE pricing cycle (3 days by
-- default, configurable). This is the "Performance History" stage of the
-- pipeline: raw sales_daily rows get rolled up into cycles, and it's cycles
-- -- not individual days -- that the decision engine reasons about. This is
-- also what lets us count "how many full cycles has this product completed"
-- for new-product eligibility.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pricing_cycles (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id            TEXT NOT NULL REFERENCES products(product_id),
    cycle_index           INTEGER NOT NULL,   -- 0, 1, 2, ... sequential per product
    start_date            TEXT NOT NULL,
    end_date              TEXT NOT NULL,
    price_at_cycle_start  REAL NOT NULL,
    units_sold            INTEGER NOT NULL DEFAULT 0,
    revenue               REAL NOT NULL DEFAULT 0,
    total_profit          REAL NOT NULL DEFAULT 0,
    avg_unit_cost         REAL,
    status                TEXT NOT NULL DEFAULT 'complete',  -- 'complete' | 'pending'
    is_post_replenishment INTEGER NOT NULL DEFAULT 0,        -- 1 if this cycle started a new baseline window
    UNIQUE(product_id, cycle_index)
);

-- -----------------------------------------------------------------------------
-- pricing_decisions
-- The single audit log for EVERYTHING the engine ever decided, whether or not
-- it changed the price. This directly satisfies two Safety & Control bullets:
-- "Complete history of every recommendation and executed change" and
-- "Ability to explain why a price was changed." When decision != 'HOLD' this
-- row IS the experiment record the spec asks for ("Every actual price change
-- must be recorded as an experiment"): old price, new price, and -- once
-- enough time has passed -- the outcome get filled in on the same row.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pricing_decisions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id           TEXT NOT NULL REFERENCES products(product_id),
    cycle_id             INTEGER NOT NULL REFERENCES pricing_cycles(id),
    decision_time        TEXT NOT NULL,
    decision             TEXT NOT NULL CHECK (decision IN ('INCREASE', 'DECREASE', 'HOLD')),
    price_before          REAL NOT NULL,
    price_after           REAL NOT NULL,     -- equals price_before when decision = 'HOLD'
    pct_change            REAL NOT NULL,      -- signed; 0 for HOLD
    reason                TEXT NOT NULL,      -- human-readable explanation (see engine/decision.py)
    is_experiment          INTEGER NOT NULL,   -- 1 when decision != 'HOLD'
    outcome                TEXT,               -- NULL (pending) | 'positive' | 'negative' | 'neutral' | 'inconclusive'
    outcome_evaluated_at   TEXT,
    executed                INTEGER NOT NULL DEFAULT 0,  -- 1 once the price change was actually applied to `products`
    execution_key           TEXT UNIQUE          -- "<product_id>:<cycle_id>" -- a DB-enforced guard against
                                                  -- executing the same cycle's decision twice (duplicate prevention)
);
CREATE INDEX IF NOT EXISTS idx_decisions_product ON pricing_decisions(product_id, decision_time);

-- -----------------------------------------------------------------------------
-- config
-- Centralized, DB-backed configuration so parameters can change "without
-- modifying the underlying program" (per spec). config.py defines the
-- defaults and typed accessors; this table lets an operator (or the API)
-- override any of them at runtime. Rows here take precedence over the
-- hard-coded defaults.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS config (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    description  TEXT
);
