"""The evaluation lab across flows: a flow in which a model asked over an API does the
work, tried on the past runs of the flow in which an agent in a sandbox did it."""

# Long expected texts come with what is tested here.
# ruff: noqa: E501, S105

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import httpx
import pytest

from engineering_control.dashboard.project_records import ProjectInvalid, ProjectNotFound
from engineering_control.engine.builtin import BUILTIN, STARTERS
from engineering_control.engine.evaluations import Evaluations, in_common, sort_of
from engineering_control.engine.records import FlowRecords
from engineering_control.sandbox import pods
from tests.test_flow_lab import _delivered, _raw, _task
from tests.test_model_edits import (  # noqa: F401 - ``stage`` is a fixture
    EDITS,
    _applied,
    _editing,
    stage,
)
from tests.test_models_api import said
from tests.test_sandbox_delivery import (  # noqa: F401 - ``world`` is a fixture
    BUNDLE,
    PASSED,
    _log,
    _prepared,
    _ran,
    _reviewed,
    _verified,
    world,
)
from tests.test_workbench import _bench_pod, _found, _read, _running

FILE = "packages/billing/src/money.ts"


def test_flows_are_of_two_sorts_and_only_one_sort_is_put_next_to_each_other() -> None:
    sorts = {key: sort_of(flow) for key, flow in BUILTIN.items()}
    assert sorts.pop("deliver-issue") == "item"
    assert sorts["review-evidence"] == "review"
    assert sum(sort == "review" for sort in sorts.values()) == 9
    assert sum(sort == "section" for sort in sorts.values()) == 5
    assert set(sorts.values()) == {"review", "section"}
    # A flow in which a model writes the change works on an item like Deliver issue.
    assert {sort_of(flow) for flow in STARTERS.values()} == {"item"}
    # Files both changes touch are counted only when both changed any.
    one, other = {"paths": ["a.py", "b.py"]}, {"paths": ["b.py", "c.py", "d.py"]}
    assert in_common(one, other) == 1 and in_common(one, {"paths": []}) is None
    assert in_common(None, other) is None and in_common(one, one) == 2


async def _sandbox(stage: Any, name: str, log: str) -> None:  # noqa: F811
    """End the sandbox of the step that is running, beside the run's workbench."""
    await stage.launcher.tick()
    (pod,) = [
        pod
        for pod in stage.fake.pods.values()
        if not pod["metadata"]["name"].startswith(pods.BENCH_PREFIX)
    ]
    assert pod["metadata"]["labels"]["engineering-control/step"] == name
    pod["status"] = {"phase": "Succeeded"}
    stage.fake.logs[pod["metadata"]["name"]] = log
    await stage.launcher.tick()


@pytest.mark.asyncio
async def test_a_flow_a_model_writes_in_is_tried_on_the_runs_of_the_flow_an_agent_wrote_in(
    stage: Any,  # noqa: F811
) -> None:
    flows = FlowRecords(stage.sessions)
    lab = Evaluations(stage.sessions, stage.runs)
    # What happened: Deliver issue, an agent in a sandbox, two rounds to get through.
    past = await _delivered(stage, rounds=2)
    assert past["state"] == "waiting"
    stage.fake.pods.clear()

    # The other way of doing it, as a flow of its own that has never been in force.
    harness = {
        **_editing(),
        "key": "model-delivery",
        "name": "Deliver, written by a model",
        "summary": "A model asked over an API writes the change.",
    }
    draft = flows.create(harness)["version"]
    account = next(a for a in stage.accounts.listing()["items"] if a["provider"] == "deepseek")
    stage.accounts.update(UUID(account["id"]), prices={"model-a": {"input": 100, "output": 400}})

    # It can be tried on its own runs, of which there are none, and on Deliver issue's.
    assert lab.sources("model-delivery") == [
        {"key": "model-delivery", "name": "Deliver, written by a model", "runs": 0},
        {"key": "deliver-issue", "name": "Deliver issue", "runs": 1},
    ]
    # A flow started for a section has no runs to share with one that works on an item.
    assert [source["key"] for source in lab.sources("audit-code")] == ["audit-code"]
    assert [source["key"] for source in lab.sources("deliver-issue")] == ["deliver-issue"]
    assert lab.candidates("model-delivery") == []
    (offered,) = lab.candidates("model-delivery", of="deliver-issue")
    assert offered["run_id"] == past["id"]

    # What is refused.
    chosen = [UUID(past["id"])]
    with pytest.raises(ProjectInvalid, match="not a run of this flow"):
        lab.start("model-delivery", version=draft, run_ids=chosen)
    audit = _raw("audit-code")
    _task(audit, "verify")["with"]["rounds"] = 1
    with pytest.raises(
        ProjectInvalid,
        match="Audit code is started for a section and Deliver issue works on an item: "
        "the runs of one say nothing of the other",
    ):
        lab.start(
            "audit-code",
            version=flows.save_draft("audit-code", audit)["version"],
            run_ids=chosen,
            of="deliver-issue",
        )
    with pytest.raises(ProjectNotFound):
        lab.start("model-delivery", version=draft, run_ids=chosen, of="no-such-flow")
    with pytest.raises(ProjectInvalid, match="has never been in force"):
        lab.start("deliver-issue", version=0, run_ids=chosen, of="model-delivery")

    started = lab.start(
        "model-delivery", version=draft, run_ids=chosen, of="deliver-issue", note="A model writes"
    )
    assert (started["flow"], started["of"], started["of_name"]) == (
        "model-delivery",
        "deliver-issue",
        "Deliver issue",
    )
    assert (started["state"], started["candidate_version"], started["baseline_version"]) == (
        "running",
        draft,
        0,
    )

    # The case goes through the same words at the same commit, by the other flow.
    created_before = dict(stage.hub.created)
    await stage.launcher.tick()
    (case,) = lab.get(UUID(started["id"]))["items"]
    trial = stage.run(case["candidate"]["run_id"])
    assert trial["kind"] == "evaluation" and trial["data"]["flow"]["key"] == "model-delivery"
    stage.fake.finish(_prepared())
    await stage.launcher.tick()
    await stage.step("install", _prepared())
    await stage.step("tests", _prepared())
    await stage.launcher.tick()
    bench = _bench_pod(stage, trial["id"])
    assert bench is not None
    _running(bench)
    stage.provider.answers = [said(EDITS)]
    stage.door.answers = [_found(FILE), _read(FILE)]
    await stage.launcher.tick()  # find and read; the model is asked
    stage.door.answers = [_applied(FILE, "packages/billing/src/money.test.ts")]
    await asyncio.wait(list(stage.launcher._calls.values()), timeout=10)
    await stage.launcher.tick()  # the edits are written and applied
    question = stage.provider.asked[-1][2]["messages"][1]["content"]
    assert "Steps: sign in, wait, reload." in question, "the issue as it read"
    await _sandbox(stage, "review", _reviewed(PASSED, provider="chatgpt"))
    await _sandbox(stage, "verify", _verified())
    await _sandbox(stage, "scan", _verified())
    await _sandbox(stage, "bundle", _log([_ran("bundle", "2 files")], artifact=BUNDLE))
    await stage.launcher.tick()
    ended = stage.run(trial["id"])
    assert (ended["state"], ended["outcome"]) == ("succeeded", "evaluated")
    assert stage.hub.created == created_before, "nothing is published"

    await stage.launcher.tick()
    result = lab.get(UUID(started["id"]))
    assert (result["state"], result["done"], result["of"]) == ("finished", 1, "deliver-issue")
    (case,) = result["items"]
    as_it_went, tried = case["baseline"], case["candidate"]
    # The run as it went is the past run itself, by the flow it followed.
    assert as_it_went["run_id"] == past["id"] and tried["run_id"] == trial["id"]
    assert (as_it_went["rounds"], tried["rounds"]) == (2, 1)
    assert as_it_went["through"] and tried["through"] and case["verdict"] == "better"
    # What each way used: an agent's work costs no money here, a model's questions do.
    assert (as_it_went["asked"], as_it_went["cost"]) == (0, 0)
    assert (tried["asked"], tried["cost"]) == (1, 0.152)
    assert tried["tokens"] >= 1280 and tried["steps"] >= 10 and as_it_went["steps"] >= 8
    assert tried["elapsed"] >= 0 and as_it_went["elapsed"] >= 0
    # Both changes touch the same two files; the list itself is not sent.
    assert case["common"] == 2 and (as_it_went["files"], tried["files"]) == (2, 2)
    assert "paths" not in tried and "paths" not in as_it_went
    totals = result["totals"]
    assert (totals["candidate"]["cost"], totals["candidate"]["asked"]) == (0.152, 1)
    assert (totals["baseline"]["cost"], totals["baseline"]["asked"]) == (0, 0)
    assert totals["baseline"]["rounds"] == 2 and totals["candidate"]["rounds"] == 1
    # It is an evaluation of the flow tried, not of the flow whose runs it used.
    assert [row["id"] for row in lab.listing("model-delivery")] == [started["id"]]
    assert lab.listing("deliver-issue") == []
    assert ("model-delivery", draft, True) in {
        (f["key"], f["version"], f["evaluation"]) for f in stage.runs.usage_totals()["flows"]
    }
    await stage.http.aclose()
    await stage.github.aclose()


def test_the_console_offers_the_runs_of_another_flow_and_starts_on_them(stage: Any) -> None:  # noqa: F811
    from fastapi.testclient import TestClient

    from engineering_control.config import Settings
    from engineering_control.dashboard.app import create_app
    from engineering_control.dashboard.repository import DashboardRepository

    past = asyncio.run(_delivered(stage))
    password = "correct horse battery staple"
    repository = DashboardRepository(str(stage.sessions.kw["bind"].url))
    repository.initialize_password_sync(password)
    client = TestClient(
        create_app(
            settings=Settings(database_url=str(repository.engine.url)),
            repository=repository,
            session_key=b"g" * 48,
        ),
        base_url="https://ade.musematic.ai",
        headers={"Origin": "https://ade.musematic.ai"},
    )
    with client:
        token = client.get("/login/challenge").json()["csrf_token"]
        client.post("/login", data={"csrf_token": token, "password": password})

        def post(path: str, **fields: Any) -> httpx.Response:
            csrf = client.get("/api/dashboard/session").json()["csrf_token"]
            return client.post(f"/api/dashboard{path}", json={"csrf_token": csrf, **fields})

        made = post(
            "/flows",
            key="my-pipeline",
            name="My pipeline",
            summary="",
            copy_of="starter-model-pipeline",
        )
        assert made.status_code == 201, made.text
        document = client.get("/api/dashboard/flows/my-pipeline").json()["draft"]["document"]
        for task in document["tasks"]:
            if task["task"].startswith("model."):
                task["with"] = {**task["with"], "model": "model-a"}
        saved = post("/flows/my-pipeline/draft", document=document)
        assert saved.status_code == 200, saved.text
        version = saved.json()["version"]

        # A flow with no runs of its own is shown the runs of the flow that has some.
        lab = client.get("/api/dashboard/flows/my-pipeline/evaluations").json()
        assert lab["sources"] == [
            {"key": "my-pipeline", "name": "My pipeline", "runs": 0},
            {"key": "deliver-issue", "name": "Deliver issue", "runs": 1},
        ]
        assert lab["of"] == "deliver-issue" and [c["run_id"] for c in lab["candidates"]] == [
            past["id"]
        ]
        own = client.get("/api/dashboard/flows/my-pipeline/evaluations?of=my-pipeline").json()
        assert own["of"] == "my-pipeline" and own["candidates"] == []
        # A flow that is not one of them is not taken at its word.
        other = client.get("/api/dashboard/flows/my-pipeline/evaluations?of=audit-code").json()
        assert other["of"] == "deliver-issue"
        # Deliver issue is shown its own runs, as before.
        theirs = client.get("/api/dashboard/flows/deliver-issue/evaluations").json()
        assert theirs["of"] == "deliver-issue" and len(theirs["candidates"]) == 1

        wrong = post("/flows/my-pipeline/evaluations", version=version, run_ids=[past["id"]])
        assert wrong.status_code == 422 and "not a run of this flow" in wrong.json()["detail"]
        started = post(
            "/flows/my-pipeline/evaluations",
            version=version,
            run_ids=[past["id"]],
            of="deliver-issue",
            note="Against the agent",
        )
        assert started.status_code == 201, started.text
        body = started.json()
        assert (body["flow"], body["of"], body["of_name"]) == (
            "my-pipeline",
            "deliver-issue",
            "Deliver issue",
        )
        shown = client.get(f"/api/dashboard/evaluations/{body['id']}").json()
        assert (
            shown["of"] == "deliver-issue" and shown["items"][0]["baseline"]["run_id"] == past["id"]
        )
        assert shown["items"][0]["common"] is None, "nothing to compare before the case has run"
        assert post(f"/evaluations/{body['id']}/stop").status_code == 200
    asyncio.run(stage.http.aclose())
    asyncio.run(stage.github.aclose())
