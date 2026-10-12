"""Tool library: tools blind people made, problem reports after updates, volunteers."""
import json

from fastapi.testclient import TestClient

from studio import db
from studio.features import find

from .conftest import post, register, user_id

SEED = json.loads(find.SEED_FILE.read_text(encoding="utf-8"))
BLIND_MADE = [i for i in SEED if i["made_by_blind"]]


def tool_id(name: str) -> int:
    with db.session() as conn:
        return conn.execute("SELECT id FROM tools WHERE name = ?", (name,)).fetchone()["id"]


def needs_upkeep(tid: int) -> int:
    with db.session() as conn:
        return conn.execute("SELECT needs_upkeep FROM tools WHERE id = ?", (tid,)).fetchone()["needs_upkeep"]


def count(sql: str, args=()) -> int:
    with db.session() as conn:
        return conn.execute(sql, args).fetchone()[0]


def switch_user(client, email, name):
    post(client, "/logout")
    register(client, email, name)


def report_problem(client, tid, problem="Buttons are silent", after="NVDA 2026.1"):
    return post(client, f"/library/tool/{tid}/problem", {"problem": problem, "after_update": after})


def test_library_lists_only_tools_blind_people_made(client):
    r = client.get("/library")
    assert r.status_code == 200 and "<h1>Tool library</h1>" in r.text
    assert f"All ({len(BLIND_MADE)})" in r.text and "Need help after an update (0)" in r.text
    assert "QuickMail" in r.text and "Maintainer: Kelly Ford." in r.text
    assert "no open problems reported" in r.text
    assert ">Cursor<" not in r.text and ">Seeing AI<" not in r.text
    assert 'aria-current="page">All' in r.text
    r = client.get("/library?show=upkeep")
    assert "No tool needs help right now" in r.text
    assert 'aria-current="page">Need help after an update' in r.text


def test_other_tools_redirect_to_find(client):
    tid = tool_id("Cursor")
    r = client.get(f"/library/tool/{tid}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/find/tool/{tid}"
    assert client.get("/library/tool/999999").status_code == 404


def test_upkeep_request_filter_and_resolve(client):
    tid = tool_id("QuickMail")
    register(client, "rep@example.com", "Reporter")
    r = report_problem(client, tid)
    assert r.status_code == 200 and "Your problem report is saved" in r.text
    assert needs_upkeep(tid) == 1
    assert "Open problems (1)" in r.text and "Problem 1: after NVDA 2026.1" in r.text
    assert "Buttons are silent" in r.text
    assert "Mark problem 1 as fixed" in r.text
    report_problem(client, tid, "Search box not announced", "Windows 11 update")
    listing = client.get("/library?show=upkeep").text
    assert "Need help after an update (1)" in listing and "QuickMail" in listing
    assert "needs help after an update, 2 open problems" in listing
    assert "AccessCaster" not in listing
    assert "Needs help after an update." in client.get("/find", params={"q": "QuickMail"}).text
    problems = [r_["id"] for r_ in _problems(tid)]
    r = post(client, f"/library/tool/{tid}/problem/{problems[0]}/resolve")
    assert "The problem is marked as fixed." in r.text and "No open problems are left" not in r.text
    assert needs_upkeep(tid) == 1  # one problem is still open
    r = post(client, f"/library/tool/{tid}/problem/{problems[1]}/resolve")
    assert "No open problems are left for this tool." in r.text
    assert needs_upkeep(tid) == 0
    assert "Fixed problems (2)" in r.text and "Open problems (0)" in r.text
    assert "No tool needs help right now" in client.get("/library?show=upkeep").text


def _problems(tid):
    with db.session() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM upkeep_requests WHERE tool_id = ? ORDER BY id", (tid,))]


def test_only_reporter_or_adder_can_resolve(client):
    register(client, "adder@example.com", "Adder")
    post(client, "/library/add", {"name": "Talking Timer", "url": "https://timer.example.org",
                                  "description": "A timer that speaks.", "category": "Everyday tasks",
                                  "made_by_blind": "yes"})
    tid = tool_id("Talking Timer")
    switch_user(client, "rep@example.com", "Reporter")
    report_problem(client, tid)
    pid = _problems(tid)[0]["id"]
    switch_user(client, "stranger@example.com", "Stranger")
    page = client.get(f"/library/tool/{tid}").text
    assert "Mark problem 1 as fixed" not in page
    assert post(client, f"/library/tool/{tid}/problem/{pid}/resolve").status_code == 404
    assert needs_upkeep(tid) == 1
    # The problem must belong to the tool in the address.
    other = tool_id("QuickMail")
    switch_user(client, "adder2@example.com", "Adder two")
    assert post(client, f"/library/tool/{other}/problem/{pid}/resolve").status_code == 404
    # The member who added the tool can resolve it.
    post(client, "/logout")
    post(client, "/login", {"email": "adder@example.com", "password": "correct horse battery"})
    assert "Mark problem 1 as fixed" in client.get(f"/library/tool/{tid}").text
    r = post(client, f"/library/tool/{tid}/problem/{pid}/resolve")
    assert r.status_code == 200 and needs_upkeep(tid) == 0


def test_research_tool_problem_resolved_by_reporter(user_client):
    tid = tool_id("AccessCaster")
    report_problem(user_client, tid)
    pid = _problems(tid)[0]["id"]
    assert post(user_client, f"/library/tool/{tid}/problem/{pid}/resolve").status_code == 200
    assert needs_upkeep(tid) == 0


def test_problem_validation(user_client):
    tid = tool_id("QuickMail")
    r = post(user_client, f"/library/tool/{tid}/problem", {"problem": "", "after_update": ""})
    assert r.status_code == 400
    assert "QuickMail: please fix the problems" in r.text
    assert 'href="#field-problem"' in r.text and 'href="#field-after_update"' in r.text
    r = post(user_client, f"/library/tool/{tid}/problem", {"problem": "It broke. My password: hunter2",
                                                           "after_update": "iOS 27"})
    assert r.status_code == 400 and "card number, password or one-time code" in r.text
    assert 'value="iOS 27"' in r.text
    assert needs_upkeep(tid) == 0 and _problems(tid) == []
    cursor = tool_id("Cursor")
    assert report_problem(user_client, cursor).status_code == 404


def test_volunteers_are_unique_per_member(client):
    tid = tool_id("QuickMail")
    register(client, "v1@example.com", "V1")
    r = post(client, f"/library/tool/{tid}/volunteer", {"skills": "I test with JAWS"})
    assert "You are on the list" in r.text or "on the list of members" in r.text
    r = post(client, f"/library/tool/{tid}/volunteer", {"skills": "I test with JAWS and write C#"})
    assert "Your skills are updated." in r.text
    assert count("SELECT COUNT(*) FROM upkeep_volunteers WHERE tool_id = ?", (tid,)) == 1
    with db.session() as conn:
        pseudonym = conn.execute("SELECT pseudonym FROM users WHERE email = 'v1@example.com'").fetchone()["pseudonym"]
    assert f"{pseudonym}: I test with JAWS and write C#" in r.text
    assert "Members helping keep it working (1)" in r.text
    assert "V1" not in r.text.split("<main", 1)[1]  # listed by code name, not real name
    switch_user(client, "v2@example.com", "V2")
    post(client, f"/library/tool/{tid}/volunteer", {"skills": "I can write docs"})
    assert count("SELECT COUNT(*) FROM upkeep_volunteers WHERE tool_id = ?", (tid,)) == 2
    assert "2 members are helping keep it working" in client.get("/library").text
    r = post(client, f"/library/tool/{tid}/volunteer/remove")
    assert "You are no longer on the list" in r.text
    assert count("SELECT COUNT(*) FROM upkeep_volunteers WHERE tool_id = ?", (tid,)) == 1


def test_volunteer_validation(user_client):
    tid = tool_id("QuickMail")
    r = post(user_client, f"/library/tool/{tid}/volunteer", {"skills": "  "})
    assert r.status_code == 400 and 'href="#field-skills"' in r.text
    assert count("SELECT COUNT(*) FROM upkeep_volunteers") == 0


def test_signed_out_can_read_but_must_sign_in_to_post(client):
    tid = tool_id("QuickMail")
    r = client.get(f"/library/tool/{tid}")
    assert r.status_code == 200
    assert "Sign in to report a problem" in r.text and "Sign in to volunteer" in r.text
    assert "Sign in to tell others whether it worked for you" in r.text
    for url, data in [(f"/library/tool/{tid}/problem", {"problem": "x", "after_update": "y"}),
                      (f"/library/tool/{tid}/volunteer", {"skills": "x"}),
                      ("/library/add", {"name": "x"})]:
        r = post(client, url, data, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), url
    assert count("SELECT COUNT(*) FROM upkeep_requests") == 0
    r = client.get("/library/add", follow_redirects=False)
    assert r.status_code == 303


def test_add_requires_confirmation(user_client):
    data = {"name": "Audio Atlas", "url": "https://atlas.example.org", "description": "Maps you can hear.",
            "category": "Everyday tasks", "maintainer": "Rosa"}
    r = post(user_client, "/library/add", data)
    assert r.status_code == 400 and "Add a tool a blind person made: please fix the problems" in r.text
    assert 'href="#field-made_by_blind"' in r.text and "Tick the box to confirm" in r.text
    assert 'value="Audio Atlas"' in r.text
    r = post(user_client, "/library/add", {**data, "made_by_blind": "yes"})
    assert r.status_code == 200 and "<h1>Audio Atlas</h1>" in r.text
    assert "Audio Atlas is now in the Tool library" in r.text
    tid = tool_id("Audio Atlas")
    assert str(r.url).endswith(f"/library/tool/{tid}")
    with db.session() as conn:
        row = conn.execute("SELECT * FROM tools WHERE id = ?", (tid,)).fetchone()
    assert row["made_by_blind"] == 1 and row["source"] == "member" and row["added_by"] == user_id()
    assert "Audio Atlas" in user_client.get("/library").text


def test_add_duplicate_shows_existing_entry(user_client):
    r = post(user_client, "/library/add", {"name": "QM", "url": "https://github.com/kellylford/QuickMail/",
                                           "description": "x", "category": "Other", "made_by_blind": "yes"})
    assert "already in the Tool library" in r.text and "<h1>QuickMail</h1>" in r.text
    assert str(r.url).endswith(f"/library/tool/{tool_id('QuickMail')}")
    r = post(user_client, "/library/add", {"name": "C", "url": "https://cursor.com/docs/release-notes",
                                           "description": "x", "category": "Other", "made_by_blind": "yes"})
    assert "nobody has" in r.text and str(r.url).endswith(f"/find/tool/{tool_id('Cursor')}")
    with db.session() as conn:
        assert conn.execute("SELECT made_by_blind FROM tools WHERE name = 'Cursor'").fetchone()[0] == 0


def test_report_from_library_returns_to_library(user_client):
    tid = tool_id("QuickMail")
    r = post(user_client, f"/find/tool/{tid}/report", {"outcome": "worked", "screen_reader": "JAWS", "back": "library"})
    assert r.url.path == f"/library/tool/{tid}"
    assert "1 member: 1 worked. Screen readers: JAWS" in r.text
    assert "1 member: 1 worked. Screen readers: JAWS" in user_client.get("/library").text
    r = post(user_client, f"/find/tool/{tid}/report", {"back": "library"})
    assert r.status_code == 400 and "Open problems" in r.text and 'href="#field-outcome"' in r.text


def test_library_works_without_ai(no_ai_app):
    client = TestClient(no_ai_app)
    register(client)
    tid = tool_id("QuickMail")
    assert client.get("/library").status_code == 200
    assert report_problem(client, tid).status_code == 200
    assert needs_upkeep(tid) == 1
