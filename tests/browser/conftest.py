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
    return context.new_page()


def sign_up(page, base: str, email: str = "browser@example.com", name: str = "Browser Tester") -> None:
    page.goto(base + "/register")
    page.get_by_label("Email address").fill(email)
    page.get_by_label("Your name").fill(name)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Create account").click()
    page.wait_for_load_state()


def sign_in(page, base: str, email: str = "browser@example.com") -> None:
    page.goto(base + "/login")
    page.get_by_label("Email address").fill(email)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_load_state()


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


def crawl(page, base: str, start: str = "/", limit: int = 250) -> list[str]:
    seen, queue, visited = set(), [start], []
    while queue and len(visited) < limit:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
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
