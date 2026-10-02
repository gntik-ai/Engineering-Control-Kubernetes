from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from test_atomic_dispatch_receipts import projections
from test_direct_delivery_approval import RUN_ID, approved_body, client_for
from test_issue_delivery_approval import _child, _dispose

from engineering_control.api.main import create_app
from engineering_control.api.service import TemporalControlService
from engineering_control.config import Settings
from engineering_control.db.models import BacklogIssueRecord, OperationRequest
from engineering_control.operations.models import parse_command
from engineering_control.operations.service import TypedCommandBus


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["outbox_insert", "committed_reply", "post_ack_revision"])
async def test_direct_receipt_failure_reports_unknown_start_and_recovers_once(tmp_path, failure):
    values = await _child(tmp_path)
    operations, metadata = values[:2]
    body = approved_body(values)
    request = body.approved_issue
    assert request is not None
    temporal, expected = client_for(request)
    fired = []
    original = metadata.record_delivery_issue_start

    def after_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("INSERT INTO notification_outbox "):
            fired.append("actual_outbox_insert")
            raise RuntimeError("inert receipt insert failure")

    async def commit_then_lose(*args, **kwargs):
        await original(*args, **kwargs)
        fired.append("committed_receipt")
        raise ConnectionError("inert receipt acknowledgement lost")

    async def acknowledge_then_change(*args, **kwargs):
        with operations.sessions.begin() as session:
            session.scalar(select(BacklogIssueRecord)).body_sha256 = "9" * 64
        fired.append("inert_start_then_revision_change")
        return SimpleNamespace(first_execution_run_id=RUN_ID)

    if failure == "outbox_insert":
        event.listen(metadata.engine, "after_cursor_execute", after_insert)
    elif failure == "committed_reply":
        metadata.record_delivery_issue_start = commit_then_lose
    else:
        temporal.start_workflow.side_effect = acknowledge_then_change
    try:
        service = TemporalControlService(temporal, "fixture-recorder-only", metadata)
        idle = Mock()
        idle.dispatch_pending = AsyncMock(return_value=False)
        app = create_app(
            settings=Settings(_env_file=None, database_url=str(operations.engine.url)),
            service=service,
            repository=metadata,
            token="x" * 32,
            operations_repository=operations,
            command_bus=idle,
        )
        with TestClient(app, raise_server_exceptions=False) as http:
            response = http.post(
                "/v1/deliveries",
                json=body.model_dump(mode="json"),
                headers={"Authorization": "Bearer " + "x" * 32},
            )
        assert response.status_code == 503
        result = response.json()
        assert result["status"] == "START_UNCONFIRMED"
        assert result["operation_id"] == str(request.issue_operation_id)
        assert result["expected_workflow_id"] == expected
        assert "inert" not in response.text and "traceback" not in response.text.lower()
        assert temporal.start_workflow.await_count == 1 and len(fired) == 1
    except BaseException:
        _dispose(values)
        raise
    finally:
        if failure == "outbox_insert":
            event.remove(metadata.engine, "after_cursor_execute", after_insert)
        metadata.record_delivery_issue_start = original
    try:
        before = await operations.operation(request.issue_operation_id)
        assert before.status == ("STARTED" if failure == "committed_reply" else "DISPATCHING")
        stored = projections(operations, request.issue_operation_id)
        assert (
            len(stored["events"])
            == len(stored["notifications"])
            == (1 if failure == "committed_reply" else 0)
        )
        with operations.sessions.begin() as session:
            row = session.get(OperationRequest, request.issue_operation_id)
            command = parse_command(row.payload_json)
            # Eligibility is measured from the persisted dispatch start, not
            # an arbitrary Python attribute or a recent projection update.
            row.started_at = datetime.now(UTC) - timedelta(minutes=2)
        duplicate = await operations.create_external(
            command,
            source_channel="telegram",
            operator_class="isolated-fixture",
            idempotency_key=uuid4().hex,
        )
        assert duplicate.operation_id == request.issue_operation_id
        bus = TypedCommandBus(service, metadata, operations)
        await bus.recover_issue_start_once()
        recovered = await operations.operation(request.issue_operation_id)
        assert recovered.status == "STARTED" and recovered.workflow_id == expected
        assert recovered.temporal_run_id == RUN_ID
        assert temporal.start_workflow.await_count == 1
        assert temporal.get_workflow_handle.return_value.describe.await_count == (
            0 if failure == "committed_reply" else 1
        )
        final = projections(operations, request.issue_operation_id)
        assert len(final["events"]) == len(final["notifications"]) == 1
        if failure == "committed_reply":
            assert final == stored
        else:
            assert final["events"][0][1] == "operation.linked"
        await bus.recover_issue_start_once()
        assert projections(operations, request.issue_operation_id) == final
    finally:
        _dispose(values)


@pytest.mark.asyncio
async def test_receipt_cancellation_propagates_without_second_start(tmp_path):
    values = await _child(tmp_path)
    try:
        metadata = values[1]
        body = approved_body(values)
        temporal, _ = client_for(body.approved_issue)
        metadata.record_delivery_issue_start = AsyncMock(side_effect=asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError):
            await TemporalControlService(
                temporal, "fixture-recorder-only", metadata
            ).start_delivery(body)
        assert temporal.start_workflow.await_count == 1
    finally:
        _dispose(values)
