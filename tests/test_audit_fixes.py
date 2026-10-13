"""Regression tests for the security and correctness audit fixes (one test or more per finding)."""
from __future__ import annotations

import threading
import time

from studio.features import build

from .conftest import post, register, user_id


def _finishes_within(seconds: float, fn, *args):
    done = threading.Event()
    result = {}

    def run():
        result["value"] = fn(*args)
        done.set()

    threading.Thread(target=run, daemon=True).start()
    assert done.wait(seconds), f"{fn.__name__} took longer than {seconds} seconds"
    return result["value"]


# ----- Build: the line diff has a bounded cost ---------------------------------------------------

def test_build_diff_of_repetitive_files_is_fast():
    # One character per line, repeating: difflib with autojunk off took about 20 seconds for this.
    old = {"index.html": "\n".join("ab"[i % 2] for i in range(1500))}
    new = {"index.html": "\n".join("abc"[i % 3] for i in range(1500))}
    diff = _finishes_within(3, build.compute_diff, old, new)
    assert diff[0]["status"] == "changed" and diff[0]["exact"] is False
    assert "Too much changed to compare line by line" in build.describe_diff_entry(diff[0])
    groups = _finishes_within(3, build.changed_lines, old, new)
    assert groups[0]["groups"]


def test_build_diff_of_ordinary_html_list_is_fast_and_small_edits_stay_exact():
    lines = []
    for i in range(4000):
        lines += ["<li>", f"item {i}", "</li>", ""]
    changed = list(lines)
    for i in range(0, len(changed), 400):
        changed.insert(i, "<li>new</li>")
    _finishes_within(3, build.changed_lines, {"a.html": "\n".join(lines)}, {"a.html": "\n".join(changed)})
    # A small edit near the top and the bottom of a long page is still compared line by line.
    small = list(lines)
    small[5] = "item five"
    small[-3] = "item last"
    entry = _finishes_within(3, build.compute_diff, {"a.html": "\n".join(lines)}, {"a.html": "\n".join(small)})[0]
    assert entry == {"path": "a.html", "status": "changed", "added": 2, "removed": 2}


# ----- Sessions can be revoked -------------------------------------------------------------------

def _recovery_code(text: str) -> str:
    import re
    return re.search(r'id="recovery-code" type="text" class="code-box" value="([A-Z2-9]{4}(?:-[A-Z2-9]{4}){3})"',
                     text).group(1)


def _copy_session(app, source):
    from fastapi.testclient import TestClient
    other = TestClient(app)
    other.cookies.set("studio_session", source.cookies.get("studio_session"))
    return other


def test_old_session_stops_working_after_a_password_reset(app, client):
    from fastapi.testclient import TestClient
    r = post(client, "/register", {"email": "ola@example.com", "display_name": "Ola", "password": "correct horse battery"})
    code = _recovery_code(r.text)
    stolen = _copy_session(app, client)
    assert stolen.get("/account", follow_redirects=False).status_code == 200
    post(client, "/logout")
    owner = TestClient(app)
    r = post(owner, "/reset-password", {"email": "ola@example.com", "code": code, "password": "a brand new password"})
    assert r.status_code == 200 and "Your password is changed" in r.text
    assert stolen.get("/account", follow_redirects=False).status_code == 303
    assert owner.get("/account", follow_redirects=False).status_code == 200


def test_sign_out_on_all_devices(app, user_client):
    other_device = _copy_session(app, user_client)
    page = user_client.get("/account")
    assert 'action="/logout-everywhere"' in page.text
    r = post(user_client, "/logout-everywhere")
    assert r.status_code == 200 and "You are signed out on every device" in r.text
    assert other_device.get("/account", follow_redirects=False).status_code == 303
    assert user_client.get("/account", follow_redirects=False).status_code == 303
    # Signing in again works.
    r = post(user_client, "/login", {"email": "amina@example.com", "password": "correct horse battery"})
    assert user_client.get("/account", follow_redirects=False).status_code == 200


def test_site_owner_set_password_signs_the_member_out(app, user_client):
    from studio import manage
    assert manage.set_password("amina@example.com", "owner set password") == 0
    assert user_client.get("/account", follow_redirects=False).status_code == 303


def test_new_recovery_code_needs_the_current_password(user_client):
    r = post(user_client, "/account/recovery-code", {})
    assert r.status_code == 400 and 'href="#field-password"' in r.text
    r = post(user_client, "/account/recovery-code", {"password": "not my password"})
    assert r.status_code == 400 and "That is not the password you use now, so no new code was made." in r.text
    r = post(user_client, "/account/recovery-code", {"password": "correct horse battery"})
    assert r.status_code == 200 and "Your new recovery code: save it now" in r.text


def test_recovery_code_is_not_readable_in_the_session_cookie(client):
    import base64
    import json
    post(client, "/register", {"email": "ola@example.com", "display_name": "Ola", "password": "correct horse battery"},
         follow_redirects=False)
    cookie = client.cookies.get("studio_session")
    payload = json.loads(base64.b64decode(cookie.split(".")[0] + "=="))
    code = _recovery_code(client.get("/account/recovery-code").text)
    assert code not in json.dumps(payload) and "code" not in payload["recovery_show"]


# ----- Sign-in: attempt limits and hashing off the event loop ------------------------------------

def test_sign_in_guesses_are_limited(client):
    from studio import auth
    register(client)
    post(client, "/logout")
    for _ in range(auth.LOGIN_MAX_PER_EMAIL_AND_IP):
        r = post(client, "/login", {"email": "amina@example.com", "password": "wrong password!"})
        assert r.status_code == 400
    r = post(client, "/login", {"email": "amina@example.com", "password": "correct horse battery"})
    assert r.status_code == 429 and "sign-in is paused for 15 minutes" in r.text
    assert "Your account and your work are unchanged." in r.text and "wrong tries" not in r.text


def test_a_stranger_cannot_lock_a_member_out_of_signing_in(app, client):
    from fastapi.testclient import TestClient
    from studio import auth
    register(client)
    post(client, "/logout")
    stranger = TestClient(app, client=("203.0.113.9", 4000))
    for _ in range(auth.LOGIN_MAX_PER_IP + 2):
        post(stranger, "/login", {"email": "amina@example.com", "password": "wrong password!"})
    r = post(client, "/login", {"email": "amina@example.com", "password": "correct horse battery"})
    assert r.status_code == 200 and "You are signed in" in r.text


def test_password_hashing_runs_off_the_event_loop(client, monkeypatch):
    import asyncio
    from studio import auth
    on_loop = []

    def spy(fn):
        def wrapper(*args):
            try:
                asyncio.get_running_loop()
                on_loop.append(fn.__name__)
            except RuntimeError:
                pass
            return fn(*args)
        return wrapper

    monkeypatch.setattr(auth, "hash_password", spy(auth.hash_password))
    monkeypatch.setattr(auth, "verify_password", spy(auth.verify_password))
    register(client)
    post(client, "/logout")
    post(client, "/login", {"email": "amina@example.com", "password": "correct horse battery"})
    assert on_loop == []


def test_registrations_are_limited_per_internet_address(app, settings):
    from fastapi.testclient import TestClient
    c = TestClient(app, client=("198.51.100.7", 5000))
    for i in range(settings.registrations_per_ip_per_hour):
        r = post(c, "/register", {"email": f"m{i}@example.com", "display_name": "M", "password": "correct horse battery"})
        assert r.status_code == 200
        post(c, "/logout")
    r = post(c, "/register", {"email": "one-more@example.com", "display_name": "M", "password": "correct horse battery"})
    assert r.status_code == 400 and "making another is paused for an hour" in r.text


# ----- Welcome credits are not unlimited ---------------------------------------------------------

def test_welcome_credits_once_per_internet_address_and_capped_per_day(app, settings):
    from fastapi.testclient import TestClient
    from studio import credits
    settings.signup_credits = 200
    settings.signup_credits_per_day = 2
    a = TestClient(app, client=("192.0.2.1", 1))
    post(a, "/register", {"email": "a1@example.com", "display_name": "A", "password": "correct horse battery"})
    post(a, "/logout")
    post(a, "/register", {"email": "a2@example.com", "display_name": "A", "password": "correct horse battery"})
    assert credits.balance(user_id("a1@example.com")) == 200
    assert credits.balance(user_id("a2@example.com")) == 0  # same address
    for n, ip in enumerate(("192.0.2.2", "192.0.2.3")):
        c = TestClient(app, client=(ip, 1))
        post(c, "/register", {"email": f"b{n}@example.com", "display_name": "B", "password": "correct horse battery"})
    assert credits.balance(user_id("b0@example.com")) == 200
    assert credits.balance(user_id("b1@example.com")) == 0  # the day's limit of 2 is used


# ----- Recovery: a stranger cannot block the owner, and tries are not kept ------------------------

def test_a_stranger_cannot_block_the_owners_recovery_code(app, client):
    from fastapi.testclient import TestClient
    from studio.features import recovery
    code = _recovery_code(post(client, "/register", {"email": "ola@example.com", "display_name": "Ola",
                                                     "password": "correct horse battery"}).text)
    post(client, "/logout")
    stranger = TestClient(app, client=("203.0.113.50", 9))
    for _ in range(recovery.MAX_TRIES_PER_HOUR + 2):
        post(stranger, "/reset-password", {"email": "ola@example.com", "code": "AAAA-BBBB-CCCC-DDDD",
                                           "password": "whatever password"})
    r = post(client, "/reset-password", {"email": "ola@example.com", "code": code, "password": "a brand new password"})
    assert r.status_code == 200 and "Your password is changed" in r.text


def test_failed_recovery_tries_leave_no_email_behind(client):
    from studio import db
    post(client, "/register", {"email": "ola@example.com", "display_name": "Ola", "password": "correct horse battery"})
    post(client, "/logout")
    post(client, "/reset-password", {"email": "ola@example.com", "code": "AAAA-BBBB-CCCC-DDDD",
                                     "password": "whatever password"})
    post(client, "/login", {"email": "ola@example.com", "password": "correct horse battery"})
    with db.session() as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        dump = "\n".join(conn.iterdump())
    assert "recovery_attempts" not in tables
    assert "ola@example.com" in dump  # the account itself
    post(client, "/account/delete", {"confirm": "yes"})
    with db.session() as conn:
        dump = "\n".join(conn.iterdump())
        left = conn.execute("SELECT COUNT(*) FROM attempts WHERE action IN ('login', 'recovery')").fetchone()[0]
    assert "ola@example.com" not in dump
    assert left == 0


# ----- Jobs never write into another member's item that reused a deleted id ---------------------

def _second_member() -> int:
    from studio import db
    with db.session() as conn:
        cur = conn.execute("INSERT INTO users (email, display_name, password_hash, pseudonym, created_at)"
                           " VALUES ('b@example.com', 'B', 'x', 'Member B', '2026-01-01T00:00:00+00:00')")
        return cur.lastrowid


def test_write_job_does_not_save_into_a_reused_document_id(user_client, fake_ai):
    from studio import db
    from .test_write import create_doc, save
    b = _second_member()
    doc_id = create_doc(user_client)
    save(user_client, doc_id, "My private sentence is here.")

    def answer(**kwargs):
        with db.session() as conn:  # A deletes the document while the AI works; B's new one takes its id
            conn.execute("DELETE FROM write_versions WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM write_docs WHERE id = ?", (doc_id,))
            new_id = conn.execute("INSERT INTO write_docs (user_id, title, kind, body, created_at, updated_at)"
                                  " VALUES (?, 'B doc', 'article', 'B text', '2030-01-01', '2030-01-01')",
                                  (b,)).lastrowid
        assert new_id == doc_id
        return {"overall_note": "Fine.", "suggestions": [{"original": "My private sentence", "proposed": "x",
                                                           "reason": "r"}]}

    fake_ai.on("write.suggest", answer)
    post(user_client, f"/write/{doc_id}/suggest", {"goal": "spelling", "instructions": ""})
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM write_suggestions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM write_batches").fetchone()[0] == 0


def test_research_job_does_not_save_into_a_reused_project_id(user_client, fake_ai):
    from studio import db
    from studio.ai import ResearchResult
    from .test_research import new_project
    b = _second_member()
    pid = new_project(user_client)

    def answer(**kwargs):
        with db.session() as conn:
            conn.execute("DELETE FROM research_projects WHERE id = ?", (pid,))
            new_id = conn.execute("INSERT INTO research_projects (user_id, title, question, created_at, updated_at)"
                                  " VALUES (?, 'B', 'B question', '2030-01-01', '2030-01-01')", (b,)).lastrowid
        assert new_id == pid
        return ResearchResult(text="A's private prior work.", sources=[])

    fake_ai.on("research.prior_work", answer)
    post(user_client, f"/research/{pid}/prior-work")
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_items").fetchone()[0] == 0


def test_errand_job_does_not_update_a_reused_errand_id(user_client, fake_ai):
    from studio import db
    from .test_errands import CONTRACT
    b = _second_member()

    def answer(**kwargs):
        with db.session() as conn:
            eid = conn.execute("SELECT MAX(id) FROM errands").fetchone()[0]
            conn.execute("DELETE FROM errands WHERE id = ?", (eid,))
            new_id = conn.execute("INSERT INTO errands (user_id, kind, request, created_at, updated_at)"
                                  " VALUES (?, 'find', 'B request', '2030-01-01', '2030-01-01')", (b,)).lastrowid
        assert new_id == eid
        return CONTRACT

    fake_ai.on("errands.understand", answer)
    post(user_client, "/do/new", {"kind": "find", "request": "My private medical question"})
    with db.session() as conn:
        row = conn.execute("SELECT * FROM errands WHERE user_id = ?", (b,)).fetchone()
    assert row["status"] == "new" and row["contract_json"] == ""


# ----- AI client: credits, fallback billing, research turns and sources --------------------------

from types import SimpleNamespace as NS  # noqa: E402


class FakeSDK:
    """A stand-in for the Anthropic SDK: returns the given messages in order and keeps each request."""

    def __init__(self, *messages, before_return=None):
        self.messages = list(messages)
        self.requests: list[dict] = []
        self.before_return = before_return
        sdk = self

        class Stream:
            def __init__(self, msg):
                self.msg = msg

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_final_message(self):
                if sdk.before_return:
                    sdk.before_return()
                return self.msg

        class Messages:
            @staticmethod
            def stream(**kw):
                import copy
                sdk.requests.append(copy.deepcopy(kw))
                return Stream(sdk.messages.pop(0) if len(sdk.messages) > 1 else sdk.messages[0])

        self.beta = NS(messages=Messages)


def _usage(inp=1000, out=1000, **extra):
    return NS(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=0, cache_read_input_tokens=0,
              server_tool_use=None, **extra)


def _msg(content, stop="end_turn", usage=None, model="claude-opus-5-5"):
    return NS(content=content, stop_reason=stop, usage=usage or _usage(), model=model)


def _text(text, citations=None):
    return NS(type="text", text=text, citations=citations or [])


def _client(uid, sdk, paid_by="credits", markup=1.25):
    from studio.ai import AIClient
    return AIClient(api_key="x", model="claude-opus-5-5", user_id=uid, paid_by=paid_by, markup=markup, sdk_client=sdk)


def test_credits_cannot_run_far_past_the_balance(user_client):
    import pytest
    from studio import credits
    from studio.ai import AINotAvailable
    uid = user_id()
    credits.add(uid, 1, "leftover")
    big = _msg([_text("hi")], usage=_usage(100_000, 32_000))
    sdk = FakeSDK(big)
    with pytest.raises(AINotAvailable) as exc:
        _client(uid, sdk).text(system="s", prompt="p", purpose="test", max_tokens=32000)
    assert "can cost up to" in exc.value.user_message
    assert sdk.requests == [] and credits.balance(uid) == 1


def test_concurrent_calls_see_each_others_holds(user_client):
    from studio import credits
    from studio.ai import AINotAvailable
    uid = user_id()
    credits.add(uid, 60, "top-up")  # enough for one call of up to 16,000 output tokens (about $0.41), not two
    results = []

    def second_call():
        try:
            _client(uid, FakeSDK(_msg([_text("b")]))).text(system="s", prompt="p", purpose="second")
            results.append("ran")
        except AINotAvailable:
            results.append("refused")

    sdk = FakeSDK(_msg([_text("a")]), before_return=second_call)
    _client(uid, sdk).text(system="s", prompt="p", purpose="first")
    assert results == ["refused"]
    assert credits.balance(uid) > 50  # the first call cost a few cents; its hold was replaced by the real cost
    assert not any(h["reason"].startswith("AI, held") for h in credits.history(uid))


def test_failed_call_releases_the_hold(user_client):
    import anthropic
    import httpx
    import pytest
    from studio import credits
    from studio.ai import AIError
    uid = user_id()
    credits.add(uid, 500, "top-up")

    class Broken:
        class beta:
            class messages:
                @staticmethod
                def stream(**kw):
                    raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))

    with pytest.raises(AIError):
        _client(uid, Broken()).text(system="s", prompt="p", purpose="test")
    assert credits.balance(uid) == 500


def test_every_attempt_of_a_fallback_is_charged(user_client):
    from studio import credits
    from studio.ai import cost_cents
    uid = user_id()
    credits.add(uid, 100_000, "top-up")
    declined = NS(type="message", model="claude-opus-5-5", input_tokens=200_000, output_tokens=30_000,
                  cache_creation_input_tokens=0, cache_read_input_tokens=0)
    served = NS(type="fallback_message", model="claude-opus-5", input_tokens=200_000, output_tokens=30_000,
                cache_creation_input_tokens=0, cache_read_input_tokens=0)
    usage = _usage(200_000, 30_000, iterations=[declined, served])
    _client(uid, FakeSDK(_msg([_text("ok")], usage=usage, model="claude-opus-5")), markup=1.0).text(
        system="s", prompt="p", purpose="test", max_tokens=1000)
    expected = cost_cents("claude-opus-5-5", 200_000, 30_000, 0, 1.0) + cost_cents("claude-opus-5", 200_000, 30_000, 0, 1.0)
    assert credits.balance(uid) == 100_000 - expected


def test_research_keeps_the_whole_turn_across_pauses(user_client):
    seg1 = [NS(type="server_tool_use", id="s1", name="web_search", input={}),
            NS(type="web_search_tool_result", tool_use_id="s1", content=[]), _text("SEG1 ")]
    seg2 = [NS(type="server_tool_use", id="s2", name="web_search", input={}),
            NS(type="web_search_tool_result", tool_use_id="s2", content=[]), _text("SEG2 ")]
    seg3 = [_text("Final answer.")]
    sdk = FakeSDK(_msg(seg1, "pause_turn"), _msg(seg2, "pause_turn"), _msg(seg3))
    result = _client(1, sdk, paid_by="own_key").research(system="s", prompt="P", purpose="test")
    third = sdk.requests[2]["messages"]
    carried = [getattr(b, "id", None) or getattr(b, "text", None) for m in third[1:] for b in m["content"]]
    assert "s1" in carried and "s2" in carried and "SEG1 " in carried


def test_research_that_never_finishes_is_an_error(user_client):
    import pytest
    from studio.ai import AIError
    sdk = FakeSDK(_msg([_text("part ")], "pause_turn"))
    with pytest.raises(AIError):
        _client(1, sdk, paid_by="own_key").research(system="s", prompt="P", purpose="test")


def test_research_sources_are_the_cited_pages_and_the_answer_has_no_narration(user_client):
    results = [NS(type="web_search_result", url=f"https://unrelated{i}.example/", title=f"U{i}") for i in range(10)]
    results.append(NS(type="web_search_result", url="https://library.example/hours", title="Hours"))
    fetch = NS(type="web_fetch_tool_result", tool_use_id="f1",
               content=NS(type="web_fetch_result", url="https://fetched.example/page",
                          content=NS(type="document", title="Fetched page")))
    blocks = [
        _text("I'll search for the opening hours first."),
        NS(type="server_tool_use", id="s1", name="web_search", input={}),
        NS(type="web_search_tool_result", tool_use_id="s1", content=results),
        NS(type="server_tool_use", id="f1", name="web_fetch", input={}),
        fetch,
        _text("The library opens at 9am.", [NS(type="web_search_result_location", url="https://library.example/hours",
                                               title="Hours", cited_text="Open 9am")]),
        _text(" The answer is 42.", [NS(type="char_location", document_index=0, document_title="Fetched page",
                                        cited_text="42")]),
    ]
    result = _client(1, FakeSDK(_msg(blocks)), paid_by="own_key").research(system="s", prompt="P", purpose="test")
    assert result.text == "The library opens at 9am. The answer is 42."
    assert [s.url for s in result.sources] == ["https://library.example/hours", "https://fetched.example/page"]
    assert len(result.other_results) == 10


# ----- Build: busy while its job lives, however long ---------------------------------------------

def test_build_stays_busy_while_its_job_runs_and_jobs_are_deduplicated(user_client, fake_ai):
    from studio import db
    from .test_build import build_v1, calls_for, changed_files, share_token
    project_id, _ = build_v1(user_client, fake_ai)
    token = share_token(project_id)
    with db.session() as conn:
        keys = [r[0] for r in conn.execute("SELECT dedupe_key FROM jobs WHERE kind LIKE 'build.%'")]
        assert keys and all(k == f"build:{token}" for k in keys)
        # A change job that has been running for 20 minutes (a long 64,000-token answer).
        conn.execute("UPDATE build_projects SET status = 'changing', updated_at = '2020-01-01T00:00:00+00:00'")
        conn.execute("INSERT INTO jobs (id, user_id, kind, title, status, created_at, started_at, dedupe_key)"
                     " VALUES ('long', ?, 'build.change', 'Changing', 'running', '2020-01-01T00:00:00+00:00',"
                     " '2020-01-01T00:00:00+00:00', ?)", (user_id(), f"build:{token}"))
    assert "The AI stopped before it finished" not in user_client.get("/build").text
    fake_ai.on("build.change", changed_files())
    r = post(user_client, f"/build/{project_id}/v/1/change", {"request": "Small chops now cost 1500."})
    assert "already working on this project" in r.text
    assert calls_for(fake_ai, "build.change") == []


# ----- Jobs: a limit per member, and waiting is not "stopped" -------------------------------------

def test_one_member_cannot_fill_every_job_thread(user_client):
    from studio import db, jobs
    uid = user_id()
    with db.session() as conn:
        for i in range(jobs.MAX_ACTIVE_PER_MEMBER):
            conn.execute("INSERT INTO jobs (id, user_id, kind, title, status, created_at) VALUES (?, ?, 'x', 'x', 'queued', ?)",
                         (f"q{i}", uid, db.now()))
    import pytest
    with pytest.raises(jobs.TooManyJobs):
        jobs.submit(uid, "x", "x", lambda ctx: "/")
    # Through a page: a plain explanation, not an error.
    r = post(user_client, "/do/new", {"kind": "find", "request": "When does the pool open?"})
    assert r.status_code == 429 and "Please wait for your other AI work to finish" in r.text
    assert 'href="/jobs">Go to Your recent work' in r.text


def test_a_job_waiting_for_a_worker_is_not_called_stopped(user_client):
    from studio import db
    with db.session() as conn:
        conn.execute("INSERT INTO jobs (id, user_id, kind, title, status, created_at) VALUES"
                     " ('waiting', ?, 'test', 'Waiting job', 'queued', '2020-01-01T00:00:00+00:00')", (user_id(),))
    r = user_client.get("/jobs/waiting")
    assert "Working on it: Waiting job" in r.text and "seems to have stopped" not in r.text


# ----- Secrets are refused the same way in every AI section --------------------------------------

SECRETS = [
    "The admin password is Hunter2!x",
    "the door PIN 4821",
    "the one-time code 482913",
    "my key sk-ant-api03-abcdefghijklmnop",
    "Diners card 30569309025904",
    "card 4111111111111111110",
]


def test_every_section_detector_refuses_the_same_secrets():
    from studio.features import build, errands, find, research
    for text in SECRETS:
        assert build.looks_like_secret(text), text
        assert errands.find_secret(text), text
        assert find.looks_secret(text), text
        assert research.looks_secret(text), text
    for ordinary in ("My password is wrong, how do I reset it?", "Steps: 1 2 3 4 5 6 7 8 9 10 11 12 13",
                     "zip code 90210", "It broke on 2026-10-12"):
        assert not errands.find_secret(ordinary), ordinary


def test_build_refuses_an_idea_with_a_password(user_client, fake_ai):
    r = post(user_client, "/build/new", {"kind": "website", "idea": "A shop site. The admin password is Hunter2!x"})
    assert r.status_code == 400 and fake_ai.calls == []


def test_write_refuses_secrets_in_every_field_and_never_sends_them(user_client, fake_ai):
    from studio import db
    from .test_write import create_doc, save
    r = post(user_client, "/write/new", {"title": "password: hunter2", "kind": "letter", "target": ""})
    assert r.status_code == 400 and "hunter2" not in r.text
    doc_id = create_doc(user_client, title="Letter to my bank")
    for body in ("My online banking password is Tr0ub4dor&3, PIN 4821.", "Key sk-ant-api03-abcdefghijklmnop",
                 "Card 30569309025904"):
        r = save(user_client, doc_id, body, title="Letter to my bank")
        assert r.status_code == 400, body
    r = save(user_client, doc_id, "Dear bank,", title="Letter to my bank", target="one-time code 482913")
    assert r.status_code == 400
    save(user_client, doc_id, "Dear bank, please help.", title="Letter to my bank")
    r = post(user_client, f"/write/{doc_id}/suggest", {"goal": "spelling", "instructions": "my password is hunter2!"})
    assert r.status_code == 400
    # A document saved before these checks is not sent either.
    with db.session() as conn:
        conn.execute("UPDATE write_docs SET body = 'My PIN 4821 is here.' WHERE id = ?", (doc_id,))
    fake_ai.on("write.suggest", {"overall_note": "", "suggestions": []})
    r = post(user_client, f"/write/{doc_id}/suggest", {"goal": "spelling", "instructions": ""})
    assert r.status_code == 400
    assert fake_ai.calls == []


# ----- Build previews cannot pass for the site's own pages ----------------------------------------

def test_preview_forms_cannot_send_anything(user_client, fake_ai):
    from fastapi.testclient import TestClient
    from .test_build import build_v1, share_token
    project_id, _ = build_v1(user_client, fake_ai)
    r = TestClient(user_client.app).get(f"/build/p/{share_token(project_id)}/v/1/")
    assert "form-action 'none'" in r.headers["content-security-policy"]


def test_previews_can_live_on_their_own_domain(settings, fake_ai):
    from fastapi.testclient import TestClient
    from studio.app import create_app
    from .test_build import build_v1, share_token
    settings.preview_base_url = "http://previews.example"
    application = create_app(settings)
    application.state.ai_factory = lambda user: fake_ai
    site = TestClient(application)
    register(site)
    project_id, _ = build_v1(site, fake_ai)
    token = share_token(project_id)
    page = site.get(f"/build/{project_id}/v/1")
    assert f'href="http://previews.example/build/p/{token}/v/1/index.html"' in page.text
    assert "url=http%3A%2F%2Fpreviews.example%2Fbuild%2Fp%2F" in page.text  # the peer check link
    r = site.get(f"/build/p/{token}/v/1/index.html", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].startswith("http://previews.example/build/p/")
    previews = TestClient(application, base_url="http://previews.example")
    assert previews.get(f"/build/p/{token}/v/1/index.html").status_code == 200
    assert previews.get("/login").status_code == 404


def test_peer_check_pages_warn_about_typing_passwords(user_client):
    from .test_checks import latest_request_id, make_request
    make_request(user_client)
    page = user_client.get(f"/checks/{latest_request_id()}")
    assert "Never type your password for this site" in page.text


# ----- Find: a search finishing after account deletion saves nothing ------------------------------

def test_find_search_finishing_after_account_deletion_is_not_saved(app, user_client, fake_ai):
    from studio import db
    from studio.features.account import anonymise_user
    from .test_find import EXTRACTED, NEED, RESEARCH
    uid = user_id()

    def search(**kwargs):  # the member deletes their account while the AI searches
        for hook in app.state.forget_user_hooks:
            hook(uid)
        anonymise_user(uid)
        return RESEARCH

    fake_ai.on("find.search", search).on("find.extract", EXTRACTED)
    post(user_client, "/find/ask", {"need": NEED})
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM find_searches").fetchone()[0] == 0


# ----- Research: a CSV upload stores a bounded amount ---------------------------------------------

def test_csv_upload_stores_a_bounded_amount(user_client):
    from studio import db
    from studio.features import research
    from .test_research import new_project, upload
    pid = new_project(user_client)
    wide = ("a" + "," * 999 + "\n1\n").encode()  # 1,000 columns: accepted
    for _ in range(research.KEEP_UPLOADS + 3):
        r = upload(user_client, pid, wide)
        assert r.status_code == 200
    with db.session() as conn:
        rows = conn.execute("SELECT meta_json FROM research_items WHERE project_id = ?", (pid,)).fetchall()
    assert len(rows) == research.KEEP_UPLOADS
    assert all(len(r["meta_json"]) < 20_000 for r in rows)
    assert "and 950 more" in user_client.get(f"/research/{pid}/data").text
    r = upload(user_client, pid, ("a" + "," * 200_000 + "\n1\n").encode())
    assert r.status_code == 400 and "columns" in r.text


# ----- Write: a suggestion can only change the place it quotes ------------------------------------

def test_write_suggestion_for_repeated_words_is_left_out(user_client, fake_ai):
    from studio import db
    from studio.features import write as write_mod
    from .test_write import body_of, create_doc, save
    body = "I love their dog. Their cat is nice. I went their yesterday."
    kept, dropped = write_mod.filter_suggestions(body, [{"original": "their", "proposed": "there", "reason": "r"},
                                                       {"original": "went their", "proposed": "went there", "reason": "r"}])
    assert [k["original"] for k in kept] == ["went their"] and dropped == 1
    doc_id = create_doc(user_client)
    save(user_client, doc_id, body)
    fake_ai.on("write.suggest", {"overall_note": "", "suggestions": [
        {"original": "their", "proposed": "there", "reason": "'went their' should be 'went there'."}]})
    post(user_client, f"/write/{doc_id}/suggest", {"goal": "spelling", "instructions": ""})
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM write_suggestions").fetchone()[0] == 0
        batch_id = conn.execute("SELECT id FROM write_batches").fetchone()[0]
        # A suggestion saved earlier whose words now appear twice is not applied anywhere.
        conn.execute("INSERT INTO write_suggestions (doc_id, batch_id, number, original, proposed, created_at)"
                     " VALUES (?, ?, 1, 'their', 'there', '2026-01-01')", (doc_id, batch_id))
    post(user_client, f"/write/{doc_id}/suggestions/{batch_id}/accept-all")
    assert body_of(doc_id) == body


# ----- Deleting an item removes its job titles from Your recent work ------------------------------

def test_deleted_errand_leaves_no_title_in_recent_work(user_client, fake_ai):
    from .test_errands import CONTRACT
    fake_ai.on("errands.understand", CONTRACT)
    post(user_client, "/do/new", {"kind": "find", "request": "My HIV clinic appointment at St Marys"})
    assert "My HIV clinic" in user_client.get("/jobs").text
    from studio import db
    with db.session() as conn:
        eid = conn.execute("SELECT id FROM errands").fetchone()[0]
    post(user_client, f"/do/{eid}/delete")
    assert "My HIV clinic" not in user_client.get("/jobs").text


def test_deleted_document_and_project_leave_no_titles(user_client, fake_ai):
    from studio.ai import ResearchResult
    from .test_build import build_v1
    from .test_research import new_project
    from .test_write import create_doc, save
    doc_id = create_doc(user_client, title="Letter about my divorce")
    save(user_client, doc_id, "Dear court,", title="Letter about my divorce")
    fake_ai.on("write.suggest", {"overall_note": "", "suggestions": []})
    post(user_client, f"/write/{doc_id}/suggest", {"goal": "spelling", "instructions": ""})
    pid = new_project(user_client, title="My private health study")
    fake_ai.on("research.prior_work", ResearchResult(text="Found.", sources=[]))
    post(user_client, f"/research/{pid}/prior-work")
    project_id, _ = build_v1(user_client, fake_ai)
    page = user_client.get("/jobs").text
    assert "divorce" in page and "private health" in page and "Kemi" in page
    post(user_client, f"/write/{doc_id}/delete")
    post(user_client, f"/research/{pid}/delete")
    post(user_client, f"/build/{project_id}/delete")
    page = user_client.get("/jobs").text
    assert "divorce" not in page and "private health" not in page and "Kemi" not in page


def test_recent_work_can_be_cleared(user_client, fake_ai):
    from .test_find import EXTRACTED, NEED, RESEARCH
    fake_ai.on("find.search", RESEARCH).on("find.extract", EXTRACTED)
    post(user_client, "/find/ask", {"need": NEED})
    assert NEED in user_client.get("/jobs").text
    r = post(user_client, "/jobs/clear")
    assert r.status_code == 200 and NEED not in r.text


# ----- Help shows the code name unless the member chooses their name ------------------------------

def test_help_shows_the_code_name_to_the_public_by_default(app, user_client):
    from fastapi.testclient import TestClient
    from studio import db
    from .test_learn import ask
    qid = ask(user_client)
    with db.session() as conn:
        pseudonym = conn.execute("SELECT pseudonym FROM users").fetchone()[0]
    public = TestClient(app)
    for url in ("/help", f"/help/q/{qid}"):
        page = public.get(url).text
        assert "Amina" not in page and pseudonym in page
    assert "Anyone can read Ask the community" in user_client.get("/account/profile").text
    post(user_client, "/account/profile", {"display_name": "Amina", "screen_reader": "", "device": "", "language": "en",
                                           "sight_onset": "prefer_not", "verbosity": "summary_first",
                                           "show_name_on_help": "yes"})
    assert "Asked by Amina" in public.get(f"/help/q/{qid}").text
