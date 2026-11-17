"""Many questions in one batch: summaries of a project's code, and trials in the lab."""

# ruff: noqa: S105

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from engineering_control.db.models import CodeSummary, ModelBatch, SandboxRun
from engineering_control.engine import validate
from engineering_control.engine.builtin import BUILTIN, SUMMARY_GROUPS, summarise_code
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.document import parse
from engineering_control.engine.evaluations import in_batches
from engineering_control.models import anthropic_api
from engineering_control.models.batches import Batches
from engineering_control.models.sealing import seal
from engineering_control.models.service import Models
from tests.test_ask_a_model import _judging
from tests.test_batches import Clock, Hub
from tests.test_model_edits import stage  # noqa: F401 - a fixture
from tests.test_models_api import ANTHROPIC_KEY, SEALING
from tests.test_sandbox_delivery import BASE, _log, _ran, world  # noqa: F401 - fixtures
from tests.test_workbench import (  # noqa: F401 - fixtures
    _bench_pod,
    _running,
    _steps,
    _tool_log,
    repo,
    tools,
)

PROJECT = "falcone-workflow"


def test_the_picker_hands_over_several_groups_and_no_file_twice(
    tools: Any,  # noqa: F811
    repo: Any,  # noqa: F811
) -> None:
    one = tools.digest({"known": {}, "limit": 2})
    assert [entry["group"] for entry in one["files"]] == [1, 1]
    several = tools.digest({"known": {}, "limit": 2, "groups": 3})
    picked = [(entry["group"], entry["path"]) for entry in several["files"]]
    assert [group for group, _path in picked] == [1, 1, 2, 2, 3, 3]
    assert len({path for _group, path in picked}) == 6, "a file is in one group only"
    # The first group is what would have been picked alone, and what is left counts
    # every group's files as picked.
    assert [path for group, path in picked if group == 1] == [e["path"] for e in one["files"]]
    assert several["left"] == one["left"] - 4
    # More groups than there are files to fill: only the ones that have files.
    every = tools.digest({"known": {}, "limit": 40, "groups": 5})
    assert {entry["group"] for entry in every["files"]} == {1} and every["left"] == 0


def test_summarising_in_batches_picks_many_groups_and_asks_about_them_as_one() -> None:
    kinds = {param.name: param for param in TASKS["code.digest"].params}
    assert (kinds["groups"].default, kinds["groups"].low, kinds["groups"].high) == (1, 1, 20)
    asks = [param.name for param in TASKS["model.summarise"].params]
    assert {"asked", "hours", "late"} <= set(asks) and "cache" not in asks

    usual = summarise_code(provider="anthropic", model="claude-haiku-5-5")
    by_id = {task["id"]: task for task in usual.as_dict()["tasks"]}
    assert "groups" not in by_id["pick"]["with"] and "asked" not in by_id["summarise"]["with"]
    batched = summarise_code(provider="anthropic", model="claude-haiku-5-5", batch=True)
    by_id = {task["id"]: task for task in batched.as_dict()["tasks"]}
    assert by_id["pick"]["with"]["groups"] == SUMMARY_GROUPS == 20
    assert by_id["summarise"]["with"]["asked"] == "batch"
    assert validate.errors(batched) == []
    # At a provider that takes no batches the flow is refused, with its reason.
    [wrong] = validate.errors(summarise_code(provider="deepseek", model="m", batch=True))
    assert "DeepSeek takes none" in wrong["text"]


def test_a_trial_in_the_lab_asks_in_batches_where_the_provider_takes_them() -> None:
    flow = parse(_judging(provider="anthropic", model="claude-haiku-5-5"))
    tried = {task["id"]: task for task in in_batches(flow).as_dict()["tasks"]}
    assert tried["judge"]["with"]["asked"] == "batch"
    # A provider that takes none is asked at once as it is, and no other task changes.
    elsewhere = parse(_judging(provider="deepseek", model="model-a"))
    assert in_batches(elsewhere).as_dict() == elsewhere.as_dict()
    plain = BUILTIN["deliver-issue"]
    assert in_batches(plain).as_dict() == plain.as_dict()
    assert validate.errors(in_batches(flow)) == []


def _groups(*groups: tuple[str, ...], left: int = 0) -> str:
    files = [
        {
            "path": path,
            "hash": f"{number}{group}" * 20,
            "lines": 12,
            "shown": 12,
            "cut": False,
            "text": f"// {path}\nexport const value = {number};",
            "group": group,
        }
        for group, paths in enumerate(groups, 1)
        for number, path in enumerate(paths, 1)
    ]
    answer = {"files": files, "left": left, "total": 9, "fresh": 0, "gone": []}
    return _tool_log("digest", answer)


def _said(*pairs: tuple[str, str]) -> str:
    return json.dumps({"summaries": [{"path": path, "summary": text} for path, text in pairs]})


@pytest.mark.asyncio
async def test_a_run_summarises_many_groups_in_one_batch_and_comes_back_for_the_rest(
    stage: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anthropic_api, "RETRIES", 0)
    hub, clock = Hub(), Clock()
    models = Models(stage.sessions, SEALING, anthropic_http=hub.factory)
    stage.launcher.models = models
    stage.launcher.batches = Batches(models, now=clock)
    # The fixture's world has an Anthropic account already: it gets the key asked with.
    account = next(
        UUID(entry["id"])
        for entry in stage.accounts.listing()["items"]
        if entry["provider"] == "anthropic"
    )
    stage.accounts.key_stored(account, seal(SEALING, account, ANTHROPIC_KEY))
    document = summarise_code(
        provider="anthropic", model="claude-haiku-5-5", steps=3, dollars=5, batch=True
    )
    run = stage.runs.start_summaries(PROJECT, document=document, toolchain=stage.toolchain)
    await stage.launcher.tick()
    stage.fake.finish(_log([_ran("checkout", f"{BASE}\n")]))
    await stage.launcher.tick()
    await stage.launcher.tick()
    bench = _bench_pod(stage, run["id"])
    assert bench is not None
    _running(bench)

    # Three groups are picked at once, and asked about as three questions of one batch.
    stage.door.answers = [
        _groups(("src/a.ts", "src/b.ts"), ("src/c.ts",), ("src/d.ts", "src/e.ts"), left=4)
    ]
    for _pass in range(3):
        await stage.launcher.tick()
        if hub.calls("POST", "/batches"):
            break
    key, job = stage.door.handed[-1]
    assert json.loads(job["files"][1]["content"])["groups"] == 20
    picked = _steps(stage, run["id"])["pick"]
    assert picked["detail"].startswith("5 files to summarise, in 3 groups: src/a.ts")
    # The console is shown which files, in which group, and never their text.
    assert picked["output"]["files"][2] == {
        "path": "src/c.ts",
        "status": None,
        "lines": 12,
        "cut": False,
        "group": 2,
    }
    [made] = hub.calls("POST", "/batches")
    sent = made[2]["requests"]
    assert [entry["custom_id"] for entry in sent] == ["group-1", "group-2", "group-3"]
    first = sent[0]["params"]["messages"][0]["content"]
    assert "The file src/a.ts:" in first and "The file src/b.ts:" in first
    assert "src/c.ts" not in first and "src/c.ts" in sent[1]["params"]["messages"][0]["content"]
    await stage.launcher.tick()
    waiting = _steps(stage, run["id"])["summarise"]
    assert waiting["state"] == "running" and "0 of 3 answered" in (waiting["detail"] or "")

    # Two groups are answered; the third was not, before the batch ended.
    hub.end(
        **{
            "group-1": _said(("src/a.ts", "Keeps the session."), ("src/b.ts", "Prices.")),
            "group-2": _said(("src/c.ts", "The cart.")),
            "group-3": "late",
        }
    )
    clock.later(seconds=61)
    stage.door.answers = [_groups(("src/d.ts", "src/e.ts"))]
    await stage.launcher.tick()
    # The run goes straight on to pick again: the first time it summarised is looked at.
    done = next(step for step in stage.run(run["id"])["steps"] if step["name"] == "summarise")
    assert (done["state"], done["outcome"]) == ("succeeded", "summarised")
    assert done["detail"] == (
        "Summaries of 3 files were written. 1 of 3 groups got no answer and are picked again."
    )
    assert done["output"]["stored"] == 3 and list(done["output"]["not_answered"]) == ["group-3"]
    # What the two answered questions used is added up, at the batch's price.
    assert done["usage"]["tokens"] == 2 * 1060 and done["usage"]["batch"] is True
    assert done["log"].startswith(
        "--- asked anthropic with the account Anthropic one, 3 questions in a batch; 2 "
        "answered after 61 seconds ---"
    )
    assert "--- group-3 ---" in done["log"] and "--- not answered:" in done["log"]
    with stage.sessions() as session:
        kept = {row.path: row.summary for row in session.scalars(select(CodeSummary))}
    # Nothing is kept of the group that got no answer: its files are picked again.
    assert kept == {
        "src/a.ts": "Keeps the session.",
        "src/b.ts": "Prices.",
        "src/c.ts": "The cart.",
    }

    # The loop comes round: the files of the group that got no answer are asked about
    # again, in the next batch.
    for _pass in range(3):
        await stage.launcher.tick()
        if len(hub.calls("POST", "/batches")) == 2:
            break
    again = hub.calls("POST", "/batches")[1][2]["requests"]
    assert [entry["custom_id"] for entry in again] == ["group-1"]
    assert "The file src/d.ts:" in again[0]["params"]["messages"][0]["content"]

    # A question the provider rejects ends the run with the provider's words, instead of
    # going round and round.
    hub.end(**{"group-1": ("invalid_request_error", "model: nope")})
    clock.later(seconds=61)
    await stage.launcher.tick()
    ended = stage.run(run["id"])
    assert (ended["state"], ended["outcome"]) == ("failed", "invalid")
    assert "model: nope" in ended["detail"]


@pytest.mark.asyncio
async def test_several_groups_are_asked_about_in_a_batch_only(stage: Any) -> None:  # noqa: F811
    document = summarise_code(provider="deepseek", model="model-a", steps=3, dollars=5)
    run = stage.runs.start_summaries(PROJECT, document=document, toolchain=stage.toolchain)
    await stage.launcher.tick()
    stage.fake.finish(_log([_ran("checkout", f"{BASE}\n")]))
    await stage.launcher.tick()
    await stage.launcher.tick()
    bench = _bench_pod(stage, run["id"])
    assert bench is not None
    _running(bench)
    stage.door.answers = [_groups(("src/a.ts",), ("src/b.ts",))]
    for _pass in range(3):
        await stage.launcher.tick()
    ended = stage.run(run["id"])
    assert ended["state"] == "failed"
    assert "asks about in one batch only" in ended["detail"]
    assert stage.provider.asked == []


def test_the_time_a_run_waits_for_a_batch_is_not_time_it_took(world: Any) -> None:  # noqa: F811
    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    run_id = UUID(run["id"])
    now = datetime.now(UTC)
    with world.sessions.begin() as session:
        record = session.get(SandboxRun, run_id)
        assert record is not None
        record.started_at = now - timedelta(hours=10)
    assert world.runs.minutes_running(run_id) in (599, 600)

    def batch(state: str, **times: datetime) -> None:
        with world.sessions.begin() as session:
            session.add(
                ModelBatch(
                    run_id=run_id,
                    provider="anthropic",
                    model="m",
                    state=state,
                    requests=1,
                    deadline_at=now,
                    **times,
                )
            )

    # Six hours in a batch that ended, and two in one that is still out.
    batch("ended", submitted_at=now - timedelta(hours=9), ended_at=now - timedelta(hours=3))
    batch("waiting", submitted_at=now - timedelta(hours=2))
    assert world.runs.minutes_running(run_id) in (119, 120)
    # One that never reached the provider was no wait.
    batch("failed", created_at=now - timedelta(hours=10))
    assert world.runs.minutes_running(run_id) in (119, 120)
