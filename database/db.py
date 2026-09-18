"""
database/db.py
-----------------------------------------------------------------------------
Thin connection/bootstrapping layer. Every other module gets its database
handle through `get_connection()` in this file -- nothing else in the
codebase opens a sqlite3 connection directly. That's a deliberate choice:
if this later moves to Postgres/MySQL for production (as the spec calls
for -- "the production system should be designed around a proper database"),
this is the ONE file that needs to change (swap sqlite3.connect(...) for a
psycopg2/SQLAlchemy engine), because every caller only depends on the
standard DB-API surface (execute/fetchall/commit), not on SQLite specifics.
"""

import sqlite3
import os
import threading

# Default location for the prototype's SQLite file. Overridable via env var
# so tests (and the simulation script) can point at their own throwaway DB
# instead of clobbering the "real" one.
DEFAULT_DB_PATH = os.environ.get(
    "PRICING_ENGINE_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pricing_engine.db"),
)

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# A lock so concurrent requests (e.g. multiple API calls) don't race each
# other while initializing the schema on first use. Actual per-request
# connections are still separate sqlite3.Connection objects.
_init_lock = threading.Lock()
_initialized_paths: set[str] = set()


def get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """
    Open a connection to the pricing engine database, creating the schema
    on first use if it doesn't exist yet.

    `sqlite3.Row` as the row_factory means query results can be accessed
    both by index (row[0]) and by column name (row["product_id"]), which
    makes the engine code far more readable than plain tuples.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # SQLite doesn't enforce foreign keys unless you ask it to per-connection.
    conn.execute("PRAGMA foreign_keys = ON")

    _ensure_schema(conn, db_path)
    return conn


def _ensure_schema(conn: sqlite3.Connection, db_path: str) -> None:
    """Apply schema.sql once per distinct database file per process."""
    with _init_lock:
        if db_path in _initialized_paths:
            return
        with open(_SCHEMA_PATH, "r") as f:
            schema_sql = f.read()
        conn.executescript(schema_sql)
        conn.commit()
        _initialized_paths.add(db_path)


def reset_database(db_path: str = DEFAULT_DB_PATH) -> None:
    """
    Delete and recreate the database file from scratch. Used by the
    synthetic data generator and by tests that want a guaranteed-clean
    slate -- never call this against a production database.
    """
    if os.path.exists(db_path):
        os.remove(db_path)
    _initialized_paths.discard(db_path)
    conn = get_connection(db_path)
    conn.close()
