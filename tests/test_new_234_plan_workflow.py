"""NEW-234 PR5: BacklogDecompositionPlanWorkflow on a real local Temporal.

Controller steps run the real DecompositionActivityHandler against SQLite; the snapshot,
GitHub, scan and provider steps are scripted (as in the UI plan workflow tests). Every
recorded history replays against the current workflow code.
"""

from __future__ import annotations

import inspect
import shutil
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from new_234_decomposition_fixtures import SOURCE_SHA, epic_output, frame_output, inventory
from new_234_plan_fixtures import (
    OPERATOR,
    WORKTREE,
    PlanHarness,
    agent_result,
    draft_for,
    review_for,
    stage_document,
    stage_inputs,
)
from pre_rejection_budget_decomposition_workflow import (
    PreRejectionBudgetDecompositionPlanWorkflow,
)
from sqlalchemy import select, update
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from test_new_234_bounded_inputs import large_frame

from engineering_control.activities import backlog_decomposition as activities
from engineering_control.activities.core import ControlActivities
from engineering_control.db.models import BacklogDecomposition, Run
from engineering_control.engineering.decomposition import (
    DecompositionRejected,
    PlanProblem,
    decomposition_workflow_id,
    part_content_sha256,
)
from engineering_control.workflows.definitions import BacklogDecompositionPlanWorkflow
from engineering_control.workflows.inputs import BacklogDecompositionPlanInput
from engineering_control.workflows.worker_registry import (
    CONTROL_TASK_QUEUE,
    GITHUB_TASK_QUEUE,
    ROLE_TASK_QUEUES,
)

_TEMPORAL_CLI = shutil.which("temporal")
pytestmark = pytest.mark.skipif(
    _TEMPORAL_CLI is None, reason="requires the `temporal` CLI on PATH (local dev server)"
)
Responder = Callable[[dict[str, Any]], Any]
PART_LOOP = [
    "decomposition.prepare_part",
    "agent.record_start",
    "decomposition.draft",
    "agent.persist_result",
    "decomposition.record_part_draft",
    "decomposition.stage_document",
    "decomposition.prepare_part_review",
    "agent.record_start",
    "decomposition.review",
    "agent.persist_result",
    "decomposition.record_part_review",
]
PART = ["decomposition.prepare_part_input", "decomposition.stage_inputs", *PART_LOOP]


class WorkflowHarness(PlanHarness):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.calls: list[str] = []
        self.queues: dict[str, set[str]] = {}
        self.responses: dict[str, Responder] = {
            "project.snapshot": lambda payload: {
                **payload,
                "status": "SNAPSHOT_VERIFIED",
                "source_worktree": WORKTREE,
                "source_sha": SOURCE_SHA,
            },
            "decomposition.read_issues": lambda payload: {
                "status": "ISSUES_READ",
                "repository": "gntik-ai/falcone-serverless",
                "issues": [],
                "listed": 5,
            },
            "decomposition.inventory": lambda payload: {
                "status": "INVENTORY_PASS",
                "inventory": inventory(),
                "inventory_sha256": inventory()["inventory_sha256"],
            },
            "decomposition.stage_inputs": stage_inputs,
            "decomposition.stage_document": stage_document,
            "decomposition.draft": lambda payload: agent_result(payload, draft_for(payload)),
            "decomposition.review": lambda payload: agent_result(payload, review_for(payload)),
        }

    def activity(self) -> Any:
        @activity.defn(name="execute_control_step")
        async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
            step = request["step"]
            payload = ControlActivities._attach_orchestration_context(request["payload"])
            self.calls.append(step)
            self.queues.setdefault(step, set()).add(activity.info().task_queue)
            if step in self.handler.supported_steps:
                return await self.handler.handle(step, payload)
            if step in {"agent.record_start", "agent.persist_result"}:
                return payload
            answer = self.responses[step](payload)
            return await answer if inspect.isawaitable(answer) else answer

        return execute_control_step

    async def run(
        self, created: dict[str, Any], *, workflow_cls: type = BacklogDecompositionPlanWorkflow
    ) -> dict[str, Any]:
        assert _TEMPORAL_CLI is not None
        request = BacklogDecompositionPlanInput(
            decomposition_id=created["id"], project="falcone-serverless", requested_by=OPERATOR
        )
        queues = (
            CONTROL_TASK_QUEUE,
            GITHUB_TASK_QUEUE,
            ROLE_TASK_QUEUES["claude"],
            ROLE_TASK_QUEUES["codex"],
            ROLE_TASK_QUEUES["scan"],
        )
        activity_fn = self.activity()
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=_TEMPORAL_CLI
        ) as env:
            async with AsyncExitStack() as stack:
                for queue in queues:
                    await stack.enter_async_context(
                        Worker(
                            env.client,
                            task_queue=queue,
                            workflows=[workflow_cls],
                            activities=[activity_fn],
                        )
                    )
                handle = await env.client.start_workflow(
                    workflow_cls.run,
                    request,
                    id=decomposition_workflow_id("falcone-serverless", created["id"]),
                    task_queue=CONTROL_TASK_QUEUE,
                    execution_timeout=timedelta(minutes=3),
                )
                result: dict[str, Any] = await handle.result()
                history = await handle.fetch_history()
        # Every history (one the pre-budget class recorded included) replays on the current.
        await Replayer(workflows=[BacklogDecompositionPlanWorkflow]).replay_workflow(history)
        self.history = history
        return result

    def row(self, created: dict[str, Any]) -> BacklogDecomposition:
        with self.repository.sessions() as session:
            row = session.get(BacklogDecomposition, UUID(created["id"]))
            assert row is not None
            return row

    def run_status(self, created: dict[str, Any]) -> str:
        with self.repository.sessions() as session:
            run = session.scalars(
                select(Run).where(Run.workflow_id == created["workflow_id"])
            ).one()
            return run.status


async def test_the_plan_reaches_candidate_with_one_review_per_part(tmp_path: Path) -> None:
    harness = WorkflowHarness(tmp_path)
    created = harness.create()
    result = await harness.run(created)
    assert result["status"] == "CANDIDATE", result
    assert (result["parts"], result["stories"], result["tasks"]) == (3, 3, 4)
    assert harness.calls == [
        "decomposition.register",
        "project.snapshot",
        "decomposition.read_issues",
        "decomposition.inventory",
        "decomposition.record_inputs",
        *PART,
        *PART,
        *PART,
        "decomposition.assemble",
        "decomposition.stage_document",
        "decomposition.persist",
        "decomposition.finalize_run",
    ]
    # Fixed opposite providers; the scan worker stages; the GitHub worker reads issues.
    assert harness.queues["decomposition.draft"] == {ROLE_TASK_QUEUES["claude"]}
    assert harness.queues["decomposition.review"] == {ROLE_TASK_QUEUES["codex"]}
    for step in ("decomposition.inventory", "decomposition.stage_inputs", "project.snapshot"):
        assert harness.queues[step] == {ROLE_TASK_QUEUES["scan"]}
    assert harness.queues["decomposition.read_issues"] == {GITHUB_TASK_QUEUE}
    assert harness.queues["decomposition.persist"] == {CONTROL_TASK_QUEUE}
    row = harness.row(created)
    assert row.status == "CANDIDATE" and row.provider_calls == 6
    assert row.approval_binding_sha256 == result["approval_binding"]
    assert harness.run_status(created) == "SUCCESS"


async def test_a_schema_refusal_and_a_revise_use_the_parts_redrafts(tmp_path: Path) -> None:
    harness = WorkflowHarness(tmp_path)
    created = harness.create()
    drafts: list[str] = []

    def draft(payload: dict[str, Any]) -> dict[str, Any]:
        drafts.append(payload["part_id"])
        if len(drafts) == 1:
            raise ApplicationError(
                "draft failed its schema",
                {"usage": {"input_tokens": 5}, "provider": "claude", "run_id": "r",
                 "findings": ["frame.contracts[0]: missing kind"]},
                type="SchemaViolation",
                non_retryable=True,
            )  # fmt: skip
        return agent_result(payload, draft_for(payload))

    reviews: list[str] = []

    def review(payload: dict[str, Any]) -> dict[str, Any]:
        reviews.append(payload["part_id"])
        first_epic = payload["part"]["number"] == 2 and reviews.count(payload["part_id"]) == 1
        return agent_result(payload, review_for(payload, "REVISE" if first_epic else "APPROVE"))

    harness.responses["decomposition.draft"] = draft
    harness.responses["decomposition.review"] = review
    result = await harness.run(created)
    assert result["status"] == "CANDIDATE", result
    # The frame: refused draft 1, draft 2 approved; RUN: REVISE then approved; API: once.
    assert len(drafts) == 5 and len(reviews) == 4
    row = harness.row(created)
    parts = row.parts_json["parts"]
    frame = next(entry for entry in parts.values() if entry["kind"] == "frame")
    assert frame["approved_attempt"] == 2
    assert frame["rejections"][0]["code"] == "DRAFT_SCHEMA_INVALID"
    run = next(entry for entry in parts.values() if entry["number"] == 2)
    assert run["approved_attempt"] == 2 and len(run["versions"]) == 2
    # The refused call's usage still counts (recorded by the next draft preparation).
    assert any(item["input_tokens"] == 5 for item in row.evidence_json["usage"])
    assert row.provider_calls == 9


async def test_an_assembly_problem_is_redrafted_by_its_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = WorkflowHarness(tmp_path)
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
    result = await harness.run(created)
    assert result["status"] == "CANDIDATE", result
    assembly = harness.calls.index("decomposition.assemble")
    # Only the owning part (RUN) is redrafted, as its attempt 2, then the plan reassembles.
    assert harness.calls[assembly + 1 : assembly + 1 + len(PART_LOOP)] == PART_LOOP
    assert harness.calls.count("decomposition.assemble") == 2
    parts = harness.row(created).parts_json["parts"]
    run = next(entry for entry in parts.values() if entry["number"] == 2)
    assert run["approved_attempt"] == 2
    assert run["rejections"] == [
        {
            "attempt": 1,
            "code": "ASSEMBLY_INVALID",
            "details": [
                {
                    "code": "DEPENDENCY_UNRESOLVED",
                    "category": "dependency",
                    "subject": "falcone-serverless:RUN-01.t1",
                    "detail": "depends on a missing task",
                }
            ],
        }
    ]


async def test_the_provider_call_budget_blocks_the_plan(tmp_path: Path) -> None:
    harness = WorkflowHarness(tmp_path)
    created = harness.create()
    with harness.repository.sessions.begin() as session:
        session.execute(
            update(BacklogDecomposition)
            .where(BacklogDecomposition.id == UUID(created["id"]))
            .values(
                parts_json={
                    "requested_epics": None,
                    "parts": {},
                    "calls": [f"earlier-{index}" for index in range(88)],
                }
            )
        )
    result = await harness.run(created)
    assert (result["status"], result["error_code"]) == ("BLOCKED", "DECOMPOSITION_BUDGET_EXHAUSTED")
    row = harness.row(created)
    assert (row.status, row.provider_calls) == ("BLOCKED", 90)
    # The frame was drafted and reviewed (calls 89 and 90); the first epic draft is refused.
    assert harness.calls.count("decomposition.draft") == 1
    assert harness.run_status(created) == "FAILED"


async def test_source_drift_and_an_expired_login_block_the_plan(tmp_path: Path) -> None:
    harness = WorkflowHarness(tmp_path)
    created = harness.create()
    harness.responses["decomposition.inventory"] = lambda payload: {
        "status": "BLOCKED",
        "blocker": "DECOMPOSITION_SOURCE_DRIFT",
        "blockers": ["issue 3 (API-01) body differs from backlog.json"],
    }
    result = await harness.run(created)
    assert (result["status"], result["error_code"]) == ("BLOCKED", "DECOMPOSITION_SOURCE_DRIFT")
    assert harness.row(created).blockers_json == ["issue 3 (API-01) body differs from backlog.json"]
    assert "decomposition.record_inputs" not in harness.calls

    (tmp_path / "login").mkdir()
    login = WorkflowHarness(tmp_path / "login")
    created = login.create()

    def expired(payload: dict[str, Any]) -> dict[str, Any]:
        raise ApplicationError(
            "claude login expired", type="MANUAL_AUTH_REQUIRED_CLAUDE", non_retryable=True
        )

    login.responses["decomposition.draft"] = expired
    result = await login.run(created)
    assert (result["status"], result["error_code"]) == ("BLOCKED", "MANUAL_AUTH_REQUIRED_CLAUDE")
    assert login.calls.count("decomposition.draft") == 1


async def test_a_failed_provider_call_is_never_retried_uncharged(tmp_path: Path) -> None:
    harness = WorkflowHarness(tmp_path)
    created = harness.create()

    def flaky(payload: dict[str, Any]) -> dict[str, Any]:
        raise ApplicationError("provider worker lost", type="ProviderWorkerLost")

    harness.responses["decomposition.review"] = flaky
    result = await harness.run(created)
    # A retryable failure would get four more attempts under ACTIVITY_RETRY.
    assert harness.calls.count("decomposition.review") == 1
    assert (result["status"], result["error_code"]) == ("BLOCKED", "PROVIDER_ACTIVITY_FAILED")
    assert harness.row(created).provider_calls == 2


async def test_a_large_frame_stages_the_epic_input_as_pieces_and_replays(tmp_path: Path) -> None:
    harness = WorkflowHarness(tmp_path)
    created = harness.create()
    big = large_frame()
    prepared: list[dict[str, Any]] = []

    def draft(payload: dict[str, Any]) -> dict[str, Any]:
        prepared.append(payload)
        if payload["part"]["kind"] == "frame":
            return agent_result(payload, frame_output(big, part=payload["part_id"]))
        content = draft_for(payload)["epic"]
        output = epic_output(content, payload["part_id"], frame_sha256=part_content_sha256(big))
        return agent_result(payload, output)

    harness.responses["decomposition.draft"] = draft
    result = await harness.run(created)
    assert result["status"] == "CANDIDATE", result
    epics = [payload for payload in prepared if payload["part"]["kind"] == "epic"]
    for payload in epics:
        files = payload["part_input_files"]
        # The epic input carries the 60-contract frame: staged as pieces, all prompted.
        assert len(files) > 1 and payload["part_input"] == files[0]
        assert all(item["bytes"] <= 64 * 1024 for item in files)
        prompt = payload["draft_request"]["prompt"]
        assert all(item["path"] in prompt for item in files)


# The per-part rejection budget (operator 2026-10-23, run 46ffe364) ---------------------------


def _rejecting(harness: WorkflowHarness, number: int | None, times: int) -> list[int]:
    """Drafts of part `number` (None: the frame) bound to another design, `times` times,
    so Hermes's checks refuse them before any review; the attempts are recorded."""
    attempts: list[int] = []

    def draft(payload: dict[str, Any]) -> dict[str, Any]:
        output = draft_for(payload)
        if payload["part"]["number"] == number:
            attempts.append(payload["draft_attempt"])
            if len(attempts) <= times:
                output["inputs"]["design_sha256"] = "0" * 64
        return agent_result(payload, output)

    harness.responses["decomposition.draft"] = draft
    return attempts


def _revising(harness: WorkflowHarness, number: int | None, times: int) -> list[str]:
    decisions: list[str] = []

    def review(payload: dict[str, Any]) -> dict[str, Any]:
        mine = payload["part"]["number"] == number
        decision = "REVISE" if mine and decisions.count("REVISE") < times else "APPROVE"
        if mine:
            decisions.append(decision)
        return agent_result(payload, review_for(payload, decision))

    harness.responses["decomposition.review"] = review
    return decisions


async def test_a_hermes_rejection_does_not_consume_a_reviewed_draft(tmp_path: Path) -> None:
    harness = WorkflowHarness(tmp_path)
    created = harness.create()
    attempts = _rejecting(harness, 2, times=1)
    decisions = _revising(harness, 2, times=2)
    result = await harness.run(created)
    # One refused draft, then three reviewed drafts (two REVISE): before the budget the
    # third draft was the last, and its REVISE left the part unresolved.
    assert result["status"] == "CANDIDATE", result
    assert attempts == [1, 2, 3, 4] and decisions == ["REVISE", "REVISE", "APPROVE"]
    run = next(
        entry for entry in harness.row(created).parts_json["parts"].values() if entry["number"] == 2
    )
    assert run["approved_attempt"] == 4 and [r["attempt"] for r in run["rejections"]] == [1]


async def test_three_reviewed_drafts_still_end_unresolved(tmp_path: Path) -> None:
    harness = WorkflowHarness(tmp_path)
    created = harness.create()
    _rejecting(harness, None, times=1)
    decisions = _revising(harness, None, times=9)
    result = await harness.run(created)
    assert (result["status"], result["error_code"]) == (
        "BLOCKED",
        "DECOMPOSITION_PART_REVIEW_UNRESOLVED",
    )
    assert decisions == ["REVISE"] * 3
    assert harness.calls.count("decomposition.draft") == 4


async def test_a_third_rejection_blocks_the_part_with_its_own_code(tmp_path: Path) -> None:
    harness = WorkflowHarness(tmp_path)
    created = harness.create()
    attempts = _rejecting(harness, None, times=9)
    result = await harness.run(created)
    assert (result["status"], result["error_code"]) == (
        "BLOCKED",
        "DECOMPOSITION_PART_REJECTIONS_EXHAUSTED",
    )
    assert attempts == [1, 2, 3] and harness.calls.count("decomposition.review") == 0
    assert harness.row(created).provider_calls == 3


async def test_rejections_stay_within_the_plans_provider_call_cap(tmp_path: Path) -> None:
    harness = WorkflowHarness(tmp_path)
    created = harness.create()
    with harness.repository.sessions.begin() as session:
        session.execute(
            update(BacklogDecomposition)
            .where(BacklogDecomposition.id == UUID(created["id"]))
            .values(
                parts_json={
                    "requested_epics": None,
                    "parts": {},
                    "calls": [f"earlier-{index}" for index in range(88)],
                }
            )
        )
    _rejecting(harness, None, times=2)
    result = await harness.run(created)
    # Two refused frame drafts use the last two calls; the third draft is refused.
    assert (result["status"], result["error_code"]) == ("BLOCKED", "DECOMPOSITION_BUDGET_EXHAUSTED")
    assert harness.row(created).provider_calls == 90


async def test_a_pre_budget_history_replays_on_the_current_workflow(tmp_path: Path) -> None:
    harness = WorkflowHarness(tmp_path)
    created = harness.create()
    _rejecting(harness, None, times=1)
    _revising(harness, None, times=9)
    # The pre-budget class: the refused draft used one of three drafts, so two reviews.
    result = await harness.run(created, workflow_cls=PreRejectionBudgetDecompositionPlanWorkflow)
    assert (result["status"], result["error_code"]) == (
        "BLOCKED",
        "DECOMPOSITION_PART_REVIEW_UNRESOLVED",
    )
    assert harness.calls.count("decomposition.review") == 2
    # Replayed above on the current class (no marker in the history: the old budget).
    assert not any(
        event.HasField("marker_recorded_event_attributes") for event in harness.history.events
    )
