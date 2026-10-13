"""Find before you build and the Tool library, driven in Chromium the way a screen reader user does.

Every control is found by its role and accessible name, or by its label, never by CSS selectors, ids
or XPath, so the test itself proves each control has a usable name. Every page the journeys reach is
checked with check_page (axe-core, one h1 that matches the title, a main landmark, no sideways
scrolling) at desktop width and at 320 pixels.
"""
import re

import pytest
from playwright.sync_api import expect

from studio.ai import ResearchResult, Source

from .conftest import check_page, check_page_narrow, new_page, sign_in, sign_up

pytestmark = pytest.mark.browser

ERROR_SUMMARY = "What to check"  # the h1 gives the count, so the summary heading does not repeat it

RESEARCH = ResearchResult(
    text="## Summary\nTwo tools exist.\n\nAI Content Describer (https://github.com/cartertemm/AI-content-describer) ...",
    sources=[Source(url="https://github.com/cartertemm/AI-content-describer", title="AI Content Describer on GitHub",
                    quote="Describes the focus object"),
             Source(url="https://menu.example.org/about", title="Menu Reader")])
EXTRACTED = {
    "summary": "Yes. **Two tools** already do this.",
    "candidates": [
        {"name": "AI Content Describer", "url": "https://github.com/cartertemm/AI-content-describer/",
         "what_it_does": "Describes the screen in NVDA.", "accessibility_notes": "Made for NVDA users.", "cost": "Free"},
        {"name": "Menu Reader", "url": "https://menu.example.org", "what_it_does": "Reads restaurant menus aloud.",
         "accessibility_notes": "The maker says it works with TalkBack.", "cost": "Free with ads"},
    ],
}


class Pages:
    """Collects check_page problems from every page a journey reaches."""

    def __init__(self, page):
        self.page = page
        self.problems: list[str] = []
        self.count = 0

    def check(self, label: str) -> None:
        self.page.wait_for_load_state()
        self.count += 1
        self.problems += check_page(self.page, label) + check_page_narrow(self.page, label)

    def assert_clean(self) -> None:
        assert not self.problems, "\n".join(self.problems)


class NoScriptPages(Pages):
    """For a page with JavaScript off. axe-core cannot run there, so this checks that the page shows its h1
    and content, then opens the same address, signed in as the same member, with JavaScript on and runs
    check_page on that copy."""

    def __init__(self, page, browser):
        super().__init__(page)
        self.browser = browser

    def check(self, label: str) -> None:
        self.page.wait_for_load_state()
        h1s = self.page.get_by_role("heading", level=1)
        if h1s.count() != 1 or not h1s.inner_text().strip():
            self.problems.append(f"{label}: no single h1 without JavaScript")
        if len(self.page.get_by_role("main").inner_text().strip()) < 20:
            self.problems.append(f"{label}: main content is empty without JavaScript")
        context = self.browser.new_context(bypass_csp=True, storage_state=self.page.context.storage_state(),
                                           viewport={"width": 1280, "height": 900})
        try:
            mirror = context.new_page()
            mirror.goto(self.page.url)
            self.count += 1
            self.problems += check_page(mirror, label) + check_page_narrow(mirror, label)
        finally:
            context.close()


def expect_h1(page, text) -> None:
    """Wait for the page whose h1 is `text` (a string, or a pattern), then let it finish loading."""
    expect(page.get_by_role("heading", level=1)).to_have_text(text)
    page.wait_for_load_state()


def is_focused(locator) -> bool:
    return locator.evaluate("el => el === document.activeElement")


def follow_error(page, message: str, field) -> None:
    """The error summary is shown; its link for `message` moves focus to `field`."""
    summary = page.locator(".error-summary").filter(has=page.get_by_role("heading", name=ERROR_SUMMARY, exact=True))
    expect(summary).to_be_visible()
    summary.get_by_role("link", name=message).click()
    assert is_focused(field), f"following the error {message!r} did not move focus to its field"


# ----- Find before you build ------------------------------------------------------------------

def test_search_the_directory_and_report_on_a_tool(live, browser):
    base = live["base"]
    page = new_page(browser)
    pages = Pages(page)
    page.goto(base + "/find")
    pages.check("find index signed out")
    expect(page.get_by_role("link", name="Sign in to ask the AI to search the web")).to_be_visible()

    sign_up(page, base, "find-search@example.com", "Find Searcher")
    page.goto(base + "/find")
    expect_h1(page, re.compile(r"^Find before you build: page 1 of \d+$"))
    pages.check("find index")

    search = page.get_by_role("search", name="Search the directory")
    search.get_by_label("What are you looking for?").fill("nvda")
    search.get_by_label("Category").select_option(label="Screen reader add-on")
    search.get_by_role("button", name="Search the directory").click()
    expect_h1(page, re.compile(r'^\d+ tools match "nvda" in the category Screen reader add-on$'))
    pages.check("find search results")
    assert page.get_by_label("What are you looking for?").input_value() == "nvda"
    expect(page.get_by_role("link", name="Show all tools")).to_be_visible()

    page.get_by_role("link", name="AI Content Describer", exact=True).click()
    expect_h1(page, "AI Content Describer: member reports")
    pages.check("find tool page")
    assert page.get_by_role("link", name="Visit AI Content Describer").get_attribute("rel") == "noopener noreferrer"

    # A report with no answer to "How did it go?" shows the error summary.
    page.get_by_label("What happened").fill("Describes images well.")
    page.get_by_role("button", name="Save how it worked for me").click()
    expect_h1(page, "Please check 1 answer: AI Content Describer: member reports")
    pages.check("find tool page with report errors")
    assert page.get_by_label("What happened").input_value() == "Describes images well."
    follow_error(page, "Choose whether the tool worked for you.", page.get_by_role("radio", name="It worked for me"))

    page.get_by_role("radio", name="It worked for me").check()
    page.get_by_label("Screen reader, braille display or magnifier you used").fill("NVDA with a Focus 40")
    page.get_by_role("button", name="Save how it worked for me").click()
    # The title and h1 say what happened, and the page opens at the top, where the thanks are.
    expect_h1(page, "Your report is saved: AI Content Describer")
    assert "#" not in page.url
    expect(page.get_by_role("status").filter(has_text="Thank you for letting other members know")).to_be_in_viewport()
    expect(page.get_by_role("heading", name="What members say (1 report)")).to_be_visible()
    expect(page.get_by_text("1 member: 1 worked. Tools used: NVDA with a Focus 40").first).to_be_visible()
    pages.check("find tool page after report")

    # Pages of results: numbered, before and after the list.
    page.get_by_role("link", name="Back to Find before you build").click()
    top = page.get_by_role("navigation", name="Pages of tools", exact=True)
    expect(top.get_by_role("link", name="Page 1", exact=True)).to_have_attribute("aria-current", "page")
    box = top.get_by_role("link", name=re.compile(r"^Next page \(2 of \d+\)$")).bounding_box()
    assert box["height"] >= 44, box
    top.get_by_role("link", name=re.compile(r"^Next page \(2 of \d+\)$")).click()
    expect_h1(page, re.compile(r"^Find before you build: page 2 of \d+$"))
    pages.check("find index page 2")
    page.get_by_role("navigation", name="Pages of tools, after the list") \
        .get_by_role("link", name=re.compile(r"^Previous page \(1 of \d+\)$")).click()
    expect_h1(page, re.compile(r"^Find before you build: page 1 of \d+$"))
    assert pages.count >= 7
    pages.assert_clean()


def test_ask_the_ai_and_add_what_it_found(live, browser):
    base = live["base"]
    live["fake_ai"].on("find.search", RESEARCH).on("find.extract", EXTRACTED)
    page = new_page(browser)
    pages = Pages(page)
    sign_up(page, base, "find-ask@example.com", "Find Asker")
    page.goto(base + "/find/ask")
    expect_h1(page, "Ask the AI what already exists")
    pages.check("ask form")

    # Too short: the error summary links to the field.
    page.get_by_label("What do you want to do or make?").fill("hi")
    page.get_by_role("button", name="Search the web").click()
    expect_h1(page, "Please check 1 answer: Ask the AI what already exists")
    pages.check("ask form with errors")
    follow_error(page, "Say in a few words what you want to do or make, for example: read restaurant menus aloud.",
                 page.get_by_label("What do you want to do or make?"))

    page.get_by_label("What do you want to do or make?").fill("read restaurant menus aloud")
    page.get_by_role("button", name="Search the web").click()
    expect_h1(page, "What already exists for: read restaurant menus aloud")
    pages.check("search result")
    expect(page.get_by_text("Written by the AI").first).to_be_visible()
    expect(page.get_by_text("Nobody on this site has checked them")).to_be_visible()
    expect(page.get_by_role("heading", name="Web pages the AI used (2)")).to_be_visible()
    expect(page.get_by_role("link", name="Read member reports on AI Content Describer")).to_be_visible()
    expect(page.get_by_role("link", name="Source: Menu Reader")).to_be_visible()

    # Adding opens a form filled in with the AI's notes, so the member checks them before anything is added.
    page.get_by_role("link", name="Add Menu Reader to the directory").click()
    expect_h1(page, "Add Menu Reader to the directory")
    pages.check("add form filled in from a search")
    assert page.get_by_label("Web address").input_value() == "https://menu.example.org"
    assert page.get_by_label("What it does").input_value() == "Reads restaurant menus aloud."
    page.get_by_role("button", name="Add the tool").click()
    expect_h1(page, "Added to the directory: Menu Reader")
    expect(page.get_by_role("status").filter(has_text="Menu Reader is now in the directory")).to_be_visible()
    expect(page.get_by_text("Description written by the AI during a web search; not checked.")).to_be_visible()
    pages.check("tool added from a search")

    # The search is listed for its owner, and opening it again shows Menu Reader as in the directory.
    page.goto(base + "/find")
    page.get_by_role("link", name="read restaurant menus aloud").click()
    expect(page.get_by_role("link", name="Read member reports on Menu Reader")).to_be_visible()
    assert page.get_by_role("link", name="Add Menu Reader to the directory").count() == 0
    pages.check("search result again")
    pages.assert_clean()


def test_add_a_tool_by_hand(live, browser):
    base = live["base"]
    page = new_page(browser)
    pages = Pages(page)
    sign_up(page, base, "find-add@example.com", "Find Adder")
    page.goto(base + "/find")
    page.get_by_role("link", name="Add a tool you know to the directory").click()
    expect_h1(page, "Add a tool to the directory")
    pages.check("add form")

    page.get_by_label("Web address").fill("not a web address")
    page.get_by_role("button", name="Add the tool").click()
    expect_h1(page, "Please check 3 answers: Add a tool to the directory")
    pages.check("add form with errors")
    assert page.get_by_label("Web address").input_value() == "not a web address"
    follow_error(page, "Enter the tool's name.", page.get_by_label("Tool's name"))
    follow_error(page, "Enter the tool's web address, for example example.com/tool or https://example.com/tool.",
                 page.get_by_label("Web address"))

    page.get_by_label("Tool's name").fill("Talking Scale")
    page.get_by_label("Web address").fill("scale.example.org/app")  # as dictated, without https://
    page.get_by_label("What it does").fill("A kitchen scale app that speaks.")
    page.get_by_label("Category").select_option(label="Everyday tasks")
    page.get_by_role("button", name="Add the tool").click()
    expect_h1(page, "Added to the directory: Talking Scale")
    expect(page.get_by_role("status").filter(has_text="at https://scale.example.org/app")).to_be_visible()
    pages.check("tool added by hand")

    # The member who added it can still change it.
    page.get_by_role("link", name="Change the details of Talking Scale").click()
    expect_h1(page, "Change the details of Talking Scale")
    pages.check("change form")
    page.get_by_label("What it does").fill("A kitchen scale app that speaks the weight.")
    page.get_by_role("button", name="Save my changes").click()
    expect_h1(page, "Your changes are saved: Talking Scale")
    expect(page.get_by_text("A kitchen scale app that speaks the weight.")).to_be_visible()

    # The same address again goes to the existing entry.
    page.goto(base + "/find/add")
    page.get_by_label("Tool's name").fill("Copy")
    page.get_by_label("Web address").fill("http://www.scale.example.org/app/")
    page.get_by_label("What it does").fill("Copy.")
    page.get_by_role("button", name="Add the tool").click()
    expect_h1(page, "Already in the directory: Talking Scale")
    expect(page.get_by_role("status").filter(has_text="it was not added again")).to_be_visible()
    pages.assert_clean()


# ----- Tool library ---------------------------------------------------------------------------

def test_library_add_report_resolve_and_volunteer(live, browser):
    base = live["base"]
    page = new_page(browser)
    pages = Pages(page)
    page.goto(base + "/library")
    expect_h1(page, "Tool library")
    pages.check("library signed out")

    sign_up(page, base, "library@example.com", "Library Member")
    page.goto(base + "/library")
    pages.check("library")
    page.get_by_role("link", name=re.compile(r"^Need help after an update \(\d+\)$")).click()
    assert page.get_by_role("navigation", name="Show tools").get_by_role("link", name=re.compile("Need help")) \
        .get_attribute("aria-current") == "page"
    pages.check("library upkeep filter")

    page.get_by_role("link", name="Add a tool a blind or low-vision person made").click()
    expect_h1(page, "Add a tool a blind or low-vision person made")
    pages.check("library add form")
    page.get_by_label("Tool's name").fill("Talking Timer")
    page.get_by_label("Web address").fill("https://timer.example.org")
    page.get_by_label("What it does").fill("A timer that speaks.")
    page.get_by_role("button", name="Add the tool to the library").click()
    expect_h1(page, "Please check 1 answer: Add a tool a blind or low-vision person made")
    pages.check("library add form with errors")
    checkbox = page.get_by_role("checkbox", name="A blind or low-vision person made this")
    follow_error(page, "Tick the box to confirm a blind or low-vision person made this tool. "
                       "Other tools belong in Find before you build.", checkbox)
    expect(checkbox).to_have_accessible_description(re.compile("Tick the box to confirm"))
    checkbox.check()
    page.get_by_role("button", name="Add the tool to the library").click()
    expect_h1(page, "Added to the Tool library: Talking Timer")
    expect(page.get_by_role("status").filter(has_text="Talking Timer is now in the Tool library")).to_be_visible()
    pages.check("library tool page")

    # The page lists its three forms at the top; each link moves focus to its heading.
    on_this_page = page.get_by_role("navigation", name="On this page")
    on_this_page.get_by_role("link", name="Report a problem after an update").click()
    assert is_focused(page.get_by_role("heading", name="Report a problem after an update"))

    # Report a problem: first with mistakes.
    page.get_by_role("button", name="Send the problem to helpers").click()
    expect_h1(page, "Please check 2 answers: Talking Timer in the Tool library")
    pages.check("library tool page with problem errors")
    follow_error(page, "Say what stopped working.", page.get_by_label("What broke"))
    page.get_by_label("What broke").fill("The time is not announced any more.")
    page.get_by_label("After which update").fill("NVDA 2026.1")
    page.get_by_role("button", name="Send the problem to helpers").click()
    expect_h1(page, "Your problem report is saved: Talking Timer")
    expect(page.get_by_role("heading", name="Open problems (1)")).to_be_visible()
    pages.check("library tool page with a problem")

    page.get_by_role("button", name="Mark problem 1 as fixed").click()
    expect_h1(page, "Problem marked as fixed: Talking Timer")
    expect(page.get_by_role("status").filter(has_text="No open problems are left")).to_be_visible()
    expect(page.get_by_role("heading", name="Fixed problems (1)")).to_be_visible()
    pages.check("library tool page after fix")

    # Volunteer: with mistakes, then for real, then update, then leave.
    page.get_by_role("button", name="Add me to the list").click()
    expect_h1(page, "Please check 1 answer: Talking Timer in the Tool library")
    pages.check("library volunteer errors")
    follow_error(page, "Say how you can help, for example: I test with JAWS on Windows 11, or I can write Python.",
                 page.get_by_label("How you can help"))
    page.get_by_label("How you can help").fill("I test with JAWS")
    page.get_by_role("button", name="Add me to the list").click()
    expect_h1(page, "You are on the helper list: Talking Timer")
    expect(page.get_by_role("status").filter(has_text="Thank you for helping")).to_be_visible()
    expect(page.get_by_role("heading", name="Members helping keep it working (1)")).to_be_visible()
    pages.check("library after volunteering")
    page.get_by_label("How you can help").fill("I test with JAWS and NVDA")
    page.get_by_role("button", name="Update how I can help").click()
    expect_h1(page, "How you can help is updated: Talking Timer")
    page.get_by_role("button", name="Take me off the list").click()
    expect_h1(page, "You are off the helper list: Talking Timer")
    pages.check("library after leaving the list")

    # A report from the library page comes back to the library page.
    page.get_by_role("radio", name="It partly worked").check()
    page.get_by_role("button", name="Save how it worked for me").click()
    expect_h1(page, "Your report is saved: Talking Timer")
    expect(page.get_by_role("link", name="Back to the Tool library")).to_be_visible()
    page.get_by_role("link", name="Back to the Tool library").click()
    page.get_by_role("link", name="Talking Timer").click()
    expect_h1(page, "Talking Timer in the Tool library")
    page.get_by_role("link", name="See Talking Timer in Find before you build").click()
    expect_h1(page, "Talking Timer: member reports")
    pages.check("library tool seen in find")
    pages.assert_clean()


# ----- Without JavaScript ---------------------------------------------------------------------

def test_key_pages_work_without_javascript(live, browser):
    base = live["base"]
    live["fake_ai"].on("find.search", RESEARCH).on("find.extract", EXTRACTED)
    setup = new_page(browser)
    sign_up(setup, base, "find-nojs@example.com", "No Script")
    page = new_page(browser, javascript=False)
    sign_in(page, base, "find-nojs@example.com")
    pages = NoScriptPages(page, browser)

    page.goto(base + "/find")
    page.get_by_label("What are you looking for?").fill("QuickMail")
    page.get_by_role("button", name="Search the directory").click()
    expect_h1(page, '1 tool matches "QuickMail"')
    page.get_by_role("link", name="QuickMail", exact=True).click()
    expect_h1(page, "QuickMail: member reports")
    page.get_by_role("radio", name="It did not work for me").check()
    page.get_by_role("button", name="Save how it worked for me").click()
    expect_h1(page, "Your report is saved: QuickMail")
    pages.check("find tool without JavaScript")

    page.get_by_role("link", name="See QuickMail in the Tool library").click()
    expect_h1(page, "QuickMail in the Tool library")
    page.get_by_label("What broke").fill("Messages are not read after the update.")
    page.get_by_label("After which update").fill("not sure")
    page.get_by_role("button", name="Send the problem to helpers").click()
    expect_h1(page, "Your problem report is saved: QuickMail")
    expect(page.get_by_role("heading", name="Open problems (1)")).to_be_visible()
    pages.check("library tool without JavaScript")

    page.goto(base + "/library")
    expect_h1(page, "Tool library")
    pages.check("library without JavaScript")

    page.goto(base + "/find/ask")
    page.get_by_label("What do you want to do or make?").fill("read restaurant menus aloud")
    page.get_by_role("button", name="Search the web").click()
    expect_h1(page, "What already exists for: read restaurant menus aloud")
    pages.check("search result without JavaScript")

    page.goto(base + "/library/add")
    page.get_by_role("button", name="Add the tool to the library").click()
    expect_h1(page, re.compile(r"^Please check \d+ answers: Add a tool a blind or low-vision person made$"))
    # Without JavaScript the summary stays an alert, named by its heading.
    expect(page.get_by_role("alert", name=ERROR_SUMMARY, exact=True)).to_be_visible()
    pages.check("library add errors without JavaScript")
    pages.assert_clean()


# ----- Keyboard -------------------------------------------------------------------------------

@pytest.mark.parametrize("path, field_label", [("/find", "What are you looking for?"),
                                               ("/find/ask", "What do you want to do or make?"),
                                               ("/library/add", "Tool's name")])
def test_tab_order_reaches_the_form_before_the_footer(live, browser, path, field_label):
    base = live["base"]
    page = new_page(browser)
    sign_up(page, base, f"find-keys{len(path)}@example.com", "Keyboard User")
    page.goto(base + path)
    page.keyboard.press("Tab")
    assert is_focused(page.get_by_role("link", name="Skip to main content"))
    field = page.get_by_label(field_label)
    footer = page.get_by_role("contentinfo")
    for _ in range(80):
        page.keyboard.press("Tab")
        if is_focused(field):
            break
        assert not footer.evaluate("el => el.contains(document.activeElement)"), \
            f"Tab reached the footer before {field_label!r} on {path}"
    else:
        pytest.fail(f"Tab never reached {field_label!r} on {path}")
