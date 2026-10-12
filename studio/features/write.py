"""Write and publish: documents, AI suggestions shown as before and after, versions, export,
finding where to publish, and drafting a cover letter.

The AI never changes a person's words by itself. Every suggestion quotes an exact excerpt of the
text; the server keeps only suggestions whose excerpt really is in the text, and the person
accepts or rejects each one. Every change to the text is saved as a version.
"""
from __future__ import annotations

import io
import json
import re
import unicodedata
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from .. import db, jobs
from ..ai import AIError, AINotAvailable
from ..web import checked_form, current_user, flash, get_ai, markdown, redirect, render, require_user, shift_headings

router = APIRouter(prefix="/write")
NAV = {"label": 'Write and publish', "url": '/write', "description": 'Write articles, papers and letters. The AI suggests changes one at a time and never silently rewords you. Get help finding a journal and submitting.'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS write_docs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('article','paper','letter','story','other')),
    body TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_write_docs_user ON write_docs(user_id);
CREATE TABLE IF NOT EXISTS write_versions (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES write_docs(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_write_versions_doc ON write_versions(doc_id);
CREATE TABLE IF NOT EXISTS write_batches (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES write_docs(id) ON DELETE CASCADE,
    goal TEXT NOT NULL,
    overall_note TEXT NOT NULL DEFAULT '',
    dropped_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_write_batches_doc ON write_batches(doc_id);
CREATE TABLE IF NOT EXISTS write_suggestions (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES write_docs(id) ON DELETE CASCADE,
    batch_id INTEGER NOT NULL REFERENCES write_batches(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    original TEXT NOT NULL,
    proposed TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected','stale')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_write_suggestions_batch ON write_suggestions(batch_id);
CREATE TABLE IF NOT EXISTS write_research (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES write_docs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    content_md TEXT NOT NULL,
    sources_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_write_research_doc ON write_research(doc_id);
"""

KINDS = [
    ("article", "Article"),
    ("paper", "Paper for a journal or conference"),
    ("letter", "Letter"),
    ("story", "Story"),
    ("other", "Something else"),
]
KIND_NAMES = dict(KINDS)

GOALS = [
    ("spelling", "Fix spelling and grammar only"),
    ("clearer", "Make it clearer and keep my voice"),
    ("structure", "Check the structure and argument"),
    ("shorter", "Make it shorter"),
    ("publication", "Prepare it for the publication I named"),
]
GOAL_NAMES = dict(GOALS)
GOAL_INSTRUCTIONS = {
    "spelling": "Fix spelling, grammar and punctuation mistakes only. Do not change word choice, style, tone or meaning.",
    "clearer": "Make sentences clearer and easier to follow. Keep the writer's own voice, word choices and meaning "
               "wherever you can. Change as little as possible.",
    "structure": "Check the structure and the argument: the order of points, missing steps, claims without support, "
                 "weak links between paragraphs. Suggest concrete edits to specific passages. Explain larger issues "
                 "in the overall note.",
    "shorter": "Make the text shorter. Cut repetition, filler and needless words. Keep every key point and the writer's voice.",
    "publication": "Prepare the text for the publication named below: suggest changes so it fits what that publication "
                   "usually expects in tone, length and structure. Only state a publication's rules if you are sure "
                   "of them; otherwise say in the overall note that the writer should check the author guidelines.",
}

MAX_TITLE = 200
MAX_TARGET = 200
MAX_BODY = 300_000
MAX_INSTRUCTIONS = 2000
MAX_AI_BODY = 150_000          # longest text we send for suggestions
VENUE_EXCERPT = 8000           # how much of the text we send when searching for venues
MAX_SUGGESTIONS = 60
GONE = "This document was deleted before the AI started, so there was nothing to work on."

STATUS_TEXT = {
    "pending": "Waiting for your decision.",
    "accepted": "Accepted. Your text has this change.",
    "rejected": "Rejected. Your text was not changed.",
    "stale": "Out of date. The words it would change are no longer in your text, so nothing was changed.",
}

SYSTEM_BASE = (
    "You help a writer on Blind Creators Studio. The writer is blind or has low vision and uses a screen reader. "
    "Put the summary first. Use plain language and short headings. Do not use tables unless asked. Do not use emoji. "
    "Never claim something was checked or tested unless it was."
)

SUGGEST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["overall_note", "suggestions"],
    "properties": {
        "overall_note": {"type": "string"},
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["original", "proposed", "reason"],
                "properties": {
                    "original": {"type": "string"},
                    "proposed": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}

# Looks like a payment card number: 15 or 16 digits, optionally in groups split by spaces or dashes.
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){14,15}\d(?!\d)")
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")


# ----- small helpers ---------------------------------------------------------------------------

def when(iso: str | None) -> str:
    """'2026-10-12T14:03:22+00:00' -> '2026-10-12 14:03 UTC'."""
    if not iso:
        return ""
    return iso[:16].replace("T", " ") + " UTC"


def word_count(text: str) -> int:
    return len((text or "").split())


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def looks_like_card_number(text: str) -> bool:
    for match in _CARD_RE.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) in (15, 16) and _luhn_ok(digits):
            return True
    return False


def _slug(title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_title).strip("-").lower()[:60].strip("-")
    return slug or "document"


def _not_found(what: str = "document") -> HTTPException:
    return HTTPException(status_code=404, detail=f"We could not find that {what}.")


def _clean_sources(sources) -> list[dict]:
    out = []
    for s in sources or []:
        url = getattr(s, "url", None) if not isinstance(s, dict) else s.get("url")
        title = getattr(s, "title", "") if not isinstance(s, dict) else s.get("title", "")
        quote = getattr(s, "quote", "") if not isinstance(s, dict) else s.get("quote", "")
        if not url or urlparse(str(url)).scheme not in ("http", "https"):
            continue
        out.append({"url": str(url), "title": str(title or url), "quote": str(quote or "")[:300]})
    return out


# ----- data access -----------------------------------------------------------------------------

def _owned_doc(doc_id: int, user: dict) -> dict:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM write_docs WHERE id = ? AND user_id = ?", (doc_id, user["id"])).fetchone()
    if row is None:
        raise _not_found()
    return dict(row)


def _get_doc(doc_id: int) -> dict | None:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM write_docs WHERE id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


def _versions(doc_id: int) -> list[dict]:
    """All versions, oldest first, each with its number (1 is the oldest)."""
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM write_versions WHERE doc_id = ? ORDER BY id", (doc_id,)).fetchall()
    out = []
    for i, r in enumerate(rows, start=1):
        v = dict(r)
        v["number"] = i
        v["when"] = when(v["created_at"])
        v["words"] = word_count(v["body"])
        out.append(v)
    return out


def _add_version(conn, doc_id: int, body: str, note: str) -> None:
    conn.execute("INSERT INTO write_versions (doc_id, body, note, created_at) VALUES (?, ?, ?, ?)",
                 (doc_id, body, note[:300], db.now()))


def _set_body(conn, doc_id: int, body: str, note: str) -> None:
    """Change the text and save it as a version. Every change to a document goes through here."""
    conn.execute("UPDATE write_docs SET body = ?, updated_at = ? WHERE id = ?", (body, db.now(), doc_id))
    _add_version(conn, doc_id, body, note)


def _owned_batch(doc: dict, batch_id: int) -> dict:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM write_batches WHERE id = ? AND doc_id = ?", (batch_id, doc["id"])).fetchone()
    if row is None:
        raise _not_found("set of suggestions")
    return dict(row)


def _suggestions(batch_id: int) -> list[dict]:
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM write_suggestions WHERE batch_id = ? ORDER BY number", (batch_id,)).fetchall()
    return [dict(r) for r in rows]


def _batches(doc_id: int) -> list[dict]:
    with db.session() as conn:
        rows = conn.execute(
            "SELECT b.*, (SELECT COUNT(*) FROM write_suggestions s WHERE s.batch_id = b.id) AS total,"
            " (SELECT COUNT(*) FROM write_suggestions s WHERE s.batch_id = b.id AND s.status = 'pending') AS waiting"
            " FROM write_batches b WHERE b.doc_id = ? ORDER BY b.id DESC", (doc_id,)).fetchall()
    out = []
    for r in rows:
        b = dict(r)
        b["when"] = when(b["created_at"])
        b["goal_name"] = GOAL_NAMES.get(b["goal"], b["goal"])
        out.append(b)
    return out


def _research_list(doc_id: int) -> list[dict]:
    with db.session() as conn:
        rows = conn.execute("SELECT id, kind, created_at FROM write_research WHERE doc_id = ? ORDER BY id DESC",
                            (doc_id,)).fetchall()
    return [dict(r, when=when(r["created_at"])) for r in rows]


def _doc_view(doc: dict) -> dict:
    view = dict(doc)
    view["words"] = word_count(doc["body"])
    view["kind_name"] = KIND_NAMES.get(doc["kind"], doc["kind"])
    view["updated"] = when(doc["updated_at"])
    return view


# ----- pages: list and create ------------------------------------------------------------------

def _new_values(title: str = "", kind: str = "article", target: str = "") -> dict:
    return {"title": title[:MAX_TITLE], "kind": kind if kind in KIND_NAMES else "article", "target": target[:MAX_TARGET]}


@router.get("")
def index(request: Request):
    user = current_user(request)
    if user is None:
        return render(request, "write/index.html", title="Write and publish", docs=[], values=_new_values(),
                      errors={}, kinds=KINDS)
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM write_docs WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
                            (user["id"],)).fetchall()
    docs = [_doc_view(dict(r)) for r in rows]
    return render(request, "write/index.html", title="Write and publish", docs=docs, values=_new_values(),
                  errors={}, kinds=KINDS)


@router.get("/new")
def new_form(request: Request, title: str = "", kind: str = "article", target: str = "",
             user: dict = Depends(require_user)):
    return render(request, "write/new.html", title="Start a new document", values=_new_values(title, kind, target),
                  errors={}, kinds=KINDS)


@router.post("/new")
async def create(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    values = {k: str(form.get(k, "")).strip() for k in ("title", "kind", "target")}
    errors: dict[str, str] = {}
    if not values["title"]:
        errors["title"] = "Enter a title for your document. You can change it later."
    elif len(values["title"]) > MAX_TITLE:
        errors["title"] = f"Use a title of {MAX_TITLE} characters or fewer."
    if values["kind"] not in KIND_NAMES:
        errors["kind"] = "Choose what kind of writing this is."
    if len(values["target"]) > MAX_TARGET:
        errors["target"] = f"Use {MAX_TARGET} characters or fewer for the publication's name."
    if errors:
        return render(request, "write/new.html", title="Start a new document: please fix the problems",
                      values=values, errors=errors, kinds=KINDS, status_code=400)
    now = db.now()
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO write_docs (user_id, title, kind, body, target, created_at, updated_at) VALUES (?, ?, ?, '', ?, ?, ?)",
            (user["id"], values["title"], values["kind"], values["target"], now, now))
        doc_id = cur.lastrowid
    flash(request, f"Your document \"{values['title']}\" is ready. Write or paste your text, then save.", "success")
    return redirect(f"/write/{doc_id}")


# ----- the document page -----------------------------------------------------------------------

def _render_doc(request: Request, doc: dict, *, title: str | None = None, values: dict | None = None,
                errors: dict | None = None, status_code: int = 200):
    versions = _versions(doc["id"])
    vals = {"title": doc["title"], "body": doc["body"], "target": doc["target"], "goal": "spelling",
            "instructions": "", "notes": ""}
    vals.update(values or {})
    return render(request, "write/doc.html", title=title or doc["title"], doc=_doc_view(doc), values=vals,
                  errors=errors or {}, goals=GOALS, versions=versions,
                  last_version=versions[-1] if versions else None,
                  batches=_batches(doc["id"]), research=_research_list(doc["id"]), status_code=status_code)


@router.get("/{doc_id}")
def doc_page(request: Request, doc_id: int, user: dict = Depends(require_user)):
    return _render_doc(request, _owned_doc(doc_id, user))


@router.post("/{doc_id}/save")
async def save(request: Request, doc_id: int, user: dict = Depends(require_user)):
    doc = _owned_doc(doc_id, user)
    form = await checked_form(request)
    body = str(form.get("body", "")).replace("\r\n", "\n")
    new_title = str(form.get("title", "")).strip()
    target = str(form.get("target", "")).strip()
    errors: dict[str, str] = {}
    if len(body) > MAX_BODY:
        errors["body"] = f"Your text is too long to save here ({len(body):,} characters). The limit is {MAX_BODY:,}. Split it into two documents."
    elif looks_like_card_number(body):
        errors["body"] = "Your text seems to contain a payment card number. Remove it and save again. This site never stores card numbers."
    if not new_title:
        errors["title"] = "Enter a title for your document."
    elif len(new_title) > MAX_TITLE:
        errors["title"] = f"Use a title of {MAX_TITLE} characters or fewer."
    if len(target) > MAX_TARGET:
        errors["target"] = f"Use {MAX_TARGET} characters or fewer for the publication's name."
    if errors:
        return _render_doc(request, doc, title=f"{doc['title']}: please fix the problems",
                           values={"body": body, "title": new_title, "target": target}, errors=errors, status_code=400)
    with db.session() as conn:
        conn.execute("UPDATE write_docs SET title = ?, target = ?, updated_at = ? WHERE id = ?",
                     (new_title, target, db.now(), doc_id))
        last = conn.execute("SELECT body FROM write_versions WHERE doc_id = ? ORDER BY id DESC LIMIT 1",
                            (doc_id,)).fetchone()
        text_changed = body != doc["body"] or (last is None and body != "")
        if text_changed:
            _set_body(conn, doc_id, body, "Saved by you")
            count = conn.execute("SELECT COUNT(*) AS n FROM write_versions WHERE doc_id = ?", (doc_id,)).fetchone()["n"]
    if text_changed:
        flash(request, f"Saved as version {count}. {plural(word_count(body), 'word')}.", "success")
    else:
        flash(request, "Saved. Your text had not changed, so no new version was needed.", "success")
    return redirect(f"/write/{doc_id}")


# ----- suggestions -----------------------------------------------------------------------------

@router.post("/{doc_id}/suggest")
async def suggest(request: Request, doc_id: int, user: dict = Depends(require_user)):
    doc = _owned_doc(doc_id, user)
    form = await checked_form(request)
    goal = str(form.get("goal", "")).strip()
    instructions = str(form.get("instructions", "")).strip()
    errors: dict[str, str] = {}
    if goal not in GOAL_NAMES:
        errors["goal"] = "Choose what you want the AI to look for."
    elif goal == "publication" and not doc["target"]:
        errors["goal"] = "Name the publication you are aiming for in the edit form and save first, or choose another goal."
    if len(instructions) > MAX_INSTRUCTIONS:
        errors["instructions"] = f"Keep your instructions to {MAX_INSTRUCTIONS} characters or fewer."
    elif looks_like_card_number(instructions):
        errors["instructions"] = "This seems to contain a payment card number. Remove it. This site never sends card numbers to the AI."
    if not doc["body"].strip():
        errors["goal"] = "Your document has no saved text yet. Write or paste your text, save it, then ask for suggestions."
    elif len(doc["body"]) > MAX_AI_BODY:
        errors["goal"] = (f"Your text is too long for one round of suggestions ({len(doc['body']):,} characters; the limit is "
                          f"{MAX_AI_BODY:,}). Split it into parts and ask about each part.")
    if errors:
        return _render_doc(request, doc, title=f"{doc['title']}: please fix the problems",
                           values={"goal": goal, "instructions": instructions}, errors=errors, status_code=400)
    try:
        ai = get_ai(request, user)
    except AINotAvailable as exc:
        flash(request, exc.user_message, "error")
        return redirect(f"/write/{doc_id}")
    job_id = jobs.submit(user["id"], "write.suggest", f"Suggestions for {doc['title']}", _suggest_job, ai, doc_id,
                         goal, instructions)
    return redirect(jobs.job_url(job_id))


def filter_suggestions(body: str, raw: list[dict]) -> tuple[list[dict], int]:
    """Keep suggestions whose original is an exact excerpt of body and that change something.
    Returns (kept, in document order) and how many were dropped."""
    kept: list[tuple[int, int, dict]] = []
    dropped = 0
    seen: set[tuple[str, str]] = set()
    for i, s in enumerate(raw or []):
        original = str(s.get("original", "")) if isinstance(s, dict) else ""
        proposed = str(s.get("proposed", "")) if isinstance(s, dict) else ""
        reason = str(s.get("reason", "")) if isinstance(s, dict) else ""
        pos = body.find(original) if original else -1
        if pos < 0 or original == proposed or (original, proposed) in seen:
            dropped += 1
            continue
        seen.add((original, proposed))
        kept.append((pos, i, {"original": original, "proposed": proposed, "reason": reason.strip()}))
    kept.sort(key=lambda t: (t[0], t[1]))
    if len(kept) > MAX_SUGGESTIONS:
        dropped += len(kept) - MAX_SUGGESTIONS
        kept = kept[:MAX_SUGGESTIONS]
    return [k[2] for k in kept], dropped


def _suggest_job(ctx, ai, doc_id: int, goal: str, instructions: str) -> str:
    doc = _get_doc(doc_id)
    if doc is None:
        raise AIError(GONE)
    body = doc["body"]
    ctx.progress("The AI is reading your text")
    prompt_parts = [
        f"Goal: {GOAL_INSTRUCTIONS[goal]}",
        f"Kind of writing: {KIND_NAMES.get(doc['kind'], doc['kind'])}.",
        f"Title: {doc['title']}",
    ]
    if doc["target"]:
        prompt_parts.append(f"Publication the writer is aiming for: {doc['target']}")
    if instructions:
        prompt_parts.append(f"Extra instructions from the writer: {instructions}")
    prompt_parts.append(
        "Rules for each suggestion:\n"
        "- original: copy an exact excerpt of the text, character for character, including punctuation. "
        "Keep it short: one sentence or less. Pick an excerpt that appears only once if you can. Suggestions must not overlap.\n"
        "- proposed: the new wording that replaces exactly that excerpt.\n"
        "- reason: one short sentence in plain words saying why.\n"
        f"Give at most {MAX_SUGGESTIONS // 2} suggestions, the most useful first. If nothing needs changing, give none.\n"
        "overall_note: two to four plain sentences about the text as a whole, summary first. Do not repeat the suggestions."
    )
    prompt_parts.append(f"The text:\n<text>\n{body}\n</text>")
    result = ai.structured(
        system=SYSTEM_BASE + " You suggest edits. The writer accepts or rejects each one, so every suggestion must quote "
                             "the text exactly. Respect the writer's voice.",
        prompt="\n\n".join(prompt_parts), schema=SUGGEST_SCHEMA, purpose="write.suggest")
    ctx.progress("Checking each suggestion against your text")
    kept, dropped = filter_suggestions(body, result.get("suggestions") or [])
    now = db.now()
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO write_batches (doc_id, goal, overall_note, dropped_count, created_at) VALUES (?, ?, ?, ?, ?)",
            (doc_id, goal, str(result.get("overall_note") or "").strip(), dropped, now))
        batch_id = cur.lastrowid
        for number, s in enumerate(kept, start=1):
            conn.execute(
                "INSERT INTO write_suggestions (doc_id, batch_id, number, original, proposed, reason, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (doc_id, batch_id, number, s["original"], s["proposed"], s["reason"], now))
    return f"/write/{doc_id}/suggestions/{batch_id}"


@router.get("/{doc_id}/suggestions/{batch_id}")
def suggestions_page(request: Request, doc_id: int, batch_id: int, user: dict = Depends(require_user)):
    doc = _owned_doc(doc_id, user)
    batch = _owned_batch(doc, batch_id)
    items = _suggestions(batch_id)
    for s in items:
        s["matches"] = s["original"] in doc["body"]
        s["context"] = in_context(doc["body"], s["original"]) if s["status"] == "pending" else ""
    n = len(items)
    counts = {k: sum(1 for s in items if s["status"] == k) for k in ("pending", "accepted", "rejected", "stale")}
    next_pending = next((s for s in items if s["status"] == "pending"), None)
    if n == 0:
        title = f"No suggestions for {doc['title']}"
    else:
        title = f"{plural(n, 'suggestion')} for {doc['title']}"
    return render(request, "write/suggestions.html", title=title, doc=_doc_view(doc),
                  batch=dict(batch, when=when(batch["created_at"]), goal_name=GOAL_NAMES.get(batch["goal"], batch["goal"])),
                  items=items, n=n, counts=counts, next_pending=next_pending, status_text=STATUS_TEXT)


def in_context(body: str, excerpt: str, width: int = 60) -> str:
    """The excerpt with a few words either side, so a short change like one word can be found by ear."""
    pos = body.find(excerpt) if excerpt else -1
    if pos < 0:
        return ""
    start, end = max(0, pos - width), min(len(body), pos + len(excerpt) + width)
    before, after = body[start:pos], body[pos + len(excerpt):end]
    if start > 0 and " " in before:
        before = before[before.index(" ") + 1:]
    if end < len(body) and " " in after:
        after = after[:after.rindex(" ")]
    if not before.strip() and not after.strip():
        return ""
    text = ("..." if start > 0 else "") + before + excerpt + after + ("..." if end < len(body) else "")
    return " ".join(text.split())


def _owned_suggestion(doc: dict, batch_id: int, suggestion_id: int) -> dict:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM write_suggestions WHERE id = ? AND batch_id = ? AND doc_id = ?",
                           (suggestion_id, batch_id, doc["id"])).fetchone()
    if row is None:
        raise _not_found("suggestion")
    return dict(row)


@router.post("/{doc_id}/suggestions/{batch_id}/{suggestion_id}/accept")
async def accept(request: Request, doc_id: int, batch_id: int, suggestion_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    doc = _owned_doc(doc_id, user)
    _owned_batch(doc, batch_id)
    s = _owned_suggestion(doc, batch_id, suggestion_id)
    back = f"/write/{doc_id}/suggestions/{batch_id}"
    if s["status"] != "pending":
        flash(request, f"Suggestion {s['number']} was already decided: {STATUS_TEXT[s['status']]}", "info")
        return redirect(back)
    with db.session() as conn:
        body = conn.execute("SELECT body FROM write_docs WHERE id = ?", (doc_id,)).fetchone()["body"]
        if s["original"] in body:
            _set_body(conn, doc_id, body.replace(s["original"], s["proposed"], 1), f"Accepted suggestion {s['number']}")
            conn.execute("UPDATE write_suggestions SET status = 'accepted' WHERE id = ?", (s["id"],))
            message, kind = f"Suggestion {s['number']} accepted. Your text has the change and is saved as a new version.", "success"
        else:
            conn.execute("UPDATE write_suggestions SET status = 'stale' WHERE id = ?", (s["id"],))
            message, kind = (f"Suggestion {s['number']} no longer matches your text, because the words it would change "
                             "are not there any more. Nothing was changed.", "error")
    flash(request, message, kind)
    return redirect(back)


@router.post("/{doc_id}/suggestions/{batch_id}/{suggestion_id}/reject")
async def reject(request: Request, doc_id: int, batch_id: int, suggestion_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    doc = _owned_doc(doc_id, user)
    _owned_batch(doc, batch_id)
    s = _owned_suggestion(doc, batch_id, suggestion_id)
    back = f"/write/{doc_id}/suggestions/{batch_id}"
    if s["status"] != "pending":
        flash(request, f"Suggestion {s['number']} was already decided: {STATUS_TEXT[s['status']]}", "info")
        return redirect(back)
    with db.session() as conn:
        conn.execute("UPDATE write_suggestions SET status = 'rejected' WHERE id = ?", (s["id"],))
    flash(request, f"Suggestion {s['number']} rejected. Your text is unchanged.", "success")
    return redirect(back)


def _join_numbers(numbers: list[int]) -> str:
    words = [str(n) for n in numbers]
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


@router.post("/{doc_id}/suggestions/{batch_id}/accept-all")
async def accept_all(request: Request, doc_id: int, batch_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    doc = _owned_doc(doc_id, user)
    _owned_batch(doc, batch_id)
    accepted: list[int] = []
    stale: list[int] = []
    with db.session() as conn:
        body = conn.execute("SELECT body FROM write_docs WHERE id = ?", (doc_id,)).fetchone()["body"]
        rows = conn.execute("SELECT * FROM write_suggestions WHERE batch_id = ? AND status = 'pending' ORDER BY number",
                            (batch_id,)).fetchall()
        for s in rows:
            if s["original"] in body:
                body = body.replace(s["original"], s["proposed"], 1)
                conn.execute("UPDATE write_suggestions SET status = 'accepted' WHERE id = ?", (s["id"],))
                accepted.append(s["number"])
            else:
                conn.execute("UPDATE write_suggestions SET status = 'stale' WHERE id = ?", (s["id"],))
                stale.append(s["number"])
        if accepted:
            note = (f"Accepted suggestion {accepted[0]}" if len(accepted) == 1
                    else f"Accepted suggestions {_join_numbers(accepted)}")
            _set_body(conn, doc_id, body, note)
    parts = []
    if accepted:
        parts.append(f"Accepted {plural(len(accepted), 'suggestion')} and saved your text as a new version.")
    if stale:
        parts.append(f"{plural(len(stale), 'suggestion')} no longer matched your text, so "
                     f"{'it was' if len(stale) == 1 else 'they were'} not applied: number {_join_numbers(stale)}.")
    if not parts:
        parts.append("There were no suggestions waiting for a decision. Nothing was changed.")
    flash(request, " ".join(parts), "success" if accepted else "info")
    return redirect(f"/write/{doc_id}/suggestions/{batch_id}")


@router.post("/{doc_id}/suggestions/{batch_id}/reject-all")
async def reject_all(request: Request, doc_id: int, batch_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    doc = _owned_doc(doc_id, user)
    _owned_batch(doc, batch_id)
    with db.session() as conn:
        cur = conn.execute("UPDATE write_suggestions SET status = 'rejected' WHERE batch_id = ? AND status = 'pending'",
                           (batch_id,))
        count = cur.rowcount
    if count:
        flash(request, f"Rejected {plural(count, 'suggestion')}. Your text is unchanged.", "success")
    else:
        flash(request, "There were no suggestions waiting for a decision. Nothing was changed.", "info")
    return redirect(f"/write/{doc_id}/suggestions/{batch_id}")


# ----- versions --------------------------------------------------------------------------------

@router.get("/{doc_id}/versions")
def versions_page(request: Request, doc_id: int, user: dict = Depends(require_user)):
    doc = _owned_doc(doc_id, user)
    versions = list(reversed(_versions(doc_id)))
    return render(request, "write/versions.html", title=f"Versions of {doc['title']}", doc=_doc_view(doc),
                  versions=versions)


def _owned_version(doc: dict, version_id: int) -> dict:
    for v in _versions(doc["id"]):
        if v["id"] == version_id:
            return v
    raise _not_found("version")


@router.get("/{doc_id}/versions/{version_id}")
def version_page(request: Request, doc_id: int, version_id: int, user: dict = Depends(require_user)):
    doc = _owned_doc(doc_id, user)
    v = _owned_version(doc, version_id)
    return render(request, "write/version.html", title=f"Version {v['number']} of {doc['title']}", doc=_doc_view(doc),
                  version=v, same_as_current=(v["body"] == doc["body"]))


@router.post("/{doc_id}/versions/{version_id}/restore")
async def restore(request: Request, doc_id: int, version_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    doc = _owned_doc(doc_id, user)
    v = _owned_version(doc, version_id)
    if v["body"] == doc["body"]:
        flash(request, f"Version {v['number']} is the same as your current text. Nothing was changed.", "info")
        return redirect(f"/write/{doc_id}")
    with db.session() as conn:
        last = conn.execute("SELECT body FROM write_versions WHERE doc_id = ? ORDER BY id DESC LIMIT 1",
                            (doc_id,)).fetchone()
        if last is None or last["body"] != doc["body"]:
            _add_version(conn, doc_id, doc["body"], f"Your text before restoring version {v['number']}")
        _set_body(conn, doc_id, v["body"], f"Restored version {v['number']}")
    flash(request, f"Restored version {v['number']}. Your text from before is kept in the versions list.", "success")
    return redirect(f"/write/{doc_id}")


# ----- export ----------------------------------------------------------------------------------

@router.get("/{doc_id}/export")
def export_page(request: Request, doc_id: int, user: dict = Depends(require_user)):
    doc = _owned_doc(doc_id, user)
    return render(request, "write/export.html", title=f"Export {doc['title']}", doc=_doc_view(doc))


def _body_without_title(doc: dict) -> str:
    """The text, minus a first line that only repeats the title as a heading (the export adds the title itself)."""
    lines = doc["body"].split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        m = re.match(r"^#\s+(.*?)\s*#*\s*$", line)
        if m and m.group(1).strip().lower() == doc["title"].strip().lower():
            return "\n".join(lines[i + 1:]).lstrip("\n")
        break
    return doc["body"]


def _heading(line: str) -> tuple[int, str] | None:
    m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
    if m and m.group(2):
        return len(m.group(1)), m.group(2)
    return None


def export_markdown(doc: dict) -> str:
    return f"# {doc['title']}\n\n{_body_without_title(doc).rstrip()}\n"


def export_text(doc: dict) -> str:
    return f"{doc['title']}\n\n{_body_without_title(doc).rstrip()}\n"


def export_html(doc: dict, lang: str = "en") -> str:
    from markupsafe import escape
    lang = lang if _LANG_RE.match(lang or "") else "en"
    content = shift_headings(markdown(_body_without_title(doc)), 1)
    return (
        "<!doctype html>\n"
        f'<html lang="{escape(lang)}">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(doc['title'])}</title>\n"
        "<style>body{max-width:45rem;margin:0 auto;padding:1rem;font-family:system-ui,sans-serif;"
        "font-size:1.125rem;line-height:1.6}</style>\n"
        "</head>\n<body>\n<main>\n"
        f"<h1>{escape(doc['title'])}</h1>\n{content}</main>\n</body>\n</html>\n"
    )


def export_docx(doc: dict) -> bytes:
    from docx import Document

    document = Document()
    document.core_properties.title = doc["title"]
    document.add_heading(doc["title"], level=1)
    for line in _body_without_title(doc).split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        heading = _heading(stripped)
        if heading:
            level, text = heading
            document.add_heading(text, level=min(level + 1, 9))
        elif re.match(r"^[-*]\s+", stripped):
            document.add_paragraph(re.sub(r"^[-*]\s+", "", stripped), style="List Bullet")
        else:
            document.add_paragraph(line.rstrip())
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


_LATEX_SPECIAL = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_LATEX_RE = re.compile("|".join(re.escape(k) for k in _LATEX_SPECIAL))


def latex_escape(text: str) -> str:
    return _LATEX_RE.sub(lambda m: _LATEX_SPECIAL[m.group(0)], text)


def export_tex(doc: dict) -> str:
    sections = {1: "section", 2: "subsection", 3: "subsubsection", 4: "paragraph", 5: "subparagraph", 6: "subparagraph"}
    out = [
        r"\documentclass{article}",
        r"\usepackage[utf8]{inputenc}",
        r"\usepackage[T1]{fontenc}",
        r"\title{" + latex_escape(doc["title"]) + "}",
        r"\date{}",
        r"\begin{document}",
        r"\maketitle",
        "",
    ]
    for line in _body_without_title(doc).split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        heading = _heading(stripped)
        if heading:
            out.append("\\" + sections[heading[0]] + "{" + latex_escape(heading[1]) + "}")
        else:
            out.append(latex_escape(line.rstrip()))
        out.append("")
    out.append(r"\end{document}")
    return "\n".join(out) + "\n"


def _download(content: str | bytes, media_type: str, filename: str) -> Response:
    return Response(content=content, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{doc_id}/export.md")
def export_md_file(doc_id: int, user: dict = Depends(require_user)):
    doc = _owned_doc(doc_id, user)
    return _download(export_markdown(doc), "text/markdown; charset=utf-8", _slug(doc["title"]) + ".md")


@router.get("/{doc_id}/export.txt")
def export_txt_file(doc_id: int, user: dict = Depends(require_user)):
    doc = _owned_doc(doc_id, user)
    return _download(export_text(doc), "text/plain; charset=utf-8", _slug(doc["title"]) + ".txt")


@router.get("/{doc_id}/export.html")
def export_html_file(doc_id: int, user: dict = Depends(require_user)):
    doc = _owned_doc(doc_id, user)
    return _download(export_html(doc, user.get("language") or "en"), "text/html; charset=utf-8",
                     _slug(doc["title"]) + ".html")


@router.get("/{doc_id}/export.docx")
def export_docx_file(doc_id: int, user: dict = Depends(require_user)):
    doc = _owned_doc(doc_id, user)
    return _download(export_docx(doc), "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                     _slug(doc["title"]) + ".docx")


@router.get("/{doc_id}/export.tex")
def export_tex_file(doc_id: int, user: dict = Depends(require_user)):
    doc = _owned_doc(doc_id, user)
    return _download(export_tex(doc), "application/x-tex; charset=utf-8", _slug(doc["title"]) + ".tex")


# ----- where to publish ------------------------------------------------------------------------

@router.post("/{doc_id}/venues")
async def venues(request: Request, doc_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    doc = _owned_doc(doc_id, user)
    if not doc["body"].strip():
        flash(request, "Your document has no saved text yet. Save your text first, so the AI knows what it is about.", "error")
        return redirect(f"/write/{doc_id}")
    try:
        ai = get_ai(request, user)
    except AINotAvailable as exc:
        flash(request, exc.user_message, "error")
        return redirect(f"/write/{doc_id}")
    job_id = jobs.submit(user["id"], "write.venues", f"Where to publish {doc['title']}", _venues_job, ai, doc_id)
    return redirect(jobs.job_url(job_id))


def _venues_job(ctx, ai, doc_id: int) -> str:
    doc = _get_doc(doc_id)
    if doc is None:
        raise AIError(GONE)
    ctx.progress("The AI is searching the web for places to publish")
    excerpt = doc["body"][:VENUE_EXCERPT]
    prompt = (
        f"Find suitable places to publish this {KIND_NAMES.get(doc['kind'], doc['kind']).lower()}: journals, magazines, "
        "websites or preprint servers.\n"
        + (f"The writer is considering: {doc['target']}. Include it and say how well it fits.\n" if doc["target"] else "")
        + f"Title: {doc['title']}\nLength: about {word_count(doc['body'])} words.\n"
        f"The text (it may be cut short):\n<text>\n{excerpt}\n</text>\n\n"
        "Start with a short summary naming the best two or three choices and why. Then give each place its own "
        "level-1 Markdown heading (# Name), with:\n"
        "- what it publishes and who reads it;\n"
        "- submission requirements: length, format, referencing style, anonymised copy, and a link to the author guidelines;\n"
        "- fees: any submission fee or open access fee (article processing charge), with the amount and currency, "
        "or say clearly that you did not find one;\n"
        "- the submission system it uses (for example a named online system or email), and anything published about how "
        "well it works with screen readers. If you found nothing, write 'Nothing found about screen reader access'. Never guess.\n"
        "Only state facts you found in your sources. Say when a fact may be out of date."
    )
    result = ai.research(system=SYSTEM_BASE + " You help writers choose where to submit their work.",
                         prompt=prompt, purpose="write.venues")
    sources = _clean_sources(result.sources)
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO write_research (doc_id, kind, content_md, sources_json, created_at) VALUES (?, 'venues', ?, ?, ?)",
            (doc_id, result.text, json.dumps(sources), db.now()))
        rid = cur.lastrowid
    return f"/write/{doc_id}/publish/{rid}"


@router.get("/{doc_id}/publish/{research_id}")
def research_page(request: Request, doc_id: int, research_id: int, user: dict = Depends(require_user)):
    doc = _owned_doc(doc_id, user)
    with db.session() as conn:
        row = conn.execute("SELECT * FROM write_research WHERE id = ? AND doc_id = ?", (research_id, doc_id)).fetchone()
    if row is None:
        raise _not_found("search result")
    research = dict(row)
    try:
        sources = _clean_sources(json.loads(research["sources_json"] or "[]"))
    except (ValueError, TypeError):
        sources = []
    return render(request, "write/research.html", title=f"Where you could publish {doc['title']}", doc=_doc_view(doc),
                  research=dict(research, when=when(research["created_at"])), sources=sources)


# ----- cover letter ----------------------------------------------------------------------------

@router.post("/{doc_id}/cover-letter")
async def cover_letter(request: Request, doc_id: int, user: dict = Depends(require_user)):
    form = await checked_form(request)
    doc = _owned_doc(doc_id, user)
    notes = str(form.get("notes", "")).strip()
    errors: dict[str, str] = {}
    if not doc["target"]:
        errors["notes"] = "Name the publication you are aiming for in the edit form and save, then draft a cover letter."
    elif not doc["body"].strip():
        errors["notes"] = "Your document has no saved text yet. Save your text first, so the letter can describe it."
    elif len(notes) > MAX_INSTRUCTIONS:
        errors["notes"] = f"Keep this to {MAX_INSTRUCTIONS} characters or fewer."
    elif looks_like_card_number(notes):
        errors["notes"] = "This seems to contain a payment card number. Remove it. This site never sends card numbers to the AI."
    if errors:
        return _render_doc(request, doc, title=f"{doc['title']}: please fix the problems", values={"notes": notes},
                           errors=errors, status_code=400)
    try:
        ai = get_ai(request, user)
    except AINotAvailable as exc:
        flash(request, exc.user_message, "error")
        return redirect(f"/write/{doc_id}")
    job_id = jobs.submit(user["id"], "write.cover_letter", f"Cover letter for {doc['title']}", _cover_letter_job,
                         ai, doc_id, notes)
    return redirect(jobs.job_url(job_id))


def _cover_letter_job(ctx, ai, doc_id: int, notes: str) -> str:
    doc = _get_doc(doc_id)
    if doc is None:
        raise AIError(GONE)
    ctx.progress("The AI is drafting your cover letter")
    prompt = (
        f"Draft a cover letter to the editor of {doc['target']}, submitting the "
        f"{KIND_NAMES.get(doc['kind'], doc['kind']).lower()} titled \"{doc['title']}\" "
        f"(about {word_count(doc['body'])} words).\n"
        + (f"The writer wants the letter to mention: {notes}\n" if notes else "")
        + "Use only facts found in the text or in the writer's notes. Never invent the writer's name, credentials, "
        "past publications, funding or affiliations. Put anything you do not know in square brackets, for example "
        "[Your name] or [Editor's name], so the writer can find and fill it in.\n"
        "Write plain text only: no Markdown, no headings, no emoji. Keep it under 400 words. "
        "Say briefly what the work is, why it suits this publication, and confirm it is not under consideration "
        "elsewhere as a bracketed item the writer must confirm.\n"
        f"The text:\n<text>\n{doc['body'][:MAX_AI_BODY]}\n</text>"
    )
    draft = ai.text(system=SYSTEM_BASE + " You draft cover letters for submissions. The writer will edit and send it themselves.",
                    prompt=prompt, purpose="write.cover_letter")
    title = f"Cover letter: {doc['title']}"[:MAX_TITLE]
    now = db.now()
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO write_docs (user_id, title, kind, body, target, created_at, updated_at) VALUES (?, ?, 'letter', ?, ?, ?, ?)",
            (doc["user_id"], title, draft, doc["target"], now, now))
        new_id = cur.lastrowid
        _add_version(conn, new_id, draft, "Drafted by the AI")
    return f"/write/{new_id}"


# ----- delete ----------------------------------------------------------------------------------

@router.get("/{doc_id}/delete")
def delete_page(request: Request, doc_id: int, user: dict = Depends(require_user)):
    doc = _owned_doc(doc_id, user)
    return render(request, "write/delete.html", title=f"Delete {doc['title']}?", doc=_doc_view(doc),
                  version_count=len(_versions(doc_id)))


@router.post("/{doc_id}/delete")
async def delete(request: Request, doc_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    doc = _owned_doc(doc_id, user)
    with db.session() as conn:
        conn.execute("DELETE FROM write_suggestions WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM write_batches WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM write_versions WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM write_research WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM write_docs WHERE id = ? AND user_id = ?", (doc_id, user["id"]))
    flash(request, f"Deleted \"{doc['title']}\" and all its versions.", "success")
    return redirect("/write")
