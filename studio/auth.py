"""Accounts: register, sign in, sign out."""
from __future__ import annotations

import re
import secrets
import sqlite3
import unicodedata

from fastapi import APIRouter, Request

from . import credits, db
from .security import hash_password, verify_password
from .web import checked_form, current_user, flash, redirect, render

router = APIRouter()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_MAX = 254          # the longest address email standards allow
NAME_MAX = 80            # same limit as the profile page
DETAIL_MAX = 100         # screen reader and device, same limit as the profile page
PASSWORD_MIN, PASSWORD_MAX = 10, 200
# Compared against when the email is unknown, so a wrong email takes as long as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
_SECRET_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")


_CARD_RUN_RE = re.compile(r"\d(?:[ \-]?\d){12,18}")
_PASSWORD_SAID_RE = re.compile(r"\b(?:password|passcode|pin|one[- ]time code|security code)\s*(?:is\b|:|=)", re.I)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def secret_problem(text: str) -> str | None:
    """A plain message if the text looks like an API key, a card number or a password, otherwise None.

    Used on fields other members can see, so a pasted secret is refused instead of shown to everyone."""
    if not text:
        return None
    if _SECRET_KEY_RE.search(text):
        return ("This looks like a Claude API key. Take it out: other members could see it. "
                "Connect your key on the Claude API key page instead.")
    for run in _CARD_RUN_RE.finditer(text):
        digits = re.sub(r"\D", "", run.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return "This looks like a card number. Take it out. This site never needs your card details."
    if _PASSWORD_SAID_RE.search(text):
        return "This looks like a password or code. Take it out. This site never needs them here."
    return None


def clean_text(value: object, limit: int) -> str:
    """One line of member text: the first value of a repeated field, no control or invisible characters at the
    ends, cut to `limit` characters. Line breaks and tabs inside become spaces."""
    if isinstance(value, list):
        value = value[0] if value else ""
    text = str(value or "")[: limit * 4]
    text = "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in text)
    while text and (text[0].isspace() or unicodedata.category(text[0]) == "Cf"):
        text = text[1:]
    while text and (text[-1].isspace() or unicodedata.category(text[-1]) == "Cf"):
        text = text[:-1]
    return text[:limit]


def _safe_next(url: str | None) -> str:
    """Only paths on this site. Browsers read "/\\evil.example" and "/\\t/evil.example" as another site."""
    if (url and url.startswith("/") and not url.startswith("//") and "\\" not in url
            and not any(ord(ch) < 33 or ord(ch) == 127 for ch in url) and len(url) <= 2000):
        return url
    return "/"


def _text(form: dict, name: str, limit: int = 1000) -> str:
    """One form value as text. A repeated field counts as its first value; very long text is cut."""
    value = form.get(name, "")
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value)[:limit]


def announce(request: Request, next_url: str, heading: str) -> None:
    """Say what just happened. On the home page it becomes the h1; anywhere else it is a message."""
    if next_url == "/":
        request.session["home_heading"] = heading
    else:
        flash(request, heading + ".", "success")


@router.get("/register")
def register_form(request: Request, next: str = "/"):
    return render(request, "auth/register.html", title="Create your account", values={}, errors={}, next=_safe_next(next))


@router.post("/register")
async def register(request: Request):
    form = await checked_form(request)
    values = {k: clean_text(form.get(k, ""), 1000) for k in ("email", "display_name", "screen_reader", "device")}
    password = _text(form, "password", PASSWORD_MAX + 1)
    errors: dict[str, str] = {}
    if not values["email"]:
        errors["email"] = "Enter your email address, like name@example.com."
    elif len(values["email"]) > EMAIL_MAX or not EMAIL_RE.match(values["email"]):
        errors["email"] = "Enter a whole email address, like name@example.com."
    if not values["display_name"]:
        errors["display_name"] = "Enter the name you want other members to see."
    elif len(values["display_name"]) > NAME_MAX:
        errors["display_name"] = f"Use {NAME_MAX} characters or fewer for your name."
    if len(password) < PASSWORD_MIN:
        errors["password"] = f"Use at least {PASSWORD_MIN} characters for your password."
    elif len(password) > PASSWORD_MAX:
        errors["password"] = f"Use {PASSWORD_MAX} characters or fewer for your password."
    for name, label in (("screen_reader", "screen reader"), ("device", "device")):
        if len(values[name]) > DETAIL_MAX:
            errors[name] = f"Use {DETAIL_MAX} characters or fewer for your {label}."
    for name in ("display_name", "screen_reader", "device"):
        problem = secret_problem(values[name])
        if problem:
            errors[name] = problem
            values[name] = ""  # never show the secret again, not even in the form
    next_url = _safe_next(_text(form, "next", 2001) or "/")
    if not errors:
        try:
            with db.session() as conn:
                cur = conn.execute(
                    "INSERT INTO users (email, display_name, password_hash, pseudonym, screen_reader, device, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (values["email"].lower(), values["display_name"], hash_password(password),
                     "Member " + secrets.token_hex(2).upper(), values["screen_reader"], values["device"], db.now()),
                )
                user_id = cur.lastrowid
        except sqlite3.IntegrityError:
            errors["email"] = "An account with this email already exists. Sign in instead, or use another email."
    if errors:
        for name in ("email", "display_name", "screen_reader", "device"):
            values[name] = values[name][:300]
        return render(request, "auth/register.html", title="Create your account: please fix the problems",
                      values=values, errors=errors, next=next_url, status_code=400)
    signup = request.app.state.settings.signup_credits
    if signup > 0:
        credits.add(user_id, signup, "Welcome credits")
    request.session.clear()
    request.session["user_id"] = user_id
    from .features import recovery  # local import: features import auth's helpers
    recovery.show_once(request, recovery.set_new_code(user_id), "Your account is ready: save your recovery code",
                       next_url)
    return redirect("/account/recovery-code")


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    if current_user(request):
        return redirect(_safe_next(next))
    return render(request, "auth/login.html", title="Sign in", values={}, errors={}, next=_safe_next(next))


@router.post("/login")
async def login(request: Request):
    form = await checked_form(request)
    email = _text(form, "email").strip().lower()
    password = _text(form, "password", PASSWORD_MAX + 1)
    next_url = _safe_next(_text(form, "next", 2001) or "/")
    errors: dict[str, str] = {}
    if not email:
        errors["email"] = "Enter the email address you signed up with."
    if not password:
        errors["password"] = "Enter your password."
    if not errors:
        row = None
        if len(email) <= EMAIL_MAX:
            with db.session() as conn:
                row = conn.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
        ok = verify_password(password[:PASSWORD_MAX], row["password_hash"] if row else _DUMMY_HASH)
        if row is None or not ok or len(password) > PASSWORD_MAX:
            errors["email"] = "The email or password is not right. Check both and try again."
    if errors:
        return render(request, "auth/login.html", title="Sign in: please fix the problems",
                      values={"email": email[:300]}, errors=errors, next=next_url, status_code=400)
    request.session.clear()
    request.session["user_id"] = row["id"]
    announce(request, next_url, "You are signed in")
    return redirect(next_url)


@router.post("/logout")
async def logout(request: Request):
    await checked_form(request)
    request.session.clear()
    announce(request, "/", "You are signed out")
    return redirect("/")
