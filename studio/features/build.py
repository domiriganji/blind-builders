"""Build. Placeholder until this section is built."""
from fastapi import APIRouter, Request

from ..web import render

router = APIRouter(prefix='/build')
NAV = {"label": 'Build', "url": '/build', "description": 'Describe a website, app or tool in your own words. The AI reads back what it understood, builds it, checks it and tells you exactly what changed.'}
SCHEMA = ""


@router.get("")
def index(request: Request):
    section = NAV or {"label": 'Build', "description": ""}
    return render(request, "placeholder.html", title=section["label"], section=section)
