"""Trying a changed flow before it is used: going through it on paper, what runs use,
and the evaluation lab, which goes through past work again on another version."""

from __future__ import annotations

import copy
import json
from typing import Any
from uuid import UUID

import httpx
import pytest

from engineering_control.dashboard.project_records import ProjectConflict, ProjectInvalid
from engineering_control.db.models import BacklogItem
from engineering_control.engine.builtin import BUILTIN
from engineering_control.engine.document import parse
from engineering_control.engine.evaluations import Evaluations
from engineering_control.engine.records import FlowRecords
from engineering_control.engine.simulate import recorded, simulate
from engineering_control.sandbox import deliver
from tests.test_sandbox_delivery import (  # noqa: F401 - ``world`` is a fixture
    BASE,
    BUNDLE,
    HEAD,
    MAKER_DONE,
    PASSED,
    _implemented,
    _log,
    _prepared,
    _ran,
    _reviewed,
    _verified,
    world,
)

CHANGES = {"decision": "REQUEST_CHANGES", "summary": "A test is missing.", "findings": []}


def _raw(key: str) -> dict[str, Any]:
    return copy.deepcopy(BUILTIN[key].as_dict())


def _task(raw: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(task for task in raw["tasks"] if task["id"] == node_id)


def _path(result: dict[str, Any]) -> list[str]:
    return [f"{step['task']}:{step['outcome']}" for step in result["steps"]]


async def _delivered(world: Any, *, rounds: int = 1) -> dict[str, Any]:  # noqa: F811
    """A delivery that went as far as the administrator's decision, after ``rounds``."""
    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    await world.step("prepare", _prepared())
    for round_number in range(1, rounds + 1):
        await world.step("implement", _implemented())
        await world.step("review", _reviewed(CHANGES if round_number < rounds else PASSED))
    await world.step("verify", _verified())
    await world.step("bundle", _log([_ran("bundle", "2 files")], artifact=BUNDLE))
    return world.run(run["id"])


# --- on paper ----------------------------------------------------------------------------


def test_a_flow_is_gone_through_on_paper_the_way_a_run_would_go() -> None:
    usual = simulate(BUILTIN["deliver-issue"])
    assert _path(usual) == [
        "prepare:ok",
        "install:ok",
        "tests:passed",
        "implement:done",
        "review:pass",
        "verify:passed",
        "scan:ok",
        "bundle:ok",
        "approve:publish",
        "publish:ok",
    ]
    assert usual["ends"] == {"how": "done", "text": "The change is published as a pull request."}
    by_task = {step["task"]: step for step in usual["steps"]}
    # Who would work: one subscription implements, the other judges.
    assert (by_task["implement"]["provider"], by_task["review"]["provider"]) == (
        "chatgpt",
        "claude",
    )
    assert by_task["verify"]["provider"] is None and by_task["approve"]["where"] == "platform"
    assert by_task["approve"]["note"] == "The run waits here until the administrator decides."
    assert by_task["implement"]["prompt"]["name"] == "implementer"
    assert by_task["implement"]["prompt"]["changed"] is False
    assert all(step["assumed"] for step in usual["steps"])
    assert usual["totals"] == {
        "sandboxes": 8,
        "agents": {"chatgpt": 1, "claude": 1},
        "rounds": 1,
        "of_rounds": 3,
    }

    # A reviewer who never agrees: three rounds, then the run fails, as a run would.
    stubborn = simulate(BUILTIN["deliver-issue"], outcomes={"review": ["changes"] * 5})
    assert [s["round"] for s in stubborn["steps"] if s["task"] == "review"] == [1, 2, 3]
    assert stubborn["ends"]["how"] == "failed" and "round 3 of 3" in stubborn["ends"]["text"]
    assert {s["provider"] for s in stubborn["steps"] if s["task"] == "implement"} == {"chatgpt"}
    assert (
        simulate(BUILTIN["deliver-issue"], outcomes={"review": ["changes"] * 3}, max_rounds=5)[
            "ends"
        ]["how"]
        == "done"
    ), "a run granted more rounds has them"

    # Tests that fail send the work back to the implementer; a discarded change ends there.
    back = simulate(BUILTIN["deliver-issue"], outcomes={"verify": ["regression"]})
    assert _path(back)[5:8] == ["verify:regression", "implement:done", "review:pass"]
    assert not back["steps"][5]["assumed"] and back["totals"]["rounds"] == 2
    gone = simulate(BUILTIN["deliver-issue"], outcomes={"approve": ["discard"]})
    assert gone["ends"]["how"] == "discarded" and _path(gone)[-1] == "approve:discard"

    # An audit of a whole repository goes through its areas; one told where to look
    # goes straight to the audit.
    whole = simulate(BUILTIN["audit-code"], scope=False, entries=3)
    assert [s["entry"] for s in whole["steps"] if s["task"] == "audit"] == [1, 2, 3]
    assert _path(whole)[1:5] == ["install:ok", "scope:no", "plan:areas", "areas:item"]
    assert next(s for s in whole["steps"] if s["task"] == "verify")["times"] == 2
    scoped = simulate(BUILTIN["audit-code"], scope=True)
    assert _path(scoped) == ["prepare:ok", "install:ok", "scope:yes", "audit:candidates",
                             "verify:done", "areas:done", "decide:ok"]  # fmt: skip
    assert scoped["ends"]["text"] == "Confirmed findings are proposed to the backlog."

    # A flow that breaks a rule is not gone through: it could not run.
    broken = _raw("deliver-issue")
    _task(broken, "bundle")["next"] = {"ok": "publish"}
    refused = simulate(parse(broken))
    assert refused["ends"]["how"] == "invalid" and refused["steps"] == []
    assert refused["problems"]


def test_a_changed_flow_shows_what_it_changes() -> None:
    changed = _raw("deliver-issue")
    _task(changed, "implement")["with"]["providers"] = ["claude"]
    changed["prompts"] = {"reviewer": {"judging": "Be strict in {{repository}}."}}
    changed["budgets"]["rounds"] = 1
    result = simulate(parse(changed), outcomes={"review": ["changes"]})
    by_task = {step["task"]: step for step in result["steps"]}
    assert (by_task["implement"]["provider"], by_task["review"]["provider"]) == (
        "claude",
        "chatgpt",
    )
    assert by_task["review"]["prompt"]["changed"] is True
    assert result["ends"]["how"] == "failed" and "round 1 of 1" in result["ends"]["text"]


@pytest.mark.asyncio
async def test_a_run_that_happened_says_how_a_changed_flow_would_have_gone(world: Any) -> None:  # noqa: F811
    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    past = await _delivered(world, rounds=2)
    told = recorded(past["steps"], past["data"])
    assert told == {
        "prepare": ["ok"],
        "install": ["ok"],
        "tests": ["passed"],
        "implement": ["done", "done"],
        "review": ["changes", "pass"],
        "verify": ["passed"],
        "scan": ["ok"],
        "bundle": ["ok"],
    }
    # The flow as it is: the same way the run went.
    same = simulate(BUILTIN["deliver-issue"], outcomes=told)
    assert [s["round"] for s in same["steps"] if s["task"] == "review"] == [1, 2]
    assert same["ends"]["how"] == "done"
    # With one round only, the same events end the run at the first review.
    once = _raw("deliver-issue")
    once["budgets"]["rounds"] = 1
    assert simulate(parse(once), outcomes=told)["ends"]["how"] == "failed"
    await world.github.aclose()


# --- what runs use -----------------------------------------------------------------------


def test_what_an_agent_used_is_read_from_its_clients_own_report() -> None:
    claude = "\n".join(
        json.dumps(event)
        for event in (
            {"type": "assistant", "message": {"usage": {"input_tokens": 9, "output_tokens": 9}}},
            {
                "type": "result",
                "usage": {
                    "input_tokens": 1200,
                    "cache_creation_input_tokens": 300,
                    "cache_read_input_tokens": 90_000,
                    "output_tokens": 500,
                },
            },
        )
    )
    assert deliver.agent_usage("claude", [_ran("agent", claude)]) == {
        "tokens": 2000,
        "input": 1500,
        "output": 500,
        "cached": 90_000,
    }
    codex = "\n".join(
        json.dumps({"type": "turn.completed", "usage": usage})
        for usage in (
            {"input_tokens": 10_000, "cached_input_tokens": 8_000, "output_tokens": 400},
            {"input_tokens": 3_000, "cached_input_tokens": 3_000, "output_tokens": "many"},
        )
    )
    assert deliver.agent_usage("chatgpt", [_ran("agent", codex), _ran("answer", "{}")]) == {
        "tokens": 2400,
        "input": 2000,
        "output": 400,
        "cached": 11_000,
    }
    # What an agent printed is not the client's report.
    said = '{"usage": {"input_tokens": 5}}\nusage: a lot\n{"type":"result","usage":"none"}'
    assert deliver.agent_usage("claude", [_ran("agent", said)]) is None
    assert deliver.agent_usage("chatgpt", [_ran("agent", "")]) is None


def _spent(tokens: int) -> str:
    """An implementer on ChatGPT that reports having used that many tokens."""
    event = {"type": "turn.completed", "usage": {"input_tokens": tokens, "output_tokens": 0}}
    return _log(
        [
            _ran("agent", json.dumps(event) + "\n"),
            _ran("answer", json.dumps(MAKER_DONE)),
            _ran("commit", f"{HEAD}\n"),
        ]
    )


@pytest.mark.asyncio
async def test_a_run_counts_what_it_uses_and_stops_at_its_flows_budget(world: Any) -> None:  # noqa: F811
    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    flows = FlowRecords(world.sessions)
    limited = _raw("deliver-issue")
    limited["budgets"]["tokens"] = 50_000
    flows.save_draft("deliver-issue", limited)
    flows.publish("deliver-issue")

    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    await world.step("prepare", _prepared())
    await world.step("implement", _spent(30_000))
    shown = world.run(run["id"])
    assert next(s for s in shown["steps"] if s["name"] == "implement")["usage"] == {
        "tokens": 30_000,
        "input": 30_000,
        "output": 0,
        "cached": 0,
    }
    # Four sandboxes so far: the clone, the installation, the tests and the implementer.
    assert shown["usage"]["tokens"] == 30_000 and shown["usage"]["sandboxes"] == 4
    await world.step("review", _reviewed(CHANGES))
    # Within the budget: the work goes back for a second round, which uses it up.
    await world.step("implement", _spent(40_000))
    ended = world.run(run["id"])
    assert (ended["state"], ended["outcome"]) == ("failed", "over_budget")
    assert "used 70,000 tokens; its flow allows 50,000" in ended["detail"]

    totals = world.runs.usage_totals(days=7)
    (project,) = totals["projects"]
    assert (project["project"], project["runs"], project["tokens"]) == (
        "falcone-workflow",
        1,
        70_000,
    )
    (flow,) = totals["flows"]
    assert (flow["key"], flow["version"], flow["failed"], flow["tokens"]) == (
        "deliver-issue",
        1,
        1,
        70_000,
    )
    (listed,) = world.runs.listing()["items"]
    assert listed["usage"]["tokens"] == 70_000
    await world.github.aclose()


# --- the evaluation lab ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_past_work_is_gone_through_again_on_a_changed_flow_and_nothing_leaves(
    world: Any,  # noqa: F811
) -> None:
    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    flows = FlowRecords(world.sessions)
    lab = Evaluations(world.sessions, world.runs)
    past = await _delivered(world, rounds=2)
    assert past["state"] == "waiting"
    (offered,) = lab.candidates("deliver-issue")
    assert offered["run_id"] == past["id"] and offered["commit"] == BASE[:12]

    # What cannot be evaluated.
    with pytest.raises(ProjectInvalid, match="the one in force"):
        lab.start("deliver-issue", version=0, run_ids=[UUID(past["id"])])
    with pytest.raises(ProjectInvalid, match="between 1 and 8"):
        lab.start("deliver-issue", version=0, run_ids=[])

    # A draft: Claude implements, in other words. It is tried without being published.
    changed = _raw("deliver-issue")
    _task(changed, "implement")["with"]["providers"] = ["claude", "chatgpt"]
    changed["prompts"] = {"implementer": {"role": "You implement {{subject}} with a test."}}
    draft = flows.save_draft("deliver-issue", changed)["version"]
    other = _raw("audit-code")
    _task(other, "verify")["with"]["rounds"] = 1
    with pytest.raises(ProjectInvalid, match="not a run of this flow"):
        lab.start(
            "audit-code",
            version=flows.save_draft("audit-code", other)["version"],
            run_ids=[UUID(past["id"])],
        )
    started = lab.start("deliver-issue", version=draft, run_ids=[UUID(past["id"])], note="Claude")
    assert (started["state"], started["cases"], started["done"]) == ("running", 1, 0)
    assert started["baseline_version"] == 0 and started["candidate_version"] == draft
    with pytest.raises(ProjectConflict, match="already under way"):
        lab.start("deliver-issue", version=draft, run_ids=[UUID(past["id"])])
    # The draft goes on being edited; the evaluation keeps what it started with.
    changed["budgets"]["rounds"] = 1
    flows.save_draft("deliver-issue", changed)

    created_before = dict(world.hub.created)
    tokens_before = len(world.hub.tokens)
    # The launcher starts the case: the same words, the same commit.
    await world.launcher.tick()
    job = world.job()
    checkout = job["commands"][0]
    assert checkout["env"]["COMMIT"] == BASE
    (case,) = lab.get(UUID(started["id"]))["items"]
    trial = world.run(case["candidate"]["run_id"])
    assert trial["kind"] == "evaluation" and trial["title"].startswith("Evaluation: #11")
    assert trial["item_id"] is None and "flow_document" not in trial["data"]
    assert trial["data"]["flow"]["version"] == draft
    assert len(world.hub.tokens) == tokens_before + 1, "the checkout's token, and no issue read"
    world.fake.finish(_prepared())
    await world.launcher.tick()
    await world.step("install", _prepared())
    await world.step("tests", _prepared())
    await world.launcher.tick()
    job = world.job()
    assert job["commands"][0]["argv"][0] == "claude"
    assert job["commands"][0]["stdin"].startswith("You implement issue #11")
    assert "Steps: sign in, wait, reload." in job["commands"][0]["stdin"], "the issue as it read"
    world.fake.finish(_implemented("claude"))
    await world.launcher.tick()
    await world.step("review", _reviewed(PASSED, provider="chatgpt"))
    await world.step("verify", _verified())
    await world.step("bundle", _log([_ran("bundle", "2 files")], artifact=BUNDLE))
    await world.launcher.tick()
    ended = world.run(trial["id"])
    # It ends where the administrator would decide: nothing waits, nothing is published.
    assert (ended["state"], ended["outcome"]) == ("succeeded", "evaluated")
    assert world.hub.created == created_before
    assert [entry["run_id"] for entry in world.runs.inbox()] == [past["id"]]
    with world.sessions() as session:
        assert session.get(BacklogItem, world.item_id).state == "accepted"

    await world.launcher.tick()
    result = lab.get(UUID(started["id"]))
    assert (result["state"], result["done"]) == ("finished", 1)
    (case,) = result["items"]
    assert case["baseline"]["rounds"] == 2 and case["candidate"]["rounds"] == 1
    assert case["baseline"]["through"] and case["candidate"]["through"]
    assert case["verdict"] == "better" and result["verdicts"] == {
        "better": 1,
        "same": 0,
        "worse": 0,
    }
    assert (
        result["totals"]["baseline"]["rounds"] == 2 and result["totals"]["candidate"]["rounds"] == 1
    )
    assert [row["id"] for row in lab.listing("deliver-issue")] == [started["id"]]
    # Evaluations are counted apart from the work.
    assert {
        (f["key"], f["version"], f["evaluation"]) for f in world.runs.usage_totals()["flows"]
    } == {
        ("deliver-issue", 0, False),
        ("deliver-issue", draft, True),
    }
    await world.github.aclose()


@pytest.mark.asyncio
async def test_an_evaluation_goes_case_by_case_and_can_be_stopped(world: Any) -> None:  # noqa: F811
    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    flows = FlowRecords(world.sessions)
    lab = Evaluations(world.sessions, world.runs)
    first = await _delivered(world)
    world.runs.decide(UUID(first["id"]), "discard")
    await world.launcher.tick()
    second = await _delivered(world)
    changed = _raw("deliver-issue")
    changed["budgets"]["rounds"] = 2
    draft = flows.save_draft("deliver-issue", changed)["version"]
    started = lab.start(
        "deliver-issue", version=draft, run_ids=[UUID(second["id"]), UUID(first["id"])]
    )
    await world.launcher.tick()
    under_way = lab.get(UUID(started["id"]))
    assert [bool(case["candidate"]) for case in under_way["items"]] == [True, False], (
        "one at a time"
    )
    stopped = lab.stop(UUID(started["id"]))
    assert stopped["state"] == "stopped"
    await world.launcher.tick()
    await world.launcher.tick()
    after = lab.get(UUID(started["id"]))
    assert after["items"][0]["candidate"]["state"] == "cancelled"
    assert after["items"][1]["candidate"] is None, "no further case starts"
    assert after["items"][0]["verdict"] == "worse", "the run as it went came through"
    with pytest.raises(ProjectConflict, match="not under way"):
        lab.stop(UUID(started["id"]))
    await world.github.aclose()


# --- the console -------------------------------------------------------------------------


def test_the_console_edits_tries_and_publishes_a_flow(world: Any) -> None:  # noqa: F811
    from fastapi.testclient import TestClient

    from engineering_control.config import Settings
    from engineering_control.dashboard.app import create_app
    from engineering_control.dashboard.repository import DashboardRepository

    password = "correct horse battery staple"  # noqa: S105 - deterministic test fixture
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

        listed = {flow["key"]: flow for flow in client.get("/api/dashboard/flows").json()["items"]}
        assert listed["deliver-issue"]["engine"]["version"] == 0
        assert listed["deliver-issue"]["engine"]["budgets"] == {
            "rounds": 3,
            "minutes": 480,
            "tokens": None,
        }
        assert listed["change-request"]["engine"] is None, "a flow that cannot run here yet"
        catalogue = client.get("/api/dashboard/flows/catalogue").json()
        assert {task["key"] for task in catalogue["tasks"]} >= {
            "agent.implement",
            "people.approval",
        }
        assert {prompt["name"] for prompt in catalogue["prompts"]} >= {"implementer", "reviewer"}

        flow = client.get("/api/dashboard/flows/deliver-issue").json()
        assert flow["draft"] is None and flow["in_force"]["version"] == 0
        assert [p["name"] for p in flow["in_force"]["prompts"]] == ["implementer", "reviewer"]
        assert client.get("/api/dashboard/flows/nothing").status_code == 404
        document = flow["in_force"]["document"]

        # A draft with an error is kept and says what is wrong.
        broken = copy.deepcopy(document)
        _task(broken, "bundle")["next"] = {"ok": "publish"}
        saved = post("/flows/deliver-issue/draft", document=broken)
        assert saved.status_code == 200
        assert {p["rule"] for p in saved.json()["problems"]} >= {"publish_decided"}
        refused = post("/flows/deliver-issue/publish", current_password=password)
        assert refused.status_code == 422 and "cannot be published" in refused.json()["detail"]
        assert post("/flows/deliver-issue/draft", document={"format": "x"}).status_code == 422

        document["prompts"] = {"reviewer": {"judging": "Be strict in {{repository}}."}}
        document["budgets"]["rounds"] = 2
        saved = post("/flows/deliver-issue/draft", document=document, note="Stricter")
        assert saved.json()["problems"] == [] and saved.json()["state"] == "draft"
        reviewer = next(p for p in saved.json()["prompts"] if p["name"] == "reviewer")
        assert [part["changed"] for part in reviewer["parts"] if part["kind"] == "editable"] == [
            False,
            True,
        ]

        # On paper, before anything runs: the draft, and a document that is not saved.
        tried = post(
            "/flows/deliver-issue/simulate", version=1, outcomes={"review": ["changes"] * 3}
        ).json()
        assert tried["ends"]["how"] == "failed" and "round 2 of 2" in tried["ends"]["text"]
        unsaved = post("/flows/deliver-issue/simulate", document=broken).json()
        assert unsaved["ends"]["how"] == "invalid"
        assert (
            "Flow deliver-issue: flow simulated"
            not in client.get("/api/dashboard/activity/recent").text
        )

        # Publishing asks for the password.
        wrong = post("/flows/deliver-issue/publish", current_password="not it")  # noqa: S106
        assert wrong.status_code == 403
        published = post("/flows/deliver-issue/publish", current_password=password, note="Go")
        assert published.status_code == 200 and published.json()["state"] == "published"
        flow = client.get("/api/dashboard/flows/deliver-issue").json()
        assert flow["in_force"]["version"] == 1 and flow["draft"] is None
        assert [v["version"] for v in flow["versions"]] == [1, 0]
        own = client.get("/api/dashboard/flows/deliver-issue/versions/0").json()
        assert own["document"]["budgets"]["rounds"] == 3
        assert client.get("/api/dashboard/flows/deliver-issue/versions/9").status_code == 404
        assert (
            "Flow deliver-issue: flow published"
            in client.get("/api/dashboard/activity/recent").text
        )

        # A draft can be dropped; the lab lists what there is to go through again.
        post("/flows/deliver-issue/draft", document=own["document"])
        assert post("/flows/deliver-issue/draft/discard").status_code == 200
        assert post("/flows/deliver-issue/draft/discard").status_code == 409
        lab = client.get("/api/dashboard/flows/deliver-issue/evaluations").json()
        assert lab == {
            "items": [],
            "candidates": [],
            "sources": [{"key": "deliver-issue", "name": "Deliver issue", "runs": 0}],
            "of": "deliver-issue",
        }
        nothing = post(
            "/flows/deliver-issue/evaluations",
            version=0,
            run_ids=["00000000-0000-0000-0000-000000000001"],
        )
        assert nothing.status_code == 422
        usage = client.get("/api/dashboard/usage?days=7").json()
        assert usage["days"] == 7 and usage["projects"] == [] and usage["flows"] == []
