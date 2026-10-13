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
from datetime import datetime, timezone
from typing import Any, Callable

from . import db
from .ai import AIError

log = logging.getLogger("studio.jobs")
_executor: ThreadPoolExecutor | None = None
_inline = False


RESTART_MESSAGE = "The site restarted before this finished. Nothing was lost except this step. Please try again."
FAILED_MESSAGE = ("Something went wrong on our side. Your work so far is saved. Please try again. If it stops at the "
                  "same step again, tell members in Ask the community which step it was.")
# What the job page tells members to expect. Building a whole website or research can take this long.
USUAL_MINUTES = 10
# The job threads are shared by every member, so one member can have only this many jobs waiting or running.
MAX_ACTIVE_PER_MEMBER = 3
# A job still running after this long has almost certainly hung. Time spent waiting for a free worker
# does not count: a queued job is waiting, not stopped.
STALLED_AFTER_SECONDS = 30 * 60


class TooManyJobs(Exception):
    """The member already has MAX_ACTIVE_PER_MEMBER jobs waiting or running."""

    user_message = (f"You already have {MAX_ACTIVE_PER_MEMBER} pieces of AI work waiting or running, so no new AI work "
                    "was started. Please wait for one to finish, then try again. You can follow them in Your recent "
                    "work.")


def is_stalled(job: dict) -> bool:
    """True if a job has been running (not waiting) for longer than any AI work takes."""
    if job.get("status") != "running":
        return False
    try:
        started = datetime.fromisoformat(job.get("started_at") or "")
    except ValueError:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds() > STALLED_AFTER_SECONDS


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
           back_url: str = "", dedupe_key: str = "", item_ref: str = "", **kwargs: Any) -> str:
    """Run fn(ctx, *args, **kwargs) in the background. fn returns the URL of the result page.

    back_url: where "Go back and try again" leads if the job fails (a site path).
    dedupe_key: if a queued or running job of this member has the same key, return it instead of starting
    a second one (protects against double submission paying twice). Make the key specific, for example
    "build.generate:<project id>".
    item_ref: the private item the job is about (for example "errands:<id>:<created_at>"). Job titles quote
    the member's words, so deleting the item calls forget_item() to remove its jobs from Your recent work.
    Raises TooManyJobs if the member already has MAX_ACTIVE_PER_MEMBER jobs waiting or running.
    """
    job_id = uuid.uuid4().hex
    safe_back = back_url if back_url.startswith("/") and not back_url.startswith("//") else ""
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")  # the checks and the insert are one step
        if dedupe_key:
            row = conn.execute("SELECT id FROM jobs WHERE user_id = ? AND dedupe_key = ? AND status IN ('queued', 'running')"
                               " ORDER BY created_at DESC LIMIT 1", (user_id, dedupe_key)).fetchone()
            if row:
                return row["id"]
        active = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status IN ('queued', 'running')",
                              (user_id,)).fetchone()[0]
        if active >= MAX_ACTIVE_PER_MEMBER:
            raise TooManyJobs()
        conn.execute(
            "INSERT INTO jobs (id, user_id, kind, title, status, created_at, back_url, dedupe_key, item_ref)"
            " VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
            (job_id, user_id, kind, title[:200], db.now(), safe_back, dedupe_key, item_ref),
        )
    if _inline or _executor is None:
        _run(job_id, user_id, fn, args, kwargs)
    else:
        _executor.submit(_run, job_id, user_id, fn, args, kwargs)
    return job_id


def still_wanted(conn, ctx: JobContext) -> bool:
    """False if the member deleted their account while the job ran (deleting an account removes the
    member's jobs and anonymises them). A job saving new private work checks this in the same transaction."""
    return conn.execute("SELECT 1 FROM jobs j JOIN users u ON u.id = j.user_id"
                        " WHERE j.id = ? AND u.email NOT LIKE '%@invalid'", (ctx.job_id,)).fetchone() is not None


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
        _fail(job_id, FAILED_MESSAGE)


def _fail(job_id: str, message: str) -> None:
    with db.session() as conn:
        conn.execute("UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                     (message, db.now(), job_id))


DELETED_ITEM_TITLE = "Work on something you deleted"


def forget_item(conn, user_id: int, item_ref: str) -> None:
    """The member deleted this item: remove its finished jobs, whose titles quote the member's words. A job
    still working keeps its row (it finishes and saves nothing) but loses its title."""
    if not item_ref:
        return
    conn.execute("DELETE FROM jobs WHERE user_id = ? AND item_ref = ? AND status NOT IN ('queued', 'running')",
                 (user_id, item_ref))
    conn.execute("UPDATE jobs SET title = ?, progress = '' WHERE user_id = ? AND item_ref = ?",
                 (DELETED_ITEM_TITLE, user_id, item_ref))


def clear_finished(user_id: int) -> int:
    """Remove the member's finished and failed jobs from Your recent work. Returns how many."""
    with db.session() as conn:
        return conn.execute("DELETE FROM jobs WHERE user_id = ? AND status IN ('done', 'failed')", (user_id,)).rowcount


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
