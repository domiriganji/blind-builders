import re

from studio import db
from studio.features import recovery

from .conftest import post, user_id

# The code is in a read-only text box, so it can be selected and copied with a screen reader or by touch.
CODE_RE = re.compile(r'<input id="recovery-code" type="text" class="code-box" value="([A-Z2-9]{4}(?:-[A-Z2-9]{4}){3})" readonly')


def _sign_up(client, email="ola@example.com", next_url="/"):
    r = post(client, "/register", {"email": email, "display_name": "Ola", "password": "correct horse battery",
                                   "next": next_url})
    assert r.status_code == 200
    return r


def test_sign_up_shows_the_code_until_it_is_saved_and_continues(client):
    r = _sign_up(client, next_url="/write")
    assert "<h1>Your account is ready: save your recovery code</h1>" in r.text
    code = CODE_RE.search(r.text).group(1)
    assert "Spelled out" in r.text and ", ".join(code[:4]) in r.text
    assert '<label for="recovery-code">Your recovery code</label>' in r.text
    assert "This is the same code as before" not in r.text
    # A reload (or a phone reloading the tab) shows the same code again, and says so.
    again = client.get("/account/recovery-code")
    assert CODE_RE.search(again.text).group(1) == code and "This is the same code as before." in again.text
    # It can be saved as a small text file, without JavaScript.
    saved = client.get("/account/recovery-code.txt")
    assert saved.status_code == 200 and code in saved.text and "attachment" in saved.headers["content-disposition"]
    assert saved.headers["cache-control"] == "no-store"
    # Saying it is saved continues to where the member was going, and the code is never shown again.
    r = post(client, "/account/recovery-code/saved", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/write"
    after = client.get("/account/recovery-code")
    assert code not in after.text and "You have a recovery code" in after.text
    assert client.get("/account/recovery-code.txt", follow_redirects=False).status_code == 303
    with db.session() as conn:
        stored = conn.execute("SELECT recovery_hash FROM users").fetchone()[0]
    assert stored and code not in stored and recovery.normalize(code) not in stored


def test_reset_with_the_code_works_once(client):
    code = CODE_RE.search(_sign_up(client).text).group(1)
    post(client, "/logout")
    r = post(client, "/reset-password", {"email": "OLA@example.com", "code": code.lower().replace("-", " "),
                                         "password": "a brand new password"})
    assert r.status_code == 200 and "Your password is changed: save your new recovery code" in r.text
    new_code = CODE_RE.search(r.text).group(1)
    assert new_code != code
    r = post(client, "/account/recovery-code/saved")
    assert "<h1>Your password is changed and you are signed in</h1>" in r.text
    post(client, "/logout")
    assert post(client, "/login", {"email": "ola@example.com", "password": "a brand new password"}).status_code == 200
    post(client, "/logout")
    reused = post(client, "/reset-password", {"email": "ola@example.com", "code": code, "password": "another password!"})
    assert reused.status_code == 400 and "not right" in reused.text


def test_wrong_codes_and_unknown_emails_get_the_same_answer(client):
    _sign_up(client)
    post(client, "/logout")
    wrong = post(client, "/reset-password", {"email": "ola@example.com", "code": "AAAA-BBBB-CCCC-DDDD",
                                             "password": "whatever password"})
    unknown = post(client, "/reset-password", {"email": "nobody@example.com", "code": "AAAA-BBBB-CCCC-DDDD",
                                               "password": "whatever password"})
    for r in (wrong, unknown):
        assert r.status_code == 400 and "The email address or recovery code is not right" in r.text
        assert 'href="#field-code"' in r.text


def test_guessing_is_limited(client):
    code = CODE_RE.search(_sign_up(client).text).group(1)
    post(client, "/logout")
    for _ in range(recovery.MAX_TRIES_PER_HOUR):
        post(client, "/reset-password", {"email": "ola@example.com", "code": "AAAA-BBBB-CCCC-DDDD", "password": "x" * 12})
    r = post(client, "/reset-password", {"email": "ola@example.com", "code": code, "password": "y" * 12})
    assert r.status_code == 429 and "resetting is paused for one hour" in r.text and "Your account is unchanged" in r.text


def test_validation_messages(client):
    r = post(client, "/reset-password", {"email": "", "code": "", "password": "short"})
    assert r.status_code == 400
    for field in ("email", "code", "password"):
        assert f'href="#field-{field}"' in r.text
    # The code is kept to correct a typo; the emptied password box is named in the list.
    r = post(client, "/reset-password", {"email": "", "code": "abcd-efgh", "password": "long enough password"})
    assert 'value="abcd-efgh"' in r.text and "Please type your new password again." in r.text
    assert 'autocapitalize="characters"' in r.text and 'autocorrect="off"' in r.text
    assert "After this you are signed in and get a new recovery code to save." in r.text


def test_make_a_new_code_from_the_account_page(user_client):
    post(user_client, "/account/recovery-code/saved")  # the sign-up code is saved
    with db.session() as conn:
        conn.execute("UPDATE users SET recovery_hash = NULL")
    page = user_client.get("/account")
    assert "You have no recovery code yet" in page.text
    assert "You have no recovery code yet" in user_client.get("/account/recovery-code").text
    r = post(user_client, "/account/recovery-code", {"password": "correct horse battery"})
    assert "Your new recovery code: save it now" in r.text and CODE_RE.search(r.text)
    r = post(user_client, "/account/recovery-code/saved")
    assert "<h1>Your new recovery code is ready</h1>" in r.text
    assert "You have a recovery code" in r.text


def test_deleted_accounts_cannot_be_recovered(client):
    code = CODE_RE.search(_sign_up(client).text).group(1)
    post(client, "/account/delete", {"confirm": "yes"})
    r = post(client, "/reset-password", {"email": "ola@example.com", "code": code, "password": "new password here"})
    assert r.status_code == 400


def test_signed_in_members_are_told_where_to_change_the_password(user_client):
    r = user_client.get("/reset-password")
    assert r.status_code == 200 and "<h1>You are signed in</h1>" in r.text
    assert "you do not need your recovery code" in r.text and 'href="/account/password"' in r.text
    assert 'action="/reset-password"' not in r.text


def test_login_page_links_to_reset(client):
    assert 'href="/reset-password"' in client.get("/login").text


def test_site_owner_can_set_a_password(user_client, capsys):
    from studio import manage
    assert manage.set_password("amina@example.com", "owner set password") == 0
    assert manage.set_password("nobody@example.com", "owner set password") == 1
    assert manage.set_password("amina@example.com", "short") == 1
    post(user_client, "/logout")
    r = post(user_client, "/login", {"email": "amina@example.com", "password": "owner set password"})
    assert r.status_code == 200
    assert user_id() > 0
