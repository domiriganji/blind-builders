"""Other AI providers: the compatible client, its billing, routing to Claude for web search and pictures, the
owner's page and members' own keys from the listed providers. No test touches the network: every request goes to
an httpx.MockTransport set as providers.TRANSPORT, and DNS answers come from a replaced providers.resolve."""
from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path

import httpx
import pytest

from studio import ai as ai_module
from studio import compat_ai, credits, db, providers, site_ai
from studio.compat_ai import CompatClient, schema_problems
from studio.features import account

from .conftest import post, register, user_id
from .test_account import GOOD_KEY, assert_accessible, h1, make_admin

PASSWORD = "correct horse battery"
OTHER_KEY = "sk-other-" + "Q1w2E3r4T5y6" * 3 + "KEY9"
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["title", "items", "kind"],
          "properties": {"title": {"type": "string"},
                         "items": {"type": "array", "items": {
                             "type": "object", "additionalProperties": False, "required": ["name", "count"],
                             "properties": {"name": {"type": "string"}, "count": {"type": "integer"}}}},
                         "kind": {"type": "string", "enum": ["page", "app"]}}}
GOOD = {"title": "Cake shop", "items": [{"name": "menu", "count": 2}], "kind": "page"}


class Provider:
    """A fake provider: answers each request with the next queued response and remembers every request."""

    def __init__(self, *answers, models=None):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []
        self.models = models if models is not None else [{"id": "model-a"}, {"id": "model-b"}]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": self.models})
        answer = self.answers.pop(0) if self.answers else chat("ok")
        return answer(request) if callable(answer) else answer

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith("/chat/completions")]


def chat(content: str, finish: str = "stop", prompt_tokens: int = 1000, completion_tokens: int = 1000):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content},
                                                  "finish_reason": finish}],
                                     "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}})


def error(status: int, message: str = "", **extra):
    return httpx.Response(status, json={"error": {"message": message, **extra}})


@pytest.fixture
def fake_provider(monkeypatch):
    holder = {"provider": Provider()}

    def use(provider: Provider) -> Provider:
        holder["provider"] = provider
        return provider

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(lambda r: holder["provider"](r)))
    monkeypatch.setattr(compat_ai, "_sleep", lambda seconds: None)
    monkeypatch.setattr(providers, "resolve", lambda host: ["93.184.215.14"])
    providers.forget_cache()
    yield use
    providers.forget_cache()


def make_client(provider_id="openai", *, paid_by="own_key", prices=(2.0, 10.0), markup=1.0, uid=1, images=False,
                note="", base_url=""):
    provider = providers.resolved(provider_id, base_url=base_url, name="Example AI")
    return CompatClient(provider=provider, api_key=OTHER_KEY, model="model-a", prices=prices, user_id=uid,
                        paid_by=paid_by, markup=markup, member_note=note, allow_images=images)


def usage_rows():
    with db.session() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM ai_usage ORDER BY id")]


# ----- the compatible client --------------------------------------------------------------------------------

def test_structured_with_json_schema(user_client, fake_provider):
    provider = fake_provider(Provider(chat(json.dumps(GOOD))))
    client = make_client(note="NOTE", uid=user_id())
    assert client.structured(system="Build it.", prompt="A cake shop", schema=SCHEMA, purpose="build.understand",
                             max_tokens=4000) == GOOD
    request = provider.requests[0]
    assert str(request.url) == "https://api.openai.com/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {OTHER_KEY}"
    body = provider.bodies()[0]
    assert body["model"] == "model-a" and body["max_completion_tokens"] == 4000 and "max_tokens" not in body
    assert body["response_format"] == {"type": "json_schema",
                                       "json_schema": {"name": "answer", "strict": True, "schema": SCHEMA}}
    assert body["messages"] == [{"role": "system", "content": "NOTE\n\nBuild it."},
                                {"role": "user", "content": "A cake shop"}]
    row = usage_rows()[0]
    # 1,000 tokens in at $2 and 1,000 out at $10 a million is 1.2 cents.
    assert (row["model"], row["input_tokens"], row["output_tokens"], row["cost_cents"], row["raw_cents"],
            row["cost_known"], row["paid_by"]) == ("openai:model-a", 1000, 1000, 2, 2, 1, "own_key")


def test_other_providers_get_max_tokens(user_client, fake_provider):
    provider = fake_provider(Provider(chat("Hello")))
    assert make_client("mistral").text(system="s", prompt="p", purpose="t", max_tokens=300) == "Hello"
    body = provider.bodies()[0]
    assert body["max_tokens"] == 300 and "max_completion_tokens" not in body and "response_format" not in body
    assert str(provider.requests[0].url) == "https://api.mistral.ai/v1/chat/completions"


def test_json_object_fallback_checks_the_shape(user_client, fake_provider):
    fenced = "```json\n" + json.dumps(GOOD) + "\n```"
    provider = fake_provider(Provider(error(400, "response_format json_schema is not supported by this model"),
                                      chat(fenced)))
    client = make_client("deepseek")
    assert client.structured(system="Build it.", prompt="p", schema=SCHEMA, purpose="t") == GOOD
    first, second = provider.bodies()
    assert first["response_format"]["type"] == "json_schema"
    assert second["response_format"] == {"type": "json_object"}
    assert json.dumps(SCHEMA) in second["messages"][0]["content"]
    assert second["messages"][0]["content"].startswith("Build it.")
    # The client remembers: the next call asks for json_object straight away.
    fake_provider(provider)
    provider.answers = [chat(json.dumps(GOOD))]
    client.structured(system="s", prompt="p", schema=SCHEMA, purpose="t")
    assert provider.bodies()[-1]["response_format"] == {"type": "json_object"}
    assert len(usage_rows()) == 2  # the refused request cost nothing and is not recorded


def test_one_repair_turn_says_what_did_not_fit(user_client, fake_provider):
    bad = {"title": "Cake shop", "items": [{"name": "menu", "count": "two"}], "kind": "poster", "extra": 1}
    provider = fake_provider(Provider(chat(json.dumps(bad)), chat(json.dumps(GOOD))))
    assert make_client().structured(system="s", prompt="p", schema=SCHEMA, purpose="t") == GOOD
    repair = provider.bodies()[1]["messages"]
    assert repair[2] == {"role": "assistant", "content": json.dumps(bad)}
    words = repair[3]["content"]
    assert "items[0].count should be integer" in words and 'kind should be one of "page", "app"' in words
    assert "has extra, which is not in the schema" in words
    assert len(usage_rows()) == 2  # both turns are paid for


def test_answers_that_never_fit_save_nothing(user_client, fake_provider):
    fake_provider(Provider(chat("Here is your site!"), chat(json.dumps({"title": "x"}))))
    with pytest.raises(ai_module.AIError) as exc:
        make_client().structured(system="s", prompt="p", schema=SCHEMA, purpose="t")
    message = exc.value.user_message
    assert "nothing was saved" in message and "choose another model on Your account" in message
    assert OTHER_KEY not in message


def test_schema_problems():
    assert schema_problems(GOOD, SCHEMA) == []
    assert schema_problems([], SCHEMA) == ["the answer should be object"]
    assert schema_problems({"title": 1, "items": [], "kind": "page"}, SCHEMA) == ["title should be string"]
    assert "the answer is missing kind" in schema_problems({"title": "a", "items": []}, SCHEMA)
    assert schema_problems({"a": None}, {"type": "object", "properties": {"a": {"type": ["string", "null"]}}}) == []
    assert schema_problems(True, {"type": "integer"}) == ["the answer should be integer"]


def test_cut_off_and_refused_answers(user_client, fake_provider):
    fake_provider(Provider(chat('{"title": "Ca', finish="length"), chat("", finish="content_filter")))
    client = make_client(uid=user_id())
    with pytest.raises(ai_module.AIError) as exc:
        client.structured(system="s", prompt="p", schema=SCHEMA, purpose="t")
    assert "cut off, so nothing was saved" in exc.value.user_message
    with pytest.raises(ai_module.AIRefused) as exc:
        client.text(system="s", prompt="p", purpose="t")
    assert "would not do this request" in exc.value.user_message
    assert len(usage_rows()) == 2  # both answers were paid for


@pytest.mark.parametrize("answer, words", [
    (error(401, "Incorrect API key provided: " + OTHER_KEY), "OpenAI did not accept your API key"),
    (error(403, "forbidden"), "connect it again on Your account"),
    (error(404, "The model model-a does not exist"), "The model may not exist any more"),
    (error(400, "model_not_found", code="model_not_found"), "could not find the model model-a"),
    (error(500, "boom"), "had a problem on its side (code 500)"),
    (httpx.Response(302, headers={"location": "https://elsewhere.example/v1/chat/completions"}),
     "does not follow that"),
])
def test_provider_errors_are_plain_and_never_hold_the_key(user_client, fake_provider, answer, words):
    fake_provider(Provider(answer, answer, answer, answer))
    with pytest.raises(ai_module.AIError) as exc:
        make_client().text(system="s", prompt="p", purpose="t")
    assert words in exc.value.user_message
    assert OTHER_KEY not in exc.value.user_message and OTHER_KEY not in repr(exc.value)
    assert exc.value.__cause__ is None and (exc.value.__context__ is None or exc.value.__suppress_context__)
    assert usage_rows() == []


def test_busy_providers_are_tried_twice_more(user_client, fake_provider, monkeypatch):
    waits = []
    monkeypatch.setattr(compat_ai, "_sleep", waits.append)
    provider = fake_provider(Provider(error(429), error(429), error(429)))
    with pytest.raises(ai_module.AIError) as exc:
        make_client().text(system="s", prompt="p", purpose="t")
    assert "busy right now" in exc.value.user_message and "try again in a minute" in exc.value.user_message
    assert len(provider.requests) == 3 and waits == [1.0, 2.0]
    provider = fake_provider(Provider(error(503), chat("Back")))
    assert make_client().text(system="s", prompt="p", purpose="t") == "Back"
    assert len(provider.requests) == 2


def test_site_key_errors_say_who_can_fix_them(user_client, fake_provider):
    fake_provider(Provider(error(401), error(404)))
    client = make_client(paid_by="site", uid=user_id())
    with pytest.raises(ai_module.AIError) as exc:
        client.text(system="s", prompt="p", purpose="t")
    assert "The site's key for OpenAI is not working" in exc.value.user_message
    assert "owner can check it in AI settings" in exc.value.user_message
    with pytest.raises(ai_module.AIError) as exc:
        client.text(system="s", prompt="p", purpose="t")
    assert "tell the site's owner" in exc.value.user_message


def test_the_token_limit_name_is_switched_once(user_client, fake_provider):
    provider = fake_provider(Provider(
        error(400, "Unsupported parameter: 'max_completion_tokens' is not supported with this model."), chat("ok")))
    assert make_client("openai").text(system="s", prompt="p", purpose="t", max_tokens=99) == "ok"
    first, second = provider.bodies()
    assert first["max_completion_tokens"] == 99 and second["max_tokens"] == 99 and "max_completion_tokens" not in second


def test_could_not_reach_the_provider(user_client, monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("no route", request=request)
    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(refuse))
    with pytest.raises(ai_module.AIError) as exc:
        make_client("groq").text(system="s", prompt="p", purpose="t")
    assert "could not reach Groq" in exc.value.user_message


def test_pictures_become_image_url_parts(user_client, fake_provider):
    blocks = [{"type": "text", "text": "Picture 1"},
              {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}},
              {"type": "text", "text": "How does it look?"}]
    provider = fake_provider(Provider(chat(json.dumps(GOOD))))
    make_client(images=True).structured(system="s", prompt=blocks, schema=SCHEMA, purpose="build.design_review")
    content = provider.bodies()[0]["messages"][1]["content"]
    assert content == [{"type": "text", "text": "Picture 1"},
                       {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
                       {"type": "text", "text": "How does it look?"}]
    # A client not allowed pictures never sends them, and charges nothing.
    provider = fake_provider(Provider())
    with pytest.raises(ai_module.AINotAvailable):
        make_client(images=False).structured(system="s", prompt=blocks, schema=SCHEMA, purpose="t")
    assert provider.requests == []
    # Text blocks alone are sent as one string, which every provider takes.
    provider = fake_provider(Provider(chat("ok")))
    make_client().text(system="s", prompt=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], purpose="t")
    assert provider.bodies()[0]["messages"][1]["content"] == "a\n\nb"


# ----- billing and limits are the same for every provider ----------------------------------------------------

def test_credits_and_limits_cover_compatible_calls(user_client, fake_provider):
    uid = user_id()
    credits.add(uid, 1000, "test")
    provider = fake_provider(Provider(chat("ok", prompt_tokens=100_000, completion_tokens=20_000)))
    client = make_client(paid_by="credits", markup=1.25, uid=uid, prices=(3.0, 15.0))
    assert client.text(system="s", prompt="p", purpose="write.check", max_tokens=1000) == "ok"
    # 100,000 in at $3 and 20,000 out at $15 a million is 60 cents; with the markup, 75.
    assert credits.balance(uid) == 1000 - 75
    row = usage_rows()[0]
    assert (row["cost_cents"], row["raw_cents"], row["paid_by"]) == (75, 60, "credits")
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ai_spend_holds").fetchone()[0] == 0
    # The member's monthly limit stops a call that could cost too much before anything is sent.
    site_ai.save({"member_month_cents": 61}, None)
    with pytest.raises(ai_module.AINotAvailable) as exc:
        client.text(system="s", prompt="p", purpose="t", max_tokens=64000)
    assert "for this month on the site's key" in exc.value.user_message
    assert len(provider.requests) == 1 and credits.balance(uid) == 925
    # A balance too small for the most a call can cost stops it too.
    site_ai.save({"member_month_cents": 0}, None)
    with pytest.raises(ai_module.AINotAvailable) as exc:
        make_client(paid_by="credits", markup=1.25, uid=uid, prices=(300.0, 1500.0)).text(
            system="s", prompt="p", purpose="t", max_tokens=64000)
    assert "nothing was charged" in exc.value.user_message and len(provider.requests) == 1


def test_free_ai_counts_against_the_site_limit(user_client, fake_provider):
    uid = user_id()
    fake_provider(Provider(chat("ok", prompt_tokens=10_000, completion_tokens=10_000)))
    make_client(paid_by="site", uid=uid, prices=(1.0, 1.0)).text(system="s", prompt="p", purpose="t", max_tokens=100)
    assert site_ai.spent_this_month(uid) == 2 and site_ai.spent_this_month() == 2


def test_own_key_without_prices_records_cost_not_known(user_client, fake_provider):
    fake_provider(Provider(chat("ok")))
    make_client(prices=None, uid=user_id()).text(system="s", prompt="p", purpose="research.answer")
    row = usage_rows()[0]
    assert (row["cost_known"], row["cost_cents"], row["model"]) == (0, 0, "openai:model-a")
    page = unescape(user_client.get("/account/credits").text)
    assert "cost not known for this model" in page and "OpenAI: model-a" in page
    # The site's key can never run a model without prices.
    with pytest.raises(ai_module.AINotAvailable):
        make_client(prices=None, paid_by="site", uid=user_id()).text(system="s", prompt="p", purpose="t")


# ----- routing ---------------------------------------------------------------------------------------------

class Recorder:
    def __init__(self, name):
        self.name, self.calls = name, []

    def structured(self, **kw):
        self.calls.append("structured")
        return self.name

    def text(self, **kw):
        self.calls.append("text")
        return self.name

    def research(self, **kw):
        self.calls.append("research")
        return self.name


def test_routed_ai_sends_each_step_to_the_right_ai():
    main, claude = Recorder("main"), Recorder("claude")
    routed = ai_module.RoutedAI(main=main, research=claude, pictures=claude)
    picture = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}]
    assert routed.text(system="s", prompt="p", purpose="t") == "main"
    assert routed.structured(system="s", prompt=[{"type": "text", "text": "p"}], schema={}, purpose="t") == "main"
    assert routed.structured(system="s", prompt=picture, schema={}, purpose="t") == "claude"
    assert routed.research(system="s", prompt="p", purpose="t") == "claude"
    nothing = ai_module.RoutedAI(main=main, research_reason="no search", pictures_reason="no pictures")
    with pytest.raises(ai_module.AINotAvailable, match="no search"):
        nothing.research(system="s", prompt="p", purpose="t")
    with pytest.raises(ai_module.AINotAvailable, match="no pictures"):
        nothing.text(system="s", prompt=picture, purpose="t")


def _user(email="amina@example.com") -> dict:
    with db.session() as conn:
        return dict(conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone())


def _set_site_other(settings, provider_id="openai", vision="claude", main="other"):
    site_ai.save_other_key(settings, providers.get(provider_id), OTHER_KEY, None)
    site_ai.save({"other_model": "model-a", "other_price_in": "2.00", "other_price_out": "10.00", "main_ai": main,
                  "vision": vision}, None)


def test_the_default_site_still_gets_one_claude_client(user_client, app):
    settings = app.state.settings
    site_ai.save_key(settings, GOOD_KEY, None)
    site_ai.save({"pay_mode": "free", "member_month_cents": 100}, None)
    client = ai_module.default_factory(settings)(_user())
    assert type(client) is ai_module.AIClient and client.paid_by == "site"
    # A saved key for another provider changes nothing until the owner makes it the main AI.
    _set_site_other(settings, main="anthropic")
    assert type(ai_module.default_factory(settings)(_user())) is ai_module.AIClient


def test_web_search_and_pictures_use_claude_with_another_main_ai(user_client, app):
    settings = app.state.settings
    site_ai.save_key(settings, GOOD_KEY, None)
    site_ai.save({"pay_mode": "free", "member_month_cents": 100}, None)
    _set_site_other(settings)
    routed = ai_module.default_factory(settings)(_user())
    assert isinstance(routed, ai_module.RoutedAI)
    assert isinstance(routed.main, CompatClient) and routed.main.paid_by == "site"
    assert routed.main.prices == (2.0, 10.0) and not routed.main.allow_images
    assert type(routed._research) is ai_module.AIClient and routed._research.paid_by == "site"
    assert routed._pictures is routed._research
    site_ai.save({"vision": "main"}, None)
    routed = ai_module.default_factory(settings)(_user())
    assert routed._pictures is routed.main and routed.main.allow_images


def test_without_a_claude_key_search_and_pictures_say_why(user_client, app, fake_provider):
    settings = app.state.settings
    site_ai.save({"pay_mode": "free", "member_month_cents": 100}, None)
    _set_site_other(settings)
    provider = fake_provider(Provider())
    routed = ai_module.default_factory(settings)(_user())
    with pytest.raises(ai_module.AINotAvailable) as exc:
        routed.research(system="s", prompt="p", purpose="research.answer")
    message = exc.value.user_message
    assert message.startswith("This step searches the live web, which on this site needs Claude.")
    assert "nothing was charged" in message and "connect your own Claude API key on Your account" in message
    picture = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}]
    with pytest.raises(ai_module.AIError) as exc:  # Build's design review catches AIError and says the AI could not look
        routed.structured(system="s", prompt=picture, schema=SCHEMA, purpose="build.design_review")
    assert "pictures" in exc.value.user_message
    assert provider.requests == []
    # Everyday steps still work on the main AI.
    provider.answers = [chat("Hello")]
    assert routed.text(system="s", prompt="p", purpose="help.ai_answer") == "Hello"


def test_credits_mode_needs_credits_whichever_ai(user_client, app):
    settings = app.state.settings
    _set_site_other(settings)
    with pytest.raises(ai_module.AINotAvailable):
        ai_module.default_factory(settings)(_user())
    credits.add(user_id(), 100, "test")
    routed = ai_module.default_factory(settings)(_user())
    assert routed.main.paid_by == "credits" and routed.main.markup == settings.credit_markup


def test_a_members_own_key_from_another_provider(user_client, app, fake_provider):
    settings = app.state.settings
    fake_provider(Provider())
    r = post(user_client, "/account/ai-key/other", {"other_provider": "gemini", "other_api_key": OTHER_KEY})
    assert h1(r.text) == "Your Google Gemini key is connected"
    with pytest.raises(ai_module.AINotAvailable) as exc:
        ai_module.default_factory(settings)(_user())
    assert "Choose the model for your Google Gemini key first" in exc.value.user_message
    r = post(user_client, "/account/ai-key/model", {"own_model": "model-b"})
    assert h1(r.text) == "Your model choice is saved"
    routed = ai_module.default_factory(settings)(_user())
    assert routed.main.provider == "gemini" and routed.main.paid_by == "own_key" and routed.main.prices is None
    with pytest.raises(ai_module.AINotAvailable) as exc:
        routed.research(system="s", prompt="p", purpose="t")
    assert "in place of your other key" in exc.value.user_message
    # With the site's Claude key, free on this site, web search steps use it.
    site_ai.save_key(settings, GOOD_KEY, None)
    site_ai.save({"pay_mode": "free", "member_month_cents": 100}, None)
    routed = ai_module.default_factory(settings)(_user())
    assert type(routed._research) is ai_module.AIClient and routed._research.paid_by == "site"
    assert routed._pictures is routed._research
    page = unescape(user_client.get("/account").text)
    assert "You use your own API key from Google Gemini ending in" in page and "with the model model-b" in page
    assert "Web search steps need Claude, so they use the site's Claude key, free for you." in page


def test_a_claude_key_after_another_key_starts_fresh(user_client, app, fake_provider, monkeypatch):
    monkeypatch.setattr(account, "verify_api_key", lambda key: True)
    fake_provider(Provider())
    post(user_client, "/account/ai-key/other", {"other_provider": "openai", "other_api_key": OTHER_KEY})
    post(user_client, "/account/ai-key/model", {"own_model_typed": "my-model", "own_price_in": "1",
                                                "own_price_out": "2", "own_vision": "yes"})
    assert (_user()["own_model"], _user()["own_vision"], _user()["own_price_in"]) == ("my-model", "main", "1.00")
    post(user_client, "/account/ai-key", {"api_key": GOOD_KEY})
    row = _user()
    assert (row["own_provider"], row["own_model"], row["own_vision"], row["own_price_in"]) == ("anthropic", "", "", "")
    client = ai_module.default_factory(app.state.settings)(_user())
    assert type(client) is ai_module.AIClient and client.model == "claude-opus-5-5"


def test_sections_use_only_the_three_methods():
    """RoutedAI offers structured, text and research; a section reading anything else would break with it."""
    for path in Path(__file__).parent.parent.joinpath("studio", "features").glob("*.py"):
        code = re.sub(r"#.*", "", path.read_text())
        for name in re.findall(r"(?<![\w.])ai\.([A-Za-z_]+)", code):
            assert name in {"structured", "text", "research", "html"}, (path.name, name)


# ----- the owner's page -------------------------------------------------------------------------------------

@pytest.fixture
def owner(client, monkeypatch, fake_provider):
    monkeypatch.setattr(account, "verify_api_key", lambda key: True)
    register(client, "owner@example.com", "Owner")
    make_admin("owner@example.com")
    return client


def _save_other(owner, **data):
    form = {"other_provider": "openai", "other_api_key": OTHER_KEY, "other_password": PASSWORD}
    form.update(data)
    return post(owner, "/account/admin/ai/other-key", form)


def test_the_owner_page_offers_providers_and_says_claude_searches(owner):
    r = owner.get("/account/admin/ai")
    assert_accessible(r)
    page = unescape(r.text)
    for name in ("OpenAI", "Google Gemini", "OpenRouter", "DeepSeek", "Mistral", "Groq", "xAI (Grok)",
                 "Another compatible provider"):
        assert f">{name}: " in page, name
    assert "Research, Do this for me, Find before you build, and finding publishing venues in Write" in page
    assert "Main AI for members on your key" in page and "Who looks at pictures of built pages" in page
    assert 'value="anthropic" checked' in page and 'value="claude" checked' in page


def test_the_providers_key_is_saved_only_with_the_password(owner, fake_provider, app):
    provider = fake_provider(Provider())
    r = _save_other(owner, other_password="not it")
    assert r.status_code == 400 and 'href="#field-other_password"' in r.text
    assert provider.requests == [] and site_ai.get("other_key_enc") == ""
    assert OTHER_KEY not in r.text
    assert_accessible(r)
    r = _save_other(owner)
    assert r.status_code == 200 and h1(r.text) == "The key for OpenAI is saved"
    assert provider.requests[0].headers["authorization"] == f"Bearer {OTHER_KEY}"
    assert str(provider.requests[0].url) == "https://api.openai.com/v1/models"
    stored = site_ai.get("other_key_enc")
    assert stored and OTHER_KEY not in stored and site_ai.other_key(app.state.settings) == OTHER_KEY
    page = unescape(owner.get("/account/admin/ai").text)
    assert "A key for <strong>OpenAI</strong> ending in <strong>KEY9</strong> is saved" in page
    assert OTHER_KEY not in page
    # Removing it needs the password too, and the main AI goes back to Claude.
    site_ai.save({"main_ai": "other", "other_model": "model-a", "other_price_in": "1", "other_price_out": "1"}, None)
    r = post(owner, "/account/admin/ai/other-key/remove", {"other_remove_password": ""})
    assert r.status_code == 400 and site_ai.other_provider() is not None
    r = post(owner, "/account/admin/ai/other-key/remove", {"other_remove_password": PASSWORD})
    assert h1(r.text) == "The key for OpenAI is removed"
    assert site_ai.other_provider() is None and site_ai.main_is_claude() and site_ai.get("other_key_enc") == ""


def test_a_key_the_provider_does_not_accept_is_not_saved(owner, fake_provider):
    fake_provider(type("Refuses", (Provider,), {"__call__": lambda self, r: error(401, "bad key " + OTHER_KEY)})())
    r = _save_other(owner, other_provider="xai")
    assert r.status_code == 400 and "xAI (Grok) did not accept this key, so it was not saved" in unescape(r.text)
    assert OTHER_KEY not in r.text and site_ai.get("other_key_enc") == ""


def test_models_come_from_the_providers_list_and_prices_are_required(owner, fake_provider):
    fake_provider(Provider(models=[{"id": "gpt-6.2-sol"}, {"id": "gpt-6-mini"}, {"id": "text-embedding-9"},
                                   {"id": "whisper-2"}]))
    _save_other(owner)
    page = owner.get("/account/admin/ai")
    assert_accessible(page)
    text = unescape(page.text)
    assert 'value="gpt-6.2-sol"' in text and 'value="gpt-6-mini"' in text
    assert "text-embedding-9" not in text and "whisper-2" not in text  # models that cannot write answers
    r = post(owner, "/account/admin/ai/main", {"main_ai": "other", "other_model": "gpt-6.2-sol", "vision": "claude"})
    assert r.status_code == 400 and h1(r.text) == "Please check 2 answers: AI settings for your key"
    assert 'href="#field-other_price_in"' in r.text and 'href="#field-other_price_out"' in r.text
    assert site_ai.main_is_claude()
    assert_accessible(r)
    r = post(owner, "/account/admin/ai/main", {"main_ai": "other", "other_model": "gpt-6.2-sol",
                                               "other_price_in": "$1.75", "other_price_out": "14", "vision": "main"})
    assert h1(r.text) == "The main AI is saved"
    assert "now uses OpenAI, model gpt-6.2-sol. Web search steps still use Claude." in unescape(r.text)
    assert (site_ai.main_choice(), site_ai.other_model(), site_ai.other_prices(), site_ai.vision_choice()) == (
        "other", "gpt-6.2-sol", (1.75, 14.0), "main")
    # A typed name is used instead of the list, for a model the list does not show.
    r = post(owner, "/account/admin/ai/main", {"main_ai": "other", "other_model": "gpt-6-mini",
                                               "other_model_typed": "gpt-7-preview", "other_price_in": "1",
                                               "other_price_out": "2", "vision": "claude"})
    assert site_ai.other_model() == "gpt-7-preview"
    assert 'value="gpt-7-preview"' in owner.get("/account/admin/ai").text
    pricing = unescape(owner.get("/pricing").text)
    assert "Most steps use OpenAI: gpt-7-preview, at the prices the site's owner set: $1.00" in pricing
    assert "Steps that search the web use Claude" in pricing


def test_the_main_ai_needs_a_saved_key_and_a_model(owner):
    r = post(owner, "/account/admin/ai/main", {"main_ai": "other", "vision": "claude"})
    assert r.status_code == 400 and "Save a key for another provider first" in unescape(r.text)
    _save_other(owner)
    r = post(owner, "/account/admin/ai/main", {"main_ai": "other", "other_model_typed": "has space",
                                               "other_price_in": "1", "other_price_out": "1", "vision": "maybe"})
    assert r.status_code == 400 and 'href="#field-other_model_typed"' in r.text and 'href="#field-vision"' in r.text


def test_openrouter_prices_fill_in_from_its_list(owner, fake_provider):
    fake_provider(Provider(models=[
        {"id": "openai/gpt-6.2-sol", "name": "GPT 6.2 Sol",
         "pricing": {"prompt": "0.00000175", "completion": "0.000014"}},
        {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}}]))
    _save_other(owner, other_provider="openrouter")
    page = unescape(owner.get("/account/admin/ai").text)
    assert "openai/gpt-6.2-sol (GPT 6.2 Sol): $1.75 per million tokens in, $14.00 out" in page
    assert "the prices in OpenRouter's list are used" in page
    r = post(owner, "/account/admin/ai/main", {"main_ai": "other", "other_model": "openai/gpt-6.2-sol",
                                               "vision": "claude"})
    assert h1(r.text) == "The main AI is saved" and "The prices are from OpenRouter's list" in unescape(r.text)
    assert site_ai.other_prices() == (1.75, 14.0)
    page = owner.get("/account/admin/ai").text
    assert 'id="field-other_price_in" name="other_price_in" type="text" value="1.75"' in page
    # A model whose price varies is never priced from the list: the owner types prices.
    r = post(owner, "/account/admin/ai/main", {"main_ai": "other", "other_model": "openrouter/auto",
                                               "vision": "claude"})
    assert r.status_code == 400 and 'href="#field-other_price_in"' in r.text


@pytest.mark.parametrize("address, resolves_to, words", [
    ("http://llm.example.com/v1", ["93.184.215.14"], "must start with https://"),
    ("https://user:secret@llm.example.com/v1", ["93.184.215.14"], "user name or password"),
    ("https://llm.example.com/v1?key=1", ["93.184.215.14"], "? or # part"),
    ("https://127.0.0.1/v1", [], "special internet address"),
    ("https://[::1]/v1", [], "special internet address"),
    ("https://localhost/v1", [], "special internet address"),
    ("https://llm.example.com/v1", ["10.1.2.3"], "private network"),
    ("https://llm.example.com/v1", ["93.184.215.14", "192.168.0.9"], "private network"),
    ("https://llm.example.com/v1", ["169.254.169.254"], "private network"),
    ("https://llm.example.com/v1", ["fd00::1"], "private network"),
    ("https://llm.example.com/v1", ["::ffff:10.0.0.1"], "private network"),
    ("https://llm.example.com/v1", [], "could not find llm.example.com"),
])
def test_custom_addresses_must_be_safe(owner, fake_provider, monkeypatch, address, resolves_to, words):
    provider = fake_provider(Provider())
    monkeypatch.setattr(providers, "resolve", lambda host: resolves_to)
    r = _save_other(owner, other_provider="custom", other_name="Example AI", other_address=address)
    assert r.status_code == 400 and words in unescape(r.text)
    assert 'href="#field-other_address"' in r.text
    assert provider.requests == [] and site_ai.get("other_key_enc") == ""


def test_a_custom_provider_is_checked_again_before_each_request(owner, fake_provider, monkeypatch, app):
    provider = fake_provider(Provider())
    r = _save_other(owner, other_provider="custom", other_name="Example AI",
                    other_address="https://llm.example.com/v1/")
    assert h1(r.text) == "The key for Example AI is saved"
    assert str(provider.requests[0].url) == "https://llm.example.com/v1/models"
    assert site_ai.other_provider().base_url == "https://llm.example.com/v1"
    site_ai.save({"main_ai": "other", "other_model": "m", "other_price_in": "1", "other_price_out": "1",
                  "pay_mode": "free", "member_month_cents": 100}, None)
    routed = ai_module.default_factory(app.state.settings)(_user("owner@example.com"))
    provider.answers = [chat("fine")]
    assert routed.text(system="s", prompt="p", purpose="t") == "fine"
    # The name now points somewhere private: nothing is sent.
    monkeypatch.setattr(providers, "resolve", lambda host: ["127.0.0.1"])
    sent = len(provider.requests)
    with pytest.raises(ai_module.AIError) as exc:
        routed.text(system="s", prompt="p", purpose="t")
    assert "nothing was sent" in exc.value.user_message and len(provider.requests) == sent
    assert "Example AI: m" in unescape(owner.get("/account/credits").text)  # the usage page names it


def test_redirects_are_never_followed(owner, fake_provider):
    fake_provider(type("Moves", (Provider,), {
        "__call__": lambda self, r: (self.requests.append(r) or
                                     httpx.Response(301, headers={"location": "https://10.0.0.1/v1/models"}))})())
    r = _save_other(owner, other_provider="custom", other_name="Example AI", other_address="https://llm.example.com/v1")
    assert r.status_code == 400 and "does not follow that" in unescape(r.text)
    assert site_ai.get("other_key_enc") == ""


def test_members_never_type_an_address_or_use_the_owners_custom_choice(user_client, fake_provider):
    provider = fake_provider(Provider())
    page = user_client.get("/account/ai-key")
    assert_accessible(page)
    assert 'value="custom"' not in page.text and "other_address" not in page.text
    assert "Web search steps (Research, Do this for me, Find before you build" in unescape(page.text)
    for choice in ("custom", "anthropic", "evil"):
        r = post(user_client, "/account/ai-key/other", {"other_provider": choice, "other_api_key": OTHER_KEY,
                                                        "other_address": "https://10.0.0.1/v1"})
        assert r.status_code == 400 and 'href="#field-other_provider"' in r.text
    assert provider.requests == [] and _user()["own_api_key_enc"] is None
    assert_accessible(r)


def test_members_choose_a_model_from_the_live_list(user_client, fake_provider):
    fake_provider(Provider(models=[{"id": "grok-9"}, {"id": "grok-9-mini"}]))
    r = post(user_client, "/account/ai-key/other", {"other_provider": "xai", "other_api_key": OTHER_KEY})
    assert h1(r.text) == "Your xAI (Grok) key is connected"
    assert OTHER_KEY not in r.text and "KEY9" in r.text
    assert_accessible(r)
    assert 'value="grok-9-mini"' in r.text and "Prices are optional" in r.text
    r = post(user_client, "/account/ai-key/model", {"own_model": "grok-9", "own_price_in": "abc"})
    assert r.status_code == 400 and 'href="#field-own_price_in"' in r.text
    assert_accessible(r)
    r = post(user_client, "/account/ai-key/model", {"own_model": "grok-9", "own_price_in": "3"})
    assert r.status_code == 400 and "Enter both prices" in r.text
    r = post(user_client, "/account/ai-key/model", {"own_model": "grok-9", "own_price_in": "3", "own_price_out": "15"})
    assert h1(r.text) == "Your model choice is saved"
    row = _user()
    assert (row["own_provider"], row["own_model"], row["own_price_in"], row["own_price_out"]) == (
        "xai", "grok-9", "3.00", "15.00")
    assert h1(user_client.get("/account/ai-key").text) == "Your AI key"
    assert '<a href="/account/ai-key">Your AI key</a>' in user_client.get("/account").text
    r = post(user_client, "/account/ai-key/remove")
    assert h1(r.text) == "Your xAI (Grok) key is removed"
    row = _user()
    assert row["own_api_key_enc"] is None and row["own_provider"] == "anthropic" and row["own_model"] == ""


def test_a_list_that_cannot_be_read_leaves_a_box_to_type_in(user_client, fake_provider, app):
    fake_provider(Provider())
    post(user_client, "/account/ai-key/other", {"other_provider": "deepseek", "other_api_key": OTHER_KEY})
    providers.forget_cache()
    fake_provider(type("Down", (Provider,), {"__call__": lambda self, r: error(503, "down")})())
    page = user_client.get("/account/ai-key")
    assert_accessible(page)
    assert "could not be read" in unescape(page.text) and "You can type the model name instead" in page.text
    r = post(user_client, "/account/ai-key/model", {"own_model_typed": "deepseek-v9"})
    assert h1(r.text) == "Your model choice is saved" and _user()["own_model"] == "deepseek-v9"


def test_the_preset_list_is_complete_and_has_no_model_names():
    by_id = {p.id: p for p in providers.PRESETS}
    assert by_id["openai"].base_url == "https://api.openai.com/v1"
    assert by_id["gemini"].base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert by_id["openrouter"].base_url == "https://openrouter.ai/api/v1" and by_id["openrouter"].lists_prices
    assert by_id["deepseek"].base_url == "https://api.deepseek.com/v1"
    assert by_id["mistral"].base_url == "https://api.mistral.ai/v1"
    assert by_id["groq"].base_url == "https://api.groq.com/openai/v1"
    assert by_id["xai"].base_url == "https://api.x.ai/v1"
    for p in providers.PRESETS:
        assert p.base_url.startswith("https://")
    source = Path(providers.__file__).read_text()
    assert not re.search(r"gpt-\d|gemini-\d|grok-\d|mistral-(large|small)|deepseek-(chat|reasoner)", source)
