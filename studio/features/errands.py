"""Do this for me: the AI finds information on the web, or prepares a task on a website for the person to do.

The flow for every errand:
1. The person says what they need, in their own words.
2. The AI reads back an errand contract (goal, steps, what it will not do, when it is done, questions).
3. The person corrects it or says go ahead.
4. The AI finds the answer with sources, or prepares the task: where to go, steps with screen reader
   tips, prepared answers and known accessibility problems.

The site never logs in, never pays and never submits or sends anything for the person. It finds,
explains and prepares; the person stays in control. Requests that look like they contain passwords,
card numbers or one-time codes are refused before anything is saved.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from markupsafe import Markup

from .. import db, jobs, sensitive
from ..ai import AIError, AINotAvailable
from ..web import checked_form, flash, get_ai, markdown, redirect, render, require_user

router = APIRouter(prefix="/do")
NAV = {"label": "Do this for me", "url": "/do", "description": "Ask the AI to find information on the web and bring it back with sources, or to prepare a task like a form or an application for you to send."}

SCHEMA = """
CREATE TABLE IF NOT EXISTS errands (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('find', 'task')),
    request TEXT NOT NULL,
    contract_json TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    result_md TEXT NOT NULL DEFAULT '',
    sources_json TEXT NOT NULL DEFAULT '[]',
    prepared_json TEXT NOT NULL DEFAULT '',
    parent_id INTEGER REFERENCES errands(id) ON DELETE SET NULL,
    notes_json TEXT NOT NULL DEFAULT '[]',
    job_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_errands_user ON errands(user_id);
"""

KINDS = [
    ("find", "Find information and bring it back to me"),
    ("task", "Help me do a task on a website"),
]
REQUEST_HINT = "Say what you need, as you would to a friend. Do not include passwords, card numbers or codes."
MAX_REQUEST = 4000
MAX_NOTE = 4000
MAX_NOTES = 20  # corrections and answers kept for one errand

# Status of an errand:
#   new         saved; the AI has not read it back yet (a job may be running, or it failed)
#   understood  the read-back is ready and waits for the person
#   working     the person said go ahead; the AI is finding or preparing (or that job failed)
#   done        the result is ready
STATUS_WORDS = {
    "new": "Reading back your request",
    "understood": "Waiting for you to check what I understood",
    "working": "Working on it",
    "done": "Done",
}

# The three things the site never does. Every contract says them, whatever the AI wrote.
SITE_RULES = [
    "Log in to any website for you, or ask for your passwords or codes.",
    "Pay for anything, or ask for your card or bank details.",
    "Send, submit or sign anything for you. You do that yourself, when you are ready.",
]
# AI-written "will not do" items about the same things are dropped, so the rules are not said twice.
_RULE_TOPICS = re.compile(
    r"\b(passwords?|passcodes?|log ?in|login|sign ?in|pay|pays|paying|payment|payments|card|bank|"
    r"submit|submits|submitting|send|sends|sending|sign)\b", re.IGNORECASE)

# ----- secrets -----------------------------------------------------------------------------------
# One detector for every section that sends a member's words to the AI (studio/sensitive.py).

find_secret = sensitive.find_secret
_luhn_ok = sensitive.luhn_ok


# ----- small helpers -----------------------------------------------------------------------------

def _short(text: str, limit: int = 80) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:.-")
    return (cut or text[:limit]) + "..."


def _str(value: Any, limit: int = 2000) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return " ".join(str(value).split())[:limit]


def _list(value: Any) -> list:
    """The value if the AI gave a list, otherwise an empty list (never a string or a number to loop over)."""
    return value if isinstance(value, list) else []


def _str_list(value: Any, limit: int = 30) -> list[str]:
    return [s for s in (_str(v) for v in _list(value)) if s][:limit]


def _form_text(form: dict, name: str) -> str:
    """One text field from a checked form: the first value if it was sent twice, line endings made plain."""
    value = form.get(name)
    if isinstance(value, list):
        value = value[0] if value else ""
    if not isinstance(value, str):
        return ""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _web_url(url: Any) -> str:
    """The URL if it is an absolute http or https address, otherwise an empty string."""
    url = _str(url, 2000)
    parsed = urlparse(url)
    return url if parsed.scheme in ("http", "https") and parsed.netloc else ""


def _loads(text: str, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


_HEADING_TAG = re.compile(r"<(/?)h([1-6])>")


def answer_html(md: str, top: int = 3) -> Markup:
    """Render AI Markdown (raw HTML escaped) with its headings moved under the page's own headings.

    The AI's top heading level becomes `top`, and no heading skips a level (an h2 followed by an h4
    becomes h3 then h4), so people moving by headings never land in a gap.
    """
    html = str(markdown(md))
    levels = [int(m.group(2)) for m in _HEADING_TAG.finditer(html) if not m.group(1)]
    if not levels:
        return Markup(html)
    shift = top - min(levels)
    state = {"prev": top - 1, "open": top}

    def fix(m: re.Match) -> str:
        if m.group(1):
            return f"</h{state['open']}>"
        level = min(max(int(m.group(2)) + shift, top), state["prev"] + 1, 6)
        state["prev"] = state["open"] = level
        return f"<h{level}>"

    return Markup(_HEADING_TAG.sub(fix, html))


# The site never asks for these, so a read-back question or request for them is dropped.
_ASKS_FOR_SECRET = re.compile(
    r"\b(passwords?|passcodes?|passphrases?|pins?|pin (?:code|number)|cvv|cvc|(?:credit|debit|bank|payment) cards?|"
    r"card details|card security|bank details|bank account|sort code|routing number|iban|"
    r"one[- ]time|verification code|security code|login code|sign-in code|otp|2fa)\b", re.IGNORECASE)


def normalize_contract(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    ai_will_not = [item for item in _str_list(raw.get("will_not_do")) if not _RULE_TOPICS.search(item)]
    return {
        "goal": _str(raw.get("goal"), 500),
        "steps": _str_list(raw.get("steps")),
        "will_not_do": SITE_RULES + ai_will_not,
        "done_when": _str(raw.get("done_when"), 1000),
        "questions": [q for q in _str_list(raw.get("questions")) if not _ASKS_FOR_SECRET.search(q)],
        "information_needed_from_you": [i for i in _str_list(raw.get("information_needed_from_you"))
                                        if not _ASKS_FOR_SECRET.search(i)],
    }


_SENSITIVE_FIELD = re.compile(
    r"\b(passwords?|passcodes?|pin|card|cvv|cvc|security code|one[- ]time|verification code|"
    r"bank account|account number|routing|sort code|iban)\b", re.IGNORECASE)


def normalize_prepared(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    where = raw.get("where_to_go") if isinstance(raw.get("where_to_go"), dict) else {}
    steps = []
    for s in _list(raw.get("steps")):
        if isinstance(s, dict) and _str(s.get("instruction")):
            steps.append({"instruction": _str(s.get("instruction")), "screen_reader_tip": _str(s.get("screen_reader_tip"))})
    answers = []
    for a in _list(raw.get("prepared_answers")):
        if not isinstance(a, dict) or not _str(a.get("field_label")):
            continue
        label = _str(a.get("field_label"), 200)
        value = a.get("suggested_answer")
        answer = (str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else "").strip()[:2000]
        note = _str(a.get("note"), 1000)
        # Never show a secret as a prepared answer, whatever the AI wrote.
        if _SENSITIVE_FIELD.search(label) or find_secret(answer):
            if not re.fullmatch(r"\[[^\]]{1,200}\]", answer):
                answer = f"[your {label.lower()}]"
                note = (note + " " if note else "") + "Type this yourself. Never share it with anyone, including this site."
        answers.append({"field_label": label, "suggested_answer": answer or f"[your {label.lower()}]", "note": note})
    return {
        "where_to_go": {"title": _str(where.get("title"), 300), "url": _web_url(where.get("url"))},
        "before_you_start": _str_list(raw.get("before_you_start")),
        "steps": steps[:60],
        "prepared_answers": answers[:60],
        "known_accessibility_problems": _str_list(raw.get("known_accessibility_problems")),
        "what_happens_after": _str(raw.get("what_happens_after"), 2000),
    }


def _clean_sources(sources: Any) -> list[dict]:
    out, seen = [], set()
    for s in sources or []:
        url = _web_url(getattr(s, "url", None) if not isinstance(s, dict) else s.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        title = getattr(s, "title", "") if not isinstance(s, dict) else s.get("title", "")
        quote = getattr(s, "quote", "") if not isinstance(s, dict) else s.get("quote", "")
        out.append({"url": url, "title": _str(title, 300) or url, "quote": _str(quote, 300)})
    return out


# ----- storage -----------------------------------------------------------------------------------

def _row(errand_id: int) -> dict | None:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM errands WHERE id = ?", (errand_id,)).fetchone()
    return dict(row) if row else None


def _owned(errand_id: int, user: dict) -> dict:
    errand = _row(errand_id)
    if errand is None or errand["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="We could not find that errand.")
    return errand


def _update(errand_id: int, **fields: Any) -> None:
    fields["updated_at"] = db.now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.session() as conn:
        conn.execute(f"UPDATE errands SET {cols} WHERE id = ?", (*fields.values(), errand_id))


def _job_row(ctx, errand_id: int, created_at: str) -> dict | None:
    """The errand a job was started for. Ids of deleted errands can be reused, so a job also checks the
    owner and the creation time, and never touches a newer errand that took the id."""
    with db.session() as conn:
        row = conn.execute("SELECT * FROM errands WHERE id = ? AND user_id = ? AND created_at = ?",
                           (errand_id, ctx.user_id, created_at)).fetchone()
    return dict(row) if row else None


def _job_update(ctx, errand_id: int, created_at: str, **fields: Any) -> bool:
    """Like _update, for a job: only the errand it was started for. False if that errand is gone."""
    fields["updated_at"] = db.now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.session() as conn:
        cur = conn.execute(f"UPDATE errands SET {cols} WHERE id = ? AND user_id = ? AND created_at = ?",
                           (*fields.values(), errand_id, ctx.user_id, created_at))
        return cur.rowcount > 0


def _create(user_id: int, kind: str, request_text: str, parent_id: int | None = None) -> int:
    now = db.now()
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO errands (user_id, kind, request, status, parent_id, created_at, updated_at)"
            " VALUES (?, ?, ?, 'new', ?, ?, ?)",
            (user_id, kind, request_text, parent_id, now, now),
        )
        return int(cur.lastrowid)


DUPLICATE_SECONDS = 120


def _recent_duplicate(user_id: int, kind: str, request_text: str, parent_id: int | None = None) -> int | None:
    """An errand with the same words sent in the last two minutes (a double press of the button), if any."""
    since = (datetime.now(timezone.utc) - timedelta(seconds=DUPLICATE_SECONDS)).replace(microsecond=0).isoformat()
    with db.session() as conn:
        row = conn.execute(
            "SELECT id FROM errands WHERE user_id = ? AND kind = ? AND request = ? AND parent_id IS ?"
            " AND status IN ('new', 'understood') AND created_at >= ? ORDER BY id DESC LIMIT 1",
            (user_id, kind, request_text, parent_id, since),
        ).fetchone()
    return int(row["id"]) if row else None


def _list_for(user_id: int) -> list[dict]:
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM errands WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
                            (user_id,)).fetchall()
    return [_view(dict(r)) for r in rows]


def _job_is_stale(job: dict) -> bool:
    """True if a job has been running (not waiting for a free worker) long after any AI call could take."""
    return jobs.is_stalled(job)


def _view(errand: dict) -> dict:
    """The errand with its JSON fields decoded and a short label for headings and lists."""
    errand = dict(errand)
    errand["contract"] = _loads(errand["contract_json"], None)
    errand["prepared"] = _loads(errand["prepared_json"], None)
    errand["sources"] = _loads(errand["sources_json"], [])
    errand["notes"] = [n for n in _list(_loads(errand["notes_json"], [])) if isinstance(n, dict)]
    if errand["status"] == "understood" and not isinstance(errand["contract"], dict):
        errand["status"] = "new"  # the read-back is missing: offer to read it back again
    if errand["status"] == "done" and errand["kind"] == "task" and not isinstance(errand["prepared"], dict):
        errand["prepared"] = normalize_prepared({})
    goal = (errand["contract"] or {}).get("goal") or ""
    errand["label"] = _short(goal or errand["request"])
    errand["kind_words"] = "Find information" if errand["kind"] == "find" else "Task on a website"
    errand["status_words"] = STATUS_WORDS.get(errand["status"], errand["status"])
    job = jobs.get(errand["job_id"]) if errand["job_id"] else None
    errand["job"] = job
    errand["job_failed"] = bool(job and job["status"] == "failed")
    errand["job_error"] = job["error"] if errand["job_failed"] else ""
    errand["job_active"] = bool(job and job["status"] in ("queued", "running"))
    if errand["job_active"] and _job_is_stale(job):
        # The server restarted or the work hung: say so and offer to try again, instead of waiting forever.
        errand["job_active"], errand["job_failed"] = False, True
        errand["job_error"] = "It may have been stopped by a restart of the site. Nothing was lost: your request is saved."
    if errand["status"] in ("new", "working") and not errand["job_active"]:
        errand["status_words"] = ("Not read back yet" if errand["status"] == "new" else "Not finished yet")
    return errand


# ----- prompts -----------------------------------------------------------------------------------

BASE_RULES = (
    "The person is blind or has low vision and uses a screen reader. Put the summary first. Use plain "
    "language, short sentences and short headings. No tables unless asked. No emoji. Never claim something "
    "was checked or tested unless it was.\n"
    "This site never logs in to any website, never pays for anything and never submits, sends or signs "
    "anything for the person. It finds, explains and prepares; the person stays in control and does the "
    "final steps themselves. Never ask for passwords, card numbers, bank details or one-time codes."
)

UNDERSTAND_SYSTEM = BASE_RULES + """

Your job now: read the person's request and say back, as an errand contract, what you understood
BEFORE doing anything. Do not do the errand yet and do not search the web.
- goal: one short sentence in plain words, starting with a verb, for example "Find the opening hours of the
  Leeds central library". At most 12 words.
- steps: the few steps you will take, in order, in plain words (for example "Search official sources",
  "Bring back the answer with links"). For a task on a website, the steps are about finding the official
  place and preparing instructions and answers; the person does the task themselves.
- will_not_do: what you will not do. Always include: log in for them, pay for anything, send or submit anything.
- done_when: one sentence that says when the errand is finished.
- questions: only questions whose answers change the result (for example which country or city). Empty if none.
- information_needed_from_you: for a task, the facts the person may want to give so you can prepare answers
  (for example their name or the date they want). Never passwords, card numbers, codes or other secrets.
  Empty for most "find" errands."""

FIND_SYSTEM = BASE_RULES + """

Your job now: find the information the person asked for on the live web and bring it back.
Write in Markdown:
1. Start with the answer in two or three plain sentences (no heading before it).
2. Then the details under short headings (## Heading).
3. End with a section "## What I could not confirm" that lists anything uncertain, out of date, missing or
   conflicting between sources. If everything was confirmed, say which sources confirmed it.
Prefer official and primary sources. Give dates for facts that change (prices, opening hours, rules).
Do not invent addresses, phone numbers, prices or dates. Links must be ones you actually found."""

TASK_RESEARCH_SYSTEM = BASE_RULES + """

Your job now: the person wants to do a task on a website themselves. Search the live web to find:
- the official website or page where this task is done (prefer the government or the organisation itself,
  not third-party agents that charge fees);
- the current official instructions: what is needed before starting, the steps, the fields asked for,
  fees, deadlines and what happens after;
- any reports from blind or low-vision people about accessibility problems on that website or process
  (for example CAPTCHAs, time limits, unlabelled buttons, PDF-only forms), and accessible alternatives the
  organisation itself offers.
Write the findings in Markdown, summary first, with short headings. End with "## What I could not confirm"."""

TASK_PREPARE_SYSTEM = BASE_RULES + """

Your job now: turn the research into a plan the person can follow by themselves.
- where_to_go: the official page to start from, with its title and full https address. Use only an address
  that appears in the research. If none was found, leave url empty.
- before_you_start: what to have ready (documents, information, time needed, fees).
- steps: numbered actions on the website, each with a screen_reader_tip: practical advice for doing that
  step with a screen reader (for example "Press H to move by heading to 'Your details'", or "The date
  field is three separate boxes: day, month, year"). Say when a tip is general advice rather than known
  from a report about this site.
- prepared_answers: suggested answers for form fields, ONLY from information the person gave in their
  request or their answers. For anything you do not know, or anything sensitive (ID numbers, passwords,
  card or bank details, codes), write a placeholder in square brackets, for example
  "[your passport number]". Never invent personal facts. Never fill in a password, card number or code.
- known_accessibility_problems: problems reported by real people or stated by the site. Empty if none
  were found. Do not guess.
- what_happens_after: what happens after the person sends the form themselves (emails, waiting times).
Remember: the site does not log in, pay or submit. The person does the task."""

CONTRACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["goal", "steps", "will_not_do", "done_when", "questions", "information_needed_from_you"],
    "properties": {
        "goal": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}},
        "will_not_do": {"type": "array", "items": {"type": "string"}},
        "done_when": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "information_needed_from_you": {"type": "array", "items": {"type": "string"}},
    },
}

PREPARE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["where_to_go", "before_you_start", "steps", "prepared_answers",
                 "known_accessibility_problems", "what_happens_after"],
    "properties": {
        "where_to_go": {
            "type": "object", "additionalProperties": False, "required": ["title", "url"],
            "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
        },
        "before_you_start": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["instruction", "screen_reader_tip"],
            "properties": {"instruction": {"type": "string"}, "screen_reader_tip": {"type": "string"}},
        }},
        "prepared_answers": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["field_label", "suggested_answer", "note"],
            "properties": {"field_label": {"type": "string"}, "suggested_answer": {"type": "string"},
                           "note": {"type": "string"}},
        }},
        "known_accessibility_problems": {"type": "array", "items": {"type": "string"}},
        "what_happens_after": {"type": "string"},
    },
}


def _profile_line(profile: dict) -> str:
    bits = []
    if profile.get("screen_reader"):
        bits.append(f"screen reader: {profile['screen_reader']}")
    if profile.get("device"):
        bits.append(f"device: {profile['device']}")
    return ("About the person (" + "; ".join(bits) + ").") if bits else "The person did not say which screen reader or device they use."


def _context(errand: dict, profile: dict, *, with_contract: bool) -> str:
    kind = "Find information and bring it back" if errand["kind"] == "find" else "Help me do a task on a website (the person does the task)"
    parts = [f"Kind of errand: {kind}.", "The person's request, in their own words:", errand["request"]]
    notes = _loads(errand["notes_json"], [])
    if notes:
        parts.append("What the person told you after earlier read-backs (corrections and answers), oldest first:")
        parts.extend(f"- {n.get('text', '')}" for n in notes)
    if errand.get("parent_id"):
        parent = _row(errand["parent_id"])
        if parent and parent["user_id"] == errand["user_id"]:
            parts.append("This is a follow-up to an earlier errand.")
            parts.append("Earlier request: " + parent["request"])
            if parent["result_md"]:
                parts.append("Earlier answer (written by the AI):\n" + parent["result_md"][:8000])
    contract = _loads(errand["contract_json"], None)
    if with_contract and contract:
        parts.append("The errand contract the person agreed to:")
        parts.append("Goal: " + contract.get("goal", ""))
        parts.extend(f"Step: {s}" for s in contract.get("steps", []))
        parts.append("Done when: " + contract.get("done_when", ""))
    parts.append(_profile_line(profile))
    return "\n".join(parts)


# ----- background jobs ---------------------------------------------------------------------------

def _understand(ctx, ai, errand_id: int, created_at: str, profile: dict) -> str:
    errand = _job_row(ctx, errand_id, created_at)
    if errand is None:
        return "/do"
    ctx.progress("Reading your request")
    raw = ai.structured(system=UNDERSTAND_SYSTEM, prompt=_context(errand, profile, with_contract=False),
                        schema=CONTRACT_SCHEMA, purpose="errands.understand")
    contract = normalize_contract(raw)
    if not contract["goal"]:
        contract["goal"] = _short(errand["request"], 120)
    if not _job_update(ctx, errand_id, created_at, contract_json=json.dumps(contract), status="understood"):
        return "/do"
    return f"/do/{errand_id}"


def _run(ctx, ai, errand_id: int, created_at: str, profile: dict) -> str:
    errand = _job_row(ctx, errand_id, created_at)
    if errand is None:
        return "/do"
    context = _context(errand, profile, with_contract=True)
    if errand["kind"] == "find":
        ctx.progress("Searching the web and reading sources")
        result = ai.research(system=FIND_SYSTEM, prompt=context, purpose="errands.find")
        text = (result.text or "").strip() if isinstance(result.text, str) else ""
        if not text:
            # Never show an empty page that says "Found". The job page explains, and the errand offers Try again.
            raise AIError("The AI searched but did not write an answer. Press Try again, or reword your request.")
        if not _job_update(ctx, errand_id, created_at, result_md=text,
                           sources_json=json.dumps(_clean_sources(result.sources)), status="done"):
            return "/do"
        return f"/do/{errand_id}"
    if errand["result_md"].strip():
        # An earlier try finished the web research but stopped while preparing: use it, do not pay for it twice.
        research_text = errand["result_md"]
        sources = _loads(errand["sources_json"], [])
    else:
        ctx.progress("Finding the official website and its instructions")
        research = ai.research(system=TASK_RESEARCH_SYSTEM, prompt=context, purpose="errands.task_research")
        research_text = (research.text or "").strip() if isinstance(research.text, str) else ""
        sources = _clean_sources(research.sources)
        if not _job_update(ctx, errand_id, created_at, result_md=research_text, sources_json=json.dumps(sources)):
            return "/do"
    ctx.progress("Preparing the steps and your answers")
    source_lines = "\n".join(f"- {s['title']}: {s['url']}" for s in sources)
    prompt = (context + "\n\nResearch notes (written by the AI from web search):\n" + (research_text or "(none)")
              + "\n\nSources found:\n" + (source_lines or "(none)"))
    raw = ai.structured(system=TASK_PREPARE_SYSTEM, prompt=prompt, schema=PREPARE_SCHEMA,
                        purpose="errands.task_prepare")
    prepared = normalize_prepared(raw)
    if not _job_update(ctx, errand_id, created_at, prepared_json=json.dumps(prepared), status="done"):
        return "/do"
    return f"/do/{errand_id}"


def _profile(user: dict) -> dict:
    return {"screen_reader": (user.get("screen_reader") or "").strip()[:100],
            "device": (user.get("device") or "").strip()[:100]}


def _start(request: Request, user: dict, errand_id: int, step: str):
    """Start the understand or run job. Returns a redirect, or None with a flash message if there is no AI."""
    try:
        ai = get_ai(request, user)
    except AINotAvailable as exc:
        flash(request, exc.user_message, "error")
        return None
    errand = _view(_row(errand_id))
    # The title names the errand, so the list in "Your recent work" says which errand each line is about.
    if step == "understand":
        title = f"Reading back your request: {_short(errand['request'], 60)}"
        job_id = jobs.submit(user["id"], "errands.understand", title, _understand, ai, errand_id,
                             errand["created_at"], _profile(user),
                             dedupe_key=f"errands.understand:{errand_id}:{errand['created_at']}",
                             item_ref=_item_ref(errand))
    else:
        title = ("Finding the information: " if errand["kind"] == "find" else "Preparing your task: ") + errand["label"]
        job_id = jobs.submit(user["id"], "errands.run", title, _run, ai, errand_id, errand["created_at"],
                             _profile(user), dedupe_key=f"errands.run:{errand_id}:{errand['created_at']}",
                             item_ref=_item_ref(errand))
    _update(errand_id, job_id=job_id)
    return redirect(jobs.job_url(job_id))


def _item_ref(errand: dict) -> str:
    """What the errand's jobs are about, so deleting the errand removes them (see jobs.forget_item)."""
    return f"errands:{errand['id']}:{errand['created_at']}"


def _add_note(errand: dict, text: str) -> None:
    notes = _loads(errand["notes_json"], [])
    notes.append({"text": text, "at": db.now()})
    _update(errand["id"], notes_json=json.dumps(notes))


# ----- pages -------------------------------------------------------------------------------------

# Errands on the index are grouped under headings, so a long list can be skipped through by heading.
GROUPS = [
    ("waiting", "Waiting for you to check what I understood"),
    ("working", "Being worked on or not finished"),
    ("done", "Done"),
]


def _group(errand: dict) -> str:
    if errand["status"] == "understood":
        return "waiting"
    return "done" if errand["status"] == "done" else "working"


def _index_page(request: Request, user: dict, *, values: dict | None = None, errors: dict | None = None,
                status_code: int = 200, title: str = "Do this for me"):
    title = title + (": please fix the problems" if errors else "")
    errands = _list_for(user["id"])
    groups = [(heading, [e for e in errands if _group(e) == key]) for key, heading in GROUPS]
    return render(request, "errands/index.html", title=title, kinds=KINDS, hint=REQUEST_HINT,
                  values=values or {"kind": "find", "request": ""}, errors=errors or {},
                  errands=errands, groups=[g for g in groups if g[1]], status_code=status_code)


@router.get("")
def index(request: Request, user: dict = Depends(require_user)):
    deleted = request.session.pop("errands_deleted", None)
    if isinstance(deleted, str) and deleted:
        # The h1 says what just happened.
        return _index_page(request, user, title=f"Errand deleted: {deleted}")
    return _index_page(request, user)


@router.get("/new")
def new_errand_get(request: Request, user: dict = Depends(require_user)):
    """Someone reloaded or bookmarked the address of the form: show the form."""
    return redirect("/do")


@router.post("/new")
async def new_errand(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    kind = _form_text(form, "kind")
    text = _form_text(form, "request")
    errors: dict[str, str] = {}
    if kind not in ("find", "task"):
        errors["kind"] = ("Choose what you want: find information and bring it back, "
                          "or help with a task on a website.")
    if not text:
        errors["request"] = ("Write what you need in the box. For example: "
                             "find the opening hours of the central library in Leeds.")
    elif len(text) > MAX_REQUEST:
        errors["request"] = f"Make your request shorter: at most {MAX_REQUEST:,} characters. It has {len(text):,}."
    else:
        secret = find_secret(text)
        if secret:
            errors["request"] = secret
    if errors:
        return _index_page(request, user, values={"kind": kind, "request": text}, errors=errors, status_code=400)
    duplicate = _recent_duplicate(user["id"], kind, text)
    if duplicate:
        # The button was pressed twice, or the same request was sent again after a failed try:
        # do not make (or pay for) the same errand twice.
        return _resume(request, user, duplicate)
    errand_id = _create(user["id"], kind, text)
    return _start(request, user, errand_id, "understand") or redirect(f"/do/{errand_id}")


def _resume(request: Request, user: dict, errand_id: int):
    """Go to an errand that was just made; if its read-back stopped, try it again."""
    errand = _view(_owned(errand_id, user))
    if errand["status"] == "new" and not errand["job_active"]:
        return _start(request, user, errand_id, "understand") or redirect(f"/do/{errand_id}")
    return redirect(f"/do/{errand_id}")


def _understood_title(errand: dict) -> str:
    return f"Here is what I understood: {errand['label']}"


def _done_title(errand: dict) -> str:
    return (f"Found: {errand['label']}" if errand["kind"] == "find" else f"Ready for you to do: {errand['label']}")


def _errand_page(request: Request, errand: dict, *, values: dict | None = None, errors: dict | None = None,
                 status_code: int = 200):
    suffix = ": please fix the problems" if errors else ""
    ctx = {"errand": errand, "values": values or {}, "errors": errors or {}, "status_code": status_code}
    if errand["status"] == "understood" and errand["contract"]:
        return render(request, "errands/understood.html", title=_understood_title(errand) + suffix, **ctx)
    if errand["status"] == "done" and errand["kind"] == "find":
        return render(request, "errands/found.html", title=_done_title(errand) + suffix,
                      answer=answer_html(errand["result_md"]), **ctx)
    if errand["status"] == "done":
        return render(request, "errands/ready.html", title=_done_title(errand) + suffix, **ctx)
    if errand["status"] == "new":
        title = ("Reading back your request" if errand["job_active"]
                 else f"Saved but not read back yet: {_short(errand['request'], 60)}")
    else:
        title = (f"Working on it: {errand['label']}" if errand["job_active"]
                 else f"Not finished yet: {errand['label']}")
    return render(request, "errands/pending.html", title=title, **ctx)


@router.get("/{errand_id}")
def show(request: Request, errand_id: int, user: dict = Depends(require_user)):
    return _errand_page(request, _view(_owned(errand_id, user)))


@router.post("/{errand_id}/respond")
async def respond(request: Request, errand_id: int, user: dict = Depends(require_user)):
    form = await checked_form(request)
    errand = _view(_owned(errand_id, user))
    if errand["status"] != "understood":
        flash(request, "This errand is not waiting for an answer from you right now.", "info")
        return redirect(f"/do/{errand_id}")
    action = _form_text(form, "action")
    text = _form_text(form, "answer")
    error = ""
    if action not in ("update", "go"):
        error = "Press Update to have me read it back again, or Go ahead to start."
    elif action == "update" and not text:
        error = "Write your corrections or answers, then press Update. If everything is right, press Go ahead."
    elif len(text) > MAX_NOTE:
        error = f"Make this shorter: at most {MAX_NOTE:,} characters. It has {len(text):,}."
    elif text and len(errand["notes"]) >= MAX_NOTES:
        error = (f"This errand already has {MAX_NOTES} corrections, which is the most it can hold. "
                 "Empty this box and press Go ahead, or start a new errand with everything in one request.")
    elif text:
        error = find_secret(text) or ""
    if error:
        return _errand_page(request, errand, values={"answer": text}, errors={"answer": error}, status_code=400)
    try:
        get_ai(request, user)
    except AINotAvailable as exc:
        flash(request, exc.user_message, "error")
        return redirect(f"/do/{errand_id}")
    if text:
        _add_note(errand, text)
    if action == "update":
        _update(errand_id, status="new")
        return _start(request, user, errand_id, "understand") or redirect(f"/do/{errand_id}")
    _update(errand_id, status="working")
    return _start(request, user, errand_id, "run") or redirect(f"/do/{errand_id}")


@router.post("/{errand_id}/retry")
async def retry(request: Request, errand_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    errand = _view(_owned(errand_id, user))
    if errand["job_active"]:
        return redirect(jobs.job_url(errand["job_id"]))
    if errand["status"] == "new":
        return _start(request, user, errand_id, "understand") or redirect(f"/do/{errand_id}")
    if errand["status"] == "working":
        return _start(request, user, errand_id, "run") or redirect(f"/do/{errand_id}")
    return redirect(f"/do/{errand_id}")


@router.post("/{errand_id}/follow-up")
async def follow_up(request: Request, errand_id: int, user: dict = Depends(require_user)):
    form = await checked_form(request)
    errand = _view(_owned(errand_id, user))
    if errand["status"] != "done" or errand["kind"] != "find":
        flash(request, "You can ask a follow-up question once an answer has been found.", "info")
        return redirect(f"/do/{errand_id}")
    text = _form_text(form, "question")
    error = ""
    if not text:
        error = "Write your follow-up question in the box, then press Ask the follow-up."
    elif len(text) > MAX_REQUEST:
        error = f"Make your question shorter: at most {MAX_REQUEST:,} characters. It has {len(text):,}."
    else:
        error = find_secret(text) or ""
    if error:
        return _errand_page(request, errand, values={"question": text}, errors={"question": error}, status_code=400)
    duplicate = _recent_duplicate(user["id"], "find", text, parent_id=errand_id)
    if duplicate:
        return _resume(request, user, duplicate)
    new_id = _create(user["id"], "find", text, parent_id=errand_id)
    return _start(request, user, new_id, "understand") or redirect(f"/do/{new_id}")


@router.get("/{errand_id}/delete")
def confirm_delete(request: Request, errand_id: int, user: dict = Depends(require_user)):
    errand = _view(_owned(errand_id, user))
    return render(request, "errands/delete.html", title=f"Delete this errand: {errand['label']}", errand=errand)


@router.post("/{errand_id}/delete")
async def delete(request: Request, errand_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    errand = _owned(errand_id, user)
    with db.session() as conn:
        conn.execute("DELETE FROM errands WHERE id = ? AND user_id = ?", (errand["id"], user["id"]))
        jobs.forget_item(conn, user["id"], _item_ref(errand))
    request.session["errands_deleted"] = _view(errand)["label"]
    return redirect("/do")


@router.get("/{errand_id}/respond")
@router.get("/{errand_id}/retry")
@router.get("/{errand_id}/follow-up")
def form_address_get(request: Request, errand_id: int, user: dict = Depends(require_user)):
    """Someone reloaded or bookmarked the address a form was sent to: take them to the errand."""
    _owned(errand_id, user)
    return redirect(f"/do/{errand_id}")


def forget_user(user_id: int) -> None:
    """Called when a member deletes their account: remove their private errands."""
    with db.session() as conn:
        conn.execute("DELETE FROM errands WHERE user_id = ?", (user_id,))
