"""Write and publish. Placeholder until this section is built."""
from fastapi import APIRouter, Request

from ..web import render

router = APIRouter(prefix='/write')
NAV = {"label": 'Write and publish', "url": '/write', "description": 'Write articles, papers and letters. The AI suggests changes one at a time and never silently rewords you. Get help finding a journal and submitting.'}
SCHEMA = ""


@router.get("")
def index(request: Request):
    section = NAV or {"label": 'Write and publish', "description": ""}
    return render(request, "placeholder.html", title=section["label"], section=section)
