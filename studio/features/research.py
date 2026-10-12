"""Research: run your own research program.

Four stages per project: find prior work with sources (AI web search), plan a study that blind
participants can take part in (AI), keep notes and upload a CSV file whose facts the site computes
itself (then the AI interprets them), and draft a paper whose citations the site checks against the
numbered sources. Every result says who wrote it: the AI or the site.
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from markupsafe import Markup

from .. import db, jobs
from ..ai import AIError, AINotAvailable
from ..web import checked_form, current_user, flash, get_ai, markdown, redirect, render, require_user

router = APIRouter(prefix="/research")
NAV = {"label": 'Research', "url": '/research', "description": 'Run your own research: find prior work with sources, plan a study and draft a paper.'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_projects (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    question TEXT NOT NULL,
    field TEXT NOT NULL DEFAULT '',
    known TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_projects_user ON research_projects(user_id);
CREATE TABLE IF NOT EXISTS research_items (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('prior_work', 'plan', 'data', 'draft')),
    content_md TEXT NOT NULL DEFAULT '',
    sources_json TEXT NOT NULL DEFAULT '[]',
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_items_project ON research_items(project_id, kind, id);
CREATE TABLE IF NOT EXISTS research_notes (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL UNIQUE REFERENCES research_projects(id) ON DELETE CASCADE,
    body TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""

# ----- limits ----------------------------------------------------------------------------------

MAX_TITLE = 200
MAX_QUESTION = 2000
MAX_FIELD = 100
MAX_KNOWN = 5000
MAX_NOTES = 20000
MAX_CSV_BYTES = 200 * 1024
# A request far bigger than any allowed upload is refused before the body is read.
MAX_UPLOAD_REQUEST = 1024 * 1024
MAX_STAT_COLUMNS = 50
SAMPLE_ROWS = 10
CSV_TYPES = {"text/csv", "application/csv", "text/comma-separated-values"}
RESULT_NEEDED = "[RESULT NEEDED]"

# ----- AI instructions -------------------------------------------------------------------------

COMMON = (
    "The person you are helping is blind or has low vision and uses a screen reader. "
    "Put the summary first. Use plain language and short headings in Markdown. "
    "No tables unless asked. No emoji. "
    "Never claim something was checked or tested unless it was."
)

PRIOR_WORK_SYSTEM = COMMON + """

You help a blind researcher find prior academic work on their research question. Search the web
for peer-reviewed papers, preprints, theses, systematic reviews and reports from research bodies.
Prefer recent work and well-cited work. Read the pages you find when you can.

Write your answer in Markdown with these headings, in this order:
## Summary
Three to five sentences: what the prior work says about the question, and how much there is.
## Key studies
A list. For each study give the authors, the year, the title, the venue (journal, conference or
publisher) and one or two sentences on what it found. Only list studies that appeared in your search
results in this session. If you could not find the authors, year or venue, write "not found" for
that part. Never guess them.
## What is known
## What is not known yet
Gaps and open questions that the person's own study could fill.
## How to search further
Search terms and the databases or libraries worth trying next.

Never invent a citation, author, year, venue, DOI or finding. If you found little, say so plainly.
"""

PLAN_SYSTEM = COMMON + """

You help a blind researcher plan a study that answers their research question. Base the plan on
the prior work you are given and say which numbered source supports a choice, written as [n].
Only cite numbers from the list you are given. If there is no prior work, say so in the summary.

Write the plan in Markdown with these headings, in this order:
## Summary
## Hypothesis
## Method
## Participants and how to recruit them accessibly
Who, how many and why. Recruit through channels blind people use, with accessible sign-up.
Include blind and low-vision participants whenever the question is about them. Never use
blindfolded sighted people as stand-ins for blind participants.
## Measures
## Making every instrument usable by blind participants
For each survey, task, interview guide or test: how it works with screen readers (NVDA, JAWS,
VoiceOver, TalkBack), keyboards and braille displays. No CAPTCHAs, no image-only questions, no
drag-and-drop without a keyboard way, no strict time limits unless the time is what is measured.
Offer accessible documents and, where needed, braille or large print. Plan a pilot with blind
participants before the real study.
## Consent and ethics
Accessible consent forms that a screen reader reads in order, a way to ask questions, approval by
an ethics review board where one applies, data protection, and fair pay for participants.
## Analysis
## Timeline
## Risks
Risks to the study and to participants, and what to do about each.

This is a plan for the person to check. Do not claim any of it was tested.
"""

DATA_SYSTEM = COMMON + """

You help a blind researcher understand their data. The section "Computed by the site" holds exact
facts the site counted and calculated from their CSV file. Do not recompute or contradict them. You
may also see the first rows of the file and their notes.

Write in Markdown with these headings, in this order:
## Summary
## Patterns worth a closer look
## What these facts cannot tell you
## Analyses to try
For each analysis, say what it would show, what it assumes about the data and how to run it with
accessible tools.
## What to check in the data
Missing values, odd values, columns that may be coded wrongly.

This is your interpretation, not a result. Never invent numbers that are not in the facts or the
rows you were given. Say plainly when you are unsure.
"""

DRAFT_SYSTEM = COMMON + f"""

You write a first draft of a research paper for a blind researcher, from their question, plan,
notes and data facts. They will edit it.

Rules:
- Cite only the numbered sources you are given, written as [n], for example [2]. Never cite
  anything else and never invent a source, author or finding.
- Do not write a reference list. The site adds the numbered sources itself.
- Where a result, number, participant detail or quote is not available, write {RESULT_NEEDED}
  in its place. Never invent data, participants, statistics or quotes.
- Facts under "Computed by the site" are exact and may be reported as descriptive statistics.

Write in Markdown with these headings, in this order:
# A title for the paper
## Abstract
## Introduction
## Related work
## Method
## Results
## Discussion
## Limitations
## Conclusion
"""

# ----- stages ----------------------------------------------------------------------------------

STAGES = [
    {
        "key": "prior_work", "number": 1, "name": "Prior work", "slug": "prior-work",
        "explanation": "The AI searches the web for studies on your question and lists them with sources. "
                       "You open the sources to check them.",
        "start": "Find prior work", "again": "Search again", "read": "Read the prior work",
    },
    {
        "key": "plan", "number": 2, "name": "Study plan", "slug": "plan",
        "explanation": "The AI writes a study plan from your question and the prior work, including how to "
                       "recruit blind participants and make every survey and task usable with a screen reader.",
        "start": "Write a study plan", "again": "Write the plan again", "read": "Read the study plan",
    },
    {
        "key": "data", "number": 3, "name": "Notes and data", "slug": "data",
        "explanation": "Keep notes and upload a CSV file. The site counts rows and columns and works out "
                       "averages itself. The AI can then describe patterns.",
        "start": "", "again": "", "read": "Open notes and data",
    },
    {
        "key": "draft", "number": 4, "name": "Draft paper", "slug": "draft",
        "explanation": "The AI drafts a paper that cites only the sources from stage 1, and writes "
                       f"{RESULT_NEEDED} wherever results are missing. The site then checks every citation number.",
        "start": "Write a draft paper", "again": "Write the draft again", "read": "Read the draft paper",
    },
]
STAGE_BY_SLUG = {s["slug"]: s for s in STAGES}

# ----- small helpers ---------------------------------------------------------------------------

_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_SECRET_RE = re.compile(r"\b(password|passcode|passwd|one[- ]time code|verification code)\s*(is|:|=)\s*\S+", re.I)


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


def looks_secret(text: str) -> bool:
    """True if the text seems to hold a card number, a password or a one-time code."""
    if _SECRET_RE.search(text or ""):
        return True
    for m in _CARD_RE.finditer(text or ""):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return True
    return False


SECRET_MESSAGE = ("This looks like a card number, a password or a one-time code. "
                  "Never put those here. Remove it and try again.")


def _clean(form: dict, key: str) -> str:
    value = form.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def _date(stamp: str | None) -> str:
    return (stamp or "")[:10]


def _when(stamp: str | None) -> str:
    return (stamp or "")[:16].replace("T", " ") + " UTC" if stamp else ""


def _safe_sources(sources) -> list[dict]:
    out = []
    for s in sources or []:
        url = (s.get("url") if isinstance(s, dict) else getattr(s, "url", "")) or ""
        title = (s.get("title") if isinstance(s, dict) else getattr(s, "title", "")) or ""
        quote = (s.get("quote") if isinstance(s, dict) else getattr(s, "quote", "")) or ""
        if urlparse(url).scheme in ("http", "https"):
            out.append({"url": url, "title": title.strip() or url, "quote": quote.strip()})
    return out


_HEADING_RE = re.compile(r"<(/?)h([1-6])>")


def place_headings(text: str, top: int) -> Markup:
    """Render Markdown and move its headings so the highest one sits at level `top`.
    markdown() escapes any raw HTML first, so only the site's own heading tags are rewritten."""
    html = str(markdown(text))
    levels = [int(m.group(2)) for m in _HEADING_RE.finditer(html)]
    if not levels:
        return Markup(html)
    shift = top - min(levels)
    return Markup(_HEADING_RE.sub(lambda m: f"<{m.group(1)}h{max(1, min(6, int(m.group(2)) + shift))}>", html))


def _numbered_sources(sources: list[dict]) -> str:
    if not sources:
        return "(No sources yet.)"
    return "\n".join(f"[{i}] {s['title']} {s['url']}" for i, s in enumerate(sources, start=1))


# ----- CSV facts, computed by the site ---------------------------------------------------------

_NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_DELIMITER_NAMES = {",": "comma", ";": "semicolon", "\t": "tab", "|": "vertical bar"}


class CSVProblem(ValueError):
    pass


def format_number(x: float) -> str:
    if abs(x) >= 1e15:
        return f"{x:.6g}"
    if x == int(x):
        return str(int(x))
    text = f"{x:.4f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def compute_csv_stats(text: str) -> dict:
    """Count and calculate facts about CSV text. The first row is the column names.
    Numeric columns (every filled cell is a number) get count, mean, min and max; other columns get
    the number of distinct values and the three most common values."""
    # The separator is whichever of comma, semicolon, tab or vertical bar appears most in the first
    # line (the column names). Comma wins a tie. Predictable, unlike guessing from the whole file.
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = max(_DELIMITER_NAMES, key=lambda d: (first_line.count(d), d == ","))
    try:
        rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in r)]
    except csv.Error as exc:
        raise CSVProblem("The file could not be read as CSV. Check that it is a plain CSV file.") from exc
    if not rows:
        raise CSVProblem("The file is empty.")
    header, data = rows[0], rows[1:]
    if not data:
        raise CSVProblem("The file has a first row of column names but no rows of data under it.")
    width = max(len(header), max(len(r) for r in data))
    names = []
    for i in range(width):
        name = header[i].strip() if i < len(header) else ""
        names.append(name or f"Column {i + 1}")
    uneven = sum(1 for r in data if len(r) != len(header))
    columns = []
    for i, name in enumerate(names[:MAX_STAT_COLUMNS]):
        cells = [(r[i].strip() if i < len(r) else "") for r in data]
        filled = [c for c in cells if c != ""]
        col = {"name": name, "filled": len(filled), "empty": len(cells) - len(filled)}
        if filled and all(_NUMBER_RE.match(c) for c in filled):
            values = [float(c) for c in filled]
            mean = sum(values) / len(values)
            col.update(kind="number", count=len(values), mean=mean, min=min(values), max=max(values),
                       mean_text=format_number(mean), min_text=format_number(min(values)),
                       max_text=format_number(max(values)))
        else:
            counts = Counter(filled)
            col.update(kind="text", distinct=len(counts),
                       top=[{"value": v[:100], "count": n} for v, n in counts.most_common(3)])
        columns.append(col)
    sample_rows = [[c[:100] for c in r[:MAX_STAT_COLUMNS]] for r in data[:SAMPLE_ROWS]]
    return {
        "rows": len(data),
        "columns": width,
        "column_names": names,
        "delimiter": _DELIMITER_NAMES.get(delimiter, repr(delimiter)),
        "uneven_rows": uneven,
        "empty_cells": sum(c["empty"] for c in columns),
        "column_stats": columns,
        "columns_shown": len(columns),
        "sample": sample_rows,
    }


def facts_as_text(stats: dict | None) -> str:
    """The computed facts as plain sentences, for the AI prompt and the Markdown export."""
    if not stats:
        return "No data file has been uploaded."
    lines = [
        f"Rows of data (not counting the column names): {stats['rows']}",
        f"Columns: {stats['columns']}",
        "Column names: " + ", ".join(stats["column_names"]),
        f"Rows with a different number of cells than the first row: {stats['uneven_rows']}",
    ]
    for c in stats["column_stats"]:
        if c["kind"] == "number":
            lines.append(f"- {c['name']} (numbers): {c['count']} values, {c['empty']} empty, mean {c['mean_text']}, "
                         f"smallest {c['min_text']}, largest {c['max_text']}")
        else:
            top = "; ".join(f"{t['value']} ({t['count']})" for t in c["top"]) or "none"
            lines.append(f"- {c['name']} (text): {c['filled']} filled, {c['empty']} empty, "
                         f"{c['distinct']} distinct values; most common: {top}")
    if stats["columns_shown"] < stats["columns"]:
        lines.append(f"Facts were computed for the first {stats['columns_shown']} columns only.")
    return "\n".join(lines)


def _sample_as_text(stats: dict | None) -> str:
    if not stats or not stats.get("sample"):
        return "(none)"
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(stats["column_names"][:MAX_STAT_COLUMNS])
    writer.writerows(stats["sample"])
    return out.getvalue()


# ----- citations, checked by the site ----------------------------------------------------------

_CITE_RE = re.compile(r"\[(\s*\d+(?:\s*(?:,|-|–)\s*\d+)*\s*)\](?!\()")


def check_citations(text: str, source_count: int) -> dict:
    """Find every [n], [n, m] and [n-m] in the text and compare the numbers with the source list."""
    cited: set[int] = set()
    for m in _CITE_RE.finditer(text or ""):
        for part in m.group(1).split(","):
            bounds = [int(x) for x in re.split(r"[-–]", part) if x.strip()]
            if len(bounds) == 2 and 0 <= bounds[1] - bounds[0] <= 50:
                cited.update(range(bounds[0], bounds[1] + 1))
            else:
                cited.update(bounds)
    matched = sorted(n for n in cited if 1 <= n <= source_count)
    return {
        "matched": matched,
        "unmatched": sorted(n for n in cited if not 1 <= n <= source_count),
        "uncited": [n for n in range(1, source_count + 1) if n not in cited],
        "result_needed": (text or "").count(RESULT_NEEDED),
    }


# ----- storage ---------------------------------------------------------------------------------

def _project(project_id: int, user: dict) -> dict:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM research_projects WHERE id = ? AND user_id = ?",
                           (project_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="We could not find that research project.")
    return dict(row)


def _item_dict(row) -> dict:
    item = dict(row)
    item["sources"] = json.loads(item.get("sources_json") or "[]")
    item["meta"] = json.loads(item.get("meta_json") or "{}")
    return item


def _latest(project_id: int, kind: str) -> dict | None:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM research_items WHERE project_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
                           (project_id, kind)).fetchone()
    return _item_dict(row) if row else None


def _item(project_id: int, kind: str, version: int | None) -> dict | None:
    if version is None:
        return _latest(project_id, kind)
    with db.session() as conn:
        row = conn.execute("SELECT * FROM research_items WHERE id = ? AND project_id = ? AND kind = ?",
                           (version, project_id, kind)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="We could not find that version.")
    return _item_dict(row)


def _versions(project_id: int, kind: str) -> list[dict]:
    with db.session() as conn:
        rows = conn.execute("SELECT id, created_at FROM research_items WHERE project_id = ? AND kind = ? ORDER BY id DESC",
                            (project_id, kind)).fetchall()
    return [{"id": r["id"], "when": _when(r["created_at"])} for r in rows]


def _notes(project_id: int) -> dict | None:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM research_notes WHERE project_id = ?", (project_id,)).fetchone()
    return dict(row) if row else None


def _save_item(project_id: int, kind: str, content: str, sources: list[dict] | None = None,
               meta: dict | None = None) -> bool:
    """Add a new version. Returns False if the project was deleted while the AI was working."""
    stamp = db.now()
    with db.session() as conn:
        if conn.execute("SELECT 1 FROM research_projects WHERE id = ?", (project_id,)).fetchone() is None:
            return False
        conn.execute(
            "INSERT INTO research_items (project_id, kind, content_md, sources_json, meta_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, kind, content, json.dumps(sources or []), json.dumps(meta or {}), stamp),
        )
        conn.execute("UPDATE research_projects SET updated_at = ? WHERE id = ?", (stamp, project_id))
    return True


def _load_project(project_id: int) -> dict | None:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM research_projects WHERE id = ?", (project_id,)).fetchone()
    return dict(row) if row else None


def _stages(project: dict) -> list[dict]:
    pid = project["id"]
    notes = _notes(pid)
    out = []
    for stage in STAGES:
        latest = _latest(pid, stage["key"])
        done_at = latest["created_at"] if latest else None
        if stage["key"] == "data" and notes and notes["body"].strip():
            done_at = max(filter(None, [done_at, notes["updated_at"]]))
        out.append({**stage, "done_at": _date(done_at), "done": bool(done_at),
                    "url": f"/research/{pid}/{stage['slug']}"})
    return out


# ----- pages: list and create ------------------------------------------------------------------

def _index_page(request: Request, user: dict | None, *, values: dict | None = None, errors: dict | None = None,
                status_code: int = 200):
    projects = []
    if user:
        with db.session() as conn:
            rows = conn.execute("SELECT * FROM research_projects WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
                                (user["id"],)).fetchall()
        for r in rows:
            p = dict(r)
            p["done_count"] = sum(1 for s in _stages(p) if s["done"])
            projects.append(p)
    title = "Run your own research" + (": please fix the problems" if errors else "")
    return render(request, "research/index.html", title=title, projects=projects, values=values or {},
                  errors=errors or {}, status_code=status_code)


@router.get("")
def index(request: Request):
    return _index_page(request, current_user(request))


@router.post("")
async def create(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    values = {k: _clean(form, k) for k in ("title", "question", "field", "known")}
    errors: dict[str, str] = {}
    if not values["title"]:
        errors["title"] = "Enter a short name for your project."
    elif len(values["title"]) > MAX_TITLE:
        errors["title"] = f"Use {MAX_TITLE} characters or fewer for the name."
    if not values["question"]:
        errors["question"] = "Enter your research question."
    elif len(values["question"]) > MAX_QUESTION:
        errors["question"] = f"Use {MAX_QUESTION} characters or fewer for the question."
    if len(values["field"]) > MAX_FIELD:
        errors["field"] = f"Use {MAX_FIELD} characters or fewer for the field."
    if len(values["known"]) > MAX_KNOWN:
        errors["known"] = f"Use {MAX_KNOWN} characters or fewer here."
    for key in ("title", "question", "field", "known"):
        if key not in errors and looks_secret(values[key]):
            errors[key] = SECRET_MESSAGE
    if errors:
        return _index_page(request, user, values=values, errors=errors, status_code=400)
    stamp = db.now()
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO research_projects (user_id, title, question, field, known, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user["id"], values["title"], values["question"], values["field"], values["known"], stamp, stamp),
        )
        project_id = cur.lastrowid
    flash(request, "Your research project is saved. Start with stage 1, prior work.", "success")
    return redirect(f"/research/{project_id}")


# ----- project page ----------------------------------------------------------------------------

@router.get("/{project_id}")
def project_page(request: Request, project_id: int, user: dict = Depends(require_user)):
    project = _project(project_id, user)
    stages = _stages(project)
    next_stage = next((s for s in stages if not s["done"]), None)
    return render(request, "research/project.html", title=project["title"], project=project, stages=stages,
                  done_count=sum(1 for s in stages if s["done"]), next_stage=next_stage)


# ----- starting AI work ------------------------------------------------------------------------

def _start(request: Request, user: dict, project: dict, kind: str, title: str, fn, back: str):
    try:
        ai = get_ai(request, user)
    except AINotAvailable as exc:
        flash(request, exc.user_message, "error")
        return redirect(back)
    job_id = jobs.submit(user["id"], f"research.{kind}", title, fn, ai, project["id"])
    return redirect(jobs.job_url(job_id))


@router.post("/{project_id}/prior-work")
async def start_prior_work(request: Request, project_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    project = _project(project_id, user)
    return _start(request, user, project, "prior_work", f"Finding prior work for {project['title']}",
                  _run_prior_work, f"/research/{project_id}")


@router.post("/{project_id}/plan")
async def start_plan(request: Request, project_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    project = _project(project_id, user)
    return _start(request, user, project, "plan", f"Writing a study plan for {project['title']}",
                  _run_plan, f"/research/{project_id}")


@router.post("/{project_id}/draft")
async def start_draft(request: Request, project_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    project = _project(project_id, user)
    return _start(request, user, project, "draft", f"Drafting a paper for {project['title']}",
                  _run_draft, f"/research/{project_id}")


@router.post("/{project_id}/data/describe")
async def start_describe(request: Request, project_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    project = _project(project_id, user)
    latest = _latest(project_id, "data")
    notes = _notes(project_id)
    has_stats = bool(latest and latest["meta"].get("stats"))
    has_notes = bool(notes and notes["body"].strip())
    if not has_stats and not has_notes:
        return _data_page(request, project, errors={"csv": "Upload a data file or save some notes before asking the AI."},
                          status_code=400)
    return _start(request, user, project, "data", f"Describing the data for {project['title']}",
                  _run_data, f"/research/{project_id}/data")


# ----- background work -------------------------------------------------------------------------

def _project_brief(project: dict) -> str:
    parts = [f"Project: {project['title']}", f"Research question: {project['question']}"]
    if project["field"]:
        parts.append(f"Field: {project['field']}")
    if project["known"]:
        parts.append(f"What the researcher already knows or suspects:\n{project['known']}")
    return "\n".join(parts)


def _run_prior_work(ctx, ai, project_id: int) -> str:
    project = _load_project(project_id)
    if project is None:
        return "/research"
    ctx.progress("Searching for studies on your question")
    result = ai.research(
        system=PRIOR_WORK_SYSTEM,
        prompt=_project_brief(project) + "\n\nFind the prior academic work on this question.",
        purpose="research.prior_work",
        max_searches=10,
    )
    if not (result.text or "").strip():
        raise AIError("The AI returned an empty answer. Please try again.")
    ctx.progress("Saving what was found")
    sources = _safe_sources(result.sources)
    if not _save_item(project_id, "prior_work", result.text.strip(), sources, {"source_count": len(sources)}):
        return "/research"
    return f"/research/{project_id}/prior-work"


def _prior_work_context(project_id: int) -> tuple[str, list[dict]]:
    prior = _latest(project_id, "prior_work")
    if not prior:
        return "No prior work search has been done yet.", []
    return prior["content_md"][:15000], prior["sources"]


def _run_plan(ctx, ai, project_id: int) -> str:
    project = _load_project(project_id)
    if project is None:
        return "/research"
    prior_text, sources = _prior_work_context(project_id)
    ctx.progress("Writing the study plan")
    text = ai.text(
        system=PLAN_SYSTEM,
        prompt=(f"{_project_brief(project)}\n\nPrior work found so far:\n{prior_text}\n\n"
                f"Numbered sources:\n{_numbered_sources(sources)}\n\nWrite the study plan."),
        purpose="research.plan",
    )
    if not (text or "").strip():
        raise AIError("The AI returned an empty answer. Please try again.")
    if not _save_item(project_id, "plan", text.strip(), sources, {"citations": check_citations(text, len(sources))}):
        return "/research"
    return f"/research/{project_id}/plan"


def _run_data(ctx, ai, project_id: int) -> str:
    project = _load_project(project_id)
    if project is None:
        return "/research"
    latest = _latest(project_id, "data")
    meta = dict(latest["meta"]) if latest else {}
    stats = meta.get("stats")
    notes = _notes(project_id)
    ctx.progress("Reading the computed facts and your notes")
    text = ai.text(
        system=DATA_SYSTEM,
        prompt=(f"{_project_brief(project)}\n\nComputed by the site:\n{facts_as_text(stats)}\n\n"
                f"First rows of the file:\n{_sample_as_text(stats)}\n\n"
                f"Researcher's notes:\n{(notes or {}).get('body', '').strip()[:15000] or '(none)'}\n\n"
                "Describe the patterns and suggest analyses."),
        purpose="research.data",
    )
    if not (text or "").strip():
        raise AIError("The AI returned an empty answer. Please try again.")
    meta["interpreted_at"] = db.now()
    if not _save_item(project_id, "data", text.strip(), [], meta):
        return "/research"
    return f"/research/{project_id}/data"


def _run_draft(ctx, ai, project_id: int) -> str:
    project = _load_project(project_id)
    if project is None:
        return "/research"
    prior_text, sources = _prior_work_context(project_id)
    plan = _latest(project_id, "plan")
    data = _latest(project_id, "data")
    notes = _notes(project_id)
    stats = data["meta"].get("stats") if data else None
    interpretation = data["content_md"] if data and data["content_md"] else "(none)"
    ctx.progress("Writing the draft paper")
    text = ai.text(
        system=DRAFT_SYSTEM,
        prompt=(f"{_project_brief(project)}\n\nPrior work:\n{prior_text}\n\n"
                f"Numbered sources (cite only these, as [n]):\n{_numbered_sources(sources)}\n\n"
                f"Study plan:\n{plan['content_md'][:15000] if plan else '(none yet)'}\n\n"
                f"Researcher's notes:\n{(notes or {}).get('body', '').strip()[:15000] or '(none)'}\n\n"
                f"Computed by the site:\n{facts_as_text(stats)}\n\n"
                f"The AI's earlier interpretation of the data (not checked):\n{interpretation[:8000]}\n\n"
                "Write the draft paper."),
        purpose="research.draft",
        max_tokens=32000,
    )
    if not (text or "").strip():
        raise AIError("The AI returned an empty answer. Please try again.")
    ctx.progress("Checking the citation numbers")
    citations = check_citations(text, len(sources))
    if not _save_item(project_id, "draft", text.strip(), sources, {"citations": citations}):
        return "/research"
    return f"/research/{project_id}/draft"


# ----- result pages ----------------------------------------------------------------------------

def _stage_page(request: Request, project_id: int, user: dict, slug: str, version: int | None, template: str):
    project = _project(project_id, user)
    stage = STAGE_BY_SLUG[slug]
    item = _item(project_id, stage["key"], version)
    latest = _latest(project_id, stage["key"])
    is_latest = bool(item and latest and item["id"] == latest["id"])
    heading = f"{stage['name']}: {project['title']}"
    if item and not is_latest:
        heading = f"{stage['name']}: {project['title']}, version from {_when(item['created_at'])}"
    ctx = {"project": project, "stage": stage, "item": item, "is_latest": is_latest,
           "stage_url": f"/research/{project_id}/{slug}",
           "versions": _versions(project_id, stage["key"]),
           "content_html": place_headings(item["content_md"], 3) if item else ""}
    if item and stage["key"] == "draft":
        ctx["citations"] = item["meta"].get("citations") or check_citations(item["content_md"], len(item["sources"]))
        ctx["write_url"] = "/write/new?" + urlencode({"title": project["title"], "kind": "paper"})
    return render(request, template, title=heading, **ctx)


@router.get("/{project_id}/prior-work")
def prior_work_page(request: Request, project_id: int, version: int | None = None, user: dict = Depends(require_user)):
    return _stage_page(request, project_id, user, "prior-work", version, "research/prior_work.html")


@router.get("/{project_id}/plan")
def plan_page(request: Request, project_id: int, version: int | None = None, user: dict = Depends(require_user)):
    return _stage_page(request, project_id, user, "plan", version, "research/plan.html")


@router.get("/{project_id}/draft")
def draft_page(request: Request, project_id: int, version: int | None = None, user: dict = Depends(require_user)):
    return _stage_page(request, project_id, user, "draft", version, "research/draft.html")


# ----- exports ---------------------------------------------------------------------------------

def _sources_md(sources: list[dict]) -> str:
    if not sources:
        return "No sources.\n"
    return "".join(f"{i}. {s['title']}. <{s['url']}>\n" for i, s in enumerate(sources, start=1))


def _md_response(text: str, filename: str) -> Response:
    return Response(text, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _export(project_id: int, user: dict, slug: str) -> Response:
    project = _project(project_id, user)
    stage = STAGE_BY_SLUG[slug]
    item = _latest(project_id, stage["key"])
    if item is None:
        raise HTTPException(status_code=404, detail=f"This project has no {stage['name'].lower()} yet.")
    lines = [f"# {stage['name']}: {project['title']}", "",
             f"Research question: {project['question']}", "",
             f"Written by the AI on {_date(item['created_at'])}. It has not been checked by a person.", ""]
    if stage["key"] == "draft":
        c = item["meta"].get("citations") or check_citations(item["content_md"], len(item["sources"]))
        lines.append(f"Checked by the site: {len(c['matched'])} cited source numbers match the source list; "
                     f"places marked {RESULT_NEEDED}: {c['result_needed']}.")
        if c["unmatched"]:
            lines.append("Citations the site could not match: " + ", ".join(f"[{n}]" for n in c["unmatched"]))
        lines.append("")
    lines += ["---", "", item["content_md"], "", "## Sources", "", _sources_md(item["sources"])]
    return _md_response("\n".join(lines), f"research-{project_id}-{slug}.md")


@router.get("/{project_id}/prior-work.md")
def export_prior_work(project_id: int, user: dict = Depends(require_user)):
    return _export(project_id, user, "prior-work")


@router.get("/{project_id}/plan.md")
def export_plan(project_id: int, user: dict = Depends(require_user)):
    return _export(project_id, user, "plan")


@router.get("/{project_id}/draft.md")
def export_draft(project_id: int, user: dict = Depends(require_user)):
    return _export(project_id, user, "draft")


# ----- notes and data --------------------------------------------------------------------------

def _data_page(request: Request, project: dict, *, errors: dict | None = None, notes_value: str | None = None,
               status_code: int = 200):
    pid = project["id"]
    item = _latest(pid, "data")
    notes = _notes(pid)
    stats = item["meta"].get("stats") if item else None
    title = f"Notes and data: {project['title']}" + (": please fix the problems" if errors else "")
    return render(request, "research/data.html", title=title, project=project, item=item, stats=stats,
                  meta=item["meta"] if item else {}, notes=notes,
                  notes_value=notes_value if notes_value is not None else (notes or {}).get("body", ""),
                  interpretation_html=place_headings(item["content_md"], 3) if item and item["content_md"] else "",
                  errors=errors or {}, status_code=status_code, sample_rows=SAMPLE_ROWS,
                  max_columns=MAX_STAT_COLUMNS, when=_when)


@router.get("/{project_id}/data")
def data_page(request: Request, project_id: int, user: dict = Depends(require_user)):
    return _data_page(request, _project(project_id, user))


@router.post("/{project_id}/notes")
async def save_notes(request: Request, project_id: int, user: dict = Depends(require_user)):
    form = await checked_form(request)
    project = _project(project_id, user)
    body = form.get("notes", "")
    body = body.strip() if isinstance(body, str) else ""
    if len(body) > MAX_NOTES:
        return _data_page(request, project, errors={"notes": f"Use {MAX_NOTES} characters or fewer for notes."},
                          notes_value=body, status_code=400)
    if looks_secret(body):
        return _data_page(request, project, errors={"notes": SECRET_MESSAGE}, notes_value=body, status_code=400)
    stamp = db.now()
    with db.session() as conn:
        conn.execute(
            "INSERT INTO research_notes (project_id, body, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(project_id) DO UPDATE SET body = excluded.body, updated_at = excluded.updated_at",
            (project_id, body, stamp),
        )
        conn.execute("UPDATE research_projects SET updated_at = ? WHERE id = ?", (stamp, project_id))
    flash(request, "Your notes are saved.", "success")
    return redirect(f"/research/{project_id}/data")


def _csv_error_page(request: Request, project: dict, message: str):
    return _data_page(request, project, errors={"csv": message}, status_code=400)


TOO_LARGE = "The file is larger than 200 KB. Upload a smaller CSV file, for example only the columns you need."


@router.post("/{project_id}/csv")
async def upload_csv(request: Request, project_id: int, user: dict = Depends(require_user)):
    project = _project(project_id, user)
    # Refuse a huge upload before reading it. Nothing is saved, so no CSRF check is needed for this refusal.
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_UPLOAD_REQUEST:
        return _csv_error_page(request, project, TOO_LARGE)
    form = await checked_form(request)
    upload = form.get("csv")
    if upload is None or isinstance(upload, str) or not getattr(upload, "filename", ""):
        return _csv_error_page(request, project, "Choose a CSV file to upload.")
    filename = upload.filename.replace("\\", "/").rsplit("/", 1)[-1][:120]
    content_type = (upload.content_type or "").split(";")[0].strip().lower()
    if content_type not in CSV_TYPES and not filename.lower().endswith(".csv"):
        return _csv_error_page(request, project, "This is not a CSV file. Upload a file that ends in .csv.")
    raw = await upload.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        return _csv_error_page(request, project, TOO_LARGE)
    if b"\x00" in raw:
        return _csv_error_page(request, project, "This is not a CSV file. It looks like a binary file, such as a spreadsheet or an image.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _csv_error_page(request, project, "The file is not saved as UTF-8 text. Save it again as CSV UTF-8 and upload it.")
    try:
        stats = compute_csv_stats(text)
    except CSVProblem as exc:
        return _csv_error_page(request, project, str(exc))
    _save_item(project_id, "data", "", [], {"stats": stats, "filename": filename, "uploaded_at": db.now()})
    flash(request, f"The site computed facts about {filename}: {stats['rows']} rows and {stats['columns']} columns.", "success")
    return redirect(f"/research/{project_id}/data")


# ----- delete ----------------------------------------------------------------------------------

@router.get("/{project_id}/delete")
def delete_confirm(request: Request, project_id: int, user: dict = Depends(require_user)):
    project = _project(project_id, user)
    return render(request, "research/delete.html", title=f"Delete {project['title']}?", project=project)


@router.post("/{project_id}/delete")
async def delete(request: Request, project_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    project = _project(project_id, user)
    with db.session() as conn:
        conn.execute("DELETE FROM research_items WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM research_notes WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM research_projects WHERE id = ? AND user_id = ?", (project_id, user["id"]))
    flash(request, f"Deleted the research project {project['title']}.", "success")
    return redirect("/research")
