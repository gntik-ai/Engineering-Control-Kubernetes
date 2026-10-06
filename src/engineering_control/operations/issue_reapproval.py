"""NEW-143: re-create the voided per-issue campaign approval of an issue-start loop.

issue-start approves its backlog campaign for exactly one issue operation
(``BacklogApprovalBinding.issue_approval``). Pausing the campaign (``backlog-pause``) voids
that approval, and the general ``backlog-approve`` re-approves the campaign without the
issue reference, so the loop's next delivery child is refused ("direct child campaign
approval differs from its operation"). A fresh issue-start cannot replace the loop either:
its ChangeSet already belongs to the existing operation's approval.

``issue-reapprove`` rebuilds the loop's original request from durable records (the
operation's stored command and binding, cross-checked against the loop's recorded child
approvals) and re-runs issue-start's own approval path for it. This module only plans and
audits; TypedCommandBus.reapprove_issue performs the approval with MetadataRepository.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.db.models import (
    BacklogIssueRecord,
    DashboardEvent,
    GitHubBacklogCampaign,
    OperationRequest,
    Run,
    Step,
)
from engineering_control.engineering.backlog import (
    BACKLOG_PROJECT_PAIRS,
    BacklogApprovalBinding,
)
from engineering_control.operations.correction_extension import (
    LOOP_PAUSE_TARGET_STEP,
    loop_pause_target_id,
)
from engineering_control.operations.issue_approval import (
    current_issue_approval_binding,
    require_issue_request_approval,
)
from engineering_control.operations.issue_delivery_approval import STEP_NAME
from engineering_control.operations.issue_start import issue_start_request, issue_workflow_id
from engineering_control.operations.models import SolveIssueCommand, parse_command

REAPPROVAL_EVENT_TYPE = "issue_approval_reissued"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ISSUE_LOOP = re.compile(
    rf"backlog-loop-(?P<project>falcone|musematic|llmwiki)-(?P<campaign>{_UUID})"
    rf"-operation-(?P<operation>{_UUID})"
)
_OPEN_OPERATION_STATES = frozenset({"DISPATCHING", "STARTED"})
_APPROVED_CAMPAIGN_STATES = frozenset(
    {"APPROVED_POLICY_PENDING", "APPROVED_INVENTORY", "APPROVED", "RUNNING"}
)
_OPEN_LOOP_STATES = frozenset({"QUEUED", "RUNNING", "PAUSED", "BLOCKED", "RATE_LIMITED"})
_RISK_RANK = {"R0": 0, "R1": 1, "R2": 2, "R3": 3}


class IssueReapprovalBlocked(ValueError):
    """A refused re-approval: a stable blocker code and operator guidance."""

    def __init__(self, blocker: str, detail: str) -> None:
        super().__init__(f"{blocker}: {detail}")
        self.blocker = blocker
        self.detail = detail


def reapproval_phrase(operation_id: UUID) -> str:
    return f"REAPPROVE_ISSUE_{operation_id.hex[:8].upper()}"


def _refuse(code: str, detail: str) -> IssueReapprovalBlocked:
    return IssueReapprovalBlocked(f"ISSUE_REAPPROVE_{code}", detail)


def _loop_paused_on(
    session: Session,
    loop_workflow_id: str,
    target: str,
    current_run_id: str | None,
    awaited_issue: str | None,
) -> Run:
    """The loop Run, proven PAUSED on ``target`` by the open execution.

    Its durable pause record (NEW-133) decides when the current Temporal run wrote it;
    a loop paused before that record existed is proven by the open run's own
    AWAITING_OPERATOR status query instead. Both come from the server, never the client."""
    loops = session.scalars(
        select(Run)
        .where(Run.workflow_id == loop_workflow_id, Run.run_type == "existing-backlog-loop")
        .limit(2)
    ).all()
    if len(loops) != 1 or loops[0].status != "PAUSED" or not current_run_id:
        raise _refuse("LOOP_NOT_PAUSED", "the issue loop is not open and PAUSED")
    loop = loops[0]
    pause = session.get(Step, loop_pause_target_id(loop.id))
    value = pause.result_json if pause is not None else None
    fence = value.get("fence") if isinstance(value, dict) else None
    if (
        pause is not None
        and pause.name == LOOP_PAUSE_TARGET_STEP
        and pause.run_id == loop.id
        and isinstance(value, dict)
        and isinstance(fence, dict)
        and fence.get("workflow_run_id") == current_run_id
    ):
        if value.get("loop_status") != "PAUSED" or value.get("issue") != target:
            raise _refuse("LOOP_NOT_PAUSED", f"the issue loop is not paused on {target}")
        return loop
    if awaited_issue != target:
        raise _refuse("LOOP_NOT_PAUSED", f"the issue loop is not awaiting the operator on {target}")
    return loop


def plan_issue_reapproval(
    session: Session,
    *,
    loop_workflow_id: str,
    project: str,
    repository: str,
    issue_number: int,
    confirmation: str,
    current_run_id: str | None,
    awaited_issue: str | None,
) -> dict[str, Any]:
    """Every precondition, re-read from durable records; returns the loop's request.

    ``status`` is REAPPROVE (run issue-start's approval path) or ALREADY_APPROVED (the
    current campaign approval is already this operation's: an idempotent rerun)."""
    match = _ISSUE_LOOP.fullmatch(loop_workflow_id)
    if match is None or match["project"] != project:
        raise _refuse("LOOP_INVALID", "the workflow is not an issue-start loop of that project")
    operation_id = UUID(match["operation"])
    campaign_id = UUID(match["campaign"])
    if confirmation != reapproval_phrase(operation_id):
        raise _refuse("CONFIRMATION_MISMATCH", f"confirm with {reapproval_phrase(operation_id)}")
    full_repository = f"gntik-ai/{repository}"
    target = f"{full_repository}#{issue_number}"
    if full_repository not in BACKLOG_PROJECT_PAIRS[project]:
        raise _refuse("OPERATION_MISMATCH", f"{full_repository} is not a {project} repository")
    operation = session.get(OperationRequest, operation_id)
    command = parse_command(operation.payload_json) if operation is not None else None
    if (
        operation is None
        or not isinstance(command, SolveIssueCommand)
        or operation.action != "solve_issue"
        or operation.target_type != "github_issue"
        or operation.project != project
        or operation.target_id != target
        or command.project.value != project
        or command.backlog_campaign_id != campaign_id
        or command.repository != full_repository
        or command.issue_number != issue_number
        or (operation.workflow_id is not None and operation.workflow_id != loop_workflow_id)
    ):
        raise _refuse("OPERATION_MISMATCH", "the loop's issue operation does not match")
    if operation.status not in _OPEN_OPERATION_STATES:
        raise _refuse("OPERATION_FINISHED", f"the issue operation is {operation.status}")
    request = issue_start_request(command, operation.id, operation.binding_hash)
    if issue_workflow_id(request) != loop_workflow_id:
        raise _refuse("OPERATION_MISMATCH", "the loop is not this operation's issue loop")
    try:
        current = current_issue_approval_binding(session, command)
    except ValueError as error:
        raise _refuse("STALE_BINDING", str(error)) from error
    if current != operation.binding_hash:
        raise _refuse(
            "STALE_BINDING",
            "the issue changed since the operation was approved; amend it first (issue-amend)",
        )
    # The loop's own records of the request it runs: every delivery child approval it
    # registered carries it, and the rebuilt request must be exactly that one.
    recorded = session.scalars(
        select(Step).where(
            Step.name == STEP_NAME,
            Step.result_json["parent_workflow_id"].as_string() == loop_workflow_id,
        )
    ).all()
    expected = request.model_dump(mode="json")
    if any(record.result_json.get("request") != expected for record in recorded):
        raise _refuse("LOOP_REQUEST_MISMATCH", "the loop ran a different request")
    _loop_paused_on(session, loop_workflow_id, target, current_run_id, awaited_issue)
    campaign = session.get(GitHubBacklogCampaign, campaign_id)
    raw = campaign.approval_json.get("binding") if campaign is not None else None
    if campaign is None or campaign.project != project or not isinstance(raw, dict):
        raise _refuse("POLICY_UNAVAILABLE", "the campaign has no recorded approval policy")
    try:
        binding = BacklogApprovalBinding.model_validate(raw)
    except ValueError as error:
        raise _refuse("POLICY_UNAVAILABLE", "the campaign approval policy is malformed") from error
    issue = session.scalar(
        select(BacklogIssueRecord).where(
            BacklogIssueRecord.backlog_campaign_id == campaign_id,
            BacklogIssueRecord.repository == full_repository,
            BacklogIssueRecord.issue_number == issue_number,
        )
    )
    maximum = command.maximum_risk.value
    if (
        issue is None
        or binding.delivery_mode != command.delivery_mode
        or _RISK_RANK[maximum] > _RISK_RANK[binding.maximum_risk]
        or _RISK_RANK.get(issue.calculated_risk, 99) > _RISK_RANK[maximum]
    ):
        raise _refuse(
            "POLICY_EXCEEDED",
            f"the operation ({maximum}, {command.delivery_mode}) is outside the campaign's "
            f"approved policy ({binding.maximum_risk}, {binding.delivery_mode})",
        )
    try:
        require_issue_request_approval(session, request)
    except ValueError as error:
        raise _refuse("OPERATION_APPROVAL_INVALID", str(error)) from error
    others = session.scalars(
        select(Run.workflow_id).where(
            Run.run_type == "existing-backlog-loop",
            Run.status.in_(_OPEN_LOOP_STATES),
            Run.workflow_id.startswith(f"backlog-loop-{project}-{campaign_id}", autoescape=True),
            Run.workflow_id != loop_workflow_id,
        )
    ).all()
    if others:
        raise _refuse("CAMPAIGN_IN_USE", f"another loop of the campaign is open ({others[0]})")
    reference = binding.issue_approval
    if (
        reference is not None
        and reference.operation_id == str(operation.id)
        and reference.binding_sha256 == operation.binding_hash
        and campaign.approval_binding == binding.sha256()
        and campaign.status in _APPROVED_CAMPAIGN_STATES
        and isinstance(campaign.approval_json.get("approval"), dict)
    ):
        return {"status": "ALREADY_APPROVED", "request": request, "campaign": campaign.status}
    if reference is not None and reference.operation_id != str(operation.id):
        other = session.get(OperationRequest, UUID(reference.operation_id))
        if other is not None and other.status in _OPEN_OPERATION_STATES:
            raise _refuse(
                "CAMPAIGN_BOUND_TO_OTHER_ISSUE",
                f"the campaign approval belongs to the open issue operation {other.id}",
            )
    return {"status": "REAPPROVE", "request": request, "campaign": campaign.status}


def record_issue_reapproval(
    session: Session,
    *,
    loop_workflow_id: str,
    operation_id: UUID,
    issue_ref: str,
    approval_binding: str,
    approval_status: str,
    actor: str,
    now: datetime | None = None,
) -> int:
    """Audit one re-approval (idempotent per resulting campaign approval binding)."""
    campaign_id = _ISSUE_LOOP.fullmatch(loop_workflow_id)
    existing = session.scalar(
        select(DashboardEvent).where(
            DashboardEvent.event_type == REAPPROVAL_EVENT_TYPE,
            DashboardEvent.entity_id == str(operation_id),
            DashboardEvent.metadata_json["approval_binding"].as_string() == approval_binding,
        )
    )
    if existing is not None:
        return int(existing.id)
    repository, _, number = issue_ref.partition("#")
    event = DashboardEvent(
        occurred_at=now or datetime.now(UTC),
        level="WARNING",
        event_type=REAPPROVAL_EVENT_TYPE,
        source="controller",
        host=None,
        worker_kind="controller",
        project=campaign_id["project"] if campaign_id else None,
        workflow_id=loop_workflow_id,
        temporal_run_id=None,
        issue_number=int(number),
        campaign_id=campaign_id["campaign"] if campaign_id else None,
        entity_type="operation_request",
        entity_id=str(operation_id),
        current_phase="ISSUE_APPROVAL_REISSUED",
        message="Per-issue campaign approval re-created for the existing issue operation",
        metadata_json={
            "approved_by": actor,
            "repository": repository,
            "approval_binding": approval_binding,
            "approval_status": approval_status,
        },
    )
    session.add(event)
    session.flush()
    return int(event.id)
