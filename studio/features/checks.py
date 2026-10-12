"""Peer checks. Placeholder until this section is built."""
from fastapi import APIRouter, Request

from ..web import render

router = APIRouter(prefix='/checks')
NAV = {"label": 'Peer checks', "url": '/checks', "description": 'Ask other blind members to test your website or app with their screen reader, and check theirs. Every check is recorded honestly.'}
SCHEMA = ""


@router.get("")
def index(request: Request):
    section = NAV or {"label": 'Peer checks', "description": ""}
    return render(request, "placeholder.html", title=section["label"], section=section)
