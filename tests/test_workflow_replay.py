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
import json
import shutil
import uuid
from datetime import timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from pre_new_113_gitops_workflow import ParentStagingGitOpsDeliveryWorkflow
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from engineering_control.workflows.definitions import (
    ConnectionsRefreshWorkflow,
    ExistingGitHubBacklogLoopWorkflow,
    FinalClosureWorkflow,
    FindingVerificationWorkflow,
    IssueAmendObservationWorkflow,
    IssueDeliveryWorkflow,
    StagingGitOpsDeliveryWorkflow,
)
from engineering_control.workflows.inputs import (
    ConnectionsRefreshInput,
    ExistingGitHubBacklogLoopInput,
    FinalClosureInput,
    FindingVerificationInput,
    IssueAmendObservationInput,
    IssueDeliveryInput,
    StagingGitOpsDeliveryInput,
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


def _registered_child(step: str, request: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    """Complete a scripted child Run registration the way the real activity does.

    Merged with 2b678d9 (child-run-start-memo-v1): the workflow validates that a
    registration echoes its child workflow ID and returns the Run's UUID.
    """
    if (
        step != "backlog.register_child_run"
        or value.get("status") != "REGISTERED"
        or "child_workflow_id" in value
    ):
        return value
    child = request["payload"].get("child_workflow_id")
    return {**value, "child_workflow_id": child, "run_id": str(uuid5(NAMESPACE_URL, str(child)))}


def _scripted_execute_control_step(script: dict[str, dict[str, Any]]) -> Any:
    """Build an execute_control_step double keyed by step name.

    Each scripted response fully replaces the workflow's `context` dict, the
    same way the real activity result does -- callers only need to include
    whatever keys the workflow reads back out of it.
    """

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        step = request["step"]
        if step == "gitops.notify_merged" and step not in script:
            return {"status": "NOTIFIED"}  # NEW-127: each merge is announced at once
        if step == "github.refresh_mirrors" and step not in script:
            return {"status": "PASS"}  # NEW-71: every loop issue refreshes the mirrors
        if step not in script:
            raise AssertionError(f"unscripted step {step!r} invoked with {request['payload']!r}")
        return _registered_child(step, request, script[step])

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
        if step == "gitops.notify_merged" and step not in script:
            return {"status": "NOTIFIED"}  # NEW-127: each merge is announced at once
        if step == "github.refresh_mirrors" and step not in script:
            return {"status": "PASS"}  # NEW-71
        if step not in script:
            raise AssertionError(f"unscripted step {step!r} invoked with {request['payload']!r}")
        return _registered_child(step, request, script[step])

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

    NEW-114: such a FAILED child now pauses the loop instead; this history is recorded
    with the NEW-114 markers off and must replay against the current code unchanged.
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
        _PreNew114BacklogLoopWorkflow,
        run_arg,
        activity_fn,
        child_workflows=(IssueDeliveryWorkflow,),
    )
    assert result is not None, "the backlog loop crashed on a failed delivery child"
    assert any(
        entry.get("status") == "FAILED"
        and entry.get("blocker") == "DELIVERY_CHILD_WORKFLOW_FAILED"
        and entry.get("attempt") == 1
        for entry in result["results"]
    )
    assert result.get("completion") is None
    assert not _new_114_markers(history)

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
    # NEW-33b: the retry preflight reads the loop Run's status, so the pause must be
    # recorded against this workflow (otherwise engineering-ctl backlog-retry is refused).
    loop_states = [
        json.loads(event.activity_task_scheduled_event_attributes.input.payloads[0].data)
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    paused = [
        state["payload"]
        for state in loop_states
        if state.get("step") == "backlog.record_loop_state"
        and state["payload"].get("loop_status") == "PAUSED"
    ]
    assert paused and all(state.get("workflow_id") for state in paused)
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


# -- post-merge staging GitOps flow -----------------------------------------------------

_GITOPS_PLAN = {
    "change_set_id": "00000000-0000-0000-0000-000000000007",
    "project": "musematic",
    "risk_class": "R1",
    "source_repository": "gntik-ai/musematic",
    "source_pr_number": 77,
    "source_pr_url": "https://github.com/gntik-ai/musematic/pull/77",
    "source_head_sha": "a" * 40,
    "source_base_branch": "main",
    "deployment_repository": "gntik-ai/musematic-deploy",
    "deployment_pr_number": None,
    "deployment_pr_url": None,
    "deployment_head_sha": None,
    "deployment_base_branch": "main",
    "issue_repository": "gntik-ai/musematic",
    "issue_number": 1026,
}


def _gitops_script(**overrides: dict[str, Any]) -> dict[str, dict[str, Any]]:
    script: dict[str, dict[str, Any]] = {
        "gitops.prepare": {
            "status": "READY",
            "plan": _GITOPS_PLAN,
            "staging_flow": {
                "argo_application": "musematic-platform",
                "main_ci_workflow": None,
                "image_workflow": "deploy.yml",
                "release_tag_enabled": True,
                "pin_chart_to_merge": True,
            },
        },
        "gitops.pr_checks": {"status": "CHECKS_PASSED"},
        "gitops.merge": {"status": "MERGED", "target": "source", "merge_sha": "d" * 40},
        # NEW-127: each merge is announced right after it succeeded.
        "gitops.notify_merged": {"status": "NOTIFIED"},
        "gitops.release_tag": {"status": "TAG_READY", "release_tag": "v0.0.11"},
        "gitops.workflow_run": {"status": "RUN_SUCCEEDED"},
        "gitops.environment_check": {"status": "HEALTHY", "diagnosis": {}},
        "gitops.image_pin": {
            "status": "PINNED",
            "image_tag": "sha-ddddddd",
            "pinned_repositories": ["ghcr.io/gntik-ai/musematic/web"],
        },
        "gitops.trigger_sync": {
            "status": "SYNC_TRIGGERED",
            "previous_operation_started_at": "",
            "chart_pin": {"changed": True},
        },
        "gitops.sync_status": {"status": "SYNCED", "argo_revision": "d" * 40},
        "gitops.verify_workloads": {"status": "VERIFIED"},
        "gitops.close_issue": {"status": "ISSUE_CLOSED"},
        "gitops.persist_result": {"status": "PERSISTED"},
    }
    script.update(overrides)
    return script


def _gitops_input() -> StagingGitOpsDeliveryInput:
    return StagingGitOpsDeliveryInput(
        change_set_id=_GITOPS_PLAN["change_set_id"],
        project="musematic",
        issue_repository="gntik-ai/musematic",
        issue_number=1026,
    )


def _scheduled_steps(history: Any) -> list[dict[str, Any]]:
    return [
        json.loads(event.activity_task_scheduled_event_attributes.input.payloads[0].data)
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]


async def test_staging_gitops_workflow_happy_path_replays_cleanly() -> None:
    history, result = await _run_to_history(
        StagingGitOpsDeliveryWorkflow,
        _gitops_input(),
        _scripted_execute_control_step(_gitops_script()),
    )

    assert result is not None and result["status"] == "STAGING_VERIFIED"
    assert result["evidence"]["merge_sha"] == "d" * 40
    assert result["evidence"]["release_tag"] == "v0.0.11"
    steps = [request["step"] for request in _scheduled_steps(history)]
    assert steps == [
        "gitops.prepare",
        "gitops.pr_checks",
        "gitops.merge",
        "gitops.notify_merged",
        "gitops.release_tag",
        "gitops.workflow_run",
        "gitops.environment_check",
        "gitops.image_pin",
        "gitops.trigger_sync",
        "gitops.sync_status",
        "gitops.verify_workloads",
        "gitops.close_issue",
        "gitops.persist_result",
    ]
    persisted = _scheduled_steps(history)[-1]["payload"]
    assert persisted["outcome"] == "STAGING_VERIFIED"
    assert persisted["image_tag"] == "sha-ddddddd"
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


async def test_staging_gitops_workflow_failed_checks_block_before_merge() -> None:
    history, result = await _run_to_history(
        StagingGitOpsDeliveryWorkflow,
        _gitops_input(),
        _scripted_execute_control_step(
            _gitops_script(
                **{"gitops.pr_checks": {"status": "BLOCKED", "blocker": "SOURCE_PR_CI_FAILED"}}
            )
        ),
    )

    assert result is not None
    assert (result["status"], result["blocker"]) == ("BLOCKED", "SOURCE_PR_CI_FAILED")
    steps = [request["step"] for request in _scheduled_steps(history)]
    assert steps == ["gitops.prepare", "gitops.pr_checks", "gitops.persist_result"]
    assert _scheduled_steps(history)[-1]["payload"]["outcome"] == "BLOCKED"
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


async def test_staging_gitops_workflow_records_the_ci_baseline_verdict() -> None:
    history, result = await _run_to_history(
        StagingGitOpsDeliveryWorkflow,
        _gitops_input(),
        _scripted_execute_control_step(
            _gitops_script(
                **{
                    "gitops.pr_checks": {
                        "status": "BLOCKED",
                        "blocker": "SOURCE_PR_CI_REGRESSION",
                        "failed_checks": ["Lint Frontend"],
                        "preexisting_checks": ["CI gate"],
                    }
                }
            )
        ),
    )

    assert result is not None and result["blocker"] == "SOURCE_PR_CI_REGRESSION"
    persisted = _scheduled_steps(history)[-1]["payload"]
    assert persisted["ci_regressions"] == ["Lint Frontend"]
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)
    assert "ci_regression_reason" not in persisted

    # NEW-99: an unverified regression (the PR edits CI configuration) keeps its reason.
    unverified, _ = await _run_to_history(
        StagingGitOpsDeliveryWorkflow,
        _gitops_input(),
        _scripted_execute_control_step(
            _gitops_script(
                **{
                    "gitops.pr_checks": {
                        "status": "BLOCKED",
                        "blocker": "SOURCE_PR_CI_REGRESSION",
                        "failed_checks": ["quality"],
                        "preexisting_checks": [],
                        "reason": "CI_BASELINE_CI_CONFIG_CHANGED",
                        "failure_excerpts": {"quality": "not ok 278"},
                    }
                }
            )
        ),
    )
    persisted = _scheduled_steps(unverified)[-1]["payload"]
    assert persisted["ci_regression_reason"] == "CI_BASELINE_CI_CONFIG_CHANGED"
    assert persisted["ci_failure_excerpts"] == {"quality": "not ok 278"}
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(unverified)

    passed, _ = await _run_to_history(
        StagingGitOpsDeliveryWorkflow,
        _gitops_input(),
        _scripted_execute_control_step(
            _gitops_script(
                **{
                    "gitops.pr_checks": {
                        "status": "CHECKS_PASSED",
                        "ci_baseline": {
                            "pr": "https://github.com/gntik-ai/musematic/pull/900",
                            "preexisting_failures": ["CI gate"],
                        },
                    }
                }
            )
        ),
    )
    recorded = _scheduled_steps(passed)[-1]["payload"]
    assert recorded["ci_preexisting_failures"] == ["CI gate"]
    assert recorded["ci_baseline_pr"].endswith("/pull/900")


async def test_staging_gitops_pins_and_verifies_a_later_release_that_ships_the_merge() -> None:
    # NEW-68: gitops.workflow_run reports a later release; pin/sync/verify/close use its
    # commit while the ChangeSet keeps its own merge SHA.
    release_sha = "e" * 40
    history, result = await _run_to_history(
        StagingGitOpsDeliveryWorkflow,
        _gitops_input(),
        _scripted_execute_control_step(
            _gitops_script(
                **{
                    "gitops.workflow_run": {
                        "status": "RUN_SUCCEEDED",
                        "release_tag": "v0.0.12",
                        "release_sha": release_sha,
                    }
                }
            )
        ),
    )

    assert result is not None and result["status"] == "STAGING_VERIFIED"
    steps = _scheduled_steps(history)
    by_step = {request["step"]: request["payload"] for request in steps}
    for step in ("gitops.image_pin", "gitops.verify_workloads", "gitops.close_issue"):
        assert by_step[step]["merge_sha"] == release_sha
        assert by_step[step]["delivered_merge_sha"] == "d" * 40
    persisted = by_step["gitops.persist_result"]
    assert persisted["merge_sha"] == "d" * 40 and persisted["release_sha"] == release_sha
    assert persisted["release_tag"] == "v0.0.12"
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


async def test_a_write_policy_block_alerts_once_and_resumes_after_approval() -> None:
    # NEW-84: the first BLOCKED_BY_POLICY queues one operator alert; the step is re-tried
    # (every 5 minutes, or at once on the operator's retry signal) until approved.
    script = _gitops_script()
    denials = {"gitops.merge": 1}

    @activity.defn(name="execute_control_step")
    async def policy(request: dict[str, Any]) -> dict[str, Any]:
        step = request["step"]
        if step == "notify.write_policy_required":
            return {"status": "NOTIFIED"}
        if denials.get(step):
            denials[step] -= 1
            return {"status": "BLOCKED_BY_POLICY", "reason_code": "POLICY_DISABLED"}
        return script[step]

    async with await WorkflowEnvironment.start_local(dev_server_existing_path=_TEMPORAL_CLI) as env:
        workers = [
            Worker(
                env.client,
                task_queue=queue,
                workflows=[StagingGitOpsDeliveryWorkflow],
                activities=[policy],
            )
            for queue in (CONTROL_TASK_QUEUE, GITHUB_TASK_QUEUE)
        ]
        async with workers[0], workers[1]:
            handle = await env.client.start_workflow(
                StagingGitOpsDeliveryWorkflow.run,
                _gitops_input(),
                id=f"replay-test-write-policy-{uuid.uuid4()}",
                task_queue=CONTROL_TASK_QUEUE,
                execution_timeout=timedelta(minutes=2),
            )
            for _ in range(200):
                state = await handle.query("status")
                if state.get("phase") == "AWAITING_WRITE_POLICY":
                    break
                await asyncio.sleep(0.1)
            await handle.signal("retry", "gitops.merge")
            result = await handle.result()
            history = await handle.fetch_history()

    assert result["status"] == "STAGING_VERIFIED"
    steps = [request["step"] for request in _scheduled_steps(history)]
    assert steps.count("notify.write_policy_required") == 1
    notice = next(
        r["payload"]
        for r in _scheduled_steps(history)
        if r["step"] == "notify.write_policy_required"
    )
    assert notice["step"] == "gitops.merge" and notice["reason_code"] == "POLICY_DISABLED"
    assert notice["project"] == "musematic" and notice["issue"].endswith("#1026")
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


@workflow.defn(name="IssueDeliveryWorkflow", sandboxed=False)
class _PublishedIssueDeliveryStub:
    """Stands in for IssueDeliveryWorkflow: the source PR has been published."""

    @workflow.run
    async def run(self, request: IssueDeliveryInput) -> dict[str, Any]:
        return {"status": "SOURCE_PR", "context": {"change_set_id": _GITOPS_PLAN["change_set_id"]}}


def _full_delivery_loop_script(
    **gitops_overrides: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    return {
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
        # NEW-112 review: the loop durably records each CI-repair round first.
        "backlog.record_ci_repair": {"status": "RECORDED"},
        "backlog.record_loop_state": {"status": "RECORDED"},
        "backlog.record_skip": {"status": "SKIPPED", "reason_code": "OPERATOR"},
        "backlog.restore_published_change_set": {"status": "RESTORED"},
        "gitops.flow_for_project": {"status": "ENABLED", "enabled": True},
        **_gitops_script(**gitops_overrides),
    }


_LOOP_WORKFLOWS = [
    ExistingGitHubBacklogLoopWorkflow,
    _PublishedIssueDeliveryStub,
    StagingGitOpsDeliveryWorkflow,
]


async def _run_loop(
    script: dict[str, dict[str, Any]],
    signals: list[tuple[str, Any]],
    workflows: list[type] | None = None,
    activity_fn: Any = None,
) -> tuple[Any, Any, list[str]]:
    activity_fn = activity_fn or _scripted_execute_control_step(script)
    workflows = workflows or _LOOP_WORKFLOWS
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
        ):
            handle = await env.client.start_workflow(
                ExistingGitHubBacklogLoopWorkflow.run,
                ExistingGitHubBacklogLoopInput(
                    project="musematic",
                    backlog_campaign_id="camp-1",
                    maximum_risk="R2",
                    delivery_mode="full-delivery",
                    continue_after_blocked_issue=False,
                ),
                id=f"replay-test-gitops-loop-{uuid.uuid4()}",
                task_queue=CONTROL_TASK_QUEUE,
                execution_timeout=timedelta(minutes=2),
            )
            for name, argument in signals:
                for _ in range(200):
                    state = await handle.query("status")
                    if str(state.get("phase", "")).startswith("AWAITING_OPERATOR"):
                        break
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


async def test_a_failed_loop_bookkeeping_step_pauses_and_retries_instead_of_failing() -> None:
    # NEW-82: #1125's loop went FAILED when backlog.register_child_run raised; the step
    # now pauses the issue for the operator and the retry re-runs it.
    script = _full_delivery_loop_script()
    failures = {"backlog.register_child_run": 1}

    @activity.defn(name="execute_control_step")
    async def flaky(request: dict[str, Any]) -> dict[str, Any]:
        step = request["step"]
        if failures.get(step):
            failures[step] -= 1
            raise ApplicationError(
                "backlog child run idempotency binding changed", non_retryable=True
            )
        if step == "gitops.notify_merged" and step not in script:
            return {"status": "NOTIFIED"}  # NEW-127: each merge is announced at once
        if step == "github.refresh_mirrors" and step not in script:
            return {"status": "PASS"}
        return _registered_child(step, request, script[step])

    history, result, children = await _run_loop(
        script, [("retry", "gntik-ai/musematic#1026")], activity_fn=flaky
    )

    assert result["status"] == "COMPLETE"
    paused = [
        request["payload"]
        for request in _scheduled_steps(history)
        if request["step"] == "backlog.record_loop_state"
        and request["payload"].get("loop_status") == "PAUSED"
    ]
    assert [state["blocker"] for state in paused] == ["LOOP_STEP_FAILED_BACKLOG_REGISTER_CHILD_RUN"]
    assert all(state.get("workflow_id") for state in paused)
    await Replayer(workflows=_LOOP_WORKFLOWS).replay_workflow(history)


@workflow.defn(name="FindingVerificationWorkflow", sandboxed=False)
class _FailingVerificationStub:
    """Stands in for FindingVerificationWorkflow failing (e.g. a stale mirror)."""

    @workflow.run
    async def run(self, request: Any) -> dict[str, Any]:
        raise ApplicationError("Git command failed (128): unknown revision", non_retryable=True)


async def test_a_failed_revalidation_child_pauses_the_loop_instead_of_failing_it() -> None:
    # NEW-72: musematic #1125's revalidation child failed on a stale mirror and the whole
    # loop went FAILED. NEW-70: the pause must carry the loop's workflow id (retryable).
    # NEW-71: every issue iteration refreshes the canonical mirrors first.
    script = _full_delivery_loop_script(
        **{
            "backlog.revalidate_gate": {
                "status": "BLOCKED",
                "blocker": "CURRENT_SOURCE_REVALIDATION_REQUIRED",
            },
            "backlog.materialize_candidate": {"status": "READY", "finding_id": "finding-1"},
        }
    )
    history, result, children = await _run_loop(
        script,
        [
            ("retry", "gntik-ai/musematic#1026"),
            ("skip", "gntik-ai/musematic#1026|OPERATOR"),
        ],
        workflows=[*_LOOP_WORKFLOWS, _FailingVerificationStub],
    )

    assert result["status"] == "COMPLETE"
    # NEW-74: the retried revalidation runs under a fresh child id.
    assert [child.rsplit("-issue-", 1)[1] for child in children] == [
        "1-revalidate-1026",
        "1-revalidate-1026-r1",
    ]
    steps = _scheduled_steps(history)
    names = [request["step"] for request in steps]
    assert names.index("github.refresh_mirrors") < names.index("backlog.revalidate_gate")
    assert "backlog.record_revalidation" not in names
    paused = [
        request["payload"]
        for request in steps
        if request["step"] == "backlog.record_loop_state"
        and request["payload"].get("loop_status") == "PAUSED"
    ]
    # NEW-114: the failed child pauses with its own blocker (retryable).
    assert [state["blocker"] for state in paused] == ["REVALIDATION_FAILED"] * 2
    assert all(state.get("workflow_id") for state in paused)
    await Replayer(workflows=[*_LOOP_WORKFLOWS, _FailingVerificationStub]).replay_workflow(history)


async def test_full_delivery_loop_runs_the_staging_gitops_child_when_enabled() -> None:
    history, result, children = await _run_loop(_full_delivery_loop_script(), [])

    assert result["status"] == "COMPLETE"
    assert any(
        item.get("stage") == "staging-gitops" and item.get("status") == "STAGING_VERIFIED"
        for item in result["results"]
    )
    # The pre-merge candidate pipeline is never started when the flow is enabled.
    assert [child.rsplit("-issue-", 1)[1] for child in children] == [
        "1-attempt-1",
        "1-staging-gitops",
    ]
    registered = [
        request["payload"]["child_kind"]
        for request in _scheduled_steps(history)
        if request["step"] == "backlog.register_child_run"
    ]
    assert registered == ["issue-delivery", "staging-gitops"]
    await Replayer(workflows=_LOOP_WORKFLOWS).replay_workflow(history)


async def test_blocked_staging_gitops_pauses_retries_only_the_child_then_skips() -> None:
    history, result, children = await _run_loop(
        _full_delivery_loop_script(
            **{"gitops.prepare": {"status": "BLOCKED", "blocker": "ENVIRONMENT_UNHEALTHY"}}
        ),
        [
            ("retry", "gntik-ai/musematic#1026"),
            ("skip", "gntik-ai/musematic#1026|OPERATOR"),
        ],
    )

    assert result["status"] == "COMPLETE"
    assert {"status": "SKIPPED", "reason_code": "OPERATOR"} in result["results"]
    assert [child.rsplit("-issue-", 1)[1] for child in children] == [
        "1-attempt-1",
        "1-staging-gitops",
        "1-staging-gitops-retry-1",
    ]
    paused = [
        request["payload"]
        for request in _scheduled_steps(history)
        if request["step"] == "backlog.record_loop_state"
        and request["payload"].get("loop_status") == "PAUSED"
    ]
    assert [state["blocker"] for state in paused] == ["ENVIRONMENT_UNHEALTHY"] * 2
    assert all(state.get("workflow_id") for state in paused)
    await Replayer(workflows=_LOOP_WORKFLOWS).replay_workflow(history)


async def test_ci_regression_redelivers_twice_then_waits_for_the_operator() -> None:
    # NEW-64: the baseline gate (NEW-63) attributes PR CI failures to the ChangeSet, so
    # the loop re-runs the issue delivery (same ChangeSet, same PR) before a fresh GitOps
    # child -- at most twice automatically, then it pauses for the operator.
    regression = {
        "status": "BLOCKED",
        "blocker": "SOURCE_PR_CI_REGRESSION",
        "failed_checks": ["Lint Frontend"],
        "preexisting_checks": ["CI gate"],
        "baseline_pr": "https://github.com/gntik-ai/musematic/pull/900",
    }
    history, result, children = await _run_loop(
        _full_delivery_loop_script(**{"gitops.pr_checks": regression}),
        [("skip", "gntik-ai/musematic#1026|OPERATOR")],
    )

    assert result["status"] == "COMPLETE"
    assert [child.rsplit("-issue-", 1)[1] for child in children] == [
        "1-attempt-1",
        "1-staging-gitops",
        "1-ci-repair-1-attempt-1",
        "1-staging-gitops-retry-1",
        "1-ci-repair-2-attempt-1",
        "1-staging-gitops-retry-2",
    ]
    steps = _scheduled_steps(history)
    paused = [
        request["payload"]["blocker"]
        for request in steps
        if request["step"] == "backlog.record_loop_state"
        and request["payload"].get("loop_status") == "PAUSED"
    ]
    assert paused == ["SOURCE_PR_CI_REGRESSION"]
    registered = [
        request["payload"]["child_kind"]
        for request in steps
        if request["step"] == "backlog.register_child_run"
    ]
    assert registered.count("issue-delivery") == 3
    # Each repair round is durably recorded by the loop right before its repair child.
    names = [request["step"] for request in steps]
    authorizations = [
        (request["payload"]["ordinal"], request["payload"]["repair_round"])
        for request in steps
        if request["step"] == "backlog.record_ci_repair"
    ]
    assert authorizations == [(1, 1), (1, 2)]
    for position in [
        index for index, name in enumerate(names) if name == "backlog.record_ci_repair"
    ]:
        assert names[position + 1] == "backlog.register_child_run"
    await Replayer(workflows=_LOOP_WORKFLOWS).replay_workflow(history)


async def test_an_operator_retried_ci_repair_round_is_recorded_like_an_automatic_one() -> None:
    # NEW-112 review: after the two automatic repair rounds, each operator retry from the
    # CI regression starts another round (falcone #980 used rounds 3-7); every round is
    # durably recorded by the loop right before its repair child.
    regression = {
        "status": "BLOCKED",
        "blocker": "SOURCE_PR_CI_REGRESSION",
        "failed_checks": ["Lint Frontend"],
        "preexisting_checks": ["CI gate"],
        "baseline_pr": "https://github.com/gntik-ai/musematic/pull/900",
    }
    history, result, children = await _run_loop(
        _full_delivery_loop_script(**{"gitops.pr_checks": regression}),
        [("retry", "gntik-ai/musematic#1026"), ("skip", "gntik-ai/musematic#1026|OPERATOR")],
    )

    assert result["status"] == "COMPLETE"
    repairs = [child.rsplit("-issue-", 1)[1] for child in children if "-ci-repair-" in child]
    assert repairs == [
        "1-ci-repair-1-attempt-1",
        "1-ci-repair-2-attempt-1",
        "1-ci-repair-3-attempt-1",
    ]
    steps = _scheduled_steps(history)
    names = [request["step"] for request in steps]
    authorizations = [
        (request["payload"]["ordinal"], request["payload"]["repair_round"])
        for request in steps
        if request["step"] == "backlog.record_ci_repair"
    ]
    assert authorizations == [(1, 1), (1, 2), (1, 3)]
    for position in [
        index for index, name in enumerate(names) if name == "backlog.record_ci_repair"
    ]:
        following = steps[position + 1]
        assert following["step"] == "backlog.register_child_run"
        assert "-ci-repair-" in following["payload"]["child_workflow_id"]
    await Replayer(workflows=_LOOP_WORKFLOWS).replay_workflow(history)


_DELIVERY_TO_SPEC_SCRIPT: dict[str, dict[str, Any]] = {
    "delivery.load_confirmed_finding": {"status": "OK", "run_id": "run-1"},
    "delivery.ensure_issue": {"status": "OK", "run_id": "run-1"},
    "delivery.acquire_leases": {"status": "LEASES_ACQUIRED", "run_id": "run-1"},
    "delivery.route_providers": {"status": "ROUTED", "run_id": "run-1"},
    "delivery.allocate_worktrees": {"status": "WORKTREES_ALLOCATED", "run_id": "run-1"},
    "delivery.persist_worktree_leases": {"status": "WORKTREE_LEASES_PERSISTED"},
    "delivery.deterministic_prechecks": {"status": "PRECHECKS_PASS", "run_id": "run-1"},
    "delivery.prepare_toolchain": {"status": "TOOLCHAIN_READY", "run_id": "run-1"},
    "delivery.prepare_spec": {"status": "BLOCKED", "blocker": "SPEC_STOP", "run_id": "run-1"},
    "delivery.release_leases": {"status": "LEASES_RELEASED", "run_id": "run-1"},
}


def _scheduled_step_names(history: Any) -> list[str]:
    return [
        json.loads(event.activity_task_scheduled_event_attributes.input.payloads[0].data)["step"]
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]


@workflow.defn(name="IssueDeliveryWorkflow", sandboxed=False)
class _PreToolchainIssueDeliveryWorkflow(IssueDeliveryWorkflow):
    """IssueDeliveryWorkflow exactly as it ran before delivery-prepare-toolchain-v1."""

    @workflow.run
    async def run(self, request: IssueDeliveryInput) -> dict[str, Any]:
        return await super().run(request)

    def _prepare_toolchain_patched(self) -> bool:
        return False


async def test_issue_delivery_prepares_the_toolchain_after_prechecks_and_replays() -> None:
    activity_fn = _scripted_execute_control_step(_DELIVERY_TO_SPEC_SCRIPT)
    run_arg = IssueDeliveryInput(finding_id="finding-1", risk_class="R1", dry_run=False)

    history, result = await _run_to_history(IssueDeliveryWorkflow, run_arg, activity_fn)

    assert result is not None and result["status"] == "BLOCKED"
    steps = _scheduled_step_names(history)
    prechecks = steps.index("delivery.deterministic_prechecks")
    assert steps[prechecks + 1 : prechecks + 3] == [
        "delivery.prepare_toolchain",
        "delivery.prepare_spec",
    ]
    markers = [
        event.marker_recorded_event_attributes.SerializeToString()
        for event in history.events
        if event.HasField("marker_recorded_event_attributes")
    ]
    assert any(b"delivery-prepare-toolchain-v1" in marker for marker in markers)
    await Replayer(workflows=[IssueDeliveryWorkflow]).replay_workflow(history)


async def test_offline_maker_dependency_requests_are_resolved_then_the_maker_reruns() -> None:
    # NEW-75: the maker edits manifests and returns DEPENDENCIES_REQUIRED; the scan
    # worker resolves the lockfiles and the maker runs again (at most two rounds).
    script = {
        **_DELIVERY_TO_SPEC_SCRIPT,
        "delivery.prepare_spec": {"status": "SPEC_READY", "run_id": "run-1"},
        "agent.record_start": {"status": "STARTED", "run_id": "run-1"},
        "delivery.invoke_spec": {"status": "SUCCESS", "run_id": "run-1"},
        "delivery.invoke_maker": {"status": "SUCCESS", "run_id": "run-1"},
        "agent.persist_result": {
            "status": "SUCCESS",
            "run_id": "run-1",
            "agent_result": {
                "structured_result": {
                    "decision": "DEPENDENCIES_REQUIRED",
                    "dependency_resolution": [{"ecosystem": "go", "directory": "services/x"}],
                }
            },
        },
        "delivery.create_openspec": {"status": "PLANNED", "run_id": "run-1"},
        "delivery.prepare_maker": {"status": "SOURCE_MAKER_READY", "run_id": "run-1"},
        "delivery.resolve_dependencies": {
            "status": "DEPENDENCIES_RESOLVED",
            "run_id": "run-1",
            "dependency_resolution_result": {"files": ["services/x/go.sum"], "commit": "c" * 40},
        },
        "delivery.prepare_deployment_maker": {
            "status": "BLOCKED",
            "blocker": "SOURCE_MAKER_DID_NOT_COMPLETE",
            "run_id": "run-1",
        },
    }
    run_arg = IssueDeliveryInput(finding_id="finding-1", risk_class="R1", dry_run=False)

    history, result = await _run_to_history(
        IssueDeliveryWorkflow, run_arg, _scripted_execute_control_step(script)
    )

    assert result is not None and result["status"] == "BLOCKED"
    steps = _scheduled_step_names(history)
    assert steps.count("delivery.resolve_dependencies") == 2
    assert steps.count("delivery.invoke_maker") == 3
    first = steps.index("delivery.resolve_dependencies")
    assert steps[first + 1 : first + 3] == ["delivery.prepare_maker", "agent.record_start"]
    rounds = [
        request["payload"].get("dependency_round")
        for request in _scheduled_steps(history)
        if request["step"] == "delivery.prepare_maker"
    ]
    assert rounds == [None, 1, 2]
    # NEW-77: a later round never carries the previous round's provider result.
    reruns = [
        request["payload"]
        for request in _scheduled_steps(history)
        if request["step"] == "delivery.prepare_maker"
        and request["payload"].get("dependency_round")
    ]
    assert reruns and all("agent_result" not in payload for payload in reruns)
    await Replayer(workflows=[IssueDeliveryWorkflow]).replay_workflow(history)


async def test_validation_findings_are_recorded_for_the_next_correction() -> None:
    # NEW-76: a validation REQUEST_CHANGES with named findings is recorded on the
    # ChangeSet before the attempt ends, so the next maker round sees it.
    script = {
        **_DELIVERY_TO_SPEC_SCRIPT,
        "delivery.prepare_spec": {"status": "SPEC_READY", "run_id": "run-1"},
        "agent.record_start": {"status": "STARTED", "run_id": "run-1"},
        "delivery.invoke_spec": {"status": "SUCCESS", "run_id": "run-1"},
        "delivery.invoke_maker": {"status": "SUCCESS", "run_id": "run-1"},
        "delivery.invoke_deployment_maker": {"status": "SUCCESS", "run_id": "run-1"},
        "delivery.invoke_checker": {"status": "SUCCESS", "run_id": "run-1"},
        "agent.persist_result": {
            "status": "SUCCESS",
            "run_id": "run-1",
            "agent_result": {"structured_result": {"decision": "COMPLETE"}},
        },
        "delivery.create_openspec": {"status": "PLANNED", "run_id": "run-1"},
        "delivery.prepare_maker": {"status": "SOURCE_MAKER_READY", "run_id": "run-1"},
        "delivery.prepare_deployment_maker": {"status": "READY", "run_id": "run-1"},
        "delivery.converge_deployment_pin": {
            "status": "DEPLOYMENT_PIN_UNCHANGED",
            "run_id": "run-1",
        },
        "delivery.prepare_checker": {"status": "CHECKER_READY", "run_id": "run-1"},
        "delivery.validate_changeset": {
            "status": "REQUEST_CHANGES",
            "blocker": "CHANGESET_VALIDATION_FINDINGS",
            "validation_findings": ["source: trivy CVE-2026-84445"],
            "run_id": "run-1",
        },
        "delivery.record_validation_findings": {"status": "RECORDED", "run_id": "run-1"},
    }
    run_arg = IssueDeliveryInput(finding_id="finding-1", risk_class="R1", dry_run=False)

    history, result = await _run_to_history(
        IssueDeliveryWorkflow, run_arg, _scripted_execute_control_step(script)
    )

    assert result is not None and result["status"] == "REQUEST_CHANGES"
    steps = _scheduled_step_names(history)
    validated = steps.index("delivery.validate_changeset")
    assert steps[validated + 1] == "delivery.record_validation_findings"
    await Replayer(workflows=[IssueDeliveryWorkflow]).replay_workflow(history)


# -- NEW-118: deterministic CI-pin convergence ------------------------------------------

_DELIVERY_TO_CHECKER_SCRIPT: dict[str, dict[str, Any]] = {
    **_DELIVERY_TO_SPEC_SCRIPT,
    "delivery.prepare_spec": {"status": "SPEC_READY", "run_id": "run-1"},
    "agent.record_start": {"status": "STARTED", "run_id": "run-1"},
    "delivery.invoke_spec": {"status": "SUCCESS", "run_id": "run-1"},
    "delivery.invoke_maker": {"status": "SUCCESS", "run_id": "run-1"},
    "delivery.invoke_deployment_maker": {"status": "SUCCESS", "run_id": "run-1"},
    "delivery.invoke_checker": {"status": "SUCCESS", "run_id": "run-1"},
    "agent.persist_result": {
        "status": "SUCCESS",
        "run_id": "run-1",
        "agent_result": {"structured_result": {"decision": "COMPLETE"}},
    },
    "delivery.create_openspec": {"status": "PLANNED", "run_id": "run-1"},
    "delivery.prepare_maker": {"status": "SOURCE_MAKER_READY", "run_id": "run-1"},
    "delivery.prepare_deployment_maker": {"status": "READY", "run_id": "run-1"},
    "delivery.converge_deployment_pin": {
        "status": "DEPLOYMENT_PIN_CONVERGED",
        "run_id": "run-1",
        "deployment_pin_result": {"commit": "c" * 40},
    },
    "delivery.prepare_checker": {"status": "CHECKER_READY", "run_id": "run-1"},
    "delivery.validate_changeset": {"status": "REQUEST_CHANGES", "run_id": "run-1"},
}


@workflow.defn(name="IssueDeliveryWorkflow", sandboxed=False)
class _PrePinIssueDeliveryWorkflow(IssueDeliveryWorkflow):
    """IssueDeliveryWorkflow exactly as it ran before delivery-deployment-pin-convergence-v1."""

    @workflow.run
    async def run(self, request: IssueDeliveryInput) -> dict[str, Any]:
        return await super().run(request)

    def _pin_convergence_patched(self) -> bool:
        return False


async def test_new_118_pin_converges_after_the_deployment_maker_before_the_checker() -> None:
    run_arg = IssueDeliveryInput(finding_id="finding-1", risk_class="R1", dry_run=False)

    history, result = await _run_to_history(
        IssueDeliveryWorkflow, run_arg, _scripted_execute_control_step(_DELIVERY_TO_CHECKER_SCRIPT)
    )

    assert result is not None and result["status"] == "REQUEST_CHANGES"
    steps = _scheduled_step_names(history)
    deployment_maker = steps.index("delivery.invoke_deployment_maker")
    assert steps[deployment_maker : deployment_maker + 5] == [
        "delivery.invoke_deployment_maker",
        "agent.persist_result",
        "delivery.converge_deployment_pin",
        "delivery.prepare_checker",
        "agent.record_start",
    ]
    assert steps.index("delivery.converge_deployment_pin") < steps.index("delivery.invoke_checker")
    assert steps.count("delivery.converge_deployment_pin") == 1
    (pin,) = _payloads(history, "delivery.converge_deployment_pin")
    assert pin["agent_result"] == {"structured_result": {"decision": "COMPLETE"}}
    assert "delivery_request" in pin
    assert any(b"delivery-deployment-pin-convergence-v1" in marker for marker in _markers(history))
    await Replayer(workflows=[IssueDeliveryWorkflow]).replay_workflow(history)


async def test_new_118_a_blocked_pin_stops_the_delivery_and_releases_its_leases() -> None:
    script = {
        **_DELIVERY_TO_CHECKER_SCRIPT,
        "delivery.converge_deployment_pin": {
            "status": "BLOCKED",
            "blocker": "DEPLOYMENT_PIN_AMBIGUOUS",
            "run_id": "run-1",
        },
    }
    run_arg = IssueDeliveryInput(finding_id="finding-1", risk_class="R1", dry_run=False)

    history, result = await _run_to_history(
        IssueDeliveryWorkflow, run_arg, _scripted_execute_control_step(script)
    )

    assert result is not None and result["status"] == "BLOCKED"
    assert result["context"]["blocker"] == "DEPLOYMENT_PIN_AMBIGUOUS"
    steps = _scheduled_step_names(history)
    assert steps[-2:] == ["delivery.converge_deployment_pin", "delivery.release_leases"]
    assert "delivery.prepare_checker" not in steps
    await Replayer(workflows=[IssueDeliveryWorkflow]).replay_workflow(history)


async def test_new_118_a_pre_pin_delivery_history_replays_without_the_step() -> None:
    script = dict(_DELIVERY_TO_CHECKER_SCRIPT)
    del script["delivery.converge_deployment_pin"]
    run_arg = IssueDeliveryInput(finding_id="finding-1", risk_class="R1", dry_run=False)

    history, result = await _run_to_history(
        _PrePinIssueDeliveryWorkflow, run_arg, _scripted_execute_control_step(script)
    )

    assert result is not None and result["status"] == "REQUEST_CHANGES"
    steps = _scheduled_step_names(history)
    assert "delivery.converge_deployment_pin" not in steps
    persisted = steps.index("delivery.invoke_deployment_maker") + 1
    assert steps[persisted : persisted + 2] == ["agent.persist_result", "delivery.prepare_checker"]
    assert not any(b"delivery-deployment-pin-convergence-v1" in m for m in _markers(history))
    await Replayer(workflows=[IssueDeliveryWorkflow]).replay_workflow(history)


async def test_pre_toolchain_issue_delivery_history_replays_without_the_new_step() -> None:
    """A delivery recorded before the patch (no marker) must replay unchanged against the
    current code, i.e. without scheduling delivery.prepare_toolchain."""
    activity_fn = _scripted_execute_control_step(_DELIVERY_TO_SPEC_SCRIPT)
    run_arg = IssueDeliveryInput(finding_id="finding-1", risk_class="R1", dry_run=False)

    history, result = await _run_to_history(
        _PreToolchainIssueDeliveryWorkflow, run_arg, activity_fn
    )

    assert result is not None and result["status"] == "BLOCKED"
    steps = _scheduled_step_names(history)
    assert "delivery.prepare_toolchain" not in steps
    prechecks = steps.index("delivery.deterministic_prechecks")
    assert steps[prechecks + 1] == "delivery.prepare_spec"
    assert not any(event.HasField("marker_recorded_event_attributes") for event in history.events)
    await Replayer(workflows=[IssueDeliveryWorkflow]).replay_workflow(history)


async def test_issue_delivery_passes_a_reused_openspec_through_unchanged_steps() -> None:
    """NEW-53: spec reuse is an activity-level short-circuit -- the workflow schedules
    the same steps (no patch marker) and SPEC_REUSED is not a stop status."""
    reused = {"status": "SPEC_REUSED", "spec_reused": True, "run_id": "run-1"}
    script = {
        **_DELIVERY_TO_SPEC_SCRIPT,
        "delivery.prepare_spec": reused,
        "agent.record_start": reused,
        "delivery.invoke_spec": reused,
        "agent.persist_result": reused,
        "delivery.create_openspec": {"status": "OPENSPEC_READY", "spec_reused": True},
        "delivery.prepare_maker": {"status": "BLOCKED", "blocker": "MAKER_STOP"},
    }
    run_arg = IssueDeliveryInput(finding_id="finding-1", risk_class="R1", dry_run=False)

    history, result = await _run_to_history(
        IssueDeliveryWorkflow, run_arg, _scripted_execute_control_step(script)
    )

    assert result is not None and result["status"] == "BLOCKED"
    steps = _scheduled_step_names(history)
    spec = steps.index("delivery.prepare_spec")
    assert steps[spec : spec + 6] == [
        "delivery.prepare_spec",
        "agent.record_start",
        "delivery.invoke_spec",
        "agent.persist_result",
        "delivery.create_openspec",
        "delivery.prepare_maker",
    ]
    markers = [
        event.marker_recorded_event_attributes.SerializeToString()
        for event in history.events
        if event.HasField("marker_recorded_event_attributes")
    ]
    assert not any(b"reuse-spec" in marker for marker in markers)
    await Replayer(workflows=[IssueDeliveryWorkflow]).replay_workflow(history)


_REFRESHED_BODY = "Issue body\n\n## Operator decisions\n\n- JSON only; 5 MB; 20/hour"


async def test_issue_delivery_refreshes_an_existing_issue_body_and_replays() -> None:
    """NEW-44: decisions added to an existing issue after backlog import reach the
    spec/maker, because delivery re-reads the body right after ensure_issue."""
    script = {
        **_DELIVERY_TO_SPEC_SCRIPT,
        "delivery.ensure_issue": {
            "status": "ISSUE_READY",
            "run_id": "run-1",
            "persona": "existing-github-issue",
            "issue_repository": "gntik-ai/musematic",
            "issue_number": 993,
            "actual_behavior": "Issue body",
        },
        "delivery.refresh_issue_body": {
            "status": "ISSUE_BODY_REFRESHED",
            "body_excerpt": _REFRESHED_BODY,
            "body_sha256": "0" * 64,
        },
    }
    run_arg = IssueDeliveryInput(finding_id="finding-1", risk_class="R1", dry_run=False)

    history, result = await _run_to_history(
        IssueDeliveryWorkflow, run_arg, _scripted_execute_control_step(script)
    )

    assert result is not None and result["status"] == "BLOCKED"
    requests = _scheduled_steps(history)
    steps = [request["step"] for request in requests]
    ensure = steps.index("delivery.ensure_issue")
    assert steps[ensure + 1 : ensure + 3] == [
        "delivery.refresh_issue_body",
        "delivery.acquire_leases",
    ]
    refresh = requests[ensure + 1]["payload"]
    assert refresh == {"issue_repository": "gntik-ai/musematic", "issue_number": 993}
    assert requests[ensure + 2]["payload"]["actual_behavior"] == _REFRESHED_BODY
    await Replayer(workflows=[IssueDeliveryWorkflow]).replay_workflow(history)


async def test_issue_delivery_stops_when_the_issue_changed_since_approval_and_replays() -> None:
    """NEW-112 review: an approved delivery may refresh only to its approved issue text; an
    edit after approval blocks before any lease with the operator's re-approval guidance."""
    script = {
        **_DELIVERY_TO_SPEC_SCRIPT,
        "delivery.ensure_issue": {
            "status": "ISSUE_READY",
            "run_id": "run-1",
            "persona": "existing-github-issue",
            "issue_repository": "gntik-ai/musematic",
            "issue_number": 993,
            "actual_behavior": "Issue body",
            "approved_issue_body_sha256": "a" * 64,
        },
        "delivery.refresh_issue_body": {
            "status": "ISSUE_BODY_CHANGED_SINCE_APPROVAL",
            "blocker": "ISSUE_BODY_CHANGED_SINCE_APPROVAL",
            "blocker_detail": "re-import and re-approve",
            "approved_body_sha256": "a" * 64,
            "current_body_sha256": "b" * 64,
        },
        "delivery.release_leases": {"status": "NO_RUN_TO_RELEASE_LEASES_FOR"},
    }
    run_arg = IssueDeliveryInput(finding_id="finding-1", risk_class="R1", dry_run=False)

    history, result = await _run_to_history(
        IssueDeliveryWorkflow, run_arg, _scripted_execute_control_step(script)
    )

    assert result["status"] == "BLOCKED"
    assert result["context"]["blocker"] == "ISSUE_BODY_CHANGED_SINCE_APPROVAL"
    assert result["context"]["blocker_detail"] == "re-import and re-approve"
    assert result["context"]["actual_behavior"] == "Issue body"
    requests = _scheduled_steps(history)
    steps = [request["step"] for request in requests]
    assert "delivery.acquire_leases" not in steps
    refresh = next(r for r in requests if r["step"] == "delivery.refresh_issue_body")
    assert refresh["payload"]["approved_issue_body_sha256"] == "a" * 64
    await Replayer(workflows=[IssueDeliveryWorkflow]).replay_workflow(history)


async def test_issue_delivery_keeps_the_imported_body_when_the_refresh_fails() -> None:
    script = {
        **_DELIVERY_TO_SPEC_SCRIPT,
        "delivery.ensure_issue": {
            "status": "ISSUE_READY",
            "run_id": "run-1",
            "persona": "existing-github-issue",
            "issue_repository": "gntik-ai/musematic",
            "issue_number": 993,
            "actual_behavior": "Issue body",
        },
        "delivery.refresh_issue_body": {
            "status": "ISSUE_BODY_UNAVAILABLE",
            "reason": "CommandFailure",
        },
    }
    run_arg = IssueDeliveryInput(finding_id="finding-1", risk_class="R1", dry_run=False)

    history, _ = await _run_to_history(
        IssueDeliveryWorkflow, run_arg, _scripted_execute_control_step(script)
    )

    requests = _scheduled_steps(history)
    leases = next(r for r in requests if r["step"] == "delivery.acquire_leases")
    assert leases["payload"]["actual_behavior"] == "Issue body"
    assert leases["payload"]["status"] == "ISSUE_READY"


async def test_r3_issue_delivery_keeps_the_approved_issue_body_and_replays() -> None:
    """NEW-95 review: an R3 delivery works only from the issue text its R3 approval bound
    (the imported copy); a later edit to the live issue must not reach the maker."""
    script = {
        **_DELIVERY_TO_SPEC_SCRIPT,
        "delivery.ensure_issue": {
            "status": "ISSUE_READY",
            "run_id": "run-1",
            "persona": "existing-github-issue",
            "issue_repository": "gntik-ai/falcone",
            "issue_number": 953,
            "actual_behavior": "Approved issue body",
        },
        "delivery.refresh_issue_body": {
            "status": "ISSUE_BODY_REFRESHED",
            "body_excerpt": "Edited after approval",
            "body_sha256": "0" * 64,
        },
    }
    run_arg = IssueDeliveryInput(finding_id="finding-1", risk_class="R3", dry_run=False)

    history, _ = await _run_to_history(
        IssueDeliveryWorkflow, run_arg, _scripted_execute_control_step(script)
    )

    assert "delivery.refresh_issue_body" not in _scheduled_step_names(history)
    requests = _scheduled_steps(history)
    leases = next(r for r in requests if r["step"] == "delivery.acquire_leases")
    assert leases["payload"]["actual_behavior"] == "Approved issue body"
    await Replayer(workflows=[IssueDeliveryWorkflow]).replay_workflow(history)


async def test_issue_delivery_does_not_refresh_findings_without_an_existing_issue() -> None:
    """Hermes-discovered findings own their body; only imported backlog issues refresh."""
    script = {
        **_DELIVERY_TO_SPEC_SCRIPT,
        "delivery.ensure_issue": {
            "status": "ISSUE_READY",
            "run_id": "run-1",
            "persona": "api-consumer",
            "issue_repository": "gntik-ai/musematic",
            "issue_number": 12,
        },
    }
    run_arg = IssueDeliveryInput(finding_id="finding-1", risk_class="R1", dry_run=False)

    history, _ = await _run_to_history(
        IssueDeliveryWorkflow, run_arg, _scripted_execute_control_step(script)
    )

    assert "delivery.refresh_issue_body" not in _scheduled_step_names(history)


def test_backlog_loop_outcome_is_compacted_below_the_payload_limit() -> None:
    """NEW-59: 19 retry rounds of full child contexts exceeded Temporal's 2 MB limit."""
    from engineering_control.workflows.definitions import _compact_loop_outcome

    bulky = {"status": "REQUEST_CHANGES", "attempt": 3, "context": {"x": "y" * 50_000}}
    outcome = _compact_loop_outcome({"status": "PAUSED", "results": [bulky] * 300})

    assert outcome["status"] == "PAUSED"
    assert outcome["results_total"] == 300
    assert len(outcome["results"]) == 200
    assert outcome["results"][0] == {"status": "REQUEST_CHANGES", "attempt": 3}
    assert len(json.dumps(outcome)) < 100_000


async def test_gate_retry_reactivates_the_campaign_and_fails_closed_on_a_stale_reload() -> None:
    """NEW-91 (falcone-charts #47, 2026-09-28): after a gate pause the operator's retry
    reloaded the item while the campaign was still PAUSED, so its approval read as not
    current, load_execution returned no items, and the loop re-ran revalidation on its
    stale copy of the (already confirmed) finding, which then crashed.

    The retry must record RUNNING (which reactivates the campaign approval) before the
    reload, and a reload without items must pause on its blocker instead.
    """
    steps: list[tuple[str, dict[str, Any]]] = []
    loads = 0

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        nonlocal loads
        step = request["step"]
        steps.append((step, request["payload"]))
        if step == "backlog.load_execution":
            loads += 1
            if loads == 1:
                return {
                    "status": "READY",
                    "items": [{"repository": "gntik-ai/musematic", "issue_number": 1026}],
                }
            return {"status": "BLOCKED", "blocker": "CURRENT_BACKLOG_APPROVAL_REQUIRED"}
        if step == "backlog.revalidate_gate":
            return {"status": "BLOCKED", "blocker": "CURRENT_RUNTIME_REVALIDATION_REQUIRED"}
        if step == "github.refresh_mirrors":
            return {"status": "PASS"}
        if step in {"backlog.register_run", "backlog.record_loop_state"}:
            return {"status": "RECORDED"}
        raise AssertionError(f"unscripted step {step!r}")

    history, result, _ = await _run_with_signals(
        _single_issue_input(),
        execute_control_step,
        [("retry", "gntik-ai/musematic#1026"), ("cancel", None)],
    )

    assert result["status"] == "CANCELLED"
    sequence = [
        (step, payload.get("loop_status"), payload.get("blocker"))
        for step, payload in steps
        if step
        in {"backlog.record_loop_state", "backlog.load_execution", "backlog.revalidate_gate"}
    ]
    first_pause = sequence.index(
        ("backlog.record_loop_state", "PAUSED", "CURRENT_RUNTIME_REVALIDATION_REQUIRED")
    )
    after_retry = sequence[first_pause + 1 :]
    # RUNNING is recorded before the reload...
    assert after_retry[0] == ("backlog.record_loop_state", "RUNNING", None)
    assert after_retry[1][0] == "backlog.load_execution"
    # ...and the failed reload pauses on its own blocker without re-running the gate.
    assert after_retry[2] == (
        "backlog.record_loop_state",
        "PAUSED",
        "CURRENT_BACKLOG_APPROVAL_REQUIRED",
    )
    assert sum(1 for step, _, _ in sequence if step == "backlog.revalidate_gate") == 1
    await Replayer(
        workflows=[ExistingGitHubBacklogLoopWorkflow, IssueDeliveryWorkflow]
    ).replay_workflow(history)


async def test_a_start_blocked_at_selection_registers_a_closed_run_and_replays() -> None:
    """NEW-97: the loop records a closed Run for a start blocked at selection, so the
    operation ends instead of blocking the issue forever."""
    steps: list[tuple[str, dict[str, Any]]] = []

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        steps.append((request["step"], request["payload"]))
        if request["step"] == "backlog.load_execution":
            return {"status": "BLOCKED", "blocker": "DIRECT_ISSUE_NOT_CURRENTLY_ELIGIBLE"}
        if request["step"] == "backlog.register_run":
            return {"status": "REGISTERED"}
        raise AssertionError(f"unscripted step {request['step']!r}")

    history, result = await _run_to_history(
        ExistingGitHubBacklogLoopWorkflow, _single_issue_input(), execute_control_step
    )

    assert result["blocker"] == "DIRECT_ISSUE_NOT_CURRENTLY_ELIGIBLE"
    registered = [payload for step, payload in steps if step == "backlog.register_run"]
    assert len(registered) == 1
    assert registered[0]["closed_blocker"] == "DIRECT_ISSUE_NOT_CURRENTLY_ELIGIBLE"
    await Replayer(workflows=[ExistingGitHubBacklogLoopWorkflow]).replay_workflow(history)


@workflow.defn(name="ExistingGitHubBacklogLoopWorkflow", sandboxed=False)
class _PreClosedRunBacklogLoopWorkflow(ExistingGitHubBacklogLoopWorkflow):
    """ExistingGitHubBacklogLoopWorkflow as it ran before the blocked start closed its Run."""

    @workflow.run
    async def run(self, request: ExistingGitHubBacklogLoopInput) -> dict[str, Any]:
        return await super().run(request)

    def _blocked_start_closes_run_patched(self) -> bool:
        return False


async def test_a_pre_patch_blocked_start_replays_without_registering_a_run() -> None:
    """A blocked start recorded before NEW-97 (no marker) replays against the current code
    without scheduling backlog.register_run."""

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        if request["step"] == "backlog.load_execution":
            return {"status": "BLOCKED", "blocker": "DIRECT_ISSUE_NOT_CURRENTLY_ELIGIBLE"}
        raise AssertionError(f"unscripted step {request['step']!r}")

    history, result = await _run_to_history(
        _PreClosedRunBacklogLoopWorkflow, _single_issue_input(), execute_control_step
    )

    assert result["blocker"] == "DIRECT_ISSUE_NOT_CURRENTLY_ELIGIBLE"
    assert "backlog.register_run" not in _scheduled_step_names(history)
    markers = [
        event.marker_recorded_event_attributes.SerializeToString()
        for event in history.events
        if event.HasField("marker_recorded_event_attributes")
    ]
    assert not any(b"blocked-start-closes-run" in marker for marker in markers)
    await Replayer(workflows=[ExistingGitHubBacklogLoopWorkflow]).replay_workflow(history)


@workflow.defn(name="IssueDeliveryWorkflow", sandboxed=False)
class _FirstRepairBlockedStub:
    """IssueDeliveryWorkflow whose first CI repair blocks (#1001: the maker had no logs)."""

    @workflow.run
    async def run(self, request: IssueDeliveryInput) -> dict[str, Any]:
        if "-ci-repair-1-" in workflow.info().workflow_id:
            return {"status": "BLOCKED", "blocker": "SOURCE_MAKER_DID_NOT_COMPLETE"}
        return {"status": "SOURCE_PR", "context": {"change_set_id": _GITOPS_PLAN["change_set_id"]}}


_REPAIR_BLOCKED_WORKFLOWS = [
    ExistingGitHubBacklogLoopWorkflow,
    _FirstRepairBlockedStub,
    StagingGitOpsDeliveryWorkflow,
]
_REGRESSION = {
    "status": "BLOCKED",
    "blocker": "SOURCE_PR_CI_REGRESSION",
    "failed_checks": ["quality"],
    "preexisting_checks": [],
}


async def test_a_retry_after_a_blocked_ci_repair_rechecks_ci_before_repairing() -> None:
    # NEW-101: the retry re-runs the GitOps CI check so the second repair receives fresh
    # failures (and NEW-100 log excerpts) instead of the stale evidence of the first.
    script = _full_delivery_loop_script()
    scripted = _scripted_execute_control_step(script)
    checks = 0
    order: list[str] = []

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        nonlocal checks
        payload = request["payload"]
        if request["step"] == "gitops.persist_result":
            order.append(f"persist {payload.get('ci_failure_excerpts', {}).get('quality')}")
        if request["step"] == "backlog.register_child_run" and "-ci-repair-" in str(
            payload.get("child_workflow_id")
        ):
            order.append("repair " + str(payload["child_workflow_id"]).rsplit("-issue-", 1)[1])
        if request["step"] == "backlog.restore_published_change_set":
            order.append(f"restore {payload['phase']}")
            if payload["phase"] == "plan":
                return {"status": "READY", "version": 7, "pull_requests": [{"url": "u"}]}
            assert payload["version"] == 7 and payload["live_heads"] == [{"url": "u"}]
            return {"status": "RESTORED"}
        if request["step"] == "gitops.pr_heads":
            return {"status": "OK", "heads": [{"url": "u"}]}
        if request["step"] == "gitops.pr_checks":
            checks += 1
            return {**_REGRESSION, "failure_excerpts": {"quality": f"not ok round-{checks}"}}
        return await scripted(request)

    history, result, children = await _run_loop(
        script,
        [
            ("retry", "gntik-ai/musematic#1026"),
            # At the automatic limit the recheck pauses on the regression; the operator's
            # retry then repairs with that fresh evidence, never without a recheck.
            ("retry", "gntik-ai/musematic#1026"),
            ("skip", "gntik-ai/musematic#1026|OPERATOR"),
        ],
        workflows=_REPAIR_BLOCKED_WORKFLOWS,
        activity_fn=execute_control_step,
    )

    assert result["status"] == "COMPLETE"
    assert [child.rsplit("-issue-", 1)[1] for child in children] == [
        "1-attempt-1",
        "1-staging-gitops",
        "1-ci-repair-1-attempt-1",
        "1-staging-gitops-retry-1",
        "1-ci-repair-2-attempt-1",
        "1-staging-gitops-retry-2",
        "1-ci-repair-3-attempt-1",
        "1-staging-gitops-retry-3",
    ]
    steps = _scheduled_steps(history)
    paused = [
        request["payload"]["blocker"]
        for request in steps
        if request["step"] == "backlog.record_loop_state"
        and request["payload"].get("loop_status") == "PAUSED"
    ]
    assert paused == [
        "CI_REPAIR_SOURCE_MAKER_DID_NOT_COMPLETE",
        "SOURCE_PR_CI_REGRESSION",
        "SOURCE_PR_CI_REGRESSION",
    ]
    # Each repair after the first follows a GitOps run that persisted fresh excerpts.
    # NEW-102: the retry after the stopped repair (round 1) restores the published binding
    # before its recheck; the regression retry at the limit repairs directly.
    assert order == [
        "persist not ok round-1",
        "repair 1-ci-repair-1-attempt-1",
        "restore plan",
        "restore apply",
        "persist not ok round-2",
        "repair 1-ci-repair-2-attempt-1",
        "persist not ok round-3",
        "repair 1-ci-repair-3-attempt-1",
        "persist not ok round-4",
    ]
    await Replayer(workflows=_REPAIR_BLOCKED_WORKFLOWS).replay_workflow(history)


@workflow.defn(name="ExistingGitHubBacklogLoopWorkflow", sandboxed=False)
class _PreRecheckBacklogLoopWorkflow(ExistingGitHubBacklogLoopWorkflow):
    """The loop as it ran before NEW-101: a retry repaired again at once."""

    @workflow.run
    async def run(self, request: ExistingGitHubBacklogLoopInput) -> dict[str, Any]:
        return await super().run(request)

    def _repair_retry_rechecks_patched(self, ordinal: int) -> bool:
        return False


async def test_a_pre_recheck_repair_retry_history_replays_unchanged() -> None:
    workflows = [
        _PreRecheckBacklogLoopWorkflow,
        _FirstRepairBlockedStub,
        StagingGitOpsDeliveryWorkflow,
    ]
    history, _, children = await _run_loop(
        _full_delivery_loop_script(**{"gitops.pr_checks": _REGRESSION}),
        [
            ("retry", "gntik-ai/musematic#1026"),
            ("skip", "gntik-ai/musematic#1026|OPERATOR"),
        ],
        workflows=workflows,
    )

    assert [child.rsplit("-issue-", 1)[1] for child in children][2:4] == [
        "1-ci-repair-1-attempt-1",
        "1-ci-repair-2-attempt-1",
    ]
    await Replayer(workflows=_REPAIR_BLOCKED_WORKFLOWS).replay_workflow(history)


async def test_a_refused_or_failing_restore_pauses_and_the_retry_restores_again() -> None:
    # NEW-102: a restore that is refused (e.g. the PR head moved) pauses with its reason;
    # the retry repeats the restore. A restore step that raises becomes a pause too, and
    # an operator skip then ends the issue without failing the loop.
    script = _full_delivery_loop_script(**{"gitops.pr_checks": _REGRESSION})
    scripted = _scripted_execute_control_step(script)
    applies = 0

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        nonlocal applies
        if request["step"] == "backlog.restore_published_change_set":
            if request["payload"]["phase"] == "plan":
                return {"status": "READY", "version": 1, "pull_requests": [{"url": "u"}]}
            applies += 1
            if applies == 1:
                return {"status": "NOT_RESTORED", "reason": "PUBLISHED_PR_HEAD_CHANGED"}
            raise ApplicationError("database unavailable", non_retryable=True)
        if request["step"] == "gitops.pr_heads":
            return {"status": "OK", "heads": [{"url": "u"}]}
        return await scripted(request)

    history, result, children = await _run_loop(
        script,
        [
            ("retry", "gntik-ai/musematic#1026"),
            ("retry", "gntik-ai/musematic#1026"),
            ("retry", "gntik-ai/musematic#1026"),
            ("skip", "gntik-ai/musematic#1026|OPERATOR"),
        ],
        workflows=_REPAIR_BLOCKED_WORKFLOWS,
        activity_fn=execute_control_step,
    )

    assert result["status"] == "COMPLETE"
    paused = [
        request["payload"]["blocker"]
        for request in _scheduled_steps(history)
        if request["step"] == "backlog.record_loop_state"
        and request["payload"].get("loop_status") == "PAUSED"
    ]
    assert paused == [
        "CI_REPAIR_SOURCE_MAKER_DID_NOT_COMPLETE",
        "CI_REPAIR_RESTORE_PUBLISHED_PR_HEAD_CHANGED",
        "CI_REPAIR_RESTORE_STEP_FAILED",
        "CI_REPAIR_RESTORE_STEP_FAILED",
    ]
    # No recheck or further repair ran while the ChangeSet was not restored.
    assert [child.rsplit("-issue-", 1)[1] for child in children] == [
        "1-attempt-1",
        "1-staging-gitops",
        "1-ci-repair-1-attempt-1",
    ]
    await Replayer(workflows=_REPAIR_BLOCKED_WORKFLOWS).replay_workflow(history)


async def test_a_late_reset_after_restore_is_restored_again_on_retry() -> None:
    # NEW-102 review: a timed-out repair's late write resets the ChangeSet after the
    # restore, so the recheck's prepare refuses it; the retry restores again first.
    script = _full_delivery_loop_script(**{"gitops.pr_checks": _REGRESSION})
    scripted = _scripted_execute_control_step(script)
    prepares = 0
    order: list[str] = []

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        nonlocal prepares
        step = request["step"]
        if step == "backlog.restore_published_change_set":
            order.append(f"restore {request['payload']['phase']}")
            if request["payload"]["phase"] == "plan":
                return {"status": "READY", "version": 1, "pull_requests": [{"url": "u"}]}
            return {"status": "RESTORED"}
        if step == "gitops.pr_heads":
            return {"status": "OK", "heads": [{"url": "u"}]}
        if step == "gitops.prepare":
            prepares += 1
            order.append(f"prepare {prepares}")
            if prepares == 2:
                return {"status": "BLOCKED", "blocker": "CHANGE_SET_STATE_NOT_DELIVERABLE"}
        return await scripted(request)

    history, result, _ = await _run_loop(
        script,
        [
            ("retry", "gntik-ai/musematic#1026"),
            ("retry", "gntik-ai/musematic#1026"),
            ("skip", "gntik-ai/musematic#1026|OPERATOR"),
        ],
        workflows=_REPAIR_BLOCKED_WORKFLOWS,
        activity_fn=execute_control_step,
    )

    assert result["status"] == "COMPLETE"
    assert order[:8] == [
        "prepare 1",
        "restore plan",
        "restore apply",
        "prepare 2",
        "restore plan",
        "restore apply",
        "prepare 3",
        "prepare 4",
    ]
    paused = [
        request["payload"]["blocker"]
        for request in _scheduled_steps(history)
        if request["step"] == "backlog.record_loop_state"
        and request["payload"].get("loop_status") == "PAUSED"
    ]
    assert paused == [
        "CI_REPAIR_SOURCE_MAKER_DID_NOT_COMPLETE",
        "CHANGE_SET_STATE_NOT_DELIVERABLE",
        "SOURCE_PR_CI_REGRESSION",
    ]
    await Replayer(workflows=_REPAIR_BLOCKED_WORKFLOWS).replay_workflow(history)


_DEPLOYMENT_PLAN = {
    **_GITOPS_PLAN,
    "deployment_pr_number": 9,
    "deployment_pr_url": "https://github.com/gntik-ai/musematic-deploy/pull/9",
    "deployment_head_sha": "e" * 40,
}
_DEPLOYMENT_REGRESSION = {
    "status": "BLOCKED",
    "blocker": "DEPLOYMENT_PR_CI_REGRESSION",
    "failed_checks": ["validate"],
    "preexisting_checks": [],
    "reason": "DEPLOYMENT_CI_NOT_BASELINED",
    "failure_excerpts": {"validate": "not ok 51 - approved baseline"},
}


def _deployment_prepare() -> dict[str, Any]:
    prepared = dict(_gitops_script()["gitops.prepare"])
    prepared["plan"] = _DEPLOYMENT_PLAN
    return prepared


async def test_a_failing_deployment_pr_ci_blocks_before_either_merge_and_replays() -> None:
    # NEW-103: falcone #1001 merged its source while the falcone-charts CI was still
    # running and then failed; now nothing merges until the deployment CI passes.
    history, result = await _run_to_history(
        StagingGitOpsDeliveryWorkflow,
        _gitops_input(),
        _scripted_execute_control_step(
            _gitops_script(
                **{
                    "gitops.prepare": _deployment_prepare(),
                    "gitops.deployment_pr_checks": _DEPLOYMENT_REGRESSION,
                }
            )
        ),
    )

    assert result is not None and result["blocker"] == "DEPLOYMENT_PR_CI_REGRESSION"
    steps = [request["step"] for request in _scheduled_steps(history)]
    assert "gitops.merge" not in steps
    assert steps.index("gitops.pr_checks") < steps.index("gitops.deployment_pr_checks")
    persisted = _scheduled_steps(history)[-1]["payload"]
    assert persisted["ci_regressions"] == ["validate"]
    assert persisted["ci_regression_target"] == "deployment"
    assert persisted["ci_regression_reason"] == "DEPLOYMENT_CI_NOT_BASELINED"
    assert persisted["ci_failure_excerpts"] == {"validate": "not ok 51 - approved baseline"}
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


async def test_a_deployment_ci_regression_is_repaired_like_a_source_one() -> None:
    history, result, children = await _run_loop(
        _full_delivery_loop_script(
            **{
                "gitops.prepare": _deployment_prepare(),
                "gitops.deployment_pr_checks": _DEPLOYMENT_REGRESSION,
            }
        ),
        [("skip", "gntik-ai/musematic#1026|OPERATOR")],
    )

    assert result["status"] == "COMPLETE"
    assert [child.rsplit("-issue-", 1)[1] for child in children] == [
        "1-attempt-1",
        "1-staging-gitops",
        "1-ci-repair-1-attempt-1",
        "1-staging-gitops-retry-1",
        "1-ci-repair-2-attempt-1",
        "1-staging-gitops-retry-2",
    ]
    paused = [
        request["payload"]["blocker"]
        for request in _scheduled_steps(history)
        if request["step"] == "backlog.record_loop_state"
        and request["payload"].get("loop_status") == "PAUSED"
    ]
    assert paused == ["DEPLOYMENT_PR_CI_REGRESSION"]
    await Replayer(workflows=_LOOP_WORKFLOWS).replay_workflow(history)


# -- NEW-113: Argo CD sync failure diagnosis, remediation and escalation ---------------

_MESSAGE_980 = (
    'Deployment.apps "falcone-control-plane-executor" is invalid: '
    'spec.template.spec.containers[0].env[1].valueFrom: Invalid value: "": '
    "may not be specified when value is not empty"
)
_EXECUTOR = {
    "group": "apps",
    "kind": "Deployment",
    "namespace": "musematic-platform",
    "name": "falcone-control-plane-executor",
}
_EXECUTOR_KEY = "apps/Deployment/musematic-platform/falcone-control-plane-executor"
_SYNC_FAILED = {
    "status": "SYNC_FAILED",
    "blocker": "ARGO_SYNC_FAILED",
    "message": "one or more objects failed to apply",
    "phase": "Failed",
    "operation_started_at": "2026-10-02T09:00:00Z",
}


def _diagnosed(failure_class: str, **diagnosis: Any) -> dict[str, Any]:
    return {
        "status": "DIAGNOSED",
        "failure_class": failure_class,
        "diagnosis": {
            "application": "musematic-platform",
            "message": "one or more objects failed to apply",
            "failed_resources": [],
            "degraded_resources": [],
            "failure_class": failure_class,
            **diagnosis,
        },
    }


def _sync_recovery_script(**overrides: Any) -> dict[str, dict[str, Any]]:
    return _gitops_script(
        **{
            "gitops.sync_status": _SYNC_FAILED,
            "gitops.notify_sync": {"status": "NOTIFIED"},
            "gitops.environment_issue": {
                "status": "ISSUE_OPENED",
                "issue_number": 501,
                "issue_url": "https://github.com/gntik-ai/musematic/issues/501",
            },
            **overrides,
        }
    )


def _sequenced_step(
    script: dict[str, dict[str, Any]], sequences: dict[str, list[dict[str, Any]]]
) -> Any:
    """Like _scripted_execute_control_step; a sequenced step answers in order, then repeats
    its last answer."""
    remaining = {step: list(answers) for step, answers in sequences.items()}

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        step = request["step"]
        answers = remaining.get(step)
        if answers:
            return answers.pop(0) if len(answers) > 1 else answers[0]
        if step not in script:
            raise AssertionError(f"unscripted step {step!r} invoked with {request['payload']!r}")
        return _registered_child(step, request, script[step])

    return execute_control_step


@workflow.defn(name="StagingGitOpsDeliveryWorkflow", sandboxed=False)
class _FastStagingGitOpsWorkflow(StagingGitOpsDeliveryWorkflow):
    """The real flow with millisecond durable timers, so polls and back-off finish here."""

    _TIMER_SCALE = 0.001

    @workflow.run
    async def run(self, request: StagingGitOpsDeliveryInput) -> dict[str, Any]:
        return await super().run(request)


def _markers(history: Any) -> list[bytes]:
    return [
        event.marker_recorded_event_attributes.SerializeToString()
        for event in history.events
        if event.HasField("marker_recorded_event_attributes")
    ]


def _payloads(history: Any, step: str) -> list[dict[str, Any]]:
    return [request["payload"] for request in _scheduled_steps(history) if request["step"] == step]


_BEFORE_SYNC = [
    "gitops.prepare",
    "gitops.pr_checks",
    "gitops.merge",
    "gitops.notify_merged",  # NEW-127
    "gitops.release_tag",
    "gitops.workflow_run",
    "gitops.environment_check",
    "gitops.image_pin",
    "gitops.trigger_sync",
]
# Histories recorded before NEW-127 never announced the merge as its own step.
_BEFORE_SYNC_PRE_127 = [step for step in _BEFORE_SYNC if step != "gitops.notify_merged"]


async def test_a_patch_conflict_is_replaced_then_verified() -> None:
    # falcone#980: SYNC_FAILED -> PATCH_CONFLICT -> Replace=true sync of only the failed
    # Deployment -> SYNCED -> the unchanged verification and issue close.
    script = _sync_recovery_script(
        **{
            "gitops.diagnose_sync": _diagnosed(
                "PATCH_CONFLICT",
                failed_resources=[
                    {
                        **_EXECUTOR,
                        "status": "SyncFailed",
                        "message": _MESSAGE_980,
                        "failure_class": "PATCH_CONFLICT",
                        "managed": True,
                        "eligible": True,
                    }
                ],
            ),
            "gitops.remediate_sync": {
                "status": "REMEDIATION_TRIGGERED",
                "action": "replace",
                "resources": [_EXECUTOR],
                "remediated_keys": [_EXECUTOR_KEY],
                "previous_operation_started_at": "2026-10-02T09:00:00Z",
            },
        }
    )
    history, result = await _run_to_history(
        StagingGitOpsDeliveryWorkflow,
        _gitops_input(),
        _sequenced_step(
            script,
            {"gitops.sync_status": [_SYNC_FAILED, {"status": "SYNCED", "argo_revision": "r1"}]},
        ),
    )

    assert result is not None and result["status"] == "STAGING_VERIFIED"
    assert [request["step"] for request in _scheduled_steps(history)] == [
        *_BEFORE_SYNC,
        "gitops.sync_status",
        "gitops.diagnose_sync",
        "gitops.notify_sync",
        "gitops.remediate_sync",
        "gitops.sync_status",
        "gitops.notify_sync",
        "gitops.verify_workloads",
        "gitops.close_issue",
        "gitops.persist_result",
    ]
    (remediation,) = _payloads(history, "gitops.remediate_sync")
    assert remediation["action"] == "replace" and remediation["resources"] == [_EXECUTOR]
    assert (remediation["round"], remediation["already_remediated"]) == (1, [])
    # Finding 2/4: a stable identity and the failed operation the diagnosis describes.
    assert remediation["remediation_id"].endswith(":round-1:replace")
    assert remediation["remediation_id"].startswith("replay-test-StagingGitOpsDeliveryWorkflow-")
    assert remediation["failed_operation_started_at"] == "2026-10-02T09:00:00Z"
    assert _payloads(history, "gitops.sync_status")[1]["selective"] is True
    notices = _payloads(history, "gitops.notify_sync")
    assert [notice["event"] for notice in notices] == ["detected", "remediation"]
    assert notices[1]["result"] == "SYNCED"
    persisted = _payloads(history, "gitops.persist_result")[0]
    assert persisted["argo_revision"] == "r1"
    assert persisted["argo_sync_failure_class"] == "PATCH_CONFLICT"
    assert persisted["argo_sync_remediations"][0]["resources"] == [_EXECUTOR_KEY]
    assert any(b"gitops-argo-sync-remediation-v1" in marker for marker in _markers(history))
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


_SCOPE_INHERITED = {
    "status": "SYNC_SCOPE_INHERITED",
    "blocker": "ARGO_SYNC_SCOPE_INHERITED",
    "scope_violations": ["resources", "sync_options", "explicit_revision"],
    "phase": "Succeeded",
    "sync": "OutOfSync",
    "operation_started_at": "2026-10-03T10:00:00Z",
}
_FULL_RESYNC = {
    "status": "REMEDIATION_TRIGGERED",
    "action": "full_resync",
    "resources": [],
    "remediated_keys": [],
    "previous_operation_started_at": "2026-10-03T10:00:00Z",
}


async def test_new_116_an_inherited_scope_is_resynced_once_as_a_full_sync() -> None:
    # falcone-staging 2026-10-03: Hermes's full sync ran with the operator's selective
    # scope. It is never SYNCED; one TRUE full sync is requested, then verified.
    script = _sync_recovery_script(
        **{
            "gitops.diagnose_sync": _diagnosed("OTHER"),
            "gitops.remediate_sync": _FULL_RESYNC,
        }
    )
    history, result = await _run_to_history(
        StagingGitOpsDeliveryWorkflow,
        _gitops_input(),
        _sequenced_step(
            script,
            {"gitops.sync_status": [_SCOPE_INHERITED, {"status": "SYNCED", "argo_revision": "r1"}]},
        ),
    )

    assert result is not None and result["status"] == "STAGING_VERIFIED"
    assert [request["step"] for request in _scheduled_steps(history)][len(_BEFORE_SYNC) :] == [
        "gitops.sync_status",
        "gitops.diagnose_sync",
        "gitops.notify_sync",
        "gitops.remediate_sync",
        "gitops.sync_status",
        "gitops.notify_sync",
        "gitops.verify_workloads",
        "gitops.close_issue",
        "gitops.persist_result",
    ]
    (remediation,) = _payloads(history, "gitops.remediate_sync")
    assert (remediation["action"], remediation["resources"], remediation["attempt"]) == (
        "full_resync",
        [],
        1,
    )
    assert remediation["remediation_id"].endswith(":round-1:full_resync")
    assert remediation["failed_operation_started_at"] == "2026-10-03T10:00:00Z"
    # The follow-up poll expects a full sync (no selective flag).
    assert _payloads(history, "gitops.sync_status")[1]["selective"] is False
    detected = _payloads(history, "gitops.notify_sync")[0]
    assert (detected["event"], detected["failure_class"]) == ("detected", "SCOPE_INHERITED")
    persisted = _payloads(history, "gitops.persist_result")[0]
    assert persisted["argo_sync_failure_class"] == "SCOPE_INHERITED"
    markers = _markers(history)
    assert any(b"gitops-argo-sync-scope-inherited-v1" in marker for marker in markers)
    assert any(b"gitops-argo-sync-remediation-v1" in marker for marker in markers)
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


async def test_new_116_a_scope_inherited_again_escalates_after_one_full_resync() -> None:
    script = _sync_recovery_script(
        **{
            "gitops.sync_status": _SCOPE_INHERITED,
            "gitops.diagnose_sync": _diagnosed("OTHER"),
            "gitops.remediate_sync": _FULL_RESYNC,
        }
    )
    history, result = await _run_to_history(
        StagingGitOpsDeliveryWorkflow, _gitops_input(), _scripted_execute_control_step(script)
    )

    assert result is not None
    assert (result["status"], result["blocker"], result["failure_class"]) == (
        "BLOCKED",
        "ARGO_SYNC_FAILED",
        "SCOPE_INHERITED",
    )
    assert len(_payloads(history, "gitops.remediate_sync")) == 1
    (issue,) = _payloads(history, "gitops.environment_issue")
    assert (issue["failure_class"], issue["blocker"]) == ("SCOPE_INHERITED", "ARGO_SYNC_FAILED")
    assert [attempt["result"] for attempt in issue["attempts"]] == ["SYNC_SCOPE_INHERITED"]
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


# -- NEW-118: resources Argo CD would prune are escalated at once, never pruned --------

_KEYCLOAK = {
    "group": "",
    "kind": "ConfigMap",
    "namespace": "musematic-platform",
    "name": "falcone-keycloak-config",
}
_PRUNE_REQUIRED = {
    "status": "SYNC_PRUNE_REQUIRED",
    "blocker": "ARGO_PRUNE_REQUIRED",
    "phase": "Succeeded",
    "sync": "OutOfSync",
    "operation_started_at": "2026-10-03T12:00:00Z",
}
_PRUNE_DIAGNOSED = _diagnosed(
    "PRUNE_REQUIRED",
    prune_required_resources=[
        {
            **_KEYCLOAK,
            "status": "OutOfSync",
            "tracking_id": "musematic-platform:/ConfigMap:musematic-platform/"
            "falcone-keycloak-config",
            "safe_kind": True,
        }
    ],
    prune_required_resources_total=1,
)


async def test_new_118_prune_drift_is_escalated_immediately_and_never_pruned() -> None:
    # falcone-charts #45 removed ConfigMap falcone-keycloak-config; with prune=false the
    # application stayed OutOfSync for the whole 20-minute poll (ARGO_SYNC_TIMEOUT).
    history, result = await _run_to_history(
        StagingGitOpsDeliveryWorkflow,
        _gitops_input(),
        _scripted_execute_control_step(
            _sync_recovery_script(
                **{
                    "gitops.sync_status": _PRUNE_REQUIRED,
                    "gitops.diagnose_sync": _PRUNE_DIAGNOSED,
                }
            )
        ),
    )

    assert result is not None
    assert (result["status"], result["blocker"], result["failure_class"]) == (
        "BLOCKED",
        "ARGO_PRUNE_REQUIRED",
        "PRUNE_REQUIRED",
    )
    steps = [request["step"] for request in _scheduled_steps(history)]
    assert steps[len(_BEFORE_SYNC) :] == [
        "gitops.sync_status",  # one poll: no 20-minute timeout
        "gitops.diagnose_sync",
        "gitops.notify_sync",
        "gitops.environment_issue",
        "gitops.notify_sync",
        "gitops.persist_result",
    ]
    assert "gitops.remediate_sync" not in steps
    (issue,) = _payloads(history, "gitops.environment_issue")
    assert (issue["blocker"], issue["failure_class"]) == ("ARGO_PRUNE_REQUIRED", "PRUNE_REQUIRED")
    assert issue["diagnosis"]["prune_required_resources"][0]["name"] == "falcone-keycloak-config"
    notices = _payloads(history, "gitops.notify_sync")
    assert [(notice["event"], notice["blocker"]) for notice in notices] == [
        ("detected", "ARGO_PRUNE_REQUIRED"),
        ("escalated", "ARGO_PRUNE_REQUIRED"),
    ]
    persisted = _payloads(history, "gitops.persist_result")[0]
    assert (persisted["blocker"], persisted["argo_sync_failure_class"]) == (
        "ARGO_PRUNE_REQUIRED",
        "PRUNE_REQUIRED",
    )
    markers = _markers(history)
    assert any(b"gitops-argo-prune-escalation-v1" in marker for marker in markers)
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


async def test_new_118_prune_drift_after_a_remediation_is_escalated_not_pruned() -> None:
    # A replace repaired the failed Deployment; the follow-up sync leaves only prune drift.
    script = _sync_recovery_script(
        **{
            "gitops.remediate_sync": {
                "status": "REMEDIATION_TRIGGERED",
                "action": "replace",
                "resources": [_EXECUTOR],
                "remediated_keys": [_EXECUTOR_KEY],
                "previous_operation_started_at": "2026-10-02T09:00:00Z",
            },
        }
    )
    conflict = _diagnosed(
        "PATCH_CONFLICT",
        failed_resources=[
            {
                **_EXECUTOR,
                "status": "SyncFailed",
                "message": _MESSAGE_980,
                "failure_class": "PATCH_CONFLICT",
                "managed": True,
                "eligible": True,
            }
        ],
    )
    history, result = await _run_to_history(
        StagingGitOpsDeliveryWorkflow,
        _gitops_input(),
        _sequenced_step(
            script,
            {
                "gitops.sync_status": [_SYNC_FAILED, _PRUNE_REQUIRED],
                "gitops.diagnose_sync": [conflict, _PRUNE_DIAGNOSED],
            },
        ),
    )

    assert result is not None and result["blocker"] == "ARGO_PRUNE_REQUIRED"
    actions = [payload["action"] for payload in _payloads(history, "gitops.remediate_sync")]
    assert actions == ["replace"]
    steps = [request["step"] for request in _scheduled_steps(history)]
    assert steps[-4:] == [
        "gitops.diagnose_sync",
        "gitops.environment_issue",
        "gitops.notify_sync",
        "gitops.persist_result",
    ]
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


async def test_new_118_a_timed_out_sync_with_prune_drift_is_escalated_as_prune() -> None:
    pending = {"status": "SYNC_PENDING", "phase": "Succeeded", "sync": "OutOfSync"}
    history, result = await _run_to_history(
        _FastStagingGitOpsWorkflow,
        _gitops_input(),
        _scripted_execute_control_step(
            _sync_recovery_script(
                **{"gitops.sync_status": pending, "gitops.diagnose_sync": _PRUNE_DIAGNOSED}
            )
        ),
    )

    assert result is not None and result["blocker"] == "ARGO_PRUNE_REQUIRED"
    assert "gitops.remediate_sync" not in [r["step"] for r in _scheduled_steps(history)]
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


async def test_an_unremediable_sync_failure_opens_an_issue_and_notifies() -> None:
    history, result = await _run_to_history(
        StagingGitOpsDeliveryWorkflow,
        _gitops_input(),
        _scripted_execute_control_step(
            _sync_recovery_script(**{"gitops.diagnose_sync": _diagnosed("OTHER")})
        ),
    )

    assert result is not None
    assert (result["status"], result["blocker"], result["failure_class"]) == (
        "BLOCKED",
        "ARGO_SYNC_FAILED",
        "OTHER",
    )
    assert result["evidence"]["argo_sync_issue_number"] == 501
    assert [request["step"] for request in _scheduled_steps(history)][len(_BEFORE_SYNC) :] == [
        "gitops.sync_status",
        "gitops.diagnose_sync",
        "gitops.notify_sync",
        "gitops.environment_issue",
        "gitops.notify_sync",
        "gitops.persist_result",
    ]
    (issue,) = _payloads(history, "gitops.environment_issue")
    assert (issue["action"], issue["failure_class"], issue["blocker"]) == (
        "report_sync",
        "OTHER",
        "ARGO_SYNC_FAILED",
    )
    assert issue["attempts"] == []
    escalated = _payloads(history, "gitops.notify_sync")[-1]
    assert escalated["event"] == "escalated"
    assert escalated["issue_url"] == "https://github.com/gntik-ai/musematic/issues/501"
    persisted = _payloads(history, "gitops.persist_result")[0]
    assert (persisted["outcome"], persisted["blocker"]) == ("BLOCKED", "ARGO_SYNC_FAILED")
    assert persisted["argo_sync_failure_class"] == "OTHER"
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


async def test_a_transient_failure_is_resynced_at_most_three_times_then_escalated() -> None:
    history, result = await _run_to_history(
        _FastStagingGitOpsWorkflow,
        _gitops_input(),
        _scripted_execute_control_step(
            _sync_recovery_script(
                **{
                    "gitops.diagnose_sync": _diagnosed("TRANSIENT"),
                    "gitops.remediate_sync": {
                        "status": "REMEDIATION_TRIGGERED",
                        "action": "resync",
                        "resources": [],
                        "remediated_keys": [],
                        "previous_operation_started_at": "2026-10-02T09:00:00Z",
                    },
                }
            )
        ),
    )

    assert result is not None
    assert (result["blocker"], result["failure_class"]) == ("ARGO_SYNC_FAILED", "TRANSIENT")
    remediations = _payloads(history, "gitops.remediate_sync")
    assert [(item["action"], item["attempt"], item["round"]) for item in remediations] == [
        ("resync", 1, 1),
        ("resync", 2, 2),
        ("resync", 3, 3),
    ]
    assert all(item["resources"] == [] for item in remediations)
    assert len(_payloads(history, "gitops.sync_status")) == 4
    assert [notice["event"] for notice in _payloads(history, "gitops.notify_sync")] == [
        "detected",
        "remediation",
        "remediation",
        "remediation",
        "escalated",
    ]
    (issue,) = _payloads(history, "gitops.environment_issue")
    assert [attempt["result"] for attempt in issue["attempts"]] == ["SYNC_FAILED"] * 3
    # Exponential back-off before each re-sync (scaled for the test).
    timers = [
        event.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta()
        for event in history.events
        if event.HasField("timer_started_event_attributes")
    ]
    assert timers == [timedelta(milliseconds=ms) for ms in (30, 60, 120)]
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


async def test_a_synced_but_unhealthy_timeout_is_diagnosed_and_escalated() -> None:
    degraded = [
        {
            **_EXECUTOR,
            "health": "Degraded",
            "failure_class": "OTHER",
            "pod_reasons": [{"pod": "x-1", "container": "main", "reason": "CrashLoopBackOff"}],
        }
    ]
    pending = {
        "status": "SYNC_PENDING",
        "phase": "Succeeded",
        "sync": "Synced",
        "health": "Degraded",
    }
    history, result = await _run_to_history(
        _FastStagingGitOpsWorkflow,
        _gitops_input(),
        _scripted_execute_control_step(
            _sync_recovery_script(
                **{
                    "gitops.sync_status": pending,
                    "gitops.diagnose_sync": _diagnosed("OTHER", degraded_resources=degraded),
                }
            )
        ),
    )

    assert result is not None
    assert (result["status"], result["blocker"], result["failure_class"]) == (
        "BLOCKED",
        "ARGO_SYNC_TIMEOUT",
        "OTHER",
    )
    steps = [request["step"] for request in _scheduled_steps(history)]
    assert steps.count("gitops.sync_status") == 40
    assert "gitops.remediate_sync" not in steps
    assert steps[-5:] == [
        "gitops.diagnose_sync",
        "gitops.notify_sync",
        "gitops.environment_issue",
        "gitops.notify_sync",
        "gitops.persist_result",
    ]
    (issue,) = _payloads(history, "gitops.environment_issue")
    assert issue["blocker"] == "ARGO_SYNC_TIMEOUT"
    assert issue["diagnosis"]["degraded_resources"] == degraded
    assert _payloads(history, "gitops.notify_sync")[0]["blocker"] == "ARGO_SYNC_TIMEOUT"
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


async def test_a_pre_new_113_sync_failure_history_replays_unchanged() -> None:
    """A sync failure recorded by the PARENT implementation (origin/main, before NEW-113)
    replays against the current code with its old BLOCKED return."""
    history, result = await _run_to_history(
        ParentStagingGitOpsDeliveryWorkflow,
        _gitops_input(),
        _scripted_execute_control_step(_sync_recovery_script()),
    )

    assert result is not None
    assert (result["status"], result["blocker"]) == ("BLOCKED", "ARGO_SYNC_FAILED")
    assert "failure_class" not in result
    assert [request["step"] for request in _scheduled_steps(history)] == [
        *_BEFORE_SYNC_PRE_127,
        "gitops.sync_status",
        "gitops.persist_result",
    ]
    assert not _markers(history)
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


def _timer_durations(history: Any) -> list[timedelta]:
    return [
        event.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta()
        for event in history.events
        if event.HasField("timer_started_event_attributes")
    ]


def test_production_timers_are_not_rescaled() -> None:
    interval = timedelta(seconds=30)
    assert StagingGitOpsDeliveryWorkflow._TIMER_SCALE == 1.0
    assert StagingGitOpsDeliveryWorkflow()._timer(interval) is interval


async def test_an_execution_mid_sync_poll_upgrades_into_the_new_path() -> None:
    """The parent implementation records a sync poll and its 30 s timer; the worker is
    then upgraded. The new code replays that prefix, records its own identical 30 s poll
    timer, and the next failure takes the NEW-113 path (marker after the old prefix)."""
    pending = {"status": "SYNC_PENDING", "phase": "Running", "sync": "OutOfSync"}
    activity_fn = _sequenced_step(
        _sync_recovery_script(**{"gitops.diagnose_sync": _diagnosed("OTHER")}),
        {"gitops.sync_status": [pending, pending, _SYNC_FAILED]},
    )

    def workers(client: Any, workflow_cls: type) -> list[Worker]:
        return [
            Worker(client, task_queue=queue, workflows=[workflow_cls], activities=[activity_fn])
            for queue in (CONTROL_TASK_QUEUE, GITHUB_TASK_QUEUE)
        ]

    async with await WorkflowEnvironment.start_local(dev_server_existing_path=_TEMPORAL_CLI) as env:
        old = workers(env.client, ParentStagingGitOpsDeliveryWorkflow)
        async with old[0], old[1]:
            handle = await env.client.start_workflow(
                StagingGitOpsDeliveryWorkflow.run,
                _gitops_input(),
                id=f"replay-test-mid-poll-upgrade-{uuid.uuid4()}",
                task_queue=CONTROL_TASK_QUEUE,
                execution_timeout=timedelta(minutes=3),
            )
            for _ in range(300):
                prefix = await handle.fetch_history()
                if _timer_durations(prefix):
                    break
                await asyncio.sleep(0.1)
        assert _timer_durations(prefix) == [timedelta(seconds=30)]
        assert not _markers(prefix)
        new = workers(env.client, StagingGitOpsDeliveryWorkflow)
        async with new[0], new[1]:
            result = await handle.result()
            history = await handle.fetch_history()

    assert (result["status"], result["blocker"], result["failure_class"]) == (
        "BLOCKED",
        "ARGO_SYNC_FAILED",
        "OTHER",
    )
    # Old timer and the upgraded code's timer are the same production command.
    assert _timer_durations(history) == [timedelta(seconds=30)] * 2
    steps = [request["step"] for request in _scheduled_steps(history)]
    assert steps[len(_BEFORE_SYNC_PRE_127) :] == [
        "gitops.sync_status",
        "gitops.sync_status",
        "gitops.sync_status",
        "gitops.diagnose_sync",
        "gitops.notify_sync",
        "gitops.environment_issue",
        "gitops.notify_sync",
        "gitops.persist_result",
    ]
    marker_index = next(
        index
        for index, event in enumerate(history.events)
        if event.HasField("marker_recorded_event_attributes")
    )
    timer_index = next(
        index
        for index, event in enumerate(history.events)
        if event.HasField("timer_started_event_attributes")
    )
    assert marker_index > timer_index
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)


async def test_a_failed_remediation_activity_is_not_retried_and_escalates() -> None:
    # NEW-113 review fallback: the mutating step runs as a single attempt; a failed (or
    # lost) attempt may already have written its operation, so the flow escalates
    # (ARGO_REMEDIATION_OUTCOME_UNKNOWN) instead of retrying and syncing twice.
    script = _sync_recovery_script(
        **{
            "gitops.diagnose_sync": _diagnosed(
                "PATCH_CONFLICT",
                failed_resources=[
                    {
                        **_EXECUTOR,
                        "status": "SyncFailed",
                        "message": _MESSAGE_980,
                        "failure_class": "PATCH_CONFLICT",
                        "managed": True,
                        "eligible": True,
                    }
                ],
            ),
        }
    )
    base = _sequenced_step(script, {"gitops.sync_status": [_SYNC_FAILED]})
    calls: list[str] = []

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        if request["step"] == "gitops.remediate_sync":
            calls.append(request["step"])
            raise ApplicationError("kubectl patch response lost", type="CommandFailure")
        result: dict[str, Any] = await base(request)
        return result

    history, result = await _run_to_history(
        StagingGitOpsDeliveryWorkflow, _gitops_input(), execute_control_step
    )

    assert calls == ["gitops.remediate_sync"]  # exactly one attempt, never retried
    assert result is not None
    assert (result["status"], result["blocker"], result["failure_class"]) == (
        "BLOCKED",
        "ARGO_SYNC_FAILED",
        "PATCH_CONFLICT",
    )
    (issue,) = _payloads(history, "gitops.environment_issue")
    assert issue["action"] == "report_sync"
    notices = [notice["event"] for notice in _payloads(history, "gitops.notify_sync")]
    assert notices[0] == "detected" and notices[-1] == "escalated"


# -- NEW-114: failures pause, free slots refill, nothing-delivered is reported --------


@workflow.defn(name="ExistingGitHubBacklogLoopWorkflow", sandboxed=False)
class _PreNew114BacklogLoopWorkflow(ExistingGitHubBacklogLoopWorkflow):
    """ExistingGitHubBacklogLoopWorkflow as it ran before the NEW-114 markers existed."""

    @workflow.run
    async def run(self, request: ExistingGitHubBacklogLoopInput) -> dict[str, Any]:
        return await super().run(request)

    @staticmethod
    def _child_failure_pauses_patched(ordinal: int) -> bool:
        return False

    @staticmethod
    def _free_slots_patched(ordinal: int) -> bool:
        return False

    @staticmethod
    def _nothing_delivered_patched() -> bool:
        return False


def _new_114_markers(history: Any) -> list[bytes]:
    return [
        marker
        for marker in _markers(history)
        if any(
            name in marker
            for name in (b"child-failure-pauses", b"free-slots", b"nothing-delivered")
        )
    ]


@workflow.defn(name="IssueDeliveryWorkflow", sandboxed=False)
class _DeliveredIssueStub:
    """Stands in for IssueDeliveryWorkflow: the issue's source PR is published."""

    @workflow.run
    async def run(self, request: IssueDeliveryInput) -> dict[str, Any]:
        return {"status": "SOURCE_PR", "source_pr_url": "https://example.invalid/pr/1"}


@workflow.defn(name="FindingVerificationWorkflow", sandboxed=False)
class _VerdictVerificationStub:
    """FindingVerificationWorkflow returning the verdict encoded in its finding id."""

    @workflow.run
    async def run(self, request: Any) -> dict[str, Any]:
        finding = request["finding_id"] if isinstance(request, dict) else request.finding_id
        return {"status": str(finding).rsplit("-", 1)[1]}


async def _run_new_114_loop(
    run_arg: ExistingGitHubBacklogLoopInput,
    activity_fn: Any,
    signals: list[tuple[str, Any]],
    workflows: list[type],
) -> tuple[Any, Any, list[str], list[dict[str, Any]]]:
    """Run the loop; at each operator pause send the next signal and keep the status
    the loop reported half a second after it."""
    statuses: list[dict[str, Any]] = []
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
                id=f"replay-test-new-114-{uuid.uuid4()}",
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
                statuses.append(await handle.query("status"))
            result = await handle.result()
            history = await handle.fetch_history()
    children = [
        event.start_child_workflow_execution_initiated_event_attributes.workflow_id
        for event in history.events
        if event.HasField("start_child_workflow_execution_initiated_event_attributes")
    ]
    return history, result, children, statuses


def _loop_states(history: Any, loop_status: str) -> list[dict[str, Any]]:
    return [
        request["payload"]
        for request in _scheduled_steps(history)
        if request["step"] == "backlog.record_loop_state"
        and request["payload"].get("loop_status") == loop_status
    ]


def _item(number: int, repository: str = "gntik-ai/musematic") -> dict[str, Any]:
    return {"repository": repository, "issue_number": number}


async def test_new_114_a_failed_delivery_child_pauses_with_its_code_even_when_continuing() -> None:
    # Live 2026-10-02: an IssueDeliveryWorkflow child FAILED with a non-retryable
    # PolicyViolation ("base SHA mismatch") and the loop recorded COMPLETE.
    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        step = request["step"]
        if step == "delivery.load_confirmed_finding":
            raise ApplicationError(
                "base SHA mismatch for falcone-charts: expected aaaa, got bbbb",
                type="PolicyViolation",
                non_retryable=True,
            )
        script: dict[str, dict[str, Any]] = {
            "backlog.load_execution": {"status": "SELECTED", "items": [_item(1026)]},
            "backlog.register_run": {"status": "REGISTERED"},
            "backlog.revalidate_gate": {
                "status": "READY",
                "finding_id": "finding-1",
                "risk_class": "R1",
            },
            "backlog.register_child_run": {"status": "REGISTERED"},
            "delivery.release_leases": {"status": "LEASES_RELEASED"},
            "backlog.record_loop_state": {"status": "RECORDED"},
            "backlog.record_skip": {
                "status": "SKIPPED",
                "reason_code": "OPERATOR",
                "repository": "gntik-ai/musematic",
                "issue_number": 1026,
            },
            "github.refresh_mirrors": {"status": "PASS"},
        }
        return _registered_child(step, request, script[step])

    history, result, children, statuses = await _run_new_114_loop(
        ExistingGitHubBacklogLoopInput(
            project="musematic",
            backlog_campaign_id="camp-1",
            maximum_risk="R1",
            delivery_mode="source-pr",
            continue_after_blocked_issue=True,
        ),
        execute_control_step,
        [
            # A retry for an issue the loop is not waiting on is ignored, but recorded.
            ("retry", "gntik-ai/falcone#45"),
            ("retry", "gntik-ai/musematic#1026"),
            ("skip", "gntik-ai/musematic#1026|OPERATOR"),
        ],
        [ExistingGitHubBacklogLoopWorkflow, IssueDeliveryWorkflow],
    )

    paused = _loop_states(history, "PAUSED")
    assert [state["blocker"] for state in paused] == ["DELIVERY_FAILED:BASE_SHA_MISMATCH"] * 2
    assert all(state.get("workflow_id") for state in paused)
    assert statuses[0]["ignored_signals"] == [
        {"signal": "retry", "value": "gntik-ai/falcone#45", "awaited": "gntik-ai/musematic#1026"}
    ]
    assert [child.rsplit("-issue-", 1)[1] for child in children] == [
        "1-attempt-1",
        "1-retry-1-attempt-1",
    ]
    # The skipped issue frees its slot; nothing else is eligible, so the loop says so.
    assert result["status"] == "COMPLETE"
    assert result["completion"] == "COMPLETE_NOTHING_DELIVERED"
    (complete,) = _loop_states(history, "COMPLETE")
    assert complete["completion"] == "COMPLETE_NOTHING_DELIVERED"
    assert complete["outcomes"] == ["gntik-ai/musematic#1026:SKIPPED:OPERATOR"]
    refills = [
        request["payload"]
        for request in _scheduled_steps(history)
        if request["step"] == "backlog.load_execution"
        and "exclude_issue_refs" in request["payload"]
    ]
    assert [refill["exclude_issue_refs"] for refill in refills] == [["gntik-ai/musematic#1026"]]
    await Replayer(
        workflows=[ExistingGitHubBacklogLoopWorkflow, IssueDeliveryWorkflow]
    ).replay_workflow(history)


async def test_new_114_a_failed_revalidation_pauses_even_when_continuing() -> None:
    # Live 2026-10-02: a FindingVerificationWorkflow child FAILED (provider error) and the
    # loop moved on and recorded COMPLETE.
    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        step = request["step"]
        script: dict[str, dict[str, Any]] = {
            "backlog.load_execution": {"status": "SELECTED", "items": [_item(7)]},
            "backlog.register_run": {"status": "REGISTERED"},
            "backlog.revalidate_gate": {
                "status": "BLOCKED",
                "blocker": "CURRENT_SOURCE_REVALIDATION_REQUIRED",
            },
            "backlog.materialize_candidate": {"status": "READY", "finding_id": "finding-FAILED"},
            "backlog.register_child_run": {"status": "REGISTERED"},
            "backlog.record_revalidation": {"status": "REVALIDATION_RECORDED"},
            "backlog.record_loop_state": {"status": "RECORDED"},
            "backlog.record_skip": {"status": "SKIPPED", "reason_code": "OPERATOR"},
            "github.refresh_mirrors": {"status": "PASS"},
        }
        return _registered_child(step, request, script[step])

    history, result, children, _ = await _run_new_114_loop(
        ExistingGitHubBacklogLoopInput(
            project="musematic",
            backlog_campaign_id="camp-1",
            maximum_risk="R1",
            delivery_mode="source-pr",
            continue_after_blocked_issue=True,
        ),
        execute_control_step,
        [("retry", "gntik-ai/musematic#7"), ("skip", "gntik-ai/musematic#7|OPERATOR")],
        [ExistingGitHubBacklogLoopWorkflow, _VerdictVerificationStub],
    )

    assert [state["blocker"] for state in _loop_states(history, "PAUSED")] == [
        "REVALIDATION_FAILED"
    ] * 2
    assert [child.rsplit("-issue-", 1)[1] for child in children] == [
        "1-revalidate-7",
        "1-revalidate-7-r1",
    ]
    assert result["completion"] == "COMPLETE_NOTHING_DELIVERED"
    await Replayer(
        workflows=[ExistingGitHubBacklogLoopWorkflow, _VerdictVerificationStub]
    ).replay_workflow(history)


def _free_slot_activity(
    gates: dict[int, dict[str, Any]], selectable: list[int]
) -> tuple[Any, list[dict[str, Any]]]:
    """Activity double whose backlog.load_execution honours exclude_issue_refs and, like
    the real approval check, selects nothing while the campaign is not RUNNING."""
    loads: list[dict[str, Any]] = []
    campaign = {"status": "RUNNING"}

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        step, payload = request["step"], request["payload"]
        if step == "backlog.record_loop_state":
            campaign["status"] = payload["loop_status"]
            return {"status": "RECORDED"}
        if step == "backlog.load_execution":
            loads.append(payload)
            if campaign["status"] != "RUNNING":
                return {"status": "BLOCKED", "blocker": "CURRENT_BACKLOG_APPROVAL_REQUIRED"}
            excluded = set(payload.get("exclude_issue_refs") or ())
            items = [
                _item(number)
                for number in selectable
                if f"gntik-ai/musematic#{number}" not in excluded
            ][: payload["maximum_items"]]
            return {"status": "SELECTED", "items": items}
        if step == "backlog.revalidate_gate":
            return gates[int(payload["item"]["issue_number"])]
        if step == "backlog.materialize_candidate":
            number = payload["item"]["issue_number"]
            return {"status": "READY", "finding_id": f"finding-{number}-REJECTED"}
        script: dict[str, dict[str, Any]] = {
            "backlog.register_run": {"status": "REGISTERED"},
            "backlog.register_child_run": {"status": "REGISTERED"},
            "backlog.record_revalidation": {"status": "REVALIDATION_RECORDED"},
            "backlog.record_loop_state": {"status": "RECORDED"},
            "backlog.record_skip": {"status": "SKIPPED", "reason_code": "NEEDS_PRODUCT_DECISION"},
            "github.refresh_mirrors": {"status": "PASS"},
        }
        return _registered_child(step, request, script[step])

    return execute_control_step, loads


_READY = {"status": "READY", "finding_id": "finding-ready", "risk_class": "R1"}
_REVALIDATE = {"status": "BLOCKED", "blocker": "CURRENT_SOURCE_REVALIDATION_REQUIRED"}


async def test_new_114_a_rejected_issue_frees_its_slot_for_the_next_eligible_one() -> None:
    # Live 2026-10-03: the only item was REJECTED and the loop recorded COMPLETE although
    # #2 was eligible within the same approval.
    activity_fn, loads = _free_slot_activity({1: _REVALIDATE, 2: _READY}, [1, 2])
    history, result, children, _ = await _run_new_114_loop(
        ExistingGitHubBacklogLoopInput(
            project="musematic",
            backlog_campaign_id="camp-1",
            maximum_risk="R1",
            delivery_mode="source-pr",
            maximum_items=1,
        ),
        activity_fn,
        [],
        [ExistingGitHubBacklogLoopWorkflow, _VerdictVerificationStub, _DeliveredIssueStub],
    )

    assert [child.rsplit("-issue-", 1)[1] for child in children] == [
        "1-revalidate-1",
        "2-attempt-1",
    ]
    assert result["status"] == "COMPLETE" and "completion" not in result
    assert {"status": "BLOCKED", "blocker": "CURRENT_REVALIDATION_REJECTED"}.items() <= (
        result["results"][0].items()
    )
    assert [load.get("exclude_issue_refs") for load in loads] == [
        None,
        None,
        ["gntik-ai/musematic#1"],
    ]
    assert all(load["maximum_items"] == 1 for load in loads)
    await Replayer(
        workflows=[ExistingGitHubBacklogLoopWorkflow, _VerdictVerificationStub, _DeliveredIssueStub]
    ).replay_workflow(history)


async def test_new_114_a_skip_no_longer_leaks_into_the_next_issue() -> None:
    # Before NEW-114 the "skipped" check read the last result of any issue, so the issue
    # after a skipped one was skipped too, without a delivery.
    blocked = {"status": "BLOCKED", "blocker": "R3_REQUIRES_OPERATOR"}
    activity_fn, _ = _free_slot_activity({1: blocked, 2: _READY}, [1, 2])
    run_arg = ExistingGitHubBacklogLoopInput(
        project="musematic",
        backlog_campaign_id="camp-1",
        maximum_risk="R1",
        delivery_mode="source-pr",
        maximum_items=2,
    )
    workflows = [ExistingGitHubBacklogLoopWorkflow, _DeliveredIssueStub]
    history, result, children, _ = await _run_new_114_loop(
        run_arg, activity_fn, [("skip", "gntik-ai/musematic#1|NEEDS_PRODUCT_DECISION")], workflows
    )

    assert [child.rsplit("-issue-", 1)[1] for child in children] == ["2-attempt-1"]
    assert result["status"] == "COMPLETE" and "completion" not in result
    await Replayer(workflows=workflows).replay_workflow(history)

    # The same run recorded before NEW-114 skipped #2 and replays unchanged.
    activity_fn, _ = _free_slot_activity({1: blocked, 2: _READY}, [1, 2])
    old_workflows = [_PreNew114BacklogLoopWorkflow, _DeliveredIssueStub]
    old_history, old_result, old_children, _ = await _run_new_114_loop(
        run_arg,
        activity_fn,
        [("skip", "gntik-ai/musematic#1|NEEDS_PRODUCT_DECISION")],
        old_workflows,
    )
    assert old_children == []
    assert old_result["status"] == "COMPLETE" and "completion" not in old_result
    assert not _new_114_markers(old_history)
    await Replayer(workflows=workflows).replay_workflow(old_history)


async def test_new_114_a_gate_skip_reactivates_the_campaign_before_refilling() -> None:
    # Review of 1f16012: the gate pause left the campaign PAUSED, so the refill's approval
    # check refused selection and the loop reported nothing delivered with #2 eligible.
    blocked = {"status": "BLOCKED", "blocker": "R3_REQUIRES_OPERATOR"}
    activity_fn, loads = _free_slot_activity({1: blocked, 2: _READY}, [1, 2])
    workflows = [ExistingGitHubBacklogLoopWorkflow, _DeliveredIssueStub]
    history, result, children, _ = await _run_new_114_loop(
        ExistingGitHubBacklogLoopInput(
            project="musematic",
            backlog_campaign_id="camp-1",
            maximum_risk="R1",
            delivery_mode="source-pr",
            maximum_items=1,
        ),
        activity_fn,
        [("skip", "gntik-ai/musematic#1|NEEDS_PRODUCT_DECISION")],
        workflows,
    )

    assert [child.rsplit("-issue-", 1)[1] for child in children] == ["2-attempt-1"]
    assert result["status"] == "COMPLETE" and "completion" not in result
    order = [
        request["payload"].get("loop_status") or "REFILL"
        for request in _scheduled_steps(history)
        if request["step"] == "backlog.record_loop_state"
        or "exclude_issue_refs" in request["payload"]
    ]
    assert order == ["PAUSED", "RUNNING", "REFILL", "COMPLETE"]
    assert [load.get("exclude_issue_refs") for load in loads] == [None, ["gntik-ai/musematic#1"]]
    await Replayer(workflows=workflows).replay_workflow(history)


async def test_new_114_a_gitops_skip_frees_the_slot_in_full_delivery() -> None:
    # Review of 1f16012: skipping a blocked GitOps stage completed a maximum_items=1 loop
    # although another issue was eligible.
    script = _full_delivery_loop_script()
    prepares = {"count": 0}
    campaign = {"status": "RUNNING"}

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        step, payload = request["step"], request["payload"]
        if step == "backlog.record_loop_state":
            campaign["status"] = payload["loop_status"]
            return {"status": "RECORDED"}
        if step == "backlog.load_execution":
            if campaign["status"] != "RUNNING":
                return {"status": "BLOCKED", "blocker": "CURRENT_BACKLOG_APPROVAL_REQUIRED"}
            excluded = set(payload.get("exclude_issue_refs") or ())
            items = [
                _item(number)
                for number in (1026, 1027)
                if f"gntik-ai/musematic#{number}" not in excluded
            ]
            return {"status": "SELECTED", "items": items[: payload["maximum_items"]]}
        if step == "gitops.prepare":
            prepares["count"] += 1
            if prepares["count"] == 1:
                return {"status": "BLOCKED", "blocker": "ENVIRONMENT_UNHEALTHY"}
        if step == "github.refresh_mirrors":
            return {"status": "PASS"}
        return _registered_child(step, request, script[step])

    history, result, children = await _run_loop(
        script,
        [("skip", "gntik-ai/musematic#1026|OPERATOR")],
        activity_fn=execute_control_step,
    )

    assert [child.rsplit("-issue-", 1)[1] for child in children] == [
        "1-attempt-1",
        "1-staging-gitops",
        "2-attempt-1",
        "2-staging-gitops",
    ]
    assert result["status"] == "COMPLETE" and "completion" not in result
    await Replayer(workflows=_LOOP_WORKFLOWS).replay_workflow(history)


def _approved_backlog(tmp_path: Any) -> tuple[Any, str]:
    """A real EngineeringActivityHandler over a RUNNING, genuinely approved campaign with
    three eligible issues (#1, #2, #3)."""
    from datetime import UTC, datetime
    from datetime import timedelta as delta

    from engineering_control.activities.engineering import EngineeringActivityHandler
    from engineering_control.config import Settings
    from engineering_control.db.base import Base
    from engineering_control.db.models import BacklogIssueRecord, GitHubBacklogCampaign
    from engineering_control.engineering.backlog import (
        BACKLOG_APPROVAL_PHRASE,
        BacklogApprovalBinding,
    )

    campaign_id = "11411411-4114-4114-8114-000000000001"
    state_root = tmp_path / "state"
    (state_root / "github-policy-requests").mkdir(parents=True, mode=0o700)
    handler = EngineeringActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'approved.sqlite'}", state_root=state_root)
    )
    Base.metadata.create_all(handler.engine)
    now = datetime.now(UTC)
    binding = BacklogApprovalBinding(
        backlog_campaign_id=campaign_id,
        project="musematic",
        paired_repositories=("gntik-ai/musematic", "gntik-ai/musematic-deploy"),
        source_head="a" * 40,
        deployment_head="b" * 40,
        issue_filter="all",
        dependency_plan_hash="d" * 64,
        maximum_risk="R1",
        maximum_items=1,
        delivery_mode="source-pr",
        allowed_github_operations=(),
        allowed_build_workflows=(),
        staging_targets=(),
        include_enhancements=False,
        merge_policy="no-merge",
        issue_close_policy="never",
        expires_at=now + delta(hours=4),
    )
    with handler.sessions.begin() as session:
        session.add(
            GitHubBacklogCampaign(
                id=uuid.UUID(campaign_id),
                project="musematic",
                source_repository="gntik-ai/musematic",
                deployment_repository="gntik-ai/musematic-deploy",
                imported_at=now,
                issue_query="state:open",
                import_filter="all",
                source_default_branch="main",
                deployment_default_branch="main",
                source_head_at_import="a" * 40,
                deployment_head_at_import="b" * 40,
                issue_count=3,
                eligible_count=3,
                deferred_count=0,
                duplicate_count=0,
                stale_count=0,
                already_fixed_count=0,
                existing_pr_count=0,
                blocked_count=0,
                dependency_graph_hash="c" * 64,
                plan_hash="d" * 64,
                approval_binding=binding.sha256(),
                approval_json={
                    "binding": binding.model_dump(mode="json"),
                    "approval": {
                        "approved_by": "cli:engineering-ctl",
                        "approved_at": now.isoformat(),
                        "typed_confirmation": BACKLOG_APPROVAL_PHRASE,
                    },
                },
                maximum_risk="R1",
                maximum_items=1,
                maximum_concurrency=1,
                delivery_mode="source-pr",
                include_enhancements=False,
                status="RUNNING",
                workflow_id=f"backlog-import-musematic-{campaign_id}",
            )
        )
        for number in (1, 2, 3):
            session.add(
                BacklogIssueRecord(
                    backlog_campaign_id=uuid.UUID(campaign_id),
                    repository="gntik-ai/musematic",
                    issue_number=number,
                    issue_url_reference=f"https://github.com/gntik-ai/musematic/issues/{number}",
                    title=f"Bounded issue {number}",
                    state="OPEN",
                    labels_json=["bug"],
                    milestone=None,
                    issue_type="PRODUCT_BUG",
                    openspec_kind=None,
                    severity="MEDIUM",
                    calculated_risk="R1",
                    component="general",
                    source_or_deployment_owner="source",
                    documentation_levels_json=[],
                    dependencies_json=[],
                    blocking_issues_json=[],
                    issues_blocked_json=[],
                    linked_prs_json=[],
                    linked_branches_json=[],
                    linked_commits_json=[],
                    duplicate_candidates_json=[],
                    current_source_reproduction="PENDING_CURRENT_STATE_REVALIDATION",
                    current_runtime_reproduction="NOT_REQUIRED",
                    current_runtime_required=False,
                    reproduction_mode="SOURCE_ONLY",
                    candidate_status="IMPORTED",
                    eligibility_status="ELIGIBLE_FOR_REVALIDATION",
                    eligibility_reason="FILTER_MATCH_PENDING_REVALIDATION",
                    planned_wave="W0",
                    planned_order=number,
                    finding_id=None,
                    change_set_id=None,
                    delivery_status="NOT_STARTED",
                    final_closure_status="OPEN",
                    documentation_impact="DOCUMENTATION_REQUIRED",
                    body_sha256=str(number) * 64,
                    github_updated_at=now,
                    github_snapshot_json={"body_excerpt": f"bounded body {number}"},
                )
            )
    return handler, campaign_id


async def test_new_114_refill_after_a_gate_skip_passes_the_real_approval_check(
    tmp_path: Any,
) -> None:
    # The real backlog.load_execution/record_loop_state: the gate pause sets the campaign
    # PAUSED (voiding its approval for selection); the refill reactivates it first.
    handler, campaign_id = _approved_backlog(tmp_path)
    gates = {
        1: {"status": "BLOCKED", "blocker": "R3_REQUIRES_OPERATOR"},
        2: _READY,
        3: _READY,
    }

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        step, payload = request["step"], request["payload"]
        if step == "backlog.load_execution":
            return handler._load_backlog_execution(payload)
        if step == "backlog.record_loop_state":
            for pending in (tmp_path / "state" / "github-policy-requests").iterdir():
                pending.unlink()  # the root policy bridge consumes each request
            return handler._record_backlog_loop_state(payload)
        if step == "backlog.revalidate_gate":
            return gates[int(payload["item"]["issue_number"])]
        script: dict[str, dict[str, Any]] = {
            "backlog.register_run": {"status": "REGISTERED"},
            "backlog.register_child_run": {"status": "REGISTERED"},
            "backlog.record_skip": {"status": "SKIPPED", "reason_code": "NEEDS_PRODUCT_DECISION"},
            "github.refresh_mirrors": {"status": "PASS"},
        }
        return _registered_child(step, request, script[step])

    workflows = [ExistingGitHubBacklogLoopWorkflow, _DeliveredIssueStub]
    history, result, children, _ = await _run_new_114_loop(
        ExistingGitHubBacklogLoopInput(
            project="musematic",
            backlog_campaign_id=campaign_id,
            maximum_risk="R1",
            delivery_mode="source-pr",
            maximum_items=1,
        ),
        execute_control_step,
        [("skip", "gntik-ai/musematic#1|NEEDS_PRODUCT_DECISION")],
        workflows,
    )

    # One slot: #1 was skipped (freed), #2 delivered, #3 never started.
    assert [child.rsplit("-issue-", 1)[1] for child in children] == ["2-attempt-1"]
    assert result["status"] == "COMPLETE" and "completion" not in result
    await Replayer(workflows=workflows).replay_workflow(history)


async def test_issue_amend_observation_reads_the_body_on_the_github_queue_and_replays() -> None:
    """NEW-115: /loop amend reads the current issue body with the delivery's own
    read-only step on the GitHub worker; a new workflow type, so no patch marker."""
    observed = {
        "status": "ISSUE_BODY_REFRESHED",
        "source_body_sha256": "c" * 64,
        "body_excerpt": "Edited body",
        "body_sha256": "d" * 64,
        "body_chars": 11,
        "issue_state": "OPEN",
        "updated_at": "2026-10-03T10:00:00Z",
    }
    history, result = await _run_to_history(
        IssueAmendObservationWorkflow,
        IssueAmendObservationInput(issue_repository="gntik-ai/falcone", issue_number=42),
        _scripted_execute_control_step({"delivery.refresh_issue_body": observed}),
    )

    assert result == observed
    assert _scheduled_steps(history) == [
        {
            "step": "delivery.refresh_issue_body",
            "payload": {"issue_repository": "gntik-ai/falcone", "issue_number": 42},
        }
    ]
    queues = [
        event.activity_task_scheduled_event_attributes.task_queue.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    assert queues == [GITHUB_TASK_QUEUE]
    assert _markers(history) == []
    await Replayer(workflows=[IssueAmendObservationWorkflow]).replay_workflow(history)
