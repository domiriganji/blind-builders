"""Recovery codes: getting back in after forgetting a password, with no email and no staff.

Each member gets a recovery code when they sign up, shown once. With their email address, that code and a
new password they can reset their password themselves. Each reset (or a new code made on the account page)
replaces the code, and signs the member out on every other device. Wrong guesses are limited per email
address and internet address together, and per internet address, so a stranger who knows a member's email
cannot block the member's own correct code.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from .. import db, limits
from ..security import decrypt_secret, encrypt_secret, hash_password, verify_password
from ..web import checked_form, current_user, end_all_sessions, redirect, render, require_user, sign_in

router = APIRouter()
NAV = None

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0, O, 1 or I, which are easy to mix up
GROUPS, GROUP_LEN = 4, 4
MAX_TRIES_PER_HOUR = 10          # for one email address from one internet address
MAX_TRIES_PER_IP_PER_HOUR = 30   # from one internet address, whatever the email address
HOUR = 60 * 60
PASSWORD_MIN, PASSWORD_MAX = 10, 1000
_DUMMY_HASH = hash_password("not-a-real-recovery-code")


def seed() -> None:
    with db.session() as conn:
        db.ensure_column(conn, "users", "recovery_hash", "TEXT")
        # Wrong tries used to be kept here, by email address, for ever. They now live in the attempts table.
        conn.execute("DROP TABLE IF EXISTS recovery_attempts")


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
    """Keep the code in the session just long enough to show it on the next page. The session cookie is
    signed but readable, so the code in it is encrypted with the site's secret key."""
    secret = request.app.state.settings.secret_key
    request.session["recovery_show"] = {"code_enc": encrypt_secret(secret, code), "heading": heading,
                                        "next": next_url}


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
    code = None
    if isinstance(shown, dict) and shown.get("code_enc"):
        code = decrypt_secret(request.app.state.settings.secret_key, shown["code_enc"])
    if code:
        return render(request, "recovery/show.html", title=shown["heading"], code=code,
                      spelled=spelled_out(code), next_url=_safe_next(shown.get("next") or "/"))
    return _manage_page(request, user)


def _manage_page(request: Request, user: dict, errors: dict | None = None, status_code: int = 200):
    has_code = bool(user.get("recovery_hash"))
    title = "Your recovery code" if has_code else "You have no recovery code yet"
    if errors:
        title += ": please fix the problems"
    return render(request, "recovery/manage.html", title=title, has_code=has_code, errors=errors or {},
                  status_code=status_code)


@router.post("/account/recovery-code")
async def make_new_code(request: Request, user: dict = Depends(require_user)):
    # A new code can reset the password, so it needs the current password: someone who only has a copy of
    # the member's session (a shared computer) cannot use it to take the account over.
    form = await checked_form(request)
    password = _text(form, "password", PASSWORD_MAX + 1)
    keys = [(limits.key("new-code", user["id"]), MAX_TRIES_PER_HOUR),
            (limits.key("ip", limits.client_ip(request)), MAX_TRIES_PER_IP_PER_HOUR)]
    if not password:
        return _manage_page(request, user, {"password": "Enter your current password."}, 400)
    if any(limits.count("recovery", k, HOUR) >= most for k, most in keys):
        return _manage_page(request, user, {"password": "Too many wrong tries. Wait an hour and try again."}, 429)
    if not await run_in_threadpool(verify_password, password[:PASSWORD_MAX], user["password_hash"]):
        for k, _ in keys:
            limits.record("recovery", k, HOUR, subject=limits.key(user["email"]))
        return _manage_page(request, user, {"password": "That is not your current password. Try again."}, 400)
    code = await run_in_threadpool(set_new_code, user["id"])
    show_once(request, code, "Your new recovery code: save it now", "/account")
    return redirect("/account/recovery-code")


@router.get("/reset-password")
def reset_form(request: Request):
    if current_user(request):
        return redirect("/account")
    return render(request, "recovery/reset.html", title="Reset your password with your recovery code",
                  values={}, errors={})


def _try_keys(email: str, ip: str) -> list[tuple[str, int]]:
    return [(limits.key(email, ip), MAX_TRIES_PER_HOUR), (limits.key("ip", ip), MAX_TRIES_PER_IP_PER_HOUR)]


def _too_many_tries(email: str, ip: str) -> bool:
    return any(limits.count("recovery", k, HOUR) >= most for k, most in _try_keys(email, ip))


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
    status_code = 400
    ip = limits.client_ip(request)
    if not errors:
        if _too_many_tries(email, ip):
            errors["code"] = ("Too many wrong tries from your internet connection. Wait an hour and try again. "
                              "This protects accounts from people guessing.")
            status_code = 429
        else:
            with db.session() as conn:
                row = conn.execute("SELECT id, recovery_hash FROM users WHERE email = ? AND email NOT LIKE '%@invalid'",
                                   (email,)).fetchone()
            stored = row["recovery_hash"] if row and row["recovery_hash"] else _DUMMY_HASH
            if row is None or not row["recovery_hash"] or not await run_in_threadpool(verify_password, code, stored):
                for k, _ in _try_keys(email, ip):
                    limits.record("recovery", k, HOUR, subject=limits.key(email))
                errors["code"] = "The email address or recovery code is not right. Check both and try again."
            else:
                user_id = row["id"]
    if errors:
        return render(request, "recovery/reset.html", title="Reset your password: please fix the problems",
                      values={"email": email[:300]}, errors=errors, status_code=status_code)
    password_hash = await run_in_threadpool(hash_password, password)
    with db.session() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
        end_all_sessions(conn, user_id)  # anyone else signed in to this account is signed out
    limits.forget_email(email)
    code = await run_in_threadpool(set_new_code, user_id)  # the old code is used up
    sign_in(request, user_id)
    show_once(request, code, "Your password is changed: save your new recovery code", "/")
    return redirect("/account/recovery-code")


def forget_user(user_id: int) -> None:
    with db.session() as conn:
        row = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.execute("UPDATE users SET recovery_hash = NULL WHERE id = ?", (user_id,))
    if row and row["email"]:
        limits.forget_email(row["email"])  # wrong sign-in and recovery tries for this address
