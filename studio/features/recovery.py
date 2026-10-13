"""Recovery codes: getting back in after forgetting a password, with no email and no staff.

Each member gets a recovery code when they sign up, shown once. With their email address, that code and a
new password they can reset their password themselves. Each reset (or a new code made on the account page)
replaces the code, and signs the member out on every other device. Wrong guesses are limited per email
address and internet address together, and per internet address, so a stranger who knows a member's email
cannot block the member's own correct code.
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from .. import db, limits
from ..security import decrypt_secret, encrypt_secret, hash_password, verify_password
from ..web import checked_form, current_user, date_words, end_all_sessions, error_title, redirect, render, require_user, sign_in

router = APIRouter()
NAV = None

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0, O, 1 or I, which are easy to mix up
GROUPS, GROUP_LEN = 4, 4
MAX_TRIES_PER_HOUR = 10          # for one email address from one internet address
MAX_TRIES_PER_IP_PER_HOUR = 30   # from one internet address, whatever the email address
HOUR = 60 * 60
PASSWORD_MIN, PASSWORD_MAX = 10, 1000
_DUMMY_HASH = hash_password("not-a-real-recovery-code")
# A new code stays on its page (through reloads, or a phone reloading the tab) until the member says it is
# saved, or for at most this long.
SHOW_SECONDS = 60 * 60
TOO_MANY_TRIES = ("Several tries from your internet connection did not work, so resetting is paused for one hour to "
                  "protect accounts from guessing. Your account is unchanged. Please try again after that.")


def seed() -> None:
    with db.session() as conn:
        db.ensure_column(conn, "users", "recovery_hash", "TEXT")
        # 0 while a new code waits for the member to say "I have saved my code". Codes made before this column
        # existed count as saved.
        db.ensure_column(conn, "users", "recovery_saved", "INTEGER NOT NULL DEFAULT 1")
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
        conn.execute("UPDATE users SET recovery_hash = ?, recovery_saved = 0 WHERE id = ?",
                     (hash_password(normalize(code)), user_id))
    return code


def code_state(request: Request, user: dict) -> str:
    """Where the member's recovery code stands: 'none' (no code), 'waiting' (a new code is on its page,
    not yet confirmed as saved), 'unsaved' (a code was made but never confirmed, and it can no longer be
    shown) or 'saved'. Home and Your account use this, so neither says a code is saved before the member does."""
    if not user.get("recovery_hash"):
        return "none"
    if _shown_code(request):
        return "waiting"
    return "unsaved" if user.get("recovery_saved") == 0 else "saved"


def show_once(request: Request, code: str, heading: str, next_url: str = "/", done: str = "") -> None:
    """Keep the code in the session until the member says it is saved (or SHOW_SECONDS pass), so a reload does
    not lose it. The session cookie is signed but readable, so the code in it is encrypted with the site's
    secret key. `done` is what the next page says once the member continues."""
    secret = request.app.state.settings.secret_key
    request.session["recovery_show"] = {"code_enc": encrypt_secret(secret, code), "heading": heading,
                                        "next": next_url, "done": done, "at": int(time.time()), "seen": False}


def _shown_code(request: Request) -> tuple[dict, str] | None:
    """The code waiting to be saved, with its details, or None."""
    shown = request.session.get("recovery_show")
    if not isinstance(shown, dict) or not shown.get("code_enc"):
        return None
    if time.time() - int(shown.get("at") or 0) > SHOW_SECONDS:
        request.session.pop("recovery_show", None)
        return None
    code = decrypt_secret(request.app.state.settings.secret_key, shown["code_enc"])
    return (shown, code) if code else None


def _text(form: dict, name: str, limit: int) -> str:
    value = form.get(name, "")
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value)[:limit]


def _safe_next(url: str) -> str:
    return url if url.startswith("/") and not url.startswith("//") else "/"


@router.get("/account/recovery-code")
def recovery_page(request: Request, user: dict = Depends(require_user)):
    found = _shown_code(request)
    if found:
        shown, code = found
        again = bool(shown.get("seen"))
        if not again:
            request.session["recovery_show"] = {**shown, "seen": True}
        site = request.app.state.settings.site_name
        return render(request, "recovery/show.html", title=shown["heading"], code=code,
                      spelled=spelled_out(code), again=again, file_name=_file_name(site, shown))
    return _manage_page(request, user)


@router.get("/account/recovery-code.txt")
def recovery_file(request: Request, user: dict = Depends(require_user)):
    """The code waiting to be saved, as a small text file to keep. Works without JavaScript."""
    found = _shown_code(request)
    if not found:
        return redirect("/account/recovery-code")
    site = request.app.state.settings.site_name
    shown, code = found
    text = (f"Recovery code for {site}\r\n\r\n{code}\r\n\r\nSpelled out: {spelled_out(code)}.\r\n\r\n"
            f"Made on {_made_on(shown)} for {user['email']}. A newer code replaces this one.\r\n\r\n"
            f"With your email address, this code lets you set a new password if you forget yours. "
            f"Keep it private.\r\n")
    name = _file_name(site, shown)
    ascii_name = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "-" for ch in name)
    return Response(text, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{ascii_name}"; '
                                                    f"filename*=UTF-8''{quote(name)}",
                             "Cache-Control": "no-store"})


def _made_on(shown: dict) -> str:
    made = datetime.fromtimestamp(int(shown.get("at") or time.time()), timezone.utc)
    return date_words(made.isoformat())


def _file_name(site: str, shown: dict) -> str:
    """'Blind Builders recovery code, 13 October 2026.txt': the date tells several saved codes apart."""
    clean = "".join(ch for ch in site if ch not in '/\\:*?"<>|' and ord(ch) >= 32).strip() or "Recovery"
    return f"{clean} recovery code, {_made_on(shown)}.txt"


@router.post("/account/recovery-code/saved")
async def code_saved(request: Request, user: dict = Depends(require_user)):
    """The member says the code is saved: it is never shown again."""
    await checked_form(request)
    shown = request.session.pop("recovery_show", None)
    code = decrypt_secret(request.app.state.settings.secret_key, shown.get("code_enc") or "") \
        if isinstance(shown, dict) else None
    # Only the code that works now counts: a code made since on another device replaces this one.
    if code and user.get("recovery_hash") and await run_in_threadpool(verify_password, normalize(code),
                                                                      user["recovery_hash"]):
        with db.session() as conn:
            conn.execute("UPDATE users SET recovery_saved = 1 WHERE id = ?", (user["id"],))
    next_url = _safe_next(str(shown.get("next") or "/")) if isinstance(shown, dict) else "/account"
    done = str(shown.get("done") or "") if isinstance(shown, dict) else ""
    if done:
        from ..auth import announce  # local import: auth imports this module
        if next_url == "/account":
            request.session["account_heading"] = done
        else:
            announce(request, next_url, done)
    return redirect(next_url)


def _manage_page(request: Request, user: dict, errors: dict | None = None, status_code: int = 200):
    state = code_state(request, user)
    titles = {"saved": "Your recovery code", "unsaved": "Your recovery code was not saved: make a new one",
              "none": "You have no recovery code yet"}
    title = error_title(titles.get(state, "Your recovery code"), errors or {})
    return render(request, "recovery/manage.html", title=title, has_code=state == "saved", state=state,
                  errors=errors or {}, status_code=status_code)


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
        return _manage_page(request, user, {"password": (
            "Several password tries did not work, so making a new code is paused for an hour to protect your "
            "account. Your code is unchanged. Please try again after that.")}, 429)
    if not await run_in_threadpool(verify_password, password[:PASSWORD_MAX], user["password_hash"]):
        for k, _ in keys:
            limits.record("recovery", k, HOUR, subject=limits.key(user["email"]))
        return _manage_page(request, user, {"password": (
            "That is not the password you use now, so no new code was made. Check it and try again.")}, 400)
    code = await run_in_threadpool(set_new_code, user["id"])
    show_once(request, code, "Your new recovery code: save it now", "/account",
              done="Your new recovery code is ready")
    return redirect("/account/recovery-code")


@router.get("/reset-password")
def reset_form(request: Request):
    if current_user(request):
        return render(request, "recovery/reset.html", title="You are signed in", signed_in=True, values={}, errors={})
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
            errors["code"] = TOO_MANY_TRIES
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
        if "password" not in errors:
            errors["password"] = ("The password box was emptied so your password is not kept on the page. "
                                  "Please type your new password again.")
        return render(request, "recovery/reset.html", title=error_title("Reset your password", errors),
                      values={"email": email[:300], "code": _text(form, "code", 40).strip()}, errors=errors,
                      status_code=status_code)
    password_hash = await run_in_threadpool(hash_password, password)
    with db.session() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
        end_all_sessions(conn, user_id)  # anyone else signed in to this account is signed out
    limits.forget_email(email)
    code = await run_in_threadpool(set_new_code, user_id)  # the old code is used up
    sign_in(request, user_id)
    show_once(request, code, "Your password is changed: save your new recovery code", "/",
              done="Your password is changed and you are signed in")
    return redirect("/account/recovery-code")


def forget_user(user_id: int) -> None:
    with db.session() as conn:
        row = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.execute("UPDATE users SET recovery_hash = NULL WHERE id = ?", (user_id,))
    if row and row["email"]:
        limits.forget_email(row["email"])  # wrong sign-in and recovery tries for this address
