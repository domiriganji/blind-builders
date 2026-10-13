"""The site owner's AI settings: the site's key kept safe, the model, who pays, and monthly limits."""
from __future__ import annotations

from html import unescape
from types import SimpleNamespace

import pytest

from studio import ai as ai_module
from studio import credits, db, site_ai
from studio.features import account

from .conftest import post, register, user_id
from .test_account import GOOD_KEY, assert_accessible, h1, make_admin

OTHER_KEY = "sk-ant-api03-" + "Z9y8X7w6V5u4T3s2R1q0" * 4 + "LMNO"
PASSWORD = "correct horse battery"


@pytest.fixture
def owner(client, monkeypatch):
    checked = []

    def fake_verify(key):
        checked.append(key)
        return True

    monkeypatch.setattr(account, "verify_api_key", fake_verify)
    register(client, "owner@example.com", "Owner")
    make_admin("owner@example.com")
    client.checked = checked
    return client


def test_only_the_owner_can_open_the_ai_settings(user_client):
    assert user_client.get("/account/admin/ai").status_code == 404
    for url in ("/account/admin/ai", "/account/admin/ai/key", "/account/admin/ai/key/remove"):
        assert post(user_client, url, {"api_key": GOOD_KEY, "password": PASSWORD}).status_code == 404
    assert site_ai.get("site_key_enc") == ""


def test_the_page_explains_how_the_key_is_kept_safe(owner):
    r = owner.get("/account/admin/ai")
    assert r.status_code == 200 and h1(r.text) == "AI settings for your key"
    assert "No key is saved yet." in r.text
    assert "never shown again" in r.text and "needs your password" in r.text
    assert 'value="claude-opus-5-5" checked' in r.text
    assert_accessible(r)
    assert 'href="/account/admin/ai"' in owner.get("/account/admin").text


def test_saving_the_key_needs_the_password_and_keeps_it_encrypted(owner, app):
    r = post(owner, "/account/admin/ai/key", {"api_key": GOOD_KEY, "password": "wrong password"})
    assert r.status_code == 400
    assert h1(r.text) == "Please check 1 answer: AI settings for your key"
    assert 'href="#field-password"' in r.text and site_ai.get("site_key_enc") == ""
    assert owner.checked == []  # Anthropic is not asked until the password is right

    r = post(owner, "/account/admin/ai/key", {"api_key": GOOD_KEY, "password": PASSWORD})
    assert r.status_code == 200 and unescape(h1(r.text)) == "The site's Claude API key is saved"
    stored = site_ai.get("site_key_enc")
    assert stored and GOOD_KEY not in stored
    assert GOOD_KEY not in r.text and "WXYZ" in r.text
    settings = app.state.settings
    assert site_ai.site_key(settings) == GOOD_KEY and site_ai.key_source(settings) == "page"

    page = owner.get("/account/admin/ai").text
    assert GOOD_KEY not in page and "A key ending in <strong>WXYZ</strong> is saved" in page

    # Removing it needs the password too.
    r = post(owner, "/account/admin/ai/key/remove", {"remove_password": ""})
    assert r.status_code == 400 and 'href="#field-remove_password"' in r.text
    assert site_ai.key_source(settings) == "page"
    r = post(owner, "/account/admin/ai/key/remove", {"remove_password": PASSWORD})
    assert unescape(h1(r.text)) == "The site's Claude API key is removed"
    assert site_ai.key_source(settings) is None and site_ai.get("site_key_enc") == ""


def test_wrong_passwords_are_limited(owner):
    for _ in range(5):
        post(owner, "/account/admin/ai/key", {"api_key": GOOD_KEY, "password": "not it"})
    r = post(owner, "/account/admin/ai/key", {"api_key": GOOD_KEY, "password": PASSWORD})
    assert r.status_code == 400 and "paused for an hour" in r.text
    assert site_ai.get("site_key_enc") == ""


def test_a_saved_key_takes_the_place_of_the_server_setting(app):
    settings = app.state.settings
    settings.anthropic_api_key = OTHER_KEY
    assert site_ai.key_source(settings) == "environment" and site_ai.key_last4(settings) == "LMNO"
    site_ai.save_key(settings, GOOD_KEY, None)
    assert site_ai.site_key(settings) == GOOD_KEY and site_ai.key_last4(settings) == "WXYZ"
    # A key the site can no longer read (its secret key changed) is not used.
    site_ai.save({"site_key_enc": "not-a-real-token"}, None)
    assert site_ai.key_unreadable(settings) and site_ai.site_key(settings) == OTHER_KEY


def test_model_payment_and_limits_are_saved(owner):
    r = post(owner, "/account/admin/ai", {"model": "claude-sonnet-5-5", "pay_mode": "free",
                                          "site_month": "50", "member_month": "2.50"})
    assert r.status_code == 200 and h1(r.text) == "Your AI settings are saved"
    assert site_ai.get("model") == "claude-sonnet-5-5" and site_ai.pay_mode() == "free"
    assert site_ai.site_month_limit() == 5000 and site_ai.member_month_limit() == 250
    page = owner.get("/account/admin/ai").text
    assert 'value="claude-sonnet-5-5" checked' in page and 'value="free" checked' in page
    assert 'value="2.50"' in page


def test_settings_validation(owner):
    r = post(owner, "/account/admin/ai", {"model": "gpt-9", "pay_mode": "maybe", "site_month": "lots",
                                          "member_month": "-1"})
    assert r.status_code == 400
    assert h1(r.text) == "Please check 4 answers: AI settings for your key"
    for name in ("model", "pay_mode", "site_month", "member_month"):
        assert f'href="#field-{name}"' in r.text, name
    # Free for members needs at least one limit, so the owner's bill cannot grow without end.
    r = post(owner, "/account/admin/ai", {"model": "claude-opus-5-5", "pay_mode": "free",
                                          "site_month": "0", "member_month": "0"})
    assert r.status_code == 400 and "set at least one limit" in r.text
    assert site_ai.pay_mode() == "credits"


def _settings(**extra):
    base = dict(secret_key="test-secret-key", anthropic_api_key=None, model="claude-opus-5-5",
                credit_markup=1.25, stripe_secret_key="")
    base.update(extra)
    return SimpleNamespace(**base)


def _user(email="amina@example.com") -> dict:
    with db.session() as conn:
        return dict(conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone())


def test_the_factory_follows_the_owner_settings(user_client):
    settings = _settings()
    factory = ai_module.default_factory(settings)
    with pytest.raises(ai_module.AINotAvailable) as exc:
        factory(_user())
    assert "buy credits" not in exc.value.user_message  # buying is not set up here

    site_ai.save_key(settings, GOOD_KEY, None)
    with pytest.raises(ai_module.AINotAvailable):
        factory(_user())  # credits mode and no credits
    credits.add(user_id(), 100, "test")
    client = factory(_user())
    assert (client.paid_by, client.model) == ("credits", "claude-opus-5-5")

    site_ai.save({"model": "claude-haiku-5-5", "pay_mode": "free", "member_month_cents": 100}, None)
    client = factory(_user())
    assert (client.paid_by, client.model, client.markup) == ("site", "claude-haiku-5-5", 1.0)


def test_members_choose_a_model_for_their_own_key(user_client, monkeypatch, app):
    monkeypatch.setattr(account, "verify_api_key", lambda key: True)
    post(user_client, "/account/ai-key", {"api_key": OTHER_KEY})
    page = user_client.get("/account/ai-key").text
    assert "Which Claude model the AI uses with your key" in page and 'value="" checked' in page
    r = post(user_client, "/account/ai-key/model", {"own_model": "claude-fable-5-1"})
    assert h1(r.text) == "Your model choice is saved"
    client = ai_module.default_factory(app.state.settings)(_user())
    assert (client.paid_by, client.model) == ("own_key", "claude-fable-5-1")
    assert post(user_client, "/account/ai-key/model", {"own_model": "gpt-9"}).status_code == 400
    post(user_client, "/account/ai-key/model", {"own_model": ""})
    assert ai_module.default_factory(app.state.settings)(_user()).model == "claude-opus-5-5"


class _Recorder:
    """A stand-in for the Anthropic SDK that answers with a fixed usage and remembers the request."""

    def __init__(self, input_tokens=1000, output_tokens=1000):
        recorder = self

        class Usage:
            cache_creation_input_tokens = 0; cache_read_input_tokens = 0; server_tool_use = None  # noqa: E702
        Usage.input_tokens = input_tokens
        Usage.output_tokens = output_tokens

        class Block:
            type = "text"; text = "ok"  # noqa: E702

        class Msg:
            content = [Block()]; stop_reason = "end_turn"; usage = Usage(); model = "claude-opus-5-5"  # noqa: E702

        class Stream:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get_final_message(self): return Msg()

        class Messages:
            @staticmethod
            def stream(**kw):
                recorder.kwargs = kw
                return Stream()

        self.beta = SimpleNamespace(messages=Messages())
        self.kwargs = None


def test_monthly_limits_stop_work_before_it_starts(user_client):
    uid = user_id()
    sdk = _Recorder()
    client = ai_module.AIClient(api_key="k", model="claude-opus-5-5", user_id=uid, paid_by="site", markup=1.0,
                                sdk_client=sdk)
    site_ai.save({"member_month_cents": 100}, None)
    # The most this call can cost is far more than $1.00, so it never starts and nothing is recorded.
    with pytest.raises(ai_module.AINotAvailable) as exc:
        client.text(system="s", prompt="p", purpose="test.limit", max_tokens=64000)
    assert "left of your $1.00 for this month" in exc.value.user_message
    assert "connect your own Claude API key" in exc.value.user_message
    assert sdk.kwargs is None
    # A small call fits, is recorded at Anthropic's price, and its hold is released.
    assert client.text(system="s", prompt="p", purpose="test.limit", max_tokens=1000) == "ok"
    assert site_ai.spent_this_month(uid) == 3  # 1,000 in and 1,000 out on Opus 5.5 is 2.4 cents
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ai_spend_holds").fetchone()[0] == 0

    site_ai.save({"member_month_cents": 0, "site_month_cents": 3}, None)
    with pytest.raises(ai_module.AINotAvailable) as exc:
        client.text(system="s", prompt="p", purpose="test.limit", max_tokens=1000)
    assert "budget for this month on the site's key is used up" in exc.value.user_message


def test_limits_never_apply_to_members_own_keys(user_client):
    site_ai.save({"member_month_cents": 1, "site_month_cents": 1}, None)
    client = ai_module.AIClient(api_key="k", model="claude-opus-5-5", user_id=user_id(), paid_by="own_key",
                                markup=1.0, sdk_client=_Recorder())
    assert client.text(system="s", prompt="p", purpose="test.own", max_tokens=16000) == "ok"


def test_haiku_gets_no_fallback_and_its_long_prompt_price(user_client):
    sdk = _Recorder()
    ai_module.AIClient(api_key="k", model="claude-haiku-5-5", user_id=user_id(), paid_by="own_key", markup=1.0,
                       sdk_client=sdk).text(system="s", prompt="p", purpose="t")
    assert "fallbacks" not in sdk.kwargs and "betas" not in sdk.kwargs
    sdk = _Recorder()
    ai_module.AIClient(api_key="k", model="claude-sonnet-5-5", user_id=user_id(), paid_by="own_key", markup=1.0,
                       sdk_client=sdk).text(system="s", prompt="p", purpose="t")
    assert sdk.kwargs["fallbacks"] == "default"
    assert ai_module.prices("claude-haiku-5-5", 50_000) == (0.10, 0.50)
    assert ai_module.prices("claude-haiku-5-5", 150_000) == (0.50, 2.50)


def test_members_see_free_ai_and_what_is_left(user_client, app):
    settings = app.state.settings
    site_ai.save_key(settings, GOOD_KEY, None)
    site_ai.save({"pay_mode": "free", "member_month_cents": 500}, None)
    page = unescape(user_client.get("/account").text)
    assert "On this site the AI is free for you: the site's owner pays for it." in page
    assert "<strong>$5.00</strong> left of $5.00" in page
    home = unescape(user_client.get("/").text)
    # The AI is set up (free), so the home page does not list setting it up as a next step.
    assert "Your next steps" not in home and "the AI is free for members, up to $5.00 each a month" in home
    pricing = unescape(user_client.get("/pricing").text)
    assert "the AI is free for members" in pricing and "up to $5.00 for each member a month" in pricing


def test_holds_left_by_a_restart_are_released(app):
    with db.session() as conn:
        conn.execute("INSERT INTO ai_spend_holds (user_id, cents, created_at) VALUES (1, 500, 'now')")
    site_ai.release_all_holds()
    assert site_ai.spent_this_month() == 0
