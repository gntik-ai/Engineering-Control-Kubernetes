from __future__ import annotations

import hmac
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.api.schemas import ExistingBacklogLoopStart
from engineering_control.db.models import (
    BacklogIssueRecord,
    GitHubBacklogCampaign,
    OperationRequest,
)
from engineering_control.engineering.backlog import (
    BACKLOG_PROJECT_PAIRS,
    BacklogApprovalBinding,
    canonical_sha256,
)
from engineering_control.operations.models import SolveIssueCommand, parse_command


def current_issue_approval_binding(session: Session, command: SolveIssueCommand) -> str:
    """Hash the authoritative issue snapshot; never include incidental owner IDs."""
    campaign = session.get(GitHubBacklogCampaign, command.backlog_campaign_id)
    issue = session.scalar(
        select(BacklogIssueRecord).where(
            BacklogIssueRecord.backlog_campaign_id == command.backlog_campaign_id,
            BacklogIssueRecord.repository == command.repository,
            BacklogIssueRecord.issue_number == command.issue_number,
        )
    )
    if (
        campaign is None
        or issue is None
        or campaign.project != command.project.value
        or command.repository not in BACKLOG_PROJECT_PAIRS[command.project.value]
    ):
        raise ValueError("issue approval target is not in its authoritative project backlog")
    return canonical_sha256(
        {
            "action": command.action,
            "target_type": "github_issue",
            "target_id": f"{command.repository}#{command.issue_number}",
            "project": command.project.value,
            "payload": command.model_dump(mode="json"),
            "resolved": {
                "backlog_campaign_id": str(campaign.id),
                "source_repository": campaign.source_repository,
                "deployment_repository": campaign.deployment_repository,
                "source_head": campaign.source_head_at_import,
                "deployment_head": campaign.deployment_head_at_import,
                "plan_hash": campaign.plan_hash,
                "issue_title": issue.title,
                "issue_state": issue.state,
                "issue_body_sha256": issue.body_sha256,
                "risk": issue.calculated_risk,
                "eligibility": issue.eligibility_status,
            },
            "production": False,
        }
    )


def require_issue_operation_approval(
    session: Session,
    operation_id: UUID,
    expected_binding: str,
    *,
    project: str,
    campaign_id: UUID,
    repository: str,
    issue_number: int,
    delivery_mode: str,
    maximum_risk: str,
    correction_limit: int,
) -> dict[str, Any]:
    """Verify the durable originating operation at a controller-side boundary."""
    operation = session.get(OperationRequest, operation_id)
    if (
        operation is None
        or operation.action != "solve_issue"
        or operation.target_type != "github_issue"
        or operation.status not in {"DISPATCHING", "STARTED"}
        or not hmac.compare_digest(operation.binding_hash, expected_binding)
    ):
        raise ValueError("durable issue approval operation is unavailable or changed")
    command = parse_command(operation.payload_json)
    if not isinstance(command, SolveIssueCommand) or (
        command.project.value != project
        or command.backlog_campaign_id != campaign_id
        or command.repository != repository
        or command.issue_number != issue_number
        or command.delivery_mode != delivery_mode
        or command.maximum_risk.value != maximum_risk
        or command.correction_limit != correction_limit
        or operation.target_id != f"{repository}#{issue_number}"
        or operation.project != project
    ):
        raise ValueError("durable issue approval execution target changed")
    if not hmac.compare_digest(current_issue_approval_binding(session, command), expected_binding):
        raise ValueError("issue approval snapshot changed; prepare and confirm a new action")
    return {"issue_operation_id": str(operation.id), "issue_approval_binding": expected_binding}


def require_issue_request_approval(session: Session, request: ExistingBacklogLoopStart) -> None:
    # Revalidate even internally constructed/model_copy inputs at this boundary.
    request = ExistingBacklogLoopStart.model_validate(request.model_dump(mode="json"))
    if request.issue_operation_id is None or request.issue_approval_binding is None:
        raise ValueError("direct issue execution requires its durable operation approval")
    if request.target_repository is None or request.target_issue_number is None:
        raise ValueError("durable issue approval requires an exact issue target")
    require_issue_operation_approval(
        session,
        request.issue_operation_id,
        request.issue_approval_binding,
        project=request.project,
        campaign_id=request.backlog_campaign_id,
        repository=request.target_repository,
        issue_number=request.target_issue_number,
        delivery_mode=request.delivery_mode,
        maximum_risk=request.maximum_risk.value,
        correction_limit=request.correction_limit,
    )


def require_backlog_issue_approval(session: Session, binding: BacklogApprovalBinding) -> None:
    """Check the issue reference covered by the campaign's immutable approval hash."""
    reference = binding.issue_approval
    if reference is None:
        return  # Historical campaign approvals keep their separate policy contract.
    operation = session.get(OperationRequest, UUID(reference.operation_id))
    if operation is None:
        raise ValueError("backlog issue operation is unavailable")
    command = parse_command(operation.payload_json)
    if not isinstance(command, SolveIssueCommand):
        raise ValueError("backlog issue operation is not a direct issue command")
    require_issue_operation_approval(
        session,
        operation.id,
        reference.binding_sha256,
        project=binding.project,
        campaign_id=UUID(binding.backlog_campaign_id),
        repository=command.repository,
        issue_number=command.issue_number,
        delivery_mode=binding.delivery_mode,
        maximum_risk=binding.maximum_risk,
        correction_limit=command.correction_limit,
    )
