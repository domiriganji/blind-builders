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
    own_api_key_enc TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0,
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


def init(feature_schemas: list[str]) -> None:
    with session() as conn:
        conn.executescript(CORE_SCHEMA)
        for schema in feature_schemas:
            if schema:
                conn.executescript(schema)
