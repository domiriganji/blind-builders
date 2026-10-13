# Blind Creators Studio

A website where blind and low-vision people turn their ideas into reality with AI: websites and apps, writing and publishing, research, and tasks done on the web. Every page is built for screen readers, keyboards, voice and braille displays first, works without JavaScript, and is light enough for cheap phones on slow data.

There is no phone line and no support staff. The AI does the work, and members help each other. Members pay for the AI themselves, either with their own Claude API key or with credits.

## What you can do

| Section | What it does |
|---|---|
| Build | Describe a website, a phone web app, a small tool or a game in your own words. The AI reads back what it understood, you correct it or approve it, it builds it, and the site checks every page and tells you exactly what changed and what nobody has checked. Preview it, download it, or ask other members to test it. |
| Do this for me | "Find this and bring me the information", with sources. Or "help me do this task on a website": it finds the official place, explains each step with screen reader tips and prepares your answers. It never logs in, pays or submits for you. |
| Write and publish | Write articles, papers, letters and stories. AI suggestions are shown as before and after, and you accept or reject each one. Every version is kept. Export to Markdown, text, HTML, Word or LaTeX. Find where to publish and draft a cover letter. |
| Find before you build | A directory of tools, seeded from research on what was built for blind people in 2026, with reports from members about whether each one worked for them. The AI can also search the web for tools that already exist. |
| Research | Run your own research: prior work with sources, a study plan that is accessible to blind participants, statistics the site computes from your data, and a draft paper whose citations the site checks. |
| Peer checks | Ask other blind members to test your website with their screen reader, and check theirs. Each check goes into a public record that shows if anyone changes it later. Optional credit rewards go from the person asking to the person checking. |
| AI tools report | Which AI coding tools work with screen readers right now, reported by members after each update. |
| Tool library | Tools blind people made for each other, and a way to report what broke after an update and volunteer to fix it. |
| Learn and help | Screen-reader-first guides, and a place to ask the community, with an optional AI-suggested answer while you wait. |

## How the site treats you

- **Read-back first.** Before the AI builds or does anything, it tells you what it understood.
- **Whole answers, summary first.** Nothing streams or talks over your screen reader. Slow work runs in the background: you get a page that says what is happening and a "Check again" link. With JavaScript on, it checks by itself, plays three short taps (rhythm, not pitch) and moves you to the result.
- **Honest labels.** What the AI wrote, what the site checked, and what nobody checked are always shown separately.
- **Your words stay yours.** The AI never silently changes your writing.
- **No secrets.** The site never asks for passwords, card numbers or codes, and refuses text that looks like them.

## Run it on your computer

You need Python 3.11 or newer.

```
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
STUDIO_SECRET_KEY=any-long-random-text uvicorn studio.app:get_app --factory --port 8000
```

Open http://localhost:8000, create an account, and connect your Claude API key on your account page (get one at console.anthropic.com).

## Put it on the internet

Any host that runs Python works (a small virtual server, Render, Railway, Fly.io and similar). You need:

- **A persistent disk** for the database file (`data/studio.db` by default, or set `STUDIO_DB_PATH`). Back it up.
- **HTTPS** in front of the app.
- These settings:

| Setting | What it is |
|---|---|
| `STUDIO_SECRET_KEY` | Required. Long random text. Keep it the same forever: it signs sign-ins and encrypts members' saved API keys. |
| `STUDIO_BASE_URL` | Your public address, for example `https://studio.example.org`. |
| `ANTHROPIC_API_KEY` | Optional. The site's own Claude key, used for members who buy credits. Without it, members use their own keys. |
| `STUDIO_MODEL` | Optional. Default `claude-opus-5-5`. |
| `STUDIO_CREDIT_MARKUP` | Optional. Default `1.25`: credits cost the AI price plus 25 percent. |
| `STUDIO_SIGNUP_CREDITS` | Optional. Free credits for new members, in cents. Anyone can make accounts, so these are paid for with your Claude key: they go to one account per internet address every 30 days, and to at most `STUDIO_SIGNUP_CREDITS_PER_DAY` (default 20) new accounts a day. |
| `STUDIO_PREVIEW_BASE_URL` | Recommended. A separate domain for members' build previews, for example `https://previews-studio.example.org`, pointed at the same app. Then a member's page can never pass for a page of your site. |
| `STUDIO_JOB_WORKERS` | Optional. Default 4: how many AI jobs run at once. Each member can have 3 waiting or running. |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Optional. Turn on buying credits. Add a Stripe webhook to `<your address>/payments/stripe/webhook` for `checkout.session.completed` and `checkout.session.async_payment_succeeded`. |

Start it with `uvicorn studio.app:get_app --factory --host 0.0.0.0 --port 8000`. Sign-in tries and new accounts are limited per internet address. Behind a proxy on the same machine (Caddy, nginx), uvicorn reads the visitor's address from `X-Forwarded-For`; for a proxy elsewhere, add `--forwarded-allow-ips` with its address, or every visitor shares the proxy's address and its limits. Make yourself an admin with `python -m studio.manage make-admin you@example.com`.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest -q
```

The tests include real-browser checks: Playwright drives Chromium, runs axe-core (WCAG 2.2 A and AA) on every page, repeats pages with JavaScript off and at 320 pixels wide, and walks each section the way a screen reader user would.

## How this was decided

The product came out of research and a multi-agent conference, kept in `docs/`:

- `docs/research/`: a verified landscape of tools built for blind people between April and October 2026, what blind people say they need, and the gaps.
- `docs/conference/`: the full transcript of rounds 1 to 5, the judge scores and the plans the panel wrote. The panel's eight ideas became the sections of this website; the parts the organizers did not want (a phone line, a shop, grant funding) were left out.

Developers: read `docs/DEVELOPING.md` first.
