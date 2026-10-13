"""Visual checks: open a build's pages in a real browser (Chromium, through Playwright) and measure what
sighted visitors would meet, on a phone (390 by 844) and on a computer (1280 by 800).

What is measured on every page and size, with JavaScript in the page itself:
- the page scrolls sideways;
- text whose contrast with its background is below WCAG AA (4.5:1, or 3:1 for large text);
- text cut off by a box with overflow hidden or clip;
- text that sits on top of other text;
- on the phone: buttons, links that are not inside a sentence, and form fields smaller than 24 by 24
  pixels (WCAG 2.2 target size), and text smaller than 12 pixels;
- pictures that did not load.
Each page is also photographed (PNG, at most 2400 pixels tall) so an AI can look at it.

Every problem has two texts: builder_text, for the member, about the effect on visitors in plain words and
never naming colours; and fix_text, for the AI that fixes it, which may name colours and CSS.

The built pages are untrusted code. They are written to a fresh temporary directory and opened from file://
in a fresh browser context per page and size. Every request that is not a file inside that directory is
blocked, as are WebSockets, downloads, service workers and permissions; the browser has no working proxy and
no network. Each page has a hard time limit, the whole check has one too, and the browser is always closed
and the directory always deleted.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

PHONE = {"name": "phone", "width": 390, "height": 844, "label": "on a phone"}
COMPUTER = {"name": "computer", "width": 1280, "height": 800, "label": "on a computer"}
SIZES = (PHONE, COMPUTER)
SIZE_LABELS = {s["name"]: s["label"] for s in SIZES}

MAX_PAGES = 12
MAX_PLACES = 3             # example places reported per kind of problem
MAX_BLOCKED = 20           # blocked addresses remembered per check
SCREENSHOT_MAX_HEIGHT = 2400
PAGE_TIMEOUT_S = 15        # the most one page at one size may take, from opening to its picture
LAUNCH_TIMEOUT_S = 30

# The seven checks, in the order they are reported, with plain names for the member.
CHECKS = {
    "sideways": "Nothing makes the page scroll sideways",
    "contrast": "Text stands out clearly from what is behind it, so it is easy to read",
    "cut_off": "No text is cut off",
    "overlap": "No text sits on top of other text",
    "target_size": "Buttons, links and form fields are big enough to tap on a phone",
    "small_text": "Text is big enough to read on a phone",
    "broken_image": "Every picture loads",
}
PHONE_ONLY = {"target_size", "small_text"}
# Not one of the seven checks, but a problem: the page never finished opening.
TIMEOUT_KIND = "timeout"

# The member never reads these words in builder_text (the organizer's rule: talk about effects, not colours).
COLOUR_WORDS = ("red", "green", "blue", "yellow", "orange", "purple", "pink", "brown", "black", "white",
                "grey", "gray")

CHROMIUM_ARGS = [
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-sync",
    "--no-pings",
    "--dns-prefetch-disable",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
]
# A proxy that does not exist: if anything ever got past the request blocking, it would go nowhere.
DEAD_PROXY = {"server": "http://127.0.0.1:9"}


# ----- the report ------------------------------------------------------------------------------

@dataclass
class Problem:
    kind: str
    count: int
    examples: list[str]          # up to three places, by their visible text: the button "Order now"
    builder_text: str            # plain words about the effect on visitors, never colours
    fix_text: str                # a technical instruction for the AI
    details: list[str] = field(default_factory=list)  # technical facts about the examples

    def to_dict(self) -> dict:
        return {"kind": self.kind, "count": self.count, "examples": list(self.examples),
                "builder_text": self.builder_text, "fix_text": self.fix_text, "details": list(self.details)}


@dataclass
class PageCheck:
    path: str
    size: str                    # "phone" or "computer"
    width: int
    height: int
    problems: list[Problem] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)   # kinds of check passed
    unknown_contrast: int = 0    # text on a picture or a blend: contrast not measured
    checked: bool = True         # False if the page could not be measured (time limit or an error)
    note: str = ""               # why it was not checked, in plain words

    def to_dict(self) -> dict:
        return {"path": self.path, "size": self.size, "width": self.width, "height": self.height,
                "problems": [p.to_dict() for p in self.problems], "passed": list(self.passed),
                "unknown_contrast": self.unknown_contrast, "checked": self.checked, "note": self.note}


@dataclass
class Screenshot:
    path: str
    size: str
    width: int
    height: int
    png: bytes


@dataclass
class VisualReport:
    available: bool = True
    pages: list[PageCheck] = field(default_factory=list)
    screenshots: list[Screenshot] = field(default_factory=list)
    skipped_pages: list[str] = field(default_factory=list)   # HTML pages not checked (over the page limit)
    blocked_requests: list[str] = field(default_factory=list)
    error: str = ""              # plain words, if the check as a whole could not run

    @property
    def problems(self) -> list[tuple[PageCheck, Problem]]:
        return [(page, problem) for page in self.pages for problem in page.problems]

    def problem_count(self) -> int:
        return len(self.problems)

    def passed_checks(self) -> list[str]:
        """The kinds of check no measured page failed (empty if nothing was measured)."""
        measured = [p for p in self.pages if p.checked]
        if not measured:
            return []
        failed = {problem.kind for _, problem in self.problems}
        return [kind for kind in CHECKS if kind not in failed]

    def to_dict(self) -> dict:
        """Everything but the pictures, for storing as JSON."""
        return {
            "available": self.available,
            "checks": list(CHECKS),
            "sizes": [{"name": s["name"], "width": s["width"], "height": s["height"]} for s in SIZES],
            "pages": [p.to_dict() for p in self.pages],
            "passed_checks": self.passed_checks(),
            "problem_count": self.problem_count(),
            "skipped_pages": list(self.skipped_pages),
            "blocked_requests": list(self.blocked_requests),
            "screenshots": [{"path": s.path, "size": s.size, "width": s.width, "height": s.height}
                            for s in self.screenshots],
            "error": self.error,
        }


# ----- is a browser available? ---------------------------------------------------------------------

_lock = threading.Lock()
_available: bool | None = None
_sandbox = True  # Chromium's own sandbox; off only where it cannot start (for example as root in a container)


def available() -> bool:
    """True when Playwright is installed and Chromium starts here. Asked once, then remembered. Never raises."""
    global _available
    with _lock:
        if _available is None:
            try:
                _available = _probe()
            except BaseException:  # noqa: BLE001 - a missing browser must never break a build
                _available = False
        return _available


def _probe() -> bool:
    if os.environ.get("STUDIO_VISUAL_CHECKS", "").strip().lower() in ("0", "off", "no", "false"):
        return False
    try:
        import playwright.async_api  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    result: dict = {}

    async def launch_once() -> bool:
        global _sandbox
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            for sandbox in (True, False):
                try:
                    browser = await pw.chromium.launch(chromium_sandbox=sandbox, args=CHROMIUM_ARGS,
                                                       proxy=DEAD_PROXY, timeout=LAUNCH_TIMEOUT_S * 1000)
                except Exception:  # noqa: BLE001
                    continue
                await browser.close()
                _sandbox = sandbox
                return True
        return False

    def main() -> None:
        try:
            result["ok"] = asyncio.run(asyncio.wait_for(launch_once(), LAUNCH_TIMEOUT_S * 2 + 10))
        except BaseException:  # noqa: BLE001
            result["ok"] = False

    thread = threading.Thread(target=main, name="visual-check-probe", daemon=True)
    thread.start()
    thread.join(LAUNCH_TIMEOUT_S * 2 + 20)
    return bool(result.get("ok"))


# ----- checking a site ------------------------------------------------------------------------------

def _html_pages(files: dict[str, str], pages: list[str] | None) -> tuple[list[str], list[str]]:
    """The pages to check (index.html first, at most MAX_PAGES) and the HTML pages left out."""
    wanted = [p for p in (pages if pages is not None else sorted(files)) if p in files and p.lower().endswith(".html")]
    seen: list[str] = []
    for p in (["index.html"] if "index.html" in wanted else []) + wanted:
        if p not in seen:
            seen.append(p)
    return seen[:MAX_PAGES], seen[MAX_PAGES:]


def check_site(files: dict[str, str], *, pages: list[str] | None = None, timeout_s: int = 60,
               page_timeout_s: float = PAGE_TIMEOUT_S) -> VisualReport:
    """Open the site's HTML pages in Chromium on a phone and a computer, and measure what visitors meet.
    Never raises: anything that goes wrong is in the report, in plain words."""
    chosen, skipped = _html_pages(files, pages)
    if not available():
        return VisualReport(available=False, skipped_pages=chosen + skipped,
                            error="This server cannot open pages in a browser, so the look was not checked.")
    report = VisualReport(skipped_pages=skipped)
    if not chosen:
        report.error = "There are no HTML pages to check."
        return report
    holder: dict = {}

    def main() -> None:
        try:
            holder["report"] = asyncio.run(_check_all(files, chosen, report, timeout_s, page_timeout_s))
        except BaseException:  # noqa: BLE001 - the report says what happened
            if not report.error:
                report.error = "The browser stopped unexpectedly, so some pages were not checked."
            holder["report"] = report

    thread = threading.Thread(target=main, name="visual-check", daemon=True)
    thread.start()
    thread.join(timeout_s + 30)
    if thread.is_alive():
        report.error = (f"The check took longer than {timeout_s} seconds, so it was stopped and some pages were "
                        "not checked.")
        return report
    return holder.get("report", report)


def _inside(url: str, root: Path) -> bool:
    """True only for a file:// address of a file inside root."""
    parsed = urlparse(url)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        return False
    try:
        target = Path(unquote(parsed.path)).resolve()
    except (OSError, ValueError):
        return False
    return target == root or root in target.parents


def _write_files(files: dict[str, str], root: Path) -> None:
    for path, content in files.items():
        target = (root / path).resolve()
        if root not in target.parents:
            continue  # never write outside the directory
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


async def _check_all(files: dict[str, str], chosen: list[str], report: VisualReport, timeout_s: float,
                     page_timeout_s: float) -> VisualReport:
    from playwright.async_api import async_playwright

    loop = asyncio.get_running_loop()
    # A page stopped at its time limit leaves Playwright calls that end with "page closed": expected, not news.
    loop.set_exception_handler(lambda _loop, _context: None)
    deadline = loop.time() + timeout_s
    root = Path(tempfile.mkdtemp(prefix="studio-visual-")).resolve()
    try:
        _write_files(files, root)
        async with async_playwright() as pw:
            browser = None
            try:
                for path in chosen:
                    hung = 0.0
                    for size in SIZES:
                        remaining = deadline - loop.time()
                        if hung:
                            # It never finished opening at the first size: it would only hang again.
                            report.pages.append(_timeout_check(path, size, hung))
                            continue
                        if remaining < 2:
                            report.pages.append(_not_checked(path, size, (
                                "The time for checking ran out before this page, so it was not checked.")))
                            continue
                        if browser is None:
                            browser = await asyncio.wait_for(_launch(pw), min(LAUNCH_TIMEOUT_S, remaining))
                        limit = min(page_timeout_s, remaining)
                        page_check, healthy = await _check_one(browser, root, path, size, limit, report)
                        report.pages.append(page_check)
                        if any(p.kind == TIMEOUT_KIND for p in page_check.problems):
                            hung = limit
                        if not healthy:
                            await _close_quietly(browser)
                            browser = None
            finally:
                if browser is not None:
                    await _close_quietly(browser)
    except asyncio.TimeoutError:
        report.error = "The browser did not start in time, so the pages were not checked."
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return report


async def _launch(pw):
    return await pw.chromium.launch(chromium_sandbox=_sandbox, args=CHROMIUM_ARGS, proxy=DEAD_PROXY,
                                    timeout=LAUNCH_TIMEOUT_S * 1000)


async def _close_quietly(thing) -> None:
    try:
        await asyncio.wait_for(thing.close(), 10)
    except BaseException:  # noqa: BLE001
        pass


def _not_checked(path: str, size: dict, note: str) -> PageCheck:
    return PageCheck(path=path, size=size["name"], width=size["width"], height=size["height"], checked=False,
                     note=note)


async def _check_one(browser, root: Path, path: str, size: dict, limit: float,
                     report: VisualReport) -> tuple[PageCheck, bool]:
    """One page at one size, in its own context. Returns the result and whether the browser is still sound."""
    phone = size["name"] == "phone"
    context = await asyncio.wait_for(browser.new_context(
        viewport={"width": size["width"], "height": size["height"]},
        screen={"width": size["width"], "height": size["height"]},
        device_scale_factor=1, is_mobile=phone, has_touch=phone,
        java_script_enabled=True, accept_downloads=False, service_workers="block", permissions=[],
        offline=True, color_scheme="light", locale="en-GB",
    ), 10)
    healthy = True

    async def guard(route):
        url = route.request.url
        if _inside(url, root):
            await route.continue_()
            return
        if len(report.blocked_requests) < MAX_BLOCKED and url not in report.blocked_requests:
            report.blocked_requests.append(_public_url(url, root))
        await route.abort("blockedbyclient")

    def no_sockets(ws) -> None:
        if len(report.blocked_requests) < MAX_BLOCKED:
            report.blocked_requests.append(_public_url(ws.url, root))
        asyncio.ensure_future(ws.close())

    try:
        await context.route("**/*", guard)
        await context.route_web_socket(re.compile(".*"), no_sockets)
        measured, png, shot_width, shot_height = await asyncio.wait_for(
            _measure(context, (root / path).as_uri(), size, limit), limit)
    except asyncio.TimeoutError:
        healthy = False
        result = _timeout_check(path, size, limit)
    except Exception:  # noqa: BLE001 - a page that crashes the tab
        healthy = False
        result = _not_checked(path, size, "The browser could not open this page, so it was not checked.")
    else:
        if png:
            report.screenshots.append(Screenshot(path=path, size=size["name"], width=shot_width,
                                                 height=shot_height, png=png))
        result = _page_check(path, size, measured, root)
    try:
        await asyncio.wait_for(context.close(), 5)
    except BaseException:  # noqa: BLE001
        healthy = False
    return result, healthy


async def _measure(context, url: str, size: dict, limit: float):
    page = await context.new_page()
    page.on("popup", lambda popup: asyncio.ensure_future(popup.close()))
    page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.dismiss()))
    ms = max(1000, int(limit * 1000)) + 2000  # the asyncio limit around this always comes first
    await page.goto(url, wait_until="load", timeout=ms)
    await page.wait_for_timeout(150)  # let scripts that run on load finish their first paint
    measured = await page.evaluate(MEASURE_JS, {"phone": size["name"] == "phone", "width": size["width"],
                                                "maxPlaces": MAX_PLACES})
    height = max(1, min(int(measured.get("page_height") or size["height"]), SCREENSHOT_MAX_HEIGHT))
    # A page not set up for phones is laid out wider than the phone and shrunk: photograph all of it.
    width = max(size["width"], min(int(measured.get("layout_width") or size["width"]), 1600))
    png = await page.screenshot(type="png", full_page=True, animations="disabled", caret="hide", timeout=ms,
                                clip={"x": 0, "y": 0, "width": width, "height": height})
    return measured, png, width, height


def _public_url(url: str, root: Path) -> str:
    """An address without the temporary directory's name (it means nothing to anyone)."""
    return url.replace(root.as_uri() + "/", "")[:200]


# ----- turning measurements into problems ------------------------------------------------------------

def _quote_list(places: list[str]) -> str:
    places = [p for p in places if p][:MAX_PLACES]
    if not places:
        return ""
    if len(places) == 1:
        return places[0]
    return ", ".join(places[:-1]) + " and " + places[-1]


def _for_example(places: list[str]) -> str:
    listed = _quote_list(places)
    return f", for example {listed}" if listed else ""


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _are(n: int, one: str, many: str) -> str:
    return f"{n} {one} is" if n == 1 else f"{n} {many} are"


def _timeout_check(path: str, size: dict, limit: float) -> PageCheck:
    seconds = max(1, int(round(limit)))
    problem = Problem(
        kind=TIMEOUT_KIND, count=1, examples=[],
        builder_text=(f"This page did not finish opening within {seconds} seconds, so the site could not check "
                      "how it looks. Something on it may keep running without stopping, which can freeze "
                      "visitors' browsers."),
        fix_text=(f"{path} did not finish loading within {seconds} seconds in Chromium at {size['width']}px wide: "
                  "probably an endless loop, or a script that never finishes. Make sure no script runs forever "
                  "or blocks the page while it loads."))
    return PageCheck(path=path, size=size["name"], width=size["width"], height=size["height"],
                     problems=[problem], checked=False,
                     note=f"This page did not finish opening within {seconds} seconds.")


def _page_check(path: str, size: dict, m: dict, root: Path) -> PageCheck:
    where = f"{path} at {size['width']}px wide ({size['name']})"
    clean = root.as_uri() + "/"

    def details(items: list) -> list[str]:
        return [str(d).replace(clean, "")[:300] for d in items][:MAX_PLACES]

    def places(items: list) -> list[str]:
        return [str(p)[:120] for p in items][:MAX_PLACES]

    problems: list[Problem] = []
    phone = size["name"] == "phone"

    side = m.get("sideways") or {}
    if side.get("overflow", 0) > 1:
        ex = places(side.get("places", []))
        problems.append(Problem(
            kind="sideways", count=1, examples=ex, details=details(side.get("details", [])),
            builder_text=("The page is wider than the screen, so visitors have to scroll sideways to see all of "
                          f"it{_for_example(ex) and ', because of ' + _quote_list(ex)}."),
            fix_text=(f"{where}: the page scrolls sideways by {side['overflow']}px. Elements that stick out: "
                      f"{'; '.join(details(side.get('details', []))) or 'not identified'}. Make the layout "
                      "responsive: max-width: 100% on images and media, no fixed widths wider than the screen, "
                      "flex-wrap or grid with minmax(), overflow-wrap: anywhere for long words, and wide tables "
                      "inside a container with overflow-x: auto.")))

    con = m.get("contrast") or {}
    if con.get("count", 0):
        ex = places(con.get("places", []))
        problems.append(Problem(
            kind="contrast", count=con["count"], examples=ex, details=details(con.get("details", [])),
            builder_text=("Some text is hard to read for many people because it does not stand out enough from "
                          f"what is behind it{_for_example(ex)}."),
            fix_text=(f"{where}: {_are(con['count'], 'piece of text', 'pieces of text')} below WCAG AA contrast: "
                      f"{'; '.join(details(con.get('details', [])))}. Change the text or background colours in "
                      "style.css so normal text reaches at least 4.5:1 and large text (24px, or 18.66px bold) "
                      "at least 3:1 against its background.")))

    cut = m.get("cut_off") or {}
    if cut.get("count", 0):
        ex = places(cut.get("places", []))
        problems.append(Problem(
            kind="cut_off", count=cut["count"], examples=ex, details=details(cut.get("details", [])),
            builder_text=f"Some text is cut off, so visitors cannot read all of it{_for_example(ex)}.",
            fix_text=(f"{where}: {_plural(cut['count'], 'box', 'boxes')} cut off text with overflow "
                      f"hidden or clip: {'; '.join(details(cut.get('details', [])))}. Let the text wrap and the "
                      "box grow: remove fixed heights and widths, overflow: hidden, white-space: nowrap and "
                      "text-overflow: ellipsis from these.")))

    over = m.get("overlap") or {}
    if over.get("count", 0):
        ex = places(over.get("places", []))
        problems.append(Problem(
            kind="overlap", count=over["count"], examples=ex, details=details(over.get("details", [])),
            builder_text=f"Some text sits on top of other text, so both are hard to read{_for_example(ex)}.",
            fix_text=(f"{where}: {_plural(over['count'], 'pair')} of text boxes overlapping: "
                      f"{'; '.join(details(over.get('details', [])))}. Keep text in normal flow, flex or grid "
                      "layout; avoid absolute or fixed positioning and negative margins for text, and give lines "
                      "a line-height of at least 1.4.")))

    if phone:
        tap = m.get("target_size") or {}
        if tap.get("count", 0):
            ex = places(tap.get("places", []))
            problems.append(Problem(
                kind="target_size", count=tap["count"], examples=ex, details=details(tap.get("details", [])),
                builder_text=("Some buttons, links or form fields are too small or too close together to tap "
                              f"easily on a phone{_for_example(ex)}."),
                fix_text=(f"{where}: {_are(tap['count'], 'control', 'controls')} smaller than 24 by 24 CSS pixels "
                          f"with too little space around them: {'; '.join(details(tap.get('details', [])))}. "
                          "Give buttons, links that stand alone (navigation, lists of links) and form fields a "
                          "min-height of 44px with padding, display: inline-block or block for standalone links, "
                          "and at least 8px between neighbouring controls.")))
        small = m.get("small_text") or {}
        if small.get("count", 0):
            ex = places(small.get("places", []))
            if small.get("zoomed_out"):
                builder = ("The page is not set up for phones: phones show it shrunk down to fit, so the text "
                           "is too small to read without zooming.")
                fix = (f"{where}: the page lays out at {small.get('layout_width')}px and phones shrink it, so "
                       "text appears below 12px. Add <meta name=\"viewport\" content=\"width=device-width, "
                       "initial-scale=1\"> to the <head> of every page, and make the layout responsive.")
            else:
                builder = f"Some text is too small to read comfortably on a phone{_for_example(ex)}."
                fix = (f"{where}: {_are(small['count'], 'piece of text', 'pieces of text')} smaller than 12px: "
                       f"{'; '.join(details(small.get('details', [])))}. Use a base font size of 16 to 18px and "
                       "no text smaller than 14px.")
            problems.append(Problem(kind="small_text", count=small["count"], examples=ex,
                                    details=details(small.get("details", [])), builder_text=builder, fix_text=fix))

    img = m.get("broken_image") or {}
    if img.get("count", 0):
        ex = places(img.get("places", []))
        problems.append(Problem(
            kind="broken_image", count=img["count"], examples=ex, details=details(img.get("details", [])),
            builder_text=("Some pictures do not load, so visitors see an empty space or a broken picture"
                          f"{_for_example(ex)}."),
            fix_text=(f"{where}: {_plural(img['count'], 'image')} failed to load: "
                      f"{'; '.join(details(img.get('details', [])))}. Use only images that are among the "
                      "project's own files (or inline SVG); remove images from other sites and missing files.")))

    failed = {p.kind for p in problems}
    applicable = [k for k in CHECKS if phone or k not in PHONE_ONLY]
    return PageCheck(path=path, size=size["name"], width=size["width"], height=size["height"], problems=problems,
                     passed=[k for k in applicable if k not in failed],
                     unknown_contrast=int(con.get("unknown", 0) or 0))


def names_a_colour(text: str) -> bool:
    """True if the text names a colour or holds a colour code (the member must never be told these)."""
    lowered = text.lower()
    if re.search(r"#[0-9a-f]{3,8}\b", lowered):
        return True
    return any(re.search(rf"\b{word}\b", lowered) for word in COLOUR_WORDS)


# ----- the measuring script, run inside each page ----------------------------------------------------

MEASURE_JS = r"""
async (opts) => {
  const MAXP = opts.maxPlaces;
  const doc = document.documentElement;
  const body = document.body || doc;
  const vw = doc.clientWidth || opts.width;
  const zoom = Math.min(1, opts.width / Math.max(vw, 1));
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'TITLE', 'OPTION', 'OPTGROUP', 'TEXTAREA',
                        'HEAD', 'META', 'LINK', 'DATALIST']);
  const MAX_ELEMENTS = 6000;
  const clean = (s, n = 60) => {
    s = (s || '').replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n - 3).trimEnd() + '...' : s;
  };

  const styles = new Map();
  const cs = (el) => { let s = styles.get(el); if (!s) { s = getComputedStyle(el); styles.set(el, s); } return s; };

  // Hidden on purpose: inside a box of 1 pixel or less that clips (the usual "visually hidden" pattern).
  const hiddenMemo = new Map();
  function clipsAway(e) {
    const s = cs(e);
    const r = e.getBoundingClientRect();
    const clips = s.overflow !== 'visible' || (s.clip && s.clip !== 'auto') || (s.clipPath && s.clipPath !== 'none');
    if (clips && (r.width <= 1.5 || r.height <= 1.5)) return true;
    if (s.clipPath && /inset\(\s*50%/.test(s.clipPath)) return true;
    if (s.clip && /^rect\(\s*0(px)?[\s,]+0(px)?[\s,]+0(px)?[\s,]+0(px)?\s*\)$/.test(s.clip)) return true;
    return false;
  }
  function hidden(el) {
    if (!el || el === doc || el.nodeType !== 1) return false;
    if (hiddenMemo.has(el)) return hiddenMemo.get(el);
    const v = clipsAway(el) || hidden(el.parentElement);
    hiddenMemo.set(el, v);
    return v;
  }
  function shown(el) {
    if (el.checkVisibility && !el.checkVisibility({visibilityProperty: true, opacityProperty: true})) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    if (r.right <= 0 || r.bottom <= 0) return false;  // parked off the page, like a skip link
    return !hidden(el);
  }

  function labelOf(el) {
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria;
    const ids = el.getAttribute('aria-labelledby');
    if (ids) {
      const t = ids.split(/\s+/).map(id => { const x = document.getElementById(id); return x ? (x.innerText || x.textContent) : ''; }).join(' ');
      if (t.trim()) return t;
    }
    if (el.labels && el.labels.length) {
      const t = [...el.labels].map(l => l.innerText || l.textContent).join(' ');
      if (t.trim()) return t;
    }
    return el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || '';
  }
  const HOST = 'a[href], button, [role=button], [role=link], h1, h2, h3, h4, h5, h6, summary, label';
  function place(el) {
    const tag = el.tagName;
    if (tag === 'IMG') {
      const alt = clean(el.getAttribute('alt'));
      return alt ? `the picture "${alt}"` : 'a picture with no description';
    }
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') {
      const type = (el.getAttribute('type') || '').toLowerCase();
      if (tag === 'INPUT' && ['submit', 'button', 'reset', 'image'].includes(type)) {
        const t = clean(el.value || labelOf(el) || el.getAttribute('alt'));
        return t ? `the button "${t}"` : 'a button with no name';
      }
      const kind = type === 'checkbox' ? 'tick box' : type === 'radio' ? 'choice' : 'form field';
      const t = clean(labelOf(el));
      return t ? `the ${kind} "${t}"` : `a ${kind} with no label`;
    }
    const host = (el.closest && el.closest(HOST)) || el;
    const role = (host.getAttribute('role') || '').toLowerCase();
    const htag = host.tagName;
    // The name a screen reader would say, when it is given, else the visible words.
    const text = clean(host.getAttribute('aria-label') || host.innerText || host.textContent || labelOf(host));
    if (htag === 'BUTTON' || role === 'button') return text ? `the button "${text}"` : 'a button with no name';
    if (htag === 'A' || role === 'link') return text ? `the link "${text}"` : 'a link with no name';
    if (/^H[1-6]$/.test(htag)) return text ? `the heading "${text}"` : 'a heading with no text';
    if (htag === 'SUMMARY') return `the expanding section "${text}"`;
    if (text) return `"${text}"`;
    if (htag === 'TABLE') return 'a table';
    if (['SVG', 'svg', 'VIDEO', 'CANVAS', 'IFRAME', 'PICTURE'].includes(htag)) return 'a picture or embedded item';
    return 'a part of the page with no text';
  }
  function sel(el) {
    const parts = [];
    for (let e = el; e && e.nodeType === 1 && parts.length < 4; e = e.parentElement) {
      let p = e.tagName.toLowerCase();
      if (e.id) { parts.unshift(p + '#' + e.id); break; }
      if (e.classList && e.classList.length) p += '.' + [...e.classList].slice(0, 2).join('.');
      parts.unshift(p);
      if (e === body) break;
    }
    return parts.join(' > ');
  }
  function addPlace(bucket, el, detail, label) {
    bucket.count += 1;
    const where = label || place(el);
    if (bucket.places.length < MAXP && !bucket.places.includes(where)) {
      bucket.places.push(where);
      bucket.details.push(detail);
    }
  }
  const bucket = () => ({count: 0, places: [], details: []});

  // Colours: any CSS colour the browser understands, as [r, g, b, alpha].
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 1;
  const c2d = canvas.getContext('2d', {willReadFrequently: true});
  const colours = new Map();
  function rgba(str) {
    if (colours.has(str)) return colours.get(str);
    let out;
    const m = /^rgba?\(([^)]+)\)$/.exec(str || '');
    if (m) {
      const p = m[1].split(/[\s,\/]+/).filter(Boolean).map(x => x.endsWith('%') ? parseFloat(x) / 100 : parseFloat(x));
      out = [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
    } else {
      c2d.clearRect(0, 0, 1, 1);
      c2d.fillStyle = '#000';
      c2d.fillStyle = str;
      c2d.fillRect(0, 0, 1, 1);
      const d = c2d.getImageData(0, 0, 1, 1).data;
      out = [d[0], d[1], d[2], d[3] / 255];
    }
    colours.set(str, out);
    return out;
  }
  const blend = (top, under) => [0, 1, 2].map(i => top[i] * top[3] + under[i] * (1 - top[3])).concat([1]);
  function lum(c) {
    const f = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
  }
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const hex = (c) => '#' + c.slice(0, 3).map(v => Math.round(v).toString(16).padStart(2, '0')).join('');
  const bgMemo = new Map();
  function background(el) {   // null when it sits on a picture or a gradient
    if (bgMemo.has(el)) return bgMemo.get(el);
    const layers = [];
    let unknown = false;
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      const s = cs(e);
      if (s.backgroundImage && s.backgroundImage !== 'none') { unknown = true; break; }
      const c = rgba(s.backgroundColor);
      if (c[3] > 0) { layers.push(c); if (c[3] >= 1) break; }
    }
    let out = null;
    if (!unknown) {
      out = [255, 255, 255, 1];  // the browser's own page colour
      for (let i = layers.length - 1; i >= 0; i--) out = blend(layers[i], out);
    }
    bgMemo.set(el, out);
    return out;
  }

  // Every visible piece of text, by the element that holds it, with the boxes of its lines.
  const owners = new Map();
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  let node, scanned = 0;
  while ((node = walker.nextNode()) && scanned < 30000) {
    scanned++;
    if (!node.nodeValue || !node.nodeValue.trim()) continue;
    const el = node.parentElement;
    if (!el || SKIP.has(el.tagName) || el.closest('svg')) continue;
    let rects = owners.get(el);
    if (rects === undefined) { rects = shown(el) ? [] : null; owners.set(el, rects); }
    if (rects === null) continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    for (const r of range.getClientRects()) if (r.width > 0.5 && r.height > 0.5) rects.push(r);
  }
  const texts = [...owners].filter(([, rects]) => rects && rects.length).slice(0, 3000);

  // (b) contrast
  const contrast = Object.assign(bucket(), {unknown: 0});
  for (const [el] of texts) {
    const s = cs(el);
    const fg = rgba(s.webkitTextFillColor || s.color);
    const bg = background(el);
    if (fg[3] === 0 || !bg) { contrast.unknown++; continue; }
    const text = blend(fg, bg);
    const r = ratio(text, bg);
    const px = parseFloat(s.fontSize) || 16;
    const weight = parseInt(s.fontWeight, 10) || 400;
    const need = (px >= 24 || (px >= 18.66 && weight >= 700)) ? 3 : 4.5;
    if (r + 0.005 < need) {
      addPlace(contrast, el, `${sel(el)}: text ${hex(text)} on ${hex(bg)}, ${r.toFixed(2)}:1, needs ${need}:1 (font ${px}px, weight ${weight})`);
    }
  }

  // (c) text cut off by a box that hides what does not fit
  const cut = bucket();
  const all = [...body.querySelectorAll('*')].slice(0, MAX_ELEMENTS);
  const scrolls = (s) => ['auto', 'scroll'].includes(s.overflowX) || ['auto', 'scroll'].includes(s.overflowY);
  for (const el of all) {
    if (SKIP.has(el.tagName) || el.closest('svg') || ['INPUT', 'SELECT', 'IMG'].includes(el.tagName)) continue;
    const s = cs(el);
    const cx = s.overflowX === 'hidden' || s.overflowX === 'clip';
    const cy = s.overflowY === 'hidden' || s.overflowY === 'clip';
    if (!cx && !cy) continue;
    if (!shown(el)) continue;
    const box = el.getBoundingClientRect();
    const left = box.left + (parseFloat(s.borderLeftWidth) || 0), right = box.right - (parseFloat(s.borderRightWidth) || 0);
    const top = box.top + (parseFloat(s.borderTopWidth) || 0), bottom = box.bottom - (parseFloat(s.borderBottomWidth) || 0);
    let culprit = null;
    for (const [t, rects] of texts) {
      if (!el.contains(t)) continue;
      let skip = false;
      for (let e = t; e && e !== el; e = e.parentElement) {
        const es = cs(e);
        if (es.position === 'fixed' || scrolls(es)) { skip = true; break; }
      }
      if (skip) continue;
      for (const r of rects) {
        if ((cx && (r.right > right + 1 || r.left < left - 1)) || (cy && (r.bottom > bottom + 1 || r.top < top - 1))) { culprit = t; break; }
      }
      if (culprit) break;
    }
    if (culprit) addPlace(cut, culprit, `${sel(el)} (overflow ${s.overflowX} ${s.overflowY}, ${Math.round(box.width)}x${Math.round(box.height)}px) hides part of ${sel(culprit)}`);
  }

  // (d) text on top of other text
  const overlap = bucket();
  const area = (r) => Math.max(0, r.width) * Math.max(0, r.height);
  const items = texts.slice(0, 900).map(([el, rects]) => {
    let l = Infinity, t = Infinity, r = -Infinity, b = -Infinity, a = 0;
    for (const x of rects) { l = Math.min(l, x.left); t = Math.min(t, x.top); r = Math.max(r, x.right); b = Math.max(b, x.bottom); a += area(x); }
    return {el, rects, l, t, r, b, a};
  });
  for (let i = 0; i < items.length; i++) {
    const A = items[i];
    for (let j = i + 1; j < items.length; j++) {
      const B = items[j];
      if (A.r <= B.l || B.r <= A.l || A.b <= B.t || B.b <= A.t) continue;
      if (A.el.contains(B.el) || B.el.contains(A.el)) continue;
      let shared = 0;
      for (const x of A.rects) for (const y of B.rects) {
        const w = Math.min(x.right, y.right) - Math.max(x.left, y.left);
        const h = Math.min(x.bottom, y.bottom) - Math.max(x.top, y.top);
        if (w > 0 && h > 0) shared += w * h;
      }
      if (shared > 0.2 * Math.min(A.a, B.a)) {
        const label = `${place(A.el)} and ${place(B.el)}`;
        addPlace(overlap, A.el, `${sel(A.el)} overlaps ${sel(B.el)} by ${Math.round(shared)} square px`, label);
      }
    }
  }

  // (e) targets too small to tap, on a phone (WCAG 2.2 target size, with its spacing and inline exceptions)
  const targetSize = bucket();
  if (opts.phone) {
    const TSEL = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=link], [role=checkbox], [role=radio], [role=switch], [role=tab], [role=menuitem]';
    const ACTIVE = 'a[href], button, [role=link], [role=button]';
    function inSentence(el) {
      if (!(el.tagName === 'A' || (el.getAttribute('role') || '') === 'link')) return false;
      if (cs(el).display !== 'inline') return false;
      let block = el.parentElement;
      while (block && block !== body && (cs(block).display.startsWith('inline') || cs(block).display === 'contents')) block = block.parentElement;
      if (!block) return false;
      let other = '';
      const w = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
      let t;
      while ((t = w.nextNode()) && other.length < 500) {
        const holder = t.parentElement && t.parentElement.closest(ACTIVE);
        if (holder && block.contains(holder)) continue;
        other += ' ' + t.nodeValue;
      }
      return /[\p{L}\p{N}]{2,}/u.test(other);
    }
    const targets = [];
    for (const el of [...body.querySelectorAll(TSEL)].slice(0, 1500)) {
      if (el.disabled || !shown(el) || inSentence(el)) continue;
      const r = el.getBoundingClientRect();
      let box = {l: r.left, t: r.top, r: r.right, b: r.bottom};
      const type = (el.getAttribute('type') || '').toLowerCase();
      if ((type === 'checkbox' || type === 'radio') && el.labels) {
        for (const lab of el.labels) {
          const lr = lab.getBoundingClientRect();
          if (lr.width < 1 || lr.height < 1) continue;
          const gap = Math.max(lr.left - box.r, box.l - lr.right, lr.top - box.b, box.t - lr.bottom, 0);
          if (gap <= 8) box = {l: Math.min(box.l, lr.left), t: Math.min(box.t, lr.top), r: Math.max(box.r, lr.right), b: Math.max(box.b, lr.bottom)};
        }
      }
      const w = box.r - box.l, h = box.b - box.t;
      targets.push({el, box, w, h, small: w < 23.5 || h < 23.5, cx: (box.l + box.r) / 2, cy: (box.t + box.b) / 2});
    }
    for (const T of targets) {
      if (!T.small) continue;
      let ok = true;
      for (const O of targets) {
        if (O === T || O.el.contains(T.el) || T.el.contains(O.el)) continue;
        const dx = Math.max(O.box.l - T.cx, 0, T.cx - O.box.r);
        const dy = Math.max(O.box.t - T.cy, 0, T.cy - O.box.b);
        if (Math.hypot(dx, dy) < 12) { ok = false; break; }
        if (O.small && Math.hypot(O.cx - T.cx, O.cy - T.cy) < 24) { ok = false; break; }
      }
      if (!ok) addPlace(targetSize, T.el, `${sel(T.el)}: ${Math.round(T.w)}x${Math.round(T.h)}px with another control within 12px`);
    }
  }

  // (f) text too small on a phone
  const small = Object.assign(bucket(), {zoomed_out: zoom < 0.99, layout_width: vw});
  if (opts.phone) {
    for (const [el] of texts) {
      const px = parseFloat(cs(el).fontSize) || 16;
      const seen = px * zoom;
      if (seen < 11.95) addPlace(small, el, `${sel(el)}: ${px}px${zoom < 0.99 ? `, shown at about ${seen.toFixed(1)}px because the page is shrunk to ${Math.round(zoom * 100)}%` : ''}`);
    }
  }

  // (g) pictures that did not load
  const broken = bucket();
  for (const img of [...document.images].slice(0, 500)) {
    if (!img.getAttribute('src') && !img.getAttribute('srcset')) continue;
    if (img.checkVisibility && !img.checkVisibility()) continue;
    if (!img.complete) continue;  // still waiting (for example a picture far down that loads later)
    let bad = img.naturalWidth === 0;
    if (bad) {
      try { await Promise.race([img.decode(), new Promise((_, no) => setTimeout(no, 1000))]); bad = false; } catch (e) { bad = true; }
    }
    if (bad) addPlace(broken, img, `${sel(img)} src="${clean(img.getAttribute('src') || img.getAttribute('srcset'), 150)}"`);
  }

  // (a) sideways scrolling
  const sideways = Object.assign(bucket(), {overflow: Math.max(0, Math.round(doc.scrollWidth - doc.clientWidth))});
  if (sideways.overflow > 1) {
    const cw = doc.clientWidth;
    const containedX = (el) => {
      for (let e = el.parentElement; e && e !== body && e !== doc; e = e.parentElement) {
        if (cs(e).overflowX !== 'visible') return true;
      }
      return false;
    };
    for (const el of all) {
      if (SKIP.has(el.tagName)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.right <= cw + 1) continue;
      const p = el.parentElement;
      if (p && p !== body && p !== doc && p.getBoundingClientRect().right > cw + 1) continue;
      if (containedX(el)) continue;
      addPlace(sideways, el, `${sel(el)}: ${Math.round(r.width)}px wide, reaches ${Math.round(r.right)}px on a ${cw}px screen`);
    }
  }

  return {
    sideways, contrast, cut_off: cut, overlap, target_size: targetSize, small_text: small, broken_image: broken,
    page_height: Math.max(doc.scrollHeight, body.scrollHeight || 0), layout_width: vw,
  };
}
"""
