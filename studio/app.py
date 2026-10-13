"""Create the web application."""
from __future__ import annotations

import importlib
import logging
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import ai, credits, db, jobs, site_ai
from .auth import router as auth_router
from .config import Settings, load_settings
from .core_routes import router as core_router
from .web import FormExpired, LoginRequired, flash, keep_form, redirect, render

log = logging.getLogger("studio")

# Order here is the order of the main navigation and the home page.
FEATURES = ["build", "errands", "write", "find", "research", "checks", "ai_tools", "library", "learn", "account", "recovery",
            "owner_ai"]

TOO_LARGE_PAGE = b"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>That was too large to send</title></head>
<body><main><h1>That was too large to send</h1><p>What you sent is bigger than this site accepts, so nothing was saved.
Use your browser's Back button to return to what you typed, make it smaller (for example a shorter text or a
smaller file) and send it again.</p>
<p><a href="/">Go to the home page</a></p></main></body></html>"""


class BodySizeLimit:
    """Refuse request bodies over max_bytes with a plain page. The body is read (up to the limit) before the
    app sees it, so a chunked upload without Content-Length cannot fill the disk; the app then gets the same
    bytes back. Bodies on this site are small forms and files of a few hundred kilobytes."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def _reject(self, send):
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"text/html; charset=utf-8")]})
        await send({"type": "http.response.body", "body": TOO_LARGE_PAGE})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length and length.isdigit() and int(length) > self.max_bytes:
            return await self._reject(send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                return await self._reject(send)
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


DEFAULT_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
               "frame-ancestors 'none'; base-uri 'self'; object-src 'none'")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    if settings.secret_key_is_random:
        log.warning("STUDIO_SECRET_KEY is not set; sign-ins and saved API keys will not survive a restart.")
    db.configure(settings.db_path)
    modules = [importlib.import_module(f"studio.features.{name}") for name in FEATURES]
    db.init([getattr(m, "SCHEMA", "") for m in modules])
    for m in modules:
        seed = getattr(m, "seed", None)
        if callable(seed):
            seed()
    jobs.configure(settings.job_workers, settings.jobs_inline)
    credits.release_all_holds()
    site_ai.release_all_holds()

    app = FastAPI(title=settings.site_name, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.ai_factory = ai.default_factory(settings)
    app.state.nav = [m.NAV for m in modules if getattr(m, "NAV", None)]
    # Sections that hold private work define forget_user(user_id); account deletion calls each one.
    app.state.forget_user_hooks = [m.forget_user for m in modules if callable(getattr(m, "forget_user", None))]

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", DEFAULT_CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    if settings.preview_base_url:
        preview_host = urlsplit(settings.preview_base_url).netloc

        @app.middleware("http")
        async def previews_on_their_own_domain(request: Request, call_next):
            # The preview domain serves build previews and nothing else; the site's own domain sends preview
            # addresses there, so a member's page never appears under the site's address.
            is_preview = request.url.path.startswith("/build/p/")
            on_preview_host = request.headers.get("host", "") == preview_host
            if on_preview_host and not is_preview:
                return PlainTextResponse("Not found", status_code=404)
            if is_preview and not on_preview_host:
                query = ("?" + request.url.query) if request.url.query else ""
                return RedirectResponse(settings.preview_base_url + request.url.path + query, status_code=307)
            return await call_next(request)

    app.add_middleware(BodySizeLimit, max_bytes=settings.max_body_bytes)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="studio_session",
        same_site="lax",
        https_only=settings.base_url.startswith("https://"),
        max_age=60 * 60 * 24 * 30,
    )
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(core_router)
    app.include_router(auth_router)
    for m in modules:
        app.include_router(m.router)

    def _from_another_site(request: Request) -> bool:
        """True if the browser says the form was sent from another website. Browsers send Origin with every
        form POST; words from another site are never kept, so no site can put text in a member's forms."""
        origin = request.headers.get("origin")
        if origin is None:
            referer = request.headers.get("referer")
            return bool(referer) and urlparse(referer).netloc != request.url.netloc
        return urlparse(origin).netloc != request.url.netloc

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        if request.method == "POST" and not _from_another_site(request):
            # Signed out before the form was sent: keep the member's words, to put back after signing in.
            try:
                form = await request.form()
            except Exception:  # noqa: BLE001 - a body that is not a form has no words to keep
                form = None
            if form is not None:
                keep_form(request, form, exc.next_url, "signed_out", exc.last_user_id)
        return redirect("/login?" + urlencode({"next": exc.next_url}))

    def _same_site_referer(request: Request) -> str:
        referer = urlparse(request.headers.get("referer") or "")
        if referer.netloc != request.url.netloc or not referer.path.startswith("/") or referer.path.startswith("//"):
            return ""
        return referer.path + ("?" + referer.query if referer.query else "")

    def _section_link(path: str) -> tuple[str, str]:
        """The section a missing page was in, so the page can offer a way back there."""
        for item in app.state.nav:
            if path.startswith(item["url"] + "/"):
                return item["url"], f"Go to {item['label']}"
        if path.startswith("/help/"):
            return "/help", "Go to Ask the community"
        if path.startswith("/account/"):
            return "/account", "Go to Your account"
        if path.startswith("/jobs/"):
            return "/jobs", "Go to Your recent work"
        return "", ""

    async def form_expired(request: Request, exc: FormExpired):
        if _from_another_site(request):
            exc.form = {}  # never keep or show words another website sent
        page = _same_site_referer(request)
        if "studio_session" not in request.cookies:
            heading = "This form needs refreshing: nothing was saved"
            return render(request, "error.html", title=heading, heading=heading, status_code=400, kept_note=False,
                          message="Your browser did not send this site's cookie. The page may have been open for a "
                                  "very long time, or cookies may be turned off for this site, which needs them to "
                                  "send forms. Use your browser's Back button to return to the form, or copy your "
                                  "words from below, then send it again.",
                          kept=keep_form(request, exc.form, request.url.path, "expired"))
        if page:
            kept = keep_form(request, exc.form, page, "expired")
            if not kept:
                flash(request, "This form needed refreshing to keep your account safe, so nothing was done. "
                               "Please choose the button again.", "info")
            return redirect(page)
        heading = "This form needs refreshing: nothing was saved"
        return render(request, "error.html", title=heading, heading=heading, status_code=400, kept_note=False,
                      message="It stopped working to keep your account safe, for example because the page was open "
                              "for a long time. Use your browser's Back button to return to what you typed (Alt+Left "
                              "Arrow, or Command+[ on a Mac), then send it again.",
                      kept=keep_form(request, exc.form, request.url.path, "expired"))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        back_url = back_label = ""
        sign_out = False
        if isinstance(exc, FormExpired):
            return await form_expired(request, exc)
        if exc.status_code == 404:
            heading = "Page not found"
            message = (exc.detail if exc.detail and exc.detail != "Not Found" else
                       "We could not find that page. Check the address, or use the links below.")
            back_url, back_label = _section_link(request.url.path)
        elif exc.status_code == 405:
            heading = "That page only works from its form"
            if request.url.path == "/logout":
                message = "To sign out, choose the button below."
                sign_out = True
            else:
                message = ("This address is the one a form sends to, so it has nothing to show by itself. "
                           "Nothing was changed.")
                parent = request.url.path.rstrip("/").rsplit("/", 1)[0]
                if parent:
                    back_url, back_label = parent, "Go to the page this form is on"
        else:
            heading, message = "Something is not right", str(exc.detail)
        return render(request, "error.html", title=heading, heading=heading, message=message, back_url=back_url,
                      back_label=back_label, sign_out=sign_out, status_code=exc.status_code)

    @app.exception_handler(jobs.TooManyJobs)
    async def too_many_jobs(request: Request, exc: jobs.TooManyJobs):
        heading = "Please wait for your other AI work to finish"
        return render(request, "error.html", title=heading, heading=heading, message=exc.user_message,
                      back_url="/jobs", back_label="Go to Your recent work", status_code=429)

    @app.exception_handler(OverflowError)
    async def number_too_large(request: Request, exc: OverflowError):
        # An id in the address too large for the database cannot belong to anything.
        return render(request, "error.html", title="Page not found", heading="Page not found",
                      message="We could not find that page. Check the address, or use the links below.", status_code=404)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return render(request, "error.html", title="Some information did not arrive",
                      heading="Some information did not arrive",
                      message="This can happen when a link is cut short. Nothing was changed. Please go back and "
                              "try again.",
                      status_code=400)

    return app


def get_app() -> FastAPI:
    """Entry point for uvicorn: `uvicorn studio.app:get_app --factory`."""
    return create_app()
