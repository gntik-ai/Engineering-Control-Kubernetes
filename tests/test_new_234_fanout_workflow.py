"""NEW-234 PR6: BacklogDecompositionFanOutWorkflow on a real local Temporal.

The fan-out and change-request controller steps run the real handlers against SQLite; the
snapshot, Codex review and GitHub title search are scripted. Every recorded history
replays against the current workflow code.
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
from new_234_fanout_fixtures import APPROVE, KEYS, FanoutHarness, worktree
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from engineering_control.activities import decomposition_fanout as fanout_activities
from engineering_control.activities.core import ControlActivities
from engineering_control.workflows.definitions import (
    ALL_WORKFLOWS,
    BacklogDecompositionFanOutWorkflow,
    ControlledWorkflow,
    UiBlueprintFanOutWorkflow,
)
from engineering_control.workflows.inputs import BacklogDecompositionFanOutInput
from engineering_control.workflows.worker_registry import (
    CONTROL_TASK_QUEUE,
    GITHUB_TASK_QUEUE,
    ROLE_TASK_QUEUES,
)

_TEMPORAL_CLI = shutil.which("temporal")
temporal = pytest.mark.skipif(
    _TEMPORAL_CLI is None, reason="requires the `temporal` CLI on PATH (local dev server)"
)
Responder = Callable[[dict[str, Any]], Any]
ITEM = [
    "decomposition_fanout.prepare_item",
    "decomposition_fanout.prepare_review",
    "agent.record_start",
    "change_request.review",
    "agent.persist_result",
    "change_request.record_review",
    "change_request.github_dedupe",
    "change_request.persist",
]


def test_the_fanout_steps_route_to_the_controller_and_are_registered() -> None:
    queue = ControlledWorkflow._activity_queue
    for step in fanout_activities.DecompositionFanoutActivityHandler.supported_steps:
        assert queue(step, {}) == CONTROL_TASK_QUEUE
        assert step in ControlActivities.steps_for_worker("controller")
        for kind in ("claude", "codex", "scan", "github", "browser", "delivery", "release"):
            assert step not in ControlActivities.steps_for_worker(kind)
    assert queue("change_request.review", {}) == ROLE_TASK_QUEUES["codex"]
    assert queue("change_request.github_dedupe", {}) == GITHUB_TASK_QUEUE
    assert BacklogDecompositionFanOutWorkflow in ALL_WORKFLOWS
    assert UiBlueprintFanOutWorkflow in ALL_WORKFLOWS


class WorkflowHarness(FanoutHarness):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.calls: list[str] = []
        self.queues: dict[str, set[str]] = {}
        self.responses: dict[str, Responder] = {
            "project.snapshot": lambda payload: {
                **payload,
                "status": "SNAPSHOT_VERIFIED",
                "source_worktree": worktree(payload["project"], "source_worktree"),
                "deployment_worktree": worktree(payload["project"], "deployment_worktree"),
            },
            "change_request.review": lambda payload: {
                **payload,
                "status": "SUCCESS",
                "agent_result": {"structured_result": APPROVE},
            },
            "change_request.github_dedupe": lambda payload: {
                **payload,
                "dedupe": {
                    "status": "CLEAR",
                    "matches": [],
                    "searched_repository": payload["target_repository"],
                },
            },  # fmt: skip
        }

    def activity(self) -> Any:
        @activity.defn(name="execute_control_step")
        async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
            step = request["step"]
            payload = ControlActivities._attach_orchestration_context(request["payload"])
            self.calls.append(step)
            self.queues.setdefault(step, set()).add(activity.info().task_queue)
            if step in self.fanout.supported_steps:
                return await self.fanout.handle(step, payload)
            if step in self.cr.supported_steps:
                return await self.cr.handle(step, payload)
            if step in {"agent.record_start", "agent.persist_result"}:
                return payload
            answer = self.responses[step](payload)
            return await answer if inspect.isawaitable(answer) else answer

        return execute_control_step

    async def run(self, decomposition: dict[str, Any], **options: Any) -> dict[str, Any]:
        assert _TEMPORAL_CLI is not None
        claim = self.repository.claim_fanout_sync(UUID(decomposition["id"]))
        request = BacklogDecompositionFanOutInput(decomposition_id=decomposition["id"], **options)
        queues = (
            CONTROL_TASK_QUEUE,
            GITHUB_TASK_QUEUE,
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
                            workflows=[BacklogDecompositionFanOutWorkflow],
                            activities=[activity_fn],
                        )
                    )
                handle = await env.client.start_workflow(
                    BacklogDecompositionFanOutWorkflow.run,
                    request,
                    id=claim["workflow_id"],
                    task_queue=CONTROL_TASK_QUEUE,
                    execution_timeout=timedelta(minutes=3),
                )
                result: dict[str, Any] = await handle.result()
                history = await handle.fetch_history()
        await Replayer(workflows=[BacklogDecompositionFanOutWorkflow]).replay_workflow(history)
        return result


@temporal
async def test_the_fanout_drafts_every_task_once_and_resumes_idempotently(
    tmp_path: Path,
) -> None:
    harness = WorkflowHarness(tmp_path)
    approved = harness.approved()
    result = await harness.run(approved)
    assert result["status"] == "COMPLETED", result
    assert (result["drafted"], result["failed"], result["skipped"]) == (4, 0, 0)
    assert harness.calls == [
        "decomposition_fanout.plan",
        "project.snapshot",
        "project.snapshot",
        *ITEM * 4,
        "decomposition_fanout.finalize",
    ]
    assert harness.queues["change_request.review"] == {ROLE_TASK_QUEUES["codex"]}
    assert harness.queues["change_request.github_dedupe"] == {GITHUB_TASK_QUEUE}
    assert harness.queues["project.snapshot"] == {ROLE_TASK_QUEUES["scan"]}
    assert harness.queues["change_request.persist"] == {CONTROL_TASK_QUEUE}
    for key in KEYS:
        assert harness.request(approved, key)["status"] == "CANDIDATE"
    view = harness.repository.get_sync(UUID(approved["id"]))
    assert view is not None and view["fanout_workflow_id"] is None
    # An approval replay starts a new execution; nothing is drafted twice.
    harness.calls.clear()
    again = await harness.run(approved)
    assert (again["drafted"], again["processed"]) == (0, 0)
    assert harness.calls == ["decomposition_fanout.plan", "decomposition_fanout.finalize"]
    assert len(harness.rows()) == 4


@temporal
async def test_batches_continue_as_new_up_to_max_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fanout_activities, "FANOUT_BATCH", 2)
    harness = WorkflowHarness(tmp_path)
    approved = harness.approved()
    result = await harness.run(approved, max_items=3)
    assert (result["status"], result["processed"], result["drafted"]) == ("COMPLETED", 3, 3)
    assert result["pending"] == 1
    assert harness.calls.count("decomposition_fanout.plan") == 2
    # The snapshots taken by the first run are carried into the next (continue-as-new).
    assert harness.calls.count("project.snapshot") == 2


@temporal
async def test_an_unavailable_snapshot_blocks_and_releases_the_marker(tmp_path: Path) -> None:
    harness = WorkflowHarness(tmp_path)
    approved = harness.approved()

    def unavailable(payload: dict[str, Any]) -> dict[str, Any]:
        raise ApplicationError("mirror missing", non_retryable=True)

    harness.responses["project.snapshot"] = unavailable
    result = await harness.run(approved)
    assert (result["status"], result["error_code"]) == (
        "BLOCKED",
        "DECOMPOSITION_FANOUT_SNAPSHOT_UNAVAILABLE",
    )
    assert harness.rows() == []
    view = harness.repository.get_sync(UUID(approved["id"]))
    assert view is not None and view["fanout_workflow_id"] is None
    assert view["fanout"]["last_run"]["blocker"] == "DECOMPOSITION_FANOUT_SNAPSHOT_UNAVAILABLE"


@temporal
async def test_a_failed_review_is_recorded_for_the_retry(tmp_path: Path) -> None:
    harness = WorkflowHarness(tmp_path)
    approved = harness.approved()

    def review(payload: dict[str, Any]) -> dict[str, Any]:
        verdict = {"decision": "maybe"} if payload["item_key"] == KEYS[1] else APPROVE
        return {**payload, "status": "SUCCESS", "agent_result": {"structured_result": verdict}}

    harness.responses["change_request.review"] = review
    result = await harness.run(approved)
    assert (result["drafted"], result["failed"]) == (3, 1)
    assert result["failed_items"][0]["item_key"] == "API-01.t1"
    failed = harness.request(approved, KEYS[1])
    assert failed["status"] == "FAILED" and failed["failure"]["phase"] == "review"
