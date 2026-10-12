"""Build, driven in a real Chromium the way a screen reader user does.

Every control is found by its role and accessible name, or by its label, never by CSS selectors, ids or
XPath, so the test itself proves that each control has a usable name. Every page reached is checked with
check_page and check_page_narrow (axe-core, one h1 matching the title, a main landmark, no sideways
scrolling at 1280 and 320 pixels).
"""
import io
import zipfile

import pytest
from playwright.sync_api import expect

from .conftest import check_page, check_page_narrow, new_page, sign_in, sign_up

pytestmark = pytest.mark.browser

SPEC = {
    "summary": "A menu website for Kemi's food stall. Customers see today's dishes and prices.",
    "audience": "People who buy lunch at the stall.",
    "pages": [{"name": "Menu", "purpose": "Shows the dishes and prices.", "contents": ["Small chops", "Jollof rice"]}],
    "features": ["A list of dishes with prices"],
    "data_kept": "Nothing is stored.",
    "questions": ["Should prices be shown in naira?"],
    "additions_not_asked": ["A dark colour switch", "A printable menu page"],
    "not_doing": ["Online ordering or payments"],
    "suggested_title": "Kemi's menu",
}
SPEC_2 = dict(SPEC, summary="A menu website in naira for Kemi's food stall.", questions=[])

INDEX_V1 = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kemi Kitchen menu</title><link rel="stylesheet" href="style.css"></head>
<body><main>
<h1>Kemi Kitchen menu</h1>
<p>Small chops: 1200 naira</p>
<p>Jollof rice: 1500 naira</p>
<p><a href="about.html">About Kemi</a></p>
</main></body>
</html>
"""
ABOUT_NO_LANG = """<!doctype html>
<html><head><title>About Kemi</title></head>
<body><main><h1>About Kemi</h1><p>We cook every day.</p></main></body></html>
"""
CSS = "body { font-size: 1.2rem; }\n"


def files(index=INDEX_V1, about=ABOUT_NO_LANG):
    return [{"path": "index.html", "content": index}, {"path": "about.html", "content": about},
            {"path": "style.css", "content": CSS}]


GENERATED = {"files": files(), "summary": "I built a **menu** page and an about page.",
             "how_to_use": "Open index.html.", "not_done": ["I guessed the opening hours."]}
CHANGED = {"files": files(index=INDEX_V1.replace("Small chops: 1200 naira", "Small chops: 1500 naira")),
           "summary": "I changed the price of small chops.", "how_to_use": "Open index.html.", "not_done": []}
FIXED = {"files": files(index=CHANGED["files"][0]["content"], about=ABOUT_NO_LANG.replace("<html>", '<html lang="en">')),
         "summary": "I added the page language to about.html.", "how_to_use": "Open index.html.", "not_done": []}


def structure_problems(page, width: int | None = None) -> list[str]:
    """check_page without axe-core, for pages opened with JavaScript off.

    With JavaScript off, Chromium never runs timers, so axe-core (which waits on them) cannot run.
    The same pages are checked with axe-core by the journey that has JavaScript on.
    """
    original = page.viewport_size
    if width:
        page.set_viewport_size({"width": width, "height": 800})
    where = f"{page.url} (no JavaScript{f', {width}px' if width else ''})"
    problems = []
    try:
        h1s = page.get_by_role("heading", level=1)
        if h1s.count() != 1:
            problems.append(f"{where}: {h1s.count()} h1 elements")
        else:
            h1 = " ".join(h1s.inner_text().split())
            title = " ".join(page.title().split(" – ")[0].split())
            if h1 != title:
                problems.append(f"{where}: title {title!r} does not match h1 {h1!r}")
        main = page.get_by_role("main")
        if main.count() != 1 or len(main.inner_text().strip()) < 20:
            problems.append(f"{where}: no single main landmark with content")
        overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        if overflow > 2:
            problems.append(f"{where}: scrolls sideways by {overflow}px")
    finally:
        page.set_viewport_size(original)
    return problems


class Checker:
    """Checks every page the journey reaches and keeps the problems for one assert at the end."""

    def __init__(self, page, *, javascript: bool = True):
        self.page = page
        self.javascript = javascript
        self.problems: list[str] = []
        self.pages = 0

    def __call__(self, h1: str | None = None) -> None:
        self.page.wait_for_load_state()
        if h1 is not None:
            expect(self.page.get_by_role("heading", level=1)).to_have_text(h1)
        if self.javascript:
            self.problems += check_page(self.page) + check_page_narrow(self.page)
        else:
            self.problems += structure_problems(self.page) + structure_problems(self.page, width=320)
        self.pages += 1

    def assert_clean(self) -> None:
        assert not self.problems, "\n".join(self.problems)


def link(page, name):
    return page.get_by_role("link", name=name, exact=True)


def button(page, name):
    return page.get_by_role("button", name=name, exact=True)


def signed_in(page):
    """Wait until the page after signing in has loaded (it has the Sign out button)."""
    expect(page.get_by_role("button", name="Sign out")).to_be_visible()


def open_build(page):
    page.get_by_role("navigation", name="Sections").get_by_role("link", name="Build", exact=True).click()


def send_idea(page, title="Kemi Kitchen", kind="A website", idea="A menu for my food stall.\nIt shows small chops."):
    page.get_by_label("Title").fill(title)
    page.get_by_label(kind, exact=True).check()
    page.get_by_label("Your idea").fill(idea)
    button(page, "Send my idea to the AI").click()


def test_build_journey_from_idea_to_delete(live, browser):
    fake, base = live["fake_ai"], live["base"]
    page = new_page(browser)
    check = Checker(page)
    sign_up(page, base, "build-journey@example.com", "Kemi")
    signed_in(page)

    # The section's index, then the form sent empty: the error summary links move focus to the fields.
    open_build(page)
    check("Build a website, app, tool or game")
    expect(page.get_by_text("You have no projects yet.")).to_be_visible()
    button(page, "Send my idea to the AI").click()
    check("Build a website, app, tool or game: please fix the problems")
    expect(page.get_by_role("heading", name="There are 2 problems")).to_be_visible()
    link(page, "Describe your idea in your own words.").click()
    expect(page.get_by_label("Your idea")).to_be_focused()

    # Send the idea. The AI reads it back; nothing is built yet.
    fake.on("build.understand", SPEC)
    send_idea(page)
    check("Kemi Kitchen: here is what I understood")
    expect(page.get_by_text("Written by the AI. Nothing is built yet.")).to_be_visible()
    expect(page.get_by_text("Should prices be shown in naira?")).to_be_visible()
    expect(page.get_by_label("A dark colour switch")).not_to_be_checked()
    expect(page.get_by_label("A printable menu page")).not_to_be_checked()

    # Update with nothing written: an error that says how to fix it, and its link focuses the box.
    button(page, "Update what you understood").click()
    check("Kemi Kitchen: here is what I understood: please fix the problems")
    link(page, "Write your corrections or answers first, or choose This is right: build it.").click()
    expect(page.get_by_label("Corrections and answers")).to_be_focused()

    # A correction: the AI reads it again, and the person's own words are shown back.
    fake.on("build.understand", SPEC_2)
    page.get_by_label("Corrections and answers").fill("Prices are in naira.")
    button(page, "Update what you understood").click()
    check("Kemi Kitchen: here is what I understood")
    expect(page.get_by_text("A menu website in naira for Kemi's food stall.")).to_be_visible()
    expect(page.get_by_role("listitem").filter(has_text="Prices are in naira.")).to_be_visible()

    # Approve with one extra ticked.
    fake.on("build.generate", GENERATED)
    page.get_by_label("A printable menu page").check()
    button(page, "This is right: build it").click()
    check("Kemi Kitchen: version 1 is ready")
    expect(page.get_by_role("heading", name="Checked by the site")).to_be_visible()
    expect(page.get_by_text("Written by the AI. The site has not checked these claims.")).to_be_visible()
    expect(page.get_by_text("Problem:")).to_be_visible()  # about.html has no page language
    assert "A printable menu page" in fake.calls[-1][2]["prompt"]
    assert "A dark colour switch" not in fake.calls[-1][2]["prompt"]

    # Read the files, one file, and come back.
    link(page, "Read the files").click()
    check("Kemi Kitchen: the files in version 1")
    link(page, "index.html").click()
    check("Kemi Kitchen: index.html in version 1")
    expect(page.get_by_text("Small chops: 1200 naira")).to_be_visible()
    link(page, "Back to version 1").click()
    check("Kemi Kitchen: version 1 is ready")

    # Download the zip.
    with page.expect_download() as download:
        link(page, "Download all files as a zip").click()
    assert download.value.suggested_filename == "kemi-kitchen-version-1.zip"
    with zipfile.ZipFile(io.BytesIO(open(download.value.path(), "rb").read())) as zf:
        assert sorted(zf.namelist()) == ["about.html", "index.html", "style.css"]

    # The preview is served under the sandbox policy and is itself a sound page.
    with page.expect_response(lambda r: "/build/p/" in r.url and r.url.endswith("index.html")) as preview:
        link(page, "Open the preview of version 1").click()
    headers = preview.value.headers
    assert headers["content-security-policy"].startswith("sandbox allow-scripts")
    assert "allow-same-origin" not in headers["content-security-policy"]
    assert headers["referrer-policy"] == "no-referrer"
    check("Kemi Kitchen menu")
    page.go_back()
    check("Kemi Kitchen: version 1 is ready")

    # Ask for a change, first with nothing written.
    button(page, "Ask the AI to make this change").click()
    check("Kemi Kitchen: version 1 is ready: please fix the problems")
    link(page, "Describe the change you want.").click()
    expect(page.get_by_label("The change you want")).to_be_focused()
    fake.on("build.change", CHANGED)
    page.get_by_label("The change you want").fill("Small chops now cost 1500.")
    button(page, "Ask the AI to make this change").click()
    check("Kemi Kitchen: version 2 is ready")
    expect(page.get_by_role("heading", name="What changed (computed by the site, not the AI)")).to_be_visible()
    expect(page.get_by_text("Changed index.html: 1 line added, 1 line removed.")).to_be_visible()

    # The changed lines, marked with words.
    link(page, "See the changed lines").click()
    check("Kemi Kitchen: the changed lines in version 2")
    expect(page.get_by_text("Removed: <p>Small chops: 1200 naira</p>")).to_be_visible()
    expect(page.get_by_text("Added: <p>Small chops: 1500 naira</p>")).to_be_visible()
    link(page, "Back to version 2").click()
    check("Kemi Kitchen: version 2 is ready")

    # Fix the problems the checks found: a new version with no problems.
    fake.on("build.change", FIXED)
    button(page, "Fix the problems the checks found").click()
    check("Kemi Kitchen: version 3 is ready")
    expect(button(page, "Fix the problems the checks found")).to_have_count(0)
    assert "In about.html:" in fake.calls[-1][2]["prompt"]

    # The project page lists every version; an older one warns that changes start from it.
    link(page, "All versions of this project").click()
    check("Kemi Kitchen: your project and its versions")
    expect(page.get_by_role("heading", name="All versions (3)")).to_be_visible()
    link(page, "Version 1").click()
    check("Kemi Kitchen: version 1 is ready")
    expect(page.get_by_text("This is an older version.")).to_be_visible()
    link(page, "version 3, the latest").click()
    check("Kemi Kitchen: version 3 is ready")

    # Delete, and the next page says what happened.
    link(page, "All versions of this project").click()
    link(page, "Delete this project").click()
    check("Delete Kemi Kitchen")
    button(page, "Delete this project").click()
    check("Deleted Kemi Kitchen and all its versions")
    expect(link(page, "Kemi Kitchen")).to_have_count(0)
    expect(page.get_by_text("You have no projects yet.")).to_be_visible()

    assert check.pages >= 20
    check.assert_clean()


def test_build_when_the_ai_fails(live, browser):
    fake, base = live["fake_ai"], live["base"]
    page = new_page(browser)
    check = Checker(page)
    sign_up(page, base, "build-fails@example.com", "Femi")
    signed_in(page)
    from studio.ai import AIError

    # The AI cannot read the idea: the job page says so, and the idea is kept.
    fake.on("build.understand", AIError("The AI is busy right now (rate limit). Please try again in a minute."))
    open_build(page)
    send_idea(page, title="Rivers quiz", kind="A game", idea="A quiz about rivers.")
    check("This did not finish: Reading Rivers quiz")
    expect(page.get_by_text("The AI is busy right now (rate limit).")).to_be_visible()
    open_build(page)
    link(page, "Rivers quiz").click()
    check("Rivers quiz: the AI has not read your idea yet")
    expect(page.get_by_text("A quiz about rivers.")).to_be_visible()

    # Ask again; the AI reads it this time.
    fake.on("build.understand", dict(SPEC, summary="A ten-question quiz about rivers."))
    page.get_by_label("More details").fill("Ten questions.")
    button(page, "Ask the AI to read my idea").click()
    check("Rivers quiz: here is what I understood")

    # The AI makes files the site cannot accept: nothing is saved and the reason is plain.
    fake.on("build.generate", dict(GENERATED, files=[{"path": "index.html", "content": INDEX_V1},
                                                     {"path": "../escape.html", "content": "x"}]))
    button(page, "This is right: build it").click()
    check("This did not finish: Building Rivers quiz")
    expect(page.get_by_text("cannot accept, so nothing was saved")).to_be_visible()
    link(page, "Go back and try again").click()
    check("Rivers quiz: here is what I understood")
    check.assert_clean()


def test_preview_cannot_read_the_site_cookies(live, browser):
    """Without bypassing the page's own policy, a preview runs in a sandbox with no access to cookies."""
    fake, base = live["fake_ai"], live["base"]
    setup = new_page(browser)
    sign_up(setup, base, "build-sandbox@example.com", "Sandbox")
    signed_in(setup)
    fake.on("build.understand", SPEC)
    fake.on("build.generate", GENERATED)
    open_build(setup)
    send_idea(setup, title="Sandbox test", idea="A menu page for the sandbox test.")
    button(setup, "This is right: build it").click()
    href = link(setup, "Open the preview of version 1").get_attribute("href")

    context = browser.new_context()  # the sandbox policy applies here
    page = context.new_page()
    sign_in(page, base, "build-sandbox@example.com")
    signed_in(page)
    page.goto(base + href)
    expect(page.get_by_role("heading", level=1)).to_have_text("Kemi Kitchen menu")
    assert page.evaluate("window.origin") == "null"
    result = page.evaluate("() => { try { return 'read: ' + document.cookie; } catch (e) { return 'blocked'; } }")
    assert result == "blocked"
    context.close()


def test_build_works_without_javascript(live, browser):
    fake, base = live["fake_ai"], live["base"]
    setup = new_page(browser)
    sign_up(setup, base, "build-nojs@example.com", "No Script")
    signed_in(setup)
    page = new_page(browser, javascript=False)
    sign_in(page, base, "build-nojs@example.com")
    signed_in(page)
    check = Checker(page, javascript=False)

    open_build(page)
    check("Build a website, app, tool or game")
    button(page, "Send my idea to the AI").click()
    check("Build a website, app, tool or game: please fix the problems")
    fake.on("build.understand", SPEC)
    send_idea(page, title="Quiet menu", idea="A menu that works with no scripts.")
    check("Quiet menu: here is what I understood")
    fake.on("build.generate", GENERATED)
    button(page, "This is right: build it").click()
    check("Quiet menu: version 1 is ready")
    link(page, "Read the files").click()
    check("Quiet menu: the files in version 1")
    link(page, "style.css").click()
    check("Quiet menu: style.css in version 1")
    link(page, "Back to version 1").click()
    fake.on("build.change", CHANGED)
    page.get_by_label("The change you want").fill("Small chops now cost 1500.")
    button(page, "Ask the AI to make this change").click()
    check("Quiet menu: version 2 is ready")
    link(page, "See the changed lines").click()
    check("Quiet menu: the changed lines in version 2")
    link(page, "Back to version 2").click()
    link(page, "All versions of this project").click()
    check("Quiet menu: your project and its versions")
    link(page, "Delete this project").click()
    check("Delete Quiet menu")
    button(page, "Delete this project").click()
    check("Deleted Quiet menu and all its versions")
    check.assert_clean()


def test_keyboard_order_on_the_new_idea_form(live, browser):
    base = live["base"]
    page = new_page(browser)
    sign_up(page, base, "build-keys@example.com", "Keys")
    signed_in(page)
    page.goto(base + "/build")
    page.keyboard.press("Tab")
    expect(link(page, "Skip to main content")).to_be_focused()

    first_field = page.get_by_label("Title")
    footer = page.get_by_role("navigation", name="About this site")
    for _ in range(80):
        page.keyboard.press("Tab")
        if first_field.evaluate("el => el === document.activeElement"):
            break
        assert not footer.evaluate("nav => nav.contains(document.activeElement)"), \
            "Tab reached the footer links before the form's first field"
    else:
        pytest.fail("Tab never reached the Title field")

    # The rest of the form follows in reading order: the kind of thing, the idea, then the button.
    page.keyboard.press("Tab")
    expect(page.get_by_label("A website", exact=True)).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.get_by_label("Your idea")).to_be_focused()
    page.keyboard.press("Tab")
    expect(button(page, "Send my idea to the AI")).to_be_focused()
