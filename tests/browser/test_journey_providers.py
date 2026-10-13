"""Choosing another AI provider, driven in Chromium the way a screen reader user does: the owner saves another
provider's key and makes it the main AI, and a member connects a key from a listed company and chooses its model.
Every control is found by its role and accessible name or by its label. No request leaves the machine: the
providers' answers come from an httpx.MockTransport."""
from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect

from studio import manage, providers, site_ai

from .conftest import PASSWORD, new_page, sign_up
from .test_journey_account import account_page, assert_page_ok, follow_error, heading

pytestmark = pytest.mark.browser

OTHER_KEY = "sk-other-" + "Q1w2E3r4T5y6" * 3 + "KEY9"


@pytest.fixture
def models(monkeypatch):
    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {OTHER_KEY}":
            return httpx.Response(401, json={"error": {"message": "no"}})
        return httpx.Response(200, json={"data": [
            {"id": "sol-6.2", "name": "Sol 6.2", "pricing": {"prompt": "0.000002", "completion": "0.00001"}},
            {"id": "sol-6-mini"}]})

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(answer))
    providers.forget_cache()
    yield
    providers.forget_cache()


def test_owner_chooses_another_main_ai(live, browser, models):
    base = live["base"]
    page = new_page(browser)
    sign_up(page, base, "journey-providers-owner@example.com", "Owner")
    assert manage.make_admin("journey-providers-owner@example.com") == 0
    page.goto(base + "/account/admin/ai")
    assert heading(page) == "AI settings for your key"
    assert_page_ok(page, "owner AI settings")

    page.get_by_role("group", name="Which company the key is from").get_by_label("OpenRouter: ").check()
    key = page.get_by_label("API key from the company")
    key.fill("sk-other-not-the-right-one-1234")
    page.get_by_label("Your password, to save the company's key").fill(PASSWORD)
    page.get_by_role("button", name="Check and save the company's key").click()
    assert heading(page) == "Please check 1 answer: AI settings for your key"
    assert_page_ok(page, "owner AI settings with errors")
    follow_error(page, "OpenRouter did not accept this key, so it was not saved.", key)

    page.get_by_role("group", name="Which company the key is from").get_by_label("OpenRouter: ").check()
    key.fill(OTHER_KEY)
    page.get_by_label("Your password, to save the company's key").fill(PASSWORD)
    page.get_by_role("button", name="Check and save the company's key").click()
    assert heading(page) == "The key for OpenRouter is saved"
    assert_page_ok(page, "owner AI settings after saving the key")
    assert OTHER_KEY not in page.content()

    group = page.get_by_role("group", name="Main AI for members on your key", exact=True)
    group.get_by_label("OpenRouter, with the model and prices below").check()
    page.get_by_role("group", name="Model from OpenRouter's list").get_by_label("sol-6-mini").check()
    page.get_by_role("button", name="Save the main AI").click()
    assert heading(page) == "Please check 2 answers: AI settings for your key"
    assert_page_ok(page, "main AI with errors")
    follow_error(page, "Enter the price per million tokens in (what the AI reads) in US dollars, from OpenRouter's "
                       "price page.", page.get_by_label("Price per million tokens in (what the AI reads), in US dollars"))

    page.get_by_role("group", name="Model from OpenRouter's list").get_by_label("sol-6.2 (Sol 6.2)").check()
    page.get_by_role("button", name="Save the main AI").click()
    assert heading(page) == "The main AI is saved"
    assert_page_ok(page, "main AI saved")
    expect(page.get_by_role("main").get_by_text("The prices are from OpenRouter's list")).to_be_visible()
    assert site_ai.other_model() == "sol-6.2" and site_ai.other_prices() == (2.0, 10.0)
    site_ai.remove_other_key()


def test_member_uses_a_key_from_another_company(live, browser, models):
    base = live["base"]
    page = new_page(browser)
    sign_up(page, base, "journey-providers-member@example.com", "Member")
    account_page(page)
    page.get_by_role("link", name="Your Claude API key", exact=True).click()
    expect(page.get_by_role("main").get_by_text("Web search steps (Research, Do this for me")).to_be_visible()
    page.get_by_role("group", name="Which company the key is from").get_by_label("Mistral: ").check()
    page.get_by_label("API key from that company").fill(OTHER_KEY)
    page.get_by_role("button", name="Check and save the other company's key").click()
    assert heading(page) == "Your Mistral key is connected"
    assert_page_ok(page, "member key from another company")

    page.get_by_role("button", name="Save my model choice").click()
    assert heading(page) == "Please check 1 answer: Your AI key"
    assert_page_ok(page, "member model with errors")
    page.get_by_role("group", name="Model from Mistral's list").get_by_label("sol-6-mini").check()
    page.get_by_role("button", name="Save my model choice").click()
    assert heading(page) == "Your model choice is saved"
    account_page(page)
    expect(page.get_by_role("main").get_by_text("You use your own API key from Mistral ending in KEY9")).to_be_visible()
    page.get_by_role("link", name="Your AI key", exact=True).click()
    page.get_by_role("button", name="Remove my key").click()
    assert heading(page) == "Your Mistral key is removed"
    assert_page_ok(page, "account after removing the other key")
