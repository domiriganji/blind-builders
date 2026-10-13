"""Counting attempts, to slow down password guessing and mass sign-ups.

Each row holds an action ("login", "register", ...), a key made of hashed parts (an email address, an
internet address), and a subject (the hashed email, so deleting an account can remove its rows). Rows are
deleted once they are older than the longest time they are counted for, so nothing is kept for long.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from fastapi import Request

from . import db


def client_ip(request: Request) -> str:
    """The visitor's internet address. Behind a proxy on the same machine, uvicorn reads it from the
    X-Forwarded-For header (see docs/DEVELOPING.md)."""
    return request.client.host if request.client else ""


def key(*parts: object) -> str:
    """A hash of the parts, so the table holds no email addresses or internet addresses in plain text."""
    joined = "\x1f".join(str(p).strip().lower() for p in parts)
    return hashlib.sha256(("studio-limits:" + joined).encode()).hexdigest()


def _since(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def count(action: str, key_: str, seconds: int) -> int:
    with db.session() as conn:
        return conn.execute("SELECT COUNT(*) FROM attempts WHERE action = ? AND key = ? AND created_at >= ?",
                            (action, key_, _since(seconds))).fetchone()[0]


def record(action: str, key_: str, keep_seconds: int, subject: str = "") -> None:
    """Count one attempt, and delete this action's rows older than keep_seconds."""
    with db.session() as conn:
        conn.execute("DELETE FROM attempts WHERE action = ? AND created_at < ?", (action, _since(keep_seconds)))
        conn.execute("INSERT INTO attempts (action, key, subject, created_at) VALUES (?, ?, ?, ?)",
                     (action, key_, subject, db.now()))


def clear(action: str, key_: str) -> None:
    with db.session() as conn:
        conn.execute("DELETE FROM attempts WHERE action = ? AND key = ?", (action, key_))


def forget_email(email: str) -> None:
    """Remove every row about this email address (when the account is deleted, or after a reset)."""
    with db.session() as conn:
        conn.execute("DELETE FROM attempts WHERE subject = ?", (key(email),))
