from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
import test_operation_dispatch_receipt as receipt_fixtures
from sqlalchemy import event, select, text
from test_direct_delivery_approval import approved_body, client_for
from test_issue_delivery_approval import _child, _dispose
from test_operation_dispatch_receipt import (
    RUN,
    WORKFLOW,
    seed,
    snapshot,
)

from engineering_control.api.schemas import OperationAccepted
from engineering_control.db.models import DashboardEvent, NotificationOutbox

# Register the shared explicit SQLite/PostgreSQL fixture in this test module.
repository = receipt_fixtures.repository


def projections(repository, operation_id):
    with repository.sessions() as session:
        events = session.scalars(
            select(DashboardEvent).where(
                DashboardEvent.entity_type == "operation",
                DashboardEvent.entity_id == str(operation_id),
            )
        ).all()
        notifications = session.scalars(
            select(NotificationOutbox).where(
                NotificationOutbox.payload_json["operation_id"].as_string() == str(operation_id)
            )
        ).all()
        return {
            "events": [
                (row.id, row.event_type, row.current_phase, row.workflow_id, row.temporal_run_id)
                for row in events
            ],
            "notifications": [
                (str(row.id), row.topic, row.status, row.payload_json) for row in notifications
            ],
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["STARTED", "COMPLETED", "FAILED", "BLOCKED"])
async def test_dispatch_receipt_already_has_event_and_intent_when_it_returns(repository, status):
    operation_id = seed(repository)
    arguments = {"workflow_id": WORKFLOW, "run_id": RUN} if status == "STARTED" else {}
    assert await repository.finish(operation_id, status=status, **arguments) is True
    stored = projections(repository, operation_id)
    assert len(stored["events"]) == 1
    assert stored["events"][0][1:3] == (f"operation.{status.lower()}", status)
    assert len(stored["notifications"]) == (1 if status == "STARTED" else 0)
    if status == "STARTED":
        notice = stored["notifications"][0]
        assert notice[1:3] == ("telegram.engineering-operation-started", "PENDING")
        assert notice[3]["workflow_id"] == WORKFLOW
        assert notice[3]["phase"] == "QUEUED"


@pytest.mark.asyncio
async def test_first_existing_workflow_receipt_has_truthful_linked_notification(repository):
    operation_id = seed(repository)
    assert (
        await repository.finish(
            operation_id, status="STARTED", workflow_id=WORKFLOW, run_id=RUN, linked=True
        )
        is True
    )
    stored = projections(repository, operation_id)
    assert len(stored["events"]) == len(stored["notifications"]) == 1
    assert stored["events"][0][1] == "operation.linked"
    assert stored["notifications"][0][3]["phase"] == "LINKED"
    assert stored["notifications"][0][3]["event_type"] == "operation.linked"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["event", "outbox"])
async def test_projection_failure_rolls_back_receipt_then_retry_commits_once(
    repository, failure_kind
):
    operation_id = seed(repository)
    before = snapshot(repository, operation_id)
    fired = []

    def fail_projection(session, context, instances):
        for row in session.new:
            matches = (
                failure_kind == "event"
                and isinstance(row, DashboardEvent)
                and row.entity_id == str(operation_id)
            ) or (
                failure_kind == "outbox"
                and isinstance(row, NotificationOutbox)
                and row.payload_json.get("operation_id") == str(operation_id)
            )
            if matches:
                fired.append(failure_kind)
                raise RuntimeError("inert projection persistence failure")

    event.listen(repository.sessions.class_, "before_flush", fail_projection)
    try:
        with pytest.raises(RuntimeError, match="inert projection"):
            await repository.finish(
                operation_id, status="STARTED", workflow_id=WORKFLOW, run_id=RUN
            )
    finally:
        event.remove(repository.sessions.class_, "before_flush", fail_projection)
    assert fired == [failure_kind]
    assert snapshot(repository, operation_id) == before
    assert projections(repository, operation_id) == {"events": [], "notifications": []}
    assert (
        await repository.finish(operation_id, status="STARTED", workflow_id=WORKFLOW, run_id=RUN)
        is True
    )
    stored = projections(repository, operation_id)
    assert len(stored["events"]) == len(stored["notifications"]) == 1


@pytest.mark.asyncio
async def test_lost_reply_after_commit_does_not_duplicate_notification_intent(
    repository, monkeypatch
):
    operation_id = seed(repository)
    original = repository._finish
    lose_first_reply = True

    def lost_reply(*args, **kwargs):
        nonlocal lose_first_reply
        applied = original(*args, **kwargs)
        if lose_first_reply:
            lose_first_reply = False
            raise ConnectionError("inert application acknowledgement lost after commit")
        return applied

    monkeypatch.setattr(repository, "_finish", lost_reply)
    with pytest.raises(ConnectionError, match="after commit"):
        await repository.finish(operation_id, status="STARTED", workflow_id=WORKFLOW, run_id=RUN)
    before = snapshot(repository, operation_id)
    stored = projections(repository, operation_id)
    assert len(stored["events"]) == len(stored["notifications"]) == 1
    assert (
        await repository.finish(operation_id, status="STARTED", workflow_id=WORKFLOW, run_id=RUN)
        is False
    )
    assert snapshot(repository, operation_id) == before
    assert projections(repository, operation_id) == stored


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["dashboard_events", "notification_outbox"])
async def test_failure_after_actual_projection_insert_rolls_back_the_transaction(repository, table):
    operation_id = seed(repository)
    before = snapshot(repository, operation_id)
    inserted = []

    def lose_after_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith(f"INSERT INTO {table} "):
            inserted.append(table)
            raise RuntimeError("inert failure after actual projection INSERT")

    event.listen(repository.engine, "after_cursor_execute", lose_after_insert)
    try:
        with pytest.raises(RuntimeError, match="after actual projection INSERT"):
            await repository.finish(
                operation_id, status="STARTED", workflow_id=WORKFLOW, run_id=RUN
            )
    finally:
        event.remove(repository.engine, "after_cursor_execute", lose_after_insert)
    assert inserted == [table]
    assert snapshot(repository, operation_id) == before
    assert projections(repository, operation_id) == {"events": [], "notifications": []}
    assert (
        await repository.finish(operation_id, status="STARTED", workflow_id=WORKFLOW, run_id=RUN)
        is True
    )
    stored = projections(repository, operation_id)
    assert len(stored["events"]) == len(stored["notifications"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted_status", ["ACCEPTED", "EXISTING"])
async def test_approved_direct_writer_commits_intent_before_bus_observes_receipt(
    tmp_path: Path, accepted_status
):
    values = await _child(tmp_path)
    try:
        operations, metadata = values[:2]
        body = approved_body(values)
        _, expected = client_for(body.approved_issue)
        operation_id = body.approved_issue.issue_operation_id
        accepted = OperationAccepted(workflow_id=expected, run_id=RUN, status=accepted_status)
        await metadata.record_delivery_issue_start(body, accepted)
        stored = projections(operations, operation_id)
        assert len(stored["events"]) == len(stored["notifications"]) == 1
        assert stored["events"][0][1] == (
            "operation.linked" if accepted_status == "EXISTING" else "operation.started"
        )
        assert (
            await operations.finish(
                operation_id, status="STARTED", workflow_id=expected, run_id=RUN
            )
            is False
        )
        await metadata.record_delivery_issue_start(body, accepted)
        assert projections(operations, operation_id) == stored
    finally:
        _dispose(values)


@pytest.mark.asyncio
async def test_direct_writer_projection_failure_is_not_an_acknowledged_start(tmp_path: Path):
    values = await _child(tmp_path)
    try:
        operations, metadata = values[:2]
        body = approved_body(values)
        _, expected = client_for(body.approved_issue)
        operation_id = body.approved_issue.issue_operation_id
        before = snapshot(operations, operation_id)
        fired = []

        def fail_projection(session, context, instances):
            if any(
                isinstance(row, NotificationOutbox)
                and row.payload_json.get("operation_id") == str(operation_id)
                for row in session.new
            ):
                fired.append(True)
                raise RuntimeError("inert direct outbox failure")

        event.listen(metadata.sessions.class_, "before_flush", fail_projection)
        try:
            with pytest.raises(RuntimeError, match="inert direct"):
                await metadata.record_delivery_issue_start(
                    body, OperationAccepted(workflow_id=expected, run_id=RUN)
                )
        finally:
            event.remove(metadata.sessions.class_, "before_flush", fail_projection)
        assert fired == [True]
        assert snapshot(operations, operation_id) == before
        assert projections(operations, operation_id) == {"events": [], "notifications": []}
    finally:
        _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("repository", ["postgresql"], indirect=True)
async def test_two_postgresql_first_writers_have_one_receipt_event_and_intent(repository):
    from engineering_control.db.models import OperationRequest

    operation_id = seed(repository)
    blocker = repository.sessions()
    tasks = []
    try:
        blocker.begin()
        blocker.get(OperationRequest, operation_id, with_for_update=True)
        owner_pid = blocker.scalar(text("SELECT pg_backend_pid()"))
        tasks = [
            asyncio.create_task(
                repository.finish(operation_id, status="STARTED", workflow_id=WORKFLOW, run_id=RUN)
            )
            for _ in range(2)
        ]
        deadline = time.monotonic() + 5
        waiters = 0
        while time.monotonic() < deadline:
            with repository.engine.connect() as observer:
                # Include transitive waiting: the second SELECT may wait for the
                # first lock waiter rather than directly for the lock holder.
                waiters = observer.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND wait_event_type='Lock' AND ("
                        ":owner = ANY(pg_blocking_pids(pid)) OR EXISTS ("
                        "SELECT 1 FROM unnest(pg_blocking_pids(pid)) AS blocker "
                        "WHERE :owner = ANY(pg_blocking_pids(blocker))))"
                    ),
                    {"owner": owner_pid},
                )
            if waiters >= 2:
                break
            await asyncio.sleep(0.02)
        assert owner_pid is not None and waiters >= 2
        blocker.commit()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert sorted(outcomes) == [False, True]
        stored = projections(repository, operation_id)
        assert len(stored["events"]) == len(stored["notifications"]) == 1
    finally:
        blocker.rollback()
        blocker.close()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
