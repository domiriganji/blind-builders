"""Do this for me (/do), driven in a real Chromium the way a screen reader user moves through it.

Every control is found only by its role and accessible name, or by its label, never by CSS, ids or
XPath, so the test itself shows that every control has a usable name. Every page reached is checked
with check_page and check_page_narrow (axe-core, one h1 that matches the title, a main landmark, no
sideways scrolling at desktop width and at 320 pixels).
"""
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect

from studio.ai import AIError, ResearchResult, Source

from .conftest import check_page, check_page_narrow, new_page, sign_in, sign_up

pytestmark = pytest.mark.browser

CONTRACT = {
    "goal": "Find the opening hours of the central library",
    "steps": ["Search the library's own website", "Bring back the hours with links"],
    "will_not_do": ["Guess hours that are not published"],
    "done_when": "You have this week's opening hours and a link to check them.",
    "questions": ["Which city is the library in?"],
    "information_needed_from_you": [],
}
CONTRACT_LEEDS = dict(CONTRACT, goal="Find the opening hours of Leeds central library", questions=[])
FIND_ANSWER = ResearchResult(
    text=("The central library is open 9am to 5pm, Monday to Saturday.\n\n"
          "# Opening hours\n\nClosed on Sundays.\n\n#### Holidays\n\nClosed on public holidays.\n\n"
          "# What I could not confirm\n\nWhether hours change in summer. <script>alert(1)</script>"),
    sources=[Source(url="https://library.example.org/hours", title="Library opening hours", quote="Open 9 to 5"),
             Source(url="javascript:alert(1)", title="Bad link")],
)
SUMMER_CONTRACT = dict(CONTRACT, goal="Find the summer opening hours", questions=[])
SUMMER_ANSWER = ResearchResult(text="The hours are the same in summer.",
                               sources=[Source(url="https://library.example.org/summer", title="Summer hours")])
TASK_CONTRACT = {
    "goal": "Renew my library card online",
    "steps": ["Find the official renewal page", "Prepare the steps and answers"],
    "will_not_do": ["Log in for you", "Pay the fee", "Submit the form"],
    "done_when": "You have the steps and answers to renew your card yourself.",
    "questions": ["What is your PIN?"],
    "information_needed_from_you": ["Your full name", "Your password for the library site"],
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


# ----- helpers -----------------------------------------------------------------------------------

def _check(page, label: str = "") -> None:
    """Every page a journey reaches must pass check_page at full width and at 320 pixels."""
    problems = check_page(page, label) + check_page_narrow(page, label)
    assert problems == [], "\n".join(problems)


def _check_without_js(page, browser, label: str) -> None:
    """check_page for a page opened with JavaScript off.

    axe-core cannot run in a page whose JavaScript is off, so the exact HTML that page received is
    loaded into a second page at the same address, with the site's own script blocked, and checked there.
    """
    html = page.content()
    origin = "{0.scheme}://{0.netloc}".format(urlparse(page.url))
    checker = new_page(browser, width=page.viewport_size["width"])
    try:
        checker.route("**/static/enhance.js", lambda route: route.abort())
        checker.route("**/no-javascript-snapshot", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=html))
        checker.goto(origin + "/no-javascript-snapshot")
        _check(checker, label)
    finally:
        checker.context.close()


def _h1(page):
    return page.get_by_role("heading", level=1)


def _expect_h1(page, text: str) -> None:
    expect(_h1(page)).to_have_text(text)
    page.wait_for_load_state()


def _follow_error_link(page, message: str, field_label: str) -> None:
    """The error summary is there, says how to fix the problem, and its link moves focus to the field."""
    summary = page.locator(".error-summary").filter(has=page.get_by_role("heading", name="What to check", exact=True))
    expect(summary).to_be_visible()
    expect(summary.get_by_role("link")).to_have_count(1)
    link = summary.get_by_role("link", name=message)
    expect(link).to_be_visible()
    link.click()
    expect(page.get_by_label(field_label)).to_be_focused()


def _new_errand(page, base: str, kind_label: str, text: str) -> None:
    page.goto(base + "/do")
    _expect_h1(page, "Do this for me")
    page.get_by_label(kind_label).check()
    page.get_by_label("What you need", exact=True).fill(text)
    page.get_by_role("button", name="Read back my request").click()


# ----- journeys ----------------------------------------------------------------------------------

def test_find_journey_with_corrections_follow_up_and_delete(live, browser):
    base, fake = live["base"], live["fake_ai"]
    answers = iter([CONTRACT, CONTRACT_LEEDS])
    fake.on("errands.understand", lambda **kw: next(answers)).on("errands.find", FIND_ANSWER)
    page = new_page(browser)
    sign_up(page, base, "errands-find@example.com", "Errand Finder")

    # The section index explains what the site will never do, and has the form.
    page.get_by_role("navigation", name="Sections").get_by_role("link", name="Do this for me").click()
    _expect_h1(page, "Do this for me")
    _check(page, "index (empty)")
    expect(page.get_by_role("heading", name="What this site will never do")).to_be_visible()
    expect(page.get_by_text("You have no requests yet")).to_be_visible()
    expect(page.get_by_label("Find information and bring it back to me")).to_be_checked()

    # Sending the form empty shows the error summary; its link moves focus to the box.
    page.get_by_role("button", name="Read back my request").click()
    _expect_h1(page, "Please check 1 answer: Do this for me")
    _check(page, "index with an empty request")
    _follow_error_link(page, "Write what you need in the box", "What you need")

    # A request with a password is refused before anything is saved, and says how to fix it.
    page.get_by_label("What you need", exact=True).fill("Log me in to the library, my password is Sunflower77")
    page.get_by_role("button", name="Read back my request").click()
    _expect_h1(page, "Please check 1 answer: Do this for me")
    _check(page, "index with a password")
    _follow_error_link(page, "Please take it out", "What you need")
    expect(page.get_by_text("You have no requests yet")).to_be_visible()

    # A real request: the AI reads it back before doing anything.
    page.get_by_label("What you need", exact=True).fill("When is the central library open?")
    page.get_by_role("button", name="Read back my request").click()
    _expect_h1(page, "Here is what I understood: When is the central library open?")
    _check(page, "read-back")
    expect(page.get_by_text("Find the opening hours of the central library", exact=True)).to_be_visible()
    expect(page.get_by_text("Nothing has been done yet")).to_be_visible()
    expect(page.get_by_role("heading", name="Questions for you")).to_be_visible()
    expect(page.get_by_text("Which city is the library in?")).to_be_visible()
    for rule in ("Log in to any website for you", "Pay for anything", "Send, submit or sign anything for you"):
        expect(page.get_by_role("listitem").filter(has_text=rule)).to_have_count(1)
    expect(page.get_by_role("heading", name="Your request, in your words")).to_be_visible()

    # Update with nothing written: the error says what to do, and its link moves focus to the box.
    page.get_by_role("button", name="Update what you understood").click()
    _expect_h1(page, "Please check 1 answer: Here is what I understood: When is the central library open?")
    _check(page, "read-back with an empty correction")
    _follow_error_link(page, "Write your corrections or answers", "Corrections or answers")

    # A correction: the AI reads it back again, and the person's words are shown unchanged.
    page.get_by_label("Corrections or answers").fill("It is the one in Leeds.")
    page.get_by_role("button", name="Update what you understood").click()
    _expect_h1(page, "Here is what I understood: When is the central library open?")
    _check(page, "second read-back")
    expect(page.get_by_text("Find the opening hours of Leeds central library", exact=True)).to_be_visible()
    expect(page.get_by_role("listitem").filter(has_text="It is the one in Leeds.")).to_be_visible()
    expect(page.get_by_role("heading", name="Questions for you")).to_have_count(0)

    # Go ahead: the answer comes back with sources, labelled as the AI's, and says what was checked.
    page.get_by_role("button", name="This is right: go ahead").click()
    _expect_h1(page, "Answer ready: When is the central library open?")
    _check(page, "found")
    expect(page.get_by_role("heading", name="Answer written by the AI", level=2)).to_be_visible()
    expect(page.get_by_role("heading", name="Opening hours", level=3)).to_be_visible()
    expect(page.get_by_role("heading", name="Holidays", level=4)).to_be_visible()
    expect(page.get_by_role("heading", name="What I could not confirm", level=3)).to_be_visible()
    expect(page.get_by_text("open 9am to 5pm")).to_be_visible()
    expect(page.get_by_role("heading", name="Sources the AI used (1)")).to_be_visible()
    expect(page.get_by_role("link", name="Library opening hours")).to_have_attribute(
        "href", "https://library.example.org/hours")
    expect(page.get_by_role("link", name="Bad link")).to_have_count(0)
    expect(page.get_by_role("heading", name="What was checked")).to_be_visible()
    expect(page.get_by_text("Nobody has checked it.")).to_be_visible()

    # Follow-up: an empty question shows the error, then a real one is read back first.
    page.get_by_role("button", name="Ask the follow-up").click()
    _expect_h1(page, "Please check 1 answer: Answer ready: When is the central library open?")
    _check(page, "found with an empty follow-up")
    _follow_error_link(page, "Write your follow-up question in the box", "Your follow-up question")
    fake.on("errands.understand", SUMMER_CONTRACT).on("errands.find", SUMMER_ANSWER)
    page.get_by_label("Your follow-up question").fill("Are the hours different in summer?")
    page.get_by_role("button", name="Ask the follow-up").click()
    _expect_h1(page, "Here is what I understood: Are the hours different in summer?")
    _check(page, "follow-up read-back")
    expect(page.get_by_role("link", name="your earlier request")).to_be_visible()
    page.get_by_role("button", name="This is right: go ahead").click()
    _expect_h1(page, "Answer ready: Are the hours different in summer?")
    _check(page, "follow-up found")
    expect(page.get_by_role("link", name="Summer hours")).to_be_visible()

    # The earlier request is one link away.
    page.get_by_role("link", name="your earlier request").click()
    _expect_h1(page, "Answer ready: When is the central library open?")

    # The index lists both requests first, under a heading, by the member's own words.
    page.get_by_role("link", name="Back to all your requests").click()
    _expect_h1(page, "Do this for me")
    _check(page, "index with requests")
    expect(page.get_by_role("heading", level=2).first).to_have_text("Your requests")
    done = page.get_by_role("heading", name="Done (2)", level=3)
    expect(done).to_be_visible()
    expect(page.get_by_role("link", name="Are the hours different in summer?", exact=True)).to_be_visible()
    expect(page.get_by_text("The AI understood: Find the summer opening hours.")).to_be_visible()

    # Delete: a confirmation page, a way back, then the next h1 says what happened.
    page.get_by_role("link", name="Delete request: Are the hours different in summer?").click()
    _expect_h1(page, "Delete this request: Are the hours different in summer?")
    _check(page, "delete confirmation")
    expect(page.get_by_text("You cannot undo this")).to_be_visible()
    page.get_by_role("link", name="Keep it and go back").click()
    _expect_h1(page, "Answer ready: Are the hours different in summer?")
    page.get_by_role("link", name="Delete this request").click()
    _expect_h1(page, "Delete this request: Are the hours different in summer?")
    page.get_by_role("button", name="Delete this request").click()
    _expect_h1(page, "Request deleted: Are the hours different in summer?")
    _check(page, "index after delete")
    expect(page.get_by_role("link", name="Are the hours different in summer?", exact=True)).to_have_count(0)
    expect(page.get_by_role("heading", name="Done (1)", level=3)).to_be_visible()


def test_task_journey_prepares_answers_and_hides_secrets(live, browser):
    base, fake = live["base"], live["fake_ai"]
    fake.on("errands.understand", TASK_CONTRACT)
    fake.on("errands.task_research", TASK_RESEARCH).on("errands.task_prepare", PREPARED)
    page = new_page(browser)
    sign_up(page, base, "errands-task@example.com", "Errand Tasker")

    _new_errand(page, base, "Help me do a task on a website", "Help me renew my library card on the library website")
    _expect_h1(page, "Here is what I understood: Help me renew my library card on the library website")
    _check(page, "task read-back")
    help_heading = page.get_by_role("heading", name="Information that would help me prepare your answers")
    expect(help_heading).to_be_visible()
    expect(page.get_by_role("listitem").filter(has_text="Your full name")).to_be_visible()
    # The site never asks for secrets, even when the AI's read-back does.
    expect(page.get_by_text("Your password for the library site")).to_have_count(0)
    expect(page.get_by_text("What is your PIN?")).to_have_count(0)

    # An answer with a card number is refused and the link moves focus to the box.
    page.get_by_label("Corrections or answers").fill("Pay with 4111 1111 1111 1111")
    page.get_by_role("button", name="This is right: go ahead").click()
    _expect_h1(page, "Please check 1 answer: Here is what I understood: Help me renew my library card on the library website")
    _check(page, "task read-back with a card number")
    _follow_error_link(page, "This looks like it includes a card number", "Corrections or answers")

    page.get_by_label("Corrections or answers").fill("My name is Amina Yusuf.")
    page.get_by_role("button", name="This is right: go ahead").click()
    _expect_h1(page, "Ready for you to do: Help me renew my library card on the library website")
    _check(page, "task ready")
    expect(page.get_by_text("The AI wrote this plan from web pages it found")).to_be_visible()
    expect(page.get_by_role("link", name="Renew your library card")).to_have_attribute(
        "href", "https://library.example.org/renew")
    for heading in ("Where to go", "Before you start", "Steps", "Prepared answers", "Known accessibility problems",
                    "What happens after", "What was checked"):
        expect(page.get_by_role("heading", name=heading, exact=True, level=2)).to_be_visible()
    # Each step ends with a full stop, and its tip is a paragraph of its own, so they never run together.
    expect(page.get_by_text("Open the renewal page.", exact=True)).to_be_visible()
    expect(page.get_by_text("Screen reader tip: Press H to move to the heading 'Renew'.", exact=True)).to_be_visible()
    # Each prepared answer is a read-only box named by its field, reachable by heading and by form field.
    expect(page.get_by_role("heading", name="Answer for: Full name", level=3)).to_be_visible()
    answer = page.get_by_role("textbox", name="Answer for: Full name")
    expect(answer).to_have_value("Amina Yusuf")
    expect(answer).not_to_be_editable()
    expect(answer).to_have_accessible_description("Note: From your answer.")
    # "Select all" in the box selects that answer only, ready to copy by keyboard or voice.
    answer.focus()
    page.keyboard.press("ControlOrMeta+a")
    assert answer.evaluate("el => el.value.substring(el.selectionStart, el.selectionEnd)") == "Amina Yusuf"
    # The password the AI suggested is never shown: it is a gap for the person to fill in.
    assert "hunter2" not in page.content()
    expect(page.get_by_role("textbox", name="Answer for: Password")).to_have_value("[your password]")
    expect(page.get_by_role("link", name="ask the community for help")).to_be_visible()
    expect(page.get_by_text("Checked by the site")).to_be_visible()
    # No follow-up form on a task: nothing to ask.
    expect(page.get_by_role("button", name="Ask the follow-up")).to_have_count(0)


def test_failures_can_be_retried(live, browser):
    base, fake = live["base"], live["fake_ai"]
    fake.on("errands.understand", AIError("The AI is busy right now (rate limit). Please try again in a minute."))
    page = new_page(browser)
    sign_up(page, base, "errands-retry@example.com", "Errand Retry")

    # The read-back fails: the job page says so in plain words.
    _new_errand(page, base, "Find information and bring it back to me", "Bus times from Leeds to York")
    _expect_h1(page, "This did not finish: Reading back your request: Bus times from Leeds to York")
    _check(page, "failed read-back job")
    expect(page.get_by_text("The AI is busy right now")).to_be_visible()

    # The request is saved; its page explains and offers to read it back.
    page.goto(base + "/do")
    _expect_h1(page, "Do this for me")
    expect(page.get_by_role("heading", name="Being worked on or not finished (1)")).to_be_visible()
    expect(page.get_by_text("Not read back yet")).to_be_visible()
    page.get_by_role("link", name="Bus times from Leeds to York", exact=True).click()
    _expect_h1(page, "Saved but not read back yet: Bus times from Leeds to York")
    _check(page, "saved, not read back")
    expect(page.get_by_text("The AI is busy right now")).to_be_visible()
    expect(page.get_by_role("link", name="Check how you pay for the AI")).to_be_visible()
    fake.on("errands.understand", dict(CONTRACT, goal="Find bus times from Leeds to York", questions=[]))
    page.get_by_role("button", name="Read back my request").click()
    _expect_h1(page, "Here is what I understood: Bus times from Leeds to York")

    # The search fails: Try again finishes it.
    fake.on("errands.find", AIError("Could not reach the AI service. Please try again."))
    page.get_by_role("button", name="This is right: go ahead").click()
    _expect_h1(page, "This did not finish: Finding the information: Bus times from Leeds to York")
    _check(page, "failed search job")
    page.goto(base + "/do")
    page.get_by_role("link", name="Bus times from Leeds to York", exact=True).click()
    _expect_h1(page, "Not finished yet: Bus times from Leeds to York")
    _check(page, "not finished")
    expect(page.get_by_text("Could not reach the AI service")).to_be_visible()
    fake.on("errands.find", ResearchResult(text="Trains leave every 15 minutes.",
                                           sources=[Source(url="https://trains.example.org", title="Timetable")]))
    page.get_by_role("button", name="Try again").click()
    _expect_h1(page, "Answer ready: Bus times from Leeds to York")
    _check(page, "found after retry")

    # Another member gets "Page not found" for this request.
    errand_url = page.url
    other = new_page(browser)
    sign_up(other, base, "errands-other@example.com", "Someone Else")
    other.goto(errand_url)
    _expect_h1(other, "Page not found")
    _check(other, "another member's request")
    expect(other.get_by_text("We could not find that request.")).to_be_visible()


def test_works_without_javascript(live, browser):
    base, fake = live["base"], live["fake_ai"]
    fake.on("errands.understand", CONTRACT).on("errands.find", FIND_ANSWER)
    setup = new_page(browser)
    sign_up(setup, base, "errands-nojs@example.com", "No Script")
    page = new_page(browser, javascript=False)
    sign_in(page, base, "errands-nojs@example.com")

    page.goto(base + "/do")
    _expect_h1(page, "Do this for me")
    _check_without_js(page, browser, "index without JavaScript")
    page.get_by_role("button", name="Read back my request").click()
    _expect_h1(page, "Please check 1 answer: Do this for me")
    _check_without_js(page, browser, "index with an error, without JavaScript")
    _follow_error_link(page, "Write what you need in the box", "What you need")
    page.get_by_label("What you need", exact=True).fill("When is the central library open?")
    page.get_by_role("button", name="Read back my request").click()
    _expect_h1(page, "Here is what I understood: When is the central library open?")
    _check_without_js(page, browser, "read-back without JavaScript")
    page.get_by_label("Corrections or answers").fill("Leeds, England")
    page.get_by_role("button", name="This is right: go ahead").click()
    _expect_h1(page, "Answer ready: When is the central library open?")
    _check_without_js(page, browser, "found without JavaScript")
    expect(page.get_by_text("open 9am to 5pm")).to_be_visible()
    page.get_by_role("link", name="Delete this request").click()
    _expect_h1(page, "Delete this request: When is the central library open?")
    _check_without_js(page, browser, "delete without JavaScript")
    page.get_by_role("button", name="Delete this request").click()
    _expect_h1(page, "Request deleted: When is the central library open?")
    _check_without_js(page, browser, "index after delete, without JavaScript")


def test_keyboard_order_on_the_request_form(live, browser):
    page = new_page(browser)
    sign_up(page, live["base"], "errands-keys@example.com", "Key Board")
    page.goto(live["base"] + "/do")
    _expect_h1(page, "Do this for me")
    _check(page, "index for keyboard")

    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="Skip to main content")).to_be_focused()

    first_field = page.get_by_label("Find information and bring it back to me")
    footer_link = page.get_by_role("navigation", name="About this site").get_by_role("link", name="How this site works")
    is_focused = "el => el === document.activeElement"
    reached_field = False
    for _ in range(80):
        page.keyboard.press("Tab")
        assert not footer_link.evaluate(is_focused), "Tab reached the footer links before the request form"
        if first_field.evaluate(is_focused):
            reached_field = True
            break
    assert reached_field, "Tab never reached the first field of the request form"

    # The rest of the form follows in reading order: the other choice by arrow key, then the box, then the button.
    page.keyboard.press("ArrowDown")
    expect(page.get_by_label("Help me do a task on a website")).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.get_by_label("What you need", exact=True)).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.get_by_role("button", name="Read back my request")).to_be_focused()
