"""Background jobs for slow AI work.

The person submits a form, gets a job page that says what is happening, and is taken to the result
when it is done. Nothing streams: the result appears whole. Without JavaScript the job page has a
"Check again" link; with JavaScript it checks by itself, plays a short rhythm and moves focus to a
link to the result.
"""
from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from . import db
from .ai import AIError

log = logging.getLogger("studio.jobs")
_executor: ThreadPoolExecutor | None = None
_inline = False


def configure(workers: int, inline: bool) -> None:
    global _executor, _inline
    _inline = inline
    if not inline and _executor is None:
        _executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="studio-job")


@dataclass
class JobContext:
    job_id: str
    user_id: int

    def progress(self, text: str) -> None:
        """Record a short, plain sentence about the current step, shown on the job page."""
        with db.session() as conn:
            conn.execute("UPDATE jobs SET progress = ? WHERE id = ?", (text[:300], self.job_id))


def submit(user_id: int, kind: str, title: str, fn: Callable[..., str], *args: Any, **kwargs: Any) -> str:
    """Run fn(ctx, *args, **kwargs) in the background. fn returns the URL of the result page."""
    job_id = uuid.uuid4().hex
    with db.session() as conn:
        conn.execute(
            "INSERT INTO jobs (id, user_id, kind, title, status, created_at) VALUES (?, ?, ?, ?, 'queued', ?)",
            (job_id, user_id, kind, title[:200], db.now()),
        )
    if _inline or _executor is None:
        _run(job_id, user_id, fn, args, kwargs)
    else:
        _executor.submit(_run, job_id, user_id, fn, args, kwargs)
    return job_id


def _run(job_id: str, user_id: int, fn: Callable[..., str], args: tuple, kwargs: dict) -> None:
    with db.session() as conn:
        conn.execute("UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?", (db.now(), job_id))
    ctx = JobContext(job_id=job_id, user_id=user_id)
    try:
        result_url = fn(ctx, *args, **kwargs)
        with db.session() as conn:
            conn.execute("UPDATE jobs SET status = 'done', result_url = ?, finished_at = ? WHERE id = ?",
                         (result_url or "", db.now(), job_id))
    except AIError as exc:
        _fail(job_id, exc.user_message)
    except Exception:  # noqa: BLE001 - a job must always end in a visible state
        log.exception("job %s failed", job_id)
        _fail(job_id, "Something went wrong on our side. Your work so far is saved. Please try again.")


def _fail(job_id: str, message: str) -> None:
    with db.session() as conn:
        conn.execute("UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                     (message, db.now(), job_id))


def get(job_id: str) -> dict | None:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def recent_for_user(user_id: int, limit: int = 10) -> list[dict]:
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                            (user_id, limit)).fetchall()
    return [dict(r) for r in rows]


def job_url(job_id: str) -> str:
    return f"/jobs/{job_id}"
