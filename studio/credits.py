"""Credits are US cents held by a user. They pay for AI work done with the site's own Claude key."""
from __future__ import annotations

from . import db


def balance(user_id: int) -> int:
    with db.session() as conn:
        row = conn.execute("SELECT COALESCE(SUM(delta_cents), 0) AS b FROM credit_ledger WHERE user_id = ?", (user_id,)).fetchone()
    return int(row["b"])


def add(user_id: int, delta_cents: int, reason: str, ref: str = "") -> None:
    """Add (positive) or remove (negative) credits. Every change is a ledger row; nothing is ever edited."""
    with db.session() as conn:
        conn.execute(
            "INSERT INTO credit_ledger (user_id, delta_cents, reason, ref, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, int(delta_cents), reason, ref, db.now()),
        )


def history(user_id: int, limit: int = 50) -> list[dict]:
    with db.session() as conn:
        rows = conn.execute(
            "SELECT delta_cents, reason, ref, created_at FROM credit_ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"
