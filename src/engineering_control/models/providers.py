"""The providers a model can be asked at over their API.

Beside the two subscriptions (a command-line client signed in to Claude or ChatGPT),
a model can be reached with an API key, paid by use. Anthropic
is spoken to with its own library; the other text providers accept the same kind of request
(OpenAI's chat completions), each at its own address.

An address is chosen from the ones listed here, never typed: a key is sent nowhere else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final


@dataclass(frozen=True)
class Endpoint:
    key: str
    name: str
    url: str


@dataclass(frozen=True)
class ApiProvider:
    key: str
    name: str
    # Whose models these are. A change is not judged by the family that wrote it: the
    # Anthropic API and a Claude subscription are one family, OpenAI's and ChatGPT's another.
    family: str
    # How the provider is spoken to: "anthropic" (its own library) or "openai" (chat
    # completions over HTTP).
    style: str
    endpoints: tuple[Endpoint, ...]
    # Where a key is made, said to the administrator.
    keys_at: str
    # What a key of this provider looks like, when the provider says.
    key_pattern: re.Pattern[str] | None = None
    key_shape: str = ""
    # What the limit on the answer's length is called in a request.
    limit_name: str = "max_tokens"
    # Whether the provider holds the model to the shape of the answer itself ("schema")
    # or only to a JSON object ("object"), the platform checking the shape either way.
    shape: str = "object"
    # Whether a request can say how hard the model reasons.
    effort: bool = False
    # What the provider puts before the name of a model when it lists or names one.
    model_prefix: str = ""
    # How the provider reads the beginning of a prompt again from a cache instead of
    # anew, at a lower price: by itself ("automatic"), or only where the request marks
    # the end of what is to be kept ("marked").
    cache: str = "automatic"
    # What a token written to the cache costs, as a multiple of a token that is only
    # read: kept for minutes, kept for an hour. 1 where writing costs nothing more.
    cache_write: tuple[float, float] = (1.0, 1.0)
    # How the provider takes questions in a batch, answered within hours at a lower
    # price: "" when it takes none, "anthropic" (its own interface), "files" (a file of
    # requests is uploaded and a batch made of it, as OpenAI has it) or "google".
    batch: str = ""
    # What a token costs in a batch, as a part of what it costs asked at once.
    batch_price: float = 0.5
    # For a provider that takes a batch as a file of requests: what a request names as
    # the place it is put to.
    batch_endpoint: str = "/v1/chat/completions"

    def endpoint(self, key: str | None) -> Endpoint:
        for endpoint in self.endpoints:
            if endpoint.key == key:
                return endpoint
        return self.endpoints[0]

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "family": self.family,
            "endpoints": [
                {"key": e.key, "name": e.name, "host": host_of(e.url)} for e in self.endpoints
            ],
            "keys_at": self.keys_at,
            "key_shape": self.key_shape,
            "cache": self.cache,
            "batch": bool(self.batch),
            "batch_price": self.batch_price,
        }


def host_of(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


API_PROVIDERS: Final = (
    ApiProvider(
        key="anthropic",
        name="Anthropic API",
        family="claude",
        style="anthropic",
        endpoints=(Endpoint("default", "Anthropic", "https://api.anthropic.com"),),
        keys_at="console.anthropic.com → API keys",
        key_pattern=re.compile(r"^sk-ant-api\d{2}-[A-Za-z0-9_-]{40,400}$"),
        key_shape="it starts with sk-ant-api",
        cache="marked",
        cache_write=(1.25, 2.0),
        batch="anthropic",
    ),
    ApiProvider(
        key="openai",
        name="OpenAI API",
        family="chatgpt",
        style="openai",
        endpoints=(Endpoint("default", "OpenAI", "https://api.openai.com/v1"),),
        keys_at="platform.openai.com → API keys",
        key_pattern=re.compile(r"^sk-(?!or-)[A-Za-z0-9_-]{20,400}$"),
        key_shape="it starts with sk-",
        limit_name="max_completion_tokens",
        shape="schema",
        effort=True,
        batch="files",
    ),
    ApiProvider(
        key="openrouter",
        name="OpenRouter",
        family="openrouter",
        style="openai",
        endpoints=(Endpoint("default", "OpenRouter", "https://openrouter.ai/api/v1"),),
        keys_at="openrouter.ai/settings/keys",
        key_pattern=re.compile(r"^sk-or-[A-Za-z0-9_-]{16,380}$"),
        key_shape="it starts with sk-or-",
        shape="schema",
    ),
    ApiProvider(
        # The Gemini API of Google AI Studio, through the address at which it accepts
        # chat completions. Vertex AI, which signs in with a Google Cloud account and
        # not with a key, is not this.
        key="google",
        name="Google Gemini API",
        family="gemini",
        style="openai",
        endpoints=(
            Endpoint(
                "default", "Google", "https://generativelanguage.googleapis.com/v1beta/openai"
            ),
        ),
        keys_at="aistudio.google.com → API keys",
        shape="schema",
        effort=True,
        model_prefix="models/",
        batch="google",
    ),
    ApiProvider(
        key="deepseek",
        name="DeepSeek",
        family="deepseek",
        style="openai",
        endpoints=(Endpoint("default", "DeepSeek", "https://api.deepseek.com"),),
        keys_at="platform.deepseek.com → API keys",
    ),
    ApiProvider(
        key="qwen",
        name="Qwen",
        family="qwen",
        style="openai",
        endpoints=(
            Endpoint(
                "international",
                "International (Singapore)",
                "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            ),
            Endpoint(
                "china", "China (Beijing)", "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ),
        ),
        keys_at="Alibaba Cloud Model Studio → API keys (a key works in its own region only)",
        batch="files",
    ),
    ApiProvider(
        key="glm",
        name="GLM",
        family="glm",
        style="openai",
        endpoints=(
            Endpoint("international", "International (Z.ai)", "https://api.z.ai/api/paas/v4"),
            Endpoint("china", "China (BigModel)", "https://open.bigmodel.cn/api/paas/v4"),
        ),
        keys_at="z.ai → API keys (or open.bigmodel.cn for the Chinese platform)",
    ),
    ApiProvider(
        key="kimi",
        name="Kimi",
        family="kimi",
        style="openai",
        endpoints=(
            Endpoint("international", "International", "https://api.moonshot.ai/v1"),
            Endpoint("china", "China", "https://api.moonshot.cn/v1"),
        ),
        keys_at="platform.moonshot.ai → API keys (or platform.moonshot.cn)",
        batch="files",
        batch_price=0.6,
    ),
)
BY_KEY: Final = {provider.key: provider for provider in API_PROVIDERS}
API_KEYS: Final = tuple(provider.key for provider in API_PROVIDERS)
# The family of each subscription, for the same comparison.
FAMILY_OF: Final = {
    "claude": "claude",
    "chatgpt": "chatgpt",
    **{provider.key: provider.family for provider in API_PROVIDERS},
}
# How many models an account keeps prices for at most.
MAX_PRICED: Final = 2000
# What a model's name may look like, whoever the provider.
MODEL_NAME: Final = re.compile(r"^(?=.{1,100}$)~?[A-Za-z0-9][A-Za-z0-9._:/\[\]-]*$")
_KEY: Final = re.compile(r"^[\x21-\x7e]{16,400}$")


def is_api(provider: str) -> bool:
    return provider in BY_KEY


def check_key(provider: str, pasted: str) -> str:
    """The key as it is kept, or ``ValueError`` with a sentence for the administrator.
    The key is never part of that sentence."""
    kind = BY_KEY[provider]
    text = (pasted or "").strip()
    if not text:
        raise ValueError("Nothing was pasted")
    if text.startswith("sk-ant-oat"):
        raise ValueError(
            "That is a token of a Claude subscription, not an API key. It belongs to an "
            "account of the kind Claude."
        )
    if not _KEY.fullmatch(text):
        raise ValueError("That is not an API key: a key is one line without spaces")
    if kind.key_pattern is not None and not kind.key_pattern.fullmatch(text):
        raise ValueError(f"That is not an API key of {kind.name}: {kind.key_shape}")
    return text


def clean_prices(value: Any) -> dict[str, dict[str, float]]:
    """What a million tokens cost on each model, as the administrator wrote it: read,
    written and, when the provider prices it apart, read again from its cache."""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("Prices are given per model")
    cleaned: dict[str, dict[str, float]] = {}
    for model, prices in value.items():
        name = str(model).strip()
        if not MODEL_NAME.fullmatch(name):
            raise ValueError(f"{name[:40] or 'An empty name'} is not the name of a model")
        if not isinstance(prices, dict):
            raise ValueError(f"The prices of {name} are not given")
        entry: dict[str, float] = {}
        for part in ("input", "output", "cached", "cache_write", "request"):
            if prices.get(part) in (None, ""):
                continue
            try:
                amount = float(prices[part])
            except (TypeError, ValueError) as error:
                raise ValueError(f"The price of {name} is not a number") from error
            if not 0 <= amount <= 10_000:
                raise ValueError(f"The price of {name} is between 0 and 10,000 per million")
            entry[part] = amount
        if "input" not in entry or "output" not in entry:
            raise ValueError(f"{name} needs a price for what is read and for what is written")
        cleaned[name] = entry
    if len(cleaned) > MAX_PRICED:
        raise ValueError(f"Prices for at most {MAX_PRICED} models are kept")
    return cleaned


def cost_of(
    prices: dict[str, Any] | None,
    model: str,
    used: dict[str, Any],
    *,
    write: float = 1.0,
    part: float = 1.0,
    precision: int = 6,
) -> float | None:
    """What a call cost, when the account says what its model costs. Tokens read again
    from the provider's cache count at their own price, or else as tokens read. Of the
    tokens read, the ones written to the cache count ``write`` times; ``part`` is what
    is paid of the whole, for a question that was asked in a batch."""
    entry = (prices or {}).get(model)
    if not isinstance(entry, dict):
        return None
    try:
        read, written = float(entry["input"]), float(entry["output"])
        again = float(entry.get("cached", read))
        kept = int(used.get("cache_write", 0) or 0)
        total = int(used.get("input", 0) or 0) * read + int(used.get("output", 0) or 0) * written
        total += int(used.get("cached", 0) or 0) * again + kept * (
            float(entry.get("cache_write", read * write)) - read
        )
        total += float(entry.get("request", 0)) * int(used.get("responses", 1)) * 1_000_000
    except (KeyError, TypeError, ValueError):
        return None
    return round(total * part / 1_000_000, precision)
