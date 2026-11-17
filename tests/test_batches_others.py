"""Batches at the providers other than Anthropic: the ones that take a file of requests
as OpenAI does (OpenAI, Qwen, Kimi), and Google."""

# ruff: noqa: S105

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.accounts import Accounts
from engineering_control.db.base import Base
from engineering_control.db.models import ModelBatch
from engineering_control.engine import validate
from engineering_control.engine.document import parse
from engineering_control.models import google_batch
from engineering_control.models.batches import Batches
from engineering_control.models.providers import BY_KEY
from engineering_control.models.sealing import seal
from engineering_control.models.service import Models, Question
from engineering_control.models.wire import WireError
from tests.test_ask_a_model import _judging
from tests.test_batches import GOOD, START, Clock
from tests.test_models_api import SEALING, SHAPE

OPENAI_KEY = "sk-" + "o9" * 24
GOOGLE_KEY = "AIza" + "g" * 35


def _completion(answer: Any, **usage: int) -> dict[str, Any]:
    return {
        "model": "gpt-nano-2026",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(answer)},
            }
        ],
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 400},
            **usage,
        },
    }


class Files:
    """A provider that takes a batch as a file of requests."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []
        self.files: dict[str, str] = {}
        self.batches: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self.keys: set[str] = set()

    def end(self, status: str = "completed", **results: Any) -> None:
        """The last batch has ended. Each request not named is answered well; one named
        with a pair is refused with that status and those words; with "late" it was not
        answered before the batch ended."""
        batch = self.batches[next(reversed(self.batches))]
        answered, failed = [], []
        for line in self.files[batch["input_file_id"]].splitlines():
            custom = json.loads(line)["custom_id"]
            result = results.get(custom, GOOD)
            if result == "late":
                failed.append(
                    {
                        "custom_id": custom,
                        "response": None,
                        "error": {"code": "batch_expired", "message": "expired"},
                    }
                )
            elif isinstance(result, tuple):
                failed.append(
                    {
                        "custom_id": custom,
                        "response": {
                            "status_code": result[0],
                            "body": {"error": {"message": result[1]}},
                        },
                        "error": None,
                    }
                )
            else:
                answered.append(
                    {
                        "custom_id": custom,
                        "response": {"status_code": 200, "body": _completion(result)},
                        "error": None,
                    }
                )
        self.files["out-1"] = "\n".join(json.dumps(entry) for entry in answered)
        self.files["err-1"] = "\n".join(json.dumps(entry) for entry in failed)
        batch.update(
            status=status,
            output_file_id="out-1" if answered else None,
            error_file_id="err-1" if failed else None,
            request_counts={
                "total": len(answered) + len(failed),
                "completed": len(answered),
                "failed": len(failed),
            },
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1").removeprefix("/compatible-mode/v1")
        self.asked.append((request.method, path))
        self.keys.add(request.headers.get("authorization", ""))
        if path == "/files" and request.method == "POST":
            body = request.content.decode()
            assert 'name="purpose"' in body and "batch" in body
            lines = [line for line in body.splitlines() if line.startswith('{"custom_id"')]
            name = f"file-{len(self.files) + 1}"
            self.files[name] = "\n".join(lines)
            return httpx.Response(200, json={"id": name})
        if path == "/batches" and request.method == "POST":
            made = json.loads(request.content)
            name = f"batch_{len(self.batches) + 1}"
            total = len(self.files[made["input_file_id"]].splitlines())
            self.batches[name] = {
                "id": name,
                "status": "validating",
                "created_at": START.timestamp(),
                "request_counts": {"total": total, "completed": 0, "failed": 0},
                **made,
            }
            return httpx.Response(200, json=self.batches[name])
        if path == "/batches":
            return httpx.Response(200, json={"data": list(reversed(self.batches.values()))})
        if path.endswith("/content"):
            return httpx.Response(200, text=self.files[path.split("/")[2]])
        if path.startswith("/files/") and request.method == "DELETE":
            self.deleted.append(path.split("/")[2])
            return httpx.Response(200, json={"deleted": True})
        name = path.split("/")[2]
        if path.endswith("/cancel"):
            self.batches[name]["status"] = "cancelling"
        return httpx.Response(200, json=self.batches[name])


class Google:
    """Google's own interface for batches."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str, Any, str | None]] = []
        self.batches: dict[str, dict[str, Any]] = {}

    def end(self, *inlined: dict[str, Any], state: str = "JOB_STATE_SUCCEEDED") -> None:
        batch = self.batches[next(reversed(self.batches))]
        batch["metadata"]["state"] = state
        batch["done"] = True
        batch["response"] = {"inlinedResponses": {"inlinedResponses": list(inlined)}}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.asked.append((request.method, path, body, request.headers.get("x-goog-api-key")))
        if path.endswith(":batchGenerateContent"):
            name = f"batches/b{len(self.batches) + 1}"
            listed = body["batch"]["input_config"]["requests"]["requests"]
            self.batches[name] = {
                "name": name,
                "metadata": {
                    "state": "JOB_STATE_PENDING",
                    "createTime": START.isoformat().replace("+00:00", "Z"),
                    "displayName": body["batch"]["display_name"],
                    "batchStats": {
                        "requestCount": str(len(listed)),
                        "pendingRequestCount": str(len(listed)),
                    },
                },
            }
            return httpx.Response(200, json=self.batches[name])
        if path == "/v1beta/batches":
            return httpx.Response(200, json={"operations": list(self.batches.values())})
        name = path.removeprefix("/v1beta/")
        if name.endswith(":cancel"):
            name = name.removesuffix(":cancel")
            self.batches[name]["metadata"]["state"] = "JOB_STATE_CANCELLED"
            return httpx.Response(200, json={})
        return httpx.Response(200, json=self.batches[name])


class Asked:
    def __init__(self, tmp_path: Path, stand_in: Any) -> None:
        self.engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'others.db'}")
        Base.metadata.create_all(self.engine)
        self.sessions: sessionmaker[Session] = sessionmaker(self.engine, expire_on_commit=False)
        self.accounts = Accounts(self.sessions)
        self.clock = Clock()
        self.models = Models(
            self.sessions, SEALING, http=httpx.AsyncClient(transport=httpx.MockTransport(stand_in))
        )
        self.batches = Batches(self.models, now=self.clock)

    def key(self, name: str, provider: str, key: str, **more: Any) -> UUID:
        account_id = UUID(self.accounts.create(name=name, provider=provider, **more)["id"])
        self.accounts.key_stored(account_id, seal(SEALING, account_id, key))
        return account_id

    def batch(self) -> ModelBatch:
        with self.sessions() as session:
            return session.scalars(select(ModelBatch).order_by(ModelBatch.created_at)).all()[-1]

    async def follow(self, batch_id: UUID) -> Any:
        read = self.batches._read(batch_id)
        assert read is not None
        return await self.batches._follow(read[0])


def question(provider: str, model: str, **more: Any) -> Question:
    return Question(
        **{
            "choices": ((provider, model),),
            "system": "The rules.",
            "prompt": "The question.",
            "schema": SHAPE,
            "max_tokens": 2000,
            "seconds": 30,
            "effort": "low",
            "batch": True,
            **more,
        }
    )


def test_which_providers_take_batches_and_at_what_price() -> None:
    assert {key: kind.batch for key, kind in BY_KEY.items()} == {
        "anthropic": "anthropic",
        "openai": "files",
        "google": "google",
        "deepseek": "",
        "qwen": "files",
        "glm": "",
        "kimi": "files",
    }
    assert BY_KEY["kimi"].batch_price == 0.6
    assert all(kind.batch_price == 0.5 for key, kind in BY_KEY.items() if key != "kimi")

    def errors(raw: dict[str, Any]) -> list[str]:
        return [found["text"] for found in validate.errors(parse(raw))]

    for provider in ("openai", "google", "qwen", "kimi", "anthropic"):
        assert errors(_judging(provider=provider, model="m", asked="batch")) == [], provider
    for provider in ("deepseek", "glm"):
        [said] = errors(_judging(provider=provider, model="m", asked="batch"))
        assert "takes none" in said and "Anthropic API, OpenAI API, Google Gemini API" in said


@pytest.mark.asyncio
async def test_a_batch_at_openai_is_a_file_of_requests_and_its_answers_another(
    tmp_path: Path,
) -> None:
    hub = Files()
    asked = Asked(tmp_path, hub)
    account = asked.key("OpenAI one", "openai", OPENAI_KEY)
    asked.accounts.update(account, prices={"gpt-nano": {"input": 1, "output": 4, "cached": 0.1}})
    questions = [(f"q{number}", question("openai", "gpt-nano")) for number in range(4)]
    batch_id = await asked.batches.submit(None, None, questions)
    assert hub.keys == {f"Bearer {OPENAI_KEY}"}
    assert hub.asked == [("POST", "/files"), ("POST", "/batches")]
    lines = [json.loads(line) for line in hub.files["file-1"].splitlines()]
    assert [line["custom_id"] for line in lines] == ["q0", "q1", "q2", "q3"]
    assert {(line["method"], line["url"]) for line in lines} == {("POST", "/v1/chat/completions")}
    body = lines[0]["body"]
    assert body["model"] == "gpt-nano" and body["max_completion_tokens"] == 2000
    assert body["messages"][0] == {"role": "system", "content": "The rules."}
    assert body["messages"][1]["content"].startswith("The question.\n\nAnswer with one JSON")
    assert body["response_format"]["type"] == "json_schema" and body["reasoning_effort"] == "low"
    made = hub.batches["batch_1"]
    assert (made["endpoint"], made["completion_window"]) == ("/v1/chat/completions", "24h")
    # The batch carries the name of the platform's record of it.
    assert made["metadata"] == {"engineering_control": str(batch_id)}
    assert (asked.batch().state, asked.batch().remote_id) == ("waiting", "batch_1")

    asked.clock.later(seconds=61)
    hub.batches["batch_1"]["status"] = "in_progress"
    assert (await asked.follow(batch_id)).state == "waiting"
    hub.end(q1=(400, "The model does not take that."), q2=(500, "Server error"), q3="late")
    asked.clock.later(seconds=61)
    ended = await asked.follow(batch_id)
    assert [(a.name, a.state, a.kind) for a in ended.answers] == [
        ("q0", "answered", None),
        ("q1", "failed", "invalid"),
        ("q2", "failed", "busy"),
        ("q3", "late", "late"),
    ]
    good = ended.answers[0]
    assert json.loads(good.text) == GOOD and good.model == "gpt-nano-2026"
    assert "The model does not take that." in (ended.answers[1].error or "")
    assert good.usage is not None and good.usage["batch"] is True
    assert good.usage["cost"] == round((600 * 1 + 50 * 4 + 400 * 0.1) / 1e6 / 2, 6)
    # The file of questions is not left with the provider.
    assert hub.deleted == ["file-1"]


@pytest.mark.asyncio
async def test_a_batch_openai_would_not_take_says_why_and_a_lost_one_is_found_by_its_label(
    tmp_path: Path,
) -> None:
    hub = Files()
    asked = Asked(tmp_path, hub)
    account = asked.key("OpenAI one", "openai", OPENAI_KEY)
    batch_id = await asked.batches.submit(None, None, [("question", question("openai", "m"))])
    hub.batches["batch_1"].update(
        status="failed",
        errors={"data": [{"code": "model_not_found", "message": "The model m does not exist."}]},
    )
    asked.clock.later(seconds=61)
    ended = await asked.follow(batch_id)
    [answer] = ended.answers
    assert (answer.state, answer.kind) == ("failed", "invalid")
    assert "did not take the batch: The model m does not exist." in (answer.error or "")

    # A batch that was recorded and not known to be handed in is found at the provider
    # by the label it was handed in with, whatever else was handed in at that time.
    held = asked.models._one(account)
    lost = asked.batches._record(None, None, held, "m", [("question", question("openai", "m"))], 24)
    hub.files["file-9"] = '{"custom_id": "question"}'
    for name, label in (("batch_8", "another"), ("batch_9", str(lost))):
        hub.batches[name] = {
            "id": name,
            "status": "in_progress",
            "created_at": asked.clock.now.timestamp(),
            "request_counts": {"total": 1, "completed": 0, "failed": 0},
            "metadata": {"engineering_control": label},
            "input_file_id": "file-9",
        }
    asked.clock.later(minutes=3)
    found = await asked.follow(lost)
    assert found.state == "waiting" and "found again" in found.detail
    assert asked.batch().remote_id == "batch_9"
    assert hub.asked.count(("POST", "/batches")) == 1, "nothing was handed in again"
    # Stopping it asks the provider to.
    await asked.batches._stop(asked.batches._read(lost)[0], held, "stopped")  # type: ignore[index]
    assert ("POST", "/batches/batch_9/cancel") in hub.asked


@pytest.mark.asyncio
async def test_qwen_and_kimi_take_batches_the_same_way_at_their_own_address_and_price(
    tmp_path: Path,
) -> None:
    hub = Files()
    asked = Asked(tmp_path, hub)
    kimi = asked.key("Kimi one", "kimi", "sk-" + "k7" * 24)
    asked.accounts.update(kimi, prices={"kimi-k2.6": {"input": 1, "output": 4}})
    batch_id = await asked.batches.submit(None, None, [("question", question("kimi", "kimi-k2.6"))])
    body = json.loads(hub.files["file-1"])["body"]
    # They are held to a JSON object, as when they are asked at once, and are not told
    # how hard to reason.
    assert body["response_format"] == {"type": "json_object"} and "reasoning_effort" not in body
    assert body["max_tokens"] == 2000
    hub.end()
    asked.clock.later(seconds=61)
    [answer] = (await asked.follow(batch_id)).answers
    assert answer.usage is not None
    assert answer.usage["cost"] == round((1000 * 1 + 50 * 4) / 1e6 * 0.6, 6), "Kimi: 60%"
    asked.key("Qwen one", "qwen", "sk-" + "q5" * 24)
    await asked.batches.submit(None, None, [("question", question("qwen", "qwen-plus"))])
    assert hub.asked.count(("POST", "/batches")) == 2


def _generated(text: str, reason: str = "STOP", **usage: int) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "finishReason": reason,
                "content": {
                    "parts": [
                        {"text": "Let me think.", "thought": True},
                        {"text": text},
                    ]
                },
            }
        ],
        "modelVersion": "gemini-flash-lite-001",
        "usageMetadata": {
            "promptTokenCount": 1000,
            "candidatesTokenCount": 40,
            "thoughtsTokenCount": 60,
            "cachedContentTokenCount": 300,
            **usage,
        },
    }


@pytest.mark.asyncio
async def test_a_batch_at_google_is_handed_in_and_fetched_through_googles_own_interface(
    tmp_path: Path,
) -> None:
    hub = Google()
    asked = Asked(tmp_path, hub)
    account = asked.key("Google one", "google", GOOGLE_KEY)
    asked.accounts.update(
        account, prices={"gemini-flash-lite": {"input": 1, "output": 4, "cached": 0.1}}
    )
    questions = [(f"q{number}", question("google", "gemini-flash-lite")) for number in range(4)]
    batch_id = await asked.batches.submit(None, None, questions)
    method, path, body, key = hub.asked[0]
    assert (method, path) == ("POST", "/v1beta/models/gemini-flash-lite:batchGenerateContent")
    assert key == GOOGLE_KEY
    made = body["batch"]
    assert made["display_name"] == str(batch_id)
    listed = made["input_config"]["requests"]["requests"]
    assert [entry["metadata"]["key"] for entry in listed] == ["q0", "q1", "q2", "q3"]
    request = listed[0]["request"]
    assert request["systemInstruction"] == {"parts": [{"text": "The rules."}]}
    assert request["contents"][0]["parts"][0]["text"].startswith("The question.\n\nAnswer with")
    assert request["generationConfig"] == {
        "maxOutputTokens": 2000,
        "responseMimeType": "application/json",
    }
    assert (asked.batch().state, asked.batch().remote_id) == ("waiting", "batches/b1")

    asked.clock.later(seconds=61)
    assert (await asked.follow(batch_id)).state == "waiting"
    assert hub.asked[-1][:2] == ("GET", "/v1beta/batches/b1")
    hub.end(
        {"response": _generated(json.dumps(GOOD)), "metadata": {"key": "q0"}},
        {"error": {"code": 400, "message": "Bad request."}},
        {"response": _generated("{", "MAX_TOKENS")},
        {"response": {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}},
    )
    asked.clock.later(seconds=61)
    ended = await asked.follow(batch_id)
    # Each answer is matched to its request by the name it was given, or by its place.
    assert [(a.name, a.state, a.kind) for a in ended.answers] == [
        ("q0", "answered", None),
        ("q1", "failed", "invalid"),
        ("q2", "failed", "cut"),
        ("q3", "failed", "declined"),
    ]
    good = ended.answers[0]
    # What the model thought is not its answer, and is paid as written.
    assert json.loads(good.text) == GOOD and good.model == "gemini-flash-lite-001"
    assert good.usage is not None
    assert (good.usage["input"], good.usage["cached"], good.usage["output"]) == (700, 300, 100)
    assert good.usage["cost"] == round((700 * 1 + 100 * 4 + 300 * 0.1) / 1e6 / 2, 6)

    # A batch Google gives up is said so for each of its questions.
    second = await asked.batches.submit(
        None, None, [("question", question("google", "gemini-flash-lite"))]
    )
    hub.end(state="JOB_STATE_FAILED")
    asked.clock.later(seconds=61)
    [failed] = (await asked.follow(second)).answers
    assert (failed.state, failed.kind) == ("failed", "busy")

    # Its time over, it is stopped at Google's own address for that.
    third = await asked.batches.submit(
        None, None, [("question", question("google", "gemini-flash-lite"))], hours=1
    )
    asked.clock.later(hours=1, seconds=1)
    await asked.follow(third)
    assert ("POST", "/v1beta/batches/b3:cancel") in [entry[:2] for entry in hub.asked]


def test_what_google_generated_is_read_or_said_why_not() -> None:
    wire = google_batch.read(_generated('{"a": 1}'), "m", 100)
    assert (wire.text, wire.read, wire.cached, wire.written) == ('{"a": 1}', 700, 300, 100)
    with pytest.raises(WireError) as cut:
        google_batch.read(_generated("{", "MAX_TOKENS"), "m", 100)
    assert cut.value.kind == "cut"
    with pytest.raises(WireError) as declined:
        google_batch.read(_generated("", "SAFETY"), "m", 100)
    assert declined.value.kind == "declined"
    with pytest.raises(WireError) as nothing:
        google_batch.read("not an object", "m", 100)
    assert nothing.value.kind == "busy"
    assert google_batch.root_of("https://generativelanguage.googleapis.com/v1beta/openai") == (
        "https://generativelanguage.googleapis.com/v1beta"
    )
    assert datetime.now(UTC) - timedelta(days=1) < datetime.now(UTC)
