"""The AI on any provider that speaks the OpenAI-compatible Chat Completions API (studio/providers.py).

CompatClient has the same three methods as studio.ai.AIClient and shares its billing (BilledClient): the member
note before every system prompt, the site's monthly limits, credit holds and settling, and the ai_usage row
("provider:model"). Its cost is the usage the provider reports times the prices per million tokens set for the
model; with no prices (a member's own key, prices not given) the cost is recorded as not known.

- structured(): asks for response_format json_schema (strict, the schema as given). If the provider refuses that,
  it asks for json_object with the schema written into the system message. Either way the answer is checked
  against the schema here (schema_problems), and one repair turn says what did not fit.
- text(): a plain answer.
- research(): not offered: the standard API has no common web search, so studio.ai.RoutedAI sends it to Claude.
- Pictures (Anthropic-style base64 image blocks) are sent as image_url parts with a data: URL, only when the
  client is allowed pictures.
- No redirect is followed, a custom address is checked again before every request, and at most 2 retries are
  made on 429 and 5xx answers. The key is never put in a message, a log line or an exception.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from typing import Any

import httpx

from . import providers
from .ai import AIError, AINotAvailable, AIRefused, BilledClient, Usage

log = logging.getLogger("studio.compat_ai")

CHAT_TIMEOUT = httpx.Timeout(600.0, connect=15.0, write=60.0, pool=15.0)
MAX_RETRIES = 2
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}
# The most one picture can cost in input tokens with the providers we know (holds use it, so they always cover).
IMAGE_MAX_TOKENS = 6_000
_sleep = time.sleep  # tests replace this

REFUSAL = ("The AI would not do this request. This can happen by mistake, for example with words about passwords "
           "or PINs. Try saying it another way. If you think it should work, ask in Ask the community.")
TOO_LONG = ("The AI's answer was too long to finish and came back cut off, so nothing was saved. Please try again "
            "with a smaller request, for example one page or one change at a time.")
JSON_ONLY = ("Answer with one JSON object and nothing else: no words before or after it, and no code fence. It "
             "must match this JSON Schema exactly, with every listed property and no others:\n")
REPAIR = ("Your last answer did not match the JSON Schema the site needs. What did not fit: {problems}. Answer "
          "again with the whole JSON object only, matching the schema exactly, with no other text.")


# ----- checking answers against a schema ---------------------------------------------------------------

def _type_ok(value: Any, kind: str) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, float) and value.is_integer())
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "null":
        return value is None
    return True  # a type this checker does not know is not held against the answer


def schema_problems(value: Any, schema: dict, path: str = "the answer", limit: int = 10) -> list[str]:
    """What in value does not fit schema, in short phrases. Covers the JSON Schema the site's sections use
    (docs/DEVELOPING.md): type, properties, required, additionalProperties false, items, enum (and anyOf)."""
    problems: list[str] = []

    def check(v: Any, s: Any, where: str) -> None:
        if len(problems) >= limit or not isinstance(s, dict):
            return
        if "anyOf" in s and isinstance(s["anyOf"], list):
            if not any(not schema_problems(v, sub, where, 1) for sub in s["anyOf"]):
                problems.append(f"{where} fits none of the allowed shapes")
            return
        kinds = s.get("type")
        if kinds is not None:
            kinds_list = kinds if isinstance(kinds, list) else [kinds]
            if not any(_type_ok(v, k) for k in kinds_list):
                problems.append(f"{where} should be {' or '.join(kinds_list)}")
                return
        if "enum" in s and isinstance(s["enum"], list) and v not in s["enum"]:
            allowed = ", ".join(json.dumps(e) for e in s["enum"][:12])
            problems.append(f"{where} should be one of {allowed}")
            return
        if isinstance(v, dict):
            props = s.get("properties") if isinstance(s.get("properties"), dict) else {}
            for name in s.get("required") or []:
                if name not in v:
                    problems.append(f"{where} is missing {name}")
            if s.get("additionalProperties") is False:
                for name in v:
                    if name not in props:
                        problems.append(f"{where} has {name}, which is not in the schema")
            for name, sub in props.items():
                if name in v:
                    check(v[name], sub, f"{where}.{name}" if where != "the answer" else name)
        if isinstance(v, list) and isinstance(s.get("items"), dict):
            for i, item in enumerate(v):
                check(item, s["items"], f"{where}[{i}]")

    check(value, schema, path)
    return problems[:limit]


_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n?(.*?)\n?\s*```\s*$", re.S)


def parse_json(text: str) -> Any:
    """The JSON in an answer, without a code fence around it. Raises ValueError if there is none."""
    text = (text or "").strip()
    match = _FENCE.match(text)
    if match:
        text = match.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _error_text(response: httpx.Response) -> str:
    """The provider's error words, lower case, for deciding what to do. Never shown to anyone."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:2000].lower()
    if isinstance(body, list) and body:
        body = body[0]
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            return " ".join(str(err.get(k, "")) for k in ("message", "code", "type", "param", "status")).lower()
        return str(err).lower()
    return str(body).lower()


def _param_unsupported(error: str, param: str) -> bool:
    """The provider does not know the parameter at all (not: the value is too large, see _token_cap)."""
    return param in error and any(w in error for w in ("unsupported", "not supported", "unknown", "unrecognized",
                                                         "not allowed", "extra input", "extra field",
                                                         "extra_forbidden", "not permitted", "no such parameter"))


_TOO_MANY_TOKENS = ("too large", "too big", "too high", "at most", "maximum", "valid range", "exceed",
                    "less than or equal", "must be <=", "upper bound", "out of range")


def _token_cap(error: str, sent: int) -> int | None:
    """When a 400 says the longest answer asked for is more than the model allows, the most it allows (or 0 when
    the message gives no number). None when the error is about something else."""
    if not ("max_tokens" in error or "max_completion_tokens" in error or "max_output_tokens" in error
            or "output tokens" in error or "completion tokens" in error):
        return None
    if not any(w in error for w in _TOO_MANY_TOKENS):
        return None
    numbers = [int(n) for n in re.findall(r"(?<![\w.])(\d{3,7})(?![\w]|\.\d)", error.replace(",", ""))]
    allowed = [n for n in numbers if 0 < n < sent]
    return max(allowed) if allowed else 0


def _schema_refused(error: str) -> bool:
    """The provider refused the json_schema answer format (so json_object is worth a try)."""
    return any(w in error for w in ("response_format", "json_schema", "schema", "structured output",
                                    "structured_output", "json mode"))


def _model_missing(error: str) -> bool:
    return "model_not_found" in error or ("model" in error and any(
        w in error for w in ("not found", "does not exist", "not exist", "no such", "unknown model",
                             "invalid model", "not available", "decommissioned", "deprecated")))


def compat_cost_cents(prices: tuple[float, float], input_tokens: int, output_tokens: int, markup: float) -> int:
    in_price, out_price = prices
    return int(math.ceil(round((input_tokens / 1e6 * in_price + output_tokens / 1e6 * out_price) * 100 * markup, 6)))


class CompatClient(BilledClient):
    """An OpenAI-compatible Chat Completions provider, billed like Claude. Feature code calls structured() or
    text(); research() is always routed to Claude (studio.ai.RoutedAI)."""

    def __init__(self, *, provider: providers.Provider, api_key: str, model: str,
                 prices: tuple[float, float] | None, user_id: int, paid_by: str, markup: float,
                 member_note: str = "", allow_images: bool = False):
        super().__init__(model=model, user_id=user_id, paid_by=paid_by, markup=markup, member_note=member_note)
        self.provider = provider.id
        self.provider_info = provider
        self.prices = prices
        self.allow_images = allow_images
        self.__key = api_key  # name-mangled, and never printed
        self._token_param = provider.token_param
        self._json_mode = "json_schema"
        # The longest answer the model allows, once the provider has said so (see _token_cap).
        self._answer_cap: int | None = None

    def __repr__(self) -> str:
        return f"CompatClient(provider={self.provider!r}, model={self.model!r}, paid_by={self.paid_by!r})"

    # ----- public API -------------------------------------------------------------------------

    def structured(self, *, system: str, prompt: str | list[dict], schema: dict, purpose: str,
                   effort: str = "medium", max_tokens: int = 32000) -> dict:
        messages = [{"role": "system", "content": self._with_note(system)},
                    {"role": "user", "content": self._content(prompt)}]
        answer = self._chat(purpose, messages, max_tokens, schema)
        value, problems = self._checked(answer, schema)
        if not problems:
            return value
        repair = messages + [{"role": "assistant", "content": answer},
                             {"role": "user", "content": REPAIR.format(problems="; ".join(problems))}]
        answer = self._chat(purpose, repair, max_tokens, schema)
        value, problems = self._checked(answer, schema)
        if not problems:
            return value
        log.info("%s:%s answer did not fit the schema after one repair: %s", self.provider, self.model,
                 "; ".join(problems)[:500])
        raise AIError(f"The AI ({self.provider_info.name}, model {self.model}) twice gave an answer without the shape "
                      "the site needs, so nothing was saved. Please try again. If it happens again, "
                      f"{self._choose_another()}")

    def text(self, *, system: str, prompt: str | list[dict], purpose: str, effort: str = "medium",
             max_tokens: int = 16000) -> str:
        messages = [{"role": "system", "content": self._with_note(system)},
                    {"role": "user", "content": self._content(prompt)}]
        return self._chat(purpose, messages, max_tokens, None).strip()

    def research(self, **kwargs) -> Any:
        raise AINotAvailable("This step searches the live web, which on this site needs Claude, so it did not run "
                             "and nothing was charged. Please connect a Claude API key on Your account.")

    # ----- internals ----------------------------------------------------------------------------

    def _where(self) -> str:
        return "on Your account, on the AI key page" if self.paid_by == "own_key" else "in the site owner's AI settings"

    def _choose_another(self) -> str:
        if self.paid_by == "own_key":
            return "choose another model on Your account, on the AI key page."
        return "tell the site's owner, who can choose another model in AI settings."

    def _checked(self, answer: str, schema: dict) -> tuple[Any, list[str]]:
        try:
            value = parse_json(answer)
        except ValueError:
            return None, ["the answer was not JSON"]
        return value, schema_problems(value, schema)

    def _content(self, prompt: str | list[dict]) -> str | list[dict]:
        """A prompt as Chat Completions content: text stays text; Anthropic-style blocks become content parts,
        with pictures as image_url data: URLs (only when this client may send pictures)."""
        if isinstance(prompt, str):
            return prompt
        parts: list[dict] = []
        has_image = False
        for block in prompt:
            if not isinstance(block, dict):
                parts.append({"type": "text", "text": str(block)})
                continue
            if block.get("type") == "image":
                if not self.allow_images:
                    raise AINotAvailable("This step sends pictures to the AI, and the AI chosen for it here cannot "
                                         "look at pictures, so it did not run and nothing was charged.")
                source = block.get("source") or {}
                if source.get("type") == "base64":
                    url = f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
                else:
                    url = str(source.get("url", ""))
                parts.append({"type": "image_url", "image_url": {"url": url}})
                has_image = True
            else:
                parts.append({"type": "text", "text": str(block.get("text", ""))})
        if not has_image:
            # Some providers take only a string, so text alone is sent as one.
            return "\n\n".join(p["text"] for p in parts)
        return parts

    def _input_estimate(self, messages: list[dict], schema: dict | None) -> int:
        chars, images = 0, 0
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                for part in content:
                    if part.get("type") == "image_url":
                        images += 1
                    else:
                        chars += len(str(part.get("text", "")))
            else:
                chars += len(str(content or ""))
        if schema is not None:
            chars += 2 * len(json.dumps(schema))  # once in response_format, maybe again in the system message
        return chars // 3 + images * IMAGE_MAX_TOKENS + 1000

    def _answer_tokens(self, max_tokens: int) -> int:
        """The longest answer to ask for: max_tokens, or less when the model allows less."""
        return min(max_tokens, self._answer_cap) if self._answer_cap else max_tokens

    def _worst(self, messages: list[dict], max_tokens: int, schema: dict | None, markup: float) -> int:
        if self.prices is None:
            return 0
        return compat_cost_cents(self.prices, self._input_estimate(messages, schema), self._answer_tokens(max_tokens),
                                 markup)

    def _chat(self, purpose: str, messages: list[dict], max_tokens: int, schema: dict | None) -> str:
        if self.prices is None and self.paid_by != "own_key":
            raise AINotAvailable("The site's AI has no prices set for its model, so it cannot be used yet and nothing "
                                 "was charged. The site's owner can set them in AI settings.")

        def send() -> tuple[tuple[str, str], Usage]:
            data = self._post(messages, max_tokens, schema)
            choices = data.get("choices") if isinstance(data, dict) else None
            if not choices or not isinstance(choices[0], dict):
                raise AIError(f"{self.provider_info.name}'s answer was empty, so nothing was saved. Please try again.")
            choice = choices[0]
            message = choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
            text = content if isinstance(content, str) else ""
            finish = str(choice.get("finish_reason") or "")
            if message.get("refusal"):
                finish = "content_filter"
            return (text, finish), self._usage(data, messages, text)

        text, finish = self._billed(purpose, lambda markup: self._worst(messages, max_tokens, schema, markup), send)
        if finish == "content_filter":
            raise AIRefused(REFUSAL)
        if finish == "length":
            raise AIError(TOO_LONG)
        return text

    def _usage(self, data: dict, messages: list[dict], text: str) -> Usage:
        """What the call used. Tokens a provider reports outside completion_tokens (thinking that only shows in
        total_tokens, or reasoning_tokens reported apart) are counted as output, and a count the provider leaves
        out is estimated at about 4 characters a token, so no call is recorded as free by mistake."""
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}

        def count(value: Any) -> int | None:
            if isinstance(value, bool):
                return None
            try:
                number = int(value)
            except (TypeError, ValueError):
                return None
            return number if number >= 0 else None

        input_tokens = count(usage.get("prompt_tokens"))
        output_tokens = count(usage.get("completion_tokens"))
        total = count(usage.get("total_tokens"))
        details = usage.get("completion_tokens_details")
        reasoning = count(details.get("reasoning_tokens")) if isinstance(details, dict) else None
        if input_tokens is None:
            input_tokens = self._input_estimate(messages, None)
        if output_tokens is None:
            output_tokens = max(len(text) // 4, 1)
            if reasoning:
                output_tokens += reasoning
        elif reasoning and reasoning > output_tokens:
            output_tokens += reasoning  # cannot be part of completion_tokens, so it was reported apart
        if total is not None:
            output_tokens = max(output_tokens, total - input_tokens)
        raw = 0.0
        if self.prices is not None:
            raw = (input_tokens / 1e6 * self.prices[0] + output_tokens / 1e6 * self.prices[1]) * 100
        return Usage(model=f"{self.provider}:{self.model}", input_tokens=input_tokens, output_tokens=output_tokens,
                     web_searches=0, raw_cents=raw, cost_known=self.prices is not None)

    def _body(self, messages: list[dict], max_tokens: int, schema: dict | None) -> dict:
        body: dict[str, Any] = {"model": self.model, "messages": messages, self._token_param: max_tokens}
        if schema is not None:
            if self._json_mode == "json_schema":
                body["response_format"] = {"type": "json_schema",
                                           "json_schema": {"name": "answer", "strict": True, "schema": schema}}
            else:
                body["response_format"] = {"type": "json_object"}
                first = dict(messages[0])
                first["content"] = f"{first.get('content') or ''}\n\n{JSON_ONLY}{json.dumps(schema)}".strip()
                body["messages"] = [first] + list(messages[1:])
        return body

    def _post(self, messages: list[dict], max_tokens: int, schema: dict | None) -> dict:
        provider = self.provider_info
        name = provider.name
        swapped = fell_back = False
        clamps = 0
        while True:
            sent = self._answer_tokens(max_tokens)
            response = self._send(self._body(messages, sent, schema))
            if response.status_code in (400, 422):
                error = _error_text(response)
                cap = _token_cap(error, sent)
                if cap is not None:
                    if cap and clamps < 2:
                        # The model allows shorter answers than this step asks for: ask for the most it allows.
                        clamps += 1
                        self._answer_cap = cap
                        continue
                    raise AIError(f"{name}'s model {self.model} cannot write answers as long as this step needs, so "
                                  f"nothing was done. To keep working, {self._choose_another()}")
                if not swapped and _param_unsupported(error, self._token_param):
                    swapped = True
                    self._token_param = ("max_tokens" if self._token_param == "max_completion_tokens"
                                         else "max_completion_tokens")
                    continue
                if (schema is not None and self._json_mode == "json_schema" and not fell_back
                        and _schema_refused(error) and not _model_missing(error)):
                    fell_back = True
                    self._json_mode = "json_object"
                    continue
            break
        status = response.status_code
        if status < 300:
            try:
                data = response.json()
            except ValueError:
                raise AIError(f"{name}'s answer could not be read, so nothing was saved. Please try again.") from None
            return data if isinstance(data, dict) else {}
        log.info("%s answered %s for model %s", provider.id, status, self.model)
        own = self.paid_by == "own_key"
        if 300 <= status < 400:
            raise AIError(f"{name} sent the request to another address. For safety the site does not follow that, "
                          f"so nothing was done. {self._fix_address()}")
        if status in (401, 403):
            if own:
                raise AIError(f"{name} did not accept your API key, so nothing was done. The key may have been "
                              f"deleted or changed on {name}'s website. Please connect it again on Your account, "
                              "on the AI key page.")
            raise AIError(f"The site's key for {name} is not working right now, so nothing was done and nothing "
                          "was charged. The site's owner can check it in AI settings. Please try again later, or "
                          "connect your own key on Your account.")
        error = _error_text(response) if status in (400, 404, 422) else ""
        if status == 404 or _model_missing(error):
            raise AIError(f"{name} could not find the model {self.model}, so nothing was done. The model may not "
                          f"exist any more. To keep working, {self._choose_another()}")
        if status == 429:
            raise AIError(f"{name} is busy right now, so nothing was done. Please try again in a minute.")
        if status >= 500:
            raise AIError(f"{name} had a problem on its side (code {status}), so nothing was done. Please try "
                          "again in a few minutes.")
        raise AIError(f"{name} could not do this request (code {status}), so nothing was done. Please try again. "
                      f"If it happens again, {self._choose_another()}")

    def _fix_address(self) -> str:
        if self.provider == providers.CUSTOM:
            return "The site's owner can check the provider's address in AI settings."
        return "Please try again later."

    def _send(self, body: dict) -> httpx.Response:
        """One request, with at most MAX_RETRIES more on 429 and 5xx answers, waiting longer each time. A custom
        address is checked before every attempt, and each connection to it is pinned to a checked public address
        (providers.PublicOnlyTransport)."""
        provider = self.provider_info
        url = f"{provider.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.__key}", "Content-Type": "application/json",
                   "Accept": "application/json"}
        name = provider.name
        attempt = 0
        while True:
            try:
                providers.check_before_request(provider)
            except providers.ProviderError as exc:
                raise AIError(f"{exc.user_message} {self._fix_address()}") from None
            try:
                with providers.http_client(CHAT_TIMEOUT, provider) as client:
                    response = client.post(url, headers=headers, json=body)
            except httpx.TimeoutException:
                raise AIError(f"{name} did not answer in time, so nothing was done. Please try again in a few "
                              "minutes, perhaps with a smaller request.") from None
            except httpx.HTTPError:
                raise AIError(f"The site could not reach {name}, so nothing was done. Please try again in a few "
                              "minutes.") from None
            if response.status_code not in RETRY_STATUSES or attempt >= MAX_RETRIES:
                return response
            attempt += 1
            wait = float(2 ** (attempt - 1))
            try:
                wait = min(max(float(response.headers.get("retry-after", wait)), 0.0), 20.0)
            except ValueError:
                pass
            _sleep(wait)
