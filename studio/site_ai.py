"""The site owner's AI settings: the site's own Claude API key, the model members use with it, who pays, and
spending limits.

- The key is saved from the owner's AI settings page, encrypted with the site's secret key, and never shown
  again (only its last 4 characters). Without one, the ANTHROPIC_API_KEY setting is used, if the server has it.
- Members who connect their own key choose their own model; everyone else uses the owner's choice.
- Who pays for AI on the site's key: members, with credits (the default), or the owner, free for members.
- Limits cover all AI work on the site's key, whoever pays: the most the key may spend in a month, and the most
  each member may use in a month. Amounts are what Anthropic charges the owner, in US cents; 0 means no limit.
  Before each call the most it can cost is set aside (reserve), in one locked step, so calls running at the same
  time cannot go past a limit together; the hold is removed once the real cost is recorded in ai_usage.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import credits, db
from .security import decrypt_secret, encrypt_secret

# The models the owner (and members with their own key) can choose. Prices are from ai.PRICES.
MODELS = [
    ("claude-opus-5-5", "Claude Opus 5.5",
     "Very capable at building, writing and research. The recommended choice. $4 per million tokens in, $20 out."),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5",
     "Strong, at half the price of Opus 5.5. $2 per million tokens in, $10 out."),
    ("claude-haiku-5-5", "Claude Haiku 5.5",
     "The lowest cost. Good for short answers, weaker at building whole websites and at research. "
     "$0.10 per million tokens in, $0.50 out."),
    ("claude-fable-5-1", "Claude Fable 5.1",
     "Anthropic's most capable model, and the most expensive. $10 per million tokens in, $50 out."),
]
MODEL_NAMES = {model_id: name for model_id, name, _ in MODELS}
PAY_MODES = [
    ("credits", "Members pay with credits they buy, or that you give them"),
    ("free", "Free for members, within the limits below: you pay Anthropic"),
]

# The tables (site_settings, ai_spend_holds) are in db.CORE_SCHEMA, because studio.ai uses them.


# ----- stored settings --------------------------------------------------------------------------

def get(name: str, default: str = "") -> str:
    with db.session() as conn:
        row = conn.execute("SELECT value FROM site_settings WHERE name = ?", (name,)).fetchone()
    return row["value"] if row else default


def get_int(name: str, default: int = 0) -> int:
    try:
        return int(get(name, str(default)))
    except ValueError:
        return default


def save(values: dict[str, str | int], by_user_id: int | None) -> None:
    with db.session() as conn:
        for name, value in values.items():
            conn.execute(
                "INSERT INTO site_settings (name, value, updated_at, updated_by) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at,"
                " updated_by = excluded.updated_by",
                (name, str(value), db.now(), by_user_id))


def delete(*names: str) -> None:
    with db.session() as conn:
        for name in names:
            conn.execute("DELETE FROM site_settings WHERE name = ?", (name,))


# ----- the site's key -----------------------------------------------------------------------------

def save_key(settings, key: str, by_user_id: int) -> None:
    save({"site_key_enc": encrypt_secret(settings.secret_key, key), "site_key_last4": key[-4:],
          "site_key_saved_at": db.now()}, by_user_id)


def remove_key() -> None:
    delete("site_key_enc", "site_key_last4", "site_key_saved_at")


def key_source(settings) -> str | None:
    """'page' if the owner saved a key on the AI settings page (and it can be read), 'environment' if the
    server's ANTHROPIC_API_KEY setting is used instead, or None if the site has no key."""
    enc = get("site_key_enc")
    if enc and decrypt_secret(settings.secret_key, enc):
        return "page"
    if settings.anthropic_api_key:
        return "environment"
    return None


def key_unreadable(settings) -> bool:
    """True if a key was saved on the page but the site's secret key has changed since, so it cannot be read."""
    enc = get("site_key_enc")
    return bool(enc) and not decrypt_secret(settings.secret_key, enc)


def site_key(settings) -> str | None:
    enc = get("site_key_enc")
    if enc:
        key = decrypt_secret(settings.secret_key, enc)
        if key:
            return key
    return settings.anthropic_api_key or None


def key_last4(settings) -> str:
    source = key_source(settings)
    if source == "page":
        return get("site_key_last4")
    if source == "environment":
        return (settings.anthropic_api_key or "")[-4:]
    return ""


# ----- model and who pays -----------------------------------------------------------------------

def site_model(settings) -> str:
    chosen = get("model")
    return chosen if chosen in MODEL_NAMES else settings.model


def member_model(settings, user: dict) -> str:
    """The model for a member using their own key: their choice, or the site's."""
    try:
        chosen = user["own_model"] or ""
    except (KeyError, IndexError):
        chosen = ""
    return chosen if chosen in MODEL_NAMES else site_model(settings)


def model_name(model_id: str) -> str:
    return MODEL_NAMES.get(model_id, model_id)


def pay_mode() -> str:
    return "free" if get("pay_mode") == "free" else "credits"


def site_month_limit() -> int:
    return max(get_int("site_month_cents"), 0)


def member_month_limit() -> int:
    return max(get_int("member_month_cents"), 0)


# ----- spending and limits ----------------------------------------------------------------------

def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month_start(now: datetime | None = None) -> datetime:
    start = month_start(now)
    return start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def _spent(conn, user_id: int | None) -> int:
    """What the site's key spent this month (for one member, or for everyone), with work still running."""
    since = _iso(month_start())
    if user_id is None:
        used = conn.execute("SELECT COALESCE(SUM(raw_cents), 0) FROM ai_usage WHERE paid_by IN ('credits', 'site')"
                            " AND created_at >= ?", (since,)).fetchone()[0]
        held = conn.execute("SELECT COALESCE(SUM(cents), 0) FROM ai_spend_holds").fetchone()[0]
    else:
        used = conn.execute("SELECT COALESCE(SUM(raw_cents), 0) FROM ai_usage WHERE paid_by IN ('credits', 'site')"
                            " AND user_id = ? AND created_at >= ?", (user_id, since)).fetchone()[0]
        held = conn.execute("SELECT COALESCE(SUM(cents), 0) FROM ai_spend_holds WHERE user_id = ?",
                            (user_id,)).fetchone()[0]
    return int(used) + int(held)


def spent_this_month(user_id: int | None = None) -> int:
    with db.session() as conn:
        return _spent(conn, user_id)


def member_left_this_month(user_id: int) -> int | None:
    """What a member has left of their monthly limit on the site's key, or None if there is no limit."""
    limit = member_month_limit()
    if not limit:
        return None
    return max(limit - spent_this_month(user_id), 0)


def renews_on() -> str:
    """The day monthly limits start again, in words."""
    from .web import date_words  # here, not at the top: web imports features that import this module
    return date_words(_iso(next_month_start()))


def reserve(user_id: int, cents: int) -> tuple[int | None, str]:
    """Set aside the most a call on the site's key can cost, if both limits allow it. Returns (hold id, "") or
    (None, a plain message saying which limit was reached and what the member can do now)."""
    site_limit, member_limit = site_month_limit(), member_month_limit()
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if site_limit:
            site_spent = _spent(conn, None)
            if site_spent + cents > site_limit:
                return None, (
                    "The AI budget for this month on the site's key is used up, so this step did not start and "
                    f"nothing was charged. It renews on {renews_on()}. To keep working now, connect your own Claude "
                    "API key on Your account.")
        if member_limit:
            member_spent = _spent(conn, user_id)
            if member_spent + cents > member_limit:
                left = max(member_limit - member_spent, 0)
                return None, (
                    f"This step can cost up to {credits.format_cents(cents)}, and you have "
                    f"{credits.format_cents(left)} left of your {credits.format_cents(member_limit)} for this month "
                    f"on the site's key. So it did not start, and nothing was charged. Your allowance renews on "
                    f"{renews_on()}. To keep working now, connect your own Claude API key on Your account.")
        cur = conn.execute("INSERT INTO ai_spend_holds (user_id, cents, created_at) VALUES (?, ?, ?)",
                           (user_id, int(cents), db.now()))
        return int(cur.lastrowid), ""


def release(hold_id: int) -> None:
    with db.session() as conn:
        conn.execute("DELETE FROM ai_spend_holds WHERE id = ?", (hold_id,))


def release_all_holds() -> None:
    """At start-up no AI work is running (it runs in this process), so any hold left is from a restart."""
    with db.session() as conn:
        conn.execute("DELETE FROM ai_spend_holds")
