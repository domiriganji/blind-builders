"""The AI providers the site can use, and the safe way to talk to their addresses.

- "anthropic" is Claude, through the Anthropic SDK (studio/ai.py, AIClient). It is the default, and the only
  provider for steps that search the live web.
- Every other provider speaks the OpenAI-compatible Chat Completions API (studio/compat_ai.py, CompatClient).
  PRESETS is the one list of them: add, change or remove a provider there. Their addresses are fixed here,
  and the companies can change them, so check them now and then (docs/DEVELOPING.md says how).
- "custom" is another compatible provider at an address the site owner types (members never can). The address
  must be https, with no user name, password, query or fragment, and its host must resolve only to public
  internet addresses. The host is turned into ASCII the way httpx does (IDNA 2008), so the name checked is the
  name connected to. It is checked when it is saved and before every request, and every connection to it goes
  through PublicOnlyBackend, which resolves the name again, checks each address, and connects to exactly the
  address it checked (so DNS that changes between the check and the connection cannot lead anywhere private).
- Model names are never written here: they come from each provider's own GET {base}/models list, so new models
  appear by themselves, and a model can always be typed instead if the list cannot be read.
- No request follows a redirect.
"""
from __future__ import annotations

import ipaddress
import socket
import time
import urllib.parse
from dataclasses import dataclass
from threading import Lock
from typing import Any

import httpcore
import httpx

ANTHROPIC = "anthropic"
CUSTOM = "custom"


@dataclass(frozen=True)
class Provider:
    id: str
    name: str             # the company or service, as members read it
    about: str            # one plain sentence for the choice lists
    base_url: str         # the Chat Completions base address; "" for CUSTOM, where the owner types it
    keys_url: str = ""    # where to create an API key
    prices_url: str = ""  # where the provider lists its prices
    # The request field for the longest answer: OpenAI's newer models take only max_completion_tokens.
    token_param: str = "max_tokens"
    # True when the provider's /models list includes prices (OpenRouter), used to pre-fill the price fields.
    lists_prices: bool = False


CLAUDE = Provider(ANTHROPIC, "Anthropic (Claude)", "Claude, the AI this site was built with.",
                  "https://api.anthropic.com", "https://platform.claude.com/", "https://www.anthropic.com/pricing")

# The OpenAI-compatible providers ready to choose. Addresses can change: check each provider's documentation.
PRESETS: list[Provider] = [
    Provider("openai", "OpenAI", "The company that makes the GPT models.", "https://api.openai.com/v1",
             "https://platform.openai.com/api-keys", "https://openai.com/api/pricing/",
             token_param="max_completion_tokens"),
    Provider("gemini", "Google Gemini", "Google's Gemini models.",
             "https://generativelanguage.googleapis.com/v1beta/openai", "https://aistudio.google.com/apikey",
             "https://ai.google.dev/gemini-api/docs/pricing"),
    Provider("openrouter", "OpenRouter", "Many companies' models with one key.", "https://openrouter.ai/api/v1",
             "https://openrouter.ai/settings/keys", "https://openrouter.ai/models", lists_prices=True),
    Provider("deepseek", "DeepSeek", "DeepSeek's models.", "https://api.deepseek.com/v1",
             "https://platform.deepseek.com/api_keys", "https://api-docs.deepseek.com/quick_start/pricing"),
    Provider("mistral", "Mistral", "Mistral AI's models.", "https://api.mistral.ai/v1",
             "https://console.mistral.ai/api-keys", "https://mistral.ai/pricing"),
    Provider("groq", "Groq", "Open models served very fast.", "https://api.groq.com/openai/v1",
             "https://console.groq.com/keys", "https://groq.com/pricing"),
    Provider("xai", "xAI (Grok)", "xAI's Grok models.", "https://api.x.ai/v1",
             "https://console.x.ai/", "https://docs.x.ai/docs/models"),
]
PRESET_IDS = {p.id for p in PRESETS}
CUSTOM_PROVIDER = Provider(CUSTOM, "Another compatible provider",
                           "Any other service that speaks the OpenAI-compatible Chat Completions API, at an "
                           "address you type.", "")

# Test hook: an httpx transport (for example httpx.MockTransport) used for every request instead of the network.
TRANSPORT: httpx.BaseTransport | None = None
MODELS_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
# Fees in an OpenRouter-style price list that are not per token. A model with any of them is never priced from the
# list, because the site's limits and credits count tokens only.
OTHER_FEES = ("request", "image", "web_search", "input_audio", "audio")
MAX_MODELS = 500
# Kinds of model in a /models list that cannot write answers (pictures, speech, search vectors). They are left out
# of the choice list; any model name can still be typed.
NOT_CHAT_WORDS = ("embed", "tts", "whisper", "dall-e", "moderation", "transcribe", "realtime", "-audio", "sora",
                  "image-gen", "gpt-image")


class ProviderError(Exception):
    """A provider problem with a message that is safe to show (it never holds the key)."""

    def __init__(self, user_message: str, status: int | None = None):
        super().__init__(user_message)
        self.user_message = user_message
        self.status = status


# ----- looking providers up -------------------------------------------------------------------------

def get(provider_id: str) -> Provider | None:
    if provider_id == ANTHROPIC:
        return CLAUDE
    if provider_id == CUSTOM:
        return CUSTOM_PROVIDER
    return next((p for p in PRESETS if p.id == provider_id), None)


def resolved(provider_id: str, *, base_url: str = "", name: str = "") -> Provider | None:
    """The provider to use: a preset as listed, or the custom provider with the owner's address and name."""
    if provider_id == CUSTOM:
        if not base_url:
            return None
        return Provider(CUSTOM, name or "Your other provider", CUSTOM_PROVIDER.about, base_url.rstrip("/"))
    return get(provider_id)


def display_name(provider_id: str, custom_name: str = "") -> str:
    if provider_id == CUSTOM:
        return custom_name or "your other provider"
    provider = get(provider_id)
    return provider.name if provider else provider_id


def model_label(stored: str, custom_name: str = "") -> str:
    """A model as ai_usage stores it ("openai:gpt-x", or a Claude model id), in plain words."""
    from . import site_ai  # here, not at the top: site_ai imports this module

    provider_id, sep, model = stored.partition(":")
    if sep and (provider_id in PRESET_IDS or provider_id == CUSTOM):
        return f"{display_name(provider_id, custom_name)}: {model}"
    return site_ai.model_name(stored)


# ----- addresses ----------------------------------------------------------------------------------------

def resolve(host: str) -> list[str]:
    """Every address the host name resolves to. Tests replace this."""
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def _public(address: str) -> bool:
    """True only for an ordinary public internet address (IPv4 or IPv6)."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if inner is not None and not _public(str(inner)):
            return False
        if ip.is_site_local:
            return False
    if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified):
        return False
    return ip.is_global


ADDRESS_SHAPE = ("Type the provider's full address for the OpenAI-compatible API, starting with https://, "
                 "for example https://api.example.com/v1.")


def address_problem(url: str, *, resolve_host: bool = True) -> str | None:
    """A plain message if the owner's address for another provider is not safe to use, otherwise None.

    It must be https, with a host name, and no user name, password, query or fragment. Its host must resolve,
    and only to public internet addresses: never this server, the local network, or special addresses."""
    url = (url or "").strip()
    if not url:
        return ADDRESS_SHAPE
    if len(url) > 300 or any(ch.isspace() for ch in url):
        return "The address has a space in it or is too long. " + ADDRESS_SHAPE
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return "This does not look like a web address. " + ADDRESS_SHAPE
    if parts.scheme != "https":
        return "The address must start with https://, so the key is always sent encrypted. " + ADDRESS_SHAPE
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return ("The address has a user name or password in it. Take that part out: the key goes in its own box. "
                + ADDRESS_SHAPE)
    if parts.query or parts.fragment or url.endswith("?") or "#" in url:
        return "The address has a ? or # part. Take it out. " + ADDRESS_SHAPE
    if not (parts.hostname or "").strip("."):
        return "The address has no host name. " + ADDRESS_SHAPE
    host = connect_host(url)  # the name httpx will look up, which is the one to check
    if host is None:
        return "This does not look like a web address. " + ADDRESS_SHAPE
    if port is not None and not 1 <= port <= 65535:
        return "The address has a port number that cannot be used. " + ADDRESS_SHAPE
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is None and not resolve_host and not _local_name(host):
        return None
    addresses = _addresses(host)
    if not addresses:
        return (f"The site could not find {parts.hostname or host} on the internet. Check the address and try "
                "again.")
    if not all(_public(a) for a in addresses):
        return ("This address leads to this server, a private network or a special internet address, which the "
                "site never sends keys to. Use the provider's public address on the internet.")
    return None


def normalize_address(url: str) -> str:
    return url.strip().rstrip("/")


def connect_host(url: str) -> str | None:
    """The host name exactly as httpx sends it to DNS: lower case ASCII, with international names in IDNA 2008
    (xn--) form. Python's own socket module would use IDNA 2003, which can name a different domain, so checks
    always use this. None if httpx cannot read the address."""
    try:
        raw = httpx.URL(url).raw_host
    except (httpx.InvalidURL, UnicodeError, ValueError):
        return None
    try:
        host = raw.decode("ascii").strip(".").lower()
    except UnicodeDecodeError:
        return None
    return host or None


def _local_name(host: str) -> bool:
    return host == "localhost" or host.endswith(".localhost")


def _addresses(host: str) -> list[str]:
    """Every address an ASCII host name (or a literal address) leads to, or [] if it cannot be found."""
    try:
        return [str(ipaddress.ip_address(host.split("%", 1)[0]))]
    except ValueError:
        pass
    if _local_name(host):
        return ["127.0.0.1"]
    try:
        return resolve(host)
    except (OSError, UnicodeError):
        return []


class PublicOnlyBackend(httpcore.SyncBackend):
    """The network layer for owner-typed addresses: each connection resolves the name once, refuses unless every
    address is public, and connects to one of exactly those addresses. TLS still checks the certificate against
    the name in the address (httpcore passes the original host as the server name)."""

    def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                    socket_options=None):
        addresses = _addresses(host.strip(".").lower())
        if not addresses or not all(_public(a) for a in addresses):
            raise httpcore.ConnectError("The address does not lead only to public internet addresses.")
        last: Exception | None = None
        for address in addresses:
            try:
                return super().connect_tcp(address, port, timeout=timeout, local_address=local_address,
                                           socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        raise last if last else httpcore.ConnectError("No address could be reached.")


class PublicOnlyTransport(httpx.HTTPTransport):
    """An httpx transport whose every connection goes through PublicOnlyBackend. It never uses proxy settings from
    the environment: a proxy would resolve the name itself, out of reach of the check."""

    def __init__(self) -> None:
        super().__init__(trust_env=False)
        self._pool = httpcore.ConnectionPool(ssl_context=httpx.create_ssl_context(trust_env=False),
                                             network_backend=PublicOnlyBackend())


# ----- talking to providers ---------------------------------------------------------------------------

def http_client(timeout: httpx.Timeout, provider: Provider | None = None) -> httpx.Client:
    """An httpx client that never follows redirects. For the owner's custom provider, every connection is checked
    and pinned by PublicOnlyTransport. Tests set TRANSPORT to keep it off the network."""
    transport = TRANSPORT
    if transport is None and provider is not None and provider.id == CUSTOM:
        transport = PublicOnlyTransport()
    return httpx.Client(timeout=timeout, follow_redirects=False, transport=transport, trust_env=transport is None)


def check_before_request(provider: Provider) -> None:
    """Raise ProviderError if the provider's address is not safe now (custom addresses only: presets are fixed)."""
    if provider.id == CUSTOM:
        problem = address_problem(provider.base_url)
        if problem:
            raise ProviderError(f"The address saved for {provider.name} cannot be used now, so nothing was "
                                f"sent to it. {problem}")


def _prices_from(item: dict) -> tuple[float, float] | None:
    pricing = item.get("pricing") if isinstance(item, dict) else None
    if not isinstance(pricing, dict):
        return None
    try:
        per_in = float(pricing.get("prompt"))
        per_out = float(pricing.get("completion"))
    except (TypeError, ValueError):
        return None
    if per_in < 0 or per_out < 0:
        return None  # "varies", for example a router that picks a model for you
    for fee in OTHER_FEES:
        try:
            if float(pricing.get(fee) or 0) != 0:
                return None  # a fee per request or per picture: the owner types prices that allow for it
        except (TypeError, ValueError):
            return None
    try:
        reasoning = float(pricing.get("internal_reasoning") or 0)
    except (TypeError, ValueError):
        return None
    if reasoning not in (0, per_out):
        return None  # thinking billed at its own price, which token prices alone cannot follow
    return round(per_in * 1e6, 4), round(per_out * 1e6, 4)


def _chat_model(model_id: str) -> bool:
    lowered = model_id.lower()
    return not any(word in lowered for word in NOT_CHAT_WORDS)


def _list_problem(provider: Provider, status: int | None, kind: str, checking: bool) -> ProviderError:
    """A plain message for a model list that could not be read.

    checking: True while checking a new key before it is saved (so the key was not saved), False when listing the
    models for a key that is already saved (so the key stays saved, and the model can be typed instead)."""
    name = provider.name
    custom = provider.id == CUSTOM
    if checking:
        outcome = "so the key was not checked or saved"
        fallback = "Please try again in a minute."
    else:
        outcome = "so its list of models could not be read"
        fallback = "Please try again in a minute, or type the model name below."
    address = "Check the provider's address. " if custom else ""
    if kind == "timeout":
        text = f"{name} did not answer in time, {outcome}. {fallback}"
    elif kind == "unreachable":
        text = f"The site could not reach {name}, {outcome}. {address}{fallback}"
    elif kind == "redirect":
        text = (f"{name} sent the site to another address. For safety the site does not follow that, {outcome}. "
                f"{address}{fallback}")
    elif kind == "refused":
        if checking:
            text = (f"{name} did not accept this key, so it was not saved. It may have been deleted, or part of it "
                    f"may be missing. Check the key on {name}'s website and paste it again.")
        else:
            text = (f"{name} did not accept the saved key, so its list of models could not be read. The key may have "
                    f"been deleted on {name}'s website. Please save the key again.")
    elif kind == "busy":
        text = f"{name} is busy right now, {outcome}. {fallback}"
    elif kind == "no_list":
        where = ("Check that the address is the one for the OpenAI-compatible API. " if custom
                 else "Please try again later. ")
        text = f"{name} has no list of models at this address, {outcome}. {where}" + (
            "" if checking else "You can type the model name below.")
    elif kind == "not_a_list":
        where = ("Check that the address is the one for the OpenAI-compatible API. " if custom
                 else "Please try again later. ")
        text = f"{name}'s answer was not a list of models, {outcome}. {where}" + (
            "" if checking else "You can type the model name below.")
    else:
        text = f"{name} had a problem (code {status}), {outcome}. {fallback}"
    return ProviderError(text.strip(), status)


def fetch_models(provider: Provider, api_key: str, *, checking: bool = True) -> list[dict[str, Any]]:
    """The provider's own list of models: [{"id", "name", "prices": (in, out) per million tokens or None}].

    Raises ProviderError with a plain message (never holding the key) if the list cannot be read. A list that
    can be read with this key is how the site checks a key before saving it (checking=True); pages that list the
    models for a saved key pass checking=False, so the message says the key is still saved."""
    check_before_request(provider)
    try:
        with http_client(MODELS_TIMEOUT, provider) as client:
            response = client.get(f"{provider.base_url.rstrip('/')}/models",
                                  headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
    except httpx.TimeoutException:
        raise _list_problem(provider, None, "timeout", checking) from None
    except httpx.HTTPError:
        raise _list_problem(provider, None, "unreachable", checking) from None
    status = response.status_code
    if 300 <= status < 400:
        raise _list_problem(provider, status, "redirect", checking)
    if status in (401, 403):
        raise _list_problem(provider, status, "refused", checking)
    if status == 429:
        raise _list_problem(provider, status, "busy", checking)
    if status == 404:
        raise _list_problem(provider, status, "no_list", checking)
    if status >= 400:
        raise _list_problem(provider, status, "problem", checking)
    try:
        body = response.json()
    except ValueError:
        raise _list_problem(provider, status, "not_a_list", checking) from None
    items = body.get("data") if isinstance(body, dict) else body
    if isinstance(body, dict) and items is None:
        items = body.get("models")
    if not isinstance(items, list):
        raise _list_problem(provider, status, "not_a_list", checking)
    models: dict[str, dict] = {}
    for item in items:
        model_id = item.get("id") if isinstance(item, dict) else item if isinstance(item, str) else None
        if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 200:
            continue
        model_id = model_id.strip()
        if not _chat_model(model_id) or model_id in models:
            continue
        label = item.get("name") if isinstance(item, dict) and isinstance(item.get("name"), str) else ""
        models[model_id] = {"id": model_id, "name": label.strip()[:120] if label and label != model_id else "",
                            "prices": _prices_from(item) if isinstance(item, dict) else None}
    return sorted(models.values(), key=lambda m: m["id"].lower())[:MAX_MODELS]


# A short memory of each saved key's model list, so pages do not ask the provider on every visit. A list that could
# not be read is remembered for a minute too, so a provider that is down or refuses the key is not asked again on
# every page (pages fetch the list in a worker thread, never on the server's event loop).
_CACHE: dict[tuple[str, str, int], tuple[float, list[dict] | ProviderError]] = {}
_CACHE_LOCK = Lock()
CACHE_SECONDS = 600
FAILURE_SECONDS = 60


def cached_models(provider: Provider, api_key: str) -> list[dict[str, Any]]:
    """The model list for a saved key (fetch_models with checking=False), remembered for a while."""
    key = (provider.id, provider.base_url, hash(api_key))
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit:
            age = time.monotonic() - hit[0]
            if isinstance(hit[1], ProviderError):
                if age < FAILURE_SECONDS:
                    raise hit[1]
            elif age < CACHE_SECONDS:
                return hit[1]
    try:
        models = fetch_models(provider, api_key, checking=False)
    except ProviderError as exc:
        with _CACHE_LOCK:
            _CACHE[key] = (time.monotonic(), exc)
        raise
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), models)
    return models


def forget_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


# ----- keys and model names typed by people --------------------------------------------------------------

def key_problem(key: str, provider_name: str) -> str | None:
    """A plain message if the text cannot be an API key, otherwise None (each company's keys look different)."""
    if not key:
        return f"Paste your API key from {provider_name}."
    if any(ch.isspace() for ch in key):
        return "The key has a space or line break in it. Copy it again, without spaces."
    if len(key) < 16:
        return "This is shorter than an API key. Check you copied the whole key."
    if len(key) > 500:
        return "This is longer than an API key. Check you copied only the key."
    if not all(33 <= ord(ch) < 127 for ch in key):
        return "The key has characters an API key does not use. Copy it again."
    return None


def model_problem(model: str) -> str | None:
    if not model:
        return "Choose a model from the list, or type its name."
    if len(model) > 200 or not all(ch.isalnum() or ch in "._:/@+-" for ch in model):
        return ("Type the model name exactly as the provider writes it, with no spaces, for example "
                "provider-model-1.")
    return None


def parse_price(text: str) -> float | None:
    """US dollars per million tokens from text like "2.50" or "$0.15", or None if it is not a price."""
    text = (text or "").strip().lstrip("$").strip().replace(",", "")
    if not text or len(text) > 12:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if value != value or value < 0 or value > 10_000:
        return None
    return value


def price_text(value: float) -> str:
    """A price for a form field or a sentence: 2.5 -> "2.50", 0.075 -> "0.075"."""
    text = f"{value:.4f}".rstrip("0")
    whole, _, frac = text.partition(".")
    return f"{whole}.{frac.ljust(2, '0')}"
