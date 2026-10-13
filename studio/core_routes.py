"""Home, about, accessibility statement and the background-job pages."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from . import jobs
from .web import redirect, render, require_user

router = APIRouter()
HOME_TITLE = "Build your ideas with AI, without needing to see the screen"
# A job still "queued" or "running" after this long has almost certainly stopped (for example the
# server restarted while it ran). Saying so is better than "Working on it" for ever.
STALLED_AFTER_SECONDS = 30 * 60


@router.get("/")
def home(request: Request):
    # After signing in or out, or deleting an account, the h1 says what just happened.
    heading = request.session.pop("home_heading", None)
    return render(request, "home.html", title=heading if isinstance(heading, str) and heading else HOME_TITLE,
                  home_title=HOME_TITLE)


@router.get("/about")
def about(request: Request):
    return render(request, "pages/about.html", title="How this site works")


@router.get("/accessibility")
def accessibility(request: Request):
    return render(request, "pages/accessibility.html", title="Accessibility")


def _owned_job(job_id: str, user: dict) -> dict:
    job = jobs.get(job_id)
    if job is None or job["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="We could not find that piece of work.")
    return job


def _local_path(url: str | None) -> str | None:
    """A path on this site, or None. Result links and "go back" links never lead to other sites."""
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url:
        return None
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in url):
        return None
    return url


def _seconds_since(text: str | None) -> int:
    try:
        started = datetime.fromisoformat(text or "")
    except ValueError:
        return 0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))


def _elapsed_words(seconds: int) -> str:
    if seconds < 120:
        return "1 second" if seconds == 1 else f"{seconds} seconds"
    if seconds < 2 * 3600:
        return f"{seconds // 60} minutes"
    hours = seconds // 3600
    return "1 hour" if hours == 1 else f"{hours} hours"


def _state(job: dict) -> str:
    """done, failed, stalled or working."""
    if job["status"] in ("done", "failed"):
        return job["status"]
    if _seconds_since(job["created_at"]) > STALLED_AFTER_SECONDS:
        return "stalled"
    return "working"


def _when(text: str | None) -> str:
    """2026-10-12 14:03 UTC"""
    return (text or "")[:16].replace("T", " ") + " UTC" if text else ""


@router.get("/jobs")
def job_list(request: Request, user: dict = Depends(require_user)):
    groups: dict[str, list[dict]] = {"working": [], "failed": [], "done": []}
    for job in jobs.recent_for_user(user["id"], 30):
        state = _state(job)
        item = {**job, "state": state, "when": _when(job["created_at"]),
                "result_url": _local_path(job["result_url"]) or ""}
        groups["failed" if state == "stalled" else state].append(item)
    counts = [(len(groups["working"]), "still working"), (len(groups["failed"]), "did not finish"),
              (len(groups["done"]), "done")]
    summary = ", ".join(f"{n} {words}" for n, words in counts if n)
    return render(request, "jobs/list.html", title="Your recent work", groups=groups,
                  total=sum(n for n, _ in counts), summary=summary)


@router.get("/jobs/{job_id}.json")
def job_json(job_id: str, user: dict = Depends(require_user)):
    job = _owned_job(job_id, user)
    data = {k: job[k] for k in ("id", "status", "title", "progress", "result_url", "error")}
    data["result_url"] = _local_path(job["result_url"]) or ("/jobs" if job["status"] == "done" else "")
    return JSONResponse(data)


@router.get("/jobs/{job_id}")
def job_page(request: Request, job_id: str, user: dict = Depends(require_user)):
    job = _owned_job(job_id, user)
    state = _state(job)
    if state == "done":
        return redirect(_local_path(job["result_url"]) or "/jobs")
    elapsed = _elapsed_words(_seconds_since(job["created_at"]))
    if state == "failed":
        title = f"This did not finish: {job['title']}"
    elif state == "stalled":
        title = f"This seems to have stopped: {job['title']}"
    else:
        title = f"Working on it: {job['title']}"
    # "Go back and try again" goes to the page the job named when it started, else to the page the
    # person came from, but only if either is on this site.
    back_url = _local_path(job.get("back_url"))
    if not back_url:
        referer = urlsplit(request.headers.get("referer") or "")
        own_host = request.headers.get("host", "")
        if referer.netloc == own_host and referer.path != f"/jobs/{job_id}":
            back_url = _local_path(referer.path + ("?" + referer.query if referer.query else ""))
    return render(request, "jobs/status.html", title=title, job=job, state=state, elapsed=elapsed,
                  back_url=back_url)
