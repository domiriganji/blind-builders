"""Research section: prior work, study plan, notes and data, draft paper, exports, ownership and no-AI path."""
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
    return int(r.headers["location"].rsplit("/", 1)[1])


def assert_accessible(r):
    assert r.status_code == 200, r.text
    errors = check_html(r.text).errors
    assert not errors, errors
    assert len(re.findall(r"<h1[ >]", r.text)) == 1


def h1(r) -> str:
    return re.search(r"<h1>(.*?)</h1>", r.text, re.S).group(1)


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
    assert h1(r) == "Exams and screen readers"
    assert "<title>Exams and screen readers –" in r.text
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
    assert h1(r) == "Run your own research: please fix the problems"
    assert 'href="#field-title"' in r.text and 'href="#field-question"' in r.text
    assert 'aria-invalid="true"' in r.text
    r = post(user_client, "/research", {"title": "x" * 201, "question": "Why?"})
    assert r.status_code == 400 and "200 characters or fewer" in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_projects").fetchone()[0] == 0


def test_create_refuses_card_numbers_and_passwords(user_client):
    r = post(user_client, "/research", {"title": "Mine", "question": "Card 4111 1111 1111 1111 works?"})
    assert r.status_code == 400 and "card number" in r.text
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
    assert h1(r) == "Prior work: Exams and screen readers"
    assert "Written by the AI" in r.text and "Open the sources to check" in r.text
    assert 'href="https://example.org/a" rel="noopener noreferrer"' in r.text
    assert "Sources returned by the search (3)" in r.text
    kind, purpose, kwargs = fake_ai.calls[-1]
    assert (kind, purpose) == ("research", "research.prior_work")
    assert "never invent" in kwargs["system"].lower() and "screen reader" in kwargs["system"]
    assert "one question per page" in kwargs["prompt"]
    # The AI's ## headings sit under the page's h2.
    assert "<h3>Summary</h3>" in r.text and "<h2>Summary</h2>" not in r.text

    # Stage 2: study plan, given the prior work and numbered sources.
    r = post(user_client, f"/research/{pid}/plan")
    assert r.url.path == f"/research/{pid}/plan"
    assert_accessible(r)
    assert h1(r) == "Study plan: Exams and screen readers"
    _, purpose, kwargs = fake_ai.calls[-1]
    assert purpose == "research.plan"
    assert "Three studies looked at screen readers" in kwargs["prompt"]
    assert "[3] One question per page https://example.org/c" in kwargs["prompt"]
    assert "blind participants" in kwargs["system"]

    # Stage 3: notes, CSV and the AI's interpretation.
    r = post(user_client, f"/research/{pid}/notes", {"notes": "Pilot ran with 4 people."})
    assert r.url.path == f"/research/{pid}/data" and "Your notes are saved" in r.text
    r = upload(user_client, pid, CSV_TEXT.encode())
    assert_accessible(r)
    assert "Computed by the site" in r.text and "4 rows and 3 columns" in r.text
    r = post(user_client, f"/research/{pid}/data/describe")
    assert r.url.path == f"/research/{pid}/data"
    assert_accessible(r)
    assert "<h2>The AI's interpretation</h2>" in r.text and "Scores rise with practice" in r.text
    assert "It is an interpretation, not a result" in r.text
    _, purpose, kwargs = fake_ai.calls[-1]
    assert purpose == "research.data"
    assert "score (numbers): 3 values, 1 empty, mean 4, smallest 3, largest 5" in kwargs["prompt"]
    assert "Pilot ran with 4 people." in kwargs["prompt"]
    assert "Ana,3,A" in kwargs["prompt"]

    # Stage 4: draft.
    r = post(user_client, f"/research/{pid}/draft")
    assert r.url.path == f"/research/{pid}/draft"
    assert_accessible(r)
    assert h1(r) == "Draft paper: Exams and screen readers"
    assert "The AI wrote this draft" in r.text
    _, purpose, kwargs = fake_ai.calls[-1]
    assert purpose == "research.draft"
    assert "[RESULT NEEDED]" in kwargs["system"]
    assert "A within-subjects study" in kwargs["prompt"] and "Pilot ran with 4 people." in kwargs["prompt"]
    assert "score (numbers)" in kwargs["prompt"]
    assert 'href="/research/%d/draft.md"' % pid in r.text
    assert 'href="/write/new?title=Exams+and+screen+readers&amp;kind=paper"' in r.text
    assert "Edit it in Write and publish" in r.text

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
    assert "<li>[7]</li>" in section
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
    assert "<li>[1]</li>" in r.text and "There are no sources yet" in r.text
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
    assert "This did not finish" in r.text and "empty answer" in r.text


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
    assert a["mean_text"] == "3.0625" and a["min_text"] == "-2" and a["max_text"] == "10"
    assert b["top"][0] == {"value": "x", "count": 3}
    assert c3["empty"] == 1 and c3["distinct"] == 1
    # A column with a word among numbers is not numeric; NaN is not a number here.
    mixed = research.compute_csv_stats("v\n1\n2\nnan\n")["column_stats"][0]
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
    assert "Empty cells: 1" in page
    assert "<strong>score</strong> (numbers): 3 values, 1 empty. Mean 4, smallest 3, largest 5." in page
    assert "<strong>group</strong> (text): 4 filled, 0 empty, 2 distinct values. Most common: A (3); B (1)." in page
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
    assert h1(r).endswith(": please fix the problems")
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
    assert r.status_code == 400 and "card number" in r.text
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
    assert r.url.path == "/research" and "Deleted the research project" in r.text
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
