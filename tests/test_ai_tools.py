"""AI tools report: seed data, the public table, tool pages, member reports and the JSON endpoint."""
import json
import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from studio import db
from studio.a11y_check import check_html
from studio.features import ai_tools

from .conftest import post, register, user_id

GOOD_REPORT = {
    "tool_version": "2.1.296",
    "screen_reader": "NVDA",
    "screen_reader_version": "2026.2",
    "os": "Windows 11",
    "turn_end": "yes",
    "output_readable": "yes",
    "changes_reviewable": "no",
    "menus_spoken": "yes",
    "approvals_readable": "yes",
    "typing_echoed": "not_tested",
    "overall": "usable_with_workarounds",
    "workarounds": "Turn on the bell and use **NVDA+6** to review.",
    "note": "Works well in Windows Terminal.",
}


def report(client, slug="claude-code", **changes):
    data = dict(GOOD_REPORT, **changes)
    return post(client, f"/ai-tools/{slug}/reports", data, follow_redirects=False)


def tool_id(slug):
    with db.session() as conn:
        return conn.execute("SELECT id FROM ai_tools WHERE slug = ?", (slug,)).fetchone()["id"]


def insert_report(slug, uid, overall, days_ago, version="1.0"):
    created = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(microsecond=0).isoformat()
    with db.session() as conn:
        conn.execute(
            "INSERT INTO ai_tool_reports (tool_id, user_id, tool_version, screen_reader, os, turn_end, output_readable,"
            " changes_reviewable, menus_spoken, approvals_readable, typing_echoed, overall, created_at)"
            " VALUES (?, ?, ?, 'JAWS', 'Windows 11', 'yes', 'yes', 'yes', 'yes', 'yes', 'yes', ?, ?)",
            (tool_id(slug), uid, version, overall, created))


def table_row(page_text, name):
    for row in re.findall(r"<tr>(.*?)</tr>", page_text, re.S):
        if f">{name}</a>" in row:
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row)).strip()
    raise AssertionError(f"no table row for {name}")


# ----- seed -----------------------------------------------------------------------------------------

def test_seed_loads_every_required_tool(app):
    with db.session() as conn:
        slugs = {r["slug"] for r in conn.execute("SELECT slug FROM ai_tools")}
    for slug in ["claude-code", "claude-desktop", "codex-cli", "codex-desktop", "copilot-cli", "vs-code-copilot",
                 "cursor", "gemini-cli", "antigravity-cli", "devin-desktop", "lovable"]:
        assert slug in slugs


def test_seed_is_idempotent_and_updates_notes(app, tmp_path):
    with db.session() as conn:
        before = conn.execute("SELECT COUNT(*) AS n FROM ai_tools").fetchone()["n"]
    assert before == len(ai_tools.load_seed())
    ai_tools.seed()
    ai_tools.seed()
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM ai_tools").fetchone()["n"] == before
    # A changed note in the file updates the row in place, keeping its id (so reports stay attached).
    old_id = tool_id("cursor")
    data = json.loads(ai_tools.SEED_FILE.read_text())
    for t in data["tools"]:
        if t["slug"] == "cursor":
            t["research_note"] = "A newer note. It replaces the old one."
            t["sources"] = [{"title": "Cursor release notes", "url": "https://cursor.com/docs/release-notes"}]
    changed = tmp_path / "seed.json"
    changed.write_text(json.dumps(data))
    ai_tools.seed(changed)
    with db.session() as conn:
        row = conn.execute("SELECT id, research_note, research_sources_json FROM ai_tools WHERE slug = 'cursor'").fetchone()
        assert conn.execute("SELECT COUNT(*) AS n FROM ai_tools").fetchone()["n"] == before
    assert row["id"] == old_id and row["research_note"] == "A newer note. It replaces the old one."
    assert len(json.loads(row["research_sources_json"])) == 1


def test_every_seeded_source_is_http_and_every_note_is_short():
    tools = ai_tools.load_seed()
    assert len(tools) >= 11
    for t in tools:
        assert t["kind"] in ai_tools.KINDS
        assert t["sources"], t["slug"]
        for s in t["sources"]:
            assert re.match(r"^https?://", s["url"]), s["url"]
        if t["url"]:
            assert re.match(r"^https?://", t["url"])
        sentences = [s for s in re.split(r"(?<=[.!?\"])\s+(?=[A-Z])", t["research_note"]) if s]
        assert 2 <= len(sentences) <= 4, (t["slug"], len(sentences))


def test_seed_rejects_non_web_sources(tmp_path):
    bad = {"tools": [{"slug": "x", "name": "X", "kind": "terminal", "url": "", "research_note": "A. B.",
                      "sources": [{"title": "t", "url": "javascript:alert(1)"}]}]}
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    try:
        ai_tools.load_seed(path)
    except ValueError:
        return
    raise AssertionError("a javascript: source was accepted")


# ----- public pages -------------------------------------------------------------------------------

def test_index_lists_tools_in_a_table(client):
    page = client.get("/ai-tools")
    assert page.status_code == 200
    assert "<h1>AI tools report</h1>" in page.text
    assert "<title>AI tools report" in page.text
    assert '<th scope="col">Tool and kind</th>' in page.text
    assert '<th scope="row"><a href="/ai-tools/cursor">Cursor</a>' in page.text
    assert "Kind: editor" in table_row(page.text, "Cursor")
    assert 'href="/ai-tools/claude-code"' in page.text
    assert "No reports yet" in table_row(page.text, "Cursor")
    for _key, _q, statement, _hint in ai_tools.CHECKS:
        assert statement in page.text
    assert not check_html(page.text).errors


def test_tool_page_shows_dated_research_and_sources(client):
    page = client.get("/ai-tools/claude-code")
    assert page.status_code == 200
    assert "What the research found (October 2026)" in page.text
    assert "--ax-screen-reader" in page.text
    assert 'href="https://code.claude.com/docs/en/accessibility" rel="noopener noreferrer"' in page.text
    assert "No reports yet" in page.text
    assert "sign in" in page.text and "/login?next=/ai-tools/claude-code" in page.text
    assert 'name="tool_version"' not in page.text  # signed out: no form
    assert len(re.findall(r"<h1[ >]", page.text)) == 1
    assert not check_html(page.text).errors


def test_unknown_slug_is_404(client):
    assert client.get("/ai-tools/no-such-tool").status_code == 404
    assert client.get("/ai-tools/no-such-tool/reports.json").status_code == 404


# ----- posting reports -----------------------------------------------------------------------------

def test_member_reports_and_it_appears_everywhere(user_client):
    page = user_client.get("/ai-tools/claude-code")
    assert 'name="tool_version"' in page.text and 'value="NVDA"' in page.text  # screen reader prefilled
    r = report(user_client)
    assert r.status_code == 303 and r.headers["location"].startswith("/ai-tools/claude-code#report-")
    page = user_client.get("/ai-tools/claude-code")
    assert "is published" in page.text
    assert "<h2>Version 2.1.296: 1 report</h2>" in page.text
    assert "You can tell when it has finished: yes" in page.text
    assert "You can review what it changed: no" in page.text
    assert "What you type is spoken: not tested" in page.text
    assert "Overall: Usable with workarounds" in page.text
    assert "<strong>NVDA+6</strong>" in page.text  # member text rendered as Markdown
    assert "Reported on" in page.text and "by you" in page.text
    assert "Member verdict in the last 90 days: 0 usable, 1 with workarounds, 0 not usable" in page.text
    assert not check_html(page.text).errors
    index = user_client.get("/ai-tools")
    row = table_row(index.text, "Claude Code (terminal)")
    assert "0 usable, 1 with workarounds, 0 not usable" in row
    assert datetime.now(timezone.utc).strftime("%Y-%m-%d") in row


def test_reporting_same_version_again_updates(user_client):
    report(user_client)
    r = report(user_client, overall="usable", tool_version="v2.1.296")
    assert r.status_code == 303
    with db.session() as conn:
        rows = conn.execute("SELECT overall FROM ai_tool_reports").fetchall()
    assert [x["overall"] for x in rows] == ["usable"]
    assert "is updated" in user_client.get("/ai-tools/claude-code").text


def test_every_check_is_required(user_client):
    data = {k: v for k, v in GOOD_REPORT.items() if k not in ai_tools.CHECK_KEYS and k != "overall"}
    r = post(user_client, "/ai-tools/claude-code/reports", data)
    assert r.status_code == 400
    assert "please fix the problems" in r.text
    assert "There are 7 problems" in r.text
    for key in ai_tools.CHECK_KEYS + ("overall",):
        assert f'href="#field-{key}"' in r.text
        assert f'id="error-{key}"' in r.text
    assert 'value="2.1.296"' in r.text  # what they typed is kept
    assert len(re.findall(r"<h1[ >]", r.text)) == 1
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM ai_tool_reports").fetchone()["n"] == 0


def test_text_fields_validated(user_client):
    r = post(user_client, "/ai-tools/claude-code/reports",
             dict(GOOD_REPORT, tool_version="  ", screen_reader="", os="", turn_end="maybe", overall="great",
                  screen_reader_version="x" * 61, note="n" * 4001))
    assert r.status_code == 400
    for key in ("tool_version", "screen_reader", "os", "turn_end", "overall", "screen_reader_version", "note"):
        assert f'href="#field-{key}"' in r.text, key


def test_secrets_are_refused(user_client):
    r = post(user_client, "/ai-tools/claude-code/reports", dict(GOOD_REPORT, note="my key is sk-ant-api03-abcdefghijklmnop"))
    assert r.status_code == 400 and "API key" in r.text
    r = post(user_client, "/ai-tools/claude-code/reports", dict(GOOD_REPORT, workarounds="card 4242 4242 4242 4242"))
    assert r.status_code == 400 and 'href="#field-workarounds"' in r.text
    # Version numbers and ordinary numbers are fine.
    assert report(user_client, note="Tried 2.1.296 and 2.1.290 on build 26300.8497").status_code == 303


def test_sign_in_required_to_post(client):
    r = report(client)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM ai_tool_reports").fetchone()["n"] == 0


def test_post_without_csrf_rejected(user_client):
    r = user_client.post("/ai-tools/claude-code/reports", data=GOOD_REPORT)
    assert r.status_code == 400


def test_posting_to_unknown_tool_is_404(user_client):
    assert report(user_client, slug="no-such-tool").status_code == 404


def test_member_text_cannot_inject_html(user_client):
    report(user_client, note="<script>alert(1)</script> # Big heading")
    page = user_client.get("/ai-tools/claude-code")
    assert "<script>alert(1)</script>" not in page.text
    assert len(re.findall(r"<h1[ >]", page.text)) == 1


# ----- ownership --------------------------------------------------------------------------------------

def test_only_the_author_can_delete_a_report(client):
    register(client, "a@example.com", "A")
    report(client)
    with db.session() as conn:
        rid = conn.execute("SELECT id FROM ai_tool_reports").fetchone()["id"]
    post(client, "/logout")
    register(client, "b@example.com", "B")
    page = client.get("/ai-tools/claude-code")
    assert "Delete your report" not in page.text
    assert post(client, f"/ai-tools/claude-code/reports/{rid}/delete").status_code == 404
    # The wrong tool in the address is also 404, even for the author.
    post(client, "/logout")
    post(client, "/login", {"email": "a@example.com", "password": "correct horse battery"})
    assert post(client, f"/ai-tools/cursor/reports/{rid}/delete").status_code == 404
    assert "Delete your report for version 2.1.296" in client.get("/ai-tools/claude-code").text
    r = post(client, f"/ai-tools/claude-code/reports/{rid}/delete", follow_redirects=False)
    assert r.status_code == 303
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM ai_tool_reports").fetchone()["n"] == 0


def test_reports_show_member_name_not_real_name(client):
    register(client, "a@example.com", "Amina Real Name")
    report(client)
    post(client, "/logout")
    page = client.get("/ai-tools/claude-code")
    assert "Amina Real Name" not in page.text
    assert re.search(r"by Member [0-9A-F]{4}", page.text)


# ----- summaries, grouping and regressions ---------------------------------------------------------------

def test_table_summary_counts_only_last_90_days(user_client):
    uid = user_id()
    insert_report("cursor", uid, "usable", days_ago=1, version="3.23")
    insert_report("cursor", uid, "usable", days_ago=89, version="3.22")
    insert_report("cursor", uid, "not_usable", days_ago=91, version="3.10")
    insert_report("cursor", uid, "not_usable", days_ago=300, version="2.0")
    row = table_row(user_client.get("/ai-tools").text, "Cursor")
    assert "2 usable, 0 with workarounds, 0 not usable" in row
    insert_report("lovable", uid, "usable", days_ago=120)
    assert "No reports in the last 90 days" in table_row(user_client.get("/ai-tools").text, "Lovable")
    page = user_client.get("/ai-tools/cursor")
    assert "Member verdict in the last 90 days: 2 usable, 0 with workarounds, 0 not usable" in page.text


def test_reports_grouped_by_version_newest_first(client):
    register(client, "a@example.com", "A")
    uid = user_id("a@example.com")
    insert_report("codex-cli", uid, "usable", days_ago=10, version="0.153.0")
    insert_report("codex-cli", uid, "not_usable", days_ago=2, version="0.154.0")
    insert_report("codex-cli", uid, "not_usable", days_ago=1, version="0.154.0")
    insert_report("codex-cli", uid, "usable", days_ago=12, version="0.153.0")
    page = client.get("/ai-tools/codex-cli")
    headings = re.findall(r"<h2>(Version [^<]+)</h2>", page.text)
    assert headings == ["Version 0.154.0: 2 reports", "Version 0.153.0: 2 reports"]
    assert "Most recently reported version: 0.154.0" in page.text
    assert "Members report more problems with version 0.154.0 than with version 0.153.0" in page.text
    assert not check_html(page.text).errors


def test_group_by_version_and_regression_helpers():
    def r(version, overall, date="2026-10-01"):
        return {"tool_version": version, "overall": overall, "date": date}
    groups = ai_tools.group_by_version([r("2.0", "usable"), r("V2.0 ", "usable"), r("1.9", "not_usable")])
    assert [g["version"] for g in groups] == ["2.0", "1.9"]
    assert groups[0]["verdict"] == "2 usable, 0 with workarounds, 0 not usable"
    assert ai_tools.regression_notice(groups) == ""  # newer version is better, no notice
    assert ai_tools.regression_notice(groups[:1]) == ""
    worse = ai_tools.group_by_version([r("2.0", "not_usable"), r("1.9", "usable")])
    assert "version 2.0" in ai_tools.regression_notice(worse)
    assert ai_tools.verdict_text({"usable": 4, "usable_with_workarounds": 1, "not_usable": 0}) == \
        "4 usable, 1 with workarounds, 0 not usable"
    assert ai_tools.verdict_text({"usable": 0, "usable_with_workarounds": 0, "not_usable": 0}) == "No reports yet"


# ----- JSON -------------------------------------------------------------------------------------

def test_json_endpoint(user_client):
    report(user_client)
    uid = user_id()
    insert_report("claude-code", uid, "usable", days_ago=200, version="2.1.100")
    r = user_client.get("/ai-tools/claude-code/reports.json")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    data = r.json()
    assert data["tool"]["slug"] == "claude-code" and data["tool"]["kind"] == "terminal"
    assert data["research"]["as_of"] == "October 2026" and data["research"]["sources"]
    assert data["summary_last_90_days"] == {"usable": 0, "usable_with_workarounds": 1, "not_usable": 0}
    assert len(data["reports"]) == 2
    first = data["reports"][0]
    assert first["tool_version"] == "2.1.296"
    assert first["checks"]["changes_reviewable"] == "no"
    assert set(first["checks"]) == set(ai_tools.CHECK_KEYS)
    assert "user_id" not in first and "pseudonym" not in first and "email" not in json.dumps(data)


def test_json_is_public(client):
    data = client.get("/ai-tools/lovable/reports.json").json()
    assert data["reports"] == [] and data["tool"]["name"] == "Lovable"


# ----- without AI -----------------------------------------------------------------------------------

def test_works_when_ai_is_not_available(no_ai_app):
    client = TestClient(no_ai_app)
    register(client)
    assert client.get("/ai-tools").status_code == 200
    r = report(client)
    assert r.status_code == 303
    page = client.get("/ai-tools/claude-code")
    assert "Version 2.1.296: 1 report" in page.text
    assert "connect your own Claude API key" not in page.text


def test_index_links_every_tool_page_and_each_passes_checks(user_client):
    index = user_client.get("/ai-tools")
    slugs = re.findall(r'href="/ai-tools/([a-z0-9-]+)"', index.text)
    assert len(slugs) >= 11
    for slug in slugs:
        page = user_client.get(f"/ai-tools/{slug}")
        assert page.status_code == 200, slug
        assert not check_html(page.text).errors, slug
        assert len(re.findall(r"<h1[ >]", page.text)) == 1
