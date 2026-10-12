"""Find before you build: directory, member reports, adding tools and the AI web search."""
import json
import re
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from studio import db
from studio.ai import AIError, ResearchResult, Source
from studio.features import find

from .conftest import post, register, user_id

SEED = json.loads(find.SEED_FILE.read_text(encoding="utf-8"))


def tool_id(name: str) -> int:
    with db.session() as conn:
        return conn.execute("SELECT id FROM tools WHERE name = ?", (name,)).fetchone()["id"]


def count(sql: str, args=()) -> int:
    with db.session() as conn:
        return conn.execute(sql, args).fetchone()[0]


def switch_user(client, email, name):
    post(client, "/logout")
    register(client, email, name)


# ----- seed data -------------------------------------------------------------------------------

def test_seed_file_is_well_formed():
    assert 40 <= len(SEED) <= 80
    urls = [item["url"] for item in SEED]
    assert len(urls) == len(set(urls))
    for item in SEED:
        assert item["name"].strip() and item["description"].strip(), item
        assert item["category"] in find.CATEGORIES, item
        assert isinstance(item["made_by_blind"], bool)
        assert item["source"] == "research"
        parts = urlsplit(item["url"])
        assert parts.scheme in ("http", "https") and parts.netloc, item["url"]


def test_seed_urls_come_from_the_research():
    research = (find.SEED_FILE.parent.parent.parent / "docs" / "research")
    blob = (research / "verification-results.json").read_text() + (research / "landscape-brief.md").read_text()
    missing = [item["url"] for item in SEED if item["url"] not in blob]
    assert not missing, missing


def test_seed_loads_and_is_idempotent(client):
    assert count("SELECT COUNT(*) FROM tools") == len(SEED)
    find.seed()
    find.seed()
    assert count("SELECT COUNT(*) FROM tools") == len(SEED)
    assert count("SELECT COUNT(*) FROM tools WHERE made_by_blind = 1") == sum(1 for i in SEED if i["made_by_blind"])
    with db.session() as conn:
        for row in conn.execute("SELECT url, source FROM tools"):
            assert row["url"].startswith(("http://", "https://"))
            assert row["source"] == "research"


def test_seed_does_not_duplicate_a_member_entry(user_client):
    with db.session() as conn:
        conn.execute("DELETE FROM tools WHERE url = ?", (SEED[0]["url"],))
    post(user_client, "/find/add", {"name": "Mine", "url": SEED[0]["url"], "description": "Added by me.",
                                    "category": "Other"})
    find.seed()
    with db.session() as conn:
        rows = conn.execute("SELECT name, source FROM tools WHERE url = ?", (SEED[0]["url"],)).fetchall()
    assert [(r["name"], r["source"]) for r in rows] == [("Mine", "member")]


# ----- directory -------------------------------------------------------------------------------

def test_index_lists_every_tool_in_pages(client):
    r = client.get("/find")
    assert r.status_code == 200
    assert "<h1>Find before you build</h1>" in r.text
    assert f"All tools in the directory ({len(SEED)})" in r.text
    assert "Next page of tools" in r.text and "Page 1 of" in r.text
    assert r.text.count('<h3><a href="/find/tool/') == find.PAGE_SIZE
    last = client.get("/find?page=99")
    assert "Previous page of tools" in last.text and "Next page of tools" not in last.text
    assert "No member has reported on this yet" in r.text


def test_search_matches_name_and_description(client):
    r = client.get("/find", params={"q": "Balatro"})
    assert "1 tool matches your search" in r.text and "Blindfold for Balatro" in r.text
    r = client.get("/find", params={"q": "RODECaster"})  # only in the description
    assert "AccessCaster" in r.text
    r = client.get("/find", params={"q": "nvda add-on"})
    assert "AI Content Describer" in r.text and "Glide" not in r.text


def test_search_treats_wildcards_as_text(client):
    r = client.get("/find", params={"q": "%"})
    assert "0 tools match your search" in r.text
    assert "No tools in the directory match" in r.text


def test_category_filter(client):
    r = client.get("/find", params={"category": "Games"})
    games = [i for i in SEED if i["category"] == "Games"]
    assert f"{len(games)} tools match your search" in r.text
    assert "Blindfold for Balatro" in r.text and "QuickMail" not in r.text
    r = client.get("/find", params={"category": "Games", "q": "Godot"})
    assert "UnseenGodot" in r.text and "Blindfold" not in r.text
    r = client.get("/find", params={"category": "Not a category"})
    assert f"All tools in the directory ({len(SEED)})" in r.text


def test_tool_page_shows_facts_and_link(client):
    tid = tool_id("QuickMail")
    r = client.get(f"/find/tool/{tid}")
    assert r.status_code == 200
    assert "<h1>QuickMail</h1>" in r.text
    assert 'href="https://github.com/kellylford/QuickMail" rel="noopener noreferrer"' in r.text
    assert "The site's research says a blind or low-vision person made this." in r.text
    assert "See QuickMail in the Tool library" in r.text
    assert client.get("/find/tool/999999").status_code == 404


# ----- member reports --------------------------------------------------------------------------

def test_report_summary_counts(client):
    tid = tool_id("Seeing AI")
    register(client, "a@example.com", "A")
    post(client, f"/find/tool/{tid}/report", {"outcome": "did_not_work", "screen_reader": "NVDA"})
    # A member's latest report replaces their earlier one in the summary.
    post(client, f"/find/tool/{tid}/report", {"outcome": "worked", "screen_reader": "NVDA", "device": "Android phone",
                                              "sight_onset": "later", "language": "Arabic", "note": "Reads menus well."})
    switch_user(client, "b@example.com", "B")
    post(client, f"/find/tool/{tid}/report", {"outcome": "worked", "screen_reader": "TalkBack"})
    switch_user(client, "c@example.com", "C")
    r = post(client, f"/find/tool/{tid}/report", {"outcome": "partly", "screen_reader": "nvda"})
    assert r.status_code == 200 and "Your report is saved" in r.text
    expected = "3 members: 2 worked, 1 partly. Screen readers: NVDA, TalkBack"
    assert expected in r.text
    assert expected in client.get("/find", params={"q": "Seeing AI"}).text
    assert "Sight: lost sight later in life" in r.text and "Language: Arabic" in r.text
    assert "Reads menus well." in r.text
    assert "What members say (4 reports)" in r.text
    assert find.report_summaries([tid])[tid] == expected


def test_report_summary_singular_and_empty(client):
    tid = tool_id("Glide")
    assert find.report_summaries([tid])[tid] == "No member has reported on this yet"
    register(client)
    post(client, f"/find/tool/{tid}/report", {"outcome": "did_not_work"})
    assert find.report_summaries([tid])[tid] == "1 member: 1 did not work"


def test_report_prefills_profile(user_client):
    r = user_client.get(f"/find/tool/{tool_id('Glide')}")
    assert 'name="screen_reader" type="text" value="NVDA"' in r.text
    assert 'value="prefer_not" selected' in r.text


def test_report_validation(user_client):
    tid = tool_id("Glide")
    r = post(user_client, f"/find/tool/{tid}/report", {"note": "no outcome"})
    assert r.status_code == 400
    assert "Glide: please fix the problems" in r.text and 'href="#field-outcome"' in r.text
    assert "no outcome" in r.text  # the note is kept
    r = post(user_client, f"/find/tool/{tid}/report", {"outcome": "worked", "sight_onset": "sometimes"})
    assert r.status_code == 400 and 'href="#field-sight_onset"' in r.text
    r = post(user_client, f"/find/tool/{tid}/report", {"outcome": "worked", "note": "My card is 4111 1111 1111 1111"})
    assert r.status_code == 400 and "card number, password or one-time code" in r.text
    assert count("SELECT COUNT(*) FROM tool_reports") == 0


def test_signed_out_can_read_but_must_sign_in_to_post(client):
    tid = tool_id("Glide")
    r = client.get(f"/find/tool/{tid}")
    assert r.status_code == 200 and "Sign in to tell others whether it worked for you" in r.text
    assert 'name="outcome"' not in r.text
    r = post(client, f"/find/tool/{tid}/report", {"outcome": "worked"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert count("SELECT COUNT(*) FROM tool_reports") == 0
    r = client.get("/find/add", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert "Sign in to ask the AI to search the web" in client.get("/find").text


def test_report_needs_csrf(user_client):
    r = user_client.post(f"/find/tool/{tool_id('Glide')}/report", data={"outcome": "worked"})
    assert r.status_code == 400
    assert count("SELECT COUNT(*) FROM tool_reports") == 0


# ----- adding tools ----------------------------------------------------------------------------

def test_add_tool_and_duplicate_url(user_client):
    r = post(user_client, "/find/add", {"name": "Talking Scale", "url": "https://scale.example.org/app",
                                        "description": "A kitchen scale app that speaks.", "category": "Everyday tasks",
                                        "maintainer": "Sam"})
    assert r.status_code == 200
    assert "<h1>Talking Scale</h1>" in r.text and "Talking Scale is now in the directory" in r.text
    assert "added by Member" in r.text
    new_id = tool_id("Talking Scale")
    assert str(r.url).endswith(f"/find/tool/{new_id}")
    with db.session() as conn:
        row = conn.execute("SELECT * FROM tools WHERE id = ?", (new_id,)).fetchone()
    assert row["source"] == "member" and row["added_by"] == user_id() and row["made_by_blind"] == 0
    # Same address with http, www and a trailing slash goes to the existing entry.
    r = post(user_client, "/find/add", {"name": "Copy", "url": "http://www.scale.example.org/app/",
                                        "description": "", "category": ""})
    assert str(r.url).endswith(f"/find/tool/{new_id}")
    assert "already in the directory" in r.text
    assert count("SELECT COUNT(*) FROM tools WHERE name = 'Copy'") == 0
    # A researched tool's address also counts.
    r = post(user_client, "/find/add", {"name": "QM", "url": "https://github.com/kellylford/QuickMail",
                                        "description": "x", "category": "Other"})
    assert "<h1>QuickMail</h1>" in r.text


def test_add_tool_validation(user_client):
    r = post(user_client, "/find/add", {"name": "", "url": "javascript:alert(1)", "description": "", "category": "Nope"})
    assert r.status_code == 400
    assert "Add a tool to the directory: please fix the problems" in r.text
    for name in ("name", "url", "description", "category"):
        assert f'href="#field-{name}"' in r.text
    r = post(user_client, "/find/add", {"name": "X", "url": "ftp://example.org", "description": "d", "category": "Other"})
    assert r.status_code == 400 and "starts with http:// or https://" in r.text
    r = post(user_client, "/find/add", {"name": "X", "url": "https://example.org", "description": "my password is hunter2",
                                        "category": "Other"})
    assert r.status_code == 400 and "card number, password or one-time code" in r.text
    assert count("SELECT COUNT(*) FROM tools WHERE source = 'member'") == 0


def test_add_tool_made_by_blind_appears_in_library(user_client):
    post(user_client, "/find/add", {"name": "Braille Notes", "url": "https://notes.example.org",
                                    "description": "Notes for braille displays.", "category": "Writing and publishing",
                                    "made_by_blind": "yes"})
    tid = tool_id("Braille Notes")
    assert "Braille Notes" in user_client.get("/library").text
    r = user_client.get(f"/find/tool/{tid}")
    assert "The member who added this says a blind or low-vision person made it." in r.text


def test_url_helpers():
    assert find.valid_url("https://example.org/x")
    assert not find.valid_url("javascript:alert(1)")
    assert not find.valid_url("https://exa mple.org")
    assert not find.valid_url("example.org")
    assert find.norm_url("HTTP://WWW.Example.org/a/") == find.norm_url("https://example.org/a")
    assert find.norm_url("https://example.org/a?b=1") != find.norm_url("https://example.org/a")


def test_secret_detection():
    assert find.looks_secret("card 4111-1111-1111-1111")
    assert find.looks_secret("The one-time code: 123456")
    assert find.looks_secret("my PIN is 1234")
    assert not find.looks_secret("I use a password manager with NVDA")
    assert not find.looks_secret("NVDA 2026.1 broke it")


# ----- AI web search ---------------------------------------------------------------------------

RESEARCH = ResearchResult(
    text="## Summary\nTwo tools exist.\n\nAI Content Describer (https://github.com/cartertemm/AI-content-describer) ...",
    sources=[Source(url="https://github.com/cartertemm/AI-content-describer", title="AI Content Describer on GitHub",
                    quote="Describes the focus object"),
             Source(url="https://menu.example.org/about", title="Menu Reader"),
             Source(url="javascript:alert(1)", title="Bad source")])
EXTRACTED = {
    "summary": "Yes. **Two tools** already do this.\n\n# A heading the AI should not have used",
    "candidates": [
        {"name": "AI Content Describer", "url": "https://github.com/cartertemm/AI-content-describer/",
         "what_it_does": "Describes the screen in NVDA.", "accessibility_notes": "Made for NVDA users.", "cost": "Free"},
        {"name": "Menu Reader", "url": "https://menu.example.org", "what_it_does": "Reads restaurant menus aloud.",
         "accessibility_notes": "The maker says it works with TalkBack.", "cost": "Free with ads"},
        {"name": "Bad Link", "url": "javascript:alert(1)", "what_it_does": "x", "accessibility_notes": "", "cost": ""},
    ],
}
NEED = "read restaurant menus aloud"


def run_search(client, fake_ai, need=NEED):
    fake_ai.on("find.search", RESEARCH).on("find.extract", EXTRACTED)
    return post(client, "/find/ask", {"need": need})


def test_ai_search_journey(user_client, fake_ai):
    r = run_search(user_client, fake_ai)
    assert r.status_code == 200
    assert fake_ai.purposes() == ["find.search", "find.extract"]
    kind, _, kwargs = fake_ai.calls[0]
    assert kind == "research" and NEED in kwargs["prompt"] and "NVDA" in kwargs["prompt"]
    assert "screen reader" in kwargs["system"] and "No emoji" in kwargs["system"]
    assert "Menu Reader: https://menu.example.org/about" in fake_ai.calls[1][2]["prompt"]
    schema = fake_ai.calls[1][2]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]["candidates"]["items"]["required"]) == {
        "name", "url", "what_it_does", "accessibility_notes", "cost"}
    search_id = count("SELECT MAX(id) FROM find_searches")
    assert str(r.url).endswith(f"/find/search/{search_id}")
    page = r.text
    assert f"<h1>What already exists for: {NEED}</h1>" in page
    assert "<strong>Two tools</strong>" in page and "<h1>A heading" not in page
    assert "Nobody on this site has checked them" in page
    assert "In our directory." in page and "Read member reports on AI Content Describer" in page
    assert "Add Menu Reader to the directory" in page
    assert "Bad Link" not in page and "javascript:" not in page
    assert "Web pages the AI used (2)" in page
    assert len(re.findall(r"<h1[ >]", page)) == 1
    # The search is listed on the index for its owner.
    assert f'href="/find/search/{search_id}"' in user_client.get("/find").text


def test_add_candidate_to_directory(user_client, fake_ai):
    run_search(user_client, fake_ai)
    search_id = count("SELECT MAX(id) FROM find_searches")
    r = post(user_client, f"/find/search/{search_id}/add", {"candidate": "1"})
    assert r.status_code == 200 and "<h1>Menu Reader</h1>" in r.text
    assert "Description written by the AI during a web search; not checked." in r.text
    with db.session() as conn:
        row = conn.execute("SELECT * FROM tools WHERE name = 'Menu Reader'").fetchone()
    assert row["source"] == "member" and row["added_by"] == user_id() and row["url"] == "https://menu.example.org"
    page = user_client.get(f"/find/search/{search_id}").text
    assert "Add Menu Reader to the directory" not in page and "Read member reports on Menu Reader" in page
    # Adding again, or adding one that was already there, shows the existing entry.
    r = post(user_client, f"/find/search/{search_id}/add", {"candidate": "1"})
    assert "already in the directory" in r.text
    assert count("SELECT COUNT(*) FROM tools WHERE name = 'Menu Reader'") == 1
    r = post(user_client, f"/find/search/{search_id}/add", {"candidate": "0"})
    assert "<h1>AI Content Describer</h1>" in r.text
    assert post(user_client, f"/find/search/{search_id}/add", {"candidate": "9"}).status_code == 404
    assert post(user_client, f"/find/search/{search_id}/add", {"candidate": "x"}).status_code == 404


def test_search_results_belong_to_their_owner(client, fake_ai):
    register(client, "owner@example.com", "Owner")
    run_search(client, fake_ai)
    search_id = count("SELECT MAX(id) FROM find_searches")
    switch_user(client, "other@example.com", "Other")
    assert client.get(f"/find/search/{search_id}").status_code == 404
    assert post(client, f"/find/search/{search_id}/add", {"candidate": "1"}).status_code == 404
    assert f'href="/find/search/{search_id}"' not in client.get("/find").text
    assert count("SELECT COUNT(*) FROM tools WHERE name = 'Menu Reader'") == 0
    post(client, "/logout")
    r = client.get(f"/find/search/{search_id}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_ask_page_prefills_need(client):
    r = client.get("/find/ask", params={"need": "a talking kitchen scale"})
    assert r.status_code == 200 and "<h1>Ask the AI what already exists</h1>" in r.text
    assert "Sign in to ask the AI" in r.text
    register(client)
    r = client.get("/find/ask", params={"need": "a talking kitchen scale"})
    assert ">a talking kitchen scale</textarea>" in r.text


def test_ask_validation(user_client, fake_ai):
    r = post(user_client, "/find/ask", {"need": "hi"})
    assert r.status_code == 400 and "Ask the AI what already exists: please fix the problems" in r.text
    r = post(user_client, "/find/ask", {"need": "x" * 600})
    assert r.status_code == 400 and "Keep it under" in r.text
    r = post(user_client, "/find/ask", {"need": "pay with card 4111 1111 1111 1111 please"})
    assert r.status_code == 400 and "card number" in r.text
    assert fake_ai.calls == []


def test_ai_failure_is_explained_on_the_job_page(user_client, fake_ai):
    fake_ai.on("find.search", AIError("The AI is busy right now (rate limit). Please try again in a minute."))
    r = post(user_client, "/find/ask", {"need": NEED})
    assert "This did not finish" in r.text and "rate limit" in r.text
    assert count("SELECT COUNT(*) FROM find_searches") == 0


def test_empty_search_result_is_honest(user_client, fake_ai):
    fake_ai.on("find.search", ResearchResult(text="Nothing found.", sources=[]))
    fake_ai.on("find.extract", {"summary": "I found nothing.", "candidates": []})
    r = post(user_client, "/find/ask", {"need": NEED})
    assert "Tools the AI found (0)" in r.text and "That does not prove nothing exists" in r.text


def test_no_ai_explains_and_keeps_the_need(no_ai_app):
    client = TestClient(no_ai_app)
    register(client)
    r = post(client, "/find/ask", {"need": NEED})
    assert r.status_code == 200
    assert "/find/ask" in str(r.url)
    assert "connect your own Claude API key" in r.text
    assert f">{NEED}</textarea>" in r.text
    assert count("SELECT COUNT(*) FROM find_searches") == 0
    assert count("SELECT COUNT(*) FROM jobs") == 0
    # The directory itself does not need the AI.
    assert client.get("/find", params={"q": "NVDA"}).status_code == 200
