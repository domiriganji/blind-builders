"""Accounts: register, sign in, sign out."""
from __future__ import annotations

import re
import secrets
import sqlite3
import unicodedata

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from . import credits, db, limits
from .security import hash_password, verify_password
from .sensitive import API_KEY_MESSAGE, SECRET_MESSAGE
from .web import (checked_form, current_user, end_all_sessions, error_title, flash, forget_kept_form, has_kept_form,
                  redirect, render, require_user, sign_in)

router = APIRouter()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_MAX = 254          # the longest address email standards allow
NAME_MAX = 80            # same limit as the profile page
DETAIL_MAX = 100         # screen reader and device, same limit as the profile page
PASSWORD_MIN, PASSWORD_MAX = 10, 200
# Compared against when the email is unknown, so a wrong email takes as long as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
# Wrong passwords are limited per email address and internet address together, per internet address, and
# (much higher, so one stranger cannot lock a member out) per email address. A refused try runs no hashing.
LOGIN_WINDOW = 15 * 60
LOGIN_MAX_PER_EMAIL_AND_IP = 10
LOGIN_MAX_PER_IP = 30
LOGIN_MAX_PER_EMAIL = 100
LOGIN_TOO_MANY = ("Several sign-in tries did not work, so sign-in is paused for 15 minutes to protect your account "
                  "from guessing. Your account and your work are unchanged. Please try again after that.")
PASSWORD_CLEARED = ("The password box was emptied so your password is not kept on the page. "
                    "Please type your password again.")
REGISTER_WINDOW = 60 * 60
WELCOME_PER_IP_SECONDS = 30 * 24 * 60 * 60
DAY = 24 * 60 * 60
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
        return API_KEY_MESSAGE
    for run in _CARD_RUN_RE.finditer(text):
        digits = re.sub(r"\D", "", run.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return SECRET_MESSAGE
    if _PASSWORD_SAID_RE.search(text):
        return SECRET_MESSAGE
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


# Where a sign-in link was sent from, in words: "Sign in to use Build."
_NEXT_WORDS = {"/account": "to see Your account", "/jobs": "to see Your recent work",
               "/help": "to use Ask the community"}


def next_words(request: Request, next_url: str) -> str:
    """Why the member is asked to sign in, from the page they came from, or "" for the home page."""
    path = next_url.split("?", 1)[0]
    if path in ("", "/"):
        return ""
    for item in request.app.state.nav:
        if path == item["url"] or path.startswith(item["url"] + "/"):
            return f"to use {item['label']}"
    for prefix, words in _NEXT_WORDS.items():
        if path == prefix or path.startswith(prefix + "/"):
            return words
    return "to continue"


@router.get("/register")
def register_form(request: Request, next: str = "/"):
    user = current_user(request)
    if user:
        return render(request, "auth/register.html", title="You are already signed in", values={}, errors={},
                      next=_safe_next(next), signed_in_as=user["email"])
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
        errors["display_name"] = "Enter your name. Other members see your code name unless you choose to show your name."
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
    settings = request.app.state.settings
    ip_key = limits.key(limits.client_ip(request))
    if not errors and limits.count("register", ip_key, REGISTER_WINDOW) >= settings.registrations_per_ip_per_hour:
        errors["email"] = ("Several new accounts were made from your internet connection in the last hour, so "
                           "making another is paused for an hour. Nothing was saved. Please try again after that.")
    if not errors:
        with db.session() as conn:
            taken = conn.execute("SELECT 1 FROM users WHERE email = ?", (values["email"].lower(),)).fetchone()
        if taken:
            errors["email"] = "An account with this email already exists. Sign in instead, or use another email."
    if not errors:
        password_hash = await run_in_threadpool(hash_password, password)
        try:
            with db.session() as conn:
                cur = conn.execute(
                    "INSERT INTO users (email, display_name, password_hash, pseudonym, screen_reader, device, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (values["email"].lower(), values["display_name"], password_hash,
                     "Member " + secrets.token_hex(2).upper(), values["screen_reader"], values["device"], db.now()),
                )
                user_id = cur.lastrowid
        except sqlite3.IntegrityError:
            errors["email"] = "An account with this email already exists. Sign in instead, or use another email."
    if errors:
        for name in ("email", "display_name", "screen_reader", "device"):
            values[name] = values[name][:300]
        if "password" not in errors:
            errors["password"] = PASSWORD_CLEARED
        return render(request, "auth/register.html", title=error_title("Create your account", errors),
                      values=values, errors=errors, next=next_url, status_code=400)
    limits.record("register", ip_key, REGISTER_WINDOW)
    _give_welcome_credits(settings, user_id, ip_key)
    sign_in(request, user_id)
    from .features import recovery  # local import: features import auth's helpers
    code = await run_in_threadpool(recovery.set_new_code, user_id)
    recovery.show_once(request, code, "Your account is ready: save your recovery code", next_url,
                       done="Your account is ready")
    return redirect("/account/recovery-code")


def _give_welcome_credits(settings, user_id: int, ip_key: str) -> None:
    """Welcome credits, if the site gives them: one account per internet address every 30 days, and at most
    settings.signup_credits_per_day accounts a day across the whole site."""
    if settings.signup_credits <= 0:
        return
    if limits.count("welcome_ip", ip_key, WELCOME_PER_IP_SECONDS):
        return
    if limits.count("welcome_day", "site", DAY) >= settings.signup_credits_per_day:
        return
    credits.add(user_id, settings.signup_credits, "Welcome credits")
    limits.record("welcome_ip", ip_key, WELCOME_PER_IP_SECONDS)
    limits.record("welcome_day", "site", DAY)


def _login_keys(email: str, ip: str) -> list[tuple[str, int]]:
    return [(limits.key(email, ip), LOGIN_MAX_PER_EMAIL_AND_IP), (limits.key("ip", ip), LOGIN_MAX_PER_IP),
            (limits.key(email), LOGIN_MAX_PER_EMAIL)]


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    if current_user(request):
        return redirect(_safe_next(next))
    next_url = _safe_next(next)
    return render(request, "auth/login.html", title="Sign in", values={}, errors={}, next=next_url,
                  reason=next_words(request, next_url), words_kept=has_kept_form(request))


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
    status_code = 400
    if not errors:
        ip = limits.client_ip(request)
        keys = _login_keys(email[:EMAIL_MAX], ip)
        if any(limits.count("login", k, LOGIN_WINDOW) >= most for k, most in keys):
            errors["email"] = LOGIN_TOO_MANY
            status_code = 429
        else:
            row = None
            if len(email) <= EMAIL_MAX:
                with db.session() as conn:
                    row = conn.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
            ok = await run_in_threadpool(verify_password, password[:PASSWORD_MAX],
                                         row["password_hash"] if row else _DUMMY_HASH)
            if row is None or not ok or len(password) > PASSWORD_MAX:
                errors["email"] = "The email or password is not right. Check both and try again."
                for k, _ in keys:
                    limits.record("login", k, LOGIN_WINDOW, subject=limits.key(email[:EMAIL_MAX]))
            else:
                limits.clear("login", keys[0][0])
    if errors:
        if "password" not in errors:
            errors["password"] = PASSWORD_CLEARED
        return render(request, "auth/login.html", title=error_title("Sign in", errors),
                      values={"email": email[:300]}, errors=errors, next=next_url, status_code=status_code,
                      reason=next_words(request, next_url), words_kept=has_kept_form(request))
    sign_in(request, row["id"])
    announce(request, next_url, "You are signed in")
    return redirect(next_url)


@router.post("/logout")
async def logout(request: Request):
    await checked_form(request)
    forget_kept_form(request)
    request.session.clear()
    announce(request, "/", "You are signed out")
    return redirect("/")


@router.post("/logout-everywhere")
async def logout_everywhere(request: Request, user: dict = Depends(require_user)):
    """Sign out on every device and browser, including this one (for a lost phone or a shared computer)."""
    await checked_form(request)
    with db.session() as conn:
        end_all_sessions(conn, user["id"])
    forget_kept_form(request)
    request.session.clear()
    announce(request, "/", "You are signed out on every device")
    return redirect("/")
