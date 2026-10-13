# Blind Creators Studio

A website where blind and low-vision people turn their ideas into reality with AI: websites and apps, writing and publishing, research, and tasks done on the web. Every page is built for screen readers, keyboards, voice and braille displays first, works without JavaScript, and is light enough for cheap phones on slow data.

There is no phone line and no support staff. The AI does the work, and members help each other. Members use the AI with their own API key (Claude, or another listed AI company), or on the site's keys: with credits, or free within limits the site owner sets. Claude is the default; the site owner and members can choose another provider and model.

## What you can do

| Section | What it does |
|---|---|
| Build | Describe a website, a phone web app, a small tool or a game in your own words, and say how it should feel to the people who use it. The AI reads back what it understood, you correct it or approve it, and it builds it with a polished, professional design. The site checks every page for accessibility, opens each page on a phone and a computer to check it is easy to read and nothing is broken, and an AI looks at pictures of the pages; problems found are fixed once before you see the result. You are told how it looks in outcomes (easy to read, works on phones, looks finished), never in colours, and exactly what changed and what nobody has checked. Preview it, download it, or ask other members to test it. |
| Do this for me | "Find this and bring me the information", with sources. Or "help me do this task on a website": it finds the official place, explains each step with screen reader tips and prepares your answers. It never logs in, pays or submits for you. |
| Write and publish | Write articles, papers, letters and stories. AI suggestions are shown as before and after, and you accept or reject each one. Every version is kept. Export to Markdown, text, HTML, Word or LaTeX. Find where to publish and draft a cover letter. |
| Find before you build | A directory of tools, seeded from research on what was built for blind people in 2026, with reports from members about whether each one worked for them. The AI can also search the web for tools that already exist. |
| Research | Run your own research: prior work with sources, a study plan that is accessible to blind participants, statistics the site computes from your data, and a draft paper whose citations the site checks. |
| Peer checks | Ask blind and low-vision members to test your website with their own screen reader, braille display or magnifier, and check theirs. Sighted friends or volunteers can also check how it looks, answering in plain outcomes (does it look finished, is anything broken, would you trust it). Each check goes into a public record that shows if anyone changes it later. Optional credit rewards go from the person asking to the person checking. |
| AI tools report | Which AI coding tools work with screen readers right now, reported by members after each update. |
| Tool library | Tools blind people made for each other, and a way to report what broke after an update and volunteer to fix it. |
| Learn and help | Screen-reader-first guides, and a place to ask the community, with an optional AI-suggested answer while you wait. |

## How the site treats you

- **Read-back first.** Before the AI builds or does anything, it tells you what it understood.
- **Whole answers, summary first.** Nothing streams or talks over your screen reader. Slow work runs in the background: you get a page that says what is happening and a "Check again" link. With JavaScript on, it checks by itself, plays three short taps (rhythm, not pitch) and moves you to the result.
- **Honest labels.** What the AI wrote, what the site checked, and what nobody checked are always shown separately.
- **Your words stay yours.** The AI never silently changes your writing.
- **No secrets.** The site never asks for passwords, card numbers or codes, and refuses text that looks like them.
- **Getting back in without email.** When you sign up you get a recovery code, shown once. With your email address and that code you can set a new password yourself. The site sends no emails and has no staff, so save the code. "Sign every device out" on your account page ends sessions on other devices.

## Run it on your computer

You need Python 3.11 or newer.

```
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
STUDIO_SECRET_KEY=any-long-random-text uvicorn studio.app:get_app --factory --port 8000
```

Open http://localhost:8000, create an account, and connect your Claude API key on your account page (get one in the Claude Console at platform.claude.com).

## Put it on the internet

Any host that runs Python works (a small virtual server, Render, Railway, Fly.io and similar). You need:

- **A persistent disk** for the database file (`data/studio.db` by default, or set `STUDIO_DB_PATH`). Back it up.
- **HTTPS** in front of the app.
- These settings:

| Setting | What it is |
|---|---|
| `STUDIO_SECRET_KEY` | Required. Long random text. Keep it the same forever: it signs sign-ins and encrypts saved API keys (members' and the site's). Keep it apart from copies of the database. |
| `STUDIO_BASE_URL` | Your public address, for example `https://studio.example.org`. |
| `ANTHROPIC_API_KEY` | Optional. The site's own Claude key. You can instead save it on the site, in Your account, Site owner tools, AI settings for your key; a key saved there takes the place of this one. Without either, members use their own keys. |
| `STUDIO_MODEL` | Optional. Default `claude-opus-5-5`. The model chosen in AI settings for your key takes its place. |
| `STUDIO_CREDIT_MARKUP` | Optional. Default `1.25`: credits cost the AI price plus 25 percent. |
| `STUDIO_SIGNUP_CREDITS` | Optional. Free credits for new members, in cents. Anyone can make accounts, so these are paid for with your Claude key: they go to one account per internet address every 30 days, and to at most `STUDIO_SIGNUP_CREDITS_PER_DAY` (default 20) new accounts a day. |
| `STUDIO_PREVIEW_BASE_URL` | Recommended. A separate domain for members' build previews, for example `https://previews-studio.example.org`, pointed at the same app. Then a member's page can never pass for a page of your site. |
| `STUDIO_JOB_WORKERS` | Optional. Default 4: how many AI jobs run at once. Each member can have 3 waiting or running. |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Optional. Turn on buying credits. Add a Stripe webhook to `<your address>/payments/stripe/webhook` for `checkout.session.completed` and `checkout.session.async_payment_succeeded`. |

**Visual checks for Build.** To check how members' builds look on a phone and a computer, the server needs a browser: run `pip install playwright` (it is in `requirements.txt`) and then `playwright install chromium` (add `--with-deps` on a fresh Linux server for the system libraries). Without them, builds still work, and each version page says plainly that the look was not checked. The built pages run in a locked-down Chromium with no network and a time limit per page; run the site as a user other than root so Chromium's own sandbox is on too. Set `STUDIO_VISUAL_CHECKS=off` to turn the visual checks off.

Start it with `uvicorn studio.app:get_app --factory --host 0.0.0.0 --port 8000`. Sign-in tries and new accounts are limited per internet address. Behind a proxy on the same machine (Caddy, nginx), uvicorn reads the visitor's address from `X-Forwarded-For`; for a proxy elsewhere, add `--forwarded-allow-ips` with its address, or every visitor shares the proxy's address and its limits. Make yourself an admin with `python -m studio.manage make-admin you@example.com`.

**AI settings for your key** (Your account, Site owner tools). As the site owner you can:

- save the site's Claude API key: Anthropic checks it first, it is stored encrypted, only its last 4 characters are ever shown, and saving or removing it needs your password;
- choose the model members use with it: Claude Opus 5.5 (recommended), Sonnet 5.5 (half the price), Haiku 5.5 (lowest cost, weaker at building and research) or Fable 5.1 (most capable, most expensive);
- choose who pays: members with credits, or you, free for members;
- set monthly limits: the most your key may spend for the whole site, and the most each member may use. Before each AI step the site sets aside the most it can cost, so a limit is never passed; when one is reached, members are told plainly and can still use their own key. When the AI is free for members, at least one limit is required. Set a spending limit in the Claude Console (platform.claude.com) too.

Members who connect their own key choose their own model on their key page. Limits never apply to them.

### Choosing the AI provider and model

Claude is the default, and nothing changes until you choose otherwise. In AI settings for your key you can also:

- **save a key from another provider** that speaks the OpenAI-compatible API: OpenAI, Google Gemini, OpenRouter (many companies' models with one key), DeepSeek, Mistral, Groq, xAI (Grok), or another compatible provider at an address you type. The key is kept the same way as the Claude key: the provider checks it first, it is encrypted, only its last 4 characters are shown, and saving or removing it needs your password;
- **make it the main AI** for members on your keys, with a model from the provider's own live list (new models appear by themselves; you can also type a model name);
- **set its prices** in US dollars per million tokens in and out. They are required, because your limits and members' credits are worked out from them. For OpenRouter they are filled in from its list; for the others, copy them from the provider's price page;
- **choose who looks at pictures of built pages**: Claude (recommended), or the main model if you checked that it can look at pictures.

Payment mode and limits cover whatever runs on your keys, Claude or not. Each usage row records the provider and model.

**Web search uses Claude.** The OpenAI-compatible API has no common way to search the web, so the steps that search the live web always use Claude: Research, Do this for me, Find before you build, and finding publishing venues in Write. Keep your Claude key saved for them. Without one, those steps are not available to members on your keys, and they are told so plainly; nothing is charged.

**Members' own keys.** On their key page, members can connect a key from Claude or from one of the listed providers (never an address they type), choose its model from the provider's live list or type one, and give prices if they want to see costs (otherwise the usage page says the cost is not known). With a key from another provider, web search steps use the site's Claude key the site's way (free or with credits) if the site has one, and otherwise are not available.

**Your own address.** "Another compatible provider" must be an https address with no user name, password, query or fragment, and its name must lead only to public internet addresses (never this server or a private network). It is checked when you save it and again before each request. The site never follows a redirect.

**Addresses can change.** The listed providers' addresses are set in `studio/providers.py` (the `PRESETS` list). Companies sometimes move them: if a provider stops answering, check its documentation and update the list.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest -q
```

The tests include real-browser checks: Playwright drives Chromium, runs axe-core (WCAG 2.2 A and AA) on every page, repeats pages with JavaScript off and at 320 pixels wide, and walks each section the way a screen reader user would.

## Known limits

- **The AI was tested with a stand-in.** Every AI feature is tested against a fake Claude that returns fixed answers, and the request format follows Anthropic's documentation, but this code has not yet been run against the real Claude API. The same goes for the other providers: their requests follow the OpenAI-compatible Chat Completions format and are tested against a stand-in. Try each section once with a real key before inviting members.
- **Not yet tested by blind people.** The site passes automated checks (axe-core, keyboard order, no JavaScript, 400 percent zoom) and browser tests that find every control by its spoken name. That is not the same as people using NVDA, JAWS, VoiceOver and TalkBack. Testing with blind members is the most important next step; Peer checks and Ask the community are there for that feedback.
- **Build makes web pages and web apps.** It writes HTML, CSS and JavaScript, which phones can install from the browser. It does not make native app-store apps, servers, databases, sign-ins or payments inside what members build. The guide "apps-on-your-phone" explains the route to app stores.
- **Do this for me never logs in or submits.** It finds, explains and prepares; the member does the last step on the real website.
- **Payments are Stripe only.** A refund made in Stripe does not take credits back automatically; an admin can adjust credits.
- **One server process and SQLite.** Fine for hundreds to a few thousand members. A much larger site would need a database server and a job queue.

## How this was decided

The product came out of research and a multi-agent conference, kept in `docs/`:

- `docs/research/`: a verified landscape of tools built for blind people between April and October 2026, what blind people say they need, and the gaps.
- `docs/conference/`: the full transcript of rounds 1 to 5, the judge scores and the plans the panel wrote. The panel's eight ideas became the sections of this website; the parts the organizers did not want (a phone line, a shop, grant funding) were left out.

Developers: read `docs/DEVELOPING.md` first.
