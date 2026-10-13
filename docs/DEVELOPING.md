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
- `STUDIO_CREDIT_MARKUP` (default 1.25) is what credits cost compared with the AI price. `STUDIO_SIGNUP_CREDITS` gives new members free credits, in cents.
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
| `studio/web.py` | `render(request, template, title=..., **ctx)`, `checked_form(request)`, `require_user`, `current_user`, `flash`, `redirect`, `get_ai(request, user)`, the `markdown`, `nest_headings` and `shift_headings` template filters |
| `studio/ai.py` | `AIClient.structured / text / research`, `AIError`, `AINotAvailable`, `AIRefused`, `ResearchResult`, `Source` |
| `studio/jobs.py` | `submit(user_id, kind, title, fn, *args, back_url="", dedupe_key="")`, `job_url(job_id)`, `JobContext.progress(text)` |
| `studio/db.py` | `with db.session() as conn:` and `db.now()` |
| `studio/credits.py` | `balance`, `add`, `history`, `format_cents` |
| `studio/a11y_check.py` | `check_html(html) -> Report` with `.errors`, `.warnings`, `.passed`, `.not_checked`, `.to_dict()` |
| `studio/security.py` | password hashing, `encrypt_secret`, `decrypt_secret` |
| `studio/templates/base.html` and `_macros.html` | the layout and the form macros |
| `tests/conftest.py` | `client`, `user_client`, `fake_ai`, `no_ai_app`, `post`, `register`, `user_id` |

Do not edit these files from a section. If a section needs a change in a shared file, say so in your report.

## Rules for every page

1. **One h1 that says where you are or what just happened.** Pass the same words as `title=` to `render`, for example `title="Cake shop: version 2 is ready"`. Screen readers announce the title first.
2. **Headings in order** (h1, then h2, then h3). People move by headings. Put the summary first, details after.
3. **Forms use the macros** from `_macros.html` (pass `mark_optional=false` for a field that is optional but central, so it is not announced as "optional"): `csrf_input`, `error_summary`, `field`, `textarea`, `select`, `radios`, `checkbox`, `post_button`, `sources_list`. Import them with `{% from "_macros.html" import ... %}`. Every field gets a visible label. Hints and errors are linked automatically.
4. **Every POST handler** starts with `form = await checked_form(request)`, which checks the CSRF token. Read values with `form_text(form, name, limit)`: a field sent twice arrives as a list, and every value needs a length limit. After success, `redirect()` (303) to a page. On a validation error, re-render the form with `status_code=400`, `errors={field: message}` and a title ending in ": please fix the problems".
5. **No JavaScript required.** No streaming text. No auto-refresh. No content that changes while someone is reading. `static/enhance.js` is the only script.
6. **Buttons are `<button>`, links are `<a>`.** Link text makes sense on its own ("Open the preview", never "click here").
7. **Data tables have `<th scope="col">`.** Never use tables for layout.
8. **Plain language.** Short sentences. Buttons say what they do. Dates as `2026-10-12` or in words.
9. **Light pages.** No images unless they carry information, no web fonts, no external scripts.

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
- Show member or AI Markdown with `|markdown|nest_headings(3)` (or the level under your section heading), so its headings never skip a level or pose as the page's own. Pictures in Markdown become labelled links; the site shows no images from member or AI text.
- `structured()` schemas must be JSON Schema objects with `"additionalProperties": false` and every property listed in `"required"` (a requirement of structured outputs). Use strings, numbers, booleans, arrays and nested objects.
- `research()` searches the live web and returns `ResearchResult(text, sources)`. Always show the sources with the `sources_list` macro.
- `AIError` raised inside a job is shown to the person on the job page in plain words. Do not catch it inside the job.
- Every system prompt says: the person is blind or has low vision and uses a screen reader; put the summary first; use plain language and short headings; no tables unless asked; no emoji; never claim something was checked or tested unless it was.

## Honesty rules

- Label AI-written text as written by the AI. Label machine-checked facts as checked by the site. Never present AI claims as checked.
- Every result that could be wrong says what was checked, how, and what was not checked.
- Never silently change a person's words. Show before and after, and let them accept or reject.
- Never ask for, store or send passwords, card numbers or one-time codes. Refuse input that looks like them.

## Security

- Parameterized SQL only. Escape everything (Jinja autoescape is on). Render AI or member text with the `markdown` filter, which escapes raw HTML; never with `|safe`.
- Personal items (projects, documents, errands, research) are visible only to their owner. Check ownership on every route and return 404 for anything else.
- Accept only `http` and `https` URLs from people. Render external links with `rel="noopener noreferrer"`.
- Anything served from a member's build runs under a sandbox Content-Security-Policy so it cannot read the site's cookies.

## Tests

Each section has tests for: the main journey with `fake_ai`; validation errors; another member cannot see or change private items (404); what happens when the AI is not available (`no_ai_app`); and every page linked from the section index renders. `tests/test_site_pages.py` follows every link on the site and runs the accessibility checker on every page, so link your pages from your section's index. `python -m pytest -q` must pass in full.

## Links between sections

Sections link to each other with plain URLs and query parameters, never by importing each other's code:

- `/checks/new?url=<absolute URL>&title=<title>` opens the Peer checks request form pre-filled.
- `/find/ask?need=<text>` opens the Find form pre-filled.
- `/help/ask?title=<text>` opens the Ask the community form pre-filled.
