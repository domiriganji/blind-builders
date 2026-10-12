"""Learn and help. Placeholder until this section is built."""
from fastapi import APIRouter, Request

from ..web import render

router = APIRouter()
NAV = {"label": "Learn and help", "url": "/learn", "description": "Guides for building with AI using a screen reader, and a place to ask the community for help."}
SCHEMA = ""


@router.get("/learn")
def index(request: Request):
    return render(request, "placeholder.html", title=NAV["label"], section=NAV)


@router.get("/help")
def help_index(request: Request):
    return render(request, "placeholder.html", title="Ask the community",
                  section={"label": "Ask the community", "description": "Members answer each other's questions."})
