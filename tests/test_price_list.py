"""List prices: what the models of an account cost, read from public lists when the
Accounts page is refreshed, and kept without touching what was written by hand."""

# ruff: noqa: S105

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from engineering_control.models.pricelist import (
    LITELLM,
    MODELS_DEV,
    PriceList,
    PriceListError,
    PriceLists,
    renewed,
)
from engineering_control.models.providers import MAX_PRICED, clean_prices
from tests.test_ask_a_model import _console, _post
from tests.test_dashboard_accounts import FakeCluster
from tests.test_models_api import OTHER_KEY, Provider

FIRST: dict[str, Any] = {
    "anthropic": {
        "models": {
            "claude-opus-5-5": {"cost": {"input": 4, "output": 20, "cache_read": 0.2}},
            # A price by size of the prompt is not read: the list price is the first.
            "claude-haiku-5-5": {
                "cost": {
                    "input": 0.1,
                    "output": 0.5,
                    "cache_read": 0.01,
                    "tiers": [{"input": 0.5, "output": 2.5}],
                }
            },
            "no-cost": {"name": "Nothing said"},
            "half": {"cost": {"input": 1}},
        }
    },
    "alibaba": {"models": {"qwen-plus": {"cost": {"input": 0.4, "output": 1.2}}}},
    "alibaba-cn": {"models": {"qwen-plus": {"cost": {"input": 0.115, "output": 0.287}}}},
    "deepseek": {
        "models": {"model-a": {"cost": {"input": 0.3, "output": 1.2, "cache_read": 0.03}}}
    },
}
SECOND: dict[str, Any] = {
    "sample_spec": {"litellm_provider": "one of the providers"},
    "gpt-nano": {
        "litellm_provider": "openai",
        "input_cost_per_token": 2e-07,
        "output_cost_per_token": 1.25e-06,
        "cache_read_input_token_cost": 2e-08,
    },
    # The same name at another provider is not this provider's price.
    "claude-opus-5-5": {
        "litellm_provider": "bedrock",
        "input_cost_per_token": 9e-06,
        "output_cost_per_token": 9e-05,
    },
    "claude-sonnet-5-5": {
        "litellm_provider": "anthropic",
        "input_cost_per_token": 2e-06,
        "output_cost_per_token": 1e-05,
        "cache_read_input_token_cost": 1e-07,
    },
    "gemini/gemini-flash": {
        "litellm_provider": "gemini",
        "input_cost_per_token": 7.5e-07,
        "output_cost_per_token": 3.75e-06,
    },
    "deepseek/model-b": {
        "litellm_provider": "deepseek",
        "input_cost_per_token": 5e-07,
        "output_cost_per_token": 2e-06,
    },
    "deepseek/free": {"litellm_provider": "deepseek", "input_cost_per_token": "a lot"},
}


def test_a_price_is_read_from_the_first_list_that_has_it_for_the_providers_own_address() -> None:
    found = PriceList(models_dev=FIRST, litellm=SECOND, read=("models.dev", "LiteLLM"))
    assert found.price("anthropic", "default", "claude-opus-5-5") == {
        "input": 4.0,
        "output": 20.0,
        "cached": 0.2,
    }
    assert found.price("anthropic", None, "claude-haiku-5-5") == {
        "input": 0.1,
        "output": 0.5,
        "cached": 0.01,
    }
    # An address has its own prices: the international one and the Chinese one differ.
    assert found.price("qwen", "international", "qwen-plus") == {"input": 0.4, "output": 1.2}
    assert found.price("qwen", "china", "qwen-plus") == {"input": 0.115, "output": 0.287}
    # What the first list does not have is read from the second, per token there.
    assert found.price("openai", "default", "gpt-nano") == {
        "input": 0.2,
        "output": 1.25,
        "cached": 0.02,
    }
    assert found.price("anthropic", "default", "claude-sonnet-5-5") == {
        "input": 2.0,
        "output": 10.0,
        "cached": 0.1,
    }
    assert found.price("google", "default", "gemini-flash") == {"input": 0.75, "output": 3.75}
    # Nothing is made up: a model no list knows, half a price, a price that is no number.
    for provider, model in (
        ("anthropic", "claude-nothing"),
        ("anthropic", "no-cost"),
        ("anthropic", "half"),
        ("deepseek", "free"),
        ("claude", "claude-opus-5-5"),
        ("glm", "glm-5"),
    ):
        assert found.price(provider, "default", model) is None, model
    # With only the second list read, the second list's price of this provider.
    only = PriceList(litellm=SECOND, read=("LiteLLM",))
    assert only.price("anthropic", "default", "claude-opus-5-5") is None
    assert only.price("deepseek", "default", "model-b") == {"input": 0.5, "output": 2.0}


def test_list_prices_are_put_in_and_what_was_written_by_hand_stays() -> None:
    listed = {
        "a": {"input": 1.0, "output": 2.0},
        "b": {"input": 3.0, "output": 4.0, "cached": 0.3},
        "c": {"input": 5.0, "output": 6.0},
        "d": {"input": 9.0, "output": 9.0},
    }
    now = {
        "b": {"input": 3.0, "output": 4.0, "cached": 0.3},  # as the list has it
        "c": {"input": 4.5, "output": 6.0},  # the list's, since changed there
        "d": {"input": 7.0, "output": 7.0, "own": True},  # written by hand
        "gone": {"input": 1.0, "output": 1.0},  # a model the key no longer reaches
    }
    prices, done = renewed(now, ["a", "b", "c", "d", "e"], listed.get)
    assert done == {"priced": 3, "added": 1, "changed": 1, "yours": 1, "unlisted": 1}
    assert prices == {
        "a": {"input": 1.0, "output": 2.0},
        "b": {"input": 3.0, "output": 4.0, "cached": 0.3},
        "c": {"input": 5.0, "output": 6.0},
        "d": {"input": 7.0, "output": 7.0, "own": True},
        "gone": {"input": 1.0, "output": 1.0},
    }
    assert now["c"] == {"input": 4.5, "output": 6.0}, "what was given is not changed"
    # Read again, nothing changes.
    again, nothing = renewed(prices, ["a", "b", "c", "d", "e"], listed.get)
    assert again == prices and (nothing["added"], nothing["changed"]) == (0, 0)
    # No more models are priced than an account keeps.
    many = [f"m{number}" for number in range(MAX_PRICED + 20)]
    full, counted = renewed({}, many, lambda _model: {"input": 1.0, "output": 1.0})
    assert len(full) == MAX_PRICED == counted["added"]
    assert len(clean_prices(full)) == MAX_PRICED


class Lists(Provider):
    """A provider behind a stand-in, and the two public lists of prices."""

    def __init__(self) -> None:
        super().__init__()
        self.first: Any = FIRST
        self.second: Any = SECOND
        self.read: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in (MODELS_DEV, LITELLM):
            self.read.append(request.url.host)
            found = self.first if url == MODELS_DEV else self.second
            if found is None:
                return httpx.Response(503, text="down")
            return (
                httpx.Response(200, text=found)
                if isinstance(found, str)
                else httpx.Response(200, json=found)
            )
        return super().__call__(request)


@pytest.mark.asyncio
async def test_the_lists_are_read_once_in_a_while_and_one_of_them_is_enough() -> None:
    hub = Lists()
    http = httpx.AsyncClient(transport=httpx.MockTransport(hub))
    lists = PriceLists(lambda: http)
    found = await lists.read()
    assert found.read == ("models.dev", "LiteLLM")
    assert sorted(hub.read) == ["models.dev", "raw.githubusercontent.com"]
    # Asked again at once, what was read is used: nothing is fetched.
    assert await lists.read() is found and len(hub.read) == 2

    # One list down, or answering something that is no list: the other is enough.
    for broken in (None, "<html>not found</html>", {"error": "moved"}, {}):
        hub.first = broken
        one = await PriceLists(lambda: http).read()
        assert one.read == ("LiteLLM",), broken
        assert one.price("deepseek", "default", "model-b") == {"input": 0.5, "output": 2.0}
        assert one.price("deepseek", "default", "model-a") is None
    hub.first, hub.second = FIRST, None
    assert (await PriceLists(lambda: http).read()).read == ("models.dev",)
    # Neither: it is said, and nothing is made of it.
    hub.first = None
    with pytest.raises(PriceListError, match="Neither list of prices could be read"):
        await PriceLists(lambda: http).read()


def test_refreshing_the_accounts_page_reads_the_prices_and_keeps_what_was_written(
    tmp_path: Path,
) -> None:
    hub, fake = Lists(), FakeCluster()
    client = _console(tmp_path, hub, fake)

    def refresh() -> Any:
        csrf = client.get("/api/dashboard/session").json()["csrf_token"]
        return client.post("/api/dashboard/account-prices", json={"csrf_token": csrf})

    def prices(account: dict[str, Any]) -> dict[str, Any]:
        listing = client.get("/api/dashboard/accounts").json()["items"]
        found: dict[str, Any] = next(a for a in listing if a["id"] == account["id"])["prices"]
        return found

    try:
        account = _post(client, "", name="DeepSeek one", provider="deepseek").json()
        assert _post(client, f"/{account['id']}/sign-in", credential=OTHER_KEY).status_code == 200
        claude = _post(client, "", name="Claude one", provider="claude").json()
        assert prices(account) == {}

        # The key reaches model-a and model-b: one is in the first list, one in the second.
        listed = refresh()
        assert listed.status_code == 200, listed.text
        said = listed.json()
        assert said["lists"] == ["models.dev", "LiteLLM"]
        assert (said["priced"], said["changed"]) == (2, 2)
        assert said["accounts"] == [
            {
                "name": "DeepSeek one",
                "provider": "deepseek",
                "priced": 2,
                "added": 2,
                "changed": 0,
                "yours": 0,
                "unlisted": 0,
            }
        ], "a subscription has no prices"
        assert prices(account) == {
            "model-a": {"input": 0.3, "output": 1.2, "cached": 0.03},
            "model-b": {"input": 0.5, "output": 2.0},
        }
        assert OTHER_KEY not in listed.text
        # Pressed again, nothing changes and the lists are not fetched again.
        fetched = len(hub.read)
        assert refresh().json()["changed"] == 0 and len(hub.read) == fetched

        # A price changed by hand is the administrator's: it stays when the list moves.
        changed = _post(
            client,
            f"/{account['id']}/update",
            prices={
                "model-a": {"input": 0.25, "output": 1.0, "cached": 0.03},
                "model-b": {"input": 0.5, "output": 2.0},
                "model-own": {"input": 7, "output": 8},
            },
        )
        assert changed.json()["prices"] == {
            "model-a": {"input": 0.25, "output": 1.0, "cached": 0.03, "own": True},
            "model-b": {"input": 0.5, "output": 2.0},
            "model-own": {"input": 7.0, "output": 8.0, "own": True},
        }
        client.app.state.models.price_lists._kept = None  # type: ignore[attr-defined]
        hub.second = {
            **SECOND,
            "deepseek/model-b": {
                "litellm_provider": "deepseek",
                "input_cost_per_token": 6e-07,
                "output_cost_per_token": 2.4e-06,
            },
        }
        moved = refresh().json()
        assert moved["accounts"][0] == {
            "name": "DeepSeek one",
            "provider": "deepseek",
            "priced": 1,
            "added": 0,
            "changed": 1,
            "yours": 1,
            "unlisted": 0,
        }
        assert prices(account) == {
            "model-a": {"input": 0.25, "output": 1.0, "cached": 0.03, "own": True},
            "model-b": {"input": 0.6, "output": 2.4},
            "model-own": {"input": 7.0, "output": 8.0, "own": True},
        }

        # Its line deleted, the model has the list price again.
        _post(
            client,
            f"/{account['id']}/update",
            prices={"model-b": {"input": 0.6, "output": 2.4}},
        )
        assert refresh().json()["accounts"][0]["added"] == 1
        assert prices(account)["model-a"] == {"input": 0.3, "output": 1.2, "cached": 0.03}

        # Neither list can be read: it is said, and the prices are as they were.
        client.app.state.models.price_lists._kept = None  # type: ignore[attr-defined]
        before = prices(account)
        hub.first = hub.second = None
        down = refresh()
        assert down.status_code == 422 and "Neither list of prices" in down.json()["detail"]
        assert prices(account) == before
        listing = client.get("/api/dashboard/accounts").json()["items"]
        assert "prices" not in next(a for a in listing if a["id"] == claude["id"])
        # Without a session nothing is read or changed.
        client.cookies.clear()
        signed_out = client.post("/api/dashboard/account-prices", json={"csrf_token": "x" * 40})
        assert signed_out.status_code == 401
    finally:
        client.__exit__(None, None, None)
