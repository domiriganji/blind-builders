"""Real-browser accessibility checks over every page reachable by links, signed out and signed in.

- axe-core (WCAG 2.2 A and AA plus best practices): no serious or critical violations.
- JavaScript off: every page still shows its h1 and main content.
- 320 pixels wide (what 1280 pixels looks like at 400 percent zoom): no sideways scrolling.
- Keyboard: the skip link is the first thing Tab reaches and it moves focus to the main content.
"""
import pytest

from .conftest import crawl, new_page, run_axe, sign_up

pytestmark = pytest.mark.browser
BLOCKING = {"serious", "critical"}


def _audit(page, base, paths):
    problems, notes = [], []
    for path in paths:
        page.goto(base + path)
        for v in run_axe(page):
            line = f"{path}: [{v['impact']}] {v['id']}: {v['help']} | {' || '.join(v['nodes'])}"
            (problems if v["impact"] in BLOCKING else notes).append(line)
    return problems, notes


def test_axe_signed_out(live, browser):
    page = new_page(browser)
    paths = crawl(page, live["base"])
    problems, notes = _audit(page, live["base"], paths)
    print("\n".join(notes))
    assert not problems, "\n".join(problems)


def test_axe_signed_in(live, browser):
    page = new_page(browser)
    sign_up(page, live["base"], "axe@example.com", "Axe Tester")
    paths = crawl(page, live["base"])
    assert len(paths) >= 8
    problems, notes = _audit(page, live["base"], paths)
    print("\n".join(notes))
    assert not problems, "\n".join(problems)


def test_pages_work_without_javascript(live, browser):
    page = new_page(browser, javascript=False)
    sign_up(page, live["base"], "nojs@example.com", "No JS")
    paths = crawl(page, live["base"])
    empty = []
    for path in paths:
        page.goto(live["base"] + path)
        h1 = page.locator("h1")
        if h1.count() != 1 or not h1.first.inner_text().strip() or len(page.locator("main").inner_text().strip()) < 20:
            empty.append(path)
    assert not empty, f"pages that need JavaScript to show content: {empty}"


def test_no_sideways_scrolling_at_phone_width(live, browser):
    page = new_page(browser, width=320)
    sign_up(page, live["base"], "narrow@example.com", "Narrow")
    paths = crawl(page, live["base"])
    wide = []
    for path in paths:
        page.goto(live["base"] + path)
        overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        if overflow > 2:
            wide.append(f"{path} (+{overflow}px)")
    assert not wide, f"pages that scroll sideways at 320px: {wide}"


def test_skip_link_is_first_and_works(live, browser):
    page = new_page(browser)
    page.goto(live["base"] + "/")
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.textContent.trim()") == "Skip to main content"
    page.keyboard.press("Enter")
    assert page.evaluate("document.activeElement.id") == "main"
