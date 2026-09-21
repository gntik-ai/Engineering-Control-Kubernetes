"""F-11: Temporal replay/determinism coverage.

Prior to this file, nothing in this repo ever executed a workflow against a
real Temporal server and replayed its recorded history against the current
workflow code. That means every `workflow.patched()` fork (there are 8,
across FinalClosureWorkflow and ExistingGitHubBacklogLoopWorkflow) was
trusted to be replay-safe on faith alone -- if a future change removed or
reordered a patch marker incorrectly, nothing would catch it before it broke
an in-flight production workflow.

These tests spin up a real local Temporal dev server (via the `temporal` CLI
already on PATH -- no network download, no time-skipping server needed since
none of the exercised paths sleep), run a workflow end-to-end with a single
scripted `execute_control_step` activity double, then feed the recorded
history through `temporalio.worker.Replayer` against the *current* workflow
code. `Replayer.replay_workflow` raises on any non-determinism.
"""

from __future__ import annotations

import shutil
import uuid
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from engineering_control.workflows.definitions import (
    ConnectionsRefreshWorkflow,
    ExistingGitHubBacklogLoopWorkflow,
    FinalClosureWorkflow,
)
from engineering_control.workflows.inputs import (
    ConnectionsRefreshInput,
    ExistingGitHubBacklogLoopInput,
    FinalClosureInput,
)
from engineering_control.workflows.worker_registry import (
    CONTROL_TASK_QUEUE,
    GITHUB_TASK_QUEUE,
)

_TEMPORAL_CLI = shutil.which("temporal")

pytestmark = pytest.mark.skipif(
    _TEMPORAL_CLI is None,
    reason="requires the `temporal` CLI on PATH to run a local dev server",
)


def _scripted_execute_control_step(script: dict[str, dict[str, Any]]) -> Any:
    """Build an execute_control_step double keyed by step name.

    Each scripted response fully replaces the workflow's `context` dict, the
    same way the real activity result does -- callers only need to include
    whatever keys the workflow reads back out of it.
    """

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        step = request["step"]
        if step not in script:
            raise AssertionError(f"unscripted step {step!r} invoked with {request['payload']!r}")
        return script[step]

    return execute_control_step


async def _run_to_history(workflow_cls: type, run_arg: Any, activity_fn: Any) -> Any:
    # FinalClosureWorkflow's GitHub-write steps (final.merge_source,
    # final.inspect_artifact, ...) dispatch to GITHUB_TASK_QUEUE rather than
    # the default CONTROL_TASK_QUEUE (see ControlledWorkflow._activity_queue),
    # so both queues need a worker regardless of which workflow is under test.
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=_TEMPORAL_CLI
    ) as env:
        async with (
            Worker(
                env.client,
                task_queue=CONTROL_TASK_QUEUE,
                workflows=[workflow_cls],
                activities=[activity_fn],
            ),
            Worker(
                env.client,
                task_queue=GITHUB_TASK_QUEUE,
                workflows=[workflow_cls],
                activities=[activity_fn],
            ),
        ):
            handle = await env.client.start_workflow(
                workflow_cls.run,
                run_arg,
                id=f"replay-test-{workflow_cls.__name__}-{uuid.uuid4()}",
                task_queue=CONTROL_TASK_QUEUE,
                execution_timeout=timedelta(minutes=1),
            )
            try:
                result = await handle.result()
            except WorkflowFailureError:
                result = None
            history = await handle.fetch_history()
    return history, result


async def test_connections_refresh_workflow_replays_cleanly() -> None:
    """Baseline: a workflow with zero patches replays against its own history."""
    activity_fn = _scripted_execute_control_step(
        {"connections.refresh": {"status": "COMPLETE"}}
    )
    history, result = await _run_to_history(
        ConnectionsRefreshWorkflow, ConnectionsRefreshInput(scope="all"), activity_fn
    )
    assert result == {"status": "COMPLETE", "scope": "all", "result": {"status": "COMPLETE"}}
    await Replayer(workflows=[ConnectionsRefreshWorkflow]).replay_workflow(history)


async def test_final_closure_workflow_replays_cleanly_through_hardened_recovery_gate() -> None:
    """Exercises the `final-closure-recovery-v2` patch: a fresh execution takes
    the hardened-recovery branch (skips the pre-merge-persist checkpoint,
    checkpoints before waiting on the build) on its way to a terminal BLOCKED
    result, and that recorded history must replay cleanly against current code.
    """
    activity_fn = _scripted_execute_control_step(
        {
            "final.prepare": {"status": "FINAL_CLOSURE_READY"},
            "final.merge_source": {"status": "MERGED_OK"},
            "final.persist_source_merge": {"status": "SOURCE_MERGE_RECORDED"},
            "final.inspect_artifact": {
                "status": "AWAITING_FINAL_BUILD",
                "blocker": "BUILD_STILL_PENDING",
            },
        }
    )
    run_arg = FinalClosureInput(
        backlog_campaign_id="camp-1",
        change_set_id="cs-1",
        finding_id="F-11",
        project="falcone",
        issue_repository="acme/falcone",
        issue_number=42,
    )
    history, result = await _run_to_history(FinalClosureWorkflow, run_arg, activity_fn)
    assert result == {
        "status": "BLOCKED",
        "blocker": "BUILD_STILL_PENDING",
        "context": {"status": "AWAITING_FINAL_BUILD", "blocker": "BUILD_STILL_PENDING"},
    }
    await Replayer(workflows=[FinalClosureWorkflow]).replay_workflow(history)


async def test_existing_backlog_loop_workflow_replays_cleanly_through_failure_path() -> None:
    """Exercises all 6 patches in ExistingGitHubBacklogLoopWorkflow and the
    NEW-16 failure-recording path: an uncaught exception from a malformed
    `items` payload must still record `backlog.record_loop_state` before
    re-raising, and that history must replay cleanly against current code.
    """
    activity_fn = _scripted_execute_control_step(
        {
            "backlog.load_execution": {"status": "READY", "items": "not-a-list"},
            "backlog.register_run": {"status": "REGISTERED"},
            "backlog.record_loop_state": {"status": "RECORDED"},
        }
    )
    run_arg = ExistingGitHubBacklogLoopInput(
        project="falcone",
        backlog_campaign_id="camp-1",
        maximum_risk="low",
        delivery_mode="source-pr",
    )
    history, result = await _run_to_history(ExistingGitHubBacklogLoopWorkflow, run_arg, activity_fn)
    assert result is None  # the workflow is expected to fail (uncaught TypeError)
    await Replayer(workflows=[ExistingGitHubBacklogLoopWorkflow]).replay_workflow(history)
