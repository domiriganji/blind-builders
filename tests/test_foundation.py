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
    assert r.headers["location"] == "/"


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
