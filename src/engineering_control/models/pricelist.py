"""What the providers' models cost, read from public lists.

No provider says over its API what its models cost. Two lists that are kept in public
do: models.dev, which tells a provider's addresses apart (a Chinese address has other
prices than the international one), and LiteLLM's list, which knows more names. The
first is asked for a model's price, then the second.

These are list prices, in US dollars per million tokens: what is read, what is written
and, where the provider prices it apart, what is read again from its cache. A price by
size of the prompt, by region or by contract is not in them: such a price is written by
hand on the account, and a price written by hand is never replaced from here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import structlog

from engineering_control.models.providers import MAX_PRICED

LOG = structlog.get_logger(__name__)
MODELS_DEV: Final = "https://models.dev/api.json"
LITELLM: Final = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
# Longer than either list is by far: more is not read.
MAX_BYTES: Final = 40_000_000
# For how long what was read is used again, so that pressing a button twice asks once.
FRESH_SECONDS: Final = 600.0
# Where a provider's address is found in models.dev: by provider and address.
IN_MODELS_DEV: Final = {
    ("anthropic", "default"): "anthropic",
    ("openai", "default"): "openai",
    ("google", "default"): "google",
    ("deepseek", "default"): "deepseek",
    ("qwen", "international"): "alibaba",
    ("qwen", "china"): "alibaba-cn",
    ("glm", "international"): "zai",
    ("glm", "china"): "zhipuai",
    ("kimi", "international"): "moonshotai",
    ("kimi", "china"): "moonshotai-cn",
}
# What LiteLLM writes before a model's name, and what it calls the provider.
IN_LITELLM: Final = {
    "anthropic": ("", "anthropic"),
    "openai": ("", "openai"),
    "google": ("gemini/", "gemini"),
    "deepseek": ("deepseek/", "deepseek"),
    "qwen": ("dashscope/", "dashscope"),
    "glm": ("zai/", "zai"),
    "kimi": ("moonshot/", "moonshot"),
}


class PriceListError(Exception):
    """Neither list could be read."""


def _amount(value: Any, times: float = 1.0) -> float | None:
    """A price per million tokens, or None when it is none."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    amount = round(float(value) * times, 6)
    return amount if 0 <= amount <= 10_000 else None


@dataclass(frozen=True)
class PriceList:
    """The two lists as they were read. ``read`` names the ones that could be."""

    models_dev: dict[str, Any] = field(default_factory=dict)
    litellm: dict[str, Any] = field(default_factory=dict)
    read: tuple[str, ...] = ()
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def price(self, provider: str, endpoint: str | None, model: str) -> dict[str, float] | None:
        """What a model costs at that provider's address, or None when neither list
        says."""
        listed = self.models_dev.get(IN_MODELS_DEV.get((provider, endpoint or "default"), ""))
        models = listed.get("models") if isinstance(listed, dict) else None
        entry = models.get(model) if isinstance(models, dict) else None
        cost = entry.get("cost") if isinstance(entry, dict) else None
        if isinstance(cost, dict):
            found = self._of(cost.get("input"), cost.get("output"), cost.get("cache_read"), 1.0)
            if found is not None:
                return found
        prefix, name = IN_LITELLM.get(provider, ("", ""))
        entry = self.litellm.get(f"{prefix}{model}") if name else None
        if isinstance(entry, dict) and entry.get("litellm_provider") == name:
            return self._of(
                entry.get("input_cost_per_token"),
                entry.get("output_cost_per_token"),
                entry.get("cache_read_input_token_cost"),
                1_000_000.0,
            )
        return None

    @staticmethod
    def _of(read: Any, written: Any, again: Any, times: float) -> dict[str, float] | None:
        first, second = _amount(read, times), _amount(written, times)
        if first is None or second is None:
            return None
        found = {"input": first, "output": second}
        third = _amount(again, times)
        if third is not None:
            found["cached"] = third
        return found


def renewed(
    now: dict[str, Any], models: list[str], price_of: Callable[[str], dict[str, float] | None]
) -> tuple[dict[str, Any], dict[str, int]]:
    """An account's prices with the list's put in: for every model its key reaches that
    the list has a price for. A price the administrator wrote is left as it is, and so
    is one of a model the list does not know. Returns the prices and what was done."""
    prices: dict[str, Any] = {name: dict(entry) for name, entry in now.items()}
    done = {"priced": 0, "added": 0, "changed": 0, "yours": 0, "unlisted": 0}
    for model in models:
        before = prices.get(model)
        if isinstance(before, dict) and before.get("own"):
            done["yours"] += 1
            continue
        found = price_of(model)
        if found is None:
            done["unlisted"] += 1
            continue
        if before is None and len(prices) >= MAX_PRICED:
            continue
        done["priced"] += 1
        if before is None:
            done["added"] += 1
        elif any(before.get(part) != found.get(part) for part in ("input", "output", "cached")):
            done["changed"] += 1
        prices[model] = found
    return prices, done


class PriceLists:
    """Reads the lists, and keeps what it read for a while."""

    def __init__(self, http: Callable[[], httpx.AsyncClient]) -> None:
        self._http = http
        self._kept: PriceList | None = None

    async def _one(self, name: str, url: str) -> dict[str, Any] | None:
        try:
            response = await self._http().get(url, timeout=60, follow_redirects=True)
        except httpx.HTTPError as error:
            LOG.warning("price_list_not_read", list=name, error=type(error).__name__)
            return None
        if response.status_code != 200 or len(response.content) > MAX_BYTES:
            LOG.warning("price_list_not_read", list=name, status=response.status_code)
            return None
        try:
            found = await asyncio.to_thread(json.loads, response.content)
        except ValueError:
            LOG.warning("price_list_not_read", list=name, error="not JSON")
            return None
        return found if isinstance(found, dict) and found else None

    async def read(self) -> PriceList:
        """The lists as they are now, or as they were read within the last minutes.
        Raises ``PriceListError`` when neither can be read."""
        now = datetime.now(UTC)
        if self._kept is not None and (now - self._kept.at).total_seconds() < FRESH_SECONDS:
            return self._kept
        first, second = await asyncio.gather(
            self._one("models.dev", MODELS_DEV), self._one("LiteLLM", LITELLM)
        )
        # What does not look like the list it should be is not one.
        if first is not None and not any(
            isinstance(entry, dict) and "models" in entry for entry in first.values()
        ):
            first = None
        if second is not None and not any(
            isinstance(entry, dict) and "litellm_provider" in entry for entry in second.values()
        ):
            second = None
        if first is None and second is None:
            raise PriceListError(
                "Neither list of prices could be read (models.dev, LiteLLM on GitHub). "
                "The prices are as they were."
            )
        self._kept = PriceList(
            models_dev=first or {},
            litellm=second or {},
            read=tuple(
                name
                for name, found in (("models.dev", first), ("LiteLLM", second))
                if found is not None
            ),
            at=now,
        )
        return self._kept
