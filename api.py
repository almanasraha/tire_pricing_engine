"""
api.py
-----------------------------------------------------------------------------
This is "the module" -- the piece meant to be integrated into the existing
usawheelstires.com website. It exposes the whole pricing engine as a small
REST service (built with Flask, which is already a mainstream, lightweight
choice for exactly this kind of internal microservice) so that whatever the
website's own backend is (PHP, WordPress, Node, another Python app, etc.),
it can talk to this engine over plain HTTP/JSON instead of needing to share
a language or a process with it.

WHY A SEPARATE HTTP SERVICE RATHER THAN A LIBRARY THE WEBSITE IMPORTS:
The spec explicitly asks for the pipeline stages to stay "conceptually
separate... so that pricing decisions are auditable and the learning system
can be improved without compromising transactional systems." Running this
as its own service (its own process, its own database) means the pricing
engine can be redeployed, retrained, or even temporarily taken down without
touching the website's own checkout/inventory code at all. The website only
ever needs to know a handful of URLs.

HOW THE WEBSITE WOULD ACTUALLY USE THIS, DAY TO DAY:
  1. Whenever the website's own systems record a sale or a restock, it also
     POSTs that same event here (POST /ingest/sales, POST /ingest/inventory-receipt)
     -- keeping this engine's view of the world in sync with reality.
  2. A daily scheduled job (a cron job, a cloud scheduler, etc.) calls
     POST /pricing/tick once a day. That one call runs the entire pipeline:
     closes any pricing cycles that have finished, makes a decision for
     every eligible product, executes any approved changes, and scores any
     experiments that are now old enough to evaluate.
  3. The website reads current prices back out of its OWN inventory
     database as always (this engine writes the new price into `products`,
     but production would extend that write, or a webhook, to also push
     the change into the website's real product catalog) -- or, at minimum,
     GET /products/<id> here whenever it wants to display "what does our
     pricing engine currently think this product should cost."
  4. GET /products/<id>/history and /recommendation are what a "why did the
     price change?" admin page in the website would call.

Run locally with:  python3 api.py   (serves on http://localhost:8000)
"""

from datetime import date
import sqlite3

from flask import Flask, g, jsonify, request

from config import get_config, set_config_value, PricingConfig
from database.db import get_connection
from engine.ingestion import record_daily_sale, record_inventory_receipt, ValidationError
from engine.history import close_due_cycles, get_cycle_history, cycles_completed_count
from engine.eligibility import evaluate_eligibility
from engine.learning import build_price_performance_profile, get_directional_reluctance, evaluate_pending_experiments
from engine.decision import decide_price
from engine.execution import run_pricing_cycle, run_pricing_cycle_for_all

app = Flask(__name__)


# -----------------------------------------------------------------------------
# Connection handling: Flask's `g` object is a per-request scratch space.
# Opening one SQLite connection per incoming request (and closing it once the
# response is sent) is the standard, safe pattern for a request-driven web
# service -- it means concurrent requests never share a connection/cursor.
# -----------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = get_connection()
    return g.db


@app.teardown_appcontext
def close_db(_exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def get_cfg() -> PricingConfig:
    """Re-read config fresh on every request rather than caching it, so a
    config change made via PUT /config/<key> takes effect on the very next
    request -- no restart required, matching the spec's "changed without
    modifying the underlying program" requirement."""
    return get_config(get_db())


# -----------------------------------------------------------------------------
# Error handling: turn our own exception types into clean HTTP responses
# instead of letting Flask return an opaque 500 for everything.
# -----------------------------------------------------------------------------
@app.errorhandler(ValidationError)
def handle_validation_error(exc: ValidationError):
    return jsonify({"error": "validation_error", "message": str(exc)}), 400


@app.errorhandler(ValueError)
def handle_value_error(exc: ValueError):
    return jsonify({"error": "not_found_or_bad_request", "message": str(exc)}), 404


@app.errorhandler(KeyError)
def handle_missing_field(exc: KeyError):
    return jsonify({"error": "missing_field", "message": f"Missing required field: {exc}"}), 400


# =============================================================================
# Health check
# =============================================================================

@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "tire-pricing-engine"})


# =============================================================================
# Product catalog
# =============================================================================

@app.get("/products")
def list_products():
    """List products. Supports optional filters so a website admin page can
    show e.g. only locked products, or only one category, without pulling
    the whole catalog every time."""
    conn = get_db()
    query = "SELECT * FROM products WHERE 1=1"
    params = []
    if request.args.get("category"):
        query += " AND category = ?"
        params.append(request.args["category"])
    if request.args.get("locked") is not None:
        query += " AND is_locked = ?"
        params.append(1 if request.args["locked"].lower() in ("1", "true", "yes") else 0)
    query += " ORDER BY product_id"

    rows = conn.execute(query, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/products")
def create_product():
    """
    Onboard a new product. This is the ONLY manual step the spec allows --
    "Products should automatically enter the pricing system when they
    appear in the company's inventory/product database" -- so in production
    this endpoint is what a sync job (reading the website's real product
    feed) would call automatically for every new SKU it sees; a human never
    has to fill in pricing rules for it.
    """
    body = request.get_json(force=True)
    conn = get_db()
    now = date.today().isoformat()

    required = ["product_id", "sku", "name", "cost", "current_price"]
    missing = [f for f in required if f not in body]
    if missing:
        return jsonify({"error": "missing_field", "message": f"Missing required fields: {missing}"}), 400

    conn.execute(
        """
        INSERT INTO products (product_id, sku, name, brand, category, cost, current_price,
                               current_inventory, created_at, is_locked, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            body["product_id"], body["sku"], body["name"], body.get("brand"), body.get("category"),
            body["cost"], body["current_price"], body.get("current_inventory", 0), now, now,
        ),
    )
    conn.commit()
    return jsonify({"created": True, "product_id": body["product_id"]}), 201


@app.get("/products/<product_id>")
def get_product(product_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)).fetchone()
    if row is None:
        return jsonify({"error": "not_found", "message": f"No such product: {product_id}"}), 404
    result = dict(row)
    result["cycles_completed"] = cycles_completed_count(conn, product_id)
    return jsonify(result)


@app.post("/products/<product_id>/lock")
def lock_product(product_id: str):
    """"Ability to lock individual products from automatic pricing" --
    an operator override that takes effect immediately and is always
    respected by engine/eligibility.py before any pricing logic runs."""
    conn = get_db()
    conn.execute("UPDATE products SET is_locked = 1 WHERE product_id = ?", (product_id,))
    conn.commit()
    return jsonify({"product_id": product_id, "is_locked": True})


@app.post("/products/<product_id>/unlock")
def unlock_product(product_id: str):
    conn = get_db()
    conn.execute("UPDATE products SET is_locked = 0 WHERE product_id = ?", (product_id,))
    conn.commit()
    return jsonify({"product_id": product_id, "is_locked": False})


# =============================================================================
# Data ingestion (Stage 1: Data Collection)
# =============================================================================

@app.post("/ingest/sales")
def ingest_sales():
    """
    The website (or its POS/e-commerce backend) calls this once per
    product per day with that day's actual sales. Validation happens
    inside record_daily_sale -- bad rows are rejected with a 400 and never
    reach the rest of the pipeline.
    """
    body = request.get_json(force=True)
    conn = get_db()
    row_id = record_daily_sale(
        conn, get_cfg(),
        product_id=body["product_id"], sale_date=body["sale_date"],
        units_sold=body["units_sold"], unit_price=body["unit_price"], unit_cost=body["unit_cost"],
    )
    return jsonify({"ingested": True, "sales_daily_id": row_id}), 201


@app.post("/ingest/inventory-receipt")
def ingest_inventory_receipt():
    """The website calls this whenever new stock is received for a product."""
    body = request.get_json(force=True)
    conn = get_db()
    row_id = record_inventory_receipt(
        conn, get_cfg(),
        product_id=body["product_id"], received_at=body["received_at"],
        quantity=body["quantity"], unit_cost=body["unit_cost"],
    )
    return jsonify({"ingested": True, "inventory_receipt_id": row_id}), 201


# =============================================================================
# Pricing pipeline (Stages 2-7)
# =============================================================================

@app.get("/products/<product_id>/recommendation")
def get_recommendation(product_id: str):
    """
    A DRY RUN: shows what the engine would decide right now WITHOUT
    executing it or writing an audit row. This is what a "preview the next
    price change" admin screen on the website would call.
    """
    conn = get_db()
    decision = decide_price(conn, get_cfg(), product_id)
    return jsonify({
        "product_id": decision.product_id, "decision": decision.decision,
        "price_before": decision.price_before, "price_after": decision.price_after,
        "pct_change": decision.pct_change, "reason": decision.reason,
    })


@app.post("/products/<product_id>/run-cycle")
def run_cycle_for_product(product_id: str):
    """Actually decide AND execute (if applicable) for one product, logging
    a full audit row regardless of the outcome."""
    conn = get_db()
    result = run_pricing_cycle(conn, get_cfg(), product_id)
    return jsonify(result)


@app.post("/pricing/tick")
def pricing_tick():
    """
    The single endpoint a daily scheduler needs to call. Runs the entire
    back half of the pipeline for every product in one shot:

        1. Close any pricing cycles whose window has fully elapsed
           (Data Collection -> Performance History)
        2. Make and execute a pricing decision for every product
           (Pricing Decision -> Price Execution)
        3. Score any price-change experiments that now have enough
           subsequent data to evaluate (Outcome Evaluation -> Learning)

    Idempotent and safe to call more than once on the same day: cycles that
    are already closed are skipped, and duplicate decisions for a cycle
    that's already been decided are rejected by the database (see
    engine/execution.py).
    """
    conn = get_db()
    cfg = get_cfg()

    product_ids = [r["product_id"] for r in conn.execute("SELECT product_id FROM products").fetchall()]
    newly_closed_cycles = sum(len(close_due_cycles(conn, cfg, pid)) for pid in product_ids)

    decisions = run_pricing_cycle_for_all(conn, cfg)
    evaluated_experiments = evaluate_pending_experiments(conn, cfg)

    return jsonify({
        "cycles_closed": newly_closed_cycles,
        "decisions": decisions,
        "experiments_evaluated": evaluated_experiments,
    })


@app.get("/products/<product_id>/history")
def get_history(product_id: str):
    """Full auditable history for one product: every closed cycle, plus
    every recommendation ever made (including HOLDs) with its reason."""
    conn = get_db()
    cycles = [c.__dict__ for c in get_cycle_history(conn, product_id)]
    decisions = [
        dict(r) for r in conn.execute(
            "SELECT * FROM pricing_decisions WHERE product_id = ? ORDER BY decision_time", (product_id,)
        ).fetchall()
    ]
    return jsonify({"product_id": product_id, "cycles": cycles, "decisions": decisions})


@app.get("/products/<product_id>/profile")
def get_profile(product_id: str):
    """The learned price -> (units, profit) relationship for this product --
    the concrete data behind the spec's "$180 sold ~5 units / $150 profit"
    example."""
    conn = get_db()
    cfg = get_cfg()
    eligibility = evaluate_eligibility(conn, cfg, product_id)
    profile = build_price_performance_profile(conn, product_id, start_cycle_index=eligibility.relevant_cycles_start_index)
    reluctance = get_directional_reluctance(conn, cfg, product_id)
    return jsonify({
        "product_id": product_id,
        "eligibility": {
            "eligible": eligibility.eligible, "reason": eligibility.reason,
            "is_new_product": eligibility.is_new_product, "is_stale_zero_sales": eligibility.is_stale_zero_sales,
            "is_post_replenishment_window": eligibility.is_post_replenishment_window,
        },
        "price_performance_profile": profile,
        "directional_reluctance": reluctance,
    })


# =============================================================================
# Configuration (centralized, runtime-tunable -- see config.py)
# =============================================================================

@app.get("/config")
def get_all_config():
    cfg = get_cfg()
    return jsonify(cfg.__dict__)


@app.put("/config/<key>")
def update_config(key: str):
    """Change one tunable parameter at runtime -- e.g.
    PUT /config/max_price_move_pct {"value": 1.5} -- with no code deploy or
    restart needed, per the spec's centralized-configuration requirement."""
    body = request.get_json(force=True)
    if "value" not in body:
        return jsonify({"error": "missing_field", "message": "Missing required field: value"}), 400
    conn = get_db()
    try:
        set_config_value(conn, key, body["value"], body.get("description", ""))
    except ValueError as exc:
        return jsonify({"error": "invalid_config_key", "message": str(exc)}), 400
    return jsonify({"updated": True, "key": key, "value": body["value"]})


if __name__ == "__main__":
    # debug=False: this stands in for a production-style run. Flask's
    # built-in server is fine for local development/demo purposes; a real
    # deployment would run this behind gunicorn/uvicorn+ASGI or similar.
    app.run(host="0.0.0.0", port=8000, debug=False)
