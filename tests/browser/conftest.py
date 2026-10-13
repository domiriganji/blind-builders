"""A live server plus a real Chromium, for axe-core checks, no-JavaScript checks and phone-width checks."""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from studio.app import create_app  # noqa: E402
from studio.config import Settings  # noqa: E402

from ..conftest import FakeAI  # noqa: E402

AXE_SOURCE = (Path(__file__).parent / "vendor" / "axe.min.js").read_text()
PASSWORD = "correct horse battery"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    import uvicorn

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    settings = Settings(secret_key="browser-tests", db_path=tmp_path_factory.mktemp("live") / "live.db",
                        jobs_inline=True, base_url=base)
    app = create_app(settings)
    fake = FakeAI()
    app.state.ai_factory = lambda user: fake
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield {"base": base, "app": app, "fake_ai": fake}
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def new_page(browser, *, javascript: bool = True, width: int = 1280):
    context = browser.new_context(bypass_csp=True, java_script_enabled=javascript,
                                  viewport={"width": width, "height": 900})
    page = context.new_page()
    _JS_OFF[id(context)] = (not javascript, browser)
    return page


_JS_OFF: dict[int, tuple] = {}


def sign_up(page, base: str, email: str = "browser@example.com", name: str = "Browser Tester") -> None:
    page.goto(base + "/register")
    page.get_by_label("Email address").fill(email)
    page.get_by_label("Your name").fill(name)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Create account").click()
    playwright_api.expect(page.get_by_role("button", name="Sign out")).to_be_visible()


def sign_in(page, base: str, email: str = "browser@example.com") -> None:
    page.goto(base + "/login")
    page.get_by_label("Email address").fill(email)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    playwright_api.expect(page.get_by_role("button", name="Sign out")).to_be_visible()


def run_axe(page) -> list[dict]:
    """Run axe-core for WCAG 2.0, 2.1 and 2.2 A and AA plus best practices; return violations."""
    page.evaluate(AXE_SOURCE)
    result = page.evaluate(
        """async () => {
            const r = await axe.run(document, {runOnly: {type: 'tag', values:
              ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice']}});
            return r.violations.map(v => ({id: v.id, impact: v.impact, help: v.help,
              nodes: v.nodes.slice(0, 5).map(n => n.target.join(' ') + ' :: ' + (n.failureSummary || '').split('\\n').slice(0, 2).join(' '))}));
        }"""
    )
    return result


def same_site_links(page, base: str) -> list[str]:
    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    out = []
    for h in hrefs:
        if not h.startswith(base):
            continue
        path = h[len(base):].split("#")[0]
        if not path or path.startswith(("/static/", "/logout")) or "/preview" in path or "/p/" in path:
            continue
        last = path.split("?", 1)[0].rsplit("/", 1)[-1]
        if "/export" in path or ("." in last and not last.endswith(".html")):
            continue  # downloads and JSON
        out.append(path)
    return out


def kind_of(path: str) -> str:
    """/find/tool/12 and /find/tool/40 are the same kind of page; so are ?page=2 and ?page=3."""
    import re
    base, _, query = path.partition("?")
    base = re.sub(r"/\d+(?=/|$)", "/{n}", base)
    keys = sorted(part.split("=")[0] for part in query.split("&") if part)
    return base + ("?" + "&".join(keys) if keys else "")


def crawl(page, base: str, start: str = "/", limit: int = 250, per_kind: int = 2) -> list[str]:
    seen, queue, visited, kinds = set(), [start], [], {}
    while queue and len(visited) < limit:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        kind = kind_of(path)
        if kinds.get(kind, 0) >= per_kind:
            continue
        kinds[kind] = kinds.get(kind, 0) + 1
        try:
            response = page.goto(base + path)
        except playwright_api.Error:
            continue  # a download or an aborted navigation
        if response is None or "text/html" not in (response.headers.get("content-type") or ""):
            continue
        visited.append(path)
        for link in same_site_links(page, base):
            if link not in seen:
                queue.append(link)
    return visited


def check_page(page, label: str = "") -> list[str]:
    """Everything a page must pass, in plain lines. Use it on every page a journey reaches.

    - axe-core: no serious or critical WCAG 2.2 A/AA or best-practice violations
    - exactly one h1, and the <title> (before the site name) says the same thing
    - a main landmark
    - no sideways scrolling at the current viewport width
    """
    where = label or page.url
    js_off, browser = _JS_OFF.get(id(page.context), (False, None))
    if js_off:
        return _check_page_without_js(page, where, browser)
    problems = []
    for v in run_axe(page):
        if v["impact"] in ("serious", "critical"):
            problems.append(f"{where}: axe [{v['impact']}] {v['id']}: {v['help']} | {' || '.join(v['nodes'])}")
    h1s = page.locator("h1")
    if h1s.count() != 1:
        problems.append(f"{where}: {h1s.count()} h1 elements")
    else:
        h1 = " ".join(h1s.first.inner_text().split())
        title = " ".join(page.title().split(" – ")[0].split())
        if h1 != title:
            problems.append(f"{where}: title {title!r} does not match h1 {h1!r}")
    if page.locator("main").count() != 1:
        problems.append(f"{where}: no single main element")
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    if overflow > 2:
        problems.append(f"{where}: scrolls sideways by {overflow}px at width {page.viewport_size['width']}")
    return problems


def check_page_narrow(page, label: str = "") -> list[str]:
    """check_page at 320 pixels wide (400 percent zoom), then back to the original width.
    Works with JavaScript off too (the full check then runs on a JavaScript-enabled twin)."""
    original = page.viewport_size
    page.set_viewport_size({"width": 320, "height": 800})
    try:
        return check_page(page, (label or page.url) + " @320px")
    finally:
        page.set_viewport_size(original)


def _check_page_without_js(page, where: str, browser) -> list[str]:
    """axe-core needs JavaScript, so for a page seen with JavaScript off: check the structure here, then open
    the same address in a JavaScript-enabled twin with the same sign-in and run the full check there."""
    problems = []
    if page.locator("h1").count() != 1:
        problems.append(f"{where} (no JavaScript): {page.locator('h1').count()} h1 elements")
    elif len(page.locator("main").inner_text().strip()) < 20:
        problems.append(f"{where} (no JavaScript): the main content is nearly empty")
    twin_context = browser.new_context(bypass_csp=True, storage_state=page.context.storage_state(),
                                       viewport=page.viewport_size)
    try:
        twin = twin_context.new_page()
        twin.goto(page.url)
        problems += check_page(twin, where + " (same address with JavaScript on)")
    finally:
        twin_context.close()
    return problems
