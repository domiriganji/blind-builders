"""AI tools report: a public, member-reported record of which AI building tools work with screen readers,
version by version.

Each tool has a short research note (what public sources said as of October 2026, with links) and member
reports. A report records the tool version, the screen reader and operating system, six plain checks
(yes, no or not tested) and an overall verdict. Anyone can read; members sign in to post. A member has
one report per tool version: posting again for the same version updates it. Members can delete their own
reports. Nothing here needs the AI, so it works for everyone, with or without credits or an API key.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import db
from ..web import checked_form, current_user, flash, redirect, render, require_user

router = APIRouter(prefix="/ai-tools")
NAV = {"label": "AI tools report", "url": "/ai-tools",
       "description": "Which AI coding tools work with screen readers right now, reported by members after each update."}

SEED_FILE = Path(__file__).resolve().parent.parent / "data" / "seed_ai_tools.json"
RESEARCH_AS_OF = "October 2026"
SUMMARY_DAYS = 90

KINDS = ("terminal", "editor", "desktop app", "website")
ANSWERS = (("yes", "Yes"), ("no", "No"), ("not_tested", "I did not test this"))
ANSWER_WORDS = {"yes": "yes", "no": "no", "not_tested": "not tested"}
OVERALL = (("usable", "Usable"), ("usable_with_workarounds", "Usable with workarounds"), ("not_usable", "Not usable"))
OVERALL_WORDS = dict(OVERALL)

# The six checks: (column, question for the form, plain statement shown on reports, hint).
CHECKS = (
    ("turn_end", "Can you tell when the AI has finished?", "You can tell when it has finished",
     "For example a sound, a spoken message, or the prompt coming back."),
    ("output_readable", "Can you read the answer at your own pace?",
     "You can read the answer at your own pace without it jumping",
     "The answer stays still while you read it with your screen reader or braille display. Nothing jumps, repeats or disappears."),
    ("changes_reviewable", "Can you review what it changed?", "You can review what it changed",
     "For example, the changed lines or files are read to you before or after you approve them."),
    ("menus_spoken", "Are menus and choices announced?", "Menus and choices are announced",
     "When a list of options appears, your screen reader says the options and which one is selected."),
    ("approvals_readable", "Are permission questions read in full?", "Permission questions are read in full",
     "When the tool asks to run a command or change a file, you can hear the whole question before you answer."),
    ("typing_echoed", "Is what you type spoken?", "What you type is spoken",
     "Your screen reader echoes the letters or words you type into the tool."),
)
CHECK_KEYS = tuple(c[0] for c in CHECKS)

# Versions are short so they fit in a heading on a 320 pixel wide screen.
TEXT_LIMITS = {"tool_version": 30, "screen_reader": 80, "screen_reader_version": 30, "os": 80}
LONG_LIMIT = 4000

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_tools (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    vendor TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK (kind IN ('terminal', 'editor', 'desktop app', 'website')),
    url TEXT NOT NULL DEFAULT '',
    research_note TEXT NOT NULL DEFAULT '',
    research_sources_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS ai_tool_reports (
    id INTEGER PRIMARY KEY,
    tool_id INTEGER NOT NULL REFERENCES ai_tools(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    tool_version TEXT NOT NULL,
    screen_reader TEXT NOT NULL,
    screen_reader_version TEXT NOT NULL DEFAULT '',
    os TEXT NOT NULL,
    turn_end TEXT NOT NULL CHECK (turn_end IN ('yes', 'no', 'not_tested')),
    output_readable TEXT NOT NULL CHECK (output_readable IN ('yes', 'no', 'not_tested')),
    changes_reviewable TEXT NOT NULL CHECK (changes_reviewable IN ('yes', 'no', 'not_tested')),
    menus_spoken TEXT NOT NULL CHECK (menus_spoken IN ('yes', 'no', 'not_tested')),
    approvals_readable TEXT NOT NULL CHECK (approvals_readable IN ('yes', 'no', 'not_tested')),
    typing_echoed TEXT NOT NULL CHECK (typing_echoed IN ('yes', 'no', 'not_tested')),
    overall TEXT NOT NULL CHECK (overall IN ('usable', 'usable_with_workarounds', 'not_usable')),
    workarounds TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_tool_reports_tool ON ai_tool_reports(tool_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_tool_reports_user ON ai_tool_reports(user_id);
"""


# ----- seed data ----------------------------------------------------------------------------------

def _is_web_url(url: str) -> bool:
    return bool(re.match(r"^https?://[^\s/]+\.[^\s]+$", url or ""))


def load_seed(path: Path | None = None) -> list[dict]:
    """Read and check the seed file. Raises ValueError if an entry is malformed."""
    data = json.loads((path or SEED_FILE).read_text(encoding="utf-8"))
    tools = data["tools"]
    seen: set[str] = set()
    for t in tools:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", t["slug"]) or t["slug"] in seen:
            raise ValueError(f"bad or repeated slug in AI tools seed: {t['slug']!r}")
        seen.add(t["slug"])
        if t["kind"] not in KINDS:
            raise ValueError(f"unknown kind {t['kind']!r} for {t['slug']}")
        if t.get("url") and not _is_web_url(t["url"]):
            raise ValueError(f"tool url must be http or https: {t['slug']}")
        if not t.get("sources"):
            raise ValueError(f"research note without sources: {t['slug']}")
        for s in t["sources"]:
            if not _is_web_url(s["url"]):
                raise ValueError(f"source url must be http or https: {s['url']!r}")
    return tools


def seed(path: Path | None = None) -> None:
    """Insert or update every tool from the seed file, matched by slug. Safe to run on every start.
    Tools removed from the file are kept, because members' reports point to them."""
    tools = load_seed(path)
    with db.session() as conn:
        for t in tools:
            sources = json.dumps([{"title": s.get("title", ""), "url": s["url"]} for s in t["sources"]])
            conn.execute(
                "INSERT INTO ai_tools (slug, name, vendor, kind, url, research_note, research_sources_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(slug) DO UPDATE SET name = excluded.name, vendor = excluded.vendor,"
                " kind = excluded.kind, url = excluded.url, research_note = excluded.research_note,"
                " research_sources_json = excluded.research_sources_json",
                (t["slug"], t["name"], t.get("vendor", ""), t["kind"], t.get("url", ""), t["research_note"], sources),
            )


# ----- reading ----------------------------------------------------------------------------------

def _cutoff(days: int = SUMMARY_DAYS) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def _empty_counts() -> dict:
    return {"usable": 0, "usable_with_workarounds": 0, "not_usable": 0}


def verdict_text(counts: dict) -> str:
    """Plain summary like '4 usable, 1 with workarounds, 0 not usable', or 'No reports yet'."""
    if not sum(counts.values()):
        return "No reports yet"
    return (f"{counts['usable']} usable, {counts['usable_with_workarounds']} with workarounds, "
            f"{counts['not_usable']} not usable")


def _tool_dict(row) -> dict:
    tool = dict(row)
    tool["sources"] = json.loads(tool.pop("research_sources_json") or "[]")
    return tool


def get_tool(slug: str) -> dict:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM ai_tools WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="We could not find that tool. Go to the AI tools report to see every tool.")
    return _tool_dict(row)


def tools_with_summary() -> list[dict]:
    """Every tool with its verdict counts from the last 90 days and the date of its latest report."""
    cutoff = _cutoff()
    with db.session() as conn:
        tools = [_tool_dict(r) for r in conn.execute("SELECT * FROM ai_tools ORDER BY name COLLATE NOCASE")]
        recent = conn.execute(
            "SELECT tool_id, overall, COUNT(*) AS n FROM ai_tool_reports WHERE created_at >= ? GROUP BY tool_id, overall",
            (cutoff,)).fetchall()
        latest = conn.execute("SELECT tool_id, MAX(created_at) AS latest FROM ai_tool_reports GROUP BY tool_id").fetchall()
    counts: dict[int, dict] = {}
    for r in recent:
        counts.setdefault(r["tool_id"], _empty_counts())[r["overall"]] = r["n"]
    latest_by_tool = {r["tool_id"]: r["latest"] for r in latest}
    for t in tools:
        t["counts"] = counts.get(t["id"], _empty_counts())
        t["verdict"] = verdict_text(t["counts"])
        if not sum(t["counts"].values()) and t["id"] in latest_by_tool:
            t["verdict"] = f"No reports in the last {SUMMARY_DAYS} days"
        t["latest"] = (latest_by_tool.get(t["id"]) or "")[:10]
    return tools


def _report_dict(row) -> dict:
    r = dict(row)
    r["date"] = r["created_at"][:10]
    r["checks"] = [{"key": key, "statement": statement, "answer": r[key], "answer_words": ANSWER_WORDS[r[key]]}
                   for key, _question, statement, _hint in CHECKS]
    r["overall_words"] = OVERALL_WORDS[r["overall"]]
    return r


def reports_for(tool_id: int) -> list[dict]:
    with db.session() as conn:
        rows = conn.execute(
            "SELECT r.*, u.pseudonym FROM ai_tool_reports r JOIN users u ON u.id = r.user_id"
            " WHERE r.tool_id = ? ORDER BY r.created_at DESC, r.id DESC", (tool_id,)).fetchall()
    return [_report_dict(r) for r in rows]


def recent_counts(reports: list[dict], days: int = SUMMARY_DAYS) -> dict:
    """Verdict counts for the reports made in the last `days` days."""
    cutoff = _cutoff(days)
    counts = _empty_counts()
    for r in reports:
        if r["created_at"] >= cutoff:
            counts[r["overall"]] += 1
    return counts


def _version_key(version: str) -> str:
    return re.sub(r"\s+", " ", version.strip().lower().removeprefix("version ").lstrip("v"))


def group_by_version(reports: list[dict]) -> list[dict]:
    """Group reports (already newest first) by tool version. Groups are ordered by their newest report."""
    groups: dict[str, dict] = {}
    for r in reports:
        key = _version_key(r["tool_version"])
        group = groups.get(key)
        if group is None:
            group = groups[key] = {"version": r["tool_version"], "reports": [], "counts": _empty_counts(),
                                   "latest": r["date"]}
        group["reports"].append(r)
        group["counts"][r["overall"]] += 1
    for g in groups.values():
        g["verdict"] = verdict_text(g["counts"])
    return list(groups.values())


def regression_notice(groups: list[dict]) -> str:
    """A plain sentence when the most recently reported version has a larger share of 'not usable'
    reports than the version reported before it. Empty otherwise. It only compares counts; it is not a test."""
    if len(groups) < 2:
        return ""
    new, old = groups[0], groups[1]
    new_total, old_total = len(new["reports"]), len(old["reports"])
    new_bad, old_bad = new["counts"]["not_usable"], old["counts"]["not_usable"]
    if new_bad and new_bad * old_total > old_bad * new_total:
        return (f"Members report more problems with version {new['version']} than with version {old['version']}: "
                f"{new_bad} of {new_total} reports for {new['version']} say not usable, "
                f"compared with {old_bad} of {old_total} for {old['version']}.")
    return ""


# ----- validation -------------------------------------------------------------------------------

_SECRET_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}|\bsk-[A-Za-z0-9]{20,}|\b(password|passcode|one-time code)\s*[:=]", re.I)
_CARD_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def looks_secret(text: str) -> bool:
    """True if the text seems to contain an API key, a password or a card number."""
    if _SECRET_RE.search(text):
        return True
    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return True
    return False


def _clean_line(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def validate_report(form: dict) -> tuple[dict, dict]:
    """Return (values, errors). Errors are in the order the fields appear on the form."""
    values = {k: _clean_line(form.get(k)) for k in TEXT_LIMITS}
    for k in CHECK_KEYS + ("overall",):
        values[k] = str(form.get(k) or "")
    for k in ("workarounds", "note"):
        values[k] = str(form.get(k) or "").replace("\r\n", "\n").strip()
    errors: dict[str, str] = {}
    if not values["tool_version"]:
        errors["tool_version"] = "Enter the version of the tool you used, for example 2.1.296."
    if not values["screen_reader"]:
        errors["screen_reader"] = "Enter the screen reader you used, for example NVDA."
    if not values["os"]:
        errors["os"] = "Enter your operating system, for example Windows 11."
    for k, limit in TEXT_LIMITS.items():
        if k not in errors and len(values[k]) > limit:
            errors[k] = f"Use {limit} characters or fewer."
    for key, question, _statement, _hint in CHECKS:
        if values[key] not in ANSWER_WORDS:
            errors[key] = f"Choose yes, no or I did not test this for: {question}"
    if values["overall"] not in OVERALL_WORDS:
        errors["overall"] = "Choose your overall verdict: usable, usable with workarounds, or not usable."
    for k, label in (("workarounds", "Workarounds"), ("note", "Your note")):
        if len(values[k]) > LONG_LIMIT:
            errors[k] = f"{label} is too long. Use {LONG_LIMIT} characters or fewer."
    for k in ("tool_version", "screen_reader", "screen_reader_version", "os", "workarounds", "note"):
        if k not in errors and looks_secret(values[k]):
            errors[k] = ("This looks like a password, API key or card number. Remove it: reports are public, "
                         "and this site never asks for those.")
    return values, errors


# ----- pages ------------------------------------------------------------------------------------

def _tool_page(request: Request, tool: dict, *, values: dict | None = None, errors: dict | None = None,
               status_code: int = 200):
    reports = reports_for(tool["id"])
    groups = group_by_version(reports)
    recent = recent_counts(reports)
    user = current_user(request)
    if values is None:
        values = {"screen_reader": (user or {}).get("screen_reader", "")}
    own_versions = sorted({r["tool_version"] for r in reports if user and r["user_id"] == user["id"]})
    title = f"How {tool['name']} works with screen readers"
    if errors:
        title += ": please fix the problems"
    return render(request, "ai_tools/tool.html", title=title, tool=tool, groups=groups,
                  report_count=len(reports), recent_verdict=verdict_text(recent), summary_days=SUMMARY_DAYS,
                  regression=regression_notice(groups), research_as_of=RESEARCH_AS_OF,
                  checks=CHECKS, answers=ANSWERS, overall_options=OVERALL, values=values, errors=errors or {},
                  own_versions=own_versions, status_code=status_code)


@router.get("")
def index(request: Request):
    tools = tools_with_summary()
    recent_total = sum(sum(t["counts"].values()) for t in tools)
    return render(request, "ai_tools/index.html", title=NAV["label"], tools=tools, recent_total=recent_total,
                  summary_days=SUMMARY_DAYS, checks=CHECKS, research_as_of=RESEARCH_AS_OF)


@router.get("/{slug}/reports.json")
def reports_json(slug: str):
    tool = get_tool(slug)
    reports = reports_for(tool["id"])
    recent = recent_counts(reports)
    return JSONResponse({
        "tool": {k: tool[k] for k in ("slug", "name", "vendor", "kind", "url")},
        "research": {"as_of": RESEARCH_AS_OF, "note": tool["research_note"], "sources": tool["sources"]},
        "summary_last_90_days": recent,
        "reports": [
            {"id": r["id"], "tool_version": r["tool_version"], "screen_reader": r["screen_reader"],
             "screen_reader_version": r["screen_reader_version"], "os": r["os"],
             "checks": {k: r[k] for k in CHECK_KEYS}, "overall": r["overall"],
             "workarounds": r["workarounds"], "note": r["note"], "created_at": r["created_at"]}
            for r in reports
        ],
    })


@router.get("/{slug}")
def tool_page(request: Request, slug: str):
    return _tool_page(request, get_tool(slug))


@router.post("/{slug}/reports")
async def add_report(request: Request, slug: str, user: dict = Depends(require_user)):
    form = await checked_form(request)
    tool = get_tool(slug)
    values, errors = validate_report(form)
    if errors:
        return _tool_page(request, tool, values=values, errors=errors, status_code=400)
    columns = ("tool_version", "screen_reader", "screen_reader_version", "os") + CHECK_KEYS + ("overall", "workarounds", "note")
    with db.session() as conn:
        mine = conn.execute("SELECT id, tool_version FROM ai_tool_reports WHERE tool_id = ? AND user_id = ?",
                            (tool["id"], user["id"])).fetchall()
        existing = next((r["id"] for r in mine if _version_key(r["tool_version"]) == _version_key(values["tool_version"])), None)
        if existing:
            conn.execute(f"UPDATE ai_tool_reports SET {', '.join(c + ' = ?' for c in columns)}, created_at = ? WHERE id = ?",
                         tuple(values[c] for c in columns) + (db.now(), existing))
            report_id = existing
            message = f"Your report for version {values['tool_version']} is updated. Thank you."
        else:
            cur = conn.execute(
                f"INSERT INTO ai_tool_reports (tool_id, user_id, {', '.join(columns)}, created_at)"
                f" VALUES (?, ?, {', '.join('?' for _ in columns)}, ?)",
                (tool["id"], user["id"]) + tuple(values[c] for c in columns) + (db.now(),))
            report_id = cur.lastrowid
            message = f"Your report for version {values['tool_version']} is published. Thank you."
    flash(request, message, "success")
    return redirect(f"/ai-tools/{tool['slug']}#report-{report_id}")


@router.post("/{slug}/reports/{report_id}/delete")
async def delete_report(request: Request, slug: str, report_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    tool = get_tool(slug)
    with db.session() as conn:
        row = conn.execute("SELECT id, user_id, tool_version FROM ai_tool_reports WHERE id = ? AND tool_id = ?",
                           (report_id, tool["id"])).fetchone()
        if row is None or row["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail="We could not find that report among yours.")
        conn.execute("DELETE FROM ai_tool_reports WHERE id = ?", (report_id,))
    flash(request, f"Your report for version {row['tool_version']} is deleted.", "success")
    return redirect(f"/ai-tools/{tool['slug']}")
