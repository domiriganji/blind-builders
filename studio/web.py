"""Shared web helpers for every feature: rendering, forms with CSRF checks, flash messages, Markdown, current user."""
from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Any

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


templates.env.filters["markdown"] = markdown
templates.env.filters["shift_headings"] = shift_headings

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


def current_user(request: Request) -> dict | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    cached = getattr(request.state, "user", None)
    if cached and cached["id"] == user_id:
        return cached
    with db.session() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row["email"].endswith("@invalid"):
        # Missing or deleted account: treat as signed out on every device.
        request.session.pop("user_id", None)
        return None
    user = dict(row)
    request.state.user = user
    return user


class LoginRequired(Exception):
    def __init__(self, next_url: str):
        self.next_url = next_url


def require_user(request: Request) -> dict:
    """FastAPI dependency: the signed-in user, or a redirect to sign in."""
    user = current_user(request)
    if user is None:
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
