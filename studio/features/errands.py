"""Do this for me. Placeholder until this section is built."""
from fastapi import APIRouter, Request

from ..web import render

router = APIRouter(prefix='/do')
NAV = {"label": 'Do this for me', "url": '/do', "description": 'Ask the AI to find information on the web and bring it back with sources, or to prepare a task like a form or an application for you to send.'}
SCHEMA = ""


@router.get("")
def index(request: Request):
    section = NAV or {"label": 'Do this for me', "description": ""}
    return render(request, "placeholder.html", title=section["label"], section=section)
