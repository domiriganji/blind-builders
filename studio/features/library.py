"""Tool library. Placeholder until this section is built."""
from fastapi import APIRouter, Request

from ..web import render

router = APIRouter(prefix='/library')
NAV = {"label": 'Tool library', "url": '/library', "description": 'Tools that blind people made for each other, and help to keep them working when screen readers and apps update.'}
SCHEMA = ""


@router.get("")
def index(request: Request):
    section = NAV or {"label": 'Tool library', "description": ""}
    return render(request, "placeholder.html", title=section["label"], section=section)
