"""Workflow projections must not imply a verified current Temporal-run outcome."""

from __future__ import annotations

import asyncio
import os
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from engineering_control.api.main import create_app
from engineering_control.api.service import TemporalControlService
from engineering_control.cli import dispatch
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import Run
from engineering_control.db.repository import EmptyMetadataRepository, SQLMetadataRepository
from engineering_control.workflow_outcome import (
    RunOutcomeRow,
    derive_workflow_application_projection,
)


def test_missing_run_returns_null_business_outcome_not_green() -> None:
    projection = derive_workflow_application_projection([])
    assert projection.run_association == "NONE"
    assert projection.business_status is None
    assert projection.error_code is None


def test_duplicate_rows_fail_closed_ambiguous_not_latest_wins() -> None:
    now = datetime.now(UTC)
    rows = [
        RunOutcomeRow(
            status="BLOCKED",
            error_code="OLD_ERR",
            started_at=now - timedelta(hours=2),
            finished_at=now - timedelta(hours=1),
            updated_at=now - timedelta(hours=1),
        ),
        RunOutcomeRow(
            status="SUCCESS",
            error_code=None,
            started_at=now - timedelta(minutes=5),
            finished_at=now,
            updated_at=now,
        ),
    ]
    projection = derive_workflow_application_projection(rows, now=now)
    assert projection.run_association == "AMBIGUOUS"
    assert projection.business_status is None
    assert projection.run_projection == "AMBIGUOUS"


def test_unsafe_status_and_error_code_strings_get_safe_fallbacks() -> None:
    now = datetime.now(UTC)
    row = RunOutcomeRow(
        status="not-a-real-status<script>alert(1)</script>",
        error_code="../../etc/passwd; DROP TABLE runs;--" * 5,
        started_at=now - timedelta(minutes=1),
        finished_at=now,
        updated_at=now,
    )
    projection = derive_workflow_application_projection([row], now=now)
    assert projection.business_status is None
    assert projection.persisted_status is None
    assert projection.error_code == "REDACTED_ERROR_CODE"


def test_terminal_blocked_row_is_not_stale_merely_because_old() -> None:
    old_now = datetime.now(UTC) - timedelta(days=400)
    row = RunOutcomeRow(
        status="BLOCKED",
        error_code="CLEANUP_IDENTITY_INSPECTION_BLOCKED",
        started_at=old_now - timedelta(minutes=10),
        finished_at=old_now,
        updated_at=old_now,
    )
    projection = derive_workflow_application_projection([row], now=datetime.now(UTC))
    assert projection.run_projection == "TERMINAL"
    assert projection.business_status is None
    assert projection.persisted_status == "BLOCKED"
    assert projection.error_code == "CLEANUP_IDENTITY_INSPECTION_BLOCKED"


@pytest.mark.asyncio
async def test_sql_repository_limits_rows_to_two_and_stays_ambiguous(tmp_path: Path) -> None:
    repository = SQLMetadataRepository(f"sqlite:///{tmp_path / 'workflow_outcome.sqlite'}")
    Base.metadata.create_all(repository.engine)
    workflow_id = "cleanup-duplicate-rows"
    with repository.sessions.begin() as session:
        for _ in range(3):
            session.add(
                Run(
                    id=uuid4(),
                    campaign_id=None,
                    change_set_id=None,
                    workflow_id=workflow_id,
                    run_type="cleanup",
                    status="BLOCKED",
                    project="platform",
                )
            )
    rows = await repository.get_workflow_projection(workflow_id)
    assert len(rows) == 2
    projection = derive_workflow_application_projection(rows)
    assert projection.run_association == "AMBIGUOUS"


class _BlockedCompletedStatus:
    name = "COMPLETED"


class _BlockedCompletedDescription:
    run_id = "temporal-run-id-unrelated-to-app-uuid"
    status = _BlockedCompletedStatus()
    workflow_type = "CleanupWorkflow"


class _BlockedCompletedHandle:
    def __init__(self) -> None:
        self.result_called = False

    async def describe(self) -> _BlockedCompletedDescription:
        return _BlockedCompletedDescription()

    async def result(self) -> dict[str, Any]:
        self.result_called = True
        return {}


class _CleanupClient:
    def __init__(self, handle: _BlockedCompletedHandle) -> None:
        self.handle = handle

    def get_workflow_handle(self, _workflow_id: str) -> _BlockedCompletedHandle:
        return self.handle


@pytest.mark.asyncio
async def test_service_preserves_temporal_completed_while_exposing_blocked_application(
    tmp_path: Path,
) -> None:
    repository = SQLMetadataRepository(f"sqlite:///{tmp_path / 'workflow_outcome_service.sqlite'}")
    Base.metadata.create_all(repository.engine)
    workflow_id = "closure-cleanup-inspection-a277575-20260915"
    now = datetime.now(UTC)
    with repository.sessions.begin() as session:
        session.add(
            Run(
                id=uuid4(),
                campaign_id=None,
                change_set_id=None,
                workflow_id=workflow_id,
                run_type="cleanup",
                status="BLOCKED",
                project="platform",
                started_at=now - timedelta(minutes=5),
                finished_at=now,
                error_code="CLEANUP_IDENTITY_INSPECTION_BLOCKED",
            )
        )
    handle = _BlockedCompletedHandle()
    service = TemporalControlService(
        _CleanupClient(handle),  # type: ignore[arg-type]
        "engineering-control",
        repository,
    )

    result = await service.get_workflow(workflow_id)

    assert result["status"] == "COMPLETED"
    assert result["run_id"] == "temporal-run-id-unrelated-to-app-uuid"
    assert handle.result_called is False
    assert result["application"]["run_association"] == "UNVERIFIED"
    assert result["application"]["business_status"] is None
    assert result["application"]["persisted_status"] == "BLOCKED"
    assert result["application"]["error_code"] == "CLEANUP_IDENTITY_INSPECTION_BLOCKED"
    assert result["application"]["run_projection"] == "TERMINAL"


@pytest.mark.asyncio
async def test_service_surfaces_unavailable_without_raw_error_text_on_db_failure() -> None:
    handle = _BlockedCompletedHandle()
    service = TemporalControlService(
        _CleanupClient(handle),  # type: ignore[arg-type]
        "engineering-control",
        EmptyMetadataRepository(),
    )

    result = await service.get_workflow("some-non-acceptance-workflow")

    assert result["status"] == "COMPLETED"
    assert handle.result_called is False
    assert result["application"]["run_association"] == "UNAVAILABLE"
    assert result["application"]["business_status"] is None
    assert "durable run metadata store is unavailable" not in str(result["application"])


@pytest.mark.parametrize(
    "code", ["token=canary", "sk-canary", "ERR\nTOKEN", "ERR;DROP", "A" * 121, "código"]
)
def test_error_codes_are_rejected_not_rewritten(code: str) -> None:
    now = datetime.now(UTC)
    row = RunOutcomeRow("BLOCKED", code, now, now, now)
    assert derive_workflow_application_projection([row]).error_code == "REDACTED_ERROR_CODE"


@pytest.mark.parametrize(
    "status", ["SUCCESS", "COMPLETE", "COMPLETED", "BLOCKED", "PAUSED", "AWAITING_APPROVAL"]
)
def test_unverified_projection_never_claims_current_run_outcome(status: str) -> None:
    now = datetime.now(UTC)
    result = derive_workflow_application_projection([RunOutcomeRow(status, None, now, now, now)])
    assert result.persisted_status == status
    assert result.business_status is None
    assert result.run_association == "UNVERIFIED"
    assert result.scope == "WORKFLOW_ID_ONLY"


def test_queued_projection_has_original_timestamp_and_orphan_classification() -> None:
    now = datetime.now(UTC)
    old = (now - timedelta(hours=2)).replace(tzinfo=None)
    result = derive_workflow_application_projection(
        [RunOutcomeRow("QUEUED", None, None, None, old)], now=now
    )
    assert result.persisted_status == "QUEUED"
    assert result.run_projection == "ORPHANED_START_INTENT"
    assert result.updated_at == old.replace(tzinfo=UTC).isoformat()
    assert result.observed_at == now.isoformat()
    assert result.started_at is None and result.finished_at is None


@pytest.mark.asyncio
async def test_actual_database_failure_remains_visible_and_redacted(tmp_path: Path) -> None:
    repository = SQLMetadataRepository(f"sqlite:///{tmp_path / 'no_tables.sqlite'}")
    handle = _BlockedCompletedHandle()
    service = TemporalControlService(_CleanupClient(handle), "engineering-control", repository)  # type: ignore[arg-type]
    result = await service.get_workflow("cleanup-no-tables")
    assert result["application"]["run_association"] == "UNAVAILABLE"
    assert "sqlite" not in str(result) and "no such table" not in str(result)
    assert not handle.result_called


@pytest.mark.asyncio
async def test_empty_sql_result_is_none_not_unavailable(tmp_path: Path) -> None:
    repository = SQLMetadataRepository(f"sqlite:///{tmp_path / 'empty.sqlite'}")
    Base.metadata.create_all(repository.engine)
    handle = _BlockedCompletedHandle()
    service = TemporalControlService(_CleanupClient(handle), "engineering-control", repository)  # type: ignore[arg-type]
    result = await service.get_workflow("cleanup-not-present")
    assert result["application"]["run_association"] == "NONE"
    assert result["application"]["business_status"] is None


@pytest.mark.asyncio
async def test_request_cancellation_is_not_swallowed() -> None:
    class CancelledRepository(EmptyMetadataRepository):
        async def get_workflow_projection(self, workflow_id: str) -> list[RunOutcomeRow]:
            raise asyncio.CancelledError

    handle = _BlockedCompletedHandle()
    service = TemporalControlService(
        _CleanupClient(handle), "engineering-control", CancelledRepository()
    )  # type: ignore[arg-type]
    with pytest.raises(asyncio.CancelledError):
        await service.get_workflow("cleanup-cancelled-read")


@pytest.mark.parametrize("command", ["status", "diagnose", "run"])
def test_cli_preserves_separate_application_projection(command: str) -> None:
    value = {
        "status": "COMPLETED",
        "application": {
            "persisted_status": "BLOCKED",
            "business_status": None,
            "run_association": "UNVERIFIED",
        },
    }

    class Client:
        def request(self, method: str, path: str) -> dict[str, Any]:
            assert method == "GET" and path == "/v1/runs/cleanup-cli"
            return value

    assert dispatch(Client(), Namespace(command=command, workflow_id="cleanup-cli")) is value  # type: ignore[arg-type]


def test_actual_api_route_keeps_authentication_and_returns_projection(tmp_path: Path) -> None:
    repository = SQLMetadataRepository(f"sqlite:///{tmp_path / 'api.sqlite'}")
    Base.metadata.create_all(repository.engine)
    with repository.sessions.begin() as session:
        session.add(
            Run(
                workflow_id="cleanup-api",
                run_type="cleanup",
                project="platform",
                status="BLOCKED",
                error_code="CLEANUP_IDENTITY_INSPECTION_BLOCKED",
            )
        )
    handle = _BlockedCompletedHandle()
    service = TemporalControlService(_CleanupClient(handle), "engineering-control", repository)  # type: ignore[arg-type]
    app = create_app(settings=Settings(), service=service, token="fixture-token-" * 4)
    with TestClient(app) as client:
        assert client.get("/v1/runs/cleanup-api").status_code == 401
        response = client.get(
            "/v1/runs/cleanup-api", headers={"Authorization": "Bearer " + "fixture-token-" * 4}
        )
    assert response.status_code == 200
    assert response.json()["application"]["persisted_status"] == "BLOCKED"
    assert response.json()["status"] == "COMPLETED"
    assert not handle.result_called


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("ENGINEERING_TEST_POSTGRES_URL"),
    reason="disposable PostgreSQL fixture not supplied",
)
async def test_postgresql_projection_uses_read_only_bounded_transaction() -> None:
    url = os.environ["ENGINEERING_TEST_POSTGRES_URL"]
    assert url == "postgresql+psycopg://engineering_ci@127.0.0.1:5432/engineering_control_ci"
    repository = SQLMetadataRepository(url)
    Base.metadata.create_all(repository.engine)
    workflow_id = "fixture-projection-" + str(uuid4())
    with repository.sessions.begin() as session:
        session.add(
            Run(
                workflow_id=workflow_id,
                run_type="cleanup",
                project="platform",
                status="BLOCKED",
                error_code="CLEANUP_IDENTITY_INSPECTION_BLOCKED",
            )
        )
    observed = []

    def inspect_transaction(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.startswith("SELECT runs.status"):
            cursor.execute("SHOW transaction_read_only")
            read_only = cursor.fetchone()[0]
            cursor.execute("SHOW statement_timeout")
            observed.append((read_only, cursor.fetchone()[0]))

    event.listen(repository.engine, "before_cursor_execute", inspect_transaction)
    rows = await repository.get_workflow_projection(workflow_id)
    assert len(rows) == 1 and rows[0].status == "BLOCKED"
    assert observed == [("on", "3s")]
    repository.engine.dispose()
