from __future__ import annotations

from typing import Any

import pytest

from engineering_control.api.schemas import AcceptanceLifecycleStart
from engineering_control.api.service import TemporalControlService
from engineering_control.db.repository import EmptyMetadataRepository


class _Status:
    name = "COMPLETED"


class _Description:
    run_id = "run-id"
    status = _Status()
    workflow_type = "AcceptanceLifecycleWorkflow"


class _Handle:
    def __init__(self) -> None:
        self.result_called = False

    async def describe(self) -> _Description:
        return _Description()

    async def result(self) -> dict[str, Any]:
        self.result_called = True
        return {
            "status": "PASS",
            "change_set_state": "AWAITING_STAGING_APPROVAL",
            "maker_provider": "codex-synthetic",
            "checker_provider": "claude-synthetic",
            "evidence_sha256": "a" * 64,
            "redaction_status": "SCANNED_CLEAN",
            "evidence_path": "/private/path/must-not-be-returned",
        }


class _Client:
    def __init__(self, handle: _Handle) -> None:
        self.handle = handle

    def get_workflow_handle(self, _workflow_id: str) -> _Handle:
        return self.handle


class _StartedHandle:
    first_execution_run_id = "started-run-id"


class _StartClient:
    def __init__(self) -> None:
        self.workflow_input: Any = None
        self.task_queue = ""

    async def start_workflow(
        self, _workflow_type: str, workflow_input: Any, *, task_queue: str, **_kwargs: Any
    ) -> _StartedHandle:
        self.workflow_input = workflow_input
        self.task_queue = task_queue
        return _StartedHandle()


@pytest.mark.asyncio
async def test_acceptance_status_returns_only_normalized_evidence_metadata() -> None:
    handle = _Handle()
    service = TemporalControlService(
        _Client(handle),  # type: ignore[arg-type]
        "engineering-control",
        EmptyMetadataRepository(),
    )

    result = await service.get_workflow("acceptance-11111111-1111-4111-8111-111111111111")

    assert handle.result_called is True
    assert result["validation"]["evidence_sha256"] == "a" * 64
    assert "evidence_path" not in result["validation"]


@pytest.mark.asyncio
async def test_non_acceptance_status_never_reads_workflow_result() -> None:
    handle = _Handle()
    service = TemporalControlService(
        _Client(handle),  # type: ignore[arg-type]
        "engineering-control",
        EmptyMetadataRepository(),
    )

    result = await service.get_workflow("audit-falcone-fixture")

    assert result["status"] == "COMPLETED"
    assert handle.result_called is False
    assert "validation" not in result


@pytest.mark.asyncio
async def test_acceptance_service_binds_private_activity_and_control_queues() -> None:
    client = _StartClient()
    activity_queue = "engineering-acceptance-validation-bounded"
    service = TemporalControlService(
        client,  # type: ignore[arg-type]
        "engineering-acceptance-control-validation-bounded",
        EmptyMetadataRepository(),
        acceptance_activity_task_queue=activity_queue,
    )

    accepted = await service.start_acceptance(
        AcceptanceLifecycleStart(hold_seconds=2),
        "/home/hermes/engineering-bootstrap/acceptance-runs/bounded",
    )

    assert accepted.run_id == "started-run-id"
    assert client.task_queue == "engineering-acceptance-control-validation-bounded"
    assert client.workflow_input.activity_task_queue == activity_queue
