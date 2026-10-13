"""The one place the site talks to Claude.

Who pays:
- If the user connected their own Claude API key, the call uses it and the site charges nothing.
- Otherwise, if the server has a key and the user has credits, the call uses the server key and the
  user's credits are charged model cost times the markup.
- Otherwise the user is told how to connect a key or buy credits. Nothing is faked.

Every call checks for a refusal before reading content, and opts into Anthropic's server-side
fallback (fallbacks="default") so a declined request is retried on the recommended model.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

from . import credits, db, site_ai

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# US dollars per million tokens (input, output), from Anthropic's published prices.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
}
# Claude Haiku 5.5 costs more per token when one request's input is over 100,000 tokens.
LONG_PROMPT_PRICES = {"claude-haiku-5-5": (100_000, (0.50, 2.50))}
# Models that take no server-side fallback (sending one is an error).
NO_FALLBACK_MODELS = {"claude-haiku-5-5"}
WEB_SEARCH_DOLLARS = 0.01  # per search
# The most one image can cost in input tokens. Models with high-resolution vision (Opus 4.7 and later, including
# Opus 5 and 5.5, and Sonnet 5 and later) take images up to 2576 pixels on the long edge, at up to about 4,784
# tokens each; older models scale them down to about 1,600. Holds use the larger figure, so they always cover.
IMAGE_MAX_TOKENS = 4_784
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}
WEB_FETCH_TOOL = {"type": "web_fetch_20260209", "name": "web_fetch"}

# Put before every system prompt, so the AI speaks to every member the same way (docs/VOICE.md, 4.18).
MEMBER_VOICE = """\
The person you work for is blind or has low vision. They may read with a screen reader in speech or braille, \
with magnification or zoom, or with some sight. Their tools: {tools}. Their answer style: {style}. {language}\
Treat them as a capable adult, a maker and the expert on their own access. Be warm, calm and direct. Do not \
pity them, do not praise ordinary things, and do not apologise or reassure out of habit. Never write despite, \
brave, inspiring, overcome, suffer, special needs, visually impaired, visually challenged or differently abled, \
and never write simply, just, easy, obviously or do not worry. Mention blindness only when it matters to the \
answer. Words like see and look are fine, but do not assume they listen: write read, check or go to, not "you \
will hear". Do not explain how screen readers work unless asked. Many readers speak English as a second \
language: use plain words and no idioms. If something failed, say what happened, whether anything was lost, \
and what they can do next. Never claim something was checked or tested unless it was. Put the summary first. \
Use short sentences and short headings. No tables unless asked, no emoji, no decorative characters or ASCII \
art. Keep list items short, with the key word first. Never mention their blindness or low vision, or how they \
read, in anything written for other readers (a website, a letter, a paper, a form answer) unless they ask. \
{looks}"""
LOOKS_EFFECT = ("When you tell them how something looks, say the effect it has and what it suits, for example calm, "
                "finished and easy to read on a phone. Do not name colours, colour codes or fonts, and never ask them "
                "to choose colours. If they mention colours themselves, follow their wishes and talk about colours "
                "freely.")
LOOKS_COLOURS = ("When you tell them how something looks, say the effect first, then name the main colours in plain "
                 "words, for example deep green and cream. Give colour codes and font names only if they ask.")


def member_voice(user: dict) -> str:
    """The shared note on how to speak to this member, filled from their profile."""
    from .features.account import LANGUAGES  # here, not at the top: features import this module

    def get(name: str, default: Any = "") -> Any:
        try:
            return user[name] if user[name] is not None else default
        except (KeyError, IndexError):
            return default

    tools = " ".join(str(get("screen_reader")).split())[:100] or "not given"
    style = "full detail" if get("verbosity") == "full" else "summary first, then details"
    code = get("language", "en") or "en"
    language = ""
    if code != "en":
        name = dict(LANGUAGES).get(code, "")
        if name:
            language = (f"Their main language is {name}; the site is in English, so if they write to you in {name}, "
                        f"answer in {name}. ")
    looks = LOOKS_COLOURS if get("describe_colours", 0) else LOOKS_EFFECT
    return MEMBER_VOICE.format(tools=tools, style=style, language=language, looks=looks)


class AIError(Exception):
    """An AI problem with a message that is safe and useful to show the user."""

    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


class AINotAvailable(AIError):
    pass


class AIRefused(AIError):
    pass


@dataclass
class Source:
    url: str
    title: str
    quote: str = ""


@dataclass
class ResearchResult:
    """text: the answer. sources: the pages the answer cites (search results and fetched pages).
    other_results: pages the searches returned that the answer does not cite (not "used")."""
    text: str
    sources: list[Source] = field(default_factory=list)
    other_results: list[Source] = field(default_factory=list)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """An attribute of an SDK object, or a key of a plain dict (beta fields can arrive either way)."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def prices(model: str, input_tokens: int = 0) -> tuple[float, float]:
    """US dollars per million tokens (input, output) for a request with this much input."""
    long = LONG_PROMPT_PRICES.get(model)
    if long and input_tokens > long[0]:
        return long[1]
    return PRICES.get(model, PRICES["claude-opus-5-5"])


def cost_cents(model: str, input_tokens: int, output_tokens: int, web_searches: int, markup: float) -> int:
    in_price, out_price = prices(model, input_tokens)
    dollars = input_tokens / 1e6 * in_price + output_tokens / 1e6 * out_price + web_searches * WEB_SEARCH_DOLLARS
    return int(math.ceil(dollars * 100 * markup))


class AIClient:
    """Wraps the Anthropic SDK. Feature code calls structured(), research() or text()."""

    def __init__(self, *, api_key: str, model: str, user_id: int, paid_by: str, markup: float,
                 sdk_client: Any | None = None, member_note: str = ""):
        self.model = model
        self.member_note = member_note  # member_voice(user), put before every system prompt
        self.user_id = user_id
        self.paid_by = paid_by  # "own_key", "credits", or "site" (the owner pays, free for the member)
        self.markup = markup
        self._client = sdk_client or anthropic.Anthropic(api_key=api_key, max_retries=3)

    # ----- public API -------------------------------------------------------------------------

    def structured(self, *, system: str, prompt: str | list[dict], schema: dict, purpose: str,
                   effort: str = "medium", max_tokens: int = 32000) -> dict:
        """Ask for JSON that matches `schema` (a JSON Schema object with additionalProperties false)."""
        message = self._call(
            purpose=purpose,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        text = "".join(b.text for b in message.content if b.type == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIError("The AI's answer came back cut off or incomplete, so nothing was saved. "
                          "Please try again.") from exc

    def text(self, *, system: str, prompt: str, purpose: str, effort: str = "medium",
             max_tokens: int = 16000) -> str:
        message = self._call(
            purpose=purpose,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            output_config={"effort": effort},
        )
        return "".join(b.text for b in message.content if b.type == "text").strip()

    def research(self, *, system: str, prompt: str, purpose: str, effort: str = "medium",
                 max_searches: int = 8, fetch: bool = True, max_tokens: int = 16000) -> ResearchResult:
        """Answer using live web search (and page fetch). Returns the answer, the sources it cites, and the
        other pages the searches returned."""
        tools: list[dict] = [dict(WEB_SEARCH_TOOL, max_uses=max_searches)]
        if fetch:
            tools.append(dict(WEB_FETCH_TOOL, max_uses=max_searches))
        blocks: list[Any] = []  # every content block so far, across paused and resumed turns
        message = None
        for _ in range(6):  # resume paused server-tool turns a few times at most
            messages: list[dict] = [{"role": "user", "content": prompt}]
            if blocks:
                # The whole turn so far, so a second pause does not drop what the first part found.
                messages.append({"role": "assistant", "content": list(blocks)})
            message = self._call(purpose=purpose, system=system, messages=messages, max_tokens=max_tokens,
                                 output_config={"effort": effort}, tools=tools)
            blocks.extend(message.content)
            if message.stop_reason != "pause_turn":
                break
        if message is not None and message.stop_reason == "pause_turn":
            raise AIError("The research took too long to finish, so nothing was saved. Please try again, "
                          "perhaps with a narrower question.")
        return _research_result(blocks)

    # ----- internals ----------------------------------------------------------------------------

    def _worst_case_cents(self, kwargs: dict, markup: float | None = None) -> int:
        """The most a call can cost: the longest answer allowed, the input (about 3 characters a token, which
        overestimates; each image counts as the most an image can cost, IMAGE_MAX_TOKENS, however large its
        data), and every web search and page fetch allowed, each reading about 10,000 tokens."""
        chars = len(str(kwargs.get("system", "")))
        images = 0
        for message in kwargs.get("messages", []):
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "image":
                        images += 1
                    else:
                        chars += len(json.dumps(block, default=str))
            else:
                chars += len(json.dumps(content, default=str))
        input_tokens = chars // 3 + images * IMAGE_MAX_TOKENS + 1000
        searches = 0
        for tool in kwargs.get("tools") or []:
            uses = int(tool.get("max_uses", 0) or 0)
            input_tokens += uses * 10_000
            if tool.get("name") == "web_search":
                searches += uses
        return cost_cents(self.model, input_tokens, int(kwargs.get("max_tokens", 0)), searches,
                          self.markup if markup is None else markup)

    def _call(self, *, purpose: str, **kwargs) -> Any:
        if self.member_note:
            kwargs["system"] = f"{self.member_note}\n\n{kwargs.get('system') or ''}".strip()
        spend_hold = None
        if self.paid_by in ("credits", "site"):
            # The site owner's monthly limits cover all work on the site's key (see studio/site_ai.py).
            spend_hold, reason = site_ai.reserve(self.user_id, self._worst_case_cents(kwargs, markup=1.0))
            if spend_hold is None:
                raise AINotAvailable(reason)
        try:
            return self._call_reserved(purpose, kwargs)
        finally:
            if spend_hold is not None:
                site_ai.release(spend_hold)

    def _call_reserved(self, purpose: str, kwargs: dict) -> Any:
        hold_id = None
        if self.paid_by == "credits":
            # Hold the most this call can cost before making it, and settle the real cost after, so a member
            # can never run past their balance (and calls running at the same time see each other's holds).
            worst = self._worst_case_cents(kwargs)
            hold_id = credits.hold(self.user_id, worst, f"AI, held until it finishes: {purpose}")
            if hold_id is None:
                have = credits.balance(self.user_id)
                raise AINotAvailable(
                    f"This step can cost up to {credits.format_cents(worst)} in credits, and you have "
                    f"{credits.format_cents(max(have, 0))} free to use, so it did not start and nothing was charged. "
                    "To keep working, buy credits or connect your own Claude API key on Your account.")
        charged = 0
        try:
            try:
                # On a decline, Anthropic retries on its recommended model (not offered for every model).
                fallback = {} if self.model in NO_FALLBACK_MODELS else {"betas": [FALLBACK_BETA], "fallbacks": "default"}
                with self._client.beta.messages.stream(model=self.model, **fallback, **kwargs) as stream:
                    message = stream.get_final_message()
            except anthropic.AuthenticationError as exc:
                if self.paid_by == "own_key":
                    raise AIError("Anthropic did not accept your Claude API key, so nothing was done. The key may "
                                  "have been deleted or changed in the Claude Console (platform.claude.com). Please connect it again on "
                                  "Your account.") from exc
                raise AIError("The site's own AI key is not working right now, so nothing was done and nothing was "
                              "charged. Please try again later, or connect your own Claude API key on Your "
                              "account.") from exc
            except anthropic.PermissionDeniedError as exc:
                if self.paid_by == "own_key":
                    raise AIError(f"Your Claude API key is not allowed to use the AI model chosen for it "
                                  f"({site_ai.model_name(self.model)}), so nothing was done. Check the key's limits "
                                  "in the Claude Console (platform.claude.com), or choose another model or "
                                  "connect a different key on Your account.") from exc
                raise AIError("The site's own AI key is not allowed to use the AI model this site uses, so nothing "
                              "was done and nothing was charged. Please try again later, or connect your own "
                              "Claude API key on Your account.") from exc
            except anthropic.RateLimitError as exc:
                raise AIError("The AI service is busy right now, so nothing was done. Please try again in a "
                              "minute.") from exc
            except anthropic.APIStatusError as exc:
                raise AIError(f"The AI service had a problem on its side (code {exc.status_code}), so nothing was "
                              "done. Please try again in a few minutes.") from exc
            except anthropic.APIConnectionError as exc:
                raise AIError("The site could not reach the AI service, so nothing was done. Please try again in a "
                              "few minutes.") from exc
            charged = self._record_usage(purpose, message)
        finally:
            if hold_id is not None:
                credits.settle(hold_id, self.user_id, charged if self.paid_by == "credits" else 0, f"AI: {purpose}")
        if message.stop_reason == "refusal":
            raise AIRefused("The AI would not do this request. This can happen by mistake, for example with words "
                            "about passwords or PINs. Try saying it another way. If you think it should work, ask in "
                            "Ask the community.")
        if message.stop_reason == "max_tokens":
            raise AIError("The AI's answer was too long to finish, so nothing was saved. Please try again with a "
                          "smaller request, for example one page or one change at a time.")
        return message

    def _record_usage(self, purpose: str, message: Any) -> int:
        """Record what the call cost. Returns the cost in cents (what credits pay, with the markup).

        After a server-side fallback, the top-level usage covers only the attempt that answered; every
        attempt is billed, so each one in usage.iterations is counted at its own model's price."""
        usage = getattr(message, "usage", None)
        if usage is None:
            return 0
        markup = self.markup if self.paid_by == "credits" else 1.0
        served_by = getattr(message, "model", self.model) or self.model
        attempts = list(_field(usage, "iterations") or []) or [usage]
        per_attempt_searches = any(_field(a, "server_tool_use") for a in attempts)
        total_in = total_out = total_searches = 0
        dollars_cents = 0.0
        raw_dollars_cents = 0.0  # what Anthropic charges, without the credits markup
        for attempt in attempts:
            input_tokens = (_field(attempt, "input_tokens", 0) or 0) + (_field(attempt, "cache_creation_input_tokens", 0) or 0)
            # Cache reads cost about a tenth of normal input.
            input_tokens += int((_field(attempt, "cache_read_input_tokens", 0) or 0) * 0.1)
            output_tokens = _field(attempt, "output_tokens", 0) or 0
            server = _field(attempt if per_attempt_searches else usage, "server_tool_use")
            searches = (_field(server, "web_search_requests", 0) or 0) if server else 0
            if not per_attempt_searches and attempt is not attempts[-1]:
                searches = 0  # the top-level count, added once
            model = _field(attempt, "model") or served_by
            in_price, out_price = prices(model, input_tokens)
            attempt_cents = (input_tokens / 1e6 * in_price + output_tokens / 1e6 * out_price
                             + searches * WEB_SEARCH_DOLLARS) * 100
            dollars_cents += attempt_cents * markup
            raw_dollars_cents += attempt_cents
            total_in += input_tokens
            total_out += output_tokens
            total_searches += searches
        cents = int(math.ceil(dollars_cents))
        with db.session() as conn:
            conn.execute(
                "INSERT INTO ai_usage (user_id, purpose, model, input_tokens, output_tokens, web_searches, cost_cents,"
                " raw_cents, paid_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.user_id, purpose, served_by, total_in, total_out, total_searches, cents,
                 int(math.ceil(raw_dollars_cents)), self.paid_by, db.now()),
            )
        return cents


_SERVER_TOOL_BLOCKS = {"server_tool_use", "web_search_tool_result", "web_fetch_tool_result"}


def _research_result(blocks: list[Any]) -> ResearchResult:
    """The answer, the sources it cites and the other search results, from a research turn's content blocks.

    The answer is the text after the last tool call: text before it is the model saying what it will search
    for next ("I'll search for the opening hours first."), not the answer."""
    all_texts: list[str] = []
    answer: list[str] = []
    fetched: list[tuple[str, str]] = []  # (url, title) of each fetched page, in order: citations point at them
    cited: dict[str, Source] = {}
    returned: dict[str, Source] = {}

    def cite(url: str | None, title: str | None, quote: str | None) -> None:
        if not url:
            return
        src = cited.setdefault(url, Source(url=url, title=title or url))
        if not src.quote:
            src.quote = (quote or "")[:300]

    for block in blocks:
        kind = _field(block, "type")
        if kind == "web_fetch_tool_result":
            content = _field(block, "content")
            if _field(content, "type") == "web_fetch_result" and _field(content, "url"):
                document = _field(content, "content")
                fetched.append((_field(content, "url"), _field(document, "title", "") or _field(content, "url")))
        if kind in _SERVER_TOOL_BLOCKS:
            answer = []
        if kind == "web_search_tool_result":
            content = _field(block, "content")
            if isinstance(content, list):
                for r in content:
                    url = _field(r, "url")
                    if url and url not in returned:
                        returned[url] = Source(url=url, title=_field(r, "title", "") or url)
        elif kind == "text":
            text = _field(block, "text", "") or ""
            all_texts.append(text)
            answer.append(text)
            for c in _field(block, "citations") or []:
                quote = _field(c, "cited_text", "")
                if _field(c, "url"):
                    cite(_field(c, "url"), _field(c, "title", ""), quote)
                    continue
                index = _field(c, "document_index")
                if isinstance(index, int) and 0 <= index < len(fetched):
                    url, title = fetched[index]
                    cite(url, _field(c, "document_title", "") or title, quote)
    text = "".join(answer).strip() or "".join(all_texts).strip()
    others = [src for url, src in returned.items() if url not in cited]
    return ResearchResult(text=text, sources=list(cited.values()), other_results=others)


def default_factory(settings) -> Callable[[dict], AIClient]:
    """Build the function that gives each user an AI client, or explains why they cannot have one.

    A member's own key comes first, with the model they chose. Otherwise the site's key (studio/site_ai.py)
    with the owner's model: free for the member if the owner chose that, else paid with credits."""
    from .security import decrypt_secret

    def factory(user: dict) -> AIClient:
        note = member_voice(user)
        if user.get("own_api_key_enc"):
            key = decrypt_secret(settings.secret_key, user["own_api_key_enc"])
            if key:
                return AIClient(api_key=key, model=site_ai.member_model(settings, user), user_id=user["id"],
                                paid_by="own_key", markup=settings.credit_markup, member_note=note)
        site_key = site_ai.site_key(settings)
        if site_key:
            model = site_ai.site_model(settings)
            if site_ai.pay_mode() == "free":
                return AIClient(api_key=site_key, model=model, user_id=user["id"], paid_by="site", markup=1.0,
                                member_note=note)
            if credits.balance(user["id"]) > 0:
                return AIClient(api_key=site_key, model=model, user_id=user["id"], paid_by="credits",
                                markup=settings.credit_markup, member_note=note)
            if settings.stripe_secret_key:
                raise AINotAvailable("To use the AI, buy credits or connect your own Claude API key on Your account.")
        raise AINotAvailable("To use the AI, connect your own Claude API key on Your account.")

    return factory
