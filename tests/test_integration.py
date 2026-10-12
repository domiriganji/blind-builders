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
