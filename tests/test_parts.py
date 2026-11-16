"""Parts: flows that other flows run. A part is written once; each flow that runs it is
checked, tried and run with the part's tasks in place of the task that runs it."""

# Long expected texts come with what is tested here.
# ruff: noqa: E501, S105

from __future__ import annotations

import asyncio
import copy
from typing import Any
from uuid import UUID

import httpx
import pytest

from engineering_control.dashboard.project_records import ProjectInvalid
from engineering_control.dashboard.project_sections import SectionRecords
from engineering_control.engine import parts, validate
from engineering_control.engine.builtin import BUILTIN, STARTERS
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.document import parse
from engineering_control.engine.evaluations import Evaluations, sort_of
from engineering_control.engine.library import traits
from engineering_control.engine.records import FlowRecords
from engineering_control.engine.simulate import simulate
from tests.test_loop import MODEL, _answered, _choice
from tests.test_model_edits import stage  # noqa: F401 - a fixture
from tests.test_models_api import said
from tests.test_sandbox_delivery import _prepared, world  # noqa: F401 - fixtures

PROJECT = "falcone-workflow"


def _loop(**changes: Any) -> dict[str, Any]:
    """The loop as a part, with its models named."""
    raw = copy.deepcopy(STARTERS["starter-loop-part"].as_dict())
    raw.update({"key": "loop", "name": "The loop"})
    for task in raw["tasks"]:
        if task["task"].startswith("model."):
            task["with"] = {**task["with"], **MODEL}
    raw.update(changes)
    return raw


def _caller(**with_call: Any) -> dict[str, Any]:
    """Deliver issue in which the part does the work an agent did."""
    raw = copy.deepcopy(BUILTIN["deliver-issue"].as_dict())
    tasks = {task["id"]: task for task in raw["tasks"]}
    tasks["tests"]["next"] = {"passed": "work", "failed": "work"}
    tasks["review"]["with"] = {**tasks["review"].get("with", {}), "other_than": "work"}
    tasks["review"]["next"] = {"pass": "verify", "changes": "work"}
    tasks["verify"]["next"] = {**tasks["verify"]["next"], "regression": "work"}
    raw["tasks"] = [task for task in raw["tasks"] if task["id"] != "implement"]
    raw["tasks"].append(
        {
            "id": "work",
            "task": "control.part",
            "with": {"part": "loop", **with_call},
            "next": {"done": "review"},
        }
    )
    return raw


def _rules(raw: dict[str, Any]) -> list[str]:
    return [problem["text"] for problem in validate.errors(parse(raw))]


@pytest.fixture
def flows(world: Any) -> FlowRecords:  # noqa: F811
    return FlowRecords(world.sessions)


def _in_force(flows: FlowRecords, raw: dict[str, Any]) -> int:
    if flows.exists(raw["key"]):
        flows.save_draft(raw["key"], raw)
    else:
        flows.create(raw)
    return int(flows.publish(raw["key"])["version"])


# --- what a part is --------------------------------------------------------------------------


def test_a_part_is_a_flow_that_is_left_where_it_leads_nowhere() -> None:
    part = parse(_loop())
    assert part.part is True and part.as_dict()["part"] is True
    assert "part" not in BUILTIN["deliver-issue"].as_dict()
    assert parse({**_loop(), "part": False}).sha256() != part.sha256()
    # The model's ways done and other lead nowhere in it: that is how it is left.
    assert parts.exits(part) == ("done", "other")
    changes = parts.writer(part)
    assert changes is not None and changes.task == "code.apply"
    assert parts.described(part) == {
        "key": "loop",
        "name": "The loop",
        "summary": part.summary,
        "exits": ["done", "other"],
        "writes": True,
    }
    assert parts.calls(parse(_caller())) == ["loop"] and parts.calls(part) == []
    kind = TASKS["control.part"]
    assert (kind.where, kind.outcomes, [p.kind for p in kind.params]) == ("part", (), ["part"])

    # A part is held to what its tasks say, not to what a whole flow has to have: it
    # needs no clone before it and no review after it.
    assert _rules(_loop()) == []
    starter = [p["text"] for p in validate.errors(STARTERS["starter-loop-part"])]
    assert starter and all("does not name the model it asks" in text for text in starter)
    # What a part may not have.
    waits = _loop()
    waits["tasks"].append({"id": "approve", "task": "people.approval", "next": {}})
    next(t for t in waits["tasks"] if t["id"] == "decide")["next"]["done"] = "approve"
    assert any(
        "a part does not wait for the administrator and publishes nothing" in text
        for text in _rules(waits)
    )
    nested = _loop()
    nested["tasks"].append(
        {"id": "more", "task": "control.part", "with": {"part": "other"}, "next": {}}
    )
    next(t for t in nested["tasks"] if t["id"] == "decide")["next"]["other"] = "more"
    assert any("a part does not run another" in text for text in _rules(nested))
    asks = _loop()
    asks["tasks"].append(
        {
            "id": "judge",
            "task": "agent.review",
            "with": {"other_than": "apply"},
            "next": {"pass": "decide", "changes": "decide"},
        }
    )
    next(t for t in asks["tasks"] if t["id"] == "decide")["next"]["done"] = "judge"
    assert any(
        "asks about other tasks of its flow, which a part does not know" in text
        for text in _rules(asks)
    )
    closed = {
        **_loop(),
        "start": "find",
        "tasks": [{"id": "find", "task": "code.find", "next": {"found": "find", "none": "find"}}],
    }
    assert any("The part has no way out" in text for text in _rules(closed))


def test_a_flow_that_runs_a_part_is_checked_with_the_parts_tasks_in_place(
    flows: FlowRecords,
) -> None:
    # A part that is not there, or not in force yet, cannot be run.
    assert _rules(_caller()) == [
        "work runs the part loop, which is not there or has no version in force yet: put the part in force first"
    ]
    flows.create(_loop())
    assert "has no version in force yet" in _rules(_caller())[0]
    assert flows.parts_in_force() == []
    version = _in_force(flows, _loop())
    assert [entry["key"] for entry in flows.parts_in_force()] == ["loop"]

    # In force, the flow keeps every rule: the part's tasks stand where the task that
    # runs it stood, named after it, and the review judges what the part's Apply changed.
    document = parse(_caller())
    assert validate.errors(document) == []
    expanded, wrong, origin = parts.expand(document)
    assert wrong == [] and expanded.node("work") is None
    assert [node.id for node in expanded.nodes][-6:] == [
        "work-decide",
        "work-find",
        "work-read",
        "work-write",
        "work-apply",
        "work-look",
    ]
    assert set(origin.values()) == {"work"} and len(origin) == 6
    by_id = {node.id: node for node in expanded.nodes}
    # What led to the part leads to its first task; its way out leads where the flow says.
    assert by_id["tests"].next == {"passed": "work-decide", "failed": "work-decide"}
    assert by_id["review"].next["changes"] == "work-decide"
    assert by_id["work-decide"].next == {
        "search": "work-find",
        "read": "work-read",
        "edit": "work-write",
        "check": "work-look",
        "done": "review",
    }
    assert by_id["work-apply"].next == {"applied": "work-decide", "refused": "work-decide"}
    assert by_id["review"].params["other_than"] == "work-apply"
    assert expanded.part is False and expanded.key == "deliver-issue"
    # A flow that runs no part is what it was.
    same, _wrong, none = parts.expand(BUILTIN["deliver-issue"])
    assert same is BUILTIN["deliver-issue"] and none == {}
    # What kind of flow it is is read from what runs: the part changes code.
    assert traits(document)["writes"] is True and sort_of(document) == "item"
    assert simulate(document, outcomes={})["ends"]["how"] in ("done", "ended", "failed")

    # What is wrong with a task of the part where it runs is said of the task that runs
    # it: here the flow clones nothing before the part looks at the code.
    bare = _caller()
    bare["tasks"] = [task for task in bare["tasks"] if task["id"] != "prepare"]
    bare["start"] = "install"
    found = validate.errors(parse(bare))
    inside = [problem for problem in found if problem["text"].startswith("work-find ")]
    assert inside and inside[0]["where"] == "work"
    assert "needs checkout, which no task before it produces" in inside[0]["text"]
    # A part with an error cannot be put in force, so none that is run has one.
    careless = _loop()
    next(t for t in careless["tasks"] if t["id"] == "write")["with"]["model"] = ""
    flows.save_draft("loop", careless)
    with pytest.raises(ProjectInvalid, match="write does not name the model it asks"):
        flows.publish("loop")
    flows.discard_draft("loop")

    # How a part cannot be run.
    for change, why in (
        (
            {"with": {"part": "deliver-issue"}},
            "work runs Deliver issue, which is a flow of its own, not a part",
        ),
        ({"with": {"part": ""}}, "work does not say which part it runs"),
        (
            {"next": {"done": "review", "failed": "review"}},
            "work leads on from failed, which The loop does not end with. It ends with: done, other",
        ),
    ):
        raw = _caller()
        next(t for t in raw["tasks"] if t["id"] == "work").update(change)
        assert why in _rules(raw), why
    long = _caller()
    for task in long["tasks"]:
        if task["id"] == "work":
            task["id"] = "the-work-that-the-model-does-in-its-loop"
        task["next"] = {
            k: ("the-work-that-the-model-does-in-its-loop" if v == "work" else v)
            for k, v in task["next"].items()
        }
        if task.get("with", {}).get("other_than") == "work":
            task["with"]["other_than"] = "the-work-that-the-model-does-in-its-loop"
    assert any("is too long a name for the tasks of The loop" in text for text in _rules(long))
    first = _caller()
    first["start"] = "work"
    assert any("The flow starts with the part The loop" in text for text in _rules(first))
    # A part in which nothing changes the code has no work for a review to judge.
    reading = _loop(key="look-around", name="Look around")
    reading["tasks"] = [t for t in reading["tasks"] if t["id"] not in ("write", "apply")]
    next(t for t in reading["tasks"] if t["id"] == "decide")["next"].pop("edit")
    _in_force(flows, reading)
    assert any(
        "review judges the work of work, a part in which no one task changes the code" in text
        for text in _rules(_caller(part="look-around"))
    )
    assert version == 1


# --- a run -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_goes_through_the_parts_tasks_and_keeps_the_version_it_started_with(
    stage: Any,  # noqa: F811
) -> None:
    flows = FlowRecords(stage.sessions)
    first = _in_force(flows, _loop())
    _in_force(flows, _caller())
    stage.provider.answers = [said(_choice("done", "Nothing to change."))]
    run = stage.runs.start_delivery(stage.item_id, toolchain=stage.toolchain)
    assert stage.run(run["id"])["data"]["flow"]["parts"] == {"loop": first}
    await stage.step("prepare", _prepared())
    for _pass in range(4):
        await _answered(stage)
        if len(stage.run(run["id"])["steps"]) > 4:
            break
    names = [step["name"] for step in stage.run(run["id"])["steps"]]
    # The part's task is a step of the run, named after the task that runs the part.
    assert names[:4] == ["prepare", "install", "tests", "work-decide"]
    decided = next(s for s in stage.run(run["id"])["steps"] if s["name"] == "work-decide")
    assert (decided["state"], decided["outcome"]) == ("succeeded", "chose_done")
    assert names[4] == "review", "the way out of the part leads where the flow says"
    question = stage.provider.asked[-1][2]["messages"][1]["content"]
    assert "Resolve what the run is about" in question

    # The part is changed and put in force while the run is under way: the run keeps the
    # part it started with, and the next run has the new one.
    changed = _loop()
    next(t for t in changed["tasks"] if t["id"] == "decide")["with"]["instruction"] = (
        "Do it the new way."
    )
    second = _in_force(flows, changed)
    assert second == first + 1
    engine = (
        stage.launcher.flows["flow"]
        if "flow" in stage.launcher.flows
        else next(iter(stage.launcher.flows.values()))
    )
    with stage.sessions() as session:
        from engineering_control.db.models import SandboxRun

        data = session.get(SandboxRun, UUID(run["id"])).data
    pinned = parts.expand(parse(_caller()), pinned=data["flow"]["parts"])[0]
    assert (
        pinned.node("work-decide").params["instruction"].startswith("Resolve what the run is about")
    )  # type: ignore[union-attr]
    assert (
        parts.expand(parse(_caller()))[0].node("work-decide").params["instruction"]
        == "Do it the new way."
    )  # type: ignore[union-attr]
    assert engine is not None
    stage.runs.request_cancel(UUID(run["id"]))
    await stage.launcher.tick()
    stage.fake.pods.clear()
    again = stage.runs.start_delivery(stage.item_id, toolchain=stage.toolchain)
    assert stage.run(again["id"])["data"]["flow"]["parts"] == {"loop": second}
    await stage.http.aclose()
    await stage.github.aclose()


def test_a_part_runs_only_inside_the_flows_that_run_it(world: Any, flows: FlowRecords) -> None:  # noqa: F811
    _in_force(flows, _loop())
    _in_force(flows, _caller())
    # A section cannot have it, and the lab does not try it by itself.
    sections = SectionRecords(world.sessions)
    with pytest.raises(
        ProjectInvalid, match="That is a part: it runs only inside the flows that run it"
    ):
        sections.assign(PROJECT, "issues", "loop")
    lab = Evaluations(world.sessions, world.runs)
    assert lab.sources("loop") == []
    with pytest.raises(ProjectInvalid, match="A part does not run by itself"):
        lab.start("loop", version=1, run_ids=[UUID(int=1)])
    summary = {row["key"]: row for row in flows.summary()}
    assert summary["loop"]["part"] is True and summary["deliver-issue"]["part"] is False
    assert flows.runs_part("loop") == [{"flow": "deliver-issue", "name": "Deliver issue"}]
    assert flows.shown("loop", 1)["exits"] == ["done", "other"]
    assert (
        flows.shown("deliver-issue", flows.version_in_force("deliver-issue") or 0)["exits"] is None
    )


# --- the console -----------------------------------------------------------------------------


def test_the_console_makes_parts_and_offers_them_to_the_designer(world: Any) -> None:  # noqa: F811
    from fastapi.testclient import TestClient

    from engineering_control.config import Settings
    from engineering_control.dashboard.app import create_app
    from engineering_control.dashboard.repository import DashboardRepository

    password = "correct horse battery staple"
    repository = DashboardRepository(str(world.sessions.kw["bind"].url))
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

        catalogue = client.get("/api/dashboard/flows/catalogue").json()
        assert catalogue["parts"] == []
        assert [(s["key"], s["part"]) for s in catalogue["starters"]] == [
            ("starter-model-pipeline", False),
            ("starter-model-loop", False),
            ("starter-loop-part", True),
        ]
        kinds = {task["key"]: task for task in catalogue["tasks"]}
        assert (
            kinds["control.part"]["where"] == "part"
            and kinds["control.part"]["params"][0]["kind"] == "part"
        )

        # A part with one task to build on, and the loop to begin from.
        empty = post("/flows", key="my-part", name="My part", summary="", part=True)
        assert empty.status_code == 201, empty.text
        assert empty.json()["document"]["part"] is True and empty.json()["exits"] == [
            "found",
            "none",
        ]
        made = post(
            "/flows", key="the-loop", name="The loop", summary="", copy_of="starter-loop-part"
        )
        assert made.status_code == 201, made.text
        document = made.json()["document"]
        assert document["part"] is True and made.json()["exits"] == ["done", "other"]
        for task in document["tasks"]:
            if task["task"].startswith("model."):
                task["with"] = {**task["with"], **MODEL}
        assert post("/flows/the-loop/draft", document=document).status_code == 200
        assert client.get("/api/dashboard/flows/catalogue").json()["parts"] == [], (
            "a draft is not offered"
        )
        assert post("/flows/the-loop/publish", current_password=password).status_code == 200
        offered = client.get("/api/dashboard/flows/catalogue").json()["parts"]
        assert offered == [
            {
                "key": "the-loop",
                "name": "The loop",
                "summary": document["summary"],
                "exits": ["done", "other"],
                "writes": True,
            }
        ]
        listed = {flow["key"]: flow for flow in client.get("/api/dashboard/flows").json()["items"]}
        assert listed["the-loop"]["engine"]["part"] is True

        # A flow that runs it is checked as it runs.
        caller = _caller(part="the-loop")
        checked = post("/flows/check", document=caller)
        assert (
            checked.status_code == 200
            and [p for p in checked.json()["problems"] if p["level"] == "error"] == []
        )
        wrong = _caller(part="no-such-part")
        problems = post("/flows/check", document=wrong).json()["problems"]
        assert [(p["rule"], p["where"]) for p in problems if p["level"] == "error"] == [
            ("part", "work")
        ]
        refused = post(f"/projects/{PROJECT}/sections/issues/flow", flow="the-loop")
        assert refused.status_code == 422 and "That is a part" in refused.json()["detail"]
    asyncio.run(world.github.aclose())
