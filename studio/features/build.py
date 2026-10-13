"""Build: a member describes a website, phone web app, small tool or game in their own words.

The flow:
1. The member writes their idea. The AI reads back what it understood (build.understand).
2. The member corrects it, or approves it and picks which of the AI's suggested extras to include.
3. The AI writes the files (build.generate). The site validates every path, runs its own
   accessibility checks on every HTML page and saves version 1.
4. The member asks for changes (build.change). The site computes what changed with difflib,
   so "what changed" is a machine fact, never the AI's claim.
5. After every build or change, if the server has a browser (studio/visual_check.py), the site opens each
   page on a phone and a computer and measures what sighted visitors meet. An AI then looks at pictures of
   the pages (build.design_review). If either finds problems, the AI repairs them once (build.visual_repair)
   and the site checks again. The member is told about the look by its effect (easy to read, works on phones,
   looks finished). Colours are named only for members who chose that in Your profile (users.describe_colours,
   docs/VOICE.md 4.4); the shared member note tells the AI which, and the page applies the same choice.

Everything the AI builds is static HTML, CSS and plain JavaScript. Previews are served under a
sandbox Content-Security-Policy so a built page cannot read the site's cookies.
"""
from __future__ import annotations

import base64
import bisect
import difflib
import io
import json
import re
import secrets
import zipfile
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from .. import credits, db, jobs, sensitive, visual_check
from ..a11y_check import check_html
from ..ai import AIError, AINotAvailable
from ..web import (checked_form, current_user, error_title, flash, form_text, get_ai, redirect, render,
                   require_user)

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
    feel TEXT NOT NULL DEFAULT '',
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
    visual_json TEXT NOT NULL DEFAULT '{}',
    design_review_json TEXT NOT NULL DEFAULT '{}',
    repaired_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (project_id, number)
);
"""


def seed() -> None:
    """Add the columns that came after the first release to databases made before them."""
    with db.session() as conn:
        db.ensure_column(conn, "build_projects", "feel", "TEXT NOT NULL DEFAULT ''")
        # The site's visual checks, the AI's look at pictures of the pages, and the one automatic repair.
        db.ensure_column(conn, "build_versions", "visual_json", "TEXT NOT NULL DEFAULT '{}'")
        db.ensure_column(conn, "build_versions", "design_review_json", "TEXT NOT NULL DEFAULT '{}'")
        db.ensure_column(conn, "build_versions", "repaired_json", "TEXT NOT NULL DEFAULT '{}'")


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
FEEL_LABEL = "How should it feel to the people who use it?"
FEEL_HINT = "For example friendly, calm, professional or playful. The AI designs the look to suit it."
MAX_FEEL = 200
MAX_TITLE = 120
MAX_IDEA = 20_000
MAX_REQUEST = 10_000
MAX_CORRECTIONS_TOTAL = 50_000  # all corrections on one project together, so the AI prompt stays a sane size
MAX_AI_TEXT = 20_000  # any single piece of text the AI writes about a build (summary, how to use, ...)
# While the AI works on a project, a second request for the same project is refused. "Working" is read from
# the job itself (queued or running), however long it takes; a restart marks unfinished jobs as stopped.
BUSY_STATUSES = {"understanding", "building", "changing"}
BUSY_MESSAGE = ("The AI is already working on this project, so nothing new was started. You can follow it in "
                "Your recent work, and try again when it is done.")
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
# form-action 'none': a build has no server, so its forms send nothing anywhere (a fake sign-in form cannot
# post a password to another site).
PREVIEW_CSP = ("sandbox allow-scripts allow-forms allow-popups allow-modals; "
               "default-src 'self' 'unsafe-inline' data:; form-action 'none'; frame-ancestors 'none'")

# Things the site itself does not check about a build (added to the checker's own list).
JS_NOT_CHECKED = "Whether the JavaScript works. The site reads the files but does not run them."
JS_NOT_CHECKED_WITH_LOOK = ("Whether the buttons, forms and scripts work. The site opens each page in a browser to "
                            "check how it looks when it first opens, but does not press anything or fill in forms.")
SITE_NOT_CHECKED = [
    JS_NOT_CHECKED,
    "CSS, JavaScript and other files that are not HTML pages. Only HTML pages get the machine checks.",
    "Saving data and installing as an app inside the preview. The preview runs in a protected space, so these "
    "may not work there. They work when you put the files on your own website.",
]

# ----- AI prompts and schemas -------------------------------------------------------------------

# Who the person is, how to speak to them and how to describe looks come from the shared member note
# (studio/ai.member_voice, docs/VOICE.md 4.18), which AIClient puts before every system prompt. These prompts
# add only what the task needs.
LOOKS_RULE = "Follow the member note on describing looks."
# What the AI writes for other readers (the built pages) never discloses the person's sight (docs/VOICE.md 4.7).
OUTWARD_DISCLOSURE = ("Never mention the person's blindness or low vision, or how they read, in the files or anything "
                      "meant for other readers, unless they ask.")

WHAT_CAN_BE_BUILT = (
    "What can be built here: static HTML, CSS and plain JavaScript files only, at most 12 files. "
    "There is no server: no accounts or sign-in, no database, no payments, no sending email or messages, "
    "no sharing data between visitors. Data can only be saved in the visitor's own browser (localStorage). "
    "If the idea needs something that cannot be built here, say so in not_doing and describe the closest "
    "thing that can be built."
)

UNDERSTAND_SYSTEM = f"""You help a person plan a small website, web app, tool or game that will then be built for them.

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
- look_and_feel: one or two sentences on how the finished thing will look and feel to visitors. Lead with the
  effect and what it suits, for example "Warm and welcoming, like a family bakery; clear and easy to read on a
  phone." Follow how the person said it should feel, if they did. {LOOKS_RULE}
Only put in pages and features what the person asked for, or what is needed for it to work.

{WHAT_CAN_BE_BUILT}
"""

UNDERSTAND_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "audience", "pages", "features", "data_kept", "questions",
                 "additions_not_asked", "not_doing", "suggested_title", "look_and_feel"],
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
        "look_and_feel": {"type": "string"},
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
- Every sound must also give the same news as text, for example in the role="status" message, so people who
  cannot hear it get it too. Never use sound alone.
- Never stop people zooming (no user-scalable=no, no maximum-scale).
- Each HTML page is under 60 KB. At most 12 files.
- File paths are relative, like index.html or css/style.css: no leading slash, no "..", no backslashes.
  Only these extensions: html, css, js, json, webmanifest, svg, txt, md. There must be an index.html.
- No images unless the person supplied them.
- If data must be saved, use localStorage, wrap every use of it in try and catch so the page still works when
  storage is blocked, and say in how_to_use that the data stays only in that browser on that device.
- Never ask for or store passwords, card numbers or one-time codes.

Design: many visitors will see it, so it must look polished and finished. Follow every rule.
- Design a polished, modern visual look that suits the purpose and the audience, and how the person wants it
  to feel. It should look as if a professional designer made it.
- A coherent colour palette of a few colours that meets WCAG AA contrast: at least 4.5:1 for normal text, 3:1
  for large text and for the edges of buttons and form fields. Check every text and background pair.
- A readable type scale: base font size 16 to 18px, line height about 1.5, clearly larger headings. System font
  stacks only, for example system-ui, -apple-system, "Segoe UI", Roboto, sans-serif. No web fonts.
- Generous spacing between sections and around text. Lines of text at most about 70 characters wide.
- A responsive layout that works from 320px phones to wide screens with no sideways scrolling: include
  <meta name="viewport" content="width=device-width, initial-scale=1">, use flexible widths, flex-wrap or grid,
  and max-width: 100% on media.
- A clear visual hierarchy: the most important thing is the most prominent.
- Buttons look like buttons and are at least 44px tall. Links that stand alone (navigation, lists of links) are
  at least 44px tall too. Links inside sentences are underlined. Form fields are at least 44px tall, with their
  labels visible above them.
- Visible focus outlines on every link, button and form field (for example :focus-visible with a 3px outline).
  Never remove outlines.
- No text on top of other text, no text cut off, no text smaller than 14px.
- Put the styles in a separate style.css linked from every page.

What to return:
- files: every file, each with its complete content.
- summary: what you built, summary first, in plain words. Do not say you checked or tested anything.
- how_to_use: how to open it and use it with a screen reader in speech or braille, a keyboard, or zoom.
- not_done: anything the person asked for that you did not do, and any guesses you made. Empty if none.
- In summary and how_to_use, describe the look first by how it feels to visitors and what it suits, for example
  "a calm, professional look that suits a law office". Follow the member note on describing looks.
- """ + OUTWARD_DISCLOSURE

PHONE_APP_RULES = """This is a web app people can install on their phone. Also include:
- manifest.webmanifest with name, short_name, start_url "./index.html", scope "./", display "standalone",
  background_color and theme_color. Phones need an icon to install an app, so you may add one simple
  icon.svg (with a <title>) and list it in the manifest with sizes "any".
- A small service worker, sw.js, that caches the app's own files so it works offline.
- In index.html, link the manifest and register "sw.js" only if "serviceWorker" in navigator, inside try and catch."""

GENERATE_SYSTEM = f"""You build small websites, web apps, tools and games for a person who described what they want.

{BUILD_RULES}"""

CHANGE_SYSTEM = f"""You change a small website, web app, tool or game that you built earlier, as the person asks.

Change only what the person asked for. Leave every other file and every other line exactly as it is.
Return the complete new set of files, including the ones you did not change. Any file you leave out is deleted.
In summary, say what you changed, in plain words.

{BUILD_RULES}"""

DESIGN_REVIEW_SYSTEM = f"""You are an experienced visual designer. You look at pictures of a small website, web app, tool or
game that an AI built for a person. The person who made this is blind or has low vision and may not see these
pictures. They have asked you for an honest, specific report on how sighted visitors will experience it.

Each picture shows one page as it first opens, on a phone (390 pixels wide) or a computer (1280 pixels wide).
Judge: does it look finished and professional; is all text easy to read; is there a clear order of importance;
is the spacing comfortable; does it work on both the phone and the computer; does the look suit its purpose, its
audience and how the person wants it to feel; is anything broken, overlapping, cut off, crowded, misaligned or
oddly empty.

- verdict: "looks_finished" if a sighted visitor would see a finished, well made result. "needs_work" if
  anything noticeable should be fixed first.
- summary_for_builder: one or two plain sentences for the person about how sighted visitors will experience it:
  whether it looks finished and professional, easy to read, works on phones and computers, and suits the people
  it is for. Say the effect first, for example "It looks finished and welcoming, and is easy to read on a
  phone." {LOOKS_RULE}
- problems: only problems worth fixing, most important first. Empty if none. page: the file name, like
  index.html. builder_text: one plain, specific sentence about the problem and its effect on visitors, for
  example "Some text on the menu is faint, so prices are hard to read on a phone." {LOOKS_RULE}
  fix_text: a precise technical instruction for the AI that will fix it. Here you may name colours, CSS
  properties and values.
You only see these pictures. Do not claim you tested or checked anything else."""

DESIGN_REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary_for_builder", "problems"],
    "properties": {
        "verdict": {"type": "string", "enum": ["looks_finished", "needs_work"]},
        "summary_for_builder": {"type": "string"},
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["page", "builder_text", "fix_text"],
                "properties": {
                    "page": {"type": "string"},
                    "builder_text": {"type": "string"},
                    "fix_text": {"type": "string"},
                },
            },
        },
    },
}
REVIEW_VERDICTS = {"looks_finished", "needs_work"}
# Said instead of the AI's words when they name colours and the member chose looks described by their effect only
# (users.describe_colours = 0). Members who chose colours read the AI's words as written. The last words,
# "Your profile", become a link to that choice.
REVIEW_SUMMARY_WITHHELD = ("The AI's description of the look named colours, so it is not shown here. Your profile is "
                           "set to describe looks by their effect. To read descriptions with colours, change this in "
                           "Your profile.")
REVIEW_PROBLEM_WITHHELD = "The AI found something on this page that may be hard for visitors to read."
# What older versions stored when the site withheld the AI's words at the time it answered.
OLD_WITHHELD = {
    "The AI described the look by its colours, which this page does not show. Its opinion is below.":
        REVIEW_SUMMARY_WITHHELD,
    "The AI found something on this page that may be hard for visitors to see or read.": REVIEW_PROBLEM_WITHHELD,
}
OLD_SUMMARY_NOT_KEPT = ("The AI's description of the look named colours. This version was made before the site "
                        "could show colours, so those words were not kept.")
PROFILE_LOOKS_URL = "/account/profile#field-describe_looks"
MAX_REVIEW_SHOTS = 6
MAX_REVIEW_PROBLEMS = 10


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
        "look_and_feel": _text(raw.get("look_and_feel"), 2000),
    }


def normalize_review(raw: object) -> dict:
    """The AI's look at the pictures, in exactly the shape the pages expect. The AI's words are kept as written:
    whether a member reads colour words is their own choice, applied when the page is shown (review_for_member)."""
    if not isinstance(raw, dict):
        return {"state": "error", "message": "The AI's answer about the pictures came back incomplete."}
    verdict = raw.get("verdict") if raw.get("verdict") in REVIEW_VERDICTS else ""
    problems = []
    items = raw.get("problems")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        builder_text, fix_text = _text(item.get("builder_text"), 1000), _text(item.get("fix_text"), 3000)
        if builder_text or fix_text:
            problems.append({"page": _text(item.get("page"), 200), "builder_text": builder_text, "fix_text": fix_text})
    summary = _text(raw.get("summary_for_builder"), 3000)
    return {"state": "done", "verdict": verdict, "summary": summary, "fix_summary": summary,
            "problems": problems[:MAX_REVIEW_PROBLEMS]}


def review_for_member(review: dict, describe_colours: bool) -> dict:
    """The AI's opinion as this member reads it. Members who chose looks described by their effect only
    (the default) are not shown AI sentences that name colours; they are told so, and where to change it.
    summary_withheld: the summary was replaced, so the page links to Your profile."""
    def own(text: str) -> str:
        return OLD_WITHHELD.get(text, text)

    summary = review.get("summary", "")
    if describe_colours and summary in OLD_WITHHELD:
        summary = OLD_SUMMARY_NOT_KEPT
    summary = own(summary)
    problems = [own(p.get("builder_text", "")) for p in review.get("problems", []) if p.get("builder_text")]
    if not describe_colours:
        if summary and visual_check.names_a_colour(summary):
            summary = REVIEW_SUMMARY_WITHHELD
        problems = [REVIEW_PROBLEM_WITHHELD if visual_check.names_a_colour(t) else t for t in problems]
    return {"summary": summary, "summary_withheld": summary == REVIEW_SUMMARY_WITHHELD, "problems": problems}


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


# ----- how it looks to sighted visitors ---------------------------------------------------------

def _size_words(sizes: list[str]) -> str:
    names = [visual_check.SIZE_LABELS.get(s, s) for s in sizes]
    if len(names) == 2 and all(n.startswith("on a ") for n in names):
        return f"on a {names[0][5:]} and a {names[1][5:]}"
    return " and ".join(names)


def visual_problems(visual: dict) -> list[dict]:
    """The problems the site's visual checks found, one per page and kind (a problem found on both the phone
    and the computer counts once): path, kind, where (plain words), builder_text, fix_texts."""
    merged: dict[tuple[str, str], dict] = {}
    for page in visual.get("pages", []) if isinstance(visual, dict) else []:
        for problem in page.get("problems", []):
            entry = merged.setdefault((page.get("path", ""), problem.get("kind", "")), {
                "path": page.get("path", ""), "kind": problem.get("kind", ""), "sizes": [],
                "builder_text": problem.get("builder_text", ""), "fix_texts": []})
            if page.get("size") not in entry["sizes"]:
                entry["sizes"].append(page.get("size"))
            entry["fix_texts"].append(problem.get("fix_text", ""))
    out = list(merged.values())
    for entry in out:
        entry["where"] = f"{entry['path']}, {_size_words(entry['sizes'])}"
    return out


def _visual_measured(visual: dict) -> bool:
    return any(page.get("checked") for page in visual.get("pages", []))


def _measured_sizes(visual: dict) -> set[tuple[str, str]]:
    """(path, size) of every page the site measured."""
    return {(page.get("path"), page.get("size")) for page in visual.get("pages", []) if page.get("checked")}


def visual_fix_lines(visual: dict, review: dict | None = None) -> list[str]:
    """Technical instructions for the AI, from the site's visual checks and the AI's look at the pictures."""
    lines = [fix for entry in visual_problems(visual) for fix in entry["fix_texts"] if fix]
    if review and review.get("state") == "done" and review.get("verdict") == "needs_work":
        from_review = [f"{p['page'] or 'All pages'} (from a designer who looked at pictures of the pages): "
                       f"{p['fix_text']}" for p in review.get("problems", []) if p.get("fix_text")]
        summary = review.get("fix_summary") or review.get("summary")
        if not from_review and summary:
            from_review = [f"All pages (from a designer who looked at pictures of the pages): {summary}"]
        lines += from_review
    return lines


# difflib with autojunk off takes time that grows much faster than the file size when lines repeat
# (blank lines, "</div>"), and members decide what the AI writes. So the site trims the lines that are
# the same at the start and end, splits long files at lines that appear once on each side, and compares
# each stretch line by line only when that stays cheap. Otherwise the whole stretch counts as removed and
# added, and the page says so.
DIFF_MAX_LINES = 1500        # lines on either side of one file's changed stretch
DIFF_MAX_PAIRS = 10_000      # pairs of equal lines across the two sides of that stretch
DIFF_MAX_TOTAL_LINES = 3000  # line-by-line comparison across all the files of one version


class _DiffBudget:
    def __init__(self):
        self.lines_left = DIFF_MAX_TOTAL_LINES


def _unique_anchors(a: list[str], b: list[str]) -> list[tuple[int, int]]:
    """Lines that appear exactly once on each side, kept in the same order on both (as in patience diff)."""
    count_a, count_b = Counter(a), Counter(b)
    where_b = {line: j for j, line in enumerate(b) if count_b[line] == 1}
    pairs = [(i, where_b[line]) for i, line in enumerate(a) if count_a[line] == 1 and line in where_b]
    tails: list[int] = []      # smallest j that ends a chain of each length
    tail_at: list[int] = []    # index in pairs of that chain's last pair
    before: list[int | None] = []
    for k, (_, j) in enumerate(pairs):
        n = bisect.bisect_left(tails, j)
        if n == len(tails):
            tails.append(j)
            tail_at.append(k)
        else:
            tails[n] = j
            tail_at[n] = k
        before.append(tail_at[n - 1] if n else None)
    chain = []
    k = tail_at[-1] if tail_at else None
    while k is not None:
        chain.append(pairs[k])
        k = before[k]
    return chain[::-1]


def _diff_range(a: list[str], b: list[str], alo: int, ahi: int, blo: int, bhi: int, budget: _DiffBudget,
                use_anchors: bool) -> tuple[list[tuple[str, int, int, int, int]], bool]:
    while alo < ahi and blo < bhi and a[alo] == b[blo]:
        alo += 1
        blo += 1
    while ahi > alo and bhi > blo and a[ahi - 1] == b[bhi - 1]:
        ahi -= 1
        bhi -= 1
    if alo == ahi and blo == bhi:
        return [], True
    if alo == ahi or blo == bhi:
        return [("insert" if bhi > blo else "delete", alo, ahi, blo, bhi)], True
    mid_a, mid_b = a[alo:ahi], b[blo:bhi]
    counts_b = Counter(mid_b)
    pairs = sum(counts_b.get(line, 0) for line in mid_a)
    if pairs == 0:  # no line in common: replacing the whole stretch is the exact answer
        return [("replace", alo, ahi, blo, bhi)], True
    size = len(mid_a) + len(mid_b)
    if max(len(mid_a), len(mid_b)) <= DIFF_MAX_LINES and pairs <= DIFF_MAX_PAIRS and size <= budget.lines_left:
        budget.lines_left -= size
        ops = difflib.SequenceMatcher(None, mid_a, mid_b, autojunk=False).get_opcodes()
        return [(tag, i1 + alo, i2 + alo, j1 + blo, j2 + blo) for tag, i1, i2, j1, j2 in ops if tag != "equal"], True
    if use_anchors:
        anchors = _unique_anchors(mid_a, mid_b)
        if anchors:
            ops: list[tuple[str, int, int, int, int]] = []
            exact = True
            next_a, next_b = alo, blo
            for i, j in anchors + [(len(mid_a), len(mid_b))]:
                part, part_exact = _diff_range(a, b, next_a, alo + i, next_b, blo + j, budget, False)
                ops += part
                exact = exact and part_exact
                next_a, next_b = alo + i + 1, blo + j + 1
            return ops, exact
    return [("replace", alo, ahi, blo, bhi)], False


def _line_ops(a: list[str], b: list[str], budget: _DiffBudget) -> tuple[list[tuple[str, int, int, int, int]], bool]:
    """The changed stretches between two lists of lines, and whether they were compared line by line."""
    return _diff_range(a, b, 0, len(a), 0, len(b), budget, True)


def _file_diffs(old: dict[str, str], new: dict[str, str]) -> list[tuple[dict, list]]:
    """For each file that differs: its summary entry and the changed stretches."""
    out = []
    budget = _DiffBudget()
    for path in sorted(set(old) | set(new)):
        if path not in old:
            n = len(new[path].splitlines())
            out.append(({"path": path, "status": "added", "added": n, "removed": 0}, [("insert", 0, 0, 0, n)]))
        elif path not in new:
            n = len(old[path].splitlines())
            out.append(({"path": path, "status": "removed", "added": 0, "removed": n}, [("delete", 0, n, 0, 0)]))
        elif old[path] != new[path]:
            ops, exact = _line_ops(old[path].splitlines(), new[path].splitlines(), budget)
            entry = {"path": path, "status": "changed", "added": sum(j2 - j1 for _, _, _, j1, j2 in ops),
                     "removed": sum(i2 - i1 for _, i1, i2, _, _ in ops)}
            if not exact:
                entry["exact"] = False
            out.append((entry, ops))
    return out


def compute_diff(old: dict[str, str], new: dict[str, str]) -> list[dict]:
    """Which files were added, removed or changed, with counts of lines added and removed (difflib)."""
    return [entry for entry, _ in _file_diffs(old, new)]


WORD_CHANGE_MAX = 120  # longest changed part, in characters, that is worth saying in one sentence
_TOKEN_RE = re.compile(r"\S+|\s+")


def word_change(old_line: str, new_line: str) -> tuple[str, str] | None:
    """When a line was changed only a little: the words that changed (old, new), so a member does not have to
    compare two long lines from memory. None when most of the line changed, or nothing did."""
    if old_line == new_line:
        return None
    a, b = _TOKEN_RE.findall(old_line), _TOKEN_RE.findall(new_line)
    start = 0
    while start < len(a) and start < len(b) and a[start] == b[start]:
        start += 1
    end = 0
    while end < len(a) - start and end < len(b) - start and a[-1 - end] == b[-1 - end]:
        end += 1
    old_part = "".join(a[start:len(a) - end]).strip()
    new_part = "".join(b[start:len(b) - end]).strip()
    kept = len("".join(a[:start])) + len("".join(a[len(a) - end:]))
    if not (old_part or new_part) or kept < max(len(old_line), len(new_line)) / 2:
        return None
    if len(old_part) > WORD_CHANGE_MAX or len(new_part) > WORD_CHANGE_MAX:
        return None
    return old_part, new_part


def _word_change_sentence(line_number: int, change: tuple[str, str]) -> str:
    old_part, new_part = change
    if not old_part:
        return f'In line {line_number}, "{new_part}" was added.'
    if not new_part:
        return f'In line {line_number}, "{old_part}" was removed.'
    return f'In line {line_number}, "{old_part}" became "{new_part}".'


def changed_lines(old: dict[str, str], new: dict[str, str]) -> list[dict]:
    """For each file that differs: groups of changed lines, each line marked "Added" or "Removed". Where a
    removed line and an added line are mostly the same, "notes" says which words changed (computed here)."""
    out = []
    for entry, ops in _file_diffs(old, new):
        a = old.get(entry["path"], "").splitlines()
        b = new.get(entry["path"], "").splitlines()
        groups = []
        for tag, i1, i2, j1, j2 in ops:
            lines = [("Removed", line) for line in a[i1:i2]] + [("Added", line) for line in b[j1:j2]]
            if j2 > j1:
                where = f"line {j1 + 1}" if j2 - j1 == 1 else f"lines {j1 + 1} to {j2}"
                where = f"New version, {where}"
            else:
                where = f"line {i1 + 1}" if i2 - i1 == 1 else f"lines {i1 + 1} to {i2}"
                where = f"Old version, {where}"
            notes = []
            if tag == "replace" and i2 - i1 == j2 - j1:
                for k in range(i2 - i1):
                    change = word_change(a[i1 + k], b[j1 + k])
                    if change:
                        notes.append(_word_change_sentence(j1 + k + 1, change))
            groups.append({"where": where, "lines": lines, "notes": notes})
        out.append({**entry, "groups": groups})
    return out


def describe_diff_entry(entry: dict) -> str:
    if entry["status"] == "added":
        return f"Added {entry['path']}: {_plural(entry['added'], 'line')}."
    if entry["status"] == "removed":
        return f"Removed {entry['path']}: {_plural(entry['removed'], 'line')} gone."
    if entry["added"] == 0 and entry["removed"] == 0:
        return f"Changed {entry['path']}: only line endings changed, no lines added or removed."
    sentence = (f"Changed {entry['path']}: {_plural(entry['added'], 'line')} added, "
                f"{_plural(entry['removed'], 'line')} removed.")
    if entry.get("exact") is False:
        sentence += (" Too much changed to compare line by line, so every line from the first change to the "
                     "last counts as removed and added.")
    return sentence


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
    """True if the text seems to contain a password, a PIN, a one-time code, an API key or a card number."""
    if sensitive.find_secret(text):
        return True
    for match in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return any(r.search(text) for r in _SECRET_RES)


# The shared wording from docs/VOICE.md 4.9, from studio/sensitive.py.
SECRET_MESSAGE = sensitive.SECRET_MESSAGE


def _display_title(project: dict) -> str:
    return project["title"] or "Your new project"


def _reading_title(title: str) -> str:
    """The job's title, which must make sense in the list of Your recent work."""
    return f"Reading back your idea: {title}" if title else "Reading back your new idea"


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


def job_key(project: dict) -> str:
    """The dedupe key of every AI job for this project. It holds the share token, which is unique to the
    project, so a job for a deleted project never blocks a new project that reused its id."""
    return f"build:{project['share_token']}"


def _busy_job(project: dict) -> str | None:
    """The id of the AI job working on this project (queued or running), or None."""
    if project["status"] not in BUSY_STATUSES:
        return None
    with db.session() as conn:
        row = conn.execute("SELECT id FROM jobs WHERE user_id = ? AND dedupe_key = ? AND status IN ('queued', 'running')"
                           " ORDER BY created_at DESC LIMIT 1", (project["user_id"], job_key(project))).fetchone()
    return row["id"] if row else None


def _is_busy(project: dict) -> bool:
    """True while an AI job for this project is queued or running."""
    return _busy_job(project) is not None


# ----- peer checks of a project's previews ----------------------------------------------------------
# A member can ask other members to check a preview (/checks/new?url=...). The requests live in the Peer checks
# section's table; Build reads the member's own requests whose address is one of this project's previews, so the
# version page can list them and deleting the project can close them.

WANTED_WORDS = {"screen_reader": "people who use screen readers", "sighted": "sighted people, to check how it looks",
                "both": "people who use screen readers, and sighted people to check how it looks"}


def _preview_path(project: dict, number: int | None = None) -> str:
    """The part of a preview address that is the same on any domain: one version, or every version."""
    return f"/build/p/{project['share_token']}/v/" + (f"{number}/" if number is not None else "")


def peer_checks(project: dict, number: int | None = None, *, open_only: bool = False) -> list[dict]:
    """The member's own peer check requests for this project's previews (one version, or all of them)."""
    with db.session() as conn:
        rows = conn.execute(
            "SELECT r.id, r.title, r.status, r.max_checks, r.reward_cents, r.wanted,"
            " (SELECT COUNT(*) FROM check_reports c WHERE c.request_id = r.id) AS done"
            " FROM check_requests r WHERE r.requester_id = ? AND instr(r.url, ?) > 0"
            + (" AND r.status = 'open'" if open_only else "") + " ORDER BY r.id",
            (project["user_id"], _preview_path(project, number))).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["wanted_words"] = WANTED_WORDS.get(item["wanted"], WANTED_WORDS["both"])
        item["unpaid_cents"] = item["reward_cents"] * max(item["max_checks"] - item["done"], 0)
        out.append(item)
    return out


def _close_peer_checks(conn, project: dict) -> tuple[int, int]:
    """Close the member's open requests for this project's previews, which stop working when it is deleted, and
    return the credits held for checks not done, as the Peer checks section does when a request is closed.
    Runs inside the caller's transaction. Returns (requests closed, cents returned)."""
    rows = conn.execute(
        "SELECT r.id, r.title, r.max_checks, r.reward_cents,"
        " (SELECT COUNT(*) FROM check_reports c WHERE c.request_id = r.id) AS done"
        " FROM check_requests r WHERE r.requester_id = ? AND r.status = 'open' AND instr(r.url, ?) > 0",
        (project["user_id"], _preview_path(project))).fetchall()
    returned = 0
    for row in rows:
        conn.execute("UPDATE check_requests SET status = 'closed', closed_at = ? WHERE id = ?", (db.now(), row["id"]))
        refund = row["reward_cents"] * max(row["max_checks"] - row["done"], 0)
        if refund > 0:
            conn.execute("INSERT INTO credit_ledger (user_id, delta_cents, reason, ref, created_at)"
                         " VALUES (?, ?, ?, ?, ?)",
                         (project["user_id"], refund, f"Returned from peer checks: {row['title']}",
                          f"check:{row['id']}", db.now()))
            returned += refund
    return len(rows), returned


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
            f"The site's machine checks looked at {_plural(pages, 'page')} and found {found}.")


def _preview_url(project: dict, number: int, path: str = "index.html", request: Request | None = None) -> str:
    """The preview's address: on the preview domain if the site has one (STUDIO_PREVIEW_BASE_URL)."""
    base = request.app.state.settings.preview_base_url if request is not None else ""
    return f"{base}/build/p/{project['share_token']}/v/{number}/{quote(path)}"


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
           errors: dict | None = None, status_code: int = 200, note: str = ""):
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
                  feel_label=FEEL_LABEL, feel_hint=FEEL_HINT, values=values or {}, errors=errors or {},
                  note=note, status_code=status_code)


def _status_label(project: dict) -> str:
    if project["status"] in BUSY_STATUSES and not _is_busy(project):
        # The job stopped without saying so (for example the server restarted). Do not claim it is working.
        return "The AI stopped before it finished, so you can try again"
    return STATUS_LABELS.get(project["status"], project["status"])


@router.get("")
def index(request: Request):
    deleted = request.session.pop("build_deleted", None)
    if deleted and current_user(request):
        if isinstance(deleted, str):  # a session from before peer checks were closed on delete
            deleted = {"title": deleted}
        title = f"Deleted {deleted.get('title', 'your project')} and all its versions"
        closed, returned = int(deleted.get("closed") or 0), int(deleted.get("returned") or 0)
        if closed:
            title += f", and closed {_plural(closed, 'peer check request')}"
        note = (f"{credits.format_cents(returned)} held for checks not done came back to your credits."
                if returned else "")
        return _index(request, title=title, note=note)
    return _index(request)


@router.get("/new")
def new_project_get():
    """The form's own address, reached by a reload or a restored tab after a form error: go to the form."""
    return redirect("/build#field-idea")


def _seconds_since(stamp: str) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


@router.post("/new")
async def new_project(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    values = {k: _form_text(form, k) for k in ("title", "kind", "idea")}
    values["feel"] = form_text(form, "feel", 10_000).strip()
    errors: dict[str, str] = {}
    if len(values["title"]) > MAX_TITLE:
        errors["title"] = (f"Use {MAX_TITLE} characters or fewer for the title. "
                           f"It is now {len(values['title'])} characters.")
    elif looks_like_secret(values["title"]):
        errors["title"] = SECRET_MESSAGE
    if values["kind"] not in KIND_LABELS:
        errors["kind"] = "Choose what you want to make."
    if not values["idea"]:
        errors["idea"] = "Describe your idea in your own words."
    elif len(values["idea"]) > MAX_IDEA:
        errors["idea"] = (f"Use {MAX_IDEA:,} characters or fewer for your idea. It is now {len(values['idea']):,} "
                          "characters.")
    elif looks_like_secret(values["idea"]):
        errors["idea"] = SECRET_MESSAGE
    if len(values["feel"]) > MAX_FEEL:
        errors["feel"] = (f"Use {MAX_FEEL} characters or fewer here, for example a few words like friendly and "
                          f"calm. It is now {len(values['feel'])} characters.")
    elif values["feel"] and looks_like_secret(values["feel"]):
        errors["feel"] = SECRET_MESSAGE
    if errors:
        return _index(request, title=error_title(INDEX_TITLE, errors), values=values, errors=errors,
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
            "INSERT INTO build_projects (user_id, title, kind, idea, feel, status, spec_json, share_token, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, 'new', NULL, ?, ?, ?)",
            (user["id"], values["title"], values["kind"], values["idea"], values["feel"], token, now, now),
        )
        project_id = cur.lastrowid
    try:
        ai = get_ai(request, user)
    except AINotAvailable as exc:
        flash(request, exc.user_message, "error")
        return redirect(f"/build/{project_id}")
    _set_status(project_id, "understanding", token)
    job_id = jobs.submit(user["id"], "build.understand", _reading_title(values["title"]), _understand, ai,
                         project_id, token, back_url=f"/build/{project_id}",
                         dedupe_key=job_key({"share_token": token}), item_ref=job_key({"share_token": token}))
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
                  f"Title: {project['title'] or '(none given; suggest one)'}\n"
                  f"{_feel_line(project)}\n\n"
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
        raise AIError(f"The AI's answer came back incomplete, so {nothing_saved} Please try again.")
    try:
        files = validate_files(result.get("files"))
    except BuildFilesError as exc:
        raise AIError(f"The AI made a mistake in the files, so {nothing_saved} Please try again. If it happens "
                      f"again, ask for something smaller. Technical detail: {exc}") from exc
    about = {"summary": _text(result.get("summary")), "how_to_use": _text(result.get("how_to_use")),
             "not_done": _texts(result.get("not_done"))}
    return files, about


def _save_version(project_id: int, token: str, files: dict[str, str], about: dict, request_text: str,
                  base: dict[str, str], base_number: int, look: dict | None = None) -> int:
    checks = run_checks(files)
    checks["not_done"] = about["not_done"]
    diff = {"base": base_number, "files": compute_diff(base, files)}
    look = look or {}
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
            " diff_json, checks_json, visual_json, design_review_json, repaired_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, number, json.dumps(files), request_text, about["summary"], about["how_to_use"],
             json.dumps(diff), json.dumps(checks), json.dumps(look.get("visual") or {}),
             json.dumps(look.get("review") or {}), json.dumps(look.get("repaired") or {}), db.now()),
        )
        conn.execute("UPDATE build_projects SET status = 'ready', updated_at = ? WHERE id = ?", (db.now(), project_id))
    return number


def _feel_line(project: dict) -> str:
    feel = (project.get("feel") or "").strip()
    if feel:
        return f"How the person wants it to feel to the people who use it, in their words: <feel>{feel}</feel>"
    return "How it should feel: the person did not say. Choose what suits the purpose and the audience."


def _system_for(base: str, project: dict) -> str:
    return base + ("\n\n" + PHONE_APP_RULES if project["kind"] == "phone_app" else "")


def _files_text(files: dict[str, str]) -> str:
    return "\n\n".join(f'<file path="{p}">\n{c}\n</file>' for p, c in sorted(files.items()))


# ----- the look: the site's visual checks, the AI's look at pictures, and one repair ------------------

def _review_shots(report) -> list:
    """Up to MAX_REVIEW_SHOTS pictures: index.html on the phone and the computer first, then the other pages."""
    shots = sorted(report.screenshots, key=lambda s: (s.path != "index.html", s.size != "phone"))
    by_page: dict[str, list] = {}
    for shot in shots:
        by_page.setdefault(shot.path, []).append(shot)
    ordered = [shot for page in by_page.values() for shot in page]
    return ordered[:MAX_REVIEW_SHOTS]


def _site_findings_text(visual: dict) -> str:
    problems = visual_problems(visual)
    if not problems:
        return "The site's own measurements found no problems."
    return "The site's own measurements found these problems:\n" + "\n".join(
        f"- {p['where']}: {p['builder_text']}" for p in problems)


def _design_review(ctx, ai, project: dict, report) -> dict:
    """An AI looks at pictures of the pages and says how sighted visitors will experience them."""
    shots = _review_shots(report)
    if not shots:
        return {"state": "skipped", "reason": "no_pictures"}
    ctx.progress("An AI is looking at pictures of the pages")
    spec = _load_spec(project)["spec"] or {}
    blocks: list[dict] = []
    for i, shot in enumerate(shots, start=1):
        blocks.append({"type": "text", "text": f"Picture {i}: {shot.path} {visual_check.SIZE_LABELS.get(shot.size, '')}"
                                                f" ({shot.width} pixels wide)."})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": shot.media_type,
                                                   "data": base64.b64encode(shot.png).decode("ascii")}})
    blocks.append({"type": "text", "text": (
        f"What it is: {KIND_LABELS.get(project['kind'], 'Something else')}\n"
        f"Title: {project['title']}\n"
        f"What it is for: {spec.get('summary') or '(not given)'}\n"
        f"Who it is for: {spec.get('audience') or '(not given)'}\n"
        f"{_feel_line(project)}\n"
        f"How the plan said it would feel: {spec.get('look_and_feel') or '(not given)'}\n\n"
        f"{_site_findings_text(report.to_dict())}\n\n"
        "Look at the pictures and say how sighted visitors will experience it.")})
    try:
        raw = ai.structured(system=DESIGN_REVIEW_SYSTEM, prompt=blocks, schema=DESIGN_REVIEW_SCHEMA,
                            purpose="build.design_review", max_tokens=8000)
    except AIError as exc:
        # The files are good and already paid for: save them, and say the AI could not look.
        return {"state": "error", "message": exc.user_message}
    return normalize_review(raw)


def _accessibility_errors(files: dict[str, str]) -> int:
    return _check_counts(run_checks(files))[0]


def _repair_sentence(fixed: int, before: int, after: int) -> str:
    if fixed:
        sentence = (f"The AI fixed {_plural(fixed, 'problem')} the checks found, and the site measured the pages "
                    "again to make sure.")
        if after:
            sentence += f" {_plural(after, 'problem')} {'is' if after == 1 else 'are'} still there, listed in this section."
        return sentence
    if before:
        return ("Before you saw it, the AI tried once to fix what the checks found, but the problems are still "
                "there.")
    # Only the AI's look at the pictures asked for this: whether it is better is the AI's opinion, not a fact.
    return ("After an AI looked at pictures of the pages, the AI changed the design once. The site measured the "
            "changed pages again, and the AI's opinion of them is above.")


def repair_heading(repaired: dict) -> str:
    """The heading over what the one repair did: it says fixed only when the site measured a fix."""
    if repaired.get("kept") and repaired.get("fixed"):
        return "Fixed before you saw it"
    if repaired.get("kept") and not repaired.get("before"):
        return "Changed before you saw it"
    return "An automatic repair was tried before you saw it"


def _look_and_repair(ctx, ai, project_id: int, token: str, files: dict[str, str]) -> tuple[dict[str, str], dict]:
    """Check how the files look, ask an AI to look at pictures, and repair once if either found problems.
    Returns the files to save (the repaired ones only if they are valid and not worse) and what to store."""
    if not visual_check.available():
        return files, {"visual": {"available": False}, "review": {"state": "skipped", "reason": "unavailable"},
                       "repaired": {}}
    project = _project_row(project_id, token)
    if project is None:
        raise AIError(DELETED_MESSAGE)
    ctx.progress("Checking how it looks on a phone and a computer")
    report = visual_check.check_site(files, max_screenshots=MAX_REVIEW_SHOTS)
    visual = report.to_dict()
    review = _design_review(ctx, ai, project, report)
    report.screenshots = []  # sent: let the pictures go before the second check
    before = visual_problems(visual)
    fix_lines = visual_fix_lines(visual, review)
    if not fix_lines:
        return files, {"visual": visual, "review": review, "repaired": {}}

    # One repair, never more.
    if _project_row(project_id, token) is None:
        raise AIError(DELETED_MESSAGE)
    ctx.progress("Fixing what the checks found")
    found_by = "and an AI designer looked at pictures of them" if any("(from a designer" in f for f in fix_lines) else ""
    prompt = (f"What it is: {KIND_LABELS.get(project['kind'], 'Something else')}\n"
              f"Title: {project['title']}\n"
              f"{_feel_line(project)}\n\n"
              f"The files you just made:\n\n{_files_text(files)}\n\n"
              "The site opened every page in a real browser, on a phone 390 pixels wide and a computer 1280 pixels "
              f"wide{', ' + found_by if found_by else ''}. Fix these problems with how the pages look:\n<problems>\n"
              + "\n".join(f"- {line}" for line in fix_lines) +
              "\n</problems>\nKeep all the content, features and accessibility exactly as they are. Change only what "
              "is needed to fix these problems, mostly in style.css.\n")
    repaired = {"ran": True, "kept": False, "before": len(before), "after": len(before), "fixed": 0}
    try:
        raw = ai.structured(system=_system_for(CHANGE_SYSTEM, project), prompt=prompt, schema=FILES_SCHEMA,
                            purpose="build.visual_repair", max_tokens=64000)
        new_files, _ = _files_result(raw, "the first files were kept.")
    except AIError as exc:
        why = ("it made a mistake in its new files" if isinstance(exc.__cause__, BuildFilesError)
               else "the AI could not finish")
        repaired["summary"] = (f"Before you saw it, the AI tried once to fix what the checks found, but {why}, "
                               "so this version keeps the files it made first.")
        return files, {"visual": visual, "review": review, "repaired": repaired}

    ctx.progress("Checking how it looks again")
    new_report = visual_check.check_site(new_files, max_screenshots=MAX_REVIEW_SHOTS)
    new_visual = new_report.to_dict()
    after = visual_problems(new_visual)
    measured_after = _measured_sizes(new_visual)
    # A page measured before that cannot be measured now (it hangs, crashes, is gone, or time ran out) hides
    # its problems: that is never better.
    lost = _measured_sizes(visual) - measured_after
    if lost:
        repaired["summary"] = ("Before you saw it, the AI tried once to fix what the checks found, but the site could "
                               "not measure every page of its new files, so this version keeps the files it made "
                               "first.")
        return files, {"visual": visual, "review": review, "repaired": repaired}
    if _accessibility_errors(new_files) > _accessibility_errors(files) or len(after) > len(before):
        repaired["summary"] = ("Before you saw it, the AI tried once to fix what the checks found, but its new files "
                               "were not better, so this version keeps the files it made first.")
        return files, {"visual": visual, "review": review, "repaired": repaired}
    new_review = _design_review(ctx, ai, project, new_report)
    new_report.screenshots = []
    after_keys = {(p["path"], p["kind"]) for p in after}
    # Fixed only where the site measured that page again, at every size the problem was found, and it is gone.
    fixed = sum(1 for p in before if (p["path"], p["kind"]) not in after_keys
                and all((p["path"], size) in measured_after for size in p["sizes"]))
    repaired.update(kept=True, after=len(after), fixed=fixed,
                    summary=_repair_sentence(fixed, len(before), len(after)))
    return new_files, {"visual": new_visual, "review": new_review, "repaired": repaired}


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
                  f"Title: {project['title']}\n"
                  f"{_feel_line(project)}\n\n"
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
        result = ai.structured(system=_system_for(GENERATE_SYSTEM, project), prompt=prompt, schema=FILES_SCHEMA,
                               purpose="build.generate", max_tokens=64000)
        ctx.progress("Checking the files")
        files, about = _files_result(result, "nothing was saved.")
        files, look = _look_and_repair(ctx, ai, project_id, token, files)
        number = _save_version(project_id, token, files, about, "First version", {}, 0, look)
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
        prompt = (f"What it is: {KIND_LABELS.get(project['kind'], 'Something else')}\n"
                  f"Title: {project['title']}\n"
                  f"{_feel_line(project)}\n\n"
                  f"The current files (version {base_number}):\n\n{_files_text(base)}\n\n"
                  f"What the person asks you to change:\n<request>\n{request_text}\n</request>\n")
        result = ai.structured(system=_system_for(CHANGE_SYSTEM, project), prompt=prompt, schema=FILES_SCHEMA,
                               purpose="build.change", max_tokens=64000)
        ctx.progress("Checking the files and working out what changed")
        files, about = _files_result(result, f"nothing was saved and version {base_number} is unchanged.")
        files, look = _look_and_repair(ctx, ai, project_id, token, files)
        number = _save_version(project_id, token, files, about, request_text, base, base_number, look)
    except BaseException:
        _set_status(project_id, "ready", token)
        raise
    return f"/build/{project_id}/v/{number}"


# ----- project page ----------------------------------------------------------------------------

AI_NOT_AVAILABLE_TITLE = "the AI is not available, so nothing was sent to it"
BUSY_KEPT_TITLE = "the AI is already working on it, so your words were not sent"


def _project_page(request: Request, user: dict, project: dict, *, values: dict | None = None,
                  errors: dict | None = None, status_code: int = 200, note: str = ""):
    """The project's own page. `note` replaces the usual end of the title, to say what just happened."""
    stored = _load_spec(project)
    versions = _versions(project["id"])
    title_text = _display_title(project)
    busy_job = _busy_job(project)
    busy = busy_job is not None
    values = values or {}
    # The extras ticked on the read-back: as just sent (after an error), else as saved with the project.
    ticked = values.get("ticked", stored.get("chosen_additions", []))
    ctx = dict(project=project, stored=stored, spec=stored["spec"], versions=versions, busy=busy,
               job_url=jobs.job_url(busy_job) if busy else "", ticked=ticked,
               chosen_additions=stored.get("chosen_additions", []),
               unread_corrections=len(stored["corrections"]) - stored["applied"],
               kind_label=KIND_LABELS.get(project["kind"], "Something else"),
               status_label=_status_label(project), values=values, errors=errors or {})
    if versions:
        latest = versions[0]
        checks = json.loads(latest["checks_json"])
        ctx.update(latest=latest, latest_check=_check_sentence(checks),
                   preview_url=_preview_url(project, latest["number"], request=request))
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
    title = error_title(f"{title_text}: {note or about}", errors)
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
    additions = (stored["spec"] or {}).get("additions_not_asked", [])
    chosen = [a for i, a in enumerate(additions) if form.get(f"add_{i}") == "yes"]
    if _is_busy(project):
        if not corrections:
            flash(request, BUSY_MESSAGE, "info")
            return redirect(f"/build/{project_id}")
        # A form sent from an older copy of the page: never lose the member's words.
        return _project_page(request, user, project, values={"corrections": corrections, "ticked": chosen},
                             note=BUSY_KEPT_TITLE, status_code=409)
    if action == "build" and not stored["spec"]:
        return redirect(f"/build/{project_id}")
    unread = len(stored["corrections"]) > stored["applied"]
    errors: dict[str, str] = {}
    if len(corrections) > MAX_REQUEST:
        errors["corrections"] = (f"Use {MAX_REQUEST:,} characters or fewer here. It is now {len(corrections):,} "
                                 "characters.")
    elif corrections and looks_like_secret(corrections):
        errors["corrections"] = SECRET_MESSAGE
    elif corrections and sum(map(len, stored["corrections"])) + len(corrections) > MAX_CORRECTIONS_TOTAL:
        errors["corrections"] = (f"Together, your corrections on this project would pass the limit of "
                                 f"{MAX_CORRECTIONS_TOTAL:,} characters, so these were not sent. To go on, start a "
                                 "new project with your idea and your corrections written together.")
    elif action == "update" and stored["spec"] and not corrections and not unread:
        errors["corrections"] = ("Write your corrections or answers first. If everything is right, choose This is "
                                 "right: build it.")
    if errors:
        return _project_page(request, user, project, values={"corrections": corrections, "ticked": chosen},
                             errors=errors, status_code=400)
    if stored["spec"]:
        # The ticks are kept with the project, so a new read-back or a failed build never loses them.
        stored["chosen_additions"] = chosen
        with db.session() as conn:
            conn.execute("UPDATE build_projects SET spec_json = ? WHERE id = ?", (json.dumps(stored), project_id))
    try:
        ai = get_ai(request, user)
    except AINotAvailable as exc:
        flash(request, exc.user_message, "error")
        return _project_page(request, user, project, values={"corrections": corrections, "ticked": chosen},
                             note=AI_NOT_AVAILABLE_TITLE)
    if corrections:
        # The person's words are kept with the project, whether they lead to a new read-back or the build.
        stored["corrections"].append(corrections)
        with db.session() as conn:
            conn.execute("UPDATE build_projects SET spec_json = ?, updated_at = ? WHERE id = ?",
                         (json.dumps(stored), db.now(), project_id))
    token = project["share_token"]
    if action == "build":
        _set_status(project_id, "building", token)
        job_id = jobs.submit(user["id"], "build.generate", f"Building {_display_title(project)}", _generate, ai,
                             project_id, token, chosen, back_url=f"/build/{project_id}",
                             dedupe_key=job_key(project), item_ref=job_key(project))
        return redirect(jobs.job_url(job_id))
    _set_status(project_id, "understanding", token)
    job_id = jobs.submit(user["id"], "build.understand", _reading_title(_display_title(project)), _understand, ai,
                         project_id, token, back_url=f"/build/{project_id}",
                         dedupe_key=job_key(project), item_ref=job_key(project))
    return redirect(jobs.job_url(job_id))


@router.get("/{project_id:int}/understand")
def understand_get(request: Request, project_id: int, user: dict = Depends(require_user)):
    """The form's own address, reached by a reload or a restored tab after a form error: go to the project."""
    _owned_project(project_id, user)
    return redirect(f"/build/{project_id}")


# ----- versions -------------------------------------------------------------------------------

def _stored_json(text: str | None) -> dict:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


LOOK_NOT_RUN = ("The look of this version was not checked: it was made before the site started checking how "
                "builds look.")
LOOK_UNAVAILABLE = ("The look was not checked. This server cannot open pages in a browser, so neither the site nor "
                    "an AI could check how this version looks on screen.")


def _look_context(version: dict, describe_colours: bool = False) -> dict:
    """What the version page says about how the build looks on screen: the effect, and colour words only for
    members who chose them in Your profile (describe_colours)."""
    visual = _stored_json(version.get("visual_json"))
    review = _stored_json(version.get("design_review_json"))
    repaired = _stored_json(version.get("repaired_json"))
    total = len(visual_check.CHECKS)
    look: dict = {"total": total, "problems": [], "check_lines": [], "unchecked_pages": [], "unknown_contrast": 0,
                  "unknown_images": 0, "review": None, "review_note": "",
                  "repaired": repaired.get("summary", "") if repaired else "",
                  "repaired_heading": repair_heading(repaired) if repaired else ""}
    if not visual:
        look.update(state="not_run", sentence=LOOK_NOT_RUN)
    elif not visual.get("available", True):
        look.update(state="unavailable", sentence=LOOK_UNAVAILABLE)
    elif not _visual_measured(visual):
        problems = visual_problems(visual)
        look.update(state="error", problems=problems,
                    sentence=("The site tried to check how it looks on a phone and a computer, but could not "
                              "measure any page. " + (visual.get("error") or "")).strip())
    else:
        pages = visual.get("pages", [])
        problems = visual_problems(visual)
        label = visual_check.SIZE_LABELS
        unchecked = [f"{page['path']} {label.get(page['size'], page['size'])}" for page in pages if not page.get("checked")]
        # Pages the check should have reached but never mentions (an older report, or one cut short).
        listed = {(page.get("path"), page.get("size")) for page in pages}
        files = _stored_json(version.get("files_json"))
        expected, _ = visual_check._html_pages(files, None)
        unchecked += [f"{path} {size['label']}" for path in expected if path not in visual.get("skipped_pages", [])
                      for size in visual_check.SIZES if (path, size["name"]) not in listed]
        error = visual.get("error") or ""
        complete = not unchecked and not error
        passed = visual_check.passed_kinds(pages, measured_only=True)
        failed = {p["kind"] for p in problems}
        lines = [{"label": text, "state": "failed" if kind in failed else
                  "passed" if kind in passed and complete else "partial"}
                 for kind, text in visual_check.CHECKS.items()]
        n_passed = sum(line["state"] == "passed" for line in lines)
        if complete and not problems and n_passed == total:
            sentence = f"Checked by the site on a phone and a computer: passed all {total} visual checks."
        elif complete:
            sentence = f"Checked by the site on a phone and a computer: passed {n_passed} of {total} visual checks."
        else:
            sentence = ("Checked by the site on a phone and a computer, but not every page could be measured, so no "
                        "check is passed on every page. " + (error + " " if error else "") +
                        f"On the pages it measured, {len([k for k in passed if k not in failed])} of {total} "
                        "visual checks found nothing wrong.")
        if problems:
            sentence += f" It found {_plural(len(problems), 'problem')}:"
        look.update(
            state="checked", sentence=sentence, problems=problems, unchecked_pages=unchecked, check_lines=lines,
            unknown_contrast=sum(int(page.get("unknown_contrast") or 0) for page in pages),
            unknown_images=sum(int(page.get("unknown_images") or 0) for page in pages),
            skipped_pages=visual.get("skipped_pages", []))
    if review.get("state") == "done":
        shown = review_for_member(review, describe_colours)
        summary_before, _, summary_after = (shown["summary"].rpartition("Your profile") if shown["summary_withheld"]
                                            else ("", "", ""))
        look["review"] = {
            "summary": shown["summary"],
            # A withheld summary ends with a link to the choice in Your profile.
            "summary_before_link": summary_before, "summary_after_link": summary_after,
            "summary_withheld": shown["summary_withheld"], "profile_url": PROFILE_LOOKS_URL,
            "verdict": {"looks_finished": "In the AI's opinion, it looks finished.",
                        "needs_work": "In the AI's opinion, it still needs some work."}.get(review.get("verdict"), ""),
            "problems": shown["problems"] if review.get("verdict") == "needs_work" else [],
        }
    elif review.get("state") == "error":
        look["review_note"] = ("No AI looked at pictures of this version, because the AI could not answer: "
                               + review.get("message", ""))
    elif look["state"] != "not_run":
        look["review_note"] = "No AI looked at pictures of this version, because the site could not take any."
    return look


_WHERE_LINE_RE = re.compile(r"^(?P<what>.*) on line (?P<line>\d+)$", re.S)
# The machine checker's own "not checked" lines about the look. The visual checks measure these when they run,
# so the page never lists contrast as not checked next to "Passed: Text stands out clearly" (docs/VOICE.md 4.8).
LOOK_NOT_MEASURED = "Contrast, layout and spacing: not measured on this version."
# Older words in the machine checker's list (also stored with older versions), as members read them now.
NOT_CHECKED_WORDING = {
    "How a real screen reader speaks the page (ask a peer check).":
        "How a real screen reader reads the page, in speech and in braille (ask for a peer check).",
}


def _about_the_look(item: str) -> bool:
    lowered = item.lower()
    return "contrast" in lowered or "looks right to sighted" in lowered


def _finding_place(finding: dict, file_url: str) -> dict:
    """A finding with its place split into the element and a link to that line of the file."""
    out = dict(finding)
    match = _WHERE_LINE_RE.match(finding.get("where") or "")
    if match:
        out.update(where_what=match.group("what"), line=int(match.group("line")),
                   line_url=f"{file_url}#L{match.group('line')}")
    return out


def _chosen_for_v1(project: dict) -> list[str] | None:
    """The extras the member ticked for version 1, or None when the AI offered none or the ticks are not known
    (projects built before the ticks were kept)."""
    stored = _load_spec(project)
    if not (stored["spec"] or {}).get("additions_not_asked") or "chosen_additions" not in stored:
        return None
    return stored["chosen_additions"]


def _version_context(project: dict, version: dict, describe_colours: bool = False) -> dict:
    files = json.loads(version["files_json"])
    checks = json.loads(version["checks_json"])
    diff = json.loads(version["diff_json"]) or {}
    pages = []
    for page in checks.get("pages", []):
        findings = page["report"]["findings"]
        file_url = f"/build/{project['id']}/v/{version['number']}/files/{quote(page['path'])}"
        pages.append({
            "found": _found_text(sum(f["severity"] == "error" for f in findings),
                                 sum(f["severity"] != "error" for f in findings)),
            "path": page["path"],
            "size": _size(page["bytes"]),
            "passed": _plural(len(page["report"]["passed"]), "check"),
            "errors": [_finding_place(f, file_url) for f in findings if f["severity"] == "error"],
            "warnings": [_finding_place(f, file_url) for f in findings if f["severity"] != "error"],
        })
    look = _look_context(version, describe_colours)
    not_checked: list[str] = []
    for page in checks.get("pages", []):
        for item in page["report"]["not_checked"]:
            item = NOT_CHECKED_WORDING.get(item, item)
            if _about_the_look(item):
                item = "" if look["state"] == "checked" else LOOK_NOT_MEASURED
            if item and item not in not_checked:
                not_checked.append(item)
    site_not_checked = list(SITE_NOT_CHECKED)
    if look["state"] == "checked":
        site_not_checked[0] = JS_NOT_CHECKED_WITH_LOOK
    not_checked += site_not_checked
    if look.get("unknown_contrast"):
        n = look["unknown_contrast"]
        not_checked.append(("Whether 1 piece of text is easy to read. The site cannot measure it, because it sits"
                            if n == 1 else
                            f"Whether {n} pieces of text are easy to read. The site cannot measure these, because "
                            "they sit")
                           + " on a picture, a colour fade or a box over the page, or are faded.")
    if look.get("unknown_images"):
        n = look["unknown_images"]
        not_checked.append(f"Whether {_plural(n, 'picture')} {'loads' if n == 1 else 'load'}: "
                           f"{'it was' if n == 1 else 'they were'} still loading when the site measured the page.")
    if look.get("unchecked_pages"):
        not_checked.append("How these pages look, because the site could not measure them (they did not finish "
                           "opening in time, or the check stopped first): " + ", ".join(look["unchecked_pages"]) + ".")
    if look.get("skipped_pages"):
        not_checked.append(f"How these pages look, because the site checks the first {visual_check.MAX_PAGES} pages "
                           "only: " + ", ".join(look["skipped_pages"]) + ".")
    unchecked_files = [p for p in sorted(files) if _ext(p) != "html"]
    errors, warnings = _check_counts(checks)
    diff_entries = diff.get("files", []) if version["number"] > 1 else []
    return dict(
        look=look, fix_count=errors + len(look["problems"]),
        # The extras ticked for version 1, or None when not known (projects built before the ticks were kept).
        chosen_additions=_chosen_for_v1(project) if version["number"] == 1 else None,
        files=files, checks=checks, pages=pages, not_checked=not_checked, unchecked_files=unchecked_files,
        not_done=checks.get("not_done", []), error_count=errors, warning_count=warnings,
        check_sentence=_check_sentence(checks),
        diff_base=diff.get("base", 0), diff_lines=[describe_diff_entry(e) for e in diff_entries],
        diff_count=len(diff_entries),
    )


def _version_page(request: Request, project: dict, version: dict, *, values: dict | None = None,
                  errors: dict | None = None, status_code: int = 200, note: str = ""):
    user = current_user(request) or {}
    ctx = _version_context(project, version, bool(user.get("describe_colours")))
    settings = request.app.state.settings
    number = version["number"]
    preview = _preview_url(project, number, request=request)
    title_text = _display_title(project)
    absolute = preview if settings.preview_base_url else settings.base_url + preview
    # Each request names its kind, so the two show under different names in Peer checks.
    checks_link = "/checks/new?" + urlencode({"url": absolute, "title": f"{title_text}, version {number}: screen "
                                              "reader check", "kind": "screen_reader"})
    sighted_link = "/checks/new?" + urlencode({"url": absolute, "title": f"{title_text}, version {number}: how it "
                                               "looks", "kind": "sighted"})
    latest = _versions(project["id"])[0]["number"]
    if errors:
        title = error_title(f"your change to {title_text}, version {number}", errors)
    elif note:
        title = f"{title_text}: version {number} {note}"
    elif number == latest:
        title = f"{title_text}: version {number} is ready"
    else:
        title = f"{title_text}: version {number} (older; the latest is version {latest})"
    busy = _is_busy(project)
    return render(request, "build/version.html", title=title, project=project, version=version,
                  preview_url=preview, checks_link=checks_link, sighted_link=sighted_link, latest_number=latest,
                  peer_checks=peer_checks(project, number), busy=busy,
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
                "url": _file_url(project_id, number, p)} for p, c in sorted(files.items())]
    return render(request, "build/files.html", title=f"{_display_title(project)}: the files in version {number}",
                  project=project, version=version, listing=listing)


def _file_url(project_id: int, number: int, path: str) -> str:
    return f"/build/{project_id}/v/{number}/files/{quote(path)}"


def _numbered_lines(content: str) -> list[str]:
    """The file's lines for the numbered list. Leading spaces become no-break spaces, so the indentation still
    shows outside a <pre>; tabs count as four spaces."""
    out = []
    for line in content.splitlines():
        stripped = line.lstrip(" \t")
        indent = line[:len(line) - len(stripped)].replace("\t", "    ")
        out.append("\u00a0" * len(indent) + stripped)
    return out


@router.get("/{project_id:int}/v/{number:int}/files/{path:path}")
def file_page(request: Request, project_id: int, number: int, path: str, user: dict = Depends(require_user)):
    project = _owned_project(project_id, user)
    version = _get_version(project_id, number)
    files = json.loads(version["files_json"])
    if path not in files:
        raise HTTPException(status_code=404, detail="There is no file with that name in this version.")
    content = files[path]
    paths = sorted(files)
    at = paths.index(path)
    previous = {"path": paths[at - 1], "url": _file_url(project_id, number, paths[at - 1])} if at > 0 else None
    following = ({"path": paths[at + 1], "url": _file_url(project_id, number, paths[at + 1])}
                 if at + 1 < len(paths) else None)
    return render(request, "build/file.html", title=f"{_display_title(project)}: {path} in version {number}",
                  project=project, version=version, path=path, numbered=_numbered_lines(content),
                  previous=previous, following=following,
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


BUSY_CHANGE_ERROR = ("The AI is already working on this project, so this change was not sent. Your words are kept "
                     "here: send them again when it is done. You can follow it in Your recent work.")


async def _submit_change(request: Request, user: dict, project: dict, version: dict, request_text: str,
                         typed: bool = False):
    """typed: the member wrote request_text themselves, so it must never be lost."""
    if _is_busy(project):
        if typed:
            return _version_page(request, project, version, values={"request": request_text},
                                 errors={"request": BUSY_CHANGE_ERROR}, status_code=409)
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
                         project["id"], project["share_token"], version["number"], request_text,
                         back_url=f"/build/{project['id']}/v/{version['number']}",
                         dedupe_key=job_key(project), item_ref=job_key(project))
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
        error = f"Use {MAX_REQUEST:,} characters or fewer for your change. It is now {len(text):,} characters."
    elif looks_like_secret(text):
        error = SECRET_MESSAGE
    if error:
        return _version_page(request, project, version, values={"request": text}, errors={"request": error},
                             status_code=400)
    return await _submit_change(request, user, project, version, text, typed=True)


@router.get("/{project_id:int}/v/{number:int}/change")
def ask_for_change_get(request: Request, project_id: int, number: int, user: dict = Depends(require_user)):
    """The form's own address, reached by a reload after a form error: go to the form on the version page."""
    _owned_project(project_id, user)
    return redirect(f"/build/{project_id}/v/{number}#field-request")


@router.get("/{project_id:int}/v/{number:int}/fix")
def fix_problems_get(request: Request, project_id: int, number: int, user: dict = Depends(require_user)):
    _owned_project(project_id, user)
    return redirect(f"/build/{project_id}/v/{number}#fix")


def fix_request_text(checks: dict, visual: dict | None = None) -> str:
    """The request the site writes for "Fix the problems the checks found"."""
    lines = ["Fix these problems that the site's checks found. Do not change anything else."]
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
    look_lines = visual_fix_lines(visual or {})
    if look_lines:
        lines.append("")
        lines.append("How the pages look, found by opening them in a browser on a phone and a computer:")
        lines += [f"- {line}" for line in look_lines]
    return "\n".join(lines)


@router.post("/{project_id:int}/v/{number:int}/fix")
async def fix_problems(request: Request, project_id: int, number: int, user: dict = Depends(require_user)):
    await checked_form(request)
    project = _owned_project(project_id, user)
    version = _get_version(project_id, number)
    checks = json.loads(version["checks_json"])
    visual = _stored_json(version.get("visual_json"))
    if _check_counts(checks)[0] == 0 and not visual_problems(visual):
        flash(request, "The checks found no problems to fix in this version.", "info")
        return redirect(f"/build/{project_id}/v/{number}")
    return await _submit_change(request, user, project, version, fix_request_text(checks, visual))


# ----- delete ---------------------------------------------------------------------------------

@router.get("/{project_id:int}/delete")
def delete_form(request: Request, project_id: int, user: dict = Depends(require_user)):
    project = _owned_project(project_id, user)
    versions = _versions(project_id)
    open_checks = peer_checks(project, open_only=True)
    held = sum(c["unpaid_cents"] for c in open_checks)
    return render(request, "build/delete.html", title=f"Delete {_display_title(project)}", project=project,
                  version_count=len(versions), latest_number=versions[0]["number"] if versions else 0,
                  open_checks=open_checks, held_cents=held, held=credits.format_cents(held))


@router.post("/{project_id:int}/delete")
async def delete_project(request: Request, project_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    project = _owned_project(project_id, user)
    with db.session() as conn:
        # Deleting, closing the peer checks of its previews and returning their held credits are one unit.
        conn.execute("BEGIN IMMEDIATE")
        closed, returned = _close_peer_checks(conn, project)
        conn.execute("DELETE FROM build_versions WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM build_projects WHERE id = ? AND user_id = ?", (project_id, user["id"]))
        jobs.forget_item(conn, user["id"], job_key(project))
    # The next page's heading says what happened (read once, like a flash message).
    request.session["build_deleted"] = {"title": _display_title(project), "closed": closed, "returned": returned}
    return redirect("/build")


def forget_user(user_id: int) -> None:
    """Called when a member deletes their account: remove their projects (versions cascade)."""
    with db.session() as conn:
        conn.execute("DELETE FROM build_projects WHERE user_id = ?", (user_id,))
