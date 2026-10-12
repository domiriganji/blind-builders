"""Accounts: register, sign in, sign out."""
from __future__ import annotations

import re
import secrets
import sqlite3

from fastapi import APIRouter, Request

from . import credits, db
from .security import hash_password, verify_password
from .web import checked_form, current_user, flash, redirect, render

router = APIRouter()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _safe_next(url: str | None) -> str:
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return "/"


@router.get("/register")
def register_form(request: Request, next: str = "/"):
    return render(request, "auth/register.html", title="Create your account", values={}, errors={}, next=_safe_next(next))


@router.post("/register")
async def register(request: Request):
    form = await checked_form(request)
    values = {k: str(form.get(k, "")).strip() for k in ("email", "display_name", "screen_reader", "device")}
    password = str(form.get("password", ""))
    errors: dict[str, str] = {}
    if not EMAIL_RE.match(values["email"]):
        errors["email"] = "Enter an email address, like name@example.com."
    if not values["display_name"]:
        errors["display_name"] = "Enter the name you want other members to see."
    if len(password) < 10:
        errors["password"] = "Use at least 10 characters for your password."
    next_url = _safe_next(str(form.get("next", "/")))
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
            errors["email"] = "An account with this email already exists. Sign in instead."
    if errors:
        return render(request, "auth/register.html", title="Create your account: please fix the problems",
                      values=values, errors=errors, next=next_url, status_code=400)
    signup = request.app.state.settings.signup_credits
    if signup > 0:
        credits.add(user_id, signup, "Welcome credits")
    request.session.clear()
    request.session["user_id"] = user_id
    flash(request, "Your account is ready. Welcome.", "success")
    return redirect(next_url)


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    if current_user(request):
        return redirect(_safe_next(next))
    return render(request, "auth/login.html", title="Sign in", values={}, errors={}, next=_safe_next(next))


@router.post("/login")
async def login(request: Request):
    form = await checked_form(request)
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    next_url = _safe_next(str(form.get("next", "/")))
    with db.session() as conn:
        row = conn.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        return render(request, "auth/login.html", title="Sign in: please fix the problem", values={"email": email},
                      errors={"email": "The email or password is not right."}, next=next_url, status_code=400)
    request.session.clear()
    request.session["user_id"] = row["id"]
    flash(request, "You are signed in.", "success")
    return redirect(next_url)


@router.post("/logout")
async def logout(request: Request):
    await checked_form(request)
    request.session.clear()
    flash(request, "You are signed out.", "success")
    return redirect("/")
