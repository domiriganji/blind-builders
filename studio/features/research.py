"""Research. Placeholder until this section is built."""
from fastapi import APIRouter, Request

from ..web import render

router = APIRouter(prefix='/research')
NAV = {"label": 'Research', "url": '/research', "description": 'Run your own research: find prior work with sources, plan a study and draft a paper.'}
SCHEMA = ""


@router.get("")
def index(request: Request):
    section = NAV or {"label": 'Research', "description": ""}
    return render(request, "placeholder.html", title=section["label"], section=section)
