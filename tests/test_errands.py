"""Do this for me: find errands, task errands, the read-back contract, secret refusal, ownership and no-AI path."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from studio import db
from studio.a11y_check import check_html
from studio.ai import AIError, ResearchResult, Source
from studio.features import errands

from .conftest import post, register, user_id

CONTRACT = {
    "goal": "Find the opening hours of the central library",
    "steps": ["Search the library's own website", "Bring back the hours with links"],
    "will_not_do": ["Guess hours that are not published"],
    "done_when": "You have this week's opening hours and a link to check them.",
    "questions": ["Which city is the library in?"],
    "information_needed_from_you": [],
}
FIND_ANSWER = ResearchResult(
    text=("The central library is open 9am to 5pm, Monday to Saturday.\n\n"
          "# Opening hours\n\nClosed on Sundays.\n\n## Holidays\n\nClosed on public holidays.\n\n"
          "# What I could not confirm\n\nWhether hours change in summer. <script>alert(1)</script>"),
    sources=[Source(url="https://library.example.org/hours", title="Library opening hours", quote="Open 9 to 5"),
             Source(url="javascript:alert(1)", title="Bad link")],
)
TASK_CONTRACT = {
    "goal": "Renew my library card online",
    "steps": ["Find the official renewal page", "Prepare the steps and answers"],
    "will_not_do": ["Log in for you", "Pay the fee", "Submit the form"],
    "done_when": "You have the steps and answers to renew your card yourself.",
    "questions": [],
    "information_needed_from_you": ["Your full name", "Your library card number"],
}
TASK_RESEARCH = ResearchResult(
    text="Renew at the official library site. You need your card number.",
    sources=[Source(url="https://library.example.org/renew", title="Renew your card")],
)
PREPARED = {
    "where_to_go": {"title": "Renew your library card", "url": "https://library.example.org/renew"},
    "before_you_start": ["Have your library card number ready"],
    "steps": [
        {"instruction": "Open the renewal page", "screen_reader_tip": "Press H to move to the heading 'Renew'."},
        {"instruction": "Fill in your details", "screen_reader_tip": "The date field is three boxes."},
    ],
    "prepared_answers": [
        {"field_label": "Full name", "suggested_answer": "Amina Yusuf", "note": "From your answer."},
        {"field_label": "Library card number", "suggested_answer": "[your library card number]", "note": ""},
        {"field_label": "Password", "suggested_answer": "hunter2", "note": ""},
    ],
    "known_accessibility_problems": ["The page has a picture CAPTCHA with no audio option."],
    "what_happens_after": "You get an email within two days.",
}


def _a11y_ok(html: str) -> None:
    report = check_html(html)
    assert report.errors == [], [(f.rule, f.message, f.where) for f in report.errors]
    assert [w for w in report.warnings if w.rule == "heading-order"] == [], report.warnings
    assert len(re.findall(r"<h1[ >]", html)) == 1


def _h1(html: str) -> str:
    return re.search(r"<h1>(.*?)</h1>", html, re.S).group(1).strip()


def _title(html: str) -> str:
    return re.search(r"<title>(.*?) – ", html, re.S).group(1).strip()


def _errand(errand_id: int) -> dict:
    with db.session() as conn:
        return dict(conn.execute("SELECT * FROM errands WHERE id = ?", (errand_id,)).fetchone())


def _last_errand_id() -> int:
    with db.session() as conn:
        return conn.execute("SELECT MAX(id) AS m FROM errands").fetchone()["m"]


def _new(client, kind="find", text="When is the central library in Leeds open?"):
    return post(client, "/do/new", {"kind": kind, "request": text})


# ----- index -------------------------------------------------------------------------------------

def test_index_explains_rules_and_has_form(user_client):
    r = user_client.get("/do")
    assert r.status_code == 200
    _a11y_ok(r.text)
    assert _h1(r.text) == "Do this for me" == _title(r.text)
    assert "Enter your passwords" in r.text and "Pay for anything" in r.text and "Submit, send or sign" in r.text
    assert "Find information and bring it back to me" in r.text
    assert "Help me do a task on a website" in r.text
    assert "Say what you need, as you would to a friend. Do not include passwords, card numbers or codes." in r.text
    assert "You have no errands yet" in r.text


def test_signed_out_is_asked_to_sign_in(client):
    r = client.get("/do", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


# ----- find journey ------------------------------------------------------------------------------

def test_find_journey(user_client, fake_ai):
    fake_ai.on("errands.understand", CONTRACT).on("errands.find", FIND_ANSWER)
    r = _new(user_client)
    assert r.status_code == 200
    eid = _last_errand_id()
    assert r.url.path == f"/do/{eid}"
    _a11y_ok(r.text)
    assert _h1(r.text) == "Here is what I understood: Find the opening hours of the central library" == _title(r.text)
    assert "Search the library&#39;s own website" in r.text
    assert "Which city is the library in?" in r.text
    assert "this week&#39;s opening hours" in r.text
    assert "Nothing has been done yet" in r.text
    # The AI saw the request and the person's profile, and nothing was done yet.
    kind, purpose, kwargs = fake_ai.calls[0]
    assert (kind, purpose) == ("structured", "errands.understand")
    assert "When is the central library in Leeds open?" in kwargs["prompt"]
    assert "NVDA" in kwargs["prompt"] and "screen reader" in kwargs["system"]
    assert fake_ai.purposes() == ["errands.understand"]
    assert _errand(eid)["status"] == "understood"

    # Answer the question and go ahead.
    r = post(user_client, f"/do/{eid}/respond", {"answer": "Leeds, England", "action": "go"})
    assert r.status_code == 200 and r.url.path == f"/do/{eid}"
    _a11y_ok(r.text)
    assert _h1(r.text) == "Found: Find the opening hours of the central library" == _title(r.text)
    assert "open 9am to 5pm" in r.text
    # Headings from the AI sit under the h1: its top level becomes h2.
    assert "<h2>Opening hours</h2>" in r.text and "<h3>Holidays</h3>" in r.text
    assert "<h2>What I could not confirm</h2>" in r.text
    assert "<script>alert(1)</script>" not in r.text
    assert 'href="https://library.example.org/hours" rel="noopener noreferrer"' in r.text
    assert "javascript:alert" not in r.text
    assert "check important facts" in r.text.lower() or "Check important facts" in r.text
    assert "Leeds, England" in r.text  # the person's answer is shown, unchanged
    _, purpose, kwargs = fake_ai.calls[-1]
    assert purpose == "errands.find"
    assert "Leeds, England" in kwargs["prompt"] and "Find the opening hours" in kwargs["prompt"]
    assert "What I could not confirm" in kwargs["system"]
    row = _errand(eid)
    assert row["status"] == "done"
    assert json.loads(row["sources_json"]) == [
        {"url": "https://library.example.org/hours", "title": "Library opening hours", "quote": "Open 9 to 5"}]
    # The index lists it as done.
    index = user_client.get("/do").text
    assert "Find the opening hours of the central library" in index and "Done" in index


def test_will_not_do_always_has_site_rules(user_client, fake_ai):
    fake_ai.on("errands.understand", CONTRACT)
    r = _new(user_client)
    for rule in errands.SITE_RULES:
        assert rule in r.text.replace("&#39;", "'")
    assert "Guess hours that are not published" in r.text
    contract = json.loads(_errand(_last_errand_id())["contract_json"])
    assert contract["will_not_do"][:3] == errands.SITE_RULES


def test_will_not_do_rules_added_when_ai_gives_none(user_client, fake_ai):
    fake_ai.on("errands.understand", dict(CONTRACT, will_not_do=[], goal=""))
    r = _new(user_client, text="Bus times from Leeds to York")
    contract = json.loads(_errand(_last_errand_id())["contract_json"])
    assert contract["will_not_do"] == errands.SITE_RULES
    # An empty goal falls back to the person's own words.
    assert contract["goal"] == "Bus times from Leeds to York"
    assert _h1(r.text) == "Here is what I understood: Bus times from Leeds to York"


def test_ai_rule_duplicates_are_not_repeated():
    contract = errands.normalize_contract(TASK_CONTRACT)
    assert contract["will_not_do"] == errands.SITE_RULES


def test_update_reruns_the_read_back(user_client, fake_ai):
    answers = iter([CONTRACT, dict(CONTRACT, goal="Find the opening hours of Leeds central library")])
    fake_ai.on("errands.understand", lambda **kw: next(answers))
    _new(user_client)
    eid = _last_errand_id()
    r = post(user_client, f"/do/{eid}/respond", {"answer": "It is the one in Leeds.", "action": "update"})
    assert r.url.path == f"/do/{eid}"
    assert _h1(r.text) == "Here is what I understood: Find the opening hours of Leeds central library"
    assert fake_ai.purposes() == ["errands.understand", "errands.understand"]
    assert "It is the one in Leeds." in fake_ai.calls[-1][2]["prompt"]
    assert "It is the one in Leeds." in r.text


def test_update_needs_text(user_client, fake_ai):
    fake_ai.on("errands.understand", CONTRACT)
    _new(user_client)
    eid = _last_errand_id()
    r = post(user_client, f"/do/{eid}/respond", {"answer": "  ", "action": "update"})
    assert r.status_code == 400
    _a11y_ok(r.text)
    assert _h1(r.text).endswith(": please fix the problems") and _h1(r.text) == _title(r.text)
    assert 'href="#field-answer"' in r.text and 'aria-invalid="true"' in r.text
    assert fake_ai.purposes() == ["errands.understand"]


def test_corrections_with_a_secret_are_refused(user_client, fake_ai):
    fake_ai.on("errands.understand", CONTRACT)
    _new(user_client)
    eid = _last_errand_id()
    r = post(user_client, f"/do/{eid}/respond", {"answer": "my password is Sunflower77", "action": "go"})
    assert r.status_code == 400 and "password" in r.text
    assert "Sunflower77" in r.text  # kept in the box so they can edit it, but never saved
    assert _errand(eid)["status"] == "understood"
    assert "Sunflower77" not in _errand(eid)["notes_json"]


def test_respond_only_when_waiting(user_client, fake_ai):
    fake_ai.on("errands.understand", CONTRACT).on("errands.find", FIND_ANSWER)
    _new(user_client)
    eid = _last_errand_id()
    post(user_client, f"/do/{eid}/respond", {"action": "go"})
    r = post(user_client, f"/do/{eid}/respond", {"action": "go"})
    assert "not waiting for an answer" in r.text
    assert fake_ai.purposes().count("errands.find") == 1


def test_follow_up_creates_child_with_context(user_client, fake_ai):
    fake_ai.on("errands.understand", CONTRACT).on("errands.find", FIND_ANSWER)
    _new(user_client)
    parent = _last_errand_id()
    post(user_client, f"/do/{parent}/respond", {"action": "go"})
    fake_ai.on("errands.understand", dict(CONTRACT, goal="Find summer opening hours"))
    r = post(user_client, f"/do/{parent}/follow-up", {"question": "Are the hours different in summer?"})
    child = _last_errand_id()
    assert child != parent
    row = _errand(child)
    assert row["parent_id"] == parent and row["kind"] == "find"
    assert row["request"] == "Are the hours different in summer?"
    assert _h1(r.text) == "Here is what I understood: Find summer opening hours"
    assert f'href="/do/{parent}"' in r.text  # link back to the earlier errand
    prompt = fake_ai.calls[-1][2]["prompt"]
    assert "open 9am to 5pm" in prompt and "When is the central library in Leeds open?" in prompt
    r = post(user_client, f"/do/{child}/respond", {"action": "go"})
    assert _h1(r.text) == "Found: Find summer opening hours"
    assert "open 9am to 5pm" in fake_ai.calls[-1][2]["prompt"]
    # Deleting the parent keeps the follow-up.
    post(user_client, f"/do/{parent}/delete")
    assert _errand(child)["parent_id"] is None


def test_follow_up_validation(user_client, fake_ai):
    fake_ai.on("errands.understand", CONTRACT).on("errands.find", FIND_ANSWER)
    _new(user_client)
    eid = _last_errand_id()
    post(user_client, f"/do/{eid}/respond", {"action": "go"})
    r = post(user_client, f"/do/{eid}/follow-up", {"question": ""})
    assert r.status_code == 400
    _a11y_ok(r.text)
    assert _h1(r.text) == "Found: Find the opening hours of the central library: please fix the problems"
    assert 'href="#field-question"' in r.text
    r = post(user_client, f"/do/{eid}/follow-up", {"question": "My card is 4111 1111 1111 1111, is that ok?"})
    assert r.status_code == 400 and "card number" in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM errands").fetchone()["n"] == 1


def test_follow_up_only_after_an_answer(user_client, fake_ai):
    fake_ai.on("errands.understand", CONTRACT)
    _new(user_client)
    eid = _last_errand_id()
    r = post(user_client, f"/do/{eid}/follow-up", {"question": "And in summer?"})
    assert "once an answer has been found" in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM errands").fetchone()["n"] == 1


# ----- task journey ------------------------------------------------------------------------------

def test_task_journey(user_client, fake_ai):
    fake_ai.on("errands.understand", TASK_CONTRACT)
    fake_ai.on("errands.task_research", TASK_RESEARCH).on("errands.task_prepare", PREPARED)
    r = _new(user_client, kind="task", text="Help me renew my library card on the library website")
    eid = _last_errand_id()
    assert _h1(r.text) == "Here is what I understood: Renew my library card online"
    assert "Your library card number" in r.text and "Never give passwords" in r.text
    r = post(user_client, f"/do/{eid}/respond", {"answer": "My name is Amina Yusuf.", "action": "go"})
    assert r.status_code == 200
    _a11y_ok(r.text)
    assert _h1(r.text) == "Ready for you to do: Renew my library card online" == _title(r.text)
    assert fake_ai.purposes() == ["errands.understand", "errands.task_research", "errands.task_prepare"]
    research_call, prepare_call = fake_ai.calls[1], fake_ai.calls[2]
    assert "My name is Amina Yusuf." in research_call[2]["prompt"]
    assert "Renew at the official library site" in prepare_call[2]["prompt"]
    assert "https://library.example.org/renew" in prepare_call[2]["prompt"]
    assert "[your passport number]" in prepare_call[2]["system"]
    text = r.text
    assert '<a href="https://library.example.org/renew" rel="noopener noreferrer">Renew your library card</a>' in text
    assert "Have your library card number ready" in text
    assert "<ol>" in text and "Open the renewal page" in text and "Screen reader tip: Press H" in text
    assert "<pre>Amina Yusuf</pre>" in text and "<pre>[your library card number]</pre>" in text
    # A password suggested by the AI is never shown; it becomes a gap for the person.
    assert "hunter2" not in text and "<pre>[your password]</pre>" in text
    assert "picture CAPTCHA" in text
    assert "You get an email within two days." in text
    assert "This site does not log in, pay or send anything for you." in text
    assert "Renew your card" in text  # research source listed
    row = _errand(eid)
    assert row["status"] == "done" and json.loads(row["prepared_json"])["where_to_go"]["url"]


def test_task_with_unsafe_url_shows_no_link(user_client, fake_ai):
    fake_ai.on("errands.understand", TASK_CONTRACT).on("errands.task_research", ResearchResult(text="Nothing found."))
    fake_ai.on("errands.task_prepare", dict(PREPARED, where_to_go={"title": "Library", "url": "javascript:alert(1)"},
                                            prepared_answers=[], known_accessibility_problems=[], steps=[]))
    _new(user_client, kind="task", text="Renew my library card")
    r = post(user_client, f"/do/{_last_errand_id()}/respond", {"action": "go"})
    _a11y_ok(r.text)
    assert "javascript:" not in r.text
    assert "did not find an official web address" in r.text
    assert "That does not mean there are none" in r.text
    assert "did not give any sources" in r.text


def test_normalize_prepared_hides_card_numbers():
    prepared = errands.normalize_prepared({"prepared_answers": [
        {"field_label": "Payment", "suggested_answer": "4111111111111111", "note": ""},
        {"field_label": "Card number", "suggested_answer": "[your card number]", "note": ""},
    ]})
    assert [a["suggested_answer"] for a in prepared["prepared_answers"]] == ["[your payment]", "[your card number]"]


# ----- secrets -----------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Log me in, my password is Sunflower77",
    "password: hunter",
    "My PIN is 4821, please check my balance",
    "the verification code is 482913",
    "my OTP is 123456",
    "pay with 4111 1111 1111 1111 please",
    "card 4111-1111-1111-1111",
    "number 5555555555554444 thanks",
])
def test_requests_with_secrets_are_refused(user_client, fake_ai, text):
    r = _new(user_client, text=text)
    assert r.status_code == 400
    _a11y_ok(r.text)
    assert _h1(r.text) == "Do this for me: please fix the problems" == _title(r.text)
    assert 'href="#field-request"' in r.text and "Please take it out" in r.text
    assert fake_ai.calls == []
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM errands").fetchone()["n"] == 0


@pytest.mark.parametrize("text", [
    "I forgot my password and need to reset it on my bank's website",
    "My password is not working on the council site",
    "The tracking number is 1234567890123, where is my parcel?",
    "Find out what a PIN reminder letter looks like",
    "My order reference is 9876 5432 1098 7654 3",
])
def test_ordinary_requests_are_allowed(text):
    assert errands.find_secret(text) is None


def test_long_number_without_luhn_is_allowed(user_client, fake_ai):
    fake_ai.on("errands.understand", CONTRACT)
    r = _new(user_client, text="Track parcel 1234567890123 for me")
    assert r.status_code == 200
    assert _errand(_last_errand_id())["request"] == "Track parcel 1234567890123 for me"


# ----- validation --------------------------------------------------------------------------------

def test_new_errand_validation(user_client, fake_ai):
    r = post(user_client, "/do/new", {"kind": "", "request": ""})
    assert r.status_code == 400
    _a11y_ok(r.text)
    assert 'href="#field-kind"' in r.text and 'href="#field-request"' in r.text
    assert "There are 2 problems" in r.text
    r = post(user_client, "/do/new", {"kind": "hack", "request": "x" * 5000})
    assert r.status_code == 400 and "at most 4,000 characters" in r.text
    assert fake_ai.calls == []


def test_new_errand_keeps_what_was_typed(user_client):
    r = post(user_client, "/do/new", {"request": "Find bus times to York"})
    assert r.status_code == 400 and "Find bus times to York" in r.text


def test_forms_need_csrf(user_client):
    r = user_client.post("/do/new", data={"kind": "find", "request": "Bus times"})
    assert r.status_code == 400


# ----- failures and retry ------------------------------------------------------------------------

def test_failed_read_back_can_be_retried(user_client, fake_ai):
    fake_ai.on("errands.understand", AIError("The AI is busy right now (rate limit). Please try again in a minute."))
    r = _new(user_client)
    eid = _last_errand_id()
    assert "This did not finish" in r.text
    page = user_client.get(f"/do/{eid}")
    _a11y_ok(page.text)
    assert _h1(page.text).startswith("Saved but not read back yet")
    assert "The AI is busy right now" in page.text and "Read back my request" in page.text
    assert "When is the central library in Leeds open?" in page.text
    fake_ai.on("errands.understand", CONTRACT)
    r = post(user_client, f"/do/{eid}/retry")
    assert _h1(r.text) == "Here is what I understood: Find the opening hours of the central library"


def test_failed_run_can_be_retried(user_client, fake_ai):
    fake_ai.on("errands.understand", CONTRACT).on("errands.find", AIError("Could not reach the AI service."))
    _new(user_client)
    eid = _last_errand_id()
    post(user_client, f"/do/{eid}/respond", {"action": "go"})
    page = user_client.get(f"/do/{eid}")
    _a11y_ok(page.text)
    assert _h1(page.text) == "Not finished yet: Find the opening hours of the central library"
    assert "Could not reach the AI service." in page.text and "Try again" in page.text
    fake_ai.on("errands.find", FIND_ANSWER)
    r = post(user_client, f"/do/{eid}/retry")
    assert _h1(r.text) == "Found: Find the opening hours of the central library"


def test_retry_on_finished_errand_does_nothing(user_client, fake_ai):
    fake_ai.on("errands.understand", CONTRACT)
    _new(user_client)
    eid = _last_errand_id()
    r = post(user_client, f"/do/{eid}/retry")
    assert r.url.path == f"/do/{eid}" and fake_ai.purposes() == ["errands.understand"]


def test_running_job_page(user_client, fake_ai):
    from studio import jobs
    fake_ai.on("errands.understand", CONTRACT)
    _new(user_client)
    eid = _last_errand_id()
    job_id = jobs.submit(user_id(), "errands.run", "Finding the information", lambda ctx: "/do")
    with db.session() as conn:
        conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        conn.execute("UPDATE errands SET status = 'working', job_id = ? WHERE id = ?", (job_id, eid))
    page = user_client.get(f"/do/{eid}")
    _a11y_ok(page.text)
    assert _h1(page.text) == "Working on it: Find the opening hours of the central library"
    assert f'href="/jobs/{job_id}"' in page.text
    r = post(user_client, f"/do/{eid}/retry", follow_redirects=False)
    assert r.headers["location"] == f"/jobs/{job_id}"


def test_errand_deleted_while_job_runs(user_client, fake_ai):
    eid = errands._create(user_id(), "find", "Bus times")
    assert errands._understand(type("Ctx", (), {"progress": lambda self, t: None})(), fake_ai, -1, {}) == "/do"
    assert _errand(eid)["status"] == "new"


# ----- delete ------------------------------------------------------------------------------------

def test_delete_with_confirmation(user_client, fake_ai):
    fake_ai.on("errands.understand", CONTRACT)
    _new(user_client)
    eid = _last_errand_id()
    r = user_client.get(f"/do/{eid}/delete")
    assert r.status_code == 200
    _a11y_ok(r.text)
    assert _h1(r.text) == "Delete this errand: Find the opening hours of the central library"
    assert "cannot undo" in r.text
    r = post(user_client, f"/do/{eid}/delete")
    assert r.url.path == "/do" and "The errand is deleted." in r.text
    assert user_client.get(f"/do/{eid}").status_code == 404


# ----- ownership ---------------------------------------------------------------------------------

def test_other_members_get_404(client, fake_ai):
    fake_ai.on("errands.understand", CONTRACT).on("errands.find", FIND_ANSWER)
    register(client, "a@example.com", "A")
    _new(client)
    eid = _last_errand_id()
    post(client, f"/do/{eid}/respond", {"action": "go"})
    post(client, "/logout")
    register(client, "b@example.com", "B")
    assert "central library" not in client.get("/do").text
    assert client.get(f"/do/{eid}").status_code == 404
    assert client.get(f"/do/{eid}/delete").status_code == 404
    assert post(client, f"/do/{eid}/delete").status_code == 404
    assert post(client, f"/do/{eid}/respond", {"action": "go"}).status_code == 404
    assert post(client, f"/do/{eid}/retry").status_code == 404
    assert post(client, f"/do/{eid}/follow-up", {"question": "More?"}).status_code == 404
    assert client.get("/do/999999").status_code == 404
    assert _errand(eid)["status"] == "done"
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM errands").fetchone()["n"] == 1


# ----- no AI -------------------------------------------------------------------------------------

def test_no_ai_saves_request_and_explains(no_ai_app):
    client = TestClient(no_ai_app)
    register(client)
    r = _new(client)
    eid = _last_errand_id()
    assert r.status_code == 200 and r.url.path == f"/do/{eid}"
    _a11y_ok(r.text)
    assert "connect your own Claude API key" in r.text
    assert _h1(r.text).startswith("Saved but not read back yet")
    assert "Read back my request" in r.text and 'href="/account"' in r.text
    assert _errand(eid)["status"] == "new"
    r = post(client, f"/do/{eid}/retry")
    assert "connect your own Claude API key" in r.text and _errand(eid)["status"] == "new"
    index = client.get("/do").text
    assert "Not read back yet" in index


def test_no_ai_on_go_ahead_keeps_the_read_back(no_ai_app):
    client = TestClient(no_ai_app)
    register(client)
    eid = errands._create(user_id(), "find", "Bus times from Leeds to York")
    errands._update(eid, status="understood", contract_json=json.dumps(errands.normalize_contract(CONTRACT)))
    r = post(client, f"/do/{eid}/respond", {"answer": "Tomorrow morning", "action": "go"})
    assert "connect your own Claude API key" in r.text
    assert _h1(r.text).startswith("Here is what I understood")
    assert _errand(eid)["status"] == "understood" and _errand(eid)["notes_json"] == "[]"


# ----- helpers -----------------------------------------------------------------------------------

def test_answer_html_moves_headings_under_h1():
    assert "<h2>A</h2>" in errands.answer_html("### A\n\n#### B")
    assert "<h3>B</h3>" in errands.answer_html("### A\n\n#### B")
    assert "<h2>Top</h2>" in errands.answer_html("# Top")
    assert "&lt;b&gt;" in errands.answer_html("<b>bold</b>")


def test_luhn():
    assert errands._luhn_ok("4111111111111111")
    assert not errands._luhn_ok("1234567890123")
