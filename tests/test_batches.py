"""Questions asked in a batch: handed in, followed across restarts, fetched, and never
handed in twice."""

# ruff: noqa: S105

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import httpx2
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.accounts import Accounts
from engineering_control.db.base import Base
from engineering_control.db.models import ModelBatch, ModelBatchRequest
from engineering_control.engine import validate
from engineering_control.engine.builtin import STARTERS
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.document import parse
from engineering_control.models import anthropic_api
from engineering_control.models.batches import Batches, listing, plus
from engineering_control.models.sealing import seal
from engineering_control.models.service import ModelError, Models, Question
from engineering_control.platform_metrics import platform_metrics
from engineering_control.sandbox.launcher import Launcher
from tests.test_ask_a_model import (
    PASSED,
    _collected,
    _console,
    _judging,
    _step,
)
from tests.test_dashboard_accounts import FakeCluster
from tests.test_models_api import ANTHROPIC_KEY, SEALING, SHAPE, Provider, streamed
from tests.test_sandbox_delivery import world  # noqa: F401 - a fixture
from tests.test_sandbox_runs import SANDBOXES

GOOD = {"decision": "PASS", "summary": "Fine.", "findings": []}
START = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def later(self, **span: float) -> None:
        self.now += timedelta(**span)


class Hub:
    """Anthropic's API as the library sees it: batches that are made, asked about,
    ended and fetched, and a model that is asked at once."""

    def __init__(self) -> None:
        self.batches: dict[str, dict[str, Any]] = {}
        self.asked: list[tuple[str, str, Any]] = []
        self.refuse: list[httpx2.Response] = []
        self.at_once: list[httpx2.Response] = []

    def factory(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self))

    # --- what a test does to a batch -------------------------------------------------------

    def end(self, batch_id: str | None = None, **results: Any) -> None:
        """The batch has ended. Each request is answered with ``results[name]``: a text
        (an answer), "late", "stopped", or a pair (kind of error, its words). A request
        that is not named is answered well."""
        batch = self.batches[batch_id or next(reversed(self.batches))]
        batch["status"] = "ended"
        batch["results"] = {
            request["custom_id"]: results.get(request["custom_id"], json.dumps(GOOD))
            for request in batch["requests"]
        }

    def outside(self, requests: int, made: datetime) -> str:
        """A batch the platform's records know nothing of."""
        name = f"msgbatch_{len(self.batches) + 1:03d}"
        self.batches[name] = {
            "requests": [{"custom_id": f"q{n}", "params": {}} for n in range(requests)],
            "status": "in_progress",
            "created_at": made,
            "results": {},
            "cancelled": False,
        }
        return name

    # --- the API ---------------------------------------------------------------------------

    def _said(self, name: str) -> dict[str, Any]:
        batch = self.batches[name]
        results = batch["results"]
        kinds = [
            "succeeded"
            if isinstance(value, str) and value not in ("late", "stopped")
            else "expired"
            if value == "late"
            else "canceled"
            if value == "stopped"
            else "errored"
            for value in results.values()
        ]
        total = len(batch["requests"])
        return {
            "id": name,
            "type": "message_batch",
            "processing_status": batch["status"],
            "request_counts": {
                "processing": 0 if batch["status"] == "ended" else total,
                "succeeded": kinds.count("succeeded"),
                "errored": kinds.count("errored"),
                "canceled": kinds.count("canceled"),
                "expired": kinds.count("expired"),
            },
            "created_at": batch["created_at"].isoformat(),
            "expires_at": (batch["created_at"] + timedelta(hours=24)).isoformat(),
            "ended_at": START.isoformat() if batch["status"] == "ended" else None,
            "archived_at": None,
            "cancel_initiated_at": START.isoformat() if batch["cancelled"] else None,
            "results_url": f"https://api.anthropic.com/v1/messages/batches/{name}/results"
            if batch["status"] == "ended"
            else None,
        }

    def _lines(self, name: str) -> str:
        lines = []
        for custom_id, value in self.batches[name]["results"].items():
            if value == "late":
                result: dict[str, Any] = {"type": "expired"}
            elif value == "stopped":
                result = {"type": "canceled"}
            elif isinstance(value, str):
                result = {
                    "type": "succeeded",
                    "message": {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-haiku-5-5-20260101",
                        "content": [{"type": "text", "text": value}],
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": 900,
                            "output_tokens": 60,
                            "cache_creation_input_tokens": 100,
                            "cache_read_input_tokens": 300,
                        },
                    },
                }
            else:
                kind, words = value
                result = {
                    "type": "errored",
                    "error": {"type": "error", "error": {"type": kind, "message": words}},
                }
            lines.append(json.dumps({"custom_id": custom_id, "result": result}))
        return "\n".join(lines) + "\n"

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.asked.append((request.method, path, body))
        self.keys = getattr(self, "keys", set()) | {
            request.headers.get("x-api-key") or request.headers.get("authorization")
        }
        if path == "/v1/messages":
            return self.at_once.pop(0) if self.at_once else streamed(json.dumps(GOOD))
        if self.refuse:
            return self.refuse.pop(0)
        if path == "/v1/messages/batches" and request.method == "POST":
            name = f"msgbatch_{len(self.batches) + 1:03d}"
            self.batches[name] = {
                "requests": body["requests"],
                "status": "in_progress",
                "created_at": START,
                "results": {},
                "cancelled": False,
            }
            return httpx2.Response(200, json=self._said(name))
        if path == "/v1/messages/batches":
            listed = [self._said(name) for name in reversed(self.batches)]
            return httpx2.Response(
                200,
                json={
                    "data": listed,
                    "has_more": False,
                    "first_id": listed[0]["id"] if listed else None,
                    "last_id": listed[-1]["id"] if listed else None,
                },
            )
        name = path.split("/")[4]
        if path.endswith("/results"):
            return httpx2.Response(
                200, headers={"content-type": "application/x-jsonl"}, content=self._lines(name)
            )
        if path.endswith("/cancel"):
            self.batches[name]["cancelled"] = True
            return httpx2.Response(200, json=self._said(name))
        return httpx2.Response(200, json=self._said(name))

    def calls(self, method: str, ending: str = "") -> list[tuple[str, str, Any]]:
        return [entry for entry in self.asked if entry[0] == method and entry[1].endswith(ending)]


class Asked:
    """A database with an Anthropic account, Anthropic behind a stand-in, and a clock."""

    def __init__(self, tmp_path: Path) -> None:
        self.engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'batches.db'}")
        Base.metadata.create_all(self.engine)
        self.sessions: sessionmaker[Session] = sessionmaker(self.engine, expire_on_commit=False)
        self.accounts = Accounts(self.sessions)
        self.hub, self.clock = Hub(), Clock()
        self.models = Models(self.sessions, SEALING, anthropic_http=self.hub.factory)
        self.batches = Batches(self.models, now=self.clock)
        self.account = self.key("Anthropic one", "anthropic", ANTHROPIC_KEY)

    def key(self, name: str, provider: str, key: str) -> UUID:
        account_id = UUID(self.accounts.create(name=name, provider=provider)["id"])
        self.accounts.key_stored(account_id, seal(SEALING, account_id, key))
        return account_id

    def batch(self) -> ModelBatch:
        with self.sessions() as session:
            found = session.scalars(select(ModelBatch).order_by(ModelBatch.created_at)).all()
            return found[-1]

    def requests(self) -> list[ModelBatchRequest]:
        with self.sessions() as session:
            return list(
                session.scalars(
                    select(ModelBatchRequest).order_by(ModelBatchRequest.position)
                ).all()
            )


@pytest.fixture
def asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(anthropic_api, "RETRIES", 0)
    found = Asked(tmp_path)
    yield found
    found.engine.dispose()


def question(**more: Any) -> Question:
    return Question(
        **{
            "choices": (("anthropic", "claude-haiku-5-5"),),
            "system": "The rules.",
            "prompt": "The question.",
            "schema": SHAPE,
            "max_tokens": 2000,
            "seconds": 30,
            "batch": True,
            **more,
        }
    )


# --- one batch, from the hand-in to its answers ----------------------------------------------


@pytest.mark.asyncio
async def test_a_batch_is_recorded_handed_in_followed_and_fetched(asked: Asked) -> None:
    asked.accounts.update(
        asked.account, prices={"claude-haiku-5-5": {"input": 1, "output": 5, "cached": 0.1}}
    )
    step = None
    await asked.batches.submit(step, None, [("question", question())], hours=6)
    batch = asked.batch()
    assert (batch.state, batch.remote_id, batch.requests) == ("waiting", "msgbatch_001", 1)
    assert (batch.provider, batch.model, batch.account_id) == (
        "anthropic",
        "claude-haiku-5-5",
        asked.account,
    )
    assert batch.deadline_at.replace(tzinfo=UTC) == START + timedelta(hours=6)
    [made] = asked.hub.calls("POST", "/batches")
    [sent] = made[2]["requests"]
    assert sent["custom_id"] == "question"
    params = sent["params"]
    # What is asked is what would be asked at once, without what a batch cannot take.
    assert params["model"] == "claude-haiku-5-5" and params["max_tokens"] == 2000
    assert params["system"] == "The rules."
    assert params["messages"][0]["content"].startswith("The question.\n\nAnswer with one JSON")
    assert params["output_config"]["format"]["schema"] == SHAPE
    assert not {"stream", "fallbacks", "betas"} & set(params)
    assert ANTHROPIC_KEY not in json.dumps(asked.requests()[0].question)

    # It is not asked about at every pass: a minute apart at first.
    batch_id = batch.id
    for _ in range(3):
        waiting = await asked.batches._follow(asked.batches._read(batch_id)[0])  # type: ignore[index]
        assert waiting.state == "waiting"
    assert asked.hub.calls("GET") == []
    assert waiting.detail == (
        "In a batch at Anthropic API since 17 Nov 09:00 UTC: 0 of 1 answered. It is waited "
        "for until 17 Nov 15:00 UTC."
    )
    asked.clock.later(seconds=61)
    assert (await asked.batches._follow(asked.batches._read(batch_id)[0])).state == "waiting"  # type: ignore[index]
    assert len(asked.hub.calls("GET")) == 1
    # After ten minutes, every five.
    asked.clock.later(minutes=11)
    await asked.batches._follow(asked.batches._read(batch_id)[0])  # type: ignore[index]
    asked.clock.later(seconds=200)
    await asked.batches._follow(asked.batches._read(batch_id)[0])  # type: ignore[index]
    assert len(asked.hub.calls("GET")) == 2
    asked.clock.later(seconds=101)
    asked.hub.end()
    ended = await asked.batches._follow(asked.batches._read(batch_id)[0])  # type: ignore[index]
    assert ended.state == "ended" and ended.provider == "anthropic"
    assert ended.account == "Anthropic one" and ended.account_id == asked.account
    assert ended.seconds == 61 + 11 * 60 + 200 + 101
    [answer] = ended.answers
    assert (answer.name, answer.state, answer.text) == ("question", "answered", json.dumps(GOOD))
    assert answer.model == "claude-haiku-5-5-20260101"
    # Half of what the same tokens cost asked at once.
    at_once = (1000 * 1 + 60 * 5 + 300 * 0.1 + 100 * 1 * 0.25) / 1e6
    assert answer.usage == {
        "tokens": 1060,
        "input": 1000,
        "output": 60,
        "cached": 300,
        "cache_write": 100,
        "cost": round(at_once / 2, 6),
        "batch": True,
        "provider": "anthropic",
        "model": "claude-haiku-5-5-20260101",
        "account": "Anthropic one",
        "account_id": str(asked.account),
    }
    after = asked.batch()
    assert (after.state, after.answered, after.detail) == (
        "ended",
        1,
        "1 of 1 questions were answered.",
    )
    # Ended, it is read from its records and the provider is not asked again.
    count = len(asked.hub.asked)
    again = await asked.batches._follow(asked.batches._read(batch_id)[0])  # type: ignore[index]
    assert again.answers == ended.answers and len(asked.hub.asked) == count

    shown = listing(asked.sessions)
    assert shown["out"] == 0 and shown["items"][0]["state"] == "ended"
    assert shown["items"][0]["provider_name"] == "Anthropic API"
    assert shown["items"][0]["account"] == "Anthropic one"


@pytest.mark.asyncio
async def test_many_questions_are_one_batch_and_each_has_its_own_end(asked: Asked) -> None:
    questions = [(f"q{number}", question(prompt=f"Question {number}.")) for number in range(4)]
    batch_id = await asked.batches.submit(None, None, questions)
    [made] = asked.hub.calls("POST", "/batches")
    assert [entry["custom_id"] for entry in made[2]["requests"]] == ["q0", "q1", "q2", "q3"]
    asked.hub.end(
        q1="late",
        q2=("invalid_request_error", "max_tokens too large"),
        q3=("api_error", "Internal server error"),
    )
    asked.clock.later(seconds=61)
    ended = await asked.batches._follow(asked.batches._read(batch_id)[0])  # type: ignore[index]
    assert [(a.name, a.state, a.kind) for a in ended.answers] == [
        ("q0", "answered", None),
        ("q1", "late", "late"),
        ("q2", "failed", "invalid"),
        ("q3", "failed", "busy"),
    ]
    assert ended.answers[0].question.prompt.startswith("Question 0.")
    assert "max_tokens too large" in (ended.answers[2].error or "")
    assert asked.batch().detail == "1 of 4 questions were answered."
    assert asked.batches.numbers() == {"out": 0, "oldest_seconds": 0.0}


@pytest.mark.asyncio
async def test_a_batch_that_cannot_be_handed_in_says_why_and_leaves_nothing(asked: Asked) -> None:
    # Busy: nothing is left of the try, the account rests, and it is worth trying later.
    asked.hub.refuse = [
        httpx2.Response(
            529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
        )
    ]
    with pytest.raises(ModelError) as busy:
        await asked.batches.submit(None, None, [("question", question())])
    assert (busy.value.label, busy.value.later) == ("busy", True)
    with asked.sessions() as session:
        assert session.scalars(select(ModelBatch)).all() == []
    assert asked.accounts.get(asked.account)["state"] == "limited"
    with pytest.raises(ModelError, match="Waiting for an account") as none:
        await asked.batches.submit(None, None, [("question", question())])
    assert none.value.later is True

    # A request the provider rejects: the batch is kept as failed, with its words.
    second = asked.key("Anthropic two", "anthropic", ANTHROPIC_KEY)
    asked.hub.refuse = [
        httpx2.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "model: nope"},
            },
        )
    ]
    with pytest.raises(ModelError, match="rejected the request") as invalid:
        await asked.batches.submit(None, None, [("question", question())])
    assert invalid.value.later is False
    assert (asked.batch().state, asked.batch().account_id) == ("failed", second)

    # A key that is refused: the account waits for a new one.
    asked.hub.refuse = [
        httpx2.Response(
            401, json={"type": "error", "error": {"type": "authentication_error", "message": "no"}}
        )
    ]
    with pytest.raises(ModelError) as refused:
        await asked.batches.submit(None, None, [("question", question())])
    assert refused.value.later is True
    assert asked.accounts.get(second)["state"] == "sign_in_required"

    # A provider that takes no batches is not handed one.
    with pytest.raises(ModelError, match="takes no questions in a batch") as never:
        await asked.batches.submit(
            None, None, [("question", question(choices=(("deepseek", "model-a"),)))]
        )
    assert never.value.label == "no_batch"


@pytest.mark.asyncio
async def test_a_batch_whose_time_is_over_is_stopped_and_what_it_answered_is_kept(
    asked: Asked,
) -> None:
    questions = [(f"q{number}", question()) for number in range(2)]
    batch_id = await asked.batches.submit(None, None, questions, hours=2)
    asked.clock.later(hours=2, seconds=1)
    waiting = await asked.batches._follow(asked.batches._read(batch_id)[0])  # type: ignore[index]
    # The provider is asked to stop it, and the batch is followed until it has.
    assert len(asked.hub.calls("POST", "/cancel")) == 1
    assert waiting.state == "waiting" and waiting.detail.endswith("It is being stopped.")
    assert (asked.batch().state, asked.batch().stopping) == ("stopping", "late")
    asked.hub.end(q1="stopped")
    asked.clock.later(seconds=301)
    ended = await asked.batches._follow(asked.batches._read(batch_id)[0])  # type: ignore[index]
    assert [(a.name, a.state) for a in ended.answers] == [("q0", "answered"), ("q1", "late")]
    assert len(asked.hub.calls("POST", "/cancel")) == 1, "it is asked to stop once"
    assert asked.batch().detail == (
        "It was not answered in its time: 1 of 2 questions were answered when it was stopped."
    )


# --- never handed in twice -------------------------------------------------------------------


def _recorded(asked: Asked, requests: int = 1) -> UUID:
    """A batch that was recorded and is not known to have reached the provider."""
    held = asked.models._one(asked.account)
    return asked.batches._record(
        None, None, held, "claude-haiku-5-5", [(f"q{n}", question()) for n in range(requests)], 24
    )


@pytest.mark.asyncio
async def test_a_batch_that_may_have_reached_the_provider_is_looked_for_before_it_is_handed_in(
    asked: Asked,
) -> None:
    # Just recorded: whoever recorded it may still be handing it in.
    first = _recorded(asked)
    soon = await asked.batches._follow(asked.batches._read(first)[0])  # type: ignore[index]
    assert soon.state == "waiting" and "being handed" in soon.detail
    assert asked.hub.asked == []

    # Later, and the provider knows no batch the platform does not: it is handed in.
    asked.clock.later(minutes=3)
    handed = await asked.batches._follow(asked.batches._read(first)[0])  # type: ignore[index]
    assert handed.state == "waiting"
    assert len(asked.hub.calls("POST", "/batches")) == 1
    assert asked.batch().remote_id == "msgbatch_001"

    # The provider has one batch of that size and time the platform knows nothing of:
    # that is the one. It is taken, and nothing is handed in.
    second = _recorded(asked, requests=3)
    theirs = asked.hub.outside(3, asked.clock.now)
    asked.hub.outside(2, asked.clock.now)  # another size: not it
    asked.clock.later(minutes=3)
    found = await asked.batches._follow(asked.batches._read(second)[0])  # type: ignore[index]
    assert found.state == "waiting" and "found again" in found.detail
    assert asked.batch().remote_id == theirs and asked.batch().state == "waiting"
    assert len(asked.hub.calls("POST", "/batches")) == 1

    # Two it could be: it is not handed in again, and says why.
    third = _recorded(asked, requests=5)
    asked.hub.outside(5, asked.clock.now)
    asked.hub.outside(5, asked.clock.now)
    asked.clock.later(minutes=3)
    unsure = await asked.batches._follow(asked.batches._read(third)[0])  # type: ignore[index]
    assert unsure.state == "failed" and "so that nothing is paid twice" in unsure.detail
    assert len(asked.hub.calls("POST", "/batches")) == 1
    # An older batch of the same size is not it either.
    fourth = _recorded(asked, requests=7)
    asked.hub.outside(7, asked.clock.now - timedelta(hours=2))
    asked.clock.later(minutes=3)
    await asked.batches._follow(asked.batches._read(fourth)[0])  # type: ignore[index]
    assert len(asked.hub.calls("POST", "/batches")) == 2


@pytest.mark.asyncio
async def test_batches_nobody_waits_for_are_stopped_and_brought_to_an_end(asked: Asked) -> None:
    batch_id = await asked.batches.submit(None, None, [("question", question())])
    assert asked.batches.numbers()["out"] == 1
    asked.clock.later(minutes=30)
    assert asked.batches.numbers() == {"out": 1, "oldest_seconds": 1800.0}
    assert "engineering_batches_out 1" in "\n".join(platform_metrics(asked.sessions))
    # No step waits for it: it is stopped at the provider, then followed to its end.
    with asked.sessions.begin() as session:
        session.get(ModelBatch, batch_id).step_id = None  # type: ignore[union-attr]
    assert await asked.batches.sweep() == 1
    assert asked.batch().state == "waiting", "a batch that is no step's is left to its time"
    asked.clock.later(hours=24)
    assert await asked.batches.sweep() == 1
    assert len(asked.hub.calls("POST", "/cancel")) == 1 and asked.batch().state == "stopping"
    asked.hub.end(question="stopped")
    asked.clock.later(minutes=6)
    assert await asked.batches.sweep() == 1
    assert (asked.batch().state, asked.batch().answered) == ("ended", 0)
    assert await asked.batches.sweep() == 0


def test_what_two_questions_of_a_step_used_is_added() -> None:
    first = {"tokens": 100, "input": 80, "output": 20, "cached": 5, "cost": 0.001, "batch": True}
    second = {"tokens": 50, "input": 40, "output": 10, "cached": 0, "model": "m", "account": "A"}
    assert plus(None) is None and plus(None, second) == second
    assert plus(first, second) == {
        "tokens": 150,
        "input": 120,
        "output": 30,
        "cached": 5,
        "cost": 0.001,
        "model": "m",
        "account": "A",
        "batch": True,
    }


# --- the settings and the rules --------------------------------------------------------------


def test_every_task_that_asks_a_model_can_be_asked_in_a_batch_where_the_provider_takes_one() -> (
    None
):
    for task in TASKS.values():
        names = [param.name for param in task.params]
        wanted = task.where == "model" and task.key != "model.summarise"
        assert {"asked", "hours", "late"} <= set(names) or not wanted, task.key
    asks = {param.name: param for param in TASKS["model.judge"].params}
    assert asks["asked"].choices == ("at_once", "batch") and asks["asked"].default == "at_once"
    assert (asks["hours"].default, asks["hours"].low, asks["hours"].high) == (24, 1, 24)
    assert asks["late"].choices == ("stop", "ask") and asks["late"].default == "stop"

    def said(raw: dict[str, Any]) -> list[tuple[str, str]]:
        return [(found["level"], found["text"]) for found in validate.problems(parse(raw))]

    fine = _judging(provider="anthropic", model="claude-haiku-5-5", asked="batch", hours=6)
    assert [text for level, text in said(fine) if level == "error"] == []
    wrong = _judging(asked="batch")  # DeepSeek takes no batches
    assert (
        "error",
        "judge is to be asked in a batch, and DeepSeek takes none: ask it at once, or name a "
        "provider that does (Anthropic API)",
    ) in said(wrong)
    assert any("hours of judge is between 1 and 24" in text for _, text in said(_judging(hours=30)))
    # In a loop it is allowed, and said what it means.
    loop = STARTERS["starter-model-loop"].as_dict()
    for task in loop["tasks"]:
        if task["task"] == "model.choose":
            task["with"] = {
                **task.get("with", {}),
                "provider": "anthropic",
                "model": "claude-haiku-5-5",
                "asked": "batch",
            }
            chooser = task["id"]
    warned = [text for level, text in said(loop) if level == "warning"]
    assert any(
        text.startswith(f"{chooser} is asked in a batch at every step of its loop")
        for text in warned
    )
    assert not [text for level, text in said(loop) if level == "error" and "batch" in text], (
        "a loop asked in batches is the administrator's to choose"
    )


# --- a run that waits for a batch ------------------------------------------------------------


def _batched(world: Any, hub: Hub, clock: Clock) -> None:  # noqa: F811
    """The run's world with Anthropic behind the stand-in, and the launcher's clock."""
    models = Models(world.sessions, SEALING, anthropic_http=hub.factory)
    world.launcher.models = models
    world.launcher.batches = Batches(models, now=clock)
    account_id = UUID(world.accounts.create(name="Anthropic one", provider="anthropic")["id"])
    world.accounts.key_stored(account_id, seal(SEALING, account_id, ANTHROPIC_KEY))
    world.accounts.update(
        account_id, prices={"claude-haiku-5-5": {"input": 1, "output": 5, "cached": 0.1}}
    )
    world.anthropic_account = account_id


def _in_a_batch(**more: Any) -> dict[str, Any]:
    return _judging(provider="anthropic", model="claude-haiku-5-5", asked="batch", **more)


@pytest.mark.asyncio
async def test_a_step_waits_for_its_batch_across_a_restart_and_goes_on_with_the_answer(
    world: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anthropic_api, "RETRIES", 0)
    hub, clock = Hub(), Clock()
    _batched(world, hub, clock)
    run = await _collected(world, _in_a_batch(hours=6))
    await world.launcher.tick()
    # The question is at Anthropic as a batch; no sandbox runs and nothing is in memory
    # that the wait depends on.
    judge = _step(world, run["id"], "judge")
    assert judge["state"] == "running" and world.fake.pods == {}
    assert world.launcher._calls == {}
    [made] = hub.calls("POST", "/batches")
    [sent] = made[2]["requests"]
    assert sent["custom_id"] == "question"
    said = sent["params"]["messages"][0]["content"]
    assert "Judge what is asked below." in said and "Is the change complete?" in said
    await world.launcher.tick()
    waiting = _step(world, run["id"], "judge")
    assert waiting["detail"] == (
        "In a batch at Anthropic API since 17 Nov 09:00 UTC: 0 of 1 answered. It is waited "
        "for until 17 Nov 15:00 UTC."
    )
    assert world.run(run["id"])["state"] == "running"

    # The launcher starts again: it finds the batch in its records and hands nothing in.
    again = Launcher(world.runs, world.launcher.cluster, SANDBOXES, world.github)
    again.models = world.launcher.models
    again.batches = Batches(again.models, now=clock)
    world.launcher = again
    clock.later(hours=1)
    await world.launcher.tick()
    assert len(hub.calls("POST", "/batches")) == 1
    assert _step(world, run["id"], "judge")["state"] == "running"

    # Anthropic has answered: the next look fetches it and the run goes on.
    hub.end(question=json.dumps(PASSED))
    clock.later(minutes=6)
    await world.launcher.tick()
    judged = _step(world, run["id"], "judge")
    assert (judged["state"], judged["outcome"]) == ("succeeded", "passed")
    assert judged["detail"] == "Correct and tested."
    assert judged["usage"]["batch"] is True and judged["usage"]["provider"] == "anthropic"
    assert judged["usage"]["account"] == "Anthropic one"
    assert judged["usage"]["cost"] == round(
        (1000 * 1 + 60 * 5 + 300 * 0.1 + 100 * 0.25) / 1e6 / 2, 6
    )
    assert judged["log"].startswith(
        "--- asked anthropic (claude-haiku-5-5-20260101) with the account Anthropic one, in a "
        "batch; answered after 66 minutes ---"
    )
    assert "--- answered ---" in judged["log"] and "Correct and tested." in judged["log"]
    assert hub.calls("POST", "/v1/messages") == [], "nothing was asked at once"
    # The run waits for the administrator's decision, as it would have asked at once.
    assert world.run(run["id"])["state"] == "waiting"
    shown = world.launcher.batches.listing()["items"][0]
    assert (shown["state"], shown["step"], shown["run"]) == ("ended", "judge", run["title"])


@pytest.mark.asyncio
async def test_an_answer_of_the_wrong_shape_is_asked_for_again_at_once(
    world: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anthropic_api, "RETRIES", 0)
    hub, clock = Hub(), Clock()
    _batched(world, hub, clock)
    run = await _collected(world, _in_a_batch())
    await world.launcher.tick()
    hub.end(question='{"decision": "MAYBE"}')
    hub.at_once = [streamed(json.dumps(PASSED))]
    clock.later(seconds=61)
    await world.launcher.tick()
    # Not another batch: one question at once, saying what was wrong.
    import asyncio

    await asyncio.wait(list(world.launcher._calls.values()), timeout=10)
    [direct] = hub.calls("POST", "/v1/messages")
    said = direct[2]["messages"][0]["content"]
    assert "An earlier answer could not be used:" in said and "decision" in said
    assert len(hub.calls("POST", "/batches")) == 1
    await world.launcher.tick()
    judged = _step(world, run["id"], "judge")
    assert (judged["state"], judged["outcome"]) == ("succeeded", "passed")
    # What the batch used and what the question at once used are both counted.
    assert judged["usage"]["tokens"] == 2 * 1060 and judged["usage"]["batch"] is True
    assert ", twice; answered in" in judged["log"]


@pytest.mark.asyncio
async def test_a_late_batch_stops_the_run_or_is_asked_at_once_as_the_task_says(
    world: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anthropic_api, "RETRIES", 0)
    hub, clock = Hub(), Clock()
    _batched(world, hub, clock)
    run = await _collected(world, _in_a_batch(hours=2))
    await world.launcher.tick()
    clock.later(hours=2, minutes=1)
    await world.launcher.tick()
    assert len(hub.calls("POST", "/cancel")) == 1
    assert _step(world, run["id"], "judge")["detail"].endswith("It is being stopped.")
    hub.end(question="stopped")
    clock.later(minutes=6)
    await world.launcher.tick()
    late = world.run(run["id"])
    assert (late["state"], late["outcome"]) == ("failed", "batch_late")
    assert late["detail"].startswith("The batch was not answered within 2 hours.")
    assert hub.calls("POST", "/v1/messages") == []

    # Set to be asked at once when it is late: it is, at the full price.
    run = await _collected(world, _in_a_batch(hours=2, late="ask"))
    await world.launcher.tick()
    clock.later(hours=2, minutes=1)
    await world.launcher.tick()
    hub.end(question="stopped")
    hub.at_once = [streamed(json.dumps(PASSED))]
    clock.later(minutes=6)
    await world.launcher.tick()
    import asyncio

    await asyncio.wait(list(world.launcher._calls.values()), timeout=10)
    await world.launcher.tick()
    judged = _step(world, run["id"], "judge")
    assert (judged["state"], judged["outcome"]) == ("succeeded", "passed")
    assert "batch" not in judged["usage"]
    assert len(hub.calls("POST", "/v1/messages")) == 1


@pytest.mark.asyncio
async def test_stopping_a_run_stops_its_batch_at_the_provider(
    world: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anthropic_api, "RETRIES", 0)
    hub, clock = Hub(), Clock()
    _batched(world, hub, clock)
    run = await _collected(world, _in_a_batch())
    await world.launcher.tick()
    world.runs.request_cancel(UUID(run["id"]))
    await world.launcher.tick()
    assert world.run(run["id"])["state"] == "cancelled"
    assert len(hub.calls("POST", "/cancel")) == 1
    with world.sessions() as session:
        [batch] = session.scalars(select(ModelBatch)).all()
        assert (batch.state, batch.stopping) == ("stopping", "stopped")
    # It is followed to its end by the launcher's sweep, though no step waits for it.
    hub.end(question="stopped")
    clock.later(minutes=6)
    world.launcher._swept = -1e9
    await world.launcher.tick()
    with world.sessions() as session:
        [batch] = session.scalars(select(ModelBatch)).all()
        assert batch.state == "ended" and batch.detail == (
            "It was stopped: 0 of 1 questions were answered."
        )


@pytest.mark.asyncio
async def test_a_run_whose_questions_are_all_to_be_batched_asks_them_so_where_it_can(
    world: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anthropic_api, "RETRIES", 0)
    hub, clock = Hub(), Clock()
    _batched(world, hub, clock)
    run = await _collected(world, _judging(provider="anthropic", model="claude-haiku-5-5"))
    world.runs.merge_data(UUID(run["id"]), {"ask": "batch"})
    await world.launcher.tick()
    assert len(hub.calls("POST", "/batches")) == 1 and hub.calls("POST", "/v1/messages") == []


def test_the_console_lists_the_batches(tmp_path: Path) -> None:
    client = _console(tmp_path, Provider(), FakeCluster())
    try:
        assert client.get("/api/dashboard/batches").json() == {"items": [], "out": 0}
        sessions = client.repository.sessions  # type: ignore[attr-defined]
        with sessions.begin() as session:
            session.add(
                ModelBatch(
                    id=uuid4(),
                    provider="anthropic",
                    model="claude-haiku-5-5",
                    remote_id="msgbatch_9",
                    state="waiting",
                    requests=3,
                    answered=1,
                    deadline_at=START + timedelta(hours=24),
                    submitted_at=START,
                )
            )
        shown = client.get("/api/dashboard/batches").json()
        assert shown["out"] == 1
        [batch] = shown["items"]
        assert (batch["state"], batch["requests"], batch["answered"]) == ("waiting", 3, 1)
        assert batch["provider_name"] == "Anthropic API" and batch["run"] is None
        assert "msgbatch_9" not in json.dumps(shown), "its name at the provider is not shown"
        client.cookies.clear()
        assert client.get("/api/dashboard/batches").status_code == 401
    finally:
        client.__exit__(None, None, None)
    assert httpx.codes.OK == 200


def test_the_console_hands_in_a_batch_to_try_an_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(anthropic_api, "RETRIES", 0)
    hub = Hub()
    client = _console(tmp_path, Provider(), FakeCluster())
    client.app.state.models._anthropic_http = hub.factory  # type: ignore[attr-defined]

    def post(path: str, **fields: Any) -> Any:
        csrf = client.get("/api/dashboard/session").json()["csrf_token"]
        return client.post(f"/api/dashboard/accounts{path}", json={"csrf_token": csrf, **fields})

    try:
        listed = client.get("/api/dashboard/accounts").json()["providers"]
        takes = {entry["key"]: entry.get("batch") for entry in listed if entry["kind"] == "api"}
        assert takes["anthropic"] is True and takes["deepseek"] is False
        account = post("", name="Anthropic one", provider="anthropic").json()
        sessions = client.repository.sessions  # type: ignore[attr-defined]
        Accounts(sessions).key_stored(
            UUID(account["id"]), seal(SEALING, UUID(account["id"]), ANTHROPIC_KEY)
        )
        tried = post(f"/{account['id']}/batch-trial", model="claude-haiku-5-5")
        assert tried.status_code == 200, tried.text
        assert tried.json()["account"] == "Anthropic one" and tried.json()["batch"]
        [made] = hub.calls("POST", "/batches")
        assert made[2]["requests"][0]["params"]["model"] == "claude-haiku-5-5"
        shown = client.get("/api/dashboard/batches").json()
        assert shown["out"] == 1 and shown["items"][0]["run"] is None
        assert shown["items"][0]["account"] == "Anthropic one"
        # A provider that takes none is not handed one, nor a subscription.
        deepseek = post("", name="DeepSeek one", provider="deepseek").json()
        refused = post(f"/{deepseek['id']}/batch-trial", model="model-a")
        assert refused.status_code == 422 and "takes no questions in a batch" in refused.text
        claude = post("", name="Claude one", provider="claude").json()
        assert post(f"/{claude['id']}/batch-trial", model="m").status_code == 422
        assert post(f"/{account['id']}/batch-trial", model="two words").status_code == 422
    finally:
        client.__exit__(None, None, None)
