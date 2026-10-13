from studio import a11y_check, credits, jobs
from studio.ai import AIClient, cost_cents

from .conftest import post, register, user_id


def test_home_lists_every_section(client):
    page = client.get("/")
    assert page.status_code == 200
    for label in ["Build", "Do this for me", "Write and publish", "Find before you build", "Research",
                  "Peer checks", "AI tools report", "Tool library", "Learn and help"]:
        assert label in page.text


def test_register_sign_out_and_in(client):
    register(client)
    assert "Your account is ready" in client.get("/").text or "Sign out" in client.get("/").text
    post(client, "/logout")
    assert "Sign in" in client.get("/").text
    r = post(client, "/login", {"email": "amina@example.com", "password": "correct horse battery"})
    assert r.status_code == 200 and "Sign out" in r.text


def test_register_errors_are_listed_and_linked(client):
    r = post(client, "/register", {"email": "bad", "display_name": "", "password": "short"})
    assert r.status_code == 400
    assert 'href="#field-email"' in r.text and 'href="#field-password"' in r.text
    assert 'aria-invalid="true"' in r.text


def test_wrong_password(client):
    register(client)
    post(client, "/logout")
    r = post(client, "/login", {"email": "amina@example.com", "password": "nope nope nope"})
    assert r.status_code == 400 and "not right" in r.text


def test_forms_reject_missing_csrf(client):
    r = client.post("/login", data={"email": "a@b.co", "password": "x"})
    assert r.status_code == 400


def test_private_pages_ask_to_sign_in(client):
    r = client.get("/jobs", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_open_redirect_blocked(client):
    r = post(client, "/register", {"email": "z@example.com", "display_name": "Z", "password": "long enough pw",
                                   "next": "//evil.example"}, follow_redirects=False)
    # Sign-up first shows the recovery code; its "continue" link must not lead to another site.
    assert r.headers["location"] == "/account/recovery-code"
    page = client.get("/account/recovery-code")
    assert 'href="/">I have saved my code' in page.text and "evil.example" not in page.text


def test_job_runs_and_redirects(user_client):
    uid = user_id()

    def work(ctx, value):
        ctx.progress("Doing the thing")
        return f"/done/{value}"

    job_id = jobs.submit(uid, "test", "A test job", work, 7)
    r = user_client.get(f"/jobs/{job_id}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/done/7"
    assert user_client.get(f"/jobs/{job_id}.json").json()["status"] == "done"


def test_failed_job_shows_plain_reason(user_client):
    from studio.ai import AIError
    uid = user_id()

    def work(ctx):
        raise AIError("The AI is busy right now.")

    job_id = jobs.submit(uid, "test", "Busy job", work)
    r = user_client.get(f"/jobs/{job_id}")
    assert "This did not finish" in r.text and "The AI is busy right now." in r.text


def test_other_people_cannot_see_your_job(client):
    register(client, "a@example.com", "A")
    job_id = jobs.submit(user_id("a@example.com"), "test", "Private", lambda ctx: "/x")
    post(client, "/logout")
    register(client, "b@example.com", "B")
    assert client.get(f"/jobs/{job_id}").status_code == 404


def test_cost_and_credit_charging(app):
    from studio import db
    with db.session() as conn:
        conn.execute("INSERT INTO users (email, display_name, password_hash, pseudonym, created_at) VALUES ('c@x.co','C','x','M1','now')")
        uid = conn.execute("SELECT id FROM users WHERE email='c@x.co'").fetchone()["id"]
    credits.add(uid, 500, "test top-up")
    assert credits.balance(uid) == 500
    # 1M input + 0.1M output on Opus 5.5 = $4 + $2 = $6 -> 600 cents, times 1.25 markup = 750
    assert cost_cents("claude-opus-5-5", 1_000_000, 100_000, 0, 1.25) == 750

    class Usage:
        input_tokens = 1000; output_tokens = 1000; cache_creation_input_tokens = 0; cache_read_input_tokens = 0
        server_tool_use = None

    class Block:
        type = "text"; text = '{"ok": true}'

    class Msg:
        content = [Block()]; stop_reason = "end_turn"; usage = Usage(); model = "claude-opus-5-5"

    class Stream:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_final_message(self): return Msg()

    class Beta:
        class messages:
            last_kwargs = None
            @staticmethod
            def stream(**kw):
                Beta.messages.last_kwargs = kw
                return Stream()

    class SDK:
        beta = Beta

    client = AIClient(api_key="x", model="claude-opus-5-5", user_id=uid, paid_by="credits", markup=1.25, sdk_client=SDK())
    assert client.structured(system="s", prompt="p", schema={"type": "object"}, purpose="test") == {"ok": True}
    assert Beta.messages.last_kwargs["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in Beta.messages.last_kwargs["betas"]
    assert credits.balance(uid) < 500


def test_refusal_is_reported_plainly(app):
    import pytest
    from studio.ai import AIRefused

    class Msg:
        content = []; stop_reason = "refusal"; usage = None; model = "claude-opus-5-5"

    class Stream:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_final_message(self): return Msg()

    class SDK:
        class beta:
            class messages:
                @staticmethod
                def stream(**kw): return Stream()

    client = AIClient(api_key="x", model="claude-opus-5-5", user_id=1, paid_by="own_key", markup=1.0, sdk_client=SDK())
    with pytest.raises(AIRefused):
        client.text(system="s", prompt="p", purpose="t")


def test_checker_finds_common_problems():
    bad = """<html><head></head><body><h1></h1><h3>Skip</h3><img src="a.png"><input name="q">
    <button></button><a href="/x"></a><div id="d"></div><div id="d"></div>
    <meta name="viewport" content="width=device-width, user-scalable=no"></body></html>"""
    report = a11y_check.check_html(bad)
    rules = {f.rule for f in report.findings}
    for rule in ["html-lang", "document-title", "empty-heading", "heading-order", "img-alt", "form-label",
                 "button-name", "link-name", "duplicate-id", "viewport-zoom", "landmark-main"]:
        assert rule in rules, rule
    assert report.not_checked


def test_checker_passes_a_good_page():
    good = """<!doctype html><html lang="en"><head><title>Cakes</title>
    <meta name="viewport" content="width=device-width, initial-scale=1"></head>
    <body><main><h1>Cakes</h1><h2>Order</h2><img src="c.png" alt="A chocolate cake">
    <label for="n">Name</label><input id="n" name="n"><button>Order</button><a href="/m">Menu</a></main></body></html>"""
    report = a11y_check.check_html(good)
    assert report.errors == [], report.findings


# ----- regressions from the audit ---------------------------------------------------------------

import re  # noqa: E402

import pytest  # noqa: E402

from studio import db  # noqa: E402
from studio.auth import _safe_next  # noqa: E402


def _h1(html: str) -> str:
    return re.sub(r"\s+", " ", re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S).group(1)).strip()


def _title(html: str) -> str:
    return re.search(r"<title>(.*?) – ", html).group(1)


@pytest.mark.parametrize("bad", ["//evil.example", "/\\evil.example", "/\t/evil.example", "/\n/evil.example",
                                 "https://evil.example", "javascript:alert(1)", "", None])
def test_safe_next_only_allows_paths_on_this_site(bad):
    assert _safe_next(bad) == "/"


def test_safe_next_keeps_ordinary_paths():
    assert _safe_next("/build/new?idea=cake") == "/build/new?idea=cake"


def test_sign_in_with_backslash_next_stays_on_site(client):
    register(client)
    post(client, "/logout")
    r = post(client, "/login", {"email": "amina@example.com", "password": "correct horse battery",
                                "next": "/\\evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_form_errors_keep_h1_and_title_the_same(client):
    r = post(client, "/register", {"email": "bad", "display_name": "", "password": "short"})
    assert _h1(r.text) == _title(r.text) == "Create your account: please fix the problems"
    r = post(client, "/login", {"email": "nobody@example.com", "password": "whatever whatever"})
    assert r.status_code == 400
    assert _h1(r.text) == _title(r.text) == "Sign in: please fix the problems"
    assert a11y_check.check_html(r.text).errors == []


def test_sign_in_with_empty_fields_says_what_to_enter(client):
    r = post(client, "/login", {"email": "", "password": ""})
    assert r.status_code == 400
    assert "Enter the email address you signed up with." in r.text and "Enter your password." in r.text
    assert 'href="#field-password"' in r.text


def test_register_limits_and_secrets(client):
    r = post(client, "/register", {"email": "a" * 250 + "@example.com", "display_name": "N" * 81,
                                   "password": "p" * 201, "screen_reader": "s" * 101, "device": "d" * 101})
    assert r.status_code == 400
    for name in ["email", "display_name", "password", "screen_reader", "device"]:
        assert f'href="#field-{name}"' in r.text, name
    r = post(client, "/register", {"email": "k@example.com", "display_name": "sk-ant-api03-Ab12Cd34Ef56",
                                   "password": "long enough pw"})
    assert r.status_code == 400 and "looks like a Claude API key" in r.text
    assert "sk-ant-api03-Ab12Cd34Ef56" not in r.text


def test_register_with_unicode_name_works(client):
    register(client, "zoe@example.com", "Zoë 王芳")
    assert "Zoë 王芳" in client.get("/account").text


def test_after_signing_in_out_and_registering_the_h1_says_so(client):
    r = post(client, "/register", {"email": "amina@example.com", "display_name": "Amina",
                                   "password": "correct horse battery"})
    assert _h1(r.text) == _title(r.text) == "Your account is ready: save your recovery code"
    r = post(client, "/logout")
    assert _h1(r.text) == "You are signed out"
    r = post(client, "/login", {"email": "amina@example.com", "password": "correct horse battery"})
    assert _h1(r.text) == "You are signed in"
    # Only once: reloading shows the normal home page.
    assert _h1(client.get("/").text) == "Build your ideas with AI, without needing to see the screen"
    post(client, "/logout")
    r = post(client, "/login", {"email": "amina@example.com", "password": "correct horse battery",
                                "next": "/jobs"})
    assert _h1(r.text) == "Your recent work" and "You are signed in." in r.text


def test_wrong_email_and_wrong_password_get_the_same_answer(client):
    register(client)
    post(client, "/logout")
    a = post(client, "/login", {"email": "nobody@example.com", "password": "correct horse battery"})
    b = post(client, "/login", {"email": "amina@example.com", "password": "wrong wrong wrong"})
    assert a.status_code == b.status_code == 400
    assert "The email or password is not right. Check both and try again." in a.text
    assert "The email or password is not right. Check both and try again." in b.text


def test_done_job_without_result_link_is_listed_as_done(user_client):
    jobs.submit(user_id(), "test", "Quiet job", lambda ctx: "")
    page = user_client.get("/jobs").text
    assert "Quiet job: done." in page and "still working" not in page
    assert "<h2>Done (1)</h2>" in page


def test_job_list_is_split_under_headings(user_client):
    from studio.ai import AIError
    uid = user_id()
    jobs.submit(uid, "test", "Good job", lambda ctx: "/account")

    def fails(ctx):
        raise AIError("The AI is busy right now.")

    jobs.submit(uid, "test", "Bad job", fails)
    with db.session() as conn:
        conn.execute("INSERT INTO jobs (id, user_id, kind, title, status, created_at) VALUES"
                     " ('w1', ?, 'test', 'Slow job', 'running', ?)", (uid, db.now()))
    page = user_client.get("/jobs").text
    assert "<h2>Still working (1)</h2>" in page and "<h2>Did not finish (1)</h2>" in page
    assert page.index("Still working (1)") < page.index("Did not finish (1)") < page.index("Done (1)")
    assert '<a href="/account">Good job</a>' in page and '<a href="/jobs/w1">Slow job</a>' in page
    assert "1 still working, 1 did not finish, 1 done" in page
    assert a11y_check.check_html(page).errors == []


def test_stalled_job_says_it_seems_to_have_stopped(user_client):
    uid = user_id()
    with db.session() as conn:
        conn.execute("INSERT INTO jobs (id, user_id, kind, title, status, created_at, started_at) VALUES"
                     " ('old1', ?, 'test', 'Old job', 'running', '2020-01-01T00:00:00+00:00',"
                     " '2020-01-01T00:00:00+00:00')", (uid,))
    r = user_client.get("/jobs/old1")
    assert _h1(r.text) == "This seems to have stopped: Old job"
    assert 'data-job-status="failed"' in r.text  # the page does not keep checking by itself
    assert "Check again" not in r.text
    assert "seems to have stopped" in user_client.get("/jobs").text


def test_job_result_link_never_leaves_the_site(user_client):
    job_id = jobs.submit(user_id(), "test", "Odd job", lambda ctx: "https://evil.example/x")
    r = user_client.get(f"/jobs/{job_id}", follow_redirects=False)
    assert r.headers["location"] == "/jobs"
    assert user_client.get(f"/jobs/{job_id}.json").json()["result_url"] == "/jobs"
    assert "evil.example" not in user_client.get("/jobs").text


def test_failed_job_back_link_only_goes_to_this_site(user_client):
    from studio.ai import AIError

    def fails(ctx):
        raise AIError("The AI is busy right now.")

    job_id = jobs.submit(user_id(), "test", "Busy job", fails)
    r = user_client.get(f"/jobs/{job_id}", headers={"referer": "https://evil.example/phish"})
    assert "evil.example" not in r.text
    assert 'href="/jobs">See all your recent work' in r.text
    r = user_client.get(f"/jobs/{job_id}", headers={"referer": "http://testserver/build/new?x=1"})
    assert 'href="/build/new?x=1"' in r.text


def test_working_job_page_and_json(user_client):
    uid = user_id()
    with db.session() as conn:
        conn.execute("INSERT INTO jobs (id, user_id, kind, title, status, progress, created_at) VALUES"
                     " ('run1', ?, 'test', 'Slow job', 'running', 'Reading your idea', ?)", (uid, db.now()))
    r = user_client.get("/jobs/run1")
    assert _h1(r.text) == _title(r.text) == "Working on it: Slow job"
    assert "Reading your idea" in r.text and 'href="/jobs/run1"' in r.text
    assert 'data-job-status="running"' in r.text
    assert re.search(r"Started \d+ seconds? ago", r.text)
    assert user_client.get("/jobs/run1.json").json() == {
        "id": "run1", "status": "running", "title": "Slow job", "progress": "Reading your idea",
        "result_url": "", "error": ""}
    post(user_client, "/logout")
    register(user_client, "b@example.com", "B")
    assert user_client.get("/jobs/run1.json").status_code == 404


def test_elapsed_words():
    from studio.core_routes import _elapsed_words
    assert _elapsed_words(1) == "1 second"
    assert _elapsed_words(45) == "45 seconds"
    assert _elapsed_words(300) == "5 minutes"
    assert _elapsed_words(3 * 3600) == "3 hours"
