"""Credits are US cents held by a user. They pay for AI work done with the site's own Claude key."""
from __future__ import annotations

from . import db


def balance(user_id: int) -> int:
    with db.session() as conn:
        row = conn.execute("SELECT COALESCE(SUM(delta_cents), 0) AS b FROM credit_ledger WHERE user_id = ?", (user_id,)).fetchone()
    return int(row["b"])


def add(user_id: int, delta_cents: int, reason: str, ref: str = "") -> None:
    """Add (positive) or remove (negative) credits. Every change is a ledger row; nothing is ever edited
    (only a hold for running AI work is replaced by its real cost, see hold())."""
    with db.session() as conn:
        conn.execute(
            "INSERT INTO credit_ledger (user_id, delta_cents, reason, ref, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, int(delta_cents), reason, ref, db.now()),
        )


HOLD_REF = "ai-hold"


def hold(user_id: int, cents: int, reason: str) -> int | None:
    """Set aside credits for AI work about to start, if the balance covers them. Returns the hold's ledger id,
    or None if the balance is too low. Checking and holding happen in one locked step, so work running at
    the same time cannot spend the same credits twice. settle() replaces the hold with the real cost."""
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT COALESCE(SUM(delta_cents), 0) AS b FROM credit_ledger WHERE user_id = ?",
                           (user_id,)).fetchone()
        if int(row["b"]) < cents:
            return None
        cur = conn.execute(
            "INSERT INTO credit_ledger (user_id, delta_cents, reason, ref, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, -int(cents), reason, HOLD_REF, db.now()))
        return int(cur.lastrowid)


def settle(hold_id: int, user_id: int, cents: int, reason: str) -> None:
    """Replace a hold with what the work really cost (nothing, if it failed)."""
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM credit_ledger WHERE id = ? AND user_id = ? AND ref = ?", (hold_id, user_id, HOLD_REF))
        if cents > 0:
            conn.execute(
                "INSERT INTO credit_ledger (user_id, delta_cents, reason, ref, created_at) VALUES (?, ?, ?, '', ?)",
                (user_id, -int(cents), reason, db.now()))


def release_all_holds() -> None:
    """At start-up no AI work is running (it runs in this process), so any hold left is from a restart."""
    with db.session() as conn:
        conn.execute("DELETE FROM credit_ledger WHERE ref = ?", (HOLD_REF,))


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
