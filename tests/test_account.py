"""Your account and How paying works: profile, own key, credits, buying, webhook, admin, manage.py, delete."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studio import ai as ai_module
from studio import credits, db, manage
from studio.a11y_check import check_html
from studio.features import account

from .conftest import post, register, user_id

GOOD_KEY = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0" * 4 + "WXYZ"
WEBHOOK_SECRET = "whsec_test_secret"


def h1(html: str) -> str:
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    assert m, "no h1"
    return re.sub(r"\s+", " ", m.group(1)).strip()


def assert_accessible(response) -> None:
    assert response.status_code in (200, 400), response.status_code
    report = check_html(response.text)
    assert report.errors == [], [(f.rule, f.message, f.where) for f in report.errors]
    assert len(re.findall(r"<h1[ >]", response.text)) == 1
    title = re.search(r"<title>(.*?) – ", response.text).group(1)
    assert h1(response.text) == title


def user_row(email: str = "amina@example.com") -> dict:
    with db.session() as conn:
        return dict(conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone())


def make_admin(email: str = "amina@example.com") -> None:
    with db.session() as conn:
        conn.execute("UPDATE users SET is_admin = 1 WHERE email = ?", (email,))


@pytest.fixture
def key_ok(monkeypatch):
    seen = []

    def fake_verify(key):
        seen.append(key)
        return True

    monkeypatch.setattr(account, "verify_api_key", fake_verify)
    return seen


# ----- pages and links --------------------------------------------------------------------------

def test_account_requires_sign_in(client):
    for path in ["/account", "/account/profile", "/account/ai-key", "/account/credits", "/account/credits/buy",
                 "/account/delete", "/account/admin"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path


def test_account_summary_and_links(user_client):
    r = user_client.get("/account")
    assert r.status_code == 200
    assert h1(r.text) == "Your account"
    assert "Amina" in r.text and "amina@example.com" in r.text
    assert user_row()["pseudonym"] in r.text
    assert "The AI is not set up for you yet" in r.text
    assert 'href="/account/ai-key"' in r.text
    for link in ["/account/profile", "/account/ai-key", "/account/credits", "/account/credits/buy", "/pricing",
                 "/account/delete"]:
        assert f'href="{link}"' in r.text
    assert 'href="/account/admin"' not in r.text
    # Summary comes before the links.
    assert r.text.index("How you pay for AI") < r.text.index("Change your details")
    assert_accessible(r)


def test_every_account_page_is_accessible(user_client):
    for path in ["/account", "/account/profile", "/account/ai-key", "/account/credits", "/account/credits/buy",
                 "/account/delete", "/pricing"]:
        assert_accessible(user_client.get(path))


def test_account_shows_credits_mode(user_client, app):
    app.state.settings.anthropic_api_key = "sk-ant-site-key"
    credits.add(user_id(), 320, "Welcome credits")
    r = user_client.get("/account")
    assert "You use credits. Balance: <strong>$3.20</strong>" in r.text


def test_credits_without_site_key_are_not_usable(user_client):
    credits.add(user_id(), 320, "Welcome credits")
    r = user_client.get("/account")
    assert "The AI is not set up for you yet" in r.text
    assert "does not offer credits yet" in r.text


def test_no_ai_app_account_explains_both_ways(no_ai_app):
    c = TestClient(no_ai_app)
    register(c)
    r = c.get("/account")
    assert r.status_code == 200
    assert "The AI is not set up for you yet" in r.text
    assert "Connect your own Claude API key" in r.text
    assert "credits" in r.text
    assert c.get("/account/credits").status_code == 200
    assert c.get("/pricing").status_code == 200


# ----- profile ----------------------------------------------------------------------------------

def test_profile_shows_code_name_and_current_values(user_client):
    r = user_client.get("/account/profile")
    assert h1(r.text) == "Your profile and preferences"
    assert user_row()["pseudonym"] in r.text
    assert 'value="NVDA"' in r.text
    assert "only helps match" in r.text
    assert "Summary first, then details" in r.text and "Full detail" in r.text
    assert "Show my name on peer checks instead of my code name" in r.text


def test_profile_edit_saves(user_client):
    r = post(user_client, "/account/profile", {
        "display_name": "Amina B", "screen_reader": "TalkBack", "device": "Android phone", "language": "ha",
        "sight_onset": "later", "verbosity": "full", "show_name_on_checks": "yes"})
    assert r.status_code == 200 and "Your profile is saved." in r.text
    row = user_row()
    assert (row["display_name"], row["screen_reader"], row["device"], row["language"], row["sight_onset"],
            row["verbosity"], row["show_name_on_checks"]) == (
        "Amina B", "TalkBack", "Android phone", "ha", "later", "full", 1)
    page = user_client.get("/account/profile").text
    assert '<option value="later" selected>' in page
    assert 'value="full" checked' in page
    assert 'name="show_name_on_checks" value="yes" checked' in page
    # Unticking the box turns it off again.
    post(user_client, "/account/profile", {"display_name": "Amina B", "language": "en", "sight_onset": "prefer_not",
                                           "verbosity": "summary_first"})
    assert user_row()["show_name_on_checks"] == 0


def test_profile_validation_errors(user_client):
    r = post(user_client, "/account/profile", {
        "display_name": "", "screen_reader": "x" * 150, "language": "klingon", "sight_onset": "sometimes",
        "verbosity": "loud"})
    assert r.status_code == 400
    assert h1(r.text) == "Your profile and preferences: please fix the problems"
    for name in ["display_name", "screen_reader", "language", "sight_onset", "verbosity"]:
        assert f'href="#field-{name}"' in r.text, name
    assert 'aria-invalid="true"' in r.text
    assert user_row()["display_name"] == "Amina"
    assert_accessible(r)


def test_profile_needs_csrf(user_client):
    r = user_client.post("/account/profile", data={"display_name": "Hacker"})
    assert r.status_code == 400
    assert user_row()["display_name"] == "Amina"


# ----- own API key ------------------------------------------------------------------------------

def test_key_field_is_a_password_field_without_autocomplete(user_client):
    r = user_client.get("/account/ai-key")
    assert re.search(r'<input id="field-api_key" name="api_key" type="password" value="" required autocomplete="off"', r.text)
    assert "console.anthropic.com" in r.text and "encrypted" in r.text and "never show it again" in r.text


@pytest.mark.parametrize("bad, message", [
    ("", "Paste your Claude API key."),
    ("sk-ant-abc def" + "x" * 40, "space"),
    ("sk-proj-" + "x" * 50, "starts with sk-ant-"),
    ("sk-ant-short", "too short"),
    ("sk-ant-" + "x" * 400, "too long"),
    ("sk-ant-" + "x" * 40 + "!$", "characters"),
])
def test_key_format_errors(user_client, key_ok, bad, message):
    r = post(user_client, "/account/ai-key", {"api_key": bad})
    assert r.status_code == 400
    assert message in r.text
    assert h1(r.text) == "Your Claude API key: please fix the problems"
    assert 'href="#field-api_key"' in r.text
    assert key_ok == []  # never sent to Anthropic
    assert user_row()["own_api_key_enc"] is None
    if bad:
        assert bad not in r.text  # the key is never echoed back


def test_key_verify_failure_message(user_client, monkeypatch):
    monkeypatch.setattr(account, "verify_api_key",
                        lambda key: "Anthropic says this key is not valid. It may have been deleted.")
    r = post(user_client, "/account/ai-key", {"api_key": GOOD_KEY})
    assert r.status_code == 400
    assert "Anthropic says this key is not valid" in r.text
    assert GOOD_KEY not in r.text
    assert user_row()["own_api_key_enc"] is None
    assert_accessible(r)


def test_key_is_stored_encrypted_and_only_last_four_shown(user_client, key_ok, settings):
    r = post(user_client, "/account/ai-key", {"api_key": "  " + GOOD_KEY + "  "})
    assert r.status_code == 200
    assert key_ok == [GOOD_KEY]
    assert "ending in WXYZ is connected" in r.text
    assert "You use your own Claude API key ending in <strong>WXYZ</strong>" in r.text
    assert GOOD_KEY not in r.text
    stored = user_row()["own_api_key_enc"]
    assert stored and GOOD_KEY not in stored
    raw = Path(settings.db_path).read_bytes()
    for wal in Path(settings.db_path).parent.glob("*.db-wal"):
        raw += wal.read_bytes()
    assert GOOD_KEY.encode() not in raw
    from studio.security import decrypt_secret
    assert decrypt_secret(settings.secret_key, stored) == GOOD_KEY
    page = user_client.get("/account/ai-key")
    assert "WXYZ" in page.text and GOOD_KEY not in page.text and GOOD_KEY[:-4] not in page.text
    assert "Remove my key" in page.text
    assert_accessible(page)


def test_remove_key(user_client, key_ok):
    post(user_client, "/account/ai-key", {"api_key": GOOD_KEY})
    r = post(user_client, "/account/ai-key/remove")
    assert r.status_code == 200 and "removed" in r.text
    assert user_row()["own_api_key_enc"] is None
    assert "The AI is not set up for you yet" in user_client.get("/account").text
    assert "Remove my key" not in user_client.get("/account/ai-key").text


def test_remove_key_needs_csrf(user_client, key_ok):
    post(user_client, "/account/ai-key", {"api_key": GOOD_KEY})
    r = user_client.post("/account/ai-key/remove")
    assert r.status_code == 400
    assert user_row()["own_api_key_enc"] is not None


def test_unreadable_key_is_explained(user_client):
    with db.session() as conn:
        conn.execute("UPDATE users SET own_api_key_enc = 'garbage' WHERE email = 'amina@example.com'")
    assert "can no longer be read" in user_client.get("/account").text
    assert "can no longer be read" in user_client.get("/account/ai-key").text


def test_default_factory_uses_own_key(user_client, key_ok, app):
    post(user_client, "/account/ai-key", {"api_key": GOOD_KEY})
    settings = app.state.settings
    client = ai_module.default_factory(settings)(user_row())
    assert client.paid_by == "own_key"
    assert client.user_id == user_id()


def test_default_factory_without_key_explains(user_client, app):
    with pytest.raises(ai_module.AINotAvailable):
        ai_module.default_factory(app.state.settings)(user_row())


def test_verify_api_key_reports_failures(monkeypatch):
    import anthropic
    import httpx

    class FakeModels:
        def __init__(self, exc):
            self.exc = exc

        def list(self, limit):
            assert limit == 1
            if self.exc:
                raise self.exc
            return []

    def fake_client(exc):
        class Client:
            def __init__(self, **kwargs):
                assert kwargs["api_key"] == GOOD_KEY
                self.models = FakeModels(exc)
        return Client

    request = httpx.Request("GET", "https://api.anthropic.com/v1/models")
    auth = anthropic.AuthenticationError("bad", response=httpx.Response(401, request=request), body=None)
    monkeypatch.setattr(account.anthropic, "Anthropic", fake_client(auth))
    assert "not valid" in account.verify_api_key(GOOD_KEY)
    monkeypatch.setattr(account.anthropic, "Anthropic", fake_client(anthropic.APIConnectionError(request=request)))
    assert "could not reach" in account.verify_api_key(GOOD_KEY)
    monkeypatch.setattr(account.anthropic, "Anthropic", fake_client(None))
    assert account.verify_api_key(GOOD_KEY) is True


# ----- credits and usage ------------------------------------------------------------------------

def test_credits_page_shows_ledger_and_usage(user_client, app, fake_ai):
    uid = user_id()
    credits.add(uid, 500, "Welcome credits")
    credits.add(uid, -12, "AI: build.understand")
    with db.session() as conn:
        conn.execute("INSERT INTO ai_usage (user_id, purpose, model, input_tokens, output_tokens, web_searches,"
                     " cost_cents, paid_by, created_at) VALUES (?, 'build.understand', 'claude-opus-5-5', 100, 50,"
                     " 0, 12, 'credits', '2026-10-01T10:00:00+00:00')", (uid,))
        conn.execute("INSERT INTO ai_usage (user_id, purpose, model, input_tokens, output_tokens, web_searches,"
                     " cost_cents, paid_by, created_at) VALUES (?, 'research.answer', 'claude-opus-5-5', 100, 50,"
                     " 2, 7, 'own_key', '2026-10-02T10:00:00+00:00')", (uid,))
    r = user_client.get("/account/credits")
    assert r.status_code == 200
    assert h1(r.text) == "Your credits and AI usage"
    assert "Credit balance: <strong>$4.88</strong>" in r.text
    assert "+$5.00" in r.text and "Welcome credits" in r.text and "-$0.12" in r.text
    assert "Build: understand" in r.text and "Research: answer" in r.text
    assert "AI: Build: understand" in r.text and "build.understand" not in r.text
    assert "2026-10-01" in r.text and "2026-10-02" in r.text
    assert "<td>$0.07, your own key</td>" in r.text and "<td>$0.12, credits</td>" in r.text
    assert '<th scope="col">What it was for</th>' in r.text
    assert 'href="/account/credits/buy"' in r.text
    # Summary (balance) comes before the history.
    assert r.text.index("Credit balance") < r.text.index("Credit history")
    assert_accessible(r)


def test_credits_page_only_shows_your_own_usage(client):
    register(client, "a@example.com", "A")
    credits.add(user_id("a@example.com"), 777, "Secret gift for A")
    post(client, "/logout")
    register(client, "b@example.com", "B")
    r = client.get("/account/credits")
    assert "Secret gift for A" not in r.text and "$7.77" not in r.text


def test_credits_page_after_payment_says_what_happens(user_client):
    r = user_client.get("/account/credits?paid=1")
    assert "as soon as Stripe confirms" in r.text


# ----- buying -----------------------------------------------------------------------------------

def test_buy_page_when_payments_not_set_up(user_client):
    r = user_client.get("/account/credits/buy")
    assert r.status_code == 200
    assert h1(r.text) == "Buy credits"
    assert "Buying credits is not set up on this site yet" in r.text
    assert 'href="/account/ai-key"' in r.text
    assert "$5.00" in r.text and "$10.00" in r.text and "$20.00" in r.text
    assert 'action="/account/credits/buy"' not in r.text
    r = post(user_client, "/account/credits/buy", {"pack": "500"})
    assert r.status_code == 200 and "not set up on this site yet" in r.text


@pytest.fixture
def payments_on(app):
    app.state.settings.stripe_secret_key = "sk_test_123"
    app.state.settings.stripe_webhook_secret = WEBHOOK_SECRET
    app.state.settings.anthropic_api_key = "sk-ant-site-key"
    return app.state.settings


def test_buy_page_with_payments_shows_form(user_client, payments_on):
    r = user_client.get("/account/credits/buy")
    assert 'action="/account/credits/buy"' in r.text
    assert "never sees your card number" in r.text
    assert "25 percent" in r.text
    assert_accessible(r)


def test_checkout_redirects_to_stripe(user_client, payments_on, monkeypatch):
    calls = []

    def fake_checkout(secret_key, **kwargs):
        calls.append((secret_key, kwargs))
        return {"id": "cs_test_abc", "url": "https://checkout.stripe.com/c/pay/cs_test_abc"}

    monkeypatch.setattr(account, "create_checkout_session", fake_checkout)
    r = post(user_client, "/account/credits/buy", {"pack": "1000"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://checkout.stripe.com/c/pay/cs_test_abc"
    secret, kwargs = calls[0]
    assert secret == "sk_test_123"
    assert kwargs["amount_cents"] == 1000 and kwargs["user_id"] == user_id()
    assert kwargs["success_url"] == "http://testserver/account/credits?paid=1"
    assert kwargs["cancel_url"] == "http://testserver/account/credits/buy?cancelled=1"
    with db.session() as conn:
        row = dict(conn.execute("SELECT * FROM payments WHERE provider_ref = 'cs_test_abc'").fetchone())
    assert row["status"] == "pending" and row["credits_cents"] == 1000 and row["user_id"] == user_id()
    assert credits.balance(user_id()) == 0  # nothing added until Stripe confirms


def test_checkout_rejects_unknown_pack(user_client, payments_on, monkeypatch):
    monkeypatch.setattr(account, "create_checkout_session", lambda *a, **k: pytest.fail("should not be called"))
    r = post(user_client, "/account/credits/buy", {"pack": "1"})
    assert r.status_code == 400 and h1(r.text) == "Buy credits: please fix the problems"
    assert 'href="#field-pack"' in r.text


def test_checkout_failure_is_explained(user_client, payments_on, monkeypatch):
    def broken(*a, **k):
        raise account.PaymentError("We could not reach the payment service.")

    monkeypatch.setattr(account, "create_checkout_session", broken)
    r = post(user_client, "/account/credits/buy", {"pack": "500"})
    assert r.status_code == 200
    assert "could not reach the payment service" in r.text and "Nothing was charged" in r.text


def test_create_checkout_session_sends_the_right_form(monkeypatch):
    sent = {}

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"id": "cs_1", "url": "https://checkout.stripe.com/x"}'

    def fake_urlopen(req, timeout):
        sent["url"] = req.full_url
        sent["auth"] = req.get_header("Authorization")
        sent["body"] = req.data.decode()
        return Resp()

    monkeypatch.setattr(account.urllib.request, "urlopen", fake_urlopen)
    out = account.create_checkout_session("sk_test_9", user_id=7, email="a@example.com", amount_cents=500,
                                          success_url="https://s/ok", cancel_url="https://s/no", site_name="Studio")
    assert out["id"] == "cs_1"
    assert sent["url"] == "https://api.stripe.com/v1/checkout/sessions"
    assert sent["auth"] == "Bearer sk_test_9"
    from urllib.parse import parse_qs
    form = parse_qs(sent["body"])
    assert form["mode"] == ["payment"]
    assert form["line_items[0][price_data][currency]"] == ["usd"]
    assert form["line_items[0][price_data][unit_amount]"] == ["500"]
    assert form["client_reference_id"] == ["7"]
    assert form["metadata[user_id]"] == ["7"]
    assert form["success_url"] == ["https://s/ok"]


def _signed(payload: bytes, secret: str = WEBHOOK_SECRET, t: int | None = None) -> dict:
    t = int(time.time()) if t is None else t
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return {"Stripe-Signature": f"t={t},v1={sig}", "Content-Type": "application/json"}


def _event(session_id: str, uid: int, amount: int = 500, status: str = "paid") -> bytes:
    return json.dumps({"id": "evt_1", "type": "checkout.session.completed", "data": {"object": {
        "id": session_id, "object": "checkout.session", "payment_status": status, "amount_total": amount,
        "currency": "usd", "client_reference_id": str(uid), "metadata": {"user_id": str(uid)}}}}).encode()


def test_webhook_adds_credits_once(user_client, payments_on):
    uid = user_id()
    payload = _event("cs_live_1", uid, 1000)
    r = user_client.post("/payments/stripe/webhook", content=payload, headers=_signed(payload))
    assert r.status_code == 200 and r.json()["result"] == "credited"
    assert credits.balance(uid) == 1000
    r = user_client.post("/payments/stripe/webhook", content=payload, headers=_signed(payload))
    assert r.status_code == 200 and r.json()["result"] == "duplicate"
    assert credits.balance(uid) == 1000
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM payments WHERE provider_ref = 'cs_live_1'").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "paid"
    page = user_client.get("/account/credits").text
    assert "Bought credits" in page and "$10.00" in page


def test_webhook_completes_a_pending_checkout(user_client, payments_on, monkeypatch):
    monkeypatch.setattr(account, "create_checkout_session",
                        lambda *a, **k: {"id": "cs_pending", "url": "https://checkout.stripe.com/p"})
    post(user_client, "/account/credits/buy", {"pack": "2000"}, follow_redirects=False)
    payload = _event("cs_pending", user_id(), 2000)
    assert user_client.post("/payments/stripe/webhook", content=payload,
                            headers=_signed(payload)).json()["result"] == "credited"
    assert credits.balance(user_id()) == 2000
    with db.session() as conn:
        assert conn.execute("SELECT status FROM payments WHERE provider_ref = 'cs_pending'").fetchone()[0] == "paid"


def test_webhook_rejects_bad_signatures(user_client, payments_on):
    uid = user_id()
    payload = _event("cs_bad", uid)
    bad_secret = user_client.post("/payments/stripe/webhook", content=payload, headers=_signed(payload, "wrong"))
    assert bad_secret.status_code == 400 and bad_secret.json()["error"] == "bad signature"
    old = user_client.post("/payments/stripe/webhook", content=payload,
                           headers=_signed(payload, t=int(time.time()) - 600))
    assert old.status_code == 400
    missing = user_client.post("/payments/stripe/webhook", content=payload)
    assert missing.status_code == 400
    tampered = _event("cs_bad", uid, 999999)
    r = user_client.post("/payments/stripe/webhook", content=tampered, headers=_signed(payload))
    assert r.status_code == 400
    assert credits.balance(uid) == 0


def test_webhook_ignores_unpaid_and_other_events(user_client, payments_on):
    uid = user_id()
    unpaid = _event("cs_unpaid", uid, status="unpaid")
    assert user_client.post("/payments/stripe/webhook", content=unpaid,
                            headers=_signed(unpaid)).json()["result"] == "ignored: not paid yet"
    other = json.dumps({"type": "invoice.paid", "data": {"object": {}}}).encode()
    assert user_client.post("/payments/stripe/webhook", content=other, headers=_signed(other)).status_code == 200
    stranger = _event("cs_stranger", 99999)
    assert "unknown member" in user_client.post("/payments/stripe/webhook", content=stranger,
                                                headers=_signed(stranger)).json()["result"]
    assert credits.balance(uid) == 0


def test_webhook_without_secret_is_refused(user_client):
    payload = _event("cs_x", user_id())
    r = user_client.post("/payments/stripe/webhook", content=payload, headers=_signed(payload))
    assert r.status_code == 400
    assert credits.balance(user_id()) == 0


def test_signature_checker_accepts_any_matching_v1():
    payload = b'{"a":1}'
    t = 1_700_000_000
    good = hmac.new(b"s", f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    header = f"t={t},v1={'0' * 64},v1={good},v0=abc"
    assert account.verify_stripe_signature(payload, header, "s", now=t + 10)
    assert not account.verify_stripe_signature(payload, header, "s", now=t + 301)
    assert not account.verify_stripe_signature(payload, "v1=" + good, "s", now=t)
    assert not account.verify_stripe_signature(payload, f"t={t}", "s", now=t)


# ----- pricing ----------------------------------------------------------------------------------

def test_pricing_is_public_and_explains_both_ways(client):
    r = client.get("/pricing")
    assert r.status_code == 200
    assert h1(r.text) == "How paying works"
    assert "own Claude API key" in r.text and "charges nothing" in r.text
    assert "plus 25 percent" in r.text
    assert "estimates" in r.text.lower()
    assert '<th scope="col">Own key</th>' in r.text
    # Building a small website: 15,000 in and 20,000 out on Opus 5.5 = $0.06 + $0.40 = $0.46; with 25% = $0.575 -> $0.58
    assert "about $0.46" in r.text and "about $0.58" in r.text
    assert "$4.00 per million tokens read" in r.text
    assert_accessible(r)


def test_pricing_follows_markup_setting(client, app):
    app.state.settings.credit_markup = 1.5
    r = client.get("/pricing")
    assert "plus 50 percent" in r.text
    assert "about $0.69" in r.text  # $0.46 x 1.5


# ----- admin ------------------------------------------------------------------------------------

def test_admin_is_404_for_members(user_client):
    assert user_client.get("/account/admin").status_code == 404
    r = post(user_client, "/account/admin", {"email": "amina@example.com", "amount": "5", "reason": "x"})
    assert r.status_code == 404
    assert credits.balance(user_id()) == 0


def test_admin_grant_works(client):
    register(client, "friend@example.com", "Friend")
    post(client, "/logout")
    register(client, "owner@example.com", "Owner")
    make_admin("owner@example.com")
    page = client.get("/account")
    assert 'href="/account/admin"' in page.text
    r = client.get("/account/admin")
    assert r.status_code == 200 and h1(r.text) == "Site owner tools"
    assert "Members: 2" in r.text
    assert_accessible(r)
    r = post(client, "/account/admin", {"email": "Friend@Example.com", "amount": "2.50",
                                        "reason": "Thanks for helping in Help"})
    assert r.status_code == 200 and "Gave $2.50 of credits to friend@example.com" in r.text
    assert credits.balance(user_id("friend@example.com")) == 250
    assert "Thanks for helping in Help" in credits.history(user_id("friend@example.com"))[0]["reason"]


def test_admin_grant_validation(client):
    register(client, "owner@example.com", "Owner")
    make_admin("owner@example.com")
    r = post(client, "/account/admin", {"email": "nobody@example.com", "amount": "lots", "reason": ""})
    assert r.status_code == 400
    assert h1(r.text) == "Site owner tools: please fix the problems"
    assert "No member has this email address." in r.text
    assert "Enter an amount in US dollars" in r.text
    assert 'href="#field-reason"' in r.text
    r = post(client, "/account/admin", {"email": "owner@example.com", "amount": "5000", "reason": "too much"})
    assert r.status_code == 400 and "at most $1000.00" in r.text
    assert credits.balance(user_id("owner@example.com")) == 0
    assert_accessible(r)


# ----- manage.py --------------------------------------------------------------------------------

def test_manage_commands(user_client, settings, monkeypatch, capsys):
    monkeypatch.setenv("STUDIO_DB_PATH", str(settings.db_path))
    assert manage.main(["make-admin", "amina@example.com"]) == 0
    assert user_row()["is_admin"] == 1
    assert "can now use the site owner tools" in capsys.readouterr().out
    assert manage.main(["make-admin", "amina@example.com"]) == 0
    assert "already" in capsys.readouterr().out
    assert manage.main(["grant-credits", "amina@example.com", "500", "Thank", "you"]) == 0
    assert credits.balance(user_id()) == 500
    assert credits.history(user_id())[0]["reason"] == "Gift from the site: Thank you"
    assert "$5.00" in capsys.readouterr().out
    assert manage.main(["make-admin", "ghost@example.com"]) == 1
    assert manage.main(["grant-credits", "ghost@example.com", "500", "x"]) == 1
    assert manage.main(["grant-credits", "amina@example.com", "five", "x"]) == 2
    assert manage.main(["grant-credits", "amina@example.com", "-5", "x"]) == 2
    assert credits.balance(user_id()) == 500
    assert user_client.get("/account/admin").status_code == 200


def test_manage_runs_as_a_module(tmp_path):
    env = dict(os.environ, STUDIO_DB_PATH=str(tmp_path / "cli.db"))
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, "-m", "studio.manage", "make-admin", "nobody@example.com"],
                         cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 1
    assert "No member has the email address nobody@example.com" in out.stderr


# ----- delete -----------------------------------------------------------------------------------

def test_delete_needs_confirmation(user_client):
    r = user_client.get("/account/delete")
    assert h1(r.text) == "Delete your account"
    assert "cannot be undone" in r.text
    r = post(user_client, "/account/delete", {})
    assert r.status_code == 400 and 'href="#field-confirm"' in r.text
    assert user_row()["display_name"] == "Amina"
    assert_accessible(r)


def test_delete_account_anonymises_and_signs_out(user_client, key_ok):
    post(user_client, "/account/ai-key", {"api_key": GOOD_KEY})
    uid = user_id()
    pseudonym = user_row()["pseudonym"]
    old_hash = user_row()["password_hash"]
    r = post(user_client, "/account/delete", {"confirm": "yes"})
    assert r.status_code == 200 and "Your account is deleted" in r.text
    assert "Sign in" in r.text
    with db.session() as conn:
        row = dict(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())
    assert row["email"] == f"deleted-{uid}@invalid"
    assert row["display_name"] == "Former member"
    assert row["own_api_key_enc"] is None
    assert row["password_hash"] != old_hash
    assert row["pseudonym"] == pseudonym  # community posts keep the code name
    assert row["screen_reader"] == "" and row["device"] == ""
    # The old details no longer sign in, and the email can be used again.
    r = post(user_client, "/login", {"email": "amina@example.com", "password": "correct horse battery"})
    assert r.status_code == 400
    assert user_client.get("/account", follow_redirects=False).status_code == 303
    register(user_client)
    assert user_id() != uid
