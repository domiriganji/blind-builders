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


RESTART_MESSAGE = "The site restarted before this finished. Nothing was lost except this step. Please try again."


def configure(workers: int, inline: bool) -> None:
    global _executor, _inline
    _inline = inline
    if not inline and _executor is None:
        _executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="studio-job")
    # Jobs run in this process's threads, so any job still queued or running from before a restart is dead.
    # (Run the site as one process; the threads are the workers.)
    with db.session() as conn:
        conn.execute("UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE status IN ('queued', 'running')",
                     (RESTART_MESSAGE, db.now()))


@dataclass
class JobContext:
    job_id: str
    user_id: int

    def progress(self, text: str) -> None:
        """Record a short, plain sentence about the current step, shown on the job page."""
        with db.session() as conn:
            conn.execute("UPDATE jobs SET progress = ? WHERE id = ?", (text[:300], self.job_id))


def submit(user_id: int, kind: str, title: str, fn: Callable[..., str], *args: Any,
           back_url: str = "", dedupe_key: str = "", **kwargs: Any) -> str:
    """Run fn(ctx, *args, **kwargs) in the background. fn returns the URL of the result page.

    back_url: where "Go back and try again" leads if the job fails (a site path).
    dedupe_key: if a queued or running job of this member has the same key, return it instead of starting
    a second one (protects against double submission paying twice). Make the key specific, for example
    "build.generate:<project id>".
    """
    if dedupe_key:
        with db.session() as conn:
            row = conn.execute("SELECT id FROM jobs WHERE user_id = ? AND dedupe_key = ? AND status IN ('queued', 'running')"
                               " ORDER BY created_at DESC LIMIT 1", (user_id, dedupe_key)).fetchone()
        if row:
            return row["id"]
    job_id = uuid.uuid4().hex
    safe_back = back_url if back_url.startswith("/") and not back_url.startswith("//") else ""
    with db.session() as conn:
        conn.execute(
            "INSERT INTO jobs (id, user_id, kind, title, status, created_at, back_url, dedupe_key)"
            " VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)",
            (job_id, user_id, kind, title[:200], db.now(), safe_back, dedupe_key),
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
