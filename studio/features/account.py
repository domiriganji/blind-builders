"""Your account and How paying works.

- /account: who you are and how you pay for AI, summary first.
- /account/profile: name, screen reader, device, language, blind or low vision since, answer style, code name.
- /account/password: change your password while signed in.
- /account/ai-key: connect or remove your own Claude API key (stored encrypted, only the last 4 shown).
- /account/credits: balance, credit history and AI usage history.
- /account/credits/buy: buy credits through Stripe Checkout, only when the site owner has set up payments.
- /payments/stripe/webhook: Stripe tells the site a payment went through (signed, no CSRF, JSON).
- /account/admin: site owner tools (members with is_admin = 1 only).
- /account/delete: anonymise the account.
- /pricing: how paying works, with rough costs worked out from the site's own price list.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .. import credits, db, limits, site_ai
from ..ai import PRICES, WEB_SEARCH_DOLLARS, cost_cents
from ..auth import PASSWORD_MAX, PASSWORD_MIN, clean_text, secret_problem
from ..security import decrypt_secret, encrypt_secret, hash_password, verify_password
from ..web import (KEPT_FORMS_SCHEMA, checked_form, date_words, end_all_sessions, error_title, flash, redirect,
                   render, require_user, sign_in)

log = logging.getLogger("studio.account")
router = APIRouter()
NAV = None
SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    provider TEXT NOT NULL,
    provider_ref TEXT NOT NULL UNIQUE,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    credits_cents INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
""" + KEPT_FORMS_SCHEMA  # members' words kept from a form that could not be sent (studio/web.py, keep_form)


def seed() -> None:
    with db.session() as conn:
        # "Which fits you best": blind, low vision or prefer not to say. It used to be an answer to "when", which
        # does not fit someone born blind, so earlier "I have low vision" answers move here.
        db.ensure_column(conn, "users", "vision", "TEXT NOT NULL DEFAULT 'prefer_not'")
        conn.execute("UPDATE users SET vision = 'low_vision', sight_onset = 'prefer_not'"
                     " WHERE sight_onset = 'low_vision'")

# ----- choices for the profile form -------------------------------------------------------------

SIGHT_ONSET = [
    ("birth", "Birth or early childhood"),
    ("later", "Later in life"),
    ("prefer_not", "Prefer not to say"),
]
VISION = [
    ("blind", "Blind"),
    ("low_vision", "Low vision"),
    ("prefer_not", "Prefer not to say"),
]
VERBOSITY = [
    ("summary_first", "Summary first, then details"),
    ("full", "Full detail"),
]
# How the AI describes the look of what a member builds. The effect only is the default (the organizer's choice).
DESCRIBE_LOOKS = [
    ("effect", "The effect only, for example: looks calm and finished, easy to read on a phone"),
    ("colours", "The effect, then the main colours in plain words"),
]
LANGUAGES = [
    ("en", "English"),
    ("ar", "Arabic"),
    ("bn", "Bengali"),
    ("zh", "Chinese"),
    ("nl", "Dutch"),
    ("fr", "French"),
    ("de", "German"),
    ("ha", "Hausa"),
    ("hi", "Hindi"),
    ("ig", "Igbo"),
    ("id", "Indonesian"),
    ("it", "Italian"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("fa", "Persian"),
    ("pl", "Polish"),
    ("pt", "Portuguese"),
    ("ru", "Russian"),
    ("es", "Spanish"),
    ("sw", "Swahili"),
    ("tr", "Turkish"),
    ("uk", "Ukrainian"),
    ("ur", "Urdu"),
    ("vi", "Vietnamese"),
    ("yo", "Yoruba"),
    ("other", "Another language"),
]

# Section names for the AI usage history ("build.understand" -> "Build: understand").
SECTION_LABELS = {
    "build": "Build",
    "errands": "Do this for me",
    "write": "Write and publish",
    "find": "Find before you build",
    "research": "Research",
    "checks": "Peer checks",
    "ai_tools": "AI tools report",
    "library": "Tool library",
    "learn": "Learn and help",
    "help": "Ask the community",
    "account": "Your account",
}

# Credit packs in US cents. One cent of credit pays for one cent of AI cost including the markup.
PACKS = [500, 1000, 2000]
MAX_GRANT_CENTS = 100_000
KEY_RE = re.compile(r"^sk-ant-[A-Za-z0-9_\-]+$")
KEY_MIN, KEY_MAX = 40, 300
STRIPE_CHECKOUT_URL = "https://api.stripe.com/v1/checkout/sessions"
STRIPE_TOLERANCE_SECONDS = 300
STRIPE_MAX_BODY = 256 * 1024  # Checkout Session events are a few kilobytes

# Rough sizes of typical actions, used only for the estimates on the pricing page.
# (what you do, input tokens, output tokens, web searches)
ESTIMATES = [
    ("Understanding an idea and reading it back to you", 3_000, 1_500, 0),
    ("A short answer or summary", 2_000, 800, 0),
    ("Checking a 2,000-word piece of writing", 6_000, 6_000, 0),
    ("Building a small website (one version)", 15_000, 20_000, 0),
    ("A web search in Do this for me, or a research question", 30_000, 4_000, 5),
]


# ----- helpers ----------------------------------------------------------------------------------

def _settings(request: Request):
    return request.app.state.settings


def _key_last4(settings, user: dict) -> str | None:
    """The last 4 characters of the member's saved key, or None if there is no readable key."""
    if not user.get("own_api_key_enc"):
        return None
    key = decrypt_secret(settings.secret_key, user["own_api_key_enc"])
    return key[-4:] if key else None


def payments_ready(settings) -> bool:
    """Credits can be bought only when Stripe is set up and the site has its own Claude key to spend them on."""
    return bool(settings.stripe_secret_key and site_ai.site_key(settings))


def ai_status(settings, user: dict) -> dict:
    """How this member pays for AI, worked out the same way studio.ai.default_factory does."""
    balance = credits.balance(user["id"])
    status = {
        "balance": balance,
        "balance_text": credits.format_cents(balance),
        "site_has_key": site_ai.key_source(settings) is not None,
        "can_buy": payments_ready(settings),
        "key_unreadable": False,
    }
    if user.get("own_api_key_enc"):
        last4 = _key_last4(settings, user)
        if last4:
            return {**status, "mode": "own_key", "last4": last4}
        status["key_unreadable"] = True
    if status["site_has_key"] and site_ai.pay_mode() == "free":
        left = site_ai.member_left_this_month(user["id"])
        limit = site_ai.member_month_limit()
        return {**status, "mode": "free", "renews": site_ai.renews_on(),
                "free_left_text": credits.format_cents(left) if left is not None else "",
                "free_limit_text": credits.format_cents(limit) if limit else ""}
    if status["site_has_key"] and balance > 0:
        return {**status, "mode": "credits"}
    return {**status, "mode": "none"}


def _purpose_label(purpose: str) -> str:
    section, _, step = purpose.partition(".")
    label = SECTION_LABELS.get(section, section.replace("_", " ").capitalize())
    step = step.replace("_", " ").replace(".", ", ")
    return f"{label}: {step}" if step else label


def _reason_text(reason: str) -> str:
    """studio.ai records AI charges as "AI: <purpose>"; show the purpose in words."""
    if reason.startswith("AI: "):
        return "AI: " + _purpose_label(reason[4:])
    return reason


def _date(text: str) -> str:
    return date_words(text)


def typical_costs(settings) -> tuple[str, str]:
    """The cheapest and dearest typical task on the pricing page, at Anthropic's prices for the site's model."""
    model = site_ai.site_model(settings)
    cents = [cost_cents(model, tin, tout, searches, 1.0) for _, tin, tout, searches in ESTIMATES]
    return _estimate_text(min(cents)), _estimate_text(max(cents))


def _require_admin(user: dict) -> None:
    if not user.get("is_admin"):
        raise HTTPException(status_code=404)


def _clean(form: dict, name: str, limit: int) -> str:
    return clean_text(form.get(name, ""), limit)


def _announce(request: Request, heading: str) -> None:
    """The next account page's h1 says what just happened (see _done_title)."""
    request.session["account_heading"] = heading


def _done_title(request: Request, default: str) -> str:
    heading = request.session.pop("account_heading", None)
    return heading if isinstance(heading, str) and heading else default


def _parse_dollars(text: str) -> int | None:
    text = text.strip().lstrip("$").strip()
    if not re.fullmatch(r"\d{1,6}(\.\d{1,2})?", text):
        return None
    whole, _, frac = text.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


# ----- account summary --------------------------------------------------------------------------

@router.get("/account")
def index(request: Request, user: dict = Depends(require_user)):
    settings = _settings(request)
    return render(request, "account/index.html", title=_done_title(request, "Your account"),
                  status=ai_status(settings, user), sight=dict(SIGHT_ONSET), vision=dict(VISION),
                  verbosity=dict(VERBOSITY))


# ----- profile ----------------------------------------------------------------------------------

def _profile_values(user: dict) -> dict:
    return {
        "display_name": user["display_name"],
        "screen_reader": user["screen_reader"],
        "device": user["device"],
        "language": user["language"],
        "sight_onset": user["sight_onset"] if user["sight_onset"] in dict(SIGHT_ONSET) else "prefer_not",
        "vision": user.get("vision") or "prefer_not",
        "verbosity": user["verbosity"],
        "show_name_on_checks": bool(user["show_name_on_checks"]),
        "show_name_on_help": bool(user["show_name_on_help"]),
        "describe_looks": "colours" if user["describe_colours"] else "effect",
    }


def _render_profile(request: Request, user: dict, values: dict, errors: dict, status_code: int = 200):
    title = error_title("Your profile and preferences", errors)
    return render(request, "account/profile.html", title=title, values=values, errors=errors,
                  pseudonym=user["pseudonym"], languages=LANGUAGES, sight_options=SIGHT_ONSET,
                  vision_options=VISION, verbosity_options=VERBOSITY, looks_options=DESCRIBE_LOOKS,
                  status_code=status_code)


@router.get("/account/profile")
def profile_form(request: Request, user: dict = Depends(require_user)):
    return _render_profile(request, user, _profile_values(user), {})


@router.post("/account/profile")
async def profile_save(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    values = {
        "display_name": _clean(form, "display_name", 200),
        "screen_reader": _clean(form, "screen_reader", 200),
        "device": _clean(form, "device", 200),
        "language": _clean(form, "language", 20),
        "sight_onset": _clean(form, "sight_onset", 20) or "prefer_not",
        "vision": _clean(form, "vision", 20) or "prefer_not",
        "verbosity": _clean(form, "verbosity", 20),
        "show_name_on_checks": form.get("show_name_on_checks") == "yes",
        "show_name_on_help": form.get("show_name_on_help") == "yes",
        "describe_looks": _clean(form, "describe_looks", 20) or "effect",
    }
    errors: dict[str, str] = {}
    if not values["display_name"]:
        errors["display_name"] = "Enter your name. Other members see your code name unless you choose to show your name."
    elif len(values["display_name"]) > 80:
        errors["display_name"] = "Use 80 characters or fewer for your name."
    if len(values["screen_reader"]) > 100:
        errors["screen_reader"] = "Use 100 characters or fewer for your screen reader."
    if len(values["device"]) > 100:
        errors["device"] = "Use 100 characters or fewer for your device."
    if values["language"] not in dict(LANGUAGES):
        errors["language"] = "Choose a language from the list."
    if values["sight_onset"] not in dict(SIGHT_ONSET):
        errors["sight_onset"] = "Choose one of the options, or Prefer not to say."
    if values["vision"] not in dict(VISION):
        errors["vision"] = "Choose Blind, Low vision or Prefer not to say."
    if values["verbosity"] not in dict(VERBOSITY):
        errors["verbosity"] = "Choose how you want answers: summary first, or full detail."
    if values["describe_looks"] not in dict(DESCRIBE_LOOKS):
        errors["describe_looks"] = "Choose how the AI should describe looks: the effect only, or the effect and colours."
    for name in ("display_name", "screen_reader", "device"):
        problem = secret_problem(values[name])
        if problem:
            errors[name] = problem
            values[name] = ""  # never show the secret again, not even in the form
    if errors:
        return _render_profile(request, user, values, errors, status_code=400)
    with db.session() as conn:
        conn.execute(
            "UPDATE users SET display_name = ?, screen_reader = ?, device = ?, language = ?, sight_onset = ?,"
            " vision = ?, verbosity = ?, show_name_on_checks = ?, show_name_on_help = ?, describe_colours = ?"
            " WHERE id = ?",
            (values["display_name"], values["screen_reader"], values["device"], values["language"],
             values["sight_onset"], values["vision"], values["verbosity"], 1 if values["show_name_on_checks"] else 0,
             1 if values["show_name_on_help"] else 0, 1 if values["describe_looks"] == "colours" else 0,
             user["id"]),
        )
    _announce(request, "Your profile is saved")
    return redirect("/account")


# ----- change password --------------------------------------------------------------------------

PASSWORD_TRIES_PER_HOUR = 10
HOUR = 60 * 60


def _render_password(request: Request, errors: dict | None = None, status_code: int = 200):
    errors = errors or {}
    return render(request, "account/password.html", title=error_title("Change your password", errors),
                  errors=errors, status_code=status_code)


@router.get("/account/password")
def password_form(request: Request, user: dict = Depends(require_user)):
    return _render_password(request)


@router.post("/account/password")
async def password_save(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    current = str(form.get("current_password", ""))[:PASSWORD_MAX + 1]
    new = str(form.get("new_password", ""))[:PASSWORD_MAX + 1]
    errors: dict[str, str] = {}
    try_key = limits.key("change-password", user["id"])
    if not current:
        errors["current_password"] = "Enter the password you use now."
    elif limits.count("change_password", try_key, HOUR) >= PASSWORD_TRIES_PER_HOUR:
        errors["current_password"] = ("Several tries did not work, so changing the password is paused for an hour "
                                      "to protect your account. Your password is unchanged. Please try again "
                                      "after that.")
    elif not await run_in_threadpool(verify_password, current[:PASSWORD_MAX], user["password_hash"]):
        limits.record("change_password", try_key, HOUR, subject=limits.key(user["email"]))
        errors["current_password"] = "That is not the password you use now. Check it and try again."
    if len(new) < PASSWORD_MIN:
        errors["new_password"] = f"Use at least {PASSWORD_MIN} characters for your new password."
    elif len(new) > PASSWORD_MAX:
        errors["new_password"] = f"Use {PASSWORD_MAX} characters or fewer for your new password."
    if errors:
        if "new_password" not in errors:
            errors["new_password"] = ("The password boxes were emptied so your passwords are not kept on the page. "
                                      "Please type your new password again.")
        return _render_password(request, errors, status_code=400)
    password_hash = await run_in_threadpool(hash_password, new)
    with db.session() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user["id"]))
        end_all_sessions(conn, user["id"])  # anyone else signed in to this account is signed out
    limits.clear("change_password", try_key)
    sign_in(request, user["id"])  # this browser stays signed in
    _announce(request, "Your password is changed")
    flash(request, "Use your new password from now on. Any other device signed in to your account is signed out. "
                   "Your recovery code still works.", "success")
    return redirect("/account")


# ----- own Claude API key -----------------------------------------------------------------------

def check_key_format(key: str) -> str | None:
    """A plain message if the text cannot be a Claude API key, otherwise None."""
    if not key:
        return "Paste your Claude API key."
    if any(ch.isspace() for ch in key):
        return "The key has a space or line break in it. Copy it again, without spaces."
    if not key.startswith("sk-ant-"):
        return "A Claude API key starts with sk-ant-. Check you copied the whole key."
    if len(key) < KEY_MIN:
        return "This is shorter than a Claude API key. Check you copied the whole key."
    if len(key) > KEY_MAX:
        return "This is longer than a Claude API key. Check you copied only the key."
    if not KEY_RE.match(key):
        return "The key has characters a Claude API key does not use. Copy it again."
    return None


def verify_api_key(key: str) -> bool | str:
    """Ask Anthropic whether the key works. True if it does, otherwise a plain message saying why not."""
    try:
        anthropic.Anthropic(api_key=key, max_retries=1, timeout=20.0).models.list(limit=1)
    except anthropic.AuthenticationError:
        return ("Anthropic did not accept this key, so it was not saved. It may have been deleted, or part of it "
                "may be missing. Check the key in the Anthropic Console and paste it again.")
    except anthropic.PermissionDeniedError:
        return ("Anthropic says this key is not allowed to use Claude, so it was not saved. Check the key's settings "
                "in the Anthropic Console.")
    except anthropic.RateLimitError:
        return ("Anthropic is busy right now and could not check the key, so it was not saved. "
                "Please try again in a minute.")
    except anthropic.APIConnectionError:
        return "The site could not reach Anthropic to check the key, so it was not saved. Please try again in a minute."
    except anthropic.APIStatusError as exc:
        return (f"Anthropic had a problem on its side (code {exc.status_code}), so the key was not checked or saved. "
                "Please try again later.")
    except Exception:  # noqa: BLE001 - any other failure is shown as a plain message, never a crash
        log.exception("unexpected error while checking an API key")
        return "Something went wrong on our side while checking the key, so it was not saved. Please try again later."
    return True


def _render_key(request: Request, user: dict, error: str = "", status_code: int = 200):
    settings = _settings(request)
    title = error_title("Your Claude API key", [error]) if error else _done_title(request, "Your Claude API key")
    site_model = site_ai.site_model(settings)
    model_options = [("", f"The site's choice, now {site_ai.model_name(site_model)}")] + [
        (model_id, f"{name}: {about}") for model_id, name, about in site_ai.MODELS]
    return render(request, "account/ai_key.html", title=title, errors={"api_key": error} if error else {},
                  last4=_key_last4(settings, user), has_saved=bool(user.get("own_api_key_enc")),
                  model_options=model_options, own_model=user.get("own_model") or "",
                  costs=typical_costs(settings), status_code=status_code)


@router.get("/account/ai-key")
def key_form(request: Request, user: dict = Depends(require_user)):
    return _render_key(request, user)


@router.post("/account/ai-key")
async def key_save(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    key = str(form.get("api_key", "")).strip()
    error = check_key_format(key)
    if error is None:
        result = await run_in_threadpool(verify_api_key, key)
        if result is not True:
            error = str(result) if result else "Anthropic did not accept this key, so it was not saved."
    if error:
        return _render_key(request, user, error, status_code=400)
    settings = _settings(request)
    with db.session() as conn:
        conn.execute("UPDATE users SET own_api_key_enc = ? WHERE id = ?",
                     (encrypt_secret(settings.secret_key, key), user["id"]))
    _announce(request, "Your Claude API key is connected")
    flash(request, f"Your Claude API key ending in {key[-4:]} is connected. The AI now uses it, "
                   "Anthropic bills you directly, and this site charges nothing for AI.", "success")
    return redirect("/account")


@router.post("/account/ai-key/model")
async def key_model(request: Request, user: dict = Depends(require_user)):
    """The model the AI uses with the member's own key."""
    form = await checked_form(request)
    choice = _clean(form, "own_model", 40)
    if choice and choice not in site_ai.MODEL_NAMES:
        return _render_key(request, user, "Choose one of the models in the list.", status_code=400)
    with db.session() as conn:
        conn.execute("UPDATE users SET own_model = ? WHERE id = ?", (choice, user["id"]))
    name = site_ai.model_name(choice) if choice else "the site's choice"
    _announce(request, "Your model choice is saved")
    flash(request, f"With your own key, the AI now uses {name}.", "success")
    return redirect("/account/ai-key")


@router.post("/account/ai-key/remove")
async def key_remove(request: Request, user: dict = Depends(require_user)):
    await checked_form(request)
    with db.session() as conn:
        conn.execute("UPDATE users SET own_api_key_enc = NULL WHERE id = ?", (user["id"],))
    _announce(request, "Your Claude API key is removed")
    flash(request, "Your Claude API key is removed from this site. To stop it working everywhere, "
                   "delete it in the Anthropic Console (console.anthropic.com) too.", "success")
    return redirect("/account")


# ----- credits and usage ------------------------------------------------------------------------

@router.get("/account/credits")
def credits_page(request: Request, paid: str = "", user: dict = Depends(require_user)):
    settings = _settings(request)
    ledger = [
        {**row, "date": _date(row["created_at"]), "reason": _reason_text(row["reason"]),
         "change": ("+" if row["delta_cents"] > 0 else "") + credits.format_cents(row["delta_cents"])}
        for row in credits.history(user["id"], 50)
    ]
    with db.session() as conn:
        rows = conn.execute(
            "SELECT purpose, cost_cents, paid_by, created_at FROM ai_usage WHERE user_id = ? ORDER BY id DESC LIMIT 50",
            (user["id"],),
        ).fetchall()
        totals = conn.execute(
            "SELECT paid_by, COUNT(*) AS n, COALESCE(SUM(cost_cents), 0) AS cents FROM ai_usage WHERE user_id = ?"
            " GROUP BY paid_by", (user["id"],),
        ).fetchall()
    usage = [
        {"date": _date(r["created_at"]), "label": _purpose_label(r["purpose"]),
         "cost": credits.format_cents(r["cost_cents"]), "paid_by": r["paid_by"]}
        for r in rows
    ]
    by_payer = {t["paid_by"]: {"count": t["n"], "cost": credits.format_cents(t["cents"])} for t in totals}
    title = "Thank you: your payment went to Stripe" if paid == "1" else "Your credits and AI usage"
    return render(request, "account/credits.html", title=title,
                  status=ai_status(settings, user), ledger=ledger, usage=usage, by_payer=by_payer,
                  just_paid=paid == "1")


def _pack_label(cents: int) -> str:
    return credits.format_cents(cents)


def _render_buy(request: Request, user: dict, errors: dict | None = None, status_code: int = 200,
                cancelled: bool = False):
    settings = _settings(request)
    if errors:
        title = error_title("Buy credits", errors)
    elif cancelled:
        title = "You left the payment page: nothing was charged"
    else:
        title = "Buy credits"
    return render(request, "account/buy.html", title=title, errors=errors or {},
                  packs=[(str(c), f"{_pack_label(c)} of credits") for c in PACKS],
                  stripe_ready=payments_ready(settings), markup_percent=_markup_percent(settings), status=ai_status(settings, user),
                  cancelled=cancelled, status_code=status_code)


@router.get("/account/credits/buy")
def buy_page(request: Request, cancelled: str = "", user: dict = Depends(require_user)):
    return _render_buy(request, user, cancelled=cancelled == "1")


class PaymentError(Exception):
    pass


def create_checkout_session(secret_key: str, *, user_id: int, email: str, amount_cents: int,
                            success_url: str, cancel_url: str, site_name: str) -> dict:
    """Create a Stripe Checkout Session with the standard library. Returns Stripe's JSON (with "id" and "url")."""
    fields = [
        ("mode", "payment"),
        ("line_items[0][quantity]", "1"),
        ("line_items[0][price_data][currency]", "usd"),
        ("line_items[0][price_data][unit_amount]", str(amount_cents)),
        ("line_items[0][price_data][product_data][name]",
         f"{credits.format_cents(amount_cents)} of AI credits on {site_name}"),
        ("success_url", success_url),
        ("cancel_url", cancel_url),
        ("client_reference_id", str(user_id)),
        ("metadata[user_id]", str(user_id)),
        ("metadata[credits_cents]", str(amount_cents)),
    ]
    if email and not email.endswith("@invalid"):
        fields.append(("customer_email", email))
    request = urllib.request.Request(
        STRIPE_CHECKOUT_URL,
        data=urllib.parse.urlencode(fields).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {secret_key}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            answer = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        log.warning("Stripe refused the checkout session: %s %s", exc.code, exc.read()[:500])
        raise PaymentError("The payment service did not accept the request.") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        log.warning("Could not reach Stripe: %s", exc)
        raise PaymentError("We could not reach the payment service.") from exc
    if not isinstance(answer, dict):
        raise PaymentError("The payment service gave an answer we did not understand.")
    return answer


@router.post("/account/credits/buy")
async def buy(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    settings = _settings(request)
    if not payments_ready(settings):
        flash(request, "Credits are not on sale on this site yet, so nothing was charged. You can use the AI by "
                       "connecting your own Claude API key.", "info")
        return redirect("/account/credits/buy")
    pack = _clean(form, "pack", 10)
    if not pack.isdigit() or int(pack) not in PACKS:
        return _render_buy(request, user, {"pack": "Choose how many credits to buy."}, status_code=400)
    amount = int(pack)
    try:
        session = await run_in_threadpool(
            create_checkout_session, settings.stripe_secret_key, user_id=user["id"], email=user["email"],
            amount_cents=amount, success_url=f"{settings.base_url}/account/credits?paid=1",
            cancel_url=f"{settings.base_url}/account/credits/buy?cancelled=1", site_name=settings.site_name,
        )
    except PaymentError as exc:
        flash(request, f"{exc} Nothing was charged. Please try again later.", "error")
        return redirect("/account/credits/buy")
    url = str(session.get("url") or "")
    session_id = str(session.get("id") or "")
    if not url.startswith("https://") or not session_id:
        flash(request, "The payment service gave an answer we did not understand. Nothing was charged. "
                       "Please try again later.", "error")
        return redirect("/account/credits/buy")
    try:
        with db.session() as conn:
            conn.execute(
                "INSERT INTO payments (user_id, provider, provider_ref, amount_cents, currency, credits_cents, status,"
                " created_at) VALUES (?, 'stripe', ?, ?, 'usd', ?, 'pending', ?)",
                (user["id"], session_id, amount, amount, db.now()),
            )
    except sqlite3.IntegrityError:
        pass  # the same session already recorded
    return redirect(url)


def verify_stripe_signature(payload: bytes, header: str, secret: str, now: float | None = None,
                            tolerance: int = STRIPE_TOLERANCE_SECONDS) -> bool:
    """Check a Stripe-Signature header: t=<time>,v1=<HMAC-SHA256 of "t.payload">. Constant-time compare."""
    if not header or not secret:
        return False
    timestamp = None
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    if not timestamp or not re.fullmatch(r"[0-9]{1,12}", timestamp) or not signatures:
        return False
    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), timestamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    # compare_digest refuses non-ASCII text, so compare bytes; a header with odd characters simply does not match.
    return any(hmac.compare_digest(expected.encode(), sig.encode("utf-8", "replace")) for sig in signatures)


def _record_paid_session(obj: dict) -> str:
    """Add credits for a paid Checkout Session, once. Returns what happened, for the JSON reply."""
    session_id = str(obj.get("id") or "")
    currency = str(obj.get("currency") or "").lower()
    amount = obj.get("amount_total")
    if not session_id or not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return "ignored: missing session id or amount"
    if currency != "usd":
        return "ignored: not US dollars"
    metadata = obj.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    # Only sessions this site created carry both of these. Another product on the same Stripe account
    # must never turn into credits for whichever member happens to have the same number.
    ref_user = str(metadata.get("user_id") or "")
    meta_credits = str(metadata.get("credits_cents") or "")
    with db.session() as conn:
        row = conn.execute("SELECT * FROM payments WHERE provider = 'stripe' AND provider_ref = ?",
                           (session_id,)).fetchone()
        if row is not None:
            if row["status"] == "paid":
                return "duplicate"
            user_id = row["user_id"]
            credit = min(row["credits_cents"], amount)
            updated = conn.execute("UPDATE payments SET status = 'paid', amount_cents = ? WHERE id = ? AND status != 'paid'",
                                   (amount, row["id"])).rowcount
            if updated != 1:
                return "duplicate"
        else:
            if not re.fullmatch(r"[0-9]{1,12}", ref_user) or not re.fullmatch(r"[0-9]{1,9}", meta_credits):
                return "ignored: not a credit purchase from this site"
            user_id = int(ref_user)
            if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
                return "ignored: unknown member"
            credit = min(int(meta_credits), amount)
            if credit <= 0:
                return "ignored: no credits on the session"
            try:
                conn.execute(
                    "INSERT INTO payments (user_id, provider, provider_ref, amount_cents, currency, credits_cents,"
                    " status, created_at) VALUES (?, 'stripe', ?, ?, 'usd', ?, 'paid', ?)",
                    (user_id, session_id, amount, credit, db.now()),
                )
            except sqlite3.IntegrityError:
                return "duplicate"
        conn.execute(
            "INSERT INTO credit_ledger (user_id, delta_cents, reason, ref, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, credit, "Bought credits", f"stripe:{session_id}", db.now()),
        )
    return "credited"


@router.post("/payments/stripe/webhook")
async def stripe_webhook(request: Request):
    """Called by Stripe, not by people: no CSRF token, a signed body instead. Replies with JSON."""
    settings = _settings(request)
    if not settings.stripe_webhook_secret:
        return JSONResponse({"error": "payments are not set up"}, status_code=400)
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > STRIPE_MAX_BODY:
        return JSONResponse({"error": "too large"}, status_code=413)
    payload = await request.body()
    if len(payload) > STRIPE_MAX_BODY:
        return JSONResponse({"error": "too large"}, status_code=413)
    if not verify_stripe_signature(payload, request.headers.get("stripe-signature", ""),
                                   settings.stripe_webhook_secret):
        return JSONResponse({"error": "bad signature"}, status_code=400)
    try:
        event = json.loads(payload.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse({"error": "bad payload"}, status_code=400)
    if not isinstance(event, dict):
        return JSONResponse({"error": "bad payload"}, status_code=400)
    kind = event.get("type")
    data = event.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    result = "ignored: event not used"
    if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded") and isinstance(obj, dict):
        if obj.get("payment_status") == "paid":
            result = _record_paid_session(obj)
        else:
            result = "ignored: not paid yet"
    return JSONResponse({"received": True, "result": result})


# ----- pricing (public) -------------------------------------------------------------------------

def _markup_percent(settings) -> str:
    percent = (settings.credit_markup - 1.0) * 100
    return f"{percent:.0f}" if abs(percent - round(percent)) < 0.05 else f"{percent:.1f}"


def _estimate_text(cents: int) -> str:
    return "under $0.01" if cents <= 1 else "about " + credits.format_cents(cents)


@router.get("/pricing")
def pricing(request: Request):
    settings = _settings(request)
    model = site_ai.site_model(settings)
    model_known = model in PRICES
    in_price, out_price = PRICES.get(model, PRICES["claude-opus-5-5"])
    rows = []
    for label, tin, tout, searches in ESTIMATES:
        own = cost_cents(model, tin, tout, searches, 1.0)
        site = cost_cents(model, tin, tout, searches, settings.credit_markup)
        searched = f", {searches} web searches" if searches else ""
        size = f"Typical size: {tin:,} tokens in, {tout:,} out{searched}."
        rows.append({"label": label, "size": size, "own": _estimate_text(own), "credits": _estimate_text(site)})
    free = site_ai.key_source(settings) is not None and site_ai.pay_mode() == "free"
    member_limit = site_ai.member_month_limit()
    return render(request, "account/pricing.html", title="How paying works", rows=rows, model=model,
                  model_name=site_ai.model_name(model), free_for_members=free,
                  free_limit=credits.format_cents(member_limit) if member_limit else "",
                  model_known=model_known, in_price=f"{in_price:.2f}", out_price=f"{out_price:.2f}",
                  search_price=f"{WEB_SEARCH_DOLLARS * 1000:.0f}", markup_percent=_markup_percent(settings),
                  site_has_key=site_ai.key_source(settings) is not None, stripe_ready=payments_ready(settings),
                  signup_credits=credits.format_cents(settings.signup_credits) if settings.signup_credits > 0 else "")


# ----- site owner tools -------------------------------------------------------------------------

def site_counts() -> dict:
    with db.session() as conn:
        one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "members": one("SELECT COUNT(*) FROM users WHERE email NOT LIKE 'deleted-%@invalid'"),
            "deleted": one("SELECT COUNT(*) FROM users WHERE email LIKE 'deleted-%@invalid'"),
            "with_key": one("SELECT COUNT(*) FROM users WHERE own_api_key_enc IS NOT NULL"),
            "jobs": one("SELECT COUNT(*) FROM jobs"),
            "jobs_failed": one("SELECT COUNT(*) FROM jobs WHERE status = 'failed'"),
            "ai_calls": one("SELECT COUNT(*) FROM ai_usage"),
            "ai_credits_cents": one("SELECT COALESCE(SUM(cost_cents), 0) FROM ai_usage WHERE paid_by = 'credits'"),
            "ai_own_key_cents": one("SELECT COALESCE(SUM(cost_cents), 0) FROM ai_usage WHERE paid_by = 'own_key'"),
            "paid_count": one("SELECT COUNT(*) FROM payments WHERE status = 'paid'"),
            "paid_cents": one("SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE status = 'paid'"),
        }


def grant_credits(email: str, cents: int, reason: str, ref: str = "") -> int | None:
    """Give credits to the member with this email. Returns their id, or None if there is no such member.

    With a ref, the same grant is added only once, even if the form is sent twice at the same moment."""
    with db.session() as conn:
        row = conn.execute("SELECT id FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
        if row is None:
            return None
        conn.execute(
            "INSERT INTO credit_ledger (user_id, delta_cents, reason, ref, created_at)"
            " SELECT ?, ?, ?, ?, ? WHERE ? = '' OR NOT EXISTS (SELECT 1 FROM credit_ledger WHERE ref = ?)",
            (row["id"], int(cents), f"Gift from the site: {reason}", ref, db.now(), ref, ref),
        )
    return row["id"]


def _grant_already_given(ref: str) -> bool:
    with db.session() as conn:
        return conn.execute("SELECT 1 FROM credit_ledger WHERE ref = ?", (ref,)).fetchone() is not None


def _render_admin(request: Request, values: dict, errors: dict, status_code: int = 200):
    counts = site_counts()
    for key in ("ai_credits_cents", "ai_own_key_cents", "paid_cents"):
        counts[key + "_text"] = credits.format_cents(counts[key])
    title = error_title("Site owner tools", errors) if errors else _done_title(request, "Site owner tools")
    token = values.get("grant_token") or secrets.token_urlsafe(16)
    return render(request, "account/admin.html", title=title, counts=counts, values=values, errors=errors,
                  grant_token=token,
                  site_has_key=site_ai.key_source(_settings(request)) is not None, status_code=status_code)


@router.get("/account/admin")
def admin_page(request: Request, user: dict = Depends(require_user)):
    _require_admin(user)
    return _render_admin(request, {}, {})


@router.post("/account/admin")
async def admin_grant(request: Request, user: dict = Depends(require_user)):
    _require_admin(user)
    form = await checked_form(request)
    values = {k: _clean(form, k, 300) for k in ("email", "amount", "reason")}
    token = _clean(form, "grant_token", 64)
    if re.fullmatch(r"[A-Za-z0-9_\-]{16,64}", token):
        values["grant_token"] = token
        ref = f"admin:{user['id']}:{token}"
        if _grant_already_given(ref):
            _announce(request, "Site owner tools: those credits were already given")
            flash(request, "This form was sent twice. The credits were given once, and nothing more was added.", "info")
            return redirect("/account/admin")
    else:
        ref = f"admin:{user['id']}:{secrets.token_urlsafe(16)}"
    errors: dict[str, str] = {}
    with db.session() as conn:
        found = conn.execute("SELECT id FROM users WHERE email = ?", (values["email"].lower(),)).fetchone()
    if not values["email"]:
        errors["email"] = "Enter the member's email address."
    elif found is None:
        errors["email"] = "No member has this email address."
    cents = _parse_dollars(values["amount"])
    if cents is None or cents <= 0:
        errors["amount"] = "Enter an amount in US dollars, like 5 or 2.50."
    elif cents > MAX_GRANT_CENTS:
        errors["amount"] = f"Grant at most {credits.format_cents(MAX_GRANT_CENTS)} at a time."
    if not values["reason"]:
        errors["reason"] = "Say why, for example: thank you for answering questions in Ask the community."
    elif len(values["reason"]) > 200:
        errors["reason"] = "Use 200 characters or fewer for the reason."
    elif secret_problem(values["reason"]):
        errors["reason"] = secret_problem(values["reason"])
    if errors:
        return _render_admin(request, values, errors, status_code=400)
    grant_credits(values["email"], cents, values["reason"], ref=ref)
    _announce(request, f"Site owner tools: gave {credits.format_cents(cents)} of credits to {values['email'].lower()}")
    return redirect("/account/admin")


# ----- delete -----------------------------------------------------------------------------------

def anonymise_user(user_id: int) -> None:
    """Remove everything that identifies the member, and their job list. Community posts stay under their code name."""
    with db.session() as conn:
        conn.execute("DELETE FROM jobs WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM kept_forms WHERE user_id = ?", (user_id,))
        conn.execute(
            "UPDATE users SET email = ?, display_name = 'Former member', password_hash = ?, own_api_key_enc = NULL,"
            " screen_reader = '', device = '', language = 'en', sight_onset = 'prefer_not', vision = 'prefer_not',"
            " verbosity = 'summary_first', show_name_on_checks = 0, show_name_on_help = 0, describe_colours = 0, own_model = '',"
            " is_admin = 0 WHERE id = ?",
            (f"deleted-{user_id}@invalid", hash_password(secrets.token_urlsafe(32)), user_id),
        )


def _render_delete(request: Request, errors: dict, status_code: int = 200, failed: bool = False):
    if failed:
        title = "Your account is not deleted yet"
    else:
        title = error_title("Delete your account", errors)
    return render(request, "account/delete.html", title=title, errors=errors, failed=failed,
                  status_code=status_code)


@router.get("/account/delete")
def delete_form(request: Request, user: dict = Depends(require_user)):
    return _render_delete(request, {})


@router.post("/account/delete")
async def delete_account(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    if form.get("confirm") != "yes":
        return _render_delete(request, {"confirm": "To delete your account, tick the box that says I understand. "
                                                   "Your account is not deleted."},
                              status_code=400)
    # Delete private work first. If any section fails, stop before the account is anonymised, so the
    # member is still signed in and can simply try again (every forget_user is safe to run twice).
    for hook in getattr(request.app.state, "forget_user_hooks", []):
        try:
            hook(user["id"])
        except Exception:  # noqa: BLE001 - say so plainly instead of a half-deleted account and an error page
            log.exception("forget_user hook %r failed for user %s", hook, user["id"])
            return _render_delete(request, {}, status_code=500, failed=True)
    anonymise_user(user["id"])
    # Once more, now that the account is anonymised: a job that finished in between saved nothing new
    # after this point, but may have saved just before it.
    for hook in getattr(request.app.state, "forget_user_hooks", []):
        try:
            hook(user["id"])
        except Exception:  # noqa: BLE001 - the account is already deleted; the first pass did the work
            log.exception("forget_user hook %r failed again for user %s", hook, user["id"])
    request.session.clear()
    request.session["home_heading"] = "Your account is deleted"
    flash(request, "You are signed out. Your private work and your details are removed from this site. "
                   "Thank you for being part of this community.", "success")
    return redirect("/")
