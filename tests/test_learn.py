"""Learn and help: guides, and Ask the community with member answers and one labelled AI answer."""
import re
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from studio import db
from studio.a11y_check import check_html
from studio.ai import AIError
from studio.features import learn

from .conftest import post, register, user_id

GUIDE_SLUGS = ["start-here", "first-website", "ai-coding-in-the-terminal", "apps-on-your-phone",
               "publishing-papers", "safe-web-tasks", "charts-and-data"]
AI_LABEL = "Suggested by the AI. Not checked by a member."


def h1s(html: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", h).strip() for h in re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.S)]


def page_title(html: str) -> str:
    return re.search(r"<title>(.*?) – ", html, re.S).group(1).strip()


def ask(client, title="NVDA does not read my preview", body="When I open the preview, NVDA says nothing.", **extra):
    data = {"title": title, "body": body, "screen_reader": "NVDA", "device": "Windows laptop"}
    data.update(extra)
    r = post(client, "/help/ask", data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


def switch_user(client, email, name):
    post(client, "/logout")
    register(client, email, name)


def answers_for(question_id):
    with db.session() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM help_answers WHERE question_id = ? ORDER BY id",
                                              (question_id,)).fetchall()]


def question_row(question_id):
    with db.session() as conn:
        return dict(conn.execute("SELECT * FROM help_questions WHERE id = ?", (question_id,)).fetchone())


# ----- guides ---------------------------------------------------------------------------------------

def test_all_guides_load_in_order_with_front_matter(app):
    guides = learn.load_guides()
    assert [g.slug for g in guides] == GUIDE_SLUGS
    for g in guides:
        assert g.title and g.summary
        assert g.last_checked == "2026-10"
        assert g.last_checked_words == "October 2026"
        assert "\n# " not in "\n" + g.body


def test_learn_index_lists_guides_and_links_to_help(client):
    r = client.get("/learn")
    assert r.status_code == 200
    assert h1s(r.text) == ["Learn and help"] and page_title(r.text) == "Learn and help"
    for g in learn.load_guides():
        assert f'href="/learn/{g.slug}"' in r.text
        assert g.summary.replace("'", "&#39;") in r.text
    assert 'href="/help"' in r.text and 'href="/help/ask"' in r.text
    assert not check_html(r.text).errors


@pytest.mark.parametrize("slug", GUIDE_SLUGS)
def test_every_guide_renders_with_one_h1_and_passes_the_checker(client, slug):
    guide = learn.get_guide(slug)
    r = client.get(f"/learn/{slug}")
    assert r.status_code == 200
    assert h1s(r.text) == [guide.title.replace("'", "&#39;")]
    assert page_title(r.text) == guide.title.replace("'", "&#39;")
    assert "Last checked October 2026" in r.text
    assert f'href="/help/ask?title={quote(guide.title)}"' in r.text
    report = check_html(r.text, name=slug)
    assert not report.errors, report.errors
    assert not [f for f in report.warnings if f.rule in ("heading-order", "page-weight")], report.warnings
    # every external link opens safely
    for tag in re.findall(r'<a href="https?://[^"]*"[^>]*>', r.text):
        assert 'rel="noopener noreferrer"' in tag, tag


def test_guide_ask_link_prefills_the_question_title(user_client):
    guide = learn.get_guide("safe-web-tasks")
    r = user_client.get(f"/help/ask?title={quote(guide.title)}")
    assert f'value="{guide.title}"' in r.text


def test_unknown_guide_is_404(client):
    assert client.get("/learn/no-such-guide").status_code == 404
    assert client.get("/learn/Start-Here").status_code == 404
    assert client.get("/learn/..%2F..%2Fapp").status_code == 404
    assert learn.get_guide("../features/learn") is None


def test_front_matter_parser():
    g = learn.parse_guide("demo", "---\ntitle: A: guide\nsummary: 'Short'\nlast_checked: 2026-10\norder: 3\n---\n\n## Part\n\nText\n")
    assert (g.title, g.summary, g.last_checked, g.order) == ("A: guide", "Short", "2026-10", 3)
    assert g.body.startswith("## Part")
    # an h1 inside a code block is fine; a real one is not
    learn.parse_guide("demo", "---\ntitle: T\nsummary: S\nlast_checked: 2026-10\n---\n```\n# comment\n```\n")
    bad = [
        "no front matter\n",
        "---\ntitle: T\nsummary: S\n",  # not closed
        "---\ntitle: T\nlast_checked: 2026-10\n---\nBody\n",  # no summary
        "---\ntitle: T\nsummary: S\nlast_checked: 2026-10\n---\n# Big heading\n",
        "---\ntitle: T\nsummary: S\nlast_checked: 2026-10\norder: first\n---\nBody\n",
        "---\ntitle T\n---\nBody\n",
    ]
    for text in bad:
        with pytest.raises(learn.GuideError):
            learn.parse_guide("demo", text)


def test_last_checked_words_falls_back_to_the_raw_value():
    g = learn.parse_guide("d", "---\ntitle: T\nsummary: S\nlast_checked: autumn\n---\nBody\n")
    assert g.last_checked_words == "autumn"


# ----- refusing secrets ------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "my password is hunter22",
    "Password: Tr0ub4dor&3",
    "The verification code is 482913",
    "OTP: 1234",
    "card 4111 1111 1111 1111 please",
    "key sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
])
def test_secrets_are_recognised(text):
    assert learn.looks_secret(text)


@pytest.mark.parametrize("text", [
    "My password is not accepted on the sign-in page",
    "the password is wrong every time",
    "Windows installer error code 1603",
    "It happened in 2026 and again on 2026-10-12",
    "Order number 1234567890123",
    "I have 1111111111111111 problems",
])
def test_ordinary_text_is_not_mistaken_for_a_secret(text):
    assert not learn.looks_secret(text)


# ----- asking ------------------------------------------------------------------------------------------

def test_ask_form_is_prefilled_from_the_profile(user_client):
    r = user_client.get("/help/ask?title=Help%20with%20Build")
    assert r.status_code == 200
    assert h1s(r.text) == ["Ask a question"]
    assert 'value="Help with Build"' in r.text
    assert 'value="NVDA"' in r.text and 'value="Windows laptop"' in r.text
    assert "Never post passwords or codes." in r.text
    assert not check_html(r.text).errors


def test_ask_validation_errors(user_client):
    r = post(user_client, "/help/ask", {"title": "", "body": "", "screen_reader": "x" * 101, "device": ""})
    assert r.status_code == 400
    assert page_title(r.text) == "Ask a question: please fix the problems"
    for name in ("title", "body", "screen_reader"):
        assert f'href="#field-{name}"' in r.text
    r = post(user_client, "/help/ask", {"title": "t" * 201, "body": "b" * 10_001})
    assert r.status_code == 400 and "at most 200 characters" in r.text and "at most 10,000 characters" in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM help_questions").fetchone()[0] == 0


def test_ask_refuses_passwords_and_codes_and_keeps_the_text(user_client):
    r = post(user_client, "/help/ask", {"title": "Sign in trouble", "body": "My password is Secret123 and it fails"})
    assert r.status_code == 400
    assert "looks like it contains a password" in r.text
    assert 'value="Sign in trouble"' in r.text  # the rest of the form is kept
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM help_questions").fetchone()[0] == 0


def test_question_journey_answer_helpful_once_and_mark_answered(client):
    register(client, "amina@example.com", "Amina")
    qid = ask(client)
    page = client.get(f"/help/q/{qid}")
    assert h1s(page.text) == ["NVDA does not read my preview"]
    assert "Not answered yet. 0 answers from members" in page.text
    assert "Get a suggested answer from the AI while you wait" in page.text
    assert "Mark as answered" in page.text
    assert not check_html(page.text).errors

    switch_user(client, "bo@example.com", "Bo")
    page = client.get(f"/help/q/{qid}")
    assert "Get a suggested answer from the AI" not in page.text and "Mark as answered" not in page.text
    r = post(client, f"/help/q/{qid}/answer", {"body": "Press **NVDA+Space** to switch to browse mode."})
    assert r.status_code == 200 and "Your answer is posted" in r.text
    assert "Answer from Bo" in r.text and "<strong>NVDA+Space</strong>" in r.text
    answer_id = answers_for(qid)[0]["id"]
    assert "This helped me" not in r.text  # not on your own answer
    assert post(client, f"/help/q/{qid}/answers/{answer_id}/helpful").status_code == 400

    switch_user(client, "cy@example.com", "Cy")
    r = post(client, f"/help/q/{qid}/answers/{answer_id}/helpful")
    assert "You marked this answer as helpful" in r.text and "1 member found this helpful" in r.text
    r = post(client, f"/help/q/{qid}/answers/{answer_id}/helpful")
    assert "You already marked this answer as helpful" in r.text
    assert answers_for(qid)[0]["helpful_count"] == 1

    post(client, "/logout")
    post(client, "/login", {"email": "amina@example.com", "password": "correct horse battery"})
    r = post(client, f"/help/q/{qid}/answers/{answer_id}/helpful")
    assert answers_for(qid)[0]["helpful_count"] == 2
    r = post(client, f"/help/q/{qid}/answered")
    assert "Your question is marked as answered" in r.text
    assert question_row(qid)["status"] == "answered"
    assert "Mark as not answered yet" in r.text
    r = post(client, f"/help/q/{qid}/reopen")
    assert question_row(qid)["status"] == "open"


def test_answer_validation_and_missing_question(user_client):
    qid = ask(user_client)
    r = post(user_client, f"/help/q/{qid}/answer", {"body": "   "})
    assert r.status_code == 400
    assert page_title(r.text).endswith(": please fix the problems")
    assert 'href="#field-body"' in r.text
    r = post(user_client, f"/help/q/{qid}/answer", {"body": "Try this code: 482913 is my one-time code is 482913"})
    assert r.status_code == 400 and "looks like it contains" in r.text
    assert answers_for(qid) == []
    assert user_client.get("/help/q/999").status_code == 404
    assert post(user_client, "/help/q/999/answer", {"body": "Hello"}).status_code == 404
    assert post(user_client, f"/help/q/{qid}/answers/999/helpful").status_code == 404


def test_helpful_needs_the_answer_to_belong_to_the_question(client):
    register(client, "a@example.com", "A")
    q1 = ask(client, title="First")
    q2 = ask(client, title="Second")
    switch_user(client, "b@example.com", "B")
    post(client, f"/help/q/{q1}/answer", {"body": "An answer"})
    answer_id = answers_for(q1)[0]["id"]
    switch_user(client, "c@example.com", "C")
    assert post(client, f"/help/q/{q2}/answers/{answer_id}/helpful").status_code == 404


def test_cannot_mark_another_members_question_answered(client):
    register(client, "a@example.com", "A")
    qid = ask(client)
    switch_user(client, "b@example.com", "B")
    assert post(client, f"/help/q/{qid}/answered").status_code == 404
    assert question_row(qid)["status"] == "open"
    post(client, "/logout")
    post(client, "/login", {"email": "a@example.com", "password": "correct horse battery"})
    post(client, f"/help/q/{qid}/answered")
    switch_user(client, "c@example.com", "C")
    assert post(client, f"/help/q/{qid}/reopen").status_code == 404
    assert question_row(qid)["status"] == "answered"


def test_member_text_is_escaped(client):
    register(client)
    qid = ask(client, body="<script>alert(1)</script> and [a link](https://example.com)")
    r = client.get(f"/help/q/{qid}")
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text
    assert '<a href="https://example.com" rel="noopener noreferrer">a link</a>' in r.text


def test_rendered_member_text_is_safe_and_structured():
    html = str(learn.render_text('[t](https://x.example "Title") [j](javascript:alert(1))\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n# Top', shift=2))
    assert '<a href="https://x.example" rel="noopener noreferrer" title="Title">' in html
    assert "javascript:" not in html.split("[j]")[0] and 'href="javascript' not in html
    assert '<th scope="col">A</th>' in html and "<th>" not in html
    assert "<h3>Top</h3>" in html and "<h1>" not in html


# ----- the AI's suggested answer --------------------------------------------------------------------

def test_ai_answer_is_labelled_limited_to_one_and_fits_the_asker(user_client, fake_ai):
    fake_ai.on("help.ai_answer", "Switch to browse mode first.\n\n## Steps\n\n1. Press NVDA+Space.")
    qid = ask(user_client, screen_reader="JAWS 2026", device="Android phone")
    r = post(user_client, f"/help/q/{qid}/ai")
    assert r.status_code == 200
    assert r.url.path == f"/help/q/{qid}"
    assert AI_LABEL in r.text and "Suggested answer from the AI" in r.text
    assert "and a suggested answer from the AI" in r.text
    assert "<h4>Steps</h4>" in r.text  # its headings sit under the answer's own heading
    assert "Get a suggested answer from the AI while you wait" not in r.text
    assert not check_html(r.text).errors
    answers = answers_for(qid)
    assert len(answers) == 1 and answers[0]["is_ai"] == 1 and answers[0]["user_id"] == user_id()

    kind, purpose, kwargs = fake_ai.calls[0]
    assert (kind, purpose) == ("text", "help.ai_answer")
    system = kwargs["system"]
    assert "JAWS 2026" in system and "Android phone" in system
    assert "Build (/build)" in system and "Ask the community (/help)" in system and "Your account (/account)" in system
    assert "Start here: using this site with a screen reader (/learn/start-here)" in system
    assert "blind or has low vision and uses a screen reader" in system
    assert "No tables" in system and "No emoji" in system and "Never claim something was checked or tested" in system
    assert "NVDA does not read my preview" in kwargs["prompt"]

    r = post(user_client, f"/help/q/{qid}/ai")
    assert "already has a suggested answer from the AI" in r.text
    assert len(fake_ai.calls) == 1 and len(answers_for(qid)) == 1


def test_the_asker_can_mark_the_ai_answer_helpful(user_client, fake_ai):
    fake_ai.on("help.ai_answer", "Try browse mode.")
    qid = ask(user_client)
    post(user_client, f"/help/q/{qid}/ai")
    answer_id = answers_for(qid)[0]["id"]
    r = post(user_client, f"/help/q/{qid}/answers/{answer_id}/helpful")
    assert r.status_code == 200 and answers_for(qid)[0]["helpful_count"] == 1


def test_only_one_ai_answer_even_if_saved_twice(user_client, fake_ai):
    fake_ai.on("help.ai_answer", "One")
    qid = ask(user_client)

    class Ctx:
        def progress(self, text):
            pass

    learn._write_ai_answer(Ctx(), fake_ai, qid, user_id(), "s", "p")
    with db.session() as conn:  # a second save racing past the first check is stopped by the index
        with pytest.raises(Exception):
            conn.execute("INSERT INTO help_answers (question_id, user_id, body, is_ai, created_at)"
                         " VALUES (?, ?, 'Two', 1, 'now')", (qid, user_id()))
    assert learn._write_ai_answer(Ctx(), fake_ai, qid, user_id(), "s", "p") == f"/help/q/{qid}#answers"
    assert len(answers_for(qid)) == 1 and len(fake_ai.calls) == 1


def test_only_the_asker_can_ask_the_ai(client, fake_ai):
    fake_ai.on("help.ai_answer", "Answer")
    register(client, "a@example.com", "A")
    qid = ask(client)
    switch_user(client, "b@example.com", "B")
    assert post(client, f"/help/q/{qid}/ai").status_code == 404
    assert fake_ai.calls == [] and answers_for(qid) == []


def test_ai_failure_is_shown_plainly_and_saves_nothing(user_client, fake_ai):
    fake_ai.on("help.ai_answer", AIError("The AI is busy right now (rate limit). Please try again in a minute."))
    qid = ask(user_client)
    r = post(user_client, f"/help/q/{qid}/ai")
    assert "This did not finish" in r.text and "The AI is busy right now" in r.text
    assert answers_for(qid) == []
    fake_ai.on("help.ai_answer", "   ")
    r = post(user_client, f"/help/q/{qid}/ai")
    assert "The AI did not write an answer" in r.text
    assert answers_for(qid) == []


def test_without_ai_the_asker_is_told_why_and_members_can_still_answer(no_ai_app):
    client = TestClient(no_ai_app)
    register(client)
    qid = ask(client)
    r = post(client, f"/help/q/{qid}/ai")
    assert r.status_code == 200 and r.url.path == f"/help/q/{qid}"
    assert "connect your own Claude API key" in r.text and "Members can still answer your question." in r.text
    assert answers_for(qid) == []
    r = post(client, f"/help/q/{qid}/answer", {"body": "Following up: it works in Firefox."})
    assert "Your answer is posted" in r.text


# ----- browsing and signed-out visitors --------------------------------------------------------------

def test_help_lists_questions_with_counts_and_filters(client):
    register(client, "a@example.com", "Amina")
    q1 = ask(client, title="First question")
    q2 = ask(client, title="Second question")
    switch_user(client, "b@example.com", "Bo")
    post(client, f"/help/q/{q1}/answer", {"body": "Here is how."})
    post(client, "/logout")
    post(client, "/login", {"email": "a@example.com", "password": "correct horse battery"})
    post(client, f"/help/q/{q1}/answered")

    r = client.get("/help")
    assert h1s(r.text) == ["Ask the community"] and page_title(r.text) == "Ask the community"
    assert 'href="/help/ask"' in r.text and ">Ask a question<" in r.text
    assert '<a href="/help" aria-current="page">Newest</a>' in r.text
    assert 'href="/help?show=open"' in r.text and "Not answered yet</a>" in r.text
    assert r.text.index("Second question") < r.text.index("First question")
    assert "1 answer from members. Answered." in r.text
    assert "0 answers from members. Not answered yet." in r.text
    assert "Asked by Amina" in r.text
    assert not check_html(r.text).errors

    r = client.get("/help?show=open")
    assert '<a href="/help?show=open" aria-current="page">' in r.text
    assert "Second question" in r.text and "First question" not in r.text
    assert f'href="/help/q/{q2}"' in r.text


def test_help_pages_through_older_questions(user_client):
    uid = user_id()
    with db.session() as conn:
        for i in range(learn.PAGE_SIZE + 2):
            conn.execute("INSERT INTO help_questions (user_id, title, body, created_at) VALUES (?, ?, 'b', ?)",
                         (uid, f"Question number {i}", db.now()))
    r = user_client.get("/help")
    assert 'href="/help?page=2"' in r.text and "Question number 0<" not in r.text
    r = user_client.get("/help?page=2")
    assert "Question number 0<" in r.text and 'href="/help?page=1"' in r.text
    assert "Older questions" not in r.text


def test_empty_help_pages(client):
    assert "No questions yet" in client.get("/help").text
    assert "Every question has been marked as answered" in client.get("/help?show=open").text


def test_signed_out_visitors_can_read_but_not_post(client, fake_ai):
    fake_ai.on("help.ai_answer", "Answer")
    register(client)
    qid = ask(client)
    post(client, f"/help/q/{qid}/answer", {"body": "More detail from me."})
    answer_id = answers_for(qid)[0]["id"]
    post(client, "/logout")

    assert "NVDA does not read my preview" in client.get("/help").text
    r = client.get(f"/help/q/{qid}")
    assert r.status_code == 200 and "More detail from me." in r.text
    assert f'href="/login?next=/help/q/{qid}"' in r.text
    assert "This helped me" not in r.text and "Post your answer" not in r.text
    assert not check_html(r.text).errors

    r = client.get("/help/ask", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    for url, data in [("/help/ask", {"title": "T", "body": "B"}), (f"/help/q/{qid}/answer", {"body": "B"}),
                      (f"/help/q/{qid}/answers/{answer_id}/helpful", {}), (f"/help/q/{qid}/answered", {}),
                      (f"/help/q/{qid}/ai", {})]:
        r = post(client, url, data, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), url
    assert len(answers_for(qid)) == 1 and question_row(qid)["status"] == "open" and fake_ai.calls == []


def test_posting_needs_the_form_token(user_client):
    qid = ask(user_client)
    r = user_client.post(f"/help/q/{qid}/answer", data={"body": "No token"})
    assert r.status_code == 400 and answers_for(qid) == []


def test_the_askers_own_replies_are_not_counted_as_answers(user_client):
    qid = ask(user_client)
    r = post(user_client, f"/help/q/{qid}/answer", {"body": "It also happens in Edge."})
    assert "Reply from Amina, who asked" in r.text
    assert "Not answered yet. 0 answers from members." in r.text
    assert "0 answers from members. Not answered yet." in user_client.get("/help").text
