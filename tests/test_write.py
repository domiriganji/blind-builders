"""Write and publish: documents, versions, AI suggestions accepted one at a time, export, venues, cover letters."""
import io
import re

import pytest
from fastapi.testclient import TestClient

from studio import db
from studio.a11y_check import check_html
from studio.ai import AIError, ResearchResult, Source
from studio.features import write as write_mod

from .conftest import post, register

BODY = "# Introduction\n\nThe cat sat on teh mat. It was happy.\n\nBlind writers deserve good tools."


def create_doc(client, title="My essay", kind="article", target="") -> int:
    r = post(client, "/write/new", {"title": title, "kind": kind, "target": target}, follow_redirects=False)
    assert r.status_code == 303, r.text
    m = re.fullmatch(r"/write/(\d+)", r.headers["location"])
    assert m, r.headers["location"]
    return int(m.group(1))


def save(client, doc_id, body, title="My essay", target=""):
    return post(client, f"/write/{doc_id}/save", {"body": body, "title": title, "target": target})


def body_of(doc_id) -> str:
    with db.session() as conn:
        return conn.execute("SELECT body FROM write_docs WHERE id = ?", (doc_id,)).fetchone()["body"]


def versions_of(doc_id) -> list[dict]:
    with db.session() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM write_versions WHERE doc_id = ? ORDER BY id", (doc_id,))]


def suggestion_rows(batch_id) -> list[dict]:
    with db.session() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM write_suggestions WHERE batch_id = ? ORDER BY number",
                                              (batch_id,))]


def h1(html: str) -> str:
    found = re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    assert len(found) == 1, found
    return found[0].strip()


def ask_suggestions(client, doc_id, fake_ai, suggestions, note="Mostly clear.", goal="spelling") -> int:
    fake_ai.on("write.suggest", {"overall_note": note, "suggestions": suggestions})
    r = post(client, f"/write/{doc_id}/suggest", {"goal": goal, "instructions": ""}, follow_redirects=False)
    assert r.status_code == 303, r.text
    job_page = client.get(r.headers["location"], follow_redirects=False)
    assert job_page.status_code == 303, job_page.text
    m = re.fullmatch(rf"/write/{doc_id}/suggestions/(\d+)", job_page.headers["location"])
    assert m, job_page.headers["location"]
    return int(m.group(1))


# ----- index and creating ------------------------------------------------------------------------

def test_signed_out_index_explains_and_links_to_sign_in(client):
    r = client.get("/write")
    assert r.status_code == 200
    assert h1(r.text) == "Write and publish"
    assert 'href="/login?next=/write"' in r.text and 'href="/register?next=/write"' in r.text
    assert "never changes your words by itself" in r.text
    assert not check_html(r.text).errors


def test_signed_out_cannot_open_new_form(client):
    r = client.get("/write/new", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_create_and_list(user_client):
    r = user_client.get("/write")
    assert "You have no documents yet" in r.text and "Start a new document" in r.text
    doc_id = create_doc(user_client, "Letter to the council", "letter", "City News")
    page = user_client.get(f"/write/{doc_id}")
    assert page.status_code == 200
    assert h1(page.text) == "Letter to the council"
    assert "<title>Letter to the council –" in page.text
    assert "Nothing saved yet" in page.text and "Aiming for: City News" in page.text
    assert 'rows="20"' in page.text and ">Your text" in page.text
    for heading in ["Get suggestions from the AI", "Versions", "Export", "Find where to publish", "Draft a cover letter"]:
        assert f">{heading}</h2>" in page.text
    listing = user_client.get("/write")
    assert f'href="/write/{doc_id}"' in listing.text and "Letter" in listing.text
    assert '<th scope="col">Words</th>' in listing.text and "You have 1 document." in listing.text


def test_new_form_is_prefilled(user_client):
    r = user_client.get("/write/new?title=Grant+story&kind=story")
    assert r.status_code == 200 and h1(r.text) == "Start a new document"
    assert 'value="Grant story"' in r.text
    assert re.search(r'value="story" checked', r.text)
    # An unknown kind falls back to article
    r = user_client.get("/write/new?kind=poem")
    assert re.search(r'value="article" checked', r.text)


def test_create_validation_errors(user_client):
    r = post(user_client, "/write/new", {"title": "", "kind": "poem", "target": "x" * 300})
    assert r.status_code == 400
    assert h1(r.text) == "Start a new document: please fix the problems"
    assert 'href="#field-title"' in r.text and 'href="#field-kind"' in r.text and 'href="#field-target"' in r.text
    assert 'aria-invalid="true"' in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM write_docs").fetchone()["n"] == 0


def test_create_requires_csrf(user_client):
    r = user_client.post("/write/new", data={"title": "T", "kind": "article"})
    assert r.status_code == 400


# ----- saving and versions -----------------------------------------------------------------------

def test_save_creates_versions_and_skips_unchanged(user_client):
    doc_id = create_doc(user_client)
    r = save(user_client, doc_id, "First draft.")
    assert r.status_code == 200 and "Saved as version 1" in r.text
    assert "2 words" in r.text
    save(user_client, doc_id, "Second draft, longer.")
    r = save(user_client, doc_id, "Second draft, longer.")
    assert "no new version was needed" in r.text
    versions = versions_of(doc_id)
    assert [v["body"] for v in versions] == ["First draft.", "Second draft, longer."]
    assert all(v["note"] == "Saved by you" for v in versions)
    page = user_client.get(f"/write/{doc_id}/versions")
    assert h1(page.text) == "Versions of My essay"
    assert "2 versions saved" in page.text
    # Newest first
    assert page.text.index("Version 2") < page.text.index("Version 1")
    v1 = user_client.get(f"/write/{doc_id}/versions/{versions[0]['id']}")
    assert h1(v1.text) == "Version 1 of My essay"
    assert "First draft." in v1.text and "Restore this version" in v1.text
    v2 = user_client.get(f"/write/{doc_id}/versions/{versions[1]['id']}")
    assert "same as your current text" in v2.text and "Restore this version" not in v2.text


def test_save_normalises_line_endings_and_updates_title_and_target(user_client):
    doc_id = create_doc(user_client)
    save(user_client, doc_id, "Line one\r\nLine two", title="New title", target="The Journal")
    assert body_of(doc_id) == "Line one\nLine two"
    page = user_client.get(f"/write/{doc_id}")
    assert h1(page.text) == "New title" and "Aiming for: The Journal" in page.text


def test_save_validation_errors(user_client):
    doc_id = create_doc(user_client)
    r = save(user_client, doc_id, "Pay with 4111 1111 1111 1111 please", title="")
    assert r.status_code == 400
    assert h1(r.text) == "My essay: please fix the problems"
    assert "card number" in r.text and 'href="#field-body"' in r.text and 'href="#field-title"' in r.text
    # The person's text is kept in the form so nothing is lost
    assert "Pay with 4111 1111 1111 1111 please" in r.text
    assert body_of(doc_id) == "" and versions_of(doc_id) == []
    r = save(user_client, doc_id, "x" * (write_mod.MAX_BODY + 1))
    assert r.status_code == 400 and "too long" in r.text


def test_card_number_detector():
    assert write_mod.looks_like_card_number("4111-1111-1111-1111")
    assert write_mod.looks_like_card_number("card 378282246310005 here")
    assert not write_mod.looks_like_card_number("ISBN 978-0-306-40615-7")
    assert not write_mod.looks_like_card_number("1234 5678 9012 3456")  # fails the Luhn check
    assert not write_mod.looks_like_card_number("In 2026 we had 300 members.")


def test_restore_saves_current_first(user_client):
    doc_id = create_doc(user_client)
    save(user_client, doc_id, "Old text.")
    save(user_client, doc_id, "New text.")
    first = versions_of(doc_id)[0]
    r = post(user_client, f"/write/{doc_id}/versions/{first['id']}/restore")
    assert r.status_code == 200 and "Restored version 1" in r.text
    assert body_of(doc_id) == "Old text."
    notes = [(v["body"], v["note"]) for v in versions_of(doc_id)]
    assert notes[-1] == ("Old text.", "Restored version 1")
    assert ("New text.", "Saved by you") in notes
    # Restoring a version identical to the current text changes nothing
    latest = versions_of(doc_id)[-1]
    before = len(versions_of(doc_id))
    r = post(user_client, f"/write/{doc_id}/versions/{latest['id']}/restore")
    assert "Nothing was changed" in r.text and len(versions_of(doc_id)) == before


def test_restore_keeps_unversioned_current_text(user_client):
    doc_id = create_doc(user_client)
    save(user_client, doc_id, "Version one.")
    first = versions_of(doc_id)[0]
    # Simulate text that was never saved as a version (for example, from before versions existed).
    with db.session() as conn:
        conn.execute("UPDATE write_docs SET body = 'Loose text.' WHERE id = ?", (doc_id,))
    post(user_client, f"/write/{doc_id}/versions/{first['id']}/restore")
    notes = [(v["body"], v["note"]) for v in versions_of(doc_id)]
    assert notes[-2] == ("Loose text.", "Your text before restoring version 1")
    assert notes[-1] == ("Version one.", "Restored version 1")


# ----- suggestions -------------------------------------------------------------------------------

def test_suggestions_keep_only_exact_excerpts_in_document_order(user_client, fake_ai):
    doc_id = create_doc(user_client)
    save(user_client, doc_id, BODY)
    batch_id = ask_suggestions(user_client, doc_id, fake_ai, [
        {"original": "Blind writers deserve good tools.", "proposed": "Blind writers deserve great tools.",
         "reason": "Stronger word."},
        {"original": "teh", "proposed": "the", "reason": "Spelling."},
        {"original": "This sentence is not in the text.", "proposed": "Anything.", "reason": "Invented."},
        {"original": "It was happy.", "proposed": "It was happy.", "reason": "No change."},
    ])
    kind, purpose, kwargs = fake_ai.calls[-1]
    assert kind == "structured" and purpose == "write.suggest"
    assert "teh mat" in kwargs["prompt"] and "screen reader" in kwargs["system"]
    rows = suggestion_rows(batch_id)
    assert [(r["number"], r["original"]) for r in rows] == [(1, "teh"), (2, "Blind writers deserve good tools.")]
    assert all(r["status"] == "pending" for r in rows)
    page = user_client.get(f"/write/{doc_id}/suggestions/{batch_id}")
    assert page.status_code == 200
    assert h1(page.text) == "2 suggestions for My essay"
    assert "<title>2 suggestions for My essay –" in page.text
    assert "Suggestion 1 of 2" in page.text and "Suggestion 2 of 2" in page.text
    assert "<strong>Was:</strong> teh" in page.text and "<strong>Now:</strong> the" in page.text
    assert "<strong>Why:</strong> Spelling." in page.text
    assert "<strong>Where it is in your text:</strong> # Introduction The cat sat on teh mat. It was happy." in page.text
    assert "The AI's overall note" in page.text and "Mostly clear." in page.text and "Written by the AI" in page.text
    assert "2 more suggestions were left out" in page.text
    assert "Accept suggestion 1" in page.text and "Reject suggestion 2" in page.text
    assert "Accept all that still match" in page.text and "Reject all remaining" in page.text
    # Nothing changed yet
    assert body_of(doc_id) == BODY
    # The document page links the batch
    assert f"/write/{doc_id}/suggestions/{batch_id}" in user_client.get(f"/write/{doc_id}").text


def test_one_suggestion_heading_is_singular(user_client, fake_ai):
    doc_id = create_doc(user_client)
    save(user_client, doc_id, BODY)
    batch_id = ask_suggestions(user_client, doc_id, fake_ai, [{"original": "teh", "proposed": "the", "reason": "Spelling."}])
    page = user_client.get(f"/write/{doc_id}/suggestions/{batch_id}")
    assert h1(page.text) == "1 suggestion for My essay"
    assert "each one quotes your text word for word" in page.text


def test_no_suggestions(user_client, fake_ai):
    doc_id = create_doc(user_client)
    save(user_client, doc_id, BODY)
    batch_id = ask_suggestions(user_client, doc_id, fake_ai, [], note="This reads well.")
    page = user_client.get(f"/write/{doc_id}/suggestions/{batch_id}")
    assert h1(page.text) == "No suggestions for My essay"
    assert "This reads well." in page.text and "Accept all" not in page.text


def test_accept_applies_exactly_once(user_client, fake_ai):
    doc_id = create_doc(user_client)
    save(user_client, doc_id, "teh cat and teh dog")
    batch_id = ask_suggestions(user_client, doc_id, fake_ai, [{"original": "teh", "proposed": "the", "reason": "Spelling."}])
    s = suggestion_rows(batch_id)[0]
    r = post(user_client, f"/write/{doc_id}/suggestions/{batch_id}/{s['id']}/accept")
    assert r.status_code == 200 and "Suggestion 1 accepted" in r.text
    assert body_of(doc_id) == "the cat and teh dog"
    assert versions_of(doc_id)[-1]["note"] == "Accepted suggestion 1"
    assert versions_of(doc_id)[-1]["body"] == "the cat and teh dog"
    # Accepting again does nothing more
    r = post(user_client, f"/write/{doc_id}/suggestions/{batch_id}/{s['id']}/accept")
    assert "already decided" in r.text
    assert body_of(doc_id) == "the cat and teh dog"
    assert len(versions_of(doc_id)) == 2
    assert suggestion_rows(batch_id)[0]["status"] == "accepted"
    assert "Accepted. Your text has this change." in r.text


def test_stale_suggestion_is_detected(user_client, fake_ai):
    doc_id = create_doc(user_client)
    save(user_client, doc_id, BODY)
    batch_id = ask_suggestions(user_client, doc_id, fake_ai, [{"original": "teh", "proposed": "the", "reason": "Spelling."}])
    save(user_client, doc_id, BODY.replace("teh", "thee"))
    page = user_client.get(f"/write/{doc_id}/suggestions/{batch_id}")
    assert "This no longer matches your text" in page.text
    s = suggestion_rows(batch_id)[0]
    r = post(user_client, f"/write/{doc_id}/suggestions/{batch_id}/{s['id']}/accept")
    assert "no longer matches your text" in r.text and "Nothing was changed" in r.text
    assert suggestion_rows(batch_id)[0]["status"] == "stale"
    assert body_of(doc_id) == BODY.replace("teh", "thee")
    assert "Out of date." in r.text


def test_reject_changes_nothing(user_client, fake_ai):
    doc_id = create_doc(user_client)
    save(user_client, doc_id, BODY)
    batch_id = ask_suggestions(user_client, doc_id, fake_ai, [{"original": "teh", "proposed": "the", "reason": "Spelling."}])
    s = suggestion_rows(batch_id)[0]
    r = post(user_client, f"/write/{doc_id}/suggestions/{batch_id}/{s['id']}/reject")
    assert "Suggestion 1 rejected" in r.text
    assert suggestion_rows(batch_id)[0]["status"] == "rejected"
    assert body_of(doc_id) == BODY and len(versions_of(doc_id)) == 1
    # A rejected suggestion cannot then be accepted
    post(user_client, f"/write/{doc_id}/suggestions/{batch_id}/{s['id']}/accept")
    assert body_of(doc_id) == BODY


def test_accept_all_that_still_match(user_client, fake_ai):
    doc_id = create_doc(user_client)
    save(user_client, doc_id, "Alpha one. Beta two. Gamma three.")
    batch_id = ask_suggestions(user_client, doc_id, fake_ai, [
        {"original": "Alpha one.", "proposed": "Alpha 1.", "reason": "Digits."},
        {"original": "Beta two.", "proposed": "Beta 2.", "reason": "Digits."},
        {"original": "Gamma three.", "proposed": "Gamma 3.", "reason": "Digits."},
    ])
    rows = suggestion_rows(batch_id)
    post(user_client, f"/write/{doc_id}/suggestions/{batch_id}/{rows[1]['id']}/reject")
    # The person edits the text so suggestion 3 no longer matches
    save(user_client, doc_id, "Alpha one. Beta two. Gamma 3!")
    r = post(user_client, f"/write/{doc_id}/suggestions/{batch_id}/accept-all")
    assert r.status_code == 200
    assert "Accepted 1 suggestion" in r.text and "1 suggestion no longer matched your text" in r.text
    assert body_of(doc_id) == "Alpha 1. Beta two. Gamma 3!"
    assert [s["status"] for s in suggestion_rows(batch_id)] == ["accepted", "rejected", "stale"]
    assert versions_of(doc_id)[-1]["note"] == "Accepted suggestion 1"
    # Nothing left to do
    r = post(user_client, f"/write/{doc_id}/suggestions/{batch_id}/accept-all")
    assert "no suggestions waiting" in r.text


def test_accept_all_several_saves_one_version(user_client, fake_ai):
    doc_id = create_doc(user_client)
    save(user_client, doc_id, "teh one, teh two.")
    batch_id = ask_suggestions(user_client, doc_id, fake_ai, [
        {"original": "teh one", "proposed": "the one", "reason": "Spelling."},
        {"original": "teh two", "proposed": "the two", "reason": "Spelling."},
    ])
    post(user_client, f"/write/{doc_id}/suggestions/{batch_id}/accept-all")
    assert body_of(doc_id) == "the one, the two."
    assert versions_of(doc_id)[-1]["note"] == "Accepted suggestions 1 and 2"
    assert len(versions_of(doc_id)) == 2


def test_reject_all_remaining(user_client, fake_ai):
    doc_id = create_doc(user_client)
    save(user_client, doc_id, BODY)
    batch_id = ask_suggestions(user_client, doc_id, fake_ai, [
        {"original": "teh", "proposed": "the", "reason": "Spelling."},
        {"original": "It was happy.", "proposed": "It was glad.", "reason": "Variety."},
    ])
    first = suggestion_rows(batch_id)[0]
    post(user_client, f"/write/{doc_id}/suggestions/{batch_id}/{first['id']}/accept")
    r = post(user_client, f"/write/{doc_id}/suggestions/{batch_id}/reject-all")
    assert "Rejected 1 suggestion" in r.text
    assert [s["status"] for s in suggestion_rows(batch_id)] == ["accepted", "rejected"]
    assert body_of(doc_id) == BODY.replace("teh", "the", 1)
    assert "Accept all that still match" not in r.text


def test_suggest_validation(user_client, fake_ai):
    doc_id = create_doc(user_client)
    r = post(user_client, f"/write/{doc_id}/suggest", {"goal": "spelling"})
    assert r.status_code == 400 and "no saved text yet" in r.text
    assert h1(r.text) == "My essay: please fix the problems"
    save(user_client, doc_id, BODY)
    r = post(user_client, f"/write/{doc_id}/suggest", {"goal": "nonsense"})
    assert r.status_code == 400 and 'href="#field-goal"' in r.text
    r = post(user_client, f"/write/{doc_id}/suggest", {"goal": "publication"})
    assert r.status_code == 400 and "Name the publication" in r.text
    r = post(user_client, f"/write/{doc_id}/suggest", {"goal": "clearer", "instructions": "y" * 3000})
    assert r.status_code == 400 and 'href="#field-instructions"' in r.text
    assert fake_ai.calls == []


def test_suggest_for_publication_sends_target_and_instructions(user_client, fake_ai):
    doc_id = create_doc(user_client, target="Disability Studies Quarterly")
    save(user_client, doc_id, BODY, target="Disability Studies Quarterly")
    fake_ai.on("write.suggest", {"overall_note": "", "suggestions": []})
    post(user_client, f"/write/{doc_id}/suggest", {"goal": "publication", "instructions": "Keep British spelling"})
    prompt = fake_ai.calls[-1][2]["prompt"]
    assert "Disability Studies Quarterly" in prompt and "Keep British spelling" in prompt


def test_suggest_ai_error_is_shown_on_job_page(user_client, fake_ai):
    doc_id = create_doc(user_client)
    save(user_client, doc_id, BODY)
    fake_ai.on("write.suggest", AIError("The AI is busy right now (rate limit). Please try again in a minute."))
    r = post(user_client, f"/write/{doc_id}/suggest", {"goal": "spelling"})
    assert "This did not finish" in r.text and "rate limit" in r.text


def test_in_context():
    body = "word " * 30 + "teh target " + "more " * 30
    ctx = write_mod.in_context(body, "teh")
    assert ctx.startswith("...") and ctx.endswith("...") and "teh target" in ctx
    assert ctx.startswith("...word ") and ctx.endswith(" more...")
    assert write_mod.in_context("teh", "teh") == ""
    assert write_mod.in_context("abc", "zzz") == ""


def test_filter_suggestions_unit():
    body = "one two three two"
    kept, dropped = write_mod.filter_suggestions(body, [
        {"original": "three", "proposed": "3", "reason": "a"},
        {"original": "one", "proposed": "1", "reason": "b"},
        {"original": "", "proposed": "x", "reason": "empty"},
        {"original": "four", "proposed": "4", "reason": "missing"},
        {"original": "one", "proposed": "1", "reason": "duplicate"},
    ])
    assert [k["original"] for k in kept] == ["one", "three"]
    assert dropped == 3


# ----- export ------------------------------------------------------------------------------------

EXPORT_BODY = "# Methods\n\nWe paid $5 & saved 10% on item #3_b {x}.\n\n## Results\n\n- first point\n- second point\n\nA <script>alert(1)</script> tag."


def test_export_page_lists_formats(user_client):
    doc_id = create_doc(user_client, "Cost & value")
    page = user_client.get(f"/write/{doc_id}/export")
    assert h1(page.text) == "Export Cost &amp; value"
    for ext in ["docx", "md", "txt", "html", "tex"]:
        assert f'href="/write/{doc_id}/export.{ext}"' in page.text
    assert not check_html(page.text).errors


def test_export_markdown_and_text(user_client):
    doc_id = create_doc(user_client, "Cost study")
    save(user_client, doc_id, EXPORT_BODY, title="Cost study")
    md = user_client.get(f"/write/{doc_id}/export.md")
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
    assert 'attachment; filename="cost-study.md"' == md.headers["content-disposition"]
    assert md.text.startswith("# Cost study\n\n# Methods")
    txt = user_client.get(f"/write/{doc_id}/export.txt")
    assert txt.headers["content-type"].startswith("text/plain")
    assert txt.text.startswith("Cost study\n\n") and "We paid $5" in txt.text


def test_export_skips_repeated_title_heading(user_client):
    doc_id = create_doc(user_client, "Cost study")
    save(user_client, doc_id, "# Cost study\n\nBody text.", title="Cost study")
    assert user_client.get(f"/write/{doc_id}/export.md").text == "# Cost study\n\nBody text.\n"


def test_export_html_is_complete_and_accessible(user_client):
    doc_id = create_doc(user_client, "Cost study")
    save(user_client, doc_id, EXPORT_BODY, title="Cost study")
    r = user_client.get(f"/write/{doc_id}/export.html")
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["content-disposition"].startswith("attachment")
    html = r.text
    assert html.startswith("<!doctype html>") and '<html lang="en">' in html
    assert "<title>Cost study</title>" in html
    assert h1(html) == "Cost study"
    assert "<h2>Methods</h2>" in html and "<h3>Results</h3>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    report = check_html(html)
    assert not report.errors, report.findings


def test_export_docx_parses_with_headings(user_client):
    from docx import Document
    doc_id = create_doc(user_client, "Cost study")
    save(user_client, doc_id, EXPORT_BODY, title="Cost study")
    r = user_client.get(f"/write/{doc_id}/export.docx")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    document = Document(io.BytesIO(r.content))
    paragraphs = [(p.style.name, p.text) for p in document.paragraphs]
    assert paragraphs[0] == ("Heading 1", "Cost study")
    assert ("Heading 2", "Methods") in paragraphs and ("Heading 3", "Results") in paragraphs
    assert ("List Bullet", "first point") in paragraphs
    assert any(t == "We paid $5 & saved 10% on item #3_b {x}." for _, t in paragraphs)
    assert not any(t.startswith("#") for _, t in paragraphs)
    assert document.core_properties.title == "Cost study"


def test_export_tex_escapes_special_characters(user_client):
    doc_id = create_doc(user_client, "Cost & 100% study_1")
    save(user_client, doc_id, EXPORT_BODY, title="Cost & 100% study_1")
    r = user_client.get(f"/write/{doc_id}/export.tex")
    assert r.status_code == 200 and r.headers["content-disposition"].endswith('.tex"')
    tex = r.text
    assert tex.startswith("\\documentclass{article}") and tex.rstrip().endswith("\\end{document}")
    assert "\\title{Cost \\& 100\\% study\\_1}" in tex
    assert "\\section{Methods}" in tex and "\\subsection{Results}" in tex
    assert "We paid \\$5 \\& saved 10\\% on item \\#3\\_b \\{x\\}." in tex
    assert write_mod.latex_escape("a\\b~c^d") == "a\\textbackslash{}b\\textasciitilde{}c\\textasciicircum{}d"
    body = tex.split("\\maketitle", 1)[1]
    assert "$5" not in body.replace("\\$5", "") and "#3" not in body.replace("\\#3", "")


def test_export_empty_document(user_client):
    doc_id = create_doc(user_client, "Empty")
    from docx import Document
    assert Document(io.BytesIO(user_client.get(f"/write/{doc_id}/export.docx").content)).paragraphs[0].text == "Empty"
    assert "\\begin{document}" in user_client.get(f"/write/{doc_id}/export.tex").text
    assert check_html(user_client.get(f"/write/{doc_id}/export.html").text).errors == []


# ----- where to publish and cover letters --------------------------------------------------------

def test_venues_research_with_sources(user_client, fake_ai):
    doc_id = create_doc(user_client, "Screen readers in class", "paper")
    save(user_client, doc_id, BODY, title="Screen readers in class")
    fake_ai.on("write.venues", ResearchResult(
        text="Summary: two good fits.\n\n# Journal of Examples\n\nNo fee found. Nothing found about screen reader access.",
        sources=[Source(url="https://example.org/guidelines", title="Author guidelines", quote="Submit online."),
                 Source(url="javascript:alert(1)", title="Bad link")]))
    r = post(user_client, f"/write/{doc_id}/venues")
    assert r.status_code == 200
    assert h1(r.text) == "Where you could publish Screen readers in class"
    assert "<h2>Journal of Examples</h2>" in r.text
    assert 'href="https://example.org/guidelines" rel="noopener noreferrer"' in r.text
    assert "javascript:" not in r.text
    assert "Sources (1)" in r.text and "has not checked it" in r.text and "Written by the AI" in r.text
    kind, purpose, kwargs = fake_ai.calls[-1]
    assert kind == "research" and purpose == "write.venues"
    assert "open access fee" in kwargs["prompt"] and "screen reader" in kwargs["prompt"]
    assert not check_html(r.text).errors
    with db.session() as conn:
        rid = conn.execute("SELECT id FROM write_research WHERE doc_id = ?", (doc_id,)).fetchone()["id"]
    assert f'href="/write/{doc_id}/publish/{rid}"' in user_client.get(f"/write/{doc_id}").text


def test_venues_without_sources_warns(user_client, fake_ai):
    doc_id = create_doc(user_client)
    save(user_client, doc_id, BODY)
    fake_ai.on("write.venues", ResearchResult(text="Nothing reliable found.", sources=[]))
    r = post(user_client, f"/write/{doc_id}/venues")
    assert "did not list any sources" in r.text


def test_venues_needs_text(user_client, fake_ai):
    doc_id = create_doc(user_client)
    r = post(user_client, f"/write/{doc_id}/venues")
    assert "no saved text yet" in r.text and fake_ai.calls == []


def test_cover_letter_creates_new_document(user_client, fake_ai):
    doc_id = create_doc(user_client, "Screen readers in class", "paper", "Journal of Examples")
    save(user_client, doc_id, BODY, title="Screen readers in class", target="Journal of Examples")
    page = user_client.get(f"/write/{doc_id}")
    assert "Draft a cover letter for Journal of Examples" in page.text
    fake_ai.on("write.cover_letter", "Dear [Editor's name],\n\nI am submitting my paper.\n\n[Your name]")
    r = post(user_client, f"/write/{doc_id}/cover-letter", {"notes": "I teach braille."}, follow_redirects=False)
    job = user_client.get(r.headers["location"], follow_redirects=False)
    new_url = job.headers["location"]
    new_id = int(new_url.rsplit("/", 1)[1])
    assert new_id != doc_id
    with db.session() as conn:
        new = dict(conn.execute("SELECT * FROM write_docs WHERE id = ?", (new_id,)).fetchone())
    assert new["kind"] == "letter" and new["title"] == "Cover letter: Screen readers in class"
    assert new["target"] == "Journal of Examples" and new["body"].startswith("Dear [Editor's name]")
    assert [(v["note"], v["body"]) for v in versions_of(new_id)] == [("Drafted by the AI", new["body"])]
    page = user_client.get(new_url)
    assert h1(page.text) == "Cover letter: Screen readers in class"
    assert "Drafted by the AI" in page.text and "The AI wrote this draft" in page.text
    kind, purpose, kwargs = fake_ai.calls[-1]
    assert kind == "text" and purpose == "write.cover_letter"
    assert "Journal of Examples" in kwargs["prompt"] and "I teach braille." in kwargs["prompt"]
    # The original is unchanged
    assert body_of(doc_id) == BODY


def test_cover_letter_needs_target(user_client, fake_ai):
    doc_id = create_doc(user_client)
    save(user_client, doc_id, BODY)
    page = user_client.get(f"/write/{doc_id}")
    assert "first fill in" in page.text and 'action="/write/%d/cover-letter"' % doc_id not in page.text
    r = post(user_client, f"/write/{doc_id}/cover-letter", {"notes": ""})
    assert r.status_code == 400 and "Name the publication" in r.text
    assert 'href="#field-notes"' in r.text and 'id="field-notes"' in r.text
    assert fake_ai.calls == []


# ----- deleting ----------------------------------------------------------------------------------

def test_delete_document(user_client, fake_ai):
    doc_id = create_doc(user_client, "Throwaway")
    save(user_client, doc_id, BODY, title="Throwaway")
    ask_suggestions(user_client, doc_id, fake_ai, [{"original": "teh", "proposed": "the", "reason": "Spelling."}])
    page = user_client.get(f"/write/{doc_id}/delete")
    assert h1(page.text) == "Delete Throwaway?" and "cannot be undone" in page.text
    r = post(user_client, f"/write/{doc_id}/delete")
    assert r.status_code == 200 and "Deleted" in r.text
    assert user_client.get(f"/write/{doc_id}").status_code == 404
    with db.session() as conn:
        for table in ["write_versions", "write_suggestions", "write_batches", "write_research"]:
            assert conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0


# ----- ownership ---------------------------------------------------------------------------------

def test_other_members_get_404_everywhere(client, fake_ai):
    register(client, "owner@example.com", "Owner")
    doc_id = create_doc(client, target="Some Journal")
    save(client, doc_id, BODY, target="Some Journal")
    batch_id = ask_suggestions(client, doc_id, fake_ai, [{"original": "teh", "proposed": "the", "reason": "Spelling."}])
    sid = suggestion_rows(batch_id)[0]["id"]
    vid = versions_of(doc_id)[0]["id"]
    fake_ai.on("write.venues", ResearchResult(text="x", sources=[]))
    post(client, f"/write/{doc_id}/venues")
    with db.session() as conn:
        rid = conn.execute("SELECT id FROM write_research").fetchone()["id"]
    post(client, "/logout")
    register(client, "other@example.com", "Other")
    calls_before = len(fake_ai.calls)
    gets = ["", "/versions", f"/versions/{vid}", "/export", "/export.md", "/export.txt", "/export.html",
            "/export.docx", "/export.tex", f"/suggestions/{batch_id}", f"/publish/{rid}", "/delete"]
    for suffix in gets:
        r = client.get(f"/write/{doc_id}{suffix}")
        assert r.status_code == 404, suffix
    posts = [("/save", {"body": "hacked", "title": "x"}), ("/suggest", {"goal": "spelling"}), ("/venues", {}),
             ("/cover-letter", {}), (f"/versions/{vid}/restore", {}), (f"/suggestions/{batch_id}/{sid}/accept", {}),
             (f"/suggestions/{batch_id}/{sid}/reject", {}), (f"/suggestions/{batch_id}/accept-all", {}),
             (f"/suggestions/{batch_id}/reject-all", {}), ("/delete", {})]
    for suffix, data in posts:
        r = post(client, f"/write/{doc_id}{suffix}", data)
        assert r.status_code == 404, suffix
    assert len(fake_ai.calls) == calls_before
    assert body_of(doc_id) == BODY
    assert suggestion_rows(batch_id)[0]["status"] == "pending"
    assert "My essay" not in client.get("/write").text


def test_suggestion_and_version_ids_must_belong_to_the_document(user_client, fake_ai):
    a = create_doc(user_client, "A")
    b = create_doc(user_client, "B")
    save(user_client, a, BODY, title="A")
    save(user_client, b, BODY, title="B")
    batch_a = ask_suggestions(user_client, a, fake_ai, [{"original": "teh", "proposed": "the", "reason": "Spelling."}])
    sid = suggestion_rows(batch_a)[0]["id"]
    vid_a = versions_of(a)[0]["id"]
    assert user_client.get(f"/write/{b}/suggestions/{batch_a}").status_code == 404
    assert post(user_client, f"/write/{b}/suggestions/{batch_a}/{sid}/accept").status_code == 404
    assert user_client.get(f"/write/{b}/versions/{vid_a}").status_code == 404
    assert post(user_client, f"/write/{b}/versions/{vid_a}/restore").status_code == 404
    assert user_client.get("/write/999999").status_code == 404
    assert body_of(a) == BODY and body_of(b) == BODY


# ----- no AI available ---------------------------------------------------------------------------

def test_no_ai_explains_what_to_do(no_ai_app):
    client = TestClient(no_ai_app)
    register(client)
    doc_id = create_doc(client, target="Some Journal")
    save(client, doc_id, BODY, target="Some Journal")
    for url, data in [(f"/write/{doc_id}/suggest", {"goal": "spelling"}), (f"/write/{doc_id}/venues", {}),
                      (f"/write/{doc_id}/cover-letter", {"notes": ""})]:
        r = post(client, url, data, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == f"/write/{doc_id}"
        page = client.get(f"/write/{doc_id}")
        assert "connect your own Claude API key" in page.text
    # Writing, versions and export still work without the AI
    assert client.get(f"/write/{doc_id}/export.docx").status_code == 200
    assert len(versions_of(doc_id)) == 1
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 0


# ----- every page passes the accessibility checker -----------------------------------------------

def test_all_write_pages_pass_checks(user_client, fake_ai):
    doc_id = create_doc(user_client, "Checked paper", "paper", "Some Journal")
    save(user_client, doc_id, BODY, title="Checked paper", target="Some Journal")
    save(user_client, doc_id, BODY + "\n\nMore.", title="Checked paper", target="Some Journal")
    batch_id = ask_suggestions(user_client, doc_id, fake_ai, [
        {"original": "teh", "proposed": "the", "reason": "Spelling."},
        {"original": "It was happy.", "proposed": "", "reason": "Not needed."},
        {"original": "missing words", "proposed": "x", "reason": "Dropped."},
    ], note="# Overall\n\nGood work.")
    first = suggestion_rows(batch_id)[0]
    post(user_client, f"/write/{doc_id}/suggestions/{batch_id}/{first['id']}/accept")
    fake_ai.on("write.venues", ResearchResult(text="# Place\n\nDetails.", sources=[Source(url="https://example.org", title="Ex")]))
    post(user_client, f"/write/{doc_id}/venues")
    with db.session() as conn:
        rid = conn.execute("SELECT id FROM write_research").fetchone()["id"]
    vid = versions_of(doc_id)[0]["id"]
    paths = ["/write", "/write/new", f"/write/{doc_id}", f"/write/{doc_id}/versions", f"/write/{doc_id}/versions/{vid}",
             f"/write/{doc_id}/export", f"/write/{doc_id}/suggestions/{batch_id}", f"/write/{doc_id}/publish/{rid}",
             f"/write/{doc_id}/delete"]
    problems = []
    for path in paths:
        r = user_client.get(path)
        assert r.status_code == 200, path
        h1(r.text)
        for f in check_html(r.text, name=path).findings:
            problems.append(f"{path}: {f.severity} {f.rule}: {f.message} {f.where}")
    assert not problems, "\n".join(problems)
    # Every page is reachable from the section index
    doc_page = user_client.get(f"/write/{doc_id}").text
    for path in paths[3:]:
        if path in (f"/write/{doc_id}/versions/{vid}",):
            assert path in user_client.get(f"/write/{doc_id}/versions").text
        else:
            assert f'href="{path}"' in doc_page, path
    assert f'href="/write/{doc_id}"' in user_client.get("/write").text


def test_validation_error_pages_pass_checks(user_client):
    r = post(user_client, "/write/new", {"title": "", "kind": ""})
    assert not check_html(r.text).errors
    doc_id = create_doc(user_client)
    r = post(user_client, f"/write/{doc_id}/suggest", {"goal": "spelling"})
    assert not check_html(r.text).errors
    r = post(user_client, f"/write/{doc_id}/cover-letter", {})
    assert not check_html(r.text).errors


@pytest.mark.parametrize("title,expected", [("Cost & value!", "cost-value"), ("***", "document"), ("Été 2026", "ete-2026")])
def test_slug(title, expected):
    assert write_mod._slug(title) == expected
