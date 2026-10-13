"""Home, about, accessibility statement and the background-job pages."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from . import credits, jobs, site_ai
from .web import checked_form, current_user, date_words, flash, redirect, render, require_user

router = APIRouter()
HOME_TITLE = "Build your ideas with AI: websites, apps, writing and research"
# A job running far longer than any AI work takes has almost certainly hung. Saying so is better than
# "Working on it" for ever. (A restart marks unfinished jobs as stopped; see jobs.configure.)
STALLED_AFTER_SECONDS = jobs.STALLED_AFTER_SECONDS


def pay_facts(settings) -> dict:
    """What a visitor needs to know about paying for the AI here, in the same words on every page."""
    from .features.account import payments_ready, typical_costs  # here: features import this module's helpers
    free = site_ai.site_ai_ready(settings) and site_ai.pay_mode() == "free"
    limit = site_ai.member_month_limit()
    low, high = typical_costs(settings)
    return {"free": free, "free_limit": credits.format_cents(limit) if free and limit else "",
            "can_buy": payments_ready(settings), "cost_low": low, "cost_high": high}


@router.get("/")
def home(request: Request):
    # After signing in or out, or deleting an account, the h1 says what just happened.
    heading = request.session.pop("home_heading", None)
    settings = request.app.state.settings
    user = current_user(request)
    next_steps = None
    if user:
        from .features.account import ai_status
        if ai_status(settings, user)["mode"] == "none":
            next_steps = {"code_saved": bool(user.get("recovery_hash"))}
    return render(request, "home.html", title=heading if isinstance(heading, str) and heading else HOME_TITLE,
                  home_title=HOME_TITLE, pay=pay_facts(settings), next_steps=next_steps)


@router.get("/about")
def about(request: Request):
    return render(request, "pages/about.html", title="How this site works", pay=pay_facts(request.app.state.settings))


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
    if jobs.is_stalled(job):
        return "stalled"
    return "working"


def _when(text: str | None) -> str:
    """12 October 2026, 14:03 UTC"""
    return date_words(text, with_time=True) if text else ""


def _clock(text: str | None) -> str:
    """14:03, to tell apart work with the same name."""
    return (text or "")[11:16]


@router.get("/jobs")
def job_list(request: Request, user: dict = Depends(require_user)):
    groups: dict[str, list[dict]] = {"working": [], "failed": [], "done": []}
    for job in jobs.recent_for_user(user["id"], 30):
        state = _state(job)
        item = {**job, "state": state, "when": _when(job["created_at"]), "clock": _clock(job["created_at"]),
                "result_url": _local_path(job["result_url"]) or ""}
        groups["failed" if state == "stalled" else state].append(item)
    counts = [(len(groups["working"]), "still working"), (len(groups["failed"]), "did not finish"),
              (len(groups["done"]), "done")]
    summary = ", ".join(f"{n} {words}" for n, words in counts if n)
    return render(request, "jobs/list.html", title="Your recent work", groups=groups,
                  total=sum(n for n, _ in counts), summary=summary)


@router.post("/jobs/clear")
async def clear_jobs(request: Request, user: dict = Depends(require_user)):
    """Remove finished work from the list. Titles quote the member's words, which a shared device shows."""
    await checked_form(request)
    removed = jobs.clear_finished(user["id"])
    if removed:
        flash(request, f"Removed {removed} finished {'piece' if removed == 1 else 'pieces'} of work from this list. "
                       "Your saved results are not changed.", "success")
    else:
        flash(request, "There was no finished work to remove. Your saved results are not changed.", "info")
    return redirect("/jobs")


@router.get("/jobs/{job_id}.json")
def job_json(job_id: str, user: dict = Depends(require_user)):
    """For enhance.js on the job page. `state` is what the page itself would say: working, done, failed or
    stalled (running far longer than any AI work takes)."""
    job = _owned_job(job_id, user)
    data = {k: job[k] for k in ("id", "status", "title", "progress", "result_url", "error")}
    data["state"] = _state(job)
    data["result_url"] = _local_path(job["result_url"]) or ("/jobs" if job["status"] == "done" else "")
    data["back_url"] = _local_path(job.get("back_url")) or ""
    data["elapsed"] = _elapsed_words(_seconds_since(job["created_at"]))
    if data["state"] == "failed" and not data["error"]:
        data["error"] = jobs.FAILED_MESSAGE
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
                  back_url=back_url, failed_message=jobs.FAILED_MESSAGE, usual_minutes=jobs.USUAL_MINUTES,
                  stalled_minutes=STALLED_AFTER_SECONDS // 60)
