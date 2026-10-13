"""What studio/static/enhance.js adds, in a real browser, and that each page still works without it.

- The error summary is read once: focus moves to it and it stops being an alert (it stays one without JavaScript).
- Each prepared answer box gets a Copy button, and one status message says whether the copy worked.
- The Write document page asks before leaving with changes to "Your text" that were not sent.
"""
import pytest

from .conftest import check_page, check_page_narrow, new_page, sign_up
from .test_journey_errands import PREPARED, TASK_CONTRACT, TASK_RESEARCH, _expect_h1, _new_errand
from .test_journey_write import h1, start_document

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

pytestmark = pytest.mark.browser


def assert_clean(page, label: str) -> None:
    problems = check_page(page, label) + check_page_narrow(page, label)
    assert not problems, "\n".join(problems)


def _summary(page):
    return page.locator(".error-summary").filter(has=page.get_by_role("heading", name="What to check", exact=True))


def test_error_summary_is_focused_and_read_once(live, browser):
    base = live["base"]
    for javascript in (True, False):
        page = new_page(browser, javascript=javascript)
        page.goto(base + "/login")
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_load_state()
        # The h1 and the title give the count; the summary's heading does not say it again.
        assert h1(page) == "Please check 2 answers: Sign in"
        summary = _summary(page)
        expect(summary).to_have_count(1)
        expect(summary.get_by_role("link")).to_have_count(2)
        assert "Please check" not in summary.inner_text()
        if javascript:
            # Focus moves to the summary, which reads it; it is no longer also an alert, so it is read once.
            expect(summary).to_be_focused()
            expect(page.get_by_role("alert")).to_have_count(0)
            assert summary.get_attribute("role") is None
            assert_clean(page, "sign-in errors")
        else:
            # Without JavaScript nothing moves focus, so the summary stays an alert, named by its heading.
            expect(page.get_by_role("alert", name="What to check", exact=True)).to_be_visible()
        page.context.close()


# A second answer with real text to copy, and a gap in it. "Library card number" is only a gap, so it has no box.
COPY_PREPARED = {**PREPARED, "prepared_answers": PREPARED["prepared_answers"] + [
    {"field_label": "Address", "suggested_answer": "12 Elm Street, [your postcode]", "note": ""}]}


def _ready_page(live, browser, email: str, javascript: bool = True):
    base, fake = live["base"], live["fake_ai"]
    fake.on("errands.understand", TASK_CONTRACT)
    fake.on("errands.task_research", TASK_RESEARCH).on("errands.task_prepare", COPY_PREPARED)
    page = new_page(browser, javascript=javascript)
    sign_up(page, base, email, "Copy Tester")
    _new_errand(page, base, "Help me do a task on a website", "Help me renew my library card on the library website")
    page.get_by_role("button", name="This is right: go ahead").click()
    _expect_h1(page, "Ready for you to do: Help me renew my library card on the library website")
    return page


def test_prepared_answers_have_copy_buttons(live, browser):
    page = _ready_page(live, browser, "copy-answers@example.com")
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live["base"])
    answer = page.get_by_role("textbox", name="Answer for: Full name")
    button = page.get_by_role("button", name="Copy the answer for Full name", exact=True)
    expect(button).to_be_visible()
    # An answer that is only a gap has nothing to copy, so it has no Copy button.
    expect(page.get_by_role("button", name="Copy the answer for Library card number")).to_have_count(0)
    expect(page.get_by_role("button", name="Copy the answer for Address", exact=True)).to_be_visible()
    # The button comes straight after its box, by keyboard too.
    answer.focus()
    page.keyboard.press("Tab")
    expect(button).to_be_focused()
    assert button.evaluate("b => b.type") == "button"  # never sends a form
    assert_clean(page, "ready page with Copy buttons")

    # Copying puts the answer on the clipboard and says so in one status message.
    statuses_before = page.get_by_role("status").count()
    page.keyboard.press("Enter")
    status = page.get_by_role("status").filter(has_text="Copied: the answer for Full name")
    expect(status).to_have_count(1)
    assert page.evaluate("navigator.clipboard.readText()") == "Amina Yusuf"
    expect(button).to_be_focused()
    seen = page.locator(".copy-result")
    expect(seen.nth(0)).to_have_text("Copied")
    page.get_by_role("button", name="Copy the answer for Address", exact=True).click()
    # The status says the answer has a gap to fill in.
    expect(page.get_by_role("status").filter(
        has_text="Copied: the answer for Address. It has a gap to fill in.")).to_have_count(1)
    assert page.get_by_role("status").count() == statuses_before  # the same message is reused
    assert page.evaluate("navigator.clipboard.readText()") == "12 Elm Street, [your postcode]"
    # Only the latest copy shows the visible word "Copied".
    expect(seen.nth(0)).to_have_text("")
    expect(seen.nth(1)).to_have_text("Copied")

    # When the browser does not let the page copy, it says how to do it by hand, and the text is selected.
    page.evaluate("""() => {
        navigator.clipboard.writeText = () => Promise.reject(new Error("blocked"));
        document.execCommand = () => false;
    }""")
    button.click()
    expect(page.get_by_role("status").filter(
        has_text="Could not copy. Select the text in the box and copy it.")).to_have_count(1)
    expect(answer).to_be_focused()
    assert answer.evaluate("el => el.value.substring(el.selectionStart, el.selectionEnd)") == "Amina Yusuf"
    page.context.close()


def test_prepared_answers_without_javascript_have_no_copy_buttons(live, browser):
    page = _ready_page(live, browser, "copy-nojs@example.com", javascript=False)
    expect(page.get_by_role("textbox", name="Answer for: Full name")).to_have_value("Amina Yusuf")
    expect(page.get_by_role("button", name="Copy the answer for Full name")).to_have_count(0)
    page.context.close()


def test_write_asks_before_leaving_with_unsent_changes(live, browser):
    base = live["base"]
    page = new_page(browser)
    sign_up(page, base, "write-leave@example.com", "Write Leave")
    start_document(page, base, "Leaving essay", check=lambda *a: None)
    dialogs = []

    def on_dialog(dialog):
        dialogs.append(dialog.type)
        dialog.dismiss()  # stay on the page

    page.on("dialog", on_dialog)
    leave = page.get_by_role("link", name="Write and publish", exact=True).first

    # Typed but not saved: leaving asks, and staying keeps every word.
    text = page.get_by_role("textbox", name="Your text")
    text.click()
    page.keyboard.type("Words not saved yet.")
    leave.click()
    page.wait_for_timeout(500)
    assert dialogs == ["beforeunload"]
    assert h1(page) == "Leaving essay: new document started"
    assert text.input_value().endswith("Words not saved yet.")

    # Saving sends a form, which never asks.
    page.get_by_role("button", name="Save my text").click()
    page.wait_for_load_state()
    assert dialogs == ["beforeunload"]
    assert h1(page) != "Leaving essay: new document started"
    assert page.get_by_role("textbox", name="Your text").input_value().endswith("Words not saved yet.")

    # Nothing changed since the page opened: leaving does not ask.
    page.get_by_role("textbox", name="Your text").click()
    leave.click()
    page.wait_for_load_state()
    assert dialogs == ["beforeunload"] and h1(page) == "Write and publish"
    page.context.close()
