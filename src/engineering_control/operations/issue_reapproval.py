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

NEW-229: a campaign-wide (``backlog-start``) loop runs under its campaign approval, which
expires after 24 h; its paused ChangeSet could not be continued under a fresh
``backlog-approve`` (custody refused it) nor under issue-reapprove (no issue operation).
The same ``issue-reapprove`` now re-issues such a loop's campaign approval with exactly the
scope its recorded delivery children ran (``_plan_campaign_reapproval``), audited on the
campaign so the NEW-144 custody chain can follow it.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from engineering_control.api.schemas import ExistingBacklogLoopStart
from engineering_control.db.models import (
    BacklogIssueRecord,
    ChangeSet,
    ComponentLease,
    DashboardEvent,
    GitHubBacklogCampaign,
    OperationRequest,
    Run,
    Step,
    WorktreeLease,
)
from engineering_control.engineering.backlog import (
    BACKLOG_PROJECT_PAIRS,
    BacklogApprovalBinding,
)
from engineering_control.engineering.backlog_authority import lapsing_backlog_approval
from engineering_control.operations.correction_extension import (
    LOOP_PAUSE_TARGET_STEP,
    loop_pause_target_id,
)
from engineering_control.operations.issue_approval import (
    campaign_loop_prefix,
    current_issue_approval_binding,
    operation_holds_campaign,
    require_issue_request_approval,
)
from engineering_control.operations.issue_delivery_approval import (
    CHILD_AUTHORITY_MINIMUM_LIFETIME,
    LOOP_REQUEST_STEP,
    REISSUE_EVENT_TYPE,
    STEP_NAME,
    campaign_loop_scope,
    loop_request_id,
)
from engineering_control.operations.issue_start import issue_start_request, issue_workflow_id
from engineering_control.operations.models import SolveIssueCommand, parse_command
from engineering_control.projects import PROJECT_ALTERNATION

REAPPROVAL_EVENT_TYPE = REISSUE_EVENT_TYPE
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ISSUE_LOOP = re.compile(
    rf"backlog-loop-(?P<project>{PROJECT_ALTERNATION})-(?P<campaign>{_UUID})"
    rf"-operation-(?P<operation>{_UUID})"
)
# NEW-229: a campaign-wide loop started by backlog-start (its own start identity last).
_CAMPAIGN_LOOP = re.compile(
    rf"backlog-loop-(?P<project>{PROJECT_ALTERNATION})-(?P<campaign>{_UUID})-(?P<loop>{_UUID})"
)
# Delivery-child Run states that mean the child may still be working.
_ACTIVE_CHILD_STATES = frozenset({"QUEUED", "RUNNING", "RATE_LIMITED", "AWAITING_APPROVAL"})
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


def campaign_loop_identity(loop_workflow_id: str, project: str) -> tuple[UUID, UUID] | None:
    """NEW-229: (campaign_id, loop start id) of a campaign-wide loop of ``project``."""
    match = _CAMPAIGN_LOOP.fullmatch(loop_workflow_id)
    if match is None or match["project"] != project:
        return None
    return UUID(match["campaign"]), UUID(match["loop"])


def loop_reapproval_phrase(loop_workflow_id: str, project: str) -> str | None:
    """issue-reapprove's confirmation for a loop: derived from its issue operation (NEW-143)
    or, for a campaign-wide loop, from the loop's own start identity (NEW-229)."""
    identity = issue_loop_identity(loop_workflow_id, project) or campaign_loop_identity(
        loop_workflow_id, project
    )
    return reapproval_phrase(identity[1]) if identity is not None else None


def _refuse(code: str, detail: str) -> IssueReapprovalBlocked:
    return IssueReapprovalBlocked(f"ISSUE_REAPPROVE_{code}", detail)


def issue_loop_identity(loop_workflow_id: str, project: str) -> tuple[UUID, UUID] | None:
    """(campaign_id, operation_id) of an issue-start loop of ``project``, else None."""
    match = _ISSUE_LOOP.fullmatch(loop_workflow_id)
    if match is None or match["project"] != project:
        return None
    return UUID(match["campaign"]), UUID(match["operation"])


def loop_paused_on(
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
    validated: dict[str, Any] | None = None,
    closed_children: Collection[str] = (),
) -> dict[str, Any]:
    """Every precondition, re-read from durable records; returns the loop's request.

    ``validated`` is an earlier plan's campaign identity: re-run inside the approving
    transaction, the campaign must still have exactly that approval and status.

    ``status`` is REAPPROVE (run issue-start's approval path) or ALREADY_APPROVED (the
    current campaign approval is already this operation's: an idempotent rerun).

    NEW-229: a campaign-wide loop is planned by _plan_campaign_reapproval."""
    match = _ISSUE_LOOP.fullmatch(loop_workflow_id)
    if match is None and campaign_loop_identity(loop_workflow_id, project) is not None:
        return _plan_campaign_reapproval(
            session,
            loop_workflow_id=loop_workflow_id,
            project=project,
            repository=repository,
            issue_number=issue_number,
            confirmation=confirmation,
            current_run_id=current_run_id,
            awaited_issue=awaited_issue,
            validated=validated,
            closed_children=closed_children,
        )
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
    loop_paused_on(session, loop_workflow_id, target, current_run_id, awaited_issue)
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
        plan = {"status": "ALREADY_APPROVED", "request": request, "campaign": campaign.status}
        return _unchanged(plan, campaign, validated)
    if reference is not None and reference.operation_id != str(operation.id):
        other = session.get(OperationRequest, UUID(reference.operation_id))
        # NEW-162 review: a STARTED sibling whose loop has ended no longer holds the campaign.
        if other is not None and operation_holds_campaign(session, other):
            raise _refuse(
                "CAMPAIGN_BOUND_TO_OTHER_ISSUE",
                f"the campaign approval belongs to the open issue operation {other.id}",
            )
    plan = {"status": "REAPPROVE", "request": request, "campaign": campaign.status}
    return _unchanged(plan, campaign, validated)


def _plan_campaign_reapproval(
    session: Session,
    *,
    loop_workflow_id: str,
    project: str,
    repository: str,
    issue_number: int,
    confirmation: str,
    current_run_id: str | None,
    awaited_issue: str | None,
    validated: dict[str, Any] | None,
    closed_children: Collection[str],
) -> dict[str, Any]:
    """NEW-229: plan_issue_reapproval for a campaign-wide loop paused on this issue.

    The request is the one the loop registered with (LOOP_REQUEST_STEP) and every
    delivery child it registered recorded; all must agree, and it must be a campaign-wide
    request of this campaign; the re-issue keeps exactly its scope. Refused while another
    loop of the campaign is open or any child of the campaign may still run: a Run still
    recorded active that the caller did not prove closed in Temporal
    (``closed_children``), or an unreleased, unexpired component lease or unpublished
    worktree lease of a child."""
    identity = campaign_loop_identity(loop_workflow_id, project)
    if identity is None:
        raise _refuse("LOOP_INVALID", "the workflow is not a campaign loop of that project")
    campaign_id, loop_id = identity
    if confirmation != reapproval_phrase(loop_id):
        raise _refuse("CONFIRMATION_MISMATCH", f"confirm with {reapproval_phrase(loop_id)}")
    full_repository = f"gntik-ai/{repository}"
    target = f"{full_repository}#{issue_number}"
    if full_repository not in BACKLOG_PROJECT_PAIRS[project]:
        raise _refuse("OPERATION_MISMATCH", f"{full_repository} is not a {project} repository")
    # The loop's own records of the request it runs: the one it registered with and the
    # one every delivery child approval it registered carries; all must be the same.
    requests = [
        record.result_json.get("request")
        for record in session.scalars(
            select(Step)
            .where(
                Step.name == STEP_NAME,
                Step.result_json["parent_workflow_id"].as_string() == loop_workflow_id,
            )
            .order_by(Step.created_at)
        ).all()
    ]
    loops = session.scalars(
        select(Run.id)
        .where(Run.workflow_id == loop_workflow_id, Run.run_type == "existing-backlog-loop")
        .limit(2)
    ).all()
    started = session.get(Step, loop_request_id(loops[0])) if len(loops) == 1 else None
    if (
        started is not None
        and started.name == LOOP_REQUEST_STEP
        and started.run_id == loops[0]
        and isinstance(started.result_json, dict)
        and started.result_json.get("loop_workflow_id") == loop_workflow_id
    ):
        requests.insert(0, started.result_json.get("request"))
    if not requests:
        raise _refuse(
            "NO_CUSTODY",
            "the loop recorded no request (it started before NEW-229 and no delivery child "
            "holds a ChangeSet); renew the campaign approval with backlog-approval-candidate "
            "and backlog-approve, then retry",
        )
    expected = requests[0]
    if not isinstance(expected, dict) or any(item != expected for item in requests):
        raise _refuse("LOOP_REQUEST_MISMATCH", "the loop ran a different request")
    try:
        request = ExistingBacklogLoopStart.model_validate(expected)
    except ValueError as error:
        raise _refuse("LOOP_REQUEST_MISMATCH", "the loop's recorded request is invalid") from error
    if (
        request.model_dump(mode="json") != expected
        or campaign_loop_scope(expected) is None
        or request.project != project
        or request.backlog_campaign_id != campaign_id
    ):
        raise _refuse("LOOP_REQUEST_MISMATCH", "the loop is not a campaign-wide loop of it")
    loop_paused_on(session, loop_workflow_id, target, current_run_id, awaited_issue)
    campaign = session.get(GitHubBacklogCampaign, campaign_id)
    if campaign is None or campaign.project != project:
        raise _refuse("POLICY_UNAVAILABLE", "the campaign has no recorded approval policy")
    issue = session.scalar(
        select(BacklogIssueRecord).where(
            BacklogIssueRecord.backlog_campaign_id == campaign_id,
            BacklogIssueRecord.repository == full_repository,
            BacklogIssueRecord.issue_number == issue_number,
        )
    )
    maximum = request.maximum_risk.value
    if issue is None or _RISK_RANK.get(issue.calculated_risk, 99) > _RISK_RANK[maximum]:
        raise _refuse("POLICY_EXCEEDED", f"the issue is outside the loop's approved {maximum}")
    prefix = campaign_loop_prefix(project, campaign_id)
    others = session.scalars(
        select(Run.workflow_id).where(
            Run.run_type == "existing-backlog-loop",
            Run.status.in_(_OPEN_LOOP_STATES),
            Run.workflow_id.startswith(prefix, autoescape=True),
            Run.workflow_id != loop_workflow_id,
        )
    ).all()
    if others:
        raise _refuse("CAMPAIGN_IN_USE", f"another loop of the campaign is open ({others[0]})")
    # A paused parent does not prove its children stopped (NEW-168 review): every child
    # recorded active must be proven closed in Temporal by the caller.
    unproven = [
        name for name in campaign_children(session, loop_workflow_id) if name not in closed_children
    ]
    if unproven:
        raise _refuse("CHILD_ACTIVE", f"the campaign's child {unproven[0]} may still be running")
    children = (
        select(Run.id)
        .where(
            Run.workflow_id.startswith(f"{prefix}-", autoescape=True),
            Run.run_type != "existing-backlog-loop",
        )
        .scalar_subquery()
    )
    now = datetime.now(UTC)
    component = session.scalar(
        select(ComponentLease.id)
        .where(
            ComponentLease.owner_run_id.in_(children),
            ComponentLease.released_at.is_(None),
            ComponentLease.expires_at > now,
        )
        .limit(1)
    )
    # A published ChangeSet keeps its worktree lease for the release path (NEW-32); only
    # an unpublished one's lease means a child may still be writing.
    worktree = session.scalar(
        select(WorktreeLease.id)
        .join(ChangeSet, ChangeSet.id == WorktreeLease.change_set_id)
        .where(
            WorktreeLease.owner_run_id.in_(children),
            WorktreeLease.released_at.is_(None),
            WorktreeLease.expires_at > now,
            ChangeSet.source_pr_url.is_(None),
        )
        .limit(1)
    )
    if component is not None or worktree is not None:
        raise _refuse("CHILD_ACTIVE", "a child of the campaign still holds a lease")
    current = lapsing_backlog_approval(campaign)
    if (
        current is not None
        and _utc(current.expires_at) - now >= CHILD_AUTHORITY_MINIMUM_LIFETIME
        and campaign.status in _APPROVED_CAMPAIGN_STATES
        and isinstance(campaign.approval_json.get("approval"), dict)
        and _campaign_reissue_event(session, loop_workflow_id, campaign) is not None
    ):
        plan = {"status": "ALREADY_APPROVED", "request": request, "campaign": campaign.status}
        return _unchanged(plan, campaign, validated)
    plan = {"status": "REAPPROVE", "request": request, "campaign": campaign.status}
    return _unchanged(plan, campaign, validated)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# More children than this cannot be proven closed and refuse (as NEW-168's bound).
MAX_PROVEN_CHILDREN = 256


def campaign_children(session: Session, loop_workflow_id: str) -> list[str]:
    """NEW-229: the child workflow IDs a campaign loop's re-issue must see proven closed
    in Temporal: every child of the loop, whatever its recorded status (a child reset in
    Temporal may run again; NEW-168 review), and every child of another loop of the
    campaign still recorded active (those loops are closed, which ends their children)."""
    match = _CAMPAIGN_LOOP.fullmatch(loop_workflow_id)
    if match is None:
        raise _refuse("LOOP_INVALID", "the workflow is not a campaign loop")
    prefix = campaign_loop_prefix(match["project"], match["campaign"])
    names = session.scalars(
        select(Run.workflow_id)
        .where(
            Run.run_type != "existing-backlog-loop",
            or_(
                Run.workflow_id.startswith(f"{loop_workflow_id}-", autoescape=True),
                and_(
                    Run.workflow_id.startswith(f"{prefix}-", autoescape=True),
                    Run.status.in_(_ACTIVE_CHILD_STATES),
                ),
            ),
        )
        .distinct()
        .order_by(Run.workflow_id)
        .limit(MAX_PROVEN_CHILDREN + 1)
    ).all()
    if len(names) > MAX_PROVEN_CHILDREN:
        raise _refuse("CHILD_ACTIVE", "too many campaign children to verify as closed")
    return [str(name) for name in names]


def _campaign_reissue_event(
    session: Session, loop_workflow_id: str, campaign: GitHubBacklogCampaign
) -> DashboardEvent | None:
    """NEW-229: the audit event of the campaign's current approval re-issued for the loop."""
    return session.scalar(
        select(DashboardEvent)
        .where(
            DashboardEvent.event_type == REAPPROVAL_EVENT_TYPE,
            DashboardEvent.entity_type == "backlog_campaign",
            DashboardEvent.entity_id == str(campaign.id),
            DashboardEvent.workflow_id == loop_workflow_id,
            DashboardEvent.metadata_json["approval_binding"].as_string()
            == campaign.approval_binding,
        )
        .limit(1)
    )


def _unchanged(
    plan: dict[str, Any], campaign: GitHubBacklogCampaign, validated: dict[str, Any] | None
) -> dict[str, Any]:
    plan["approval_binding"] = campaign.approval_binding
    if validated is not None and (
        plan["status"] != validated["status"]
        or plan["campaign"] != validated["campaign"]
        or plan["approval_binding"] != validated["approval_binding"]
    ):
        raise _refuse(
            "CAMPAIGN_CHANGED",
            "the campaign approval or ownership changed since it was validated; run again",
        )
    return plan


def record_issue_reapproval(
    session: Session,
    *,
    loop_workflow_id: str,
    operation_id: UUID | None,
    issue_ref: str,
    approval_binding: str,
    approval_status: str,
    actor: str,
    issue_approval_binding: str | None = None,
    loop_scope: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> int:
    """Audit one re-approval (idempotent per resulting campaign approval binding).

    NEW-229: without an issue operation (a campaign-wide loop) the event is recorded on
    the campaign, for the loop, with the exact scope it re-issued (``loop_scope``)."""
    campaign_id = _ISSUE_LOOP.fullmatch(loop_workflow_id) or _CAMPAIGN_LOOP.fullmatch(
        loop_workflow_id
    )
    if operation_id is None and (campaign_id is None or loop_scope is None):
        raise ValueError("a campaign approval re-issue requires its loop and scope")
    entity_type, entity_id = (
        ("operation_request", str(operation_id))
        if operation_id is not None or campaign_id is None
        else ("backlog_campaign", campaign_id["campaign"])
    )
    existing = session.scalar(
        select(DashboardEvent).where(
            DashboardEvent.event_type == REAPPROVAL_EVENT_TYPE,
            DashboardEvent.entity_id == entity_id,
            DashboardEvent.metadata_json["approval_binding"].as_string() == approval_binding,
            *(
                ()
                if operation_id is not None
                else (
                    DashboardEvent.entity_type == entity_type,
                    DashboardEvent.workflow_id == loop_workflow_id,
                )
            ),
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
        entity_type=entity_type,
        entity_id=entity_id,
        current_phase="ISSUE_APPROVAL_REISSUED",
        message=(
            "Per-issue campaign approval re-created for the existing issue operation"
            if operation_id is not None
            else "Campaign approval re-issued with the paused campaign loop's own scope"
        ),
        metadata_json={
            "approved_by": actor,
            "repository": repository,
            "approval_binding": approval_binding,
            "approval_status": approval_status,
            # NEW-144: the operation binding the re-issue continues (custody link).
            **(
                {"issue_approval_binding": issue_approval_binding} if issue_approval_binding else {}
            ),
            # NEW-229: the campaign-wide loop scope the re-issue kept (custody link).
            **({"loop_scope": loop_scope} if operation_id is None else {}),
        },
    )
    session.add(event)
    session.flush()
    return int(event.id)
