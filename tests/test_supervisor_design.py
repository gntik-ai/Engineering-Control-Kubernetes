"""The supervisor's design role: it reads the task list and the flows, drafts a flow,
checks it, and proposes it. Nothing it drafts takes effect before the administrator
accepts it, and the rules hold for its drafts as for anyone's."""

from __future__ import annotations

import copy
import json
from typing import Any
from uuid import UUID

import pytest

from engineering_control.dashboard.project_records import ProjectNotFound
from engineering_control.engine.builtin import BUILTIN
from engineering_control.engine.records import FlowRecords
from engineering_control.supervisor.definition import CATALOGUE, DEFAULT_SKILLS, TOOLS
from tests.test_supervisor import world  # noqa: F401 - a fixture


def _sweep() -> dict[str, Any]:
    """A flow that only reads and produces items: the clean-up, told to look for less."""
    raw = copy.deepcopy(BUILTIN["code-clean"].as_dict())
    raw.update(key="dead-code-sweep", name="Dead code sweep", action="Look for dead code")
    raw["summary"] = "Look for code nothing uses and propose taking it out."
    raw["prompts"]["auditor"]["role"] = (
        "You look for code nothing uses in {{repository}}, checked out in the current "
        "directory at commit {{base_sha}}. Do not modify files."
    )
    return raw


def test_the_design_tools_are_tools_like_the_others() -> None:
    kinds = {tool.name: tool.kind for tool in CATALOGUE}
    assert {
        name: kinds[name] for name in ("list_tasks", "list_flows", "get_flow", "check_flow")
    } == {
        "list_tasks": "read",
        "list_flows": "read",
        "get_flow": "read",
        "check_flow": "read",
    }
    proposing = TOOLS["propose_flow"]
    # A flow is never taken into the library without the administrator.
    assert (proposing.kind, proposing.default_mode, proposing.modes) == (
        "plan",
        "confirm",
        ("confirm",),
    )
    assert "design-a-flow" in {skill["key"] for skill in DEFAULT_SKILLS}


@pytest.mark.asyncio
async def test_the_supervisor_reads_what_flows_are_built_from_and_checks_a_draft(
    world: Any,  # noqa: F811
) -> None:
    world.account("Claude one", "claude")
    run_id, token = await world.running_turn("Make me a flow that finds dead code.")
    listed = json.loads(world.call(run_id, token, "list_tasks")[0])
    tasks = {task["task"]: task for task in listed["tasks"]}
    assert tasks["agent.review"]["outcomes"] == ["pass", "changes"]
    assert tasks["agent.review"]["agent"] is True and tasks["people.approval"]["agent"] is False
    assert "other_than" in tasks["agent.review"]["settings"]
    assert listed["prompts"]["auditor"]["parts_a_flow_may_word"] == [
        "role",
        "what_counts",
        "verification",
    ]
    assert "{{repository}}" in listed["prompts"]["auditor"]["may_name"]
    assert any("people.approval" in rule for rule in listed["rules"])

    flows = {flow["flow"]: flow for flow in json.loads(world.call(run_id, token, "list_flows")[0])}
    assert flows["code-clean"]["started_for_a_section_as"] == "Look for things to clean up"
    read = json.loads(world.call(run_id, token, "get_flow", flow="code-clean")[0])
    assert read["version"] == 0 and read["document"]["key"] == "code-clean"
    assert world.call(run_id, token, "get_flow", flow="nothing")[1] is True

    # A draft that skips the administrator is told which rule it breaks.
    broken = copy.deepcopy(BUILTIN["deliver-issue"].as_dict())
    broken["key"] = "fast-delivery"
    next(task for task in broken["tasks"] if task["id"] == "bundle")["next"] = {"ok": "publish"}
    checked = json.loads(world.call(run_id, token, "check_flow", document=broken)[0])
    assert any("without the administrator's decision" in said for said in checked["problems"])
    text, error = world.call(run_id, token, "propose_flow", document=broken, reason="Faster.")
    assert error and text.startswith("Refused: The draft breaks a rule, so it is not proposed")
    assert world.call(run_id, token, "check_flow", document={"format": "x"})[1] is True
    good = json.loads(world.call(run_id, token, "check_flow", document=_sweep())[0])
    assert good["problems"] == []
    assert good["on_paper"]["tasks"][1] == "audit (chatgpt) ends with candidates"
    assert good["on_paper"]["ends"] == "Confirmed findings are proposed to the backlog."
    assert FlowRecords(world.sessions).exists("dead-code-sweep") is False, "checking keeps nothing"


@pytest.mark.asyncio
async def test_a_proposed_flow_waits_and_is_in_force_once_accepted(world: Any) -> None:  # noqa: F811
    world.account("Claude one", "claude")
    flows = FlowRecords(world.sessions)
    run_id, token = await world.running_turn("Make me a flow that finds dead code.")
    text, error = world.call(run_id, token, "propose_flow", document=_sweep())
    assert error and "a reason is required" in text
    text, error = world.call(
        run_id, token, "propose_flow", document=_sweep(), reason="You asked for a dead code sweep."
    )
    assert not error and text.startswith("Not done: this waits for the administrator")
    assert not flows.exists("dead-code-sweep"), "nothing is there before it is accepted"
    (entry,) = world.toolbox.pending()
    assert entry["tool"] == "propose_flow" and entry["detail"] == (
        "A flow takes effect only when you accept it."
    )
    assert entry["summary"].startswith(
        "A new flow, Dead code sweep (dead-code-sweep): prepare → audit → verify → decide. "
        "On paper: Confirmed findings are proposed to the backlog. Accepting puts it in force."
    )
    shown = world.toolbox.proposed_flow(UUID(entry["action_id"]))
    assert shown["document"]["key"] == "dead-code-sweep" and shown["problems"] == []
    assert (shown["exists"], shown["in_force"], shown["outcome"]) == (False, None, "pending")
    assert shown["on_paper"]["ends"]["how"] == "done"
    assert [prompt["name"] for prompt in shown["prompts"]] == ["auditor", "verifier"]
    with pytest.raises(ProjectNotFound):
        world.toolbox.proposed_flow(UUID(int=7))

    # The administrator asks for a change: proposing again replaces what waited.
    weekly = {**_sweep(), "summary": "Look for code nothing uses, in the helpers first."}
    world.call(run_id, token, "propose_flow", document=weekly, reason="You asked for the helpers.")
    (entry,) = world.toolbox.pending()
    replaced = world.actions(outcome="rejected")
    assert [action.detail for action in replaced] == [
        "Replaced by a later proposal of the same flow."
    ]

    decided = world.toolbox.decide(UUID(entry["action_id"]), accept=True, via="chat")
    assert decided["outcome"] == "accepted"
    assert flows.version_in_force("dead-code-sweep") == 1
    (version,) = flows.versions("dead-code-sweep")
    assert version["note"] == "Drafted by the supervisor, accepted by the administrator"
    assert flows.document("dead-code-sweep", 1).summary == weekly["summary"]
    # The same flow again changes nothing and is said so.
    text, error = world.call(run_id, token, "propose_flow", document=weekly, reason="Again.")
    assert error and "nothing would change" in text


@pytest.mark.asyncio
async def test_a_flow_that_can_change_code_stays_a_draft_until_the_password(world: Any) -> None:  # noqa: F811
    world.account("Claude one", "claude")
    flows = FlowRecords(world.sessions)
    run_id, token = await world.running_turn("Give deliveries two rounds only.")
    changed = copy.deepcopy(BUILTIN["deliver-issue"].as_dict())
    changed["budgets"]["rounds"] = 2
    text, error = world.call(
        run_id, token, "propose_flow", document=changed, reason="You asked for two rounds."
    )
    assert not error and "waits for the administrator" in text
    (entry,) = world.toolbox.pending()
    assert entry["summary"].startswith("A new version of the flow Deliver issue (deliver-issue)")
    assert "Accepting saves it as a draft" in entry["summary"]
    world.toolbox.decide(UUID(entry["action_id"]), accept=True, via="console")
    # Accepted, it is the flow's draft: what runs is still the version in force.
    assert flows.version_in_force("deliver-issue") == 0 and flows.draft("deliver-issue") == 1
    assert flows.versions("deliver-issue")[0]["note"] == "Drafted by the supervisor"
    # While a draft is being worked on, the supervisor does not write over it.
    changed["budgets"]["rounds"] = 1
    text, error = world.call(run_id, token, "propose_flow", document=changed, reason="One round.")
    assert error and "has a draft that is being worked on" in text
    # A rejected proposal leaves nothing.
    flows.discard_draft("deliver-issue")
    world.call(run_id, token, "propose_flow", document=changed, reason="One round.")
    (entry,) = world.toolbox.pending()
    world.toolbox.decide(UUID(entry["action_id"]), accept=False, via="console")
    assert flows.draft("deliver-issue") is None and world.toolbox.pending() == []
