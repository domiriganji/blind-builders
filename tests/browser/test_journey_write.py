"""Write and publish, driven in a real Chromium the way a screen reader user moves through it.

Every control is found only by its role and accessible name, or by its label, so the test also proves
that every control has a usable name. Every page reached is checked with check_page and
check_page_narrow (axe-core, one h1 matching the title, a main landmark, no sideways scrolling).
"""
import re

import pytest

from studio.ai import AIError, ResearchResult, Source

from .conftest import check_page, check_page_narrow, new_page, sign_in, sign_up

expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = pytest.mark.browser

BODY = "# Introduction\n\nThe cat sat on teh mat. It was happy.\n\nBlind writers deserve good tools."
SUGGESTIONS = {
    "overall_note": "Mostly clear. One spelling mistake.",
    "suggestions": [
        {"original": "teh", "proposed": "the", "reason": "Spelling."},
        {"original": "It was happy.", "proposed": "It was glad.", "reason": "Variety."},
        {"original": "Words that are not in the text.", "proposed": "x", "reason": "Invented."},
    ],
}


def assert_page_ok(page, label=""):
    """Collect every problem on this page, at full width and at 320 pixels, and fail if there are any."""
    problems = check_page(page, label) + check_page_narrow(page, label)
    assert not problems, "\n".join(problems)


def h1(page) -> str:
    return " ".join(page.get_by_role("heading", level=1).inner_text().split())


def is_focused(locator) -> bool:
    return locator.evaluate("el => el === document.activeElement")


def follow_error_link(page, message: str, field):
    """The error summary is shown; following its link moves focus to the field that needs fixing."""
    summary = page.locator(".error-summary").filter(has=page.get_by_role("heading", name="What to check", exact=True))
    assert summary.count() == 1
    summary.get_by_role("link", name=message).click()
    expect(field, f"following {message!r} did not move focus to the field").to_be_focused(timeout=3000)


def start_document(page, base, title, kind="Article", target="", check=assert_page_ok):
    page.goto(base + "/")
    page.get_by_role("navigation", name="Sections").get_by_role("link", name="Write and publish").click()
    assert h1(page) == "Write and publish"
    check(page, "/write signed in")
    page.get_by_label("Title", exact=True).fill(title)
    page.get_by_label(kind, exact=True).check()
    if target:
        page.get_by_label("Publication you are aiming for").fill(target)
    page.get_by_role("button", name="Start the document").click()
    assert h1(page) == f"{title}: new document started"
    check(page, "new document")


def save_text(page, text):
    page.get_by_role("textbox", name="Your text").fill(text)
    page.get_by_role("button", name="Save my text").click()


def test_create_validation_moves_focus_to_the_field(live, browser):
    page = new_page(browser)
    base = live["base"]
    sign_up(page, base, "write-errors@example.com", "Write Errors")
    calls_before = len(live["fake_ai"].calls)
    page.goto(base + "/write")
    page.get_by_role("button", name="Start the document").click()
    assert h1(page) == "Please check 1 answer: Start a new document"
    assert_page_ok(page, "create errors")
    follow_error_link(page, "Enter a title for your document. You can change it later.",
                      page.get_by_label("Title", exact=True))
    # A title that is too long is explained with the limit
    page.get_by_label("Title", exact=True).fill("x" * 201)
    page.get_by_role("button", name="Start the document").click()
    follow_error_link(page, "Use a title of 200 characters or fewer.", page.get_by_label("Title", exact=True))
    page.get_by_label("Title", exact=True).fill("Été: a story")
    page.get_by_label("Story", exact=True).check()
    page.get_by_role("button", name="Start the document").click()
    assert h1(page) == "Été: a story: new document started"

    # Saving with mistakes: an empty title and a card number in the text
    page.get_by_label("Title", exact=True).fill("")
    save_text(page, "My card is 4111 1111 1111 1111.")
    assert h1(page) == "Please check 2 answers: Été: a story"
    assert_page_ok(page, "save errors")
    assert page.get_by_role("textbox", name="Your text").input_value() == "My card is 4111 1111 1111 1111."  # nothing lost
    follow_error_link(page, "This looks like it includes a password, PIN, code, card number or API key, so it was "
                            "not saved or sent to the AI. Please take it out: this site never needs them. If it is "
                            "not one, change the wording a little, for example \"the PIN box is not announced\", "
                            "and send it again.",
                      page.get_by_role("textbox", name="Your text"))
    follow_error_link(page, "Enter a title for your document.", page.get_by_label("Title", exact=True))

    # Asking for suggestions before any text is saved points to the text box
    page.goto(base + "/write")
    page.get_by_role("link", name="Été: a story").click()
    page.get_by_role("button", name="Save and get suggestions").click()
    assert h1(page) == "Please check 1 answer: Été: a story"
    assert_page_ok(page, "suggest errors")
    follow_error_link(page, 'Your document has no text yet. Write or paste it in "Your text", '
                            'then choose "Save and get suggestions".', page.get_by_role("textbox", name="Your text"))

    # Preparing for a publication that is not named points to the publication field
    save_text(page, BODY)
    assert h1(page) == "Été: a story: saved as version 1"
    page.get_by_label("What should the AI look for?").select_option(label="Prepare it for the publication I named")
    page.get_by_role("button", name="Save and get suggestions").click()
    follow_error_link(page, "Fill in the publication you are aiming for, so the AI knows what to prepare "
                            "your text for. Or choose another goal.",
                      page.get_by_label("Publication you are aiming for"))
    # The cover letter section explains what to do and links to the field
    page.get_by_role("link", name="Go to the publication field").click()
    expect(page.get_by_label("Publication you are aiming for")).to_be_focused(timeout=3000)
    assert len(live["fake_ai"].calls) == calls_before  # no mistake reached the AI


def test_write_suggest_accept_reject_and_versions(live, browser):
    fake = live["fake_ai"]
    page = new_page(browser)
    base = live["base"]
    sign_up(page, base, "write-journey@example.com", "Write Journey")
    start_document(page, base, "My essay", target="Journal of Examples")

    save_text(page, BODY)
    assert h1(page) == "My essay: saved as version 1"
    assert page.get_by_role("status").filter(has_text="Saved as version 1").count() == 1
    assert_page_ok(page, "saved")
    # Saving again without changes says so
    page.get_by_role("button", name="Save my text").click()
    assert h1(page) == "My essay: saved, the text had not changed"

    # Ask for suggestions
    fake.on("write.suggest", SUGGESTIONS)
    page.get_by_label("What should the AI look for?").select_option(label="Fix spelling and grammar only")
    page.get_by_label("Anything else the AI should know").fill("Keep British spelling.")
    page.get_by_role("button", name="Save and get suggestions").click()
    assert h1(page) == "2 suggestions for My essay"
    assert_page_ok(page, "suggestions")
    main = page.get_by_role("main")
    assert "Checked by the site: 1 of the AI's 3 suggestions was left out" in main.inner_text()
    note = page.get_by_role("region", name="The AI's overall note")
    assert "Written by the AI" in note.inner_text() and "Mostly clear" in note.inner_text()
    first = page.get_by_role("region", name="Suggestion 1 of 2")
    assert "Your words: teh" in first.inner_text() and "Suggested: the" in first.inner_text()
    assert 'Change, computed by the site: replaces "teh" with "the".' in first.inner_text()
    assert "The AI's reason: Spelling." in first.inner_text()
    assert "Where it is in your text:" in first.inner_text()

    # The in-page link goes to the next suggestion to decide
    page.get_by_role("link", name="Go to suggestion 1, the next one to decide").click()

    # Accept one, reject the other; each page's h1 says what happened
    page.get_by_role("button", name="Accept suggestion 1").click()
    assert h1(page) == "2 suggestions for My essay: suggestion 1 accepted"
    # The page opens at the next suggestion to decide, which starts with what just happened
    assert page.url.endswith("#suggestion-2")
    assert "Suggestion 1 accepted." in page.get_by_role("region", name="Suggestion 2 of 2").inner_text()
    assert_page_ok(page, "accepted")
    assert page.get_by_role("button", name="Accept suggestion 1").count() == 0
    assert "Accepted. Your text has this change." in page.get_by_role("region", name="Suggestion 1 of 2").inner_text()
    page.get_by_role("button", name="Reject suggestion 2").click()
    assert h1(page) == "2 suggestions for My essay: suggestion 2 rejected"
    assert_page_ok(page, "rejected")
    assert page.get_by_role("link", name=re.compile(r"^Accept all \d+ waiting")).count() == 0

    page.get_by_role("link", name="Back to My essay").click()
    assert h1(page) == "My essay"
    assert page.get_by_role("textbox", name="Your text").input_value() == BODY.replace("teh", "the")

    # A suggestion goes out of date when the text changes before it is accepted
    page.get_by_role("button", name="Save and get suggestions").click()
    assert h1(page) == "1 suggestion for My essay"
    page.get_by_role("link", name="Back to My essay").click()
    save_text(page, "A completely new text.")
    assert h1(page) == "My essay: saved as version 3"
    page.get_by_role("link", name=re.compile(r"^Suggestions from .*Fix spelling and grammar only$")).first.click()
    assert h1(page) == "1 suggestion for My essay"
    assert "This no longer matches your text" in page.get_by_role("region", name="Suggestion 1 of 1").inner_text()
    assert_page_ok(page, "stale suggestion")
    assert page.get_by_role("button", name="Accept suggestion 1").count() == 0
    page.get_by_role("button", name="Dismiss suggestion 1 (no longer matches your text)").click()
    assert h1(page) == "1 suggestion for My essay: suggestion 1 dismissed"
    assert page.get_by_role("status").filter(has_text="Suggestion 1 dismissed. Your text is unchanged.").count() == 1
    assert_page_ok(page, "stale result")

    # Accept all and reject all
    page.get_by_role("link", name="Back to My essay").click()
    save_text(page, "Alpha one. Beta two.")
    fake.on("write.suggest", {"overall_note": "", "suggestions": [
        {"original": "Alpha one.", "proposed": "Alpha 1.", "reason": "Digits."},
        {"original": "Beta two.", "proposed": "Beta 2.", "reason": "Digits."}]})
    page.get_by_role("button", name="Save and get suggestions").click()
    assert h1(page) == "2 suggestions for My essay"
    assert "The AI did not write an overall note." in page.get_by_role("region", name="The AI's overall note").inner_text()
    # Accepting all is a link to a question first, after the last suggestion
    page.get_by_role("link", name="Accept all 2 waiting suggestions").click()
    assert h1(page) == "Accept all 2 waiting suggestions for My essay?"
    assert_page_ok(page, "accept all question")
    page.get_by_role("button", name="Accept every suggestion that still fits my text").click()
    assert h1(page) == "2 suggestions for My essay: 2 suggestions accepted"
    assert_page_ok(page, "accept all")
    page.get_by_role("link", name="Back to My essay").click()
    fake.on("write.suggest", {"overall_note": "Fine.", "suggestions": [
        {"original": "Alpha 1.", "proposed": "Alpha one.", "reason": "Words."}]})
    page.get_by_role("button", name="Save and get suggestions").click()
    page.get_by_role("button", name="Reject all remaining").click()
    assert h1(page) == "1 suggestion for My essay: 1 suggestion rejected"
    assert_page_ok(page, "reject all")

    # Versions: list, open one, restore it
    page.get_by_role("link", name="Back to My essay").click()
    page.get_by_role("link", name="See all versions and restore an earlier one").click()
    assert h1(page) == "Versions of My essay"
    assert page.get_by_role("heading", level=2, name=re.compile(r"^Saved on \d{1,2} [A-Z][a-z]+ \d{4} \(\d+ versions\)$")).count() >= 1
    assert_page_ok(page, "versions")
    page.get_by_role("link", name=re.compile(r"^Version 1: saved by you, \d\d:\d\d UTC$")).click()
    assert h1(page) == "Version 1 of My essay"
    assert_page_ok(page, "version 1")
    diff = page.get_by_role("region", name="What is different from your current text")
    assert "This version has:" in diff.inner_text()
    page.get_by_role("button", name="Restore version 1").first.click()
    assert h1(page) == "My essay: version 1 restored"
    assert_page_ok(page, "restored")
    assert page.get_by_role("textbox", name="Your text").input_value() == BODY


def test_export_venues_cover_letter_and_delete(live, browser):
    fake = live["fake_ai"]
    page = new_page(browser)
    base = live["base"]
    sign_up(page, base, "write-publish@example.com", "Write Publish")
    start_document(page, base, "Screen readers in class", kind="Paper for a journal or conference",
                   target="Journal of Examples")
    save_text(page, BODY + "\n\nWe paid $5 & saved 10%.")

    # Export
    page.get_by_role("link", name="Choose a format to export").click()
    assert h1(page) == "Export Screen readers in class"
    assert_page_ok(page, "export")
    with page.expect_download() as info:
        page.get_by_role("link", name="Download as Markdown (.md)").click()
    download = info.value
    assert download.suggested_filename == "screen-readers-in-class.md"
    with open(download.path(), encoding="utf-8") as fh:
        assert fh.read().startswith("# Screen readers in class\n\n# Introduction")
    for name, filename in [("Download as a Word document (.docx)", "screen-readers-in-class.docx"),
                           ("Download as plain text (.txt)", "screen-readers-in-class.txt"),
                           ("Download as a web page (.html)", "screen-readers-in-class.html"),
                           ("Download as LaTeX (.tex)", "screen-readers-in-class.tex")]:
        with page.expect_download() as info:
            page.get_by_role("link", name=name).click()
        assert info.value.suggested_filename == filename
    page.get_by_role("link", name="Back to Screen readers in class").click()

    # Find where to publish
    fake.on("write.venues", ResearchResult(
        text="Summary: one good fit.\n\n# Journal of Examples\n\nNo fee found. Nothing found about screen reader access.",
        sources=[Source(url="https://example.org/guidelines", title="Author guidelines", quote="Submit online.")]))
    page.get_by_role("button", name="Save and search the web for places to publish").click()
    assert h1(page) == "Where to publish: Screen readers in class"
    assert_page_ok(page, "venues")
    main = page.get_by_role("main").inner_text()
    answer = page.get_by_role("region", name="Written by the AI (not checked)")
    assert "Summary: one good fit." in answer.inner_text() and "check each one" in main
    assert answer.get_by_role("heading", level=3, name="Journal of Examples").count() == 1
    assert page.get_by_role("link", name="Author guidelines").get_attribute("rel") == "noopener noreferrer"
    page.get_by_role("link", name="Back to Screen readers in class").click()
    page.get_by_role("link", name=re.compile(r"^Places to publish, found ")).click()
    assert h1(page) == "Where to publish: Screen readers in class"
    page.get_by_role("link", name="Back to Screen readers in class").click()

    # Cover letter: a long note is refused with the limit, then a draft becomes a new document
    page.get_by_label("Anything the letter should mention").fill("n" * 2001)
    page.get_by_role("button", name="Save and draft a cover letter for Journal of Examples").click()
    assert h1(page) == "Please check 1 answer: Screen readers in class"
    assert_page_ok(page, "cover letter errors")
    follow_error_link(page, "This is 2,001 characters long. Shorten it to 2,000 characters or fewer.",
                      page.get_by_label("Anything the letter should mention"))
    fake.on("write.cover_letter", "Dear [Editor's name],\n\nI am submitting my paper.\n\n[Your name]")
    page.get_by_label("Anything the letter should mention").fill("I teach braille.")
    page.get_by_role("button", name="Save and draft a cover letter for Journal of Examples").click()
    assert h1(page) == "Cover letter: Screen readers in class"
    assert "The AI wrote this draft. Nobody has checked it." in page.get_by_role("main").inner_text()
    assert_page_ok(page, "cover letter")

    # Delete the cover letter: confirm first, then the list says it is gone
    page.get_by_role("link", name="Delete Cover letter: Screen readers in class").click()
    assert h1(page) == "Delete Cover letter: Screen readers in class?"
    assert_page_ok(page, "delete confirm")
    page.get_by_role("button", name="Delete this document forever").click()
    assert h1(page) == "Write and publish: Cover letter: Screen readers in class deleted"
    assert_page_ok(page, "deleted")
    assert page.get_by_role("link", name="Cover letter: Screen readers in class").count() == 0
    assert page.get_by_role("link", name="Screen readers in class", exact=True).count() == 1


def test_unsaved_edits_go_with_the_ai_buttons(live, browser):
    """Typing in the text box and choosing an AI button straight away never throws the words away."""
    fake = live["fake_ai"]
    page = new_page(browser)
    base = live["base"]
    sign_up(page, base, "write-unsaved@example.com", "Write Unsaved")
    start_document(page, base, "Unsaved essay")
    # The title comes first in the edit form, then the text, then the publication
    labels = page.locator("#edit-title ~ .field label").all_inner_texts()
    assert [" ".join(t.split()) for t in labels][:3] == ["Title", "Your text", "Publication you are aiming for (optional)"]
    page.get_by_role("textbox", name="Your text").fill(BODY)
    fake.on("write.suggest", SUGGESTIONS)
    page.get_by_role("button", name="Save and get suggestions").click()
    assert h1(page) == "2 suggestions for Unsaved essay"
    assert page.get_by_role("status").filter(has_text="Saved your text as version 1, then asked the AI.").count() == 1
    assert "teh mat" in fake.calls[-1][2]["prompt"]
    page.get_by_role("link", name="Back to Unsaved essay").click()
    # A question about another field keeps every word typed, and nothing reaches the AI
    long_text = BODY + "\n\n" + "More words for the ending. " * 400
    calls = len(fake.calls)
    page.get_by_role("textbox", name="Your text").fill(long_text)
    page.get_by_label("Title", exact=True).fill("")
    page.get_by_role("button", name="Save and search the web for places to publish").click()
    assert h1(page) == "Please check 1 answer: Unsaved essay"
    assert page.get_by_role("textbox", name="Your text").input_value() == long_text
    assert len(fake.calls) == calls
    assert_page_ok(page, "unsaved edits kept")
    # Reloading the address the form went to leads back to the document, not to a dead end
    page.goto(page.url)
    assert h1(page) == "Unsaved essay"


def test_ai_failure_shows_a_plain_message(live, browser):
    fake = live["fake_ai"]
    page = new_page(browser)
    base = live["base"]
    sign_up(page, base, "write-fail@example.com", "Write Fail")
    start_document(page, base, "Failing essay")
    save_text(page, BODY)
    fake.on("write.suggest", AIError("The AI is busy right now (rate limit). Please try again in a minute."))
    page.get_by_role("button", name="Save and get suggestions").click()
    assert h1(page) == "This did not finish: Suggestions for Failing essay"
    assert "rate limit" in page.get_by_role("main").inner_text()
    assert_page_ok(page, "job failed")
    # An answer with missing fields is not shown as "no suggestions"
    fake.on("write.suggest", {"overall_note": "Looks fine."})
    page.goto(base + "/write")
    page.get_by_role("link", name="Failing essay").click()
    page.get_by_role("button", name="Save and get suggestions").click()
    assert h1(page) == "This did not finish: Suggestions for Failing essay"
    assert "incomplete" in page.get_by_role("main").inner_text()


def test_write_works_without_javascript(live, browser):
    """Every step works with JavaScript off. axe-core needs JavaScript, so each page reached here is checked
    with check_page afterwards in a JavaScript-enabled browser signed in as the same person."""
    fake = live["fake_ai"]
    base = live["base"]
    email = "write-nojs@example.com"
    setup = new_page(browser)
    sign_up(setup, base, email, "Write No JS")
    setup.context.close()

    visited: list[str] = []

    def shows_content(page, label=""):
        assert page.get_by_role("heading", level=1).count() == 1, label
        assert len(page.get_by_role("main").inner_text().strip()) > 20, label
        if not h1(page).startswith("Please check"):
            visited.append(page.url.split("#", 1)[0])  # the page, wherever it opened

    page = new_page(browser, javascript=False)
    sign_in(page, base, email)
    start_document(page, base, "No script essay", check=shows_content)
    save_text(page, BODY)
    assert h1(page) == "No script essay: saved as version 1"
    shows_content(page)
    fake.on("write.suggest", SUGGESTIONS)
    page.get_by_role("button", name="Save and get suggestions").click()
    assert h1(page) == "2 suggestions for No script essay"
    shows_content(page)
    page.get_by_role("button", name="Accept suggestion 1").click()
    assert h1(page) == "2 suggestions for No script essay: suggestion 1 accepted"
    shows_content(page)
    page.get_by_role("link", name="Back to No script essay").click()
    assert page.get_by_role("textbox", name="Your text").input_value() == BODY.replace("teh", "the")
    page.get_by_role("link", name="See all versions and restore an earlier one").click()
    assert h1(page) == "Versions of No script essay"
    shows_content(page)
    page.get_by_role("link", name=re.compile(r"^Version 1: saved by you, \d\d:\d\d UTC$")).click()
    page.get_by_role("button", name="Restore version 1").last.click()
    assert h1(page) == "No script essay: version 1 restored"
    shows_content(page)
    page.get_by_role("link", name="Choose a format to export").click()
    assert h1(page) == "Export No script essay"
    shows_content(page)
    with page.expect_download() as info:
        page.get_by_role("link", name="Download as plain text (.txt)").click()
    assert info.value.suggested_filename == "no-script-essay.txt"
    page.get_by_role("link", name="Back to No script essay").click()
    fake.on("write.venues", ResearchResult(text="# A magazine\n\nDetails.", sources=[]))
    page.get_by_role("button", name="Save and search the web for places to publish").click()
    assert h1(page) == "Where to publish: No script essay"
    assert "did not list any sources" in page.get_by_role("main").inner_text()
    shows_content(page)
    page.get_by_role("link", name="Back to No script essay").click()
    # Errors still work without JavaScript, and the error link still moves focus to the field
    page.get_by_label("Title", exact=True).fill("")
    page.get_by_role("button", name="Save my text").click()
    assert h1(page) == "Please check 1 answer: No script essay"
    shows_content(page)
    follow_error_link(page, "Enter a title for your document.", page.get_by_label("Title", exact=True))
    page.goto(base + "/write")
    page.get_by_role("link", name="No script essay").click()
    page.get_by_role("link", name="Delete No script essay").click()
    shows_content(page)

    checker = new_page(browser)
    sign_in(checker, base, email)
    problems = []
    for url in dict.fromkeys(visited):
        assert checker.goto(url).status == 200, url
        problems += check_page(checker, url) + check_page_narrow(checker, url)
    assert not problems, "\n".join(problems)
    assert len(visited) >= 9
    checker.context.close()

    page.get_by_role("button", name="Delete this document forever").click()
    assert h1(page) == "Write and publish: No script essay deleted"
    shows_content(page)


def _tab_until(page, target, limit=80) -> bool:
    """Press Tab until target has focus. False if the footer links come first."""
    footer = page.get_by_role("navigation", name="About this site")
    for _ in range(limit):
        page.keyboard.press("Tab")
        if is_focused(target):
            return True
        if footer.evaluate("nav => nav.contains(document.activeElement)"):
            return False
    return False


def test_keyboard_order_reaches_the_form_before_the_footer(live, browser):
    page = new_page(browser)
    base = live["base"]
    sign_up(page, base, "write-keys@example.com", "Write Keys")
    start_document(page, base, "Keyboard essay")
    for path_name, field in [("new", "Title"), ("doc", "Your text")]:
        if path_name == "new":
            page.goto(base + "/write/new")
            assert h1(page) == "Start a new document"
            assert_page_ok(page, "new form")
        else:
            page.goto(base + "/write")
            page.get_by_role("link", name="Keyboard essay").click()
        page.keyboard.press("Tab")
        skip = page.get_by_role("link", name="Skip to main content")
        assert is_focused(skip), f"{path_name}: Tab did not reach the skip link first"
        target = page.get_by_role("textbox", name=field, exact=(field == "Title"))
        assert _tab_until(page, target), f"{path_name}: the {field!r} field is not reachable before the footer"
        # The skip link takes keyboard users straight to the content, ahead of the field
        page.reload()
        page.keyboard.press("Tab")
        page.keyboard.press("Enter")
        assert _tab_until(page, target, limit=20)
