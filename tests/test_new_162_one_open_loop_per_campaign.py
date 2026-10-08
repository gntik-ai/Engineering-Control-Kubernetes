"""NEW-162: a campaign holds at most one open issue loop, and a skip closes its own loop.

falcone campaign 31c772b3 (2026-10-08): issue-start of #1008 while #978's loop was paused
replaced the campaign's one per-issue approval (BacklogApprovalBinding.issue_approval), so
#978's next child registration was refused ("direct child campaign approval differs from its
operation"); the loops' pause and end states overwrote the one campaign status; and the
final skip of #978 re-raised the step error and failed the campaign. Issue-start now refuses
while another loop of the campaign is open (operator decision 2026-10-08: one open loop per
campaign), and a skip after a failed loop step completes the loop."""

from __future__ import annotations

import asyncio
import shutil
import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from test_controlled_operations import CAMPAIGN_ID, _idempotency_fixture, _solve_issue
from test_issue_operation_approval import _approved_direct_request
from test_workflow_replay import (
    _LOOP_WORKFLOWS,
    _full_delivery_loop_script,
    _registered_child,
    _scheduled_steps,
)

from engineering_control.api.schemas import (
    BacklogApprovalCandidateRequest,
    ExistingBacklogLoopStart,
)
from engineering_control.db.models import (
    BacklogIssueRecord,
    GitHubBacklogCampaign,
    OperationRequest,
    Run,
)
from engineering_control.operations.issue_approval import (
    campaign_loop_prefix,
    operation_holds_campaign,
)
from engineering_control.operations.issue_start import issue_workflow_id
from engineering_control.operations.service import TypedCommandBus
from engineering_control.workflows.definitions import ExistingGitHubBacklogLoopWorkflow
from engineering_control.workflows.inputs import ExistingGitHubBacklogLoopInput
from engineering_control.workflows.worker_registry import CONTROL_TASK_QUEUE, GITHUB_TASK_QUEUE


async def _sibling_request(operations: Any, request: ExistingBacklogLoopStart) -> Any:
    """Issue #43 of the same campaign, with its own claimed issue-start operation."""
    with operations.sessions.begin() as session:
        original = session.scalar(
            select(BacklogIssueRecord).where(BacklogIssueRecord.issue_number == 42)
        )
        session.add(
            BacklogIssueRecord(
                **{
                    column.name: getattr(original, column.name)
                    for column in BacklogIssueRecord.__table__.columns
                    if column.name
                    not in {"id", "issue_number", "issue_url_reference", "finding_id"}
                },
                id=uuid4(),
                issue_number=43,
                issue_url_reference="https://github.com/gntik-ai/falcone/issues/43",
            )
        )
    created = await operations.create_external(
        _solve_issue().model_copy(update={"issue_number": 43}),
        source_channel="cli",
        operator_class="engineering-ctl:hermes",
        idempotency_key=_idempotency_fixture("direct", 53),
    )
    assert await operations.claim(created.operation_id) is not None
    return ExistingBacklogLoopStart(
        **{
            **request.model_dump(),
            "target_issue_number": 43,
            "issue_operation_id": created.operation_id,
            "issue_approval_binding": created.binding_hash,
        }
    )


async def _prepare(metadata: Any, request: ExistingBacklogLoopStart) -> dict[str, Any]:
    return await metadata.prepare_backlog_approval(
        CAMPAIGN_ID,
        BacklogApprovalCandidateRequest(
            project="falcone",
            delivery_mode=request.delivery_mode,
            maximum_risk=request.maximum_risk,
            maximum_items=1,
        ),
        issue_request=request,
    )


def _approval(operations: Any) -> tuple[str, str]:
    with operations.sessions() as session:
        campaign = session.get(GitHubBacklogCampaign, CAMPAIGN_ID)
        return campaign.approval_binding, campaign.status


def _operation(operations: Any, operation_id: UUID, **values: Any) -> None:
    with operations.sessions.begin() as session:
        operation = session.get(OperationRequest, operation_id)
        for name, value in values.items():
            setattr(operation, name, value)


def _loop_run(operations: Any, workflow_id: str, status: str) -> None:
    with operations.sessions.begin() as session:
        run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
        if run is None:
            session.add(
                Run(
                    workflow_id=workflow_id,
                    run_type="existing-backlog-loop",
                    status=status,
                    project="falcone",
                )
            )
        else:
            run.status = status


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["PAUSED", "RUNNING", "BLOCKED"])
async def test_issue_start_refuses_while_a_sibling_loop_is_open(tmp_path, state: str) -> None:
    operations, metadata, _handler, request = await _approved_direct_request(tmp_path)
    loop = issue_workflow_id(request)
    _operation(operations, request.issue_operation_id, status="STARTED", workflow_id=loop)
    _loop_run(operations, loop, state)
    sibling = await _sibling_request(operations, request)
    before = _approval(operations)

    with pytest.raises(ValueError, match="ISSUE_START_CAMPAIGN_IN_USE") as refused:
        await _prepare(metadata, sibling)

    assert loop in str(refused.value)
    # The open loop keeps the campaign approval it runs under.
    assert _approval(operations) == before
    # Once that loop has ended (skipped, cancelled or delivered), the next issue starts.
    _loop_run(operations, loop, "COMPLETE")
    assert (await _prepare(metadata, sibling))["status"] == "AWAITING_APPROVAL"


@pytest.mark.asyncio
async def test_issue_start_refuses_while_the_approved_operation_is_handing_off(
    tmp_path,
) -> None:
    operations, metadata, _handler, request = await _approved_direct_request(tmp_path)
    sibling = await _sibling_request(operations, request)
    before = _approval(operations)

    # The approved operation is still dispatching, or started without a registered loop Run.
    for values in (
        {"status": "DISPATCHING"},
        {"status": "STARTED", "workflow_id": issue_workflow_id(request)},
    ):
        _operation(operations, request.issue_operation_id, **values)
        with pytest.raises(ValueError, match="ISSUE_START_CAMPAIGN_IN_USE"):
            await _prepare(metadata, sibling)
        assert _approval(operations) == before

    # A blocked or completed operation no longer holds the campaign.
    _operation(operations, request.issue_operation_id, status="BLOCKED")
    assert (await _prepare(metadata, sibling))["status"] == "AWAITING_APPROVAL"


@pytest.mark.asyncio
async def test_the_loops_own_operation_never_counts_as_a_sibling(tmp_path) -> None:
    # issue-reapprove of a paused loop re-runs the same prepare for that loop's operation.
    operations, metadata, _handler, request = await _approved_direct_request(tmp_path)
    loop = issue_workflow_id(request)
    _operation(operations, request.issue_operation_id, status="STARTED", workflow_id=loop)
    _loop_run(operations, loop, "PAUSED")

    assert (await _prepare(metadata, request))["status"] == "AWAITING_APPROVAL"


@pytest.mark.asyncio
async def test_a_campaign_wide_approval_refuses_while_an_issue_loop_is_open(tmp_path) -> None:
    # Review: a general backlog-approve/backlog-start replaced the issue approval of a paused
    # issue loop (falcone #972's NEW-143 history); it is refused like an issue-start.
    operations, metadata, _handler, request = await _approved_direct_request(tmp_path)
    loop = issue_workflow_id(request)
    _operation(operations, request.issue_operation_id, status="STARTED", workflow_id=loop)
    _loop_run(operations, loop, "PAUSED")
    before = _approval(operations)
    general = BacklogApprovalCandidateRequest(
        project="falcone",
        delivery_mode=request.delivery_mode,
        maximum_risk=request.maximum_risk,
        maximum_items=5,
    )

    with pytest.raises(ValueError, match="ISSUE_START_CAMPAIGN_IN_USE"):
        await metadata.prepare_backlog_approval(CAMPAIGN_ID, general)

    assert _approval(operations) == before
    _loop_run(operations, loop, "STOPPED")
    assert (await metadata.prepare_backlog_approval(CAMPAIGN_ID, general))["status"] == (
        "AWAITING_APPROVAL"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [True, False, None])
async def test_a_loop_closed_outside_hermes_is_released_only_when_temporal_confirms(
    tmp_path, closed: bool | None
) -> None:
    # Review: a loop terminated outside Hermes kept its Run RUNNING/PAUSED and would fence
    # its campaign forever under one open loop per campaign.
    operations, metadata, _handler, request = await _approved_direct_request(tmp_path)
    loop = issue_workflow_id(request)
    _operation(operations, request.issue_operation_id, status="STARTED", workflow_id=loop)
    _loop_run(operations, loop, "PAUSED")
    sibling = await _sibling_request(operations, request)
    control = Mock()
    control.workflow_id_closed = (
        AsyncMock(side_effect=RuntimeError("temporal unavailable"))
        if closed is None
        else AsyncMock(return_value=closed)
    )
    bus = TypedCommandBus(control, metadata, operations)
    prefix = campaign_loop_prefix("falcone", CAMPAIGN_ID)

    if closed is None:
        # An unknown state never releases anything: the start fails closed.
        with pytest.raises(RuntimeError):
            await bus._close_ended_campaign_loops(prefix, exclude=issue_workflow_id(sibling))
    else:
        await bus._close_ended_campaign_loops(prefix, exclude=issue_workflow_id(sibling))
    control.workflow_id_closed.assert_awaited_once_with(loop)
    with operations.sessions() as session:
        status = session.scalar(select(Run.status).where(Run.workflow_id == loop))
    if closed:
        assert status == "CANCELLED"
        assert (await _prepare(metadata, sibling))["status"] == "AWAITING_APPROVAL"
    else:
        assert status == "PAUSED"
        with pytest.raises(ValueError, match="ISSUE_START_CAMPAIGN_IN_USE"):
            await _prepare(metadata, sibling)


@pytest.mark.asyncio
async def test_a_campaign_wide_loop_may_renew_its_own_approval(tmp_path) -> None:
    # Review 2: an expired write-policy approval of a running campaign-wide loop is renewed
    # with backlog-approval-candidate; only issue loops block a campaign-wide approval.
    operations, metadata, _handler, request = await _approved_direct_request(tmp_path)
    _operation(operations, request.issue_operation_id, status="COMPLETED")
    _loop_run(operations, campaign_loop_prefix("falcone", CAMPAIGN_ID), "RUNNING")
    general = BacklogApprovalCandidateRequest(
        project="falcone",
        delivery_mode=request.delivery_mode,
        maximum_risk=request.maximum_risk,
        maximum_items=5,
    )
    assert (await metadata.prepare_backlog_approval(CAMPAIGN_ID, general))["status"] == (
        "AWAITING_APPROVAL"
    )
    # ...but an issue-start is refused while that campaign-wide loop is open.
    sibling = await _sibling_request(operations, request)
    with pytest.raises(ValueError, match="ISSUE_START_CAMPAIGN_IN_USE"):
        await _prepare(metadata, sibling)


@pytest.mark.asyncio
async def test_issue_start_refuses_while_a_campaign_wide_start_hands_off(tmp_path) -> None:
    # Review 2: a dispatching backlog-start has no loop Run and no issue approval yet.
    operations, metadata, _handler, request = await _approved_direct_request(tmp_path)
    _operation(operations, request.issue_operation_id, status="COMPLETED")
    with operations.sessions.begin() as session:
        original = session.get(OperationRequest, request.issue_operation_id)
        handoff = OperationRequest(
            **{
                column.name: getattr(original, column.name)
                for column in OperationRequest.__table__.columns
                if column.name
                not in {"id", "idempotency_key", "workflow_id", "target_id", "target_type"}
            },
            id=uuid4(),
            idempotency_key="campaign-wide-handoff-0001",
            target_type="backlog_campaign",
            target_id=str(CAMPAIGN_ID),
        )
        handoff.action = "solve_backlog"
        handoff.status = "DISPATCHING"
        session.add(handoff)
    sibling = await _sibling_request(operations, request)

    with pytest.raises(ValueError, match="campaign-wide operation"):
        await _prepare(metadata, sibling)
    _operation(operations, handoff.id, status="BLOCKED")
    assert (await _prepare(metadata, sibling))["status"] == "AWAITING_APPROVAL"


@pytest.mark.asyncio
async def test_a_loop_run_changed_after_its_snapshot_is_never_closed(tmp_path) -> None:
    # Review 2: a reset/restart recording a new state after the closure check must win.
    operations, _metadata, _handler, request = await _approved_direct_request(tmp_path)
    loop = issue_workflow_id(request)
    _loop_run(operations, loop, "PAUSED")
    prefix = campaign_loop_prefix("falcone", CAMPAIGN_ID)
    [(workflow_id, status, updated_at)] = await operations.open_campaign_loops(prefix, exclude=None)
    _loop_run(operations, loop, "RUNNING")  # the restarted workflow records its state

    assert not await operations.close_ended_loop_run(workflow_id, status, updated_at)
    with operations.sessions() as session:
        assert session.scalar(select(Run.status).where(Run.workflow_id == loop)) == "RUNNING"


def test_an_operation_holds_its_campaign_until_its_loop_ends(tmp_path) -> None:
    operation = OperationRequest(status="STARTED", workflow_id="backlog-loop-x")

    class _Session:
        def __init__(self, status: str | None) -> None:
            self.status = status

        def scalar(self, _statement: object) -> str | None:
            return self.status

    assert operation_holds_campaign(_Session(None), operation)  # type: ignore[arg-type]
    assert operation_holds_campaign(_Session("PAUSED"), operation)  # type: ignore[arg-type]
    assert not operation_holds_campaign(_Session("COMPLETE"), operation)  # type: ignore[arg-type]
    assert operation_holds_campaign(
        _Session(None),  # type: ignore[arg-type]
        OperationRequest(status="DISPATCHING"),
    )
    assert not operation_holds_campaign(
        _Session("PAUSED"),  # type: ignore[arg-type]
        OperationRequest(status="BLOCKED", workflow_id="backlog-loop-x"),
    )


_TEMPORAL_CLI = shutil.which("temporal")
_ISSUE_REF = "gntik-ai/musematic#1026"


@activity.defn(name="execute_control_step")
async def _failed_registration(request: dict[str, Any]) -> dict[str, Any]:
    step = request["step"]
    if step == "backlog.register_child_run":
        raise ApplicationError(
            "direct child campaign approval differs from its operation",
            type="ValueError",
            non_retryable=True,
        )
    if step == "github.refresh_mirrors":
        return {"status": "PASS"}
    script = _full_delivery_loop_script()
    script["backlog.record_skip"] = {
        "status": "SKIPPED",
        "repository": "gntik-ai/musematic",
        "issue_number": 1026,
        "reason_code": "BLOCKED_BY_DEPENDENCY",
    }
    return _registered_child(step, request, script[step])


async def _skip_failed_step(workflows: list[type]) -> tuple[Any, Any]:
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=_TEMPORAL_CLI) as env:
        async with (
            Worker(
                env.client,
                task_queue=CONTROL_TASK_QUEUE,
                workflows=workflows,
                activities=[_failed_registration],
            ),
            Worker(
                env.client,
                task_queue=GITHUB_TASK_QUEUE,
                workflows=workflows,
                activities=[_failed_registration],
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
                id=f"replay-test-new162-{uuid.uuid4()}",
                task_queue=CONTROL_TASK_QUEUE,
                execution_timeout=timedelta(minutes=2),
            )
            for _ in range(200):
                state = await handle.query("status")
                if str(state.get("phase", "")).startswith("AWAITING_OPERATOR"):
                    break
                await asyncio.sleep(0.1)
            await handle.signal("skip", f"{_ISSUE_REF}|BLOCKED_BY_DEPENDENCY")
            try:
                result = await handle.result()
            except WorkflowFailureError:
                result = None
            history = await handle.fetch_history()
    return history, result


def _loop_states(history: Any) -> list[tuple[str, str | None]]:
    return [
        (request["payload"]["loop_status"], request["payload"].get("blocker"))
        for request in _scheduled_steps(history)
        if request["step"] == "backlog.record_loop_state"
    ]


@pytest.mark.skipif(_TEMPORAL_CLI is None, reason="Temporal CLI dev server unavailable")
async def test_skipping_a_failed_loop_step_completes_the_loop_and_replays() -> None:
    history, result = await _skip_failed_step(_LOOP_WORKFLOWS)

    assert result is not None and result["status"] == "COMPLETE"
    assert result["completion"] == "COMPLETE_NOTHING_DELIVERED"
    assert _loop_states(history) == [
        ("PAUSED", "LOOP_STEP_FAILED_BACKLOG_REGISTER_CHILD_RUN"),
        ("COMPLETE", None),
    ]
    steps = [request["step"] for request in _scheduled_steps(history)]
    assert steps.count("backlog.record_skip") == 1
    await Replayer(workflows=_LOOP_WORKFLOWS).replay_workflow(history)


@workflow.defn(name="ExistingGitHubBacklogLoopWorkflow", sandboxed=False)
class _PreNew162LoopWorkflow(ExistingGitHubBacklogLoopWorkflow):
    """The loop exactly as it ran before NEW-162 (production #978's shape)."""

    @workflow.run
    async def run(self, request: ExistingGitHubBacklogLoopInput) -> dict[str, Any]:
        return await super().run(request)

    def _loop_step_skip_closes_patched(self) -> bool:
        return False


@pytest.mark.skipif(_TEMPORAL_CLI is None, reason="Temporal CLI dev server unavailable")
async def test_a_pre_patch_failed_skip_history_replays_on_the_current_loop() -> None:
    workflows = [_PreNew162LoopWorkflow, *_LOOP_WORKFLOWS[1:]]
    history, result = await _skip_failed_step(workflows)

    # Before NEW-162 the skip re-raised the step error: the loop (and campaign) FAILED.
    assert result is None
    assert _loop_states(history) == [
        ("PAUSED", "LOOP_STEP_FAILED_BACKLOG_REGISTER_CHILD_RUN"),
        ("FAILED", None),
    ]
    await Replayer(workflows=_LOOP_WORKFLOWS).replay_workflow(history)
