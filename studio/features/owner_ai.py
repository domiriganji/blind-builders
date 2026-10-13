"""The site owner's AI settings page (/account/admin/ai): the site's own Claude API key, kept safe, the model
members use with it, who pays, and monthly spending limits. The rules live in studio/site_ai.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from .. import credits, limits, site_ai
from ..security import verify_password
from ..web import checked_form, date_words, error_title, flash, form_text, redirect, render, require_user
from . import account

router = APIRouter()
NAV = None

PAGE = "/account/admin/ai"
TITLE = "AI settings for your key"
PASSWORD_TRIES_PER_HOUR = 5
HOUR = 60 * 60
MAX_LIMIT_CENTS = 1_000_000  # $10,000


def _values() -> dict:
    return {
        "model": site_ai.get("model") or "",
        "pay_mode": site_ai.pay_mode(),
        "site_month": _dollars(site_ai.site_month_limit()),
        "member_month": _dollars(site_ai.member_month_limit()),
    }


def _dollars(cents: int) -> str:
    return "0" if not cents else credits.format_cents(cents).lstrip("$")


def _render(request: Request, user: dict, *, values: dict | None = None, errors: dict | None = None,
            key_errors: dict | None = None, status_code: int = 200):
    settings = request.app.state.settings
    errors, key_errors = errors or {}, key_errors or {}
    title = account._done_title(request, TITLE)
    if errors or key_errors:
        title = error_title(TITLE, {**errors, **key_errors})
    values = values or _values()
    if values.get("model") not in site_ai.MODEL_NAMES:
        values["model"] = site_ai.site_model(settings)
    model_options = [(model_id, f"{name}{', recommended' if model_id == 'claude-opus-5-5' else ''}: {about}")
                     for model_id, name, about in site_ai.MODELS]
    return render(
        request, "owner/ai.html", title=title, values=values, errors=errors, key_errors=key_errors,
        key_source=site_ai.key_source(settings), key_last4=site_ai.key_last4(settings),
        key_unreadable=site_ai.key_unreadable(settings),
        key_saved_at=date_words(site_ai.get("site_key_saved_at")),
        model_options=model_options, pay_modes=site_ai.PAY_MODES,
        spent=credits.format_cents(site_ai.spent_this_month()),
        renews=site_ai.renews_on(),
        status_code=status_code)


def _password_problem(request: Request, user: dict, password: str) -> str | None:
    """A plain message if the owner's password is missing or not right. Wrong tries are limited."""
    key = limits.key("owner-password", user["id"])
    if limits.count("owner_password", key, HOUR) >= PASSWORD_TRIES_PER_HOUR:
        return ("Several password tries did not work, so changing the site's key is paused for an hour to protect "
                "the site. Nothing was changed. Please try again after that.")
    if not password:
        return "Enter the password you sign in with. Changing the site's key needs it."
    if not verify_password(password[:1000], user["password_hash"]):
        limits.record("owner_password", key, HOUR)
        return "That is not the password you sign in with, so nothing was changed. Check it and try again."
    return None


@router.get(PAGE)
def page(request: Request, user: dict = Depends(require_user)):
    account._require_admin(user)
    return _render(request, user)


@router.post(PAGE)
async def save_settings(request: Request, user: dict = Depends(require_user)):
    account._require_admin(user)
    form = await checked_form(request)
    values = {
        "model": form_text(form, "model", 40),
        "pay_mode": form_text(form, "pay_mode", 20),
        "site_month": form_text(form, "site_month", 20),
        "member_month": form_text(form, "member_month", 20),
    }
    errors: dict[str, str] = {}
    if values["model"] not in site_ai.MODEL_NAMES:
        errors["model"] = "Choose one of the models in the list."
    if values["pay_mode"] not in dict(site_ai.PAY_MODES):
        errors["pay_mode"] = "Choose who pays for AI on your key."
    amounts = {}
    for name, label in (("site_month", "for the whole site"), ("member_month", "for each member")):
        cents = account._parse_dollars(values[name] or "0")
        if cents is None:
            errors[name] = f"Enter the monthly limit {label} in US dollars, like 50 or 2.50, or 0 for no limit."
        elif cents > MAX_LIMIT_CENTS:
            errors[name] = f"Enter at most {credits.format_cents(MAX_LIMIT_CENTS)}."
        else:
            amounts[name] = cents
    if (values["pay_mode"] == "free" and "site_month" in amounts and "member_month" in amounts
            and not amounts["site_month"] and not amounts["member_month"]):
        errors["site_month"] = ("When the AI is free for members, set at least one limit, so your bill "
                                "cannot grow without end.")
    if errors:
        return _render(request, user, values=values, errors=errors, status_code=400)
    site_ai.save({"model": values["model"], "pay_mode": values["pay_mode"],
                  "site_month_cents": amounts["site_month"], "member_month_cents": amounts["member_month"]},
                 user["id"])
    account._announce(request, "Your AI settings are saved")
    return redirect(PAGE)


@router.post(PAGE + "/key")
async def save_key(request: Request, user: dict = Depends(require_user)):
    account._require_admin(user)
    form = await checked_form(request)
    key = str(form.get("api_key", "")).strip()
    key_errors: dict[str, str] = {}
    problem = account.check_key_format(key)
    if problem:
        key_errors["api_key"] = problem
    password_problem = _password_problem(request, user, str(form.get("password", "")))
    if password_problem:
        key_errors["password"] = password_problem
    if not key_errors:
        result = await run_in_threadpool(account.verify_api_key, key)
        if result is not True:
            key_errors["api_key"] = str(result) if result else "Anthropic did not accept this key, so it was not saved."
    if key_errors:
        return _render(request, user, key_errors=key_errors, status_code=400)
    site_ai.save_key(request.app.state.settings, key, user["id"])
    account._announce(request, "The site's Claude API key is saved")
    flash(request, f"The key ending in {key[-4:]} is saved, encrypted. Members' AI work on the site's key uses it "
                   "from now on.", "success")
    return redirect(PAGE)


@router.post(PAGE + "/key/remove")
async def remove_key(request: Request, user: dict = Depends(require_user)):
    account._require_admin(user)
    form = await checked_form(request)
    problem = _password_problem(request, user, str(form.get("remove_password", "")))
    if problem:
        return _render(request, user, key_errors={"remove_password": problem}, status_code=400)
    site_ai.remove_key()
    account._announce(request, "The site's Claude API key is removed")
    flash(request, "The key is removed from this site. To stop it working everywhere, delete it in the Anthropic "
                   "Console too.", "success")
    return redirect(PAGE)
