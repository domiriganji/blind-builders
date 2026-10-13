"""The one place the site talks to the AI: Claude here, other providers in studio/compat_ai.py.

Who pays:
- If the user connected their own Claude API key, the call uses it and the site charges nothing.
- Otherwise, if the server has a key and the user has credits, the call uses the server key and the
  user's credits are charged model cost times the markup.
- Otherwise the user is told how to connect a key or buy credits. Nothing is faked.

Every call checks for a refusal before reading content, and opts into Anthropic's server-side
fallback (fallbacks="default") so a declined request is retried on the recommended model.

BilledClient holds what every provider shares, so billing is the same whoever answers: the member note before
every system prompt, the site's monthly limits (site_ai.reserve and release), credit holds and settling, and the
ai_usage row. default_factory gives each member a client, or a RoutedAI that sends web search steps to Claude
and pictures to the owner's picture choice when the main AI is another provider (docs/DEVELOPING.md, "Choosing
the AI provider").
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


@dataclass
class Usage:
    """What one call used, for its ai_usage row. raw_cents: the provider's price in cents, without the markup.
    cost_known: False when nobody gave prices for the model (a member's own key from another provider)."""
    model: str
    input_tokens: int
    output_tokens: int
    web_searches: int
    raw_cents: float
    cost_known: bool = True


class BilledClient:
    """What every AI client shares, so every provider is billed and limited the same way."""

    provider = "anthropic"

    def __init__(self, *, model: str, user_id: int, paid_by: str, markup: float, member_note: str = ""):
        self.model = model
        self.member_note = member_note  # member_voice(user), put before every system prompt
        self.user_id = user_id
        self.paid_by = paid_by  # "own_key", "credits", or "site" (the owner pays, free for the member)
        self.markup = markup

    def _with_note(self, system: str | None) -> str:
        if self.member_note:
            return f"{self.member_note}\n\n{system or ''}".strip()
        return system or ""

    def _billed(self, purpose: str, worst: Callable[[float], int], send: Callable[[], tuple[Any, Usage | None]]) -> Any:
        """Run send() inside the site's limits and the member's credit hold, then record and settle what it cost.

        worst(markup) is the most the call can cost in cents; send() returns (result, Usage or None)."""
        spend_hold = None
        if self.paid_by in ("credits", "site"):
            # The site owner's monthly limits cover all work on the site's keys (see studio/site_ai.py).
            spend_hold, reason = site_ai.reserve(self.user_id, worst(1.0))
            if spend_hold is None:
                raise AINotAvailable(reason)
        try:
            hold_id = None
            if self.paid_by == "credits":
                # Hold the most this call can cost before making it, and settle the real cost after, so a member
                # can never run past their balance (and calls running at the same time see each other's holds).
                most = worst(self.markup)
                hold_id = credits.hold(self.user_id, most, f"AI, held until it finishes: {purpose}")
                if hold_id is None:
                    have = credits.balance(self.user_id)
                    raise AINotAvailable(
                        f"This step can cost up to {credits.format_cents(most)} in credits, and you have "
                        f"{credits.format_cents(max(have, 0))} free to use, so it did not start and nothing was "
                        "charged. To keep working, buy credits or connect your own Claude API key on Your account.")
            charged = 0
            try:
                result, usage = send()
                if usage is not None:
                    charged = self._record(purpose, usage)
            finally:
                if hold_id is not None:
                    credits.settle(hold_id, self.user_id, charged if self.paid_by == "credits" else 0, f"AI: {purpose}")
            return result
        finally:
            if spend_hold is not None:
                site_ai.release(spend_hold)

    def _record(self, purpose: str, usage: Usage) -> int:
        """Write the ai_usage row. Returns the cost in cents (what credits pay, with the markup)."""
        markup = self.markup if self.paid_by == "credits" else 1.0
        # Rounded first, so a float like 75.00000000001 is not charged as 76 cents.
        cents = int(math.ceil(round(usage.raw_cents * markup, 6))) if usage.cost_known else 0
        with db.session() as conn:
            conn.execute(
                "INSERT INTO ai_usage (user_id, purpose, model, input_tokens, output_tokens, web_searches, cost_cents,"
                " raw_cents, paid_by, cost_known, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.user_id, purpose, usage.model, usage.input_tokens, usage.output_tokens, usage.web_searches,
                 cents, int(math.ceil(round(usage.raw_cents, 6))) if usage.cost_known else 0, self.paid_by,
                 1 if usage.cost_known else 0, db.now()),
            )
        return cents


class AIClient(BilledClient):
    """Wraps the Anthropic SDK. Feature code calls structured(), research() or text()."""

    def __init__(self, *, api_key: str, model: str, user_id: int, paid_by: str, markup: float,
                 sdk_client: Any | None = None, member_note: str = ""):
        super().__init__(model=model, user_id=user_id, paid_by=paid_by, markup=markup, member_note=member_note)
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
        kwargs["system"] = self._with_note(kwargs.get("system"))

        def send() -> tuple[Any, Usage | None]:
            message = self._send(kwargs)
            return message, self._usage(message)

        message = self._billed(purpose, lambda markup: self._worst_case_cents(kwargs, markup=markup), send)
        if message.stop_reason == "refusal":
            raise AIRefused("The AI would not do this request. This can happen by mistake, for example with words "
                            "about passwords or PINs. Try saying it another way. If you think it should work, ask in "
                            "Ask the community.")
        if message.stop_reason == "max_tokens":
            raise AIError("The AI's answer was too long to finish, so nothing was saved. Please try again with a "
                          "smaller request, for example one page or one change at a time.")
        return message

    def _send(self, kwargs: dict) -> Any:
        try:
            # On a decline, Anthropic retries on its recommended model (not offered for every model).
            fallback = {} if self.model in NO_FALLBACK_MODELS else {"betas": [FALLBACK_BETA], "fallbacks": "default"}
            with self._client.beta.messages.stream(model=self.model, **fallback, **kwargs) as stream:
                return stream.get_final_message()
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

    def _record_usage(self, purpose: str, message: Any) -> int:
        """Record what the call cost. Returns the cost in cents (what credits pay, with the markup)."""
        usage = self._usage(message)
        return self._record(purpose, usage) if usage is not None else 0

    def _usage(self, message: Any) -> Usage | None:
        """What the call used and cost at Anthropic's prices.

        After a server-side fallback, the top-level usage covers only the attempt that answered; every
        attempt is billed, so each one in usage.iterations is counted at its own model's price."""
        usage = getattr(message, "usage", None)
        if usage is None:
            return None
        served_by = getattr(message, "model", self.model) or self.model
        attempts = list(_field(usage, "iterations") or []) or [usage]
        per_attempt_searches = any(_field(a, "server_tool_use") for a in attempts)
        total_in = total_out = total_searches = 0
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
            raw_dollars_cents += (input_tokens / 1e6 * in_price + output_tokens / 1e6 * out_price
                                  + searches * WEB_SEARCH_DOLLARS) * 100
            total_in += input_tokens
            total_out += output_tokens
            total_searches += searches
        return Usage(model=served_by, input_tokens=total_in, output_tokens=total_out, web_searches=total_searches,
                     raw_cents=raw_dollars_cents)


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


def has_pictures(prompt: Any) -> bool:
    """True if a prompt (Anthropic-style blocks) holds a picture."""
    return isinstance(prompt, list) and any(isinstance(b, dict) and b.get("type") == "image" for b in prompt)


class RoutedAI:
    """The AI for one member when their main AI is not Claude, with the same three methods sections use.

    research() always goes to Claude (the standard API has no common web search); a prompt with pictures goes to
    the picture choice; everything else goes to the main AI. A route that has no AI raises AINotAvailable with
    its own plain reason, before anything is sent or charged."""

    def __init__(self, *, main: Any, research: Any = None, research_reason: str = "", pictures: Any = None,
                 pictures_reason: str = ""):
        self.main = main
        self._research = research
        self._research_reason = research_reason
        self._pictures = pictures
        self._pictures_reason = pictures_reason

    # Read by tests and the account pages only; sections use the three methods.
    @property
    def model(self) -> str:
        return self.main.model

    @property
    def paid_by(self) -> str:
        return self.main.paid_by

    @property
    def user_id(self) -> int:
        return self.main.user_id

    def _for(self, prompt: Any) -> Any:
        if not has_pictures(prompt):
            return self.main
        if self._pictures is None:
            raise AINotAvailable(self._pictures_reason)
        return self._pictures

    def structured(self, *, prompt: str | list[dict], **kwargs) -> dict:
        return self._for(prompt).structured(prompt=prompt, **kwargs)

    def text(self, *, prompt: str | list[dict], **kwargs) -> str:
        return self._for(prompt).text(prompt=prompt, **kwargs)

    def research(self, **kwargs) -> ResearchResult:
        if self._research is None:
            raise AINotAvailable(self._research_reason)
        return self._research.research(**kwargs)


# A member's own key is from a provider this site no longer offers (removed from providers.PRESETS).
UNLISTED_PROVIDER = ("Your saved key is from a company this site no longer offers, so the AI cannot use it. Nothing "
                     "was done and nothing was charged. To keep working, choose another company or connect a Claude "
                     "API key on Your account, on the AI key page.")


def search_unavailable(client: Any) -> str:
    """Why web search steps cannot run for this member's AI, or "" when they can. Sections whose work ends in a
    web search check this before starting, so nothing is charged for steps that could never finish."""
    if isinstance(client, RoutedAI) and client._research is None:
        return client._research_reason
    return ""


# Why a step that needs Claude cannot run, for a member whose main AI is another provider.
SEARCH_NEEDS_CLAUDE = "This step searches the live web, which on this site needs Claude."
PICTURES_NEED_CLAUDE = "Looking at pictures of the pages is set to use Claude on this site."


def _claude_missing(need: str, why: str, *, own_key: bool, can_buy: bool) -> str:
    """A plain message for a step that needs Claude when no Claude is available. Nothing was sent or charged."""
    connect = ("connect a Claude API key on Your account, in place of your other key" if own_key
               else "connect your own Claude API key on Your account")
    todo = f"To use it, buy credits, or {connect}." if can_buy else f"To use it, {connect}."
    return f"{need} {why} So this step did not run, and nothing was charged for it. {todo}"


def default_factory(settings) -> Callable[[dict], Any]:
    """Build the function that gives each user an AI client, or explains why they cannot have one.

    A member's own key comes first, with the provider and model they chose. Otherwise the site's keys
    (studio/site_ai.py) with the owner's main AI: free for the member if the owner chose that, else paid with
    credits. When everything runs on one Claude client (the default), that AIClient is returned as it always was;
    otherwise a RoutedAI sends web search steps to Claude and pictures to the picture choice."""
    from . import providers
    from .compat_ai import CompatClient
    from .security import decrypt_secret

    def own_client(user: dict, note: str) -> Any | None:
        if not user.get("own_api_key_enc"):
            return None
        key = decrypt_secret(settings.secret_key, user["own_api_key_enc"])
        if not key:
            return None
        provider_id = user.get("own_provider") or providers.ANTHROPIC
        if provider_id == providers.ANTHROPIC:
            return AIClient(api_key=key, model=site_ai.member_model(settings, user), user_id=user["id"],
                            paid_by="own_key", markup=settings.credit_markup, member_note=note)
        provider = providers.get(provider_id)
        if provider is None or provider.id not in providers.PRESET_IDS:
            # A provider no longer in PRESETS (or never offered to members). Never fall back to the site's key
            # here: Your account says the member uses their own key, so they must not be charged credits.
            raise AINotAvailable(UNLISTED_PROVIDER)
        model = (user.get("own_model") or "").strip()
        if not model:
            raise AINotAvailable(f"Choose the model for your {provider.name} key first, on Your account, on the AI "
                                 "key page. Nothing was done and nothing was charged.")
        price_in = providers.parse_price(user.get("own_price_in") or "")
        price_out = providers.parse_price(user.get("own_price_out") or "")
        prices = (price_in, price_out) if price_in is not None and price_out is not None else None
        return CompatClient(provider=provider, api_key=key, model=model, prices=prices, user_id=user["id"],
                            paid_by="own_key", markup=settings.credit_markup, member_note=note,
                            allow_images=user.get("own_vision") == "main")

    def site_payer(user: dict) -> tuple[str | None, float, str]:
        """(paid_by, markup, "") for work on the site's keys, or (None, 0, why not)."""
        if site_ai.pay_mode() == "free":
            return "site", 1.0, ""
        if credits.balance(user["id"]) > 0:
            return "credits", settings.credit_markup, ""
        if settings.stripe_secret_key:
            return None, 0.0, "To use the AI, buy credits or connect your own Claude API key on Your account."
        return None, 0.0, "To use the AI, connect your own Claude API key on Your account."

    def site_claude(user: dict, note: str) -> tuple[AIClient | None, str]:
        """The site's Claude key under the site's payment rules, or (None, why not)."""
        site_key = site_ai.site_key(settings)
        if not site_key:
            return None, "The site has no Claude key."
        paid_by, markup, _ = site_payer(user)
        if paid_by is None:
            return None, "Here it uses the site's Claude key, paid with credits, and you have none."
        return AIClient(api_key=site_key, model=site_ai.site_model(settings), user_id=user["id"], paid_by=paid_by,
                        markup=markup, member_note=note), ""

    def factory(user: dict) -> Any:
        note = member_voice(user)
        own = own_client(user, note)
        if isinstance(own, AIClient):
            return own
        if own is not None:
            can_buy = bool(getattr(settings, "stripe_secret_key", None) and site_ai.site_key(settings))
            claude, why = site_claude(user, note)
            pictures = own if own.allow_images else claude
            return RoutedAI(
                main=own, research=claude,
                research_reason=_claude_missing(SEARCH_NEEDS_CLAUDE, why, own_key=True, can_buy=can_buy),
                pictures=pictures,
                pictures_reason=_claude_missing(PICTURES_NEED_CLAUDE, why, own_key=True, can_buy=can_buy))
        if not site_ai.site_ai_ready(settings):
            if site_ai.site_key(settings) or site_ai.other_key(settings):
                paid_by, _, reason = site_payer(user)
                if paid_by is None:
                    raise AINotAvailable(reason)
            raise AINotAvailable("To use the AI, connect your own Claude API key on Your account.")
        paid_by, markup, reason = site_payer(user)
        if paid_by is None:
            raise AINotAvailable(reason)
        claude, why = site_claude(user, note)
        if site_ai.main_is_claude():
            return claude
        provider = site_ai.other_provider()
        vision_main = site_ai.vision_choice() == site_ai.VISION_MAIN
        main = CompatClient(provider=provider, api_key=site_ai.other_key(settings), model=site_ai.other_model(),
                            prices=site_ai.other_prices(), user_id=user["id"], paid_by=paid_by, markup=markup,
                            member_note=note, allow_images=vision_main)
        return RoutedAI(
            main=main, research=claude,
            research_reason=_claude_missing(SEARCH_NEEDS_CLAUDE, why, own_key=False, can_buy=False),
            pictures=main if vision_main else claude,
            pictures_reason=_claude_missing(PICTURES_NEED_CLAUDE, why, own_key=False, can_buy=False))

    return factory
