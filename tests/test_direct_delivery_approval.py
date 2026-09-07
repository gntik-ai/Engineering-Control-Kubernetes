from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from temporalio.common import WorkflowIDReusePolicy
from test_issue_delivery_approval import _child, _dispose

from engineering_control.api.main import create_app
from engineering_control.api.schemas import DeliveryStart, ExistingBacklogLoopStart
from engineering_control.api.service import TemporalControlService
from engineering_control.config import Settings
from engineering_control.db.models import (
    BacklogIssueRecord,
    GitHubBacklogCampaign,
    OperationRequest,
    Run,
)
from engineering_control.models.enums import RiskClass

RUN_ID = "22222222-3333-4333-8333-222222222222"


def client_for(request: ExistingBacklogLoopStart):
    identifier = (
        f"backlog-loop-{request.project}-{request.backlog_campaign_id}"
        f"-operation-{request.issue_operation_id}"
    )
    client = Mock()
    client.start_workflow = AsyncMock(return_value=SimpleNamespace(first_execution_run_id=RUN_ID))
    client.get_workflow_handle.return_value.describe = AsyncMock(
        return_value=SimpleNamespace(
            id=identifier, run_id=RUN_ID, workflow_type="ExistingGitHubBacklogLoopWorkflow"
        )
    )
    return client, identifier


def approved_body(values):
    request = ExistingBacklogLoopStart.model_validate(
        {key: values[4][key] for key in ExistingBacklogLoopStart.model_fields if key in values[4]}
    )
    return DeliveryStart(
        finding_id=values[6]["finding_id"], risk_class="R1", dry_run=False, approved_issue=request
    )


@pytest.mark.asyncio
async def test_internal_unapproved_real_delivery_denied_before_work() -> None:
    client, metadata = Mock(), Mock()
    service = TemporalControlService(client, "fixture-only", metadata)
    request = DeliveryStart.model_construct(
        finding_id=uuid4(), risk_class=RiskClass.R1, dry_run=False, approved_issue=None
    )
    with pytest.raises(ValidationError):
        await service.start_delivery(request)
    assert not client.mock_calls and not metadata.mock_calls


@pytest.mark.asyncio
async def test_approved_direct_delivery_adopts_operation_without_second_run(tmp_path: Path):
    values = await _child(tmp_path)
    try:
        body = approved_body(values)
        request = body.approved_issue
        assert request is not None
        client, expected = client_for(request)
        with values[1].sessions() as session:
            before = session.scalar(select(func.count()).select_from(Run))
        result = await TemporalControlService(client, "fixture-only", values[1]).start_delivery(
            body
        )
        assert result.workflow_id == expected
        call = client.start_workflow.await_args
        assert call.args[0] == "ExistingGitHubBacklogLoopWorkflow"
        assert asdict(call.args[1]) == request.model_dump(mode="json")
        assert call.kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
        with values[1].sessions() as session:
            assert session.scalar(select(func.count()).select_from(Run)) == before
            operation = session.get(OperationRequest, request.issue_operation_id)
            assert operation.status == "STARTED"
            assert operation.workflow_id == expected and operation.temporal_run_id == RUN_ID
    finally:
        _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["RUNNING", "APPROVED"])
async def test_direct_delivery_same_active_operation_is_not_started_again(
    tmp_path: Path, state: str
):
    values = await _child(tmp_path)
    try:
        body = approved_body(values)
        request = body.approved_issue
        assert request is not None
        client, expected = client_for(request)
        service = TemporalControlService(client, "fixture-only", values[1])
        with values[1].sessions.begin() as session:
            session.get(GitHubBacklogCampaign, request.backlog_campaign_id).status = state
        if state == "APPROVED":
            from temporalio.exceptions import WorkflowAlreadyStartedError

            client.start_workflow.side_effect = WorkflowAlreadyStartedError(
                expected, "ExistingGitHubBacklogLoopWorkflow", run_id=RUN_ID
            )
        result = await service.start_delivery(body)
        assert result.status == "EXISTING" and result.workflow_id == expected
        if state == "RUNNING":
            client.start_workflow.assert_not_awaited()
        client.get_workflow_handle.return_value.describe.assert_awaited_once()
    finally:
        _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["finding", "risk", "body", "owner", "link", "repository", "workflow"]
)
async def test_direct_delivery_rejects_changed_origin_before_dispatch(tmp_path: Path, change: str):
    values = await _child(tmp_path)
    try:
        body = approved_body(values)
        request = body.approved_issue
        assert request is not None
        client, _ = client_for(request)
        with values[1].sessions.begin() as session:
            issue = session.scalar(select(BacklogIssueRecord))
            assert issue is not None
            if change == "body":
                issue.body_sha256 = "9" * 64
            elif change == "owner":
                session.get(OperationRequest, request.issue_operation_id).status = "BLOCKED"
            elif change == "link":
                issue.finding_id = None
            elif change == "workflow":
                session.get(OperationRequest, request.issue_operation_id).workflow_id = "foreign"
        if change == "finding":
            body = body.model_copy(update={"finding_id": uuid4()})
        elif change == "risk":
            body = body.model_copy(update={"risk_class": RiskClass.R0})
        elif change == "repository":
            body = body.model_copy(
                update={
                    "approved_issue": request.model_copy(
                        update={"target_repository": "gntik-ai/falcone-charts"}
                    )
                }
            )
        with pytest.raises(ValueError):
            await TemporalControlService(client, "fixture-only", values[1]).start_delivery(body)
        client.start_workflow.assert_not_awaited()
    finally:
        _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["dry_run", "full_delivery"])
async def test_direct_delivery_cannot_turn_dry_run_into_execution_or_expand_scope(
    tmp_path: Path, change: str
):
    values = await _child(tmp_path)
    try:
        body = approved_body(values)
        raw = body.model_dump(mode="json")
        if change == "dry_run":
            raw["dry_run"] = True
        else:
            raw["approved_issue"]["delivery_mode"] = "full-delivery"
        with pytest.raises(ValueError):
            DeliveryStart.model_validate(raw)
    finally:
        _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["valid", "anonymous", "wrong_finding"])
async def test_direct_delivery_api_uses_real_authority_checks(tmp_path: Path, case: str):
    values = await _child(tmp_path)
    try:
        body = approved_body(values)
        request = body.approved_issue
        assert request is not None
        temporal, expected = client_for(request)
        service = TemporalControlService(temporal, "fixture-only", values[1])
        bus = Mock()
        bus.dispatch_pending = AsyncMock(return_value=False)
        app = create_app(
            settings=Settings(_env_file=None, database_url=str(values[0].engine.url)),
            service=service,
            repository=values[1],
            token="x" * 32,
            operations_repository=values[0],
            command_bus=bus,
        )
        payload = body.model_dump(mode="json")
        if case == "wrong_finding":
            payload["finding_id"] = str(uuid4())
        headers = {} if case == "anonymous" else {"Authorization": "Bearer " + "x" * 32}
        with TestClient(app) as http:
            response = http.post("/v1/deliveries", json=payload, headers=headers)
        if case == "valid":
            assert response.status_code == 202 and response.json()["workflow_id"] == expected
            temporal.start_workflow.assert_awaited_once()
        else:
            assert response.status_code == (401 if case == "anonymous" else 409)
            temporal.start_workflow.assert_not_awaited()
            assert "Traceback" not in response.text
    finally:
        _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["wrong_id", "wrong_type", "missing"])
async def test_running_projection_never_authorizes_a_new_or_unrelated_workflow(
    tmp_path: Path, identity: str
):
    values = await _child(tmp_path)
    try:
        body = approved_body(values)
        request = body.approved_issue
        assert request is not None
        temporal, expected = client_for(request)
        with values[1].sessions.begin() as session:
            session.get(GitHubBacklogCampaign, request.backlog_campaign_id).status = "RUNNING"
        if identity == "missing":
            temporal.get_workflow_handle.return_value.describe.side_effect = LookupError(
                "inert missing history"
            )
        else:
            temporal.get_workflow_handle.return_value.describe.return_value = SimpleNamespace(
                id="unrelated" if identity == "wrong_id" else expected,
                workflow_type="UnrelatedWorkflow"
                if identity == "wrong_type"
                else "ExistingGitHubBacklogLoopWorkflow",
                run_id=RUN_ID,
            )
        from engineering_control.operations.issue_start import IssueStartUnconfirmedError

        with pytest.raises(IssueStartUnconfirmedError, match="ISSUE_START_UNCONFIRMED"):
            await TemporalControlService(temporal, "fixture-only", values[1]).start_delivery(body)
        temporal.start_workflow.assert_not_awaited()
    finally:
        _dispose(values)


@pytest.mark.asyncio
async def test_legacy_dry_run_does_not_dispatch_mutating_backlog(tmp_path: Path):
    values = await _child(tmp_path)
    try:
        temporal = Mock()
        temporal.start_workflow = AsyncMock(
            return_value=SimpleNamespace(first_execution_run_id="dry-fixture")
        )
        body = DeliveryStart(finding_id=values[6]["finding_id"], risk_class="R1")
        result = await TemporalControlService(temporal, "fixture-only", values[1]).start_delivery(
            body
        )
        call = temporal.start_workflow.await_args
        assert call.args[0] == "IssueDeliveryWorkflow" and call.args[1]["dry_run"] is True
        assert set(call.args[1]) == {"finding_id", "risk_class", "dry_run"}
        assert result.workflow_id.startswith("delivery-")
    finally:
        _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_receipt_loss_returns_no_false_success_and_retry_links_one_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool
):
    from temporalio.exceptions import WorkflowAlreadyStartedError

    values = await _child(tmp_path)
    try:
        body = approved_body(values)
        request = body.approved_issue
        assert request is not None
        temporal, expected = client_for(request)
        metadata = values[1]
        original = metadata.record_delivery_issue_start

        async def lost_receipt(request, accepted):
            if committed:
                await original(request, accepted)
            raise RuntimeError("inert receipt acknowledgement lost")

        monkeypatch.setattr(metadata, "record_delivery_issue_start", lost_receipt)
        service = TemporalControlService(temporal, "fixture-only", metadata)
        with metadata.sessions() as session:
            count_before = session.scalar(select(func.count()).select_from(Run))
            started_before = session.get(OperationRequest, request.issue_operation_id).started_at
        bus = Mock()
        bus.dispatch_pending = AsyncMock(return_value=False)
        app = create_app(
            settings=Settings(_env_file=None, database_url=str(values[0].engine.url)),
            service=service,
            repository=metadata,
            token="x" * 32,
            operations_repository=values[0],
            command_bus=bus,
        )
        with TestClient(app, raise_server_exceptions=False) as http:
            response = http.post(
                "/v1/deliveries",
                json=body.model_dump(mode="json"),
                headers={"Authorization": "Bearer " + "x" * 32},
            )
        assert response.status_code == 503
        assert response.json()["status"] == "START_UNCONFIRMED"
        assert response.json()["operation_id"] == str(request.issue_operation_id)
        assert response.json()["expected_workflow_id"] == expected
        assert "inert receipt" not in response.text and "Traceback" not in response.text
        with metadata.sessions() as session:
            operation = session.get(OperationRequest, request.issue_operation_id)
            assert operation.status == ("STARTED" if committed else "DISPATCHING")
            assert operation.workflow_id == (expected if committed else None)
        monkeypatch.setattr(metadata, "record_delivery_issue_start", original)
        temporal.start_workflow.side_effect = WorkflowAlreadyStartedError(
            expected, "ExistingGitHubBacklogLoopWorkflow", run_id=RUN_ID
        )
        retry = await service.start_delivery(body)
        assert retry.status == "EXISTING" and retry.workflow_id == expected
        assert {call.kwargs["id"] for call in temporal.start_workflow.await_args_list} == {expected}
        with metadata.sessions() as session:
            operation = session.get(OperationRequest, request.issue_operation_id)
            assert operation.status == "STARTED" and operation.workflow_id == expected
            assert operation.temporal_run_id == RUN_ID
            assert operation.started_at == started_before
            assert session.scalar(select(func.count()).select_from(Run)) == count_before
    finally:
        _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["workflow", "run", "status", "owner"])
async def test_start_receipt_rejects_wrong_identity_or_revoked_operation(
    tmp_path: Path, change: str
):
    from engineering_control.api.schemas import OperationAccepted

    values = await _child(tmp_path)
    try:
        body = approved_body(values)
        request = body.approved_issue
        assert request is not None
        _, expected = client_for(request)
        receipt = OperationAccepted(workflow_id=expected, run_id=RUN_ID)
        if change == "owner":
            with values[1].sessions.begin() as session:
                session.get(OperationRequest, request.issue_operation_id).status = "BLOCKED"
        else:
            key, value = {
                "workflow": ("workflow_id", "foreign"),
                "run": ("run_id", "not-a-temporal-run-uuid"),
                "status": ("status", "FAILED"),
            }[change]
            receipt = receipt.model_copy(update={key: value})
        with pytest.raises(ValueError):
            await values[1].record_delivery_issue_start(body, receipt)
        with values[1].sessions() as session:
            operation = session.get(OperationRequest, request.issue_operation_id)
            assert operation.workflow_id is None and operation.temporal_run_id is None
            assert operation.status == ("BLOCKED" if change == "owner" else "DISPATCHING")
    finally:
        _dispose(values)
