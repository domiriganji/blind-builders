"""AI tools report, driven in Chromium the way a screen reader user moves: every control is found by its role
and accessible name or by its label, never by CSS, ids or XPath, so a passing test also proves each control
has a usable name. Every page reached must pass check_page and check_page_narrow.

The section uses no AI, so no fake answers are registered; the journeys check the fake AI is never called.
"""
import itertools

import pytest
from playwright.sync_api import expect

from studio.features import ai_tools

from .conftest import check_page, check_page_narrow, new_page, sign_in, sign_up

pytestmark = pytest.mark.browser

_emails = itertools.count(1)
TOOL = "Claude Code (terminal)"
VERSION_LABEL = f"Version of {TOOL}"
CHECK_QUESTIONS = [question for _key, question, _statement, _hint in ai_tools.CHECKS]


def email(name: str) -> str:
    return f"ai-tools-{name}-{next(_emails)}@example.com"


def checked(page, label: str = "") -> None:
    """Assert the page in front of us passes every check, at full width and at 320 pixels."""
    problems = check_page(page, label) + check_page_narrow(page, label)
    assert not problems, "\n".join(problems)


def checked_without_js(page, viewer, label: str) -> None:
    """check_page needs scripts (axe runs in the page), so a page rendered with JavaScript off is checked by
    copying its exact HTML into a second page on the same site that may run scripts."""
    assert len(page.get_by_role("main").inner_text().strip()) > 20, f"{label}: main is empty without JavaScript"
    viewer.set_content(page.content())
    checked(viewer, label + " (JavaScript off)")


def heading_1(page) -> str:
    return " ".join(page.get_by_role("heading", level=1).inner_text().split())


def choose(page, question: str, answer: str) -> None:
    page.get_by_role("group", name=question).get_by_role("radio", name=answer, exact=True).check()


def fill_report(page, version: str, overall: str = "Usable with workarounds", answer: str = "Yes",
                tool: str = TOOL) -> None:
    page.get_by_label(f"Version of {tool}", exact=True).fill(version)
    page.get_by_label("Screen reader", exact=True).fill("NVDA")
    page.get_by_label("Screen reader version").fill("2026.2")
    page.get_by_label("Operating system").fill("Windows 11")
    for question in CHECK_QUESTIONS:
        choose(page, question, answer)
    choose(page, "Overall, could you use it?", overall)
    page.get_by_role("textbox", name="Workarounds (optional)").fill("Turn on the bell and use **NVDA+6** to review.")
    page.get_by_role("textbox", name="Anything else (optional)").fill("Works well in Windows Terminal.")


def publish(page) -> None:
    page.get_by_role("button", name="Publish your report").click()
    page.wait_for_load_state()


def open_tool_from_index(page, base: str, name: str = TOOL) -> None:
    page.goto(base + "/ai-tools")
    checked(page, "AI tools report")
    page.get_by_role("link", name=name, exact=True).click()
    page.wait_for_load_state()
    assert heading_1(page) == f"How {name} works with screen readers"
    checked(page, f"{name} page")


def test_member_reports_fixes_mistakes_updates_and_deletes(live, browser):
    base, calls_before = live["base"], len(live["fake_ai"].calls)
    page = new_page(browser)
    sign_up(page, base, email("journey"), "AI Tools Tester")
    open_tool_from_index(page, base)
    expect(page.get_by_role("heading", name="Member reports: 0", level=2)).to_be_visible()
    expect(page.get_by_text("This site does not test the tools or check the reports.").first).to_be_visible()

    # Error path: publish an empty form.
    publish(page)
    assert heading_1(page) == f"How {TOOL} works with screen readers: please fix the problems"
    checked(page, "form with mistakes")
    summary = page.get_by_role("alert")
    expect(summary.get_by_role("heading", name="Please check 10 answers")).to_be_visible()
    # Following an error link moves focus to the field that needs fixing, for a text field...
    summary.get_by_role("link", name="Enter the version of the tool you used, for example 2.1.296.").click()
    expect(page.get_by_label(VERSION_LABEL, exact=True)).to_be_focused()
    # ...and for a question with choices, to its first choice.
    question = CHECK_QUESTIONS[0]
    summary.get_by_role("link", name=f"Choose yes, no or I did not test this for: {question}").click()
    expect(page.get_by_role("group", name=question).get_by_role("radio", name="Yes", exact=True)).to_be_focused()
    summary.get_by_role("link", name="Choose your overall verdict", exact=False).click()
    expect(page.get_by_role("group", name="Overall, could you use it?")
           .get_by_role("radio", name="Usable", exact=True)).to_be_focused()

    # A mistake in one field keeps everything else the member typed.
    fill_report(page, "2.1.296")
    page.get_by_role("textbox", name="Anything else (optional)").fill("my key is sk-ant-api03-abcdefghijklmnop")
    publish(page)
    checked(page, "secret refused")
    page.get_by_role("alert").get_by_role("link", name="Anything else looks like it has a password",
                                          exact=False).click()
    expect(page.get_by_role("textbox", name="Anything else (optional)")).to_be_focused()
    expect(page.get_by_label(VERSION_LABEL, exact=True)).to_have_value("2.1.296")
    expect(page.get_by_role("group", name=question).get_by_role("radio", name="Yes", exact=True)).to_be_checked()
    page.get_by_role("textbox", name="Anything else (optional)").fill("Works well in Windows Terminal.")
    publish(page)

    # Published: the h1 says so, and the report is under its version heading, labelled as the member's words.
    assert heading_1(page) == f"Your report on {TOOL} version 2.1.296 is published"
    checked(page, "report published")
    expect(page.get_by_role("heading", name="Version 2.1.296: 1 report", level=3)).to_be_visible()
    report = page.get_by_role("article", name="Usable with workarounds: NVDA 2026.2 on Windows 11")
    expect(report).to_be_visible()
    expect(report.get_by_text("Workarounds, in the member's words:")).to_be_visible()
    expect(report.get_by_text("You can tell when it has finished: yes")).to_be_visible()
    expect(report.get_by_role("button", name="Delete your report for version 2.1.296")).to_be_visible()

    # Reporting the same version again updates it.
    page.reload()
    assert heading_1(page) == f"How {TOOL} works with screen readers"
    fill_report(page, "v2.1.296", overall="Usable")
    publish(page)
    assert heading_1(page) == f"Your report on {TOOL} version v2.1.296 is updated"
    checked(page, "report updated")
    expect(page.get_by_role("heading", name="Version v2.1.296: 1 report", level=3)).to_be_visible()

    # A newer version that members say is worse gets a notice that says it is only a comparison.
    fill_report(page, "2.1.300", overall="Not usable", answer="No")
    publish(page)
    checked(page, "second version")
    expect(page.get_by_text("Possible problem after an update.")).to_be_visible()
    expect(page.get_by_text("nobody has tested it on this site", exact=False)).to_be_visible()

    # The JSON has the reports and says what was and was not checked.
    data = page.request.get(base + "/ai-tools/claude-code/reports.json").json()
    assert [r["tool_version"] for r in data["reports"]] == ["2.1.300", "v2.1.296"]
    assert "does not test" in data["reports_about"]

    # Deleting: the h1 says what happened, and the report is gone.
    page.get_by_role("button", name="Delete your report for version 2.1.300").click()
    page.wait_for_load_state()
    assert heading_1(page) == f"Your report on {TOOL} version 2.1.300 is deleted"
    checked(page, "report deleted")
    expect(page.get_by_role("button", name="Delete your report for version 2.1.300")).to_have_count(0)
    expect(page.get_by_role("heading", name="Member reports: 1", level=2)).to_be_visible()

    # The index shows the member verdict for the tool.
    page.get_by_role("link", name="Back to all AI tools").click()
    page.wait_for_load_state()
    checked(page, "index after reporting")
    row = page.get_by_role("row", name=TOOL, exact=False)
    expect(row.get_by_text("1 usable, 0 with workarounds, 0 not usable")).to_be_visible()

    assert len(live["fake_ai"].calls) == calls_before  # nothing in this section uses the AI


def test_reader_signed_out_signs_in_to_report(live, browser):
    base = live["base"]
    address = email("reader")
    sign_up(new_page(browser), base, address, "Signed Out Reader")
    page = new_page(browser)
    open_tool_from_index(page, base, "Cursor")
    # Research is dated, has sources, and says nobody on this site tested the tool.
    expect(page.get_by_role("heading", name="What the research found (October 2026)", level=2)).to_be_visible()
    expect(page.get_by_text("Nobody on this site tested the tool.", exact=False)).to_be_visible()
    expect(page.get_by_role("heading", name="Sources for the research (3)", level=3)).to_be_visible()
    expect(page.get_by_role("link", name="Cursor release notes")).to_have_attribute("rel", "noopener noreferrer")
    expect(page.get_by_role("button", name="Publish your report")).to_have_count(0)
    # The in-page link goes to the report section, where signed-out readers are told how to add a report.
    page.get_by_role("link", name="Report how Cursor worked for you").click()
    page.get_by_role("link", name="sign in", exact=True).click()
    page.wait_for_load_state()
    checked(page, "sign in page")
    page.get_by_label("Email address").fill(address)
    page.get_by_label("Password").fill("correct horse battery")
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_load_state()
    assert heading_1(page) == "How Cursor works with screen readers"
    checked(page, "Cursor page signed in")
    fill_report(page, "3.23.12", tool="Cursor")
    publish(page)
    assert heading_1(page) == "Your report on Cursor version 3.23.12 is published"
    checked(page, "Cursor report published")


def test_unknown_tool_page_is_a_plain_not_found(live, browser):
    page = new_page(browser)
    response = page.goto(live["base"] + "/ai-tools/no-such-tool")
    assert response.status == 404
    checked(page, "unknown tool")
    expect(page.get_by_text("We could not find that tool.", exact=False)).to_be_visible()


def test_key_pages_work_without_javascript(live, browser):
    base = live["base"]
    address = email("nojs")
    sign_up(new_page(browser), base, address, "No Script")
    page = new_page(browser, javascript=False)
    sign_in(page, base, address)
    viewer = new_page(browser)
    viewer.goto(base + "/about")
    page.goto(base + "/ai-tools")
    assert heading_1(page) == "AI tools report"
    checked_without_js(page, viewer, "index")
    expect(page.get_by_role("table")).to_be_visible()
    page.get_by_role("link", name="Gemini CLI", exact=True).click()
    page.wait_for_load_state()
    assert heading_1(page) == "How Gemini CLI works with screen readers"
    checked_without_js(page, viewer, "tool page")
    # The error path and publishing work with plain HTML forms.
    page.get_by_role("button", name="Publish your report").click()
    page.wait_for_load_state()
    assert heading_1(page) == "How Gemini CLI works with screen readers: please fix the problems"
    checked_without_js(page, viewer, "form with mistakes")
    expect(page.get_by_role("heading", name="Please check 10 answers")).to_be_visible()
    page.get_by_role("link", name="Enter the version of the tool you used, for example 2.1.296.").click()
    expect(page.get_by_label("Version of Gemini CLI", exact=True)).to_be_focused()
    page.get_by_label("Version of Gemini CLI", exact=True).fill("0.63.0")
    page.get_by_label("Screen reader", exact=True).fill("Orca")
    page.get_by_label("Operating system").fill("Ubuntu 24.04")
    for question in CHECK_QUESTIONS:
        choose(page, question, "I did not test this")
    choose(page, "Overall, could you use it?", "Usable")
    page.get_by_role("button", name="Publish your report").click()
    page.wait_for_load_state()
    assert heading_1(page) == "Your report on Gemini CLI version 0.63.0 is published"
    checked_without_js(page, viewer, "report published")
    page.get_by_role("button", name="Delete your report for version 0.63.0").click()
    page.wait_for_load_state()
    assert heading_1(page) == "Your report on Gemini CLI version 0.63.0 is deleted"
    checked_without_js(page, viewer, "report deleted")


def test_keyboard_order_on_the_report_form(live, browser):
    base = live["base"]
    page = new_page(browser)
    sign_up(page, base, email("keys"), "Keyboard User")
    page.goto(base + "/ai-tools/claude-code")
    checked(page, "form page")
    first_field = page.get_by_label(VERSION_LABEL, exact=True)
    footer_link = page.get_by_role("link", name="How this site works")

    def focused(locator) -> bool:
        return locator.evaluate("el => el === document.activeElement")

    page.keyboard.press("Tab")
    assert focused(page.get_by_role("link", name="Skip to main content"))
    reached_field = reached_footer = None
    for step in range(1, 400):
        page.keyboard.press("Tab")
        if reached_field is None and focused(first_field):
            reached_field = step
        if reached_footer is None and focused(footer_link):
            reached_footer = step
            break
    assert reached_field is not None, "Tab never reached the version field"
    assert reached_footer is None or reached_field < reached_footer
