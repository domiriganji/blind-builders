"""Shared web helpers for every feature: rendering, forms with CSRF checks, flash messages, Markdown, current user."""
from __future__ import annotations

import json
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from jinja2 import pass_context
from markupsafe import Markup

from . import db
from .security import new_token

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable("table")


def _link_open(self, tokens, idx, options, env):
    env["_links_open"] = env.get("_links_open", 0) + 1
    href = tokens[idx].attrGet("href") or ""
    if href.startswith(("http://", "https://")):
        tokens[idx].attrSet("rel", "noopener noreferrer")
    return self.renderToken(tokens, idx, options, env)


def _link_close(self, tokens, idx, options, env):
    env["_links_open"] = max(env.get("_links_open", 0) - 1, 0)
    return self.renderToken(tokens, idx, options, env)


def _th_open(self, tokens, idx, options, env):
    tokens[idx].attrSet("scope", "col")
    return self.renderToken(tokens, idx, options, env)


def _image(self, tokens, idx, options, env):
    # The site shows no pictures from member or AI text (they could track people and break the CSP).
    # A picture becomes a link that says what it is.
    from markdown_it.common.utils import escapeHtml
    token = tokens[idx]
    src = token.attrGet("src") or ""
    alt = self.renderInlineAsText(token.children or [], options, env).strip()
    label = escapeHtml(f"Picture: {alt}" if alt else "Picture with no description")
    if src.startswith(("http://", "https://")) and not env.get("_links_open"):
        return f'<a href="{escapeHtml(src)}" rel="noopener noreferrer">{label}</a>'
    return f"[{label}]"  # a local picture, or one inside a link (a link may not contain a link)


_md.add_render_rule("link_open", _link_open)
_md.add_render_rule("link_close", _link_close)
_md.add_render_rule("th_open", _th_open)
_md.add_render_rule("image", _image)


def markdown(text: str | None) -> Markup:
    """Render Markdown to safe HTML. Raw HTML in the text is escaped, never passed through."""
    return Markup(_md.render(text or ""))


def shift_headings(html: str, by: int = 1) -> Markup:
    """Push headings in rendered Markdown down a level so they sit under the page's own headings."""
    out = str(html)
    for level in range(6, 0, -1):
        new = min(level + by, 6)
        out = out.replace(f"<h{level}>", f"<h{new}>").replace(f"</h{level}>", f"</h{new}>")
    return Markup(out)


_HEADING_RE = re.compile(r"<(/?)h([1-6])([ >])")


def nest_headings(html: str, base: int = 2) -> Markup:
    """Place rendered Markdown's headings under the page's own: the highest level used becomes `base`,
    and levels are made continuous (an h2 followed by an h4 becomes base, base+1), never past h6."""
    text = str(html)
    levels = sorted({int(m.group(2)) for m in _HEADING_RE.finditer(text)})
    mapping = {level: min(base + i, 6) for i, level in enumerate(levels)}
    return Markup(_HEADING_RE.sub(lambda m: f"<{m.group(1)}h{mapping[int(m.group(2))]}{m.group(3)}", text))


_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")


def date_words(iso: str | None, with_time: bool = False) -> str:
    """One date format everywhere (docs/VOICE.md, 4.13): '2026-10-13T12:29:05+00:00' -> '13 October 2026',
    or '13 October 2026, 12:29 UTC' with_time. Anything that is not a date comes back unchanged."""
    text = (iso or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?", text)
    if not m or not 1 <= int(m.group(2)) <= 12:
        return text
    out = f"{int(m.group(3))} {_MONTHS[int(m.group(2)) - 1]} {m.group(1)}"
    if with_time and m.group(4):
        out += f", {m.group(4)}:{m.group(5)} UTC"
    return out


def error_title(title: str, errors: object) -> str:
    """The title (and h1) of a form shown again after a failed send: the count first, without blame
    (docs/VOICE.md, 4.9). `errors` is the dict of field messages, or a list, or one message."""
    count = len(errors) if isinstance(errors, (dict, list, tuple, set)) else (1 if errors else 0)
    if not count:
        return title
    return f"Please check {count} answer{'' if count == 1 else 's'}: {title}"


templates.env.filters["markdown"] = markdown
templates.env.filters["shift_headings"] = shift_headings
templates.env.filters["nest_headings"] = nest_headings
templates.env.filters["date_words"] = date_words

# Macros imported from _macros.html cannot see page variables, but they can see globals.
# render() sets this before the template renders, so csrf_input() works in any template.
_csrf_for_render: ContextVar[str] = ContextVar("csrf_for_render", default="")
templates.env.globals["csrf_value"] = lambda: _csrf_for_render.get()
# A member's words kept from a form that could not be sent (see keep_form). The form macros put them back.
_restored_for_render: ContextVar[dict | None] = ContextVar("restored_for_render", default=None)


def _restored(name: str, value=""):
    """The kept value for this field if the member's words were kept for this page, else `value`."""
    kept = _restored_for_render.get()
    if kept and name in kept["fields"]:
        kept["used"].add(name)
        return kept["fields"][name]
    return value


def _restored_unused() -> list[tuple[str, str]]:
    """Kept words that no form field on the page took, so the page can show them to copy."""
    kept = _restored_for_render.get()
    if not kept:
        return []
    return [(name, text) for name, text in kept["fields"].items() if name not in kept["used"]]


def _restored_tick(name: str, checked=False) -> bool:
    """A checkbox's tick: when the member's words were kept for this page, the tick they sent (an unticked box
    is not sent at all, so it comes back unticked); otherwise `checked`."""
    kept = _restored_for_render.get()
    if not kept:
        return bool(checked)
    kept["used"].add(name)
    return kept["fields"].get(name) == "yes"


@pass_context
def _ai_pays(context) -> dict:
    """What pays for an AI step started from this page, in the shared words of account.pay_words."""
    from .features.account import pay_words  # here: account imports this module
    request = context.get("request")
    return pay_words(request.app.state.settings, context.get("user"))


templates.env.globals["restored"] = _restored
templates.env.globals["ai_pays"] = _ai_pays
templates.env.globals["restored_tick"] = _restored_tick
templates.env.globals["restored_unused"] = _restored_unused


# ----- session, user, CSRF ---------------------------------------------------------------------

def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = new_token()
        request.session["csrf"] = token
    return token


FORM_EXPIRED = ("This page was open for a long time, or you signed in or out in another tab, so the form stopped "
                "working to keep your account safe. Nothing was saved.")


class FormExpired(HTTPException):
    """The form's CSRF token did not match. Carries what the member sent, so their words are not lost."""

    def __init__(self, form):
        super().__init__(status_code=400, detail=FORM_EXPIRED)
        self.form = form


async def checked_form(request: Request) -> dict[str, Any]:
    """Read a POSTed form and verify its CSRF token. Use this for every form handler."""
    form = await request.form()
    sent = form.get("csrf_token")
    if not sent or sent != request.session.get("csrf"):
        raise FormExpired(form)
    data: dict[str, Any] = {}
    for key in form.keys():
        values = form.getlist(key)
        data[key] = values if len(values) > 1 else values[0]
    data.pop("csrf_token", None)
    return data


def form_text(form: dict, name: str, limit: int = 10_000) -> str:
    """One form value as text: a repeated field counts as its first value; very long text is cut to `limit`."""
    value = form.get(name, "")
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value)[:limit]


def current_user(request: Request) -> dict | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    cached = getattr(request.state, "user", None)
    if cached and cached["id"] == user_id:
        return cached
    with db.session() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row["email"].endswith("@invalid") or request.session.get("epoch", 0) != row["session_epoch"]:
        # Missing or deleted account, or signed out everywhere since this session began (for example after
        # a password reset): treat as signed out.
        request.session.pop("user_id", None)
        request.session.pop("epoch", None)
        return None
    user = dict(row)
    request.state.user = user
    return user


def sign_in(request: Request, user_id: int) -> None:
    """Start a fresh session for this member. It stays valid until they are signed out everywhere."""
    with db.session() as conn:
        row = conn.execute("SELECT session_epoch FROM users WHERE id = ?", (user_id,)).fetchone()
    kept = request.session.get("kept_form")  # words from a form sent while signed out, to be put back
    request.session.clear()
    if isinstance(kept, str):
        request.session["kept_form"] = kept
    request.session["user_id"] = user_id
    request.session["epoch"] = row["session_epoch"] if row else 0


def end_all_sessions(conn, user_id: int) -> None:
    """Sign the member out on every device: every session cookie issued before now stops working."""
    conn.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE id = ?", (user_id,))


class LoginRequired(Exception):
    def __init__(self, next_url: str, last_user_id: int | None = None):
        self.next_url = next_url
        self.last_user_id = last_user_id  # who this browser was signed in as, if the old session still says


def require_user(request: Request) -> dict:
    """FastAPI dependency: the signed-in user, or a redirect to sign in."""
    last_user_id = request.session.get("user_id")
    user = current_user(request)
    if user is None:
        if request.method != "GET":
            # A form cannot be replayed after signing in. Send people back to the page the form was on if it
            # is on this site, else to the form's own address (sections answer a GET there with the form).
            referer = urlparse(request.headers.get("referer") or "")
            same_site = referer.netloc == request.url.netloc and referer.path.startswith("/")
            raise LoginRequired((referer.path + ("?" + referer.query if referer.query else "")) if same_site
                                else str(request.url.path), last_user_id if isinstance(last_user_id, int) else None)
        query = request.url.query
        raise LoginRequired(str(request.url.path) + ("?" + query if query else ""))
    return user


# ----- keeping a member's words when a form cannot be sent ---------------------------------------

KEPT_FORMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS kept_forms (
    token TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    fields TEXT NOT NULL,
    reason TEXT NOT NULL,
    user_id INTEGER,
    created_at TEXT NOT NULL
);
"""
KEPT_FORM_MAX_CHARS = 200_000
KEPT_FORM_SECONDS = 2 * 60 * 60
# Never kept, whatever the field holds: passwords, keys, codes and the form's own token.
_NEVER_KEEP = re.compile(r"pass|csrf|token|api_key|secret|code|card", re.IGNORECASE)
# The name of the submit buttons that say which of several things to do. The button's value is not the
# member's words, and choosing it again is part of sending the form again.
_SUBMIT_FIELD = "action"


def kept_fields(form) -> dict[str, str]:
    """The member's own words from a submitted form: text fields and ticked boxes, never the submit button,
    passwords, keys, codes or anything that looks like a secret, at most KEPT_FORM_MAX_CHARS in all."""
    from .sensitive import find_secret
    out: dict[str, str] = {}
    total = 0
    for name in form.keys():
        if name in out or name == _SUBMIT_FIELD or _NEVER_KEEP.search(name):
            continue
        value = form.get(name)
        if not isinstance(value, str) or not value.strip() or find_secret(value):
            continue
        if total + len(value) > KEPT_FORM_MAX_CHARS:
            continue
        out[name] = value
        total += len(value)
    return out


def keep_form(request: Request, form, page_path: str, reason: str, user_id: int | None = None) -> dict | None:
    """Keep the member's words from a form that could not be sent (reason "signed_out" or "expired"), so the
    page at `page_path` puts them back in its form, once. The session holds only a random token; the words
    stay on the server for at most KEPT_FORM_SECONDS. Returns what was kept, or None if there was nothing."""
    fields = kept_fields(form)
    if not fields:
        return None
    from datetime import datetime, timedelta, timezone
    oldest = (datetime.now(timezone.utc) - timedelta(seconds=KEPT_FORM_SECONDS)).isoformat(timespec="seconds")
    token = new_token()
    with db.session() as conn:
        conn.execute("DELETE FROM kept_forms WHERE created_at < ?", (oldest,))
        old = request.session.get("kept_form")
        if isinstance(old, str):
            conn.execute("DELETE FROM kept_forms WHERE token = ?", (old,))
        conn.execute("INSERT INTO kept_forms (token, path, fields, reason, user_id, created_at)"
                     " VALUES (?, ?, ?, ?, ?, ?)",
                     (token, page_path.split("?", 1)[0], json.dumps(fields), reason, user_id, db.now()))
    request.session["kept_form"] = token
    return {"fields": fields, "reason": reason, "used": set()}


def has_kept_form(request: Request) -> bool:
    return isinstance(request.session.get("kept_form"), str)


def forget_kept_form(request: Request) -> None:
    token = request.session.pop("kept_form", None)
    if isinstance(token, str):
        with db.session() as conn:
            conn.execute("DELETE FROM kept_forms WHERE token = ?", (token,))


def _take_kept_form(request: Request, user: dict | None) -> dict | None:
    """The kept words for this page, once: they are removed as they are shown."""
    token = request.session.get("kept_form")
    if not isinstance(token, str) or request.method != "GET":
        return None
    with db.session() as conn:
        row = conn.execute("SELECT * FROM kept_forms WHERE token = ?", (token,)).fetchone()
        if row is not None and row["path"] != request.url.path:
            return None
        conn.execute("DELETE FROM kept_forms WHERE token = ?", (token,))
    request.session.pop("kept_form", None)
    if row is None or (row["user_id"] is not None and (user is None or user["id"] != row["user_id"])):
        return None  # gone, or someone else signed in on this browser: their words are not shown to them
    try:
        fields = json.loads(row["fields"])
    except ValueError:
        return None
    return {"fields": fields, "reason": row["reason"], "used": set()}


def flash(request: Request, text: str, kind: str = "info") -> None:
    """Queue a message for the next page. kind is "info", "success" or "error"."""
    request.session.setdefault("flash", []).append({"kind": kind, "text": text})


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def render(request: Request, template: str, *, title: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    """Render a page. `title` is the page's own name; the site name is appended in the <title>."""
    messages = request.session.pop("flash", [])
    settings = request.app.state.settings
    _csrf_for_render.set(csrf_token(request))
    user = current_user(request)
    # `kept` (from keep_form) shows a member's words on this page; otherwise words kept for this page, if any.
    kept = context.pop("kept", None) or _take_kept_form(request, user)
    _restored_for_render.set(kept)
    ctx = {
        "request": request,
        "title": title,
        "user": user,
        "kept_reason": kept["reason"] if kept else "",
        "csrf_token": csrf_token(request),
        "messages": messages,
        "site_name": settings.site_name,
        "site_tagline": settings.site_tagline,
        "nav": request.app.state.nav,
        **context,
    }
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


def get_ai(request: Request, user: dict):
    """The AI client for this user. Raises ai.AINotAvailable with a plain explanation if there is none."""
    return request.app.state.ai_factory(user)
