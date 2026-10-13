"""Peer checks: members ask blind and low-vision members to try their website or app with their own screen
reader, braille display or magnifier, and sighted people (friends, family, volunteers) to check how it looks.

What the site itself writes about the look of a member's work leads with outcomes: whether it looks finished,
whether anything is broken or hard to read, whether a visitor would trust it (docs/VOICE.md, 4.4). Checkers'
own words are kept exactly as they typed them, colours included.

Every check is saved in a public record. Each entry carries a fingerprint (SHA-256) that covers the
entry, the request it belongs to and the fingerprint of the entry before it, so any later change to
an entry shows up when the record page recomputes the chain.

Optional rewards are credits: the requester's total is held when the request is made, each checker
is paid when they send their check, and whatever is not paid out comes back when the request closes.
No staff are involved at any step. Every credit movement is written in the same database transaction
as the change it pays for, so a crash or a second click can never hold, pay or return credits twice,
and can never leave a request that pays rewards nobody paid for.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import credits, db
from ..sensitive import find_secret
from ..web import checked_form, current_user, error_title, flash, form_text, redirect, render, require_user

try:  # the one secrets wording (docs/VOICE.md, 4.9), once studio/sensitive.py carries it
    from ..sensitive import PUBLIC_SUFFIX, SECRET_MESSAGE
except ImportError:
    SECRET_MESSAGE = ("This looks like it includes a password, PIN, code, card number or API key, so it was not "
                      "saved or sent to the AI. Please take it out: this site never needs them. If it is not one, "
                      "change the wording a little, for example \"the PIN box is not announced\", and send it again.")
    PUBLIC_SUFFIX = "Everyone can read this page."

router = APIRouter(prefix="/checks")
NAV = {"label": 'Peer checks', "url": '/checks', "description": "Ask blind and low-vision members to test your website or app with their own screen reader, braille display or magnifier, and sighted people to check how it looks. Every check is saved in the checker's own words, in a public record anyone can verify."}

# One table definition, used for new databases and when seed() rebuilds a table made before sighted checks.
# A sighted check has no task result and three answers about the look; a screen reader check is the opposite.
REPORTS_TABLE = """CREATE TABLE IF NOT EXISTS {table} (
    id INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES check_requests(id),
    checker_id INTEGER NOT NULL REFERENCES users(id),
    screen_reader TEXT NOT NULL,
    screen_reader_version TEXT NOT NULL DEFAULT '',
    browser TEXT NOT NULL,
    device TEXT NOT NULL,
    heard_first TEXT NOT NULL,
    task_result TEXT NOT NULL CHECK (task_result IN ('completed', 'partly', 'could_not', '')),
    problems TEXT NOT NULL DEFAULT '',
    show_name INTEGER NOT NULL DEFAULT 0,
    prev_hash TEXT NOT NULL DEFAULT '',
    record_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'screen_reader' CHECK (method IN ('screen_reader', 'sighted')),
    looks_finished TEXT NOT NULL DEFAULT '' CHECK (looks_finished IN ('yes', 'no', 'not_sure', '')),
    anything_broken TEXT NOT NULL DEFAULT '' CHECK (anything_broken IN ('yes', 'no', 'not_sure', '')),
    would_trust TEXT NOT NULL DEFAULT '' CHECK (would_trust IN ('yes', 'no', 'not_sure', '')),
    improve TEXT NOT NULL DEFAULT '',
    CHECK ((method = 'screen_reader' AND task_result != '') OR (method = 'sighted' AND task_result = ''
           AND looks_finished != '' AND anything_broken != '' AND would_trust != '')),
    UNIQUE (request_id, checker_id)
);
"""

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
    closed_at TEXT,
    wanted TEXT NOT NULL DEFAULT 'screen_reader' CHECK (wanted IN ('screen_reader', 'sighted', 'both'))
);
CREATE INDEX IF NOT EXISTS idx_check_requests_requester ON check_requests(requester_id);
CREATE INDEX IF NOT EXISTS idx_check_requests_status ON check_requests(status);
""" + REPORTS_TABLE.replace("{table}", "check_reports") + """\
CREATE INDEX IF NOT EXISTS idx_check_reports_checker ON check_reports(checker_id);
CREATE TABLE IF NOT EXISTS check_seen (
    user_id INTEGER NOT NULL REFERENCES users(id),
    request_id INTEGER NOT NULL REFERENCES check_requests(id),
    last_report_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, request_id)
);
"""


# ----- choices and limits ----------------------------------------------------------------------

COUNT_OPTIONS = [(str(n), f"{n} check" if n == 1 else f"{n} checks") for n in range(1, 6)]
REWARD_OPTIONS = [("0", "No reward"), ("25", "25 cents per check"), ("50", "50 cents per check"),
                  ("100", "1 dollar per check")]
RESULT_OPTIONS = [("completed", "Yes, I could do it"), ("partly", "Partly"), ("could_not", "No, I could not do it")]
RESULT_WORDS = {"completed": "Could do the task", "partly": "Could do it partly", "could_not": "Could not do the task"}

# Who the member wants to check their work. Requests made before this choice existed asked screen reader users.
TOOLS = "screen reader, braille display or magnifier"
WANTED_OPTIONS = [("screen_reader", f"Blind and low-vision people, with their own {TOOLS}"),
                  ("sighted", "Sighted people, to check how it looks"), ("both", "Both")]
WANTED_SENTENCE = {
    "screen_reader": f"This request asks blind and low-vision people to try the task with their own {TOOLS}.",
    "sighted": "This request asks sighted people to check how it looks.",
    "both": f"This request asks blind and low-vision people to try the task with their own {TOOLS}, and sighted "
            "people to check how it looks.",
}
# Who a request is for, in every link name and title, so two requests for the same page never share a name.
WANTED_WHO = {"screen_reader": "for blind and low-vision checkers", "sighted": "for sighted checkers",
              "both": "for blind, low-vision and sighted checkers"}

# How one checker checked. A sighted check answers three questions about the look, in outcome words: the
# member who reads the record may not see the page, so the site's own words say what effect the look has.
METHOD_OPTIONS = [("screen_reader", f"With a {TOOLS}"), ("sighted", "I am sighted and looked at how it appears")]
ANSWER_OPTIONS = [("yes", "Yes"), ("no", "No"), ("not_sure", "Not sure")]
ANSWER_WORDS = dict(ANSWER_OPTIONS)
LOOK_QUESTIONS = [
    ("looks_finished", "Does it look finished and professional?",
     "Choose whether it looks finished and professional: yes, no or not sure."),
    ("anything_broken", "Is anything broken, overlapping or hard to read?",
     "Choose whether anything is broken, overlapping or hard to read: yes, no or not sure."),
    ("would_trust", "Would you trust this page as a visitor or customer?",
     "Choose whether you would trust this page as a visitor or customer: yes, no or not sure."),
]
GOOD_ANSWER = {"looks_finished": "yes", "anything_broken": "no", "would_trust": "yes"}
# What each answer means for the member, said as an outcome. "good" and "bad" follow a count ("1 said ...");
# "all_good" and "all_unsure" make the whole clause for n checkers who all answered the same way.
LOOK_PHRASES = {
    "looks_finished": {
        "good": "said it looks finished and professional",
        "bad": "said it does not look finished yet",
        "all_good": lambda n: f"{_everyone(n)} said it looks finished and professional",
        "all_unsure": lambda n: f"{_no_one(n)} sure whether it looks finished and professional"},
    "anything_broken": {
        "good": "found nothing broken or hard to read",
        "bad": "found something broken, overlapping or hard to read",
        "all_good": lambda n: {1: "they found nothing broken or hard to read",
                               2: "neither found anything broken or hard to read"}.get(
                                   n, f"none of the {n} found anything broken or hard to read"),
        "all_unsure": lambda n: f"{_no_one(n)} sure whether anything is broken or hard to read"},
    "would_trust": {
        "good": "would trust it as a visitor or customer",
        "bad": "would not trust it yet",
        "all_good": lambda n: f"{_everyone(n)} would trust it as a visitor or customer",
        "all_unsure": lambda n: f"{_no_one(n)} sure whether they would trust it as a visitor or customer"},
}
SIGHTED_FIELDS = ("method", "looks_finished", "anything_broken", "would_trust", "improve")
SCREEN_READER_FIELDS = ("screen_reader", "screen_reader_version", "browser", "device", "heard_first", "task_result",
                        "problems")

LIMITS = {"title": 150, "url": 2000, "what_to_try": 4000, "screen_reader": 80, "screen_reader_version": 40,
          "browser": 80, "device": 120, "heard_first": 4000, "problems": 8000, "improve": 8000}
TEXT_CAP = 20_000  # longer than any limit above, so a too-long answer is still reported with its length
MAX_ID = 2 ** 63 - 1  # SQLite's largest integer; a bigger number in an address cannot be a request

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

# Addresses are public, so refuse one that carries sign-in details in its query, like ?password=x.
SECRET_RE = re.compile(r"\b(password|passwd|pwd|passcode|pin|one[- ]time code|otp|verification code)\s*(is|:|=)\s*\S", re.I)


def _secret_error(text: str, account_note: bool = False) -> str | None:
    """Records are public, so text that looks like a password, PIN, code or card number is not saved. The
    shared detector allows for its own mistakes ("the PIN box is not announced" is a normal report)."""
    if find_secret(text):
        note = " If the task needs an account, ask checkers to make their own test account." if account_note else ""
        return f"{SECRET_MESSAGE} {PUBLIC_SUFFIX}{note}"
    return None


def _text(form: dict, name: str) -> str:
    """One text value from the form. A field sent twice (only a crafted form does that) counts as empty."""
    if isinstance(form.get(name, ""), list):
        return ""
    return form_text(form, name, TEXT_CAP).strip()


def valid_url(url: str) -> bool:
    """Only http and https addresses with a host name, and no spaces."""
    if not url or any(c.isspace() for c in url):
        return False
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except ValueError:
        return False
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc) and bool(host)


def _url_error(url: str) -> str | None:
    if not valid_url(url):
        return "Enter the full web address, starting with https:// or http://."
    if len(url) > LIMITS["url"]:
        return f"This web address is too long. Use {LIMITS['url']} characters or fewer."
    parsed = urlparse(url)
    if parsed.username or parsed.password or SECRET_RE.search(url):
        return ("This address includes a user name, password or code, and everyone can read check records. "
                "Please take that part out and give the plain address.")
    return None


def _cents_words(cents: int) -> str:
    if cents == 100:
        return "1 dollar"
    if cents % 100 == 0:
        return f"{cents // 100} dollars"
    if cents < 100:
        return f"{cents} cents"
    return credits.format_cents(cents)


# ----- start-up --------------------------------------------------------------------------------

def seed() -> None:
    """Bring a database made before sighted checks up to date. Safe to run on every start.

    Requests gain the kind of check wanted. The reports table needs new columns and a looser rule for the
    task result (a sighted check has none), and SQLite cannot change a rule in place, so the table is
    copied into a new one with the same rows, ids and fingerprints, in one transaction.
    """
    with db.session() as conn:
        db.ensure_column(conn, "check_requests", "wanted",
                         "TEXT NOT NULL DEFAULT 'screen_reader' CHECK (wanted IN ('screen_reader', 'sighted', 'both'))")
    with db.session() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")  # only takes effect outside a transaction
        conn.execute("BEGIN IMMEDIATE")
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(check_reports)")]
        if "method" in columns:
            conn.execute("ROLLBACK")
            return
        conn.execute("DROP TABLE IF EXISTS check_reports_new")
        conn.execute(REPORTS_TABLE.replace("{table}", "check_reports_new"))
        names = ", ".join(columns)
        conn.execute(f"INSERT INTO check_reports_new ({names}) SELECT {names} FROM check_reports")
        conn.execute("DROP TABLE check_reports")
        conn.execute("ALTER TABLE check_reports_new RENAME TO check_reports")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_check_reports_checker ON check_reports(checker_id)")


# ----- record fingerprints ---------------------------------------------------------------------

HASHED_REPORT_FIELDS = ("checker_id", "screen_reader", "screen_reader_version", "browser", "device", "heard_first",
                        "task_result", "problems", "show_name", "created_at")


def compute_hash(request_row: dict, report: dict, prev_hash: str) -> str:
    """SHA-256 over canonical JSON (sorted keys) of the report, its request and the previous entry's hash.

    A sighted check also covers how it was checked, its three answers and its suggestion. Screen reader
    checks hash exactly as they did before sighted checks existed, so every older record still verifies.
    """
    payload = {k: report[k] for k in HASHED_REPORT_FIELDS}
    if report.get("method", "screen_reader") != "screen_reader":
        payload.update({k: report[k] for k in SIGHTED_FIELDS})
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


# ----- money -----------------------------------------------------------------------------------
# These read and write the credit ledger on the caller's connection, inside its transaction, so a
# credit change and the request or check it belongs to are saved together or not at all.
# credits.add opens a connection of its own and cannot take part in a transaction already open.

def _balance_in(conn, user_id: int) -> int:
    row = conn.execute("SELECT COALESCE(SUM(delta_cents), 0) AS b FROM credit_ledger WHERE user_id = ?",
                       (user_id,)).fetchone()
    return int(row["b"])


def _ledger(conn, user_id: int, delta_cents: int, reason: str, ref: str) -> None:
    conn.execute("INSERT INTO credit_ledger (user_id, delta_cents, reason, ref, created_at) VALUES (?, ?, ?, ?, ?)",
                 (user_id, int(delta_cents), reason, ref, db.now()))


# ----- data helpers ----------------------------------------------------------------------------

def label(req: dict) -> str:
    """The request's name in every link and title: its own name, its number and who it is for, so two requests
    for the same page never share a name. For example "Choir rota, version 2 (request 4, for sighted checkers)"."""
    return f"{req['title']} (request {req['id']}, {WANTED_WHO.get(req.get('wanted') or 'screen_reader')})"


def _get_request(request_id: int) -> dict:
    if not 0 < request_id <= MAX_ID:
        raise HTTPException(status_code=404, detail="We could not find that check request.")
    with db.session() as conn:
        row = conn.execute(
            "SELECT r.*, u.pseudonym AS asker, (SELECT COUNT(*) FROM check_reports c WHERE c.request_id = r.id) AS done"
            " FROM check_requests r JOIN users u ON u.id = r.requester_id WHERE r.id = ?", (request_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="We could not find that check request.")
    return dict(row)


QUEUE_SQL = (
    "SELECT r.*, (SELECT COUNT(*) FROM check_reports c WHERE c.request_id = r.id) AS done"
    " FROM check_requests r WHERE r.status = 'open' AND r.requester_id != ?"
    " AND NOT EXISTS (SELECT 1 FROM check_reports c WHERE c.request_id = r.id AND c.checker_id = ?)"
    " AND (SELECT COUNT(*) FROM check_reports c WHERE c.request_id = r.id) < r.max_checks"
    " ORDER BY r.reward_cents DESC, r.id ASC LIMIT 50")


def _queue(conn, user_id: int) -> list[dict]:
    """Open requests this member can check now, the best rewarded first."""
    return [dict(r) for r in conn.execute(QUEUE_SQL, (user_id, user_id)).fetchall()]


def _reports(request_id: int) -> list[dict]:
    with db.session() as conn:
        rows = conn.execute(
            "SELECT c.*, u.display_name, u.pseudonym, u.email FROM check_reports c JOIN users u ON u.id = c.checker_id"
            " WHERE c.request_id = ? ORDER BY c.id", (request_id,)).fetchall()
    out = []
    for r in rows:
        rep = dict(r)
        if rep.pop("email").endswith("@invalid"):
            # A deleted account's name is gone. "Former member" is not the name they chose to show.
            rep["who"], rep["who_note"] = rep["pseudonym"], "code name; this member has since left the site"
        elif rep["show_name"]:
            rep["who"], rep["who_note"] = rep["display_name"], "their name, shown by their choice"
        else:
            rep["who"], rep["who_note"] = rep["pseudonym"], "code name"
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


def is_sighted(report: dict) -> bool:
    return report.get("method") == "sighted"


def _people(n: int) -> str:
    return "1 sighted person" if n == 1 else f"{n} sighted people"


def _everyone(n: int) -> str:
    return {1: "they", 2: "both"}.get(n, f"all {n}")


def _no_one(n: int) -> str:
    return {1: "they were not", 2: "neither was"}.get(n, f"none of the {n} was")


def _look_clause(question: str, answers: list[str]) -> str:
    """One question's answers from every sighted checker, as an outcome, for example
    "both said it looks finished and professional" or "1 found something broken, overlapping or hard to read"."""
    n = len(answers)
    phrases = LOOK_PHRASES[question]
    good = sum(1 for a in answers if a == GOOD_ANSWER[question])
    unsure = sum(1 for a in answers if a == "not_sure")
    bad = n - good - unsure
    if good == n:
        return phrases["all_good"](n)
    if bad == n:
        return f"{_everyone(n)} {phrases['bad']}"
    if unsure == n:
        return phrases["all_unsure"](n)
    parts = []
    if good:
        parts.append(f"{good} {phrases['good']}")
    if bad:
        parts.append(f"{bad} {phrases['bad']}")
    if unsure:
        parts.append(f"{unsure} {'was' if unsure == 1 else 'were'} not sure")
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _sentence(text: str) -> str:
    return text[0].upper() + text[1:] + "."


def all_good(report: dict) -> bool:
    return all(report[q] == good for q, good in GOOD_ANSWER.items())


def any_bad(report: dict) -> bool:
    return any(report[q] not in (good, "not_sure") for q, good in GOOD_ANSWER.items())


def look_summary(sighted: list[dict]) -> str:
    """What the sighted checks say about the look, as outcomes: finished, easy to read, trustworthy."""
    n = len(sighted)
    clauses = {q: _look_clause(q, [r[q] for r in sighted]) for q, _, _ in LOOK_QUESTIONS}
    first, second = clauses["looks_finished"], clauses["anything_broken"]
    mixed = any(len({r[q] for r in sighted}) > 1 for q in ("looks_finished", "anything_broken"))
    if mixed:  # different answers read more easily as sentences of their own
        opening = _sentence(first) + " " + _sentence(second)
    else:
        opening = _sentence(f"{first}, and {second}")
    text = f"{_people(n)} checked how it looks. {opening} {_sentence(clauses['would_trust'])} "
    if all(all_good(r) for r in sighted):
        text += "Every answer was positive: to them, the page looks well made."
    elif any(any_bad(r) for r in sighted):
        text += "There is something to fix. What they would make better is below, in their own words."
    else:
        text += "Nobody found anything wrong, but not every answer was sure. Their own words are below."
    return text


def look_verdict(report: dict) -> str:
    """A few words for one sighted check's heading."""
    if any_bad(report):
        return "found something to fix"
    if not all_good(report):
        return "found nothing wrong, not sure of everything"
    return "looks well made"


def _summary(req: dict, reports: list[dict]) -> list[str]:
    """The record's summary, one paragraph for each kind of check, screen reader checks first."""
    if not reports:
        return ["Nobody has checked this yet."]
    paras = []
    screen = [r for r in reports if not is_sighted(r)]
    sighted = [r for r in reports if is_sighted(r)]
    wanted = req.get("wanted") or "screen_reader"
    if screen:
        counts = {k: sum(1 for r in screen if r["task_result"] == k) for k in RESULT_WORDS}
        parts = []
        if counts["completed"]:
            parts.append(f"{counts['completed']} could do the task")
        if counts["partly"]:
            parts.append(f"{counts['partly']} could do it partly")
        if counts["could_not"]:
            parts.append(f"{counts['could_not']} could not do it")
        lead = (f"1 member tried the task with a {TOOLS}." if len(screen) == 1
                else f"{len(screen)} members tried the task with a {TOOLS}.")
        paras.append(f"{lead} {_sentence(', '.join(parts))}")
    elif wanted in ("screen_reader", "both"):
        paras.append(f"Nobody has tried the task with a {TOOLS} yet.")
    if sighted:
        paras.append(look_summary(sighted))
    elif wanted in ("sighted", "both"):
        paras.append("No sighted person has checked how it looks yet.")
    return paras


def _not_checked(reports: list[dict]) -> dict:
    screen = [r for r in reports if not is_sighted(r)]
    readers_used: set[str] = set()
    devices_used: set[str] = set()
    for r in screen:
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
    appearance = sum(1 for r in screen if APPEARANCE_WORDS.search(f"{r['problems']} {r['heard_first']}"))
    return {
        "screen_reader_checks": len(screen),
        "screen_readers": [name for name, _, _ in KNOWN_SCREEN_READERS if name not in readers_used],
        "devices": [name for name, _ in KNOWN_DEVICES if name not in devices_used],
        "appearance_mentions": appearance,
        "sighted_checks": len(reports) - len(screen),
    }


# ----- closing requests ------------------------------------------------------------------------

def _close_and_refund(conn, row) -> int:
    """Close one open request inside the caller's transaction and return what was not paid out to the member
    who asked. `row` needs id, requester_id, title, max_checks, reward_cents and done. Returns the refund."""
    conn.execute("UPDATE check_requests SET status = 'closed', closed_at = ? WHERE id = ?", (db.now(), row["id"]))
    refund = row["reward_cents"] * max(row["max_checks"] - row["done"], 0)
    if refund > 0:
        _ledger(conn, row["requester_id"], refund, f"Returned from peer checks: {row['title']}", f"check:{row['id']}")
    return refund


ROW_SQL = ("SELECT r.id, r.requester_id, r.title, r.url, r.wanted, r.status, r.max_checks, r.reward_cents,"
           " r.created_at, (SELECT COUNT(*) FROM check_reports c WHERE c.request_id = r.id) AS done"
           " FROM check_requests r")


def requests_for_address(url_prefix: str, open_only: bool = False) -> list[dict]:
    """Every check request whose address starts with url_prefix, newest first, for another section to list.

    For Build, url_prefix is a preview address such as "https://site/build/p/<token>/v/2/" (one version) or
    "https://site/build/p/<token>/" (the whole project). Each item has id, title, wanted, status, done,
    max_checks, `label` (the name to use in links) and `record_url`."""
    if not url_prefix:
        return []
    sql = ROW_SQL + " WHERE substr(r.url, 1, ?) = ?" + (" AND r.status = 'open'" if open_only else "")
    with db.session() as conn:
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY r.id DESC", (len(url_prefix), url_prefix))]
    for r in rows:
        r["label"], r["record_url"] = label(r), f"/checks/{r['id']}/record"
    return rows


def close_requests_for_address(url_prefix: str) -> int:
    """Close every open request whose address starts with url_prefix, for example when Build deletes the project
    the address belongs to. Credits held for checks not done go back to whoever asked, in the same transaction.
    Safe to call twice. Returns how many requests were closed."""
    if not url_prefix:
        return 0
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(ROW_SQL + " WHERE substr(r.url, 1, ?) = ? AND r.status = 'open'",
                            (len(url_prefix), url_prefix)).fetchall()
        for row in rows:
            _close_and_refund(conn, row)
    return len(rows)


# ----- results back to the work ----------------------------------------------------------------

PREVIEW_PATH = re.compile(r"^/build/p/([A-Za-z0-9_-]{1,100})/v/([0-9]{1,9})/")
FIX_LIMIT = 9_000  # Build takes change requests under 10,000 characters


def _own_build_version(request: Request, url: str, user_id: int) -> tuple[int, int] | None:
    """(project id, version number) if the address is a preview of this member's own Build project."""
    parsed = urlparse(url)
    settings = request.app.state.settings
    hosts = {urlparse(base).netloc for base in (settings.base_url, settings.preview_base_url) if base}
    match = PREVIEW_PATH.match(parsed.path)
    if parsed.netloc not in hosts or not match:
        return None
    try:
        with db.session() as conn:
            row = conn.execute(
                "SELECT p.id FROM build_projects p JOIN build_versions v ON v.project_id = p.id"
                " WHERE p.share_token = ? AND p.user_id = ? AND v.number = ?",
                (match.group(1), user_id, int(match.group(2)))).fetchone()
    except sqlite3.Error:  # Build is not part of this site
        return None
    return (row["id"], int(match.group(2))) if row else None


def fix_request_text(req: dict, reports: list[dict], number: int) -> str:
    """A change request for Build, written from what checkers found, with their own words kept as they typed
    them. Empty when nobody found anything to fix."""
    found: list[str] = []
    for n, r in enumerate(reports, start=1):
        if is_sighted(r):
            if not (any_bad(r) or r["improve"]):
                continue
            found += ["", f"Check {n}, by a sighted person looking at how it appears: {look_verdict(r)}."]
            found += [f"- {question} {ANSWER_WORDS[r[name]]}." for name, question, _ in LOOK_QUESTIONS]
            if r["improve"]:
                found += ["What would make it better, in their words:", r["improve"]]
        else:
            if r["task_result"] == "completed" and not r["problems"]:
                continue
            found += ["", f"Check {n}, with {r['screen_reader']} on {r['browser']}, {r['device']}: "
                          f"{RESULT_WORDS[r['task_result']].lower()}."]
            if r["problems"]:
                found += ["Problems they found, in their words:", r["problems"]]
    if not found:
        return ""
    text = "\n".join([f"Peer checkers tried version {number}. Fix the problems they found and change nothing else.",
                      "", "The task they tried:", req["what_to_try"], *found])
    if len(text) > FIX_LIMIT:
        text = text[:FIX_LIMIT].rstrip() + "\n\n(Shortened here. Every check is on the check record.)"
    return text


# ----- pages -----------------------------------------------------------------------------------

@router.get("")
def index(request: Request):
    user = current_user(request)
    queue: list[dict] = []
    mine: list[dict] = []
    done_by_me: list[dict] = []
    new_checks: list[dict] = []
    with db.session() as conn:
        if user:
            queue = _queue(conn, user["id"])
            mine = [dict(r) for r in conn.execute(
                "SELECT r.*, (SELECT COUNT(*) FROM check_reports c WHERE c.request_id = r.id) AS done"
                " FROM check_requests r WHERE r.requester_id = ? ORDER BY r.id DESC", (user["id"],)).fetchall()]
            done_by_me = [dict(r) for r in conn.execute(
                "SELECT r.id, r.title, r.wanted, c.created_at AS checked_at FROM check_reports c"
                " JOIN check_requests r ON r.id = c.request_id WHERE c.checker_id = ? ORDER BY c.id DESC LIMIT 50",
                (user["id"],)).fetchall()]
            # Checks on this member's requests that came in after they last opened that request's record.
            new_checks = [dict(r) for r in conn.execute(
                "SELECT r.id, r.title, r.wanted, COUNT(c.id) AS new_count FROM check_requests r"
                " JOIN check_reports c ON c.request_id = r.id"
                " LEFT JOIN check_seen s ON s.user_id = r.requester_id AND s.request_id = r.id"
                " WHERE r.requester_id = ? AND c.id > COALESCE(s.last_report_id, 0)"
                " GROUP BY r.id ORDER BY MAX(c.id) DESC", (user["id"],)).fetchall()]
        recent = [dict(r) for r in conn.execute(
            "SELECT r.id, r.title, r.wanted, COUNT(c.id) AS done, MAX(c.id) AS last FROM check_requests r"
            " JOIN check_reports c ON c.request_id = r.id GROUP BY r.id ORDER BY last DESC LIMIT 10").fetchall()]
    return render(request, "checks/index.html", title="Peer checks", queue=queue, mine=mine,
                  mine_open=[r for r in mine if r["status"] == "open"],
                  mine_closed=[r for r in mine if r["status"] != "open"],
                  new_checks=new_checks, new_total=sum(r["new_count"] for r in new_checks),
                  done_by_me=done_by_me, recent=recent, money=_cents_words, label=label)


def _radio_errors(errors: dict) -> dict:
    """The error summary links to a radio group's first answer, which can take focus; the group cannot."""
    radio_groups = {"task_result", "method", "wanted", *GOOD_ANSWER}
    return {(f"{k}-1" if k in radio_groups else k): v for k, v in errors.items()}


def _new_form(request: Request, user: dict, values: dict, errors: dict, status_code: int = 200):
    return render(request, "checks/new.html", title=error_title("Ask for a check", errors), values=values,
                  errors=errors, summary_errors=_radio_errors(errors), count_options=COUNT_OPTIONS,
                  reward_options=REWARD_OPTIONS, wanted_options=WANTED_OPTIONS,
                  balance=credits.format_cents(credits.balance(user["id"])), status_code=status_code)


@router.get("/new")
def new_form(request: Request, url: str = "", title: str = "", kind: str = "",
             user: dict = Depends(require_user)):
    values = {"title": title.strip()[:LIMITS["title"]], "url": url.strip()[:LIMITS["url"]], "what_to_try": "",
              "max_checks": "3", "reward_cents": "0",
              "wanted": kind if kind in WANTED_SENTENCE else "both"}
    return _new_form(request, user, values, {})


@router.post("/new")
async def create_request(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    values = {k: _text(form, k) for k in ("title", "url", "what_to_try", "max_checks", "reward_cents", "wanted")}
    errors: dict[str, str] = {}
    if not values["title"]:
        errors["title"] = "Enter a short name for what you want checked."
    elif len(values["title"]) > LIMITS["title"]:
        errors["title"] = (f"This name is {len(values['title'])} characters long. "
                           f"Shorten it to {LIMITS['title']} characters or fewer.")
    if problem := _url_error(values["url"]):
        errors["url"] = problem
    if not values["what_to_try"]:
        errors["what_to_try"] = "Say what you want checkers to try, for example: find the opening hours and book a table."
    elif len(values["what_to_try"]) > LIMITS["what_to_try"]:
        errors["what_to_try"] = (f"This is {len(values['what_to_try'])} characters long. "
                                 f"Shorten it to {LIMITS['what_to_try']} characters or fewer.")
    for name in ("title", "what_to_try"):
        if name not in errors and (problem := _secret_error(values[name], account_note=name == "what_to_try")):
            errors[name] = problem
    if values["wanted"] not in WANTED_SENTENCE:
        errors["wanted"] = "Choose who should check it: blind and low-vision people, sighted people, or both."
    if values["max_checks"] not in {v for v, _ in COUNT_OPTIONS}:
        errors["max_checks"] = "Choose how many checks you want, from 1 to 5."
    if values["reward_cents"] not in {v for v, _ in REWARD_OPTIONS}:
        errors["reward_cents"] = "Choose a reward from the list."
    if errors:
        return _new_form(request, user, values, errors, status_code=400)

    max_checks, reward = int(values["max_checks"]), int(values["reward_cents"])
    total = max_checks * reward
    duplicate = short_of = request_id = None
    with db.session() as conn:
        # One writer at a time: the duplicate check, the balance, the request and the hold are one unit.
        conn.execute("BEGIN IMMEDIATE")
        same = conn.execute(
            "SELECT id FROM check_requests WHERE requester_id = ? AND status = 'open' AND title = ? AND url = ?"
            " AND what_to_try = ? AND max_checks = ? AND reward_cents = ? AND wanted = ? ORDER BY id DESC LIMIT 1",
            (user["id"], values["title"], values["url"], values["what_to_try"], max_checks, reward,
             values["wanted"])).fetchone()
        have = _balance_in(conn, user["id"])
        if same:
            duplicate = same["id"]  # the same form sent twice, for example after a double press
        elif total > have:
            short_of = have
        else:
            cur = conn.execute(
                "INSERT INTO check_requests (requester_id, title, url, what_to_try, max_checks, reward_cents, status,"
                " created_at, wanted) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                (user["id"], values["title"], values["url"], values["what_to_try"], max_checks, reward, db.now(),
                 values["wanted"]))
            request_id = cur.lastrowid
            if total > 0:
                _ledger(conn, user["id"], -total, f"Held for peer checks: {values['title']}", f"check:{request_id}")
    if duplicate:
        flash(request, "You already asked for this check and it is open. It was saved once"
                       + (", and credits were set aside once." if total else "."), "info")
        return redirect(f"/checks/{duplicate}")
    if short_of is not None:
        errors["reward_cents"] = (f"You need {credits.format_cents(total)} in credits for this reward, and you have "
                                  f"{credits.format_cents(short_of)}. Choose a smaller reward, fewer checks, or no reward.")
        return _new_form(request, user, values, errors, status_code=400)
    if total > 0:
        flash(request, f"Other members can now check it. {credits.format_cents(total)} is set aside from your "
                       "credits to pay checkers.", "success")
    else:
        flash(request, "Other members can now check it.", "success")
    return redirect(f"/checks/{request_id}")


@router.get("/{request_id}")
def request_page(request: Request, request_id: int, user: dict = Depends(require_user)):
    req = _get_request(request_id)
    reason = _why_not_eligible(req, user)
    is_owner = req["requester_id"] == user["id"]
    not_done = max(req["max_checks"] - req["done"], 0)
    if is_owner:
        # The h1 says the request's state, so right after asking or closing it says what just happened.
        title = f"Your request is {'open' if req['status'] == 'open' else 'closed'}: {label(req)}"
    else:
        title = f"Check request: {label(req)}"
    base = request.app.state.settings.base_url.rstrip('/')
    return render(request, "checks/request.html", title=title, req=req, eligible=reason is None, reason=reason,
                  is_owner=is_owner, not_done_cents=req["reward_cents"] * not_done,
                  not_done=credits.format_cents(req["reward_cents"] * not_done),
                  paid=credits.format_cents(req["reward_cents"] * req["done"]), money=_cents_words,
                  wanted_sentence=WANTED_SENTENCE[req["wanted"]],
                  share_url=f"{base}/checks/{req['id']}", record_url=f"{base}/checks/{req['id']}/record")


@router.post("/{request_id}/close")
async def close_request(request: Request, request_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    req = _get_request(request_id)
    if req["requester_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="We could not find that check request.")
    with db.session() as conn:
        # Closing and returning the credits are one unit, so the refund happens exactly once.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(ROW_SQL + " WHERE r.id = ?", (request_id,)).fetchone()
        refund = None if row["status"] != "open" else _close_and_refund(conn, row)
    if refund is None:
        flash(request, "This request was already closed. Nothing changed.", "info")
    elif refund > 0:
        flash(request, f"Nobody else can check it now. {credits.format_cents(refund)} came back to your credits.",
              "success")
    else:
        flash(request, "Nobody else can check it now.", "success")
    return redirect(f"/checks/{request_id}")


def _report_form(request: Request, req: dict, values: dict, errors: dict, status_code: int = 200):
    return render(request, "checks/report.html", title=error_title(f"Check this: {label(req)}", errors), req=req,
                  values=values, errors=errors, summary_errors=_radio_errors(errors), result_options=RESULT_OPTIONS,
                  money=_cents_words, method_options=METHOD_OPTIONS, answer_options=ANSWER_OPTIONS,
                  look_questions=LOOK_QUESTIONS, wanted_sentence=WANTED_SENTENCE[req["wanted"]],
                  status_code=status_code)


# The words a checker typed, shown back if their check could not be saved, so nothing they wrote is lost.
KEPT_LABELS = [("heard_first", "What your screen reader said or showed first"), ("problems", "Problems you found"),
               ("improve", "What would make it better")]


def _refuse(request: Request, req: dict, reason: str, kept: list[tuple[str, str]] | None = None):
    return render(request, "checks/refused.html", title=f"This check is not open to you: {label(req)}", req=req,
                  reason=reason, kept=kept or [], status_code=403)


def _already_saved(request: Request, request_id: int):
    """The same check sent twice, for example after a double press: nothing new is saved or paid."""
    flash(request, "You sent this check before. It is saved once in the record, and any reward was paid once.", "info")
    return redirect(f"/checks/{request_id}/record?sent=1")


@router.get("/{request_id}/report")
def report_form(request: Request, request_id: int, user: dict = Depends(require_user)):
    req = _get_request(request_id)
    if reason := _why_not_eligible(req, user):
        return _refuse(request, req, reason)
    values = {"screen_reader": user.get("screen_reader", ""), "screen_reader_version": "", "browser": "",
              "device": user.get("device", ""), "heard_first": "", "task_result": "", "problems": "",
              "method": "" if req["wanted"] == "both" else req["wanted"],
              "looks_finished": "", "anything_broken": "", "would_trust": "", "improve": "",
              "show_name": bool(user.get("show_name_on_checks"))}
    return _report_form(request, req, values, {})


def _check_errors(values: dict) -> dict[str, str]:
    """Validation for the way this member checked. The other set of fields is not looked at, or saved."""
    errors: dict[str, str] = {}
    if values["method"] == "screen_reader":
        required = {"screen_reader": "Enter the screen reader you used, for example NVDA or TalkBack.",
                    "browser": "Enter the browser you used, or the app's name if you tested an app.",
                    "device": "Enter the device you used, for example an Android phone or a Windows laptop.",
                    "heard_first": "Type what your screen reader said or showed first when the page opened. "
                                   "A few words from memory are enough."}
        for name, message in required.items():
            if not values[name]:
                errors[name] = message
        if values["task_result"] not in RESULT_WORDS:
            errors["task_result"] = "Choose whether you could do the task: yes, partly or no."
        texts = ("screen_reader", "screen_reader_version", "browser", "device", "heard_first", "problems")
    elif values["method"] == "sighted":
        for name, _, message in LOOK_QUESTIONS:
            if values[name] not in ANSWER_WORDS:
                errors[name] = message
        texts = ("improve",)
    else:
        return {"method": f"Choose how you checked it: with a {TOOLS}, or as a sighted person looking at how it "
                          "appears."}
    for name in texts:
        if name not in errors and len(values[name]) > LIMITS[name]:
            errors[name] = (f"This is {len(values[name])} characters long. "
                            f"Shorten it to {LIMITS[name]} characters or fewer.")
        if name not in errors and (problem := _secret_error(values[name])):
            errors[name] = problem
    return errors


@router.post("/{request_id}/report")
async def save_report(request: Request, request_id: int, user: dict = Depends(require_user)):
    form = await checked_form(request)
    req = _get_request(request_id)
    if _has_checked(request_id, user["id"]):
        return _already_saved(request, request_id)
    values: dict = {k: _text(form, k) for k in SCREEN_READER_FIELDS + SIGHTED_FIELDS}
    values["show_name"] = form.get("show_name") == "yes"
    kept = [(words, values[name]) for name, words in KEPT_LABELS if values[name]]
    if reason := _why_not_eligible(req, user):
        return _refuse(request, req, reason, kept)
    if req["wanted"] != "both":
        values["method"] = req["wanted"]  # this request asked for one kind of check only
    errors = _check_errors(values)
    if errors:
        return _report_form(request, req, values, errors, status_code=400)

    # Only the fields for the way they checked are saved; the others are stored empty.
    keep = SCREEN_READER_FIELDS if values["method"] == "screen_reader" else SIGHTED_FIELDS
    report = {k: (values[k] if k in keep else "") for k in SCREEN_READER_FIELDS + SIGHTED_FIELDS}
    report.update({"method": values["method"], "checker_id": user["id"],
                   "show_name": 1 if values["show_name"] else 0, "created_at": db.now()})
    refusal = already = None
    with db.session() as conn:
        # One writer at a time, so the chain, the count and the reward stay consistent. The reward is
        # written in this same transaction: the check and its payment are saved together or not at all.
        conn.execute("BEGIN IMMEDIATE")
        fresh = dict(conn.execute("SELECT * FROM check_requests WHERE id = ?", (request_id,)).fetchone())
        done = conn.execute("SELECT COUNT(*) AS n FROM check_reports WHERE request_id = ?", (request_id,)).fetchone()["n"]
        already = conn.execute("SELECT 1 FROM check_reports WHERE request_id = ? AND checker_id = ?",
                               (request_id, user["id"])).fetchone()
        if already:
            pass
        elif fresh["status"] != "open" or done >= fresh["max_checks"]:
            refusal = ("This request closed while you were writing, so your check was not saved and no reward was "
                       "paid.")
        else:
            last = conn.execute("SELECT record_hash FROM check_reports WHERE request_id = ? ORDER BY id DESC LIMIT 1",
                                (request_id,)).fetchone()
            prev_hash = last["record_hash"] if last else ""
            record_hash = compute_hash(fresh, report, prev_hash)
            conn.execute(
                "INSERT INTO check_reports (request_id, checker_id, screen_reader, screen_reader_version, browser, device,"
                " heard_first, task_result, problems, show_name, prev_hash, record_hash, created_at, method,"
                " looks_finished, anything_broken, would_trust, improve)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, user["id"], report["screen_reader"], report["screen_reader_version"], report["browser"],
                 report["device"], report["heard_first"], report["task_result"], report["problems"],
                 report["show_name"], prev_hash, record_hash, report["created_at"], report["method"],
                 report["looks_finished"], report["anything_broken"], report["would_trust"], report["improve"]))
            if fresh["reward_cents"] > 0:
                _ledger(conn, user["id"], fresh["reward_cents"], f"Peer check reward: {fresh['title']}",
                        f"check:{request_id}")
            if done + 1 >= fresh["max_checks"]:
                conn.execute("UPDATE check_requests SET status = 'closed', closed_at = ? WHERE id = ?",
                             (db.now(), request_id))
    if already:
        return _already_saved(request, request_id)
    if refusal:
        return _refuse(request, _get_request(request_id), refusal, kept)
    if fresh["reward_cents"] > 0:
        flash(request, f"Thank you for checking. {credits.format_cents(fresh['reward_cents'])} was added to your "
                       "credits.", "success")
    else:
        flash(request, "Thank you for checking.", "success")
    return redirect(f"/checks/{request_id}/record?sent=1")


@router.get("/{request_id}/record")
def record_page(request: Request, request_id: int, sent: str = ""):
    req = _get_request(request_id)
    reports = _reports(request_id)
    problems = verify_chain(req, reports)
    for r in reports:
        if is_sighted(r):
            r["verdict"] = look_verdict(r)
    user = current_user(request)
    just_sent = bool(sent and user and any(r["checker_id"] == user["id"] for r in reports))
    if just_sent:
        title = f"Your check is saved in the record: {label(req)}"  # just after sending: say what happened
    else:
        title = f"Check record: {label(req)}"
    waiting: list[dict] = []
    fix = None
    if user:
        with db.session() as conn:
            if just_sent:
                waiting = _queue(conn, user["id"])
            if user["id"] == req["requester_id"] and reports:
                # The member who asked has now seen every check so far: they leave "New checks" on /checks.
                conn.execute(
                    "INSERT INTO check_seen (user_id, request_id, last_report_id) VALUES (?, ?, ?)"
                    " ON CONFLICT (user_id, request_id) DO UPDATE SET last_report_id = MAX(last_report_id, excluded.last_report_id)",
                    (user["id"], request_id, max(r["id"] for r in reports)))
        if reports and (version := _own_build_version(request, req["url"], user["id"])):
            text = fix_request_text(req, reports, version[1])
            if text:
                fix = {"project_id": version[0], "number": version[1], "text": text}
    return render(request, "checks/record.html", title=title, req=req, reports=reports,
                  summary=_summary(req, reports), chain_problems=problems, not_checked=_not_checked(reports),
                  result_words=RESULT_WORDS, answer_words=ANSWER_WORDS, look_questions=LOOK_QUESTIONS,
                  wanted_sentence=WANTED_SENTENCE[req["wanted"]], just_sent=just_sent, waiting=waiting, fix=fix,
                  label=label)


def forget_user(user_id: int) -> None:
    """A member is deleting their account. Their checks stay in the public record under their code name.

    Their open requests close, so nobody spends time checking for someone who has left. Credits held for
    checks not done are returned in the same transaction, so the ledger still adds up.
    """
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(ROW_SQL + " WHERE r.requester_id = ? AND r.status = 'open'", (user_id,)).fetchall()
        for row in rows:
            _close_and_refund(conn, row)
        conn.execute("DELETE FROM check_seen WHERE user_id = ?", (user_id,))
