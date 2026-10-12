---
title: Coding with AI agents in the terminal, with a screen reader
summary: How to turn on screen reader support in Claude Code, GitHub Copilot CLI, Codex CLI and Gemini CLI, which community add-ons fill the gaps, and the known problems as of October 2026.
last_checked: 2026-10
order: 3
---

## The short version

- **Claude Code** has the most complete screen reader mode, but you must turn it on yourself: `claude --ax-screen-reader`.
- **GitHub Copilot CLI** turns its screen reader support on by itself when it detects a screen reader, and has a `--screen-reader` flag.
- **OpenAI Codex CLI** has no screen reader mode. It turns its animations off when it detects a screen reader.
- **Gemini CLI** has a `--screen-reader` flag, but since June 18, 2026 Google no longer serves free, Google AI Pro or Ultra users through it. Its replacement, Antigravity CLI, has no screen reader mode.
- Blind developers have built add-ons that fill the gaps. They are small projects and can break after updates.

These tools change every week. Each fact below links to its source, and this guide was last checked in October 2026.

## Claude Code

Anthropic's Claude Code has a screen reader mode that replaces the moving, boxed terminal display with plain lines of text, one after another ([Anthropic's accessibility documentation](https://code.claude.com/docs/en/accessibility)). It needs version 2.1.181 or later ([Anthropic support article](https://support.claude.com/en/articles/15924927-use-claude-code-cli-with-a-screen-reader)).

### How to turn it on

Use any one of these three ways. If you use more than one, the flag wins over the environment variable, and the environment variable wins over the setting.

1. Start Claude Code with the flag: `claude --ax-screen-reader`
2. Set this environment variable before starting Claude Code:

   ```
   CLAUDE_AX_SCREEN_READER=1
   ```

3. Add this setting to `~/.claude/settings.json`, so it is always on:

```
{
  "axScreenReader": true
}
```

When it starts, you hear a confirmation line such as "[Screen Reader Mode: on via flag]", then a quiet period of about 3 seconds so the line is not talked over. The environment variable `CLAUDE_AX_STARTUP_QUIET_MS` changes that quiet period.

The mode does not turn itself on when a screen reader is running. You have to choose one of the three ways above ([issue 89002](https://github.com/anthropics/claude-code/issues/89002)).

### What changes

- Each line of the conversation starts with a label you can search for: `you:`, `claude:`, `thinking:`, `tool:`, `tool error:`, `error:`, `warning:`, `Permission Required:` and `Cost:`.
- Menus become numbered lists. Type the number to choose. Yes or no questions take y or n.
- Changes to files are read line by line, with + for added lines and - for removed lines, so you can hear a change before you approve it.
- Tables are read as "Header: value" sentences.
- The terminal bell rings when a reply finishes, when a question needs your answer, and when a tool that ran for more than 5 seconds finishes.
- It marks each turn so the jump-to-prompt keys in iTerm2, the VS Code terminal and Windows Terminal move between turns. The macOS Terminal app and WezTerm do not support this.
- Deleted words and lines are announced, and so is the permission mode when you press Shift+Tab.
- A separate environment variable keeps the cursor visible for screen magnifiers:

  ```
  CLAUDE_CODE_ACCESSIBILITY=1
  ```

### Known problems

From Anthropic's documentation and from issues blind users filed:

- Costs are announced only when you exit.
- Background sessions you attach to use the alternate screen, with no scrollback ([issue 100922](https://github.com/anthropics/claude-code/issues/100922)).
- Arrow keys in the slash-command menu say nothing in screen reader mode ([issue 100253](https://github.com/anthropics/claude-code/issues/100253), October 2026).
- Some multiple-choice question pickers are hard to use with NVDA ([issue 88839](https://github.com/anthropics/claude-code/issues/88839)).
- There is no built-in speech, no earcons, and no option to hold speech until the turn is done ([issue 87978](https://github.com/anthropics/claude-code/issues/87978); [issue 70425](https://github.com/anthropics/claude-code/issues/70425)).

A blind user wrote in July 2026: "While nearly unusable to me only a couple of weeks ago, the new sr mode has enabled me to be in the CLI on a very regular basis." ([issue 11002](https://github.com/anthropics/claude-code/issues/11002))

### Claude Code in VS Code and in the desktop app

The Claude Code extension for VS Code announces each reply once when it is complete, announces permission requests and status changes, and adds hidden headings for each turn, from version 2.1.236 (August 2026). A "Focus last message" command came in version 2.1.268 ([Anthropic's VS Code documentation](https://code.claude.com/docs/en/vs-code)).

The desktop app is harder to use today. Known problems include the conversation list removing the message a screen reader is reading ([issue 99332](https://github.com/anthropics/claude-code/issues/99332)) and the file changes panel reading every line as "Add comment" with VoiceOver ([issue 100911](https://github.com/anthropics/claude-code/issues/100911)). If you need to review changes, the terminal with screen reader mode is the more reliable choice for now.

## GitHub Copilot CLI

- Copilot CLI has had a `--screen-reader` mode since October 2025, and can remember the choice in its settings ([Copilot CLI changelog](https://raw.githubusercontent.com/github/copilot-cli/main/changelog.md)).
- Its redesigned terminal interface, generally available since June 23, 2026, turns screen reader support on automatically when it detects a screen reader. Icons are labelled and animations switch themselves off. The `/theme` command offers high-contrast and colorblind themes ([GitHub changelog, June 23, 2026](https://github.blog/changelog/2026-06-23-copilot-cli-new-terminal-interface-is-generally-available/)).
- Version 1.0.71 (July 2026) announces the focused row in pickers. Version 1.0.81 (August 27, 2026) added Ctrl+Space to turn voice dictation on and off.
- GitHub publishes a guide to using Git, GitHub CLI and Copilot CLI with a screen reader ([GitHub accessibility guide](https://accessibility.github.com/documentation/guide/cli/)).

Known problem: the status bar is announced on every key press ([issue 1642](https://github.com/github/copilot-cli/issues/1642), open with no reply from GitHub).

## OpenAI Codex CLI

- Codex CLI has no screen reader flag or plain-text mode. A blind developer asked for one in April 2026, and the request is still open ([issue 20489](https://github.com/openai/codex/issues/20489)).
- Since May 2026 it turns animations off for screen readers, and since September 16, 2026 it checks for a screen reader when it starts ([pull request 46040](https://github.com/openai/codex/pull/46040)).
- If VoiceOver or another screen reader keeps announcing fragments of output, the maintainers' advice is to set the `tui.animations` setting to false in your Codex configuration ([issue 44728](https://github.com/openai/codex/issues/44728)).
- Users still reported repeated speech with JAWS and VoiceOver reaching only the end of the conversation in October 2026 (comments on [issue 20489](https://github.com/openai/codex/issues/20489)).

## Gemini CLI and Antigravity CLI

- Gemini CLI has a screen reader mode that renders output as plain text. Turn it on with the `--screen-reader` flag, or set this setting to true in Gemini CLI's settings ([Gemini CLI configuration](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/reference/configuration.md)):

  ```
  ui.accessibility.screenReader
  ```
- On June 18, 2026, Gemini CLI stopped serving free, Google AI Pro and Ultra requests, in favour of Antigravity CLI. Paid API keys and Code Assist Standard and Enterprise were not affected ([Google's announcement](https://github.com/google-gemini/gemini-cli/discussions/27274)).
- Antigravity CLI has no screen reader mode. The request to restore it has been open since May 19, 2026. Blind users wrote that they could not get past the setup, and that it is "impossible for some tasks!" ([issue 6](https://github.com/google-antigravity/antigravity-cli/issues/6)).

## Community add-ons

Blind developers and others built tools around these agents. All are small, mostly maintained by one person, and may break when the agent updates. Read each project's own page before you install it.

- **earshot** for Claude Code: detects VoiceOver, NVDA or JAWS and offers to turn on screen reader mode, delivers each answer whole instead of streaming it, speaks short progress lines such as "Starting work." and "Still working, 10 seconds.", and rings a bell when the answer lands. Needs Claude Code 2.1.292 or later. Created in October 2026 and not yet tested by its planned blind testers ([claude-code-earshot](https://github.com/unlocked-spark/claude-code-earshot)).
- **BlindPilot**: a desktop app with native controls that runs Claude Code, Codex, Gemini CLI and other agents, and splits each answer into rows you move through with the arrow keys. Warning: its README says every agent runs in bypass permissions mode by default, which means the agent acts without asking you first. Change that before you use it on anything important ([BlindPilot](https://github.com/serrebidev/BlindPilot)).
- **claude-code-reader**: a desktop wrapper around Claude Code with native controls. Its author says it was tested with VoiceOver, NVDA and JAWS. The builds are not signed ([claude-code-reader](https://github.com/doubletaponair/claude-code-reader)).
- **claude-code-audio-recap**: for Mac only. After each turn, it speaks a short summary written by a smaller Claude model ([claude-code-audio-recap](https://github.com/leonidb/claude-code-audio-recap)).
- **agentaloud**: shows Claude Code and Codex answers as structured content for NVDA ([agentaloud](https://github.com/lpintes/agentaloud)).

## Habits that help

- **Keep permission prompts on**, and listen to the + and - lines before you approve a change.
- **Put your needs in the agent's instruction file**, for example `CLAUDE.md` for Claude Code: "I use a screen reader. Put the summary first. Use short sentences and lists. No tables. Every page you make must have one h1, labelled buttons and form fields, and must move focus after an action."
- **Check with evidence, not by looking.** Jamal Mazrui, a blind developer and author: "Without sight, you check it by gathering evidence: tests that pass or fail, logs, error messages, exit codes, accessibility scans, and what your screen reader actually says." ([Blind Vibe Coding](https://raw.githubusercontent.com/jamalmazrui/BlindVibeCoding/main/index.md))
- **Ask a person to test what you built.** Use [Peer checks](/checks).

## Learn more

- "Blind Vibe Coding: Building Apps Nonvisually with AI" by Jamal Mazrui, October 2026: sixteen tutorials, from a talking timer to NVDA add-ons and mobile apps, with the Markdown source open ([BlindVibeCoding on GitHub](https://github.com/jamalmazrui/BlindVibeCoding)).
- The [AI tools report](/ai-tools) on this site, where members report which tools work with their screen reader after each update.
