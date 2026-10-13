"""Your account, How paying works, signing in and out, and the job pages, driven in Chromium the way a screen
reader user does: every control is found by its role and accessible name or by its label, never by CSS, ids or
XPath. Every page reached is checked with check_page and check_page_narrow.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time

import pytest
from playwright.sync_api import expect

from studio import db, jobs, manage
from studio.ai import AIError
from studio.features import account

from .conftest import PASSWORD, check_page, check_page_narrow, new_page, sign_in, sign_up

pytestmark = pytest.mark.browser

GOOD_KEY = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0" * 4 + "WXYZ"
WEBHOOK_SECRET = "whsec_journey"
STRIPE_PAGE = "https://checkout.stripe.test/c/pay/"
# Stands in for the Web Audio API so the test can count the three taps.
COUNT_TAPS = """
window.__taps = 0;
window.AudioContext = function () {
  this.currentTime = 0;
  this.destination = {};
  this.createOscillator = function () {
    return {frequency: {value: 0}, connect: function () {}, start: function () { window.__taps += 1; },
            stop: function () {}};
  };
  this.createGain = function () {
    return {gain: {setValueAtTime: function () {}, exponentialRampToValueAtTime: function () {}},
            connect: function () {}};
  };
};
"""


# ----- helpers ----------------------------------------------------------------------------------

def assert_page_ok(page, label: str = "") -> None:
    """Every page a journey reaches must pass the shared checks, at full width and at 320 pixels."""
    problems = check_page(page, label) + check_page_narrow(page, label)
    assert problems == [], "\n".join(problems)


def assert_no_js_page_ok(page, twin, label: str) -> None:
    """A page opened with JavaScript off. axe needs JavaScript, so the page's structure is checked here with
    locators, and the same address is opened in `twin` (JavaScript on, same member) for the full checks."""
    assert page.get_by_role("heading", level=1).count() == 1, label
    assert page.get_by_role("main").count() == 1, label
    assert " ".join(page.title().split(" – ")[0].split()) == heading(page), label
    assert len(page.get_by_role("main").inner_text().strip()) >= 20, label
    twin.goto(page.url)
    assert heading(twin) == heading(page), label
    assert_page_ok(twin, label + " (JavaScript on)")


def heading(page) -> str:
    return " ".join(page.get_by_role("heading", level=1).inner_text().split())


def is_focused(locator) -> bool:
    return locator.evaluate("el => el === document.activeElement")


def follow_error(page, message: str, field) -> None:
    """The error summary lists the problem as a link; following it puts focus on the field to fix."""
    summary = page.locator(".error-summary").filter(has=page.get_by_role("heading", name="What to check", exact=True))
    expect(summary).to_be_visible()
    summary.get_by_role("link", name=message).click()
    expect(field).to_be_focused()


def account_page(page) -> None:
    page.get_by_role("navigation", name="Your account").get_by_role("link", name="Your account").click()
    page.wait_for_load_state()


def user_id_for(email: str) -> int:
    with db.session() as conn:
        return conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]


def signed(payload: bytes) -> dict:
    t = int(time.time())
    sig = hmac.new(WEBHOOK_SECRET.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return {"Stripe-Signature": f"t={t},v1={sig}", "Content-Type": "application/json"}


class SlowJob:
    """A background job that waits until the test lets it finish, like slow AI work."""

    def __init__(self, user_id: int, title: str, finish):
        self.release = threading.Event()
        self.started = threading.Event()
        self.job_id = ""
        self._finish = finish

        def work(ctx):
            self.job_id = ctx.job_id
            ctx.progress("Reading your idea")
            self.started.set()
            assert self.release.wait(60)
            return self._finish(ctx)

        self.thread = threading.Thread(target=jobs.submit, args=(user_id, "journey.test", title, work), daemon=True)
        self.thread.start()
        assert self.started.wait(10)

    def finish(self) -> None:
        self.release.set()
        self.thread.join(10)


@pytest.fixture
def payments(live, monkeypatch):
    """Turn payments on for one test, with Stripe's Checkout page and API replaced."""
    settings = live["app"].state.settings
    old = (settings.stripe_secret_key, settings.stripe_webhook_secret, settings.anthropic_api_key)
    settings.stripe_secret_key, settings.stripe_webhook_secret = "sk_test_journey", WEBHOOK_SECRET
    settings.anthropic_api_key = "sk-ant-site-key-for-credits"
    made = []

    def fake_checkout(secret_key, **kwargs):
        session_id = f"cs_journey_{len(made) + 1}"
        made.append((session_id, kwargs))
        return {"id": session_id, "url": STRIPE_PAGE + session_id}

    monkeypatch.setattr(account, "create_checkout_session", fake_checkout)
    yield made
    settings.stripe_secret_key, settings.stripe_webhook_secret, settings.anthropic_api_key = old


# ----- signing up, in and out -------------------------------------------------------------------

def test_register_sign_out_and_sign_in(live, browser):
    base = live["base"]
    page = new_page(browser)
    page.goto(base + "/")
    assert heading(page) == "Build your ideas with AI: websites, apps, writing and research"
    assert_page_ok(page, "home signed out")
    for name, h1 in [("How this site works", "How this site works"), ("Accessibility", "Accessibility"),
                     ("How paying works", "How paying works")]:
        page.get_by_role("navigation", name="About this site").get_by_role("link", name=name).click()
        assert heading(page) == h1
        assert_page_ok(page, h1)
    page.get_by_role("link", name="Create an account").first.click()
    assert heading(page) == "Create your account"
    assert_page_ok(page, "register")

    # Mistakes first: nothing filled in, then a password that is too short.
    page.get_by_role("button", name="Create account").click()
    assert heading(page) == "Please check 3 answers: Create your account"
    assert_page_ok(page, "register with errors")
    follow_error(page, "Enter your email address, like name@example.com.", page.get_by_label("Email address"))
    page.get_by_label("Email address").fill("journey-register@example.com")
    page.get_by_label("Your name").fill("Zoë Journey")
    password = page.get_by_label("Password", exact=True)
    password.fill("short")
    # "Show my password" lets the member hear or read back what they typed.
    page.get_by_label("Show my password").check()
    expect(password).to_have_attribute("type", "text")
    page.get_by_label("Show my password").uncheck()
    expect(password).to_have_attribute("type", "password")
    page.get_by_role("button", name="Create account").click()
    follow_error(page, "Use at least 10 characters for your password.", password)
    expect(page.get_by_label("Your name")).to_have_value("Zoë Journey")

    password.fill(PASSWORD)
    page.get_by_label("Screen reader, braille display or magnifier you use most").fill("TalkBack")
    page.get_by_role("button", name="Create account").click()
    assert heading(page) == "Your account is ready: save your recovery code"
    assert_page_ok(page, "recovery code after registering")
    code_box = page.get_by_role("textbox", name="Your recovery code")
    recovery_code = code_box.input_value()
    assert len(recovery_code) == 19
    expect(code_box).to_have_attribute("readonly", "")
    # A reload does not lose the code.
    page.reload()
    expect(page.get_by_text("This is the same code as before.")).to_be_visible()
    assert page.get_by_role("textbox", name="Your recovery code").input_value() == recovery_code
    page.get_by_role("button", name="Copy the code").click()
    expect(page.get_by_role("status").filter(has_text="Cop")).to_be_visible()
    page.get_by_role("button", name="I have saved my code: continue").click()
    assert heading(page) == "Your account is ready"
    expect(page.get_by_role("heading", name="Your next steps", level=2)).to_be_visible()
    assert_page_ok(page, "home after registering")

    page.get_by_role("button", name="Sign out").click()
    assert heading(page) == "You are signed out"
    assert_page_ok(page, "home after signing out")

    page.get_by_role("navigation", name="Your account").get_by_role("link", name="Sign in").click()
    assert heading(page) == "Sign in"
    assert_page_ok(page, "sign in")
    page.get_by_label("Email address").fill("journey-register@example.com")
    page.get_by_label("Password").fill("not my password")
    page.get_by_role("button", name="Sign in").click()
    assert heading(page) == "Please check 1 answer: Sign in"
    assert_page_ok(page, "sign in with errors")
    follow_error(page, "The email or password is not right. Check both and try again.",
                 page.get_by_label("Email address"))
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    assert heading(page) == "You are signed in"
    assert_page_ok(page, "home after signing in")

    # Forgotten password: reset it with the recovery code, from the sign-in page.
    page.get_by_role("button", name="Sign out").click()
    page.get_by_role("navigation", name="Your account").get_by_role("link", name="Sign in").click()
    page.get_by_role("link", name="Reset it with your recovery code").click()
    assert heading(page) == "Reset your password with your recovery code"
    assert_page_ok(page, "reset password")
    page.get_by_label("Email address").fill("journey-register@example.com")
    page.get_by_label("Recovery code").fill(recovery_code.lower().replace("-", " "))
    page.get_by_label("New password", exact=True).fill("a new password for the journey")
    page.get_by_role("button", name="Set my new password").click()
    assert heading(page) == "Your password is changed: save your new recovery code"
    assert_page_ok(page, "new recovery code after reset")
    assert page.get_by_role("textbox", name="Your recovery code").input_value() != recovery_code


def test_sign_in_returns_to_the_page_asked_for(live, browser):
    base = live["base"]
    page = new_page(browser)
    sign_up(page, base, "journey-next@example.com", "Next")
    page.get_by_role("button", name="Sign out").click()
    page.goto(base + "/account/credits")
    assert heading(page) == "Sign in"
    expect(page.get_by_text("Sign in to see Your account. You come straight back to the page afterwards.")).to_be_visible()
    page.get_by_label("Email address").fill("journey-next@example.com")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    assert heading(page) == "Your credits and AI usage"
    expect(page.get_by_role("status").filter(has_text="You are signed in.")).to_be_visible()
    assert_page_ok(page, "credits after signing in")


# ----- your account -----------------------------------------------------------------------------

def test_profile_journey(live, browser):
    base = live["base"]
    page = new_page(browser)
    sign_up(page, base, "journey-profile@example.com", "Profile Person")
    account_page(page)
    assert heading(page) == "Your account"
    assert_page_ok(page, "account")
    expect(page.get_by_text("journey-profile@example.com")).to_be_visible()
    expect(page.get_by_text("To start using the AI, connect your own Claude API key.")).to_be_visible()

    page.get_by_role("link", name="Your profile and preferences").click()
    assert heading(page) == "Your profile and preferences"
    assert_page_ok(page, "profile")
    page.get_by_label("Your name").fill("")
    page.get_by_label("Device you use most").fill("x" * 150)
    page.get_by_role("button", name="Save my profile").click()
    assert heading(page) == "Please check 2 answers: Your profile and preferences"
    assert_page_ok(page, "profile with errors")
    follow_error(page, "Enter your name. Other members see your code name unless you choose to show your name.",
                 page.get_by_label("Your name"))
    follow_error(page, "Use 100 characters or fewer for your device.", page.get_by_label("Device you use most"))

    # A Claude API key pasted into a field other members can see is refused and not shown again.
    page.get_by_label("Your name").fill("Amina sk-ant-api03-Ab12Cd34Ef56")
    page.get_by_label("Device you use most").fill("Android phone")
    page.get_by_role("button", name="Save my profile").click()
    follow_error(page, "This looks like a Claude API key, so it was not saved. Please take it out: other members "
                       "could see it. To use your key, connect it on Your account.", page.get_by_label("Your name"))
    expect(page.get_by_label("Your name")).to_have_value("")

    page.get_by_label("Your name").fill("Amina 王芳")
    page.get_by_label("Your main language").select_option(label="Swahili")
    page.get_by_label("Blind or low vision since").select_option(label="Later in life")
    page.get_by_label("Which fits you best?").select_option(label="Low vision")
    page.get_by_role("group", name="How you like answers").get_by_label("Full detail").check()
    page.get_by_label("Show my name on peer checks instead of my code name").check()
    page.get_by_role("button", name="Save my profile").click()
    assert heading(page) == "Your profile is saved"
    assert_page_ok(page, "account after saving the profile")
    expect(page.get_by_text("Device: Android phone")).to_be_visible()
    expect(page.get_by_text("Answer style: Full detail")).to_be_visible()
    expect(page.get_by_text("Peer checks show your name")).to_be_visible()
    expect(page.get_by_text("Blind or low vision since: Later in life")).to_be_visible()
    expect(page.get_by_text("Which fits you best: Low vision")).to_be_visible()
    page.reload()
    assert heading(page) == "Your account"


def test_words_survive_being_signed_out_and_an_expired_form(live, browser):
    base = live["base"]
    page = new_page(browser)
    sign_up(page, base, "journey-kept@example.com", "Kept")
    page.goto(base + "/account/profile")
    page.get_by_label("Your name").fill("Kept Words " + "long name " * 6)
    page.get_by_label("Device you use most").fill("A braille display and an iPhone")
    # Signed out on every device (for example from another phone) before pressing Save.
    with db.session() as conn:
        conn.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE email = 'journey-kept@example.com'")
    page.get_by_role("button", name="Save my profile").click()
    assert heading(page) == "Sign in"
    expect(page.get_by_text("You were signed out before this was saved. Sign in and your words will be put back "
                            "in the form. Nothing was saved yet.")).to_be_visible()
    page.get_by_label("Email address").fill("journey-kept@example.com")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    assert heading(page) == "Your profile and preferences"
    expect(page.get_by_label("Device you use most")).to_have_value("A braille display and an iPhone")
    notice = page.get_by_role("status").filter(has_text="Your words are back in the form.")
    expect(notice).to_be_visible()
    # With JavaScript, focus goes to the notice, so keyboard and magnifier users land on it.
    expect(page.locator("main .messages")).to_be_focused()
    assert_page_ok(page, "profile with the words put back")

    # The form stopped working (signed out and in again in another tab): the words come back, with a new form.
    other = page.context.new_page()
    other.goto(base + "/")
    other.get_by_role("button", name="Sign out").click()
    sign_in(other, base, "journey-kept@example.com")
    page.get_by_label("Device you use most").fill("Changed in the old tab")
    page.get_by_role("button", name="Save my profile").click()
    assert heading(page) == "Your profile and preferences"
    expect(page.get_by_text("This form needed refreshing to keep your account safe, so nothing was saved yet."))\
        .to_be_visible()
    expect(page.get_by_label("Device you use most")).to_have_value("Changed in the old tab")
    page.get_by_role("button", name="Save my profile").click()
    assert heading(page) == "Your profile is saved"
    expect(page.get_by_text("Device: Changed in the old tab")).to_be_visible()


def test_the_heading_is_close_to_the_top(live, browser):
    base = live["base"]
    page = new_page(browser, width=390)
    sign_up(page, base, "journey-top@example.com", "Top")
    page.goto(base + "/account")
    # Only the skip link, the site name, the menu link and the 3 account controls come before the heading.
    before = page.evaluate("""() => {
        const h1 = document.querySelector('h1');
        return [...document.querySelectorAll('a[href], button')].filter(
            el => el.compareDocumentPosition(h1) & Node.DOCUMENT_POSITION_FOLLOWING).length;
    }""")
    assert before == 6
    page.get_by_role("link", name="Menu: all sections").click()
    sections = page.get_by_role("navigation", name="Sections")
    expect(sections).to_be_focused()
    sections.get_by_role("link", name="Build", exact=True).click()
    expect(page.get_by_role("navigation", name="Sections").get_by_role("link", name="Build", exact=True))\
        .to_have_attribute("aria-current", "page")


def test_profile_form_keyboard_order(live, browser):
    base = live["base"]
    page = new_page(browser)
    sign_up(page, base, "journey-keys@example.com", "Keys")
    page.goto(base + "/account/profile")
    page.keyboard.press("Tab")
    skip = page.get_by_role("link", name="Skip to main content")
    expect(skip).to_be_focused()
    name_field = page.get_by_label("Your name")
    footer_links = page.get_by_role("navigation", name="About this site").get_by_role("link")
    reached_field = False
    for _ in range(40):
        page.keyboard.press("Tab")
        if is_focused(name_field):
            reached_field = True
            break
        for i in range(footer_links.count()):
            assert not is_focused(footer_links.nth(i)), "a footer link came before the form's first field"
    assert reached_field, "Tab never reached the Your name field"
    # Skip link: Enter moves to the main content, and the next Tab stays inside it.
    page.goto(base + "/account/profile")
    page.keyboard.press("Tab")
    page.keyboard.press("Enter")
    expect(page.get_by_role("main")).to_be_focused()


def test_api_key_journey(live, browser, monkeypatch):
    checked = []

    def fake_verify(key):
        checked.append(key)
        return True if key == GOOD_KEY else "Anthropic did not accept this key, so it was not saved."

    monkeypatch.setattr(account, "verify_api_key", fake_verify)
    base = live["base"]
    page = new_page(browser)
    sign_up(page, base, "journey-key@example.com", "Key Person")
    account_page(page)
    page.get_by_role("link", name="Your Claude API key", exact=True).click()
    assert heading(page) == "Your Claude API key"
    assert_page_ok(page, "api key")
    expect(page.get_by_role("link", name="the Anthropic Console (opens Anthropic's website)")).to_be_visible()

    key_field = page.get_by_label("Claude API key")
    key_field.fill("sk-proj-" + "x" * 50)
    page.get_by_label("Show the key, to check what you pasted").check()
    expect(key_field).to_have_attribute("type", "text")
    page.get_by_role("button", name="Check and save my key").click()
    assert heading(page) == "Please check 1 answer: Your Claude API key"
    expect(key_field).to_have_attribute("type", "password")
    assert_page_ok(page, "api key with errors")
    follow_error(page, "A Claude API key starts with sk-ant-. Check you copied the whole key.", key_field)
    expect(key_field).to_have_value("")
    assert checked == []

    key_field.fill("sk-ant-api03-" + "Z" * 40)
    page.get_by_role("button", name="Check and save my key").click()
    follow_error(page, "Anthropic did not accept this key, so it was not saved.", key_field)

    key_field.fill(GOOD_KEY)
    page.get_by_role("button", name="Check and save my key").click()
    assert heading(page) == "Your Claude API key is connected"
    assert_page_ok(page, "account after connecting the key")
    expect(page.get_by_role("main").get_by_text("You use your own Claude API key ending in WXYZ")).to_be_visible()
    assert GOOD_KEY not in page.content()

    page.get_by_role("link", name="Your Claude API key", exact=True).click()
    assert heading(page) == "Your Claude API key"
    assert GOOD_KEY not in page.content()
    page.get_by_role("button", name="Remove my key").click()
    assert heading(page) == "Your Claude API key is removed"
    assert_page_ok(page, "account after removing the key")
    expect(page.get_by_text("To start using the AI, connect your own Claude API key.")).to_be_visible()


def test_credits_page_without_payments(live, browser):
    base = live["base"]
    page = new_page(browser)
    sign_up(page, base, "journey-credits@example.com", "Credits")
    uid = user_id_for("journey-credits@example.com")
    with db.session() as conn:
        conn.execute("INSERT INTO credit_ledger (user_id, delta_cents, reason, ref, created_at) VALUES"
                     " (?, 250, 'Gift from the site: welcome', '', '2026-10-01T10:00:00+00:00')", (uid,))
        conn.execute("INSERT INTO ai_usage (user_id, purpose, model, input_tokens, output_tokens, web_searches,"
                     " cost_cents, paid_by, created_at) VALUES (?, 'build.understand', 'claude-opus-5-5', 100, 50,"
                     " 0, 3, 'own_key', '2026-10-02T10:00:00+00:00')", (uid,))
    account_page(page)
    page.get_by_role("link", name="Your credits and AI usage").click()
    assert heading(page) == "Your credits and AI usage"
    assert_page_ok(page, "credits")
    history = page.get_by_role("table", name="Credit history, newest first")
    expect(history.get_by_role("cell", name="Gift from the site: welcome")).to_be_visible()
    expect(history.get_by_role("cell", name="1 October 2026")).to_be_visible()
    usage = page.get_by_role("table", name="AI usage history, newest first")
    expect(usage.get_by_role("cell", name="Build: understand")).to_be_visible()
    expect(page.get_by_text("Credits are not on sale on this site yet.")).to_be_visible()
    expect(page.get_by_role("link", name="Buy credits")).to_have_count(0)

    account_page(page)
    expect(page.get_by_role("link", name="Buy credits")).to_have_count(0)
    page.goto(base + "/account/credits/buy")  # an old bookmark
    assert heading(page) == "Buy credits"
    assert_page_ok(page, "buy credits, not set up")
    expect(page.get_by_role("button", name="Go to the payment page")).to_have_count(0)
    expect(page.get_by_role("heading", name="Packs")).to_have_count(0)
    page.get_by_role("link", name="connecting your own Claude API key").click()
    assert heading(page) == "Your Claude API key"


def test_buying_credits_journey(live, browser, payments):
    base = live["base"]
    page = new_page(browser)
    sign_up(page, base, "journey-buy@example.com", "Buyer")
    uid = user_id_for("journey-buy@example.com")
    # Stripe's own page is not part of this site. Chromium does not let a test intercept the redirect to it, so
    # the form's answer is checked here: a redirect to Stripe is replaced by a stand-in page that offers paying
    # or going back, and any other answer (such as the form with errors) passes through unchanged.
    stand_in = ('<!doctype html><html lang="en"><title>Pay</title><main><h1>Pay</h1>'
                f'<a href="{base}/account/credits?paid=1">Pay and go back</a> '
                f'<a href="{base}/account/credits/buy?cancelled=1">Go back without paying</a></main></html>')
    redirects = []

    def via_stripe(route):
        if route.request.method != "POST":
            return route.continue_()
        response = route.fetch(max_redirects=0)
        location = response.headers.get("location", "")
        if response.status == 303 and location.startswith(STRIPE_PAGE):
            redirects.append(location)
            return route.fulfill(status=200, content_type="text/html", body=stand_in)
        return route.fulfill(response=response)

    page.route(base + "/account/credits/buy", via_stripe)

    page.goto(base + "/pricing")
    assert_page_ok(page, "pricing with payments")
    expect(page.get_by_role("table", name="Estimated cost per task in US dollars, with your own key and with "
                                          "credits.")).to_be_visible()
    page.get_by_role("main").get_by_role("link", name="Buy credits").click()
    assert heading(page) == "Buy credits"
    assert_page_ok(page, "buy credits")

    page.get_by_role("button", name="Go to the payment page").click()
    assert heading(page) == "Please check 1 answer: Buy credits"
    assert_page_ok(page, "buy credits with errors")
    follow_error(page, "Choose how many credits to buy.",
                 page.get_by_role("group", name="How many credits").get_by_label("$5.00 of credits"))

    page.get_by_role("group", name="How many credits").get_by_label("$10.00 of credits").check()
    page.get_by_role("button", name="Go to the payment page").click()
    assert redirects == [STRIPE_PAGE + "cs_journey_1"]
    page.get_by_role("link", name="Pay and go back").click()
    assert heading(page) == "Thank you: your payment went to Stripe"
    assert_page_ok(page, "credits after paying")
    assert payments[0][1]["amount_cents"] == 1000 and payments[0][1]["user_id"] == uid

    # Stripe tells the site the payment went through, twice (it retries); credits are added once.
    event = json.dumps({"id": "evt_journey", "type": "checkout.session.completed", "data": {"object": {
        "id": "cs_journey_1", "payment_status": "paid", "amount_total": 1000, "currency": "usd",
        "client_reference_id": str(uid), "metadata": {"user_id": str(uid), "credits_cents": "1000"}}}}).encode()
    for expected in ("credited", "duplicate"):
        reply = page.request.post(base + "/payments/stripe/webhook", data=event, headers=signed(event))
        assert reply.status == 200 and reply.json()["result"] == expected
    forged = page.request.post(base + "/payments/stripe/webhook", data=event,
                               headers={"Stripe-Signature": "t=1,v1=00", "Content-Type": "application/json"})
    assert forged.status == 400
    page.reload()
    expect(page.get_by_text("Credit balance: $10.00.")).to_be_visible()
    expect(page.get_by_role("table", name="Credit history, newest first")
           .get_by_role("cell", name="Bought credits")).to_be_visible()
    account_page(page)
    expect(page.get_by_text("You use credits. Balance: $10.00.")).to_be_visible()

    # Leaving Stripe's page without paying.
    page.get_by_role("link", name="Buy credits").click()
    page.get_by_role("group", name="How many credits").get_by_label("$5.00 of credits").check()
    page.get_by_role("button", name="Go to the payment page").click()
    page.get_by_role("link", name="Go back without paying").click()
    assert heading(page) == "You left the payment page: nothing was charged"
    assert_page_ok(page, "buy credits after cancelling")


def test_admin_journey(live, browser):
    base = live["base"]
    friend = new_page(browser)
    sign_up(friend, base, "journey-friend@example.com", "Friend")
    page = new_page(browser)
    sign_up(page, base, "journey-owner@example.com", "Owner")
    page.goto(base + "/account/admin")
    assert heading(page) == "Page not found"
    assert_page_ok(page, "admin as a member")

    assert manage.make_admin("journey-owner@example.com") == 0
    account_page(page)
    page.get_by_role("link", name="Site owner tools").click()
    assert heading(page) == "Site owner tools"
    assert_page_ok(page, "admin")
    page.get_by_label("Member's email address").fill("nobody@example.com")
    page.get_by_label("Amount in US dollars").fill("lots")
    page.get_by_role("button", name="Give credits").click()
    assert heading(page) == "Please check 3 answers: Site owner tools"
    assert_page_ok(page, "admin with errors")
    follow_error(page, "No member has this email address.", page.get_by_label("Member's email address"))
    follow_error(page, "Enter an amount in US dollars, like 5 or 2.50.", page.get_by_label("Amount in US dollars"))
    follow_error(page, "Say why, for example: thank you for answering questions in Ask the community.",
                 page.get_by_label("Reason"))

    page.get_by_label("Member's email address").fill("Journey-Friend@example.com")
    page.get_by_label("Amount in US dollars").fill("2.50")
    page.get_by_label("Reason").fill("Thank you for helping in Help")
    page.get_by_role("button", name="Give credits").click()
    assert heading(page) == "Site owner tools: gave $2.50 of credits to journey-friend@example.com"
    assert_page_ok(page, "admin after giving credits")

    friend.goto(base + "/account/credits")
    expect(friend.get_by_role("cell", name="Gift from the site: Thank you for helping in Help")).to_be_visible()


def test_delete_account_journey(live, browser):
    base = live["base"]
    page = new_page(browser)
    sign_up(page, base, "journey-delete@example.com", "Leaving")
    account_page(page)
    page.get_by_role("link", name="Delete your account").click()
    assert heading(page) == "Delete your account"
    assert_page_ok(page, "delete")
    page.get_by_role("button", name="Delete my account").click()
    assert heading(page) == "Please check 1 answer: Delete your account"
    assert_page_ok(page, "delete with errors")
    box = page.get_by_label("I understand. Delete my account.")
    follow_error(page, "To delete your account, tick the box that says I understand. Your account is not deleted.", box)
    expect(box).to_have_attribute("aria-invalid", "true")
    box.check()
    page.get_by_role("button", name="Delete my account").click()
    assert heading(page) == "Your account is deleted"
    assert_page_ok(page, "home after deleting")
    expect(page.get_by_role("navigation", name="Your account").get_by_role("link", name="Sign in")).to_be_visible()

    page.goto(base + "/login")
    page.get_by_label("Email address").fill("journey-delete@example.com")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    assert heading(page) == "Please check 1 answer: Sign in"


# ----- job pages --------------------------------------------------------------------------------

def test_job_pages_with_javascript(live, browser):
    base = live["base"]
    page = new_page(browser)
    page.add_init_script(COUNT_TAPS)
    sign_up(page, base, "journey-jobs@example.com", "Jobs")
    uid = user_id_for("journey-jobs@example.com")
    live["fake_ai"].on("journey.summary", "A short summary.")
    live["fake_ai"].on("journey.fail", AIError("The AI is busy right now (rate limit). Please try again in a minute."))

    page.goto(base + "/jobs")
    assert heading(page) == "Your recent work"
    expect(page.get_by_text("No work yet.")).to_be_visible()
    assert_page_ok(page, "jobs, empty")

    # Work that finishes while the page is open: the page checks by itself, taps three times and moves focus.
    job = SlowJob(uid, "Summarise my notes",
                  lambda ctx: live["fake_ai"].text(system="s", prompt="p", purpose="journey.summary") and "/account")
    page.goto(base + f"/jobs/{job.job_id}")
    assert heading(page) == "Working on it: Summarise my notes"
    expect(page.get_by_text("Current step: Reading your idea")).to_be_visible()
    expect(page.get_by_label("Play three short taps when it is done")).to_be_checked()
    assert_page_ok(page, "job working")
    job.finish()
    result = page.get_by_role("link", name="Done. Open the result: Summarise my notes")
    expect(result).to_be_focused(timeout=20_000)
    assert page.evaluate("window.__taps") == 3
    # The heading and the title say it is done, and "Check again" is gone.
    assert heading(page) == "Done: Summarise my notes" and page.title().startswith("Done: Summarise my notes")
    expect(page.get_by_role("link", name="Check again")).to_have_count(0)
    page.keyboard.press("Enter")
    page.wait_for_url(base + "/account")
    assert heading(page) == "Your account"

    # Taps turned off: no sound, focus still moves.
    job = SlowJob(uid, "Quiet work", lambda ctx: "/account")
    page.goto(base + f"/jobs/{job.job_id}")
    page.get_by_label("Play three short taps when it is done").uncheck()
    job.finish()
    expect(page.get_by_role("link", name="Done. Open the result: Quiet work")).to_be_focused(timeout=20_000)
    assert page.evaluate("window.__taps") == 0

    # Work that fails while the page is open: the heading says so, the status message gives the reason, and
    # focus moves to the way on.
    job = SlowJob(uid, "Research my question",
                  lambda ctx: live["fake_ai"].text(system="s", prompt="p", purpose="journey.fail"))
    page.goto(base + f"/jobs/{job.job_id}")
    job.finish()
    expect(page.get_by_role("link", name="See all your recent work")).to_be_focused(timeout=20_000)
    expect(page.get_by_role("status")).to_contain_text("The AI is busy right now (rate limit).")
    assert heading(page) == "This did not finish: Research my question"
    assert page.title().startswith("This did not finish: Research my question")
    page.reload()
    assert heading(page) == "This did not finish: Research my question"
    expect(page.get_by_text("The AI is busy right now (rate limit). Please try again in a minute.")).to_be_visible()
    assert_page_ok(page, "job failed")

    page.get_by_role("link", name="See all your recent work").click()
    assert heading(page) == "Your recent work"
    assert_page_ok(page, "jobs list")
    expect(page.get_by_role("heading", name="Did not finish (1)")).to_be_visible()
    expect(page.get_by_role("heading", name="Done (2)")).to_be_visible()
    page.get_by_role("link", name="Research my question").click()
    assert heading(page) == "This did not finish: Research my question"


def test_job_pages_without_javascript(live, browser):
    base = live["base"]
    twin = new_page(browser)
    sign_up(twin, base, "journey-nojs-jobs@example.com", "No JS Jobs")
    page = new_page(browser, javascript=False)
    sign_in(page, base, "journey-nojs-jobs@example.com")
    assert heading(page) == "You are signed in"
    uid = user_id_for("journey-nojs-jobs@example.com")
    job = SlowJob(uid, "Build my cake shop", lambda ctx: "/account/credits")
    page.goto(base + "/jobs")
    expect(page.get_by_role("heading", name="Still working (1)")).to_be_visible()
    assert_no_js_page_ok(page, twin, "jobs list, no JavaScript")
    page.get_by_role("link", name="Build my cake shop").click()
    assert heading(page) == "Working on it: Build my cake shop"
    expect(page.get_by_label("Play three short taps when it is done")).to_be_hidden()
    assert_no_js_page_ok(page, twin, "job working, no JavaScript")
    page.get_by_role("link", name="Check again").click()
    assert heading(page) == "Working on it: Build my cake shop"
    job.finish()
    page.get_by_role("link", name="Check again").click()
    assert heading(page) == "Your credits and AI usage"


def test_account_pages_without_javascript(live, browser, monkeypatch):
    monkeypatch.setattr(account, "verify_api_key", lambda key: True)
    base = live["base"]
    twin = new_page(browser)
    sign_up(twin, base, "journey-nojs@example.com", "No JS")
    page = new_page(browser, javascript=False)
    sign_in(page, base, "journey-nojs@example.com")
    assert heading(page) == "You are signed in"
    for path, h1 in [("/account", "Your account"), ("/account/profile", "Your profile and preferences"),
                     ("/account/ai-key", "Your Claude API key"), ("/account/credits", "Your credits and AI usage"),
                     ("/account/credits/buy", "Buy credits"), ("/account/delete", "Delete your account"),
                     ("/pricing", "How paying works"), ("/jobs", "Your recent work")]:
        page.goto(base + path)
        assert heading(page) == h1, path
        assert_no_js_page_ok(page, twin, path + " without JavaScript")
    page.goto(base + "/account/profile")
    page.get_by_label("Your name").fill("")
    page.get_by_role("button", name="Save my profile").click()
    follow_error(page, "Enter your name. Other members see your code name unless you choose to show your name.",
                 page.get_by_label("Your name"))
    page.get_by_label("Your name").fill("Still No JS")
    page.get_by_role("button", name="Save my profile").click()
    assert heading(page) == "Your profile is saved"
    page.goto(base + "/account/ai-key")
    page.get_by_label("Claude API key").fill(GOOD_KEY)
    page.get_by_role("button", name="Check and save my key").click()
    assert heading(page) == "Your Claude API key is connected"
    page.get_by_role("button", name="Sign out").click()
    assert heading(page) == "You are signed out"
