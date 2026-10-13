"""Build: a member describes a website, phone web app, small tool or game in their own words.

The flow:
1. The member writes their idea. The AI reads back what it understood (build.understand).
2. The member corrects it, or approves it and picks which of the AI's suggested extras to include.
3. The AI writes the files (build.generate). The site validates every path, runs its own
   accessibility checks on every HTML page and saves version 1.
4. The member asks for changes (build.change). The site computes what changed with difflib,
   so "what changed" is a machine fact, never the AI's claim.

Everything the AI builds is static HTML, CSS and plain JavaScript. Previews are served under a
sandbox Content-Security-Policy so a built page cannot read the site's cookies.
"""
from __future__ import annotations

import difflib
import io
import json
import re
import secrets
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from .. import db, jobs
from ..a11y_check import check_html
from ..ai import AIError, AINotAvailable
from ..web import checked_form, current_user, flash, get_ai, redirect, render, require_user

router = APIRouter(prefix="/build")
NAV = {"label": 'Build', "url": '/build', "description": 'Describe a website, app or tool in your own words. The AI reads back what it understood, builds it, checks it and tells you exactly what changed.'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS build_projects (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    idea TEXT NOT NULL,
    status TEXT NOT NULL,
    spec_json TEXT,
    share_token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_build_projects_user ON build_projects(user_id);
CREATE TABLE IF NOT EXISTS build_versions (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES build_projects(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    files_json TEXT NOT NULL,
    request_text TEXT NOT NULL DEFAULT '',
    ai_summary TEXT NOT NULL DEFAULT '',
    how_to_use TEXT NOT NULL DEFAULT '',
    diff_json TEXT NOT NULL DEFAULT '{}',
    checks_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (project_id, number)
);
"""

INDEX_TITLE = "Build a website, app, tool or game"

KINDS = [
    ("website", "A website"),
    ("phone_app", "A web app people can install on their phone"),
    ("tool", "A small tool or calculator"),
    ("game", "A game"),
    ("other", "Something else"),
]
KIND_LABELS = dict(KINDS)

STATUS_LABELS = {
    "new": "Not read by the AI yet",
    "understanding": "The AI is reading your idea",
    "understood": "Waiting for you to check what the AI understood",
    "building": "The AI is building it",
    "changing": "The AI is making a change",
    "ready": "Ready",
}

IDEA_HINT = "Say it the way you would tell a friend: who it is for, what it should do, and what it should say."
MAX_TITLE = 120
MAX_IDEA = 20_000
MAX_REQUEST = 10_000
MAX_CORRECTIONS_TOTAL = 50_000  # all corrections on one project together, so the AI prompt stays a sane size
MAX_AI_TEXT = 20_000  # any single piece of text the AI writes about a build (summary, how to use, ...)
# While the AI works on a project, a second request for the same project is refused. A job that never
# finished (for example after a server restart) stops blocking the project after this many minutes.
BUSY_MINUTES = 10
BUSY_STATUSES = {"understanding", "building", "changing"}
BUSY_MESSAGE = ("The AI is already working on this project. Wait for it to finish, then try again. "
                "You can follow it in Your recent work.")
# Same idea sent twice within this many seconds (for example a double press of the button): keep one.
DUPLICATE_SECONDS = 120
MAX_SQLITE_INT = 2 ** 63 - 1

# File rules for what the AI builds.
ALLOWED_EXTENSIONS = {"html", "css", "js", "json", "webmanifest", "svg", "txt", "md"}
CONTENT_TYPES = {
    "html": "text/html; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "json": "application/json",
    "webmanifest": "application/manifest+json",
    "svg": "image/svg+xml",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
}
MAX_FILES = 12
MAX_FILE_BYTES = 300_000
MAX_TOTAL_BYTES = 1_000_000
PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*$")
PREVIEW_CSP = ("sandbox allow-scripts allow-forms allow-popups allow-modals; "
               "default-src 'self' 'unsafe-inline' data:; frame-ancestors 'none'")

# Things the site itself does not check about a build (added to the checker's own list).
SITE_NOT_CHECKED = [
    "Whether the JavaScript works. The site reads the files but does not run them.",
    "CSS, JavaScript and other files that are not HTML pages. Only HTML pages get the machine checks.",
    "Saving data and installing as an app inside the preview. The preview runs in a locked-down sandbox "
    "for your safety, so these may not work there. They need the files on your own web host.",
]

# ----- AI prompts and schemas -------------------------------------------------------------------

READER_RULES = (
    "The person you are helping is blind or has low vision and uses a screen reader. "
    "Put the summary first. Use plain language, short sentences and short headings. "
    "No tables unless asked. No emoji. Never claim something was checked or tested unless it was."
)

WHAT_CAN_BE_BUILT = (
    "What can be built here: static HTML, CSS and plain JavaScript files only, at most 12 files. "
    "There is no server: no accounts or sign-in, no database, no payments, no sending email or messages, "
    "no sharing data between visitors. Data can only be saved in the visitor's own browser (localStorage). "
    "If the idea needs something that cannot be built here, say so in not_doing and describe the closest "
    "thing that can be built."
)

UNDERSTAND_SYSTEM = f"""You help a person plan a small website, web app, tool or game that will then be built for them.
{READER_RULES}

Read their idea and say back what you understood, so they can correct you before anything is built.
- summary: two or three sentences saying what it is, who it is for and what it does.
- audience: who will use it.
- pages: each page or screen, with its name, its purpose, and what is on it.
- features: what it does.
- data_kept: what information it stores and where. If it stores nothing, say so.
- questions: things you are unsure about and need the person to answer. Keep each one short.
- additions_not_asked: things you would add that the person did not ask for. Never put these in pages or features.
- not_doing: what you will not do, including anything that cannot be built here.
- suggested_title: a short title, at most six words.
Only put in pages and features what the person asked for, or what is needed for it to work.

{WHAT_CAN_BE_BUILT}
"""

UNDERSTAND_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "audience", "pages", "features", "data_kept", "questions",
                 "additions_not_asked", "not_doing", "suggested_title"],
    "properties": {
        "summary": {"type": "string"},
        "audience": {"type": "string"},
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "purpose", "contents"],
                "properties": {
                    "name": {"type": "string"},
                    "purpose": {"type": "string"},
                    "contents": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "features": {"type": "array", "items": {"type": "string"}},
        "data_kept": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "additions_not_asked": {"type": "array", "items": {"type": "string"}},
        "not_doing": {"type": "array", "items": {"type": "string"}},
        "suggested_title": {"type": "string"},
    },
}

FILES_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["files", "summary", "how_to_use", "not_done"],
    "properties": {
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "content"],
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            },
        },
        "summary": {"type": "string"},
        "how_to_use": {"type": "string"},
        "not_done": {"type": "array", "items": {"type": "string"}},
    },
}

BUILD_RULES = """Rules for the files. Follow every one.
- Static HTML, CSS and plain JavaScript only. No frameworks, no build step.
- No external scripts, fonts, stylesheets or CDNs. Everything is in the files you return.
- Every HTML page is complete: <!doctype html>, <html lang="...">, a <title>, exactly one <h1>, a <main> element,
  a label for every form field, real <button> elements for actions and real <a href> links for moving between pages.
- Headings go in order (h1, then h2, then h3). Put the most important content first.
- Works without JavaScript wherever possible. JavaScript only adds to pages that already make sense.
- When something changes on the page after an action, announce it in an element with role="status".
- Never stop people zooming (no user-scalable=no, no maximum-scale).
- Each HTML page is under 60 KB. At most 12 files.
- File paths are relative, like index.html or css/style.css: no leading slash, no "..", no backslashes.
  Only these extensions: html, css, js, json, webmanifest, svg, txt, md. There must be an index.html.
- No images unless the person supplied them.
- If data must be saved, use localStorage, wrap every use of it in try and catch so the page still works when
  storage is blocked, and say in how_to_use that the data stays only in that browser on that device.
- Never ask for or store passwords, card numbers or one-time codes.

What to return:
- files: every file, each with its complete content.
- summary: what you built, summary first, in plain words. Do not say you checked or tested anything.
- how_to_use: how to open it and use it with a screen reader and keyboard.
- not_done: anything the person asked for that you did not do, and any guesses you made. Empty if none."""

PHONE_APP_RULES = """This is a web app people can install on their phone. Also include:
- manifest.webmanifest with name, short_name, start_url "./index.html", scope "./", display "standalone",
  background_color and theme_color. Phones need an icon to install an app, so you may add one simple
  icon.svg (with a <title>) and list it in the manifest with sizes "any".
- A small service worker, sw.js, that caches the app's own files so it works offline.
- In index.html, link the manifest and register "sw.js" only if "serviceWorker" in navigator, inside try and catch."""

GENERATE_SYSTEM = f"""You build small websites, web apps, tools and games for a person who described what they want.
{READER_RULES}

{BUILD_RULES}"""

CHANGE_SYSTEM = f"""You change a small website, web app, tool or game that you built earlier, as the person asks.
{READER_RULES}

Change only what the person asked for. Leave every other file and every other line exactly as it is.
Return the complete new set of files, including the ones you did not change. Any file you leave out is deleted.
In summary, say what you changed, in plain words.

{BUILD_RULES}"""


# ----- helpers --------------------------------------------------------------------------------

class BuildFilesError(ValueError):
    """The AI's files break the site's file rules. The message is plain and safe to show."""


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _size(n: int) -> str:
    if n < 1024:
        return _plural(n, "byte")
    return f"{n / 1024:.1f} KB"


def _ext(path: str) -> str:
    return path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""


def validate_files(files: object) -> dict[str, str]:
    """Check the AI's files against the site's rules. Returns {path: content} or raises BuildFilesError."""
    if not isinstance(files, list) or not files:
        raise BuildFilesError("There were no files.")
    if len(files) > MAX_FILES:
        raise BuildFilesError(f"There were {len(files)} files. The limit is {MAX_FILES}.")
    out: dict[str, str] = {}
    lowered: set[str] = set()
    total = 0
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("content"), str):
            raise BuildFilesError("A file was missing its name or its content.")
        path = item["path"].strip()
        content = item["content"]
        if not path:
            raise BuildFilesError("A file had no name.")
        if path.startswith("/"):
            raise BuildFilesError(f'The file name "{path}" starts with a slash. File names must be relative, like index.html.')
        if "\\" in path:
            raise BuildFilesError(f'The file name "{path}" contains a backslash. Use forward slashes for folders.')
        if ".." in path:
            raise BuildFilesError(f'The file name "{path}" contains "..", which could reach outside the project.')
        ext = _ext(path)
        if ext not in ALLOWED_EXTENSIONS:
            kind = f'type ".{ext}"' if ext else "no file type"
            raise BuildFilesError(f'The file "{path}" has {kind}, which is not allowed. Allowed types: '
                                  + ", ".join(sorted(ALLOWED_EXTENSIONS)) + ".")
        if not PATH_RE.match(path):
            raise BuildFilesError(f'The file name "{path}" uses characters other than letters, numbers, dots, '
                                  "dashes and underscores.")
        if path in out:
            raise BuildFilesError(f'There were two files called "{path}".')
        if path.lower() in lowered:
            # Windows and Mac treat these as the same file, so the zip would lose one of them.
            raise BuildFilesError(f'There were two files called "{path}", differing only in capital letters.')
        lowered.add(path.lower())
        try:
            size = len(content.encode("utf-8"))
        except UnicodeEncodeError:
            raise BuildFilesError(f'The file "{path}" contains characters that are not valid text.') from None
        if size > MAX_FILE_BYTES:
            raise BuildFilesError(f'The file "{path}" is {_size(size)}. The limit is {_size(MAX_FILE_BYTES)} per file.')
        total += size
        out[path] = content
    if total > MAX_TOTAL_BYTES:
        raise BuildFilesError(f"All files together are {_size(total)}. The limit is {_size(MAX_TOTAL_BYTES)}.")
    if "index.html" not in out:
        raise BuildFilesError("There was no index.html, the page that opens first.")
    for path in out:
        if any(other.startswith(path + "/") for other in out):
            raise BuildFilesError(f'"{path}" is used as both a file name and a folder name.')
    return out


# ----- what the AI returns: never trust its shape ---------------------------------------------------

def _text(value: object, limit: int = MAX_AI_TEXT) -> str:
    """A piece of AI text as a clean string. Wrong types become empty; broken characters are replaced."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ""
    return value.encode("utf-8", "replace").decode("utf-8").strip()[:limit]


def _texts(value: object) -> list[str]:
    """A list of AI strings. A single string becomes a one-item list, never a list of letters."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [t for t in (_text(v) for v in value) if t]


def normalize_spec(raw: object) -> dict | None:
    """What the AI understood, in exactly the shape the pages expect. None if it is not usable at all."""
    if not isinstance(raw, dict):
        return None
    pages = []
    items = raw.get("pages")
    if isinstance(items, dict):
        items = [items]
    for i, item in enumerate(items if isinstance(items, list) else [], start=1):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name, purpose, contents = _text(item.get("name"), 200), _text(item.get("purpose")), _texts(item.get("contents"))
        if not (name or purpose or contents):
            continue
        pages.append({"name": name or f"Page {i}", "purpose": purpose, "contents": contents})
    return {
        "summary": _text(raw.get("summary")),
        "audience": _text(raw.get("audience")),
        "pages": pages,
        "features": _texts(raw.get("features")),
        "data_kept": _text(raw.get("data_kept")),
        "questions": _texts(raw.get("questions")),
        "additions_not_asked": _texts(raw.get("additions_not_asked")),
        "not_doing": _texts(raw.get("not_doing")),
        "suggested_title": _text(raw.get("suggested_title"), MAX_TITLE),
    }


def run_checks(files: dict[str, str]) -> dict:
    """Machine facts about a set of files: count, size, and the accessibility checker's report per page."""
    pages = []
    for path in sorted(files):
        if _ext(path) == "html":
            report = check_html(files[path], name=path)
            pages.append({"path": path, "bytes": len(files[path].encode("utf-8")), "report": report.to_dict()})
    return {
        "file_count": len(files),
        "total_bytes": sum(len(c.encode("utf-8")) for c in files.values()),
        "pages": pages,
    }


def _line_ops(old: str, new: str) -> list[tuple[str, int, int, int, int]]:
    a, b = old.splitlines(), new.splitlines()
    return [op for op in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if op[0] != "equal"]


def compute_diff(old: dict[str, str], new: dict[str, str]) -> list[dict]:
    """Which files were added, removed or changed, with counts of lines added and removed (difflib)."""
    entries = []
    for path in sorted(set(old) | set(new)):
        if path not in old:
            entries.append({"path": path, "status": "added", "added": len(new[path].splitlines()), "removed": 0})
        elif path not in new:
            entries.append({"path": path, "status": "removed", "added": 0, "removed": len(old[path].splitlines())})
        elif old[path] != new[path]:
            added = removed = 0
            for tag, i1, i2, j1, j2 in _line_ops(old[path], new[path]):
                removed += i2 - i1
                added += j2 - j1
            entries.append({"path": path, "status": "changed", "added": added, "removed": removed})
    return entries


def changed_lines(old: dict[str, str], new: dict[str, str]) -> list[dict]:
    """For each file that differs: groups of changed lines, each line marked "Added" or "Removed"."""
    out = []
    for entry in compute_diff(old, new):
        a = old.get(entry["path"], "").splitlines()
        b = new.get(entry["path"], "").splitlines()
        groups = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            lines = [("Removed", line) for line in a[i1:i2]] + [("Added", line) for line in b[j1:j2]]
            if j2 > j1:
                where = f"line {j1 + 1}" if j2 - j1 == 1 else f"lines {j1 + 1} to {j2}"
                where = f"New version, {where}"
            else:
                where = f"line {i1 + 1}" if i2 - i1 == 1 else f"lines {i1 + 1} to {i2}"
                where = f"Old version, {where}"
            groups.append({"where": where, "lines": lines})
        out.append({**entry, "groups": groups})
    return out


def describe_diff_entry(entry: dict) -> str:
    if entry["status"] == "added":
        return f"Added {entry['path']}: {_plural(entry['added'], 'line')}."
    if entry["status"] == "removed":
        return f"Removed {entry['path']}: {_plural(entry['removed'], 'line')} gone."
    if entry["added"] == 0 and entry["removed"] == 0:
        return f"Changed {entry['path']}: only line endings changed, no lines added or removed."
    return (f"Changed {entry['path']}: {_plural(entry['added'], 'line')} added, "
            f"{_plural(entry['removed'], 'line')} removed.")


_CARD_RE = re.compile(r"(?:\d[ -]?){12,18}\d")
_SECRET_RES = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(?:my|our)\s+(?:password|passcode|pin|one[- ]time code)\s+is\b", re.I),
    re.compile(r"\b(?:password|passcode|api key)\s*[:=]\s*\S+", re.I),
]


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def looks_like_secret(text: str) -> bool:
    """True if the text seems to contain a password, an API key or a card number."""
    for match in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return any(r.search(text) for r in _SECRET_RES)


SECRET_MESSAGE = ("This looks like it contains a password, a key or a card number. Please take it out. "
                  "This site never needs them and does not store them.")


def _display_title(project: dict) -> str:
    return project["title"] or "Your new project"


def _load_spec(project: dict) -> dict:
    """The stored understanding: spec (normalized), the person's corrections, and how many the AI has read."""
    stored = {"spec": None, "corrections": [], "title_suggested": False}
    if project.get("spec_json"):
        stored.update(json.loads(project["spec_json"]))
    stored["spec"] = normalize_spec(stored["spec"]) if stored["spec"] else None
    # "applied": how many corrections the current spec already includes. A failed re-read leaves the
    # newest ones unread, and the page says so. Older rows without it count every correction as read.
    stored.setdefault("applied", len(stored["corrections"]))
    return stored


def _valid_int(n: int) -> bool:
    return 0 < n <= MAX_SQLITE_INT


def _owned_project(project_id: int, user: dict) -> dict:
    if not _valid_int(project_id):
        raise HTTPException(status_code=404, detail="We could not find that project.")
    with db.session() as conn:
        row = conn.execute("SELECT * FROM build_projects WHERE id = ? AND user_id = ?", (project_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="We could not find that project.")
    return dict(row)


def _get_version(project_id: int, number: int) -> dict:
    if not _valid_int(number):
        raise HTTPException(status_code=404, detail="We could not find that version.")
    with db.session() as conn:
        row = conn.execute("SELECT * FROM build_versions WHERE project_id = ? AND number = ?", (project_id, number)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="We could not find that version.")
    return dict(row)


def _is_busy(project: dict) -> bool:
    """True while an AI job for this project is probably still running."""
    if project["status"] not in BUSY_STATUSES:
        return False
    try:
        since = datetime.fromisoformat(project["updated_at"])
    except (TypeError, ValueError):
        return False
    return datetime.now(timezone.utc) - since < timedelta(minutes=BUSY_MINUTES)


def _versions(project_id: int) -> list[dict]:
    with db.session() as conn:
        rows = conn.execute("SELECT id, number, request_text, ai_summary, checks_json, created_at FROM build_versions"
                            " WHERE project_id = ? ORDER BY number DESC", (project_id,)).fetchall()
    return [dict(r) for r in rows]


def _set_status(project_id: int, status: str, token: str) -> None:
    # The share token is random and unique per project, so a job never touches a newer project that
    # reused the id of one deleted while the job was running.
    with db.session() as conn:
        conn.execute("UPDATE build_projects SET status = ?, updated_at = ? WHERE id = ? AND share_token = ?",
                     (status, db.now(), project_id, token))


def _project_row(project_id: int, token: str) -> dict | None:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM build_projects WHERE id = ? AND share_token = ?",
                           (project_id, token)).fetchone()
    return dict(row) if row else None


DELETED_MESSAGE = "This project was deleted while the AI was working on it, so nothing was saved."


def _check_counts(checks: dict) -> tuple[int, int]:
    errors = warnings = 0
    for page in checks.get("pages", []):
        for f in page["report"]["findings"]:
            if f["severity"] == "error":
                errors += 1
            else:
                warnings += 1
    return errors, warnings


def _found_text(errors: int, warnings: int) -> str:
    parts = []
    if errors:
        parts.append(f"{_plural(errors, 'problem')} to fix")
    if warnings:
        parts.append(_plural(warnings, "warning"))
    return " and ".join(parts) or "no problems"


def _check_sentence(checks: dict) -> str:
    errors, warnings = _check_counts(checks)
    pages = len(checks.get("pages", []))
    found = _found_text(errors, warnings)
    return (f"{_plural(checks.get('file_count', 0), 'file')}, {_size(checks.get('total_bytes', 0))} in total. "
            f"The site's machine checks found {found} on {_plural(pages, 'page')}.")


def _preview_url(project: dict, number: int, path: str = "index.html") -> str:
    return f"/build/p/{project['share_token']}/v/{number}/{quote(path)}"


# ----- preview (shareable, sandboxed) ------------------------------------------------------------

PREVIEW_HEADERS = {
    "Content-Security-Policy": PREVIEW_CSP,
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    # The preview address holds the share token: never pass it on to sites the build links to.
    "Referrer-Policy": "no-referrer",
}


def _serve_preview(token: str, number: int, path: str) -> Response:
    if not _valid_int(number) or len(token) > 100:
        raise HTTPException(status_code=404, detail="We could not find that preview.")
    with db.session() as conn:
        row = conn.execute(
            "SELECT v.files_json FROM build_versions v JOIN build_projects p ON p.id = v.project_id"
            " WHERE p.share_token = ? AND v.number = ?", (token, number)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="We could not find that preview.")
    files = json.loads(row["files_json"])
    if path not in files:
        raise HTTPException(status_code=404, detail="There is no file with that name in this preview.")
    return Response(files[path].encode("utf-8"), media_type=CONTENT_TYPES.get(_ext(path), "text/plain; charset=utf-8"),
                    headers=PREVIEW_HEADERS)


@router.get("/p/{token}/v/{number:int}/")
def preview_index(token: str, number: int):
    return _serve_preview(token, number, "index.html")


@router.get("/p/{token}/v/{number:int}/{path:path}")
def preview_file(token: str, number: int, path: str):
    return _serve_preview(token, number, path)


# ----- index and new project -----------------------------------------------------------------

def _form_text(form: dict, name: str) -> str:
    """One text value from a form. A repeated field counts once; anything that is not text is empty."""
    value = form.get(name, "")
    if isinstance(value, list):
        value = value[0] if value else ""
    return value.strip() if isinstance(value, str) else ""


def _index(request: Request, *, title: str = INDEX_TITLE, values: dict | None = None,
           errors: dict | None = None, status_code: int = 200):
    user = current_user(request)
    projects = []
    if user:
        with db.session() as conn:
            rows = conn.execute(
                "SELECT p.*, (SELECT MAX(number) FROM build_versions v WHERE v.project_id = p.id) AS latest"
                " FROM build_projects p WHERE p.user_id = ? ORDER BY p.updated_at DESC, p.id DESC", (user["id"],)).fetchall()
        for r in rows:
            p = dict(r)
            p["display_title"] = _display_title(p)
            p["kind_label"] = KIND_LABELS.get(p["kind"], "Something else")
            p["status_label"] = _status_label(p)
            projects.append(p)
    return render(request, "build/index.html", title=title, projects=projects, kinds=KINDS, idea_hint=IDEA_HINT,
                  values=values or {}, errors=errors or {}, status_code=status_code)


def _status_label(project: dict) -> str:
    if project["status"] in BUSY_STATUSES and not _is_busy(project):
        # The job stopped without saying so (for example the server restarted). Do not claim it is working.
        return "The AI stopped before it finished, so you can try again"
    return STATUS_LABELS.get(project["status"], project["status"])


@router.get("")
def index(request: Request):
    deleted = request.session.pop("build_deleted", None)
    if deleted and current_user(request):
        return _index(request, title=f"Deleted {deleted} and all its versions")
    return _index(request)


def _seconds_since(stamp: str) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


@router.post("/new")
async def new_project(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    values = {k: _form_text(form, k) for k in ("title", "kind", "idea")}
    errors: dict[str, str] = {}
    if len(values["title"]) > MAX_TITLE:
        errors["title"] = (f"Your title is {len(values['title'])} characters. "
                           f"Use a title of at most {MAX_TITLE} characters.")
    elif looks_like_secret(values["title"]):
        errors["title"] = SECRET_MESSAGE
    if values["kind"] not in KIND_LABELS:
        errors["kind"] = "Choose what you want to make."
    if not values["idea"]:
        errors["idea"] = "Describe your idea in your own words."
    elif len(values["idea"]) > MAX_IDEA:
        errors["idea"] = f"Your idea is {len(values['idea'])} characters. Please keep it under {MAX_IDEA}."
    elif looks_like_secret(values["idea"]):
        errors["idea"] = SECRET_MESSAGE
    if errors:
        return _index(request, title=f"{INDEX_TITLE}: please fix the problems", values=values, errors=errors,
                      status_code=400)
    with db.session() as conn:
        same = conn.execute(
            "SELECT id, created_at FROM build_projects WHERE user_id = ? AND kind = ? AND idea = ?"
            " ORDER BY id DESC LIMIT 1", (user["id"], values["kind"], values["idea"])).fetchone()
    if same is not None and _seconds_since(same["created_at"]) < DUPLICATE_SECONDS:
        # A double press of the button: keep one project and one AI job.
        flash(request, "You sent this idea twice, so it is saved once. This is the project.", "info")
        return redirect(f"/build/{same['id']}")
    now = db.now()
    token = secrets.token_urlsafe(16)
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO build_projects (user_id, title, kind, idea, status, spec_json, share_token, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'new', NULL, ?, ?, ?)",
            (user["id"], values["title"], values["kind"], values["idea"], token, now, now),
        )
        project_id = cur.lastrowid
    try:
        ai = get_ai(request, user)
    except AINotAvailable as exc:
        flash(request, f"{exc.user_message} Your idea is saved here.", "error")
        return redirect(f"/build/{project_id}")
    _set_status(project_id, "understanding", token)
    title = values["title"] or "your new idea"
    job_id = jobs.submit(user["id"], "build.understand", f"Reading {title}", _understand, ai, project_id, token)
    return redirect(jobs.job_url(job_id))


# ----- jobs ------------------------------------------------------------------------------------
# Each job gets the project's id and its share token, and touches the project only if both still match.

def _understand(ctx, ai, project_id: int, token: str) -> str:
    project = _project_row(project_id, token)
    if project is None:
        raise AIError(DELETED_MESSAGE)
    stored = _load_spec(project)
    had_spec = bool(stored["spec"])
    try:
        ctx.progress("Reading your idea")
        prompt = (f"What they want to make: {KIND_LABELS.get(project['kind'], 'Something else')}\n"
                  f"Title: {project['title'] or '(none given; suggest one)'}\n\n"
                  f"Their idea, in their own words:\n<idea>\n{project['idea']}\n</idea>\n")
        if stored["spec"]:
            prompt += "\nWhat you understood last time (JSON):\n" + json.dumps(stored["spec"], indent=2) + "\n"
        if stored["corrections"]:
            prompt += ("\nCorrections, answers and more details from the person, oldest first. "
                       "Follow the newest when they disagree:\n" +
                       "\n".join(f"<correction>\n{c}\n</correction>" for c in stored["corrections"]) +
                       "\n\nSay back what you understand now, with the corrections applied.")
        raw = ai.structured(system=UNDERSTAND_SYSTEM, prompt=prompt, schema=UNDERSTAND_SCHEMA,
                            purpose="build.understand")
        spec = normalize_spec(raw)
        if spec is None or not spec["summary"]:
            raise AIError("The AI's answer was missing its summary of your idea, so nothing was changed. "
                          "Please try again.")
        stored["spec"] = spec
        stored["applied"] = len(stored["corrections"])
        title = project["title"]
        if not title:
            title = spec["suggested_title"] or "Untitled project"
            stored["title_suggested"] = True
        with db.session() as conn:
            cur = conn.execute("UPDATE build_projects SET spec_json = ?, title = ?, status = 'understood',"
                               " updated_at = ? WHERE id = ? AND share_token = ?",
                               (json.dumps(stored), title, db.now(), project_id, token))
            if cur.rowcount == 0:
                raise AIError(DELETED_MESSAGE)
    except BaseException:
        _set_status(project_id, "understood" if had_spec else "new", token)
        raise
    return f"/build/{project_id}"


def _files_result(result: object, nothing_saved: str) -> tuple[dict[str, str], dict]:
    """The AI's files, checked against the site's rules, plus its other answers as clean text."""
    if not isinstance(result, dict):
        raise AIError(f"The AI's answer was not in the expected form, so {nothing_saved} Please try again.")
    try:
        files = validate_files(result.get("files"))
    except BuildFilesError as exc:
        raise AIError(f"The AI made files the site cannot accept, so {nothing_saved} {exc} Please try again. "
                      "If it happens again, ask for something smaller.") from exc
    about = {"summary": _text(result.get("summary")), "how_to_use": _text(result.get("how_to_use")),
             "not_done": _texts(result.get("not_done"))}
    return files, about


def _save_version(project_id: int, token: str, files: dict[str, str], about: dict, request_text: str,
                  base: dict[str, str], base_number: int) -> int:
    checks = run_checks(files)
    checks["not_done"] = about["not_done"]
    diff = {"base": base_number, "files": compute_diff(base, files)}
    with db.session() as conn:
        # Take the write lock first, so two jobs can never pick the same version number.
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM build_projects WHERE id = ? AND share_token = ?",
                        (project_id, token)).fetchone() is None:
            raise AIError(DELETED_MESSAGE)
        number = conn.execute("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM build_versions WHERE project_id = ?",
                              (project_id,)).fetchone()["n"]
        if base_number == 0 and number != 1:
            raise AIError("This project was already built by an earlier request, so this second copy was not "
                          "saved. Open the project to see the version that was built.")
        conn.execute(
            "INSERT INTO build_versions (project_id, number, files_json, request_text, ai_summary, how_to_use,"
            " diff_json, checks_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, number, json.dumps(files), request_text, about["summary"], about["how_to_use"],
             json.dumps(diff), json.dumps(checks), db.now()),
        )
        conn.execute("UPDATE build_projects SET status = 'ready', updated_at = ? WHERE id = ?", (db.now(), project_id))
    return number


def _generate(ctx, ai, project_id: int, token: str, additions: list[str]) -> str:
    project = _project_row(project_id, token)
    if project is None:
        raise AIError(DELETED_MESSAGE)
    stored = _load_spec(project)
    try:
        if _versions(project_id):
            raise AIError("This project is already built. To change it, use Ask for a change on the version page.")
        ctx.progress("Writing the files")
        spec = {k: v for k, v in (stored["spec"] or {}).items() if k not in ("additions_not_asked", "suggested_title")}
        prompt = (f"What to build: {KIND_LABELS.get(project['kind'], 'Something else')}\n"
                  f"Title: {project['title']}\n\n"
                  f"The person's idea, in their own words:\n<idea>\n{project['idea']}\n</idea>\n\n"
                  "What you understood, which the person approved (JSON). If a question was not answered, "
                  "choose the simplest sensible option and list your guess in not_done:\n"
                  + json.dumps(spec, indent=2) + "\n\n")
        if additions:
            prompt += "Extra things the person chose to add:\n" + "\n".join(f"- {a}" for a in additions) + "\n\n"
        else:
            prompt += "The person chose no extras. Do not add things they did not ask for.\n\n"
        if stored["corrections"]:
            prompt += ("Corrections and answers from the person, oldest first:\n" +
                       "\n".join(f"<correction>\n{c}\n</correction>" for c in stored["corrections"]) + "\n\n")
        system = GENERATE_SYSTEM + ("\n\n" + PHONE_APP_RULES if project["kind"] == "phone_app" else "")
        result = ai.structured(system=system, prompt=prompt, schema=FILES_SCHEMA, purpose="build.generate",
                               max_tokens=64000)
        ctx.progress("Checking the files")
        files, about = _files_result(result, "nothing was saved.")
        number = _save_version(project_id, token, files, about, "First version", {}, 0)
    except BaseException:
        _set_status(project_id, "ready" if _versions(project_id) else "understood", token)
        raise
    return f"/build/{project_id}/v/{number}"


def _change(ctx, ai, project_id: int, token: str, base_number: int, request_text: str) -> str:
    project = _project_row(project_id, token)
    if project is None:
        raise AIError(DELETED_MESSAGE)
    try:
        base = json.loads(_get_version(project_id, base_number)["files_json"])
        ctx.progress("Making the change")
        file_text = "\n\n".join(f'<file path="{p}">\n{c}\n</file>' for p, c in sorted(base.items()))
        prompt = (f"What it is: {KIND_LABELS.get(project['kind'], 'Something else')}\n"
                  f"Title: {project['title']}\n\n"
                  f"The current files (version {base_number}):\n\n{file_text}\n\n"
                  f"What the person asks you to change:\n<request>\n{request_text}\n</request>\n")
        system = CHANGE_SYSTEM + ("\n\n" + PHONE_APP_RULES if project["kind"] == "phone_app" else "")
        result = ai.structured(system=system, prompt=prompt, schema=FILES_SCHEMA, purpose="build.change",
                               max_tokens=64000)
        ctx.progress("Checking the files and working out what changed")
        files, about = _files_result(result, f"nothing was saved and version {base_number} is unchanged.")
        number = _save_version(project_id, token, files, about, request_text, base, base_number)
    except BaseException:
        _set_status(project_id, "ready", token)
        raise
    return f"/build/{project_id}/v/{number}"


# ----- project page ----------------------------------------------------------------------------

AI_NOT_AVAILABLE_TITLE = "the AI is not available, so nothing was sent to it"


def _project_page(request: Request, user: dict, project: dict, *, values: dict | None = None,
                  errors: dict | None = None, status_code: int = 200, note: str = ""):
    """The project's own page. `note` replaces the usual end of the title, to say what just happened."""
    stored = _load_spec(project)
    versions = _versions(project["id"])
    title_text = _display_title(project)
    busy = _is_busy(project)
    ctx = dict(project=project, stored=stored, spec=stored["spec"], versions=versions, busy=busy,
               unread_corrections=len(stored["corrections"]) - stored["applied"],
               kind_label=KIND_LABELS.get(project["kind"], "Something else"),
               status_label=_status_label(project), values=values or {}, errors=errors or {})
    if versions:
        latest = versions[0]
        checks = json.loads(latest["checks_json"])
        ctx.update(latest=latest, latest_check=_check_sentence(checks),
                   preview_url=_preview_url(project, latest["number"]))
        template = "build/project.html"
        about = ("the AI is making a change" if busy and project["status"] == "changing"
                 else "your project and its versions")
    elif stored["spec"]:
        template = "build/understood.html"
        if busy and project["status"] == "building":
            about = "the AI is building it"
        elif busy and project["status"] == "understanding":
            about = "the AI is reading your corrections"
        else:
            about = "here is what I understood"
    else:
        template = "build/waiting.html"
        about = "the AI is reading your idea" if busy else "the AI has not read your idea yet"
    title = f"{title_text}: {note or about}"
    if errors:
        title += ": please fix the problems"
    return render(request, template, title=title, status_code=status_code, **ctx)


@router.get("/{project_id:int}")
def project_page(request: Request, project_id: int, user: dict = Depends(require_user)):
    return _project_page(request, user, _owned_project(project_id, user))


@router.post("/{project_id:int}/understand")
async def understand(request: Request, project_id: int, user: dict = Depends(require_user)):
    form = await checked_form(request)
    project = _owned_project(project_id, user)
    action = "build" if _form_text(form, "action") == "build" else "update"
    corrections = _form_text(form, "corrections")
    stored = _load_spec(project)
    if _versions(project_id):
        flash(request, "This project is already built. To change it, use Ask for a change on the version page.", "info")
        return redirect(f"/build/{project_id}")
    if _is_busy(project):
        flash(request, BUSY_MESSAGE, "info")
        return redirect(f"/build/{project_id}")
    if action == "build" and not stored["spec"]:
        return redirect(f"/build/{project_id}")
    unread = len(stored["corrections"]) > stored["applied"]
    errors: dict[str, str] = {}
    if len(corrections) > MAX_REQUEST:
        errors["corrections"] = (f"This is {len(corrections)} characters. "
                                 f"Please keep it under {MAX_REQUEST} characters.")
    elif corrections and looks_like_secret(corrections):
        errors["corrections"] = SECRET_MESSAGE
    elif corrections and sum(map(len, stored["corrections"])) + len(corrections) > MAX_CORRECTIONS_TOTAL:
        errors["corrections"] = (f"All your corrections on this project together would be more than "
                                 f"{MAX_CORRECTIONS_TOTAL} characters, which is the limit. Start a new project "
                                 "and write your idea again with the corrections included.")
    elif action == "update" and stored["spec"] and not corrections and not unread:
        errors["corrections"] = "Write your corrections or answers first, or choose This is right: build it."
    if errors:
        return _project_page(request, user, project, values={"corrections": corrections}, errors=errors,
                             status_code=400)
    try:
        ai = get_ai(request, user)
    except AINotAvailable as exc:
        flash(request, exc.user_message, "error")
        return _project_page(request, user, project, values={"corrections": corrections},
                             note=AI_NOT_AVAILABLE_TITLE)
    if corrections:
        # The person's words are kept with the project, whether they lead to a new read-back or the build.
        stored["corrections"].append(corrections)
        with db.session() as conn:
            conn.execute("UPDATE build_projects SET spec_json = ?, updated_at = ? WHERE id = ?",
                         (json.dumps(stored), db.now(), project_id))
    token = project["share_token"]
    if action == "build":
        chosen = [a for i, a in enumerate(stored["spec"]["additions_not_asked"]) if form.get(f"add_{i}") == "yes"]
        _set_status(project_id, "building", token)
        job_id = jobs.submit(user["id"], "build.generate", f"Building {_display_title(project)}", _generate, ai,
                             project_id, token, chosen)
        return redirect(jobs.job_url(job_id))
    _set_status(project_id, "understanding", token)
    job_id = jobs.submit(user["id"], "build.understand", f"Reading {_display_title(project)}", _understand, ai,
                         project_id, token)
    return redirect(jobs.job_url(job_id))


# ----- versions -------------------------------------------------------------------------------

def _version_context(project: dict, version: dict) -> dict:
    files = json.loads(version["files_json"])
    checks = json.loads(version["checks_json"])
    diff = json.loads(version["diff_json"]) or {}
    pages = []
    for page in checks.get("pages", []):
        findings = page["report"]["findings"]
        pages.append({
            "found": _found_text(sum(f["severity"] == "error" for f in findings),
                                 sum(f["severity"] != "error" for f in findings)),
            "path": page["path"],
            "size": _size(page["bytes"]),
            "passed": _plural(len(page["report"]["passed"]), "check"),
            "errors": [f for f in findings if f["severity"] == "error"],
            "warnings": [f for f in findings if f["severity"] != "error"],
        })
    not_checked: list[str] = []
    for page in checks.get("pages", []):
        for item in page["report"]["not_checked"]:
            if item not in not_checked:
                not_checked.append(item)
    not_checked += SITE_NOT_CHECKED
    unchecked_files = [p for p in sorted(files) if _ext(p) != "html"]
    errors, warnings = _check_counts(checks)
    diff_entries = diff.get("files", []) if version["number"] > 1 else []
    return dict(
        files=files, checks=checks, pages=pages, not_checked=not_checked, unchecked_files=unchecked_files,
        not_done=checks.get("not_done", []), error_count=errors, warning_count=warnings,
        check_sentence=_check_sentence(checks),
        file_summary=f"{_plural(checks.get('file_count', 0), 'file')}, {_size(checks.get('total_bytes', 0))} in total.",
        diff_base=diff.get("base", 0), diff_lines=[describe_diff_entry(e) for e in diff_entries],
        diff_count=len(diff_entries),
    )


def _version_page(request: Request, project: dict, version: dict, *, values: dict | None = None,
                  errors: dict | None = None, status_code: int = 200, note: str = ""):
    ctx = _version_context(project, version)
    settings = request.app.state.settings
    preview = _preview_url(project, version["number"])
    title_text = _display_title(project)
    checks_link = "/checks/new?" + urlencode({"url": settings.base_url + preview,
                                              "title": f"{title_text}, version {version['number']}"})
    title = f"{title_text}: version {version['number']} {note or 'is ready'}"
    if errors:
        title += ": please fix the problems"
    latest = _versions(project["id"])[0]["number"]
    return render(request, "build/version.html", title=title, project=project, version=version,
                  preview_url=preview, checks_link=checks_link, latest_number=latest,
                  values=values or {}, errors=errors or {}, status_code=status_code, **ctx)


@router.get("/{project_id:int}/v/{number:int}")
def version_page(request: Request, project_id: int, number: int, user: dict = Depends(require_user)):
    project = _owned_project(project_id, user)
    return _version_page(request, project, _get_version(project_id, number))


@router.get("/{project_id:int}/v/{number:int}/files")
def files_page(request: Request, project_id: int, number: int, user: dict = Depends(require_user)):
    project = _owned_project(project_id, user)
    version = _get_version(project_id, number)
    files = json.loads(version["files_json"])
    listing = [{"path": p, "size": _size(len(c.encode("utf-8"))), "lines": _plural(len(c.splitlines()), "line"),
                "url": f"/build/{project_id}/v/{number}/files/{quote(p)}"} for p, c in sorted(files.items())]
    return render(request, "build/files.html", title=f"{_display_title(project)}: the files in version {number}",
                  project=project, version=version, listing=listing)


@router.get("/{project_id:int}/v/{number:int}/files/{path:path}")
def file_page(request: Request, project_id: int, number: int, path: str, user: dict = Depends(require_user)):
    project = _owned_project(project_id, user)
    version = _get_version(project_id, number)
    files = json.loads(version["files_json"])
    if path not in files:
        raise HTTPException(status_code=404, detail="There is no file with that name in this version.")
    content = files[path]
    return render(request, "build/file.html", title=f"{_display_title(project)}: {path} in version {number}",
                  project=project, version=version, path=path, content=content,
                  size=_size(len(content.encode("utf-8"))), lines=_plural(len(content.splitlines()), "line"))


@router.get("/{project_id:int}/v/{number:int}/changes")
def changes_page(request: Request, project_id: int, number: int, user: dict = Depends(require_user)):
    project = _owned_project(project_id, user)
    version = _get_version(project_id, number)
    diff = json.loads(version["diff_json"]) or {}
    base_number = diff.get("base", 0)
    groups: list[dict] = []
    if number > 1 and base_number:
        base = json.loads(_get_version(project_id, base_number)["files_json"])
        groups = changed_lines(base, json.loads(version["files_json"]))
        for g in groups:
            g["sentence"] = describe_diff_entry(g)
    return render(request, "build/changes.html",
                  title=f"{_display_title(project)}: the changed lines in version {number}",
                  project=project, version=version, base_number=base_number, groups=groups)


@router.get("/{project_id:int}/v/{number:int}/download.zip")
def download_zip(request: Request, project_id: int, number: int, user: dict = Depends(require_user)):
    project = _owned_project(project_id, user)
    version = _get_version(project_id, number)
    files = json.loads(version["files_json"])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in sorted(files.items()):
            zf.writestr(path, content)
    slug = re.sub(r"[^a-z0-9]+", "-", project["title"].lower()).strip("-")[:60].strip("-") or "project"
    return Response(buffer.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{slug}-version-{number}.zip"'})


async def _submit_change(request: Request, user: dict, project: dict, version: dict, request_text: str):
    if _is_busy(project):
        flash(request, BUSY_MESSAGE, "info")
        return redirect(f"/build/{project['id']}")
    try:
        ai = get_ai(request, user)
    except AINotAvailable as exc:
        flash(request, exc.user_message, "error")
        return _version_page(request, project, version, values={"request": request_text},
                             note=f"is unchanged: {AI_NOT_AVAILABLE_TITLE}")
    _set_status(project["id"], "changing", project["share_token"])
    job_id = jobs.submit(user["id"], "build.change", f"Changing {_display_title(project)}", _change, ai,
                         project["id"], project["share_token"], version["number"], request_text)
    return redirect(jobs.job_url(job_id))


@router.post("/{project_id:int}/v/{number:int}/change")
async def ask_for_change(request: Request, project_id: int, number: int, user: dict = Depends(require_user)):
    form = await checked_form(request)
    project = _owned_project(project_id, user)
    version = _get_version(project_id, number)
    text = _form_text(form, "request")
    error = ""
    if not text:
        error = "Describe the change you want."
    elif len(text) > MAX_REQUEST:
        error = f"Your request is {len(text)} characters. Please keep it under {MAX_REQUEST} characters."
    elif looks_like_secret(text):
        error = SECRET_MESSAGE
    if error:
        return _version_page(request, project, version, values={"request": text}, errors={"request": error},
                             status_code=400)
    return await _submit_change(request, user, project, version, text)


def fix_request_text(checks: dict) -> str:
    """The request the site writes for "Fix the problems the checks found"."""
    lines = ["Fix these problems that the site's accessibility checks found. Do not change anything else."]
    for page in checks.get("pages", []):
        findings = page["report"]["findings"]
        if not findings:
            continue
        lines.append("")
        lines.append(f"In {page['path']}:")
        for f in findings:
            label = "Problem" if f["severity"] == "error" else "Warning"
            where = f" ({f['where']})" if f.get("where") else ""
            lines.append(f"- {label}: {f['message']}{where}")
    return "\n".join(lines)


@router.post("/{project_id:int}/v/{number:int}/fix")
async def fix_problems(request: Request, project_id: int, number: int, user: dict = Depends(require_user)):
    await checked_form(request)
    project = _owned_project(project_id, user)
    version = _get_version(project_id, number)
    checks = json.loads(version["checks_json"])
    if _check_counts(checks)[0] == 0:
        flash(request, "The checks found no problems to fix in this version.", "info")
        return redirect(f"/build/{project_id}/v/{number}")
    return await _submit_change(request, user, project, version, fix_request_text(checks))


# ----- delete ---------------------------------------------------------------------------------

@router.get("/{project_id:int}/delete")
def delete_form(request: Request, project_id: int, user: dict = Depends(require_user)):
    project = _owned_project(project_id, user)
    return render(request, "build/delete.html", title=f"Delete {_display_title(project)}", project=project,
                  version_count=len(_versions(project_id)))


@router.post("/{project_id:int}/delete")
async def delete_project(request: Request, project_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    project = _owned_project(project_id, user)
    with db.session() as conn:
        conn.execute("DELETE FROM build_versions WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM build_projects WHERE id = ? AND user_id = ?", (project_id, user["id"]))
    # The next page's heading says what happened (read once, like a flash message).
    request.session["build_deleted"] = _display_title(project)
    return redirect("/build")


def forget_user(user_id: int) -> None:
    """Called when a member deletes their account: remove their projects (versions cascade)."""
    with db.session() as conn:
        conn.execute("DELETE FROM build_projects WHERE user_id = ?", (user_id,))
