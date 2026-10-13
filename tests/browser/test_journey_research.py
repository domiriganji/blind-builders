"""Research, driven in a real browser the way a screen reader user moves through it.

Every control is found by its role and accessible name, or by its label, never by CSS selectors or
ids, so the test itself proves each control has a usable name. Every page reached is checked with
check_page (axe-core, one h1 matching the title, a main landmark, no sideways scrolling) at the
normal width and at 320 pixels.
"""
import re

import pytest

from studio.ai import AIError, ResearchResult, Source

from .conftest import check_page, check_page_narrow, new_page, sign_in, sign_up

pytestmark = pytest.mark.browser

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
The mean score was [RESULT NEEDED].
"""
CSV_TEXT = "name,score,group\nAna,3,A\nBo,5,B\nCy,,A\nDee,4,A\n"
TITLE = "Exams and screen readers"


def _answers(fake):
    fake.on("research.prior_work", ResearchResult(text=PRIOR_TEXT, sources=list(SOURCES)))
    fake.on("research.plan", PLAN_TEXT)
    fake.on("research.data", DATA_TEXT)
    fake.on("research.draft", DRAFT_TEXT)


class Pages:
    """Collects the problems check_page finds on every page the journey reaches.

    With JavaScript off, Playwright cannot run axe-core in the page, so each page is checked for its
    h1 and main content there, and check_page runs on the same addresses afterwards with JavaScript on.
    """

    def __init__(self, page, javascript: bool = True):
        self.page = page
        self.javascript = javascript
        self.problems: list[str] = []
        self.seen: list[str] = []

    def check(self, expected_h1: str | None = None) -> None:
        page = self.page
        page.wait_for_load_state()
        heading = page.get_by_role("heading", level=1)
        assert heading.count() == 1, page.url
        if expected_h1 is not None:
            assert " ".join(heading.inner_text().split()) == expected_h1, page.url
        assert len(page.get_by_role("main").inner_text().strip()) > 40, page.url
        self.seen.append(page.url)
        if self.javascript:
            self.problems += check_page(page) + check_page_narrow(page)

    def check_again_with_javascript(self, browser, base: str, email: str) -> None:
        page = new_page(browser)
        sign_in(page, base, email)
        for url in dict.fromkeys(self.seen):
            page.goto(url)
            self.problems += check_page(page, url + " (seen without JavaScript)")
            self.problems += check_page_narrow(page, url + " (seen without JavaScript)")
        page.context.close()

    def assert_clean(self) -> None:
        assert not self.problems, "\n".join(self.problems)


def _is_focused(locator) -> bool:
    return locator.evaluate("el => el === document.activeElement")


def _start_project(page, pages: Pages, title: str = TITLE) -> None:
    page.get_by_label("Project name").fill(title)
    page.get_by_label("Research question").fill("Do blind students finish exams faster with one question per page?")
    page.get_by_label("Research field").fill("Education")
    page.get_by_label("What do you already know or suspect?").fill("Timed exams are hard.")
    page.get_by_role("button", name="Start the project").click()
    pages.check(f"Project saved: {title}")


def test_research_journey_end_to_end(live, browser):
    base, fake = live["base"], live["fake_ai"]
    _answers(fake)
    page = new_page(browser)
    pages = Pages(page)
    sign_up(page, base, "research-journey@example.com", "Research Journey")

    # The section is reached from the main navigation.
    page.get_by_role("navigation", name="Sections").get_by_role("link", name="Research", exact=True).click()
    pages.check("Run your own research")
    assert page.get_by_text("You have no research projects yet").is_visible()

    # A form sent with mistakes shows the error summary; each link in it moves focus to its field.
    page.get_by_label("Project name").fill("   ")
    page.get_by_role("button", name="Start the project").click()
    pages.check("Run your own research: please fix the problems")
    summary = page.get_by_role("alert")
    assert "Please check 2 answers" in summary.inner_text()
    summary.get_by_role("link", name="Enter a short name for your project.").click()
    assert _is_focused(page.get_by_label("Project name"))
    summary.get_by_role("link", name="Enter your research question.").click()
    assert _is_focused(page.get_by_label("Research question"))
    assert page.get_by_label("Project name").get_attribute("aria-invalid") == "true"

    # Start the project.
    _start_project(page, pages)
    assert page.get_by_text("0 of 4 stages done. Next: stage 1, prior work.").is_visible()
    for n, name in enumerate(["Prior work", "Study plan", "Notes and data", "Draft paper"], start=1):
        assert page.get_by_role("heading", name=f"{n}. {name}", level=2).is_visible()

    # Stage 1: prior work. The AI's text is labelled as the AI's; the sources are real links.
    page.get_by_role("button", name="Find prior work").click()
    pages.check(f"Prior work is ready: {TITLE}")
    assert page.get_by_text("Written by the AI on").is_visible()
    assert page.get_by_text("The site did not check that each study says what the AI says").is_visible()
    sources = page.get_by_role("region", name="Sources returned by the search (3)")
    assert sources.get_by_role("link", name="Exam access study").get_attribute("href") == "https://example.org/a"
    assert page.get_by_role("heading", name="Summary", level=3).is_visible()

    # The download works and says who wrote it.
    with page.expect_download() as info:
        page.get_by_role("link", name="Download the prior work as Markdown").click()
    text = open(info.value.path(), encoding="utf-8").read()
    assert text.startswith(f"# Prior work: {TITLE}") and "Written by the AI" in text

    page.get_by_role("link", name=f"Back to {TITLE}").click()
    pages.check(TITLE)
    assert page.get_by_text("1 of 4 stages done").is_visible()

    # Stage 2: study plan. A failed AI call first: the job page says why, in plain words.
    fake.on("research.plan", AIError("The AI is busy right now (rate limit). Please try again in a minute."))
    page.get_by_role("button", name="Write a study plan").click()
    pages.check(f"This did not finish: Writing a study plan for {TITLE}")
    assert page.get_by_text("rate limit").is_visible()
    page.get_by_role("link", name="Go back and try again").click()
    pages.check(TITLE)
    fake.on("research.plan", PLAN_TEXT)
    page.get_by_role("button", name="Write a study plan").click()
    pages.check(f"Study plan is ready: {TITLE}")
    assert page.get_by_text("Checked by the site: each citation number").is_visible()
    page.get_by_role("link", name=f"Back to {TITLE}").click()
    pages.check(TITLE)

    # Stage 3: notes and data.
    page.get_by_role("link", name="Add notes and data").click()
    pages.check(f"Notes and data: {TITLE}")
    page.get_by_label("Notes").fill("Pilot ran with 4 people.")
    page.get_by_role("button", name="Save notes").click()
    pages.check(f"Your notes are saved: {TITLE}")
    assert page.get_by_label("Notes").input_value() == "Pilot ran with 4 people."

    # Uploading with no file chosen, then a file that is not CSV: the error summary leads to the field.
    page.get_by_role("button", name="Upload and compute the facts").click()
    pages.check(f"Notes and data: {TITLE}: please fix the problems")
    page.get_by_role("alert").get_by_role("link", name="Choose a CSV file to upload.").click()
    assert _is_focused(page.get_by_label("CSV file"))
    page.get_by_label("CSV file").set_input_files(
        files=[{"name": "photo.png", "mimeType": "image/png", "buffer": b"\x89PNG\r\n\x1a\n"}])
    page.get_by_role("button", name="Upload and compute the facts").click()
    pages.check(f"Notes and data: {TITLE}: please fix the problems")
    page.get_by_role("alert").get_by_role("link", name=re.compile("^This is not a CSV file")).click()
    assert _is_focused(page.get_by_label("CSV file"))

    page.get_by_label("CSV file").set_input_files(
        files=[{"name": "scores.csv", "mimeType": "text/csv", "buffer": CSV_TEXT.encode()}])
    page.get_by_role("button", name="Upload and compute the facts").click()
    pages.check(f"The site computed facts from scores.csv: 4 rows and 3 columns: {TITLE}")
    assert page.get_by_role("heading", name="Computed by the site", level=2).is_visible()
    assert page.get_by_role("heading", name="Columns with numbers (1)", level=3).is_visible()
    assert page.get_by_role("heading", name="Columns with text (2)", level=3).is_visible()
    assert page.get_by_text("Mean 4, smallest 3, largest 5.").is_visible()
    page.get_by_role("button", name="Ask the AI to describe patterns").click()
    pages.check(f"The AI's interpretation is ready: {TITLE}")
    assert page.get_by_text("It is an interpretation, not a result").is_visible()
    assert page.get_by_text("Scores rise with practice").is_visible()
    page.get_by_role("link", name=f"Back to {TITLE}").click()
    pages.check(TITLE)

    # Stage 4: draft paper, with the citation check done by the site.
    page.get_by_role("button", name="Write a draft paper").click()
    pages.check(f"Draft paper is ready: {TITLE}")
    assert page.get_by_role("heading", name="What the site checked", level=2).is_visible()
    unmatched = page.get_by_role("heading", name="Citations the site could not match", level=2)
    assert unmatched.is_visible()
    assert page.get_by_text("Places marked [RESULT NEEDED]: 1.").is_visible()
    assert page.get_by_role("link", name="Start a paper in Write with this title").is_visible()
    with page.expect_download() as info:
        page.get_by_role("link", name="Download the draft as Markdown").click()
    text = open(info.value.path(), encoding="utf-8").read()
    assert "Citations the site could not match: [7]" in text and "Not checked:" in text

    # Running a stage again keeps the earlier version, which can be read.
    fake.on("research.draft", "# Second\n## Abstract\nOnly [1].\n")
    page.get_by_role("button", name="Write the draft again").click()
    pages.check(f"Draft paper is ready: {TITLE}")
    versions = page.get_by_role("heading", name="Earlier versions", level=2)
    assert versions.is_visible()
    page.get_by_role("link", name=re.compile(r"^Version from \d{4}-\d{2}-\d{2} ")).click()
    pages.check()
    assert page.get_by_role("heading", level=1).inner_text().startswith(f"Draft paper: {TITLE}, version from ")
    assert page.get_by_text("This is an earlier version.").is_visible()
    page.get_by_role("link", name="Read the latest draft").click()
    pages.check(f"Draft paper: {TITLE}")
    page.get_by_role("link", name=f"Back to {TITLE}").click()
    pages.check(TITLE)
    assert page.get_by_text("4 of 4 stages done").is_visible()

    # Delete, with a confirmation page that has a way out.
    page.get_by_role("link", name="Delete this project").click()
    pages.check(f"Delete {TITLE}?")
    page.get_by_role("link", name="No, keep this project").click()
    pages.check(TITLE)
    page.get_by_role("link", name="Delete this project").click()
    pages.check(f"Delete {TITLE}?")
    page.get_by_role("button", name="Yes, delete this project").click()
    pages.check(f"Deleted the research project {TITLE}")
    assert page.get_by_text("You have no research projects yet").is_visible()
    _answers(fake)

    assert len(pages.seen) >= 25
    pages.assert_clean()
    page.context.close()


def test_research_works_without_javascript(live, browser):
    base, fake = live["base"], live["fake_ai"]
    _answers(fake)
    email = "research-nojs@example.com"
    setup = new_page(browser)
    sign_up(setup, base, email, "No Script")
    setup.context.close()

    page = new_page(browser, javascript=False)
    pages = Pages(page, javascript=False)
    sign_in(page, base, email)
    page.goto(base + "/research")
    pages.check("Run your own research")
    _start_project(page, pages, "No script study")
    page.get_by_role("button", name="Find prior work").click()
    pages.check("Prior work is ready: No script study")
    page.get_by_role("link", name="Back to No script study").click()
    page.get_by_role("button", name="Write a study plan").click()
    pages.check("Study plan is ready: No script study")
    page.get_by_role("link", name="Back to No script study").click()
    page.get_by_role("link", name="Add notes and data").click()
    pages.check("Notes and data: No script study")
    page.get_by_label("CSV file").set_input_files(
        files=[{"name": "s.csv", "mimeType": "text/csv", "buffer": CSV_TEXT.encode()}])
    page.get_by_role("button", name="Upload and compute the facts").click()
    pages.check("The site computed facts from s.csv: 4 rows and 3 columns: No script study")
    page.get_by_role("button", name="Ask the AI to describe patterns").click()
    pages.check("The AI's interpretation is ready: No script study")
    page.get_by_role("link", name="Back to No script study").click()
    page.get_by_role("button", name="Write a draft paper").click()
    pages.check("Draft paper is ready: No script study")
    assert page.get_by_role("link", name="Download the draft as Markdown").is_visible()
    page.get_by_role("link", name="Back to No script study").click()
    pages.check("No script study")
    page.get_by_role("link", name="Delete this project").click()
    pages.check("Delete No script study?")
    page.get_by_role("link", name="No, keep this project").click()
    pages.check("No script study")
    # Every address reached so far, checked again with JavaScript on (axe-core needs it).
    pages.check_again_with_javascript(browser, base, email)
    page.get_by_role("link", name="Delete this project").click()
    page.get_by_role("button", name="Yes, delete this project").click()
    pages.check("Deleted the research project No script study")
    assert page.get_by_text("You have no research projects yet").is_visible()
    pages.assert_clean()
    page.context.close()


def test_long_unbroken_text_does_not_scroll_sideways(live, browser):
    base, fake = live["base"], live["fake_ai"]
    long_word = "x" * 190
    fake.on("research.prior_work", ResearchResult(text="## Summary\n" + "y" * 400, sources=[
        Source(url="https://example.org/" + "z" * 300, title="")]))
    page = new_page(browser)
    pages = Pages(page)
    sign_up(page, base, "research-long@example.com", "Long Words")
    page.goto(base + "/research")
    pages.check()
    _start_project(page, pages, long_word)
    page.get_by_role("button", name="Find prior work").click()
    pages.check(f"Prior work is ready: {long_word}")
    page.get_by_role("link", name=f"Back to {long_word}").click()
    page.get_by_role("link", name="Add notes and data").click()
    page.get_by_label("CSV file").set_input_files(files=[{
        "name": "wide.csv", "mimeType": "text/csv",
        "buffer": ("q" * 300 + ",n\n" + "w" * 300 + ",1\n").encode()}])
    page.get_by_role("button", name="Upload and compute the facts").click()
    pages.check()
    pages.assert_clean()
    _answers(fake)
    page.context.close()


def test_keyboard_order_on_the_start_form(live, browser):
    page = new_page(browser)
    sign_up(page, live["base"], "research-keys@example.com", "Keyboard")
    page.goto(live["base"] + "/research")
    page.keyboard.press("Tab")
    assert _is_focused(page.get_by_role("link", name="Skip to main content"))
    first_field = page.get_by_label("Project name")
    footer = page.get_by_role("navigation", name="About this site")
    for _ in range(80):
        page.keyboard.press("Tab")
        if _is_focused(first_field):
            break
        assert not footer.evaluate("nav => nav.contains(document.activeElement)"), \
            "a footer link was reached before the first field of the form"
    else:
        pytest.fail("Tab never reached the Project name field")
    # The fields come in the order they are read: name, question, field, what you know, then the button.
    for label in ["Research question", "Research field", "What do you already know or suspect?"]:
        page.keyboard.press("Tab")
        assert _is_focused(page.get_by_label(label)), label
    page.keyboard.press("Tab")
    assert _is_focused(page.get_by_role("button", name="Start the project"))
    page.context.close()
