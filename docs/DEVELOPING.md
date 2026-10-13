# Developing Blind Creators Studio

A website where blind and low-vision people build their ideas with AI. Python 3.13, FastAPI, Jinja2 templates rendered on the server, SQLite. No front-end framework. Every page must work without JavaScript.

## Run it

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
STUDIO_SECRET_KEY=change-me uvicorn studio.app:get_app --factory --reload
python -m pytest -q
```

Environment variables are listed in `studio/config.py`. `ANTHROPIC_API_KEY` is optional: without it, members connect their own Claude API key on their account page.

### Running the site for real

- Run one process (`uvicorn` without `--workers`). Background jobs are threads in that process; when it restarts, unfinished jobs are marked as stopped with a plain "try again" message.
- Request bodies over 3 MB are refused (`STUDIO_MAX_BODY_BYTES`).

- Set `STUDIO_SECRET_KEY` to a long random value and keep it. It signs sessions and encrypts members' saved API keys.
- Set `STUDIO_BASE_URL` to the public address, for example `https://studio.example.org`. Preview links for peer checks are built from it.
- Credits need both `ANTHROPIC_API_KEY` (the site's own Claude key) and payments. For Stripe, set `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`, and add a webhook to `<base URL>/payments/stripe/webhook` for the events `checkout.session.completed` and `checkout.session.async_payment_succeeded`. Without payments, members use their own Claude API keys.
- `STUDIO_CREDIT_MARKUP` (default 1.25) is what credits cost compared with the AI price. `STUDIO_SIGNUP_CREDITS` gives new members free credits, in cents. Anyone can make accounts and these credits spend the site's key, so they go to one account per internet address every 30 days and to at most `STUDIO_SIGNUP_CREDITS_PER_DAY` (default 20) accounts a day.
- Credit-paid AI calls hold the most a call can cost before it starts and settle the real cost after, so no one runs past their balance.
- Set `STUDIO_PREVIEW_BASE_URL` to a separate domain pointed at the same app (for example `https://previews-studio.example.org`). Build previews are then served only there, so a member's page never appears under the site's own address.
- Sign-in, password recovery and new accounts are limited per internet address (`studio/limits.py`). Behind a proxy on the same machine uvicorn takes the address from `X-Forwarded-For`; for a proxy elsewhere pass `--forwarded-allow-ips`.
- `STUDIO_JOB_WORKERS` (default 4) is how many AI jobs run at once. Each member can have 3 jobs waiting or running.
- Visual checks open members' pages in Chromium, so they run only with Chromium's own sandbox. Run the site as a user that is not root, on a system that allows unprivileged user namespaces; otherwise the checks turn themselves off and the log says why. `STUDIO_VISUAL_CHECKS_ALLOW_NO_SANDBOX=1` runs them without the sandbox anyway: only do that where the site itself runs in a container of its own. `STUDIO_VISUAL_CHECKS=off` turns them off. One check runs at a time; for a hard limit, also cap the site's memory and processes (for example systemd `MemoryMax=` and `TasksMax=`).
- Admin tasks: `python -m studio.manage make-admin EMAIL` and `python -m studio.manage grant-credits EMAIL CENTS REASON`. Admins also get a page at `/account/admin`.

## How a section is built

Each section is one module in `studio/features/` (two for Find and Library). The app imports every name in `FEATURES` in `studio/app.py`.

A section module defines:

- `router`: a FastAPI `APIRouter`.
- `NAV`: `{"label", "url", "description"}` for the main navigation and the home page, or `None`.
- `SCHEMA`: SQL run at start-up. Use `CREATE TABLE IF NOT EXISTS` and prefix table names with the section, for example `build_projects`.
- `seed()` (optional): inserts starting data. It must be idempotent (safe to run on every start).
- `forget_user(user_id)` (required if the section stores private work): deletes that member's private items. It is called when a member deletes their account.

Templates go in `studio/templates/<section>/`. Tests go in `tests/test_<section>.py`. Data files go in `studio/data/`. Guides go in `studio/content/guides/`.

### Shared code you use but do not edit

| File | What it gives you |
|---|---|
| `studio/web.py` | `render(request, template, title=..., **ctx)`, `checked_form(request)`, `require_user`, `current_user`, `flash`, `redirect`, `get_ai(request, user)`, `error_title(title, errors)`, `date_words(iso, with_time=False)`, `keep_form(request, form, page_path, reason)` and the `kept_forms` table (see "Keeping a member's words"), the `restored(name, value)` template global, the `markdown`, `nest_headings`, `shift_headings` and `date_words` template filters |
| `studio/site_ai.py` | The owner's AI settings: `site_key(settings)`, `site_model(settings)`, `member_model(settings, user)`, the main AI (`main_choice()`, `other_provider()`, `other_model()`, `other_prices()`, `vision_choice()`, `site_ai_ready(settings)`), `pay_mode()`, monthly limits with `reserve` and `release` (used by the AI clients, not by sections) |
| `studio/ai.py` | `AIClient.structured / text / research`, `RoutedAI`, `member_voice(user)`, `AIError`, `AINotAvailable`, `AIRefused`, `ResearchResult`, `Source` |
| `studio/providers.py` | The AI providers: `PRESETS` (the OpenAI-compatible providers ready to choose), the address checks, `fetch_models` and `cached_models` (each provider's live model list) |
| `studio/compat_ai.py` | `CompatClient`, the AI on an OpenAI-compatible provider, with the same three methods |
| `studio/jobs.py` | `submit(user_id, kind, title, fn, *args, back_url="", dedupe_key="", item_ref="")`, `job_url(job_id)`, `JobContext.progress(text)`, `forget_item(conn, user_id, item_ref)`, `still_wanted(conn, ctx)` |
| `studio/sensitive.py` | `find_secret(text)`: a plain message if the text holds a password, PIN, one-time code, API key or card number. `SECRET_MESSAGE`: the one refusal wording (docs/VOICE.md, 4.9). `PUBLIC_SUFFIX`: " Everyone can read this page.", added to `SECRET_MESSAGE` where the text would be public |
| `studio/db.py` | `with db.session() as conn:` and `db.now()` |
| `studio/credits.py` | `balance`, `add`, `history`, `format_cents` |
| `studio/a11y_check.py` | `check_html(html) -> Report` with `.errors`, `.warnings`, `.passed`, `.not_checked`, `.to_dict()` |
| `studio/security.py` | password hashing, `encrypt_secret`, `decrypt_secret` |
| `studio/templates/base.html` and `_macros.html` | the layout and the form macros |
| `tests/conftest.py` | `client`, `user_client`, `fake_ai`, `no_ai_app`, `post`, `register`, `user_id` |

Do not edit these files from a section. If a section needs a change in a shared file, say so in your report.

## Rules for every page

1. **One h1 that says where you are or what just happened.** Pass the same words as `title=` to `render`, for example `title="Cake shop: version 2 is ready"`. Screen readers announce the title first. After an action, the outcome goes in the next page's h1 and title, not only in a flash message (see "After an action").
2. **Headings in order** (h1, then h2, then h3). People move by headings. Put the summary first, details after.
3. **Forms use the macros** from `_macros.html` (pass `mark_optional=false` for a field that is optional but central, so it is not announced as "optional"): `csrf_input`, `error_summary`, `field`, `textarea`, `select`, `radios`, `checkbox`, `post_button`, `sources_list`. Import them with `{% from "_macros.html" import ... %}`. Every field gets a visible label. Hints and errors are linked automatically. `radios(name, legend, options, selected, hint=..., error=..., required=true)` marks each choice `required`, for a question that must be answered. A field you write by hand instead of with a macro takes its value through `restored(name, value)`, as the macros do, so a member's kept words come back in it (see "Keeping a member's words").
4. **Every POST handler** starts with `form = await checked_form(request)`, which checks the CSRF token. Read values with `form_text(form, name, limit)`: a field sent twice arrives as a list, and every value needs a length limit. After success, `redirect()` (303) to a page. On a validation error, re-render the form with `status_code=400`, `errors={field: message}` and `title=error_title(title, errors)` (from `studio/web.py`), which gives "Please check 1 answer: Sign in".
5. **No JavaScript required.** No streaming text. No auto-refresh. No content that changes while someone is reading. `static/enhance.js` is the only script.
6. **Buttons are `<button>`, links are `<a>`.** Link text makes sense on its own ("Open the preview", never "click here").
7. **Data tables have `<th scope="col">`.** Never use tables for layout.
8. **Plain language, in the voice of `docs/VOICE.md`.** Every word a member reads follows it: short sentences, buttons that say what they do, errors that say what happened and what to do next. Dates in one format, "13 October 2026": `{{ value|date_words }}` in templates, `date_words(value)` in Python (`date_words(value, with_time=True)` adds "12:29 UTC" where the time matters).
9. **Light pages.** No images unless they carry information, no web fonts, no external scripts.
10. **Missing things are a 404 with a plain reason.** Raise `HTTPException(status_code=404, detail="We could not find that project.")` (a whole sentence, in the voice of `docs/VOICE.md`). The error page shows the reason and offers a link back to the section's index ("Go to Build"), worked out from the address, so keep every section's pages under its `NAV` url.

### After an action

When a form succeeds, the next page's h1 and title say what happened ("Your report is saved: Glide", "Deleted Kemi Kitchen and all its versions"), so a member who reads only the title or the first heading knows it worked. Store the words in the session just before `redirect()`, and have the next page take them once and use them as its `title=`:

- Find and Library use `set_done(request, where, tool_id, text)` and `pop_done(request, where, tool_id)` in `studio/features/find.py`.
- Your account uses `_announce(request, heading)` and `_done_title(request, default)` in `studio/features/account.py`.
- A new section copies the same pattern: one session key, read with `pop`, and checked against the page it was meant for so it never shows on another page.

Use `flash()` for extra detail under the heading, not instead of it.

### Keeping a member's words

A member's words are never lost because a form could not be sent. When a form is sent after the member was signed out (`LoginRequired`), or after its CSRF token stopped matching (`FormExpired`), `studio/app.py` calls `web.keep_form(request, form, page_path, reason)`. That stores the text fields for at most two hours in the `kept_forms` table (its schema is `web.KEPT_FORMS_SCHEMA`), keyed by a random token in the session. The next time the member opens that page, `render()` puts the words back once, through the `restored()` template global that the form macros call, and the page says why. Words no field took are shown under the form to copy.

- Passwords, API keys, codes, card numbers, the CSRF token and anything `find_secret` flags are never kept.
- Words sent from another website are never kept or shown, so no site can put text in a member's forms.
- Words kept for one member are never shown to a different member who signs in on the same browser. Deleting an account deletes its rows in `kept_forms`.
- A section needs nothing for this beyond using `checked_form`, `require_user` and the form macros (or `restored()` in a hand-written field). The GET address of each form's page must show the form, so the words have somewhere to come back to.

### Account pages other sections link to

- `/account/profile`: the member's profile. Its sight question is "Blind or low vision since" with the answers "Birth or early childhood", "Later in life" and "Prefer not to say" (`users.sight_onset`: `birth`, `later`, `prefer_not`), and a separate optional question, "Which fits you best?" (`users.vision`: `blind`, `low_vision`, `prefer_not`). Any form that asks about sight history uses exactly these words (docs/VOICE.md, 4.14) and pre-fills from the profile.
- `/account/password`: change your password (the current one, then the new one). Changing it signs out every other device.
- `/account/ai-key`: connect a Claude API key, or a key from a listed provider, and choose its model. Link to the guide `/learn/claude-api-key` ("Getting your Claude API key, step by step") wherever a page asks a member to get a key, and name Anthropic's site as "the Claude Console (platform.claude.com)", linked to `https://platform.claude.com/` with words saying it opens Anthropic's website.

## Using the AI

```python
from fastapi import Depends, Request
from ..ai import AINotAvailable
from ..web import checked_form, flash, get_ai, redirect, require_user
from .. import jobs

@router.post("/thing/new")
async def new_thing(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    ... validate, save a row ...
    try:
        ai = get_ai(request, user)
    except AINotAvailable as exc:
        flash(request, exc.user_message, "error")
        return redirect(f"/thing/{thing_id}")
    job_id = jobs.submit(user["id"], "thing.understand", "Understanding your idea", _understand, ai, thing_id)
    return redirect(jobs.job_url(job_id))

def _understand(ctx, ai, thing_id):          # runs in the background
    ctx.progress("Reading your idea")
    result = ai.structured(system=SYSTEM, prompt=..., schema=SCHEMA_JSON, purpose="thing.understand")
    ... save result ...
    return f"/thing/{thing_id}"               # the job page sends the person here when done
```

- `purpose` is a unique string per kind of call, `<section>.<step>`. Tests register fake answers by purpose.
- Pass `back_url=` (the page to return to if the job fails) and, for anything that costs money, a specific `dedupe_key=` (for example `f"build.generate:{project_id}"`) so a double click does not run and charge twice.
- Ids of deleted rows are reused. A job is given the item's id and something unique to it (its `created_at` or a random token) and checks both, with the owner (`ctx.user_id`), in the same transaction as its final write. A job that saves new private work also checks `jobs.still_wanted(conn, ctx)`.
- Job titles quote the member's words. Pass `item_ref=` (for example `f"errands:{id}:{created_at}"`) and call `jobs.forget_item(conn, user_id, item_ref)` when the item is deleted.
- Show member or AI Markdown with `|markdown|nest_headings(3)` (or the level under your section heading), so its headings never skip a level or pose as the page's own. Pictures in Markdown become labelled links; the site shows no images from member or AI text.
- `structured()` schemas must be JSON Schema objects with `"additionalProperties": false` and every property listed in `"required"` (a requirement of structured outputs). Use strings, numbers, booleans, arrays and nested objects.
- `research()` searches the live web and returns `ResearchResult(text, sources)`. Always show the sources with the `sources_list` macro.
- `AIError` raised inside a job is shown to the person on the job page in plain words. Do not catch it inside the job.
- Use only `structured`, `text` and `research` on the object `get_ai` returns. It may be an `AIClient`, a `CompatClient` or a `RoutedAI` (see "Choosing the AI provider"), and those three methods are all they share.
- `AIClient` puts the shared member note (`ai.member_voice(user)`: who the person is, their tools, answer style and language, the tone, and how to describe looks) before every system prompt by itself. Section prompts do not repeat it; they add only what their task needs, such as the rules for text written for other readers or for editing a member's own words (`docs/VOICE.md`, 4.18).

## Choosing the AI provider

Claude is the default. The site owner can make another provider the main AI for members on the site's keys, and members can use their own key from Claude or from a listed provider. A site that chooses nothing gets exactly one `AIClient`, as before.

- **`studio/providers.py`** holds `CLAUDE`, `PRESETS` and the owner-only `CUSTOM` provider. A preset is a `Provider(id, name, about, base_url, keys_url, prices_url, token_param, lists_prices)`. Model names are never written there: they come from `GET {base_url}/models` (`fetch_models`, cached for 10 minutes by `cached_models`, and a list that could not be read for 1 minute), with a box to type a model name when the list cannot be read. `fetch_models(checking=True)` words its errors for a new key ("not saved"); pages listing a saved key's models use `checking=False`. Pages fetch the list in a worker thread: async handlers call `_render` and `_render_key` with `run_in_threadpool`, never on the event loop. List prices are used only when the model has no fee per request, picture or web search. Models whose names show they cannot write answers (embedding, speech, picture making) are left out of the list.
- **Addresses.** Preset addresses are fixed in code. The owner's own address goes through `address_problem(url)`: https only, no user info, query or fragment, and the host must resolve only to public addresses (not loopback, private, link-local, multicast, reserved or unspecified, IPv4 or IPv6, including IPv4 inside IPv6). The host is turned into ASCII the way httpx does (`connect_host`, IDNA 2008), so the name checked is the name connected to. It is checked when saved and before every request attempt (`check_before_request`), and every connection to it goes through `PublicOnlyTransport`: its `PublicOnlyBackend` resolves the name, refuses unless every address is public, and connects to one of exactly those addresses, with TLS still checked against the name. So DNS that changes between the check and the connection cannot lead anywhere private. `http_client()` never follows redirects. Members can only choose presets; checking a key (Claude or another company's) is limited to `KEY_CHECKS_PER_HOUR` per member and per internet address.
- **`studio/compat_ai.py`, `CompatClient`**: Chat Completions with a system message and a user message. Anthropic-style blocks become content parts (pictures as `image_url` data URLs) only when the client is allowed pictures; text alone is sent as one string. `max_completion_tokens` for OpenAI, `max_tokens` for the others, switched once if a 400 says the name is not supported. A 400 saying the answer length is more than the model allows is its own case: the client asks again for the most the model allows (read from the message) and remembers it; with no number, the member reads that the model cannot write answers that long. `json_object` is tried only when the error is about the answer format or schema. `structured()` asks for `json_schema` (strict); if the provider refuses, `json_object` with the schema in the system message. The answer is checked with `schema_problems` (type, properties, required, additionalProperties false, items, enum, anyOf), with one repair turn. `finish_reason` `length` is an `AIError`, `content_filter` (or a `refusal`) an `AIRefused`. At most 2 retries on 429 and 5xx. Errors name the provider and where to fix it, and never hold the key.
- **Billing is shared.** `ai.BilledClient` is the base of both clients: the member note, `site_ai.reserve` and `release`, credit holds and `settle`, and the `ai_usage` row (`Usage`). A compatible call costs `usage.prompt_tokens` and its output times the prices per million tokens set for the model. Output is `completion_tokens`, or `total_tokens - prompt_tokens` when that is more (thinking some providers count only in the total), plus `reasoning_tokens` when they are reported apart from `completion_tokens`; a count the provider leaves out is estimated at about 4 characters a token. Holds use those prices and `max_tokens`. The site's key never runs a model without prices, and both prices must be above 0. A changed model never keeps the previous model's prices without the owner confirming them (the form sends `prices_for`). A member's own key without prices records `cost_known = 0`, and the usage page says the cost is not known. `ai_usage.model` is `provider:model` for other providers and the Claude model id for Claude.
- **Routing.** `default_factory` returns a `RoutedAI` when the main AI is not Claude: `research()` always goes to Claude (the member's own Claude key, otherwise the site's Claude key under the site's payment rules), because the compatible API has no common web search; a prompt with pictures goes to the picture choice (the owner's: Claude or the main model; a member's: their own model only if they ticked that it can look at pictures); everything else goes to the main AI. A route with no AI raises `AINotAvailable` with a plain reason, before anything is sent or charged. Sections whose work ends in a web search (Do this for me, Research prior work, Find, Write's publishing venues) call `ai.search_unavailable(client)` before starting a job, so nothing is charged for steps that could never finish. A member's own key from a provider no longer in `PRESETS` raises `AINotAvailable` (never the site's key), and Your account says the company is no longer offered. Build's design review already turns that into an honest "the AI could not look" note.
- **Adding a preset.** Add a `Provider(...)` to `PRESETS` with its OpenAI-compatible base address, its keys and prices pages, `token_param="max_completion_tokens"` if it takes only that, and `lists_prices=True` if its `/models` list has OpenRouter-style `pricing`. Nothing else changes: the owner's and members' pages list it by themselves. Companies move their addresses now and then; check them against each provider's documentation.
- **Tests** never touch the network: set `providers.TRANSPORT` to an `httpx.MockTransport` and replace `providers.resolve` (see `tests/test_providers.py`).

## Honesty rules

- Label AI-written text as written by the AI. Label machine-checked facts as checked by the site. Never present AI claims as checked.
- Every result that could be wrong says what was checked, how, and what was not checked.
- Never silently change a person's words. Show before and after, and let them accept or reject.
- Never ask for, store or send passwords, card numbers or one-time codes. Refuse input that looks like them, with `sensitive.find_secret` on every field that is stored or sent to the AI.

## Security

- Parameterized SQL only. Escape everything (Jinja autoescape is on). Render AI or member text with the `markdown` filter, which escapes raw HTML; never with `|safe`.
- Personal items (projects, documents, errands, research) are visible only to their owner. Check ownership on every route and return 404 for anything else.
- Accept only `http` and `https` URLs from people. Render external links with `rel="noopener noreferrer"`.
- Anything served from a member's build runs under a sandbox Content-Security-Policy so it cannot read the site's cookies.

## Visual checks

`studio/visual_check.py` opens a build's pages in Chromium (Playwright) on a phone and a computer and measures what sighted visitors meet: sideways scrolling, text contrast, cut-off and overlapping text, small tap targets and text on phones, and broken pictures. `available()` says whether the server has a browser; `check_site(files)` never raises and returns a `VisualReport` with pictures for an AI to look at. Each problem has `builder_text` for the member and `fix_text` for the AI. Members are told about the look by its effect (easy to read, works on phones, looks finished). Colours are named only for members who chose "The effect, then the main colours" in Your profile (`users.describe_colours`), or who mention colours themselves; nobody is asked to choose colours (`docs/VOICE.md`, 4.4). Built pages are untrusted, so they run in Chromium's sandbox, with every request outside their own files blocked, no new windows, a bare environment (none of the site's secrets), a cap on renderer processes and script memory, one check at a time, and a time limit. Every chosen page and size is in the report, measured or not; a check passes only when it ran on every page. Pictures are PNG, or JPEG when a PNG would be too large to send (`SCREENSHOT_MAX_BYTES`), and `check_site(files, max_screenshots=n)` keeps only the first n. In tests, replace `visual_check.available` and `check_site` with a fake (see `tests/test_build.py`); `tests/test_visual_check.py` uses the real browser.

## Tests

Each section has tests for: the main journey with `fake_ai`; validation errors; another member cannot see or change private items (404); what happens when the AI is not available (`no_ai_app`); and every page linked from the section index renders. `tests/test_site_pages.py` follows every link on the site and runs the accessibility checker on every page, so link your pages from your section's index. `python -m pytest -q` must pass in full.

## Links between sections

Sections link to each other with plain URLs and query parameters, not by importing each other's code:

- `/checks/new?url=<absolute URL>&title=<title>&kind=screen_reader|sighted|both` opens the Peer checks request form pre-filled (`kind` says who should check; the default is both).
- `/find/ask?need=<text>` opens the Find form pre-filled.
- `/help/ask?title=<text>` opens the Ask the community form pre-filled.

The one exception is these agreed functions, which a section may import because the work has to happen inside its own database transaction:

- `studio.features.checks.requests_for_address(address, open_only=False, *, requester_id=None, conn=None)`: the peer check requests for an address, newest first. `address` is the start of a full address, or a path beginning with `/` that matches any address with it in its path, on any domain (Build passes `/build/p/<token>/v/<n>/` for one version, or `/build/p/<token>/v/` for every version). Each item has `id`, `title`, `wanted`, `status`, `done`, `max_checks`, `reward_cents`, `unpaid_cents`, `label` and `record_url`. Build uses it to list "Peer checks of this version" on a version page and the open requests on the delete page.
- `studio.features.checks.close_requests_for_address(address, *, requester_id=None, conn=None)`: closes every open request for the address and returns the credits held for checks not done to whoever asked. Returns `(requests closed, cents returned)` and is safe to call twice. Pass `conn` to run it inside your own transaction (begun with `BEGIN IMMEDIATE`). Build calls it inside its delete transaction, so deleting a project, closing its peer checks and returning their credits happen together or not at all.
