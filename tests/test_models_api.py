"""Asking a model over a provider's API: the six providers, how a key is kept, how each
provider is spoken to, and what happens when one refuses, is busy or answers badly."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import httpx2
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.accounts import Accounts
from engineering_control.db.base import Base
from engineering_control.db.models import ProviderAccount
from engineering_control.models import anthropic_api, openai_style, shape
from engineering_control.models.providers import (
    API_KEYS,
    BY_KEY,
    FAMILY_OF,
    check_key,
    clean_prices,
    cost_of,
    host_of,
)
from engineering_control.models.sealing import SealError, read_sealing_key, seal, unseal
from engineering_control.models.service import ModelError, Models, Question
from engineering_control.models.wire import WireError

SEALING = b"s" * 48
ANTHROPIC_KEY = "sk-ant-api03-" + "aB3_-" * 20  # noqa: S105 - the shape, not a key
OTHER_KEY = "sk-" + "k9" * 24  # noqa: S105 - the shape, not a key
SHAPE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "summary"],
    "properties": {
        "decision": {"type": "string", "enum": ["PASS", "REQUEST_CHANGES"]},
        "summary": {"type": "string"},
    },
}
GOOD = {"decision": "PASS", "summary": "Fine."}


# --- the providers ---------------------------------------------------------------------------


def test_seven_providers_each_at_addresses_of_its_own() -> None:
    assert API_KEYS == ("anthropic", "openai", "google", "deepseek", "qwen", "glm", "kimi")
    hosts = {key: [host_of(e.url) for e in BY_KEY[key].endpoints] for key in API_KEYS}
    assert hosts == {
        "anthropic": ["api.anthropic.com"],
        "openai": ["api.openai.com"],
        "google": ["generativelanguage.googleapis.com"],
        "deepseek": ["api.deepseek.com"],
        "qwen": ["dashscope-intl.aliyuncs.com", "dashscope.aliyuncs.com"],
        "glm": ["api.z.ai", "open.bigmodel.cn"],
        "kimi": ["api.moonshot.ai", "api.moonshot.cn"],
    }
    assert all(e.url.startswith("https://") for key in API_KEYS for e in BY_KEY[key].endpoints)
    # Only Anthropic is spoken to with its own library.
    assert [key for key in API_KEYS if BY_KEY[key].style == "anthropic"] == ["anthropic"]
    # An address that is not listed is the provider's first, never a typed one.
    assert BY_KEY["kimi"].endpoint("china").url == "https://api.moonshot.cn/v1"
    assert BY_KEY["kimi"].endpoint("https://elsewhere.example").key == "international"
    # An API and the subscription of the same company are one family.
    assert FAMILY_OF["anthropic"] == FAMILY_OF["claude"] != FAMILY_OF["openai"]
    assert FAMILY_OF["openai"] == FAMILY_OF["chatgpt"]
    assert len({FAMILY_OF[key] for key in ("google", "deepseek", "qwen", "glm", "kimi")}) == 5
    # Google has no subscription here: its models are a family of their own.
    assert FAMILY_OF["google"] not in (FAMILY_OF["claude"], FAMILY_OF["chatgpt"])
    # OpenAI and Google hold a model to the shape of its answer and take a level of
    # reasoning; Google names its models with a word before them.
    assert [key for key in API_KEYS if BY_KEY[key].shape == "schema"] == ["openai", "google"]
    assert [key for key in API_KEYS if BY_KEY[key].effort] == ["openai", "google"]
    google = BY_KEY["google"]
    assert google.endpoints[0].url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert (google.model_prefix, google.limit_name, google.key_pattern) == (
        "models/",
        "max_tokens",
        None,
    )


def test_a_key_is_checked_for_what_it_is_and_never_repeated() -> None:
    assert check_key("anthropic", f"  {ANTHROPIC_KEY}\n") == ANTHROPIC_KEY
    assert check_key("deepseek", OTHER_KEY) == OTHER_KEY
    for provider, pasted, why in (
        ("deepseek", "", "Nothing was pasted"),
        ("anthropic", "sk-ant-oat01-" + "aB3_-" * 20, "token of a Claude subscription"),
        ("kimi", "sk-ant-oat01-" + "aB3_-" * 20, "token of a Claude subscription"),
        ("anthropic", OTHER_KEY, "not an API key of Anthropic API: it starts with sk-ant-api"),
        ("openai", "key-" + "k" * 40, "not an API key of OpenAI API"),
        ("glm", "two words " + "k" * 30, "one line without spaces"),
        ("qwen", "short", "one line without spaces"),
    ):
        with pytest.raises(ValueError, match=why) as refusal:
            check_key(provider, pasted)
        assert pasted.strip() == "" or pasted.strip() not in str(refusal.value)


def test_what_a_call_cost_is_known_when_the_account_says_what_its_model_costs() -> None:
    prices = clean_prices({"deepseek-chat": {"input": "0.5", "output": 2, "cached": 0.1}})
    assert prices == {"deepseek-chat": {"input": 0.5, "output": 2.0, "cached": 0.1}}
    used = {"input": 1_000_000, "output": 500_000, "cached": 2_000_000}
    assert cost_of(prices, "deepseek-chat", used) == 0.5 + 1.0 + 0.2
    # Without a price for what is read again, it counts as read.
    assert cost_of({"m": {"input": 1, "output": 1}}, "m", used) == 1 + 0.5 + 2
    assert cost_of(prices, "another-model", used) is None and cost_of(None, "m", used) is None
    assert clean_prices(None) == {} and clean_prices({}) == {}
    for wrong, why in (
        ({"m": {"input": 1}}, "needs a price"),
        ({"m": {"input": -1, "output": 1}}, "between 0 and 10,000"),
        ({"m": {"input": "a lot", "output": 1}}, "not a number"),
        ({"two words": {"input": 1, "output": 1}}, "not the name of a model"),
        ({"m": 3}, "are not given"),
        ([1], "per model"),
    ):
        with pytest.raises(ValueError, match=why):
            clean_prices(wrong)


# --- how a key is kept -----------------------------------------------------------------------


def test_a_sealed_key_opens_with_the_platforms_key_for_its_own_account(tmp_path: Path) -> None:
    account = uuid4()
    sealed = seal(SEALING, account, OTHER_KEY)
    assert OTHER_KEY not in sealed and sealed != seal(SEALING, account, OTHER_KEY)
    assert unseal(SEALING, account, sealed) == OTHER_KEY
    for key, whose, text in (
        (b"x" * 48, account, sealed),  # another installation's key
        (SEALING, uuid4(), sealed),  # put into another account's record
        (SEALING, account, sealed[:-4] + "AAAA"),  # changed
        (SEALING, account, "not sealed at all"),
    ):
        with pytest.raises(SealError, match="Enter the API key again"):
            unseal(key, whose, text)
    file = tmp_path / "model-keys.key"
    assert read_sealing_key(file) is None and read_sealing_key(None) is None
    file.write_bytes(b"too short")
    assert read_sealing_key(file) is None
    file.write_bytes(SEALING + b"\n")
    assert read_sealing_key(file) == SEALING


# --- the shape of an answer ------------------------------------------------------------------


def test_an_answer_is_read_out_of_what_was_written_and_checked() -> None:
    text = json.dumps(GOOD)
    assert shape.read_object(text) == GOOD
    assert shape.read_object(f"```json\n{text}\n```") == GOOD
    assert shape.read_object(f"Here is my answer:\n{text}\nI hope it helps.") == GOOD
    assert shape.read_object("PASS") is None and shape.read_object("[1, 2]") is None
    assert shape.read_object("") is None
    assert shape.problem(SHAPE, GOOD) is None
    for wrong, why in (
        ({"decision": "PASS"}, "the answer has no summary"),
        ({**GOOD, "decision": "MAYBE"}, "decision is not one of PASS, REQUEST_CHANGES"),
        ({**GOOD, "summary": 3}, "summary is not of the kind string"),
        ({**GOOD, "extra": 1}, "the answer has extra, which is not asked for"),
        ([GOOD], "the answer is not of the kind object"),
    ):
        assert shape.problem(SHAPE, wrong) == why
    listed = {"type": "array", "items": {"type": "integer"}}
    assert shape.problem(listed, [1, 2]) is None
    assert shape.problem(listed, [1, True]) == "entry 2 of the answer is not of the kind integer"


# --- chat completions ------------------------------------------------------------------------


class Provider:
    """A provider that speaks chat completions: what it was asked, and what it says."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str, dict[str, Any] | None, str | None]] = []
        self.answers: list[httpx.Response] = []
        self.models = ["model-b", "model-a"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.asked.append(
            (request.method, str(request.url), body, request.headers.get("authorization"))
        )
        if request.url.path.endswith("/models"):
            if self.answers:
                return self.answers.pop(0)
            return httpx.Response(200, json={"data": [{"id": name} for name in self.models]})
        if self.answers:
            return self.answers.pop(0)
        return said(GOOD)


def said(
    answer: Any, *, reason: str = "stop", usage: dict[str, Any] | None = None
) -> httpx.Response:
    content = answer if isinstance(answer, str) else json.dumps(answer)
    return httpx.Response(
        200,
        json={
            "model": "model-a-0915",
            "choices": [
                {"finish_reason": reason, "message": {"role": "assistant", "content": content}}
            ],
            "usage": usage or {"prompt_tokens": 1200, "completion_tokens": 80},
        },
    )


@pytest.mark.asyncio
async def test_a_chat_completion_asks_for_a_json_object_and_reads_what_it_cost() -> None:
    provider = Provider()
    provider.answers = [
        said(
            GOOD,
            usage={"prompt_tokens": 1200, "completion_tokens": 80, "prompt_cache_hit_tokens": 200},
        )
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        common: dict[str, Any] = {
            "http": http,
            "key": OTHER_KEY,
            "model": "model-a",
            "system": "The rules.",
            "prompt": "The question.",
            "schema": SHAPE,
            "max_tokens": 4000,
            "seconds": 60,
        }
        wire = await openai_style.complete(
            name="DeepSeek", base_url="https://api.deepseek.com", **common
        )
        assert json.loads(wire.text) == GOOD and wire.model == "model-a-0915"
        # What was read again from the cache is counted apart from what was read new.
        assert (wire.read, wire.written, wire.cached) == (1000, 80, 200)
        method, url, body, authorization = provider.asked[-1]
        assert (method, url) == ("POST", "https://api.deepseek.com/chat/completions")
        assert authorization == f"Bearer {OTHER_KEY}"
        assert body == {
            "model": "model-a",
            "messages": [
                {"role": "system", "content": "The rules."},
                {"role": "user", "content": "The question."},
            ],
            "max_tokens": 4000,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        assert "tools" not in body and "temperature" not in body

        # OpenAI is held to the shape itself, and its limit has another name.
        provider.answers = [
            said(
                GOOD,
                usage={
                    "prompt_tokens": 500,
                    "completion_tokens": 40,
                    "prompt_tokens_details": {"cached_tokens": 100},
                },
            )
        ]
        wire = await openai_style.complete(
            name="OpenAI API",
            base_url="https://api.openai.com/v1",
            limit_name="max_completion_tokens",
            strict=True,
            effort="high",
            **common,
        )
        body = provider.asked[-1][2]
        assert (
            body is not None and body["max_completion_tokens"] == 4000 and "max_tokens" not in body
        )
        assert body["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "answer", "strict": True, "schema": SHAPE},
        }
        assert body["reasoning_effort"] == "high" and (wire.read, wire.cached) == (400, 100)

        for response, kind, words in (
            (
                httpx.Response(401, json={"error": {"message": "Incorrect API key"}}),
                "refused",
                "does not accept the key: Incorrect API key",
            ),
            (httpx.Response(403, json={"message": "forbidden here"}), "refused", "forbidden here"),
            (
                httpx.Response(
                    429, headers={"retry-after": "7"}, json={"error": {"message": "slow down"}}
                ),
                "busy",
                "limit is reached: slow down",
            ),
            (httpx.Response(503, text="upstream gone"), "busy", "answered 503: upstream gone"),
            (
                httpx.Response(400, json={"error": {"message": "Model Not Exist"}}),
                "invalid",
                "rejected the request (400): Model Not Exist",
            ),
            (said(GOOD, reason="length"), "cut", "longer than the 4,000 tokens"),
            (said("", reason="content_filter"), "declined", "declined to answer"),
            (httpx.Response(200, json={"unexpected": True}), "busy", "not an answer"),
        ):
            provider.answers = [response]
            with pytest.raises(
                WireError, match=words.replace("(", r"\(").replace(")", r"\)")
            ) as failed:
                await openid(common)
            assert failed.value.kind == kind, words
            assert OTHER_KEY not in failed.value.detail
        assert failed.value.retry_after is None
        provider.answers = [httpx.Response(429, headers={"retry-after": "7"}, json={})]
        with pytest.raises(WireError) as limited:
            await openid(common)
        assert limited.value.retry_after == 7.0

        assert await openai_style.models(
            http=http, name="Kimi", key=OTHER_KEY, base_url="https://api.moonshot.ai/v1"
        ) == ["model-a", "model-b"]
        assert provider.asked[-1][:2] == ("GET", "https://api.moonshot.ai/v1/models")

        # Google: asked like OpenAI at its own address, the shape held by the provider.
        google = BY_KEY["google"]
        gemini: dict[str, Any] = {
            **common,
            "name": google.name,
            "base_url": google.endpoints[0].url,
            "model": "gemini-pro",
            "limit_name": google.limit_name,
            "strict": True,
            "model_prefix": google.model_prefix,
        }
        answer = said(
            GOOD,
            # What the model thought is in the total and not in what it wrote.
            usage={"prompt_tokens": 900, "completion_tokens": 60, "total_tokens": 1400},
        )
        provider.answers = [
            httpx.Response(200, json={**answer.json(), "model": "models/gemini-pro"})
        ]
        wire = await openai_style.complete(effort="low", **gemini)
        method, url, body, authorization = provider.asked[-1]
        assert url == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        assert authorization == f"Bearer {OTHER_KEY}" and body is not None
        assert body["model"] == "gemini-pro" and body["max_tokens"] == 4000
        assert body["response_format"]["json_schema"]["schema"] == SHAPE
        assert body["reasoning_effort"] == "low"
        # The model is named as the administrator writes it, and thinking is paid as written.
        assert wire.model == "gemini-pro" and (wire.read, wire.written) == (900, 500)
        # A level the provider is not told (the model's own) is left out.
        await openai_style.complete(effort="default", **gemini)
        assert "reasoning_effort" not in (provider.asked[-1][2] or {})

        # A shape the provider will not take as written is asked for as a JSON object,
        # once; a request that is wrong for another reason is rejected again and said.
        before = len(provider.asked)
        provider.answers = [
            httpx.Response(400, json={"error": {"message": "Unknown name in schema"}}),
            said(GOOD),
        ]
        wire = await openai_style.complete(**gemini)
        assert json.loads(wire.text) == GOOD and len(provider.asked) == before + 2
        first, second = provider.asked[-2][2] or {}, provider.asked[-1][2] or {}
        assert first["response_format"]["type"] == "json_schema"
        assert second["response_format"] == {"type": "json_object"}
        assert {k: v for k, v in first.items() if k != "response_format"} == {
            k: v for k, v in second.items() if k != "response_format"
        }
        provider.answers = [
            httpx.Response(404, json={"error": {"message": "models/gemini-none is not found"}}),
            httpx.Response(404, json={"error": {"message": "models/gemini-none is not found"}}),
        ]
        with pytest.raises(WireError, match="gemini-none is not found") as wrong:
            await openai_style.complete(**gemini)
        assert wrong.value.kind == "invalid" and provider.answers == []
        # A refused key or a busy provider is not asked again here.
        before = len(provider.asked)
        provider.answers = [httpx.Response(401, json={"error": {"message": "API key not valid"}})]
        with pytest.raises(WireError, match="Google Gemini API does not accept the key"):
            await openai_style.complete(**gemini)
        assert len(provider.asked) == before + 1

        # Its list names models with a word before them, which is taken off.
        provider.models = ["models/gemini-pro", "models/gemini-flash", "models/embedding-1"]
        assert await openai_style.models(
            http=http,
            name=google.name,
            key=OTHER_KEY,
            base_url=google.endpoints[0].url,
            model_prefix=google.model_prefix,
        ) == ["embedding-1", "gemini-flash", "gemini-pro"]
        assert (
            provider.asked[-1][1]
            == "https://generativelanguage.googleapis.com/v1beta/openai/models"
        )
        provider.models = ["model-b", "model-a"]

    async def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no route", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unreachable)) as http:
        with pytest.raises(WireError, match="DeepSeek did not answer: ConnectTimeout") as silent:
            await openai_style.complete(
                name="DeepSeek", base_url="https://api.deepseek.com", **{**common, "http": http}
            )
        assert silent.value.kind == "busy"


async def openid(common: dict[str, Any]) -> Any:
    return await openai_style.complete(
        name="DeepSeek", base_url="https://api.deepseek.com", **common
    )


# --- Anthropic -------------------------------------------------------------------------------


class Anthropic:
    """Anthropic's API as the library sees it: a stream of events for a message."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str, dict[str, Any] | None, dict[str, str]]] = []
        self.answers: list[httpx2.Response] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content) if request.content else None
        self.asked.append((request.method, str(request.url), body, dict(request.headers)))
        if self.answers:
            return self.answers.pop(0)
        if request.url.path.endswith("/models"):
            return httpx2.Response(
                200,
                json={
                    "data": [
                        {
                            "type": "model",
                            "id": name,
                            "display_name": name,
                            "created_at": "2026-01-01T00:00:00Z",
                        }
                        for name in ("claude-sonnet-5-5", "claude-opus-5-5")
                    ],
                    "has_more": False,
                    "first_id": "claude-sonnet-5-5",
                    "last_id": "claude-opus-5-5",
                },
            )
        return streamed(json.dumps(GOOD))

    def factory(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self))


def streamed(
    text: str, *, stop: str = "end_turn", model: str = "claude-opus-5-5"
) -> httpx2.Response:
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 900,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 300,
                },
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": 60},
        },
        {"type": "message_stop"},
    ]
    body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=body.encode()
    )


def refusal(status: int, kind: str, message: str, **headers: str) -> httpx2.Response:
    return httpx2.Response(
        status, headers=headers, json={"type": "error", "error": {"type": kind, "message": message}}
    )


@pytest.mark.asyncio
async def test_a_claude_model_is_asked_through_anthropics_library_in_a_fixed_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anthropic_api, "RETRIES", 0)
    hub = Anthropic()
    common: dict[str, Any] = {
        "key": ANTHROPIC_KEY,
        "base_url": "https://api.anthropic.com",
        "system": "The rules.",
        "prompt": "The question.",
        "schema": SHAPE,
        "max_tokens": 4000,
        "seconds": 60,
        "http": hub.factory,
    }
    wire = await anthropic_api.complete(model="claude-opus-5-5", effort="high", **common)
    assert json.loads(wire.text) == GOOD and wire.model == "claude-opus-5-5"
    # What was written to the cache was read new; what was read from it is counted apart.
    assert (wire.read, wire.written, wire.cached) == (1000, 60, 300)
    method, url, body, headers = hub.asked[-1]
    assert method == "POST" and url.startswith("https://api.anthropic.com/v1/messages")
    assert headers["authorization"] == f"Bearer {ANTHROPIC_KEY}"
    assert body is not None and body["stream"] is True
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] == 4000
    assert body["system"] == "The rules."
    assert body["messages"] == [{"role": "user", "content": "The question."}]
    # The shape is given to the API itself, and how hard the model reasons beside it.
    assert body["output_config"] == {
        "format": {"type": "json_schema", "schema": SHAPE},
        "effort": "high",
    }
    # Nothing that a current model rejects is sent.
    assert not {"temperature", "top_p", "thinking", "tools", "tool_choice"} & set(body)
    # A model that can decline for safety reasons is asked with the API's own fallback.
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in headers["anthropic-beta"]

    snapshots = []

    async def observe(text: str) -> None:
        snapshots.append(text)

    for model in ("claude-opus-5-5", "claude-haiku-5-5"):
        snapshots.clear()
        reply = await anthropic_api.complete(model=model, on_text=observe, **common)
        assert snapshots == ["", reply.text] and json.loads(reply.text) == GOOD
        assert reply.written == 60

    await anthropic_api.complete(model="claude-haiku-5-5", **common)
    _method, url, body, headers = hub.asked[-1]
    assert body is not None and "fallbacks" not in body and "anthropic-beta" not in headers
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": SHAPE}}

    for response, kind, words in (
        (
            refusal(401, "authentication_error", "invalid x-api-key"),
            "refused",
            "does not accept the key",
        ),
        (refusal(403, "permission_error", "not allowed"), "refused", "does not accept the key"),
        (
            refusal(429, "rate_limit_error", "slow down", **{"retry-after": "9"}),
            "busy",
            "limit is reached",
        ),
        (refusal(529, "overloaded_error", "overloaded"), "busy", "answered 529"),
        (
            refusal(400, "invalid_request_error", "max_tokens too large"),
            "invalid",
            r"rejected the request \(400\)",
        ),
        (
            refusal(404, "not_found_error", "model: claude-nothing"),
            "invalid",
            r"rejected the request \(404\)",
        ),
        (streamed("", stop="refusal"), "declined", "declined to answer"),
        (streamed('{"decision": "PA', stop="max_tokens"), "cut", "longer than the 4,000 tokens"),
    ):
        hub.answers = [response]
        with pytest.raises(WireError, match=words) as failed:
            await anthropic_api.complete(model="claude-haiku-5-5", **common)
        assert failed.value.kind == kind, words
        assert ANTHROPIC_KEY not in failed.value.detail
    hub.answers = [refusal(429, "rate_limit_error", "slow down", **{"retry-after": "9"})]
    with pytest.raises(WireError) as limited:
        await anthropic_api.complete(model="claude-haiku-5-5", **common)
    assert limited.value.retry_after == 9.0

    listed = await anthropic_api.models(
        key=ANTHROPIC_KEY, base_url="https://api.anthropic.com", http=hub.factory
    )
    assert listed == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert hub.asked[-1][1].startswith("https://api.anthropic.com/v1/models")


# --- the service -----------------------------------------------------------------------------


class Asked:
    """A database with accounts, the providers behind stand-ins, and no waiting."""

    def __init__(self, tmp_path: Path) -> None:
        self.engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'models.db'}")
        Base.metadata.create_all(self.engine)
        self.sessions: sessionmaker[Session] = sessionmaker(self.engine, expire_on_commit=False)
        self.accounts = Accounts(self.sessions)
        self.provider = Provider()
        self.anthropic = Anthropic()
        self.waited: list[float] = []
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.provider))

        async def sleep(seconds: float) -> None:
            self.waited.append(seconds)

        self.models = Models(
            self.sessions,
            SEALING,
            http=self.http,
            anthropic_http=self.anthropic.factory,
            sleep=sleep,
        )

    def account(self, name: str, provider: str, key: str = OTHER_KEY, **more: Any) -> UUID:
        account_id = UUID(self.accounts.create(name=name, provider=provider, **more)["id"])
        self.accounts.key_stored(account_id, seal(SEALING, account_id, key))
        return account_id

    def state(self, account_id: UUID) -> dict[str, Any]:
        return self.accounts.get(account_id)


def question(*choices: tuple[str, str]) -> Question:
    return Question(
        choices=choices or (("deepseek", "model-a"),),
        system="The rules.",
        prompt="The question.",
        schema=SHAPE,
        max_tokens=2000,
        seconds=30,
    )


@pytest.fixture
def asked(tmp_path: Path) -> Any:
    world = Asked(tmp_path)
    yield world
    world.engine.dispose()


@pytest.mark.asyncio
async def test_a_question_is_asked_with_the_account_unused_longest_and_costed(asked: Asked) -> None:
    first = asked.account("DeepSeek one", "deepseek")
    second = asked.account("DeepSeek two", "deepseek", key="sk-" + "z8" * 24)
    asked.accounts.update(first, prices={"model-a-0915": {"input": 1, "output": 4}})
    reply = await asked.models.ask(question())
    assert reply.data == GOOD and (reply.provider, reply.model) == ("deepseek", "model-a-0915")
    assert reply.account == "DeepSeek one" and reply.asked == 1
    assert reply.usage == {
        "tokens": 1280,
        "input": 1200,
        "output": 80,
        "cached": 0,
        "provider": "deepseek",
        "model": "model-a-0915",
        "account": "DeepSeek one",
        "account_id": str(first),
    }, "the price is written for the name asked for, not for what the provider calls it"
    asked.accounts.update(first, prices={"model-a": {"input": 1, "output": 4}})
    assert asked.state(first)["state"] == "ready" and asked.state(first)["last_used_at"]
    # The shape is stated in the question itself, whatever the provider promises.
    sent = asked.provider.asked[-1][2]
    assert sent is not None and sent["messages"][1]["content"].startswith(
        "The question.\n\nAnswer with one JSON object"
    )
    assert '"REQUEST_CHANGES"' in sent["messages"][1]["content"]
    # The next question goes to the other account, with its own key.
    again = await asked.models.ask(question())
    assert again.account == "DeepSeek two"
    assert asked.provider.asked[-1][3] == "Bearer sk-" + "z8" * 24
    third = await asked.models.ask(question())
    assert third.account == "DeepSeek one" and third.usage["cost"] == round((1200 + 320) / 1e6, 6)
    assert asked.state(second)["state"] == "ready"
    # The key is nowhere in what an account says about itself.
    assert OTHER_KEY not in json.dumps(asked.state(first))
    await asked.http.aclose()


@pytest.mark.asyncio
async def test_an_answer_of_another_shape_is_asked_for_once_more(asked: Asked) -> None:
    account = asked.account("Kimi one", "kimi", endpoint="china")
    asked.provider.answers = [said("I think it passes."), said(GOOD)]
    reply = await asked.models.ask(question(("kimi", "model-a")))
    assert reply.asked == 2 and reply.data == GOOD
    assert reply.usage["input"] == 2400, "both questions are counted"
    first, second = (entry[2] for entry in asked.provider.asked[-2:])
    assert asked.provider.asked[-1][1] == "https://api.moonshot.cn/v1/chat/completions"
    assert first is not None and second is not None
    assert (
        "An earlier answer could not be used: it was not a JSON object."
        in second["messages"][1]["content"]
    )
    assert len(second["messages"]) == 2, "the first answer is not sent back"

    asked.provider.answers = [said({"decision": "PASS"}), said({"decision": "PASS", "summary": 1})]
    with pytest.raises(
        ModelError, match="gave no answer of the asked shape twice: summary is not"
    ) as failed:
        await asked.models.ask(question(("kimi", "model-a")))
    assert (failed.value.label, failed.value.later) == ("no_answer", False)
    metered = failed.value.metered
    assert metered and metered.asked == 2 and metered.usage["tokens"] > 0
    assert metered.text == "" and metered.data == {}
    assert asked.state(account)["state"] == "ready", "the account is not at fault"
    await asked.http.aclose()


@pytest.mark.asyncio
async def test_a_refused_key_a_busy_provider_and_a_wrong_request_are_told_apart(
    asked: Asked,
) -> None:
    refused = asked.account("GLM one", "glm")
    good = asked.account("GLM two", "glm")
    # The first account's key is refused: the account says so, and the second is asked.
    asked.provider.answers = [httpx.Response(401, json={"error": {"message": "invalid api key"}})]
    reply = await asked.models.ask(question(("glm", "model-a")))
    assert reply.account == "GLM two"
    assert asked.state(refused)["state"] == "sign_in_required"
    assert "does not accept the key" in asked.state(refused)["state_reason"]
    assert [entry["account"] for entry in asked.accounts.inbox()] == ["GLM one"]

    # A busy provider is waited for and asked again, twice; then the account rests and
    # the second provider of the question is asked.
    asked.account("DeepSeek one", "deepseek")
    asked.provider.answers = [
        httpx.Response(
            429, headers={"retry-after": "40"}, json={"error": {"message": "rate limit"}}
        ),
        httpx.Response(503, text="busy"),
        httpx.Response(503, text="busy"),
    ]
    reply = await asked.models.ask(question(("glm", "model-a"), ("deepseek", "model-b")))
    assert (reply.provider, reply.account) == ("deepseek", "DeepSeek one")
    assert asked.waited == [40.0, 15.0], "as long as the provider says, within a minute"
    resting = asked.state(good)
    assert resting["state"] == "limited" and resting["limited_until"]
    assert "answered 503" in resting["state_reason"]

    # With every account resting or refused there is nothing to ask right now.
    with pytest.raises(ModelError, match="Waiting for an account of GLM \\(model-a\\)") as waiting:
        await asked.models.ask(question(("glm", "model-a")))
    assert (waiting.value.label, waiting.value.later) == ("no_account", True)
    asked.provider.answers = [httpx.Response(503, text="busy")] * 3
    with pytest.raises(ModelError, match="answered 503") as busy:
        await asked.models.ask(question(("deepseek", "model-a")))
    assert (busy.value.label, busy.value.later) == ("busy", True)

    # A request the provider rejects is the question's fault, not the account's.
    other = asked.account("Qwen one", "qwen")
    asked.provider.answers = [httpx.Response(404, json={"error": {"message": "model not found"}})]
    with pytest.raises(
        ModelError, match=r"Qwen rejected the request \(404\): model not found"
    ) as wrong:
        await asked.models.ask(question(("qwen", "no-such-model")))
    assert (wrong.value.label, wrong.value.later) == ("invalid", False)
    assert asked.state(other)["state"] == "ready"
    assert asked.provider.asked[-1][1].startswith(
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/"
    )

    # A paused account is not asked; an installation without the sealing key asks nothing.
    asked.accounts.update(other, paused=True)
    with pytest.raises(ModelError) as paused:
        await asked.models.ask(question(("qwen", "model-a")))
    assert paused.value.later is True
    keyless = Models(asked.sessions, None, http=asked.http)
    with pytest.raises(ModelError, match="no key to open API keys with") as none:
        await keyless.ask(question(("qwen", "model-a")))
    assert none.value.later is False
    await asked.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 400, 401, 429])
async def test_single_request_uses_no_format_retry_or_alternate_account(
    asked: Asked, status: int
) -> None:
    asked.account("OpenAI one", "openai")
    asked.account("OpenAI two", "openai")
    response = (
        said({"wrong": "format"})
        if status == 200
        else httpx.Response(status, json={"error": {"message": "unavailable"}})
    )
    asked.provider.answers = [response]
    with pytest.raises(ModelError):
        await asked.models.ask(question(("openai", "model-a")), retry=False)
    assert len(asked.provider.asked) == 1 and not asked.waited
    await asked.http.aclose()


@pytest.mark.asyncio
async def test_single_request_disables_anthropic_library_retries(asked: Asked) -> None:
    asked.account("Anthropic", "anthropic", key=ANTHROPIC_KEY)
    asked.anthropic.answers = [refusal(503, "api_error", "unavailable")]
    with pytest.raises(ModelError):
        await asked.models.ask(question(("anthropic", "claude-opus-5-5")), retry=False)
    assert len(asked.anthropic.asked) == 1 and not asked.waited
    await asked.http.aclose()


@pytest.mark.asyncio
async def test_catalog_response_for_a_replaced_key_is_discarded(asked: Asked) -> None:
    account = asked.account("OpenAI", "openai")

    def rotate(_request: httpx.Request) -> httpx.Response:
        asked.accounts.key_stored(account, seal(SEALING, account, "replacement"))
        return httpx.Response(200, json={"data": [{"id": "old-key-only"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(rotate)) as http:
        catalogs = Models(asked.sessions, SEALING, http=http)
        assert await catalogs.refresh_catalogs() == {"refreshed": 0, "unavailable": 1}
    assert asked.state(account)["models"] == []
    assert asked.state(account)["state"] == "unchecked"
    await asked.http.aclose()


@pytest.mark.asyncio
async def test_anthropic_is_asked_like_the_others_and_an_account_is_tried_for_free(
    asked: Asked, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(anthropic_api, "RETRIES", 0)
    claude = asked.account("Anthropic one", "anthropic", key=ANTHROPIC_KEY)
    reply = await asked.models.ask(question(("anthropic", "claude-opus-5-5")))
    assert (reply.provider, reply.model, reply.account) == (
        "anthropic",
        "claude-opus-5-5",
        "Anthropic one",
    )
    assert reply.usage["input"] == 1000 and reply.usage["cached"] == 300
    assert asked.anthropic.asked[-1][3]["authorization"] == f"Bearer {ANTHROPIC_KEY}"
    assert not asked.provider.asked, "Anthropic is not asked as a chat completion"

    # Trying an account asks which models its key reaches: that costs nothing.
    assert await asked.models.check(claude) == ("ready", None)
    shown = asked.state(claude)
    assert shown["models"] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert shown["identity"] == "2 models" and shown["checked_at"]
    kimi = asked.account("Kimi one", "kimi")
    assert await asked.models.check(kimi) == ("ready", None)
    assert asked.state(kimi)["models"] == ["model-a", "model-b"]
    asked.provider.answers = [httpx.Response(401, json={"error": {"message": "bad key"}})]
    state, reason = await asked.models.check(kimi)
    assert state == "sign_in_required" and reason is not None and "bad key" in reason
    asked.provider.answers = [httpx.Response(502, text="gateway")]
    assert (await asked.models.check(kimi))[0] == "unchecked", "nothing is known of the key"
    # A key sealed by another installation says what to do about it.
    with asked.sessions.begin() as session:
        row = session.get(ProviderAccount, kimi)
        assert row is not None
        row.sealed_key = seal(b"x" * 48, kimi, OTHER_KEY)
    state, reason = await asked.models.check(kimi)
    assert (
        state == "sign_in_required" and reason is not None and "Enter the API key again" in reason
    )
    empty = UUID(asked.accounts.create(name="Empty", provider="glm")["id"])
    assert (await asked.models.check(empty))[0] == "signed_out"
    await asked.http.aclose()
