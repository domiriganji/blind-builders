"""Build: describe it, check what the AI understood, build, check, preview, download, change, delete."""
import base64
import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from studio import db, visual_check
from studio.a11y_check import check_html
from studio.ai import AIError
from studio.features import build
from studio.visual_check import PageCheck, Problem, Screenshot, VisualReport

from .conftest import post, register

PREVIEW_CSP = ("sandbox allow-scripts allow-forms allow-popups allow-modals; "
               "default-src 'self' 'unsafe-inline' data:; form-action 'none'; frame-ancestors 'none'")

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
<title>Kemi Kitchen</title><link rel="stylesheet" href="style.css"></head>
<body><main>
<h1>Kemi Kitchen menu</h1>
<p>Small chops: 1200 naira</p>
<p>Jollof rice: 1500 naira</p>
</main></body>
</html>
"""
ABOUT_BAD = """<!doctype html>
<html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>About</title></head>
<body><main><h1>About Kemi</h1><p>We cook every day.</p></main></body></html>
"""
FILES_V1 = [
    {"path": "index.html", "content": INDEX_V1},
    {"path": "about.html", "content": ABOUT_BAD},
    {"path": "style.css", "content": "body { font-size: 1.2rem; }\n"},
]
GENERATED = {"files": FILES_V1, "summary": "I built a **menu** page and an about page.",
             "how_to_use": "Open index.html.", "not_done": ["I guessed the opening hours."]}


# ----- a stand-in for the site's visual checks (studio/visual_check.py runs a real browser) ------------

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
CONTRAST_BUILDER = ('Some text is hard to read for many people because it does not stand out enough from what is '
                    'behind it, for example "Opening hours".')
CONTRAST_FIX = ("index.html at 390px wide (phone): 1 piece of text is below WCAG AA contrast: body > p: text #bbbbbb "
                "on #ffffff, 1.92:1, needs 4.5:1. Darken the text colour in style.css.")
REVIEW_OK = {"verdict": "looks_finished",
             "summary_for_builder": "It looks finished and welcoming, and it is easy to read on a phone and a computer.",
             "problems": []}
REVIEW_NEEDS_WORK = {"verdict": "needs_work",
                     "summary_for_builder": "It works, but the menu feels crowded on a phone.",
                     "problems": [{"page": "index.html", "builder_text": "The menu feels crowded on a phone.",
                                   "fix_text": "Give .menu li 12px of padding and set the text to #1a1a1a."}]}


def visual_report(files=None, problems=()):
    """A report shaped like visual_check.check_site's: every HTML page on a phone and a computer, with
    pictures. `problems` go on index.html at the phone size."""
    paths = sorted((p for p in (files or {"index.html": ""}) if p.endswith(".html")), key=lambda p: p != "index.html")
    report = VisualReport()
    for path in paths:
        for size in visual_check.SIZES:
            found = list(problems) if (path == "index.html" and size["name"] == "phone") else []
            kinds = [k for k in visual_check.CHECKS if size["name"] == "phone" or k not in visual_check.PHONE_ONLY]
            report.pages.append(PageCheck(path=path, size=size["name"], width=size["width"], height=size["height"],
                                          problems=found, passed=[k for k in kinds if k not in {p.kind for p in found}]))
            report.screenshots.append(Screenshot(path=path, size=size["name"], width=size["width"], height=100,
                                                 png=PNG))
    return report


def contrast_problem():
    return Problem(kind="contrast", count=1, examples=['"Opening hours"'], builder_text=CONTRAST_BUILDER,
                   fix_text=CONTRAST_FIX)


class FakeVisual:
    """Stands in for visual_check.available and check_site. Queue reports (or functions of the files) in
    .reports; when the queue is empty, every check passes."""

    def __init__(self):
        self.is_available = True
        self.reports: list = []
        self.calls: list[dict] = []
        self.kwargs: list[dict] = []

    def available(self) -> bool:
        return self.is_available

    def check_site(self, files, **kwargs):
        self.calls.append(dict(files))
        self.kwargs.append(kwargs)
        if self.reports:
            report = self.reports.pop(0)
            return report(files) if callable(report) else report
        return visual_report(files)


@pytest.fixture(autouse=True)
def fake_visual(monkeypatch, fake_ai):
    fake = FakeVisual()
    monkeypatch.setattr(build.visual_check, "available", fake.available)
    monkeypatch.setattr(build.visual_check, "check_site", fake.check_site)
    fake_ai.on("build.design_review", REVIEW_OK)
    return fake


def changed_files():
    files = json.loads(json.dumps(FILES_V1))
    files[0]["content"] = INDEX_V1.replace("Small chops: 1200 naira", "Small chops: 1500 naira")
    return {"files": files, "summary": "I changed the price of small chops.", "how_to_use": "Open index.html.",
            "not_done": []}


def assert_page_ok(r):
    assert r.status_code == 200, r.text
    assert_page_ok_status(r)


def assert_page_ok_status(r):
    """A sound page, whatever its status code (a form shown again after a refusal)."""
    assert len(re.findall(r"<h1[ >]", r.text)) == 1
    report = check_html(r.text)
    assert report.errors == [], report.findings


def h1(r) -> str:
    return re.search(r"<h1>(.*?)</h1>", r.text, re.S).group(1)


def start_project(client, fake_ai, title="Kemi Kitchen", kind="website"):
    fake_ai.on("build.understand", SPEC)
    r = post(client, "/build/new", {"title": title, "kind": kind,
                                    "idea": "A menu for my food stall.\nIt shows small chops and jollof rice."})
    assert r.status_code == 200, r.text
    match = re.search(r"/build/(\d+)$", str(r.url))
    assert match, r.url
    return int(match.group(1)), r


def build_v1(client, fake_ai, **kwargs):
    project_id, _ = start_project(client, fake_ai, **kwargs)
    fake_ai.on("build.generate", GENERATED)
    # Other test files use this helper with the real visual checks: answer the AI's look at the pictures and
    # the one repair, in case the server can run them.
    fake_ai.responses.setdefault("build.design_review", REVIEW_OK)
    fake_ai.responses.setdefault("build.visual_repair", GENERATED)
    r = post(client, f"/build/{project_id}/understand", {"action": "build", "corrections": ""})
    assert str(r.url).endswith(f"/build/{project_id}/v/1"), r.url
    return project_id, r


def share_token(project_id):
    with db.session() as conn:
        return conn.execute("SELECT share_token FROM build_projects WHERE id = ?", (project_id,)).fetchone()[0]


def calls_for(fake_ai, purpose):
    return [kw for _, p, kw in fake_ai.calls if p == purpose]


# ----- index ----------------------------------------------------------------------------------

def test_signed_out_index_explains_and_links_to_sign_in(client):
    r = client.get("/build")
    assert_page_ok(r)
    assert h1(r) == "Build a website, app, tool or game"
    assert 'href="/login?next=/build"' in r.text and 'href="/register?next=/build"' in r.text
    assert 'action="/build/new"' not in r.text


def test_signed_in_index_has_form_and_empty_list(user_client):
    r = user_client.get("/build")
    assert_page_ok(r)
    assert "Start something new" in r.text and 'action="/build/new"' in r.text
    for label in ["A website", "A web app people can install on their phone", "A small tool or calculator",
                  "A game", "Something else"]:
        assert label in r.text
    assert build.IDEA_HINT in r.text
    assert "You have no projects yet" in r.text


def test_new_project_requires_sign_in(client):
    r = post(client, "/build/new", {"kind": "website", "idea": "x"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


# ----- the full journey -----------------------------------------------------------------------

def test_full_journey(user_client, fake_ai):
    project_id, r = start_project(user_client, fake_ai)
    # What the AI understood: summary first, then the questions and additions with unticked boxes.
    assert_page_ok(r)
    assert h1(r) == "Kemi Kitchen: here is what I understood"
    assert r.text.index("Summary") < r.text.index("Who it is for") < r.text.index("Questions I need you to answer")
    assert "A menu website for Kemi&#39;s food stall." in r.text
    assert "Should prices be shown in naira?" in r.text
    assert "Things I would add that you did not ask for" in r.text and "What I will not do" in r.text
    assert 'name="add_0" value="yes">' in r.text and "checked" not in re.search(r'<input type="checkbox"[^>]*>', r.text).group()
    assert "Send my corrections and read it back again" in r.text and "This is right: build it" in r.text
    understand_call = calls_for(fake_ai, "build.understand")[0]
    assert "A menu for my food stall." in understand_call["prompt"]
    # Who the person is and how to speak to them come from the shared member note, which is not repeated here.
    assert "uses a screen reader" not in understand_call["system"]
    assert build.LOOKS_RULE in understand_call["system"]

    # Corrections re-run the understanding with the previous spec and the corrections.
    fake_ai.on("build.understand", SPEC_2)
    r = post(user_client, f"/build/{project_id}/understand", {"action": "update", "corrections": "Prices are in naira."})
    assert_page_ok(r)
    assert "A menu website in naira" in r.text and "The AI has no questions." in r.text
    second = calls_for(fake_ai, "build.understand")[1]
    assert "Prices are in naira." in second["prompt"]
    assert SPEC["summary"] in second["prompt"]
    assert "Prices are in naira." in r.text  # the person's own correction is shown back

    # Approve, with only the second addition ticked.
    fake_ai.on("build.generate", GENERATED)
    r = post(user_client, f"/build/{project_id}/understand", {"action": "build", "corrections": "", "add_1": "yes"})
    assert str(r.url).endswith(f"/build/{project_id}/v/1")
    gen = calls_for(fake_ai, "build.generate")[0]
    assert gen["max_tokens"] == 64000
    assert "A printable menu page" in gen["prompt"]
    assert "A dark colour switch" not in gen["prompt"]
    assert "no external scripts" in gen["system"].lower() and "localStorage" in gen["system"]

    # Version 1: machine checks, the AI's words labelled, Not checked.
    assert_page_ok(r)
    assert h1(r) == "Kemi Kitchen: version 1 is ready"
    text = r.text
    assert (text.index("Checked by the site") < text.index("What the AI says it built")
            < text.index("How to use it") < text.index("Not checked"))
    assert "3 files" in text
    assert "Passed" in text
    assert "Add a lang attribute" in text  # about.html has no lang: a machine-found problem
    assert "Written by the AI" in text
    assert "<strong>menu</strong>" in text
    assert "How a real screen reader reads the page, in speech and in braille" in text
    assert "I guessed the opening hours." in text
    assert "style.css" in text  # listed as not checked
    assert "What changed" not in text
    assert "Fix the problems the checks found" in text
    token = share_token(project_id)
    assert f'href="/build/p/{token}/v/1/index.html"' in text
    assert "/checks/new?url=http%3A%2F%2Ftestserver%2Fbuild%2Fp%2F" in text
    assert f"/build/{project_id}/v/1/download.zip" in text
    assert "Extras you chose: A printable menu page." in text

    # The project page now shows the latest version and the list of versions.
    r = user_client.get(f"/build/{project_id}")
    assert_page_ok(r)
    assert "Version 1" in r.text and "Kemi Kitchen: your project and its versions" == h1(r)
    assert "Extras you chose for version 1: A printable menu page." in r.text

    # Preview: right content, right type, sandbox CSP; anyone with the link can open it.
    anon = TestClient(user_client.app)
    p = anon.get(f"/build/p/{token}/v/1/index.html")
    assert p.status_code == 200
    assert p.headers["content-type"].startswith("text/html")
    assert p.headers["content-security-policy"] == PREVIEW_CSP
    assert p.text == INDEX_V1
    css = anon.get(f"/build/p/{token}/v/1/style.css")
    assert css.headers["content-type"].startswith("text/css") and css.headers["content-security-policy"] == PREVIEW_CSP
    assert anon.get(f"/build/p/{token}/v/1/").text == INDEX_V1
    assert anon.get(f"/build/p/{token}/v/1/missing.html").status_code == 404
    assert anon.get(f"/build/p/{token}/v/9/index.html").status_code == 404
    assert anon.get(f"/build/p/wrong-token/v/1/index.html").status_code == 404

    # Zip contains exactly the files.
    z = user_client.get(f"/build/{project_id}/v/1/download.zip")
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        assert sorted(zf.namelist()) == ["about.html", "index.html", "style.css"]
        assert zf.read("index.html").decode() == INDEX_V1

    # Read the files.
    r = user_client.get(f"/build/{project_id}/v/1/files")
    assert_page_ok(r)
    assert f'/build/{project_id}/v/1/files/style.css' in r.text
    r = user_client.get(f"/build/{project_id}/v/1/files/index.html")
    assert_page_ok(r)
    # One numbered item per line, each with an address of its own, so "line 7" can be found.
    assert '<li id="L7"><code>&lt;p&gt;Small chops: 1200 naira&lt;/p&gt;</code></li>' in r.text
    assert user_client.get(f"/build/{project_id}/v/1/files/nope.html").status_code == 404

    # Ask for a change: version 2, with a diff computed by the site.
    fake_ai.on("build.change", changed_files())
    r = post(user_client, f"/build/{project_id}/v/1/change", {"request": "Small chops now cost 1500."})
    assert str(r.url).endswith(f"/build/{project_id}/v/2")
    assert_page_ok(r)
    assert h1(r) == "Kemi Kitchen: version 2 is ready"
    assert "What changed (computed by the site, not the AI)" in r.text
    assert "Changed index.html: 1 line added, 1 line removed." in r.text
    assert "Nothing else changed." in r.text
    assert "Small chops now cost 1500." in r.text
    change_call = calls_for(fake_ai, "build.change")[0]
    assert change_call["max_tokens"] == 64000
    assert "Small chops now cost 1500." in change_call["prompt"]
    assert 'path="style.css"' in change_call["prompt"] and "Small chops: 1200 naira" in change_call["prompt"]

    # The changed lines, marked with words rather than + and -.
    r = user_client.get(f"/build/{project_id}/v/2/changes")
    assert_page_ok(r)
    assert "Removed: &lt;p&gt;Small chops: 1200 naira&lt;/p&gt;" in r.text
    assert "Added: &lt;p&gt;Small chops: 1500 naira&lt;/p&gt;" in r.text
    assert "Nothing else changed." in r.text
    # The words that changed come first, so nobody has to compare two long lines from memory.
    assert 'In line 7, "1200" became "1500".' in plain(r.text)
    assert plain(r.text).index('"1200" became "1500"') < plain(r.text).index("Removed: <p>Small chops")

    # Both versions keep their own preview.
    assert "1200 naira" in anon.get(f"/build/p/{token}/v/1/index.html").text
    assert "1500 naira" in anon.get(f"/build/p/{token}/v/2/index.html").text
    r = user_client.get("/build")
    assert "Latest: version 2" in r.text and "Kemi Kitchen" in r.text


def test_fix_button_sends_the_problems(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    fake_ai.on("build.change", changed_files())
    r = post(user_client, f"/build/{project_id}/v/1/fix")
    assert str(r.url).endswith(f"/build/{project_id}/v/2")
    prompt = calls_for(fake_ai, "build.change")[0]["prompt"]
    assert "In about.html:" in prompt and "Add a lang attribute" in prompt
    assert "Fix these problems" in prompt


def test_fix_with_no_problems_does_nothing(user_client, fake_ai):
    project_id, _ = start_project(user_client, fake_ai)
    fake_ai.on("build.generate", dict(GENERATED, files=[{"path": "index.html", "content": INDEX_V1}]))
    r = post(user_client, f"/build/{project_id}/understand", {"action": "build"})
    assert "Fix the problems the checks found" not in r.text
    r = post(user_client, f"/build/{project_id}/v/1/fix")
    assert "found no problems to fix" in r.text
    assert calls_for(fake_ai, "build.change") == []


def test_change_reports_added_and_removed_files(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    files = [FILES_V1[0], {"path": "contact.html", "content": "<p>a</p>\n<p>b</p>\n"}]
    fake_ai.on("build.change", {"files": files, "summary": "s", "how_to_use": "h", "not_done": []})
    r = post(user_client, f"/build/{project_id}/v/1/change", {"request": "Replace the about page with a contact page."})
    assert "Added contact.html: 2 lines." in r.text
    assert "Removed about.html" in r.text and "Removed style.css" in r.text
    assert "Nothing else changed." not in r.text


def test_phone_app_gets_manifest_rules(user_client, fake_ai):
    build_v1(user_client, fake_ai, kind="phone_app")
    system = calls_for(fake_ai, "build.generate")[0]["system"]
    assert "manifest.webmanifest" in system and "service worker" in system


def test_untitled_project_gets_suggested_title(user_client, fake_ai):
    project_id, r = start_project(user_client, fake_ai, title="")
    assert h1(r) == "Kemi&#39;s menu: here is what I understood"
    assert "You did not give a title" in r.text


# ----- file rules -----------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["../x.html", "/abs.html", "a\\b.html", "x.exe", "dir/../index.html", "noext",
                                  "has space.html", ""])
def test_bad_paths_are_rejected(path):
    with pytest.raises(build.BuildFilesError):
        build.validate_files([{"path": "index.html", "content": "x"}, {"path": path, "content": "y"}])


@pytest.mark.parametrize("path", ["../x", "/abs", "a\\b", "x.exe"])
def test_bad_paths_named_in_spec_are_rejected(path):
    with pytest.raises(build.BuildFilesError) as exc:
        build.validate_files([{"path": "index.html", "content": "x"}, {"path": path, "content": "y"}])
    assert path in str(exc.value) or "type" in str(exc.value)


def test_good_paths_are_accepted():
    files = build.validate_files([{"path": "index.html", "content": "a"}, {"path": "css/style.css", "content": "b"},
                                  {"path": "manifest.webmanifest", "content": "{}"}, {"path": "sw.js", "content": ""}])
    assert set(files) == {"index.html", "css/style.css", "manifest.webmanifest", "sw.js"}


def test_missing_index_and_other_limits_are_rejected():
    with pytest.raises(build.BuildFilesError, match="index.html"):
        build.validate_files([{"path": "home.html", "content": "x"}])
    with pytest.raises(build.BuildFilesError, match="limit is 12"):
        build.validate_files([{"path": "index.html", "content": "x"}] +
                             [{"path": f"p{i}.html", "content": "x"} for i in range(12)])
    with pytest.raises(build.BuildFilesError, match="two files"):
        build.validate_files([{"path": "index.html", "content": "x"}, {"path": "index.html", "content": "y"}])
    with pytest.raises(build.BuildFilesError):
        build.validate_files([])


def test_bad_files_from_the_ai_save_nothing(user_client, fake_ai):
    project_id, _ = start_project(user_client, fake_ai)
    fake_ai.on("build.generate", dict(GENERATED, files=[{"path": "index.html", "content": INDEX_V1},
                                                        {"path": "../x", "content": "evil"}]))
    r = post(user_client, f"/build/{project_id}/understand", {"action": "build"})
    assert "This did not finish" in r.text
    assert "The AI made a mistake in the files, so nothing was saved." in r.text
    assert "Technical detail:" in r.text and "../x" in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM build_versions").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM build_projects WHERE id = ?", (project_id,)).fetchone()[0] == "understood"


def test_missing_index_from_the_ai_is_rejected(user_client, fake_ai):
    project_id, _ = start_project(user_client, fake_ai)
    fake_ai.on("build.generate", dict(GENERATED, files=[{"path": "home.html", "content": INDEX_V1}]))
    r = post(user_client, f"/build/{project_id}/understand", {"action": "build"})
    assert "This did not finish" in r.text and "no index.html" in r.text


def test_bad_change_keeps_the_old_version(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    fake_ai.on("build.change", dict(GENERATED, files=[{"path": "index.html", "content": "x"},
                                                      {"path": "x.exe", "content": "y"}]))
    r = post(user_client, f"/build/{project_id}/v/1/change", {"request": "Add a program."})
    assert "This did not finish" in r.text and "version 1 is unchanged" in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM build_versions").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM build_projects").fetchone()[0] == "ready"


def test_compute_diff_counts_lines():
    old = {"a.html": "1\n2\n3\n", "b.css": "x\n", "gone.txt": "g\nh\n"}
    new = {"a.html": "1\nTWO\n3\n4\n", "b.css": "x\n", "new.js": "n\n"}
    diff = {e["path"]: e for e in build.compute_diff(old, new)}
    assert diff["a.html"] == {"path": "a.html", "status": "changed", "added": 2, "removed": 1}
    assert diff["gone.txt"]["status"] == "removed" and diff["gone.txt"]["removed"] == 2
    assert diff["new.js"]["status"] == "added" and diff["new.js"]["added"] == 1
    assert "b.css" not in diff
    lines = build.changed_lines(old, new)
    a = next(g for g in lines if g["path"] == "a.html")
    flat = [kind for group in a["groups"] for kind, _ in group["lines"]]
    assert flat == ["Removed", "Added", "Added"]


# ----- validation -----------------------------------------------------------------------------

def test_new_project_validation(user_client, fake_ai):
    r = post(user_client, "/build/new", {"title": "", "idea": ""})
    assert r.status_code == 400
    assert h1(r) == "Please check 2 answers: Build a website, app, tool or game"
    assert 'href="#field-idea"' in r.text and 'href="#field-kind"' in r.text
    assert 'aria-invalid="true"' in r.text
    assert fake_ai.calls == []
    r = post(user_client, "/build/new", {"title": "x" * 200, "kind": "website", "idea": "An idea"})
    assert r.status_code == 400 and 'href="#field-title"' in r.text
    r = post(user_client, "/build/new", {"kind": "spaceship", "idea": "An idea"})
    assert r.status_code == 400 and "Choose what you want to make." in r.text


@pytest.mark.parametrize("idea", ["My card is 4111 1111 1111 1111 please use it",
                                  "Use my key sk-ant-api03-abcdefghijkl",
                                  "my password is hunter2"])
def test_secrets_are_refused(user_client, fake_ai, idea):
    r = post(user_client, "/build/new", {"kind": "website", "idea": idea})
    assert r.status_code == 400 and "this site never needs them" in r.text
    assert "If it is not one, change the wording a little" in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM build_projects").fetchone()[0] == 0


def test_ordinary_numbers_are_not_refused():
    assert not build.looks_like_secret("Small chops cost 1200 naira. Call 0803 123 4567.")
    assert not build.looks_like_secret("A login page with a password field")


def test_update_needs_corrections(user_client, fake_ai):
    project_id, _ = start_project(user_client, fake_ai)
    r = post(user_client, f"/build/{project_id}/understand", {"action": "update", "corrections": ""})
    assert r.status_code == 400
    assert h1(r) == "Please check 1 answer: Kemi Kitchen: here is what I understood"
    assert 'href="#field-corrections"' in r.text
    assert len(calls_for(fake_ai, "build.understand")) == 1


def test_change_needs_a_request(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    r = post(user_client, f"/build/{project_id}/v/1/change", {"request": "  "})
    assert r.status_code == 400
    assert h1(r) == "Please check 1 answer: your change to Kemi Kitchen, version 1"
    assert "Describe the change you want." in r.text
    assert calls_for(fake_ai, "build.change") == []


def test_build_again_after_versions_exist_is_refused(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    r = post(user_client, f"/build/{project_id}/understand", {"action": "build"})
    assert "already built" in r.text
    assert len(calls_for(fake_ai, "build.generate")) == 1


def test_ai_error_while_understanding(user_client, fake_ai):
    fake_ai.on("build.understand", AIError("The AI is busy right now (rate limit)."))
    r = post(user_client, "/build/new", {"title": "Quiz", "kind": "game", "idea": "A quiz about rivers."})
    assert "This did not finish" in r.text and "The AI is busy right now" in r.text
    r = user_client.get("/build/1")
    assert_page_ok(r)
    assert h1(r) == "Quiz: the AI has not read your idea yet"
    assert "A quiz about rivers." in r.text


# ----- ownership ------------------------------------------------------------------------------

def test_other_members_get_404(client, fake_ai):
    register(client, "owner@example.com", "Owner")
    project_id, _ = build_v1(client, fake_ai)
    post(client, "/logout")
    register(client, "other@example.com", "Other")
    base = f"/build/{project_id}"
    for url in [base, f"{base}/v/1", f"{base}/v/1/files", f"{base}/v/1/files/index.html", f"{base}/v/1/changes",
                f"{base}/v/1/download.zip", f"{base}/delete"]:
        assert client.get(url).status_code == 404, url
    for url, data in [(f"{base}/understand", {"action": "build"}), (f"{base}/v/1/change", {"request": "x"}),
                      (f"{base}/v/1/fix", {}), (f"{base}/delete", {})]:
        assert post(client, url, data).status_code == 404, url
    assert "Kemi Kitchen" not in client.get("/build").text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM build_projects").fetchone()[0] == 1
    assert len(calls_for(fake_ai, "build.change")) == 0


def test_unknown_version_is_404(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    assert user_client.get(f"/build/{project_id}/v/5").status_code == 404
    assert user_client.get("/build/999").status_code == 404


# ----- no AI ----------------------------------------------------------------------------------

def test_without_ai_the_idea_is_saved_and_the_reason_is_plain(no_ai_app):
    client = TestClient(no_ai_app)
    register(client)
    r = post(client, "/build/new", {"title": "Rivers quiz", "kind": "game", "idea": "A quiz about rivers."})
    assert r.status_code == 200
    assert_page_ok(r)
    assert h1(r) == "Rivers quiz: the AI has not read your idea yet"
    assert "connect your own Claude API key" in r.text
    assert "Your idea is saved" in r.text and "A quiz about rivers." in r.text
    r = post(client, "/build/1/understand", {"action": "update", "corrections": "Ten questions."})
    assert "connect your own Claude API key" in r.text
    assert "Ten questions." in r.text  # their words are kept in the box
    r = client.get("/build")
    assert "Rivers quiz" in r.text and "Not read by the AI yet" in r.text


# ----- delete ---------------------------------------------------------------------------------

def test_delete_project(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    token = share_token(project_id)
    r = user_client.get(f"/build/{project_id}/delete")
    assert_page_ok(r)
    assert h1(r) == "Delete Kemi Kitchen"
    assert "1 version" in r.text and "cannot be undone" in r.text
    r = post(user_client, f"/build/{project_id}/delete")
    assert str(r.url).endswith("/build")
    assert "Deleted Kemi Kitchen" in r.text
    assert user_client.get(f"/build/{project_id}").status_code == 404
    assert user_client.get(f"/build/p/{token}/v/1/index.html").status_code == 404
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM build_versions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM build_projects").fetchone()[0] == 0


def test_project_pages_link_from_index(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    r = user_client.get("/build")
    assert f'href="/build/{project_id}"' in r.text
    r = user_client.get(f"/build/{project_id}")
    assert f'href="/build/{project_id}/v/1"' in r.text and f'href="/build/{project_id}/delete"' in r.text


# ----- regressions found in the audit -----------------------------------------------------------

class _Ctx:
    def progress(self, text):
        pass


def _set_row(project_id, **fields):
    with db.session() as conn:
        for k, v in fields.items():
            conn.execute(f"UPDATE build_projects SET {k} = ? WHERE id = ?", (v, project_id))


def _job_for(project_id, status="running", minutes_ago=0):
    """A job row for this project, standing in for an AI job that is still working (or one that stopped)."""
    import uuid
    with db.session() as conn:
        row = conn.execute("SELECT user_id, share_token FROM build_projects WHERE id = ?", (project_id,)).fetchone()
        conn.execute("INSERT INTO jobs (id, user_id, kind, title, status, created_at, dedupe_key)"
                     " VALUES (?, ?, 'build.change', 'Changing', ?, ?, ?)",
                     (uuid.uuid4().hex, row["user_id"], status, _minutes_ago(minutes_ago),
                      f"build:{row['share_token']}"))


def _minutes_ago(n):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(minutes=n)).replace(microsecond=0).isoformat()


def test_ai_spec_with_missing_fields_and_wrong_types_still_renders(user_client, fake_ai):
    fake_ai.on("build.understand", {"summary": "A quiz about rivers.", "pages": None, "features": "One quiz",
                                    "questions": "How many questions?", "additions_not_asked": [None, 3, ""],
                                    "not_doing": {"x": 1}, "suggested_title": 42,
                                    "audience": ["not", "a", "string"]})
    r = post(user_client, "/build/new", {"kind": "game", "idea": "A quiz about rivers."})
    assert_page_ok(r)
    assert h1(r) == "42: here is what I understood"
    assert "<li>One quiz</li>" in r.text and "<li>O</li>" not in r.text  # a string is one item, not letters
    assert "How many questions?" in r.text
    assert "No pages listed." in r.text and "The AI did not say." in r.text
    assert 'name="add_0" value="yes"' in r.text and "add_1" not in r.text  # only the one usable addition


def test_ai_spec_pages_as_strings_get_headings(user_client, fake_ai):
    fake_ai.on("build.understand", dict(SPEC, pages=["Menu", {"purpose": "Contact details"}, {}]))
    r = post(user_client, "/build/new", {"title": "Kemi Kitchen", "kind": "website", "idea": "A menu."})
    assert_page_ok(r)
    assert "<h3>Menu</h3>" in r.text and "<h3>Page 2</h3>" in r.text and "Pages (2)" in r.text


def test_ai_spec_without_summary_or_not_an_object_is_a_plain_error(user_client, fake_ai):
    fake_ai.on("build.understand", ["not", "an", "object"])
    r = post(user_client, "/build/new", {"kind": "game", "idea": "A quiz about rivers."})
    assert "This did not finish" in r.text and "missing its summary" in r.text
    fake_ai.on("build.understand", dict(SPEC, summary="  "))
    r = post(user_client, "/build/1/understand", {"action": "update"})
    assert "missing its summary" in r.text
    with db.session() as conn:
        assert tuple(conn.execute("SELECT status, spec_json FROM build_projects").fetchone()) == ("new", None)


def test_ai_files_answer_with_wrong_types(user_client, fake_ai):
    project_id, _ = start_project(user_client, fake_ai)
    fake_ai.on("build.generate", {"files": [{"path": "index.html", "content": INDEX_V1}], "summary": None,
                                  "how_to_use": 5, "not_done": "I guessed the colours."})
    r = post(user_client, f"/build/{project_id}/understand", {"action": "build"})
    assert_page_ok(r)
    assert "<li>I guessed the colours.</li>" in r.text and "<li>I</li>" not in r.text
    assert "None" not in re.search(r"What the AI says it built.*?How to use it", r.text, re.S).group()


def test_ai_files_answer_that_is_not_an_object(user_client, fake_ai):
    project_id, _ = start_project(user_client, fake_ai)
    fake_ai.on("build.generate", "just some text")
    r = post(user_client, f"/build/{project_id}/understand", {"action": "build"})
    assert "This did not finish" in r.text and "came back incomplete, so nothing was saved" in r.text
    with db.session() as conn:
        assert conn.execute("SELECT status FROM build_projects").fetchone()[0] == "understood"


def test_files_that_would_clash_in_a_zip_are_rejected():
    with pytest.raises(build.BuildFilesError, match="capital letters"):
        build.validate_files([{"path": "index.html", "content": "a"}, {"path": "Index.html", "content": "b"}])
    with pytest.raises(build.BuildFilesError, match="both a file name and a folder"):
        build.validate_files([{"path": "index.html", "content": "a"}, {"path": "a.html", "content": "b"},
                              {"path": "a.html/b.css", "content": "c"}])
    with pytest.raises(build.BuildFilesError, match="not valid text"):
        build.validate_files([{"path": "index.html", "content": "bad \ud800 text"}])


def test_huge_numbers_in_addresses_are_404_not_a_crash(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    token = share_token(project_id)
    huge = "99999999999999999999"
    for url in [f"/build/{huge}", f"/build/{project_id}/v/{huge}", f"/build/{project_id}/v/{huge}/files",
                f"/build/{huge}/delete", f"/build/p/{token}/v/{huge}/index.html", f"/build/{project_id}/v/0"]:
        r = user_client.get(url)
        assert r.status_code == 404, url
        assert "<h1>Page not found</h1>" in r.text


def test_preview_does_not_leak_its_address_or_get_sniffed(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    p = TestClient(user_client.app).get(f"/build/p/{share_token(project_id)}/v/1/index.html")
    assert p.headers["referrer-policy"] == "no-referrer"
    assert p.headers["x-content-type-options"] == "nosniff"
    assert p.headers["content-security-policy"] == PREVIEW_CSP


def test_second_change_while_the_ai_is_working_is_refused(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    _set_row(project_id, status="changing", updated_at=_minutes_ago(1))
    _job_for(project_id)
    fake_ai.on("build.change", changed_files())
    # The member's words are never lost: the form comes back with them and says why nothing was sent.
    r = post(user_client, f"/build/{project_id}/v/1/change", {"request": "Small chops now cost 1500."})
    assert r.status_code == 409
    assert_page_ok_status(r)
    assert build.BUSY_CHANGE_ERROR in r.text and 'href="#field-request"' in r.text
    assert ">Small chops now cost 1500.</textarea>" in r.text
    assert h1(r) == "Please check 1 answer: your change to Kemi Kitchen, version 1"
    r = post(user_client, f"/build/{project_id}/v/1/fix")
    assert "already working on this project" in r.text
    assert h1(r) == "Kemi Kitchen: the AI is making a change"
    assert_page_ok(r)
    assert calls_for(fake_ai, "build.change") == []
    # A job that keeps working for a long time still blocks a second, paid request.
    _set_row(project_id, status="changing", updated_at=_minutes_ago(60))
    r = post(user_client, f"/build/{project_id}/v/1/change", {"request": "Small chops now cost 1500."})
    assert build.BUSY_CHANGE_ERROR in r.text and calls_for(fake_ai, "build.change") == []
    # A job that stopped (for example in a restart) no longer blocks, and the list does not claim it is working.
    with db.session() as conn:
        conn.execute("UPDATE jobs SET status = 'failed'")
    assert "The AI stopped before it finished" in user_client.get("/build").text
    r = post(user_client, f"/build/{project_id}/v/1/change", {"request": "Small chops now cost 1500."})
    assert str(r.url).endswith(f"/build/{project_id}/v/2")


def test_second_build_while_building_is_refused(user_client, fake_ai):
    project_id, _ = start_project(user_client, fake_ai)
    _set_row(project_id, status="building", updated_at=_minutes_ago(30))
    _job_for(project_id, minutes_ago=30)
    fake_ai.on("build.generate", GENERATED)
    r = post(user_client, f"/build/{project_id}/understand", {"action": "build"})
    assert "already working on this project" in r.text
    assert h1(r) == "Kemi Kitchen: the AI is building it"
    assert_page_ok(r)
    assert calls_for(fake_ai, "build.generate") == []


def test_a_second_build_job_saves_no_second_first_version(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    with pytest.raises(AIError, match="already built"):
        build._generate(_Ctx(), fake_ai, project_id, share_token(project_id), [])
    with pytest.raises(AIError, match="already built by an earlier request"):
        build._save_version(project_id, share_token(project_id), {"index.html": "x"},
                            {"summary": "", "how_to_use": "", "not_done": []}, "First version", {}, 0)
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM build_versions").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM build_projects").fetchone()[0] == "ready"


def test_double_press_of_send_my_idea_makes_one_project(user_client, fake_ai):
    start_project(user_client, fake_ai)
    _, r = start_project(user_client, fake_ai)
    assert "You sent this idea twice" in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM build_projects").fetchone()[0] == 1
    assert len(calls_for(fake_ai, "build.understand")) == 1


def test_job_for_a_deleted_project_never_touches_a_new_project_with_the_same_id(client, fake_ai):
    register(client, "first@example.com", "First")
    project_id, _ = build_v1(client, fake_ai)
    old_token = share_token(project_id)
    post(client, f"/build/{project_id}/delete")
    post(client, "/logout")
    register(client, "second@example.com", "Second")
    new_id, _ = start_project(client, fake_ai, title="Second project")
    assert new_id == project_id  # SQLite reuses the id of the deleted newest row
    files = {"index.html": INDEX_V1}
    with pytest.raises(AIError, match="deleted while the AI was working"):
        build._save_version(project_id, old_token, files, {"summary": "", "how_to_use": "", "not_done": []},
                            "x", files, 1)
    with pytest.raises(AIError, match="deleted while the AI was working"):
        build._understand(_Ctx(), fake_ai, project_id, old_token)
    with pytest.raises(AIError, match="deleted while the AI was working"):
        build._change(_Ctx(), fake_ai, project_id, old_token, 1, "x")
    build._set_status(project_id, "building", old_token)
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM build_versions").fetchone()[0] == 0
        row = conn.execute("SELECT title, status FROM build_projects WHERE id = ?", (project_id,)).fetchone()
        assert tuple(row) == ("Second project", "understood")


def test_corrections_sent_with_build_are_kept_with_the_project(user_client, fake_ai):
    project_id, _ = start_project(user_client, fake_ai)
    fake_ai.on("build.generate", GENERATED)
    post(user_client, f"/build/{project_id}/understand", {"action": "build", "corrections": "Use naira."})
    assert "Use naira." in calls_for(fake_ai, "build.generate")[0]["prompt"]
    r = user_client.get(f"/build/{project_id}")
    assert "What you added or corrected" in r.text and "Use naira." in r.text


def test_failed_reread_keeps_the_correction_and_can_be_retried(user_client, fake_ai):
    project_id, _ = start_project(user_client, fake_ai)
    fake_ai.on("build.understand", AIError("The AI is busy right now (rate limit)."))
    r = post(user_client, f"/build/{project_id}/understand", {"action": "update", "corrections": "Prices in naira."})
    assert "This did not finish" in r.text
    r = user_client.get(f"/build/{project_id}")
    assert_page_ok(r)
    assert "has not read your newest correction yet" in r.text and "Prices in naira." in r.text
    fake_ai.on("build.understand", SPEC_2)
    r = post(user_client, f"/build/{project_id}/understand", {"action": "update", "corrections": ""})
    assert r.status_code == 200 and "A menu website in naira" in r.text
    assert "Prices in naira." in calls_for(fake_ai, "build.understand")[-1]["prompt"]
    assert "has not read your newest" not in r.text


def test_corrections_have_a_total_size_limit(user_client, fake_ai, monkeypatch):
    monkeypatch.setattr(build, "MAX_CORRECTIONS_TOTAL", 30)
    project_id, _ = start_project(user_client, fake_ai)
    post(user_client, f"/build/{project_id}/understand", {"action": "update", "corrections": "a" * 20})
    r = post(user_client, f"/build/{project_id}/understand", {"action": "update", "corrections": "b" * 20})
    assert r.status_code == 400 and "start a new project" in r.text
    assert len(calls_for(fake_ai, "build.understand")) == 2


def test_waiting_page_errors_say_please_fix(no_ai_app):
    client = TestClient(no_ai_app)
    register(client)
    post(client, "/build/new", {"title": "Rivers quiz", "kind": "game", "idea": "A quiz about rivers."})
    r = post(client, "/build/1/understand", {"action": "update", "corrections": "x" * (build.MAX_REQUEST + 1)})
    assert r.status_code == 400
    assert h1(r) == "Please check 1 answer: Rivers quiz: the AI has not read your idea yet"
    assert 'href="#field-corrections"' in r.text


def test_no_ai_headings_say_nothing_was_sent(no_ai_app):
    client = TestClient(no_ai_app)
    register(client)
    post(client, "/build/new", {"title": "Rivers quiz", "kind": "game", "idea": "A quiz about rivers."})
    r = post(client, "/build/1/understand", {"action": "update", "corrections": "Ten questions."})
    assert_page_ok(r)
    assert h1(r) == "Rivers quiz: the AI is not available, so nothing was sent to it"


def test_no_ai_on_a_version_page_says_nothing_was_sent(user_client, fake_ai):
    from studio.ai import AINotAvailable
    project_id, _ = build_v1(user_client, fake_ai)

    def no_ai(user):
        raise AINotAvailable("To use the AI, connect your own Claude API key on your account page.")

    user_client.app.state.ai_factory = no_ai
    r = post(user_client, f"/build/{project_id}/v/1/change", {"request": "Bigger text."})
    assert_page_ok(r)
    assert h1(r) == "Kemi Kitchen: version 1 is unchanged: the AI is not available, so nothing was sent to it"
    assert "Bigger text." in r.text and "connect your own Claude API key" in r.text


def test_delete_heading_says_what_happened_once(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    r = post(user_client, f"/build/{project_id}/delete")
    assert_page_ok(r)
    assert h1(r) == "Deleted Kemi Kitchen and all its versions"
    assert h1(user_client.get("/build")) == "Build a website, app, tool or game"


def test_older_version_warns_that_changes_start_from_it(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    fake_ai.on("build.change", changed_files())
    post(user_client, f"/build/{project_id}/v/1/change", {"request": "Small chops now cost 1500."})
    r = user_client.get(f"/build/{project_id}/v/1")
    assert_page_ok(r)
    assert "This is an older version" in r.text and "will not include what changed in versions after 1" in r.text
    assert "This is an older version" not in user_client.get(f"/build/{project_id}/v/2").text


def test_version_page_wording(user_client, fake_ai):
    project_id, r = build_v1(user_client, fake_ai)
    assert "Passed 1 checks" not in r.text
    assert re.search(r"Passed \d+ checks?\.", r.text)
    assert "Open the preview of version 1" in r.text
    assert "previews of every version of this project" in r.text
    assert "Open the preview of version 1" in user_client.get(f"/build/{project_id}").text


def test_zip_name_stays_short_and_plain(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai, title="Ébène " + "very long title " * 7)
    z = user_client.get(f"/build/{project_id}/v/1/download.zip")
    name = re.search(r'filename="([^"]+)"', z.headers["content-disposition"]).group(1)
    assert name.endswith("-version-1.zip") and len(name) <= 80 and name.isascii()
    assert not name.startswith("-") and "--" not in name


def test_secret_in_title_is_refused_even_when_the_idea_is_empty(user_client, fake_ai):
    r = post(user_client, "/build/new", {"title": "my password is hunter2", "kind": "website", "idea": ""})
    assert r.status_code == 400
    assert 'href="#field-title"' in r.text and 'href="#field-idea"' in r.text


# ----- how it looks to sighted visitors ------------------------------------------------------------

def look_section(text: str) -> str:
    """The version page's "How it looks on screen" section, as HTML."""
    return re.search(r"<h2>How it looks on screen</h2>(.*?)<h2>Checked by the site</h2>", text, re.S).group(1)


def plain(page: str) -> str:
    import html
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def repaired_files():
    """What the one automatic repair returns: the first files with darker text."""
    files = json.loads(json.dumps(FILES_V1))
    files[2]["content"] = "body { font-size: 1.2rem; color: #1a1a1a; }\n"
    return {"files": files, "summary": "I made the text darker.", "how_to_use": "Open index.html.", "not_done": []}


def saved_version(project_id, number=1):
    with db.session() as conn:
        return dict(conn.execute("SELECT * FROM build_versions WHERE project_id = ? AND number = ?",
                                 (project_id, number)).fetchone())


def test_when_every_visual_check_passes_there_is_no_repair(user_client, fake_ai, fake_visual):
    project_id, r = build_v1(user_client, fake_ai)
    assert_page_ok(r)
    text = r.text
    # Right after the summary line, before the accessibility details.
    assert text.index('class="lead"') < text.index("How it looks on screen") < text.index(
        "<h2>Checked by the site</h2>")
    section = plain(look_section(text))
    assert "Checked by the site on a phone and a computer: passed all 7 visual checks." in section
    assert "Passed: Text stands out clearly from what is behind it, so it is easy to read." in section
    assert "An AI looked at pictures of the pages" in section
    assert "This is the AI's opinion, not a check by the site." in section
    assert REVIEW_OK["summary_for_builder"] in section
    assert "In the AI's opinion, it looks finished." in section
    assert "Fixed before you saw it" not in section
    assert calls_for(fake_ai, "build.visual_repair") == []
    assert len(calls_for(fake_ai, "build.design_review")) == 1
    assert len(fake_visual.calls) == 1
    # The Not checked list says a person has not looked, and links to a sighted check.
    assert "but no person has looked" in text
    assert "Ask a sighted person to check how it looks" in text
    assert "kind=sighted" in text and "/checks/new?url=http%3A%2F%2Ftestserver%2Fbuild%2Fp%2F" in text
    # The look was measured, so contrast is not listed as not checked next to "Passed: Text stands out clearly".
    not_checked = plain(re.search(r"<h2>Not checked</h2>(.*?)</ul>", text, re.S).group(1))
    assert "contrast" not in not_checked.lower() and "looks right to sighted" not in not_checked
    assert "The site opens each page in a browser to check how it looks" in text
    assert "The site reads the files but does not run them." not in text
    stored = saved_version(project_id)
    assert json.loads(stored["visual_json"])["passed_checks"] == list(visual_check.CHECKS)
    assert json.loads(stored["design_review_json"])["verdict"] == "looks_finished"
    assert json.loads(stored["repaired_json"]) == {}


def test_the_repair_runs_once_when_the_checks_find_problems(user_client, fake_ai, fake_visual):
    fake_visual.reports = [lambda files: visual_report(files, [contrast_problem()])]  # then everything passes
    fake_ai.on("build.visual_repair", repaired_files())
    project_id, r = build_v1(user_client, fake_ai)
    assert_page_ok(r)
    repairs = calls_for(fake_ai, "build.visual_repair")
    assert len(repairs) == 1
    assert CONTRAST_FIX in repairs[0]["prompt"]
    assert 'path="index.html"' in repairs[0]["prompt"] and "Small chops: 1200 naira" in repairs[0]["prompt"]
    assert repairs[0]["max_tokens"] == 64000 and "WCAG AA" in repairs[0]["system"]
    assert len(fake_visual.calls) == 2  # the first files, then the repaired ones
    assert len(calls_for(fake_ai, "build.design_review")) == 2
    # The repaired files are the ones saved; the AI's summary of the build is kept.
    stored = saved_version(project_id)
    assert "#1a1a1a" in json.loads(stored["files_json"])["style.css"]
    assert stored["ai_summary"] == GENERATED["summary"]
    assert json.loads(stored["repaired_json"])["fixed"] == 1
    section = plain(look_section(r.text))
    assert "passed all 7 visual checks" in section
    assert "Fixed before you saw it The AI fixed 1 problem the checks found" in section
    assert "#1a1a1a" not in r.text and "#bbbbbb" not in r.text


def test_the_repair_never_runs_twice_and_remaining_problems_are_listed(user_client, fake_ai, fake_visual):
    fake_visual.reports = [lambda files: visual_report(files, [contrast_problem()]),
                           lambda files: visual_report(files, [contrast_problem()])]
    fake_ai.on("build.visual_repair", repaired_files())
    project_id, r = build_v1(user_client, fake_ai)
    assert_page_ok(r)
    assert len(calls_for(fake_ai, "build.visual_repair")) == 1
    section = plain(look_section(r.text))
    assert "passed 6 of 7 visual checks. It found 1 problem:" in section
    assert CONTRAST_BUILDER in section and "Where: index.html, on a phone" in section
    assert "Not passed: Text stands out clearly" in section
    assert "tried once to fix what the checks found, but the problems are still there" in section
    # The fix button covers what is left, with the technical instruction for the AI.
    assert "Fix the problems the checks found" in r.text
    fake_ai.on("build.change", changed_files())
    post(user_client, f"/build/{project_id}/v/1/fix")
    prompt = calls_for(fake_ai, "build.change")[0]["prompt"]
    assert CONTRAST_FIX in prompt and "Add a lang attribute" in prompt


def test_fix_button_appears_for_visual_problems_alone(user_client, fake_ai, fake_visual):
    project_id, _ = start_project(user_client, fake_ai)
    fake_visual.reports = [lambda files: visual_report(files, [contrast_problem()]),
                           lambda files: visual_report(files, [contrast_problem()])]
    fake_ai.on("build.generate", dict(GENERATED, files=[{"path": "index.html", "content": INDEX_V1}]))
    fake_ai.on("build.visual_repair", dict(GENERATED, files=[{"path": "index.html", "content": INDEX_V1}]))
    r = post(user_client, f"/build/{project_id}/understand", {"action": "build"})
    assert "The checks found 1 problem in how the pages look." in r.text
    assert "Fix the problems the checks found" in r.text
    fake_ai.on("build.change", dict(GENERATED, files=[{"path": "index.html", "content": INDEX_V1}]))
    r = post(user_client, f"/build/{project_id}/v/1/fix")
    assert str(r.url).endswith(f"/build/{project_id}/v/2")
    assert CONTRAST_FIX in calls_for(fake_ai, "build.change")[0]["prompt"]


@pytest.mark.parametrize("answer", [
    dict(GENERATED, files=[{"path": "index.html", "content": INDEX_V1}, {"path": "../evil.html", "content": "x"}]),
    AIError("The AI is busy right now (rate limit). Please try again in a minute."),
])
def test_a_failed_repair_keeps_the_first_files(user_client, fake_ai, fake_visual, answer):
    fake_visual.reports = [lambda files: visual_report(files, [contrast_problem()])]
    fake_ai.on("build.visual_repair", answer)
    project_id, r = build_v1(user_client, fake_ai)
    assert_page_ok(r)
    assert len(calls_for(fake_ai, "build.visual_repair")) == 1
    assert len(fake_visual.calls) == 1  # nothing new to check
    stored = saved_version(project_id)
    assert json.loads(stored["files_json"]) == {f["path"]: f["content"] for f in FILES_V1}
    section = plain(look_section(r.text))
    assert "this version keeps the files it made first" in section
    assert CONTRAST_BUILDER in section


def test_a_repair_that_breaks_accessibility_is_not_kept(user_client, fake_ai, fake_visual):
    fake_visual.reports = [lambda files: visual_report(files, [contrast_problem()])]
    worse = json.loads(json.dumps(FILES_V1))
    worse[0]["content"] = INDEX_V1.replace('<html lang="en">', "<html>")
    fake_ai.on("build.visual_repair", dict(GENERATED, files=worse))
    project_id, r = build_v1(user_client, fake_ai)
    assert json.loads(saved_version(project_id)["files_json"])["index.html"] == INDEX_V1
    assert "were not better, so this version keeps the files it made first" in plain(look_section(r.text))
    assert len(calls_for(fake_ai, "build.design_review")) == 1  # no second look at files that were not kept


def test_the_ai_review_asking_for_work_leads_to_one_repair(user_client, fake_ai, fake_visual):
    answers = [REVIEW_NEEDS_WORK, REVIEW_OK]
    fake_ai.on("build.design_review", lambda **kwargs: answers.pop(0))
    fake_ai.on("build.visual_repair", repaired_files())
    project_id, r = build_v1(user_client, fake_ai)
    assert_page_ok(r)
    repairs = calls_for(fake_ai, "build.visual_repair")
    assert len(repairs) == 1 and "Give .menu li 12px of padding" in repairs[0]["prompt"]
    section = plain(look_section(r.text))
    # Only the AI's opinion asked for this repair: the page says the design was changed, not that it improved.
    assert "Changed before you saw it After an AI looked at pictures of the pages, the AI changed the design once." in section
    assert "Fixed before you saw it" not in section and "improved" not in section
    assert REVIEW_OK["summary_for_builder"] in section


def test_ai_review_that_still_asks_for_work_is_shown_as_its_opinion(user_client, fake_ai, fake_visual):
    fake_ai.on("build.design_review", REVIEW_NEEDS_WORK)
    fake_ai.on("build.visual_repair", repaired_files())
    project_id, r = build_v1(user_client, fake_ai)
    assert len(calls_for(fake_ai, "build.visual_repair")) == 1
    section = plain(look_section(r.text))
    assert "In the AI's opinion, it still needs some work." in section
    assert "What the AI thinks could still be better: The menu feels crowded on a phone." in section
    assert "12px of padding" not in r.text  # the instruction for the AI is not shown to the member


def test_the_review_prompt_holds_pictures_and_the_purpose(user_client, fake_ai, fake_visual):
    project_id, _ = start_project(user_client, fake_ai)
    files = FILES_V1 + [{"path": f"p{i}.html", "content": INDEX_V1} for i in range(4)]
    fake_ai.on("build.generate", dict(GENERATED, files=files))
    post(user_client, f"/build/{project_id}/understand", {"action": "build"})
    call = calls_for(fake_ai, "build.design_review")[0]
    blocks = call["prompt"]
    assert isinstance(blocks, list)
    images = [b for b in blocks if b["type"] == "image"]
    assert len(images) == 6
    assert all(b["source"] == {"type": "base64", "media_type": "image/png",
                               "data": base64.b64encode(PNG).decode()} for b in images)
    labels = [b["text"] for b in blocks if b["type"] == "text"]
    assert labels[0].startswith("Picture 1: index.html on a phone")
    assert labels[1].startswith("Picture 2: index.html on a computer")
    assert SPEC["audience"] in labels[-1] and "Kemi Kitchen" in labels[-1]
    assert "The site's own measurements found no problems." in labels[-1]
    system = " ".join(call["system"].split())
    assert build.LOOKS_RULE in system and "may not see these pictures" in system
    assert "cannot see" not in system and "rely on you" not in system
    assert call["schema"]["properties"]["verdict"]["enum"] == ["looks_finished", "needs_work"]


def test_a_review_the_ai_cannot_give_still_saves_the_version(user_client, fake_ai, fake_visual):
    fake_ai.on("build.design_review", AIError("The AI declined this request."))
    project_id, r = build_v1(user_client, fake_ai)
    assert_page_ok(r)
    section = plain(look_section(r.text))
    assert "passed all 7 visual checks" in section
    assert "No AI looked at pictures of this version, because the AI could not answer: The AI declined" in section
    assert calls_for(fake_ai, "build.visual_repair") == []


def test_without_a_browser_the_look_is_not_checked_and_the_page_says_so(user_client, fake_ai, fake_visual):
    fake_visual.is_available = False
    project_id, r = build_v1(user_client, fake_ai)
    assert_page_ok(r)
    assert fake_visual.calls == []
    assert calls_for(fake_ai, "build.design_review") == [] and calls_for(fake_ai, "build.visual_repair") == []
    section = plain(look_section(r.text))
    assert build.LOOK_UNAVAILABLE in section
    assert "No AI looked at pictures of this version, because the site could not take any." in section
    assert "Ask a sighted person to check how it looks" in r.text
    assert "The site reads the files but does not run them." in r.text
    assert json.loads(saved_version(project_id)["visual_json"]) == {"available": False}
    # Nothing measured the look, so the page says so once, in plain words.
    not_checked = plain(re.search(r"<h2>Not checked</h2>(.*?)</ul>", r.text, re.S).group(1))
    assert not_checked.count(build.LOOK_NOT_MEASURED) == 1


def test_versions_made_before_the_look_was_checked_say_so(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    with db.session() as conn:
        conn.execute("UPDATE build_versions SET visual_json = '{}', design_review_json = '{}', repaired_json = '{}'")
    r = user_client.get(f"/build/{project_id}/v/1")
    assert_page_ok(r)
    assert build.LOOK_NOT_RUN in plain(look_section(r.text))
    assert "An AI looked at pictures" not in r.text


def test_the_look_is_checked_again_after_a_change(user_client, fake_ai, fake_visual):
    project_id, _ = build_v1(user_client, fake_ai)
    fake_visual.reports = [lambda files: visual_report(files, [contrast_problem()])]
    fake_ai.on("build.visual_repair", dict(changed_files(), summary="Darker text."))
    fake_ai.on("build.change", changed_files())
    r = post(user_client, f"/build/{project_id}/v/1/change", {"request": "Small chops now cost 1500."})
    assert_page_ok(r)
    assert h1(r) == "Kemi Kitchen: version 2 is ready"
    assert len(calls_for(fake_ai, "build.visual_repair")) == 1
    assert "Fixed before you saw it The AI fixed 1 problem the checks found" in plain(look_section(r.text))
    assert len(fake_visual.calls) == 3


def test_builder_facing_words_never_name_colours(user_client, fake_ai, fake_visual):
    sentences = [build.LOOK_NOT_RUN, build.LOOK_UNAVAILABLE, build.JS_NOT_CHECKED_WITH_LOOK, build.FEEL_LABEL,
                 build.FEEL_HINT, build._repair_sentence(2, 3, 1), build._repair_sentence(0, 1, 1),
                 build._repair_sentence(0, 0, 0)] + list(visual_check.CHECKS.values())
    for sentence in sentences:
        assert not visual_check.names_a_colour(sentence), sentence
    fake_visual.reports = [lambda files: visual_report(files, [contrast_problem()]),
                           lambda files: visual_report(files, [contrast_problem()])]
    fake_ai.on("build.visual_repair", repaired_files())
    _, r = build_v1(user_client, fake_ai)
    section = plain(look_section(r.text))
    assert section and not visual_check.names_a_colour(section), section
    not_checked = plain(re.search(r"<h2>Not checked</h2>(.*?)</ul>", r.text, re.S).group(1))
    assert not visual_check.names_a_colour(not_checked)


def cut_off_problem():
    return Problem(kind="cut_off", count=1, examples=['"Opening hours"'],
                   builder_text="Some text is cut off, so visitors cannot read all of it.",
                   fix_text="index.html at 390px wide (phone): a box cuts off text. Remove overflow: hidden.")


def with_pages_unmeasured(report, path, note="This page did not finish opening within 15 seconds."):
    """The report with `path` not measured at either size (as when it hangs, or the check stops first)."""
    for page in report.pages:
        if page.path == path:
            page.checked, page.passed, page.problems, page.note = False, [], [], note
    return report


def timed_out(files, path="index.html"):
    report = with_pages_unmeasured(visual_report(files), path)
    for page in report.pages:
        if page.path == path:
            page.problems = [Problem(kind=visual_check.TIMEOUT_KIND, count=1, examples=[],
                                     builder_text="This page did not finish opening within 15 seconds.",
                                     fix_text="index.html did not finish loading: an endless loop.")]
    return report


def test_a_repair_that_makes_a_page_unmeasurable_is_not_kept(user_client, fake_ai, fake_visual):
    # The repair adds a script that never stops: index.html cannot be measured again, which hides its problems.
    fake_visual.reports = [lambda files: visual_report(files, [contrast_problem(), cut_off_problem()]), timed_out]
    looping = repaired_files()
    looping["files"].append({"path": "app.js", "content": "while (true) {}"})
    fake_ai.on("build.visual_repair", looping)
    project_id, r = build_v1(user_client, fake_ai)
    assert_page_ok(r)
    assert "app.js" not in json.loads(saved_version(project_id)["files_json"])
    repaired = json.loads(saved_version(project_id)["repaired_json"])
    assert repaired["kept"] is False and repaired["fixed"] == 0
    section = plain(look_section(r.text))
    assert "could not measure every page of its new files, so this version keeps the files it made first" in section
    assert "An automatic repair was tried before you saw it" in section and "Fixed before you saw it" not in section
    assert "passed 5 of 7 visual checks. It found 2 problems:" in section


def test_a_repair_that_removes_a_page_is_not_kept(user_client, fake_ai, fake_visual):
    fake_visual.reports = [lambda files: visual_report(files, [contrast_problem()])]
    fewer = repaired_files()
    fewer["files"] = [f for f in fewer["files"] if f["path"] != "about.html"]
    fake_ai.on("build.visual_repair", fewer)
    project_id, r = build_v1(user_client, fake_ai)
    assert "about.html" in json.loads(saved_version(project_id)["files_json"])
    assert json.loads(saved_version(project_id)["repaired_json"])["kept"] is False


def test_problems_on_a_page_not_measured_again_are_not_counted_as_fixed(user_client, fake_ai, fake_visual):
    # index.html hung the first time; the second check ran out of time before it: nothing is known to be fixed.
    fake_visual.reports = [timed_out, lambda files: with_pages_unmeasured(
        visual_report(files), "index.html", "The time for checking ran out before this page, so it was not checked.")]
    fake_ai.on("build.visual_repair", repaired_files())
    project_id, r = build_v1(user_client, fake_ai)
    assert_page_ok(r)
    assert json.loads(saved_version(project_id)["repaired_json"])["fixed"] == 0
    section = plain(look_section(r.text))
    assert "Fixed before you saw it" not in section and "the AI fixed" not in section
    # A page that was not measured never reads as passing.
    assert "passed all" not in section and "of 7 visual checks." not in section.split("On the pages")[0]
    assert "but not every page could be measured" in section
    assert "Not fully checked: Text stands out clearly" in section
    not_checked = plain(re.search(r"<h2>Not checked</h2>(.*?)</ul>", r.text, re.S).group(1))
    assert "index.html on a phone" in not_checked and "index.html on a computer" in not_checked


def test_pages_the_check_never_reached_are_listed_and_its_error_is_shown(user_client, fake_ai, fake_visual):
    error = "The browser stopped part way through, so some pages were not checked."

    def cut_short(files):
        report = visual_report(files)
        report.pages = [p for p in report.pages if p.path == "index.html"]  # about.html never reached
        report.error = error
        return report

    fake_visual.reports = [cut_short]
    project_id, r = build_v1(user_client, fake_ai)
    assert_page_ok(r)
    section = plain(look_section(r.text))
    assert error in section and "passed all" not in section
    assert "Passed:" not in section and "Not fully checked: No text is cut off." in section
    not_checked = plain(re.search(r"<h2>Not checked</h2>(.*?)</ul>", r.text, re.S).group(1))
    assert "about.html on a phone" in not_checked and "about.html on a computer" in not_checked


@pytest.mark.parametrize("answer, why", [
    (AIError("The AI is busy right now (rate limit). Please try again in a minute."), "the AI could not finish"),
    ("worse", "were not better"),
])
def test_a_repair_that_fixed_nothing_is_never_headed_fixed(user_client, fake_ai, fake_visual, answer, why):
    fake_visual.reports = [lambda files: visual_report(files, [contrast_problem()]),
                           lambda files: visual_report(files, [contrast_problem(), cut_off_problem()])]
    fake_ai.on("build.visual_repair", repaired_files() if answer == "worse" else answer)
    _, r = build_v1(user_client, fake_ai)
    section = look_section(r.text)
    assert "<h3>An automatic repair was tried before you saw it</h3>" in section
    assert "Fixed before you saw it" not in section and why in plain(section)
    for sentence in (build._repair_sentence(1, 1, 0), build._repair_sentence(0, 0, 0)):
        assert "Fixed before you saw it" not in sentence and "improved" not in sentence
    assert build.repair_heading({"kept": True, "before": 1, "fixed": 0}) == "An automatic repair was tried before you saw it"
    assert build.repair_heading({"kept": True, "before": 0, "fixed": 0}) == "Changed before you saw it"
    assert build.repair_heading({"kept": True, "before": 1, "fixed": 1}) == "Fixed before you saw it"


# The withheld summary as plain() reads it: its last words, "Your profile", are a link.
WITHHELD_SHOWN = build.REVIEW_SUMMARY_WITHHELD.removesuffix(" Your profile.")
COLOURFUL = {"verdict": "needs_work",
             "summary_for_builder": "The dark blue header looks good but some text is pale grey.",
             "problems": [{"page": "index.html", "builder_text": "The light grey text is hard to read.",
                           "fix_text": "Set .note to #333333."},
                          {"page": "index.html", "builder_text": "The menu feels crowded on a phone.",
                           "fix_text": "Give .menu li 12px of padding."}]}


def test_the_ais_review_is_kept_as_written(user_client, fake_ai, fake_visual):
    review = build.normalize_review(COLOURFUL)
    assert review["summary"] == COLOURFUL["summary_for_builder"]
    assert [p["builder_text"] for p in review["problems"]] == ["The light grey text is hard to read.",
                                                                "The menu feels crowded on a phone."]
    assert review["problems"][0]["fix_text"] == "Set .note to #333333."  # the repair gets the details


def test_a_member_who_chose_the_effect_only_is_not_shown_colour_words(user_client, fake_ai, fake_visual):
    # The default profile choice: looks are described by their effect.
    fake_ai.on("build.design_review", COLOURFUL)
    fake_ai.on("build.visual_repair", repaired_files())
    _, r = build_v1(user_client, fake_ai)
    section = plain(look_section(r.text))
    assert WITHHELD_SHOWN in section
    assert build.REVIEW_PROBLEM_WITHHELD in section and "The menu feels crowded on a phone." in section
    assert not visual_check.names_a_colour(section), section
    # Sight is never given as the reason, and the member is told where to change the choice.
    assert "does not show" not in section and "cannot see" not in section
    assert f'<a href="{build.PROFILE_LOOKS_URL}">Your profile</a>.' in look_section(r.text)
    assert "Set .note to #333333." in calls_for(fake_ai, "build.visual_repair")[0]["prompt"]


def test_a_member_who_chose_colours_reads_the_ais_words_as_written(user_client, fake_ai, fake_visual):
    from .conftest import user_id
    with db.session() as conn:
        conn.execute("UPDATE users SET describe_colours = 1 WHERE id = ?", (user_id(),))
    fake_ai.on("build.design_review", COLOURFUL)
    fake_ai.on("build.visual_repair", repaired_files())
    project_id, r = build_v1(user_client, fake_ai)
    section = plain(look_section(r.text))
    assert COLOURFUL["summary_for_builder"] in section and "The light grey text is hard to read." in section
    assert WITHHELD_SHOWN not in section and build.REVIEW_PROBLEM_WITHHELD not in section
    # The choice is read when the page is shown: changing it back hides the colour words again.
    with db.session() as conn:
        conn.execute("UPDATE users SET describe_colours = 0 WHERE id = ?", (user_id(),))
    section = plain(look_section(user_client.get(f"/build/{project_id}/v/1").text))
    assert WITHHELD_SHOWN in section and "light grey" not in section


def test_older_withheld_words_read_as_the_new_ones(user_client, fake_ai, fake_visual):
    project_id, _ = build_v1(user_client, fake_ai)
    old = {"state": "done", "verdict": "needs_work", "withheld": 2,
           "summary": "The AI described the look by its colours, which this page does not show. Its opinion is below.",
           "problems": [{"page": "index.html", "fix_text": "x", "builder_text":
                         "The AI found something on this page that may be hard for visitors to see or read."}]}
    with db.session() as conn:
        conn.execute("UPDATE build_versions SET design_review_json = ?", (json.dumps(old),))
    section = plain(look_section(user_client.get(f"/build/{project_id}/v/1").text))
    assert "does not show" not in section and "see or read" not in section
    assert WITHHELD_SHOWN in section and build.REVIEW_PROBLEM_WITHHELD in section


def test_pictures_go_to_the_ai_in_their_own_format_and_only_as_many_as_it_reads(user_client, fake_ai, fake_visual):
    def jpeg_pictures(files):
        report = visual_report(files)
        for shot in report.screenshots:
            shot.png, shot.media_type = b"\xff\xd8\xff\xe0jpeg", "image/jpeg"
        return report

    fake_visual.reports = [jpeg_pictures]
    build_v1(user_client, fake_ai)
    images = [b for b in calls_for(fake_ai, "build.design_review")[0]["prompt"] if b["type"] == "image"]
    assert images and all(b["source"]["media_type"] == "image/jpeg" for b in images)
    assert all(kwargs.get("max_screenshots") == build.MAX_REVIEW_SHOTS for kwargs in fake_visual.kwargs)


# ----- how it should feel ------------------------------------------------------------------------

def test_the_feel_is_asked_for_on_the_form(user_client):
    r = user_client.get("/build")
    assert build.FEEL_LABEL in r.text and build.FEEL_HINT in r.text
    assert 'name="feel"' in r.text and "(optional)" in re.search(r'for="field-feel">(.*?)</label>', r.text, re.S).group(1)


def test_the_feel_is_validated(user_client, fake_ai):
    r = post(user_client, "/build/new", {"kind": "website", "idea": "A menu.", "feel": "x" * 201})
    assert r.status_code == 400
    assert h1(r) == "Please check 1 answer: Build a website, app, tool or game"
    assert 'href="#field-feel"' in r.text and "201 characters" in r.text
    r = post(user_client, "/build/new", {"kind": "website", "idea": "A menu.", "feel": "my password is hunter2"})
    assert r.status_code == 400 and 'href="#field-feel"' in r.text and "never needs them" in r.text
    assert fake_ai.calls == []
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM build_projects").fetchone()[0] == 0


def test_the_feel_is_stored_and_sent_to_the_ai(user_client, fake_ai):
    fake_ai.on("build.understand", dict(SPEC, look_and_feel="Warm and welcoming, like a family food stall."))
    r = post(user_client, "/build/new", {"title": "Kemi Kitchen", "kind": "website", "idea": "A menu.",
                                         "feel": "Friendly and calm"})
    assert_page_ok(r)
    project_id = int(re.search(r"/build/(\d+)$", str(r.url)).group(1))
    with db.session() as conn:
        assert conn.execute("SELECT feel FROM build_projects").fetchone()[0] == "Friendly and calm"
    assert "<feel>Friendly and calm</feel>" in calls_for(fake_ai, "build.understand")[0]["prompt"]
    # What the AI understood: how it will feel, under its own heading; the person's words shown back.
    assert r.text.index("Who it is for") < r.text.index("<h2>How it will feel to visitors</h2>") < r.text.index(
        "Pages (")
    assert "Warm and welcoming, like a family food stall." in r.text
    assert "How it should feel, in your words: Friendly and calm" in r.text
    fake_ai.on("build.generate", GENERATED)
    post(user_client, f"/build/{project_id}/understand", {"action": "build"})
    assert "<feel>Friendly and calm</feel>" in calls_for(fake_ai, "build.generate")[0]["prompt"]
    assert "Warm and welcoming" in calls_for(fake_ai, "build.generate")[0]["prompt"]
    assert "<feel>Friendly and calm</feel>" in calls_for(fake_ai, "build.design_review")[0]["prompt"][-1]["text"]
    fake_ai.on("build.change", changed_files())
    post(user_client, f"/build/{project_id}/v/1/change", {"request": "Small chops now cost 1500."})
    assert "<feel>Friendly and calm</feel>" in calls_for(fake_ai, "build.change")[0]["prompt"]


def test_without_a_feel_the_ai_chooses(user_client, fake_ai):
    start_project(user_client, fake_ai)
    assert "the person did not say" in calls_for(fake_ai, "build.understand")[0]["prompt"]


def test_look_and_feel_missing_from_the_ai_is_plain(user_client, fake_ai):
    _, r = start_project(user_client, fake_ai)
    assert_page_ok(r)
    assert re.search(r"<h2>How it will feel to visitors</h2>\s*<p>The AI did not say.</p>", r.text)


def test_the_prompts_hold_the_design_rules(user_client, fake_ai):
    assert "look_and_feel" in build.UNDERSTAND_SYSTEM and build.LOOKS_RULE in build.UNDERSTAND_SYSTEM
    for system in (build.UNDERSTAND_SYSTEM, build.GENERATE_SYSTEM, build.CHANGE_SYSTEM, build.DESIGN_REVIEW_SYSTEM):
        # The shared member note says who the person is; no prompt says they cannot see.
        assert "cannot see" not in system and "uses a screen reader" not in system
    assert build.UNDERSTAND_SCHEMA["properties"]["look_and_feel"] == {"type": "string"}
    assert "look_and_feel" in build.UNDERSTAND_SCHEMA["required"]
    for system in (build.GENERATE_SYSTEM, build.CHANGE_SYSTEM):
        for rule in ("polished, modern visual look", "WCAG AA contrast", "base font size 16 to 18px",
                     "System font", "320px phones", "visual hierarchy", "at least 44px tall", "focus outlines",
                     "separate style.css", "Follow the member note on describing looks", build.OUTWARD_DISCLOSURE,
                     "Never use sound alone", "screen reader in speech or braille, a keyboard, or zoom"):
            assert rule in system, rule
        # Every accessibility rule is still there.
        assert "exactly one <h1>" in system and 'role="status"' in system and "No external scripts" in system
    project_id, _ = build_v1(user_client, fake_ai)
    assert "WCAG AA contrast" in calls_for(fake_ai, "build.generate")[0]["system"]


def test_seed_adds_the_new_columns_to_an_older_database(tmp_path):
    import sqlite3

    from studio.app import create_app
    from studio.config import Settings
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE build_projects (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL, idea TEXT NOT NULL, status TEXT NOT NULL, spec_json TEXT,
            share_token TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE build_versions (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, number INTEGER NOT NULL,
            files_json TEXT NOT NULL, request_text TEXT NOT NULL DEFAULT '', ai_summary TEXT NOT NULL DEFAULT '',
            how_to_use TEXT NOT NULL DEFAULT '', diff_json TEXT NOT NULL DEFAULT '{}',
            checks_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, UNIQUE (project_id, number));
    """)
    conn.close()
    create_app(Settings(secret_key="k", db_path=path, jobs_inline=True, base_url="http://testserver"))
    with db.session() as conn:
        projects = {r["name"] for r in conn.execute("PRAGMA table_info(build_projects)")}
        versions = {r["name"] for r in conn.execute("PRAGMA table_info(build_versions)")}
    assert "feel" in projects
    assert {"visual_json", "design_review_json", "repaired_json"} <= versions


# ----- ease of use for screen reader, braille, magnifier and keyboard users ------------------------------

def checkbox_tag(text, name):
    return re.search(rf'<input type="checkbox"[^>]*name="{name}"[^>]*>', text).group()


def test_ticked_extras_are_kept_after_an_error_a_new_read_back_and_a_failed_build(user_client, fake_ai):
    project_id, _ = start_project(user_client, fake_ai)
    # An error: the box comes back with the tick still there.
    r = post(user_client, f"/build/{project_id}/understand", {"action": "update", "corrections": "", "add_1": "yes"})
    assert r.status_code == 400
    assert "checked" in checkbox_tag(r.text, "add_1") and "checked" not in checkbox_tag(r.text, "add_0")
    assert "Your ticks are kept either way." in r.text
    # A new read-back keeps the tick.
    r = post(user_client, f"/build/{project_id}/understand",
             {"action": "update", "corrections": "Prices in naira.", "add_1": "yes"})
    assert_page_ok(r)
    assert "checked" in checkbox_tag(r.text, "add_1") and "checked" not in checkbox_tag(r.text, "add_0")
    # A build that fails: going back finds the tick still there.
    fake_ai.on("build.generate", AIError("The AI is busy right now. Please try again in a minute."))
    r = post(user_client, f"/build/{project_id}/understand", {"action": "build", "add_1": "yes"})
    assert "This did not finish" in r.text and f'href="/build/{project_id}"' in r.text  # Go back and try again
    r = user_client.get(f"/build/{project_id}")
    assert "checked" in checkbox_tag(r.text, "add_1")


def test_while_the_ai_works_the_read_back_shows_a_status_not_a_form(user_client, fake_ai):
    project_id, _ = start_project(user_client, fake_ai)
    _set_row(project_id, status="understanding")
    _job_for(project_id)
    r = user_client.get(f"/build/{project_id}")
    assert_page_ok(r)
    assert h1(r) == "Kemi Kitchen: the AI is reading your corrections"
    assert 'name="corrections"' not in r.text and "This is right: build it" not in r.text
    assert re.search(r'href="/jobs/[0-9a-f]+">the page for this work</a>', r.text)
    # A form sent from an older copy of the page keeps the member's words on the page.
    r = post(user_client, f"/build/{project_id}/understand", {"action": "update", "corrections": "Ten dishes."})
    assert r.status_code == 409
    assert_page_ok_status(r)
    assert h1(r) == "Kemi Kitchen: the AI is already working on it, so your words were not sent"
    assert "Your words, not sent" in r.text
    # In a read-only box with a Copy button (enhance.js), not loose paragraphs.
    assert re.search(r'<textarea id="unsent-words"[^>]*readonly data-copy-name="your words">Ten dishes\.</textarea>',
                     r.text)
    assert len(calls_for(fake_ai, "build.understand")) == 1


def test_while_the_ai_reads_the_idea_the_waiting_page_shows_a_status(no_ai_app):
    client = TestClient(no_ai_app)
    register(client)
    post(client, "/build/new", {"title": "Rivers quiz", "kind": "game", "idea": "A quiz about rivers."})
    _set_row(1, status="understanding")
    _job_for(1)
    r = client.get("/build/1")
    assert_page_ok(r)
    assert h1(r) == "Rivers quiz: the AI is reading your idea"
    assert 'name="corrections"' not in r.text and "the page for this work" in r.text


def test_peer_check_links_name_who_should_check(user_client, fake_ai):
    project_id, r = build_v1(user_client, fake_ai)
    by_name = {name: href for href, name in re.findall(r'<a href="(/checks/new\?[^"]+)">([^<]+)</a>', r.text)}
    screen_reader = by_name["Ask members who use screen readers to check it"]
    sighted = by_name["Ask a sighted person to check how it looks"]
    assert "kind=screen_reader" in screen_reader and "Kemi+Kitchen%2C+version+1%3A+screen+reader+check" in screen_reader
    assert "kind=sighted" in sighted and "Kemi+Kitchen%2C+version+1%3A+how+it+looks" in sighted
    form = user_client.get(screen_reader.replace("&amp;", "&"))
    assert "Kemi Kitchen, version 1: screen reader check" in form.text


def ask_for_a_check(client, project_id, number=1, reward="0", wanted="screen_reader", title="Kemi check"):
    url = f"http://testserver/build/p/{share_token(project_id)}/v/{number}/index.html"
    r = post(client, "/checks/new", {"title": title, "url": url, "what_to_try": "Find the price of small chops.",
                                     "max_checks": "2", "reward_cents": reward, "wanted": wanted})
    assert r.status_code == 200, r.text
    return int(re.search(r"/checks/(\d+)$", str(r.url)).group(1))


def test_the_version_page_lists_its_peer_checks(user_client, fake_ai):
    project_id, r = build_v1(user_client, fake_ai)
    assert "<h2" in r.text and "Peer checks of this version</h2>" in r.text
    assert "You have not asked for a peer check of this version yet." in r.text
    check_id = ask_for_a_check(user_client, project_id)
    r = user_client.get(f"/build/{project_id}/v/1")
    assert_page_ok(r)
    assert f'<a href="/checks/{check_id}">Kemi check</a>' in r.text
    assert "For people who use screen readers. 0 of 2 checks done. Open." in plain(r.text)
    # A check of another version is not listed here.
    fake_ai.on("build.change", changed_files())
    post(user_client, f"/build/{project_id}/v/1/change", {"request": "Small chops now cost 1500."})
    assert f'href="/checks/{check_id}"' not in user_client.get(f"/build/{project_id}/v/2").text


def test_deleting_a_project_closes_its_peer_checks_and_returns_held_credits(user_client, fake_ai):
    from studio import credits

    from .conftest import user_id
    project_id, _ = build_v1(user_client, fake_ai)
    credits.add(user_id(), 500, "Test credits")
    check_id = ask_for_a_check(user_client, project_id, reward="100")
    assert credits.balance(user_id()) == 300
    r = user_client.get(f"/build/{project_id}/delete")
    assert_page_ok(r)
    assert "1 peer check request is still open for its previews. Deleting closes it, and the $2.00 held for " \
           "checks not done comes back to your credits." in plain(r.text)
    assert f'href="/checks/{check_id}"' in r.text
    assert f'href="/build/{project_id}/v/1/download.zip">Download version 1 (the latest) as a zip file</a>' in r.text
    r = post(user_client, f"/build/{project_id}/delete")
    assert_page_ok(r)
    assert h1(r) == "Deleted Kemi Kitchen and all its versions, and closed 1 peer check request"
    assert "$2.00 held for checks not done came back to your credits." in r.text
    assert credits.balance(user_id()) == 500
    with db.session() as conn:
        assert conn.execute("SELECT status FROM check_requests WHERE id = ?", (check_id,)).fetchone()[0] == "closed"


def test_deleting_a_project_with_nothing_built_says_so(user_client, fake_ai):
    project_id, _ = start_project(user_client, fake_ai)
    r = user_client.get(f"/build/{project_id}/delete")
    assert_page_ok(r)
    assert "This deletes Kemi Kitchen and your idea. Nothing has been built yet. This cannot be undone." in r.text
    assert "0 versions" not in r.text and "download.zip" not in r.text


def test_form_addresses_reached_by_reload_lead_back_to_the_form(user_client, fake_ai):
    r = user_client.get("/build/new", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/build#field-idea"
    project_id, _ = build_v1(user_client, fake_ai)
    for path, target in [(f"/build/{project_id}/understand", f"/build/{project_id}"),
                         (f"/build/{project_id}/v/1/change", f"/build/{project_id}/v/1#field-request"),
                         (f"/build/{project_id}/v/1/fix", f"/build/{project_id}/v/1#fix")]:
        r = user_client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == target, path


def test_the_fix_has_its_own_heading_after_the_checks(user_client, fake_ai):
    project_id, r = build_v1(user_client, fake_ai)
    text = r.text
    heading = '<h2 id="fix" tabindex="-1">Fix the problems the checks found (1)</h2>'
    assert heading in text
    assert text.index("<h2>Checked by the site</h2>") < text.index(heading) < text.index("<h2>What the AI says it built")
    assert 'in a new version: <a href="#fix">go to the fix</a>.' in text  # named in the opening summary


def test_check_places_link_to_that_line_of_the_file(user_client, fake_ai):
    project_id, _ = start_project(user_client, fake_ai)
    files = json.loads(json.dumps(FILES_V1))
    files[0]["content"] = INDEX_V1.replace("<p>Jollof rice: 1500 naira</p>",
                                           '<p>Jollof rice: 1500 naira</p>\n<img src="rice.svg">')
    fake_ai.on("build.generate", dict(GENERATED, files=files))
    r = post(user_client, f"/build/{project_id}/understand", {"action": "build"})
    assert ('Where: &lt;img src=&#34;rice.svg&#34;&gt; on <a href="/build/1/v/1/files/index.html#L9">line 9 of '
            'index.html</a>') in r.text
    r = user_client.get(f"/build/{project_id}/v/1/files/index.html")
    assert_page_ok(r)
    assert '<li id="L9"><code>&lt;img src=&#34;rice.svg&#34;&gt;</code></li>' in r.text
    r = user_client.get(f"/build/{project_id}/v/1/files/about.html")
    # Moving between the files without going back to the list.
    assert f'<a href="/build/{project_id}/v/1/files/index.html">Next file: index.html</a>' in r.text
    assert "Previous file" not in r.text
    r = user_client.get(f"/build/{project_id}/v/1/files/index.html")
    assert f'<a href="/build/{project_id}/v/1/files/about.html">Previous file: about.html</a>' in r.text
    assert f'<a href="/build/{project_id}/v/1/files/style.css">Next file: style.css</a>' in r.text


def test_indentation_survives_in_the_numbered_lines():
    assert build._numbered_lines("a\n  b\n\tc") == ["a", "  b", " " * 4 + "c"]


@pytest.mark.parametrize("old, new, expected", [
    ("<p>Small chops: 1,200 naira</p>", "<p>Small chops: 1,500 naira</p>", ("1,200", "1,500")),
    ("<p>We are open every day of the week</p>", "<p>We are open every day of the year</p>", ("week", "year")),
    ("<p>Open daily</p>", "<p>Open daily from nine</p>", ("", "from nine")),
    ("<p>Open</p>", "<p>Open from nine until late at night</p>", None),  # too little of the line stayed the same
    # Markup next to a changed word is not part of the change.
    ('    <p class="price">1,200 per month</p>', '    <p class="price">1,500 per month</p>', ("1,200", "1,500")),
    ("<li>Jollof rice</li>", "<li>Fried rice and plantain</li>", None),  # most of the line changed
    ("same", "same", None),
])
def test_word_change(old, new, expected):
    assert build.word_change(old, new) == expected


def test_what_changed_links_to_the_changed_lines(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    fake_ai.on("build.change", changed_files())
    r = post(user_client, f"/build/{project_id}/v/1/change", {"request": "Small chops now cost 1500."})
    section = re.search(r"<h2>What changed.*?<h2>", r.text, re.S).group()
    assert f'<a href="/build/{project_id}/v/2/changes">See the changed lines</a>' in section


def test_an_older_version_says_so_in_its_title(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    fake_ai.on("build.change", changed_files())
    post(user_client, f"/build/{project_id}/v/1/change", {"request": "Small chops now cost 1500."})
    r = user_client.get(f"/build/{project_id}/v/1")
    assert h1(r) == "Kemi Kitchen: version 1 (older; the latest is version 2)"
    assert f"<title>{h1(r)}, Blind Builders</title>" in r.text


def test_returning_members_find_their_projects_first(user_client, fake_ai):
    r = user_client.get("/build")
    assert r.text.index("Start something new") < r.text.index("Your projects")
    build_v1(user_client, fake_ai)
    r = user_client.get("/build")
    assert r.text.index("Your projects (1)") < r.text.index("Start something new")


def test_job_titles_make_sense_in_the_list_of_work(user_client, fake_ai):
    start_project(user_client, fake_ai)
    with db.session() as conn:
        titles = [row[0] for row in conn.execute("SELECT title FROM jobs")]
    assert titles == ["Reading back your idea: Kemi Kitchen"]


def test_the_corrections_hint_says_build_goes_straight_ahead(user_client, fake_ai):
    _, r = start_project(user_client, fake_ai)
    # The button's name has a colon, so it is quoted where it sits inside a sentence.
    assert ("choose the button &#34;This is right: build it&#34;, the AI builds with your words at once."
            in r.text)


# ----- final walk-through ---------------------------------------------------------------------

def test_ticks_come_back_after_being_signed_out_and_the_button_is_not_offered_to_copy(user_client, fake_ai):
    project_id, _ = start_project(user_client, fake_ai)
    from .conftest import csrf
    token = csrf(user_client)
    with db.session() as conn:  # signed out on every device, for example in another tab
        conn.execute("UPDATE users SET session_epoch = session_epoch + 1")
    r = user_client.post(f"/build/{project_id}/understand",
                         data={"action": "update", "corrections": "Make the heading bigger.", "add_1": "yes",
                               "csrf_token": token},
                         headers={"referer": f"http://testserver/build/{project_id}"}, follow_redirects=False)
    assert r.status_code == 303
    r = post(user_client, "/login", {"email": "amina@example.com", "password": "correct horse battery",
                                     "next": f"/build/{project_id}"})
    assert_page_ok(r)
    assert "Your words are back in the form." in r.text
    assert "Your words from before" not in r.text  # nothing left over to copy: no button value, no tick value
    assert "checked" in checkbox_tag(r.text, "add_1") and "checked" not in checkbox_tag(r.text, "add_0")
    assert "Make the heading bigger.</textarea>" in r.text


def test_a_new_read_back_says_it_is_new(user_client, fake_ai):
    project_id, r = start_project(user_client, fake_ai)
    assert "This is the AI's new read-back" not in r.text
    fake_ai.on("build.understand", SPEC_2)
    r = post(user_client, f"/build/{project_id}/understand", {"action": "update", "corrections": "Prices are in naira."})
    assert h1(r) == "Kemi Kitchen: here is what I understood now"
    assert ("This is the AI's new read-back. It has read your correction, listed near the end of this page "
            "under What you added or corrected.") in r.text


def test_a_change_that_does_not_finish_puts_the_request_back(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    fake_ai.on("build.change", AIError("The AI service is busy right now, so nothing was changed."))
    r = post(user_client, f"/build/{project_id}/v/1/change", {"request": "Add the Saturday run time."})
    assert "This did not finish" in r.text
    r = user_client.get(f"/build/{project_id}/v/1")
    assert_page_ok(r)
    assert "The AI could not finish your change, so nothing was changed." in r.text
    assert "Add the Saturday run time.</textarea>" in r.text
    # Sending a change replaces the kept words; a change that works leaves the box empty again.
    fake_ai.on("build.change", changed_files())
    post(user_client, f"/build/{project_id}/v/1/change", {"request": "Small chops now cost 1500."})
    r = user_client.get(f"/build/{project_id}/v/1")
    assert "The AI could not finish your change" not in r.text and "Add the Saturday run time." not in r.text


def test_the_first_sentence_counts_the_look_problems_too(user_client, fake_ai, fake_visual):
    fake_visual.reports.append(visual_report(problems=[contrast_problem()]))
    fake_ai.on("build.visual_repair", GENERATED)
    fake_visual.reports.append(visual_report(problems=[contrast_problem()]))
    project_id, r = build_v1(user_client, fake_ai)
    lead = re.search(r'<p class="lead">(.*?)</p>', r.text, re.S).group(1)
    assert ("found 1 problem to fix, and the check of how it looks on a phone and a computer found 1 more problem "
            "to fix. The AI can try to fix the 2 problems") in lead


def test_contrast_passes_only_for_the_text_the_site_could_measure(user_client, fake_ai, fake_visual):
    def report(files):
        out = visual_report(files)
        out.pages[0].unknown_contrast = 12
        return out
    fake_visual.reports.append(report)
    project_id, r = build_v1(user_client, fake_ai)
    assert "passed all 7 visual checks, on what it could measure." in r.text
    assert "Passed: Text stands out clearly" not in r.text
    assert ("For the text the site could measure: it stands out clearly from what is behind it, so it is easy to "
            "read. 12 more pieces of text could not be measured; see Not checked.") in r.text
    assert "Whether 12 pieces of text are easy to read." in r.text


def test_not_checked_says_what_peer_checkers_have_checked(user_client, fake_ai):
    project_id, r = build_v1(user_client, fake_ai)
    assert "Nobody has checked these yet:" in r.text
    url = "https://testserver" + build._preview_path({"share_token": share_token(project_id)}, 1)
    with db.session() as conn:
        asker = conn.execute("SELECT id FROM users").fetchone()[0]
        conn.execute("INSERT INTO users (email, display_name, password_hash, pseudonym, created_at)"
                     " VALUES ('c@example.com', 'C', 'x', 'Member C1', '2026-01-01')")
        checker = conn.execute("SELECT id FROM users WHERE email = 'c@example.com'").fetchone()[0]
        cur = conn.execute("INSERT INTO check_requests (requester_id, title, url, what_to_try, max_checks, status,"
                           " created_at, wanted) VALUES (?, 'Kemi', ?, 'Order', 3, 'open', '2026-01-01', 'both')",
                           (asker, url))
        conn.execute("INSERT INTO check_reports (request_id, checker_id, screen_reader, browser, device,"
                     " heard_first, task_result, record_hash, created_at) VALUES (?, ?, 'JAWS', 'Edge', 'PC',"
                     " 'Menu', 'could_not', 'h', '2026-01-01')", (cur.lastrowid, checker))
        request_id = cur.lastrowid
    r = user_client.get(f"/build/{project_id}/v/1")
    section = re.search(r"<h2>Not checked</h2>.*?<h2", r.text, re.S).group()
    assert "Nobody has checked these yet" not in section
    assert "How a real screen reader reads the page: the site cannot check this, but 1 member has tried it" in section
    assert "How it looks to a sighted person." in section  # no sighted check yet
    # The request links straight to its record once a check has come in.
    assert f'<a href="/checks/{request_id}/record">Kemi: the check record</a>.' in r.text


def test_an_older_version_says_it_is_older_in_its_heading_and_at_the_form_only(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    fake_ai.on("build.change", changed_files())
    post(user_client, f"/build/{project_id}/v/1/change", {"request": "Small chops now cost 1500."})
    r = user_client.get(f"/build/{project_id}/v/1")
    assert "This is not the latest version." not in r.text
    assert "<strong>This is an older version.</strong>" in r.text
    # Link names end before the next words begin.
    assert "Open the preview of version 1</a>. <span" in r.text
    r = user_client.get(f"/build/{project_id}")
    assert re.search(r'>Version 2</a>:\s*<span class="meta">', r.text)
