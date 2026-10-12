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
    text: str
    sources: list[Source] = field(default_factory=list)


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
        """Answer using live web search (and page fetch). Returns the text and the sources it used."""
        tools: list[dict] = [dict(WEB_SEARCH_TOOL, max_uses=max_searches)]
        if fetch:
            tools.append(dict(WEB_FETCH_TOOL, max_uses=max_searches))
        messages: list[dict] = [{"role": "user", "content": prompt}]
        texts: list[str] = []
        sources: dict[str, Source] = {}
        for _ in range(6):  # resume paused server-tool turns a few times at most
            message = self._call(purpose=purpose, system=system, messages=messages, max_tokens=max_tokens,
                                 output_config={"effort": effort}, tools=tools)
            for block in message.content:
                if block.type == "text":
                    texts.append(block.text)
                    for c in getattr(block, "citations", None) or []:
                        url = getattr(c, "url", None)
                        if url:
                            src = sources.setdefault(url, Source(url=url, title=getattr(c, "title", "") or url))
                            if not src.quote:
                                src.quote = (getattr(c, "cited_text", "") or "")[:300]
                elif block.type == "web_search_tool_result":
                    content = getattr(block, "content", None)
                    if isinstance(content, list):
                        for r in content:
                            url = getattr(r, "url", None)
                            if url and url not in sources:
                                sources[url] = Source(url=url, title=getattr(r, "title", "") or url)
            if message.stop_reason != "pause_turn":
                break
            messages = [{"role": "user", "content": prompt}, {"role": "assistant", "content": message.content}]
        return ResearchResult(text="".join(texts).strip(), sources=list(sources.values()))

    # ----- internals ----------------------------------------------------------------------------

    def _call(self, *, purpose: str, **kwargs) -> Any:
        if self.paid_by == "credits" and credits.balance(self.user_id) <= 0:
            raise AINotAvailable("You have no credits left. Buy credits or connect your own Claude API key on your account page.")
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
        self._record_usage(purpose, message)
        if message.stop_reason == "refusal":
            raise AIRefused("The AI declined this request. Try rewording it, or ask in Help if you think this is a mistake.")
        if message.stop_reason == "max_tokens":
            raise AIError("The answer was too long and got cut off. Try asking for something smaller.")
        return message

    def _record_usage(self, purpose: str, message: Any) -> None:
        usage = getattr(message, "usage", None)
        if usage is None:
            return
        input_tokens = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        # Cache reads cost about a tenth of normal input.
        input_tokens += int((getattr(usage, "cache_read_input_tokens", 0) or 0) * 0.1)
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        server = getattr(usage, "server_tool_use", None)
        searches = (getattr(server, "web_search_requests", 0) or 0) if server else 0
        model = getattr(message, "model", self.model) or self.model
        cents = cost_cents(model, input_tokens, output_tokens, searches, self.markup if self.paid_by == "credits" else 1.0)
        with db.session() as conn:
            conn.execute(
                "INSERT INTO ai_usage (user_id, purpose, model, input_tokens, output_tokens, web_searches, cost_cents, paid_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.user_id, purpose, model, input_tokens, output_tokens, searches, cents, self.paid_by, db.now()),
            )
        if self.paid_by == "credits" and cents > 0:
            credits.add(self.user_id, -cents, f"AI: {purpose}")


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
