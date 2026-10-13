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
    assert "<h2>Member reports: 1</h2>" in page.text
    assert "<h3>Version 2.1.296: 1 report</h3>" in page.text
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
    assert "Please check 7 answers" in r.text
    for key in ai_tools.CHECK_KEYS + ("overall",):
        # The summary links to the first choice, which can take focus; the group itself cannot.
        assert f'href="#field-{key}-1"' in r.text and f'id="field-{key}-1"' in r.text
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
    for key in ("tool_version", "screen_reader", "os", "turn_end-1", "overall-1", "screen_reader_version", "note"):
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
    headings = re.findall(r"<h3>(Version [^<]+)</h3>", page.text)
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


# ----- audit regressions ------------------------------------------------------------------------------

def h1(page_text):
    return re.sub(r"\s+", " ", re.search(r"<h1>(.*?)</h1>", page_text, re.S).group(1)).strip()


def title(page_text):
    return re.search(r"<title>(.*?) – ", page_text, re.S).group(1).strip()


def test_h1_says_what_just_happened(user_client):
    report(user_client)
    page = user_client.get("/ai-tools/claude-code")
    assert h1(page.text) == "Your report on Claude Code (terminal) version 2.1.296 is published"
    assert title(page.text) == h1(page.text)
    # Only the page right after the action says so; reading it again shows the usual heading.
    assert h1(user_client.get("/ai-tools/claude-code").text) == "How Claude Code (terminal) works with screen readers"
    report(user_client, overall="usable")
    assert h1(user_client.get("/ai-tools/claude-code").text) == \
        "Your report on Claude Code (terminal) version 2.1.296 is updated"
    with db.session() as conn:
        rid = conn.execute("SELECT id FROM ai_tool_reports").fetchone()["id"]
    post(user_client, f"/ai-tools/claude-code/reports/{rid}/delete", follow_redirects=False)
    page = user_client.get("/ai-tools/claude-code")
    assert h1(page.text) == "Your report on Claude Code (terminal) version 2.1.296 is deleted"
    assert not check_html(page.text).errors


def test_done_heading_belongs_to_its_tool_and_not_to_the_address(user_client):
    report(user_client)
    # Another tool's page does not take over the message, and the address cannot set it.
    assert h1(user_client.get("/ai-tools/cursor?done=Your+report+is+published").text) == \
        "How Cursor works with screen readers"
    assert h1(user_client.get("/ai-tools/claude-code?text=hacked").text) == \
        "How Claude Code (terminal) works with screen readers"


def test_error_messages_name_their_field(user_client):
    r = post(user_client, "/ai-tools/claude-code/reports",
             dict(GOOD_REPORT, screen_reader_version="x" * 31, os="o" * 81, note="password: hunter2"))
    assert r.status_code == 400
    assert "The screen reader version is too long. Shorten it to 30 characters or fewer." in r.text
    assert "The operating system is too long. Shorten it to 80 characters or fewer." in r.text
    assert "Anything else looks like it has a password, API key or card number in it." in r.text
    r = post(user_client, "/ai-tools/claude-code/reports", dict(GOOD_REPORT, workarounds="w" * 4001))
    assert "Workarounds is too long. Shorten it to 4000 characters or fewer." in r.text


def test_long_unicode_and_odd_input(user_client):
    # Only invisible characters: treated as empty, not saved as a blank-looking version.
    r = post(user_client, "/ai-tools/claude-code/reports", dict(GOOD_REPORT, tool_version="​​ ⁠"))
    assert r.status_code == 400 and 'href="#field-tool_version"' in r.text
    # A field sent twice keeps its first value instead of being saved as "['a', 'b']".
    r = post(user_client, "/ai-tools/claude-code/reports",
             dict(GOOD_REPORT, tool_version=["2.1.296", "9.9.9"], note="Ça marche très bien\x00 avec NVDA 日本語"), follow_redirects=False)
    assert r.status_code == 303
    with db.session() as conn:
        row = conn.execute("SELECT tool_version, note FROM ai_tool_reports").fetchone()
    assert row["tool_version"] == "2.1.296"
    assert row["note"] == "Ça marche très bien avec NVDA 日本語"
    page = user_client.get("/ai-tools/claude-code")
    assert "Ça marche très bien avec NVDA 日本語" in page.text
    # Exactly at the limits is accepted.
    r = report(user_client, tool_version="1" * 30, note="n" * 4000)
    assert r.status_code == 303


def test_member_can_report_at_most_a_set_number_of_versions(user_client, monkeypatch):
    monkeypatch.setattr(ai_tools, "MAX_REPORTS_PER_TOOL", 2)
    assert report(user_client, tool_version="1.0").status_code == 303
    assert report(user_client, tool_version="1.1").status_code == 303
    r = report(user_client, tool_version="1.2")
    assert r.status_code == 400
    assert "You have reported 2 versions of Claude Code (terminal)" in r.text
    assert "Delete one of your older reports" in r.text
    # Updating a version already reported still works, and other tools are not affected.
    assert report(user_client, tool_version="1.1", overall="usable").status_code == 303
    assert report(user_client, slug="cursor", tool_version="1.2").status_code == 303


def test_form_address_opened_directly_goes_to_the_form(client):
    # For example, the session ran out while the form was open: signing in again returns to this address.
    r = client.get("/ai-tools/claude-code/reports", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ai-tools/claude-code#report-form-title"
    assert client.get("/ai-tools/no-such-tool/reports").status_code == 404
    r = report(client)
    assert r.headers["location"] == "/login?next=%2Fai-tools%2Fclaude-code%2Freports"
    register(client)
    page = client.get("/ai-tools/claude-code/reports")
    assert page.status_code == 200 and 'name="tool_version"' in page.text


def test_delete_address_opened_directly_deletes_nothing(user_client):
    report(user_client)
    with db.session() as conn:
        rid = conn.execute("SELECT id FROM ai_tool_reports").fetchone()["id"]
    r = user_client.get(f"/ai-tools/claude-code/reports/{rid}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ai-tools/claude-code"
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM ai_tool_reports").fetchone()["n"] == 1


def test_regression_notice_compares_versions_by_number():
    def r(version, overall):
        return {"tool_version": version, "overall": overall, "date": "2026-10-01"}
    # Someone reports the OLD version today as not usable: that is not a problem after an update.
    groups = ai_tools.group_by_version([r("0.153.0", "not_usable"), r("0.154.0", "usable")])
    assert groups[0]["version"] == "0.153.0"  # still listed by most recent report
    assert ai_tools.regression_notice(groups) == ""
    # The newer version is worse, even though the older one was reported more recently.
    groups = ai_tools.group_by_version([r("0.153.0", "usable"), r("0.154.0", "not_usable")])
    assert ai_tools.regression_notice(groups).startswith("Members report more problems with version 0.154.0 than "
                                                         "with version 0.153.0")
    # Numbers compare as numbers, not text: 2.1.10 is newer than 2.1.9.
    groups = ai_tools.group_by_version([r("2.1.9", "usable"), r("v2.1.10", "not_usable")])
    assert "with version v2.1.10 than with version 2.1.9" in ai_tools.regression_notice(groups)
    # Versions that are not plain numbers fall back to the order they were reported in.
    groups = ai_tools.group_by_version([r("nightly", "not_usable"), r("stable", "usable")])
    assert "version nightly" in ai_tools.regression_notice(groups)


def test_pages_and_json_say_what_was_checked(user_client):
    report(user_client)
    page = user_client.get("/ai-tools/claude-code")
    assert ai_tools.REPORTS_ABOUT in page.text
    assert "Nobody on this site tested the tool." in page.text
    assert "Workarounds, in the member's words:" in page.text
    assert "Note, in the member's words:" in page.text
    assert ai_tools.REPORTS_ABOUT in user_client.get("/ai-tools").text
    data = user_client.get("/ai-tools/claude-code/reports.json").json()
    assert data["reports_about"] == ai_tools.REPORTS_ABOUT
    assert data["research"]["about"] == ai_tools.RESEARCH_ABOUT


def test_tool_page_with_only_old_reports_does_not_say_no_reports_yet(user_client):
    insert_report("lovable", user_id(), "usable", days_ago=120, version="1.0")
    page = user_client.get("/ai-tools/lovable")
    assert "No member reports in the last 90 days. Older reports are below." in page.text
    assert "No reports yet" not in page.text and "No member reports yet" not in page.text
    assert "<h3>Version 1.0: 1 report</h3>" in page.text
    assert "No member reports yet." in user_client.get("/ai-tools/cursor").text


def test_tool_page_link_texts_make_sense_alone(client):
    page = client.get("/ai-tools/cursor")
    assert ">Report how Cursor worked for you</a>" in page.text
    assert ">Cursor reports as JSON, for other tools to use</a>" in page.text
    index = client.get("/ai-tools")
    assert "/reports.json" not in index.text and "<em>tool name</em>" not in index.text


def test_seeded_facts_match_the_research():
    notes = {t["slug"]: t["research_note"] for t in ai_tools.load_seed()}
    # Claude Code rings a bell, so "sound cues are not shipped" was wrong; the maintainer named speech,
    # plain-language tool names and heading rules (issue 70425, 17 August 2026).
    assert "sound cues" not in notes["claude-code"]
    assert "plain-language tool names" in notes["claude-code"]
    # Copilot CLI's --screen-reader mode is from October 2025, before general availability.
    assert "since October 2025" in notes["copilot-cli"]
    # Issue 99332 is about the messages in a conversation, not the list of conversations.
    assert "conversation list" not in notes["claude-desktop"]
    # Only one VoiceOver user reported being stuck at first-run setup; the "very usable" quote is not from a
    # user known to be blind; VS Code dictation is experimental; the AppleVis post date is unconfirmed.
    assert notes["antigravity-cli"].count("A blind VoiceOver user") == 1
    assert "A blind user" not in notes["gemini-cli"]
    assert "experimental built-in offline dictation" in notes["vs-code-copilot"]
    assert "date could not be confirmed" in notes["codex-desktop"]
    # PR 20564 made the setting turn every animation off; detection came on 16 September 2026.
    assert "tui.animations=false turns every animation off" in notes["codex-cli"]
