from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from temporalio.converter import DefaultPayloadConverter
from test_issue_operation_approval import _approved_direct_request
from test_metadata_activity import add_campaign

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db.models import (
    BacklogIssueRecord,
    ChangeSet,
    CoverageCell,
    Finding,
    Issue,
    OperationRequest,
    Run,
    Step,
)
from engineering_control.operations.issue_delivery_approval import (
    validate_change_set_issue_approval,
)
from engineering_control.workflows.inputs import IssueDeliveryInput


@pytest.mark.parametrize("approval", [None, "11111111-1111-4111-8111-111111111111"])
def test_delivery_payload_preserves_legacy_serialized_fields(approval: str | None):
    legacy = {"finding_id": "fixture-finding", "risk_class": "R1", "dry_run": False}
    request = IssueDeliveryInput(**legacy, issue_approval_id=approval)
    expected = legacy if approval is None else {**legacy, "issue_approval_id": approval}
    converter = DefaultPayloadConverter()
    assert converter.to_payloads([request.payload()]) == converter.to_payloads([expected])
    assert converter.from_payloads(
        converter.to_payloads([request.payload()]), [IssueDeliveryInput]
    ) == [request]


async def _child(tmp_path: Path, *, eligibility: str = "READY", issue_body: str | None = None):
    operations, metadata, engineering, request = await _approved_direct_request(
        tmp_path, eligibility=eligibility, issue_body=issue_body
    )
    handler = MetadataActivityHandler(
        Settings(
            _env_file=None,
            database_url=str(operations.engine.url),
            state_root=tmp_path / "state",
            worktree_root=tmp_path / "worktrees",
            run_root=tmp_path / "runs",
            project_config_root=Path(__file__).parents[1] / "config/projects",
        )
    )
    campaign = add_campaign(handler)
    coverage = CoverageCell(
        id=uuid4(),
        campaign_id=campaign.id,
        domain="api",
        persona="operator",
        lens="correctness",
        environment="staging",
        status="CONFIRMED_FINDING",
        evidence_json=[],
        result_json={},
    )
    finding = Finding(
        id=uuid4(),
        campaign_id=campaign.id,
        coverage_cell_id=coverage.id,
        fingerprint=uuid4().hex * 2,
        project="falcone",
        domain="api",
        persona="operator",
        severity="medium",
        confidence=0.9,
        risk_class="R1",
        disposition="CONFIRMED",
        expected_behavior="fixture contract",
        actual_behavior="fixture mismatch",
        evidence_json=[],
        minimal_reproduction=["fixture only"],
        affected_source_sha="a" * 40,
        affected_deployment_sha="b" * 40,
        affected_component="api",
        impact_json={},
        recommended_action="bounded fixture repair",
    )
    with handler.sessions.begin() as session:
        session.add_all([coverage, finding])
        session.flush()
        issue = session.scalar(select(BacklogIssueRecord))
        assert issue is not None
        issue.finding_id = finding.id
        session.add(
            Issue(
                finding_id=finding.id,
                repository=issue.repository,
                number=issue.issue_number,
                url=issue.issue_url_reference,
                state="OPEN",
            )
        )
    payload = {
        **request.model_dump(mode="json"),
        "child_kind": "issue-delivery",
        "child_workflow_id": "backlog-fixture-issue-1-attempt-1",
        "finding_id": str(finding.id),
        "orchestration": {"workflow_id": "backlog-fixture-parent"},
    }
    registered = engineering._register_backlog_child_run(payload)
    child_payload = {
        "finding_id": str(finding.id),
        "risk_class": "R1",
        "dry_run": False,
        "issue_approval_id": registered["issue_approval_id"],
        "orchestration": {"workflow_id": payload["child_workflow_id"]},
    }
    return operations, metadata, engineering, handler, payload, registered, child_payload


def _dispose(values):
    for obj in values[:4]:
        obj.engine.dispose()


@pytest.mark.asyncio
async def test_child_approval_registered_once_and_persisted_before_preparation(tmp_path: Path):
    values = await _child(tmp_path)
    operations, metadata, engineering, handler, payload, registered, child_payload = values
    again = engineering._register_backlog_child_run(payload)
    assert again["issue_approval_id"] == registered["issue_approval_id"]
    loaded = handler._load_confirmed_finding(child_payload)
    assert loaded["status"] == "CONFIRMED_LOADED"
    assert loaded["issue_execution_approval"]["approval_id"] == registered["issue_approval_id"]
    with handler.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Step)) == 1
        change_set = session.get(ChangeSet, UUID(loaded["change_set_id"]))
        assert change_set is not None
        assert (
            change_set.recovery_json["issue_execution_approval"]
            == loaded["issue_execution_approval"]
        )
        assert (
            validate_change_set_issue_approval(session, change_set.id)
            == loaded["issue_execution_approval"]
        )
    assert (
        handler._issue_approval_preparation(loaded)["issue_execution_approval"]
        == loaded["issue_execution_approval"]
    )
    after_load = engineering._register_backlog_child_run(payload)
    assert after_load["issue_approval_id"] == registered["issue_approval_id"]
    _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("eligibility", ["READY", "ELIGIBLE_FOR_REVALIDATION"])
async def test_successful_revalidation_preserves_approval_of_unchanged_issue(
    tmp_path: Path, eligibility: str
):
    values = await _child(tmp_path, eligibility=eligibility)
    operations, metadata, engineering, handler, payload, registered, child_payload = values
    engineering._record_backlog_revalidation(
        {
            **payload,
            "item": {
                "repository": payload["target_repository"],
                "issue_number": payload["target_issue_number"],
            },
            "verification": {"status": "CONFIRMED"},
        }
    )
    with handler.sessions() as session:
        assert session.scalar(select(BacklogIssueRecord)).eligibility_status == "READY"
    result = handler._load_confirmed_finding(child_payload)
    assert result["status"] == "CONFIRMED_LOADED"
    _dispose(values)


@pytest.mark.asyncio
async def test_correction_child_preserves_origin_and_bounded_approval_lineage(tmp_path: Path):
    values = await _child(tmp_path)
    operations, metadata, engineering, handler, payload, registered, child_payload = values
    first = handler._load_confirmed_finding(child_payload)
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, UUID(first["change_set_id"]))
        change_set.state = "REQUEST_CHANGES"
    second_payload = {**payload, "child_workflow_id": "backlog-fixture-issue-1-attempt-2"}
    second = engineering._register_backlog_child_run(second_payload)
    loaded = handler._load_confirmed_finding(
        {
            **child_payload,
            "issue_approval_id": second["issue_approval_id"],
            "orchestration": {"workflow_id": second_payload["child_workflow_id"]},
        }
    )
    assert loaded["status"] == "CONFIRMED_LOADED"
    assert loaded["change_set_id"] == first["change_set_id"]
    assert loaded["issue_execution_approval"] == first["issue_execution_approval"]
    with handler.sessions() as session:
        change_set = session.get(ChangeSet, UUID(loaded["change_set_id"]))
        assert [
            ref["approval_id"]
            for ref in change_set.recovery_json["issue_execution_approval_children"]
        ] == [registered["issue_approval_id"], second["issue_approval_id"]]
        assert session.scalar(select(func.count()).select_from(Step)) == 2
        assert (
            validate_change_set_issue_approval(session, change_set.id)
            == first["issue_execution_approval"]
        )
    _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["missing_id", "wrong_id", "body", "risk", "deferred", "operation_revoked", "record_tampered"],
)
async def test_child_denies_changed_or_missing_approval_before_changeset(
    tmp_path: Path, change: str
):
    values = await _child(tmp_path)
    operations, metadata, engineering, handler, payload, registered, child_payload = values
    if change == "missing_id":
        child_payload.pop("issue_approval_id")
    elif change == "wrong_id":
        child_payload["issue_approval_id"] = str(uuid4())
    else:
        with handler.sessions.begin() as session:
            issue = session.scalar(select(BacklogIssueRecord))
            if change == "body":
                issue.body_sha256 = "9" * 64
            elif change == "risk":
                issue.calculated_risk = "R3"
            elif change == "deferred":
                issue.eligibility_status = "DEFERRED"
            elif change == "operation_revoked":
                session.get(
                    OperationRequest, UUID(payload["issue_operation_id"])
                ).status = "BLOCKED"
            else:
                step = session.get(Step, UUID(registered["issue_approval_id"]))
                data = deepcopy(step.result_json)
                data["issue_number"] = 999
                step.result_json = data
    result = handler._load_confirmed_finding(child_payload)
    assert result["status"] == "BLOCKED"
    with handler.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ChangeSet)) == 0
        run = session.get(Run, UUID(registered["run_id"]))
        assert run.status == "BLOCKED" and run.finished_at is not None
    _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["prepare_maker", "prepare_publication"])
async def test_later_revision_cannot_reach_metadata_preparation(tmp_path: Path, boundary: str):
    values = await _child(tmp_path)
    operations, metadata, engineering, handler, payload, registered, child_payload = values
    loaded = handler._load_confirmed_finding(child_payload)
    with handler.sessions.begin() as session:
        session.scalar(select(BacklogIssueRecord)).body_sha256 = "9" * 64
    before = deepcopy(loaded)
    result = await handler.handle("delivery." + boundary, loaded)
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "DELIVERY_ISSUE_APPROVAL_REQUIRED_OR_CHANGED"
    assert loaded == before
    _dispose(values)


@pytest.mark.asyncio
async def test_changeset_reference_cannot_be_silently_rebound(tmp_path: Path):
    values = await _child(tmp_path)
    operations, metadata, engineering, handler, payload, registered, child_payload = values
    loaded = handler._load_confirmed_finding(child_payload)
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, UUID(loaded["change_set_id"]))
        data = deepcopy(change_set.recovery_json)
        data["issue_execution_approval"]["binding_sha256"] = "0" * 64
        change_set.recovery_json = data
    with pytest.raises(ValueError, match="another issue approval"):
        handler._load_confirmed_finding(child_payload)
    assert (await handler.handle("delivery.prepare_maker", loaded))["status"] == "BLOCKED"
    _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["parent", "state", "lineage_duplicate", "limit"])
async def test_correction_cannot_expand_or_replace_original_authority(tmp_path: Path, change: str):
    values = await _child(tmp_path)
    operations, metadata, engineering, handler, payload, registered, child_payload = values
    first = handler._load_confirmed_finding(child_payload)
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, UUID(first["change_set_id"]))
        change_set.state = "REQUEST_CHANGES" if change != "state" else "IMPLEMENTING"
        if change == "lineage_duplicate":
            data = deepcopy(change_set.recovery_json)
            data["issue_execution_approval_children"] *= 2
            change_set.recovery_json = data
    maximum = payload["correction_limit"] + 1 if change == "limit" else 1
    for attempt in range(2, maximum + 2):
        next_payload = {
            **payload,
            "child_workflow_id": f"backlog-fixture-issue-1-attempt-{attempt}",
        }
        if change == "parent":
            next_payload["orchestration"] = {"workflow_id": "different-parent"}
        child = engineering._register_backlog_child_run(next_payload)
        request = {
            **child_payload,
            "issue_approval_id": child["issue_approval_id"],
            "orchestration": {"workflow_id": next_payload["child_workflow_id"]},
        }
        if attempt <= maximum:
            assert handler._load_confirmed_finding(request)["status"] == "CONFIRMED_LOADED"
        else:
            with pytest.raises(ValueError):
                handler._load_confirmed_finding(request)
    with handler.sessions() as session:
        change_set = session.get(ChangeSet, UUID(first["change_set_id"]))
        assert (
            change_set.recovery_json["issue_execution_approval"]
            == first["issue_execution_approval"]
        )
        assert session.scalar(select(func.count()).select_from(ChangeSet)) == 1
    _dispose(values)
