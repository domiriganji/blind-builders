"""Learn and help, driven in Chromium the way a screen reader user moves: every control is found by its role
and accessible name or by its label, never by CSS selectors or ids, so the test itself proves each control
has a usable name. Every page reached passes check_page at full width and at 320 pixels.
"""
import re

import pytest

from studio.features import learn

from .conftest import check_page, check_page_narrow, new_page, sign_in, sign_up

pytestmark = pytest.mark.browser
playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

AI_ANSWER = ("Switch NVDA to browse mode first, then read the preview.\n\n## Steps\n\n"
             "1. Press NVDA+Space.\n2. Press H to move to the first heading.")
AI_LABEL = "Suggested by the AI. Not checked by a member."
FOOTER_LINKS = ["How this site works", "How paying works", "Accessibility", "Ask the community"]


def audit(page, problems: list, label: str) -> None:
    """Run the shared page checks at full width and at 320 pixels, and keep every problem."""
    problems.extend(check_page(page, label))
    problems.extend(check_page_narrow(page, label))


def assert_heading(page, text: str) -> None:
    """The page's only h1 says exactly this (waiting for the new page after a click)."""
    expect(page.get_by_role("heading", level=1)).to_have_text(text)


def signed_in(page) -> None:
    """Wait until the page after signing up or in has loaded, so the next step does not race it."""
    expect(page.get_by_role("button", name="Sign out")).to_be_visible()


def follow_error_link(page, message: str, field_label: str) -> None:
    """The error summary is announced first; its link must move focus to the field with the problem."""
    summary = page.locator(".error-summary")
    expect(summary.get_by_role("heading", name="What to check", exact=True, level=2)).to_be_visible()
    summary.get_by_role("link", name=message).click()
    expect(page.get_by_label(field_label)).to_be_focused()


def test_guides_from_the_index(live, browser):
    base, problems = live["base"], []
    page = new_page(browser)
    page.goto(base + "/learn")
    assert_heading(page, "Learn and help")
    audit(page, problems, "/learn")
    guides = learn.load_guides()
    assert len(guides) == 8
    for guide in guides:
        page.goto(base + "/learn")
        page.get_by_role("heading", level=3, name=guide.title, exact=True).get_by_role("link").click()
        page.wait_for_load_state()
        assert_heading(page, guide.title)
        expect(page.get_by_text(f"Last checked {guide.last_checked_words}.")).to_be_visible()
        expect(page.get_by_role("link", name="Ask a question about this guide", exact=True)).to_be_visible()
        audit(page, problems, page.url)
    page.get_by_role("link", name="All guides").click()
    assert_heading(page, "Learn and help")
    page.get_by_role("link", name="Read questions and answers from members").click()
    assert_heading(page, "Ask the community")
    audit(page, problems, page.url)
    assert not problems, "\n".join(problems)


def test_a_missing_guide_leads_back_to_the_guides(live, browser):
    base, problems = live["base"], []
    page = new_page(browser)
    page.goto(base + "/learn/an-old-guide-name")
    assert_heading(page, "Guide not found")
    audit(page, problems, "missing guide")
    page.get_by_role("link", name="See all guides on Learn and help").click()
    assert_heading(page, "Learn and help")
    assert not problems, "\n".join(problems)


def test_asking_from_a_guide_fills_in_the_title(live, browser):
    base, problems = live["base"], []
    page = new_page(browser)
    sign_up(page, base, "guide-asker@example.com", "Guide Asker")
    signed_in(page)
    page.goto(base + "/learn")
    audit(page, problems, "/learn signed in")
    title = learn.get_guide("charts-and-data").title
    page.get_by_role("heading", level=3, name=title, exact=True).get_by_role("link").click()
    assert_heading(page, title)
    audit(page, problems, "charts guide signed in")
    page.get_by_role("link", name="Ask a question about this guide", exact=True).click()
    page.wait_for_load_state()
    assert_heading(page, "Ask a question")
    expect(page.get_by_label("Title")).to_have_value(title)
    audit(page, problems, page.url)
    assert not problems, "\n".join(problems)


def test_ask_get_an_ai_answer_answer_and_mark_helpful(live, browser):
    base, problems = live["base"], []
    live["fake_ai"].on("help.ai_answer", AI_ANSWER)
    title = "NVDA reads nothing in my preview"

    asker = new_page(browser)
    sign_up(asker, base, "journey-asker@example.com", "Asha")
    signed_in(asker)
    asker.goto(base + "/help")
    audit(asker, problems, "/help")
    asker.get_by_role("link", name="Ask a question", exact=True).click()
    asker.wait_for_load_state()
    assert_heading(asker, "Ask a question")
    audit(asker, problems, "/help/ask")

    # Sent empty: the error summary comes first, and each link moves focus to its field.
    asker.get_by_role("button", name="Post your question").click()
    asker.wait_for_load_state()
    assert_heading(asker, "Please check 2 answers: Ask a question")
    expect(asker.locator(".error-summary").get_by_role("heading", name="What to check", exact=True)).to_be_visible()
    expect(asker.locator(".error-summary").get_by_role("link")).to_have_count(2)
    audit(asker, problems, "/help/ask with errors")
    follow_error_link(asker, "Enter a short title that says what you need help with.", "Title")
    follow_error_link(asker, "Enter your question. Say what you tried and what happened.", "Your question")

    # A password in the question is refused, with a message that says how to fix it, and the text is kept.
    asker.get_by_label("Title").fill(title)
    asker.get_by_label("Your question").fill("I signed in and my password is Hunter2024 but the preview is silent.")
    asker.get_by_role("button", name="Post your question").click()
    asker.wait_for_load_state()
    assert_heading(asker, "Please check 1 answer: Ask a question")
    expect(asker.get_by_label("Title")).to_have_value(title)
    # the member's words are kept, so they only take out the password
    expect(asker.get_by_label("Your question")).to_have_value(
        "I signed in and my password is Hunter2024 but the preview is silent.")
    follow_error_link(asker, learn.SECRET_MESSAGE, "Your question")

    asker.get_by_label("Your question").fill(
        "When I open the preview of my cake shop website, NVDA says nothing. I tried pressing Tab.")
    asker.get_by_label("Screen reader, braille display or magnifier you use").fill("NVDA 2026.2")
    asker.get_by_label("Device").fill("Windows laptop")
    asker.get_by_role("button", name="Post your question").click()
    asker.wait_for_load_state()
    assert_heading(asker, f"Your question is posted: {title}")
    expect(asker.get_by_text("Members can now read and answer it.")).to_be_visible()
    audit(asker, problems, "question after asking")

    # The AI's suggested answer: done on a job page, then back here, clearly labelled.
    asker.get_by_role("button", name="Get a suggested answer from the AI while you wait").click()
    asker.wait_for_load_state()
    assert_heading(asker, f"The AI's suggested answer is ready: {title}")
    expect(asker.get_by_role("heading", level=3, name="Suggested answer from the AI")).to_be_visible()
    expect(asker.get_by_text(AI_LABEL)).to_be_visible()
    expect(asker.get_by_role("heading", level=4, name="Steps")).to_be_visible()
    expect(asker.get_by_role("button", name="Get a suggested answer from the AI while you wait")).to_have_count(0)
    audit(asker, problems, "question with the AI's answer")

    asker.get_by_role("button", name="Mark as answered").click()
    asker.wait_for_load_state()
    assert_heading(asker, f"Marked as answered: {title}")
    audit(asker, problems, "question marked as answered")
    asker.get_by_role("button", name="Mark as not answered yet").click()
    asker.wait_for_load_state()
    assert_heading(asker, f"Marked as not answered yet: {title}")
    audit(asker, problems, "question reopened")

    # Another member finds it in "not answered yet" and answers.
    helper = new_page(browser)
    sign_up(helper, base, "journey-helper@example.com", "Ben")
    signed_in(helper)
    # Help shows a code name unless the member chooses to show their name.
    helper.goto(base + "/account/profile")
    helper.get_by_label(re.compile(r"^Show my name in (Help|Ask the community) instead of my code name$")).check()
    helper.get_by_role("button", name="Save my profile").click()
    helper.wait_for_load_state()
    helper.goto(base + "/help")
    helper.get_by_role("link", name="Questions not answered yet").click()
    helper.wait_for_load_state()
    assert_heading(helper, "Ask the community: questions not answered yet")
    audit(helper, problems, "/help?show=open")
    helper.get_by_role("link", name=title).click()
    helper.wait_for_load_state()
    assert_heading(helper, title)
    expect(helper.get_by_role("button", name="Mark as answered")).to_have_count(0)
    audit(helper, problems, "question seen by another member")
    helper.get_by_role("button", name="Post your answer").click()
    helper.wait_for_load_state()
    assert_heading(helper, f"Please check 1 answer: {title}")
    audit(helper, problems, "answer with errors")
    follow_error_link(helper, "Enter your answer before posting it.", "Your answer")
    helper.get_by_label("Your answer").fill("Press **NVDA+Space** to switch to browse mode, then press H.")
    helper.get_by_role("button", name="Post your answer").click()
    helper.wait_for_load_state()
    assert_heading(helper, f"Your answer is posted: {title}")
    expect(helper.get_by_role("heading", level=3, name="Answer from Ben")).to_be_visible()
    # no "This helped me" for your own answer; the AI's answer has its own, clearly named button
    expect(helper.get_by_role("button", name="This helped me (answer 1, from Ben)")).to_have_count(0)
    ai_button = helper.get_by_role("button", name="This helped me (the AI's answer)", exact=True)
    expect(ai_button).to_be_visible()
    # what voice control users see is the whole name, so saying it works
    assert ai_button.inner_text().strip() == "This helped me (the AI's answer)"
    audit(helper, problems, "question after answering")

    # A third member marks Ben's answer as helpful, choosing the button by its full name.
    reader = new_page(browser)
    sign_up(reader, base, "journey-reader@example.com", "Cleo")
    signed_in(reader)
    reader.goto(base + "/help")
    reader.get_by_role("link", name=title).click()
    ben_button = reader.get_by_role("button", name="This helped me (answer 1, from Ben)", exact=True)
    assert ben_button.inner_text().strip() == "This helped me (answer 1, from Ben)"
    ben_button.click()
    reader.wait_for_load_state()
    assert_heading(reader, f"You marked an answer as helpful: {title}")
    expect(reader.get_by_text("1 member found this helpful.")).to_be_visible()
    expect(reader.get_by_text("You marked this answer as helpful.")).to_be_visible()
    audit(reader, problems, "question after marking helpful")
    assert not problems, "\n".join(problems)


def test_signed_out_visitors_can_read_and_are_offered_sign_in(live, browser):
    base, problems = live["base"], []
    writer = new_page(browser)
    sign_up(writer, base, "signed-out-writer@example.com", "Dev")
    signed_in(writer)
    writer.goto(base + "/help/ask")
    audit(writer, problems, "/help/ask")
    writer.get_by_label("Title").fill("Reading a long address: https://example.com/" + "a" * 120)
    writer.get_by_label("Your question").fill("# My setup\n\nTalkBack on a cheap Android phone.\n\n### What I tried\n\nEverything.")
    writer.get_by_role("button", name="Post your question").click()
    expect(writer.get_by_role("heading", level=1)).to_contain_text("Your question is posted: Reading a long address")
    audit(writer, problems, "question with a long title, at 320 pixels too")

    visitor = new_page(browser)
    visitor.goto(base + "/help")
    audit(visitor, problems, "/help signed out")
    visitor.get_by_role("link", name="Reading a long address", exact=False).click()
    visitor.wait_for_load_state()
    expect(visitor.get_by_role("heading", level=3, name="My setup")).to_be_visible()
    expect(visitor.get_by_role("heading", level=4, name="What I tried")).to_be_visible()
    expect(visitor.get_by_role("link", name="Sign in to answer this question")).to_be_visible()
    expect(visitor.get_by_role("button", name="Post your answer")).to_have_count(0)
    audit(visitor, problems, "question, signed out, long title")
    visitor.get_by_role("link", name="Sign in to answer this question").click()
    visitor.wait_for_load_state()
    assert_heading(visitor, "Sign in")
    audit(visitor, problems, "sign in from a question")
    assert not problems, "\n".join(problems)


def audit_without_javascript(page, browser, problems: list, label: str) -> None:
    """axe-core cannot run with JavaScript off (it needs timers), so check what can be checked here, then run
    the full page checks on the same address in a JavaScript-enabled tab signed in as the same member. The
    HTML is rendered on the server, so it is the same page."""
    h1 = page.get_by_role("heading", level=1)
    if h1.count() != 1 or not h1.inner_text().strip():
        problems.append(f"{label}: without JavaScript there is not exactly one h1 with text")
    if len(page.get_by_role("main").inner_text().strip()) < 100:
        problems.append(f"{label}: without JavaScript the main content is nearly empty")
    twin = page.context.browser.new_context(storage_state=page.context.storage_state(), bypass_csp=True,
                                             viewport={"width": 1280, "height": 900}).new_page()
    try:
        twin.goto(page.url)
        audit(twin, problems, label)
    finally:
        twin.context.close()


def test_learn_and_help_work_without_javascript(live, browser):
    base, problems = live["base"], []
    live["fake_ai"].on("help.ai_answer", AI_ANSWER)
    setup = new_page(browser)
    sign_up(setup, base, "nojs-learn@example.com", "Eve")
    signed_in(setup)

    page = new_page(browser, javascript=False)
    sign_in(page, base, "nojs-learn@example.com")
    signed_in(page)
    for path, h1 in [("/learn", "Learn and help"), ("/learn/start-here", learn.get_guide("start-here").title),
                     ("/learn/safe-web-tasks", learn.get_guide("safe-web-tasks").title),
                     ("/help", "Ask the community"), ("/help/ask", "Ask a question")]:
        page.goto(base + path)
        assert_heading(page, h1)
        audit_without_javascript(page, browser, problems, f"{path} without JavaScript")
    page.get_by_label("Title").fill("Braille display drops the cursor")
    page.get_by_label("Your question").fill("My Focus 40 loses the cursor in the answer box.")
    page.get_by_role("button", name="Post your question").click()
    assert_heading(page, "Your question is posted: Braille display drops the cursor")
    audit_without_javascript(page, browser, problems, "question without JavaScript")

    # Without JavaScript the AI's answer still arrives: the job page, then the question.
    page.get_by_role("button", name="Get a suggested answer from the AI while you wait").click()
    assert_heading(page, "The AI's suggested answer is ready: Braille display drops the cursor")
    expect(page.get_by_text(AI_LABEL)).to_be_visible()
    audit_without_javascript(page, browser, problems, "question with the AI's answer without JavaScript")

    # The error summary's link moves focus to the field without any script.
    page.get_by_role("button", name="Post your answer").click()
    assert_heading(page, "Please check 1 answer: Braille display drops the cursor")
    follow_error_link(page, "Enter your answer before posting it.", "Your answer")
    page.get_by_label("Your answer").fill("Update: it works after restarting NVDA.")
    page.get_by_role("button", name="Post your answer").click()
    assert_heading(page, "Your answer is posted: Braille display drops the cursor")
    # Help shows the member's code name, since Eve did not choose to show her name there.
    expect(page.get_by_role("heading", level=3, name=re.compile(r"^Reply from Member [0-9A-F]{4}, who asked$"))).to_be_visible()
    audit_without_javascript(page, browser, problems, "question after replying without JavaScript")

    page.get_by_role("button", name="Mark as answered").click()
    assert_heading(page, "Marked as answered: Braille display drops the cursor")
    audit_without_javascript(page, browser, problems, "question marked as answered without JavaScript")
    assert not problems, "\n".join(problems)


def test_keyboard_order_on_the_ask_form(live, browser):
    base = live["base"]
    page = new_page(browser)
    sign_up(page, base, "keyboard-learn@example.com", "Fay")
    signed_in(page)
    page.goto(base + "/help/ask")
    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="Skip to main content")).to_be_focused()
    title_field = page.get_by_label("Title")
    footer = page.get_by_role("contentinfo")
    for _ in range(60):
        page.keyboard.press("Tab")
        if title_field.evaluate("el => el === document.activeElement"):
            break
        for name in FOOTER_LINKS:
            assert not footer.get_by_role("link", name=name, exact=True).evaluate(
                "el => el === document.activeElement"), f"reached the footer link {name!r} before the Title field"
    else:
        pytest.fail("Tab never reached the Title field")
    # the skip link itself takes you straight to the main content
    page.goto(base + "/help/ask")
    page.keyboard.press("Tab")
    page.keyboard.press("Enter")
    expect(page.get_by_role("main")).to_be_focused()
