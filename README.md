# Tire Pricing Engine

An AI-assisted dynamic pricing engine, built to the "AI-Assisted Dynamic
Pricing System — Concept & Requirements" spec, as a module intended for
integration with **usawheelstires.com**. It's written as a general-purpose
dynamic pricing platform (per the spec's closing requirement), with tires
as the first concrete product category.

This README is the map of the project: what each piece does, why it's
built the way it is, how to run it, and how it would actually get wired
into the website. The code itself is written with the same goal — every
file's docstring and most functions carry a "why," not just a "what," so
you can walk through any part of this in an interview and explain the
reasoning, not just recite what it does.

## Quick start

```bash
pip install -r requirements.txt

# 1. Generate a synthetic tire-business dataset (products, sales history,
#    inventory receipts) and seed the database from scratch.
python3 data_generator.py

# 2. Run the engine forward through many live pricing cycles and see the
#    learning behavior play out (prints a report + saves a price chart).
python3 simulate.py --cycles 24

# 3. Run the test suite (no extra dependencies needed -- stdlib unittest).
python3 -m unittest discover -v

# 4. Run the HTTP module that a website would actually integrate with.
python3 api.py
# -> now serving on http://localhost:8000, e.g.:
curl http://localhost:8000/products/TIRE-0001/recommendation
```

## Why this architecture

The spec calls for the pipeline to stay "conceptually separate... so that
pricing decisions are auditable and the learning system can be improved
without compromising transactional systems." The codebase mirrors that
literally — one module per stage:

```
Data Collection        engine/ingestion.py    validates + stores raw sales/inventory events
        |
Performance History    engine/history.py      rolls raw daily sales into 3-day pricing cycles
        |
Historical Learning     engine/learning.py     builds the price->profit profile; scores past
        |                                      experiments as positive/negative/neutral/inconclusive
Pricing Decision        engine/decision.py     INCREASE / DECREASE / HOLD + a human-readable reason
        |
Price Execution         engine/execution.py    applies the change, logs the audit row, blocks duplicates
        |
Outcome Evaluation  ---> feeds back into engine/learning.py on a later cycle
        \_____________________________________/
                    Learning loop
```

Two more modules cut across every stage:

- **`engine/eligibility.py`** — the gate every product must pass before the
  decision engine will touch its price: not locked, enough history, not
  "old dead stock," not mid-replenishment-reset.
- **`engine/safety.py`** — the hard numeric limits (minimum margin, maximum
  move per cycle) that clamp whatever the decision logic wants to do. These
  are pure, dependency-free functions specifically so they're trivial to
  unit test and to point to as "this is the code that makes the system
  safe," independent of how clever the decision logic itself is.

`config.py` centralizes every tunable (cycle length, minimum margin,
required history, max move, etc.) with database-backed overrides, so any
of them can change at runtime with no code deploy — see `PUT /config/<key>`.

## File guide

| File | Stage / role |
|---|---|
| `database/schema.sql` | Full SQL schema, with comments tying every table/column back to a specific spec requirement |
| `database/db.py` | Connection handling; the one place that would change to move from SQLite to Postgres/MySQL |
| `config.py` | Centralized, runtime-overridable configuration |
| `models.py` | Small typed dataclasses passed between engine modules |
| `engine/ingestion.py` | Data Collection — validates and stores sales/inventory events |
| `engine/history.py` | Performance History — rolls sales into pricing cycles |
| `engine/eligibility.py` | New-product protection, locks, stale-zero-sales detection, replenishment baseline resets |
| `engine/learning.py` | Historical price/profit profile + experiment outcome scoring + directional reluctance |
| `engine/decision.py` | The INCREASE/DECREASE/HOLD decision tree, with a human-readable `reason` |
| `engine/safety.py` | Pure margin-floor and max-move clamp functions |
| `engine/execution.py` | Applies decisions, writes the audit log, prevents duplicate execution |
| `data_generator.py` | Builds a realistic synthetic tire catalog + sales history for demo/testing |
| `simulate.py` | Runs the engine forward through many live cycles; prints a report + chart |
| `api.py` | The Flask HTTP module meant to be integrated into the website |
| `tests/test_engine.py` | Unit tests for safety, eligibility, duplicate prevention, and outcome classification |

## How each spec requirement is met

- **"Learn over time what price produces the best total profit"** —
  `engine/learning.build_price_performance_profile()` builds the exact
  "priced around $180 → ~5 units → $150 profit" picture per product;
  `engine/decision.decide_price()` only moves toward a price the data
  actually supports.
- **"Operate automatically... rather than requiring products or pricing
  rules to be manually entered individually"** — a product enters the
  system via one `POST /products` call (which a catalog-sync job would call
  automatically) and immediately participates in every future
  `POST /pricing/tick` with zero further setup. No per-product pricing
  rules exist anywhere in the schema.
- **"3-day pricing cycles"** — `cfg.cycle_length_days`, default 3, and the
  only place cycle length is used is `engine/history.py`'s window math.
- **INCREASE / DECREASE / HOLD with 0.3%–1% moves, minimum margin
  enforced** — `engine/decision.py` picks a direction and strength;
  `engine/safety.clamp_price_move()` is the single choke point that
  enforces both limits no matter what the decision logic asked for.
- **"Total profit and sales behavior together"** — every comparison in
  `decide_price()` is on `avg_profit` (units × margin already combined),
  never on price or unit volume alone.
- **New-product protection, configurable cycle count, distinguishing new
  zero-sales from old dead stock** — `engine/eligibility.py`.
- **Major inventory replenishment resets the baseline** —
  `engine/ingestion.record_inventory_receipt()` flags "major" receipts and
  stamps `products.baseline_reset_at`; `eligibility.py` and `learning.py`
  both treat cycles since that reset as the current baseline.
- **Every price change recorded as an experiment; outcome evaluated as
  positive/negative/neutral/inconclusive; this learning influences future
  decisions** — `pricing_decisions` rows ARE the experiments;
  `engine/learning.evaluate_pending_experiments()` scores them once enough
  subsequent cycles exist; `get_directional_reluctance()` is what makes the
  engine gun-shy about a direction with a losing track record for a
  specific product (see `simulate.py`'s printed report for this playing out).
- **Safety and Control bullet list** — see the table below.
- **Centralized configuration, changeable without modifying the program** —
  `config.py` + `PUT /config/<key>`.
- **Thousands to hundreds of thousands of SKUs, products enter automatically**
  — see "Scaling to production" below.
- **Conceptually separate pipeline stages, auditable** — the module
  boundaries described above; `GET /products/<id>/history` returns the
  full audit trail (every cycle, every recommendation, every reason).

| Safety & Control bullet | Where it's implemented |
|---|---|
| Configurable minimum margin | `config.min_margin_pct`, `engine/safety.min_allowed_price()` |
| Maximum price movement per cycle | `config.max_price_move_pct`, `engine/safety.clamp_price_move()` |
| Minimum historical data before pricing begins | `config.min_cycles_before_eligible`, `engine/eligibility.py` |
| Protection for new products | `engine/eligibility.py` |
| Recognition of major inventory replenishment | `engine/ingestion.record_inventory_receipt()`, `products.baseline_reset_at` |
| Lock individual products | `products.is_locked`, `POST /products/<id>/lock` |
| Validation of incoming sales/inventory data | `engine/ingestion.py`'s `ValidationError` checks |
| Prevention of duplicate price executions | `pricing_decisions.execution_key` UNIQUE constraint, `engine/execution.py` |
| Complete history of every recommendation | Every decision (including HOLD) is written to `pricing_decisions` |
| Ability to explain why a price was changed | `PricingDecision.reason` on every single decision |

## Integrating with usawheelstires.com

This engine runs as its **own small HTTP service** (`api.py`), independent
of the website's own stack (whatever it is), so it can be redeployed or
retrained without touching checkout/inventory code. In production:

1. A sync job that already knows the website's real product feed calls
   `POST /products` once per new SKU (this is the one place the spec
   allows *some* setup, and it's meant to be automatic, not a human typing
   in pricing rules).
2. Whenever a sale happens or stock arrives, the website's backend also
   calls `POST /ingest/sales` / `POST /ingest/inventory-receipt` so this
   engine's view of the world matches reality.
3. A daily scheduled job calls `POST /pricing/tick` once. That single call
   closes any finished cycles, decides + executes pricing for every
   eligible product, and scores any experiments old enough to evaluate.
4. The website reads the new price back out (either by polling
   `GET /products/<id>`, or — in a fuller build — this engine would push a
   webhook back to the website's own catalog on every executed change).
5. An admin page on the website can call `GET /products/<id>/recommendation`
   (a dry run, no side effects) and `GET /products/<id>/history` to show
   staff exactly what the engine is thinking and why, and
   `POST /products/<id>/lock` to override it for a specific SKU.

### API reference

| Method & path | Purpose |
|---|---|
| `GET /health` | Liveness check |
| `GET /products` | List products (filters: `category`, `locked`) |
| `POST /products` | Onboard a new product |
| `GET /products/<id>` | Product detail |
| `POST /products/<id>/lock` / `/unlock` | Manual pricing override |
| `POST /ingest/sales` | Record one day's sales for one product |
| `POST /ingest/inventory-receipt` | Record a restock |
| `GET /products/<id>/recommendation` | Dry-run: what would the engine decide right now |
| `POST /products/<id>/run-cycle` | Decide **and** execute for one product |
| `POST /pricing/tick` | Run the full pipeline for every product (what a daily scheduler calls) |
| `GET /products/<id>/history` | Every cycle + every recommendation ever made, with reasons |
| `GET /products/<id>/profile` | The learned price/profit relationship + directional reluctance |
| `GET /config` / `PUT /config/<key>` | Read/update centralized configuration at runtime |

## Configuration reference (`config.py`)

| Key | Default | Meaning |
|---|---|---|
| `cycle_length_days` | 3 | Length of one pricing cycle |
| `min_price_move_pct` / `max_price_move_pct` | 0.3 / 1.0 | Allowed % price move per cycle |
| `min_margin_pct` | 15.0 | Hard profitability floor |
| `min_cycles_before_eligible` | 3 | Cycles a product needs before automatic pricing starts |
| `stale_zero_sales_days` | 30 | Window used to flag an established product as "dead stock" |
| `major_replenishment_pct_increase` | 200.0 | Inventory jump (%) that counts as a "major" receipt |
| `post_replenishment_cycles_required` | 3 | Fresh cycles needed after a major receipt before trusting the new baseline |
| `experiment_evaluation_cycles` | 2 | Cycles of subsequent data required before scoring a price-change experiment |
| `neutral_profit_band_pct` | 3.0 | Profit change smaller than this is "neutral," not signal |
| `consecutive_negative_reluctance_threshold` | 2 | Consecutive losing experiments in one direction before the engine gets reluctant |

Every product row also carries optional `min_margin_pct_override` /
`max_move_pct_override` columns for the rare real-world exception (a
clearance item, a loss-leader) without touching the global config.

## Scaling to production

The prototype uses SQLite so it runs anywhere with zero setup — but
`database/db.py` is the *only* file that talks to SQLite directly; every
other module goes through plain DB-API calls (`execute`/`fetchall`/`commit`)
that work unchanged against Postgres/MySQL via a driver swap. Beyond that:

- **Concurrency / scale**: swap SQLite for Postgres, add connection
  pooling, and `POST /pricing/tick` would become a fan-out job (e.g. one
  task per product batch on a queue) rather than a single synchronous loop
  — the per-product logic doesn't change at all.
- **Web server**: `api.py`'s `app.run(...)` is a dev server; production
  would run this behind gunicorn/uvicorn + a real reverse proxy, plus auth
  on the write endpoints (this prototype has none).
- **ML upgrade path**: `engine/decision.py`'s deterministic rules and
  `engine/learning.py`'s profile-building are intentionally isolated behind
  clean function boundaries so a learned demand/elasticity model could
  replace the bucket-based profile later without touching eligibility,
  safety, or execution at all — exactly what the spec asks for ("allow more
  sophisticated forecasting or machine-learning models to be incorporated
  later").

## Notes on the synthetic data

`data_generator.py` builds a plausible tire catalog (brands, sizes,
categories) where every product has a hidden, fixed price elasticity —
this is what gives the engine genuine signal to learn from instead of pure
noise. It deliberately includes edge cases so you can see every safeguard
exercised: products at 0/1/2/3 completed cycles (the new-product
eligibility boundary), two long-established "dead stock" products with
near-zero demand, and a few products with a deliberate major
replenishment event mid-history. None of this is real usawheelstires.com
data.

## Known limitations (worth naming up front in an interview)

- The decision logic is a transparent rule-based heuristic, not a trained
  model — that's explicitly what the spec asks for at this stage, with the
  architecture left open for a real forecasting model later.
- No authentication/authorization on the API — would need it before any
  real deployment.
- The synthetic demand model is a standard constant-elasticity curve; real
  tire demand has seasonality (e.g. winter tires) and competitor-price
  effects this prototype doesn't model.
- Cost changes are only applied at inventory receipts, matching how tire
  wholesale costs typically move in the real world.
