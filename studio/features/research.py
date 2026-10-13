"""Research: run your own research program.

Four stages per project: find prior work with sources (AI web search), plan a study with every survey and
task accessible (AI), keep notes and upload a CSV file whose facts the site computes itself (then the AI
interprets them), and draft a paper whose citations the site checks against the numbered sources. Every
result says who wrote it: the AI or the site. A draft can be copied into a new Write document.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from markupsafe import Markup

from .. import db, jobs, sensitive
from ..ai import AIError, AINotAvailable, search_unavailable
from ..web import (checked_form, current_user, date_words, error_title, flash, form_text, get_ai, markdown, redirect,
                   render, require_user)

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
MAX_CSV_COLUMNS = 1000      # more than this is refused: no real table needs it, and each one costs storage
KEEP_UPLOADS = 10           # uploaded files kept per project (the AI's descriptions are kept separately)
MAX_CELL_TEXT = 100
SAMPLE_ROWS = 10
MAX_SOURCES = 50
MAX_ID = 2**63 - 1  # SQLite's largest integer: a bigger number in an address is simply not found
# A second click on a button that starts AI work joins the work already running instead of paying twice.
DUPLICATE_WINDOW = timedelta(minutes=15)
CSV_TYPES = {"text/csv", "application/csv", "text/comma-separated-values"}
RESULT_NEEDED = "[RESULT NEEDED]"

# ----- AI instructions -------------------------------------------------------------------------
# The shared member note (ai.member_voice: who the person is, their tools, the tone, summary first, no
# tables, never claim a check that did not happen) is put before each of these by AIClient. These add only
# what each task needs.

PRIOR_WORK_SYSTEM = """You help a researcher find prior academic work on their own research question. Search the web
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
Gaps and open questions that the researcher's own study could fill.
## How to search further
Search terms and the databases or libraries worth trying next.

Never invent a citation, author, year, venue, DOI or finding. If you found little, say so plainly.
"""

PLAN_SYSTEM = """You help a researcher plan a study on their own research question. Base the plan on the prior
work you are given and say which numbered source supports a choice, written as [n]. Only cite numbers
from the list you are given. If there is no prior work, say so in the summary.

Make every instrument accessible, including to the researcher who runs it. Add advice on recruiting blind
or low-vision participants only when the question involves blind or low-vision people. Do not assume the
study is about blindness because the researcher is blind or has low vision.

Write the plan in Markdown with these headings, in this order:
## Summary
## Hypothesis
## Method
## Participants and how to recruit them accessibly
Who, how many and why, with accessible sign-up. When the question involves blind or low-vision people,
include them and recruit through channels they use. Never use blindfolded sighted people as stand-ins
for blind participants.
## Measures
## Making every instrument accessible
For each survey, task, interview guide or test: how it works with screen readers (NVDA, JAWS,
VoiceOver, TalkBack), braille displays, magnification and keyboards, for participants and for the
researcher. No CAPTCHAs, no image-only questions, no drag-and-drop without a keyboard way, no strict
time limits unless the time is what is measured. Offer accessible documents and, where needed, braille
or large print. Plan a pilot with people like the participants before the real study.
## Consent and ethics
Accessible consent forms that read in order in speech, braille and large print, a way to ask
questions, approval by an ethics review board where one applies, data protection, and fair pay for
participants.
## Analysis
## Timeline
## Risks
Risks to the study and to participants, and what to do about each.

This is a plan for the researcher to check. Do not claim any of it was tested.
"""

DATA_SYSTEM = """You help a researcher understand their data. The section "Computed by the site" holds exact
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

DRAFT_SYSTEM = f"""You write a first draft of a research paper from the researcher's question, plan, notes and
data facts. They will edit it.

Rules:
- Cite only the numbered sources you are given, written as [n], for example [2]. Never cite
  anything else and never invent a source, author or finding.
- Do not write a reference list. The site adds the numbered sources itself.
- Where a result, number, participant detail or quote is not available, write {RESULT_NEEDED}
  in its place. Never invent data, participants, statistics or quotes.
- Facts under "Computed by the site" are exact and may be reported as descriptive statistics.
- The paper is for other readers. Never mention the person's blindness or low vision, or how they
  read, in it, unless they ask. Do not mention the researcher's own sight in the paper unless they ask.

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
        "key": "prior_work", "number": 1, "name": "Prior work", "noun": "prior work", "slug": "prior-work",
        "explanation": "The AI searches the web for studies on your question and lists them with sources. "
                       "You open the sources to check them.",
        "start": "Find prior work", "read": "Read the prior work",
        "again": "Search for prior work again",
    },
    {
        "key": "plan", "number": 2, "name": "Study plan", "noun": "study plan", "slug": "plan",
        "explanation": "The AI writes a study plan from your question and the prior work, with every survey "
                       "and task accessible to participants and to you.",
        "start": "Write a study plan", "start_no_sources": "Write a study plan without sources",
        "read": "Read the study plan", "next": "Next: write the study plan",
        "again": "Write the study plan again",
    },
    {
        "key": "data", "number": 3, "name": "Notes and data", "noun": "notes and data", "slug": "data",
        "explanation": "Keep notes and upload a CSV file. The site counts rows and columns and works out "
                       "averages itself. The AI can then describe patterns.",
        "start": "Add notes and data", "read": "Open notes and data", "next": "Next: add notes and data",
        "again": "",
    },
    {
        "key": "draft", "number": 4, "name": "Draft paper", "noun": "draft paper", "slug": "draft",
        "explanation": "The AI drafts a paper that cites only the sources from stage 1, and writes "
                       f"{RESULT_NEEDED} wherever results are missing. The site then checks every citation number.",
        "start": "Write a draft paper", "start_no_sources": "Write a draft paper without sources",
        "read": "Read the draft paper", "next": "Next: write the draft paper",
        "again": "Write the draft paper again",
    },
]
STAGE_BY_SLUG = {s["slug"]: s for s in STAGES}
# Said next to "Write a study plan" and "Write a draft paper" while there is nothing to cite.
NO_SOURCES_NOTE = ("Stage 1 has not found any sources yet, so there are none to cite. "
                   "You can run stage 1, prior work, first.")

# ----- small helpers ---------------------------------------------------------------------------

# The shared wording for text that looks like a secret (docs/VOICE.md, 4.9), from studio/sensitive.py.
SECRET_MESSAGE = sensitive.SECRET_MESSAGE
SECRET_CONTEXT = 20  # characters quoted on each side of the words that looked like a secret


def looks_secret(text: str) -> bool:
    """True if the text seems to hold a password, a PIN, a one-time code, an API key or a card number.
    The same check as every other section (sensitive.find_secret), so the same words are accepted everywhere."""
    return bool(sensitive.find_secret(text or ""))


def _secret_span(text: str) -> tuple[int, int] | None:
    """Where in the text the words that look like a secret are, found with the shared detector alone: the
    shortest start of the text that it refuses, then the shortest end of that. None if it cannot be narrowed."""
    if not sensitive.find_secret(text):
        return None
    lo, hi = 1, len(text)
    while lo < hi:  # shortest prefix that is refused
        mid = (lo + hi) // 2
        if sensitive.find_secret(text[:mid]):
            hi = mid
        else:
            lo = mid + 1
    end = lo
    if not sensitive.find_secret(text[:end]):
        return None
    lo, hi = 0, end - 1
    while lo < hi:  # latest start that is still refused
        mid = (lo + hi + 1) // 2
        if sensitive.find_secret(text[mid:end]):
            lo = mid
        else:
            hi = mid - 1
    return lo, end


def secret_error(text: str) -> str:
    """The secrets message, quoting the words that looked like a secret with a little text either side, so the
    member can find them in a long note. Empty if the text looks fine."""
    if not looks_secret(text):
        return ""
    span = _secret_span(text)
    if span is None:
        return SECRET_MESSAGE
    start, end = max(0, span[0] - SECRET_CONTEXT), min(len(text), span[1] + SECRET_CONTEXT)
    # Whole words only: widen to the nearest space on each side.
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    near = " ".join(text[start:end].split())
    near = ("..." if start > 0 else "") + near + ("..." if end < len(text) else "")
    return SECRET_MESSAGE.replace(", so it was", f', near: "{near}". It was', 1)


def _clean(form: dict, key: str) -> str:
    value = form.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def _date(stamp: str | None) -> str:
    return date_words(stamp) if stamp else ""


def _when(stamp: str | None) -> str:
    return date_words(stamp, with_time=True) if stamp else ""


def _one_line(value, limit: int) -> str:
    """Text as one line: anything that is not text becomes empty; runs of spaces and line breaks become one space."""
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _safe_sources(sources) -> list[dict]:
    """Keep only web sources with an http or https address and a host, each once, with tidy titles and quotes."""
    out, seen = [], set()
    for s in sources if isinstance(sources, (list, tuple)) else []:
        def get(key, _s=s):
            return _s.get(key) if isinstance(_s, dict) else getattr(_s, key, "")
        url = get("url")
        if not isinstance(url, str):
            continue
        url = url.strip()
        if not url or len(url) > 2000 or any(ch.isspace() for ch in url) or url in seen:
            continue
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            continue
        seen.add(url)
        out.append({"url": url, "title": _one_line(get("title"), 300) or url, "quote": _one_line(get("quote"), 300)})
        if len(out) >= MAX_SOURCES:
            break
    return out


def _answer_text(value) -> str:
    """The AI's answer as text, or a plain failure that the job page shows. Nothing is saved from a bad answer."""
    if not isinstance(value, str):
        raise AIError("The AI's answer came back incomplete, so nothing was saved. Please try again.")
    if not value.strip():
        raise AIError("The AI's answer came back empty, so nothing was saved. Please try again.")
    return value.strip()


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
# Cells that mean "no value" in data files. They count as missing, so one "NA" does not turn a column of
# numbers into text.
MISSING_MARKS = {"na", "n/a", "#n/a", "-", ".", "null", "nan"}
# A unit at the end of a column name, for example time_seconds or score_pct.
_UNIT_RE = re.compile(r"[_\s-](seconds|secs|sec|ms|minutes|mins|min|hours|days|years|pct|percent|kg|cm|mm)$", re.I)
_UNIT_WORDS = {"seconds": "seconds", "secs": "seconds", "sec": "seconds", "ms": "milliseconds",
               "minutes": "minutes", "mins": "minutes", "min": "minutes", "hours": "hours", "days": "days",
               "years": "years", "pct": "percent", "percent": "percent", "kg": "kilograms",
               "cm": "centimetres", "mm": "millimetres"}
SAMPLE_COLUMNS = 6  # columns shown in the table of first rows, so it fits a phone screen


class CSVProblem(ValueError):
    pass


def _number(cell: str) -> float | None:
    """The cell as a finite number, or None. A value too big for the computer (like 1e999) is not a number here."""
    if not _NUMBER_RE.match(cell):
        return None
    value = float(cell)
    return value if math.isfinite(value) else None


def _mean(values: list[float]) -> float:
    try:
        return math.fsum(values) / len(values)
    except OverflowError:  # the total is too big for the computer, but the average is not
        return math.fsum(v / len(values) for v in values)


def _spread(values: list[float]) -> float | None:
    """The sample standard deviation, or None for one value or numbers too big for the computer."""
    if len(values) < 2:
        return None
    try:
        sd = statistics.stdev(values)
    except (OverflowError, ValueError, statistics.StatisticsError):
        return None
    return sd if math.isfinite(sd) else None


def format_number(x: float) -> str:
    """A number for reading: whole numbers as they are, others rounded to 2 decimal places (2 significant
    figures for very small ones), very large ones in short scientific form."""
    if not math.isfinite(x):
        return str(x)
    if abs(x) >= 1e15:
        return f"{x:.6g}"
    if x == int(x):
        return str(int(x))
    if abs(x) < 0.01:
        return f"{x:.2g}"
    text = f"{x:.2f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def unit_of(name: str) -> str:
    """The unit a column name ends in, in words ("time_seconds" -> "seconds"), or ""."""
    m = _UNIT_RE.search(name or "")
    return _UNIT_WORDS[m.group(1).lower()] if m else ""


def compute_csv_stats(text: str) -> dict:
    """Count and calculate facts about CSV text. The first row is the column names.
    Empty cells and cells like NA, n/a, -, null or NaN count as missing. Numeric columns (every other cell is
    a number) get count, mean, median, standard deviation, min and max; other columns get the number of
    distinct values and the three most common values."""
    # The separator is whichever of comma, semicolon, tab or vertical bar appears most in the first
    # line (the column names). Comma wins a tie. Predictable, unlike guessing from the whole file.
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = max(_DELIMITER_NAMES, key=lambda d: (first_line.count(d), d == ","))
    try:
        rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in r)]
    except csv.Error as exc:
        raise CSVProblem("The file could not be read as CSV. Check that it is a plain CSV file.") from exc
    if not rows:
        raise CSVProblem("The file is empty. Choose a CSV file with the column names in the first row "
                         "and your data in the rows after it.")
    header, data = rows[0], rows[1:]
    if not data:
        raise CSVProblem("The file has a first row of column names but no rows of data after it. "
                         "Add your data rows, save the file and upload it again.")
    width = max(len(header), max(len(r) for r in data))
    if width > MAX_CSV_COLUMNS:
        raise CSVProblem(f"The file has {width:,} columns. The site reads files with up to {MAX_CSV_COLUMNS:,} "
                         "columns. Remove the columns you do not need and upload it again.")
    names = []
    for i in range(width):
        name = " ".join(header[i].split())[:MAX_CELL_TEXT] if i < len(header) else ""
        names.append(name or f"Column {i + 1}")
    uneven = sum(1 for r in data if len(r) != len(header))
    columns = []
    for i, name in enumerate(names[:MAX_STAT_COLUMNS]):
        cells = [(r[i].strip() if i < len(r) else "") for r in data]
        blank = sum(1 for c in cells if c == "")
        marks = [c for c in cells if c != "" and c.lower() in MISSING_MARKS]
        filled = [c for c in cells if c != "" and c.lower() not in MISSING_MARKS]
        col = {"name": name, "filled": len(filled), "empty": blank + len(marks), "blank": blank,
               "missing_marks": sorted(set(marks))[:5]}
        numbers = [_number(c) for c in filled]
        if filled and all(n is not None for n in numbers):
            values = [n for n in numbers if n is not None]
            mean, median, sd = _mean(values), statistics.median(values), _spread(values)
            col.update(kind="number", count=len(values), mean=mean, median=median, sd=sd,
                       min=min(values), max=max(values), unit=unit_of(name),
                       mean_text=format_number(mean), min_text=format_number(min(values)),
                       max_text=format_number(max(values)))
        else:
            counts = Counter(filled)
            col.update(kind="text", distinct=len(counts),
                       top=[{"value": v[:MAX_CELL_TEXT], "count": n} for v, n in counts.most_common(3)])
        columns.append(col)
    sample_rows = [[c[:MAX_CELL_TEXT] for c in r[:MAX_STAT_COLUMNS]] for r in data[:SAMPLE_ROWS]]
    return {
        "rows": len(data),
        "columns": width,
        "column_names": names[:MAX_STAT_COLUMNS],  # the total is in "columns"
        "delimiter": _DELIMITER_NAMES.get(delimiter, repr(delimiter)),
        "uneven_rows": uneven,
        "empty_cells": sum(c["empty"] for c in columns),
        "column_stats": columns,
        "columns_shown": len(columns),
        "sample": sample_rows,
    }


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _missing_text(c: dict) -> str:
    """'none missing', '1 missing (empty)' or '3 missing (empty, NA)'. Facts saved before missing marks were
    counted have only empty cells."""
    if not c.get("empty"):
        return "none missing"
    marks = c.get("missing_marks") or []
    blank = c.get("blank", c["empty"] if not marks else 0)
    kinds = (["empty"] if blank else []) + list(marks)
    return f"{c['empty']} missing ({', '.join(kinds)})"


def column_facts(c: dict) -> str:
    """One column's facts as a sentence, for the page, the AI prompt and the Markdown export."""
    if c["kind"] == "number":
        figures = [("mean", c.get("mean")), ("median", c.get("median")), ("smallest", c.get("min")),
                   ("largest", c.get("max")), ("standard deviation", c.get("sd"))]
        parts = [f"{label} {format_number(v)}" for label, v in figures if isinstance(v, (int, float))]
        unit = c.get("unit") or ""
        said = ", ".join(parts)
        said = f"In {unit}: {said}" if unit else said[:1].upper() + said[1:]
        return f"{_plural(c['count'], 'value')}, {_missing_text(c)}. {said}."
    start = f"{c['filled']} filled, {_missing_text(c)}"
    if not c["filled"]:
        return start + "."
    if c["distinct"] == c["filled"] and c["filled"] > 1:
        return f"{start}, all {c['filled']} values are different."
    top = "; ".join(f"{t['value']} ({t['count']})" for t in c["top"])
    return f"{start}, {_plural(c['distinct'], 'distinct value')}. Most common: {top}."


def facts_as_text(stats: dict | None) -> str:
    """The computed facts as plain sentences, for the AI prompt and the Markdown export."""
    if not stats:
        return "No data file has been uploaded."
    lines = [
        f"Rows of data (not counting the column names): {stats['rows']}",
        f"Columns: {stats['columns']}",
        "Column names: " + names_text(stats["column_names"], stats["columns"]),
        f"Rows with a different number of cells than the first row: {stats['uneven_rows']}",
    ]
    for c in stats["column_stats"]:
        kind = "numbers" if c["kind"] == "number" else "text"
        lines.append(f"- {c['name']} ({kind}): {column_facts(c)}")
    if stats["columns_shown"] < stats["columns"]:
        lines.append(f"Facts were computed for the first {stats['columns_shown']} columns only.")
    return "\n".join(lines)


def names_text(names: list[str], total: int | None = None) -> str:
    """Column names as one list: the first 50 only, then how many more there are (of total columns)."""
    more = (total if total is not None else len(names)) - MAX_STAT_COLUMNS
    return ", ".join(names[:MAX_STAT_COLUMNS]) + (f", and {more} more" if more > 0 else "")


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
_HEADING_LINE_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s)")
MAX_PLACE_TEXT = 200


def _cited_in(group: str) -> set[int]:
    """The numbers one [..] cites: [2] is 2, [2, 3] is 2 and 3, [4-6] is 4, 5 and 6."""
    cited: set[int] = set()
    for part in group.split(","):
        bounds = [int(x) for x in re.split(r"[-–]", part) if x.strip()]
        if len(bounds) == 2 and 0 <= bounds[1] - bounds[0] <= 50:
            cited.update(range(bounds[0], bounds[1] + 1))
        else:
            cited.update(bounds)
    return cited


def check_citations(text: str, source_count: int) -> dict:
    """Find every [n], [n, m] and [n-m] in the text and compare the numbers with the source list."""
    cited: set[int] = set()
    for m in _CITE_RE.finditer(text or ""):
        cited |= _cited_in(m.group(1))
    matched = sorted(n for n in cited if 1 <= n <= source_count)
    return {
        "matched": matched,
        "unmatched": sorted(n for n in cited if not 1 <= n <= source_count),
        "uncited": [n for n in range(1, source_count + 1) if n not in cited],
        "result_needed": (text or "").count(RESULT_NEEDED),
    }


def citation_places(text: str, numbers: list[int]) -> list[dict]:
    """For each number, the first sentence that cites it and the heading it sits under, so a member can find
    it in the text: {"n": 7, "sentence": "A made-up claim [7].", "section": "Abstract"}."""
    wanted, found, section = set(numbers), {}, ""
    for line in (text or "").splitlines():
        heading = _HEADING_LINE_RE.match(line)
        if heading:
            section = heading.group(1)
            continue
        for m in _CITE_RE.finditer(line):
            for n in sorted(_cited_in(m.group(1)) & wanted - found.keys()):
                starts = [e.end() for e in _SENTENCE_END_RE.finditer(line, 0, m.start())]
                start = starts[-1] if starts else 0
                end_match = _SENTENCE_END_RE.search(line, m.end())
                end = end_match.end() if end_match else len(line)
                sentence = " ".join(line[start:end].split()).lstrip("-*+ ").strip()
                if len(sentence) > MAX_PLACE_TEXT:
                    sentence = sentence[:MAX_PLACE_TEXT].rsplit(" ", 1)[0] + "..."
                found[n] = {"n": n, "sentence": sentence, "section": section[:MAX_PLACE_TEXT]}
    return [found.get(n, {"n": n, "sentence": "", "section": ""}) for n in numbers]


_TAG_RE = re.compile(r"(<[^>]+>)")
_SKIP_TAGS = {"a", "code", "pre"}


def link_citations(html: str, source_count: int) -> tuple[Markup, set[int]]:
    """Turn each cited number that matches a source into a link to that source in the list (#source-n).
    The first citation of each number gets the id cite-n, so the source list can link back to it.
    Numbers outside the source list stay plain text. Returns the page HTML and the numbers that got an id."""
    first: set[int] = set()

    def number_link(m: re.Match) -> str:
        n = int(m.group(0))
        if not 1 <= n <= source_count:
            return m.group(0)
        anchor = "" if n in first else f' id="cite-{n}"'
        first.add(n)
        return f'<a href="#source-{n}"{anchor}><span class="visually-hidden">source </span>{n}</a>'

    def citation(m: re.Match) -> str:
        return "[" + re.sub(r"\d+", number_link, m.group(1)) + "]"

    out, skipping = [], 0
    for part in _TAG_RE.split(str(html)):
        tag = re.match(r"<(/?)([a-zA-Z0-9]+)", part)
        if tag:
            if tag.group(2).lower() in _SKIP_TAGS and not part.endswith("/>"):
                skipping += -1 if tag.group(1) else 1
                skipping = max(skipping, 0)
            out.append(part)
        elif part.startswith("<") or skipping:
            out.append(part)
        else:
            out.append(_CITE_RE.sub(citation, part))
    return Markup("".join(out)), first


# ----- storage ---------------------------------------------------------------------------------

def _project(project_id: int, user: dict) -> dict:
    if not 1 <= project_id <= MAX_ID:
        raise HTTPException(status_code=404, detail="We could not find that research project.")
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
    if not 1 <= version <= MAX_ID:
        raise HTTPException(status_code=404, detail="We could not find that version.")
    with db.session() as conn:
        row = conn.execute("SELECT * FROM research_items WHERE id = ? AND project_id = ? AND kind = ?",
                           (version, project_id, kind)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="We could not find that version.")
    return _item_dict(row)


def _versions(project_id: int, kind: str) -> list[dict]:
    with db.session() as conn:
        rows = conn.execute("SELECT id, created_at, meta_json FROM research_items WHERE project_id = ? AND kind = ?"
                            " ORDER BY id DESC", (project_id, kind)).fetchall()
    out = []
    for i, r in enumerate(rows):
        copy_of = json.loads(r["meta_json"] or "{}").get("copy_of")
        out.append({"id": r["id"], "number": len(rows) - i, "when": _when(r["created_at"]),
                    "copy_of": _when(copy_of) if isinstance(copy_of, str) else ""})
    return out


def _notes(project_id: int) -> dict | None:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM research_notes WHERE project_id = ?", (project_id,)).fetchone()
    return dict(row) if row else None


def _save_item(project_id: int, kind: str, content: str, sources: list[dict] | None = None,
               meta: dict | None = None, owner: tuple[int, str] | None = None) -> bool:
    """Add a new version. Returns False if the project was deleted while the AI was working.

    owner is (user id, project created_at) for background work: ids of deleted projects can be reused, so a
    job saves only into the very project it was started for."""
    stamp = db.now()
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if owner is None:
            found = conn.execute("SELECT 1 FROM research_projects WHERE id = ?", (project_id,)).fetchone()
        else:
            found = conn.execute("SELECT 1 FROM research_projects WHERE id = ? AND user_id = ? AND created_at = ?",
                                 (project_id, *owner)).fetchone()
        if found is None:
            return False
        conn.execute(
            "INSERT INTO research_items (project_id, kind, content_md, sources_json, meta_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, kind, content, json.dumps(sources or []), json.dumps(meta or {}), stamp),
        )
        conn.execute("UPDATE research_projects SET updated_at = ? WHERE id = ?", (stamp, project_id))
    return True


def _load_project(project_id: int, owner: tuple[int, str]) -> dict | None:
    """The project a job was started for (same id, owner and creation time), or None if it was deleted."""
    with db.session() as conn:
        row = conn.execute("SELECT * FROM research_projects WHERE id = ? AND user_id = ? AND created_at = ?",
                           (project_id, *owner)).fetchone()
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


def _sources_ready(project_id: int) -> bool:
    """True once stage 1 has found at least one source, so a plan or draft has something to cite."""
    prior = _latest(project_id, "prior_work")
    return bool(prior and prior["sources"])


# ----- saying what just happened ---------------------------------------------------------------
# After an action, the next page's h1 (and so its title, which screen readers announce first) says
# what happened. The words are kept in the session for the one page they belong to, so a link
# cannot make a page claim something happened, and reloading the page shows its usual heading.

def _say_done(request: Request, path: str, text: str) -> None:
    request.session["research_done"] = {"path": path, "text": text}


def _done_text(request: Request) -> str:
    done = request.session.pop("research_done", None)
    if isinstance(done, dict) and done.get("path") == request.url.path:
        return str(done.get("text") or "")
    return ""


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
    title = error_title("Run your own research", errors)
    if not errors:
        title = _done_text(request) or title
    return render(request, "research/index.html", title=title, projects=projects, values=values or {},
                  errors=errors or {}, status_code=status_code)


@router.get("")
def index(request: Request):
    return _index_page(request, current_user(request))


@router.post("")
async def create(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    values = {k: _clean(form, k) for k in ("title", "question", "field", "known")}
    # The name and field are one line each: a line break pasted into them would break headings and lists.
    values["title"] = " ".join(values["title"].split())
    values["field"] = " ".join(values["field"].split())
    errors: dict[str, str] = {}
    if not values["title"]:
        errors["title"] = "Enter a short name for your project."
    elif len(values["title"]) > MAX_TITLE:
        errors["title"] = f"Use {MAX_TITLE} characters or fewer for the name. It has {len(values['title'])} now."
    if not values["question"]:
        errors["question"] = "Enter your research question."
    elif len(values["question"]) > MAX_QUESTION:
        errors["question"] = (f"Use {MAX_QUESTION} characters or fewer for the question. "
                              f"It has {len(values['question'])} now.")
    if len(values["field"]) > MAX_FIELD:
        errors["field"] = f"Use {MAX_FIELD} characters or fewer for the field. It has {len(values['field'])} now."
    if len(values["known"]) > MAX_KNOWN:
        errors["known"] = (f"Use {MAX_KNOWN} characters or fewer for what you already know. "
                           f"It has {len(values['known'])} now.")
    for key in ("title", "question", "field", "known"):
        if key not in errors and looks_secret(values[key]):
            errors[key] = secret_error(values[key])
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
    _say_done(request, f"/research/{project_id}", f"Project saved: {values['title']}")
    return redirect(f"/research/{project_id}")


# ----- project page ----------------------------------------------------------------------------

@router.get("/{project_id}")
def project_page(request: Request, project_id: int, user: dict = Depends(require_user)):
    project = _project(project_id, user)
    stages = _stages(project)
    next_stage = next((s for s in stages if not s["done"]), None)
    # Running a stage again costs money, so those buttons sit in a section of their own, away from the
    # "Read ..." links, and their names say which stage they run.
    rerun = [s for s in stages if s["done"] and s["again"]]
    return render(request, "research/project.html", title=_done_text(request) or project["title"], project=project,
                  stages=stages, done_count=sum(1 for s in stages if s["done"]), next_stage=next_stage,
                  rerun=rerun, sources_ready=_sources_ready(project_id), no_sources_note=NO_SOURCES_NOTE)


# ----- starting AI work ------------------------------------------------------------------------

def _running_job(user_id: int, job_kind: str) -> str | None:
    """The id of the same work for the same project, if it is still queued or running."""
    with db.session() as conn:
        rows = conn.execute("SELECT id, created_at FROM jobs WHERE user_id = ? AND kind = ? AND status IN ('queued', 'running')"
                            " ORDER BY created_at DESC LIMIT 5", (user_id, job_kind)).fetchall()
    now = datetime.now(timezone.utc)
    for r in rows:
        try:
            started = datetime.fromisoformat(r["created_at"])
        except ValueError:
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if now - started < DUPLICATE_WINDOW:  # older ones were stopped by a restart and will never finish
            return r["id"]
    return None


def _start(request: Request, user: dict, project: dict, kind: str, title: str, fn, back: str, *,
           searches: bool = False):
    """Start AI work for a project. searches: the work uses web search, so it is not started (or charged) when web
    search is not available for this member."""
    job_kind = f"research.{kind}:{project['id']}"
    running = _running_job(user["id"], job_kind)
    if running:
        # A double click, or a second tab: show the work already under way instead of paying for it twice.
        flash(request, "This was already started, so it was not started again. You will not pay twice.", "info")
        return redirect(jobs.job_url(running))
    try:
        ai = get_ai(request, user)
        problem = search_unavailable(ai) if searches else ""
    except AINotAvailable as exc:
        problem = exc.user_message
    if problem:
        flash(request, problem, "error")
        return redirect(back)
    job_id = jobs.submit(user["id"], job_kind, title, fn, ai, project["id"], project["created_at"],
                         item_ref=f"research:{project['id']}:{project['created_at']}")
    return redirect(jobs.job_url(job_id))


@router.post("/{project_id}/prior-work")
async def start_prior_work(request: Request, project_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    project = _project(project_id, user)
    return _start(request, user, project, "prior_work", f"Finding prior work for {project['title']}",
                  _run_prior_work, f"/research/{project_id}", searches=True)


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


def _run_prior_work(ctx, ai, project_id: int, created_at: str) -> str:
    owner = (ctx.user_id, created_at)
    project = _load_project(project_id, owner)
    if project is None:
        return "/research"
    ctx.progress("Searching for studies on your question")
    result = ai.research(
        system=PRIOR_WORK_SYSTEM,
        prompt=_project_brief(project) + "\n\nFind the prior academic work on this question.",
        purpose="research.prior_work",
        max_searches=10,
    )
    text = _answer_text(getattr(result, "text", None))
    ctx.progress("Saving what was found")
    sources = _safe_sources(getattr(result, "sources", None))
    if not _save_item(project_id, "prior_work", text, sources, {"source_count": len(sources)}, owner=owner):
        return "/research"
    return f"/research/{project_id}/prior-work?done=1"


def _prior_work_context(project_id: int) -> tuple[str, list[dict]]:
    prior = _latest(project_id, "prior_work")
    if not prior:
        return "No prior work search has been done yet.", []
    return prior["content_md"][:15000], prior["sources"]


def _run_plan(ctx, ai, project_id: int, created_at: str) -> str:
    owner = (ctx.user_id, created_at)
    project = _load_project(project_id, owner)
    if project is None:
        return "/research"
    prior_text, sources = _prior_work_context(project_id)
    ctx.progress("Writing the study plan")
    text = _answer_text(ai.text(
        system=PLAN_SYSTEM,
        prompt=(f"{_project_brief(project)}\n\nPrior work found so far:\n{prior_text}\n\n"
                f"Numbered sources:\n{_numbered_sources(sources)}\n\nWrite the study plan."),
        purpose="research.plan",
    ))
    if not _save_item(project_id, "plan", text, sources, {"citations": check_citations(text, len(sources))}, owner=owner):
        return "/research"
    return f"/research/{project_id}/plan?done=1"


def _run_data(ctx, ai, project_id: int, created_at: str) -> str:
    owner = (ctx.user_id, created_at)
    project = _load_project(project_id, owner)
    if project is None:
        return "/research"
    latest = _latest(project_id, "data")
    meta = dict(latest["meta"]) if latest else {}
    stats = meta.get("stats")
    notes = _notes(project_id)
    ctx.progress("Reading the computed facts and your notes")
    text = _answer_text(ai.text(
        system=DATA_SYSTEM,
        prompt=(f"{_project_brief(project)}\n\nComputed by the site:\n{facts_as_text(stats)}\n\n"
                f"First rows of the file:\n{_sample_as_text(stats)}\n\n"
                f"Researcher's notes:\n{(notes or {}).get('body', '').strip()[:15000] or '(none)'}\n\n"
                "Describe the patterns and suggest analyses."),
        purpose="research.data",
    ))
    now_latest = _latest(project_id, "data")
    if (now_latest or {}).get("id") != (latest or {}).get("id") and _load_project(project_id, owner) is not None:
        # A new file was uploaded while the AI worked. Saving would put the old file's facts back on
        # the page with an interpretation of the old file, so nothing is saved.
        raise AIError("You uploaded a new data file while the AI was working, so its answer describes the old "
                      "file. Nothing was saved. Ask the AI again to describe the new file.")
    meta["interpreted_at"] = db.now()
    if not _save_item(project_id, "data", text, [], meta, owner=owner):
        return "/research"
    return f"/research/{project_id}/data?done=1#ai-title"


def _run_draft(ctx, ai, project_id: int, created_at: str) -> str:
    owner = (ctx.user_id, created_at)
    project = _load_project(project_id, owner)
    if project is None:
        return "/research"
    prior_text, sources = _prior_work_context(project_id)
    plan = _latest(project_id, "plan")
    data = _latest(project_id, "data")
    notes = _notes(project_id)
    stats = data["meta"].get("stats") if data else None
    interpretation = data["content_md"] if data and data["content_md"] else "(none)"
    ctx.progress("Writing the draft paper")
    text = _answer_text(ai.text(
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
    ))
    ctx.progress("Checking the citation numbers")
    citations = check_citations(text, len(sources))
    if not _save_item(project_id, "draft", text, sources, {"citations": citations}, owner=owner):
        return "/research"
    return f"/research/{project_id}/draft?done=1"


# ----- result pages ----------------------------------------------------------------------------

def _next_action(project_id: int, stages: list[dict], after_key: str) -> dict | None:
    """The main action in a stage page's "What next": run the next stage, or open it if it already has a result."""
    keys = [s["key"] for s in stages]
    i = keys.index(after_key) + 1
    if i >= len(stages):
        return None
    nxt = stages[i]
    if nxt["done"]:
        return {"kind": "link", "url": nxt["url"], "label": f"Next: {nxt['read'][0].lower()}{nxt['read'][1:]}"}
    if nxt["key"] == "data":
        return {"kind": "link", "url": nxt["url"], "label": nxt["next"]}
    return {"kind": "button", "url": nxt["url"], "label": nxt["next"]}


def _stage_page(request: Request, project_id: int, user: dict, slug: str, version: int | None, template: str,
                done: str = ""):
    project = _project(project_id, user)
    stage = STAGE_BY_SLUG[slug]
    item = _item(project_id, stage["key"], version)
    latest = _latest(project_id, stage["key"])
    is_latest = bool(item and latest and item["id"] == latest["id"])
    stage_url = f"/research/{project_id}/{slug}"
    heading = f"{stage['name']}: {project['title']}"
    said = _done_text(request)
    if item and is_latest and version is None and said:
        heading = said
    elif item and is_latest and done and version is None:
        # Arrived from the finished job: say so first.
        heading = f"{stage['name']} is ready: {project['title']}"
    if item and not is_latest:
        heading = f"{stage['name']}: {project['title']}, version from {_when(item['created_at'])}"
    ctx = {"project": project, "stage": stage, "item": item, "is_latest": is_latest, "stage_url": stage_url,
           "versions": _versions(project_id, stage["key"]), "sources_ready": _sources_ready(project_id),
           "no_sources_note": NO_SOURCES_NOTE, "cited_first": set(), "places": [],
           "next_action": _next_action(project_id, _stages(project), stage["key"]),
           "content_html": place_headings(item["content_md"], 3) if item else ""}
    if item:
        short = stage["noun"] if stage["key"] != "draft" else "draft"
        if is_latest:
            ctx["download_url"] = f"/research/{project_id}/{slug}.md"
            ctx["download_label"] = f"Download the {short} as Markdown"
        else:
            ctx["download_url"] = f"/research/{project_id}/{slug}.md?version={item['id']}"
            ctx["download_label"] = f"Download this version ({_when(item['created_at'])}) as Markdown"
            ctx["restore_label"] = f"Make this the current {short}"
        # Every stage links each matching citation [n] to its source, and the source back to the text.
        ctx["content_html"], ctx["cited_first"] = link_citations(ctx["content_html"], len(item["sources"]))
        if stage["key"] in ("plan", "draft"):
            citations = item["meta"].get("citations") or check_citations(item["content_md"], len(item["sources"]))
            ctx["citations"] = citations
            ctx["places"] = citation_places(item["content_md"], citations.get("unmatched") or [])
    return render(request, template, title=heading, **ctx)


@router.get("/{project_id}/prior-work")
def prior_work_page(request: Request, project_id: int, version: int | None = None, done: str = "",
                    user: dict = Depends(require_user)):
    return _stage_page(request, project_id, user, "prior-work", version, "research/prior_work.html", done)


@router.get("/{project_id}/plan")
def plan_page(request: Request, project_id: int, version: int | None = None, done: str = "",
              user: dict = Depends(require_user)):
    return _stage_page(request, project_id, user, "plan", version, "research/plan.html", done)


@router.get("/{project_id}/draft")
def draft_page(request: Request, project_id: int, version: int | None = None, done: str = "",
               user: dict = Depends(require_user)):
    return _stage_page(request, project_id, user, "draft", version, "research/draft.html", done)


def _form_version(form: dict) -> int | None:
    """The version number a form sent, or None for the latest. Anything that is not a number is not found."""
    value = form_text(form, "version", 30).strip()
    if not value:
        return None
    if not value.isdigit():
        raise HTTPException(status_code=404, detail="We could not find that version.")
    return int(value)


@router.post("/{project_id}/{slug}/restore")
async def restore_version(request: Request, project_id: int, slug: str, user: dict = Depends(require_user)):
    """Make an earlier version the current one. It is saved again as the newest version, so the version that
    was current is kept too."""
    form = await checked_form(request)
    project = _project(project_id, user)
    stage = STAGE_BY_SLUG.get(slug)
    if stage is None or stage["key"] == "data":
        raise HTTPException(status_code=404, detail="We could not find that page.")
    version = _form_version(form)
    item = _item(project_id, stage["key"], version)
    if item is None:
        raise HTTPException(status_code=404, detail="We could not find that version.")
    latest = _latest(project_id, stage["key"])
    if not latest or latest["id"] != item["id"]:
        meta = {**item["meta"], "copy_of": item["meta"].get("copy_of") or item["created_at"]}
        _save_item(project_id, stage["key"], item["content_md"], item["sources"], meta)
    short = stage["noun"] if stage["key"] != "draft" else "draft"
    _say_done(request, f"/research/{project_id}/{slug}",
              f"The {short} from {_when(item['meta'].get('copy_of') or item['created_at'])} is now the current "
              f"version: {project['title']}")
    return redirect(f"/research/{project_id}/{slug}")


WRITE_NOTE = "Drafted by the AI in Research (not checked)"


def _draft_for_write(item: dict) -> str:
    """The draft as a Write document: the AI's text, then its numbered sources."""
    text = item["content_md"].strip()
    if item["sources"]:
        text += "\n\n## Sources\n\n" + _sources_md(item["sources"]).rstrip("\n")
    return text + "\n"


@router.post("/{project_id}/draft/write")
async def draft_to_write(request: Request, project_id: int, user: dict = Depends(require_user)):
    """Copy the draft on screen into a new Write document, so it can be edited there with suggestions and
    versions. The version note keeps the record that the AI wrote it."""
    form = await checked_form(request)
    project = _project(project_id, user)
    item = _item(project_id, "draft", _form_version(form))
    if item is None:
        raise HTTPException(status_code=404, detail="This project has no draft paper yet.")
    body, title, stamp = _draft_for_write(item), project["title"][:MAX_TITLE], db.now()
    since = (datetime.now(timezone.utc) - DUPLICATE_WINDOW).replace(microsecond=0).isoformat()
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        same = conn.execute("SELECT id FROM write_docs WHERE user_id = ? AND title = ? AND body = ? AND created_at >= ?"
                            " ORDER BY id DESC LIMIT 1", (user["id"], title, body, since)).fetchone()
        if same:
            doc_id = same["id"]
        else:
            doc_id = conn.execute(
                "INSERT INTO write_docs (user_id, title, kind, body, target, created_at, updated_at)"
                " VALUES (?, ?, 'paper', ?, '', ?, ?)", (user["id"], title, body, stamp, stamp)).lastrowid
            conn.execute("INSERT INTO write_versions (doc_id, body, note, created_at) VALUES (?, ?, ?, ?)",
                         (doc_id, body, WRITE_NOTE, stamp))
    if same:
        flash(request, "This draft was already copied into this document, so no second copy was made.", "info")
    else:
        flash(request, "Your draft is copied into this new document, with its numbered sources. The AI wrote it and "
                       f"nobody has checked it. Fill in each {RESULT_NEEDED} before you share it.", "success")
    return redirect(f"/write/{doc_id}")


# ----- exports ---------------------------------------------------------------------------------

def _sources_md(sources: list[dict]) -> str:
    if not sources:
        return "No sources.\n"
    return "".join(f"{i}. {s['title']}. <{s['url']}>\n" for i, s in enumerate(sources, start=1))


def _md_response(text: str, filename: str) -> Response:
    return Response(text, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _export(project_id: int, user: dict, slug: str, version: int | None = None) -> Response:
    """The version on screen as a Markdown file (the latest when no version is given)."""
    project = _project(project_id, user)
    stage = STAGE_BY_SLUG[slug]
    item = _item(project_id, stage["key"], version)
    if item is None:
        raise HTTPException(status_code=404, detail=f"This project has no {stage['noun']} yet.")
    lines = [f"# {stage['name']}: {project['title']}", "",
             f"Research question: {project['question']}", "",
             f"Written by the AI on {_date(item['created_at'])}. Nobody has checked it.", ""]
    if stage["key"] == "draft":
        c = item["meta"].get("citations") or check_citations(item["content_md"], len(item["sources"]))
        lines.append(f"Checked by the site: {len(c['matched'])} cited source numbers match the source list; "
                     f"places marked {RESULT_NEEDED}: {c['result_needed']}.")
        if c["unmatched"]:
            lines.append("Citations the site could not match: " + ", ".join(f"[{n}]" for n in c["unmatched"]))
        lines.append("Not checked: whether each source supports the sentence that cites it.")
        lines.append("")
    elif stage["key"] == "prior_work":
        lines += ["Not checked: whether each study says what the AI says, or whether the authors, years and "
                  "venues are right. Open the sources to check.", ""]
    else:
        unmatched = (item["meta"].get("citations") or {}).get("unmatched") or []
        if unmatched:
            lines += ["Citations the site could not match: " + ", ".join(f"[{n}]" for n in unmatched), ""]
        lines += ["Not checked: whether the plan works. Pilot every survey and task with people like your "
                  "participants.", ""]
    lines += ["---", "", item["content_md"], "", "## Sources", "", _sources_md(item["sources"])]
    suffix = f"-version-{item['id']}" if version is not None else ""
    return _md_response("\n".join(lines), f"research-{project_id}-{slug}{suffix}.md")


@router.get("/{project_id}/prior-work.md")
def export_prior_work(project_id: int, version: int | None = None, user: dict = Depends(require_user)):
    return _export(project_id, user, "prior-work", version)


@router.get("/{project_id}/plan.md")
def export_plan(project_id: int, version: int | None = None, user: dict = Depends(require_user)):
    return _export(project_id, user, "plan", version)


@router.get("/{project_id}/draft.md")
def export_draft(project_id: int, version: int | None = None, user: dict = Depends(require_user)):
    return _export(project_id, user, "draft", version)


# ----- notes and data --------------------------------------------------------------------------

def _data_page(request: Request, project: dict, *, errors: dict | None = None, notes_value: str | None = None,
               notes_unsaved: bool = False, status_code: int = 200, done: str = ""):
    pid = project["id"]
    item = _latest(pid, "data")
    notes = _notes(pid)
    stats = item["meta"].get("stats") if item else None
    title = error_title(f"Notes and data: {project['title']}", errors)
    if not errors:
        said = _done_text(request)
        if said:
            title = f"{said}: {project['title']}"
        elif done and item and item["content_md"]:
            title = f"The AI's interpretation is ready: {project['title']}"
    sample_names, sample = [], []
    if stats:
        sample_names = stats["column_names"][:SAMPLE_COLUMNS]
        sample = [(row + [""] * len(sample_names))[:len(sample_names)] for row in stats.get("sample") or []]
    return render(request, "research/data.html", title=title, project=project, item=item, stats=stats,
                  meta=item["meta"] if item else {}, notes=notes, notes_unsaved=notes_unsaved,
                  notes_value=notes_value if notes_value is not None else (notes or {}).get("body", ""),
                  interpretation_html=place_headings(item["content_md"], 3) if item and item["content_md"] else "",
                  errors=errors or {}, status_code=status_code, sample_names=sample_names, sample=sample,
                  sample_columns=SAMPLE_COLUMNS, when=_when, names_text=names_text, column_facts=column_facts,
                  next_action=_next_action(pid, _stages(project), "data"), sources_ready=_sources_ready(pid),
                  no_sources_note=NO_SOURCES_NOTE, kept_note=False)


@router.get("/{project_id}/data")
def data_page(request: Request, project_id: int, done: str = "", user: dict = Depends(require_user)):
    return _data_page(request, _project(project_id, user), done=done)


TOO_LARGE = "The file is larger than 200 KB. Upload a smaller CSV file, for example only the columns you need."


async def _read_csv(upload) -> tuple[dict, str]:
    """The facts the site computes from an uploaded file, and the file's plain name. Raises CSVProblem with a
    message that says what to do."""
    filename = " ".join(upload.filename.replace("\\", "/").rsplit("/", 1)[-1].split())[:120] or "your file"
    content_type = (upload.content_type or "").split(";")[0].strip().lower()
    if content_type not in CSV_TYPES and not filename.lower().endswith(".csv"):
        raise CSVProblem("This is not a CSV file. Please choose a file that ends in .csv.")
    raw = await upload.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        raise CSVProblem(TOO_LARGE)
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        # Excel's "Unicode text" and some exports save as UTF-16, which is full of zero bytes.
        raise CSVProblem("The file is saved as UTF-16 text, which the site cannot read. "
                         "Save it again as CSV UTF-8 and upload it.")
    if b"\x00" in raw:
        raise CSVProblem("This is not a CSV file. It looks like a binary file, such as a spreadsheet or an image. "
                         "Save it as CSV UTF-8 and upload that file.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CSVProblem("The file is not saved as UTF-8 text. Save it again as CSV UTF-8 and upload it.") from exc
    return compute_csv_stats(text), filename


def _nothing_changed(project_id: int) -> str:
    """Said after a file problem, so the member knows which facts the page still uses."""
    latest = _latest(project_id, "data")
    filename = (latest or {}).get("meta", {}).get("filename") if latest and latest["meta"].get("stats") else ""
    return " Nothing was changed." + (f" The facts from {filename} are still in use." if filename else "")


async def _save_notes_and_file(request: Request, project: dict, *, file_required: bool):
    """Notes and the data file are one form, so a note typed before choosing a file is never lost. Both are
    checked first; if either has a problem nothing is saved and the typed notes stay in the box."""
    pid = project["id"]
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_UPLOAD_REQUEST:
        # Refused before reading. Nothing is saved, so no CSRF check is needed for this refusal.
        return _data_page(request, project, errors={"csv": TOO_LARGE + _nothing_changed(pid)}, status_code=400)
    form = await checked_form(request)
    saved_body = (_notes(pid) or {}).get("body", "")
    has_notes = "notes" in form
    body = form_text(form, "notes", MAX_NOTES * 10).strip() if has_notes else saved_body
    notes_changed = has_notes and body != saved_body
    upload = form.get("csv")
    file_given = upload is not None and not isinstance(upload, str) and bool(getattr(upload, "filename", ""))
    errors: dict[str, str] = {}
    if len(body) > MAX_NOTES:
        errors["notes"] = (f"Use {MAX_NOTES} characters or fewer for notes. They have {len(body)} now. "
                           "Shorten them, or keep the long parts in your own files.")
    elif has_notes and looks_secret(body):
        errors["notes"] = secret_error(body)
    stats, filename = None, ""
    if file_given:
        try:
            stats, filename = await _read_csv(upload)
        except CSVProblem as exc:
            errors["csv"] = str(exc) + _nothing_changed(pid)
    elif file_required:
        errors["csv"] = "Choose a CSV file to upload."
    if errors.get("notes") and file_given:
        errors["notes"] += " Nothing was saved, so please choose the file again too."
    if errors:
        return _data_page(request, project, errors=errors, notes_value=body if has_notes else None,
                          notes_unsaved=notes_changed, status_code=400)
    stamp = db.now()
    save_notes = has_notes and (notes_changed or not file_given)
    with db.session() as conn:
        if save_notes:
            conn.execute(
                "INSERT INTO research_notes (project_id, body, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(project_id) DO UPDATE SET body = excluded.body, updated_at = excluded.updated_at",
                (pid, body, stamp),
            )
            conn.execute("UPDATE research_projects SET updated_at = ? WHERE id = ?", (stamp, pid))
    if stats is not None:
        _save_item(pid, "data", "", [], {"stats": stats, "filename": filename, "uploaded_at": stamp})
        with db.session() as conn:
            # Keep the latest uploads only, so uploading again and again cannot fill the disk.
            conn.execute("DELETE FROM research_items WHERE project_id = ? AND kind = 'data' AND content_md = '' AND id NOT IN"
                         " (SELECT id FROM research_items WHERE project_id = ? AND kind = 'data' AND content_md = ''"
                         " ORDER BY id DESC LIMIT ?)", (pid, pid, KEEP_UPLOADS))
    if stats is not None and save_notes:
        said = f"Notes saved and facts computed from {filename}"
    elif stats is not None:
        said = f"Facts computed from {filename}"
    else:
        said = "Your notes are saved" if body else "Your notes are cleared"
    _say_done(request, f"/research/{pid}/data", said)
    return redirect(f"/research/{pid}/data")


@router.post("/{project_id}/notes")
async def save_notes(request: Request, project_id: int, user: dict = Depends(require_user)):
    """Save the notes, and the data file too if one was chosen."""
    return await _save_notes_and_file(request, _project(project_id, user), file_required=False)


@router.post("/{project_id}/csv")
async def upload_csv(request: Request, project_id: int, user: dict = Depends(require_user)):
    """Upload a data file, and save the notes too if they were sent with it."""
    return await _save_notes_and_file(request, _project(project_id, user), file_required=True)


@router.get("/{project_id}/notes")
@router.get("/{project_id}/csv")
@router.get("/{project_id}/data/describe")
def back_to_data(project_id: int, user: dict = Depends(require_user)):
    """A reload or a restored tab after the data form came back with a question lands here: go to the data page."""
    _project(project_id, user)
    return redirect(f"/research/{project_id}/data")


@router.get("/{project_id}/draft/write")
def back_to_draft(project_id: int, user: dict = Depends(require_user)):
    _project(project_id, user)
    return redirect(f"/research/{project_id}/draft")


@router.get("/{project_id}/{slug}/restore")
def back_to_stage(project_id: int, slug: str, user: dict = Depends(require_user)):
    """The address the "Make this the current ..." button sends to: go to that stage's page."""
    _project(project_id, user)
    stage = STAGE_BY_SLUG.get(slug)
    if stage is None or stage["key"] == "data":
        raise HTTPException(status_code=404, detail="We could not find that page.")
    return redirect(f"/research/{project_id}/{slug}")


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
        jobs.forget_item(conn, user["id"], f"research:{project['id']}:{project['created_at']}")
    _say_done(request, "/research", f"Deleted the research project {project['title']}")
    return redirect("/research")


def forget_user(user_id: int) -> None:
    """Called when a member deletes their account: remove their private research projects."""
    with db.session() as conn:
        conn.execute("DELETE FROM research_projects WHERE user_id = ?", (user_id,))
