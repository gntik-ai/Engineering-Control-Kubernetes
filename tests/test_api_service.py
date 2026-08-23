from __future__ import annotations

from typing import Any

import pytest

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
