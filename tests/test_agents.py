"""Agents and skills: who does a task, defined once and named by the tasks that use it."""

# Long expected texts come with what is tested here.
# ruff: noqa: E501, S105

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from engineering_control.config import Settings
from engineering_control.dashboard.app import create_app
from engineering_control.dashboard.project_records import ProjectConflict, ProjectInvalid
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.base import Base
from engineering_control.engine import validate
from engineering_control.engine.agents import Agents, clean_settings, told
from engineering_control.engine.builtin import BUILTIN, STARTERS
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.document import parse
from engineering_control.engine.records import FlowRecords
from engineering_control.engine.routing import provider_for
from tests.test_dashboard_accounts import PASSWORD
from tests.test_model_edits import stage  # noqa: F401 - a fixture
from tests.test_models_api import said
from tests.test_sandbox_delivery import (  # noqa: F401 - ``world`` is a fixture
    PASSED,
    _implemented,
    _prepared,
    world,
)

MAKER = "You make changes to code. Read before you write, and test what you change."


# --- the records -----------------------------------------------------------------------------


@pytest.fixture
def agents(world: Any) -> Agents:  # noqa: F811
    return Agents(world.sessions, FlowRecords(world.sessions))


def test_how_an_agent_works_is_checked_and_what_is_left_out_stays_open() -> None:
    assert (
        clean_settings(None) == {}
        and clean_settings({"providers": [], "models": {}, "api": {}}) == {}
    )
    full = clean_settings(
        {
            "providers": ["claude", "chatgpt"],
            "models": {
                "claude": {"model": "claude-opus-5-5", "effort": "high"},
                "chatgpt": {"model": ""},
            },
            "minutes": 30,
            "api": {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "then_provider": "kimi",
                "then_model": "kimi-k2",
                "effort": "high",
            },
        }
    )
    assert full == {
        "providers": ["claude", "chatgpt"],
        "models": {"claude": {"model": "claude-opus-5-5", "effort": "high"}},
        "minutes": 30,
        "api": {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "then_provider": "kimi",
            "then_model": "kimi-k2",
            "effort": "high",
        },
    }
    assert clean_settings({"api": {"provider": "none", "model": "x"}}) == {}
    for wrong, why in (
        ({"providers": ["deepseek"]}, "subscriptions are Claude and ChatGPT"),
        ({"providers": ["claude", "claude"]}, "each subscription once"),
        ({"models": {"deepseek": {"model": "x"}}}, "for Claude and for ChatGPT"),
        ({"models": {"claude": {"model": "two words"}}}, "model"),
        ({"minutes": 3}, "between 5 and 55 minutes"),
        ({"minutes": "long"}, "between 5 and 55 minutes"),
        ({"api": {"provider": "claude", "model": "x"}}, "not a provider a model is asked at"),
        ({"api": {"provider": "glm", "model": ""}}, "Name the model"),
        (
            {"api": {"then_provider": "glm", "then_model": "glm-5"}},
            "A second provider needs a first",
        ),
        (
            {"api": {"provider": "glm", "model": "glm-5", "effort": "extreme"}},
            "not a level of reasoning",
        ),
    ):
        with pytest.raises(ProjectInvalid, match=why):
            clean_settings(wrong)


def test_agents_and_skills_are_added_changed_and_removed(agents: Agents) -> None:
    testing = agents.save_skill(
        None,
        name="  How we  test ",
        description="The test conventions",
        text="Tests sit beside the code.",
    )
    assert testing["name"] == "How we test" and testing["text"] == "Tests sit beside the code."
    naming = agents.save_skill(
        None, name="Naming", description=None, text="Names say what a thing is."
    )
    with pytest.raises(ProjectConflict, match="There is a skill how we TEST already"):
        agents.save_skill(None, name="how we TEST", description=None, text="x")
    with pytest.raises(ProjectInvalid, match="Write what the skill says"):
        agents.save_skill(None, name="Empty", description=None, text="  ")

    maker = agents.save_agent(
        None,
        name="Maker",
        description="Changes the code",
        instructions=MAKER,
        settings={"providers": ["claude"], "minutes": 20},
        skills=[testing["id"], naming["id"]],
    )
    assert maker["skill_names"] == ["How we test", "Naming"] and maker["settings"] == {
        "providers": ["claude"],
        "minutes": 20,
    }
    for wrong, why in (
        ({"name": "maker"}, "There is an agent maker already"),
        ({"name": "  "}, "Give the agent a name"),
        ({"instructions": ""}, "Write the agent's instructions"),
        ({"skills": [str(uuid4())]}, "One of the skills is not there any more"),
        ({"skills": ["not an id"]}, "That is not a skill"),
        ({"skills": [testing["id"]] * 9}, "8 skills at most"),
    ):
        fields = {
            "name": "Other",
            "description": None,
            "instructions": "x",
            "settings": {},
            "skills": [],
            **wrong,
        }
        with pytest.raises((ProjectConflict, ProjectInvalid), match=why):
            agents.save_agent(None, **fields)

    # What a task that names the agent is given: what it is told, whole, and a hash of it.
    given = agents.get(maker["id"])
    assert given is not None
    assert given["told"] == (
        "Who does this work, as the administrator set it: the agent Maker. What follows holds for every "
        "task it is given, beside what the task itself says below.\n\n"
        f"{MAKER}\n\n"
        "A skill it has, How we test:\nTests sit beside the code.\n\n"
        "A skill it has, Naming:\nNames say what a thing is."
    )
    assert given["told"] == told(
        "Maker",
        MAKER,
        [
            {"name": "How we test", "text": "Tests sit beside the code."},
            {"name": "Naming", "text": "Names say what a thing is."},
        ],
    )
    assert (
        len(given["sha256"]) == 64
        and agents.get("not an id") is None
        and agents.get(str(uuid4())) is None
    )
    assert agents.of([maker["id"], "", str(uuid4())]).keys() == {maker["id"]}

    # Changing a skill changes what every agent that has it is told.
    agents.save_skill(
        UUID(testing["id"]), name="How we test", description=None, text="Tests sit in tests/."
    )
    changed = agents.get(maker["id"])
    assert (
        changed is not None
        and "Tests sit in tests/." in changed["told"]
        and changed["sha256"] != given["sha256"]
    )
    renamed = agents.save_agent(
        UUID(maker["id"]),
        name="Builder",
        description=None,
        instructions=MAKER,
        settings={},
        skills=[naming["id"]],
    )
    assert (
        renamed["name"] == "Builder"
        and renamed["skill_names"] == ["Naming"]
        and renamed["settings"] == {}
    )

    listing = agents.listing()
    assert [entry["name"] for entry in listing["agents"]] == ["Builder"]
    assert {entry["name"]: entry["agents"] for entry in listing["skills"]} == {
        "How we test": [],
        "Naming": ["Builder"],
    }
    assert agents.names() == [{"id": maker["id"], "name": "Builder"}]
    # A skill that is removed is taken from the agents that had it.
    agents.remove_skill(UUID(naming["id"]))
    assert agents.listing()["agents"][0]["skills"] == []
    gone = agents.remove_agent(UUID(maker["id"]))
    assert gone["name"] == "Builder" and agents.listing()["agents"] == []


# --- what a task makes of an agent -----------------------------------------------------------


def test_every_task_an_agent_or_a_model_works_in_can_name_an_agent() -> None:
    naming = sorted(
        key for key, task in TASKS.items() if any(param.kind == "agent" for param in task.params)
    )
    assert naming == sorted(
        key for key, task in TASKS.items() if task.account or task.where == "model"
    )
    assert {"agent.implement", "agent.review", "agent.change", "model.choose", "model.edit"} <= set(
        naming
    )
    assert "workspace.clone" not in naming and "code.apply" not in naming

    raw = copy.deepcopy(BUILTIN["deliver-issue"].as_dict())
    by_id = {task["id"]: task for task in raw["tasks"]}
    by_id["implement"]["with"] = {**by_id["implement"].get("with", {}), "agent": str(uuid4())}
    assert validate.errors(parse(raw)) == []
    by_id["implement"]["with"]["agent"] = "the maker"
    assert any(
        "agent of implement names one of your agents" in p["text"]
        for p in validate.errors(parse(raw))
    )

    # What the task leaves open comes from its agent; what holds for every flow does not.
    document = parse(copy.deepcopy(BUILTIN["deliver-issue"].as_dict()))
    implement, review = document.node("implement"), document.node("review")
    assert implement is not None and review is not None
    claude_only = {"a": {"settings": {"providers": ["claude"]}}}
    named = parse(
        {
            **document.as_dict(),
            "tasks": [
                {
                    **t,
                    "with": {
                        **{k: v for k, v in t.get("with", {}).items() if k != "providers"},
                        "agent": "a",
                    },
                }
                if t["id"] in ("implement", "review")
                else t
                for t in document.as_dict()["tasks"]
            ],
        }
    )
    implement, review = named.node("implement"), named.node("review")
    assert implement is not None and review is not None
    assert provider_for(implement, {}, again=False) == "chatgpt,claude"
    # A task that says its subscriptions itself keeps them, whatever its agent prefers.
    theirs = document.node("implement")
    assert theirs is not None and theirs.params["providers"] == ["chatgpt", "claude"]
    assert provider_for(theirs, {}, again=False, agents=claude_only) == "chatgpt,claude"
    assert provider_for(implement, {}, again=False, agents=claude_only) == "claude"
    # A reviewer that prefers Claude is still not given what Claude wrote.
    assert (
        provider_for(review, {"implement": "claude"}, again=False, agents=claude_only) == "chatgpt"
    )
    assert (
        provider_for(review, {"implement": "chatgpt"}, again=False, agents=claude_only) == "claude"
    )

    # A task that asks a model may leave its model to its agent.
    loop = STARTERS["starter-model-loop"].as_dict()
    assert any("does not name the model" in p["text"] for p in validate.errors(parse(loop)))
    for task in loop["tasks"]:
        if task["task"].startswith("model."):
            task["with"] = {**task.get("with", {}), "agent": str(uuid4())}
    assert validate.errors(parse(loop)) == []


def test_two_flows_to_begin_from_have_a_model_do_the_work_and_keep_every_rule() -> None:
    assert list(STARTERS) == ["starter-model-pipeline", "starter-model-loop", "starter-loop-part"]
    assert not set(STARTERS) & set(BUILTIN), "they are not flows that run as they are"
    # The third is the loop by itself, as a part: see the tests of parts.
    for flow in [flow for flow in STARTERS.values() if not flow.part]:
        problems = [problem["text"] for problem in validate.errors(flow)]
        # All that is missing is what a starter cannot know: which model to ask.
        assert problems and all("does not name the model it asks" in text for text in problems), (
            problems
        )
        kinds = [node.task for node in flow.nodes]
        assert "agent.implement" not in kinds and "code.apply" in kinds and "agent.review" in kinds
        assert {
            "check.tests",
            "check.secrets",
            "change.collect",
            "people.approval",
            "github.pull_request",
        } <= set(kinds)
        review = flow.node("review")
        assert review is not None and review.params["other_than"] == "apply"
    loop = STARTERS["starter-model-loop"]
    decide = loop.node("decide")
    assert decide is not None and decide.next == {
        "search": "find",
        "read": "read",
        "edit": "write",
        "check": "look",
        "done": "review",
    }
    assert all(
        node.next and set(node.next.values()) == {"decide"}
        for node in loop.nodes
        if node.id in ("find", "read", "look", "apply")
    )


# --- a run -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_agent_in_a_sandbox_is_told_who_it_is_and_works_as_it_is_set(
    world: Any,  # noqa: F811
    agents: Agents,
) -> None:
    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    skill = agents.save_skill(
        None, name="How we test", description=None, text="Run pnpm test before you say done."
    )
    maker = agents.save_agent(
        None,
        name="Maker",
        description=None,
        instructions=MAKER,
        settings={
            "providers": ["claude"],
            "models": {"claude": {"model": "claude-opus-5-5", "effort": "high"}},
            "minutes": 20,
        },
        skills=[skill["id"]],
    )
    raw = copy.deepcopy(BUILTIN["deliver-issue"].as_dict())
    by_id = {task["id"]: task for task in raw["tasks"]}
    # The task leaves its subscriptions open: they are its agent's to say.
    by_id["implement"]["with"] = {"agent": maker["id"]}
    flows = FlowRecords(world.sessions)
    flows.save_draft("deliver-issue", raw)
    flows.publish("deliver-issue")
    assert agents.listing()["agents"][0]["used_by"] == [
        {"flow": "deliver-issue", "name": "Deliver issue", "tasks": ["implement"], "draft": False}
    ]
    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    await world.step("prepare", _prepared())
    await world.launcher.tick()
    # The agent's subscription, model and time, since the task says none of its own.
    pod = world.fake.only_pod()
    assert pod["metadata"]["labels"]["engineering-control/step"] == "implement"
    step = world.run(run["id"])["steps"][-1]
    assert step["provider"] == "claude"
    command = next(c for c in world.job()["commands"] if c["name"] == "agent")
    assert (
        command["argv"][0] == "claude"
        and "claude-opus-5-5" in command["argv"]
        and command["timeout"] == 20 * 60
    )
    # What it is told comes before what the task says, and the platform's rules stay.
    prompt = command["stdin"]
    assert prompt.startswith("Who does this work, as the administrator set it: the agent Maker.")
    assert (
        MAKER in prompt
        and "A skill it has, How we test:\nRun pnpm test before you say done." in prompt
    )
    assert prompt.index(MAKER) < prompt.index("Signing in fails with an expired cookie")
    assert "Never change files under .github/" in prompt
    # Which agent it was, and a hash of what it was told, is kept with the step.
    noted = step["input"]["prompt"]
    assert (
        noted["agent"] == "Maker"
        and len(noted["agent_sha256"]) == 64
        and noted["name"] == "implementer"
    )

    # An agent that is gone fails the run where it is named, and says what to do.
    world.fake.finish(_implemented("claude"))
    world.runs.request_cancel(UUID(run["id"]))
    await world.launcher.tick()
    world.fake.pods.clear()
    agents.remove_agent(UUID(maker["id"]))
    again = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    await world.step("prepare", _prepared())
    await world.launcher.tick()
    ended = world.run(again["id"])
    assert (ended["state"], ended["outcome"]) == ("failed", "not_started")
    assert (
        ended["detail"]
        == "implement names an agent that is not there any more: choose another in the flow, or none"
    )
    await world.github.aclose()


@pytest.mark.asyncio
async def test_an_agent_asked_over_an_api_brings_its_model_and_what_it_is_told(stage: Any) -> None:  # noqa: F811
    agents = Agents(stage.sessions)
    judge = agents.save_agent(
        None,
        name="Second opinion",
        description=None,
        instructions="You judge whether an issue is clear enough to work on.",
        settings={"api": {"provider": "deepseek", "model": "model-from-the-agent"}},
        skills=[],
    )
    silent = agents.save_agent(
        None, name="No model", description=None, instructions="x", settings={}, skills=[]
    )
    raw = copy.deepcopy(BUILTIN["deliver-issue"].as_dict())
    by_id = {task["id"]: task for task in raw["tasks"]}
    by_id["tests"]["next"] = {"passed": "clear", "failed": "clear"}
    raw["tasks"].append(
        {
            "id": "clear",
            "task": "model.judge",
            "with": {
                "instruction": "Is the issue clear?",
                "agent": judge["id"],
                "with_change": False,
            },
            "next": {"pass": "implement", "changes": "implement"},
        }
    )
    assert validate.errors(parse(raw)) == []
    flows = FlowRecords(stage.sessions)
    flows.save_draft("deliver-issue", raw)
    flows.publish("deliver-issue")
    stage.provider.answers = [said(PASSED)]
    run = stage.runs.start_delivery(stage.item_id, toolchain=stage.toolchain)
    await stage.step("prepare", _prepared())
    await stage.launcher.tick()
    await asyncio.wait(list(stage.launcher._calls.values()), timeout=10)
    await stage.launcher.tick()
    step = next(s for s in stage.run(run["id"])["steps"] if s["name"] == "clear")
    assert (step["state"], step["outcome"]) == ("succeeded", "passed")
    body = stage.provider.asked[-1][2]
    assert body["model"] == "model-from-the-agent"
    question = body["messages"][1]["content"]
    assert question.startswith(
        "Who does this work, as the administrator set it: the agent Second opinion."
    )
    assert (
        "You judge whether an issue is clear enough to work on." in question
        and "Is the issue clear?" in question
    )
    assert step["input"]["prompt"]["agent"] == "Second opinion"
    assert step["input"]["model"] == {"provider": "deepseek", "model": "model-from-the-agent"}

    # An agent that says no model, for a task that says none either, fails the run there.
    stage.runs.request_cancel(UUID(run["id"]))
    await stage.launcher.tick()
    stage.fake.pods.clear()
    by_id = {task["id"]: task for task in raw["tasks"]}
    by_id["clear"]["with"]["agent"] = silent["id"]
    flows.save_draft("deliver-issue", raw)
    flows.publish("deliver-issue")
    again = stage.runs.start_delivery(stage.item_id, toolchain=stage.toolchain)
    await stage.step("prepare", _prepared())
    await stage.launcher.tick()
    ended = stage.run(again["id"])
    assert (ended["state"], ended["outcome"]) == ("failed", "not_asked")
    assert ended["detail"] == "clear names no model to ask, and neither does its agent"
    await stage.http.aclose()
    await stage.github.aclose()


# --- the console -----------------------------------------------------------------------------


def _console(tmp_path: Path) -> TestClient:
    repository = DashboardRepository(f"sqlite+pysqlite:///{tmp_path / 'web.db'}")
    Base.metadata.create_all(repository.engine)
    repository.initialize_password_sync(PASSWORD)
    client = TestClient(
        create_app(
            settings=Settings(database_url=str(repository.engine.url)),
            repository=repository,
            session_key=b"g" * 48,
        ),
        base_url="https://ade.musematic.ai",
        headers={"Origin": "https://ade.musematic.ai"},
    )
    client.__enter__()
    token = client.get("/login/challenge").json()["csrf_token"]
    signed = client.post(
        "/login", data={"csrf_token": token, "password": PASSWORD}, follow_redirects=False
    )
    assert signed.status_code == 303
    return client


def _post(client: TestClient, path: str, **fields: Any) -> httpx.Response:
    csrf = client.get("/api/dashboard/session").json()["csrf_token"]
    return client.post(f"/api/dashboard/{path}", json={"csrf_token": csrf, **fields})


def test_agents_skills_and_starters_in_the_console(tmp_path: Path) -> None:
    client = _console(tmp_path)
    try:
        assert client.get("/api/dashboard/agents").json() == {"agents": [], "skills": []}
        skill = _post(
            client,
            "skills",
            name="How we test",
            description="Conventions",
            text="Tests sit beside the code.",
        )
        assert skill.status_code == 201, skill.text
        made = _post(
            client,
            "agents",
            name="Maker",
            description="Changes the code",
            instructions=MAKER,
            settings={
                "providers": ["chatgpt", "claude"],
                "api": {"provider": "kimi", "model": "kimi-k2"},
            },
            skills=[skill.json()["id"]],
        )
        assert made.status_code == 201, made.text
        agent = made.json()
        assert agent["skill_names"] == ["How we test"]
        assert _post(client, "agents", name="maker", instructions="x").status_code == 409
        wrong = _post(client, "agents", name="Other", instructions="x", settings={"minutes": 500})
        assert wrong.status_code == 422 and "between 5 and 55 minutes" in wrong.json()["detail"]
        changed = _post(
            client,
            f"agents/{agent['id']}",
            name="Maker",
            instructions=MAKER + " Be brief.",
            settings={},
            skills=[],
        )
        assert (
            changed.status_code == 200
            and changed.json()["settings"] == {}
            and changed.json()["skills"] == []
        )
        assert (
            _post(
                client,
                f"skills/{skill.json()['id']}",
                name="How we test",
                text="Tests sit in tests/.",
            ).status_code
            == 200
        )

        # The designer is told which agents there are and which flows to begin from.
        catalogue = client.get("/api/dashboard/flows/catalogue").json()
        assert catalogue["agents"] == [{"id": agent["id"], "name": "Maker"}]
        assert [entry["key"] for entry in catalogue["starters"]] == [
            "starter-model-pipeline",
            "starter-model-loop",
            "starter-loop-part",
        ]
        kinds = {task["key"]: task for task in catalogue["tasks"]}
        assert kinds["agent.implement"]["params"][-1]["kind"] == "agent"

        # A new flow begun from a starter is a draft with its tasks, to be told its models.
        new = _post(
            client,
            "flows",
            key="my-loop",
            name="My loop",
            summary="A try",
            copy_of="starter-model-loop",
        )
        assert new.status_code == 201, new.text
        shown = client.get("/api/dashboard/flows/my-loop").json()["draft"]
        assert [task["id"] for task in shown["document"]["tasks"]][-6:] == [
            "decide",
            "find",
            "read",
            "write",
            "apply",
            "look",
        ]
        errors = [p["text"] for p in shown["problems"] if p["level"] == "error"]
        assert errors and all("does not name the model it asks" in text for text in errors)
        # Naming the agent in its tasks is all it needs.
        document = shown["document"]
        for task in document["tasks"]:
            if task["task"].startswith("model."):
                task["with"] = {**task["with"], "agent": agent["id"]}
        saved = _post(client, "flows/my-loop/draft", document=document, note="With the agent")
        assert saved.status_code == 200, saved.text
        assert [p for p in saved.json()["problems"] if p["level"] == "error"] == []
        listed = client.get("/api/dashboard/agents").json()["agents"][0]["used_by"]
        assert listed == [
            {"flow": "my-loop", "name": "My loop", "tasks": ["decide", "write"], "draft": True}
        ]

        gone = _post(client, f"agents/{agent['id']}/remove")
        assert gone.status_code == 200 and gone.json()["still_named_by"] == ["My loop"]
        assert _post(client, f"skills/{skill.json()['id']}/remove").status_code == 200
        assert client.get("/api/dashboard/agents").json() == {"agents": [], "skills": []}
        assert _post(client, f"agents/{uuid4()}/remove").status_code == 404
        activity = (
            json.dumps(client.get("/api/dashboard/activity").json())
            if client.get("/api/dashboard/activity").status_code == 200
            else ""
        )
        assert MAKER not in activity
    finally:
        client.__exit__(None, None, None)
