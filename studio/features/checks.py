"""Peer checks: members ask other blind members to try their website or app with a real screen reader.

Every check is saved in a public record. Each entry carries a fingerprint (SHA-256) that covers the
entry, the request it belongs to and the fingerprint of the entry before it, so any later change to
an entry shows up when the record page recomputes the chain.

Optional rewards are credits: the requester's total is held when the request is made, each checker
is paid when they send their check, and whatever is not paid out comes back when the request closes.
No staff are involved at any step.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import credits, db
from ..web import checked_form, current_user, flash, redirect, render, require_user

router = APIRouter(prefix="/checks")
NAV = {"label": 'Peer checks', "url": '/checks', "description": 'Ask other blind members to test your website or app with their screen reader, and check theirs. Every check is recorded honestly.'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS check_requests (
    id INTEGER PRIMARY KEY,
    requester_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    what_to_try TEXT NOT NULL,
    max_checks INTEGER NOT NULL,
    reward_cents INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_check_requests_requester ON check_requests(requester_id);
CREATE INDEX IF NOT EXISTS idx_check_requests_status ON check_requests(status);
CREATE TABLE IF NOT EXISTS check_reports (
    id INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES check_requests(id),
    checker_id INTEGER NOT NULL REFERENCES users(id),
    screen_reader TEXT NOT NULL,
    screen_reader_version TEXT NOT NULL DEFAULT '',
    browser TEXT NOT NULL,
    device TEXT NOT NULL,
    heard_first TEXT NOT NULL,
    task_result TEXT NOT NULL CHECK (task_result IN ('completed', 'partly', 'could_not')),
    problems TEXT NOT NULL DEFAULT '',
    show_name INTEGER NOT NULL DEFAULT 0,
    prev_hash TEXT NOT NULL DEFAULT '',
    record_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (request_id, checker_id)
);
CREATE INDEX IF NOT EXISTS idx_check_reports_checker ON check_reports(checker_id);
"""

# ----- choices and limits ----------------------------------------------------------------------

COUNT_OPTIONS = [(str(n), f"{n} check" if n == 1 else f"{n} checks") for n in range(1, 6)]
REWARD_OPTIONS = [("0", "No reward"), ("25", "25 cents per check"), ("50", "50 cents per check"),
                  ("100", "1 dollar per check")]
RESULT_OPTIONS = [("completed", "Yes, I could do it"), ("partly", "Partly"), ("could_not", "No, I could not do it")]
RESULT_WORDS = {"completed": "Could do the task", "partly": "Could do it partly", "could_not": "Could not do the task"}

LIMITS = {"title": 150, "url": 2000, "what_to_try": 4000, "screen_reader": 80, "screen_reader_version": 40,
          "browser": 80, "device": 120, "heard_first": 4000, "problems": 8000}

# Screen readers people use most, and the platform a screen reader implies when it runs on only one.
KNOWN_SCREEN_READERS = [
    ("NVDA", ("nvda",), "Windows computer"),
    ("JAWS", ("jaws",), "Windows computer"),
    ("Narrator", ("narrator",), "Windows computer"),
    ("VoiceOver", ("voiceover", "voice over"), None),
    ("TalkBack", ("talkback", "talk back"), "Android phone or tablet"),
    ("Orca", ("orca",), "Linux computer"),
]
KNOWN_DEVICES = [
    ("Windows computer", (r"\bwindows\b", r"\bpc\b")),
    ("Mac", (r"\bmac\b", r"\bmacbook", r"\bimac\b", r"\bmacos\b")),
    ("iPhone or iPad", (r"\biphone", r"\bipad", r"\bios\b")),
    ("Android phone or tablet", (r"\bandroid\b", r"\bpixel\b", r"\bgalaxy\b")),
    ("Linux computer", (r"\blinux\b", r"\bubuntu\b", r"\bdebian\b", r"\bfedora\b")),
    ("Chromebook", (r"\bchromebook", r"\bchrome ?os\b")),
]
APPEARANCE_WORDS = re.compile(r"\b(colou?rs?|contrast|layout|looks?|visual(ly)?|font size|zoom(ed)?)\b", re.I)

# Records are public, so refuse anything that looks like a password, a code or a card number.
SECRET_RE = re.compile(r"\b(password|passwd|pwd|passcode|pin|one[- ]time code|otp|verification code)\s*(is|:|=)\s*\S", re.I)
CARD_RE = re.compile(r"(?:\d[ -]?){13,19}")


def _looks_like_card(text: str) -> bool:
    for match in CARD_RE.finditer(text):
        digits = [int(c) for c in match.group() if c.isdigit()]
        if not 13 <= len(digits) <= 19:
            continue
        total = 0
        for i, d in enumerate(reversed(digits)):
            if i % 2 == 1:
                d *= 2
                if d > 9:
                    d -= 9
            total += d
        if total % 10 == 0:
            return True
    return False


def _secret_error(text: str) -> str | None:
    if SECRET_RE.search(text) or _looks_like_card(text):
        return ("This looks like a password, a code or a card number. Check records are public, so leave it out. "
                "If the task needs an account, ask checkers to create their own test account.")
    return None


def valid_url(url: str) -> bool:
    """Only http and https addresses with a host name, and no spaces."""
    if not url or any(c.isspace() for c in url):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc) and bool(parsed.hostname)


def _cents_words(cents: int) -> str:
    if cents == 100:
        return "1 dollar"
    if cents % 100 == 0:
        return f"{cents // 100} dollars"
    if cents < 100:
        return f"{cents} cents"
    return credits.format_cents(cents)


# ----- record fingerprints ---------------------------------------------------------------------

HASHED_REPORT_FIELDS = ("checker_id", "screen_reader", "screen_reader_version", "browser", "device", "heard_first",
                        "task_result", "problems", "show_name", "created_at")


def compute_hash(request_row: dict, report: dict, prev_hash: str) -> str:
    """SHA-256 over canonical JSON (sorted keys) of the report, its request and the previous entry's hash."""
    payload = {k: report[k] for k in HASHED_REPORT_FIELDS}
    payload["show_name"] = int(payload["show_name"])
    payload["checker_id"] = int(payload["checker_id"])
    payload.update({"request_id": int(request_row["id"]), "url": request_row["url"], "title": request_row["title"],
                    "what_to_try": request_row["what_to_try"], "prev_hash": prev_hash})
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_chain(request_row: dict, reports: list[dict]) -> list[str]:
    """Recompute every fingerprint in order. Returns plain sentences for each entry that does not match."""
    problems: list[str] = []
    expected_prev = ""
    for number, rep in enumerate(reports, start=1):
        if rep["prev_hash"] != expected_prev:
            problems.append(f"Entry {number} does not point to the entry before it. An entry may have been "
                            "removed, added or moved after it was written.")
        if compute_hash(request_row, rep, rep["prev_hash"]) != rep["record_hash"]:
            problems.append(f"Entry {number} does not match its fingerprint. It was changed after it was written.")
        expected_prev = rep["record_hash"]
    return problems


# ----- data helpers ----------------------------------------------------------------------------

def _get_request(request_id: int) -> dict:
    with db.session() as conn:
        row = conn.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM check_reports c WHERE c.request_id = r.id) AS done"
            " FROM check_requests r WHERE r.id = ?", (request_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="We could not find that check request.")
    return dict(row)


def _reports(request_id: int) -> list[dict]:
    with db.session() as conn:
        rows = conn.execute(
            "SELECT c.*, u.display_name, u.pseudonym FROM check_reports c JOIN users u ON u.id = c.checker_id"
            " WHERE c.request_id = ? ORDER BY c.id", (request_id,)).fetchall()
    out = []
    for r in rows:
        rep = dict(r)
        rep["who"] = rep["display_name"] if rep["show_name"] else rep["pseudonym"]
        rep["who_note"] = "their name, shown by their choice" if rep["show_name"] else "code name"
        out.append(rep)
    return out


def _has_checked(request_id: int, user_id: int) -> bool:
    with db.session() as conn:
        row = conn.execute("SELECT 1 FROM check_reports WHERE request_id = ? AND checker_id = ?",
                           (request_id, user_id)).fetchone()
    return row is not None


def _why_not_eligible(req: dict, user: dict) -> str | None:
    """None if this member may check this request now, otherwise a plain reason."""
    if req["requester_id"] == user["id"]:
        return "This is your own request. Other members check it, so the record stays independent."
    if _has_checked(req["id"], user["id"]):
        return "You already checked this. Each member checks a request once."
    if req["status"] != "open":
        return "This request is closed. It is not taking more checks."
    if req["done"] >= req["max_checks"]:
        return "This request already has all the checks it asked for."
    return None


def _summary(reports: list[dict]) -> str:
    n = len(reports)
    if n == 0:
        return "Nobody has checked this yet."
    counts = {k: sum(1 for r in reports if r["task_result"] == k) for k in RESULT_WORDS}
    parts = []
    if counts["completed"]:
        parts.append(f"{counts['completed']} could do the task")
    if counts["partly"]:
        parts.append(f"{counts['partly']} could do it partly")
    if counts["could_not"]:
        parts.append(f"{counts['could_not']} could not do it")
    lead = "1 member checked this." if n == 1 else f"{n} members checked this."
    return f"{lead} {', '.join(parts)}."


def _not_checked(reports: list[dict]) -> dict:
    readers_used: set[str] = set()
    devices_used: set[str] = set()
    for r in reports:
        sr = r["screen_reader"].lower()
        for name, keys, platform in KNOWN_SCREEN_READERS:
            if any(k in sr for k in keys):
                readers_used.add(name)
                if platform:
                    devices_used.add(platform)
        text = f"{r['device']} {r['browser']}".lower()
        for name, patterns in KNOWN_DEVICES:
            if any(re.search(p, text) for p in patterns):
                devices_used.add(name)
    appearance = sum(1 for r in reports if APPEARANCE_WORDS.search(f"{r['problems']} {r['heard_first']}"))
    return {
        "screen_readers": [name for name, _, _ in KNOWN_SCREEN_READERS if name not in readers_used],
        "devices": [name for name, _ in KNOWN_DEVICES if name not in devices_used],
        "appearance_mentions": appearance,
    }


# ----- pages -----------------------------------------------------------------------------------

@router.get("")
def index(request: Request):
    user = current_user(request)
    queue: list[dict] = []
    mine: list[dict] = []
    done_by_me: list[dict] = []
    with db.session() as conn:
        if user:
            queue = [dict(r) for r in conn.execute(
                "SELECT r.*, (SELECT COUNT(*) FROM check_reports c WHERE c.request_id = r.id) AS done"
                " FROM check_requests r WHERE r.status = 'open' AND r.requester_id != ?"
                " AND NOT EXISTS (SELECT 1 FROM check_reports c WHERE c.request_id = r.id AND c.checker_id = ?)"
                " AND (SELECT COUNT(*) FROM check_reports c WHERE c.request_id = r.id) < r.max_checks"
                " ORDER BY r.reward_cents DESC, r.id ASC LIMIT 50", (user["id"], user["id"])).fetchall()]
            mine = [dict(r) for r in conn.execute(
                "SELECT r.*, (SELECT COUNT(*) FROM check_reports c WHERE c.request_id = r.id) AS done"
                " FROM check_requests r WHERE r.requester_id = ? ORDER BY r.id DESC", (user["id"],)).fetchall()]
            done_by_me = [dict(r) for r in conn.execute(
                "SELECT r.id, r.title, c.created_at AS checked_at FROM check_reports c"
                " JOIN check_requests r ON r.id = c.request_id WHERE c.checker_id = ? ORDER BY c.id DESC LIMIT 50",
                (user["id"],)).fetchall()]
        recent = [dict(r) for r in conn.execute(
            "SELECT r.id, r.title, COUNT(c.id) AS done, MAX(c.id) AS last FROM check_requests r"
            " JOIN check_reports c ON c.request_id = r.id GROUP BY r.id ORDER BY last DESC LIMIT 10").fetchall()]
    return render(request, "checks/index.html", title="Peer checks", queue=queue, mine=mine,
                  done_by_me=done_by_me, recent=recent, money=_cents_words)


def _new_form(request: Request, user: dict, values: dict, errors: dict, status_code: int = 200):
    title = "Ask for a check: please fix the problems" if errors else "Ask for a check"
    return render(request, "checks/new.html", title=title, values=values, errors=errors,
                  count_options=COUNT_OPTIONS, reward_options=REWARD_OPTIONS,
                  balance=credits.format_cents(credits.balance(user["id"])), status_code=status_code)


@router.get("/new")
def new_form(request: Request, url: str = "", title: str = "", user: dict = Depends(require_user)):
    values = {"title": title.strip()[:LIMITS["title"]], "url": url.strip()[:LIMITS["url"]], "what_to_try": "",
              "max_checks": "3", "reward_cents": "0"}
    return _new_form(request, user, values, {})


@router.post("/new")
async def create_request(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    values = {k: str(form.get(k, "")).strip() for k in ("title", "url", "what_to_try", "max_checks", "reward_cents")}
    errors: dict[str, str] = {}
    if not values["title"]:
        errors["title"] = "Enter a short name for what you want checked."
    elif len(values["title"]) > LIMITS["title"]:
        errors["title"] = f"Use {LIMITS['title']} characters or fewer for the name."
    if not valid_url(values["url"]):
        errors["url"] = "Enter the full web address, starting with https:// or http://."
    elif len(values["url"]) > LIMITS["url"]:
        errors["url"] = "This web address is too long."
    if not values["what_to_try"]:
        errors["what_to_try"] = "Say what you want checkers to try, for example: find the opening hours and book a table."
    elif len(values["what_to_try"]) > LIMITS["what_to_try"]:
        errors["what_to_try"] = f"Use {LIMITS['what_to_try']} characters or fewer."
    for name in ("title", "what_to_try"):
        if name not in errors and (problem := _secret_error(values[name])):
            errors[name] = problem
    if values["max_checks"] not in {v for v, _ in COUNT_OPTIONS}:
        errors["max_checks"] = "Choose how many checks you want, from 1 to 5."
    if values["reward_cents"] not in {v for v, _ in REWARD_OPTIONS}:
        errors["reward_cents"] = "Choose a reward from the list."
    if not errors:
        max_checks, reward = int(values["max_checks"]), int(values["reward_cents"])
        total = max_checks * reward
        have = credits.balance(user["id"])
        if total > have:
            errors["reward_cents"] = (f"You need {credits.format_cents(total)} in credits for this reward, and you have "
                                      f"{credits.format_cents(have)}. Choose a smaller reward, fewer checks, or no reward.")
    if errors:
        return _new_form(request, user, values, errors, status_code=400)

    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO check_requests (requester_id, title, url, what_to_try, max_checks, reward_cents, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
            (user["id"], values["title"], values["url"], values["what_to_try"], max_checks, reward, db.now()))
        request_id = cur.lastrowid
    if total > 0:
        credits.add(user["id"], -total, f"Held for peer checks: {values['title']}", f"check:{request_id}")
        if credits.balance(user["id"]) < 0:
            # Another payment landed at the same moment. Undo this hold and the request, and say so.
            credits.add(user["id"], total, f"Returned, not enough credits: {values['title']}", f"check:{request_id}")
            with db.session() as conn:
                conn.execute("DELETE FROM check_requests WHERE id = ?", (request_id,))
            errors["reward_cents"] = "You do not have enough credits for this reward. Choose a smaller reward or no reward."
            return _new_form(request, user, values, errors, status_code=400)
        flash(request, f"Your request is open. {credits.format_cents(total)} is held from your credits to pay checkers.",
              "success")
    else:
        flash(request, "Your request is open. Other members can now check it.", "success")
    return redirect(f"/checks/{request_id}")


@router.get("/{request_id}")
def request_page(request: Request, request_id: int, user: dict = Depends(require_user)):
    req = _get_request(request_id)
    reason = _why_not_eligible(req, user)
    is_owner = req["requester_id"] == user["id"]
    refund = req["reward_cents"] * max(req["max_checks"] - req["done"], 0) if req["status"] == "open" else 0
    return render(request, "checks/request.html", title=f"Check request: {req['title']}", req=req,
                  eligible=reason is None, reason=reason, is_owner=is_owner, refund=credits.format_cents(refund),
                  refund_cents=refund, money=_cents_words)


@router.post("/{request_id}/close")
async def close_request(request: Request, request_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    req = _get_request(request_id)
    if req["requester_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="We could not find that check request.")
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status, max_checks, reward_cents,"
                           " (SELECT COUNT(*) FROM check_reports c WHERE c.request_id = r.id) AS done"
                           " FROM check_requests r WHERE id = ?", (request_id,)).fetchone()
        if row["status"] != "open":
            refund = None
        else:
            conn.execute("UPDATE check_requests SET status = 'closed', closed_at = ? WHERE id = ?", (db.now(), request_id))
            refund = row["reward_cents"] * max(row["max_checks"] - row["done"], 0)
    if refund is None:
        flash(request, "This request was already closed.", "info")
    elif refund > 0:
        credits.add(user["id"], refund, f"Returned from peer checks: {req['title']}", f"check:{request_id}")
        flash(request, f"This request is closed. {credits.format_cents(refund)} came back to your credits.", "success")
    else:
        flash(request, "This request is closed.", "success")
    return redirect(f"/checks/{request_id}")


def _report_form(request: Request, req: dict, values: dict, errors: dict, status_code: int = 200):
    title = f"Check this: {req['title']}"
    if errors:
        title += ": please fix the problems"
    return render(request, "checks/report.html", title=title, req=req, values=values, errors=errors,
                  result_options=RESULT_OPTIONS, money=_cents_words, status_code=status_code)


def _refuse(request: Request, req: dict, reason: str):
    return render(request, "checks/refused.html", title=f"You cannot check this: {req['title']}", req=req,
                  reason=reason, status_code=403)


@router.get("/{request_id}/report")
def report_form(request: Request, request_id: int, user: dict = Depends(require_user)):
    req = _get_request(request_id)
    if reason := _why_not_eligible(req, user):
        return _refuse(request, req, reason)
    values = {"screen_reader": user.get("screen_reader", ""), "screen_reader_version": "", "browser": "",
              "device": user.get("device", ""), "heard_first": "", "task_result": "", "problems": "",
              "show_name": bool(user.get("show_name_on_checks"))}
    return _report_form(request, req, values, {})


@router.post("/{request_id}/report")
async def save_report(request: Request, request_id: int, user: dict = Depends(require_user)):
    form = await checked_form(request)
    req = _get_request(request_id)
    if reason := _why_not_eligible(req, user):
        return _refuse(request, req, reason)
    values: dict = {k: str(form.get(k, "")).strip() for k in
                    ("screen_reader", "screen_reader_version", "browser", "device", "heard_first", "task_result", "problems")}
    values["show_name"] = form.get("show_name") == "yes"
    errors: dict[str, str] = {}
    required = {"screen_reader": "Enter the screen reader you used, for example NVDA or TalkBack.",
                "browser": "Enter the browser you used, or the app's name if you tested an app.",
                "device": "Enter the device you used, for example an Android phone or a Windows laptop.",
                "heard_first": "Type what you heard first when the page opened. A few words from memory are enough."}
    for name, message in required.items():
        if not values[name]:
            errors[name] = message
    if values["task_result"] not in RESULT_WORDS:
        errors["task_result"] = "Choose whether you could do the task."
    for name, limit in LIMITS.items():
        if name in values and name not in errors and len(values[name]) > limit:
            errors[name] = f"Use {limit} characters or fewer."
    for name in ("screen_reader_version", "browser", "device", "heard_first", "problems"):
        if name not in errors and (problem := _secret_error(values[name])):
            errors[name] = problem
    if errors:
        return _report_form(request, req, values, errors, status_code=400)

    report = {**{k: values[k] for k in values if k != "show_name"}, "checker_id": user["id"],
              "show_name": 1 if values["show_name"] else 0, "created_at": db.now()}
    refusal = None
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")  # one writer at a time, so the chain and the count stay consistent
        fresh = dict(conn.execute("SELECT * FROM check_requests WHERE id = ?", (request_id,)).fetchone())
        done = conn.execute("SELECT COUNT(*) AS n FROM check_reports WHERE request_id = ?", (request_id,)).fetchone()["n"]
        already = conn.execute("SELECT 1 FROM check_reports WHERE request_id = ? AND checker_id = ?",
                               (request_id, user["id"])).fetchone()
        if already:
            refusal = "You already checked this. Each member checks a request once."
        elif fresh["status"] != "open" or done >= fresh["max_checks"]:
            refusal = "This request closed while you were writing. Your check was not saved, and no reward was paid."
        else:
            last = conn.execute("SELECT record_hash FROM check_reports WHERE request_id = ? ORDER BY id DESC LIMIT 1",
                                (request_id,)).fetchone()
            prev_hash = last["record_hash"] if last else ""
            record_hash = compute_hash(fresh, report, prev_hash)
            conn.execute(
                "INSERT INTO check_reports (request_id, checker_id, screen_reader, screen_reader_version, browser, device,"
                " heard_first, task_result, problems, show_name, prev_hash, record_hash, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, user["id"], report["screen_reader"], report["screen_reader_version"], report["browser"],
                 report["device"], report["heard_first"], report["task_result"], report["problems"],
                 report["show_name"], prev_hash, record_hash, report["created_at"]))
            if done + 1 >= fresh["max_checks"]:
                conn.execute("UPDATE check_requests SET status = 'closed', closed_at = ? WHERE id = ?",
                             (db.now(), request_id))
    if refusal:
        req = _get_request(request_id)
        return _refuse(request, req, refusal)
    if fresh["reward_cents"] > 0:
        credits.add(user["id"], fresh["reward_cents"], f"Peer check reward: {fresh['title']}", f"check:{request_id}")
        flash(request, f"Thank you. Your check is saved in the public record, and "
                       f"{credits.format_cents(fresh['reward_cents'])} was added to your credits.", "success")
    else:
        flash(request, "Thank you. Your check is saved in the public record.", "success")
    return redirect(f"/checks/{request_id}/record")


@router.get("/{request_id}/record")
def record_page(request: Request, request_id: int):
    req = _get_request(request_id)
    reports = _reports(request_id)
    problems = verify_chain(req, reports)
    return render(request, "checks/record.html", title=f"Check record: {req['title']}", req=req, reports=reports,
                  summary=_summary(reports), chain_problems=problems, not_checked=_not_checked(reports),
                  result_words=RESULT_WORDS)
