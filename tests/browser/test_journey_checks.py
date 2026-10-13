"""Peer checks in a real browser, driven the way a screen reader user moves: by headings, labels and names.

Every control is found by its role and accessible name, or by its label, never by CSS, ids or XPath,
so the test itself proves that every control has a usable name. Every page reached is checked with
check_page and check_page_narrow (axe-core, one h1 that matches the title, a main landmark, and no
sideways scrolling at 1280 and 320 pixels wide). axe-core cannot run with JavaScript off, so in the
no-JavaScript journey each page is used without JavaScript and then checked again with it.

Peer checks never call the AI, so no fake answers are registered; the journey confirms the AI is not called.
"""
import re

import pytest

from studio import credits, db

from .conftest import check_page, check_page_narrow, new_page, sign_in, sign_up

pytestmark = pytest.mark.browser

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect


def audit(page, label: str = "") -> None:
    problems = check_page(page, label) + check_page_narrow(page, label)
    assert problems == [], "\n".join(problems)


def h1_text(page) -> str:
    return " ".join(page.get_by_role("heading", level=1).inner_text().split())


def give_credits(email: str, cents: int) -> None:
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
    credits.add(uid, cents, "browser test top-up")


def balance_of(email: str) -> int:
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
    return credits.balance(uid)


def requests_titled(title: str) -> int:
    with db.session() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM check_requests WHERE title = ?", (title,)).fetchone()["n"]


def is_focused(locator) -> bool:
    return locator.evaluate("el => el === document.activeElement")


def follow_error(page, message: str, field) -> None:
    """Follow a link in the error summary and confirm focus lands on the field it names."""
    summary = page.get_by_role("alert")
    expect(summary).to_be_visible()
    summary.get_by_role("link", name=message).click()
    expect(field).to_be_focused()


def go(page, locator) -> None:
    """Activate a link or button and wait for the next page, with or without JavaScript."""
    with page.expect_navigation():
        locator.click()


def open_section(page, base: str) -> None:
    page.goto(base + "/")
    go(page, page.get_by_role("navigation", name="Sections").get_by_role("link", name="Peer checks"))
    assert h1_text(page) == "Peer checks"


def fill_request(page, *, title: str, url: str, what: str, checks: str, reward: str,
                 who: str = "People who use screen readers") -> None:
    page.get_by_label("Name of what you want checked").fill(title)
    page.get_by_label("Web address").fill(url)
    page.get_by_label("What should checkers try?").fill(what)
    page.get_by_role("group", name="Who should check it").get_by_role("radio", name=who, exact=True).check()
    page.get_by_label("How many checks do you want?").select_option(label=checks)
    page.get_by_label("Reward for each check").select_option(label=reward)


def fill_report(page, *, result: str = "Yes, I could do it") -> None:
    page.get_by_label("Screen reader", exact=True).fill("TalkBack")
    page.get_by_label("Screen reader version (optional)").fill("15.0")
    page.get_by_label("Browser", exact=True).fill("Chrome")
    page.get_by_label("Device", exact=True).fill("Samsung Galaxy A15")
    page.get_by_label("What did you hear first when the page opened?").fill("Cake shop, heading level 1")
    page.get_by_role("radio", name=result).check()
    page.get_by_label("Problems you found (optional)").fill("The basket button has no name.")


def test_request_check_close_and_public_record(live, browser):
    base = live["base"]
    asker_email, checker_email = "journey-asker@example.com", "journey-checker@example.com"
    title = "Cake shop journey"

    # ----- the asker opens the section and asks for a check, making mistakes first -------------
    asker = new_page(browser)
    sign_up(asker, base, asker_email, "Asha Journey")
    open_section(asker, base)
    audit(asker, "index, signed in")
    go(asker, asker.get_by_role("link", name="Ask for a check"))
    assert h1_text(asker) == "Ask for a check"
    audit(asker, "new request form")

    go(asker, asker.get_by_role("button", name="Ask for the check"))
    assert h1_text(asker) == "Ask for a check: please fix the problems"
    expect(asker.get_by_role("heading", name="There are 3 problems")).to_be_visible()
    audit(asker, "new request form with errors")
    follow_error(asker, "Enter a short name for what you want checked.", asker.get_by_label("Name of what you want checked"))
    follow_error(asker, "Enter the full web address, starting with https:// or http://.", asker.get_by_label("Web address"))
    follow_error(asker, re.compile("^Say what you want checkers to try"), asker.get_by_label("What should checkers try?"))

    # A reward the asker cannot pay for is a form error that says how to fix it.
    fill_request(asker, title=title, url="https://cakes.example/order", what="Order a chocolate cake.",
                 checks="2 checks", reward="1 dollar per check")
    go(asker, asker.get_by_role("button", name="Ask for the check"))
    assert h1_text(asker) == "Ask for a check: please fix the problems"
    audit(asker, "new request form, not enough credits")
    follow_error(asker, re.compile("^You need \\$2.00 in credits"), asker.get_by_label("Reward for each check"))
    expect(asker.get_by_label("Name of what you want checked")).to_have_value(title)  # what they typed is kept

    give_credits(asker_email, 300)
    # A double press, as happens with a sticky key or a slow phone: one request, credits held once.
    asker.get_by_role("button", name="Ask for the check").dblclick()
    asker.wait_for_url(re.compile(r"/checks/\d+$"))  # the form page had a different address
    asker.wait_for_load_state("networkidle")
    assert h1_text(asker) == f"Your request is open: {title}"
    expect(asker.get_by_role("status")).to_contain_text(re.compile("held from your credits|held once"))
    audit(asker, "request page, just opened")
    assert balance_of(asker_email) == 100
    assert requests_titled(title) == 1
    expect(asker.get_by_role("link", name="Check this now")).to_have_count(0)  # not for your own request
    request_url = asker.url

    # ----- a checker finds it in the queue and checks it, making mistakes first -----------------
    checker = new_page(browser)
    sign_up(checker, base, checker_email, "Chidi Journey")
    open_section(checker, base)
    queue = checker.get_by_role("heading", name="Check someone's work")
    expect(queue).to_be_visible()
    go(checker, checker.get_by_role("link", name=title, exact=True))
    assert h1_text(checker) == f"Check request: {title}"
    expect(checker.get_by_role("link", name=f"Open {title} (the member's own page)")).to_be_visible()
    audit(checker, "request page, checker")
    go(checker, checker.get_by_role("link", name="Check this now"))
    assert h1_text(checker) == f"Check this: {title}"
    audit(checker, "report form")

    checker.get_by_label("Screen reader", exact=True).fill("")
    go(checker, checker.get_by_role("button", name="Send my check"))
    assert h1_text(checker) == f"Check this: {title}: please fix the problems"
    audit(checker, "report form with errors")
    follow_error(checker, "Enter the screen reader you used, for example NVDA or TalkBack.",
                 checker.get_by_label("Screen reader", exact=True))
    follow_error(checker, "Choose whether you could do the task: yes, partly or no.",
                 checker.get_by_role("radio", name="Yes, I could do it"))
    follow_error(checker, re.compile("^Type what you heard first"),
                 checker.get_by_label("What did you hear first when the page opened?"))

    fill_report(checker, result="Partly")
    # A double press again: the check is saved once and the reward is paid once.
    checker.get_by_role("button", name="Send my check").dblclick()
    checker.wait_for_url(re.compile(r"/record\?sent=1$"))
    checker.wait_for_load_state("networkidle")
    assert h1_text(checker) == f"Your check is saved in the record: {title}"
    expect(checker.get_by_role("status")).to_contain_text(re.compile("added to your credits|paid once"))
    audit(checker, "record, just after sending")
    assert balance_of(checker_email) == 100
    expect(checker.get_by_role("heading", name=re.compile("^Check 1: .*could do it partly$"))).to_be_visible()
    expect(checker.get_by_role("heading", name=re.compile("^Check 2"))).to_have_count(0)

    # Checks you did links to the record, and the request has left the checker's queue.
    open_section(checker, base)
    expect(checker.get_by_role("link", name=f"Check record: {title}").first).to_be_visible()
    expect(checker.get_by_role("link", name=title, exact=True)).to_have_count(0)
    audit(checker, "index after checking")

    # ----- the asker closes the request and gets back what was not paid out -------------------
    asker.goto(request_url)
    assert h1_text(asker) == f"Your request is open: {title}"
    expect(asker.get_by_text("The $1.00 held for checks not done yet comes back")).to_be_visible()
    go(asker, asker.get_by_role("button", name="Close this request"))
    assert h1_text(asker) == f"Your request is closed: {title}"
    expect(asker.get_by_role("status")).to_contain_text("$1.00 came back to your credits")
    expect(asker.get_by_role("button", name="Close this request")).to_have_count(0)
    audit(asker, "request page, just closed")
    assert balance_of(asker_email) == 200
    go(asker, asker.get_by_role("link", name=f"Read the check record for {title}"))
    assert h1_text(asker) == f"Check record: {title}"
    audit(asker, "record, signed in as asker")

    # Your requests are listed under Closed requests.
    open_section(asker, base)
    expect(asker.get_by_role("heading", name="Closed requests (1)")).to_be_visible()
    audit(asker, "index with a closed request")

    # ----- anyone can read the public record without an account -------------------------------
    reader = new_page(browser)
    open_section(reader, base)
    audit(reader, "index, signed out")
    go(reader, reader.get_by_role("link", name=f"Check record: {title}"))
    assert h1_text(reader) == f"Check record: {title}"
    expect(reader.get_by_text("1 member checked it with a screen reader. 1 could do it partly.")).to_be_visible()
    expect(reader.get_by_text(re.compile("^Every fingerprint matches"))).to_be_visible()
    expect(reader.get_by_role("heading", name="Not checked")).to_be_visible()
    audit(reader, "record, signed out")
    assert live["fake_ai"].calls == []  # peer checks are people's work, never the AI's
    for p in (asker, checker, reader):
        p.context.close()


def test_own_request_cannot_be_checked(live, browser):
    base = live["base"]
    page = new_page(browser)
    sign_up(page, base, "journey-own@example.com", "Owen Own")
    open_section(page, base)
    go(page, page.get_by_role("link", name="Ask for a check"))
    fill_request(page, title="Own page", url="https://own.example/", what="Find the menu.",
                 checks="1 check", reward="No reward")
    go(page, page.get_by_role("button", name="Ask for the check"))
    assert h1_text(page) == "Your request is open: Own page"
    page.goto(page.url + "/report")
    assert h1_text(page) == "You cannot check this: Own page"
    expect(page.get_by_text("This is your own request.")).to_be_visible()
    audit(page, "refused page")
    go(page, page.get_by_role("link", name="Find other work to check"))
    assert h1_text(page) == "Peer checks"
    page.context.close()


def works_without_js(page, expected_h1: str) -> None:
    assert h1_text(page) == expected_h1
    assert page.get_by_role("heading", level=1).count() == 1
    assert len(page.get_by_role("main").inner_text().strip()) > 40


def audit_twin(twin, url: str, label: str) -> None:
    """axe-core needs JavaScript to run, so the same page is checked again in a JavaScript page signed in as the same member."""
    twin.goto(url)
    audit(twin, label + " (checked with JavaScript on)")


def test_key_pages_work_without_javascript(live, browser):
    base = live["base"]
    asker_email, checker_email = "journey-nojs-asker@example.com", "journey-nojs-checker@example.com"
    title = "No script cafe"
    twins = {}
    for email, name in ((asker_email, "Nadia NoJS"), (checker_email, "Chris NoJS")):
        twins[email] = new_page(browser)
        sign_up(twins[email], base, email, name)

    asker = new_page(browser, javascript=False)
    sign_in(asker, base, asker_email)
    open_section(asker, base)
    works_without_js(asker, "Peer checks")
    audit_twin(twins[asker_email], asker.url, "index")
    go(asker, asker.get_by_role("link", name="Ask for a check"))
    works_without_js(asker, "Ask for a check")
    audit_twin(twins[asker_email], asker.url, "new request form")
    go(asker, asker.get_by_role("button", name="Ask for the check"))
    works_without_js(asker, "Ask for a check: please fix the problems")
    expect(asker.get_by_role("link", name="Enter a short name for what you want checked.")).to_be_visible()
    fill_request(asker, title=title, url="https://cafe.example/", what="Find the opening hours.",
                 checks="2 checks", reward="No reward")
    go(asker, asker.get_by_role("button", name="Ask for the check"))
    works_without_js(asker, f"Your request is open: {title}")
    expect(asker.get_by_role("status")).to_contain_text("Other members can now check it.")
    request_url = asker.url
    audit_twin(twins[asker_email], request_url, "request page, asker")

    checker = new_page(browser, javascript=False)
    sign_in(checker, base, checker_email)
    open_section(checker, base)
    go(checker, checker.get_by_role("link", name=title, exact=True))
    works_without_js(checker, f"Check request: {title}")
    audit_twin(twins[checker_email], checker.url, "request page, checker")
    go(checker, checker.get_by_role("link", name="Check this now"))
    works_without_js(checker, f"Check this: {title}")
    audit_twin(twins[checker_email], checker.url, "report form")
    go(checker, checker.get_by_role("button", name="Send my check"))
    works_without_js(checker, f"Check this: {title}: please fix the problems")  # task result not chosen yet
    fill_report(checker)
    go(checker, checker.get_by_role("button", name="Send my check"))
    works_without_js(checker, f"Your check is saved in the record: {title}")
    audit_twin(twins[checker_email], checker.url, "record after sending")

    asker.goto(request_url)
    go(asker, asker.get_by_role("button", name="Close this request"))
    works_without_js(asker, f"Your request is closed: {title}")
    expect(asker.get_by_role("status")).to_contain_text("Nobody else can check it now.")
    audit_twin(twins[asker_email], asker.url, "closed request")
    go(asker, asker.get_by_role("link", name=f"Read the check record for {title}"))
    works_without_js(asker, f"Check record: {title}")
    expect(asker.get_by_text("1 member checked it with a screen reader. 1 could do the task.")).to_be_visible()
    for p in (asker, checker, *twins.values()):
        p.context.close()


def test_keyboard_order_on_the_request_form(live, browser):
    base = live["base"]
    page = new_page(browser)
    sign_up(page, base, "journey-keys@example.com", "Kim Keys")
    open_section(page, base)
    go(page, page.get_by_role("link", name="Ask for a check"))
    assert h1_text(page) == "Ask for a check"
    page.goto(page.url)  # start from the top of a freshly loaded page, nothing focused
    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="Skip to main content")).to_be_focused()

    first_field = page.get_by_label("Name of what you want checked")
    footer = page.get_by_role("contentinfo").get_by_role("link")
    field_at = footer_at = None
    for step in range(1, 80):
        page.keyboard.press("Tab")
        if field_at is None and is_focused(first_field):
            field_at = step
        if footer_at is None and any(is_focused(footer.nth(i)) for i in range(footer.count())):
            footer_at = step
            break
    assert field_at is not None, "Tab never reached the first field of the form"
    assert footer_at is not None, "Tab never reached the footer links"
    assert field_at < footer_at

    # The skip link moves focus to the main content, and the next Tab goes into the page itself.
    page.goto(page.url)
    page.keyboard.press("Tab")
    page.keyboard.press("Enter")
    expect(page.get_by_role("main")).to_be_focused()
    page.context.close()


# ----- sighted checks of how it looks ----------------------------------------------------------

LOOK_QUESTIONS = ("Does it look finished and professional?", "Is anything broken, overlapping or hard to read?",
                  "Would you trust this page as a visitor or customer?")


def answer(page, question: str, choice: str) -> None:
    page.get_by_role("group", name=question).get_by_role("radio", name=choice, exact=True).check()


def fill_look(page, *, finished="Yes", broken="No", trust="Yes", better="") -> None:
    for question, choice in zip(LOOK_QUESTIONS, (finished, broken, trust)):
        answer(page, question, choice)
    if better:
        page.get_by_label("What would make it better? (optional)").fill(better)


def test_sighted_friend_checks_how_it_looks(live, browser):
    base = live["base"]
    asker_email, friend_email = "journey-look-asker@example.com", "journey-look-friend@example.com"
    title = "Bakery look journey"

    # ----- a member asks for both kinds of check, starting from a link that suggests sighted ----
    asker = new_page(browser)
    sign_up(asker, base, asker_email, "Bea Baker")
    asker.goto(base + "/checks/new?kind=sighted&url=https://bakery.example/&title=" + title.replace(" ", "+"))
    assert h1_text(asker) == "Ask for a check"
    who = asker.get_by_role("group", name="Who should check it")
    expect(who.get_by_role("radio", name="Sighted people, to check how it looks")).to_be_checked()
    audit(asker, "new request form, prefilled for sighted")
    asker.get_by_label("What should checkers try?").fill("Look at the home page and find the price of bread.")
    who.get_by_role("radio", name="Both", exact=True).check()
    go(asker, asker.get_by_role("button", name="Ask for the check"))
    assert h1_text(asker) == f"Your request is open: {title}"
    expect(asker.get_by_text(re.compile("^This request asks people who use screen readers to try the task, "
                                        "and sighted people to check how it looks"))).to_be_visible()
    expect(asker.get_by_text(re.compile("^To ask a sighted friend, family member or volunteer"))).to_be_visible()
    audit(asker, "request page, both kinds wanted")
    request_url = asker.url

    # ----- a sighted friend opens the address they were sent and checks it ---------------------
    friend = new_page(browser)
    sign_up(friend, base, friend_email, "Fran Friend")
    open_section(friend, base)
    queue_item = friend.get_by_role("listitem").filter(has=friend.get_by_role("link", name=title, exact=True))
    expect(queue_item).to_contain_text("Wanted: people who use screen readers, and sighted people to check how it looks.")
    friend.goto(request_url)
    assert h1_text(friend) == f"Check request: {title}"
    go(friend, friend.get_by_role("link", name="Check this now"))
    assert h1_text(friend) == f"Check this: {title}"
    # Both parts are on the page, each under its own heading.
    expect(friend.get_by_role("heading", name="If you used a screen reader")).to_be_visible()
    expect(friend.get_by_role("heading", name="If you are sighted and looked at how it appears")).to_be_visible()
    for question in LOOK_QUESTIONS:
        expect(friend.get_by_role("group", name=question)).to_be_visible()
    audit(friend, "report form, both kinds")

    # Sending without choosing how they checked: one problem, and its link goes to the first choice.
    go(friend, friend.get_by_role("button", name="Send my check"))
    assert h1_text(friend) == f"Check this: {title}: please fix the problems"
    expect(friend.get_by_role("heading", name="There is a problem")).to_be_visible()
    audit(friend, "report form, how you checked not chosen")
    follow_error(friend, re.compile("^Choose how you checked it"),
                 friend.get_by_role("radio", name="With a screen reader"))

    # Sighted, but the questions not answered: only the sighted questions are asked for.
    friend.get_by_role("radio", name="I am sighted and looked at how it appears").check()
    go(friend, friend.get_by_role("button", name="Send my check"))
    expect(friend.get_by_role("heading", name="There are 3 problems")).to_be_visible()
    audit(friend, "report form, sighted questions not answered")
    follow_error(friend, "Choose whether it looks finished and professional: yes, no or not sure.",
                 friend.get_by_role("group", name=LOOK_QUESTIONS[0]).get_by_role("radio", name="Yes", exact=True))
    follow_error(friend, re.compile("^Choose whether anything is broken"),
                 friend.get_by_role("group", name=LOOK_QUESTIONS[1]).get_by_role("radio", name="Yes", exact=True))
    expect(friend.get_by_role("radio", name="I am sighted and looked at how it appears")).to_be_checked()

    fill_look(friend, better="The bread prices are small on my phone; bigger prices would help.")
    go(friend, friend.get_by_role("button", name="Send my check"))
    assert h1_text(friend) == f"Your check is saved in the record: {title}"
    expect(friend.get_by_role("status")).to_contain_text("Thank you for checking.")
    audit(friend, "record after a sighted check")

    # ----- the member reads the record: an outcome they can trust, never a colour --------------
    asker.goto(request_url)
    go(asker, asker.get_by_role("link", name=f"Read the check record for {title}"))
    assert h1_text(asker) == f"Check record: {title}"
    expect(asker.get_by_text("Nobody has checked it with a screen reader yet.")).to_be_visible()
    expect(asker.get_by_text(
        "1 sighted person checked how it looks. They said it looks finished and professional, and they found "
        "nothing broken or hard to read. They would trust it as a visitor or customer. Every answer was a good "
        "one: to them, the look of this page is done well.")).to_be_visible()
    check = asker.get_by_role("heading", name=re.compile("^Check 1: .*, looked at how it appears: looks done well$"))
    expect(check).to_be_visible()
    expect(asker.get_by_role("heading", name="What would make it better, in their own words")).to_be_visible()
    expect(asker.get_by_text("Is anything broken, overlapping or hard to read? No.")).to_be_visible()
    expect(asker.get_by_text("The bread prices are small on my phone; bigger prices would help.")).to_be_visible()
    expect(asker.get_by_text(re.compile("^How it looks was checked by 1 sighted person"))).to_be_visible()
    expect(asker.get_by_text(re.compile("^Every fingerprint matches"))).to_be_visible()
    main_text = asker.get_by_role("main").inner_text().lower()
    assert "colour" not in main_text and "color" not in main_text
    audit(asker, "record with a sighted check, asker")
    assert live["fake_ai"].calls == []
    for p in (asker, friend):
        p.context.close()


def test_sighted_check_works_without_javascript(live, browser):
    base = live["base"]
    asker_email, friend_email = "journey-look-nojs-asker@example.com", "journey-look-nojs-friend@example.com"
    title = "No script bakery"
    twins = {}
    for email, name in ((asker_email, "Nell NoJS"), (friend_email, "Fred NoJS")):
        twins[email] = new_page(browser)
        sign_up(twins[email], base, email, name)

    asker = new_page(browser, javascript=False)
    sign_in(asker, base, asker_email)
    asker.goto(base + "/checks/new")
    fill_request(asker, title=title, url="https://bakery-nojs.example/", what="Look at the menu.",
                 checks="1 check", reward="No reward", who="Both")
    go(asker, asker.get_by_role("button", name="Ask for the check"))
    works_without_js(asker, f"Your request is open: {title}")
    request_url = asker.url

    friend = new_page(browser, javascript=False)
    sign_in(friend, base, friend_email)
    friend.goto(request_url + "/report")
    works_without_js(friend, f"Check this: {title}")
    # Without JavaScript both parts are visible, each under its own heading.
    expect(friend.get_by_role("heading", name="If you used a screen reader")).to_be_visible()
    expect(friend.get_by_label("Screen reader", exact=True)).to_be_visible()
    expect(friend.get_by_role("heading", name="If you are sighted and looked at how it appears")).to_be_visible()
    expect(friend.get_by_role("group", name=LOOK_QUESTIONS[2])).to_be_visible()
    audit_twin(twins[friend_email], friend.url, "report form, both kinds")
    friend.get_by_role("radio", name="I am sighted and looked at how it appears").check()
    fill_look(friend, finished="No", broken="Yes", trust="Not sure", better="The menu covers the prices on a phone.")
    go(friend, friend.get_by_role("button", name="Send my check"))
    works_without_js(friend, f"Your check is saved in the record: {title}")
    expect(friend.get_by_role("heading", name=re.compile("looked at how it appears: found something to fix$"))
           ).to_be_visible()
    expect(friend.get_by_text(re.compile("There is something to fix"))).to_be_visible()
    audit_twin(twins[friend_email], friend.url, "record with a sighted check")
    for p in (asker, friend, *twins.values()):
        p.context.close()
