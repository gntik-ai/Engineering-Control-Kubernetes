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
    Run,
)
from engineering_control.engineering.backlog import (
    BACKLOG_PROJECT_PAIRS,
    BacklogApprovalBinding,
    canonical_sha256,
)
from engineering_control.engineering.backlog_authority import _valid_r3_candidate
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
    execution_options: tuple[str, str | None] | None = None,
) -> dict[str, Any]:
    """Verify the durable originating operation at a controller-side boundary.

    ``execution_options`` (runtime proof, R3 approval binding) are compared when the
    caller carries them, so an execution cannot change the operation's approved options.
    """
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
    if execution_options is not None and execution_options != (
        command.runtime_proof,
        command.r3_approval_binding,
    ):
        raise ValueError("durable issue approval execution options changed")
    if not hmac.compare_digest(current_issue_approval_binding(session, command), expected_binding):
        raise ValueError("issue approval snapshot changed; prepare and confirm a new action")
    if command.r3_approval_binding is not None:
        # NEW-112 review: an operation-bound R3 binding is re-verified at every boundary
        # (delivery approval and publication custody), not just compared as a string: the
        # exact used candidate must still be unexpired and bound to this issue's current
        # body and the campaign's imported heads.
        issue = session.scalar(
            select(BacklogIssueRecord).where(
                BacklogIssueRecord.backlog_campaign_id == command.backlog_campaign_id,
                BacklogIssueRecord.repository == command.repository,
                BacklogIssueRecord.issue_number == command.issue_number,
            )
        )
        if (
            issue is None
            or _valid_r3_candidate(session, issue, command.r3_approval_binding) is None
        ):
            raise ValueError("R3 approval candidate expired or changed; approve it again")
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
        execution_options=(request.runtime_proof, request.r3_approval_binding),
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


# NEW-162: loop Run states that still hold their campaign (issue_reapproval keeps the same).
OPEN_LOOP_STATES = frozenset({"QUEUED", "RUNNING", "PAUSED", "BLOCKED", "RATE_LIMITED"})
CAMPAIGN_IN_USE = "ISSUE_START_CAMPAIGN_IN_USE"


def campaign_loop_prefix(project: str, campaign_id: UUID | str) -> str:
    return f"backlog-loop-{project}-{campaign_id}"


def open_campaign_loops(session: Session, prefix: str, *, exclude: str | None) -> list[str]:
    """The campaign's loop Runs (issue-start and campaign-wide) still recorded open."""
    statement = select(Run.workflow_id).where(
        Run.run_type == "existing-backlog-loop",
        Run.status.in_(OPEN_LOOP_STATES),
        Run.workflow_id.startswith(prefix, autoescape=True),
    )
    if exclude is not None:
        statement = statement.where(Run.workflow_id != exclude)
    return list(session.scalars(statement.order_by(Run.workflow_id)).all())


def operation_holds_campaign(session: Session, operation: OperationRequest) -> bool:
    """NEW-162: an issue operation holds its campaign while it dispatches and, once STARTED,
    until its loop Run ends (the operation itself is closed lazily). A STARTED operation
    whose loop has not registered a Run is still being handed off."""
    if operation.status == "DISPATCHING":
        return True
    if operation.status != "STARTED":
        return False
    if not operation.workflow_id:
        return True
    status = session.scalar(select(Run.status).where(Run.workflow_id == operation.workflow_id))
    return status is None or status in OPEN_LOOP_STATES


def require_no_open_sibling_loop(
    session: Session,
    campaign: GitHubBacklogCampaign,
    project: str,
    own_operation_id: UUID | None,
) -> None:
    """NEW-162: a campaign holds at most one open loop (operator decision 2026-10-08).

    A campaign approval names exactly one issue operation, and every loop reports its
    state into the one campaign status. Approving another issue while a sibling loop was
    open (falcone 31c772b3) voided the sibling's approval, let its pause void the new
    loop's delivery and let its end disable the shared write policy. Checked for every
    approval preparation, issue-bound or campaign-wide, under the approval and campaign
    locks; only the request's own issue loop (issue-reapprove of a paused loop) never
    counts."""
    prefix = campaign_loop_prefix(project, campaign.id)
    own_loop = f"{prefix}-operation-{own_operation_id}" if own_operation_id else None
    loops = open_campaign_loops(session, prefix, exclude=own_loop)
    if loops:
        raise ValueError(
            f"{CAMPAIGN_IN_USE}: another loop of the campaign is open ({loops[0]}); "
            "finish, skip or cancel it before starting another issue"
        )
    approval = campaign.approval_json if isinstance(campaign.approval_json, dict) else {}
    raw = approval.get("binding")
    if not isinstance(raw, dict) or not isinstance(raw.get("issue_approval"), dict):
        return
    other_id = str(raw["issue_approval"].get("operation_id", ""))
    if own_operation_id is not None and other_id == str(own_operation_id):
        return
    try:
        other = session.get(OperationRequest, UUID(other_id))
    except ValueError as error:
        raise ValueError(f"{CAMPAIGN_IN_USE}: the campaign approval is malformed") from error
    if other is not None and operation_holds_campaign(session, other):
        raise ValueError(
            f"{CAMPAIGN_IN_USE}: the campaign approval belongs to the open issue operation "
            f"{other.id}; finish, skip or cancel it before starting another issue"
        )
