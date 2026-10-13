"""Recovery codes: getting back in after forgetting a password, with no email and no staff.

Each member gets a recovery code when they sign up, shown once. With their email address, that code and a
new password they can reset their password themselves. Each reset (or a new code made on the account page)
replaces the code. Wrong guesses are limited to 10 per email address per hour.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request

from .. import db
from ..security import hash_password, verify_password
from ..web import checked_form, current_user, flash, redirect, render, require_user

router = APIRouter()
NAV = None
SCHEMA = """
CREATE TABLE IF NOT EXISTS recovery_attempts (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recovery_attempts_email ON recovery_attempts(email);
"""

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0, O, 1 or I, which are easy to mix up
GROUPS, GROUP_LEN = 4, 4
MAX_TRIES_PER_HOUR = 10
PASSWORD_MIN, PASSWORD_MAX = 10, 1000
_DUMMY_HASH = hash_password("not-a-real-recovery-code")


def seed() -> None:
    with db.session() as conn:
        db.ensure_column(conn, "users", "recovery_hash", "TEXT")


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(GROUPS * GROUP_LEN))
    return "-".join(raw[i:i + GROUP_LEN] for i in range(0, len(raw), GROUP_LEN))


def normalize(code: str) -> str:
    return "".join(ch for ch in str(code).upper() if ch.isalnum())


def spelled_out(code: str) -> str:
    """Letters one by one, groups separated, so a screen reader reads every character."""
    return "; ".join(", ".join(group) for group in code.split("-"))


def set_new_code(user_id: int) -> str:
    code = new_code()
    with db.session() as conn:
        conn.execute("UPDATE users SET recovery_hash = ? WHERE id = ?", (hash_password(normalize(code)), user_id))
    return code


def show_once(request: Request, code: str, heading: str, next_url: str = "/") -> None:
    """Keep the code in the session just long enough to show it on the next page."""
    request.session["recovery_show"] = {"code": code, "heading": heading, "next": next_url}


def _text(form: dict, name: str, limit: int) -> str:
    value = form.get(name, "")
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value)[:limit]


def _safe_next(url: str) -> str:
    return url if url.startswith("/") and not url.startswith("//") else "/"


@router.get("/account/recovery-code")
def recovery_page(request: Request, user: dict = Depends(require_user)):
    shown = request.session.pop("recovery_show", None)
    if shown:
        return render(request, "recovery/show.html", title=shown["heading"], code=shown["code"],
                      spelled=spelled_out(shown["code"]), next_url=_safe_next(shown.get("next") or "/"))
    has_code = bool(user.get("recovery_hash"))
    title = "Your recovery code" if has_code else "You have no recovery code yet"
    return render(request, "recovery/manage.html", title=title, has_code=has_code)


@router.post("/account/recovery-code")
async def make_new_code(request: Request, user: dict = Depends(require_user)):
    await checked_form(request)
    code = set_new_code(user["id"])
    show_once(request, code, "Your new recovery code: save it now", "/account")
    return redirect("/account/recovery-code")


@router.get("/reset-password")
def reset_form(request: Request):
    if current_user(request):
        return redirect("/account")
    return render(request, "recovery/reset.html", title="Reset your password with your recovery code",
                  values={}, errors={})


def _too_many_tries(email: str) -> bool:
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat()
    with db.session() as conn:
        n = conn.execute("SELECT COUNT(*) FROM recovery_attempts WHERE email = ? AND created_at >= ?",
                         (email, since)).fetchone()[0]
    return n >= MAX_TRIES_PER_HOUR


@router.post("/reset-password")
async def reset(request: Request):
    form = await checked_form(request)
    email = _text(form, "email", 400).strip().lower()
    code = normalize(_text(form, "code", 200))
    password = _text(form, "password", PASSWORD_MAX + 1)
    errors: dict[str, str] = {}
    if not email:
        errors["email"] = "Enter the email address you signed up with."
    if not code:
        errors["code"] = "Enter your recovery code. It has 16 letters and numbers; spaces and dashes do not matter."
    if len(password) < PASSWORD_MIN:
        errors["password"] = f"Use at least {PASSWORD_MIN} characters for your new password."
    elif len(password) > PASSWORD_MAX:
        errors["password"] = f"Use {PASSWORD_MAX} characters or fewer for your new password."
    user_id = None
    if not errors:
        if _too_many_tries(email):
            errors["code"] = ("Too many wrong tries for this email address. Wait an hour and try again. "
                              "This protects your account from people guessing.")
        else:
            with db.session() as conn:
                row = conn.execute("SELECT id, recovery_hash FROM users WHERE email = ? AND email NOT LIKE '%@invalid'",
                                   (email,)).fetchone()
            stored = row["recovery_hash"] if row and row["recovery_hash"] else _DUMMY_HASH
            if row is None or not row["recovery_hash"] or not verify_password(code, stored):
                with db.session() as conn:
                    conn.execute("INSERT INTO recovery_attempts (email, created_at) VALUES (?, ?)", (email, db.now()))
                errors["code"] = "The email address or recovery code is not right. Check both and try again."
            else:
                user_id = row["id"]
    if errors:
        return render(request, "recovery/reset.html", title="Reset your password: please fix the problems",
                      values={"email": email[:300]}, errors=errors, status_code=400)
    with db.session() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))
        conn.execute("DELETE FROM recovery_attempts WHERE email = ?", (email,))
    code = set_new_code(user_id)  # the old code is used up
    request.session.clear()
    request.session["user_id"] = user_id
    show_once(request, code, "Your password is changed: save your new recovery code", "/")
    return redirect("/account/recovery-code")


def forget_user(user_id: int) -> None:
    with db.session() as conn:
        conn.execute("UPDATE users SET recovery_hash = NULL WHERE id = ?", (user_id,))
