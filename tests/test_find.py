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
    pages = (len(SEED) + find.PAGE_SIZE - 1) // find.PAGE_SIZE
    assert f"<h1>Find before you build: page 1 of {pages}</h1>" in r.text
    assert f"<title>Find before you build: page 1 of {pages} – " in r.text
    assert f"All tools in the directory ({len(SEED)})" in r.text
    assert f"Next page (2 of {pages})" in r.text and "Page 1 of" in r.text
    assert r.text.count('<h3><a href="/find/tool/') == find.PAGE_SIZE
    last = client.get("/find?page=99")
    assert f"<h1>Find before you build: page {pages} of {pages}</h1>" in last.text
    assert f"Previous page ({pages - 1} of {pages})" in last.text and "Next page (" not in last.text
    assert "No member has reported on this yet" in r.text


def test_search_matches_name_and_description(client):
    r = client.get("/find", params={"q": "Balatro"})
    assert "<h1>1 tool matches &#34;Balatro&#34;</h1>" in r.text and "Blindfold for Balatro" in r.text
    r = client.get("/find", params={"q": "RODECaster"})  # only in the description
    assert "AccessCaster" in r.text
    r = client.get("/find", params={"q": "nvda add-on"})
    assert "AI Content Describer" in r.text and "Glide" not in r.text


def test_search_treats_wildcards_as_text(client):
    r = client.get("/find", params={"q": "%"})
    assert "<h1>0 tools match &#34;%&#34;</h1>" in r.text
    assert "No tools in the directory match" in r.text


def test_category_filter(client):
    r = client.get("/find", params={"category": "Games"})
    games = [i for i in SEED if i["category"] == "Games"]
    assert f"<h1>{len(games)} tools match in the category Games</h1>" in r.text
    assert "Blindfold for Balatro" in r.text and "QuickMail" not in r.text
    r = client.get("/find", params={"category": "Games", "q": "Godot"})
    assert "UnseenGodot" in r.text and "Blindfold" not in r.text
    r = client.get("/find", params={"category": "Not a category"})
    assert f"All tools in the directory ({len(SEED)})" in r.text


def test_tool_page_shows_facts_and_link(client):
    tid = tool_id("QuickMail")
    r = client.get(f"/find/tool/{tid}")
    assert r.status_code == 200
    assert "<h1>QuickMail: member reports</h1>" in r.text
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
    assert r.status_code == 200 and "<h1>Your report is saved: Seeing AI</h1>" in r.text
    expected = "3 members: 2 worked, 1 partly worked. Tools used: NVDA, TalkBack"
    assert expected in r.text
    assert expected in client.get("/find", params={"q": "Seeing AI"}).text
    assert "Blind or low vision since: later in life" in r.text and "Language: Arabic" in r.text
    assert "lost sight" not in r.text.lower() and "Sight:" not in r.text
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
    assert "<h1>Please check 1 answer: Glide: member reports</h1>" in r.text and 'href="#field-outcome"' in r.text
    assert "no outcome" in r.text  # the note is kept
    r = post(user_client, f"/find/tool/{tid}/report", {"outcome": "worked", "sight_onset": "sometimes"})
    assert r.status_code == 400 and 'href="#field-sight_onset"' in r.text
    assert "Choose one of the listed answers for Blind or low vision since, or choose Prefer not to say." in r.text
    r = post(user_client, f"/find/tool/{tid}/report", {"outcome": "worked", "note": "My card is 4111 1111 1111 1111"})
    assert r.status_code == 400 and find.PUBLIC_SECRET_MESSAGE.replace('"', "&#34;") in r.text
    assert "Everyone can read this page." in r.text and "My card is 4111" in r.text  # kept, so it can be edited
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
    assert "<h1>Added to the directory: Talking Scale</h1>" in r.text
    assert "Talking Scale is now in the directory. Thank you." in r.text
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
    assert "<h1>Already in the directory: Talking Scale</h1>" in r.text and "it was not added again" in r.text
    assert count("SELECT COUNT(*) FROM tools WHERE name = 'Copy'") == 0
    # A researched tool's address also counts.
    r = post(user_client, "/find/add", {"name": "QM", "url": "https://github.com/kellylford/QuickMail",
                                        "description": "x", "category": "Other"})
    assert "<h1>Already in the directory: QuickMail</h1>" in r.text


def test_add_tool_validation(user_client):
    r = post(user_client, "/find/add", {"name": "", "url": "javascript:alert(1)", "description": "", "category": "Nope"})
    assert r.status_code == 400
    assert "<h1>Please check 4 answers: Add a tool to the directory</h1>" in r.text
    for name in ("name", "url", "description", "category"):
        assert f'href="#field-{name}"' in r.text
    r = post(user_client, "/find/add", {"name": "X", "url": "ftp://example.org", "description": "d", "category": "Other"})
    assert r.status_code == 400 and "for example example.com/tool or https://example.com/tool" in r.text
    r = post(user_client, "/find/add", {"name": "X", "url": "https://example.org", "description": "my password is hunter2",
                                        "category": "Other"})
    assert r.status_code == 400 and "password, PIN, code, card number or API key" in r.text
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
    assert find.with_scheme("example.org/tool") == "https://example.org/tool"
    assert find.with_scheme("www.Example.org") == "https://www.Example.org"
    assert find.with_scheme("https://example.org") == "https://example.org"
    for unchanged in ("not a web address", "javascript:alert(1)", "ftp://example.org", "localhost", "a@b.org"):
        assert find.with_scheme(unchanged) == unchanged
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
    assert "braille display" in kwargs["system"] and "who tested it and how" in kwargs["system"]
    # The shared member note is added by the AI client; the section prompt does not repeat it.
    for repeated in ("No emoji", "uses a screen reader", "Put the summary first", "blind people"):
        assert repeated not in kwargs["system"] and repeated not in fake_ai.calls[1][2]["system"]
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
    assert "Web pages the AI used (2)" in page and ">Source: Menu Reader</a>" in page
    assert len(re.findall(r"<h1[ >]", page)) == 1
    # The search is listed on the index for its owner.
    assert f'href="/find/search/{search_id}"' in user_client.get("/find").text


def test_add_candidate_opens_a_filled_in_form_first(user_client, fake_ai):
    run_search(user_client, fake_ai)
    search_id = count("SELECT MAX(id) FROM find_searches")
    page = user_client.get(f"/find/search/{search_id}").text
    assert f'href="/find/add?search={search_id}&amp;candidate=1">Add Menu Reader to the directory</a>' in page
    # The old button address opens the same form; nothing is added yet.
    r = post(user_client, f"/find/search/{search_id}/add", {"candidate": "1"})
    assert r.status_code == 200 and "<h1>Add Menu Reader to the directory</h1>" in r.text
    assert 'value="https://menu.example.org"' in r.text and ">Reads restaurant menus aloud.</textarea>" in r.text
    assert count("SELECT COUNT(*) FROM tools WHERE name = 'Menu Reader'") == 0
    # Kept as the AI wrote it, the description says so.
    r = post(user_client, "/find/add", {"name": "Menu Reader", "url": "https://menu.example.org",
                                        "description": "Reads restaurant menus aloud.", "category": "Everyday tasks",
                                        "search": str(search_id), "candidate": "1"})
    assert r.status_code == 200 and "<h1>Added to the directory: Menu Reader</h1>" in r.text
    assert "Description written by the AI during a web search; not checked." in r.text
    with db.session() as conn:
        row = conn.execute("SELECT * FROM tools WHERE name = 'Menu Reader'").fetchone()
    assert row["source"] == "member" and row["added_by"] == user_id() and row["url"] == "https://menu.example.org"
    assert row["category"] == "Everyday tasks"
    page = user_client.get(f"/find/search/{search_id}").text
    assert "Add Menu Reader to the directory" not in page and "Read member reports on Menu Reader" in page
    # Adding one that is already there shows the existing entry.
    r = post(user_client, f"/find/search/{search_id}/add", {"candidate": "1"})
    assert "<h1>Already in the directory: Menu Reader</h1>" in r.text
    assert count("SELECT COUNT(*) FROM tools WHERE name = 'Menu Reader'") == 1
    r = post(user_client, f"/find/search/{search_id}/add", {"candidate": "0"})
    assert "<h1>Already in the directory: AI Content Describer</h1>" in r.text
    assert post(user_client, f"/find/search/{search_id}/add", {"candidate": "9"}).status_code == 404
    assert post(user_client, f"/find/search/{search_id}/add", {"candidate": "x"}).status_code == 404
    assert user_client.get(f"/find/add?search={search_id}&candidate=9").status_code == 404


def test_a_description_the_member_rewrote_is_theirs(user_client, fake_ai):
    run_search(user_client, fake_ai)
    search_id = count("SELECT MAX(id) FROM find_searches")
    post(user_client, "/find/add", {"name": "Menu Reader", "url": "https://menu.example.org",
                                    "description": "Reads menus aloud with TalkBack.", "category": "Other",
                                    "search": str(search_id), "candidate": "1"})
    with db.session() as conn:
        assert conn.execute("SELECT description FROM tools WHERE name = 'Menu Reader'").fetchone()[0] == \
            "Reads menus aloud with TalkBack."


def test_search_results_belong_to_their_owner(client, fake_ai):
    register(client, "owner@example.com", "Owner")
    run_search(client, fake_ai)
    search_id = count("SELECT MAX(id) FROM find_searches")
    switch_user(client, "other@example.com", "Other")
    assert client.get(f"/find/search/{search_id}").status_code == 404
    assert post(client, f"/find/search/{search_id}/add", {"candidate": "1"}).status_code == 404
    assert client.get(f"/find/add?search={search_id}&candidate=1").status_code == 404
    assert post(client, "/find/add", {"name": "Menu Reader", "url": "https://menu.example.org", "description": "d",
                                      "category": "Other", "search": str(search_id), "candidate": "1"}).status_code == 404
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
    assert r.status_code == 400 and "<h1>Please check 1 answer: Ask the AI what already exists</h1>" in r.text
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


# ----- audit regressions -----------------------------------------------------------------------

H1_RE = re.compile(r"<h1>(.*?)</h1>", re.S)
TITLE_RE = re.compile(r"<title>(.*?) – ", re.S)


def assert_h1_is_title(html: str) -> str:
    h1 = H1_RE.search(html).group(1)
    assert h1 == TITLE_RE.search(html).group(1), "the h1 must say the same as the page title"
    return h1


def test_every_error_page_h1_counts_the_answers_to_check(user_client, fake_ai):
    tid = tool_id("Glide")
    for url, data in [("/find/add", {"name": ""}), ("/find/ask", {"need": "hi"}),
                      (f"/find/tool/{tid}/report", {"note": "x"})]:
        r = post(user_client, url, data)
        assert r.status_code == 400
        assert re.match(r"Please check \d+ answers?: ", assert_h1_is_title(r.text)), url
        assert "please fix" not in r.text.lower() and "Error: " not in r.text, url


def test_search_page_h1_says_what_was_found(client):
    r = client.get("/find", params={"q": "  Balatro  ", "category": "Games"})
    assert assert_h1_is_title(r.text) == "1 tool matches &#34;Balatro&#34; in the category Games"
    r = client.get("/find", params={"q": "no such thing at all"})
    assert assert_h1_is_title(r.text) == "0 tools match &#34;no such thing at all&#34;"
    assert "No tools in the directory match" in r.text


def test_search_ignores_letter_case_in_any_alphabet(user_client):
    post(user_client, "/find/add", {"name": "Éclair Ölesen Reader", "url": "https://eclair.example.org",
                                    "description": "Reads ÅNGSTRÖM labels aloud.", "category": "Other"})
    for q in ("éclair", "ÉCLAIR", "ölesen", "ångström"):
        assert "Éclair Ölesen Reader" in user_client.get("/find", params={"q": q}).text, q


def test_long_fields_are_refused_not_cut(user_client):
    tid = tool_id("Glide")
    long_reader = "N" * 150
    r = post(user_client, f"/find/tool/{tid}/report", {"outcome": "worked", "screen_reader": long_reader})
    assert r.status_code == 400 and "Keep it under 100 characters." in r.text
    assert f'value="{long_reader}"' in r.text  # kept whole so nothing is lost
    r = post(user_client, "/find/add", {"name": "N" * 130, "url": "https://example.org/" + "a" * 600,
                                        "description": "d", "category": "Other", "maintainer": "M" * 130})
    assert r.status_code == 400
    assert "Keep the name under 120 characters." in r.text
    assert "This web address is too long" in r.text
    assert 'href="#field-maintainer"' in r.text
    long_need = "a talking scale " * 40
    r = post(user_client, "/find/ask", {"need": long_need})
    assert r.status_code == 400 and long_need.strip() in r.text
    assert count("SELECT COUNT(*) FROM tool_reports") == 0
    assert count("SELECT COUNT(*) FROM tools WHERE source = 'member'") == 0


def test_a_report_sent_twice_is_saved_once(user_client):
    tid = tool_id("Glide")
    data = {"outcome": "partly", "screen_reader": "TalkBack", "note": "Slow to start."}
    post(user_client, f"/find/tool/{tid}/report", data)
    r = post(user_client, f"/find/tool/{tid}/report", data)
    assert "<h1>Your report was already saved: Glide</h1>" in r.text and "It is listed once" in r.text
    assert count("SELECT COUNT(*) FROM tool_reports") == 1
    post(user_client, f"/find/tool/{tid}/report", {**data, "outcome": "worked"})
    assert count("SELECT COUNT(*) FROM tool_reports") == 2  # a changed report is a new report


def test_secret_detection_ignores_dates_and_pins_on_hardware():
    assert find.looks_secret("4111 1111 1111 1111")
    assert find.looks_secret("my card is 5500-0000-0000-0004 thanks")
    assert find.looks_secret("PIN 1234")
    assert not find.looks_secret("It broke between 2026-10-12 2026-10-13 and 2026-10-14")
    assert not find.looks_secret("Steps: 1 2 3 4 5 6 7 8 9 10 11 12 13")
    assert not find.looks_secret("The pin is bent on my braille display")
    assert not find.looks_secret("Call me on +44 20 7946 0958")


def test_ask_sent_twice_searches_once(user_client, fake_ai):
    run_search(user_client, fake_ai)
    r = post(user_client, "/find/ask", {"need": NEED})
    assert "It was not searched again" in r.text
    assert fake_ai.purposes() == ["find.search", "find.extract"]
    assert count("SELECT COUNT(*) FROM find_searches") == 1
    assert f"<h1>What already exists for: {NEED}</h1>" in r.text


def test_ask_while_the_same_search_runs_goes_to_that_job(user_client, fake_ai):
    uid = user_id()
    with db.session() as conn:
        conn.execute("INSERT INTO jobs (id, user_id, kind, title, status, created_at) VALUES ('j1', ?, 'find.search', ?,"
                     " 'running', ?)", (uid, "Searching the web for: " + NEED, db.now()))
    r = post(user_client, "/find/ask", {"need": NEED}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/jobs/j1"
    assert fake_ai.calls == []


def test_job_title_names_the_need(user_client, fake_ai):
    run_search(user_client, fake_ai)
    with db.session() as conn:
        assert conn.execute("SELECT title FROM jobs").fetchone()["title"] == "Searching the web for: " + NEED


def test_malformed_ai_answer_does_not_break_the_search(user_client, fake_ai):
    fake_ai.on("find.search", ResearchResult(text="notes", sources=[Source(url="https://ok.example.org", title=None)]))
    fake_ai.on("find.extract", {"summary": None, "candidates": [
        "not an object", {"name": 42, "url": "https://x.example.org"},
        {"name": "Good Tool", "url": "https://good.example.org", "what_it_does": None, "cost": 5}]})
    r = post(user_client, "/find/ask", {"need": NEED})
    assert r.status_code == 200 and "This did not finish" not in r.text
    assert "The AI did not write a summary this time." in r.text and ">None<" not in r.text
    assert "Tools the AI found (1)" in r.text and "Good Tool" in r.text
    assert "What it does, according to the AI: Not found." in r.text
    assert "https://ok.example.org" in r.text
    fake_ai.on("find.extract", ["not", "a", "dict"])
    r = post(user_client, "/find/ask", {"need": "a braille music reader"})
    assert "Tools the AI found (0)" in r.text


def test_add_candidate_rejects_negative_numbers(user_client, fake_ai):
    run_search(user_client, fake_ai)
    search_id = count("SELECT MAX(id) FROM find_searches")
    assert post(user_client, f"/find/search/{search_id}/add", {"candidate": "-1"}).status_code == 404
    assert count("SELECT COUNT(*) FROM tools WHERE source = 'member'") == 0


def test_a_shared_name_on_another_website_is_not_our_tool(user_client, fake_ai):
    fake_ai.on("find.search", ResearchResult(text="notes", sources=[]))
    fake_ai.on("find.extract", {"summary": "Two.", "candidates": [
        {"name": "Glide", "url": "https://www.glideapps.com", "what_it_does": "Builds apps.",
         "accessibility_notes": "", "cost": ""},
        {"name": "QuickMail", "url": "https://github.com/kellylford/QuickMail/releases", "what_it_does": "Email.",
         "accessibility_notes": "", "cost": ""}]})
    r = post(user_client, "/find/ask", {"need": "build an app without code"})
    assert "Add Glide to the directory" in r.text and "Read member reports on Glide" not in r.text
    assert "Read member reports on QuickMail" in r.text


def test_signing_in_after_a_session_ends_does_not_land_on_an_error(user_client, fake_ai):
    tid = tool_id("Glide")
    r = user_client.get(f"/find/tool/{tid}/report", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/find/tool/{tid}#report-form-title"
    run_search(user_client, fake_ai)
    search_id = count("SELECT MAX(id) FROM find_searches")
    r = user_client.get(f"/find/search/{search_id}/add", follow_redirects=False)
    assert r.headers["location"] == f"/find/search/{search_id}"
    assert user_client.get("/find/tool/999999/report").status_code == 404


def test_report_choices_can_take_focus_from_the_error_summary(user_client):
    r = post(user_client, f"/find/tool/{tool_id('Glide')}/report", {})
    assert 'href="#field-outcome"' in r.text
    assert re.search(r'<input type="radio" id="field-outcome" name="outcome" value="worked"[^>]*aria-invalid="true"', r.text)
    assert 'aria-describedby="error-outcome"' in r.text


def test_deleting_an_account_takes_the_member_off_volunteer_lists(user_client):
    tid = tool_id("QuickMail")
    post(user_client, f"/library/tool/{tid}/volunteer", {"skills": "I test with JAWS"})
    find.forget_user(user_id())
    assert count("SELECT COUNT(*) FROM upkeep_volunteers") == 0


def test_seed_corrections_reach_an_existing_directory(client):
    with db.session() as conn:
        conn.execute("UPDATE tools SET description = 'old claim', maintainer = 'Double Tap' WHERE name = 'Claude Code Reader'")
    find.seed()
    with db.session() as conn:
        row = conn.execute("SELECT description, maintainer FROM tools WHERE name = 'Claude Code Reader'").fetchone()
    assert row["maintainer"] == "doubletaponair" and "VoiceOver, NVDA and JAWS" in row["description"]


def test_unreadable_web_addresses_get_an_error_not_a_crash(user_client):
    for url in ("http://[broken/x", "https://example.org:99999/", "https://:80/"):
        r = post(user_client, "/find/add", {"name": "X", "url": url, "description": "d", "category": "Other"})
        assert r.status_code == 400 and "for example example.com/tool" in r.text, url
        r = post(user_client, "/library/add", {"name": "X", "url": url, "description": "d", "category": "Other",
                                               "made_by_blind": "yes"})
        assert r.status_code == 400, url
    assert not find.valid_url("http://[broken/x") and find.norm_url("http://[broken/x")


def test_report_counts_explain_themselves(user_client):
    tid = tool_id("Glide")
    assert "only their latest report counts" not in user_client.get(f"/find/tool/{tid}").text
    post(user_client, f"/find/tool/{tid}/report", {"outcome": "partly"})
    r = post(user_client, f"/find/tool/{tid}/report", {"outcome": "worked"})
    assert "1 member: 1 worked." in r.text and "What members say (2 reports)" in r.text
    assert "only their latest report counts here" in r.text
    post(user_client, f"/find/tool/{tool_id('Seeing AI')}/report", {"outcome": "partly"})
    assert find.report_summaries([tool_id("Seeing AI")])[tool_id("Seeing AI")] == "1 member: 1 partly worked"


def test_seed_facts_follow_the_research_corrections():
    by_name = {i["name"]: i for i in SEED}
    assert "free" not in by_name["Seeing AI"]["description"].lower()  # the research does not say it is free
    assert "not yet checked in claude.ai or ChatGPT" in by_name["maidr-mcp"]["description"]
    assert "June 18, 2026" in by_name["Gemini CLI screen reader setting"]["description"]
    assert "could not confirm" in by_name["PiccyBot"]["description"]
    assert "prototype" in by_name["Envision"]["description"]
    assert "building had just started" in by_name["acter"]["description"]


# ----- ease of use: titles that say what happened, fixing your own entries ---------------------

def test_after_a_report_the_title_says_it_is_saved_and_the_page_opens_at_the_top(user_client):
    tid = tool_id("Glide")
    r = post(user_client, f"/find/tool/{tid}/report", {"outcome": "worked"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/find/tool/{tid}"  # no #fragment
    page = user_client.get(r.headers["location"]).text
    assert assert_h1_is_title(page) == "Your report is saved: Glide"
    assert "Thank you for letting other members know." in page
    # The outcome is said once; the page then has its usual name again.
    assert assert_h1_is_title(user_client.get(f"/find/tool/{tid}").text) == "Glide: member reports"


def test_find_and_library_pages_of_one_tool_have_different_names(client):
    tid = tool_id("QuickMail")
    find_h1 = assert_h1_is_title(client.get(f"/find/tool/{tid}").text)
    library_h1 = assert_h1_is_title(client.get(f"/library/tool/{tid}").text)
    assert find_h1 == "QuickMail: member reports" and library_h1 == "QuickMail in the Tool library"


def test_a_web_address_without_https_is_completed(user_client):
    r = post(user_client, "/find/add", {"name": "Talking Scale", "url": "scale.example.org/app",
                                        "description": "A kitchen scale app that speaks.", "category": "Other"})
    assert r.status_code == 200 and "<h1>Added to the directory: Talking Scale</h1>" in r.text
    assert "Talking Scale is now in the directory, at https://scale.example.org/app. Thank you." in r.text
    with db.session() as conn:
        assert conn.execute("SELECT url FROM tools WHERE name = 'Talking Scale'").fetchone()[0] == \
            "https://scale.example.org/app"
    # With another answer to check, the address is shown exactly as typed.
    r = post(user_client, "/find/add", {"name": "", "url": "other.example.org", "description": "d", "category": "Other"})
    assert r.status_code == 400 and 'value="other.example.org"' in r.text


def test_the_member_who_added_a_tool_can_change_it(user_client):
    post(user_client, "/find/add", {"name": "Talking Scale", "url": "https://scale.example.org/app",
                                    "description": "A kitchen scale app.", "category": "Other"})
    tid = tool_id("Talking Scale")
    page = user_client.get(f"/find/tool/{tid}").text
    assert f'href="/find/tool/{tid}/change?back=find">Change the details of Talking Scale</a>' in page
    r = user_client.get(f"/find/tool/{tid}/change")
    assert assert_h1_is_title(r.text) == "Change the details of Talking Scale"
    assert 'value="https://scale.example.org/app"' in r.text and "Keep it as it is and go back" in r.text
    r = post(user_client, f"/find/tool/{tid}/change", {"name": "Talking Scale", "url": "", "description": "",
                                                       "category": "Other", "back": "find"})
    assert r.status_code == 400
    assert assert_h1_is_title(r.text) == "Please check 2 answers: Change the details of Talking Scale"
    r = post(user_client, f"/find/tool/{tid}/change", {"name": "Talking Kitchen Scale", "url": "scale.example.org/v2",
                                                       "description": "A kitchen scale app that speaks.",
                                                       "category": "Everyday tasks", "back": "find"})
    assert assert_h1_is_title(r.text) == "Your changes are saved: Talking Kitchen Scale"
    assert "The web address is saved as https://scale.example.org/v2." in r.text
    with db.session() as conn:
        row = conn.execute("SELECT * FROM tools WHERE id = ?", (tid,)).fetchone()
    assert (row["name"], row["url"], row["category"]) == ("Talking Kitchen Scale", "https://scale.example.org/v2",
                                                          "Everyday tasks")
    # Another entry's address is refused.
    r = post(user_client, f"/find/tool/{tid}/change", {"name": "X", "url": "https://github.com/kellylford/QuickMail",
                                                       "description": "d", "category": "Other"})
    assert r.status_code == 400 and "QuickMail already has this web address" in r.text


def test_the_member_who_added_a_tool_can_remove_it_after_a_confirmation(user_client):
    post(user_client, "/find/add", {"name": "Talking Scale", "url": "https://scale.example.org/app",
                                    "description": "A kitchen scale app.", "category": "Other"})
    tid = tool_id("Talking Scale")
    post(user_client, f"/find/tool/{tid}/report", {"outcome": "worked"})  # their own report does not block it
    r = user_client.get(f"/find/tool/{tid}/remove")
    assert assert_h1_is_title(r.text) == "Remove Talking Scale from the directory"
    assert "with what you wrote about it there. This cannot be undone." in r.text
    assert "Keep it and go back" in r.text and count("SELECT COUNT(*) FROM tools WHERE id = ?", (tid,)) == 1
    r = post(user_client, f"/find/tool/{tid}/remove")
    assert str(r.url).endswith("/find")
    assert assert_h1_is_title(r.text) == "Removed from the directory: Talking Scale"
    assert count("SELECT COUNT(*) FROM tools WHERE id = ?", (tid,)) == 0
    assert count("SELECT COUNT(*) FROM tool_reports WHERE tool_id = ?", (tid,)) == 0


def test_only_the_adder_can_change_and_only_until_others_join(client):
    register(client, "adder@example.com", "Adder")
    post(client, "/find/add", {"name": "Talking Scale", "url": "https://scale.example.org/app",
                               "description": "A kitchen scale app.", "category": "Other"})
    tid = tool_id("Talking Scale")
    switch_user(client, "other@example.com", "Other")
    assert "Change the details of" not in client.get(f"/find/tool/{tid}").text
    assert client.get(f"/find/tool/{tid}/change").status_code == 404
    assert post(client, f"/find/tool/{tid}/remove").status_code == 404
    assert client.get(f"/find/tool/{tool_id('QuickMail')}/change").status_code == 404  # research entries
    post(client, f"/find/tool/{tid}/report", {"outcome": "partly"})
    post(client, "/logout")
    post(client, "/login", {"email": "adder@example.com", "password": "correct horse battery"})
    assert "Change the details of" not in client.get(f"/find/tool/{tid}").text
    r = post(client, f"/find/tool/{tid}/remove")
    assert "can no longer be changed or removed here" in r.text
    assert count("SELECT COUNT(*) FROM tools WHERE id = ?", (tid,)) == 1


def test_members_can_delete_their_own_report(client):
    tid = tool_id("Glide")
    register(client, "a@example.com", "A")
    post(client, f"/find/tool/{tid}/report", {"outcome": "did_not_work"})
    rid = count("SELECT MAX(id) FROM tool_reports")
    page = client.get(f"/find/tool/{tid}").text
    assert f'href="/find/tool/{tid}/reports/{rid}/delete?back=find">Delete your report: It did not work for me, ' in page
    r = client.get(f"/find/tool/{tid}/reports/{rid}/delete")
    assert assert_h1_is_title(r.text) == "Delete your report on Glide"
    assert "This cannot be undone." in r.text and "Keep it and go back" in r.text
    switch_user(client, "b@example.com", "B")
    assert "Delete your report" not in client.get(f"/find/tool/{tid}").text
    assert client.get(f"/find/tool/{tid}/reports/{rid}/delete").status_code == 404
    assert post(client, f"/find/tool/{tid}/reports/{rid}/delete").status_code == 404
    post(client, "/logout")
    post(client, "/login", {"email": "a@example.com", "password": "correct horse battery"})
    r = post(client, f"/find/tool/{tid}/reports/{rid}/delete", {"back": "find"})
    assert assert_h1_is_title(r.text) == "Your report is deleted: Glide"
    assert count("SELECT COUNT(*) FROM tool_reports") == 0


def test_directory_search_is_a_named_search_with_numbered_pages(client):
    register(client)
    r = client.get("/find")
    pages = (len(SEED) + find.PAGE_SIZE - 1) // find.PAGE_SIZE
    assert '<form method="get" action="/find" role="search" aria-labelledby="search-title">' in r.text
    assert re.search(r'<label for="field-q">What are you looking for\?</label>', r.text)  # not "(optional)"
    assert ">Search the directory</button>" in r.text and ">Search</button>" not in r.text
    assert r.text.count('aria-label="Pages of tools') == 2  # before and after the list
    assert '<li><a class="button button-secondary" href="/find" aria-current="page">Page 1</a></li>' in r.text
    assert f'href="/find?page={pages}">Page {pages}</a></li>' in r.text
    # The page links are a real list inside each nav, so screen readers say how many there are.
    for nav in re.findall(r'<nav aria-label="Pages of tools[^"]*">(.*?)</nav>', r.text, re.S):
        assert nav.strip().startswith('<ul class="pager">') and nav.strip().endswith("</ul>")
        assert nav.count("<li>") == pages + 1  # every page, then Next page
    assert "It usually takes one to three minutes." in r.text
    r = client.get("/find", params={"q": "talking scale"})
    assert '<a href="/find/ask?need=talking%20scale">Not in the directory? Ask the AI to search the web</a>' in r.text


def test_tool_forms_do_not_offer_the_members_own_name(user_client):
    for path in ("/find/add", "/library/add"):
        page = user_client.get(path).text
        for name in ("name", "url"):
            assert re.search(rf'<input id="field-{name}" name="{name}"[^>]*autocomplete="off"', page), (path, name)


def test_report_form_welcomes_braille_and_magnifier_users_and_dates_read_as_words(user_client):
    tid = tool_id("Glide")
    page = user_client.get(f"/find/tool/{tid}").text
    assert "Screen reader, braille display or magnifier you used" in page and "ZoomText" in page
    assert "low-cost Android phone" in page and "cheap" not in page
    assert '<label for="field-sight_onset">Blind or low vision since <span class="optional">(optional)</span>' in page
    assert ">Birth or early childhood<" in page and ">Later in life<" in page
    assert ">Save how it worked for me</button>" in page
    post(user_client, f"/find/tool/{tid}/report", {"outcome": "worked"})
    main = user_client.get(f"/find/tool/{tid}").text.split("<main", 1)[1]
    assert not re.search(r"\d{4}-\d{2}-\d{2}", main)
    assert re.search(r"Reported \d{1,2} [A-Z][a-z]+ \d{4}\.", main)


def test_find_pages_use_respectful_words(user_client):
    pages = [user_client.get(p).text for p in ("/find", "/find/add", "/find/ask", f"/find/tool/{tool_id('Glide')}")]
    for page in pages:
        for phrase in ("like you", "lost sight", "blind people made", "please fix", "cheap"):
            assert phrase not in page, phrase
    assert "whether it worked for other members" in pages[0]
    assert "tools blind and low-vision people made for each other" in pages[0]
