from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_direct_delivery_approval import approved_body, client_for
from test_issue_delivery_approval import _child, _dispose

from engineering_control.api.service import TemporalControlService
from engineering_control.db.models import (
    BacklogIssueRecord,
    DashboardEvent,
    NotificationOutbox,
    OperationRequest,
)
from engineering_control.operations.issue_start import ISSUE_START_UNCONFIRMED, issue_workflow_id
from engineering_control.operations.models import parse_command
from engineering_control.operations.service import TypedCommandBus, operation_dispatch_loop


@pytest.mark.asyncio
async def test_lost_issue_start_reply_preserves_one_active_owner(tmp_path):
    values = await _child(tmp_path)
    try:
        operations, metadata = values[:2]
        request = approved_body(values).approved_issue
        client, expected = client_for(request)
        accepted_by_recorder = []

        async def accept_then_lose(*args, **kwargs):
            accepted_by_recorder.append(kwargs["id"])
            raise TimeoutError("inert start acknowledgement lost")

        client.start_workflow.side_effect = accept_then_lose
        preparation = Mock()
        preparation.prepare_backlog_approval = AsyncMock(
            return_value={"approval_binding": "f" * 64}
        )
        preparation.approve_backlog_campaign = AsyncMock()
        with operations.sessions() as session:
            row = session.get(OperationRequest, request.issue_operation_id)
            command = parse_command(row.payload_json)
        control = TemporalControlService(client, "isolated-recorder-only", metadata)
        bus = TypedCommandBus(control, preparation, operations)
        await bus.dispatch_claim(
            (request.issue_operation_id, command, "cli", "engineering-ctl:hermes")
        )
        first = await operations.operation(request.issue_operation_id)
        assert accepted_by_recorder == [expected]
        assert first.status == "DISPATCHING"
        assert first.workflow_id is None and first.safe_error is not None
        second = await operations.create_external(
            command,
            source_channel="telegram",
            operator_class="isolated-fixture",
            idempotency_key=uuid4().hex,
        )
        assert second.operation_id == first.operation_id
        with operations.sessions() as session:
            owners = session.scalars(
                select(OperationRequest).where(
                    OperationRequest.target_id == first.target_id,
                    OperationRequest.status.in_(["PENDING", "DISPATCHING", "STARTED", "RUNNING"]),
                )
            ).all()
            assert len(owners) == 1
        assert client.start_workflow.await_count == 1
        # Observation is not new authority: even an edited issue must not hide
        # an already-attempted execution or cause a replacement to be started.
        with operations.sessions.begin() as session:
            session.scalar(select(BacklogIssueRecord)).body_sha256 = "9" * 64
        await bus.recover_issue_start_once()
        recovered = await operations.operation(first.operation_id)
        assert recovered.status == "STARTED" and recovered.workflow_id == expected
        assert client.start_workflow.await_count == 1
        client.get_workflow_handle.return_value.describe.assert_awaited_once()
        with operations.sessions() as session:
            notices = session.scalars(select(NotificationOutbox)).all()
            assert len(notices) == 1 and notices[0].payload_json["phase"] == "LINKED"
            events = session.scalars(
                select(DashboardEvent).where(DashboardEvent.entity_id == str(first.operation_id))
            ).all()
            assert {row.event_type for row in events} == {
                "operation.start_unconfirmed",
                "operation.linked",
            }
    finally:
        _dispose(values)


@pytest.mark.asyncio
async def test_dispatcher_recovers_from_persistence_failure_without_restarting_process():
    stop = asyncio.Event()
    bus = Mock()
    calls = []

    async def dispatch():
        calls.append(len(calls))
        if len(calls) == 1:
            raise ConnectionError("inert receipt database failure")
        stop.set()
        return False

    bus.dispatch_pending = AsyncMock(side_effect=dispatch)
    await asyncio.wait_for(operation_dispatch_loop(bus, stop), timeout=3)
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "condition", ["missing", "timeout", "workflow_id", "workflow_type", "run_id"]
)
async def test_unverifiable_history_never_restarts_or_releases_issue(tmp_path, condition):
    values = await _child(tmp_path)
    try:
        operations, metadata = values[:2]
        request = approved_body(values).approved_issue
        client, expected = client_for(request)
        describe = client.get_workflow_handle.return_value.describe
        if condition in {"missing", "timeout"}:
            describe.side_effect = (
                LookupError("inert absent history")
                if condition == "missing"
                else TimeoutError("inert unavailable Temporal")
            )
        elif condition == "workflow_id":
            describe.return_value.id = "different-workflow"
        elif condition == "workflow_type":
            describe.return_value.workflow_type = "UnrelatedWorkflow"
        else:
            describe.return_value.run_id = ""
        await operations.mark_issue_start_unconfirmed(request.issue_operation_id, expected)
        bus = TypedCommandBus(
            TemporalControlService(client, "fixture-only", metadata), metadata, operations
        )
        await bus.recover_issue_start_once()
        current = await operations.operation(request.issue_operation_id)
        assert current.status == "DISPATCHING" and current.workflow_id is None
        assert current.safe_error == ISSUE_START_UNCONFIRMED
        client.start_workflow.assert_not_awaited()
        with operations.sessions() as session:
            assert not session.scalars(select(NotificationOutbox)).all()
            assert (
                len(
                    session.scalars(
                        select(DashboardEvent).where(
                            DashboardEvent.entity_id == str(request.issue_operation_id)
                        )
                    ).all()
                )
                == 1
            )
    finally:
        _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("late_status", ["COMPLETED", "FAILED", "BLOCKED"])
async def test_late_terminal_dispatch_cannot_erase_unconfirmed_ownership(tmp_path, late_status):
    values = await _child(tmp_path)
    try:
        operations = values[0]
        request = approved_body(values).approved_issue
        await operations.mark_issue_start_unconfirmed(
            request.issue_operation_id, issue_workflow_id(request)
        )
        assert await operations.finish(request.issue_operation_id, status=late_status) is False
        current = await operations.operation(request.issue_operation_id)
        assert current.status == "DISPATCHING" and current.safe_error == ISSUE_START_UNCONFIRMED
    finally:
        _dispose(values)


@pytest.mark.asyncio
async def test_recovery_after_process_exit_before_uncertainty_marker_is_read_only(tmp_path):
    values = await _child(tmp_path)
    try:
        operations, metadata = values[:2]
        request = approved_body(values).approved_issue
        client, expected = client_for(request)
        assert await operations.next_issue_start_recovery(None) is None
        with operations.sessions.begin() as session:
            row = session.get(OperationRequest, request.issue_operation_id)
            row.started_at = datetime.now(UTC) - timedelta(minutes=2)
        bus = TypedCommandBus(
            TemporalControlService(client, "fixture-only", metadata), metadata, operations
        )
        await bus.recover_issue_start_once()
        current = await operations.operation(request.issue_operation_id)
        assert current.status == "STARTED" and current.workflow_id == expected
        client.start_workflow.assert_not_awaited()
    finally:
        _dispose(values)


@pytest.mark.asyncio
async def test_changed_attempt_binding_during_observation_is_not_recorded(tmp_path):
    values = await _child(tmp_path)
    try:
        operations, metadata = values[:2]
        request = approved_body(values).approved_issue
        client, expected = client_for(request)
        await operations.mark_issue_start_unconfirmed(request.issue_operation_id, expected)
        description = client.get_workflow_handle.return_value.describe.return_value

        async def describe_then_change(**kwargs):
            with operations.sessions.begin() as session:
                session.get(OperationRequest, request.issue_operation_id).binding_hash = "8" * 64
            return description

        client.get_workflow_handle.return_value.describe.side_effect = describe_then_change
        bus = TypedCommandBus(
            TemporalControlService(client, "fixture-only", metadata), metadata, operations
        )
        with pytest.raises(ValueError, match="stored issue attempt changed"):
            await bus.recover_issue_start_once()
        current = await operations.operation(request.issue_operation_id)
        assert current.status == "DISPATCHING" and current.workflow_id is None
        with operations.sessions() as session:
            assert not session.scalars(select(NotificationOutbox)).all()
    finally:
        _dispose(values)


@pytest.mark.asyncio
async def test_recovery_iteration_is_rate_bounded_and_dispatch_cancellation_propagates():
    operations = Mock()
    operations.next_issue_start_recovery = AsyncMock(return_value=None)
    operations.claim_pending = AsyncMock(return_value=None)
    bus = TypedCommandBus(Mock(), Mock(), operations)
    for _ in range(10):
        assert await bus.dispatch_pending() is False
    operations.next_issue_start_recovery.assert_awaited_once()
    bus.dispatch_pending = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await operation_dispatch_loop(bus, asyncio.Event())


def test_direct_api_reports_unconfirmed_start_without_internal_error(tmp_path):
    from fastapi.testclient import TestClient

    from engineering_control.api.main import create_app
    from engineering_control.config import Settings
    from engineering_control.operations.issue_start import IssueStartUnconfirmedError

    values = asyncio.run(_child(tmp_path))
    try:
        operations, metadata = values[:2]
        body = approved_body(values)
        control = Mock()
        control.start_delivery = AsyncMock(
            side_effect=IssueStartUnconfirmedError(body.approved_issue)
        )
        with TestClient(
            create_app(
                settings=Settings(_env_file=None, database_url=str(operations.engine.url)),
                service=control,
                repository=metadata,
                operations_repository=operations,
                token="fixture-only-token",  # noqa: S106 - disposable isolated authentication fixture
            )
        ) as client:
            response = client.post(
                "/v1/deliveries",
                json=body.model_dump(mode="json"),
                headers={"Authorization": "Bearer fixture-only-token"},
            )
        assert response.status_code == 503
        assert response.json()["status"] == "START_UNCONFIRMED"
        assert response.json()["operation_id"] == str(body.approved_issue.issue_operation_id)
        assert "Traceback" not in response.text
    finally:
        _dispose(values)
