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
    assert 'action="/account/recovery-code/saved"' in page.text and "evil.example" not in page.text
    r = post(client, "/account/recovery-code/saved", follow_redirects=False)
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


def test_member_note_goes_before_every_system_prompt(app):
    """The shared note on how to speak to the member (docs/VOICE.md, 4.18) comes first in every call."""
    from types import SimpleNamespace
    from studio import ai

    class Block:
        type = "text"; text = "ok"

    class Msg:
        content = [Block()]; stop_reason = "end_turn"; usage = None; model = "claude-opus-5-5"

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

    client = AIClient(api_key="x", model="claude-opus-5-5", user_id=1, paid_by="own_key", markup=1.0, sdk_client=SDK(),
                      member_note="NOTE")
    client.text(system="Section prompt.", prompt="p", purpose="t")
    assert Beta.messages.last_kwargs["system"] == "NOTE\n\nSection prompt."

    with db.session() as conn:
        conn.execute("INSERT INTO users (email, display_name, password_hash, pseudonym, created_at, screen_reader)"
                     " VALUES ('n@x.co','N','x','M2','now','JAWS')")
        user = dict(conn.execute("SELECT * FROM users WHERE email='n@x.co'").fetchone())
    credits.add(user["id"], 500, "test top-up")
    settings = SimpleNamespace(anthropic_api_key="k", model="claude-opus-5-5", credit_markup=1.25, secret_key="s")
    made = ai.default_factory(settings)(user)
    assert made.member_note == ai.member_voice(user)


def test_member_voice_follows_the_profile():
    from studio.ai import LOOKS_COLOURS, LOOKS_EFFECT, member_voice
    plain = member_voice({"screen_reader": "", "verbosity": "summary_first", "language": "en", "describe_colours": 0})
    assert "Their tools: not given." in plain and "summary first, then details" in plain
    assert LOOKS_EFFECT in plain and "never ask them to choose colours" in plain
    assert "main language" not in plain
    full = member_voice({"screen_reader": "VoiceOver  on iPhone", "verbosity": "full", "language": "ha",
                         "describe_colours": 1})
    assert "Their tools: VoiceOver on iPhone." in full and "Their answer style: full detail." in full
    assert "Their main language is Hausa" in full
    assert LOOKS_COLOURS in full and LOOKS_EFFECT not in full


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


def test_checker_findings_lead_with_the_effect_on_visitors():
    """Each finding says what visitors meet first, in plain words, then the technical fix for the AI."""
    bad = """<html><head><meta http-equiv="refresh" content="5"></head><body><h1>A</h1><h1></h1><h4>B</h4>
    <img src="a.png"><input name="q"><input type="submit"><input type="image" src="b.png"><button></button>
    <a href="/x"></a><div id="d"></div><div id="d"></div><iframe src="/f"></iframe><span tabindex="3">x</span>
    <div aria-hidden="true"><a href="/y">y</a></div><table><tr><td>1</td></tr></table><audio autoplay></audio>
    <meta name="viewport" content="width=device-width, user-scalable=no"><script></script></body></html>"""
    report = a11y_check.check_html(bad + " " * 61_000)
    rules = {f.rule for f in report.findings}
    assert rules == set(a11y_check.RULES), set(a11y_check.RULES) - rules
    for f in report.findings:
        lead = f.message.split(". ")[0]
        assert not re.search(r"[<>=]|\b(?:alt|aria|tabindex|lang|h\d)\b", lead), f.message
        assert len(f.message.split(". ")) >= 2, f.message  # the technical part follows
    assert "The page has 2 main headings (h1)." in next(f.message for f in report.findings if f.rule == "one-h1")
    none = a11y_check.check_html("<html lang='en'><title>x</title><main>x</main></html>")
    assert "The page has no main heading (h1)." in next(f.message for f in none.findings if f.rule == "one-h1")


def test_checker_lists_what_it_does_not_check_in_plain_words():
    assert a11y_check.NOT_CHECKED[:2] == [
        "How a real screen reader reads the page, in speech and in braille (ask for a peer check).",
        "Contrast, layout and spacing: not measured here.",
    ]
    assert sum("contrast" in item.lower() for item in a11y_check.NOT_CHECKED) == 1


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
    return re.search(r"<title>(.*), [^,<]*</title>", html, re.S).group(1)


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


def test_page_title_is_the_page_then_a_comma_and_the_site_name(client):
    """A comma, not an en dash, between page and site name, so braille lines stay clean (docs/VOICE.md 4.17)."""
    r = client.get("/help")
    assert "<title>Ask the community, Blind Creators Studio</title>" in r.text
    assert "–" not in re.search(r"<title>(.*?)</title>", r.text, re.S).group(1)
    # A page name with its own comma still splits on the last one.
    assert _title("<title>Build your ideas with AI: websites, apps, writing and research, Blind Creators Studio"
                  "</title>") == "Build your ideas with AI: websites, apps, writing and research"


def test_form_errors_keep_h1_and_title_the_same(client):
    r = post(client, "/register", {"email": "bad", "display_name": "", "password": "short"})
    assert _h1(r.text) == _title(r.text) == "Please check 3 answers: Create your account"
    r = post(client, "/login", {"email": "nobody@example.com", "password": "whatever whatever"})
    assert r.status_code == 400
    # The wrong password is not kept, and the password field says its box was emptied.
    assert _h1(r.text) == _title(r.text) == "Please check 2 answers: Sign in"
    assert "The password box was emptied so your password is not kept on the page." in r.text
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
    assert r.status_code == 400 and "To use your own Claude API key, connect it on Your account." in r.text
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
    assert _h1(client.get("/").text) == "Build your ideas with AI: websites, apps, writing and research"
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
    assert re.search(r"Quiet job \(\d\d:\d\d\): done\.", page) and "still working" not in page
    assert "<h2>Done (1)</h2>" in page
    # The clock time is in the link name, so the rest of the line gives the date only, and with one group
    # the count is said once, in its heading.
    assert re.search(r"Started \d{1,2} \w+ \d{4}\.</span>", page) and " UTC.</span>" not in page
    assert "Your latest" not in page and "Times are in UTC" in page


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
    # Each link name has the time the work started, so two runs of the same work can be told apart.
    assert re.search(r'<a href="/account">Good job \(\d\d:\d\d\)</a>', page)
    assert re.search(r'<a href="/jobs/w1">Slow job \(\d\d:\d\d\)</a>', page)
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
    assert re.search(r'Started <span id="job-elapsed">\d+ seconds?</span> ago', r.text)
    data = user_client.get("/jobs/run1.json").json()
    assert re.fullmatch(r"\d+ seconds?", data.pop("elapsed"))
    assert data == {
        "id": "run1", "status": "running", "state": "working", "title": "Slow job", "progress": "Reading your idea",
        "result_url": "", "back_url": "", "error": ""}
    post(user_client, "/logout")
    register(user_client, "b@example.com", "B")
    assert user_client.get("/jobs/run1.json").status_code == 404


def test_elapsed_words():
    from studio.core_routes import _elapsed_words
    assert _elapsed_words(1) == "1 second"
    assert _elapsed_words(45) == "45 seconds"
    assert _elapsed_words(300) == "5 minutes"
    assert _elapsed_words(3 * 3600) == "3 hours"


def test_stalled_job_json_says_so_and_names_the_way_back(user_client):
    uid = user_id()
    with db.session() as conn:
        conn.execute("INSERT INTO jobs (id, user_id, kind, title, status, created_at, started_at, back_url) VALUES"
                     " ('old2', ?, 'test', 'Old job', 'running', '2020-01-01T00:00:00+00:00',"
                     " '2020-01-01T00:00:00+00:00', '/build/3')", (uid,))
    data = user_client.get("/jobs/old2.json").json()
    # The page would say it has stopped, so the script stops checking too, and offers the way back.
    assert data["status"] == "running" and data["state"] == "stalled" and data["back_url"] == "/build/3"


def test_job_page_says_how_the_news_arrives_without_sound(user_client):
    uid = user_id()
    with db.session() as conn:
        conn.execute("INSERT INTO jobs (id, user_id, kind, title, status, progress, created_at) VALUES"
                     " ('run2', ?, 'test', 'Slow job', 'running', 'Reading your idea', ?)", (uid, db.now()))
    page = user_client.get("/jobs/run2").text
    assert "Either way, focus moves to a link to your result." in page
    assert 'aria-describedby="hint-job-sound"' in page
    assert "between half a minute and 10 minutes" in page and "After 30 minutes" in page


# ----- ease of use: members' words are never lost to a form that could not be sent ---------------

PROFILE_PAGE = {"referer": "http://testserver/account/profile"}


def _profile_form(name: str, token: str) -> dict:
    return {"display_name": name, "screen_reader": "JAWS with a Focus 40", "language": "en",
            "sight_onset": "prefer_not", "verbosity": "summary_first", "password": "never kept anywhere",
            "csrf_token": token}


def test_words_come_back_after_signing_in_again(client):
    from .conftest import csrf
    register(client)
    token = csrf(client)
    with db.session() as conn:  # signed out on every device, for example from another phone
        conn.execute("UPDATE users SET session_epoch = session_epoch + 1")
    r = client.post("/account/profile", data=_profile_form("Amina Kept", token), headers=PROFILE_PAGE,
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Faccount%2Fprofile"
    login = client.get(r.headers["location"]).text
    assert "You were signed out before this was saved. Sign in and your words will be put back in the form." in login
    r = post(client, "/login", {"email": "amina@example.com", "password": "correct horse battery",
                                "next": "/account/profile"})
    assert _h1(r.text) == "Your profile and preferences"
    assert 'value="Amina Kept"' in r.text and 'value="JAWS with a Focus 40"' in r.text
    assert "You were signed out before this was sent, so nothing was saved yet." in r.text
    assert "Your words are back in the form." in r.text
    assert "never kept anywhere" not in r.text  # passwords are never kept
    with db.session() as conn:
        assert conn.execute("SELECT display_name FROM users").fetchone()[0] == "Amina"  # nothing was saved
        assert conn.execute("SELECT COUNT(*) FROM kept_forms").fetchone()[0] == 0  # shown once, then gone
    assert 'value="Amina Kept"' not in client.get("/account/profile").text


def test_kept_words_are_not_shown_to_someone_else_who_signs_in(client):
    from .conftest import csrf
    register(client)
    token = csrf(client)
    with db.session() as conn:
        conn.execute("UPDATE users SET session_epoch = session_epoch + 1")
    client.post("/account/profile", data=_profile_form("Amina Private", token), headers=PROFILE_PAGE)
    register(client, "other@example.com", "Other")
    page = client.get("/account/profile").text
    assert "Amina Private" not in page and "nothing was saved yet" not in page


def test_expired_form_comes_back_with_the_words_in_it(user_client):
    r = user_client.post("/account/profile", data=_profile_form("Amina Again", "stale-token"), headers=PROFILE_PAGE,
                         follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account/profile"
    page = user_client.get("/account/profile").text
    assert 'value="Amina Again"' in page
    assert "This form needed refreshing to keep your account safe, so nothing was saved yet." in page
    assert "never kept anywhere" not in page


def test_words_sent_from_another_website_are_never_kept(user_client):
    """A form another website sends (a forged form) never puts its text in a member's forms."""
    evil = {"origin": "https://evil.example", "referer": "https://evil.example/page"}
    r = user_client.post("/account/profile", data=_profile_form("Planted Words", "stale-token"), headers=evil,
                         follow_redirects=False)
    assert r.status_code == 400 and "Planted Words" not in r.text
    assert "Planted Words" not in user_client.get("/account/profile").text
    with db.session() as conn:
        conn.execute("UPDATE users SET session_epoch = session_epoch + 1")  # signed out
    user_client.post("/account/profile", data=_profile_form("Planted Again", "x"), headers=evil)
    post(user_client, "/login", {"email": "amina@example.com", "password": "correct horse battery",
                                 "next": "/account/profile"})
    assert "Planted Again" not in user_client.get("/account/profile").text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM kept_forms").fetchone()[0] == 0


def test_expired_form_without_a_page_to_return_to_shows_the_words(user_client):
    data = {"csrf_token": "stale-token", "idea": "A quiz about rivers, " * 300}
    r = user_client.post("/build/new", data=data)
    assert r.status_code == 400
    assert _h1(r.text) == _title(r.text) == "This form needs refreshing: nothing was saved"
    assert "Back button" in r.text and "reload" not in r.text.lower()
    assert "<h2 id=\"kept-title\">Your words from before</h2>" in r.text
    assert "A quiz about rivers, A quiz about rivers," in r.text and "readonly" in r.text
    assert a11y_check.check_html(r.text).errors == []


def test_secrets_are_never_kept(client):
    from studio.web import kept_fields
    kept = kept_fields({"note": "my password is Tr0ub4dor&3", "api_key": "sk-ant-abc", "code": "ABCD",
                        "idea": "A bakery website"})
    assert kept == {"idea": "A bakery website"}


# ----- ease of use: the header, the menu and where you are ---------------------------------------

def test_every_page_starts_close_to_its_heading(client):
    page = client.get("/about").text
    header = page[page.index('<header'):page.index('</header>')]
    # The header holds the site name, a link to the menu and the account links: not every section.
    assert '<a href="#sections">Menu: all sections</a>' in header
    assert 'aria-label="Sections"' not in header
    assert page.index("</main>") < page.index('<nav id="sections" class="site-sections" aria-label="Sections"')
    assert page.index('aria-label="Sections"') < page.index("<footer")
    assert a11y_check.check_html(page).errors == []


def test_current_section_and_account_page_are_marked(user_client):
    page = user_client.get("/help").text
    assert '<a href="/learn" aria-current="true">Learn and help</a>' in page
    page = user_client.get("/account").text
    assert '<a href="/account" aria-current="page">Your account</a>' in page
    assert '<li class="sign-out">' in page
    assert '<a href="/jobs" aria-current="page">Your recent work</a>' in user_client.get("/jobs").text


def test_sign_in_says_why_and_keeps_the_way_back(client):
    page = client.get("/login?next=/build/3").text
    assert "Sign in to use Build. You come straight back to the page afterwards." in page
    assert 'href="/register?next=/build/3"' in page
    page = client.get("/register?next=/build/3").text
    assert 'href="/login?next=/build/3"' in page
    assert "Sign in to" not in client.get("/login").text


def test_register_explains_the_recovery_code_and_keeps_typed_words(client):
    page = client.get("/register").text
    assert "cannot reset a forgotten password" not in page
    assert "Next you get a recovery code. It is the only way back in if you forget your password" in page
    assert 'data-shows="field-password"' in page and "Show my password" in page
    assert "Other members see a code name such as Member 5A3C." in page
    r = post(client, "/register", {"email": "bad", "display_name": "Ola", "password": "long enough password"})
    assert r.status_code == 400 and "Please type your password again." in r.text and 'value="Ola"' in r.text


def test_register_while_signed_in_says_so(user_client):
    page = user_client.get("/register").text
    assert _h1(page) == "You are already signed in"
    assert "You are already signed in as amina@example.com. To make another account, sign out first." in page
    assert 'action="/register"' not in page


def test_wrong_method_offers_a_way_on(user_client):
    r = user_client.get("/logout")
    assert r.status_code == 405 and "only works from its form" in r.text
    assert '<button type="submit" class="button button-primary">Sign out</button>' in r.text
    r = user_client.get("/account/ai-key/remove")
    assert '<a href="/account/ai-key">Go to the page this form is on</a>' in r.text


def test_missing_private_pages_offer_the_section(user_client):
    r = user_client.get("/build/99999")
    assert r.status_code == 404 and '<a href="/build">Go to Build</a>' in r.text
    assert 'href="/">Go to the home page' in r.text


def test_radio_hints_reach_each_radio(user_client):
    page = user_client.get("/account/profile").text
    assert re.search(r'id="field-verbosity-1" name="verbosity" value="summary_first"[^>]*aria-describedby="hint-verbosity"',
                     page)


# ----- ease of use: the home page says what comes next and what the AI costs ----------------------

def test_signed_out_home_says_the_account_is_free_and_the_ai_is_paid(client):
    page = client.get("/").text
    assert "The account is free. To use the AI you connect a Claude API key from Anthropic" in page
    assert "or buy credits" not in page  # payments are not set up in tests
    assert "Every page here is built to work with a screen reader, a braille display, magnification" in page


def test_first_signed_in_home_lists_next_steps_until_the_ai_is_set_up(client):
    post(client, "/register", {"email": "new@example.com", "display_name": "New", "password": "correct horse battery"})
    r = post(client, "/account/recovery-code/saved")
    assert _h1(r.text) == "Your account is ready"
    assert "<h2>Your next steps</h2>" in r.text and "Save your recovery code: done." in r.text
    assert 'href="/account/ai-key">connect your Claude API key</a>' in r.text
    assert 'href="/learn/start-here"' in r.text and r.text.index("Your next steps") < r.text.index("What you can do")


def test_logout_opened_by_its_address_when_signed_out_says_so(client):
    r = client.get("/logout")
    assert _h1(r.text) == _title(r.text) == "You are signed out"
    assert "You are already signed out. Nothing else to do here." in r.text
    assert "choose the button below" not in r.text and '<a href="/login">Sign in</a>' in r.text


def test_logout_opened_by_its_address_when_signed_in_offers_the_button(user_client):
    r = user_client.get("/logout")
    assert "To sign out, choose the button below." in r.text
    assert '<form method="post" action="/logout">' in r.text


def test_password_fields_turn_off_capitals_and_autocorrect(client):
    """Show my password makes the field plain text, where phones would add capitals and autocorrect."""
    for path in ("/register", "/login", "/reset-password"):
        page = client.get(path).text
        field = re.search(r'<input id="field-password"[^>]*>', page).group(0)
        assert 'autocapitalize="none" autocorrect="off" spellcheck="false"' in field, path


def test_sign_in_has_show_password_and_says_the_box_was_emptied(client):
    page = client.get("/login").text
    assert 'data-shows="field-password"' in page and "Show my password" in page
    r = post(client, "/login", {"email": "nobody@example.com", "password": "whatever whatever"})
    assert 'id="error-password"' in r.text and "The password box was emptied" in r.text
    assert 'value="nobody@example.com"' in r.text
