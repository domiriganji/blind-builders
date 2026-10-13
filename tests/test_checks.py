"""Peer checks: requests, reports, rewards in credits and the public, tamper-evident record."""
import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from studio import credits, db
from studio.a11y_check import check_html
from studio.features import checks as checks_module

from .conftest import csrf, post, register, user_id

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
                 max_checks="3", reward="0", wanted="screen_reader", **kwargs):
    return post(client, "/checks/new", {"title": title, "url": url, "what_to_try": what, "max_checks": max_checks,
                                        "reward_cents": reward, "wanted": wanted}, **kwargs)


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


def lbl(rid: int, title: str = "Cake shop", wanted: str = "screen_reader") -> str:
    """A request's name in links and titles: its name, number and who it is for."""
    who = {"screen_reader": "for blind and low-vision checkers", "sighted": "for sighted checkers",
           "both": "for blind, low-vision and sighted checkers"}[wanted]
    return f"{title} (request {rid}, {who})"


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
    assert h1(page) == f"Your request is open: {lbl(rid)}" == title_text(page)  # says what just happened
    assert "Other members can now check it." in page.text
    assert "No reward is offered" in page.text
    assert "Order a chocolate cake." in page.text
    assert "Close this request" in page.text
    assert "Check this now" not in page.text  # not for your own request
    assert 'rel="noopener noreferrer"' in page.text
    assert credits.balance(user_id(ASKER)) == 0
    assert_accessible(page)
    index = asker.get("/checks")
    assert f"{lbl(rid)}</a>. 0 of 3 checks done. Asked on " in index.text


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
    assert title_text(r) == h1(r) == "Please check 1 answer: Ask for a check"
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
                                    "max_checks": "9", "reward_cents": "7", "wanted": "robots"})
    assert r.status_code == 400
    for name in ("title", "what_to_try", "max_checks", "reward_cents"):
        assert f'href="#field-{name}"' in r.text
    # The radio group's error links to its first choice, which can take focus.
    assert 'href="#field-wanted-1"' in r.text and "Choose who should check it" in r.text
    assert "Please check 5 answers" in r.text
    assert_accessible(r)


def test_secrets_are_refused_in_public_text(asker):
    r = make_request(asker, what="Sign in with password: hunter22 and order")
    assert r.status_code == 400 and "looks like it includes a password" in r.text
    # The one shared wording: it allows for a mistake, says how to send it anyway, and that the page is public.
    assert "If it is not one, change the wording a little" in r.text and "Everyone can read this page." in r.text
    assert "ask checkers to make their own test account" in r.text
    assert 'value="Cake shop"' in r.text and "hunter22" in r.text  # their words are kept to correct
    r = make_request(asker, what="Pay with 4111 1111 1111 1111 to finish")
    assert r.status_code == 400 and "card number" in r.text


def test_ordinary_words_about_pins_and_passwords_are_accepted(asker):
    """Accessibility reports talk about PIN boxes and password fields all the time."""
    r = make_request(asker, what="The PIN is not announced by TalkBack. Try the password field too.",
                     follow_redirects=False)
    assert r.status_code == 303


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
    assert h1(form) == f"Check this: {lbl(rid)}" == title_text(form)
    assert 'value="NVDA"' in form.text and 'value="Windows laptop"' in form.text  # from the profile
    assert_accessible(form)

    r = send_report(asker, rid, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/checks/{rid}/record?sent=1"
    assert credits.balance(checker_id) == 100
    assert any(h["reason"] == "Peer check reward: Cake shop" for h in credits.history(checker_id))
    record = asker.get(r.headers["location"])
    assert h1(record) == f"Your check is saved in the record: {lbl(rid)}" == title_text(record)
    assert "Thank you for checking. $1.00 was added to your credits" in record.text
    # Now it leaves the queue and appears under checks you did.
    index = asker.get("/checks")
    assert f'href="/checks/{rid}"' not in index.text.split("<h2>Your requests</h2>")[0]
    assert f'href="/checks/{rid}/record">Check record: {lbl(rid)}</a>' in index.text


def test_cannot_check_own_request(asker):
    make_request(asker)
    rid = latest_request_id()
    r = asker.get(f"/checks/{rid}/report")
    assert r.status_code == 403
    assert h1(r) == f"This check is not open to you: {lbl(rid)}" == title_text(r)
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
    # Sending again (a second press, or the back button) saves nothing and pays nothing, and says so.
    r = send_report(asker, rid, heard_first="Something else")
    assert r.status_code == 200 and "You sent this check before" in r.text and "paid once" in r.text
    assert h1(r) == f"Your check is saved in the record: {lbl(rid)}"
    assert "Something else" not in r.text
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
    assert "The $1.50 set aside for checks not done yet comes back" in page.text
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
    assert h1(r) == f"Please check 5 answers: Check this: {lbl(rid)}" == title_text(r)
    for name in ("screen_reader", "browser", "device", "heard_first"):
        assert f'href="#field-{name}"' in r.text
    # The radio group's error links to its first radio button, which can take focus (a fieldset cannot).
    assert 'href="#field-task_result-1"' in r.text and 'id="field-task_result-1"' in r.text
    assert "The basket button has no name." in r.text  # what they typed is kept
    assert_accessible(r)
    r = send_report(asker, rid, problems="The test login is password: Secret123")
    assert r.status_code == 400 and "looks like it includes a password" in r.text
    assert "The test login is password: Secret123" in r.text  # kept, so they can take out just the secret
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
    assert h1(r) == f"Check record: {lbl(rid)}" == title_text(r)
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
    assert "3 members tried the task with a screen reader, braille display or magnifier. 2 could do the task, 1 could do it partly." in r.text
    assert r.text.index("3 members tried the task") < r.text.index("<h2>The checks</h2>")  # summary first
    assert "sighted person has checked" not in r.text  # this request did not ask for sighted checks
    assert "Record intact. Every fingerprint matches, checked by the site just now." in r.text
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
    assert "How it looks: no sighted person checked it" in not_checked
    assert "colour" not in r.text.lower()
    assert_accessible(r)


def test_record_notes_appearance_when_a_check_mentions_it(asker):
    make_request(asker)
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid, problems="With my low vision the contrast of the links is poor.")
    r = asker.get(f"/checks/{rid}/record")
    not_checked = r.text.split("<h2>Not checked</h2>")[1]
    assert "1 screen reader check mentions how the page looks or how easy it is to read" in not_checked
    assert "colour" not in not_checked.lower()


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
    assert "Every fingerprint matches" not in r.text
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
    assert r.status_code == 200 and f"Your check is saved in the record: {lbl(rid)}" in r.text
    assert "1 member tried the task with a screen reader, braille display or magnifier. 1 could do the task." in r.text


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


# ----- audit regressions: money ----------------------------------------------------------------

def ledger_total_for(rid: int) -> int:
    """Every credit row for one request. Held minus paid minus returned: zero once the request is closed."""
    with db.session() as conn:
        return conn.execute("SELECT COALESCE(SUM(delta_cents), 0) AS s FROM credit_ledger WHERE ref = ?",
                            (f"check:{rid}",)).fetchone()["s"]


def count(table: str) -> int:
    with db.session() as conn:
        return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def _broken(*args, **kwargs):
    raise RuntimeError("disk full")


def test_hold_and_request_are_saved_together(asker, monkeypatch):
    """If the hold cannot be written, no request is left behind that would pay rewards nobody paid for."""
    uid = user_id(ASKER)
    credits.add(uid, 500, "test top-up")
    monkeypatch.setattr(checks_module, "_ledger", _broken)
    with pytest.raises(RuntimeError):
        make_request(asker, max_checks="2", reward="100")
    assert count("check_requests") == 0
    assert credits.balance(uid) == 500


def test_reward_and_check_are_saved_together(asker, monkeypatch):
    credits.add(user_id(ASKER), 500, "test top-up")
    make_request(asker, max_checks="1", reward="100")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    monkeypatch.setattr(checks_module, "_ledger", _broken)
    with pytest.raises(RuntimeError):
        send_report(asker, rid)
    # Neither the check nor the automatic close was saved, so the member can send it again and be paid.
    assert count("check_reports") == 0
    with db.session() as conn:
        assert conn.execute("SELECT status FROM check_requests WHERE id = ?", (rid,)).fetchone()["status"] == "open"
    monkeypatch.undo()
    send_report(asker, rid)
    assert credits.balance(user_id(CHECKER)) == 100
    assert ledger_total_for(rid) == 0


def test_close_and_refund_are_saved_together(asker, monkeypatch):
    uid = user_id(ASKER)
    credits.add(uid, 500, "test top-up")
    make_request(asker, max_checks="2", reward="100")
    rid = latest_request_id()
    monkeypatch.setattr(checks_module, "_ledger", _broken)
    with pytest.raises(RuntimeError):
        post(asker, f"/checks/{rid}/close")
    with db.session() as conn:
        assert conn.execute("SELECT status FROM check_requests WHERE id = ?", (rid,)).fetchone()["status"] == "open"
    monkeypatch.undo()
    r = post(asker, f"/checks/{rid}/close")
    assert "$2.00 came back to your credits" in r.text
    assert credits.balance(uid) == 500 and ledger_total_for(rid) == 0


def test_double_submitted_request_is_saved_and_held_once(asker):
    uid = user_id(ASKER)
    credits.add(uid, 500, "test top-up")
    first = make_request(asker, max_checks="2", reward="100", follow_redirects=False)
    second = make_request(asker, max_checks="2", reward="100")
    assert count("check_requests") == 1
    assert credits.balance(uid) == 300
    rid = latest_request_id()
    assert first.headers["location"] == f"/checks/{rid}"
    assert second.status_code == 200 and "You already asked for this check" in second.text
    assert "set aside once" in second.text
    assert h1(second) == f"Your request is open: {lbl(rid)}"
    # A different request is still allowed.
    make_request(asker, title="Cake shop basket", max_checks="1", reward="100")
    assert count("check_requests") == 2 and credits.balance(uid) == 200


def test_double_submission_at_the_same_moment(asker):
    """Presses that arrive together: one request, one hold."""
    uid = user_id(ASKER)
    credits.add(uid, 300, "test top-up")
    data = {"title": "Cake shop", "url": "https://cakes.example/order", "what_to_try": "Order a cake.",
            "max_checks": "3", "reward_cents": "100", "wanted": "both", "csrf_token": csrf(asker)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: asker.post("/checks/new", data=data, follow_redirects=False), range(4)))
    assert all(r.status_code == 303 for r in results), [r.status_code for r in results]
    assert count("check_requests") == 1
    assert credits.balance(uid) == 0


def test_checks_at_the_same_moment_never_overpay(client, app):
    """More checkers than places, all at once: only max_checks are saved and paid, and nothing is lost."""
    register(client, ASKER, "Asha")
    credits.add(user_id(ASKER), 200, "test top-up")
    make_request(client, max_checks="2", reward="100")
    rid = latest_request_id()
    payloads = []
    for n in range(5):
        c = TestClient(app)
        register(c, f"c{n}@example.com", f"Checker {n}")
        payloads.append((c, {**report_data(), "csrf_token": csrf(c)}))
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda cp: cp[0].post(f"/checks/{rid}/report", data=cp[1], follow_redirects=False), payloads))
    assert count("check_reports") == 2
    assert sum(credits.balance(user_id(f"c{n}@example.com")) for n in range(5)) == 200
    assert credits.balance(user_id(ASKER)) == 0 and ledger_total_for(rid) == 0


def test_money_adds_up_over_a_whole_request(asker):
    uid = user_id(ASKER)
    credits.add(uid, 1000, "test top-up")
    make_request(asker, max_checks="5", reward="50")
    rid = latest_request_id()
    for email, name in ((CHECKER, "Chidi"), (THIRD, "Tomas")):
        sign_in_as(asker, email, name)
        send_report(asker, rid)
        send_report(asker, rid)  # a second press pays nothing more
    sign_in_as(asker, ASKER, "Asha")
    post(asker, f"/checks/{rid}/close")
    post(asker, f"/checks/{rid}/close")
    assert credits.balance(user_id(CHECKER)) == 50 and credits.balance(user_id(THIRD)) == 50
    assert credits.balance(uid) == 900
    assert ledger_total_for(rid) == 0
    page = asker.get(f"/checks/{rid}")
    assert h1(page) == f"Your request is closed: {lbl(rid)}" == title_text(page)
    assert "Checkers were paid $1.00 in total." in page.text
    assert "The $1.50 set aside for checks that were not done came back to your credits" in page.text


def test_closed_after_every_check_says_all_credits_went_to_checkers(asker):
    credits.add(user_id(ASKER), 100, "test top-up")
    make_request(asker, max_checks="1", reward="100")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid)
    sign_in_as(asker, ASKER, "Asha")
    page = asker.get(f"/checks/{rid}")
    assert "Every check was done, so all the credits set aside went to checkers." in page.text
    assert "came back" not in page.text


# ----- audit regressions: input and security ----------------------------------------------------

@pytest.mark.parametrize("path", ["/checks/{n}", "/checks/{n}/record", "/checks/{n}/report"])
def test_huge_request_number_is_404_not_a_crash(asker, path):
    assert asker.get(path.format(n=10 ** 30)).status_code == 404


def test_huge_request_number_on_post_is_404(asker):
    assert post(asker, f"/checks/{10 ** 30}/close").status_code == 404
    assert post(asker, f"/checks/{10 ** 30}/report", report_data()).status_code == 404


@pytest.mark.parametrize("url", ["https://anna:hunter22@cakes.example/order", "https://anna@cakes.example/",
                                 "https://cakes.example/login?password=hunter22"])
def test_address_with_sign_in_details_is_refused(asker, url):
    r = make_request(asker, url=url)
    assert r.status_code == 400
    assert "everyone can read check records" in r.text and 'href="#field-url"' in r.text
    assert count("check_requests") == 0


def test_too_long_address_says_the_limit(asker):
    r = make_request(asker, url="https://cakes.example/" + "a" * 2000)
    assert r.status_code == 400 and "2000 characters or fewer" in r.text


def test_too_long_name_says_how_long_it_is(asker):
    r = make_request(asker, title="x" * 151)
    assert r.status_code == 400 and "This name is 151 characters long" in r.text and "150 characters or fewer" in r.text


def test_secret_in_screen_reader_field_is_refused(asker):
    make_request(asker)
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    r = send_report(asker, rid, screen_reader="NVDA, pin: 4821")
    assert r.status_code == 400 and 'href="#field-screen_reader"' in r.text and "looks like it includes a password" in r.text


def test_field_sent_twice_is_not_saved_as_a_list(asker):
    data = {"title": ["Cake", "Shop"], "url": "https://cakes.example", "what_to_try": "Order.",
            "max_checks": "1", "reward_cents": "0"}
    r = post(asker, "/checks/new", data)
    assert r.status_code == 400 and "Enter a short name" in r.text
    assert "[&#39;" not in r.text and count("check_requests") == 0


def test_unicode_and_line_breaks_are_kept(asker):
    make_request(asker, title="Café Ünïcode 日本", what="Ordena un pastel.\n\nLuego paga 😀.")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid, heard_first="Café, titre niveau 1")
    r = asker.get(f"/checks/{rid}/record")
    assert "Check record: Café Ünïcode 日本" in r.text
    assert "Café, titre niveau 1" in r.text and "Every fingerprint matches" in r.text


def test_sent_flag_does_not_change_the_title_for_others(asker):
    make_request(asker)
    rid = latest_request_id()
    assert h1(asker.get(f"/checks/{rid}/record?sent=1")) == f"Check record: {lbl(rid)}"
    post(asker, "/logout")
    assert h1(asker.get(f"/checks/{rid}/record?sent=1")) == f"Check record: {lbl(rid)}"


def test_record_labels_whose_words_are_whose(asker):
    make_request(asker)
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid)
    r = asker.get(f"/checks/{rid}/record")
    assert "in the words of the member who asked" in r.text
    assert "in the checker's own words" in r.text
    assert "Cake shop (the member's own page)</a>" in r.text  # link text makes sense on its own
    assert "could recompute every fingerprint" in r.text  # says what the check cannot show


# ----- audit regressions: deleted accounts and the index ---------------------------------------

def test_deleted_checker_is_shown_by_code_name_not_former_member(asker):
    make_request(asker)
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi Okafor")
    send_report(asker, rid, show_name="yes")
    with db.session() as conn:
        code = conn.execute("SELECT pseudonym FROM users WHERE email = ?", (CHECKER,)).fetchone()["pseudonym"]
    r = post(asker, "/account/delete", {"confirm": "yes"})
    assert r.status_code == 200
    record = asker.get(f"/checks/{rid}/record")
    assert "Chidi Okafor" not in record.text and "Former member" not in record.text
    assert f"Who: {code} (code name; this member has since left the site)" in record.text
    assert "Every fingerprint matches" in record.text  # the entry itself is untouched


def test_deleted_requester_requests_close_and_credits_add_up(asker):
    uid = user_id(ASKER)
    credits.add(uid, 300, "test top-up")
    make_request(asker, max_checks="3", reward="100")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid)
    sign_in_as(asker, ASKER, "Asha")
    post(asker, "/account/delete", {"confirm": "yes"})
    with db.session() as conn:
        assert conn.execute("SELECT status FROM check_requests WHERE id = ?", (rid,)).fetchone()["status"] == "closed"
    assert ledger_total_for(rid) == 0
    sign_in_as(asker, THIRD, "Tomas")
    assert f'href="/checks/{rid}"' not in asker.get("/checks").text
    assert asker.get(f"/checks/{rid}/report").status_code == 403


def test_your_requests_are_split_into_open_and_closed(asker):
    make_request(asker, title="First")
    first = latest_request_id()
    make_request(asker, title="Second")
    post(asker, f"/checks/{first}/close")
    r = asker.get("/checks")
    mine = r.text.split("<h2>Your requests</h2>")[1].split("<h2>")[0]
    assert "<h3>Open requests (1)</h3>" in mine and "<h3>Closed requests (1)</h3>" in mine
    assert mine.index("Second") < mine.index("Closed requests") < mine.index("First")
    assert_accessible(r)


# ----- sighted checks of how it looks ----------------------------------------------------------
# What a blind member reads about the look is about outcomes, never colours: these tests also check
# that no colour word appears in anything the site itself writes on these pages.

SIGHTED = "sighted@example.com"
COLOUR_WORDS = re.compile(r"\b(colou?rs?|red|green|blue|yellow|purple|orange|pink|grey|gray|black|white|hex)\b|"
                          r"#[0-9a-f]{3,6}\b", re.I)


def sighted_data(**overrides):
    data = {"method": "sighted", "looks_finished": "yes", "anything_broken": "no", "would_trust": "yes",
            "improve": "A bigger order button would help on a phone."}
    data.update(overrides)
    return data


def send_sighted(client, request_id, follow_redirects=True, **overrides):
    return post(client, f"/checks/{request_id}/report", sighted_data(**overrides), follow_redirects=follow_redirects)


def site_words(html: str) -> str:
    """The visible page text without the checkers' own words (blockquotes) and the member's task (panels)."""
    html = html.split("<main", 1)[1]
    html = re.sub(r"<blockquote>.*?</blockquote>", "", html, flags=re.S)
    html = re.sub(r'<div class="panel">.*?</div>', "", html, flags=re.S)
    html = re.sub(r"<code>.*?</code>", "", html, flags=re.S)  # fingerprints are hexadecimal
    return re.sub(r"<[^>]+>", " ", html)


def reports_of(rid: int) -> list[dict]:
    with db.session() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM check_reports WHERE request_id = ? ORDER BY id", (rid,))]


def request_row(rid: int) -> dict:
    with db.session() as conn:
        return dict(conn.execute("SELECT * FROM check_requests WHERE id = ?", (rid,)).fetchone())


def test_new_form_asks_who_should_check_with_both_chosen(asker):
    r = asker.get("/checks/new")
    assert "<legend>Who should check it</legend>" in r.text
    for label in ("Blind and low-vision people, with their own screen reader, braille display or magnifier", "Sighted people, to check how it looks",
                  "Both"):
        assert f">{label}</label>" in r.text
    assert re.search(r'name="wanted" value="both" checked', r.text)
    assert_accessible(r)


@pytest.mark.parametrize("kind", ["screen_reader", "sighted", "both"])
def test_new_form_prefills_the_kind_from_the_address(asker, kind):
    r = asker.get("/checks/new", params={"kind": kind, "url": "https://my.example/", "title": "My page"})
    assert re.search(rf'name="wanted" value="{kind}" checked', r.text)
    assert len(re.findall(r'name="wanted" value="[a-z_]+" checked', r.text)) == 1


def test_unknown_kind_in_the_address_falls_back_to_both(asker):
    r = asker.get("/checks/new", params={"kind": "<script>"})
    assert re.search(r'name="wanted" value="both" checked', r.text) and "<script>" not in r.text


def test_kind_is_stored_and_shown_in_the_queue_and_request_page(asker):
    make_request(asker, title="Look shop", wanted="sighted")
    rid = latest_request_id()
    assert request_row(rid)["wanted"] == "sighted"
    page = asker.get(f"/checks/{rid}")
    assert "This request asks sighted people to check how it looks." in page.text
    # The owner gets each address in its own labelled, read-only text box, to Tab to and copy.
    assert re.search(r'<label for="field-share_url">Address for sighted checkers</label>.*?<input id="field-share_url" '
                     rf'name="share_url" type="url" value="http://testserver/checks/{rid}"[^>]*readonly', page.text, re.S)
    assert re.search(r'<label for="field-record_url">Address of the check record</label>.*?<input id="field-record_url"'
                     rf' name="record_url" type="url" value="http://testserver/checks/{rid}/record"[^>]*readonly',
                     page.text, re.S)
    assert_accessible(page)
    make_request(asker, title="Reader shop", wanted="screen_reader")
    reader_rid = latest_request_id()
    reader_page = asker.get(f"/checks/{reader_rid}").text
    assert "Address for sighted checkers" not in reader_page and ">Address for checkers</label>" in reader_page
    sign_in_as(asker, SIGHTED, "Sam Sighted")
    queue = asker.get("/checks").text.split("<h2>Check someone's work</h2>")[1].split("<h2>")[0]
    # Who is wanted is part of each link's name.
    assert f'<a href="/checks/{rid}">{lbl(rid, "Look shop", "sighted")}</a>' in queue
    assert f'<a href="/checks/{reader_rid}">{lbl(reader_rid, "Reader shop")}</a>' in queue
    page = asker.get(f"/checks/{rid}")
    assert "This request asks sighted people to check how it looks." in page.text
    assert "field-share_url" not in page.text and "field-record_url" not in page.text  # only for the owner


def test_same_request_for_a_different_kind_is_a_new_request(asker):
    make_request(asker, wanted="screen_reader")
    make_request(asker, wanted="sighted")
    assert count("check_requests") == 2


def test_report_form_for_both_shows_both_parts_under_headings(asker):
    make_request(asker, wanted="both")
    rid = latest_request_id()
    sign_in_as(asker, SIGHTED, "Sam Sighted")
    r = asker.get(f"/checks/{rid}/report")
    assert "<legend>How did you check it?</legend>" in r.text
    assert ">With a screen reader, braille display or magnifier</label>" in r.text and ">I am sighted and looked at how it appears</label>" in r.text
    assert "<h3>If you used a screen reader, braille display or magnifier</h3>" in r.text
    assert "<h3>If you are sighted and looked at how it appears</h3>" in r.text
    for _, question, _ in checks_module.LOOK_QUESTIONS:
        assert f"<legend>{question}</legend>" in r.text
    assert "What would make it better?" in r.text and 'id="field-screen_reader"' in r.text
    # Neither part is announced as required, because each is needed only for its own kind of check.
    assert not re.search(r'id="field-(screen_reader|browser|device|heard_first)"[^>]*required', r.text)
    assert r.text.index("If you used a screen reader") < r.text.index("If you are sighted")
    assert_accessible(r)


def test_report_form_for_one_kind_shows_only_that_part(asker):
    make_request(asker, title="Look only", wanted="sighted")
    look_rid = latest_request_id()
    make_request(asker, title="Reader only", wanted="screen_reader")
    reader_rid = latest_request_id()
    sign_in_as(asker, SIGHTED, "Sam Sighted")
    r = asker.get(f"/checks/{look_rid}/report")
    assert "How did you check it?" not in r.text and 'id="field-screen_reader"' not in r.text
    assert "<h3>How it looks</h3>" in r.text and "Does it look finished and professional?" in r.text
    assert "This request asks sighted people to check how it looks." in r.text
    assert_accessible(r)
    r = asker.get(f"/checks/{reader_rid}/report")
    assert "How did you check it?" not in r.text and "Does it look finished" not in r.text
    assert "<h3>With your screen reader, braille display or magnifier</h3>" in r.text
    assert re.search(r'id="field-screen_reader"[^>]*required', r.text)
    assert_accessible(r)


def test_choosing_how_you_checked_is_required_when_both_are_wanted(asker):
    make_request(asker, wanted="both")
    rid = latest_request_id()
    sign_in_as(asker, SIGHTED, "Sam Sighted")
    r = post(asker, f"/checks/{rid}/report", {"looks_finished": "yes", "improve": "Kept text"})
    assert r.status_code == 400
    assert h1(r) == f"Please check 1 answer: Check this: {lbl(rid, wanted='both')}" == title_text(r)
    assert "Please check 1 answer" in r.text
    assert 'href="#field-method-1"' in r.text and 'id="field-method-1"' in r.text
    assert "Choose how you checked it" in r.text
    assert "Kept text" in r.text and re.search(r'name="looks_finished" value="yes" checked', r.text)
    assert_accessible(r)
    assert count("check_reports") == 0


def test_sighted_validation_asks_only_the_sighted_questions(asker):
    make_request(asker, wanted="both")
    rid = latest_request_id()
    sign_in_as(asker, SIGHTED, "Sam Sighted")
    r = post(asker, f"/checks/{rid}/report", {"method": "sighted", "would_trust": "maybe"})
    assert r.status_code == 400
    assert "Please check 3 answers" in r.text
    for name in ("looks_finished", "anything_broken", "would_trust"):
        assert f'href="#field-{name}-1"' in r.text
    assert "Choose whether it looks finished and professional: yes, no or not sure." in r.text
    for name in ("screen_reader", "browser", "device", "heard_first", "task_result"):
        assert f'href="#field-{name}' not in r.text  # the screen reader part is not needed
    assert re.search(r'name="method" value="sighted" checked', r.text)
    assert_accessible(r)
    r = send_sighted(asker, rid, improve="My test password: hunter22")
    assert r.status_code == 400 and 'href="#field-improve"' in r.text and "looks like it includes a password" in r.text
    r = send_sighted(asker, rid, improve="x" * 8001)
    assert r.status_code == 400 and "8000 characters or fewer" in r.text
    assert count("check_reports") == 0


def test_screen_reader_validation_does_not_ask_the_sighted_questions(asker):
    make_request(asker, wanted="both")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    r = send_report(asker, rid, method="screen_reader", browser="")
    assert r.status_code == 400 and "Please check 1 answer" in r.text and 'href="#field-browser"' in r.text
    r = send_report(asker, rid, method="screen_reader", follow_redirects=False)
    assert r.status_code == 303
    rep = reports_of(rid)[0]
    assert rep["method"] == "screen_reader" and rep["looks_finished"] == "" and rep["improve"] == ""


def test_sighted_check_is_saved_paid_and_keeps_only_its_own_fields(asker):
    credits.add(user_id(ASKER), 200, "test top-up")
    make_request(asker, wanted="both", max_checks="2", reward="50")
    rid = latest_request_id()
    sign_in_as(asker, SIGHTED, "Sam Sighted")
    # Things typed in the screen reader part are not saved with a sighted check.
    r = post(asker, f"/checks/{rid}/report", {**report_data(), **sighted_data()}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/checks/{rid}/record?sent=1"
    rep = reports_of(rid)[0]
    assert rep["method"] == "sighted"
    assert (rep["looks_finished"], rep["anything_broken"], rep["would_trust"]) == ("yes", "no", "yes")
    assert rep["improve"] == "A bigger order button would help on a phone."
    assert rep["screen_reader"] == rep["browser"] == rep["heard_first"] == rep["task_result"] == ""
    assert credits.balance(user_id(SIGHTED)) == 50  # rewards work the same
    record = asker.get(r.headers["location"])
    assert h1(record) == f"Your check is saved in the record: {lbl(rid, wanted='both')}"
    assert "$0.50 was added to your credits" in record.text
    # And the same rules: one check per person, and a second press saves and pays nothing more.
    assert asker.get(f"/checks/{rid}/report").status_code == 403
    r = send_sighted(asker, rid, looks_finished="no")
    assert "You sent this check before" in r.text and credits.balance(user_id(SIGHTED)) == 50
    assert reports_of(rid)[0]["looks_finished"] == "yes"


def test_a_request_for_one_kind_cannot_get_the_other_kind(asker):
    make_request(asker, wanted="sighted")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    r = send_report(asker, rid, method="screen_reader")  # a crafted form
    assert r.status_code == 400 and "Choose whether it looks finished" in r.text
    assert count("check_reports") == 0


def test_record_summary_for_sighted_checks_is_in_outcome_words(asker):
    make_request(asker, wanted="sighted", max_checks="3")
    rid = latest_request_id()
    sign_in_as(asker, SIGHTED, "Sam Sighted")
    send_sighted(asker, rid, improve="The pale grey text on white is hard to read.")
    sign_in_as(asker, THIRD, "Tomas")
    send_sighted(asker, rid, improve="")
    post(asker, "/logout")
    r = asker.get(f"/checks/{rid}/record")
    expected = ("2 sighted people checked how it looks. Both said it looks finished and professional, and neither "
                "found anything broken or hard to read. Both would trust it as a visitor or customer. Every answer "
                "was positive: to them, the page looks well made.")
    assert expected in r.text
    assert r.text.index(expected) < r.text.index("<h2>The checks</h2>")  # summary first
    assert "Nobody has tried the task" not in r.text  # it was not asked for
    # Each check shows its answers and the checker's own words, labelled as theirs.
    assert re.search(r"<h3[^>]*>Check 1: .*, looked at how it appears: looks well made</h3>", r.text)
    assert "<li>Does it look finished and professional? Yes.</li>" in r.text
    assert "<li>Is anything broken, overlapping or hard to read? No.</li>" in r.text
    assert "<li>Would you trust this page as a visitor or customer? Yes.</li>" in r.text
    assert "<h4>What would make it better, in their own words</h4>" in r.text
    assert "The pale grey text on white is hard to read." in r.text  # their words are never changed
    assert "Nothing written." in r.text
    not_checked = r.text.split("<h2>Not checked</h2>")[1]
    assert "How it looks was checked by 2 sighted people" in not_checked
    assert "Nobody tried the task with a screen reader, braille display or magnifier" in not_checked
    assert "Screen readers nobody used" not in not_checked
    # Everything the site itself says about the look is free of colour words.
    assert not COLOUR_WORDS.search(site_words(r.text)), COLOUR_WORDS.search(site_words(r.text))
    assert "Every fingerprint matches" in r.text
    assert_accessible(r)


def test_record_separates_the_two_kinds_screen_reader_first(asker):
    make_request(asker, wanted="both", max_checks="3")
    rid = latest_request_id()
    sign_in_as(asker, SIGHTED, "Sam Sighted")
    send_sighted(asker, rid, looks_finished="no", anything_broken="yes", would_trust="not_sure",
                 improve="The menu covers the prices on a phone.")
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid, method="screen_reader")
    r = asker.get(f"/checks/{rid}/record")
    leads = re.findall(r'<p class="lead">(.*?)</p>', r.text, re.S)
    assert leads[0] == "1 member tried the task with a screen reader, braille display or magnifier. 1 could do the task."
    assert leads[1] == ("1 sighted person checked how it looks. They said it does not look finished yet, and they "
                        "found something broken, overlapping or hard to read. They were not sure whether they would "
                        "trust it as a visitor or customer. There is something to fix. What they would make better "
                        "is below, in their own words.")
    assert re.search(r"Check 1: .*, looked at how it appears: found something to fix</h3>", r.text)
    assert re.search(r"Check 2: .*, with NVDA, could do the task</h3>", r.text)
    assert "<li>Would you trust this page as a visitor or customer? Not sure.</li>" in r.text
    not_checked = r.text.split("<h2>Not checked</h2>")[1]
    assert "How it looks was checked by 1 sighted person" in not_checked and "Screen readers nobody used" in not_checked
    assert not COLOUR_WORDS.search(site_words(r.text))
    assert_accessible(r)


def test_record_says_when_nobody_sighted_has_checked_yet(asker):
    make_request(asker, wanted="both")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid, method="screen_reader")
    r = asker.get(f"/checks/{rid}/record")
    assert "No sighted person has checked how it looks yet." in r.text
    assert "How it looks: no sighted person checked it" in r.text
    assert "This request asks blind and low-vision people to try the task with their own screen reader, braille display or magnifier, and sighted " \
           "people" in r.text
    assert not COLOUR_WORDS.search(site_words(r.text))


def _sighted(**answers):
    return {"looks_finished": "yes", "anything_broken": "no", "would_trust": "yes", **answers}


def test_look_summary_wording_for_mixed_answers():
    text = checks_module.look_summary([
        _sighted(), _sighted(looks_finished="no"), _sighted(looks_finished="not_sure", anything_broken="yes")])
    assert text == ("3 sighted people checked how it looks. 1 said it looks finished and professional, 1 said it "
                    "does not look finished yet and 1 was not sure. 2 found nothing broken or hard to read and 1 "
                    "found something broken, overlapping or hard to read. All 3 would trust it as a visitor or "
                    "customer. There is something to fix. What they would make better is below, in their own words.")
    text = checks_module.look_summary([_sighted()])
    assert text == ("1 sighted person checked how it looks. They said it looks finished and professional, and they "
                    "found nothing broken or hard to read. They would trust it as a visitor or customer. Every answer "
                    "was positive: to them, the page looks well made.")
    text = checks_module.look_summary([_sighted(), _sighted(), _sighted()])
    assert ("All 3 said it looks finished and professional, and none of the 3 found anything broken or hard to "
            "read. All 3 would trust it") in text
    text = checks_module.look_summary([_sighted(would_trust="not_sure"), _sighted(would_trust="not_sure")])
    assert "Neither was sure whether they would trust it as a visitor or customer." in text
    assert "Nobody found anything wrong, but not every answer was sure." in text
    assert checks_module.look_verdict(_sighted(would_trust="not_sure")) == "found nothing wrong, not sure of everything"
    assert checks_module.look_verdict(_sighted(anything_broken="yes")) == "found something to fix"
    assert checks_module.look_verdict(_sighted()) == "looks well made"


# ----- sighted checks and the fingerprints -----------------------------------------------------

def legacy_hash(request_row: dict, report: dict, prev_hash: str) -> str:
    """The fingerprint exactly as it was computed before sighted checks existed."""
    import hashlib
    import json
    fields = ("checker_id", "screen_reader", "screen_reader_version", "browser", "device", "heard_first",
              "task_result", "problems", "show_name", "created_at")
    payload = {k: report[k] for k in fields}
    payload["show_name"] = int(payload["show_name"])
    payload["checker_id"] = int(payload["checker_id"])
    payload.update({"request_id": int(request_row["id"]), "url": request_row["url"], "title": request_row["title"],
                    "what_to_try": request_row["what_to_try"], "prev_hash": prev_hash})
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_screen_reader_checks_hash_exactly_as_before(asker):
    make_request(asker, wanted="both")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid, method="screen_reader")
    rep, req = reports_of(rid)[0], request_row(rid)
    assert rep["record_hash"] == legacy_hash(req, rep, "")
    # An old-format entry, with none of the new fields at all, still verifies.
    old = {k: v for k, v in rep.items() if k not in checks_module.SIGHTED_FIELDS}
    assert checks_module.compute_hash(req, old, "") == rep["record_hash"]
    assert checks_module.verify_chain(req, [old]) == []


def test_sighted_answers_are_in_the_fingerprint(asker):
    make_request(asker, wanted="both", max_checks="3")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid, method="screen_reader")
    sign_in_as(asker, SIGHTED, "Sam Sighted")
    send_sighted(asker, rid, anything_broken="yes")
    rows, req = reports_of(rid), request_row(rid)
    assert rows[1]["prev_hash"] == rows[0]["record_hash"]
    assert rows[1]["record_hash"] != legacy_hash(req, rows[1], rows[0]["record_hash"])  # new fields are covered
    assert checks_module.verify_chain(req, rows) == []
    for column, value in (("anything_broken", "no"), ("improve", "Nothing to change."), ("method", "screen_reader"),
                          ("would_trust", "not_sure"), ("looks_finished", "no")):
        tampered = [dict(r) for r in rows]
        tampered[1][column] = value
        assert checks_module.verify_chain(req, tampered) == [
            "Entry 2 does not match its fingerprint. It was changed after it was written."], column
    # Turning an old screen reader entry into a sighted one is detected too.
    tampered = [dict(r) for r in rows]
    tampered[0].update(method="sighted", looks_finished="yes", anything_broken="no", would_trust="yes")
    assert "Entry 1 does not match its fingerprint. It was changed after it was written." in \
        checks_module.verify_chain(req, tampered)


def test_tampering_with_a_sighted_answer_shows_on_the_record(asker):
    make_request(asker, wanted="sighted")
    rid = latest_request_id()
    sign_in_as(asker, SIGHTED, "Sam Sighted")
    send_sighted(asker, rid, anything_broken="yes")
    with db.session() as conn:
        conn.execute("UPDATE check_reports SET anything_broken = 'no' WHERE request_id = ?", (rid,))
    r = asker.get(f"/checks/{rid}/record")
    assert "This record was changed after it was written" in r.text
    assert "Entry 1 does not match its fingerprint" in r.text and "Every fingerprint matches" not in r.text
    assert_accessible(r)


OLD_TABLES = """
CREATE TABLE check_requests (
    id INTEGER PRIMARY KEY,
    requester_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    what_to_try TEXT NOT NULL,
    max_checks INTEGER NOT NULL,
    reward_cents INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE check_reports (
    id INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES check_requests(id),
    checker_id INTEGER NOT NULL REFERENCES users(id),
    screen_reader TEXT NOT NULL,
    screen_reader_version TEXT NOT NULL DEFAULT '',
    browser TEXT NOT NULL,
    device TEXT NOT NULL,
    heard_first TEXT NOT NULL,
    task_result TEXT NOT NULL CHECK (task_result IN ('completed', 'partly', 'could_not')),
    problems TEXT NOT NULL DEFAULT '',
    show_name INTEGER NOT NULL DEFAULT 0,
    prev_hash TEXT NOT NULL DEFAULT '',
    record_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (request_id, checker_id)
);
CREATE INDEX idx_check_reports_checker ON check_reports(checker_id);
"""


def test_a_database_from_before_sighted_checks_is_upgraded_and_still_verifies(asker):
    """A site that already has check records: start-up adds the new columns, keeps every row and fingerprint."""
    sign_in_as(asker, CHECKER, "Chidi")
    sign_in_as(asker, THIRD, "Tomas")
    with db.session() as conn:
        conn.execute("DROP TABLE check_reports")
        conn.execute("DROP TABLE check_requests")
        conn.executescript(OLD_TABLES)
    req = {"id": 7, "title": "Old cafe", "url": "https://old.example/", "what_to_try": "Find the menu."}
    rows, prev = [], ""
    for n, email in enumerate((CHECKER, THIRD), start=1):
        rep = {"checker_id": user_id(email), "screen_reader": "NVDA", "screen_reader_version": "", "browser": "Firefox",
               "device": "Windows laptop", "heard_first": f"Old cafe {n}", "task_result": "completed",
               "problems": "", "show_name": 0, "created_at": f"2026-01-0{n}T10:00:00+00:00"}
        rep["prev_hash"], rep["record_hash"] = prev, legacy_hash(req, rep, prev)
        prev = rep["record_hash"]
        rows.append(rep)
    with db.session() as conn:
        conn.execute("INSERT INTO check_requests (id, requester_id, title, url, what_to_try, max_checks, status,"
                     " created_at) VALUES (7, ?, 'Old cafe', 'https://old.example/', 'Find the menu.', 3, 'open',"
                     " '2026-01-01T09:00:00+00:00')", (user_id(ASKER),))
        for rep in rows:
            cols = ", ".join(rep)
            conn.execute(f"INSERT INTO check_reports (request_id, {cols}) VALUES (7, {', '.join('?' * len(rep))})",
                         tuple(rep.values()))
    checks_module.seed()
    checks_module.seed()  # safe to run on every start
    upgraded = reports_of(7)
    assert [r["record_hash"] for r in upgraded] == [r["record_hash"] for r in rows]
    assert all(r["method"] == "screen_reader" and r["improve"] == "" for r in upgraded)
    assert request_row(7)["wanted"] == "screen_reader"  # old requests asked screen reader users
    assert checks_module.verify_chain(request_row(7), upgraded) == []
    r = asker.get("/checks/7/record")
    assert "Every fingerprint matches" in r.text
    assert "2 members tried the task with a screen reader, braille display or magnifier. 2 could do the task." in r.text
    assert "sighted person has checked" not in r.text
    # The upgraded table takes sighted checks, and keeps its index.
    with db.session() as conn:
        conn.execute("UPDATE check_requests SET wanted = 'both' WHERE id = 7")
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_check_reports_checker'").fetchone()
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'check_reports_new'").fetchone()
    sign_in_as(asker, SIGHTED, "Sam Sighted")
    assert send_sighted(asker, 7, follow_redirects=False).status_code == 303
    assert checks_module.verify_chain(request_row(7), reports_of(7)) == []


# ----- ease of use: names, new checks, the way back to the work --------------------------------

def test_two_requests_for_the_same_page_have_different_names(asker):
    """Two requests for one build version: every link and title says which request and who it is for."""
    make_request(asker, title="Choir rota, version 2", wanted="screen_reader")
    first = latest_request_id()
    make_request(asker, title="Choir rota, version 2", wanted="sighted")
    second = latest_request_id()
    index = asker.get("/checks").text
    names = re.findall(r'<a href="/checks/\d+(?:/record)?">(.*?)</a>', index)
    assert len(names) == len(set(names)), names
    assert f'<a href="/checks/{first}/record">Check record: {lbl(first, "Choir rota, version 2")}</a>' in index
    assert f'<a href="/checks/{second}/record">Check record: {lbl(second, "Choir rota, version 2", "sighted")}</a>' \
        in index
    titles = {title_text(asker.get(f"/checks/{rid}{end}")) for rid in (first, second) for end in ("", "/record")}
    assert len(titles) == 4


def test_request_and_record_pages_say_who_asked_and_when_first(asker):
    make_request(asker)
    rid = latest_request_id()
    with db.session() as conn:
        code = conn.execute("SELECT pseudonym FROM users WHERE email = ?", (ASKER,)).fetchone()["pseudonym"]
    record = asker.get(f"/checks/{rid}/record").text.split("</h1>")[1]
    first_paragraph = re.search(r"<p>(.*?)</p>", record, re.S).group(1)
    assert first_paragraph.startswith(f"Asked by {code} on ")
    assert "0 of 3 checks done; the request is open." in first_paragraph
    own = asker.get(f"/checks/{rid}").text.split("</h1>")[1]
    assert re.search(r'<p class="lead">Asked by you on \d{1,2} [A-Z][a-z]+ \d{4}\.', own)
    sign_in_as(asker, CHECKER, "Chidi")
    other = asker.get(f"/checks/{rid}").text.split("</h1>")[1]
    assert re.search(rf'<p class="lead">Asked by {code} on .*?; the request is open\.', other, re.S)


def test_dates_are_in_words(asker):
    make_request(asker)
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid)
    for path in ("/checks", f"/checks/{rid}", f"/checks/{rid}/record"):
        main = asker.get(path).text.split("<main", 1)[1]
        assert not re.search(r"\d{4}-\d{2}-\d{2}", main), path


def test_new_checks_are_listed_until_the_record_is_opened(asker):
    make_request(asker, max_checks="3")
    rid = latest_request_id()
    assert "New checks on your requests" not in asker.get("/checks").text
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid)
    sign_in_as(asker, THIRD, "Tomas")
    send_report(asker, rid)
    assert "New checks on your requests" not in asker.get("/checks").text  # only for the member who asked
    sign_in_as(asker, ASKER, "Asha")
    index = asker.get("/checks").text
    assert "<h2>New checks on your requests (2)</h2>" in index
    assert index.index("New checks on your requests") < index.index("<h2>Check someone's work</h2>")
    assert f'<a href="/checks/{rid}/record">Check record: {lbl(rid)}</a>. 2 new checks.' in index
    assert_accessible(asker.get("/checks"))
    asker.get(f"/checks/{rid}/record")
    assert "New checks on your requests" not in asker.get("/checks").text
    sign_in_as(asker, "fourth@example.com", "Femi")
    send_report(asker, rid)
    sign_in_as(asker, ASKER, "Asha")
    assert "<h2>New checks on your requests (1)</h2>" in asker.get("/checks").text


def test_after_sending_the_next_request_is_one_link_away(asker):
    make_request(asker, title="First shop")
    first = latest_request_id()
    make_request(asker, title="Second shop")
    second = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    r = send_report(asker, first)
    assert f'<a class="button button-primary" href="/checks/{second}">Check another request (1 waiting)</a>' in r.text
    assert_accessible(r)
    r = send_report(asker, second)
    assert "Check another request" not in r.text and "No other requests are waiting for you right now." in r.text
    assert "Check another request" not in asker.get(f"/checks/{first}/record").text  # only right after sending


def test_fingerprints_come_after_the_checks_with_one_line_at_the_top(asker):
    make_request(asker)
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid)
    r = asker.get(f"/checks/{rid}/record")
    fingerprint = reports_of(rid)[0]["record_hash"]
    assert r.text.index("Record intact.") < r.text.index("<h2>What was asked</h2>")
    about = r.text.index("<h2>About the fingerprints</h2>")
    assert r.text.index("<h2>The checks</h2>") < r.text.index("<h2>Not checked</h2>") < about
    assert r.text.index(fingerprint) > about
    assert f"<li>Check 1: <code>{fingerprint}</code></li>" in r.text
    assert "Fingerprints, for checking the record only (64 characters each)" in r.text


def test_words_are_kept_when_the_request_closes_while_writing(asker):
    make_request(asker, max_checks="1")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    asker.get(f"/checks/{rid}/report")
    with db.session() as conn:
        conn.execute("UPDATE check_requests SET status = 'closed' WHERE id = ?", (rid,))
    r = send_report(asker, rid, problems="The basket button has no name.\nThe menu is not a list.")
    assert r.status_code == 403
    assert h1(r) == f"This check is not open to you: {lbl(rid)}" == title_text(r)
    assert "<h2>What you wrote</h2>" in r.text
    assert "The basket button has no name.<br>The menu is not a list." in r.text
    assert "Cake shop, heading level 1" in r.text
    assert_accessible(r)
    assert count("check_reports") == 0


def make_build_version(owner_email: str, token: str = "tok123", number: int = 2) -> int:
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO build_projects (user_id, title, kind, idea, status, share_token, created_at, updated_at)"
            " VALUES (?, 'Choir rota', 'website', 'A rota', 'ready', ?, ?, ?)",
            (user_id(owner_email), token, db.now(), db.now()))
        project_id = cur.lastrowid
        for n in range(1, number + 1):
            conn.execute("INSERT INTO build_versions (project_id, number, files_json, created_at) VALUES (?, ?, ?, ?)",
                         (project_id, n, '{"index.html": "<!doctype html><title>Rota</title><h1>Rota</h1>"}', db.now()))
    return project_id


def test_record_of_a_build_preview_offers_to_fix_what_checkers_found(asker):
    project_id = make_build_version(ASKER)
    make_request(asker, title="Choir rota, version 2", url="http://testserver/build/p/tok123/v/2/", wanted="both")
    rid = latest_request_id()
    r = asker.get(f"/checks/{rid}/record")
    assert "Ask the AI to fix what checkers found" not in r.text  # nothing found yet
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid, method="screen_reader", task_result="partly", problems="The rota table has no headers.")
    sign_in_as(asker, SIGHTED, "Sam Sighted")
    send_sighted(asker, rid, anything_broken="yes", improve="The names overlap on a phone.")
    assert "Ask the AI to fix what checkers found" not in asker.get(f"/checks/{rid}/record").text  # not their project
    sign_in_as(asker, ASKER, "Asha")
    r = asker.get(f"/checks/{rid}/record")
    assert "<h2>Ask the AI to fix what checkers found</h2>" in r.text
    form = r.text.split("<h2>Ask the AI to fix what checkers found</h2>")[1].split("</form>")[0]
    assert f'action="/build/{project_id}/v/2/change"' in form and 'name="csrf_token"' in form
    assert 'name="request"' in form and ">The change you want" in form
    assert "Peer checkers tried version 2." in form
    assert "Check 1, with NVDA on Firefox, Windows laptop: could do it partly." in form
    assert "The rota table has no headers." in form and "The names overlap on a phone." in form
    assert '<button type="submit" class="button button-primary">Ask the AI to fix what checkers found</button>' in form
    assert f'<a href="/build/{project_id}/v/2">Go to version 2 in Build</a>' in r.text
    assert_accessible(r)
    # The form reaches Build's own change form: an empty request comes back there as a form error.
    back = post(asker, f"/build/{project_id}/v/2/change", {"request": ""})
    assert back.status_code == 400 and "Describe the change you want." in back.text


def test_fix_offer_needs_the_sites_own_preview_address(asker):
    make_build_version(ASKER)
    make_request(asker, url="https://elsewhere.example/build/p/tok123/v/2/")
    rid = latest_request_id()
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, rid)
    sign_in_as(asker, ASKER, "Asha")
    assert "Ask the AI to fix what checkers found" not in asker.get(f"/checks/{rid}/record").text


def test_fix_request_text_keeps_checkers_words_and_skips_clean_checks():
    req = {"what_to_try": "Find Sunday's singers."}
    clean = {"method": "screen_reader", "task_result": "completed", "problems": "", "screen_reader": "JAWS",
             "browser": "Edge", "device": "PC"}
    assert checks_module.fix_request_text(req, [clean], 3) == ""
    found = {**clean, "task_result": "could_not", "problems": "No heading for Sunday."}
    text = checks_module.fix_request_text(req, [clean, found], 3)
    assert "Check 1" not in text and "Check 2, with JAWS on Edge, PC: could not do the task." in text
    assert "No heading for Sunday." in text and "Find Sunday's singers." in text
    long = {**found, "problems": "x" * 20_000}
    assert len(checks_module.fix_request_text(req, [long], 3)) < 10_000


def test_build_can_list_and_close_the_requests_for_its_previews(asker):
    uid = user_id(ASKER)
    credits.add(uid, 500, "test top-up")
    prefix = "http://testserver/build/p/tok123/"
    make_request(asker, title="Rota v1", url=prefix + "v/1/", max_checks="2", reward="100")
    v1 = latest_request_id()
    make_request(asker, title="Rota v2", url=prefix + "v/2/", wanted="sighted")
    v2 = latest_request_id()
    make_request(asker, title="Other", url="http://testserver/build/p/other/v/1/")
    sign_in_as(asker, CHECKER, "Chidi")
    send_report(asker, v1)
    found = checks_module.requests_for_address(prefix)
    assert [r["id"] for r in found] == [v2, v1]
    assert found[1]["label"] == lbl(v1, "Rota v1") and found[1]["record_url"] == f"/checks/{v1}/record"
    assert (found[1]["done"], found[1]["max_checks"], found[1]["status"]) == (1, 2, "open")
    assert [r["id"] for r in checks_module.requests_for_address(prefix + "v/2/")] == [v2]
    assert checks_module.requests_for_address("") == []
    assert credits.balance(uid) == 300
    assert found[1]["unpaid_cents"] == 100 and found[0]["unpaid_cents"] == 0
    assert checks_module.close_requests_for_address(prefix) == (2, 100)  # (requests closed, cents returned)
    assert checks_module.close_requests_for_address(prefix) == (0, 0)  # safe to call twice
    assert checks_module.requests_for_address(prefix, open_only=True) == []
    assert credits.balance(uid) == 400  # the one check not done came back
    assert ledger_total_for(v1) == 0
    assert len(checks_module.requests_for_address("http://testserver/build/p/other/", open_only=True)) == 1


def test_build_can_match_a_preview_path_on_any_domain_for_one_member_inside_its_own_transaction(asker):
    uid = user_id(ASKER)
    credits.add(uid, 500, "test top-up")
    make_request(asker, title="Rota", url="https://previews.example/build/p/tok9/v/1/index.html", reward="100",
                 max_checks="2")
    mine = latest_request_id()
    make_request(asker, title="Not a preview", url="https://elsewhere.example/notes?from=/build/p/tok9/v/1/")
    sign_in_as(asker, CHECKER, "Chidi")
    make_request(asker, title="Someone else's", url="https://previews.example/build/p/tok9/v/1/")
    theirs = latest_request_id()
    path = "/build/p/tok9/v/"
    assert [r["id"] for r in checks_module.requests_for_address(path)] == [theirs, mine]
    assert [r["id"] for r in checks_module.requests_for_address(path, requester_id=uid)] == [mine]
    assert checks_module.requests_for_address("/build/p/tok9/v/2/") == []
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert checks_module.close_requests_for_address(path, requester_id=uid, conn=conn) == (1, 200)
        conn.rollback()  # the caller's transaction decides: nothing was closed or returned
    assert credits.balance(uid) == 300
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert checks_module.close_requests_for_address(path, requester_id=uid, conn=conn) == (1, 200)
    assert credits.balance(uid) == 500
    assert [r["id"] for r in checks_module.requests_for_address(path, open_only=True)] == [theirs]
