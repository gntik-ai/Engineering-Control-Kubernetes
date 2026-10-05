"""NEW-137: replay safety of the provider-unavailable loop pause and failure type."""

from __future__ import annotations

import json
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer
from test_workflow_replay import (
    _DELIVERY_TO_SPEC_SCRIPT,
    _TEMPORAL_CLI,
    _full_delivery_loop_script,
    _markers,
    _paused_blockers,
    _run_loop,
    _run_to_history,
    _scheduled_steps,
)

from engineering_control.workflows.definitions import (
    ExistingGitHubBacklogLoopWorkflow,
    IssueDeliveryWorkflow,
)
from engineering_control.workflows.inputs import (
    ExistingGitHubBacklogLoopInput,
    IssueDeliveryInput,
)

pytestmark = pytest.mark.skipif(
    _TEMPORAL_CLI is None,
    reason="requires the `temporal` CLI on PATH to run a local dev server",
)

_INVOCATION = "6f1a7a52-0d4e-4a43-9a3c-1370000000a1"


def _checker_context(summary: str, kind: str) -> dict[str, Any]:
    return {
        "change_set_id": "43b4dc63-0000-4000-8000-000000000137",
        "status": "FAILED",
        "provider_step": "delivery.invoke_checker",
        "checker_request": {"run_id": _INVOCATION, "provider": "claude", "role": "code-reviewer"},
        "agent_invocation_id": _INVOCATION,
        "agent_invocation_status": "FAILED",
        "agent_result": {
            "run_id": _INVOCATION,
            "provider": "claude",
            "role": "code-reviewer",
            "status": "FAILED",
            "error_code": "PROVIDER_EXIT_NONZERO",
            "summary": summary,
            "provider_error_kind": kind,
        },
    }


@workflow.defn(name="IssueDeliveryWorkflow", sandboxed=False)
class _ProviderOverloadedDeliveryStub:
    """falcone #1012: every attempt's Claude checker fails with 529 Overloaded."""

    @workflow.run
    async def run(self, request: IssueDeliveryInput) -> dict[str, Any]:
        return {
            "status": "FAILED",
            "context": _checker_context("API Error: 529 Overloaded", "OVERLOADED"),
        }


@workflow.defn(name="IssueDeliveryWorkflow", sandboxed=False)
class _CheckerAuthFailureDeliveryStub:
    @workflow.run
    async def run(self, request: IssueDeliveryInput) -> dict[str, Any]:
        return {
            "status": "FAILED",
            "context": _checker_context("API Error: 401 authentication_error", "NON_TRANSIENT"),
        }


@workflow.defn(name="ExistingGitHubBacklogLoopWorkflow", sandboxed=False)
class _PreNew137BacklogLoopWorkflow(ExistingGitHubBacklogLoopWorkflow):
    """The loop exactly as it ran before existing-backlog-provider-unavailable-pause-v1."""

    @workflow.run
    async def run(self, request: ExistingGitHubBacklogLoopInput) -> dict[str, Any]:
        return await super().run(request)

    @staticmethod
    def _provider_unavailable_pause(
        delivery: dict[str, Any], ordinal: int, delivery_round: int
    ) -> bool:
        return False


_RETRY_THEN_SKIP = [
    ("retry", "gntik-ai/musematic#1026"),
    ("skip", "gntik-ai/musematic#1026|OPERATOR"),
]


def _new_137_markers(history: Any) -> list[bytes]:
    return [marker for marker in _markers(history) if b"provider-unavailable-pause" in marker]


async def test_a_provider_outage_pauses_with_provider_unavailable_each_round() -> None:
    workflows: list[type] = [ExistingGitHubBacklogLoopWorkflow, _ProviderOverloadedDeliveryStub]
    history, result, children = await _run_loop(
        _full_delivery_loop_script(), _RETRY_THEN_SKIP, workflows=workflows
    )

    assert result["status"] == "COMPLETE"
    # A FAILED child ends its round: no automatic second attempt, one child per retry.
    assert [child.rsplit("-issue-", 1)[1] for child in children] == [
        "1-attempt-1",
        "1-retry-1-attempt-1",
    ]
    assert _paused_blockers(history) == [
        "DELIVERY_FAILED:PROVIDER_UNAVAILABLE",
        "DELIVERY_FAILED:PROVIDER_UNAVAILABLE",
    ]
    markers = _new_137_markers(history)
    assert any(b"existing-backlog-provider-unavailable-pause-v1-1-0" in m for m in markers)
    assert any(b"existing-backlog-provider-unavailable-pause-v1-1-1" in m for m in markers)
    await Replayer(workflows=workflows).replay_workflow(history)


async def test_a_non_transient_child_failure_keeps_its_code_and_records_no_marker() -> None:
    workflows: list[type] = [ExistingGitHubBacklogLoopWorkflow, _CheckerAuthFailureDeliveryStub]
    history, result, _children = await _run_loop(
        _full_delivery_loop_script(), _RETRY_THEN_SKIP, workflows=workflows
    )

    assert result["status"] == "COMPLETE"
    assert _paused_blockers(history) == ["DELIVERY_FAILED:FAILED", "DELIVERY_FAILED:FAILED"]
    assert _new_137_markers(history) == []
    await Replayer(workflows=workflows).replay_workflow(history)


async def test_a_pre_new_137_paused_loop_replays_on_the_current_loop() -> None:
    history, result, _children = await _run_loop(
        _full_delivery_loop_script(),
        _RETRY_THEN_SKIP,
        workflows=[_PreNew137BacklogLoopWorkflow, _ProviderOverloadedDeliveryStub],
    )

    assert result["status"] == "COMPLETE"
    assert _paused_blockers(history) == ["DELIVERY_FAILED:FAILED", "DELIVERY_FAILED:FAILED"]
    assert _new_137_markers(history) == []
    await Replayer(
        workflows=[ExistingGitHubBacklogLoopWorkflow, _ProviderOverloadedDeliveryStub]
    ).replay_workflow(history)


_CHECKER_ERROR_SCRIPT: dict[str, dict[str, Any]] = {
    **_DELIVERY_TO_SPEC_SCRIPT,
    "delivery.prepare_spec": {"status": "SPEC_READY", "run_id": "run-1"},
    "agent.record_start": {"status": "STARTED", "run_id": "run-1"},
    "delivery.invoke_spec": {"status": "SUCCESS", "run_id": "run-1"},
    "delivery.invoke_maker": {"status": "SUCCESS", "run_id": "run-1"},
    "delivery.invoke_deployment_maker": {"status": "SUCCESS", "run_id": "run-1"},
    "delivery.create_openspec": {"status": "PLANNED", "run_id": "run-1"},
    "delivery.prepare_maker": {"status": "SOURCE_MAKER_READY", "run_id": "run-1"},
    "delivery.prepare_deployment_maker": {"status": "READY", "run_id": "run-1"},
    "delivery.converge_deployment_pin": {"status": "DEPLOYMENT_PIN_UNCHANGED", "run_id": "run-1"},
    "delivery.prepare_checker": {"status": "CHECKER_READY", "run_id": "run-1"},
}


@activity.defn(name="execute_control_step")
async def _checker_raises(request: dict[str, Any]) -> dict[str, Any]:
    step = request["step"]
    if step == "delivery.invoke_checker":
        raise ApplicationError(
            "checker is not configured", type="NotConfigured", non_retryable=True
        )
    if step == "agent.persist_result":
        payload = request["payload"]
        if payload.get("status") == "FAILED":
            return payload  # the real activity returns {**payload, ...}
        return {
            "status": "SUCCESS",
            "run_id": "run-1",
            "agent_result": {"structured_result": {"decision": "COMPLETE"}},
        }
    return _CHECKER_ERROR_SCRIPT[step]


async def test_a_checker_activity_error_carries_its_bounded_type_and_replays() -> None:
    run_arg = IssueDeliveryInput(finding_id="finding-1", risk_class="R1", dry_run=False)
    history, result = await _run_to_history(IssueDeliveryWorkflow, run_arg, _checker_raises)

    assert result is not None and result["status"] == "FAILED"
    failed = [
        request["payload"]
        for request in _scheduled_steps(history)
        if request["step"] == "agent.persist_result"
        and request["payload"].get("status") == "FAILED"
    ]
    assert len(failed) == 1
    assert failed[0]["blocker"] == "PROVIDER_ACTIVITY_FAILED"
    assert failed[0]["provider_failure_type"] == "NOT_CONFIGURED"
    # NEW-137 review: the previous call's result never travels into persist_result.
    assert "agent_result" not in failed[0]
    released = [
        request["payload"]
        for request in _scheduled_steps(history)
        if request["step"] == "delivery.release_leases"
    ]
    assert released and released[-1]["provider_failure_type"] == "NOT_CONFIGURED"
    await Replayer(workflows=[IssueDeliveryWorkflow]).replay_workflow(history)


async def test_a_pre_new_137_checker_error_history_replays_without_the_type() -> None:
    # Histories recorded before NEW-137 carry no provider_failure_type in the activity
    # inputs; the key is data only (no command), so they replay on the current code.
    run_arg = IssueDeliveryInput(finding_id="finding-1", risk_class="R1", dry_run=False)
    history, _result = await _run_to_history(IssueDeliveryWorkflow, run_arg, _checker_raises)
    stripped = 0
    for event in history.events:
        if not event.HasField("activity_task_scheduled_event_attributes"):
            continue
        payload = event.activity_task_scheduled_event_attributes.input.payloads[0]
        request = json.loads(payload.data)
        if "provider_failure_type" in request["payload"]:
            del request["payload"]["provider_failure_type"]
            payload.data = json.dumps(request).encode()
            stripped += 1
    assert stripped >= 2  # agent.persist_result and delivery.release_leases
    await Replayer(workflows=[IssueDeliveryWorkflow]).replay_workflow(history)
