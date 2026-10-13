"""Home, about, accessibility statement and the background-job pages."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from . import jobs
from .web import redirect, render, require_user

router = APIRouter()


@router.get("/")
def home(request: Request):
    return render(request, "home.html", title="Build your ideas with AI, without needing to see the screen")


@router.get("/about")
def about(request: Request):
    return render(request, "pages/about.html", title="How this site works")


@router.get("/accessibility")
def accessibility(request: Request):
    return render(request, "pages/accessibility.html", title="Accessibility")


def _back_url(request: Request, job: dict) -> str:
    """Where "Go back and try again" leads: the job's own back URL, else a same-site Referer, else home."""
    if job.get("back_url"):
        return job["back_url"]
    referer = request.headers.get("referer") or ""
    parsed = urlparse(referer)
    if parsed.netloc and parsed.netloc == request.url.netloc and parsed.path.startswith("/"):
        return parsed.path + ("?" + parsed.query if parsed.query else "")
    return "/"


def _owned_job(job_id: str, user: dict) -> dict:
    job = jobs.get(job_id)
    if job is None or job["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="We could not find that piece of work.")
    return job


@router.get("/jobs")
def job_list(request: Request, user: dict = Depends(require_user)):
    return render(request, "jobs/list.html", title="Your recent work", jobs=jobs.recent_for_user(user["id"], 30))


@router.get("/jobs/{job_id}.json")
def job_json(job_id: str, user: dict = Depends(require_user)):
    job = _owned_job(job_id, user)
    return JSONResponse({k: job[k] for k in ("id", "status", "title", "progress", "result_url", "error")})


@router.get("/jobs/{job_id}")
def job_page(request: Request, job_id: str, user: dict = Depends(require_user)):
    job = _owned_job(job_id, user)
    if job["status"] == "done":
        return redirect(job["result_url"] or "/jobs")
    started = datetime.fromisoformat(job["created_at"])
    seconds = int((datetime.now(timezone.utc) - started).total_seconds())
    elapsed = f"{seconds} seconds" if seconds < 120 else f"{seconds // 60} minutes"
    title = f"This did not finish: {job['title']}" if job["status"] == "failed" else f"Working on it: {job['title']}"
    return render(request, "jobs/status.html", title=title, job=job, elapsed=elapsed,
                  back_url=_back_url(request, job))
