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

import asyncio
import shutil
import uuid
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from engineering_control.workflows.definitions import (
    ConnectionsRefreshWorkflow,
    ExistingGitHubBacklogLoopWorkflow,
    FinalClosureWorkflow,
    FindingVerificationWorkflow,
    IssueDeliveryWorkflow,
)
from engineering_control.workflows.inputs import (
    ConnectionsRefreshInput,
    ExistingGitHubBacklogLoopInput,
    FinalClosureInput,
    FindingVerificationInput,
    IssueDeliveryInput,
)
from engineering_control.workflows.worker_registry import (
    CONTROL_TASK_QUEUE,
    GITHUB_TASK_QUEUE,
    ROLE_TASK_QUEUES,
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


def _scripted_execute_control_step_with_failure(
    script: dict[str, dict[str, Any]], *, fails_on: str
) -> Any:
    """Like _scripted_execute_control_step, but one named step raises a
    non-retryable ApplicationError instead of returning -- reproducing
    exactly the shape of failure a missing ControlActivities._allowed_steps
    entry produces in production (NEW-16, 2026-09-22).
    """

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        step = request["step"]
        if step == fails_on:
            raise ApplicationError("unknown or invalid control step", non_retryable=True)
        if step not in script:
            raise AssertionError(f"unscripted step {step!r} invoked with {request['payload']!r}")
        return script[step]

    return execute_control_step


async def _run_to_history(
    workflow_cls: type, run_arg: Any, activity_fn: Any, *, child_workflows: tuple[type, ...] = ()
) -> Any:
    # FinalClosureWorkflow's GitHub-write steps (final.merge_source,
    # final.inspect_artifact, ...) dispatch to GITHUB_TASK_QUEUE rather than
    # the default CONTROL_TASK_QUEUE, and FindingVerificationWorkflow's
    # finding.reproduce routes to a provider queue (claude/codex) -- see
    # ControlledWorkflow._activity_queue -- so every role queue needs a
    # worker regardless of which workflow is under test.
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=_TEMPORAL_CLI) as env:
        async with (
            Worker(
                env.client,
                task_queue=CONTROL_TASK_QUEUE,
                workflows=[workflow_cls, *child_workflows],
                activities=[activity_fn],
            ),
            Worker(
                env.client,
                task_queue=GITHUB_TASK_QUEUE,
                workflows=[workflow_cls, *child_workflows],
                activities=[activity_fn],
            ),
            Worker(
                env.client,
                task_queue=ROLE_TASK_QUEUES["claude"],
                workflows=[workflow_cls, *child_workflows],
                activities=[activity_fn],
            ),
            Worker(
                env.client,
                task_queue=ROLE_TASK_QUEUES["codex"],
                workflows=[workflow_cls, *child_workflows],
                activities=[activity_fn],
            ),
            Worker(
                env.client,
                task_queue=ROLE_TASK_QUEUES["scan"],
                workflows=[workflow_cls, *child_workflows],
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
    activity_fn = _scripted_execute_control_step({"connections.refresh": {"status": "COMPLETE"}})
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


async def test_existing_backlog_loop_workflow_records_failure_when_register_run_itself_fails() -> (
    None
):
    """Regression test for a real production incident (2026-09-22): the first
    time this workflow's backlog.register_run call was ever genuinely
    exercised, it failed with a non-retryable ApplicationError (a missing
    ControlActivities._allowed_steps entry) -- and because that call sat
    outside the workflow's try/except, backlog.record_loop_state never ran,
    campaign.status stayed stuck at RUNNING (set moments earlier inside
    backlog.load_execution), and the campaign permanently blocked GitHub
    write-policy ownership for every other campaign until its ~24h approval
    TTL expired, with no exposed way to release it early.

    Fixed by moving the register_run call inside the try block. This test
    scripts register_run itself to fail (not a later step) and asserts
    record_loop_state still gets called with loop_status=FAILED before the
    workflow re-raises.
    """
    activity_fn = _scripted_execute_control_step_with_failure(
        {
            "backlog.load_execution": {"status": "READY", "items": []},
            "backlog.record_loop_state": {"status": "RECORDED"},
        },
        fails_on="backlog.register_run",
    )
    run_arg = ExistingGitHubBacklogLoopInput(
        project="falcone",
        backlog_campaign_id="camp-1",
        maximum_risk="low",
        delivery_mode="source-pr",
    )
    history, result = await _run_to_history(ExistingGitHubBacklogLoopWorkflow, run_arg, activity_fn)
    assert result is None  # the workflow is expected to fail (register_run's ApplicationError)

    # Confirm record_loop_state actually ran (with loop_status=FAILED) before
    # the workflow failed -- i.e. this isn't just "the workflow failed", it's
    # specifically "the workflow failed AND still recorded that fact".
    recorded_payloads = [
        event.activity_task_scheduled_event_attributes.input.payloads[0].data
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    assert any(b"record_loop_state" in payload for payload in recorded_payloads)
    assert any(b"FAILED" in payload for payload in recorded_payloads)

    await Replayer(workflows=[ExistingGitHubBacklogLoopWorkflow]).replay_workflow(history)


async def test_issue_delivery_workflow_releases_leases_when_a_later_step_crashes() -> None:
    """Regression test for a real production incident (2026-09-22, NEW-20): the
    first time a delivery ever got past delivery.acquire_leases (NEW-19's
    semgrep bug crashed a later step, delivery.deterministic_prechecks --
    itself now fixed), the crash propagated uncaught out of
    IssueDeliveryWorkflow's flat step loop with no try/except at all, so the
    component-implementation lease acquired moments earlier was never
    released. It then blocked the very next retry's attempt at the same issue
    for the rest of the lease's 4h TTL.

    Fixed by wrapping the step loop in try/except and calling the new
    delivery.release_leases step on any uncaught failure (idempotent: a no-op
    if nothing was ever acquired). This test scripts the step right after
    acquire_leases to fail and asserts delivery.release_leases still gets
    called before the workflow re-raises.
    """
    activity_fn = _scripted_execute_control_step_with_failure(
        {
            "delivery.load_confirmed_finding": {"status": "OK", "run_id": "run-1"},
            "delivery.ensure_issue": {"status": "OK", "run_id": "run-1"},
            "delivery.acquire_leases": {"status": "LEASES_ACQUIRED", "run_id": "run-1"},
            "delivery.release_leases": {"status": "LEASES_RELEASED", "run_id": "run-1"},
        },
        fails_on="delivery.route_providers",
    )
    run_arg = IssueDeliveryInput(finding_id="finding-1", risk_class="R1", dry_run=False)
    history, result = await _run_to_history(IssueDeliveryWorkflow, run_arg, activity_fn)
    assert result is None  # the workflow is expected to fail (the scripted crash)

    recorded_payloads = [
        event.activity_task_scheduled_event_attributes.input.payloads[0].data
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    assert any(b"release_leases" in payload for payload in recorded_payloads)

    await Replayer(workflows=[IssueDeliveryWorkflow]).replay_workflow(history)


async def test_finding_verification_workflow_blocks_gracefully_when_controls_crash() -> None:
    """Regression test for a real production incident (2026-09-22, NEW-21): the
    first time a musematic finding reached finding.controls after two
    successful reproduction attempts, _evaluate_controls raised a bare
    ValueError ("verification attempt is not bound to fresh snapshot
    evidence") -- its own legitimate anti-staleness integrity check, wrapped
    as a non-retryable ApplicationError. FindingVerificationWorkflow (like
    IssueDeliveryWorkflow before NEW-20) had no try/except at all, so this
    crashed the entire parent ExistingGitHubBacklogLoopWorkflow instead of
    just blocking this one issue -- exactly the outcome
    continue_after_blocked_issue exists to avoid.

    Fixed by catching finding.controls' failure and recording a graceful
    BLOCKED terminal state instead -- deliberately NOT weakening
    _evaluate_controls' own integrity check, just making its failure read
    like every other blocked verification. This test scripts finding.controls
    to fail and asserts finding.record_terminal still gets called and the
    workflow returns BLOCKED instead of crashing.
    """
    activity_fn = _scripted_execute_control_step_with_failure(
        {
            "finding.load_candidate": {"status": "CANDIDATE_LOADED", "finding_id": "finding-1"},
            "finding.fresh_snapshot": {"snapshot_evidence_sha256": "a" * 64},
            "finding.prepare_verifier": {"status": "PREPARED"},
            "agent.record_start": {"status": "STARTED"},
            "finding.reproduce": {"status": "SUCCESS"},
            "agent.persist_result": {
                "status": "SUCCESS",
                "agent_result": {"structured_result": {"evidence": []}},
            },
            "finding.record_terminal": {"status": "BLOCKED"},
        },
        fails_on="finding.controls",
    )
    run_arg = FindingVerificationInput(finding_id="finding-1", project="musematic")
    history, result = await _run_to_history(FindingVerificationWorkflow, run_arg, activity_fn)
    assert result == {"status": "BLOCKED", "context": {"status": "BLOCKED"}}

    recorded_payloads = [
        event.activity_task_scheduled_event_attributes.input.payloads[0].data
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    assert any(b"record_terminal" in payload for payload in recorded_payloads)

    await Replayer(workflows=[FindingVerificationWorkflow]).replay_workflow(history)


async def test_existing_backlog_loop_continues_when_a_delivery_child_crashes() -> None:
    """Regression test for a real production incident (2026-09-23, NEW-24): the
    first delivery ever started (musematic #1025, campaign 11e90233) failed in
    delivery.deterministic_prechecks (NEW-23's evidence-ingest 422). The
    IssueDeliveryWorkflow child released its leases and re-raised, and the
    ChildWorkflowError propagated uncaught out of the backlog loop -- the fourth
    instance of the NEW-16/NEW-20/NEW-21 gap -- failing the whole campaign
    instead of blocking one issue.

    Fixed by converting the child failure into the loop's existing FAILED
    delivery result (blocker DELIVERY_CHILD_WORKFLOW_FAILED), so
    continue_after_blocked_issue applies. This test makes the delivery child
    crash and asserts the loop finishes with that blocker recorded.
    """
    activity_fn = _scripted_execute_control_step_with_failure(
        {
            "backlog.load_execution": {
                "status": "READY",
                "items": [{"repository": "gntik-ai/musematic", "issue_number": 1025}],
            },
            "backlog.register_run": {"status": "REGISTERED"},
            "backlog.revalidate_gate": {
                "status": "READY",
                "finding_id": "finding-1",
                "risk_class": "R1",
            },
            "backlog.register_child_run": {"status": "REGISTERED"},
            "delivery.release_leases": {"status": "LEASES_RELEASED"},
            "backlog.record_loop_state": {"status": "RECORDED"},
        },
        fails_on="delivery.load_confirmed_finding",
    )
    run_arg = ExistingGitHubBacklogLoopInput(
        project="musematic",
        backlog_campaign_id="camp-1",
        maximum_risk="R1",
        delivery_mode="source-pr",
    )
    history, result = await _run_to_history(
        ExistingGitHubBacklogLoopWorkflow,
        run_arg,
        activity_fn,
        child_workflows=(IssueDeliveryWorkflow,),
    )
    assert result is not None, "the backlog loop crashed on a failed delivery child"
    assert {"status": "FAILED", "blocker": "DELIVERY_CHILD_WORKFLOW_FAILED", "attempt": 1} in (
        result["results"]
    )

    await Replayer(
        workflows=[ExistingGitHubBacklogLoopWorkflow, IssueDeliveryWorkflow]
    ).replay_workflow(history)


async def _run_with_signals(
    run_arg: Any, activity_fn: Any, signals: list[tuple[str, Any]]
) -> tuple[Any, Any, list[str]]:
    """Run the backlog loop, and each time it pauses for the operator send the next
    signal. Returns history, result and the delivery child workflow ids started."""
    workflows = [ExistingGitHubBacklogLoopWorkflow, IssueDeliveryWorkflow]
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=_TEMPORAL_CLI) as env:
        async with (
            Worker(
                env.client,
                task_queue=CONTROL_TASK_QUEUE,
                workflows=workflows,
                activities=[activity_fn],
            ),
            Worker(
                env.client,
                task_queue=GITHUB_TASK_QUEUE,
                workflows=workflows,
                activities=[activity_fn],
            ),
            Worker(
                env.client,
                task_queue=ROLE_TASK_QUEUES["scan"],
                workflows=workflows,
                activities=[activity_fn],
            ),
        ):
            handle = await env.client.start_workflow(
                ExistingGitHubBacklogLoopWorkflow.run,
                run_arg,
                id=f"replay-test-pause-{uuid.uuid4()}",
                task_queue=CONTROL_TASK_QUEUE,
                execution_timeout=timedelta(minutes=2),
            )
            for name, argument in signals:
                for _ in range(200):
                    state = await handle.query("status")
                    if str(state.get("phase", "")).startswith("AWAITING_OPERATOR"):
                        break
                    description = await handle.describe()
                    assert (
                        description.status is not None and description.status.name == "RUNNING"
                    ), "the loop ended instead of pausing for the operator"
                    await asyncio.sleep(0.1)
                await handle.signal(name, argument)
                await asyncio.sleep(0.5)
            result = await handle.result()
            history = await handle.fetch_history()
    children = [
        event.start_child_workflow_execution_initiated_event_attributes.workflow_id
        for event in history.events
        if event.HasField("start_child_workflow_execution_initiated_event_attributes")
    ]
    return history, result, children


def _paused_delivery_script() -> Any:
    return _scripted_execute_control_step_with_failure(
        {
            "backlog.load_execution": {
                "status": "READY",
                "items": [{"repository": "gntik-ai/musematic", "issue_number": 1026}],
            },
            "backlog.register_run": {"status": "REGISTERED"},
            "backlog.revalidate_gate": {
                "status": "READY",
                "finding_id": "finding-1",
                "risk_class": "R1",
            },
            "backlog.register_child_run": {"status": "REGISTERED"},
            "delivery.release_leases": {"status": "LEASES_RELEASED"},
            "backlog.record_loop_state": {"status": "RECORDED"},
            "backlog.record_skip": {"status": "SKIPPED", "reason_code": "OPERATOR"},
        },
        fails_on="delivery.load_confirmed_finding",
    )


def _single_issue_input() -> ExistingGitHubBacklogLoopInput:
    return ExistingGitHubBacklogLoopInput(
        project="musematic",
        backlog_campaign_id="camp-1",
        maximum_risk="R1",
        delivery_mode="source-pr",
        continue_after_blocked_issue=False,
    )


async def test_blocked_delivery_pauses_until_the_operator_retries_then_cancels() -> None:
    """Regression test for a real production incident (2026-09-23, NEW-33): a
    single-issue solve_issue for musematic #1026 had its delivery fail; the loop
    recorded PAUSED and then ENDED, so campaign 44e4bfd3 stayed PAUSED with no
    workflow any resume/retry/skip signal could reach, still owning the GitHub
    write policy and blocking every later solve_issue for that issue.
    """
    history, result, children = await _run_with_signals(
        _single_issue_input(),
        _paused_delivery_script(),
        [("retry", "gntik-ai/musematic#1026"), ("cancel", None)],
    )

    assert result["status"] == "CANCELLED"
    assert [child.rsplit("-issue-", 1)[1] for child in children] == [
        "1-attempt-1",
        "1-retry-1-attempt-1",
    ]
    await Replayer(
        workflows=[ExistingGitHubBacklogLoopWorkflow, IssueDeliveryWorkflow]
    ).replay_workflow(history)


async def test_blocked_delivery_skip_moves_on_and_completes() -> None:
    history, result, _ = await _run_with_signals(
        _single_issue_input(),
        _paused_delivery_script(),
        [("skip", "gntik-ai/musematic#1026|OPERATOR")],
    )

    assert result["status"] == "COMPLETE"
    assert {"status": "SKIPPED", "reason_code": "OPERATOR"} in result["results"]
    await Replayer(
        workflows=[ExistingGitHubBacklogLoopWorkflow, IssueDeliveryWorkflow]
    ).replay_workflow(history)
