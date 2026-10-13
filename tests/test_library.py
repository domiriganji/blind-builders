"""Tool library: tools blind and low-vision people made, problem reports after updates, volunteers."""
import json
import re

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
    assert "no open problems reported" in r.text and "No volunteers yet." in r.text
    assert "0 members are helping" not in r.text
    assert "The library keeps working because members help each other. There are no staff." in r.text
    assert ">Add a tool a blind or low-vision person made</a>" in r.text
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
    assert r.status_code == 200 and "<h1>Your problem report is saved: QuickMail</h1>" in r.text
    assert needs_upkeep(tid) == 1
    assert "Open problems (1)" in r.text and "Problem 1. Broke after: NVDA 2026.1" in r.text
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
    assert "<h1>Problem marked as fixed: QuickMail</h1>" in r.text and "1 open problem is left." in r.text
    assert "No open problems are left" not in r.text
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
    assert "<h1>Please check 2 answers: QuickMail in the Tool library</h1>" in r.text
    assert 'href="#field-problem"' in r.text and 'href="#field-after_update"' in r.text
    r = post(user_client, f"/library/tool/{tid}/problem", {"problem": "It broke. My password: hunter2",
                                                           "after_update": "iOS 27"})
    assert r.status_code == 400 and "password, PIN, code, card number or API key" in r.text
    assert "Everyone can read this page." in r.text
    assert 'value="iOS 27"' in r.text
    assert needs_upkeep(tid) == 0 and _problems(tid) == []
    cursor = tool_id("Cursor")
    assert report_problem(user_client, cursor).status_code == 404


def test_volunteers_are_unique_per_member(client):
    tid = tool_id("QuickMail")
    register(client, "v1@example.com", "V1")
    r = post(client, f"/library/tool/{tid}/volunteer", {"skills": "I test with JAWS"})
    assert "<h1>You are on the helper list: QuickMail</h1>" in r.text and "Thank you for helping." in r.text
    r = post(client, f"/library/tool/{tid}/volunteer", {"skills": "I test with JAWS and write C#"})
    assert "<h1>How you can help is updated: QuickMail</h1>" in r.text
    assert count("SELECT COUNT(*) FROM upkeep_volunteers WHERE tool_id = ?", (tid,)) == 1
    with db.session() as conn:
        pseudonym = conn.execute("SELECT pseudonym FROM users WHERE email = 'v1@example.com'").fetchone()["pseudonym"]
    assert f"{pseudonym}: I test with JAWS and write C#" in r.text
    assert "Members helping keep it working (1)" in r.text
    import re as _re
    main_html = _re.sub(r'name="csrf_token" value="[^"]*"', "", r.text.split("<main", 1)[1])  # tokens are random
    assert "V1" not in main_html  # listed by code name, not real name
    switch_user(client, "v2@example.com", "V2")
    post(client, f"/library/tool/{tid}/volunteer", {"skills": "I can write docs"})
    assert count("SELECT COUNT(*) FROM upkeep_volunteers WHERE tool_id = ?", (tid,)) == 2
    assert "2 members are helping keep it working" in client.get("/library").text
    r = post(client, f"/library/tool/{tid}/volunteer/remove")
    assert "<h1>You are off the helper list: QuickMail</h1>" in r.text
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
    assert r.status_code == 400
    assert "<h1>Please check 1 answer: Add a tool a blind or low-vision person made</h1>" in r.text
    assert 'href="#field-made_by_blind"' in r.text and "Tick the box to confirm" in r.text
    assert 'value="Audio Atlas"' in r.text
    r = post(user_client, "/library/add", {**data, "made_by_blind": "yes"})
    assert r.status_code == 200 and "<h1>Added to the Tool library: Audio Atlas</h1>" in r.text
    assert "Audio Atlas is now in the Tool library. Thank you." in r.text
    tid = tool_id("Audio Atlas")
    assert str(r.url).endswith(f"/library/tool/{tid}")
    with db.session() as conn:
        row = conn.execute("SELECT * FROM tools WHERE id = ?", (tid,)).fetchone()
    assert row["made_by_blind"] == 1 and row["source"] == "member" and row["added_by"] == user_id()
    assert "Audio Atlas" in user_client.get("/library").text


def test_add_duplicate_shows_existing_entry(user_client):
    r = post(user_client, "/library/add", {"name": "QM", "url": "https://github.com/kellylford/QuickMail/",
                                           "description": "x", "category": "Other", "made_by_blind": "yes"})
    assert "<h1>Already in the Tool library: QuickMail</h1>" in r.text
    assert str(r.url).endswith(f"/library/tool/{tool_id('QuickMail')}")
    r = post(user_client, "/library/add", {"name": "C", "url": "https://cursor.com/docs/release-notes",
                                           "description": "x", "category": "Other", "made_by_blind": "yes"})
    assert "nobody has" in r.text and str(r.url).endswith(f"/find/tool/{tool_id('Cursor')}")
    assert "<h1>Already in the directory: Cursor</h1>" in r.text
    with db.session() as conn:
        assert conn.execute("SELECT made_by_blind FROM tools WHERE name = 'Cursor'").fetchone()[0] == 0


def test_report_from_library_returns_to_library(user_client):
    tid = tool_id("QuickMail")
    r = post(user_client, f"/find/tool/{tid}/report", {"outcome": "worked", "screen_reader": "JAWS", "back": "library"})
    assert r.url.path == f"/library/tool/{tid}"
    assert "<h1>Your report is saved: QuickMail</h1>" in r.text
    assert "1 member: 1 worked. Tools used: JAWS" in r.text
    assert "1 member: 1 worked. Tools used: JAWS" in user_client.get("/library").text
    r = post(user_client, f"/find/tool/{tid}/report", {"back": "library"})
    assert r.status_code == 400 and "Open problems" in r.text and 'href="#field-outcome"' in r.text


def test_error_pages_h1_counts_the_answers_to_check(user_client):
    tid = tool_id("QuickMail")
    for url, data in [("/library/add", {"name": ""}), (f"/library/tool/{tid}/problem", {}),
                      (f"/library/tool/{tid}/volunteer", {}), (f"/find/tool/{tid}/report", {"back": "library"})]:
        r = post(user_client, url, data)
        assert r.status_code == 400, url
        h1 = re.search(r"<h1>(.*?)</h1>", r.text).group(1)
        assert re.match(r"Please check \d+ answers?: ", h1) and f"<title>{h1} – " in r.text, url
        assert "please fix" not in r.text.lower() and "Error: " not in r.text, url


def test_the_confirm_box_is_described_by_its_error(user_client):
    r = post(user_client, "/library/add", {"name": "X", "url": "https://x.example.org", "description": "d",
                                           "category": "Other"})
    box = re.search(r'<input type="checkbox" id="field-made_by_blind"[^>]*>', r.text).group(0)
    assert 'aria-describedby="error-made_by_blind hint-made_by_blind"' in box and 'aria-invalid="true"' in box
    box = re.search(r'<input type="checkbox" id="field-made_by_blind"[^>]*>', user_client.get("/library/add").text).group(0)
    assert 'aria-describedby="hint-made_by_blind"' in box and "aria-invalid" not in box


def test_a_problem_sent_twice_is_listed_once(user_client):
    tid = tool_id("QuickMail")
    report_problem(user_client, tid)
    r = report_problem(user_client, tid)
    assert "You already reported this problem" in r.text
    assert len(_problems(tid)) == 1 and "Open problems (1)" in r.text


def test_long_problem_fields_are_refused_not_cut(user_client):
    tid = tool_id("QuickMail")
    long_update = "NVDA 2026.1 " * 20
    r = report_problem(user_client, tid, after=long_update)
    assert r.status_code == 400 and "Keep it under 200 characters." in r.text
    assert long_update.strip() in r.text
    r = post(user_client, f"/library/tool/{tid}/volunteer", {"skills": "I test with JAWS. " * 70})
    assert r.status_code == 400 and "Keep it under 1,000 characters." in r.text
    assert _problems(tid) == [] and count("SELECT COUNT(*) FROM upkeep_volunteers") == 0


def test_problem_heading_reads_well_when_the_update_is_unknown(user_client):
    r = report_problem(user_client, tool_id("QuickMail"), after="not sure")
    assert "Problem 1. Broke after: not sure" in r.text


def test_leaving_a_list_you_were_not_on_says_so(user_client):
    r = post(user_client, f"/library/tool/{tool_id('QuickMail')}/volunteer/remove")
    assert "<h1>You were not on the helper list: QuickMail</h1>" in r.text and "Nothing changed." in r.text


def test_library_counts_and_filter_heading(user_client):
    with db.session() as conn:
        conn.execute("UPDATE tools SET made_by_blind = 0 WHERE name != 'QuickMail'")
    r = user_client.get("/library")
    assert "1 tool is in the library." in r.text and "1 tools" not in r.text
    r = user_client.get("/library?show=upkeep")
    assert "<h1>Tool library: tools that need help after an update</h1>" in r.text


def test_signing_in_after_a_session_ends_returns_to_the_tool(user_client):
    tid = tool_id("QuickMail")
    for path, anchor in [("problem", "problem-form-title"), ("problem/1/resolve", "problems-title"),
                         ("volunteer", "volunteer-form-title"), ("volunteer/remove", "volunteer-form-title")]:
        r = user_client.get(f"/library/tool/{tid}/{path}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == f"/library/tool/{tid}#{anchor}", path
    assert user_client.get("/library/tool/999999/problem").status_code == 404


def test_library_works_without_ai(no_ai_app):
    client = TestClient(no_ai_app)
    register(client)
    tid = tool_id("QuickMail")
    assert client.get("/library").status_code == 200
    assert report_problem(client, tid).status_code == 200
    assert needs_upkeep(tid) == 1


# ----- ease of use -----------------------------------------------------------------------------

def test_after_each_action_the_title_says_what_happened_and_the_page_opens_at_the_top(user_client):
    tid = tool_id("QuickMail")
    for url, data, expected in [
            (f"/library/tool/{tid}/problem", {"problem": "Silent", "after_update": "NVDA 2026.1"},
             "Your problem report is saved: QuickMail"),
            (f"/library/tool/{tid}/volunteer", {"skills": "I test with JAWS"}, "You are on the helper list: QuickMail"),
            (f"/library/tool/{tid}/volunteer/remove", {}, "You are off the helper list: QuickMail"),
            (f"/find/tool/{tid}/report", {"outcome": "worked", "back": "library"}, "Your report is saved: QuickMail")]:
        r = post(user_client, url, data, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == f"/library/tool/{tid}", url
        page = user_client.get(r.headers["location"]).text
        assert f"<h1>{expected}</h1>" in page and f"<title>{expected} – " in page, url
    pid = _problems(tid)[0]["id"]
    r = post(user_client, f"/library/tool/{tid}/problem/{pid}/resolve", follow_redirects=False)
    assert r.headers["location"] == f"/library/tool/{tid}"
    assert "<h1>Problem marked as fixed: QuickMail</h1>" in user_client.get(r.headers["location"]).text
    assert "<h1>QuickMail in the Tool library</h1>" in user_client.get(f"/library/tool/{tid}").text


def test_the_three_forms_say_when_to_use_each_and_are_listed_at_the_top(user_client):
    page = user_client.get(f"/library/tool/{tool_id('QuickMail')}").text
    nav = re.search(r'<nav aria-label="On this page">(.*?)</nav>', page, re.S).group(1)
    for anchor, name in [("problem-form-title", "Report a problem after an update"),
                         ("volunteer-form-title", "I can help keep this working"),
                         ("report-form-title", "Did it work for you?")]:
        assert f'<a href="#{anchor}">{name}</a>' in nav
        assert f'<h2 id="{anchor}" tabindex="-1">{name}</h2>' in page  # following the link moves focus there
    assert "Use this if it worked before and an update broke it." in page
    assert "Use this to say how QuickMail works for you in general." in page
    assert ">Send the problem to helpers</button>" in page and ">Save how it worked for me</button>" in page
    assert ">Report the problem</button>" not in page and ">Save my report</button>" not in page


def test_the_adder_can_change_a_library_tool_and_comes_back_to_the_library(user_client):
    post(user_client, "/library/add", {"name": "Talking Timer", "url": "timer.example.org", "description": "A timer.",
                                       "category": "Everyday tasks", "made_by_blind": "yes"})
    tid = tool_id("Talking Timer")
    with db.session() as conn:
        assert conn.execute("SELECT url FROM tools WHERE id = ?", (tid,)).fetchone()[0] == "https://timer.example.org"
    page = user_client.get(f"/library/tool/{tid}").text
    assert f'href="/find/tool/{tid}/change?back=library">Change the details of Talking Timer</a>' in page
    r = post(user_client, f"/find/tool/{tid}/change", {"name": "Talking Timer", "url": "https://timer.example.org",
                                                       "description": "A timer that speaks.", "category": "Everyday tasks",
                                                       "made_by_blind": "yes", "back": "library"})
    assert r.url.path == f"/library/tool/{tid}" and "<h1>Your changes are saved: Talking Timer</h1>" in r.text


def test_library_dates_read_as_words(user_client):
    tid = tool_id("QuickMail")
    report_problem(user_client, tid)
    main = user_client.get(f"/library/tool/{tid}").text.split("<main", 1)[1]
    assert not re.search(r"\d{4}-\d{2}-\d{2}", main)
    assert re.search(r"Reported by Member \w+ on \d{1,2} [A-Z][a-z]+ \d{4}\.", main)
