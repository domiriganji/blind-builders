"""Peer checks: requests, reports, rewards in credits and the public, tamper-evident record."""
import re

import pytest
from fastapi.testclient import TestClient

from studio import credits, db
from studio.a11y_check import check_html
from studio.features import checks as checks_module

from .conftest import post, register, user_id

ASKER = "asker@example.com"
CHECKER = "checker@example.com"
THIRD = "third@example.com"


def sign_in_as(client, email, name):
    """Sign out whoever is signed in, then register (first time) or sign in as this member."""
    post(client, "/logout")
    with db.session() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
    if exists:
        r = post(client, "/login", {"email": email, "password": "correct horse battery"})
        assert r.status_code == 200
    else:
        register(client, email, name)


def make_request(client, *, title="Cake shop", url="https://cakes.example/order", what="Order a chocolate cake.",
                 max_checks="3", reward="0", **kwargs):
    return post(client, "/checks/new", {"title": title, "url": url, "what_to_try": what, "max_checks": max_checks,
                                        "reward_cents": reward}, **kwargs)


def latest_request_id() -> int:
    with db.session() as conn:
        return conn.execute("SELECT MAX(id) AS id FROM check_requests").fetchone()["id"]


def report_data(**overrides):
    data = {"screen_reader": "NVDA", "screen_reader_version": "2026.2", "browser": "Firefox",
            "device": "Windows laptop", "heard_first": "Cake shop, heading level 1",
            "task_result": "completed", "problems": "The basket button has no name."}
    data.update(overrides)
    return data


def send_report(client, request_id, follow_redirects=True, **overrides):
    return post(client, f"/checks/{request_id}/report", report_data(**overrides), follow_redirects=follow_redirects)


def assert_accessible(response):
    report = check_html(response.text)
    assert report.errors == [], [(f.rule, f.message, f.where) for f in report.errors]
    assert len(re.findall(r"<h1[ >]", response.text)) == 1


def h1(response) -> str:
    return re.search(r"<h1>(.*?)</h1>", response.text, re.S).group(1).strip()


def title_text(response) -> str:
    return re.search(r"<title>(.*?) – ", response.text, re.S).group(1).strip()


@pytest.fixture
def asker(client):
    register(client, ASKER, "Asha")
    return client


# ----- index and the request form -------------------------------------------------------------

def test_index_signed_out_explains_and_offers_sign_in(client):
    r = client.get("/checks")
    assert r.status_code == 200
    assert h1(r) == "Peer checks" == title_text(r)
    assert "/login?next=/checks" in r.text
    assert "Check someone's work" not in r.text
    assert_accessible(r)


def test_index_signed_in_has_sections(asker):
    r = asker.get("/checks")
    assert 'href="/checks/new"' in r.text and "Ask for a check" in r.text
    for heading in ("Check someone's work", "Your requests", "Checks you did"):
        assert f"<h2>{heading}</h2>" in r.text
    assert_accessible(r)


def test_new_form_needs_sign_in(client):
    r = client.get("/checks/new", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_new_form_prefills_from_query(asker):
    r = asker.get("/checks/new", params={"url": "https://my.example/app", "title": "My app"})
    assert r.status_code == 200
    assert h1(r) == "Ask for a check" == title_text(r)
    assert 'value="https://my.example/app"' in r.text and 'value="My app"' in r.text
    assert "Credits are the site" in r.text
    assert_accessible(r)


def test_prefill_is_escaped(asker):
    r = asker.get("/checks/new", params={"title": '"><script>alert(1)</script>'})
    assert "<script>alert(1)" not in r.text


def test_request_without_reward(asker):
    r = make_request(asker, follow_redirects=False)
    assert r.status_code == 303
    rid = latest_request_id()
    assert r.headers["location"] == f"/checks/{rid}"
    page = asker.get(f"/checks/{rid}")
    assert h1(page) == "Check request: Cake shop" == title_text(page)
    assert "Your request is open" in page.text
    assert "No reward is offered" in page.text
    assert "Order a chocolate cake." in page.text
    assert "Close this request" in page.text
    assert "Check this now" not in page.text  # not for your own request
    assert 'rel="noopener noreferrer"' in page.text
    assert credits.balance(user_id(ASKER)) == 0
    assert_accessible(page)
    index = asker.get("/checks")
    assert "Cake shop</a>: 0 of 3 checks done, open" in index.text


def test_request_with_reward_holds_credits(asker):
    uid = user_id(ASKER)
    credits.add(uid, 500, "test top-up")
    make_request(asker, max_checks="3", reward="50")
    assert credits.balance(uid) == 350
    reasons = [h["reason"] for h in credits.history(uid)]
    assert "Held for peer checks: Cake shop" in reasons
    held = [h for h in credits.history(uid) if h["reason"].startswith("Held")][0]
    assert held["delta_cents"] == -150 and held["ref"] == f"check:{latest_request_id()}"
    assert "50 cents in credits for each check" in asker.get(f"/checks/{latest_request_id()}").text


def test_insufficient_credits_is_a_form_error(asker):
    uid = user_id(ASKER)
    credits.add(uid, 100, "test top-up")
    r = make_request(asker, max_checks="5", reward="25")
    assert r.status_code == 400
    assert title_text(r) == h1(r) == "Ask for a check: please fix the problems"
    assert 'href="#field-reward_cents"' in r.text and "You need $1.25" in r.text and "you have $1.00" in r.text
    assert credits.balance(uid) == 100
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM check_requests").fetchone()["n"] == 0
    assert 'value="Cake shop"' in r.text  # what they typed is kept
    assert_accessible(r)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "ftp://files.example/x", "cakes.example", "https://",
                                 "http://exa mple.com", "data:text/html,hi", ""])
def test_url_validation(asker, url):
    r = make_request(asker, url=url)
    assert r.status_code == 400
    assert "Enter the full web address" in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM check_requests").fetchone()["n"] == 0


def test_http_url_is_accepted(asker):
    r = make_request(asker, url="http://plain.example/page", follow_redirects=False)
    assert r.status_code == 303


def test_required_fields_and_bad_choices(asker):
    r = post(asker, "/checks/new", {"title": "", "url": "https://x.example", "what_to_try": "",
                                    "max_checks": "9", "reward_cents": "7"})
    assert r.status_code == 400
    for name in ("title", "what_to_try", "max_checks", "reward_cents"):
        assert f'href="#field-{name}"' in r.text
    assert "There are 4 problems" in r.text


def test_secrets_are_refused_in_public_text(asker):
    r = make_request(asker, what="Sign in with password: hunter22 and order")
    assert r.status_code == 400 and "looks like a password" in r.text
    r = make_request(asker, what="Pay with 4111 1111 1111 1111 to finish")
    assert r.status_code == 400 and "card number" in r.text


def test_new_request_needs_csrf(asker):
    r = asker.post("/checks/new", data={"title": "x", "url": "https://x.example", "what_to_try": "y",
                                        "max_checks": "1", "reward_cents": "0"})
    assert r.status_code == 400


# ----- checking ---------------------------------------------------------------------------------

def test_full_journey_with_reward(asker):
    asker_id = user_id(ASKER)
    credits.add(asker_id, 300, "test top-up")
    make_request(asker, max_checks="2", reward="100")
    rid = latest_request_id()
    assert credits.balance(asker_id) == 100

    sign_in_as(asker, CHECKER, "Chidi")
    checker_id = user_id(CHECKER)
    index = asker.get("/checks")
    assert f'href="/checks/{rid}"' in index.text and "1 dollar in credits" in index.text
    page = asker.get(f"/checks/{rid}")
    assert "Check this now" in page.text and "Close this request" not in page.text
    form = asker.get(f"/checks/{rid}/report")
    assert h1(form) == "Check this: Cake shop" == title_text(form)
    assert 'value="NVDA"' in form.text and 'value="Windows laptop"' in form.text  # from the profile
    assert_accessible(form)

    r = send_report(asker, rid, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/checks/{rid}/record"
    assert credits.balance(checker_id) == 100
    assert any(h["reason"] == "Peer check reward: Cake shop" for h in credits.history(checker_id))
    record = asker.get(f"/checks/{rid}/record")
    assert "Your check is saved in the public record" in record.text and "$1.00 was added" in record.text
    # Now it leaves the queue and appears under checks you did.
    index = asker.get("/checks")
    assert f'href="/checks/{rid}"' not in index.text.split("<h2>Your requests</h2>")[0]
    assert f'href="/checks/{rid}/record">Check record: Cake shop' in index.text


def test_cannot_check_own_request(asker):
    make_request(asker)
    rid = latest_request_id()
    r = asker.get(f"/checks/{rid}/report")
    assert r.status_code == 403
    assert h1(r) == "You cannot check this: Cake shop" == title_text(r)
    assert "your own request" in r.text
    assert_accessible(r)
    r = send_report(asker, rid)
    assert r.status_code == 403
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM check_reports").fetchone()["n"] == 0
    # Your own request is not in your queue.
    assert "No requests are waiting for you right now." in asker.get("/checks").text


def test_cannot_check_twice_and_no_double_reward(asker):
    credits.add(user_id(ASKER), 500, "test top-up")
    make_request(asker, max_checks="3", reward="25")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid)
    assert asker.get(f"/checks/{rid}/report").status_code == 403
    r = send_report(asker, rid, heard_first="Something else")
    assert r.status_code == 403 and "already checked" in r.text
    assert credits.balance(user_id(CHECKER)) == 25
    assert "Check this now" not in asker.get(f"/checks/{rid}").text


def test_auto_close_at_max(asker):
    credits.add(user_id(ASKER), 100, "test top-up")
    make_request(asker, max_checks="1", reward="50")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid)
    with db.session() as conn:
        row = conn.execute("SELECT status, closed_at FROM check_requests WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "closed" and row["closed_at"]
    sign_in_as(asker, THIRD, "Tomas")
    r = asker.get(f"/checks/{rid}/report")
    assert r.status_code == 403 and "closed" in r.text
    assert f'href="/checks/{rid}"' not in asker.get("/checks").text
    # Nothing to refund: the one check was paid.
    sign_in_as(asker, ASKER, "Asha")
    page = asker.get(f"/checks/{rid}")
    assert "This request is closed." in page.text and "Close this request" not in page.text
    assert credits.balance(user_id(ASKER)) == 50


def test_close_refunds_checks_not_done(asker):
    asker_id = user_id(ASKER)
    credits.add(asker_id, 500, "test top-up")
    make_request(asker, max_checks="4", reward="50")  # 200 held
    rid = latest_request_id()
    assert credits.balance(asker_id) == 300
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid)
    sign_in_as(asker, ASKER, "Asha")
    page = asker.get(f"/checks/{rid}")
    assert "The $1.50 held for checks not done yet comes back" in page.text
    r = post(asker, f"/checks/{rid}/close")
    assert r.status_code == 200 and "$1.50 came back to your credits" in r.text
    assert credits.balance(asker_id) == 450
    assert credits.balance(user_id(CHECKER)) == 50
    assert any(h["reason"] == "Returned from peer checks: Cake shop" and h["delta_cents"] == 150
               for h in credits.history(asker_id))
    # Closing again refunds nothing more.
    r = post(asker, f"/checks/{rid}/close")
    assert "already closed" in r.text
    assert credits.balance(asker_id) == 450


def test_close_without_reward(asker):
    make_request(asker)
    rid = latest_request_id()
    r = post(asker, f"/checks/{rid}/close")
    assert "This request is closed." in r.text
    assert credits.history(user_id(ASKER)) == []


def test_only_requester_can_close(asker):
    credits.add(user_id(ASKER), 200, "test top-up")
    make_request(asker, max_checks="2", reward="50")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    r = post(asker, f"/checks/{rid}/close")
    assert r.status_code == 404
    with db.session() as conn:
        assert conn.execute("SELECT status FROM check_requests WHERE id = ?", (rid,)).fetchone()["status"] == "open"
    assert credits.balance(user_id(CHECKER)) == 0


def test_report_validation(asker):
    make_request(asker)
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    r = send_report(asker, rid, screen_reader="", browser="", device="", heard_first="", task_result="maybe")
    assert r.status_code == 400
    assert h1(r) == "Check this: Cake shop: please fix the problems" == title_text(r)
    for name in ("screen_reader", "browser", "device", "heard_first", "task_result"):
        assert f'href="#field-{name}"' in r.text
    assert "The basket button has no name." in r.text  # what they typed is kept
    assert_accessible(r)
    r = send_report(asker, rid, problems="The test login is password: Secret123")
    assert r.status_code == 400 and "looks like a password" in r.text
    r = send_report(asker, rid, heard_first="x" * 4001)
    assert r.status_code == 400 and "4000 characters or fewer" in r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM check_reports").fetchone()["n"] == 0


def test_unknown_request_is_404(asker):
    assert asker.get("/checks/999").status_code == 404
    assert asker.get("/checks/999/record").status_code == 404
    assert asker.get("/checks/999/report").status_code == 404
    assert post(asker, "/checks/999/close").status_code == 404


# ----- the public record ------------------------------------------------------------------------

def test_record_is_public_but_request_page_needs_sign_in(asker):
    make_request(asker)
    rid = latest_request_id()
    post(asker, "/logout")
    r = asker.get(f"/checks/{rid}/record")
    assert r.status_code == 200
    assert h1(r) == "Check record: Cake shop" == title_text(r)
    assert "Nobody has checked this yet." in r.text
    assert_accessible(r)
    r = asker.get(f"/checks/{rid}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = asker.get(f"/checks/{rid}/report", follow_redirects=False)
    assert r.status_code == 303


def test_record_summary_and_details(asker):
    make_request(asker, max_checks="5")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid)
    sign_in_as(asker, THIRD, "Tomas")
    send_report(asker, rid, screen_reader="TalkBack", screen_reader_version="", browser="Chrome",
                device="Samsung Galaxy A15", task_result="partly", heard_first="Cake shop\nMenu button",
                problems="")
    sign_in_as(asker, "fourth@example.com", "Femi")
    send_report(asker, rid, screen_reader="VoiceOver", device="iPhone 13", browser="Safari")
    post(asker, "/logout")
    r = asker.get(f"/checks/{rid}/record")
    assert "3 members checked this. 2 could do the task, 1 could do it partly." in r.text
    assert r.text.index("3 members checked this") < r.text.index("<h2>The checks</h2>")  # summary first
    assert "This record has not been changed since it was written." in r.text
    assert "Screen reader: NVDA version 2026.2" in r.text and "Browser: Firefox" in r.text
    assert "Device: Samsung Galaxy A15" in r.text
    assert "Cake shop<br>Menu button" in r.text  # their words, line breaks kept
    assert "None written." in r.text
    with db.session() as conn:
        hashes = [row["record_hash"] for row in conn.execute(
            "SELECT record_hash FROM check_reports WHERE request_id = ? ORDER BY id", (rid,))]
    for h in hashes:
        assert h in r.text and re.fullmatch(r"[0-9a-f]{64}", h)
    not_checked = r.text.split("<h2>Not checked</h2>")[1]
    assert "JAWS" in not_checked and "Narrator" in not_checked and "Orca" in not_checked
    assert "NVDA" not in not_checked.split("Devices")[0] and "TalkBack" not in not_checked
    assert "Mac" in not_checked and "Chromebook" in not_checked and "Linux computer" in not_checked
    assert "Windows computer" not in not_checked and "iPhone or iPad" not in not_checked
    assert "Android phone or tablet" not in not_checked
    assert "No check here says anything about it." in not_checked
    assert_accessible(r)


def test_record_notes_appearance_when_a_check_mentions_it(asker):
    make_request(asker)
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid, problems="With my low vision the contrast of the links is poor.")
    r = asker.get(f"/checks/{rid}/record")
    assert "1 check mentions colours, contrast, layout or zoom" in r.text


def test_code_name_versus_show_name(asker):
    make_request(asker, max_checks="5")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi Okafor")
    send_report(asker, rid)  # checkbox not ticked
    sign_in_as(asker, THIRD, "Tomas Berg")
    send_report(asker, rid, show_name="yes")
    with db.session() as conn:
        code = conn.execute("SELECT pseudonym FROM users WHERE email = ?", (CHECKER,)).fetchone()["pseudonym"]
    r = asker.get(f"/checks/{rid}/record")
    assert f"Who: {code} (code name)" in r.text
    assert "Chidi Okafor" not in r.text
    assert "Who: Tomas Berg (their name, shown by their choice)" in r.text


def test_show_name_default_comes_from_profile(asker):
    make_request(asker)
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    form = asker.get(f"/checks/{rid}/report")
    assert re.search(r'id="field-show_name" name="show_name" value="yes" aria-describedby', form.text)  # not checked
    with db.session() as conn:
        conn.execute("UPDATE users SET show_name_on_checks = 1 WHERE email = ?", (CHECKER,))
    form = asker.get(f"/checks/{rid}/report")
    assert 'id="field-show_name" name="show_name" value="yes" checked aria-describedby' in form.text


def test_hash_chain_links_and_verifies(asker):
    make_request(asker, max_checks="3")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid)
    sign_in_as(asker, THIRD, "Tomas")
    send_report(asker, rid, task_result="could_not")
    with db.session() as conn:
        req = dict(conn.execute("SELECT * FROM check_requests WHERE id = ?", (rid,)).fetchone())
        rows = [dict(r) for r in conn.execute("SELECT * FROM check_reports WHERE request_id = ? ORDER BY id", (rid,))]
    assert rows[0]["prev_hash"] == ""
    assert rows[1]["prev_hash"] == rows[0]["record_hash"]
    assert checks_module.compute_hash(req, rows[0], "") == rows[0]["record_hash"]
    assert checks_module.verify_chain(req, rows) == []


def test_tampered_row_is_detected(asker):
    make_request(asker, max_checks="3")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid, task_result="could_not")
    sign_in_as(asker, THIRD, "Tomas")
    send_report(asker, rid)
    with db.session() as conn:
        first = conn.execute("SELECT MIN(id) AS id FROM check_reports WHERE request_id = ?", (rid,)).fetchone()["id"]
        conn.execute("UPDATE check_reports SET task_result = 'completed' WHERE id = ?", (first,))
    r = asker.get(f"/checks/{rid}/record")
    assert "This record was changed after it was written" in r.text
    assert "Entry 1 does not match its fingerprint" in r.text
    assert "has not been changed since it was written" not in r.text
    assert_accessible(r)


def test_removed_middle_row_and_edited_request_are_detected(asker):
    make_request(asker, max_checks="3")
    rid = latest_request_id()
    for email, name in ((CHECKER, "Chidi"), (THIRD, "Tomas"), ("fourth@example.com", "Femi")):
        sign_in_as(asker, email, name)
        send_report(asker, rid)
    with db.session() as conn:
        middle = conn.execute("SELECT id FROM check_reports WHERE request_id = ? ORDER BY id LIMIT 1 OFFSET 1",
                              (rid,)).fetchone()["id"]
        conn.execute("DELETE FROM check_reports WHERE id = ?", (middle,))
    r = asker.get(f"/checks/{rid}/record")
    assert "Entry 2 does not point to the entry before it" in r.text
    with db.session() as conn:
        conn.execute("UPDATE check_requests SET url = 'https://other.example' WHERE id = ?", (rid,))
    r = asker.get(f"/checks/{rid}/record")
    assert "Entry 1 does not match its fingerprint" in r.text


def test_request_page_is_shared_with_members_but_close_is_owner_only(asker):
    make_request(asker)
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    page = asker.get(f"/checks/{rid}")
    assert page.status_code == 200 and "Close this request" not in page.text
    assert_accessible(page)


# ----- without AI ------------------------------------------------------------------------------

def test_works_without_ai(no_ai_app):
    """Peer checks do not use the AI, so everything works for members with no key and no credits."""
    client = TestClient(no_ai_app)
    register(client, ASKER, "Asha")
    make_request(client)
    rid = latest_request_id()
    sign_in_as(client, CHECKER, "Chidi")
    r = send_report(client, rid)
    assert r.status_code == 200 and "Check record: Cake shop" in r.text
    assert "1 member checked this. 1 could do the task." in r.text


def test_every_checks_page_with_data_passes_checks(asker):
    credits.add(user_id(ASKER), 500, "test top-up")
    make_request(asker, max_checks="2", reward="25", what="Find the menu.\n\nThen order a cake.")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid)
    for path in ("/checks", f"/checks/{rid}", f"/checks/{rid}/report", f"/checks/{rid}/record", "/checks/new"):
        r = asker.get(path)
        assert r.status_code in (200, 403), path
        assert_accessible(r)
    sign_in_as(asker, ASKER, "Asha")
    for path in ("/checks", f"/checks/{rid}", f"/checks/{rid}/record"):
        assert_accessible(asker.get(path))
