from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text

from engineering_control.api.schemas import OperationAccepted
from engineering_control.db.base import Base
from engineering_control.db.models import DashboardEvent, OperationRequest
from engineering_control.operations.models import RefreshConnectionsCommand
from engineering_control.operations.repository import OperationsRepository
from engineering_control.operations.service import TypedCommandBus

WORKFLOW = "dispatch-receipt-fixture"
RUN = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(params=["sqlite", "postgresql"])
def repository(request, tmp_path: Path):
    if request.param == "postgresql":
        url = os.environ.get("ENGINEERING_TEST_POSTGRES_URL")
        if url is None:
            pytest.skip("explicit isolated PostgreSQL fixture is not supplied")
        assert url == ("postgresql+psycopg://engineering_ci@127.0.0.1:5432/engineering_control_ci")
    else:
        url = f"sqlite+pysqlite:///{tmp_path / 'dispatch.db'}"
    value = OperationsRepository(url)
    value.fixture_owned_operation_ids = []
    Base.metadata.create_all(value.engine)
    try:
        yield value
    finally:
        with value.sessions.begin() as session:
            session.execute(
                delete(OperationRequest).where(
                    OperationRequest.id.in_(value.fixture_owned_operation_ids)
                )
            )
        value.engine.dispose()


def seed(repository, status="DISPATCHING", *, linked=False):
    now = datetime.now(UTC) - timedelta(seconds=10)
    operation_id = uuid4()
    with repository.sessions.begin() as session:
        session.add(
            OperationRequest(
                id=operation_id,
                action="refresh_connections",
                project=None,
                target_type="connections",
                target_id="all",
                payload_json=RefreshConnectionsCommand(scope="all").model_dump(mode="json"),
                source_channel="cli",
                operator_class="isolated-fixture",
                idempotency_key=uuid4().hex,
                binding_hash="a" * 64,
                workflow_id=WORKFLOW if linked else None,
                temporal_run_id=RUN if linked else None,
                status=status,
                safe_error="preserved safe error" if status == "FAILED" else None,
                created_at=now,
                confirmed_at=now,
                started_at=now if status != "PENDING" else None,
                completed_at=(
                    now if status in {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED"} else None
                ),
            )
        )
    repository.fixture_owned_operation_ids.append(operation_id)
    return operation_id


def snapshot(repository, operation_id):
    with repository.sessions() as session:
        row = session.get(OperationRequest, operation_id)
        assert row is not None
        return {
            field: getattr(row, field)
            for field in (
                "status",
                "workflow_id",
                "temporal_run_id",
                "safe_error",
                "created_at",
                "confirmed_at",
                "started_at",
                "completed_at",
            )
        }


def receipt_arguments(status):
    return {"workflow_id": WORKFLOW, "run_id": RUN} if status == "STARTED" else {}


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["COMPLETED", "FAILED", "BLOCKED", "CANCELLED"])
@pytest.mark.parametrize("incoming", ["STARTED", "COMPLETED", "FAILED", "BLOCKED"])
async def test_dispatch_cannot_reopen_or_rewrite_terminal_history(repository, prior, incoming):
    operation_id = seed(repository, prior, linked=True)
    before = snapshot(repository, operation_id)
    applied = await repository.finish(operation_id, status=incoming, **receipt_arguments(incoming))
    assert snapshot(repository, operation_id) == before
    assert applied is False


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["STARTED", "RUNNING"])
@pytest.mark.parametrize("incoming", ["STARTED", "COMPLETED", "FAILED", "BLOCKED"])
async def test_durable_receipt_survives_late_dispatch_result(repository, prior, incoming):
    operation_id = seed(repository, prior, linked=True)
    before = snapshot(repository, operation_id)
    applied = await repository.finish(operation_id, status=incoming, **receipt_arguments(incoming))
    assert snapshot(repository, operation_id) == before
    assert applied is False


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["PENDING", "DISPATCHING"])
@pytest.mark.parametrize("incoming", ["STARTED", "COMPLETED", "FAILED", "BLOCKED"])
async def test_first_dispatch_result_is_durably_recorded(repository, prior, incoming):
    operation_id = seed(repository, prior)
    applied = await repository.finish(operation_id, status=incoming, **receipt_arguments(incoming))
    after = snapshot(repository, operation_id)
    assert after["status"] == incoming
    assert (after["workflow_id"], after["temporal_run_id"]) == (
        (WORKFLOW, RUN) if incoming == "STARTED" else (None, None)
    )
    assert (after["completed_at"] is not None) == (incoming != "STARTED")
    assert applied is True


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["workflow_id", "run_id"])
async def test_delayed_receipt_cannot_replace_workflow_or_run_lineage(repository, changed):
    operation_id = seed(repository, "RUNNING", linked=True)
    before = snapshot(repository, operation_id)
    arguments = receipt_arguments("STARTED")
    arguments[changed] = "different-workflow-or-historical-run"
    assert await repository.finish(operation_id, status="STARTED", **arguments) is False
    assert snapshot(repository, operation_id) == before


@pytest.mark.asyncio
async def test_repeat_dispatch_completion_preserves_timestamp_and_error(repository):
    operation_id = seed(repository)
    assert (
        await repository.finish(operation_id, status="FAILED", safe_error="initial safe failure")
        is True
    )
    before = snapshot(repository, operation_id)
    assert (
        await repository.finish(operation_id, status="FAILED", safe_error="late replacement")
        is False
    )
    assert snapshot(repository, operation_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["workflow_id", "temporal_run_id", "completed_at"])
async def test_existing_receipt_fields_are_not_erased_by_inconsistent_status(repository, field):
    operation_id = seed(repository)
    with repository.sessions.begin() as session:
        row = session.get(OperationRequest, operation_id)
        setattr(row, field, datetime.now(UTC) if field == "completed_at" else "retained-id")
    before = snapshot(repository, operation_id)
    assert await repository.finish(operation_id, status="FAILED") is False
    assert snapshot(repository, operation_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"status": "STARTED"},
        {"status": "STARTED", "workflow_id": "x" * 241},
        {"status": "STARTED", "workflow_id": WORKFLOW, "run_id": "x" * 241},
        {"status": "COMPLETED", "workflow_id": WORKFLOW},
    ],
)
async def test_invalid_dispatch_receipt_does_not_change_state(repository, arguments):
    operation_id = seed(repository)
    before = snapshot(repository, operation_id)
    with pytest.raises(ValueError, match="dispatch|workflow receipt"):
        await repository.finish(operation_id, **arguments)
    assert snapshot(repository, operation_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["policy_error", "dispatch_error", "accepted", "completed"])
async def test_bus_records_first_outcome_and_one_matching_event(repository, outcome):
    operation_id = seed(repository, "PENDING")
    claim = await repository.claim(operation_id)
    assert claim is not None

    async def execute(*args, **kwargs):
        if outcome == "policy_error":
            raise ValueError("inert pre-start policy error")
        if outcome == "dispatch_error":
            raise RuntimeError("inert pre-start dispatch error")
        return (
            OperationAccepted(workflow_id=WORKFLOW, run_id=RUN) if outcome == "accepted" else None
        )

    bus = TypedCommandBus(Mock(), Mock(), repository)
    bus._execute = AsyncMock(side_effect=execute)
    await bus.dispatch_claim(claim)
    expected = {
        "policy_error": "BLOCKED",
        "dispatch_error": "FAILED",
        "accepted": "STARTED",
        "completed": "COMPLETED",
    }[outcome]
    assert snapshot(repository, operation_id)["status"] == expected
    with repository.sessions() as session:
        events = session.scalars(
            select(DashboardEvent).where(
                DashboardEvent.entity_type == "operation",
                DashboardEvent.entity_id == str(operation_id),
            )
        ).all()
        assert len(events) == 1
        assert events[0].event_type == f"operation.{expected.lower()}"
        assert events[0].current_phase == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["policy_error", "dispatch_error", "accepted", "completed"])
async def test_bus_does_not_publish_a_stale_dispatch_event(repository, outcome):
    operation_id = seed(repository, "PENDING")
    claim = await repository.claim(operation_id)
    assert claim is not None
    before = {}

    async def execute(*args, **kwargs):
        with repository.sessions.begin() as session:
            row = session.get(OperationRequest, operation_id)
            row.status = "RUNNING"
            row.workflow_id = WORKFLOW
            row.temporal_run_id = RUN
        before.update(snapshot(repository, operation_id))
        if outcome == "policy_error":
            raise ValueError("inert late policy error")
        if outcome == "dispatch_error":
            raise RuntimeError("inert late dispatch error")
        return (
            OperationAccepted(workflow_id=WORKFLOW, run_id=RUN) if outcome == "accepted" else None
        )

    bus = TypedCommandBus(Mock(), Mock(), repository)
    bus._execute = AsyncMock(side_effect=execute)
    await bus.dispatch_claim(claim)
    assert snapshot(repository, operation_id) == before
    with repository.sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(DashboardEvent)
                .where(
                    DashboardEvent.entity_type == "operation",
                    DashboardEvent.entity_id == str(operation_id),
                )
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("repository", ["postgresql"], indirect=True)
@pytest.mark.parametrize("incoming", ["STARTED", "FAILED"])
async def test_postgresql_finish_checks_state_after_waiting_for_row_lock(repository, incoming):
    if repository.engine.dialect.name != "postgresql":
        pytest.skip("row-lock race requires PostgreSQL")
    operation_id = seed(repository)
    writer = repository.sessions()
    task = None
    try:
        writer.begin()
        row = writer.get(OperationRequest, operation_id, with_for_update=True)
        writer_pid = writer.scalar(text("SELECT pg_backend_pid()"))
        row.status = "COMPLETED"
        row.workflow_id = WORKFLOW
        row.temporal_run_id = RUN
        row.completed_at = datetime.now(UTC)
        writer.flush()
        task = asyncio.create_task(
            repository.finish(operation_id, status=incoming, **receipt_arguments(incoming))
        )
        deadline = time.monotonic() + 5
        blocked = False
        while time.monotonic() < deadline:
            with repository.engine.connect() as observer:
                blocked = bool(
                    observer.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE :owner = ANY(pg_blocking_pids(pid)) "
                            "AND wait_event_type = 'Lock')"
                        ),
                        {"owner": writer_pid},
                    )
                )
            if blocked:
                break
            await asyncio.sleep(0.02)
        assert blocked, "the competing finish must actually wait for the owned row lock"
        writer.commit()
        before = snapshot(repository, operation_id)
        applied = await asyncio.wait_for(task, 5)
        assert snapshot(repository, operation_id) == before
        assert applied is False
    finally:
        writer.rollback()
        writer.close()
        if task is not None:
            await asyncio.wait_for(task, 5)
