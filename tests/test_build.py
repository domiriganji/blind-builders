"""Build: describe it, check what the AI understood, build, check, preview, download, change, delete."""
import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from studio import db
from studio.a11y_check import check_html
from studio.ai import AIError
from studio.features import build

from .conftest import post, register

PREVIEW_CSP = ("sandbox allow-scripts allow-forms allow-popups allow-modals; "
               "default-src 'self' 'unsafe-inline' data:; frame-ancestors 'none'")

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
<head><meta charset="utf-8"><title>Kemi Kitchen</title><link rel="stylesheet" href="style.css"></head>
<body><main>
<h1>Kemi Kitchen menu</h1>
<p>Small chops: 1200 naira</p>
<p>Jollof rice: 1500 naira</p>
</main></body>
</html>
"""
ABOUT_BAD = """<!doctype html>
<html><head><title>About</title></head>
<body><main><h1>About Kemi</h1><p>We cook every day.</p></main></body></html>
"""
FILES_V1 = [
    {"path": "index.html", "content": INDEX_V1},
    {"path": "about.html", "content": ABOUT_BAD},
    {"path": "style.css", "content": "body { font-size: 1.2rem; }\n"},
]
GENERATED = {"files": FILES_V1, "summary": "I built a **menu** page and an about page.",
             "how_to_use": "Open index.html.", "not_done": ["I guessed the opening hours."]}


def changed_files():
    files = json.loads(json.dumps(FILES_V1))
    files[0]["content"] = INDEX_V1.replace("Small chops: 1200 naira", "Small chops: 1500 naira")
    return {"files": files, "summary": "I changed the price of small chops.", "how_to_use": "Open index.html.",
            "not_done": []}


def assert_page_ok(r):
    assert r.status_code == 200, r.text
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
    assert "Update what you understood" in r.text and "This is right: build it" in r.text
    understand_call = calls_for(fake_ai, "build.understand")[0]
    assert "A menu for my food stall." in understand_call["prompt"]
    assert "blind" in understand_call["system"]

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
    assert "How a real screen reader speaks the page" in text
    assert "I guessed the opening hours." in text
    assert "style.css" in text  # listed as not checked
    assert "What changed" not in text
    assert "Fix the problems the checks found" in text
    token = share_token(project_id)
    assert f'href="/build/p/{token}/v/1/index.html"' in text
    assert "/checks/new?url=http%3A%2F%2Ftestserver%2Fbuild%2Fp%2F" in text
    assert f"/build/{project_id}/v/1/download.zip" in text

    # The project page now shows the latest version and the list of versions.
    r = user_client.get(f"/build/{project_id}")
    assert_page_ok(r)
    assert "Version 1" in r.text and "Kemi Kitchen: your project and its versions" == h1(r)

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
    assert "<pre>" in r.text and "Small chops: 1200 naira" in r.text
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
    assert "cannot accept" in r.text and "../x" in r.text
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
    assert h1(r) == "Build a website, app, tool or game: please fix the problems"
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
    assert r.status_code == 400 and "never needs them" in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM build_projects").fetchone()[0] == 0


def test_ordinary_numbers_are_not_refused():
    assert not build.looks_like_secret("Small chops cost 1200 naira. Call 0803 123 4567.")
    assert not build.looks_like_secret("A login page with a password field")


def test_update_needs_corrections(user_client, fake_ai):
    project_id, _ = start_project(user_client, fake_ai)
    r = post(user_client, f"/build/{project_id}/understand", {"action": "update", "corrections": ""})
    assert r.status_code == 400
    assert h1(r).endswith(": please fix the problems") and 'href="#field-corrections"' in r.text
    assert len(calls_for(fake_ai, "build.understand")) == 1


def test_change_needs_a_request(user_client, fake_ai):
    project_id, _ = build_v1(user_client, fake_ai)
    r = post(user_client, f"/build/{project_id}/v/1/change", {"request": "  "})
    assert r.status_code == 400
    assert h1(r) == "Kemi Kitchen: version 1 is ready: please fix the problems"
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
    assert "This did not finish" in r.text and "not in the expected form" in r.text
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
    fake_ai.on("build.change", changed_files())
    r = post(user_client, f"/build/{project_id}/v/1/change", {"request": "Small chops now cost 1500."})
    assert "already working on this project" in r.text
    assert h1(r) == "Kemi Kitchen: the AI is making a change"
    assert_page_ok(r)
    r = post(user_client, f"/build/{project_id}/v/1/fix")
    assert "already working on this project" in r.text
    assert calls_for(fake_ai, "build.change") == []
    # A job that never finished stops blocking after a while, and the list does not claim it is working.
    _set_row(project_id, status="changing", updated_at=_minutes_ago(build.BUSY_MINUTES + 1))
    assert "The AI stopped before it finished" in user_client.get("/build").text
    r = post(user_client, f"/build/{project_id}/v/1/change", {"request": "Small chops now cost 1500."})
    assert str(r.url).endswith(f"/build/{project_id}/v/2")


def test_second_build_while_building_is_refused(user_client, fake_ai):
    project_id, _ = start_project(user_client, fake_ai)
    _set_row(project_id, status="building", updated_at=_minutes_ago(0))
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
    assert r.status_code == 400 and "Start a new project" in r.text
    assert len(calls_for(fake_ai, "build.understand")) == 2


def test_waiting_page_errors_say_please_fix(no_ai_app):
    client = TestClient(no_ai_app)
    register(client)
    post(client, "/build/new", {"title": "Rivers quiz", "kind": "game", "idea": "A quiz about rivers."})
    r = post(client, "/build/1/understand", {"action": "update", "corrections": "x" * (build.MAX_REQUEST + 1)})
    assert r.status_code == 400
    assert h1(r) == "Rivers quiz: the AI has not read your idea yet: please fix the problems"
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
