"""The site's visual checks, in a real Chromium: each kind of problem is caught on a page made to have it and on
no other page, the member's words never name colours, and a built page cannot reach the network or run forever."""
from __future__ import annotations

import http.server
import os
import tempfile
import threading
import time

import pytest

from studio import visual_check
from studio.visual_check import names_a_colour

pytestmark = pytest.mark.skipif(not visual_check.available(), reason="Playwright and Chromium are not installed")

STYLE = """<style>
body { font: 18px/1.6 system-ui, sans-serif; margin: 0; padding: 16px; color: #222; background: #fff; }
nav ul { list-style: none; display: flex; flex-wrap: wrap; gap: 12px; padding: 0; }
nav a, button, input { display: inline-block; min-height: 44px; min-width: 44px; padding: 8px 12px; font: inherit; }
label, input { display: block; }
</style>"""


def page(body: str, *, viewport: bool = True, title: str = "Test page") -> str:
    meta = '<meta name="viewport" content="width=device-width, initial-scale=1">' if viewport else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">{meta}<title>{title}</title>{STYLE}</head>'
            f"<body><main>{body}</main></body></html>")


PICTURE = ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80" viewBox="0 0 80 80"><title>Cake</title>'
           '<rect width="80" height="80" fill="#c84"/></svg>')

GOOD = page("""
<nav aria-label="Main"><ul><li><a href="index.html">Home</a></li><li><a href="menu.html">Menu</a></li></ul></nav>
<h1>Sunny Bakery</h1>
<p>Fresh bread every morning. Read <a href="about.html">our story</a> to learn more about us.</p>
<img src="cake.svg" alt="A birthday cake" width="80" height="80">
<form><label for="name">Your name</label><input id="name" name="name"><button type="submit">Order now</button></form>
""")

PAGES = {
    "index.html": GOOD,
    "contrast.html": page('<h1>Contrast</h1><p style="color:#bbbbbb">Opening hours</p>'),
    "wide.html": page('<h1>Wide</h1><div style="width:900px">A box that is far too wide</div>'),
    "overlap.html": page('<h1>Overlap</h1><p>First words here</p><p style="margin-top:-40px">Second words here</p>'),
    "cut.html": page('<h1>Cut</h1><div style="width:120px;height:28px;overflow:hidden;white-space:nowrap">'
                     "This sentence is much too long for its little box</div>"),
    "tiny-buttons.html": page('<h1>Tiny buttons</h1><div><button aria-label="Add" style="min-height:0;min-width:0;'
                              'padding:0;font-size:14px">+</button><button aria-label="Remove" style="min-height:0;'
                              'min-width:0;padding:0;font-size:14px">-</button></div>'
                              '<p>Two buttons far apart are fine even when small:</p>'
                              '<button style="min-height:0;min-width:0;padding:0;font-size:14px">Left</button>'
                              '<p>Some words between them.</p>'
                              '<button style="min-height:0;min-width:0;padding:0;font-size:14px">Right</button>'),
    "tiny-text.html": page('<h1>Tiny text</h1><p style="font-size:10px">Small print about returns</p>'),
    "broken-image.html": page('<h1>Broken picture</h1><img src="missing.png" alt="Chocolate cake" width="80" height="80">'),
    "no-viewport.html": page("<h1>Not set up for phones</h1><p>Hello there</p>", viewport=False),
    "blend.html": page('<h1>Blend</h1><div style="background:linear-gradient(#123, #345);color:#fff;padding:16px">'
                       "<p>Words on a blend</p></div>"),
    "cake.svg": PICTURE,
}
EXPECTED = {  # page: {size: kinds found}
    "index.html": {"phone": set(), "computer": set()},
    "contrast.html": {"phone": {"contrast"}, "computer": {"contrast"}},
    "wide.html": {"phone": {"sideways"}, "computer": set()},
    "overlap.html": {"phone": {"overlap"}, "computer": {"overlap"}},
    "cut.html": {"phone": {"cut_off"}, "computer": {"cut_off"}},
    "tiny-buttons.html": {"phone": {"target_size"}, "computer": set()},
    "tiny-text.html": {"phone": {"small_text"}, "computer": set()},
    "broken-image.html": {"phone": {"broken_image"}, "computer": {"broken_image"}},
    "no-viewport.html": {"phone": {"small_text"}, "computer": set()},
    "blend.html": {"phone": set(), "computer": set()},
}


@pytest.fixture(scope="module")
def report():
    return visual_check.check_site(PAGES, timeout_s=120)


def result(report, path, size):
    return next(p for p in report.pages if p.path == path and p.size == size)


def test_every_page_is_checked_on_a_phone_and_a_computer(report):
    assert report.available and report.error == ""
    assert [(p.path, p.size) for p in report.pages][:2] == [("index.html", "phone"), ("index.html", "computer")]
    assert len(report.pages) == 2 * len(EXPECTED) and all(p.checked for p in report.pages)
    assert {(p.width, p.height) for p in report.pages} == {(390, 844), (1280, 800)}


@pytest.mark.parametrize("path", list(EXPECTED))
def test_each_problem_is_caught_where_it_is_and_nowhere_else(report, path):
    for size, kinds in EXPECTED[path].items():
        found = {p.kind for p in result(report, path, size).problems}
        assert found == kinds, (path, size, [p.to_dict() for p in result(report, path, size).problems])


def test_a_good_page_passes_every_check(report):
    phone, computer = result(report, "index.html", "phone"), result(report, "index.html", "computer")
    assert phone.problems == [] and computer.problems == []
    assert phone.passed == list(visual_check.CHECKS)
    assert set(computer.passed) == set(visual_check.CHECKS) - visual_check.PHONE_ONLY
    assert phone.unknown_contrast == 0


def test_problems_name_the_place_by_its_words(report):
    contrast = result(report, "contrast.html", "phone").problems[0]
    assert contrast.examples == ['"Opening hours"']
    assert contrast.builder_text == ("Some text is hard to read for many people because it does not stand out enough "
                                     'from what is behind it, for example "Opening hours".')
    assert "#bbbbbb" in contrast.fix_text and "4.5:1" in contrast.fix_text  # the AI gets the technical facts
    buttons = result(report, "tiny-buttons.html", "phone").problems[0]
    assert buttons.count == 2 and 'the button "Add"' in buttons.builder_text
    assert "44px" in buttons.fix_text
    picture = result(report, "broken-image.html", "phone").problems[0]
    assert 'the picture "Chocolate cake"' in picture.builder_text and "missing.png" in picture.fix_text
    wide = result(report, "wide.html", "phone").problems[0]
    assert "scroll sideways" in wide.builder_text and "900px" in wide.fix_text
    zoomed = result(report, "no-viewport.html", "phone").problems[0]
    assert "not set up for phones" in zoomed.builder_text and 'name="viewport"' in zoomed.fix_text


def test_text_on_a_blend_is_counted_but_does_not_fail(report):
    blend = result(report, "blend.html", "phone")
    assert blend.unknown_contrast >= 1 and "contrast" in blend.passed


def test_builder_text_never_names_colours(report):
    texts = [p.builder_text for page in report.pages for p in page.problems]
    assert len(texts) >= 10
    for text in texts:
        assert not names_a_colour(text), text
    # The words a member reads about passed checks too.
    for label in visual_check.CHECKS.values():
        assert not names_a_colour(label)


def test_pictures_are_taken_for_the_ai(report):
    shots = {(s.path, s.size): s for s in report.screenshots}
    assert ("index.html", "phone") in shots and ("index.html", "computer") in shots
    for shot in shots.values():
        assert shot.png.startswith(b"\x89PNG\r\n\x1a\n")
        assert 1 <= shot.height <= visual_check.SCREENSHOT_MAX_HEIGHT
    as_dict = report.to_dict()
    assert "png" not in str(as_dict["screenshots"]) and as_dict["problem_count"] == len(report.problems)
    assert as_dict["passed_checks"] == []  # some page fails each kind of check


def test_tall_pages_are_photographed_at_most_2400_pixels_tall():
    tall = page("<h1>Tall</h1>" + "".join(f"<p>Line {i}</p>" for i in range(400)))
    report = visual_check.check_site({"index.html": tall}, timeout_s=60)
    assert {s.height for s in report.screenshots} == {visual_check.SCREENSHOT_MAX_HEIGHT}


def test_names_a_colour():
    for text in ["The red button", "#fff text", "text #1A2B3C", "Grey on white", "a gray box"]:
        assert names_a_colour(text), text
    for text in ["Some text is hard to read", "Colours are chosen for you", "Greyhound racing", "Read more"]:
        assert not names_a_colour(text), text


# ----- safety: no network, no files outside the build, a time limit, nothing left behind -------------------

class _Counter(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        _Counter.hits.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_a_built_page_cannot_reach_the_network_or_other_files():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Counter)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    local = f"http://127.0.0.1:{server.server_address[1]}"
    _Counter.hits = []
    nosy = page(f"""<h1>Nosy</h1>
<img src="https://example.org/cake.png" alt="Remote cake">
<img src="{local}/pixel.png" alt="Local pixel">
<img src="file:///etc/hostname" alt="Server file">
<iframe src="file:///etc/passwd" title="Server file"></iframe>
<link rel="stylesheet" href="https://example.org/style.css">
<script src="https://example.org/script.js"></script>
<script>
fetch('https://example.org/data').catch(() => {{}});
fetch('{local}/fetch').catch(() => {{}});
navigator.sendBeacon && navigator.sendBeacon('{local}/beacon', 'x');
try {{ new WebSocket('ws://127.0.0.1:{server.server_address[1]}/socket'); }} catch (e) {{}}
setTimeout(() => {{ location.href = '{local}/navigate'; }}, 50);
</script>""")
    try:
        report = visual_check.check_site({"index.html": nosy}, timeout_s=60)
    finally:
        server.shutdown()
    assert _Counter.hits == []
    blocked = " ".join(report.blocked_requests)
    assert "https://example.org/" in blocked and "file:///etc/hostname" in blocked
    assert all(p.checked or p.problems for p in report.pages)


def test_an_endless_loop_is_stopped_by_the_time_limit():
    loop = page("<h1>Loop</h1><script>while (true) {}</script>")
    started = time.monotonic()
    report = visual_check.check_site({"index.html": loop, "fine.html": GOOD, "cake.svg": PICTURE}, timeout_s=60,
                                     page_timeout_s=3)
    assert time.monotonic() - started < 30
    stuck = [p for p in report.pages if p.path == "index.html"]
    assert len(stuck) == 2 and not any(p.checked for p in stuck)
    assert all(p.problems[0].kind == visual_check.TIMEOUT_KIND for p in stuck)
    assert "did not finish opening within 3 seconds" in stuck[0].problems[0].builder_text
    assert not names_a_colour(stuck[0].problems[0].builder_text)
    # The browser is started again, so the next page is still checked.
    fine = [p for p in report.pages if p.path == "fine.html"]
    assert len(fine) == 2 and all(p.checked and not p.problems for p in fine)


def test_the_whole_check_has_a_time_limit():
    loop = page("<h1>Loop</h1><script>while (true) {}</script>")
    files = {"index.html": loop, "a.html": loop, "b.html": loop}
    started = time.monotonic()
    report = visual_check.check_site(files, timeout_s=5, page_timeout_s=3)
    assert time.monotonic() - started < 25
    assert not any(p.checked for p in report.pages)
    assert any("time for checking ran out" in p.note for p in report.pages)


def test_the_temporary_files_are_deleted():
    def leftovers():
        return {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("studio-visual-")}

    before = leftovers()
    visual_check.check_site({"index.html": GOOD}, timeout_s=60)
    assert leftovers() == before


def test_files_are_never_written_outside_the_temporary_directory(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    visual_check._write_files({"index.html": "x", "../escape.html": "y", "a/b.css": "z"}, root.resolve())
    assert (root / "index.html").exists() and (root / "a" / "b.css").exists()
    assert not (tmp_path / "escape.html").exists()


def test_only_files_inside_the_directory_are_allowed(tmp_path):
    root = tmp_path.resolve()
    assert visual_check._inside((root / "index.html").as_uri(), root)
    assert not visual_check._inside((root.parent / "other.html").as_uri(), root)
    assert not visual_check._inside(root.as_uri() + "/../secret.txt", root)
    assert not visual_check._inside("https://example.org/", root)
    assert not visual_check._inside("file://otherhost" + str(root / "index.html"), root)


def test_index_comes_first_and_at_most_12_pages_are_checked():
    files = {f"p{i:02}.html": "x" for i in range(14)} | {"index.html": "x", "style.css": "y"}
    chosen, skipped = visual_check._html_pages(files, None)
    assert chosen[0] == "index.html" and len(chosen) == 12 and len(skipped) == 3
    assert "style.css" not in chosen + skipped


def test_without_a_browser_the_report_says_so(monkeypatch):
    monkeypatch.setattr(visual_check, "_available", False)
    report = visual_check.check_site({"index.html": GOOD})
    assert report.available is False and report.pages == [] and "cannot open pages" in report.error
    assert report.to_dict()["available"] is False


# ----- resources, the sandbox and the browser's environment --------------------------------------------------

def _descendants() -> list[int]:
    """The process ids of every process started (directly or not) by this one."""
    parents = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            try:
                with open(f"/proc/{name}/stat") as f:
                    parents[int(name)] = int(f.read().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                pass
    found, todo = set(), [os.getpid()]
    while todo:
        parent = todo.pop()
        for pid, pp in parents.items():
            if pp == parent and pid not in found:
                found.add(pid)
                todo.append(pid)
    return sorted(found)


class _Sampler:
    """Looks at this process's descendants every 50 ms while a check runs."""

    def __init__(self, look):
        self.look, self.results, self._stop = look, [], threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            self.results.append(self.look(_descendants()))
            time.sleep(0.05)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(5)


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs /proc")
def test_a_page_cannot_open_windows_that_open_more_windows():
    fan_out = page("""<h1>Fan out</h1><a id="link" href="index.html?link=1" target="_blank">Next</a>
<form id="form" action="index.html" target="_blank"><input name="form" aria-label="Form"></form>
<script>
const depth = +(new URLSearchParams(location.search).get('d') || 0);
if (depth < 5) for (let i = 0; i < 4; i++) {
  try { window.open('index.html?d=' + (depth + 1) + '&i=' + i, '_blank', 'noopener'); } catch (e) {}
}
if (!location.search) {
  document.getElementById('link').dispatchEvent(new MouseEvent('click', {bubbles: true}));
  setTimeout(() => document.getElementById('form').submit(), 50);
}
</script>""")
    with _Sampler(len) as sampler:
        visual_check.check_site({"index.html": fan_out}, timeout_s=60, page_timeout_s=5)
    # A browser, its helpers, at most two pages' processes and Playwright's driver: never hundreds.
    assert max(sampler.results) < 25, max(sampler.results)
    assert "--renderer-process-limit=2" in visual_check.CHROMIUM_ARGS
    assert any(a.startswith("--js-flags=--max-old-space-size=") for a in visual_check.CHROMIUM_ARGS)


def test_only_one_check_runs_at_a_time(monkeypatch):
    real, running, most = visual_check._check_all, [0], [0]

    async def counted(*args, **kwargs):
        running[0] += 1
        most[0] = max(most[0], running[0])
        try:
            await __import__("asyncio").sleep(0.3)
            return await real(*args, **kwargs)
        finally:
            running[0] -= 1

    monkeypatch.setattr(visual_check, "_check_all", counted)
    reports = []
    threads = [threading.Thread(target=lambda: reports.append(visual_check.check_site({"index.html": GOOD})))
               for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert most[0] == 1 and len(reports) == 3 and all(p.checked for r in reports for p in r.pages)


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs /proc")
def test_the_browser_gets_none_of_the_sites_secrets(monkeypatch):
    monkeypatch.setenv("STUDIO_SECRET_KEY", "secret-for-the-visual-check-test")
    busy = page("<h1>Busy</h1><script>const t = Date.now(); while (Date.now() - t < 1500) {}</script>")

    def browsers(pids):
        out = []
        for pid in pids:
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    program = os.path.basename(f.read().split(b"\0")[0]).decode()
                with open(f"/proc/{pid}/environ", "rb") as f:
                    environ = f.read()
            except OSError:
                continue
            if "chrom" in program or "headless_shell" in program:
                out.append(environ)
        return out

    with _Sampler(browsers) as sampler:
        visual_check.check_site({"index.html": busy}, timeout_s=60)
    seen = [environ for found in sampler.results for environ in found]
    assert seen, "no browser process was seen"
    assert not any(b"secret-for-the-visual-check-test" in environ for environ in seen)
    assert not any(b"STUDIO_SECRET_KEY" in environ for environ in seen)


class _FakeBrowser:
    async def close(self):
        pass


class _FakeChromium:
    """Chromium where the sandbox cannot start (as for a site run as root)."""

    def __init__(self):
        self.launches: list[dict] = []

    async def launch(self, **kwargs):
        self.launches.append(kwargs)
        if kwargs["chromium_sandbox"]:
            raise RuntimeError("No usable sandbox!")
        return _FakeBrowser()


class _FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_without_its_sandbox_the_browser_is_not_used_unless_the_operator_says_so(monkeypatch):
    import playwright.async_api
    chromium = _FakeChromium()
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: _FakePlaywright(chromium))
    monkeypatch.setattr(visual_check, "_sandbox", True)
    monkeypatch.delenv("STUDIO_VISUAL_CHECKS", raising=False)
    monkeypatch.setenv("STUDIO_SECRET_KEY", "not-for-the-browser")
    monkeypatch.delenv("STUDIO_VISUAL_CHECKS_ALLOW_NO_SANDBOX", raising=False)
    assert visual_check._probe() is False
    assert [launch["chromium_sandbox"] for launch in chromium.launches] == [True]
    # The operator chose to run without it.
    monkeypatch.setenv("STUDIO_VISUAL_CHECKS_ALLOW_NO_SANDBOX", "1")
    chromium.launches.clear()
    assert visual_check._probe() is True and visual_check._sandbox is False
    assert [launch["chromium_sandbox"] for launch in chromium.launches] == [True, False]
    # Either way the browser gets a bare environment, never the site's.
    for launch in chromium.launches:
        assert set(launch["env"]) == {"PATH", "HOME", "LANG"}
        assert "not-for-the-browser" not in str(launch["env"])


# ----- what is measured: pictures, faded text, overlays, tight lines, lazy pictures, a check cut short -------

NOISE = page("""<h1>Noise</h1><canvas id="c" width="1280" height="2400" style="display:block;width:100%"></canvas>
<script>
const c = document.getElementById('c'), x = c.getContext('2d'), img = x.createImageData(c.width, c.height);
for (let i = 0; i < img.data.length; i++) img.data[i] = (i % 4 === 3) ? 255 : Math.random() * 256;
x.putImageData(img, 0, 0);
</script>""")


def test_pictures_are_small_enough_to_send_and_only_as_many_as_wanted():
    report = visual_check.check_site({"index.html": NOISE, "about.html": GOOD, "menu.html": GOOD, "cake.svg": PICTURE},
                                     timeout_s=120, max_screenshots=3)
    assert len(report.screenshots) + len(report.dropped_screenshots) >= 3
    assert len(report.screenshots) <= 3
    for shot in report.screenshots:
        assert len(shot.png) <= visual_check.SCREENSHOT_MAX_BYTES
        magic = b"\x89PNG" if shot.media_type == "image/png" else b"\xff\xd8\xff"
        assert shot.png.startswith(magic)
    # On a computer the noisy page would be a PNG of about 9 MB: it is sent as a JPEG, or left out and said so.
    noisy = [s for s in report.screenshots if s.path == "index.html"]
    assert len(noisy) + len([d for d in report.dropped_screenshots if d.startswith("index.html")]) == 2
    assert all(s.media_type == "image/jpeg" for s in noisy if s.size == "computer")
    assert all(p.checked for p in report.pages) and len(report.pages) == 6


FADED_PAGES = {
    "index.html": page('<h1>Faded</h1><p style="opacity:.25">Faded words</p>'
                       '<p style="filter:opacity(30%)">Filtered words</p>'),
    "reveal.html": page('<h1>Reveal</h1><p style="color:#cccccc;opacity:0">Shown when scrolled to</p>'),
    "blend-mode.html": page('<h1>Blend mode</h1><p style="color:#cccccc;mix-blend-mode:multiply">Blended words</p>'),
    "fade-in.html": page('<style>@keyframes in { from { opacity: 0 } to { opacity: 1 } }</style>'
                         '<h1>Fade in</h1><p style="animation: in 3s forwards">Words that fade in</p>'),
}


def test_faded_text_is_measured_as_it_shows_and_hidden_text_is_not_passed():
    report = visual_check.check_site(FADED_PAGES, timeout_s=120)
    for size in ("phone", "computer"):
        faded = result(report, "index.html", size)
        contrast = [p for p in faded.problems if p.kind == "contrast"]
        assert contrast and contrast[0].count == 2, faded.to_dict()
        assert '"Faded words"' in contrast[0].examples and '"Filtered words"' in contrast[0].examples
        for path in ("reveal.html", "blend-mode.html"):
            check = result(report, path, size)
            assert not check.problems and check.unknown_contrast >= 1, check.to_dict()
        # Animations are measured at their end, as the picture shows them: no false failure mid-fade.
        assert not result(report, "fade-in.html", size).problems


OVERLAY = page("""<style>
.hero { position: relative; padding: 24px; }
.hero .bg { position: absolute; inset: 0; background: #111111; }
.hero h1, .hero p { position: relative; }
.hero h1 { color: #ffffff; }
.hero p { color: #333333; }
.cover { position: relative; padding: 24px; }
.cover::before { content: ""; position: absolute; inset: 0; background: #000000; }
.cover h2 { position: relative; color: #ffffff; }
</style>
<section class="hero"><div class="bg"></div><h1>Welcome to the shop</h1><p>Dark words on the dark box</p></section>
<section class="cover"><h2>Before overlay title</h2></section>""")


def test_text_on_a_box_laid_under_it_is_measured_against_that_box():
    report = visual_check.check_site({"index.html": OVERLAY}, timeout_s=60)
    for size in ("phone", "computer"):
        check = result(report, "index.html", size)
        contrast = [p for p in check.problems if p.kind == "contrast"]
        # Light words on the dark box are fine; dark words on it are not; on a ::before box: not known.
        assert contrast and contrast[0].examples == ['"Dark words on the dark box"'], check.to_dict()
        assert "on #111111" in contrast[0].fix_text and "on #ffffff" not in contrast[0].fix_text
        assert check.unknown_contrast >= 1


def test_text_in_a_box_with_a_tight_line_height_is_not_cut_off():
    logo = page('<h1 class="logo" style="overflow:hidden;line-height:1;font-size:32px">BAKERY HOME</h1>'
                '<h2 style="overflow:hidden;line-height:1;font-size:64px">Bakery</h2>')
    report = visual_check.check_site({"index.html": logo, "cut.html": PAGES["cut.html"]}, timeout_s=60)
    for size in ("phone", "computer"):
        assert not result(report, "index.html", size).problems, result(report, "index.html", size).to_dict()
        assert {p.kind for p in result(report, "cut.html", size).problems} == {"cut_off"}


def test_pictures_far_down_are_checked_too():
    far = page('<h1>Far</h1><div style="height:9000px"></div>'
               '<img loading="lazy" src="https://images.example.org/photo-1.jpg" alt="Far cake" width="80" height="80">')
    near_enough = page('<h1>Far but ours</h1><div style="height:9000px"></div>'
                       '<img loading="lazy" src="cake.svg" alt="Our cake" width="80" height="80">')
    report = visual_check.check_site({"index.html": far, "ours.html": near_enough, "cake.svg": PICTURE}, timeout_s=60)
    for size in ("phone", "computer"):
        broken = [p for p in result(report, "index.html", size).problems if p.kind == "broken_image"]
        assert broken and 'the picture "Far cake"' in broken[0].builder_text
        ours = result(report, "ours.html", size)
        assert not ours.problems and ours.unknown_images == 0 and "broken_image" in ours.passed
    assert "broken_image" not in report.passed_checks()


def test_a_browser_that_stops_part_way_leaves_no_page_unmentioned(monkeypatch):
    real, launches = visual_check._launch, [0]

    async def once_only(*args, **kwargs):
        launches[0] += 1
        if launches[0] > 1:
            raise RuntimeError("the browser would not start again")
        return await real(*args, **kwargs)

    monkeypatch.setattr(visual_check, "_launch", once_only)
    loop = page("<h1>Loop</h1><script>while (true) {}</script>")
    pale = page('<h1>Contact</h1><p style="color:#eeeeee">Call us</p>')
    report = visual_check.check_site({"index.html": GOOD, "about.html": loop, "contact.html": pale,
                                      "cake.svg": PICTURE}, timeout_s=60, page_timeout_s=3)
    pairs = {(p.path, p.size) for p in report.pages}
    assert pairs == {(path, size["name"]) for path in ("index.html", "about.html", "contact.html")
                     for size in visual_check.SIZES}
    assert all(p.checked for p in report.pages if p.path == "index.html")
    contact = [p for p in report.pages if p.path == "contact.html"]
    assert contact and not any(p.checked for p in contact) and all(p.note for p in contact)
    assert "some pages were not checked" in report.error
    assert report.passed_checks() == []
