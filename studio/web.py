"""Shared web helpers for every feature: rendering, forms with CSRF checks, flash messages, Markdown, current user."""
from __future__ import annotations

import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
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


# ----- session, user, CSRF ---------------------------------------------------------------------

def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = new_token()
        request.session["csrf"] = token
    return token


async def checked_form(request: Request) -> dict[str, Any]:
    """Read a POSTed form and verify its CSRF token. Use this for every form handler."""
    form = await request.form()
    sent = form.get("csrf_token")
    if not sent or sent != request.session.get("csrf"):
        raise HTTPException(status_code=400, detail="This form expired. Go back, reload the page and try again.")
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
    request.session.clear()
    request.session["user_id"] = user_id
    request.session["epoch"] = row["session_epoch"] if row else 0


def end_all_sessions(conn, user_id: int) -> None:
    """Sign the member out on every device: every session cookie issued before now stops working."""
    conn.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE id = ?", (user_id,))


class LoginRequired(Exception):
    def __init__(self, next_url: str):
        self.next_url = next_url


def require_user(request: Request) -> dict:
    """FastAPI dependency: the signed-in user, or a redirect to sign in."""
    user = current_user(request)
    if user is None:
        if request.method != "GET":
            # A form cannot be replayed after signing in. Send people back to the page the form was on if it
            # is on this site, else to the form's own address (sections answer a GET there with the form).
            referer = urlparse(request.headers.get("referer") or "")
            same_site = referer.netloc == request.url.netloc and referer.path.startswith("/")
            raise LoginRequired((referer.path + ("?" + referer.query if referer.query else "")) if same_site
                                else str(request.url.path))
        query = request.url.query
        raise LoginRequired(str(request.url.path) + ("?" + query if query else ""))
    return user


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
    ctx = {
        "request": request,
        "title": title,
        "user": current_user(request),
        "csrf_token": csrf_token(request),
        "messages": messages,
        "site_name": settings.site_name,
        "nav": request.app.state.nav,
        **context,
    }
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


def get_ai(request: Request, user: dict):
    """The AI client for this user. Raises ai.AINotAvailable with a plain explanation if there is none."""
    return request.app.state.ai_factory(user)
