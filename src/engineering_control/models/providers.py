"""The providers a model can be asked at over their API.

Beside the two subscriptions (a command-line client signed in to Claude or ChatGPT),
a model can be reached with an API key, paid by use. Six providers are known. Anthropic
is spoken to with its own library; the other five accept the same kind of request
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
    ),
    ApiProvider(
        key="openai",
        name="OpenAI API",
        family="chatgpt",
        style="openai",
        endpoints=(Endpoint("default", "OpenAI", "https://api.openai.com/v1"),),
        keys_at="platform.openai.com → API keys",
        key_pattern=re.compile(r"^sk-[A-Za-z0-9_-]{20,400}$"),
        key_shape="it starts with sk-",
        limit_name="max_completion_tokens",
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
# What a model's name may look like, whoever the provider.
MODEL_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,99}$")
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
        for part in ("input", "output", "cached"):
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
    if len(cleaned) > 60:
        raise ValueError("Prices for at most 60 models are kept")
    return cleaned


def cost_of(prices: dict[str, Any] | None, model: str, used: dict[str, int]) -> float | None:
    """What a call cost, when the account says what its model costs. Tokens read again
    from the provider's cache count at their own price, or else as tokens read."""
    entry = (prices or {}).get(model)
    if not isinstance(entry, dict):
        return None
    try:
        read, written = float(entry["input"]), float(entry["output"])
        again = float(entry.get("cached", read))
    except (KeyError, TypeError, ValueError):
        return None
    total = used.get("input", 0) * read + used.get("output", 0) * written
    total += used.get("cached", 0) * again
    return round(total / 1_000_000, 6)
