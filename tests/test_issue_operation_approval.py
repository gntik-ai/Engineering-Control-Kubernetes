from __future__ import annotations

import hashlib
import json
import runpy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import select
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from test_controlled_operations import (
    CAMPAIGN_ID,
    _idempotency_fixture,
    _repository,
    _seed_ready_issue,
    _solve_issue,
)

from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.api.schemas import (
    BacklogApprovalCandidateRequest,
    ExistingBacklogLoopStart,
)
from engineering_control.api.service import TemporalControlService
from engineering_control.config import Settings
from engineering_control.db.models import BacklogIssueRecord, OperationRequest
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.operations.service import TypedCommandBus
from engineering_control.workflows.definitions import _backlog_execution_payload
from engineering_control.workflows.inputs import ExistingGitHubBacklogLoopInput

POLICY_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/reconcile-backlog-github-policy.py"


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_matches", [True, False])
async def test_repeated_direct_handoff_uses_one_durable_workflow_id(
    tmp_path: Path, identity_matches: bool
) -> None:
    operations, metadata, handler, request = await _approved_direct_request(tmp_path)
    workflow_id = (
        f"backlog-loop-{request.project}-{request.backlog_campaign_id}"
        f"-operation-{request.issue_operation_id}"
    )
    workflow_type = "ExistingGitHubBacklogLoopWorkflow"
    client = Mock()
    client.start_workflow = AsyncMock(
        side_effect=[
            SimpleNamespace(first_execution_run_id="fixture-run"),
            WorkflowAlreadyStartedError(workflow_id, workflow_type, run_id="fixture-run"),
        ]
    )
    client.get_workflow_handle.return_value.describe = AsyncMock(
        return_value=SimpleNamespace(
            id=workflow_id,
            run_id="fixture-run",
            workflow_type=workflow_type if identity_matches else "UnrelatedWorkflow",
        )
    )
    service = TemporalControlService(client, "isolated-recorder-only", metadata)
    first = await service.start_existing_backlog_loop(request)
    if identity_matches:
        second = await service.start_existing_backlog_loop(request)
        assert first.workflow_id == second.workflow_id == workflow_id
        assert first.run_id == second.run_id == "fixture-run"
        assert second.status == "EXISTING"
    else:
        from engineering_control.operations.issue_start import IssueStartUnconfirmedError

        with pytest.raises(IssueStartUnconfirmedError, match="ISSUE_START_UNCONFIRMED"):
            await service.start_existing_backlog_loop(request)
    assert len(client.start_workflow.await_args_list) == 2
    assert all(call.kwargs["id"] == workflow_id for call in client.start_workflow.await_args_list)
    assert all(
        call.kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
        for call in client.start_workflow.await_args_list
    )
    handler.engine.dispose()
    metadata.engine.dispose()
    operations.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["cli", "telegram"])
@pytest.mark.parametrize("change", ["body", "legacy_binding"])
async def test_delayed_all_channel_dispatch_denies_changed_approval(
    tmp_path: Path, channel: str, change: str
) -> None:
    repository = _repository(tmp_path)
    _seed_ready_issue(repository)
    operation = await repository.create_external(
        _solve_issue(),
        source_channel=channel,  # type: ignore[arg-type]
        operator_class="telegram:allowlisted"
        if channel == "telegram"
        else "engineering-ctl:hermes",
        idempotency_key=_idempotency_fixture(channel, 51),
    )
    with repository.sessions.begin() as session:
        if change == "body":
            issue = session.scalar(select(BacklogIssueRecord))
            assert issue is not None
            issue.body_sha256 = "9" * 64
        else:
            row = session.get(OperationRequest, operation.operation_id)
            assert row is not None
            # The previous CLI/Telegram format signed only the command payload.
            row.binding_hash = canonical_sha256(_solve_issue().model_dump(mode="json"))
    control = Mock()
    metadata = Mock()
    bus = TypedCommandBus(control, metadata, repository)  # type: ignore[arg-type]
    assert await bus.dispatch_pending()
    refreshed = await repository.operation(operation.operation_id)
    assert refreshed is not None and refreshed.status == "BLOCKED"
    assert not control.mock_calls and not metadata.mock_calls
    assert not await bus.dispatch_pending()
    repository.engine.dispose()


async def _approved_direct_request(
    tmp_path: Path, *, eligibility: str = "READY", issue_body: str | None = None
):
    operations = _repository(tmp_path)
    _seed_ready_issue(operations)
    with operations.sessions.begin() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        assert issue is not None
        issue.eligibility_status = eligibility
        if issue_body is not None:
            # Bind a recoverable inert body before creating the approved operation.
            issue.body_sha256 = hashlib.sha256(issue_body.encode()).hexdigest()
    operation = await operations.create_external(
        _solve_issue(),
        source_channel="cli",
        operator_class="engineering-ctl:hermes",
        idempotency_key=_idempotency_fixture("direct", 52),
    )
    assert await operations.claim(operation.operation_id) is not None
    request = ExistingBacklogLoopStart(
        project="falcone",
        backlog_campaign_id=CAMPAIGN_ID,
        maximum_risk=_solve_issue().maximum_risk,
        delivery_mode=_solve_issue().delivery_mode,
        target_repository=_solve_issue().repository,
        target_issue_number=42,
        continue_after_blocked_issue=False,
        correction_limit=_solve_issue().correction_limit,
        issue_operation_id=operation.operation_id,
        issue_approval_binding=operation.binding_hash,
    )
    policy_root = tmp_path / "fixture-policy-requests"
    policy_root.mkdir(mode=0o700)
    metadata = SQLMetadataRepository(
        str(operations.engine.url), github_policy_request_root=policy_root
    )
    candidate = await metadata.prepare_backlog_approval(
        CAMPAIGN_ID,
        BacklogApprovalCandidateRequest(
            project="falcone",
            delivery_mode=request.delivery_mode,
            maximum_risk=request.maximum_risk,
            maximum_items=1,
        ),
        issue_request=request,
    )
    await metadata.approve_backlog_campaign(
        CAMPAIGN_ID, "falcone", candidate["approval_binding"], "cli:engineering-ctl"
    )
    assert (policy_root / "activate.json").is_file()
    handler = EngineeringActivityHandler(
        Settings(_env_file=None, database_url=str(operations.engine.url))
    )
    return operations, metadata, handler, request


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["unchanged", "before_start", "after_start"])
async def test_real_metadata_control_and_activity_revision_boundaries(
    tmp_path: Path, boundary: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations, metadata, handler, request = await _approved_direct_request(tmp_path)
    client = Mock()
    client.start_workflow = AsyncMock(
        return_value=SimpleNamespace(first_execution_run_id="fixture-only-run")
    )
    service = TemporalControlService(client, "isolated-recorder-only", metadata)

    def edit() -> None:
        with operations.sessions.begin() as session:
            issue = session.scalar(select(BacklogIssueRecord))
            assert issue is not None
            issue.body_sha256 = "9" * 64

    if boundary == "before_start":
        original = metadata.get_backlog_campaign

        async def changed_campaign(campaign_id):
            campaign = await original(campaign_id)
            edit()
            return campaign

        monkeypatch.setattr(metadata, "get_backlog_campaign", changed_campaign)
        with pytest.raises(ValueError, match="snapshot changed"):
            await service.start_existing_backlog_loop(request)
        client.start_workflow.assert_not_awaited()
    else:
        await service.start_existing_backlog_loop(request)
        client.start_workflow.assert_awaited_once()
        workflow_input = client.start_workflow.await_args.args[1]
        assert workflow_input.issue_operation_id == str(request.issue_operation_id)
        assert workflow_input.issue_approval_binding == request.issue_approval_binding
        payload = _backlog_execution_payload(workflow_input)
        if boundary == "after_start":
            edit()
            loaded = handler._load_backlog_execution(payload)
            gate = handler._backlog_revalidate_gate(payload)
            assert loaded["status"] == gate["status"] == "BLOCKED"
            assert (
                loaded["blocker"] == gate["blocker"] == "DIRECT_ISSUE_APPROVAL_REQUIRED_OR_CHANGED"
            )
        else:
            loaded = handler._load_backlog_execution(payload)
            assert loaded["status"] == "SELECTED"
            assert len(loaded["items"]) == 1
            assert loaded["items"][0]["issue_number"] == 42
            edit()
            gate = handler._backlog_revalidate_gate({**payload, "item": loaded["items"][0]})
            assert gate["status"] == "BLOCKED"
            assert gate["blocker"] == "DIRECT_ISSUE_APPROVAL_REQUIRED_OR_CHANGED"
    handler.engine.dispose()
    metadata.engine.dispose()
    operations.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"issue_operation_id": None, "issue_approval_binding": None},
        {"issue_operation_id": uuid4()},
        {"issue_approval_binding": "0" * 64},
        {"target_issue_number": 43},
        {"correction_limit": 5},
    ],
)
async def test_direct_approval_context_cannot_be_omitted_or_retargeted(
    tmp_path: Path, change: dict
) -> None:
    operations, metadata, handler, request = await _approved_direct_request(tmp_path)
    client = Mock()
    service = TemporalControlService(client, "isolated-recorder-only", metadata)
    changed = ExistingBacklogLoopStart.model_validate({**request.model_dump(mode="json"), **change})
    with pytest.raises(ValueError):
        await service.start_existing_backlog_loop(changed)
    assert not client.mock_calls
    payload = changed.model_dump(mode="json")
    assert handler._load_backlog_execution(payload)["status"] == "BLOCKED"
    assert handler._backlog_revalidate_gate(payload)["status"] == "BLOCKED"
    handler.engine.dispose()
    metadata.engine.dispose()
    operations.engine.dispose()


def test_legacy_workflow_activity_payload_does_not_gain_null_fields() -> None:
    request = ExistingGitHubBacklogLoopInput(
        project="falcone",
        backlog_campaign_id=str(CAMPAIGN_ID),
        maximum_risk="R2",
        delivery_mode="source-pr",
    )
    expected = asdict(request)
    del expected["issue_operation_id"]
    del expected["issue_approval_binding"]
    assert _backlog_execution_payload(request) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["prepare", "approve", "policy_consumer"])
async def test_issue_edit_blocks_metadata_and_policy_request_boundaries(
    tmp_path: Path, boundary: str
) -> None:
    operations, metadata, handler, request = await _approved_direct_request(tmp_path)
    path = metadata.github_policy_request_root / "activate.json"
    preimage = path.read_bytes()
    record = json.loads(preimage)
    namespace = runpy.run_path(str(POLICY_SCRIPT))
    with metadata.sessions() as session:
        assert namespace["validate_campaign"](session, record).id == CAMPAIGN_ID
    with operations.sessions.begin() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        assert issue is not None
        issue.body_sha256 = "9" * 64
    with pytest.raises(ValueError, match="snapshot changed"):
        if boundary == "prepare":
            await metadata.prepare_backlog_approval(
                CAMPAIGN_ID,
                BacklogApprovalCandidateRequest(
                    project="falcone",
                    delivery_mode=request.delivery_mode,
                    maximum_risk=request.maximum_risk,
                    maximum_items=1,
                ),
                issue_request=request,
            )
        elif boundary == "approve":
            await metadata.approve_backlog_campaign(
                CAMPAIGN_ID, "falcone", record["approval_binding"], "cli:engineering-ctl"
            )
        else:
            with metadata.sessions() as session:
                namespace["validate_campaign"](session, record)
    assert path.read_bytes() == preimage
    handler.engine.dispose()
    metadata.engine.dispose()
    operations.engine.dispose()


@pytest.mark.parametrize(
    "widening",
    [
        {"global_campaign_id": uuid4()},
        {"continue_after_blocked_issue": True},
        {"maximum_items": 2},
    ],
)
def test_direct_operation_schema_rejects_wider_scope(widening: dict) -> None:
    with pytest.raises(ValueError):
        ExistingBacklogLoopStart(
            **{
                "project": "falcone",
                "backlog_campaign_id": CAMPAIGN_ID,
                "maximum_risk": "R2",
                "delivery_mode": "source-pr",
                "target_repository": "gntik-ai/falcone",
                "target_issue_number": 42,
                "issue_operation_id": uuid4(),
                "issue_approval_binding": "f" * 64,
                "continue_after_blocked_issue": False,
                **widening,
            }
        )


def test_direct_operation_schema_carries_its_operation_bound_r3_binding() -> None:
    # Merged with main NEW-95: an R3 issue operation carries the exact R3 approval binding
    # that is part of the operation's approved payload (re-checked at the approval
    # boundary as an execution option), so it is not a widening of the direct scope.
    request = ExistingBacklogLoopStart(
        project="falcone",
        backlog_campaign_id=CAMPAIGN_ID,
        maximum_risk="R3",
        delivery_mode="source-pr",
        target_repository="gntik-ai/falcone",
        target_issue_number=42,
        issue_operation_id=uuid4(),
        issue_approval_binding="f" * 64,
        continue_after_blocked_issue=False,
        r3_approval_binding="0" * 64,
    )
    assert request.r3_approval_binding == "0" * 64
