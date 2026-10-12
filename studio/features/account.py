"""Account, pricing and credits. Placeholder until this section is built."""
from fastapi import APIRouter, Request

from ..web import render

router = APIRouter()
NAV = None
SCHEMA = ""


@router.get("/account")
def index(request: Request):
    return render(request, "placeholder.html", title="Your account", section={"label": "Your account", "description": ""})


@router.get("/pricing")
def pricing(request: Request):
    return render(request, "placeholder.html", title="How paying works",
                  section={"label": "How paying works", "description": "Use your own Claude API key, or buy credits."})
