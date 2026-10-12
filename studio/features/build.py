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
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise BuildFilesError(f'The file "{path}" is {_size(size)}. The limit is {_size(MAX_FILE_BYTES)} per file.')
        total += size
        out[path] = content
    if total > MAX_TOTAL_BYTES:
        raise BuildFilesError(f"All files together are {_size(total)}. The limit is {_size(MAX_TOTAL_BYTES)}.")
    if "index.html" not in out:
        raise BuildFilesError("There was no index.html, the page that opens first.")
    return out


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
    stored = {"spec": None, "corrections": [], "title_suggested": False}
    if project.get("spec_json"):
        stored.update(json.loads(project["spec_json"]))
    return stored


def _owned_project(project_id: int, user: dict) -> dict:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM build_projects WHERE id = ? AND user_id = ?", (project_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="We could not find that project.")
    return dict(row)


def _get_version(project_id: int, number: int) -> dict:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM build_versions WHERE project_id = ? AND number = ?", (project_id, number)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="We could not find that version.")
    return dict(row)


def _versions(project_id: int) -> list[dict]:
    with db.session() as conn:
        rows = conn.execute("SELECT id, number, request_text, ai_summary, checks_json, created_at FROM build_versions"
                            " WHERE project_id = ? ORDER BY number DESC", (project_id,)).fetchall()
    return [dict(r) for r in rows]


def _set_status(project_id: int, status: str) -> None:
    with db.session() as conn:
        conn.execute("UPDATE build_projects SET status = ?, updated_at = ? WHERE id = ?", (status, db.now(), project_id))


def _project_row(project_id: int) -> dict | None:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM build_projects WHERE id = ?", (project_id,)).fetchone()
    return dict(row) if row else None


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

def _serve_preview(token: str, number: int, path: str) -> Response:
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
                    headers={"Content-Security-Policy": PREVIEW_CSP, "Cache-Control": "no-store"})


@router.get("/p/{token}/v/{number:int}/")
def preview_index(token: str, number: int):
    return _serve_preview(token, number, "index.html")


@router.get("/p/{token}/v/{number:int}/{path:path}")
def preview_file(token: str, number: int, path: str):
    return _serve_preview(token, number, path)


# ----- index and new project -----------------------------------------------------------------

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
            p["status_label"] = STATUS_LABELS.get(p["status"], p["status"])
            projects.append(p)
    return render(request, "build/index.html", title=title, projects=projects, kinds=KINDS, idea_hint=IDEA_HINT,
                  values=values or {}, errors=errors or {}, status_code=status_code)


@router.get("")
def index(request: Request):
    return _index(request)


@router.post("/new")
async def new_project(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    values = {k: str(form.get(k, "")).strip() for k in ("title", "kind", "idea")}
    errors: dict[str, str] = {}
    if len(values["title"]) > MAX_TITLE:
        errors["title"] = f"Use a title of at most {MAX_TITLE} characters."
    if values["kind"] not in KIND_LABELS:
        errors["kind"] = "Choose what you want to make."
    if not values["idea"]:
        errors["idea"] = "Describe your idea in your own words."
    elif len(values["idea"]) > MAX_IDEA:
        errors["idea"] = f"Your idea is {len(values['idea'])} characters. Please keep it under {MAX_IDEA}."
    elif looks_like_secret(values["idea"]):
        errors["idea"] = SECRET_MESSAGE
    if not errors and looks_like_secret(values["title"]):
        errors["title"] = SECRET_MESSAGE
    if errors:
        return _index(request, title=f"{INDEX_TITLE}: please fix the problems", values=values, errors=errors,
                      status_code=400)
    now = db.now()
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO build_projects (user_id, title, kind, idea, status, spec_json, share_token, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'new', NULL, ?, ?, ?)",
            (user["id"], values["title"], values["kind"], values["idea"], secrets.token_urlsafe(16), now, now),
        )
        project_id = cur.lastrowid
    try:
        ai = get_ai(request, user)
    except AINotAvailable as exc:
        flash(request, f"{exc.user_message} Your idea is saved here.", "error")
        return redirect(f"/build/{project_id}")
    _set_status(project_id, "understanding")
    title = values["title"] or "your new idea"
    job_id = jobs.submit(user["id"], "build.understand", f"Reading {title}", _understand, ai, project_id)
    return redirect(jobs.job_url(job_id))


# ----- jobs ------------------------------------------------------------------------------------

def _understand(ctx, ai, project_id: int) -> str:
    project = _project_row(project_id)
    if project is None:
        raise AIError("This project was deleted.")
    stored = _load_spec(project)
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
        spec = ai.structured(system=UNDERSTAND_SYSTEM, prompt=prompt, schema=UNDERSTAND_SCHEMA,
                             purpose="build.understand")
        stored["spec"] = spec
        title = project["title"]
        if not title:
            title = (spec.get("suggested_title") or "").strip()[:MAX_TITLE] or "Untitled project"
            stored["title_suggested"] = True
        with db.session() as conn:
            conn.execute("UPDATE build_projects SET spec_json = ?, title = ?, status = 'understood', updated_at = ?"
                         " WHERE id = ?", (json.dumps(stored), title, db.now(), project_id))
    except BaseException:
        _set_status(project_id, "understood" if stored["spec"] else "new")
        raise
    return f"/build/{project_id}"


def _save_version(project_id: int, files: dict[str, str], result: dict, request_text: str,
                  base: dict[str, str], base_number: int) -> int:
    checks = run_checks(files)
    diff = {"base": base_number, "files": compute_diff(base, files)}
    not_done = [str(x) for x in result.get("not_done") or [] if str(x).strip()]
    with db.session() as conn:
        row = conn.execute("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM build_versions WHERE project_id = ?",
                           (project_id,)).fetchone()
        number = row["n"]
        checks["not_done"] = not_done
        conn.execute(
            "INSERT INTO build_versions (project_id, number, files_json, request_text, ai_summary, how_to_use,"
            " diff_json, checks_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, number, json.dumps(files), request_text, str(result.get("summary", "")),
             str(result.get("how_to_use", "")), json.dumps(diff), json.dumps(checks), db.now()),
        )
        conn.execute("UPDATE build_projects SET status = 'ready', updated_at = ? WHERE id = ?", (db.now(), project_id))
    return number


def _generate(ctx, ai, project_id: int, additions: list[str], notes: str) -> str:
    project = _project_row(project_id)
    if project is None:
        raise AIError("This project was deleted.")
    stored = _load_spec(project)
    try:
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
        all_notes = list(stored["corrections"])
        if notes:
            all_notes.append(notes)
        if all_notes:
            prompt += ("Corrections and answers from the person, oldest first:\n" +
                       "\n".join(f"<correction>\n{c}\n</correction>" for c in all_notes) + "\n\n")
        system = GENERATE_SYSTEM + ("\n\n" + PHONE_APP_RULES if project["kind"] == "phone_app" else "")
        result = ai.structured(system=system, prompt=prompt, schema=FILES_SCHEMA, purpose="build.generate",
                               max_tokens=64000)
        ctx.progress("Checking the files")
        try:
            files = validate_files(result.get("files"))
        except BuildFilesError as exc:
            raise AIError(f"The AI made files the site cannot accept, so nothing was saved. {exc} "
                          "Please try again. If it happens again, ask for something smaller.") from exc
        number = _save_version(project_id, files, result, "First version", {}, 0)
    except BaseException:
        _set_status(project_id, "understood")
        raise
    return f"/build/{project_id}/v/{number}"


def _change(ctx, ai, project_id: int, base_number: int, request_text: str) -> str:
    project = _project_row(project_id)
    if project is None:
        raise AIError("This project was deleted.")
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
        try:
            files = validate_files(result.get("files"))
        except BuildFilesError as exc:
            raise AIError(f"The AI made files the site cannot accept, so nothing was saved and version "
                          f"{base_number} is unchanged. {exc} Please try again.") from exc
        number = _save_version(project_id, files, result, request_text, base, base_number)
    except BaseException:
        _set_status(project_id, "ready")
        raise
    return f"/build/{project_id}/v/{number}"


# ----- project page ----------------------------------------------------------------------------

def _project_page(request: Request, user: dict, project: dict, *, values: dict | None = None,
                  errors: dict | None = None, status_code: int = 200):
    stored = _load_spec(project)
    versions = _versions(project["id"])
    title_text = _display_title(project)
    ctx = dict(project=project, stored=stored, spec=stored["spec"], versions=versions,
               kind_label=KIND_LABELS.get(project["kind"], "Something else"),
               status_label=STATUS_LABELS.get(project["status"], project["status"]),
               values=values or {}, errors=errors or {})
    if versions:
        latest = versions[0]
        checks = json.loads(latest["checks_json"])
        ctx.update(latest=latest, latest_check=_check_sentence(checks),
                   preview_url=_preview_url(project, latest["number"]))
        return render(request, "build/project.html", title=f"{title_text}: your project and its versions",
                      status_code=status_code, **ctx)
    if stored["spec"]:
        title = f"{title_text}: here is what I understood"
        if errors:
            title += ": please fix the problems"
        return render(request, "build/understood.html", title=title, status_code=status_code, **ctx)
    if project["status"] == "understanding":
        title = f"{title_text}: the AI is reading your idea"
    else:
        title = f"{title_text}: the AI has not read your idea yet"
    return render(request, "build/waiting.html", title=title, status_code=status_code, **ctx)


@router.get("/{project_id:int}")
def project_page(request: Request, project_id: int, user: dict = Depends(require_user)):
    return _project_page(request, user, _owned_project(project_id, user))


@router.post("/{project_id:int}/understand")
async def understand(request: Request, project_id: int, user: dict = Depends(require_user)):
    form = await checked_form(request)
    project = _owned_project(project_id, user)
    action = str(form.get("action", "update"))
    corrections = str(form.get("corrections", "")).strip()
    stored = _load_spec(project)
    if _versions(project_id):
        flash(request, "This project is already built. To change it, use Ask for a change on the version page.", "info")
        return redirect(f"/build/{project_id}")
    errors: dict[str, str] = {}
    if len(corrections) > MAX_REQUEST:
        errors["corrections"] = f"Please keep this under {MAX_REQUEST} characters."
    elif corrections and looks_like_secret(corrections):
        errors["corrections"] = SECRET_MESSAGE
    elif action == "update" and stored["spec"] and not corrections:
        errors["corrections"] = "Write your corrections or answers first, or choose This is right: build it."
    if action == "build" and not stored["spec"]:
        return redirect(f"/build/{project_id}")
    if errors:
        return _project_page(request, user, project, values={"corrections": corrections}, errors=errors,
                             status_code=400)
    try:
        ai = get_ai(request, user)
    except AINotAvailable as exc:
        flash(request, exc.user_message, "error")
        return _project_page(request, user, project, values={"corrections": corrections})
    if action == "build":
        chosen = [a for i, a in enumerate(stored["spec"].get("additions_not_asked") or [])
                  if form.get(f"add_{i}") == "yes"]
        _set_status(project_id, "building")
        job_id = jobs.submit(user["id"], "build.generate", f"Building {project['title']}", _generate, ai,
                             project_id, chosen, corrections)
        return redirect(jobs.job_url(job_id))
    if corrections:
        stored["corrections"].append(corrections)
        with db.session() as conn:
            conn.execute("UPDATE build_projects SET spec_json = ?, updated_at = ? WHERE id = ?",
                         (json.dumps(stored), db.now(), project_id))
    _set_status(project_id, "understanding")
    job_id = jobs.submit(user["id"], "build.understand", f"Reading {_display_title(project)}", _understand, ai,
                         project_id)
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
            "passed": len(page["report"]["passed"]),
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
                  errors: dict | None = None, status_code: int = 200):
    ctx = _version_context(project, version)
    settings = request.app.state.settings
    preview = _preview_url(project, version["number"])
    title_text = _display_title(project)
    checks_link = "/checks/new?" + urlencode({"url": settings.base_url + preview,
                                              "title": f"{title_text}, version {version['number']}"})
    title = f"{title_text}: version {version['number']} is ready"
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
    slug = re.sub(r"[^a-z0-9]+", "-", project["title"].lower()).strip("-") or "project"
    return Response(buffer.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{slug}-version-{number}.zip"'})


async def _submit_change(request: Request, user: dict, project: dict, version: dict, request_text: str):
    try:
        ai = get_ai(request, user)
    except AINotAvailable as exc:
        flash(request, exc.user_message, "error")
        return _version_page(request, project, version, values={"request": request_text})
    _set_status(project["id"], "changing")
    job_id = jobs.submit(user["id"], "build.change", f"Changing {project['title']}", _change, ai, project["id"],
                         version["number"], request_text)
    return redirect(jobs.job_url(job_id))


@router.post("/{project_id:int}/v/{number:int}/change")
async def ask_for_change(request: Request, project_id: int, number: int, user: dict = Depends(require_user)):
    form = await checked_form(request)
    project = _owned_project(project_id, user)
    version = _get_version(project_id, number)
    text = str(form.get("request", "")).strip()
    error = ""
    if not text:
        error = "Describe the change you want."
    elif len(text) > MAX_REQUEST:
        error = f"Please keep your request under {MAX_REQUEST} characters."
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
    flash(request, f"Deleted {_display_title(project)} and all its versions.", "success")
    return redirect("/build")


def forget_user(user_id: int) -> None:
    """Called when a member deletes their account: remove their projects (versions cascade)."""
    with db.session() as conn:
        conn.execute("DELETE FROM build_projects WHERE user_id = ?", (user_id,))
