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

from . import credits, db

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
WEB_SEARCH_DOLLARS = 0.01  # per search
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}
WEB_FETCH_TOOL = {"type": "web_fetch_20260209", "name": "web_fetch"}


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


def cost_cents(model: str, input_tokens: int, output_tokens: int, web_searches: int, markup: float) -> int:
    in_price, out_price = PRICES.get(model, PRICES["claude-opus-5-5"])
    dollars = input_tokens / 1e6 * in_price + output_tokens / 1e6 * out_price + web_searches * WEB_SEARCH_DOLLARS
    return int(math.ceil(dollars * 100 * markup))


class AIClient:
    """Wraps the Anthropic SDK. Feature code calls structured(), research() or text()."""

    def __init__(self, *, api_key: str, model: str, user_id: int, paid_by: str, markup: float,
                 sdk_client: Any | None = None):
        self.model = model
        self.user_id = user_id
        self.paid_by = paid_by  # "own_key" or "credits"
        self.markup = markup
        self._client = sdk_client or anthropic.Anthropic(api_key=api_key, max_retries=3)

    # ----- public API -------------------------------------------------------------------------

    def structured(self, *, system: str, prompt: str, schema: dict, purpose: str,
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
            raise AIError("The AI's answer was cut off or malformed. Please try again.") from exc

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

    def _worst_case_cents(self, kwargs: dict) -> int:
        """The most a call can cost: the longest answer allowed, the input (about 3 characters a token, which
        overestimates), and every web search and page fetch allowed, each reading about 10,000 tokens."""
        chars = len(str(kwargs.get("system", ""))) + len(json.dumps(kwargs.get("messages", []), default=str))
        input_tokens = chars // 3 + 1000
        searches = 0
        for tool in kwargs.get("tools") or []:
            uses = int(tool.get("max_uses", 0) or 0)
            input_tokens += uses * 10_000
            if tool.get("name") == "web_search":
                searches += uses
        return cost_cents(self.model, input_tokens, int(kwargs.get("max_tokens", 0)), searches, self.markup)

    def _call(self, *, purpose: str, **kwargs) -> Any:
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
                    f"{credits.format_cents(max(have, 0))} free to use. Buy credits or connect your own Claude API key "
                    "on your account page.")
        charged = 0
        try:
            try:
                with self._client.beta.messages.stream(
                    model=self.model,
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                    **kwargs,
                ) as stream:
                    message = stream.get_final_message()
            except anthropic.AuthenticationError as exc:
                if self.paid_by == "own_key":
                    raise AIError("Your Claude API key was rejected. Check it on your account page.") from exc
                raise AIError("The site's AI key is not working. Please try again later.") from exc
            except anthropic.PermissionDeniedError as exc:
                raise AIError("The Claude API key does not have permission for this model.") from exc
            except anthropic.RateLimitError as exc:
                raise AIError("The AI is busy right now (rate limit). Please try again in a minute.") from exc
            except anthropic.APIStatusError as exc:
                raise AIError(f"The AI service returned an error ({exc.status_code}). Please try again.") from exc
            except anthropic.APIConnectionError as exc:
                raise AIError("Could not reach the AI service. Please try again.") from exc
            charged = self._record_usage(purpose, message)
        finally:
            if hold_id is not None:
                credits.settle(hold_id, self.user_id, charged if self.paid_by == "credits" else 0, f"AI: {purpose}")
        if message.stop_reason == "refusal":
            raise AIRefused("The AI declined this request. Try rewording it, or ask in Help if you think this is a mistake.")
        if message.stop_reason == "max_tokens":
            raise AIError("The answer was too long and got cut off. Try asking for something smaller.")
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
            in_price, out_price = PRICES.get(model, PRICES["claude-opus-5-5"])
            dollars_cents += (input_tokens / 1e6 * in_price + output_tokens / 1e6 * out_price
                              + searches * WEB_SEARCH_DOLLARS) * 100 * markup
            total_in += input_tokens
            total_out += output_tokens
            total_searches += searches
        cents = int(math.ceil(dollars_cents))
        with db.session() as conn:
            conn.execute(
                "INSERT INTO ai_usage (user_id, purpose, model, input_tokens, output_tokens, web_searches, cost_cents, paid_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.user_id, purpose, served_by, total_in, total_out, total_searches, cents, self.paid_by, db.now()),
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
    """Build the function that gives each user an AI client, or explains why they cannot have one."""
    from .security import decrypt_secret

    def factory(user: dict) -> AIClient:
        if user.get("own_api_key_enc"):
            key = decrypt_secret(settings.secret_key, user["own_api_key_enc"])
            if key:
                return AIClient(api_key=key, model=settings.model, user_id=user["id"], paid_by="own_key",
                                markup=settings.credit_markup)
        if settings.anthropic_api_key:
            if credits.balance(user["id"]) > 0:
                return AIClient(api_key=settings.anthropic_api_key, model=settings.model, user_id=user["id"],
                                paid_by="credits", markup=settings.credit_markup)
            raise AINotAvailable("To use the AI, buy credits or connect your own Claude API key on your account page.")
        raise AINotAvailable("To use the AI, connect your own Claude API key on your account page. This site does not provide AI credits yet.")

    return factory
