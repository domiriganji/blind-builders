"""SQLite storage. One short-lived connection per unit of work; WAL mode so background jobs and requests can share the file."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    pseudonym TEXT NOT NULL,
    screen_reader TEXT NOT NULL DEFAULT '',
    device TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'en',
    sight_onset TEXT NOT NULL DEFAULT 'prefer_not',
    verbosity TEXT NOT NULL DEFAULT 'summary_first',
    show_name_on_checks INTEGER NOT NULL DEFAULT 0,
    show_name_on_help INTEGER NOT NULL DEFAULT 0,
    own_api_key_enc TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0,
    session_epoch INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS credit_ledger (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    delta_cents INTEGER NOT NULL,
    reason TEXT NOT NULL,
    ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_user ON credit_ledger(user_id);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    progress TEXT NOT NULL DEFAULT '',
    result_url TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id);
CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    web_searches INTEGER NOT NULL DEFAULT 0,
    cost_cents INTEGER NOT NULL,
    paid_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS site_settings (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by INTEGER
);
CREATE TABLE IF NOT EXISTS ai_spend_holds (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    cents INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    key TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts ON attempts(action, key, created_at);
CREATE INDEX IF NOT EXISTS idx_attempts_subject ON attempts(subject);
"""

_db_path: Path | None = None


def now() -> str:
    """Current UTC time as ISO 8601 text (seconds precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def configure(path: Path) -> None:
    global _db_path
    _db_path = Path(path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)


def connect() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("database not configured")
    conn = sqlite3.connect(_db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, roll back on error, always close."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    """Add a column to an existing table if it is missing (CREATE TABLE IF NOT EXISTS never adds columns)."""
    names = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in names:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def init(feature_schemas: list[str]) -> None:
    with session() as conn:
        conn.executescript(CORE_SCHEMA)
        ensure_column(conn, "jobs", "back_url", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "jobs", "dedupe_key", "TEXT NOT NULL DEFAULT ''")
        # The private item a job is about (see jobs.forget_item), so deleting the item removes its jobs.
        ensure_column(conn, "jobs", "item_ref", "TEXT NOT NULL DEFAULT ''")
        # Raised to sign a member out on every device (see web.end_all_sessions).
        ensure_column(conn, "users", "session_epoch", "INTEGER NOT NULL DEFAULT 0")
        # Help is public, so it shows the code name unless the member chooses their name.
        ensure_column(conn, "users", "show_name_on_help", "INTEGER NOT NULL DEFAULT 0")
        # How the AI describes looks: 0 = the effect only (the default), 1 = the effect, then the main colours.
        ensure_column(conn, "users", "describe_colours", "INTEGER NOT NULL DEFAULT 0")
        # The model a member chose for their own key ('' = the site's choice, see site_ai.member_model).
        ensure_column(conn, "users", "own_model", "TEXT NOT NULL DEFAULT ''")
        # What Anthropic charged for the call, without the credits markup (site_ai limits count this).
        ensure_column(conn, "ai_usage", "raw_cents", "INTEGER NOT NULL DEFAULT 0")
        for schema in feature_schemas:
            if schema:
                conn.executescript(schema)
