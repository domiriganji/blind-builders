---
title: Doing things on the web safely with AI
summary: Never share passwords or codes, check web addresses before you trust a page, why AI agents that click for you succeed only about half the time today, and how Do this for me keeps you in control.
last_checked: 2026-10
order: 6
---

## The short version

- Never give a password, a one-time code, a card number or an API key to an AI, a chat, or a person who contacts you. This site refuses them.
- Before you sign in or pay anywhere, check the web address. Listen to the part just before the first single slash.
- AI agents that click around apps for you are only about half reliable today: in a 2026 study with blind users, the best one succeeded 52.5 percent of the time.
- [Do this for me](/do) finds information and prepares tasks, and you decide what gets sent. It does not sign in as you or pay for you.

## Never share passwords or codes

- **Passwords** belong only in the sign-in box of the real website, typed by you or filled by your password manager.
- **One-time codes** sent by text message, email or an app prove that you are the one signing in. Anyone who has the code can get in as you. A real bank or company will never ask you to read a code back to them.
- **Card numbers** go only into the payment page of a shop you chose and checked.
- **API keys**, such as a Claude API key, go only on the account page of the service that uses them. On this site, that is [your account page](/account).

This site refuses questions, answers and requests that look like they contain any of these. If you are refused by mistake, reword the text, for example write "my password is not accepted" instead of including the password.

If you shared a password or code by mistake, change the password straight away on the real website, and tell your bank if it was a card number.

## Check the web address

The address tells you who really runs a page. With a screen reader, you have to ask for it on purpose:

- On Windows in Chrome, Edge or Firefox: press Ctrl+L, Alt+D or F6 to move to the address bar, and your screen reader reads the address. Press Escape to go back to the page.
- On a Mac: press Command+L.
- On a phone: move to the address bar at the top or bottom of the browser.

How to read it:

1. Find the part after `https://` and before the next single slash. That is the website's name, its domain.
2. Listen to the end of that part. In this address, the domain is `example.com`:

   ```
   https://www.example.com/sign-in
   ```

3. Be careful with long names. In this address, the domain is `account-check.net`, not `example.com`:

   ```
   https://example.com.account-check.net/sign-in
   ```

4. Listen for small spelling changes, such as a number one instead of the letter l, or an extra word like "secure" or "verify".

Safer habits:

- Do not sign in from a link in an email or text message. Open the site from your bookmarks, or type the address yourself.
- Be suspicious of any message that says you must act right now.
- If a page asks for more than it should, such as your full card details to "check your identity", leave.

## Why AI agents that click for you are only half reliable today

Some AI tools can operate apps and websites for you by clicking and typing. In a three-week study published in 2026, 8 blind people gave 1,258 commands to such agents across 12 desktop apps, through a screen-reader-accessible layer called OLLA. The best model, GPT-5, completed 52.5 percent of the tasks ([Are We There Yet?, EMNLP 2026](https://arxiv.org/abs/2609.00524)). The failures came from:

- choosing the wrong control on screen;
- planning the steps wrongly;
- losing track of what the person asked for, such as a date or a limit;
- not knowing when to stop.

Other things to know:

- Many of these agents work by looking at screenshots, and do not tell you what they are doing as they do it. Claude Code's computer use, for example, sees the screen only through screenshots ([Anthropic's documentation](https://code.claude.com/docs/en/computer-use)).
- Research also found that AI support advice can describe buttons and menus that do not exist, which is costly to disprove with a screen reader ([Kodandaram and others, CHI 2026](https://dl.acm.org/doi/10.1145/3772318.3790956)). If the AI tells you to press a button you cannot find, it may not be there.
- Some tools made for blind users that act for you add safety rails: they ask before anything that cannot be undone, switch the AI off on bank pages, and never send passwords or one-time codes to the cloud ([Mubsir](https://github.com/dusk-futile/mubsir)). Look for the same rails in any agent you use.

So an agent can save you time, but you must check its work, and it should never be the one that presses Pay or Send.

## How Do this for me keeps you in control

[Do this for me](/do) does two kinds of work:

- **Find information.** It searches the web and brings back what it found with its sources, so you can open each one and check.
- **Prepare a task.** It prepares something like a form or an application for you to send yourself.

It does not sign in to your accounts, enter passwords or codes, pay, or press Send for you. You review what it prepared, you decide, and you send it. Like everything on this site, it tells you what was checked and what was not.

## Checking what the AI found

- Open at least one source for every fact that matters, such as a price, a date or an address.
- Check the date of each source. Prices and opening hours change.
- If two sources disagree, ask the AI which one is newer, then check that source yourself.
- If something seems too good to be true, treat it as untrue until a source you trust confirms it.

If you are unsure whether something is safe, [ask the community](/help/ask?title=Is%20this%20safe%3F). Never include the password, code or card number in your question.
