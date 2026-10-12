"""AI tools report. Placeholder until this section is built."""
from fastapi import APIRouter, Request

from ..web import render

router = APIRouter(prefix='/ai-tools')
NAV = {"label": 'AI tools report', "url": '/ai-tools', "description": 'Which AI coding tools work with screen readers right now, reported by members after each update.'}
SCHEMA = ""


@router.get("")
def index(request: Request):
    section = NAV or {"label": 'AI tools report', "description": ""}
    return render(request, "placeholder.html", title=section["label"], section=section)
