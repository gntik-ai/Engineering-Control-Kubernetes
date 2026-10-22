"""NEW-234 PR5: the decomposition plan's controller activities and its durable record.

DecompositionActivityHandler runs against SQLite over PR 4's miniature snapshot; the scan,
GitHub and provider steps are scripted (new_234_plan_fixtures.drive runs the steps in the
workflow's order). Covers the pass path, redrafts after review findings and Hermes
rejections, the budgets, assembly problems mapped back to their part, the epic scope, part
reuse, and the approval binding (mismatch, drift between start and approval, tamper).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from new_234_decomposition_fixtures import (
    SOURCE_SHA,
    api_epic,
    build_decomposition_pack,
    frame_output,
    inventory,
    run_epic,
)
from new_234_plan_fixtures import (
    FRAME_CONTENT_SHA,
    OPERATOR,
    WORKTREE,
    PlanHarness,
    agent_result,
    drive,
    review_for,
    stage_inputs,
)
from sqlalchemy import select, update
from temporalio.exceptions import ApplicationError

from engineering_control.activities import backlog_decomposition as activities
from engineering_control.db.backlog_decompositions import DecompositionBudgetExhaustedError
from engineering_control.db.models import BacklogDecomposition, ChangeRequest, NotificationOutbox
from engineering_control.engineering.decomposition import (
    APPROVAL_PHRASE,
    MAX_PROVIDER_CALLS,
    DecompositionRejected,
    EpicPart,
    PlanProblem,
    approval_binding,
    assembly_redrafts,
    pack_revision,
    part_content_sha256,
    resolve_epics,
    scoped_inventory,
)


def _identity(created: dict[str, Any]) -> UUID:
    return UUID(created["id"])


def _row(harness: PlanHarness, identity: UUID) -> BacklogDecomposition:
    with harness.repository.sessions() as session:
        row = session.get(BacklogDecomposition, identity)
        assert row is not None
        return row


# The pass path ---------------------------------------------------------------------------


def test_a_plan_drafts_the_frame_then_each_epic_in_epic_order_and_becomes_a_candidate(
    tmp_path: Path,
) -> None:
    harness = PlanHarness(tmp_path)
    created = harness.create()
    result = drive(harness, created["id"])
    assert result["status"] == "CANDIDATE", result
    assert (result["stories"], result["tasks"]) == (3, 4)
    view = harness.repository.get_sync(_identity(created))
    assert view is not None
    # The frame first, then RUN (a dependency root) before API (PR 4's epic_order).
    assert [(item["kind"], item["number"]) for item in view["parts"]] == [
        ("frame", None),
        ("epic", 2),
        ("epic", 1),
    ]
    assert view["provider_calls"] == 6 and view["usage"]["total"]["calls"] == 6
    assert view["review_rounds"] == 3
    assert view["confirmation_phrase"] == APPROVAL_PHRASE
    assert view["approve_command"] == (
        f"engineering-ctl backlog-decomposition-approve {created['id']} "
        f"{result['approval_binding']} {APPROVAL_PHRASE}"
    )
    row = _row(harness, _identity(created))
    # The binding covers the project, source, the three source hashes, the plan and pack.
    assert result["approval_binding"] == approval_binding(
        decomposition_id=created["id"],
        project="falcone-serverless",
        source_sha=SOURCE_SHA,
        design_sha256=inventory()["design_sha256"],
        backlog_sha256=inventory()["backlog_sha256"],
        issues_sha256=inventory()["issues_sha256"],
        plan_sha256=str(row.plan_sha256),
        pack_revision=pack_revision(harness.pack),
        epics=None,
    )
    # Claude drafts with the spec model, Codex reviews with the configured Codex model.
    drafts = [item for item in harness.requests if item["provider"] == "claude"]
    reviews = [item for item in harness.requests if item["provider"] == "codex"]
    assert {item["role"] for item in drafts} == {"backlog-decomposer"}
    assert {item["role"] for item in reviews} == {"decomposition-reviewer"}
    assert {item["model"] for item in drafts} == {harness.settings.claude_spec_model}
    assert {item["model"] for item in reviews} == {harness.settings.codex_model}
    for item in harness.requests:
        assert item["writable"] is False and item["allowed_tools"] == ["Read", "Glob", "Grep"]
        assert item["working_directory"] == WORKTREE
    # The epic batch binds the reviewed frame; the frame names its staged references.
    assert f"inputs.frame_sha256 = {FRAME_CONTENT_SHA}" in drafts[2]["prompt"]
    assert "reference-adr-0001-execution-model-md.md" in drafts[0]["prompt"]
    assert "exactly these stories: RUN-01" in drafts[1]["prompt"]
    with harness.repository.sessions() as session:
        events = session.scalars(select(NotificationOutbox)).all()
        assert [row.payload_json["event"] for row in events] == ["DECOMPOSITION_READY"]


def test_the_part_input_binds_the_part_id_and_stays_within_64_kib(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    created = harness.create()
    drive(harness, created["id"])
    prepared = harness.step(
        "decomposition.prepare_part_input",
        {
            "decomposition_id": created["id"],
            "project": "falcone-serverless",
            "part": {"kind": "frame", "number": None},
        },
    )
    name = prepared["input_name"]
    assert name == f"part-{prepared['part_id']}-input.json"
    assert len(prepared["files"][name].encode()) <= 64 * 1024
    assert f'"part_id": "{prepared["part_id"]}"' in prepared["files"][name]
    assert stage_inputs(prepared)["files"][name]["sha256"] == prepared["file_hashes"][name]
    with pytest.raises(ApplicationError, match="no reviewed frame"):
        harness.step(
            "decomposition.prepare_part_input",
            {
                "decomposition_id": created["id"],
                "project": "falcone-serverless",
                "part": {"kind": "epic", "number": 1},
                "approved_parts": [],
            },
        )


# Redrafts --------------------------------------------------------------------------------


def test_a_revise_redrafts_the_part_with_every_earlier_finding(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    created = harness.create()

    def decide(payload: dict[str, Any], attempt: int) -> str:
        return "REVISE" if payload["part"]["number"] == 2 and attempt < 3 else "APPROVE"

    result = drive(harness, created["id"], decide=decide)
    assert result["status"] == "CANDIDATE", result
    view = harness.repository.get_sync(_identity(created))
    assert view is not None and view["provider_calls"] == 10
    epic_drafts = [
        item["prompt"]
        for item in harness.requests
        if item["provider"] == "claude" and "exactly these stories: RUN-01" in item["prompt"]
    ]
    assert len(epic_drafts) == 3
    assert "redraft 2 of at most 2" in epic_drafts[2]
    # Every round's finding, labelled by round, is carried into the last redraft.
    assert "round 1 (review of draft 1); round 2 (review of draft 2)" in epic_drafts[2]
    reviews = [item["prompt"] for item in harness.requests if item["provider"] == "codex"]
    assert any("This is the review of draft 3 of this part" in text for text in reviews)


def test_three_revisions_leave_the_part_unresolved(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    created = harness.create()
    result = drive(harness, created["id"], decide=lambda payload, attempt: "REVISE")
    assert (result["status"], result["blocker"]) == (
        "PART_UNRESOLVED",
        "DECOMPOSITION_PART_REVIEW_UNRESOLVED",
    )
    view = harness.repository.get_sync(_identity(created))
    # At most three drafts and three reviews of the part.
    assert view is not None and view["provider_calls"] == 6
    assert view["parts"][0]["drafts"] == 3 and view["parts"][0]["approved_attempt"] is None


def test_a_hermes_rejection_is_recorded_and_carried_into_the_redraft(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    created = harness.create()

    def draft(payload: dict[str, Any], attempt: int) -> Any:
        value = frame_output(part=payload["part_id"])
        value["inputs"]["design_sha256"] = "0" * 64  # bound to another design
        return value

    result = drive(harness, created["id"], draft=draft)
    assert (result["status"], result["blocker"]) == ("REJECTED", "DRAFT_BINDING_MISMATCH")
    entry = harness.repository.part_sync(_identity(created), result["part_id"])
    assert entry is not None and entry["rejections"][0]["code"] == "DRAFT_BINDING_MISMATCH"
    redraft = harness.step(
        "decomposition.prepare_part",
        {
            **{key: value for key, value in result.items() if key not in {"blocker", "blockers"}},
            "draft_attempt": 2,
            "rejection": {"code": result["blocker"], "details": result["blockers"]},
        },
    )
    prompt = redraft["draft_request"]["prompt"]
    assert (
        "Hermes rejected your previous draft deterministically (DRAFT_BINDING_MISMATCH)" in prompt
    )
    assert "inputs.design_sha256" in prompt and "Hermes rejection DRAFT_BINDING_MISMATCH" in prompt


# Budgets ---------------------------------------------------------------------------------


def test_the_provider_call_budget_refuses_the_91st_call(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    created = harness.create()
    identity = _identity(created)
    with harness.repository.sessions.begin() as session:
        session.execute(
            update(BacklogDecomposition)
            .where(BacklogDecomposition.id == identity)
            .values(
                parts_json={
                    "requested_epics": None,
                    "parts": {},
                    "calls": [f"earlier-{index}" for index in range(MAX_PROVIDER_CALLS - 1)],
                }
            )
        )
    result = drive(harness, created["id"])
    # The 90th call (the frame draft) is reserved; the frame review is refused.
    assert (result["status"], result["blocker"]) == ("BLOCKED", "DECOMPOSITION_BUDGET_EXHAUSTED")
    assert _row(harness, identity).provider_calls == MAX_PROVIDER_CALLS
    key = f"{result['part_id']}:draft:1"
    # Reserving the same call again is idempotent, a new one is refused.
    assert harness.repository.reserve_call_sync(identity, key) == MAX_PROVIDER_CALLS
    with pytest.raises(DecompositionBudgetExhaustedError):
        harness.repository.reserve_call_sync(identity, f"{result['part_id']}:review:1")


def test_a_fourth_draft_and_an_oversized_review_are_refused(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    created = harness.create()
    result = drive(harness, created["id"], decide=lambda payload, attempt: "REVISE")
    base = {key: value for key, value in result.items() if key not in {"blocker", "blockers"}}
    fourth = harness.step("decomposition.prepare_part", {**base, "draft_attempt": 4})
    assert (fourth["status"], fourth["blocker"]) == ("FAILED", "DRAFT_INPUT_INVALID")
    review = review_for({**base, "draft_sha256": "d" * 64}, "REVISE", detail="x" * 41_000)
    reviewed = harness.step(
        "decomposition.record_part_review",
        agent_result(
            {
                **base,
                "draft_sha256": "d" * 64,
                "provider_step": "decomposition.review",
                "review_request": {"run_id": "r", "provider": "codex", "role": "x"},
            },
            review,
        ),
    )
    assert (reviewed["status"], reviewed["blocker"]) == ("REJECTED", "REVIEW_TOO_LARGE")


# Assembly --------------------------------------------------------------------------------


def test_assembly_problems_map_to_the_part_that_owns_them() -> None:
    scoped = inventory()
    parts = [
        EpicPart(2, "ep02-" + "a" * 16, run_epic()),
        EpicPart(1, "ep01-" + "b" * 16, api_epic()),
    ]
    problems = [
        PlanProblem("DEPENDENCY_UNRESOLVED", "dependency", "falcone-serverless:API-01.t2", "x"),
        PlanProblem("PREREQUISITE_UNORDERED", "dependency", "RUN-01", "y"),
        PlanProblem(
            "TASK_TITLE_DUPLICATE",
            "duplicate_task",
            "run wasm components with fuel and memory limits",
            "z",
        ),
    ]
    owned, unowned = assembly_redrafts([item.as_dict() for item in problems], scoped, parts)
    assert list(owned) == ["ep02-" + "a" * 16, "ep01-" + "b" * 16]
    assert [item["code"] for item in owned["ep02-" + "a" * 16]] == [
        "PREREQUISITE_UNORDERED",
        "TASK_TITLE_DUPLICATE",
    ]
    assert unowned == []
    _owned, unowned = assembly_redrafts(
        [PlanProblem("PLAN_BINDING_MISMATCH", "other", "inputs", "w").as_dict()], scoped, parts
    )
    assert [item["subject"] for item in unowned] == ["inputs"]


def test_an_assembly_problem_comes_back_as_its_parts_redraft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = PlanHarness(tmp_path)
    created = harness.create()
    real = activities.assemble_plan
    calls: list[int] = []

    def flaky(scoped: Any, frame: Any, epics: Any) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 1:
            raise DecompositionRejected(
                "ASSEMBLY_INVALID",
                [PlanProblem("DEPENDENCY_UNRESOLVED", "dependency",
                             "falcone-serverless:RUN-01.t1", "depends on a missing task")],
            )  # fmt: skip
        return real(scoped, frame, epics)

    monkeypatch.setattr(activities, "assemble_plan", flaky)
    result = drive(harness, created["id"])
    assert result["status"] == "ASSEMBLY_REDRAFT"
    [redraft] = result["redrafts"]
    run_part = next(
        item["part_id"]
        for item in harness.repository.get_sync(_identity(created))["parts"]  # type: ignore[index]
        if item["number"] == 2
    )
    assert redraft["part_id"] == run_part
    assert redraft["problems"][0]["subject"] == "falcone-serverless:RUN-01.t1"
    # The owning part's next draft carries the assembly findings ...
    context = {key: value for key, value in result.items() if key not in {"redrafts", "status"}}
    frame_entry = context["approved_parts"][0]
    prepared = harness.step(
        "decomposition.prepare_part",
        {
            **context,
            "approved_parts": [frame_entry],
            "part": {"kind": "epic", "number": 2},
            "part_id": run_part,
            "part_input": {"path": "/in/x", "sha256": "1" * 64, "bytes": 1},
            "draft_attempt": 2,
            "rejection": {"code": "ASSEMBLY_INVALID", "details": redraft["problems"]},
        },
    )
    prompt = prepared["draft_request"]["prompt"]
    assert "Hermes assembled the whole plan and found the problems" in prompt
    assert "depends on a missing task" in prompt
    # ... and a new draft of it withdraws its approval until it is reviewed again.
    harness.repository.record_part_sync(_identity(created), run_part, "epic", 2, 2, run_epic())
    entry = harness.repository.part_sync(_identity(created), run_part)
    assert entry is not None and entry["approved_attempt"] is None
    assert entry["rejections"][0]["code"] == "ASSEMBLY_INVALID"


# Scope -----------------------------------------------------------------------------------


def test_a_scoped_plan_decomposes_only_the_selected_epics(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    created = harness.create(["EP-RUN"])
    result = drive(harness, created["id"])
    assert result["status"] == "CANDIDATE", result
    row = _row(harness, _identity(created))
    assert [story["story_id"] for story in row.plan_json["stories"]] == ["RUN-01"]  # type: ignore[index]
    assert row.parts_json["epics"] == [2]
    view = harness.repository.get_sync(_identity(created))
    assert view is not None and [item["kind"] for item in view["parts"]] == ["frame", "epic"]
    # A task of a selected story may not depend on a task of an unplanned story.
    api_only = harness.create(["1"])
    refused = drive(harness, api_only["id"])
    assert (refused["status"], refused["blocker"]) == ("REJECTED", "DRAFT_STRUCTURE_INVALID")
    assert any(item["code"] == "DEPENDENCY_UNRESOLVED" for item in refused["blockers"])
    unknown = harness.create(["EP-ZZZ"])
    blocked = drive(harness, unknown["id"])
    assert (blocked["status"], blocked["blocker"]) == ("BLOCKED", "DECOMPOSITION_EPIC_UNKNOWN")


def test_scope_resolution_and_the_scoped_inventory() -> None:
    full = inventory()
    assert resolve_epics(full, None) is None
    assert resolve_epics(full, ["EP-API", "2", "1"]) == [1, 2]
    with pytest.raises(ValueError, match="unknown epics: 7, EP-NO"):
        resolve_epics(full, ["EP-NO", "7"])
    scoped = scoped_inventory(full, [2])
    assert [epic["id"] for epic in scoped["epics"]] == ["EP-RUN"]
    assert [story["id"] for story in scoped["stories"]] == ["RUN-01"]
    assert scoped["issues_sha256"] == full["issues_sha256"]
    assert scoped_inventory(full, None) == full


# Reuse -----------------------------------------------------------------------------------


def test_an_earlier_plans_reviewed_parts_are_reused_without_a_provider_call(
    tmp_path: Path,
) -> None:
    harness = PlanHarness(tmp_path)
    first = harness.create()
    assert drive(harness, first["id"])["status"] == "CANDIDATE"
    harness.requests.clear()
    second = harness.create()
    result = drive(harness, second["id"])
    assert result["status"] == "CANDIDATE" and result["superseded"] == [first["id"]]
    view = harness.repository.get_sync(_identity(second))
    assert view is not None and view["provider_calls"] == 0 and harness.requests == []
    assert {item["reused_from"]["decomposition_id"] for item in view["parts"]} == {first["id"]}
    assert _row(harness, _identity(first)).status == "SUPERSEDED"


def test_a_reused_part_is_rechecked_against_the_current_rules(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    first = harness.create()
    assert drive(harness, first["id"])["status"] == "CANDIDATE"
    row = _row(harness, _identity(first))
    parts = copy.deepcopy(row.parts_json)
    frame_id = next(key for key, entry in parts["parts"].items() if entry["kind"] == "frame")
    version = parts["parts"][frame_id]["versions"][0]
    version["content"]["contracts"][0]["owner_repository"] = "gntik-ai/elsewhere"
    version["content_sha256"] = version["reviewed_sha256"] = part_content_sha256(version["content"])
    with harness.repository.sessions.begin() as session:
        session.execute(
            update(BacklogDecomposition)
            .where(BacklogDecomposition.id == row.id)
            .values(parts_json=parts)
        )
    second = harness.create()
    assert drive(harness, second["id"])["status"] == "CANDIDATE"
    view = harness.repository.get_sync(_identity(second))
    assert view is not None
    assert view["parts"][0]["reused_from"] is None and view["parts"][0]["drafts"] == 1


# Start and inputs ------------------------------------------------------------------------


def test_register_blocks_without_the_released_skills(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    build_decomposition_pack(harness.pack, version="1.6.0")
    created = harness.create()
    result = drive_register(harness, created)
    assert (result["status"], result["blocker"]) == ("BLOCKED", "AGENT_PACK_SKILL_MISSING")
    assert _row(harness, _identity(created)).status == "BLOCKED"


def drive_register(harness: PlanHarness, created: dict[str, Any]) -> dict[str, Any]:
    return harness.step(
        "decomposition.register",
        {
            "decomposition_id": created["id"],
            "project": "falcone-serverless",
            "orchestration": {"workflow_id": created["workflow_id"]},
        },
    )


def test_record_inputs_refuses_an_inventory_that_does_not_match(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    created = harness.create()
    context = drive_register(harness, created)
    base = {**context, "source_worktree": WORKTREE, "source_sha": SOURCE_SHA}
    other = harness.step(
        "decomposition.record_inputs", {**base, "source_sha": "2" * 40, "inventory": inventory()}
    )
    assert (other["status"], other["blocker"]) == ("FAILED", "DECOMPOSITION_INVENTORY_MISMATCH")
    altered = inventory()
    altered["agents_md"] = "changed after hashing"
    refused = harness.step("decomposition.record_inputs", {**base, "inventory": altered})
    assert (refused["status"], refused["blocker"]) == (
        "BLOCKED",
        "DECOMPOSITION_INVENTORY_MISMATCH",
    )


def test_a_part_with_a_foreign_identity_is_refused(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    created = harness.create()
    drive(harness, created["id"])
    with pytest.raises(ApplicationError, match="part identity is invalid"):
        harness.step(
            "decomposition.prepare_part",
            {
                "decomposition_id": created["id"],
                "part": {"kind": "epic", "number": 1},
                "part_id": "ep02-" + "0" * 16,
                "draft_attempt": 1,
            },
        )


# Approval --------------------------------------------------------------------------------


def _candidate(harness: PlanHarness) -> tuple[UUID, str]:
    created = harness.create()
    result = drive(harness, created["id"])
    assert result["status"] == "CANDIDATE", result
    return _identity(created), str(result["approval_binding"])


def test_approval_records_the_decision_only_and_replays(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    identity, binding = _candidate(harness)
    current = pack_revision(harness.pack)
    approved = harness.repository.approve_sync(
        identity, binding, APPROVAL_PHRASE, OPERATOR, current
    )
    assert (approved["status"], approved["replayed"]) == ("APPROVED", False)
    assert approved["decided_by"] == OPERATOR and approved["decision_sequence"] == 1
    replay = harness.repository.approve_sync(identity, binding, APPROVAL_PHRASE, OPERATOR, current)
    assert (replay["status"], replay["replayed"], replay["decision_sequence"]) == (
        "APPROVED",
        True,
        1,
    )
    with harness.repository.sessions() as session:
        # No fan-out (PR 6): no change request exists.
        assert session.scalars(select(ChangeRequest)).all() == []


@pytest.mark.parametrize(
    ("binding", "phrase", "actor", "message"),
    [
        ("0" * 64, APPROVAL_PHRASE, OPERATOR, "binding does not match"),
        (None, "APPROVE_UI_BLUEPRINT", OPERATOR, "typed confirmation must be"),
        (None, APPROVAL_PHRASE, "dashboard:someone", "allowlisted operator channel"),
    ],
)
def test_approval_refuses_a_mismatched_binding_phrase_or_actor(
    tmp_path: Path, binding: str | None, phrase: str, actor: str, message: str
) -> None:
    harness = PlanHarness(tmp_path)
    identity, bound = _candidate(harness)
    with pytest.raises(ValueError, match=message):
        harness.repository.approve_sync(
            identity, binding or bound, phrase, actor, pack_revision(harness.pack)
        )
    assert _row(harness, identity).status == "CANDIDATE"


def test_approval_refuses_a_pack_that_drifted_since_the_start(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    identity, binding = _candidate(harness)
    skill = harness.pack / "skills" / "review-decomposition" / "SKILL.md"
    skill.write_text(skill.read_text() + "\nA changed rule.\n")
    with pytest.raises(ValueError, match="DECOMPOSITION_PACK_DRIFT"):
        harness.repository.approve_sync(
            identity, binding, APPROVAL_PHRASE, OPERATOR, pack_revision(harness.pack)
        )
    assert _row(harness, identity).status == "CANDIDATE"


@pytest.mark.parametrize("field", ["plan", "inventory", "source"])
def test_approval_refuses_a_tampered_record(tmp_path: Path, field: str) -> None:
    harness = PlanHarness(tmp_path)
    identity, binding = _candidate(harness)
    row = _row(harness, identity)
    values: dict[str, Any] = {}
    if field == "plan":
        plan = copy.deepcopy(row.plan_json)
        plan["stories"][0]["tasks"][0]["title"] = "Something else"  # type: ignore[index]
        values["plan_json"] = plan
    elif field == "inventory":
        parts = copy.deepcopy(row.parts_json)
        parts["inventory"]["agents_md"] = "changed"
        values["parts_json"] = parts
    else:
        values["issues_sha256"] = "e" * 64
    with harness.repository.sessions.begin() as session:
        session.execute(
            update(BacklogDecomposition).where(BacklogDecomposition.id == identity).values(**values)
        )
    with pytest.raises(ValueError, match="no longer"):
        harness.repository.approve_sync(
            identity, binding, APPROVAL_PHRASE, OPERATOR, pack_revision(harness.pack)
        )


def test_only_a_candidate_can_be_approved_and_a_newer_candidate_supersedes(
    tmp_path: Path,
) -> None:
    harness = PlanHarness(tmp_path)
    created = harness.create()
    with pytest.raises(ValueError, match="only a CANDIDATE"):
        harness.repository.approve_sync(
            _identity(created), "0" * 64, APPROVAL_PHRASE, OPERATOR, pack_revision(harness.pack)
        )
    identity, binding = _candidate(harness)
    _newer, _ = _candidate(harness)
    with pytest.raises(ValueError, match="only a CANDIDATE"):
        harness.repository.approve_sync(
            identity, binding, APPROVAL_PHRASE, OPERATOR, pack_revision(harness.pack)
        )
    assert _row(harness, identity).status == "SUPERSEDED"
