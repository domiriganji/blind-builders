"""Create the web application."""
from __future__ import annotations

import importlib
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import ai, db, jobs
from .auth import router as auth_router
from .config import Settings, load_settings
from .core_routes import router as core_router
from .web import LoginRequired, redirect, render

log = logging.getLogger("studio")

# Order here is the order of the main navigation and the home page.
FEATURES = ["build", "errands", "write", "find", "research", "checks", "ai_tools", "library", "learn", "account"]

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

    app = FastAPI(title=settings.site_name, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.ai_factory = ai.default_factory(settings)
    app.state.nav = [m.NAV for m in modules if getattr(m, "NAV", None)]

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", DEFAULT_CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

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

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        return redirect(f"/login?next={exc.next_url}")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            heading, message = "Page not found", (exc.detail if exc.detail and exc.detail != "Not Found" else
                                                  "We could not find that page. Check the address or go to the home page.")
        else:
            heading, message = "Something is not right", str(exc.detail)
        return render(request, "error.html", title=heading, heading=heading, message=message, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return render(request, "error.html", title="Something is missing", heading="Something is missing",
                      message="Part of the request was missing or not in the right form. Go back and try again.",
                      status_code=400)

    return app


def get_app() -> FastAPI:
    """Entry point for uvicorn: `uvicorn studio.app:get_app --factory`."""
    return create_app()
