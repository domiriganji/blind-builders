"""Settings, read once from environment variables."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    site_name: str = "Blind Creators Studio"
    secret_key: str = ""
    secret_key_is_random: bool = False
    db_path: Path = Path("data/studio.db")
    base_url: str = "http://localhost:8000"
    # AI. The server key is optional: users can connect their own Claude API key instead.
    anthropic_api_key: str | None = None
    model: str = "claude-opus-5-5"
    # Credits are US cents. Users pay model cost times this markup when they use the site's AI.
    credit_markup: float = 1.25
    signup_credits: int = 0
    # Run background jobs inside the request (used by tests).
    jobs_inline: bool = False
    job_workers: int = 4
    # Payments (optional). When unset, the buy-credits page says payments are not set up.
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    extra: dict = field(default_factory=dict)


def load_settings() -> Settings:
    secret = os.environ.get("STUDIO_SECRET_KEY", "")
    random_secret = False
    if not secret:
        secret = secrets.token_urlsafe(48)
        random_secret = True
    return Settings(
        site_name=os.environ.get("STUDIO_SITE_NAME", "Blind Creators Studio"),
        secret_key=secret,
        secret_key_is_random=random_secret,
        db_path=Path(os.environ.get("STUDIO_DB_PATH", "data/studio.db")),
        base_url=os.environ.get("STUDIO_BASE_URL", "http://localhost:8000").rstrip("/"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        model=os.environ.get("STUDIO_MODEL", "claude-opus-5-5"),
        credit_markup=float(os.environ.get("STUDIO_CREDIT_MARKUP", "1.25")),
        signup_credits=int(os.environ.get("STUDIO_SIGNUP_CREDITS", "0")),
        jobs_inline=_bool("STUDIO_JOBS_INLINE"),
        job_workers=int(os.environ.get("STUDIO_JOB_WORKERS", "4")),
        stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY") or None,
        stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET") or None,
    )
