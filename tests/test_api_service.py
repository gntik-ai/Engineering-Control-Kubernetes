from __future__ import annotations

from typing import Any

import pytest

from engineering_control.api.schemas import (
    AcceptanceLifecycleStart,
    MultiProjectAuditStart,
    ObservabilityAcceptanceStart,
)
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
        self.workflow_name = ""
        self.workflow_type = ""
        self.workflow_id = ""
        self.task_queue = ""

    async def start_workflow(
        self, workflow_type: str, workflow_input: Any, *, task_queue: str, **kwargs: Any
    ) -> _StartedHandle:
        self.workflow_type = workflow_type
        self.workflow_name = workflow_type
        self.workflow_input = workflow_input
        self.workflow_id = str(kwargs.get("id", ""))
        self.task_queue = task_queue
        return _StartedHandle()


class _MultiProjectRepository(EmptyMetadataRepository):
    def __init__(self) -> None:
        self.parent_workflow_id = ""
        self.campaigns: dict[Any, tuple[Any, str]] = {}
        self.request: MultiProjectAuditStart | None = None

    async def create_multi_project_audit(
        self,
        workflow_id: str,
        campaigns: dict[Any, tuple[Any, str]],
        request: MultiProjectAuditStart,
    ) -> None:
        self.parent_workflow_id = workflow_id
        self.campaigns = campaigns
        self.request = request


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


@pytest.mark.asyncio
async def test_observability_acceptance_binds_native_canary_to_control_queue() -> None:
    client = _StartClient()
    service = TemporalControlService(
        client,  # type: ignore[arg-type]
        "engineering-control",
        EmptyMetadataRepository(),
    )

    accepted = await service.start_observability_acceptance(
        ObservabilityAcceptanceStart(confirm_read_only=True)
    )

    assert accepted.workflow_id.startswith("observability-acceptance-")
    assert accepted.run_id == "started-run-id"
    assert client.task_queue == "engineering-control"
    assert client.workflow_input.workspace_id in accepted.workflow_id


@pytest.mark.asyncio
async def test_observability_acceptance_rejects_non_read_only_contract() -> None:
    service = TemporalControlService(
        _StartClient(),  # type: ignore[arg-type]
        "engineering-control",
        EmptyMetadataRepository(),
    )
    with pytest.raises(ValueError, match="read-only contract"):
        await service.start_observability_acceptance(
            ObservabilityAcceptanceStart.model_construct(confirm_read_only=False)  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_reverse_observability_acceptance_binds_claude_maker_workflow() -> None:
    client = _StartClient()
    service = TemporalControlService(
        client,  # type: ignore[arg-type]
        "engineering-control",
        EmptyMetadataRepository(),
    )

    accepted = await service.start_reverse_observability_acceptance(
        ObservabilityAcceptanceStart(confirm_read_only=True)
    )

    assert accepted.workflow_id.startswith("reverse-observability-acceptance-")
    assert accepted.run_id == "started-run-id"
    assert client.workflow_name == "ReverseObservabilityAcceptanceWorkflow"
    assert client.task_queue == "engineering-control"


@pytest.mark.asyncio
async def test_multi_project_audit_service_creates_one_parent_and_three_bound_children() -> None:
    client = _StartClient()
    repository = _MultiProjectRepository()
    service = TemporalControlService(client, "engineering-control", repository)

    accepted = await service.start_all_project_audits(MultiProjectAuditStart(preset="snapshot"))

    assert accepted.workflow_id == repository.parent_workflow_id == client.workflow_id
    assert accepted.workflow_id.startswith("audit-all-")
    assert accepted.run_id == "started-run-id"
    assert client.workflow_type == "MultiProjectAuditWorkflow"
    assert client.task_queue == "engineering-control"
    assert {project.value for project in repository.campaigns} == {
        "falcone",
        "musematic",
        "llmwiki",
    }
    assert set(client.workflow_input.campaign_ids) == {"falcone", "musematic", "llmwiki"}
    assert all(
        child.startswith(f"{accepted.workflow_id}-")
        for child in client.workflow_input.child_workflow_ids.values()
    )
    assert repository.request is not None and repository.request.production is False
