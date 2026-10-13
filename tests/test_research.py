"""Research section: prior work, study plan, notes and data, draft paper, exports, ownership and no-AI path."""
import html
import re

import pytest
from fastapi.testclient import TestClient

from studio import db
from studio.a11y_check import check_html
from studio.ai import AIError, ResearchResult, Source
from studio.features import research

from .conftest import post, register

PRIOR_TEXT = """## Summary
Three studies looked at screen readers in online exams [1].

## Key studies
- Smith, 2024, Exam access, Journal of Accessibility [1].

## What is known
Timed exams are harder with screen readers.

## What is not known yet
Whether one question per page helps.
"""
SOURCES = [
    Source(url="https://example.org/a", title="Exam access study", quote="Timed exams were harder."),
    Source(url="https://example.org/b", title="Screen reader survey"),
    Source(url="https://example.org/c", title="One question per page"),
]
PLAN_TEXT = "## Summary\nA within-subjects study [2].\n\n## Hypothesis\nOne question per page is faster.\n"
DATA_TEXT = "## Summary\nScores rise with practice. This is the AI's reading.\n"
DRAFT_TEXT = """# One question per page
## Abstract
We studied exams [1]. Prior work agrees [2, 3]. A made-up claim [7].

## Results
The mean score was [RESULT NEEDED]. Participants: [RESULT NEEDED].
"""

CSV_TEXT = "name,score,group\nAna,3,A\nBo,5,B\nCy,,A\nDee,4,A\n"


def fake_answers(fake_ai):
    fake_ai.on("research.prior_work", ResearchResult(text=PRIOR_TEXT, sources=list(SOURCES)))
    fake_ai.on("research.plan", PLAN_TEXT)
    fake_ai.on("research.data", DATA_TEXT)
    fake_ai.on("research.draft", DRAFT_TEXT)
    return fake_ai


def new_project(client, **overrides):
    data = {"title": "Exams and screen readers", "question": "Do blind students finish exams faster with one question per page?",
            "field": "Education", "known": "Timed exams are hard."}
    data.update(overrides)
    r = post(client, "/research", data, follow_redirects=False)
    assert r.status_code == 303, r.text
    location = r.headers["location"]
    assert re.fullmatch(r"/research/\d+", location), location
    return int(location.rsplit("/", 1)[1])


def assert_accessible(r):
    assert r.status_code == 200, r.text
    errors = check_html(r.text).errors
    assert not errors, errors
    assert len(re.findall(r"<h1[ >]", r.text)) == 1


def h1(r) -> str:
    return html.unescape(re.search(r"<h1>(.*?)</h1>", r.text, re.S).group(1))


def upload(client, pid, content: bytes, filename="data.csv", ctype="text/csv"):
    return post(client, f"/research/{pid}/csv", files={"csv": (filename, content, ctype)})


# ----- index and create --------------------------------------------------------------------------

def test_index_signed_out_explains_and_offers_sign_in(client):
    r = client.get("/research")
    assert_accessible(r)
    assert h1(r) == "Run your own research"
    assert "Sign in" in r.text and 'action="/research"' not in r.text


def test_create_project_and_see_stages(user_client):
    r = user_client.get("/research")
    assert_accessible(r)
    assert "You have no research projects yet" in r.text
    pid = new_project(user_client)
    r = user_client.get(f"/research/{pid}")
    assert_accessible(r)
    # Right after saving, the h1 (and title) says what happened; on any later visit it is the project's name.
    assert h1(r) == "Project saved: Exams and screen readers"
    assert "<title>Project saved: Exams and screen readers, Blind Creators Studio</title>" in r.text
    r = user_client.get(f"/research/{pid}")
    assert h1(r) == "Exams and screen readers"
    assert "<title>Exams and screen readers, Blind Creators Studio</title>" in r.text
    # The question comes first, before the stages.
    assert r.text.index("Do blind students finish exams faster") < r.text.index("1. Prior work")
    for heading in ["1. Prior work", "2. Study plan", "3. Notes and data", "4. Draft paper"]:
        assert f"<h2>{heading}</h2>" in r.text
    assert r.text.count("Not started") == 4
    assert "0 of 4 stages done" in r.text
    listing = user_client.get("/research")
    assert f'href="/research/{pid}"' in listing.text and "0 of 4 stages done" in listing.text


def test_create_validation_errors(user_client):
    r = post(user_client, "/research", {"title": "", "question": "  "})
    assert r.status_code == 400
    assert h1(r) == "Please check 2 answers: Run your own research"
    assert 'href="#field-title"' in r.text and 'href="#field-question"' in r.text
    assert 'aria-invalid="true"' in r.text
    r = post(user_client, "/research", {"title": "x" * 201, "question": "Why?"})
    assert r.status_code == 400 and "200 characters or fewer" in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_projects").fetchone()[0] == 0


def test_create_refuses_card_numbers_and_passwords(user_client):
    r = post(user_client, "/research", {"title": "Mine", "question": "Card 4111 1111 1111 1111 works?"})
    assert r.status_code == 400 and "card number" in r.text
    # The message quotes the words it found, so they can be found in a long text.
    assert 'near: &#34;Card 4111 1111 1111 1111 works?&#34;' in r.text
    r = post(user_client, "/research", {"title": "Mine", "question": "Why?", "known": "my password is hunter22"})
    assert r.status_code == 400 and 'href="#field-known"' in r.text


def test_create_requires_sign_in(client):
    r = post(client, "/research", {"title": "T", "question": "Q"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


# ----- the four stages ---------------------------------------------------------------------------

def test_full_journey(user_client, fake_ai):
    fake_answers(fake_ai)
    pid = new_project(user_client)

    # Stage 1: prior work.
    r = post(user_client, f"/research/{pid}/prior-work")
    assert r.url.path == f"/research/{pid}/prior-work"
    assert_accessible(r)
    assert h1(r) == "Prior work is ready: Exams and screen readers"
    assert "Written by the AI" in r.text and "Open the sources to check" in r.text
    assert 'href="https://example.org/a" rel="noopener noreferrer"' in r.text
    assert "Sources returned by the search (3)" in r.text
    kind, purpose, kwargs = fake_ai.calls[-1]
    assert (kind, purpose) == ("research", "research.prior_work")
    assert "never invent" in kwargs["system"].lower()
    # The shared member note comes from AIClient, so the section's prompt does not repeat it or assume
    # that the researcher studies blindness.
    assert "blind researcher" not in kwargs["system"] and "uses a screen reader" not in kwargs["system"]
    assert "one question per page" in kwargs["prompt"]
    # The AI's ## headings sit under the page's h2.
    assert "<h3>Summary</h3>" in r.text and "<h2>Summary</h2>" not in r.text

    # Stage 2: study plan, given the prior work and numbered sources.
    r = post(user_client, f"/research/{pid}/plan")
    assert r.url.path == f"/research/{pid}/plan"
    assert_accessible(r)
    assert h1(r) == "Study plan is ready: Exams and screen readers"
    _, purpose, kwargs = fake_ai.calls[-1]
    assert purpose == "research.plan"
    assert "Three studies looked at screen readers" in kwargs["prompt"]
    assert "[3] One question per page https://example.org/c" in kwargs["prompt"]
    system = " ".join(kwargs["system"].split())
    assert "Never use blindfolded sighted people as stand-ins for blind participants" in system
    assert "only when the question involves blind or low-vision people" in system
    assert "read in order in speech, braille and large print" in system

    # Stage 3: notes, CSV and the AI's interpretation.
    r = post(user_client, f"/research/{pid}/notes", {"notes": "Pilot ran with 4 people."})
    assert r.url.path == f"/research/{pid}/data"
    assert h1(r) == "Your notes are saved: Exams and screen readers"
    r = upload(user_client, pid, CSV_TEXT.encode())
    assert_accessible(r)
    assert h1(r) == "Facts computed from data.csv: Exams and screen readers"
    assert "Computed by the site" in r.text
    r = post(user_client, f"/research/{pid}/data/describe")
    assert r.url.path == f"/research/{pid}/data"
    assert_accessible(r)
    assert h1(r) == "The AI's interpretation is ready: Exams and screen readers"
    assert "<h2>The AI's interpretation</h2>" in r.text and "Scores rise with practice" in r.text
    assert "It is an interpretation, not a result" in r.text
    _, purpose, kwargs = fake_ai.calls[-1]
    assert purpose == "research.data"
    assert ("score (numbers): 3 values, 1 missing (empty). Mean 4, median 4, smallest 3, largest 5, "
            "standard deviation 1." in kwargs["prompt"])
    assert "Pilot ran with 4 people." in kwargs["prompt"]
    assert "Ana,3,A" in kwargs["prompt"]

    # Stage 4: draft.
    r = post(user_client, f"/research/{pid}/draft")
    assert r.url.path == f"/research/{pid}/draft"
    assert_accessible(r)
    assert h1(r) == "Draft paper is ready: Exams and screen readers"
    assert "The AI wrote this draft" in r.text
    _, purpose, kwargs = fake_ai.calls[-1]
    assert purpose == "research.draft"
    assert "[RESULT NEEDED]" in kwargs["system"]
    system = " ".join(kwargs["system"].split())
    assert "Never mention the person's blindness or low vision" in system
    assert "Do not mention the researcher's own sight in the paper unless they ask." in system
    assert "A within-subjects study" in kwargs["prompt"] and "Pilot ran with 4 people." in kwargs["prompt"]
    assert "score (numbers)" in kwargs["prompt"]
    assert 'href="/research/%d/draft.md"' % pid in r.text
    assert "Copy this draft into a new Write document" in r.text
    assert "<h2>The draft, written by the AI (not checked)</h2>" in r.text

    # Every stage now shows as done with a date.
    r = user_client.get(f"/research/{pid}")
    assert "Not started" not in r.text and r.text.count("Done on ") == 4
    assert "4 of 4 stages done" in r.text
    assert f'href="/research/{pid}/draft"' in r.text and "Read the draft paper" in r.text


def test_draft_flags_citations_outside_the_source_list(user_client, fake_ai):
    fake_answers(fake_ai)
    pid = new_project(user_client)
    post(user_client, f"/research/{pid}/prior-work")
    r = post(user_client, f"/research/{pid}/draft")
    assert "<h2>Citations the site could not match</h2>" in r.text
    section = r.text.split("<h2>Citations the site could not match</h2>")[1].split("<h2>")[0]
    # The AI made the citation up, and the list quotes the sentence it is in and its heading.
    assert "The AI cited these numbers, but they are not in the list of 3 sources." in section
    assert '<li>[7] in: "A made-up claim [7]." (Abstract)</li>' in section
    assert "[1]" not in section and "[2]" not in section and "[3]" not in section
    assert "Places marked [RESULT NEEDED]: 2" in r.text
    assert "Citation numbers that match a source: [1], [2], [3]" in r.text
    with db.session() as conn:
        meta = conn.execute("SELECT meta_json FROM research_items WHERE kind = 'draft'").fetchone()[0]
    assert '"unmatched": [7]' in meta


def test_draft_without_unmatched_citations_has_no_warning(user_client, fake_ai):
    fake_answers(fake_ai)
    fake_ai.on("research.draft", "# Title\n## Abstract\nOnly real ones [1] and [3].")
    pid = new_project(user_client)
    post(user_client, f"/research/{pid}/prior-work")
    r = post(user_client, f"/research/{pid}/draft")
    assert "Citations the site could not match</h2>" not in r.text
    assert "Citation numbers the site could not match: 0" in r.text
    assert "Sources the draft does not cite: [2]" in r.text


def test_draft_with_no_sources_flags_every_citation(user_client, fake_ai):
    fake_ai.on("research.draft", "# T\n## Abstract\nSee [1].")
    pid = new_project(user_client)
    r = post(user_client, f"/research/{pid}/draft")
    assert_accessible(r)
    assert '<li>[1] in: "See [1]." (Abstract)</li>' in r.text and "There are no sources yet" in r.text
    _, _, kwargs = fake_ai.calls[-1]
    assert "No prior work search has been done yet." in kwargs["prompt"]


def test_check_citations_unit():
    c = research.check_citations("A [1]. B [2, 3]. C [4-6]. D [7]. Link [2](http://x). [RESULT NEEDED]", 3)
    assert c["matched"] == [1, 2, 3]
    assert c["unmatched"] == [4, 5, 6, 7]
    assert c["uncited"] == []
    assert c["result_needed"] == 1
    assert research.check_citations("No citations here.", 2)["uncited"] == [1, 2]


def test_running_a_stage_again_keeps_history(user_client, fake_ai):
    fake_answers(fake_ai)
    pid = new_project(user_client)
    post(user_client, f"/research/{pid}/plan")
    fake_ai.on("research.plan", "## Summary\nThe second plan.\n")
    r = post(user_client, f"/research/{pid}/plan")
    assert "The second plan." in r.text and "A within-subjects study" not in r.text
    assert "<h2>Earlier versions</h2>" in r.text
    with db.session() as conn:
        rows = conn.execute("SELECT id FROM research_items WHERE project_id = ? AND kind = 'plan' ORDER BY id",
                            (pid,)).fetchall()
    assert len(rows) == 2
    old = user_client.get(f"/research/{pid}/plan?version={rows[0]['id']}")
    assert_accessible(old)
    assert "A within-subjects study" in old.text and "This is an earlier version" in old.text
    assert user_client.get(f"/research/{pid}/plan?version=999999").status_code == 404


def test_stage_pages_before_any_result(user_client):
    pid = new_project(user_client)
    for slug, button in [("prior-work", "Find prior work"), ("plan", "Write a study plan"),
                         ("draft", "Write a draft paper")]:
        r = user_client.get(f"/research/{pid}/{slug}")
        assert_accessible(r)
        assert button in r.text
    r = user_client.get(f"/research/{pid}/data")
    assert_accessible(r)
    assert "No data file yet" in r.text


def test_failed_ai_job_says_why_and_saves_nothing(user_client, fake_ai):
    fake_ai.on("research.plan", AIError("The AI is busy right now (rate limit). Please try again in a minute."))
    pid = new_project(user_client)
    r = post(user_client, f"/research/{pid}/plan")
    assert "This did not finish" in r.text and "rate limit" in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_items").fetchone()[0] == 0


def test_empty_ai_answer_is_a_failure(user_client, fake_ai):
    fake_ai.on("research.prior_work", ResearchResult(text="   ", sources=[]))
    pid = new_project(user_client)
    r = post(user_client, f"/research/{pid}/prior-work")
    assert "This did not finish" in r.text and "came back empty, so nothing was saved" in r.text


def test_unsafe_source_urls_are_dropped(user_client, fake_ai):
    fake_ai.on("research.prior_work", ResearchResult(text="## Summary\nX", sources=[
        Source(url="javascript:alert(1)", title="Bad"), Source(url="https://ok.example/p", title="Good")]))
    pid = new_project(user_client)
    r = post(user_client, f"/research/{pid}/prior-work")
    assert "javascript:" not in r.text and "https://ok.example/p" in r.text


def test_ai_text_with_html_is_escaped(user_client, fake_ai):
    fake_ai.on("research.plan", "## Summary\n<script>alert(1)</script> plan")
    pid = new_project(user_client)
    r = post(user_client, f"/research/{pid}/plan")
    assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;" in r.text


# ----- notes and CSV -----------------------------------------------------------------------------

def test_csv_statistics_are_computed_correctly():
    stats = research.compute_csv_stats(CSV_TEXT)
    assert stats["rows"] == 4 and stats["columns"] == 3
    assert stats["column_names"] == ["name", "score", "group"]
    assert stats["empty_cells"] == 1 and stats["delimiter"] == "comma"
    name, score, group = stats["column_stats"]
    assert score["kind"] == "number"
    assert score["count"] == 3 and score["empty"] == 1
    assert score["mean"] == pytest.approx(4.0) and score["min"] == 3 and score["max"] == 5
    assert name["kind"] == "text" and name["distinct"] == 4
    assert group["kind"] == "text" and group["distinct"] == 2
    assert group["top"] == [{"value": "A", "count": 3}, {"value": "B", "count": 1}]


def test_csv_statistics_edge_cases():
    text = "a;b;\n1.5;x;\n-2;x;z\n2.75;y;z\n\n1e1;x;z;extra\n"
    stats = research.compute_csv_stats(text)
    assert stats["delimiter"] == "semicolon"
    assert stats["rows"] == 4  # the blank line is skipped
    assert stats["columns"] == 4 and stats["column_names"] == ["a", "b", "Column 3", "Column 4"]
    assert stats["uneven_rows"] == 1
    a, b, c3, _ = stats["column_stats"]
    assert a["kind"] == "number" and a["mean"] == pytest.approx((1.5 - 2 + 2.75 + 10) / 4)
    assert a["mean_text"] == "3.06" and a["min_text"] == "-2" and a["max_text"] == "10"
    assert b["top"][0] == {"value": "x", "count": 3}
    assert c3["empty"] == 1 and c3["distinct"] == 1
    # A column with a word among numbers is not numeric.
    mixed = research.compute_csv_stats("v\n1\n2\nmany\n")["column_stats"][0]
    assert mixed["kind"] == "text" and mixed["distinct"] == 3
    with pytest.raises(research.CSVProblem):
        research.compute_csv_stats("only,headers\n")
    with pytest.raises(research.CSVProblem):
        research.compute_csv_stats("\n\n")


def test_csv_page_shows_computed_facts(user_client):
    pid = new_project(user_client)
    r = upload(user_client, pid, ("﻿" + CSV_TEXT).encode("utf-8"))
    assert_accessible(r)
    page = r.text
    assert "From data.csv, uploaded" in page
    assert "Rows of data, not counting the column names: 4" in page
    assert "Columns: 3" in page and "Column names: name, score, group" in page
    assert "Missing cells, empty or written as NA, n/a, null and similar: 1" in page
    # Long column lists are split under headings: numbers first, then text.
    numbers = page.split("<h3>Columns with numbers (1)</h3>")[1].split("<h3>")[0]
    text = page.split("<h3>Columns with text (2)</h3>")[1].split("<h2>")[0]
    assert ("<strong>score</strong>: 3 values, 1 missing (empty). Mean 4, median 4, smallest 3, largest 5, "
            "standard deviation 1." in numbers)
    assert "<strong>group</strong>: 4 filled, none missing, 2 distinct values. Most common: A (3); B (1)." in text
    # A column where every value is different says so, instead of a "most common" list of ones.
    assert "<strong>name</strong>: 4 filled, none missing, all 4 values are different." in text
    assert "<strong>name</strong>" in text and "<strong>name</strong>" not in numbers
    assert "They are not written by the AI" in page
    r = user_client.get(f"/research/{pid}")
    assert "3. Notes and data</h2>\n<p><strong>Done on " in r.text


def test_csv_too_large_is_rejected(user_client):
    pid = new_project(user_client)
    big = ("a,b\n" + "1,2\n" * 60_000).encode()
    assert len(big) > 200 * 1024
    r = upload(user_client, pid, big)
    assert r.status_code == 400
    assert "larger than 200 KB" in r.text and 'href="#field-csv"' in r.text
    assert h1(r) == "Please check 1 answer: Notes and data: Exams and screen readers"
    assert "Nothing was changed." in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_items").fetchone()[0] == 0


def test_huge_upload_is_refused_before_reading(user_client):
    pid = new_project(user_client)
    r = upload(user_client, pid, b"a,b\n" + b"1,2\n" * 400_000)
    assert r.status_code == 400 and "larger than 200 KB" in r.text


def test_non_csv_files_are_rejected(user_client):
    pid = new_project(user_client)
    r = upload(user_client, pid, b"\x89PNG\r\n\x1a\n", filename="photo.png", ctype="image/png")
    assert r.status_code == 400 and "This is not a CSV file" in r.text
    r = upload(user_client, pid, b"PK\x03\x04\x00\x00binary", filename="sheet.csv", ctype="text/csv")
    assert r.status_code == 400 and "binary file" in r.text
    r = upload(user_client, pid, "name\ncafé\n".encode("latin-1"), filename="x.csv")
    assert r.status_code == 400 and "UTF-8" in r.text
    r = upload(user_client, pid, b"", filename="empty.csv")
    assert r.status_code == 400 and "empty" in r.text
    r = post(user_client, f"/research/{pid}/csv", {})
    assert r.status_code == 400 and "Choose a CSV file" in r.text
    # A .csv file sent with a generic type is accepted by its name.
    r = upload(user_client, pid, b"x\n1\n", filename="ok.csv", ctype="application/octet-stream")
    assert r.status_code == 200 and "Rows of data, not counting the column names: 1" in r.text


def test_notes_validation(user_client):
    pid = new_project(user_client)
    r = post(user_client, f"/research/{pid}/notes", {"notes": "n" * 20001})
    assert r.status_code == 400 and 'href="#field-notes"' in r.text
    assert "n" * 100 in r.text  # what they typed is kept
    r = post(user_client, f"/research/{pid}/notes", {"notes": "code: 4111-1111-1111-1111"})
    assert r.status_code == 400 and "card number" in r.text and 'near: &#34;code: 4111-1111-1111-1111&#34;' in r.text
    r = post(user_client, f"/research/{pid}/notes", {"notes": "First note"})
    r = post(user_client, f"/research/{pid}/notes", {"notes": "Second note"})
    assert "Second note" in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_notes").fetchone()[0] == 1


def test_describe_needs_notes_or_data(user_client, fake_ai):
    fake_answers(fake_ai)
    pid = new_project(user_client)
    r = post(user_client, f"/research/{pid}/data/describe")
    assert r.status_code == 400 and "Upload a data file or save some notes" in r.text
    assert fake_ai.calls == []
    post(user_client, f"/research/{pid}/notes", {"notes": "Only notes, no file."})
    r = post(user_client, f"/research/{pid}/data/describe")
    assert "Scores rise with practice" in r.text
    assert "No data file has been uploaded." in fake_ai.calls[-1][2]["prompt"]


def test_new_upload_replaces_old_interpretation(user_client, fake_ai):
    fake_answers(fake_ai)
    pid = new_project(user_client)
    upload(user_client, pid, CSV_TEXT.encode())
    post(user_client, f"/research/{pid}/data/describe")
    r = upload(user_client, pid, b"x\n1\n2\n")
    assert "Scores rise with practice" not in r.text
    assert "Rows of data, not counting the column names: 2" in r.text


# ----- exports -----------------------------------------------------------------------------------

def test_exports(user_client, fake_ai):
    fake_answers(fake_ai)
    pid = new_project(user_client)
    assert user_client.get(f"/research/{pid}/draft.md").status_code == 404
    post(user_client, f"/research/{pid}/prior-work")
    post(user_client, f"/research/{pid}/plan")
    post(user_client, f"/research/{pid}/draft")
    r = user_client.get(f"/research/{pid}/draft.md")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "attachment" in r.headers["content-disposition"]
    assert r.text.startswith("# Draft paper: Exams and screen readers")
    assert "Written by the AI" in r.text and "A made-up claim [7]" in r.text
    assert "Citations the site could not match: [7]" in r.text
    assert "1. Exam access study. <https://example.org/a>" in r.text
    assert "3. One question per page. <https://example.org/c>" in r.text
    plan = user_client.get(f"/research/{pid}/plan.md")
    assert plan.status_code == 200 and "A within-subjects study" in plan.text
    prior = user_client.get(f"/research/{pid}/prior-work.md")
    assert prior.status_code == 200 and "Three studies looked" in prior.text


# ----- delete ------------------------------------------------------------------------------------

def test_delete_project(user_client, fake_ai):
    fake_answers(fake_ai)
    pid = new_project(user_client)
    post(user_client, f"/research/{pid}/plan")
    post(user_client, f"/research/{pid}/notes", {"notes": "x"})
    r = user_client.get(f"/research/{pid}/delete")
    assert_accessible(r)
    assert h1(r) == "Delete Exams and screen readers?"
    assert "Yes, delete this project" in r.text
    r = post(user_client, f"/research/{pid}/delete")
    assert r.url.path == "/research"
    assert h1(r) == "Deleted the research project Exams and screen readers"
    assert h1(user_client.get("/research")) == "Run your own research"
    assert user_client.get(f"/research/{pid}").status_code == 404
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_items").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM research_notes").fetchone()[0] == 0


def test_delete_needs_csrf(user_client):
    pid = new_project(user_client)
    r = user_client.post(f"/research/{pid}/delete", data={})
    assert r.status_code == 400
    assert user_client.get(f"/research/{pid}").status_code == 200


# ----- ownership ---------------------------------------------------------------------------------

def test_other_members_get_404_everywhere(client, fake_ai):
    fake_answers(fake_ai)
    register(client, "owner@example.com", "Owner")
    pid = new_project(client)
    post(client, f"/research/{pid}/prior-work")
    post(client, f"/research/{pid}/plan")
    post(client, f"/research/{pid}/draft")
    with db.session() as conn:
        plan_id = conn.execute("SELECT id FROM research_items WHERE kind = 'plan'").fetchone()[0]
    post(client, "/logout")
    register(client, "other@example.com", "Other")
    calls_before = len(fake_ai.calls)
    for path in ["", "/prior-work", "/plan", "/data", "/draft", "/draft.md", "/plan.md", "/prior-work.md",
                 "/delete", f"/plan?version={plan_id}"]:
        assert client.get(f"/research/{pid}{path}").status_code == 404, path
    for path in ["/prior-work", "/plan", "/draft", "/data/describe", "/notes", "/delete"]:
        assert post(client, f"/research/{pid}{path}", {"notes": "x"}).status_code == 404, path
    assert upload(client, pid, CSV_TEXT.encode()).status_code == 404
    assert len(fake_ai.calls) == calls_before
    assert "Exams and screen readers" not in client.get("/research").text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_projects").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM research_notes").fetchone()[0] == 0


def test_project_deleted_while_ai_works(user_client, fake_ai):
    pid = new_project(user_client)

    def answer(**kwargs):
        with db.session() as conn:
            conn.execute("DELETE FROM research_projects WHERE id = ?", (pid,))
        return "## Summary\nToo late."

    fake_ai.on("research.plan", answer)
    r = post(user_client, f"/research/{pid}/plan")
    assert r.url.path == "/research"
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_items").fetchone()[0] == 0


# ----- no AI -------------------------------------------------------------------------------------

def test_without_ai_the_member_is_told_how_to_connect(no_ai_app):
    client = TestClient(no_ai_app)
    register(client)
    pid = new_project(client)
    for slug, back in [("prior-work", f"/research/{pid}"), ("plan", f"/research/{pid}"),
                       ("draft", f"/research/{pid}")]:
        r = post(client, f"/research/{pid}/{slug}")
        assert r.url.path == back
        assert "connect your own Claude API key" in r.text
    # The site's own work still happens without the AI.
    r = upload(client, pid, CSV_TEXT.encode())
    assert r.status_code == 200 and "Mean 4" in r.text
    r = post(client, f"/research/{pid}/data/describe")
    assert r.url.path == f"/research/{pid}/data" and "connect your own Claude API key" in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_secret_detection_unit():
    assert research.looks_secret("4111 1111 1111 1111")
    assert not research.looks_secret("4111 1111 1111 1112")  # fails the Luhn check
    assert research.looks_secret("Password: abc123")
    assert not research.looks_secret("We asked about password managers.")
    assert not research.looks_secret("Sample size 1234567890")


# ----- regressions from the audit -----------------------------------------------------------------

def test_numbers_too_big_for_an_address_are_not_found(client, fake_ai):
    fake_answers(fake_ai)
    client = TestClient(client.app, raise_server_exceptions=False)
    register(client)
    pid = new_project(client)
    huge = "99999999999999999999999"
    for path in [f"/research/{huge}", f"/research/{huge}/data", f"/research/{huge}/draft.md",
                 f"/research/{pid}/plan?version={huge}", f"/research/{pid}/plan?version=-1"]:
        assert client.get(path).status_code == 404, path
    assert post(client, f"/research/{huge}/plan").status_code == 404
    assert fake_ai.calls == []


def test_csv_values_too_big_for_the_computer_do_not_break_the_page(user_client):
    # 1e999 looks like a number, but a computer stores it as infinity, and the mean would be "nan".
    stats = research.compute_csv_stats("a,b\n1e999,1e308\n-1e999,1e308\n")
    a, b = stats["column_stats"]
    assert a["kind"] == "text"
    # Two values near the largest a computer can hold: the total overflows, the mean does not.
    assert b["kind"] == "number" and b["mean"] == pytest.approx(1e308) and b["mean_text"] == "1e+308"
    pid = new_project(user_client)
    r = upload(user_client, pid, b"a\n1e999\n-1e999\n")
    assert_accessible(r)
    assert "<strong>a</strong>: 2 filled" in r.text


def test_long_and_many_column_names_are_shortened_for_reading():
    header = ",".join(["x" * 5000] + [f"c{i}" for i in range(79)])
    stats = research.compute_csv_stats(header + "\n" + ",".join("1" * 80) + "\n")
    assert stats["columns"] == 80 and stats["columns_shown"] == 50
    assert len(stats["column_names"][0]) == research.MAX_CELL_TEXT
    text = research.facts_as_text(stats)
    assert "c48, and 30 more" in text and "c49" not in text
    assert "Facts were computed for the first 50 columns only." in text


def test_csv_saved_as_utf16_gets_a_message_that_says_how_to_fix_it(user_client):
    pid = new_project(user_client)
    r = upload(user_client, pid, "name,score\nAna,3\n".encode("utf-16"))
    assert r.status_code == 400
    assert "saved as UTF-16" in r.text and "Save it again as CSV UTF-8" in r.text


def test_csv_problems_say_how_to_fix_them(user_client):
    pid = new_project(user_client)
    r = upload(user_client, pid, b"only,headers\n")
    assert r.status_code == 400 and "Add your data rows, save the file and upload it again." in r.text
    r = upload(user_client, pid, b"\n\n\n")
    assert r.status_code == 400 and "column names in the first row" in r.text
    # A file name with a path or runs of spaces is reduced to a plain name.
    r = upload(user_client, pid, b"x\n1\n", filename="C:\\Users\\me\\my    data.csv")
    assert h1(r) == "Facts computed from my data.csv: Exams and screen readers"


@pytest.mark.parametrize("answer", [123, None, {"text": "x"}, ["x"]])
def test_ai_answer_of_the_wrong_type_fails_plainly(user_client, fake_ai, answer):
    fake_ai.on("research.plan", answer)
    pid = new_project(user_client)
    r = post(user_client, f"/research/{pid}/plan")
    assert "This did not finish" in r.text and "came back incomplete, so nothing was saved" in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_items").fetchone()[0] == 0


def test_search_result_with_missing_parts_fails_plainly(user_client, fake_ai):
    class Odd:
        pass

    fake_ai.on("research.prior_work", lambda **kwargs: Odd())
    pid = new_project(user_client)
    r = post(user_client, f"/research/{pid}/prior-work")
    assert "This did not finish" in r.text and "came back incomplete" in r.text


def test_sources_from_the_ai_are_cleaned():
    sources = research._safe_sources([
        {"url": "https://a.example/x", "title": "  Two\nlines  ", "quote": 5},
        {"url": "https://a.example/x", "title": "Duplicate"},
        {"url": "https:no-host", "title": "No host"},
        {"url": "https://bad example/", "title": "Space"},
        {"url": None, "title": "No address"},
        {"url": "ftp://files.example/", "title": "Not the web"},
        Source(url="https://b.example/", title=""),
        "not a source",
    ])
    assert sources == [
        {"url": "https://a.example/x", "title": "Two lines", "quote": ""},
        {"url": "https://b.example/", "title": "https://b.example/", "quote": ""},
    ]
    assert research._safe_sources(None) == [] and research._safe_sources("https://x.example") == []
    many = research._safe_sources([{"url": f"https://s.example/{i}", "title": "t"} for i in range(80)])
    assert len(many) == research.MAX_SOURCES


def test_second_click_joins_the_running_work_instead_of_paying_twice(user_client, fake_ai):
    fake_answers(fake_ai)
    pid = new_project(user_client)
    with db.session() as conn:
        uid = conn.execute("SELECT user_id FROM research_projects WHERE id = ?", (pid,)).fetchone()[0]
        conn.execute("INSERT INTO jobs (id, user_id, kind, title, status, created_at) VALUES (?, ?, ?, ?, 'running', ?)",
                     ("runningjob", uid, f"research.plan:{pid}", "Writing a study plan", db.now()))
    r = post(user_client, f"/research/{pid}/plan", follow_redirects=False)
    assert r.headers["location"] == "/jobs/runningjob"
    assert fake_ai.calls == []
    r = user_client.get("/jobs/runningjob")
    assert "already started, so it was not started again" in r.text
    # Other work still starts.
    r = post(user_client, f"/research/{pid}/prior-work")
    assert r.url.path == f"/research/{pid}/prior-work"
    # Work stuck as "running" for a long time (for example after a restart) does not block a new start.
    with db.session() as conn:
        conn.execute("UPDATE jobs SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = 'runningjob'")
    r = post(user_client, f"/research/{pid}/plan")
    assert r.url.path == f"/research/{pid}/plan"
    assert fake_ai.purposes() == ["research.prior_work", "research.plan"]


def test_new_upload_while_the_ai_describes_the_old_file(user_client, fake_ai):
    pid = new_project(user_client)
    upload(user_client, pid, CSV_TEXT.encode())

    def answer(**kwargs):
        upload(user_client, pid, b"x\n1\n2\n", filename="new.csv")
        return "## Summary\nAbout the old file."

    fake_ai.on("research.data", answer)
    r = post(user_client, f"/research/{pid}/data/describe")
    assert "This did not finish" in r.text and "uploaded a new data file while the AI was working" in r.text
    page = user_client.get(f"/research/{pid}/data").text
    assert "From new.csv" in page and "About the old file" not in page


def test_what_happened_headings_cannot_be_faked_by_a_link(user_client, fake_ai):
    fake_answers(fake_ai)
    pid = new_project(user_client)
    user_client.get(f"/research/{pid}")
    assert h1(user_client.get(f"/research/{pid}?done=created")) == "Exams and screen readers"
    assert h1(user_client.get(f"/research/{pid}/plan?done=1")) == "Study plan: Exams and screen readers"
    assert h1(user_client.get(f"/research/{pid}/data?done=1")) == "Notes and data: Exams and screen readers"
    post(user_client, f"/research/{pid}/plan")
    with db.session() as conn:
        plan_id = conn.execute("SELECT id FROM research_items WHERE kind = 'plan'").fetchone()[0]
    r = user_client.get(f"/research/{pid}/plan?version={plan_id}&done=1")
    assert h1(r) == "Study plan: Exams and screen readers"
    # A "done" heading saved for one page is not shown on another.
    post(user_client, f"/research/{pid}/notes", {"notes": "x"}, follow_redirects=False)
    assert h1(user_client.get(f"/research/{pid}")) == "Exams and screen readers"
    assert h1(user_client.get(f"/research/{pid}/data")) == "Notes and data: Exams and screen readers"


def test_clearing_notes_says_so(user_client):
    pid = new_project(user_client)
    post(user_client, f"/research/{pid}/notes", {"notes": "Something"})
    r = post(user_client, f"/research/{pid}/notes", {"notes": "   "})
    assert h1(r) == "Your notes are cleared: Exams and screen readers"


def test_error_messages_say_how_long_the_text_is(user_client):
    r = post(user_client, "/research", {"title": "t" * 250, "question": "q" * 2001, "field": "f" * 101,
                                        "known": "k" * 5001})
    assert r.status_code == 400
    assert "It has 250 now." in r.text and "It has 2001 now." in r.text
    assert "It has 101 now." in r.text and "for what you already know. It has 5001 now." in r.text
    pid = new_project(user_client)
    r = post(user_client, f"/research/{pid}/notes", {"notes": "n" * 20001})
    assert "They have 20001 now." in r.text


def test_project_name_is_kept_on_one_line(user_client):
    pid = new_project(user_client, title="  Exams\nand   screen readers ", field="Edu\r\ncation")
    r = user_client.get(f"/research/{pid}")
    assert h1(r) == "Project saved: Exams and screen readers"
    assert "<li>Field: Edu cation</li>" in r.text


def test_plan_page_says_what_the_site_checked(user_client, fake_ai):
    fake_answers(fake_ai)
    pid = new_project(user_client)
    r = post(user_client, f"/research/{pid}/plan")
    assert "Checked by the site:" in r.text and "1 could not be matched" in r.text
    assert "Not checked: whether a source supports the sentence that cites it." in r.text
    post(user_client, f"/research/{pid}/prior-work")
    r = post(user_client, f"/research/{pid}/plan")
    assert "Every one matches a source, and each links to it." in r.text
    fake_ai.on("research.plan", "## Summary\nNo citations.")
    r = post(user_client, f"/research/{pid}/plan")
    assert "The plan cites no numbered sources." in r.text


def test_exports_say_what_was_not_checked(user_client, fake_ai):
    fake_answers(fake_ai)
    pid = new_project(user_client)
    post(user_client, f"/research/{pid}/plan")
    post(user_client, f"/research/{pid}/prior-work")
    post(user_client, f"/research/{pid}/draft")
    prior = user_client.get(f"/research/{pid}/prior-work.md").text
    assert "Not checked: whether each study says what the AI says" in prior
    plan = user_client.get(f"/research/{pid}/plan.md").text
    assert "Citations the site could not match: [2]" in plan and "Not checked: whether the plan works" in plan
    assert "Not checked: whether each source supports" in user_client.get(f"/research/{pid}/draft.md").text


def test_data_page_wording(user_client, fake_ai):
    fake_answers(fake_ai)
    pid = new_project(user_client)
    r = user_client.get(f"/research/{pid}/data")
    assert " below" not in r.text and 'under the heading "Your notes and data file"' in r.text
    assert " below" not in user_client.get("/research").text
    post(user_client, f"/research/{pid}/notes", {"notes": "Only notes."})
    r = post(user_client, f"/research/{pid}/data/describe")
    assert "from your notes only, because no data file was uploaded" in r.text
    assert "Ask the AI to describe the data again" in r.text
    upload(user_client, pid, CSV_TEXT.encode())
    r = post(user_client, f"/research/{pid}/data/describe")
    assert "from the computed facts, the first 4 rows of data.csv and your notes" in r.text
    assert "Test any pattern with a statistical analysis before you report it." in r.text


# ----- ease of use: findings from blind members' sessions -----------------------------------------

def test_notes_and_file_are_one_form_so_typed_notes_are_never_lost(user_client):
    pid = new_project(user_client)
    page = user_client.get(f"/research/{pid}/data").text
    form = page.split(f'action="/research/{pid}/notes"')[1].split("</form>")[0]
    assert 'name="notes"' in form and 'name="csv"' in form and 'enctype="multipart/form-data"' in form
    assert "Save notes and file</button>" in form
    # Notes typed and a file chosen are saved together.
    r = post(user_client, f"/research/{pid}/notes", {"notes": "Pilot ran with 4 people."},
             files={"csv": ("data.csv", CSV_TEXT.encode(), "text/csv")})
    assert r.status_code == 200
    assert h1(r) == "Notes saved and facts computed from data.csv: Exams and screen readers"
    assert "Pilot ran with 4 people." in r.text and "Rows of data, not counting the column names: 4" in r.text
    # A bad file keeps the newly typed notes in the box, unsaved, and says the old facts are still in use.
    r = post(user_client, f"/research/{pid}/csv", {"notes": "A longer note I typed first."},
             files={"csv": ("photo.png", b"\x89PNG\r\n\x1a\n", "image/png")})
    assert r.status_code == 400
    assert "A longer note I typed first.</textarea>" in r.text
    assert "Your notes are not saved yet." in r.text
    assert "Nothing was changed. The facts from data.csv are still in use." in r.text
    with db.session() as conn:
        assert conn.execute("SELECT body FROM research_notes").fetchone()[0] == "Pilot ran with 4 people."
    # Notes with a problem: the file is not uploaded either, and the member is told to choose it again.
    r = post(user_client, f"/research/{pid}/notes", {"notes": "n" * 20001},
             files={"csv": ("new.csv", b"x\n1\n", "text/csv")})
    assert r.status_code == 400 and "please choose the file again too" in r.text
    assert "From data.csv" in r.text


def test_ordinary_sentences_about_passwords_and_codes_are_accepted(user_client):
    pid = new_project(user_client)
    for sentence in ["The password is hard to type with a braille display.",
                     "The verification code is read aloud by JAWS.",
                     "We asked about password managers."]:
        assert not research.looks_secret(sentence), sentence
        r = post(user_client, f"/research/{pid}/notes", {"notes": sentence})
        assert h1(r) == "Your notes are saved: Exams and screen readers", sentence
    r = post(user_client, "/research", {"title": "Login study", "question": "Why the password is hard to find?"},
             follow_redirects=False)
    assert r.status_code == 303


def test_missing_values_like_na_do_not_turn_numbers_into_text(user_client):
    text = "id,time_seconds,group\n1,182.5,A\n2,240,B\n3,NA,A\n4,300.4,A\n"
    stats = research.compute_csv_stats(text)
    _, time, _ = stats["column_stats"]
    assert time["kind"] == "number" and time["count"] == 3 and time["empty"] == 1
    assert time["missing_marks"] == ["NA"] and time["unit"] == "seconds"
    assert research.column_facts(time) == (
        "3 values, 1 missing (NA). In seconds: mean 240.97, median 240, smallest 182.5, largest 300.4, "
        "standard deviation 58.96.")
    for mark in ["n/a", "N/A", "-", ".", "null", "NaN"]:
        col = research.compute_csv_stats(f"v\n1\n{mark}\n3\n")["column_stats"][0]
        assert col["kind"] == "number" and col["empty"] == 1, mark
    pid = new_project(user_client)
    r = upload(user_client, pid, text.encode(), filename="pilot.csv")
    assert_accessible(r)
    assert "<strong>time_seconds</strong>: 3 values, 1 missing (NA). In seconds: mean 240.97" in r.text
    # The first rows are shown as a table with column headers and a caption.
    table = r.text.split("<table>")[1].split("</table>")[0]
    assert "<caption>The first 4 rows of pilot.csv, all 3 columns</caption>" in table
    assert '<th scope="col">time_seconds</th>' in table and "<td>NA</td>" in table


def test_first_rows_table_shows_every_column_in_its_own_scroll_region(user_client):
    """A wide file shows all its columns; the table scrolls sideways in a named, focusable region, not the page."""
    names = [f"c{i}" for i in range(1, 10)]
    text = ",".join(names) + "\n" + ",".join(str(i) for i in range(1, 10)) + "\n1,2\n"
    pid = new_project(user_client)
    r = upload(user_client, pid, text.encode(), filename="wide.csv")
    assert_accessible(r)
    assert ('<h3 id="first-rows-title">First rows of the file</h3>'
            '<div class="table-scroll" role="region" aria-labelledby="first-rows-title" tabindex="0">') \
        in re.sub(r">\s+<", "><", r.text)
    table = r.text.split("<table>")[1].split("</table>")[0]
    assert "<caption>The first 2 rows of wide.csv, all 9 columns</caption>" in table
    assert table.count('<th scope="col">') == 9 and '<th scope="col">c9</th>' in table and "<td>9</td>" in table
    # A short row is filled out with empty cells, so every row has a cell under each column name.
    assert table.count("<td>") == 18 and table.count("<td></td>") == 7
    # More columns than the site keeps: the caption says which ones are shown.
    many = research.MAX_STAT_COLUMNS + 5
    text = ",".join(f"k{i}" for i in range(many)) + "\n" + ",".join("1" for _ in range(many)) + "\n"
    r = upload(user_client, pid, text.encode(), filename="huge.csv")
    table = r.text.split("<table>")[1].split("</table>")[0]
    assert (f"<caption>The first row of huge.csv, the first {research.MAX_STAT_COLUMNS} of {many} columns"
            "</caption>") in table
    assert table.count('<th scope="col">') == research.MAX_STAT_COLUMNS


def test_plan_and_draft_say_when_there_are_no_sources_to_cite(user_client, fake_ai):
    fake_answers(fake_ai)
    pid = new_project(user_client)
    r = user_client.get(f"/research/{pid}")
    assert r.text.count(research.NO_SOURCES_NOTE) == 2
    # Only stage 1 is the main action; the others say plainly that they would have nothing to cite.
    assert r.text.count("button-primary") == 1
    assert 'class="button button-primary">Find prior work</button>' in r.text
    assert ">Write a study plan without sources</button>" in r.text
    assert ">Write a draft paper without sources</button>" in r.text
    r = user_client.get(f"/research/{pid}/draft")
    assert research.NO_SOURCES_NOTE in r.text and "Go to stage 1, prior work" in r.text
    post(user_client, f"/research/{pid}/prior-work")
    r = user_client.get(f"/research/{pid}")
    assert research.NO_SOURCES_NOTE not in r.text
    assert 'class="button button-primary">Write a study plan</button>' in r.text


def test_rerun_buttons_are_apart_and_say_which_stage(user_client, fake_ai):
    fake_answers(fake_ai)
    pid = new_project(user_client)
    r = post(user_client, f"/research/{pid}/prior-work")
    # The next stage is the main action on the stage page; running this one again sits under its own heading.
    assert 'class="button button-primary">Next: write the study plan</button>' in r.text
    after = r.text.split("<h3>Run this stage again</h3>")[1]
    assert "Search for prior work again (uses your key or credits)</button>" in after
    r = user_client.get(f"/research/{pid}")
    stage = r.text.split("<h2>1. Prior work</h2>")[1].split("<h2>")[0]
    assert "Read the prior work</a>" in stage and "<button" not in stage
    rerun = r.text.split("<h2>Run a stage again</h2>")[1].split("<h2>")[0]
    assert "Search for prior work again (uses your key or credits)</button>" in rerun
    assert "Write the study plan again" not in rerun  # not done yet, so nothing to run again
    r = post(user_client, f"/research/{pid}/plan")
    assert f'class="button button-primary" href="/research/{pid}/data">Next: add notes and data</a>' in r.text


def test_citations_link_to_their_sources_and_back(user_client, fake_ai):
    fake_answers(fake_ai)
    pid = new_project(user_client)
    post(user_client, f"/research/{pid}/prior-work")
    r = post(user_client, f"/research/{pid}/draft")
    assert_accessible(r)
    page = r.text
    assert '[<a href="#source-1" id="cite-1"><span class="visually-hidden">source </span>1</a>]' in page
    assert ('[<a href="#source-2" id="cite-2"><span class="visually-hidden">source </span>2</a>, '
            '<a href="#source-3" id="cite-3"><span class="visually-hidden">source </span>3</a>]') in page
    # A number with no source stays plain text: there is nothing to link to.
    assert "A made-up claim [7]." in page and 'href="#source-7"' not in page
    assert '<li id="source-1" tabindex="-1">' in page and '<a href="#cite-1">Back to [1] in the text</a>' in page
    r = post(user_client, f"/research/{pid}/plan")
    assert "<h2>The study plan, written by the AI (not checked)</h2>" in r.text
    assert 'href="#source-2" id="cite-2"' in r.text


def test_link_citations_leaves_links_and_code_alone():
    html, cited = research.link_citations('<p>A [1]. <code>x[2]</code> <a href="#">[2]</a> [5]</p>', 3)
    assert "<code>x[2]</code>" in html and '<a href="#">[2]</a>' in html and "[5]" in html
    assert cited == {1}


def test_older_versions_can_be_downloaded_and_made_current(user_client, fake_ai):
    fake_answers(fake_ai)
    pid = new_project(user_client)
    post(user_client, f"/research/{pid}/draft")
    fake_ai.on("research.draft", "# Second\n## Abstract\nThe second draft.")
    post(user_client, f"/research/{pid}/draft")
    with db.session() as conn:
        first, _ = [r[0] for r in conn.execute("SELECT id FROM research_items WHERE kind = 'draft' ORDER BY id")]
    old = user_client.get(f"/research/{pid}/draft?version={first}")
    assert_accessible(old)
    assert f'href="/research/{pid}/draft.md?version={first}">Download this version (' in old.text
    assert "Make this the current draft</button>" in old.text
    text = user_client.get(f"/research/{pid}/draft.md?version={first}").text
    assert "A made-up claim [7]" in text and "The second draft." not in text
    assert "The second draft." in user_client.get(f"/research/{pid}/draft.md").text
    r = post(user_client, f"/research/{pid}/draft/restore", {"version": str(first)})
    assert r.url.path == f"/research/{pid}/draft"
    assert h1(r).startswith("The draft from ")
    assert h1(r).endswith(" is now the current version: Exams and screen readers")
    assert "A made-up claim [7]" in r.text and "(a copy of the version from " in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_items WHERE kind = 'draft'").fetchone()[0] == 3
    assert post(user_client, f"/research/{pid}/data/restore", {"version": str(first)}).status_code == 404
    assert post(user_client, f"/research/{pid}/draft/restore", {"version": "x"}).status_code == 404


def test_copy_the_draft_into_a_new_write_document(user_client, fake_ai):
    fake_answers(fake_ai)
    pid = new_project(user_client)
    post(user_client, f"/research/{pid}/prior-work")
    post(user_client, f"/research/{pid}/draft")
    with db.session() as conn:
        draft_id = conn.execute("SELECT id FROM research_items WHERE kind = 'draft'").fetchone()[0]
    r = post(user_client, f"/research/{pid}/draft/write", {"version": str(draft_id)}, follow_redirects=False)
    assert r.status_code == 303 and re.fullmatch(r"/write/\d+", r.headers["location"])
    doc = user_client.get(r.headers["location"])
    assert doc.status_code == 200
    assert "The AI wrote it and nobody has checked it." in doc.text
    with db.session() as conn:
        row = conn.execute("SELECT * FROM write_docs").fetchone()
        note = conn.execute("SELECT note FROM write_versions WHERE doc_id = ?", (row["id"],)).fetchone()[0]
    assert row["title"] == "Exams and screen readers" and row["kind"] == "paper"
    assert "A made-up claim [7]." in row["body"] and "## Sources" in row["body"]
    assert "1. Exam access study. <https://example.org/a>" in row["body"]
    assert note == "Drafted by the AI in Research (not checked)"
    # A second press opens the same copy instead of making another.
    again = post(user_client, f"/research/{pid}/draft/write", {"version": str(draft_id)}, follow_redirects=False)
    assert again.headers["location"] == r.headers["location"]
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM write_docs").fetchone()[0] == 1


def test_new_routes_are_private(client, fake_ai):
    fake_answers(fake_ai)
    register(client, "owner2@example.com", "Owner")
    pid = new_project(client)
    post(client, f"/research/{pid}/draft")
    post(client, "/logout")
    register(client, "other2@example.com", "Other")
    for path in ["/draft/write", "/draft/restore", "/plan/restore"]:
        assert post(client, f"/research/{pid}{path}").status_code == 404, path
    assert client.get(f"/research/{pid}/draft.md?version=1").status_code == 404
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM write_docs").fetchone()[0] == 0
