"""Find before you build. Placeholder until this section is built."""
from fastapi import APIRouter, Request

from ..web import render

router = APIRouter(prefix='/find')
NAV = {"label": 'Find before you build', "url": '/find', "description": 'Search for tools that already exist, with reports from blind people about whether they worked for them.'}
SCHEMA = ""


@router.get("")
def index(request: Request):
    section = NAV or {"label": 'Find before you build', "description": ""}
    return render(request, "placeholder.html", title=section["label"], section=section)
