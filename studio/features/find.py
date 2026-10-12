"""Find before you build: a directory of tools that already exist, member reports on whether they
worked, and an AI web search for anything the directory does not have yet.

The tables here are shared with the Tool library (studio/features/library.py)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import db, jobs
from ..ai import AINotAvailable
from ..web import checked_form, current_user, flash, get_ai, redirect, render, require_user

router = APIRouter(prefix="/find")
NAV = {"label": "Find before you build", "url": "/find",
       "description": "Search for tools that already exist, with reports from blind people about whether they worked for them."}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tools (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'Other',
    made_by_blind INTEGER NOT NULL DEFAULT 0,
    maintainer TEXT NOT NULL DEFAULT '',
    license TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL CHECK (source IN ('research', 'member')),
    added_by INTEGER REFERENCES users(id),
    needs_upkeep INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (url)
);
CREATE INDEX IF NOT EXISTS idx_tools_blind ON tools(made_by_blind);
CREATE TABLE IF NOT EXISTS tool_reports (
    id INTEGER PRIMARY KEY,
    tool_id INTEGER NOT NULL REFERENCES tools(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    outcome TEXT NOT NULL CHECK (outcome IN ('worked', 'partly', 'did_not_work')),
    screen_reader TEXT NOT NULL DEFAULT '',
    device TEXT NOT NULL DEFAULT '',
    sight_onset TEXT NOT NULL DEFAULT 'prefer_not',
    language TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_reports_tool ON tool_reports(tool_id);
CREATE TABLE IF NOT EXISTS upkeep_requests (
    id INTEGER PRIMARY KEY,
    tool_id INTEGER NOT NULL REFERENCES tools(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    problem TEXT NOT NULL,
    after_update TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_upkeep_requests_tool ON upkeep_requests(tool_id);
CREATE TABLE IF NOT EXISTS upkeep_volunteers (
    id INTEGER PRIMARY KEY,
    tool_id INTEGER NOT NULL REFERENCES tools(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    skills TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (tool_id, user_id)
);
CREATE TABLE IF NOT EXISTS find_searches (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    need TEXT NOT NULL,
    result_md TEXT NOT NULL DEFAULT '',
    candidates_json TEXT NOT NULL DEFAULT '[]',
    sources_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_find_searches_user ON find_searches(user_id);
"""

SEED_FILE = Path(__file__).resolve().parent.parent / "data" / "seed_tools.json"

CATEGORIES = ["AI coding", "Screen reader add-on", "Seeing and describing", "Writing and publishing",
              "Data and charts", "Audio and media", "Games", "Everyday tasks", "Hardware", "Other"]
OUTCOMES = [("worked", "It worked for me"), ("partly", "It partly worked"), ("did_not_work", "It did not work for me")]
OUTCOME_WORDS = {"worked": "worked", "partly": "partly worked", "did_not_work": "did not work"}
SIGHT_ONSETS = [("prefer_not", "Prefer not to say"), ("birth", "Blind or low vision since birth or early childhood"),
                ("later", "Lost sight later in life")]
SIGHT_WORDS = {"birth": "since birth or early childhood", "later": "lost sight later in life"}
CATEGORY_PAIRS = [(c, c) for c in CATEGORIES]
PAGE_SIZE = 25
NEED_MAX = 500

# ----- small helpers shared with the Tool library ---------------------------------------------

_DIGIT_RUN_RE = re.compile(r"\d+(?:[ -]\d+)*")
_PASSWORD_RE = re.compile(r"\b(password|passwd|passphrase)\b\s*(is|:|=)", re.I)
_CODE_RE = re.compile(r"\b(passcode|pin|one[- ]time code|otp|security code|verification code|cvv|cvc)\b"
                      r"\s*(?:is|:|=)?\s*\d{3,}", re.I)
SECRET_MESSAGE = ("This looks like a card number, password or one-time code. Please remove it. "
                  "Never share these on this site.")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _looks_like_card(text: str) -> bool:
    """13 to 19 digits that pass the check every card number passes, written together or in groups of
    three or more (only the last group may be shorter). Dates, version numbers, phone numbers and numbered
    steps are not refused."""
    for run in _DIGIT_RUN_RE.finditer(text):
        groups = re.split(r"[ -]", run.group())
        for first in range(len(groups)):
            if len(groups[first]) < 3:
                continue
            digits = ""
            for group in groups[first:]:
                digits += group
                if len(digits) > 19:
                    break
                if len(digits) >= 13 and _luhn(digits):
                    return True
                if len(group) < 3:
                    break
    return False


def looks_secret(text: str) -> bool:
    """True if the text seems to contain a card number, password or one-time code."""
    text = text or ""
    return bool(_looks_like_card(text) or _PASSWORD_RE.search(text) or _CODE_RE.search(text))


def _split(url: str):
    """urlsplit, or None for an address it cannot read (for example a broken IPv6 host like http://[x)."""
    try:
        parts = urlsplit(url or "")
        parts.port  # noqa: B018 - reading the port checks it is a number
    except ValueError:
        return None
    return parts


def valid_url(url: str) -> bool:
    if not url or len(url) > 500 or any(c.isspace() for c in url):
        return False
    parts = _split(url)
    return parts is not None and parts.scheme in ("http", "https") and bool(parts.hostname)


def norm_url(url: str) -> str:
    """A comparison key for a URL: http and https, www and trailing slashes count as the same."""
    parts = _split((url or "").strip())
    if parts is None:
        return (url or "").strip().lower()
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    key = host + parts.path.rstrip("/")
    return f"{key}?{parts.query}" if parts.query else key


def find_by_url(url: str) -> dict | None:
    """The directory entry with this URL (compared loosely), or None."""
    key = norm_url(url)
    with db.session() as conn:
        rows = conn.execute("SELECT id, url FROM tools").fetchall()
    for row in rows:
        if norm_url(row["url"]) == key:
            return get_tool(row["id"])
    return None


def get_tool(tool_id: int) -> dict | None:
    with db.session() as conn:
        row = conn.execute(
            "SELECT t.*, u.pseudonym AS added_by_name FROM tools t LEFT JOIN users u ON u.id = t.added_by WHERE t.id = ?",
            (tool_id,)).fetchone()
    return dict(row) if row else None


def tool_or_404(tool_id: int) -> dict:
    tool = get_tool(tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="We could not find that tool. It may have a different address.")
    return tool


def clean(form: dict, name: str, limit: int) -> str:
    value = form.get(name, "")
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value).strip()[:limit]


# Values are kept whole up to this length so a form shown again with an error keeps what the person
# wrote; the real limit for each field is checked with too_long() and explained in the error.
KEEP = 20000


def too_long(values: dict, name: str, limit: int) -> bool:
    return len(values.get(name) or "") > limit


def long_message(limit: int, what: str = "it") -> str:
    return f"Keep {what} under {limit:,} characters."


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def report_summaries(tool_ids: list[int]) -> dict[int, str]:
    """One plain line per tool, from each member's latest report, for example
    "3 members: 2 worked, 1 partly worked. Screen readers: NVDA, TalkBack". No full stop at the end."""
    out = {tid: "No member has reported on this yet" for tid in tool_ids}
    if not tool_ids:
        return out
    marks = ",".join("?" * len(tool_ids))
    with db.session() as conn:
        rows = conn.execute(
            f"SELECT r.tool_id, r.outcome, r.screen_reader FROM tool_reports r WHERE r.tool_id IN ({marks})"
            " AND r.id = (SELECT MAX(r2.id) FROM tool_reports r2 WHERE r2.tool_id = r.tool_id AND r2.user_id = r.user_id)",
            tool_ids).fetchall()
    grouped: dict[int, list] = {}
    for row in rows:
        grouped.setdefault(row["tool_id"], []).append(row)
    for tid, items in grouped.items():
        counts = [(OUTCOME_WORDS[key], sum(1 for r in items if r["outcome"] == key)) for key, _ in OUTCOMES]
        parts = ", ".join(f"{n} {word}" for word, n in counts if n)
        readers: list[str] = []
        for r in items:
            name = r["screen_reader"].strip()
            if name and name.lower() not in {x.lower() for x in readers}:
                readers.append(name)
        line = f"{_plural(len(items), 'member')}: {parts}"
        if readers:
            line += ". Screen readers: " + ", ".join(sorted(readers, key=str.lower))
        out[tid] = line
    return out


def reports_for(tool_id: int) -> list[dict]:
    with db.session() as conn:
        rows = conn.execute(
            "SELECT r.*, u.pseudonym FROM tool_reports r JOIN users u ON u.id = r.user_id"
            " WHERE r.tool_id = ? ORDER BY r.id DESC", (tool_id,)).fetchall()
    out = []
    for row in rows:
        r = dict(row)
        r["outcome_text"] = dict(OUTCOMES)[r["outcome"]]
        r["sight_text"] = SIGHT_WORDS.get(r["sight_onset"], "")
        r["date"] = r["created_at"][:10]
        out.append(r)
    return out


def report_defaults(user: dict | None) -> dict:
    if not user:
        return {}
    onset = user.get("sight_onset") or "prefer_not"
    return {"screen_reader": user.get("screen_reader") or "", "device": user.get("device") or "",
            "sight_onset": onset if onset in dict(SIGHT_ONSETS) else "prefer_not", "language": ""}


REPORT_LIMITS = {"screen_reader": 100, "device": 100, "language": 60, "note": 3000}


def validate_report(form: dict) -> tuple[dict, dict]:
    values = {
        "outcome": clean(form, "outcome", 20),
        "screen_reader": clean(form, "screen_reader", KEEP),
        "device": clean(form, "device", KEEP),
        "sight_onset": clean(form, "sight_onset", 20) or "prefer_not",
        "language": clean(form, "language", KEEP),
        "note": clean(form, "note", KEEP),
    }
    errors: dict[str, str] = {}
    if values["outcome"] not in dict(OUTCOMES):
        errors["outcome"] = "Choose whether the tool worked for you."
    if values["sight_onset"] not in dict(SIGHT_ONSETS):
        errors["sight_onset"] = "Choose one of the options for when you lost sight, or Prefer not to say."
    for key, limit in REPORT_LIMITS.items():
        if too_long(values, key, limit):
            errors[key] = long_message(limit, "your note" if key == "note" else "it")
    for key in ("note", "screen_reader", "device", "language"):
        if key not in errors and looks_secret(values[key]):
            errors[key] = SECRET_MESSAGE
    return values, errors


REPORT_FIELDS = ("outcome", "screen_reader", "device", "sight_onset", "language", "note")


def save_report(tool_id: int, user_id: int, values: dict) -> bool:
    """Save a report. Returns False, saving nothing, if it repeats this member's latest report word for
    word (a form sent twice)."""
    with db.session() as conn:
        latest = conn.execute(
            "SELECT outcome, screen_reader, device, sight_onset, language, note FROM tool_reports"
            " WHERE tool_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1", (tool_id, user_id)).fetchone()
        if latest is not None and all(latest[k] == values[k] for k in REPORT_FIELDS):
            return False
        conn.execute(
            "INSERT INTO tool_reports (tool_id, user_id, outcome, screen_reader, device, sight_onset, language, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tool_id, user_id, values["outcome"], values["screen_reader"], values["device"], values["sight_onset"],
             values["language"], values["note"], db.now()))
    return True


URL_MAX = 500


def validate_tool(form: dict, *, require_blind: bool = False) -> tuple[dict, dict]:
    values = {
        "name": clean(form, "name", KEEP),
        "url": clean(form, "url", KEEP),
        "description": clean(form, "description", KEEP),
        "category": clean(form, "category", 40),
        "maintainer": clean(form, "maintainer", KEEP),
        "made_by_blind": form.get("made_by_blind") == "yes",
    }
    errors: dict[str, str] = {}
    if not values["name"]:
        errors["name"] = "Enter the tool's name."
    elif too_long(values, "name", 120):
        errors["name"] = long_message(120, "the name")
    if not values["url"]:
        errors["url"] = "Enter the tool's web address, starting with https://."
    elif too_long(values, "url", URL_MAX):
        errors["url"] = f"This web address is too long. Use the tool's main page, under {URL_MAX} characters."
    elif not valid_url(values["url"]):
        errors["url"] = "Enter a full web address that starts with http:// or https://, like https://example.com."
    if not values["description"]:
        errors["description"] = "Say in one sentence what the tool does."
    elif too_long(values, "description", 600):
        errors["description"] = "Keep the description under 600 characters. One sentence is best."
    if values["category"] not in CATEGORIES:
        errors["category"] = "Choose a category."
    if too_long(values, "maintainer", 120):
        errors["maintainer"] = long_message(120)
    if require_blind and not values["made_by_blind"]:
        errors["made_by_blind"] = ("Tick the box to confirm a blind or low-vision person made this tool. "
                                   "Other tools belong in Find before you build.")
    for key in ("name", "description", "maintainer"):
        if key not in errors and looks_secret(values[key]):
            errors[key] = SECRET_MESSAGE
    return values, errors


def insert_member_tool(values: dict, user_id: int) -> int:
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO tools (name, url, description, category, made_by_blind, maintainer, source, added_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'member', ?, ?)",
            (values["name"], values["url"], values["description"], values["category"],
             1 if values["made_by_blind"] else 0, values["maintainer"], user_id, db.now()))
        return int(cur.lastrowid)


# ----- seed ------------------------------------------------------------------------------------

def seed() -> None:
    """Add the researched tools that are not in the directory yet (matched by URL), and bring the facts of
    researched entries already there up to date with the file, so a corrected fact reaches a running site.
    Entries members added are never changed. Safe to run on every start."""
    items = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    with db.session() as conn:
        for item in items:
            if not valid_url(item["url"]):
                continue
            conn.execute(
                "INSERT INTO tools (name, url, description, category, made_by_blind, maintainer, license,"
                " source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'research', ?)"
                " ON CONFLICT (url) DO UPDATE SET name = excluded.name, description = excluded.description,"
                " category = excluded.category, made_by_blind = excluded.made_by_blind,"
                " maintainer = excluded.maintainer, license = excluded.license WHERE tools.source = 'research'",
                (item["name"], item["url"], item["description"],
                 item["category"] if item["category"] in CATEGORIES else "Other",
                 1 if item.get("made_by_blind") else 0, item.get("maintainer", ""), item.get("license", ""), db.now()))


# ----- directory pages -------------------------------------------------------------------------

def _search_tools(q: str, category: str) -> list[dict]:
    """Tools whose name or description holds every word, in any letter case and any alphabet."""
    sql, args = "SELECT * FROM tools", []
    if category:
        sql += " WHERE category = ?"
        args.append(category)
    with db.session() as conn:
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY name COLLATE NOCASE", args).fetchall()]
    words = [w.casefold() for w in q.split()[:8]]
    if words:
        rows = [r for r in rows if all(w in (r["name"] + " " + r["description"]).casefold() for w in words)]
    rows.sort(key=lambda r: r["name"].casefold())
    return rows


def _page_url(q: str, category: str, page: int) -> str:
    params = {k: v for k, v in (("q", q), ("category", category)) if v}
    if page > 1:
        params["page"] = str(page)
    return "/find" + ("?" + urlencode(params) if params else "")


def _index_title(q: str, category: str, total: int, page: int, pages: int) -> str:
    """The h1 says what the page shows, so after a search it says what was found."""
    if q or category:
        title = f"{total} {'tool matches' if total == 1 else 'tools match'}"
        if q:
            title += f' "{q}"'
        if category:
            title += f" in the category {category}"
    else:
        title = NAV["label"]
    if pages > 1:
        title += f": page {page} of {pages}"
    return title


@router.get("")
def index(request: Request, q: str = "", category: str = "", page: int = 1):
    q = " ".join(q.split())[:200]
    category = category if category in CATEGORIES else ""
    found = _search_tools(q, category)
    pages = max(1, (len(found) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 1), pages)
    shown = found[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    summaries = report_summaries([t["id"] for t in shown])
    user = current_user(request)
    searches = []
    if user:
        with db.session() as conn:
            searches = [dict(r) for r in conn.execute(
                "SELECT id, need, created_at FROM find_searches WHERE user_id = ? ORDER BY id DESC LIMIT 10",
                (user["id"],)).fetchall()]
    return render(request, "find/index.html", title=_index_title(q, category, len(found), page, pages), q=q,
                  category=category, tools=shown, total=len(found), summaries=summaries, page=page, pages=pages,
                  prev_url=_page_url(q, category, page - 1) if page > 1 else "",
                  next_url=_page_url(q, category, page + 1) if page < pages else "",
                  category_pairs=CATEGORY_PAIRS, searches=searches, searching=bool(q or category))


@router.get("/tool/{tool_id}")
def tool_page(request: Request, tool_id: int):
    tool = tool_or_404(tool_id)
    return _render_tool(request, tool, values=report_defaults(current_user(request)), errors={})


def _render_tool(request: Request, tool: dict, *, values: dict, errors: dict, status_code: int = 200):
    title = tool["name"] + (": please fix the problems" if errors else "")
    return render(request, "find/tool.html", title=title, tool=tool, reports=reports_for(tool["id"]),
                  summary=report_summaries([tool["id"]])[tool["id"]], values=values, errors=errors,
                  outcomes=OUTCOMES, sight_onsets=SIGHT_ONSETS, status_code=status_code)


@router.get("/tool/{tool_id}/report")
def report_get(tool_id: int):
    """The form posts here. Someone whose session ran out signs in and lands here; send them to the form."""
    tool_or_404(tool_id)
    return redirect(f"/find/tool/{tool_id}#report-form-title")


@router.post("/tool/{tool_id}/report")
async def post_report(request: Request, tool_id: int, user: dict = Depends(require_user)):
    form = await checked_form(request)
    tool = tool_or_404(tool_id)
    values, errors = validate_report(form)
    back = "library" if form.get("back") == "library" and tool["made_by_blind"] else "find"
    if errors:
        if back == "library":
            from .library import render_library_tool  # same section, second module
            return render_library_tool(request, tool, report_values=values, report_errors=errors, status_code=400)
        return _render_tool(request, tool, values=values, errors=errors, status_code=400)
    if save_report(tool_id, user["id"], values):
        flash(request, "Thank you. Your report is saved and other members can read it.", "success")
    else:
        flash(request, "This report was already saved. It is listed once.", "info")
    return redirect(f"/{back}/tool/{tool_id}#reports-title")


@router.get("/add")
def add_form(request: Request, user: dict = Depends(require_user)):
    return render(request, "find/add.html", title="Add a tool to the directory", values={"category": "Other"},
                  errors={}, category_pairs=CATEGORY_PAIRS)


@router.post("/add")
async def add_tool(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    values, errors = validate_tool(form)
    if "url" not in errors:
        existing = find_by_url(values["url"])
        if existing:
            flash(request, f"{existing['name']} is already in the directory with this address. Here is its entry.", "info")
            return redirect(f"/find/tool/{existing['id']}")
    if errors:
        return render(request, "find/add.html", title="Add a tool to the directory: please fix the problems",
                      values=values, errors=errors, category_pairs=CATEGORY_PAIRS, status_code=400)
    tool_id = insert_member_tool(values, user["id"])
    flash(request, f"{values['name']} is now in the directory. Thank you.", "success")
    return redirect(f"/find/tool/{tool_id}")


# ----- AI web search ---------------------------------------------------------------------------

BASE_RULES = ("The person is blind or has low vision and uses a screen reader. Put the summary first. "
              "Use plain language and short headings. No tables. No emoji. "
              "Never claim something was checked or tested unless a source says who tested it and how.")

RESEARCH_SYSTEM = (
    "You help a blind or low-vision person find tools, apps, browser extensions, screen reader add-ons, "
    "open-source projects and services that already exist, so they do not build something that is already there. "
    + BASE_RULES +
    " Search the web. For each tool give its name, its own web address, what it does, what blind users or the maker "
    "say about screen reader use (and who said it), and what it costs. Say plainly when you found nothing about "
    "accessibility. Prefer tools that are still maintained.")

EXTRACT_SYSTEM = (
    "You turn research notes about existing tools into a short list. " + BASE_RULES +
    " Only include tools that the notes name together with a web address; never invent an address. "
    "what_it_does: one plain sentence. accessibility_notes: what the notes say about using it with a screen reader, "
    "and who said it; if the notes say nothing, write 'Nothing found about screen reader use.' "
    "cost: what the notes say about price, or 'Not found.' summary: two to four short sentences that answer whether "
    "something already exists for the need, written in plain Markdown without headings.")

EXTRACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "candidates"],
    "properties": {
        "summary": {"type": "string"},
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "url", "what_it_does", "accessibility_notes", "cost"],
                "properties": {
                    "name": {"type": "string"},
                    "url": {"type": "string"},
                    "what_it_does": {"type": "string"},
                    "accessibility_notes": {"type": "string"},
                    "cost": {"type": "string"},
                },
            },
        },
    },
}


def _ask_page(request: Request, *, need: str, errors: dict, status_code: int = 200):
    title = "Ask the AI what already exists" + (": please fix the problems" if errors else "")
    return render(request, "find/ask.html", title=title, need=need, errors=errors, status_code=status_code)


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "..."


def _job_title(need: str) -> str:
    return "Searching the web for: " + _short(need, 150)


def _seconds_ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


@router.get("/ask")
def ask_form(request: Request, need: str = ""):
    return _ask_page(request, need=need.strip()[:NEED_MAX], errors={})


@router.post("/ask")
async def ask(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    need = clean(form, "need", 5000)
    errors: dict[str, str] = {}
    if len(need) < 5:
        errors["need"] = "Say in a few words what you want to do or make, for example: read restaurant menus aloud."
    elif len(need) > NEED_MAX:
        errors["need"] = f"Keep it under {NEED_MAX} characters. A sentence or two is enough."
    elif looks_secret(need):
        errors["need"] = SECRET_MESSAGE
    if errors:
        return _ask_page(request, need=need, errors=errors, status_code=400)
    # A form sent twice (a double press, or Enter pressed again while waiting) must not pay for two searches.
    with db.session() as conn:
        running = conn.execute(
            "SELECT id FROM jobs WHERE user_id = ? AND kind = 'find.search' AND title = ? AND status IN ('queued', 'running')"
            " ORDER BY created_at DESC LIMIT 1", (user["id"], _job_title(need))).fetchone()
        recent = conn.execute(
            "SELECT id FROM find_searches WHERE user_id = ? AND need = ? AND created_at >= ? ORDER BY id DESC LIMIT 1",
            (user["id"], need, _seconds_ago(120))).fetchone()
    if running is not None:
        flash(request, "This search is already running. It was not started twice.", "info")
        return redirect(jobs.job_url(running["id"]))
    if recent is not None:
        flash(request, "You asked this a moment ago, so here is that result. It was not searched again.", "info")
        return redirect(f"/find/search/{recent['id']}")
    try:
        ai = get_ai(request, user)
    except AINotAvailable as exc:
        flash(request, exc.user_message, "error")
        return redirect("/find/ask?" + urlencode({"need": need}))
    profile = {"screen_reader": user.get("screen_reader") or "", "device": user.get("device") or ""}
    job_id = jobs.submit(user["id"], "find.search", _job_title(need), _search_job, ai, user["id"], need, profile)
    return redirect(jobs.job_url(job_id))


def _text(value, limit: int) -> str:
    """A string from the AI's answer, or "" if the field is missing or not text."""
    return value.strip()[:limit] if isinstance(value, str) else ""


def _search_job(ctx, ai, user_id: int, need: str, profile: dict) -> str:
    ctx.progress("Searching the web for tools that already exist")
    uses = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in profile.items() if v) or "not given"
    research = ai.research(
        system=RESEARCH_SYSTEM,
        prompt=(f"What already exists for this need?\n\nNeed: {need}\n\nWhat the person uses: {uses}\n\n"
                "Find up to 8 existing tools. Put the ones blind people say work with screen readers first."),
        purpose="find.search")
    sources = []
    for s in research.sources or []:
        url = _text(getattr(s, "url", ""), 600)
        if valid_url(url):
            sources.append({"url": url, "title": _text(getattr(s, "title", ""), 300) or url,
                            "quote": _text(getattr(s, "quote", ""), 300)})
    ctx.progress("Picking out the tools the search found")
    source_lines = "\n".join(f"- {s['title']}: {s['url']}" for s in sources)
    data = ai.structured(
        system=EXTRACT_SYSTEM,
        prompt=(f"The need: {need}\n\nResearch notes:\n{research.text or ''}\n\n"
                f"Web pages used:\n{source_lines or '- none'}"),
        schema=EXTRACT_SCHEMA, purpose="find.extract")
    if not isinstance(data, dict):
        data = {}
    raw = data.get("candidates")
    candidates = []
    seen: set[str] = set()
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        url = _text(c.get("url"), 600)
        name = _text(c.get("name"), 120)
        if not name or not valid_url(url) or norm_url(url) in seen:
            continue
        seen.add(norm_url(url))
        candidates.append({"name": name, "url": url,
                           "what_it_does": _text(c.get("what_it_does"), 600),
                           "accessibility_notes": _text(c.get("accessibility_notes"), 1200),
                           "cost": _text(c.get("cost"), 300)})
        if len(candidates) >= 12:
            break
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO find_searches (user_id, need, result_md, candidates_json, sources_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, need, _text(data.get("summary"), 4000), json.dumps(candidates), json.dumps(sources), db.now()))
        search_id = cur.lastrowid
    return f"/find/search/{search_id}"


def _owned_search(search_id: int, user: dict) -> dict:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM find_searches WHERE id = ? AND user_id = ?", (search_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="We could not find that search.")
    search = dict(row)
    search["candidates"] = json.loads(search["candidates_json"] or "[]")
    search["sources"] = json.loads(search["sources_json"] or "[]")
    return search


def _host(url: str) -> str:
    parts = _split(url)
    host = parts.netloc.lower() if parts else ""
    return host[4:] if host.startswith("www.") else host


def _match_directory(candidates: list[dict]) -> None:
    """Mark each candidate that is already in the directory: the same address, or the same name on the same
    website. A name alone is not enough: two different tools can share a name."""
    with db.session() as conn:
        rows = [dict(r) for r in conn.execute("SELECT id, name, url FROM tools").fetchall()]
    by_url = {norm_url(r["url"]): r["id"] for r in rows}
    by_name = {(r["name"].strip().casefold(), _host(r["url"])): r["id"] for r in rows}
    for c in candidates:
        c["tool_id"] = by_url.get(norm_url(c["url"])) or by_name.get((c["name"].strip().casefold(), _host(c["url"])))
    ids = [c["tool_id"] for c in candidates if c["tool_id"]]
    summaries = report_summaries(ids)
    for c in candidates:
        c["summary"] = summaries.get(c["tool_id"], "")
        c["has_reports"] = bool(c["tool_id"]) and not c["summary"].startswith("No member")


@router.get("/search/{search_id}")
def search_result(request: Request, search_id: int, user: dict = Depends(require_user)):
    search = _owned_search(search_id, user)
    _match_directory(search["candidates"])
    return render(request, "find/search.html", title=f"What already exists for: {' '.join(search['need'].split())}",
                  search=search)


@router.get("/search/{search_id}/add")
def add_candidate_get(search_id: int, user: dict = Depends(require_user)):
    """The add buttons post here. Someone whose session ran out signs in and lands here; show the search."""
    _owned_search(search_id, user)
    return redirect(f"/find/search/{search_id}")


@router.post("/search/{search_id}/add")
async def add_candidate(request: Request, search_id: int, user: dict = Depends(require_user)):
    form = await checked_form(request)
    search = _owned_search(search_id, user)
    try:
        index_ = int(str(form.get("candidate", "")))
        if index_ < 0:
            raise IndexError(index_)
        candidate = search["candidates"][index_]
    except (ValueError, IndexError):
        raise HTTPException(status_code=404, detail="We could not find that tool in your search.")
    existing = find_by_url(candidate["url"])
    if existing:
        flash(request, f"{existing['name']} is already in the directory. Here is its entry.", "info")
        return redirect(f"/find/tool/{existing['id']}")
    description = (candidate["what_it_does"] or "No description yet.").rstrip()
    values = {"name": candidate["name"], "url": candidate["url"],
              "description": f"{description} (Description written by the AI during a web search; not checked.)",
              "category": "Other", "maintainer": "", "made_by_blind": False}
    tool_id = insert_member_tool(values, user["id"])
    flash(request, f"{candidate['name']} is now in the directory. Tell others whether it works for you.", "success")
    return redirect(f"/find/tool/{tool_id}")


def forget_user(user_id: int) -> None:
    """Called when a member deletes their account: remove their private find searches, and take them off
    the lists of members helping keep library tools working, since they can no longer help."""
    with db.session() as conn:
        conn.execute("DELETE FROM find_searches WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM upkeep_volunteers WHERE user_id = ?", (user_id,))
