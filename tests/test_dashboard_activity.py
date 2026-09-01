from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engineering_control.dashboard.events import BestEffortEventRecorder, sse_events
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.dashboard.schemas import ActivityEventInput
from engineering_control.db.base import Base


def _repository(tmp_path: Path) -> DashboardRepository:
    repository = DashboardRepository(f"sqlite+pysqlite:///{tmp_path / 'events.db'}")
    Base.metadata.create_all(repository.engine)
    return repository


@pytest.mark.asyncio
async def test_event_insertion_ordering_redaction_allowlist_and_size(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first = await repository.record_event(
        ActivityEventInput(
            level="INFO",
            event_type="workflow.started",
            source="controller",
            workflow_id="workflow-1",
            message="Bearer abcdefghijklmnopqrstuvwxyz123456",
            metadata={
                "status": "RUNNING",
                "token": "must-not-persist",
                "unexpected": "ignored",
                "step": "x" * 10_000,
            },
        )
    )
    second = await repository.record_event(
        ActivityEventInput(
            level="ERROR",
            event_type="workflow.failed",
            source="worker",
            worker_kind="scan",
            message="Bounded failure",
        )
    )
    events = await repository.recent_events(limit=500)
    assert [event["id"] for event in events] == [first, second]
    assert events[0]["message"] == "Bearer [REDACTED]"
    assert "token" not in events[0]["metadata"]
    assert "unexpected" not in events[0]["metadata"]
    assert len(events[0]["metadata"]["step"]) == 240
    assert events[1]["source"] == "worker"


@pytest.mark.asyncio
async def test_sse_resume_heartbeat_disconnect_and_retention(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    old_id = await repository.record_event(
        ActivityEventInput(
            level="INFO",
            event_type="workflow.started",
            source="controller",
            message="Old",
            occurred_at=datetime.now(UTC) - timedelta(days=20),
        )
    )
    new_id = await repository.record_event(
        ActivityEventInput(
            level="INFO",
            event_type="workflow.completed",
            source="worker",
            message="New",
        )
    )

    async def connected() -> bool:
        return False

    async def authorized() -> bool:
        return True

    chunks = [
        chunk
        async for chunk in sse_events(
            repository,
            after_id=old_id,
            disconnected=connected,
            authorized=authorized,
            poll_seconds=0,
            heartbeat_seconds=0,
            maximum_iterations=2,
        )
    ]
    assert any(f"id: {new_id}" in chunk for chunk in chunks)
    assert any("heartbeat" in chunk for chunk in chunks)
    assert await repository.delete_expired_events(retention_days=14) == 1
    assert [event["id"] for event in await repository.recent_events(limit=10)] == [new_id]

    async def disconnected() -> bool:
        return True

    assert [
        chunk
        async for chunk in sse_events(
            repository,
            after_id=0,
            disconnected=disconnected,
            authorized=authorized,
            maximum_iterations=1,
        )
    ] == []


@pytest.mark.asyncio
async def test_sse_stops_when_session_authorization_is_revoked(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    async def connected() -> bool:
        return False

    async def revoked() -> bool:
        return False

    assert [
        chunk
        async for chunk in sse_events(
            repository,
            after_id=0,
            disconnected=connected,
            authorized=revoked,
            poll_seconds=0,
            heartbeat_seconds=0,
            authorization_seconds=0,
            maximum_iterations=2,
        )
    ] == []


@pytest.mark.asyncio
async def test_sse_rechecks_authorization_and_stops_within_the_bound(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    authorization_calls = 0

    async def connected() -> bool:
        return False

    async def authorized_then_revoked() -> bool:
        nonlocal authorization_calls
        authorization_calls += 1
        return authorization_calls == 1

    chunks = [
        chunk
        async for chunk in sse_events(
            repository,
            after_id=0,
            disconnected=connected,
            authorized=authorized_then_revoked,
            poll_seconds=0.001,
            heartbeat_seconds=0,
            authorization_seconds=0.005,
            maximum_iterations=8,
        )
    ]
    assert authorization_calls == 2
    assert len(chunks) <= 5
    assert all("heartbeat" in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_event_persistence_failure_is_best_effort() -> None:
    class FailingRepository:
        async def record_event(self, event: ActivityEventInput) -> int:
            raise RuntimeError("database unavailable")

    recorder = BestEffortEventRecorder(FailingRepository())  # type: ignore[arg-type]
    await recorder.record(
        ActivityEventInput(
            level="INFO",
            event_type="workflow.started",
            source="controller",
            message="Safe",
        )
    )
