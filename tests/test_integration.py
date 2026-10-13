"""Journeys that cross sections, and the shared behaviour sections rely on."""
from fastapi.testclient import TestClient

from studio import db

from .conftest import post, register, user_id


def _count(sql, *args):
    with db.session() as conn:
        return conn.execute(sql, args).fetchone()[0]


def test_sign_in_keeps_the_full_address(client):
    r = client.get("/write/new?title=My+story&kind=story", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fwrite%2Fnew%3Ftitle%3DMy%2Bstory%26kind%3Dstory"
    register(client)
    post(client, "/logout")
    page = client.get(r.headers["location"])
    assert 'value="/write/new?title=My+story&amp;kind=story"' in page.text
    signed_in = post(client, "/login", {"email": "amina@example.com", "password": "correct horse battery",
                                        "next": "/write/new?title=My+story&kind=story"})
    assert signed_in.status_code == 200 and "My story" in signed_in.text


def test_deleting_an_account_removes_private_work_and_signs_out_other_devices(app, fake_ai):
    phone = TestClient(app)
    laptop = TestClient(app)
    register(phone)
    uid = user_id()
    with db.session() as conn:
        conn.execute("INSERT INTO build_projects (user_id, title, kind, idea, status, spec_json, share_token, created_at, updated_at)"
                     " VALUES (?, 'Shop', 'website', 'idea', 'draft', '{}', 'tok', ?, ?)", (uid, db.now(), db.now()))
        conn.execute("INSERT INTO write_docs (user_id, title, kind, body, target, created_at, updated_at)"
                     " VALUES (?, 'Essay', 'article', 'text', '', ?, ?)", (uid, db.now(), db.now()))
        conn.execute("INSERT INTO find_searches (user_id, need, created_at) VALUES (?, 'a screen reader', ?)", (uid, db.now()))
    post(laptop, "/login", {"email": "amina@example.com", "password": "correct horse battery"})
    assert "Sign out" in laptop.get("/").text
    r = post(phone, "/account/delete", {"confirm": "yes"})
    assert r.status_code == 200
    assert _count("SELECT COUNT(*) FROM build_projects WHERE user_id = ?", uid) == 0
    assert _count("SELECT COUNT(*) FROM write_docs WHERE user_id = ?", uid) == 0
    assert _count("SELECT COUNT(*) FROM jobs WHERE user_id = ?", uid) == 0
    for table in ("errands", "research_projects", "find_searches"):
        assert _count(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", uid) == 0
    assert "Sign out" not in laptop.get("/").text
    assert laptop.get("/account", follow_redirects=False).status_code == 303


def test_build_links_to_a_prefilled_peer_check(user_client):
    page = user_client.get("/checks/new?url=http%3A%2F%2Ftestserver%2Fbuild%2Fp%2Fabc%2Fv%2F1%2F&title=Cake+shop")
    assert page.status_code == 200
    assert 'value="http://testserver/build/p/abc/v/1/"' in page.text and 'value="Cake shop"' in page.text


def test_shared_markdown_marks_external_links_and_table_headers():
    from studio.web import markdown
    html = str(markdown("[site](https://example.org) [here](/learn)\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n<script>x</script>"))
    assert '<a href="https://example.org" rel="noopener noreferrer">' in html
    assert '<a href="/learn">' in html
    assert '<th scope="col">a</th>' in html
    assert "<script>" not in html


def test_links_from_one_section_to_another_resolve(user_client):
    for url in ["/find/ask?need=a+recipe+app", "/help/ask?title=Question+about+a+guide",
                "/write/new?title=Draft&kind=paper", "/checks/new?url=https%3A%2F%2Fexample.org&title=Mine"]:
        r = user_client.get(url)
        assert r.status_code == 200, url


def test_too_large_requests_get_a_plain_page(settings):
    from fastapi.testclient import TestClient
    from studio.app import create_app
    settings.max_body_bytes = 1000
    c = TestClient(create_app(settings))
    big = "x" * 5000
    r = c.post("/login", content=big, headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413 and "That was too large" in r.text

    def chunks():
        for _ in range(10):
            yield b"y" * 500

    r = c.post("/login", content=chunks(), headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


def test_huge_ids_are_not_found(user_client):
    r = user_client.get("/write/99999999999999999999999")
    assert r.status_code == 404 and "Page not found" in r.text


def test_wrong_method_is_explained(user_client):
    r = user_client.get("/logout")
    assert r.status_code == 405 and "only works from its form" in r.text


def test_signing_in_after_a_form_returns_to_the_page_not_the_form(client):
    r = client.post("/write/new", data={"title": "x"}, headers={"referer": "http://testserver/write?tab=1"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fwrite%3Ftab%3D1"
    r = client.post("/write/new", data={"title": "x"}, headers={"referer": "https://evil.example/write"},
                    follow_redirects=False)
    assert r.headers["location"] == "/login?next=%2Fwrite%2Fnew"


def test_jobs_dedupe_back_url_and_restart_recovery(user_client, app, settings):
    from studio import jobs
    uid = user_id()
    calls = []

    def slow(ctx):
        calls.append(1)
        return "/done"

    with db.session() as conn:
        conn.execute("INSERT INTO jobs (id, user_id, kind, title, status, created_at, dedupe_key) VALUES"
                     " ('running1', ?, 'k', 'Running', 'running', ?, 'k:1')", (uid, db.now()))
    assert jobs.submit(uid, "k", "Again", slow, dedupe_key="k:1") == "running1"
    assert calls == []
    other = jobs.submit(uid, "k", "Other", slow, dedupe_key="k:2")
    assert other != "running1" and calls == [1]

    def broken(ctx):
        raise RuntimeError("boom")

    failed = jobs.submit(uid, "k", "Broken", broken, back_url="/write/7")
    assert 'href="/write/7"' in user_client.get(f"/jobs/{failed}").text
    failed_evil = jobs.submit(uid, "k", "Broken", broken, back_url="//evil.example")
    page = user_client.get(f"/jobs/{failed_evil}", headers={"referer": "https://evil.example/x"})
    assert "evil.example" not in page.text

    from studio.app import create_app
    create_app(settings)  # a restart: the job left running is now failed with a plain reason
    job = jobs.get("running1")
    assert job["status"] == "failed" and "restarted" in job["error"]


def test_markdown_pictures_become_links_and_headings_nest():
    from studio.web import markdown, nest_headings
    html = str(markdown("![a cat](https://example.org/cat.png)"))
    assert "<img" not in html and 'Picture: a cat' in html and 'rel="noopener noreferrer"' in html
    nested = str(nest_headings("<h1>A</h1><p>x</p><h3>B</h3>", base=2))
    assert nested == "<h2>A</h2><p>x</p><h3>B</h3>"
    assert str(nest_headings("<h2>A</h2><h4>B</h4><h6>C</h6>", base=3)) == "<h3>A</h3><h4>B</h4><h5>C</h5>"


def test_radio_groups_can_take_focus_from_the_error_summary(client):
    from studio.web import templates
    t = templates.env.from_string('{% from "_macros.html" import radios, checkbox %}'
                                  '{{ radios("kind", "Kind", [("a", "A"), ("b", "B")], error="Choose one") }}'
                                  '{{ checkbox("agree", "I agree", error="Tick this") }}')
    html = t.render()
    assert 'id="field-kind" tabindex="-1"' in html and 'aria-invalid="true"' in html
    assert 'aria-describedby="error-agree"' in html and 'id="error-agree"' in html


def test_sources_list_links_only_web_addresses_and_optional_marks_can_be_hidden():
    from studio.web import templates
    t = templates.env.from_string('{% from "_macros.html" import sources_list, textarea %}'
                                  '{{ sources_list(sources) }}{{ textarea("body", "Your text", mark_optional=false) }}'
                                  '{{ textarea("note", "Note") }}')
    html = t.render(sources=[{"url": "https://example.org", "title": "Good"}, {"url": "javascript:alert(1)", "title": "Bad"}])
    assert '<a href="https://example.org"' in html and "javascript:" not in html.split("Bad")[0].split("Good")[1]
    assert 'href="javascript' not in html
    assert ">Your text</label>" in html and "Note <span class=\"optional\">(optional)</span>" in html


def test_form_text_takes_the_first_value_and_limits_length():
    from studio.web import form_text
    assert form_text({"a": ["one", "two"]}, "a") == "one"
    assert form_text({"a": "x" * 50}, "a", 10) == "x" * 10
    assert form_text({}, "missing") == ""


def test_credit_hold_counts_images_by_their_real_cost():
    from studio.ai import AIClient, IMAGE_MAX_TOKENS
    client = AIClient(api_key="x", model="claude-opus-5-5", user_id=1, paid_by="credits", markup=1.0, sdk_client=object())
    big_png = "A" * 300_000  # about 225 KB of base64 picture data
    with_images = {"system": "s", "max_tokens": 1000, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": big_png}}] * 6 + [
        {"type": "text", "text": "Review the design."}]}]}
    text_only = {"system": "s", "max_tokens": 1000, "messages": [{"role": "user", "content": "Review the design."}]}
    extra = client._worst_case_cents(with_images) - client._worst_case_cents(text_only)
    # six images cost at most 6 * 1,600 input tokens: about 4 cents on Opus 5.5, not dollars
    assert 0 < extra <= 5
    assert IMAGE_MAX_TOKENS == 1_600
