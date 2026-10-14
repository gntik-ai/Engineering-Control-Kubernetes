"""NEW-168: operator reset of a stuck, never-published delivery ChangeSet.

A delivery attempt can stop with its ChangeSet in a state no bounded retry child may
start from (PLANNED/IMPLEMENTING), so every ``backlog-retry`` was refused with "another
delivery child requires an explicit correction state" and the operator hand-wrote a
guarded SQL UPDATE (ops-history db-fix-2026100*-cs*.log). ``change-reset`` replaces that
UPDATE for an issue-start loop that is PAUSED on the ChangeSet's issue: it moves the
ChangeSet to REQUEST_CHANGES (as NEW-137 does after a provider outage), so the loop's next
backlog-retry binds a child counted in the bounded correction lineage (no bypass).

It never moves a ChangeSet's base: a ChangeSet pinned to an old base needs a fresh
backlog import and issue-start (a new finding, revalidated at the current heads).

Every precondition is re-read here from durable records under the Finding and ChangeSet
row locks (the order delivery.load_confirmed_finding and publication custody use); the
caller additionally proves in Temporal that every delivery child is closed. The reset is
audited as a DashboardEvent in the same transaction.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from engineering_control.db.models import (
    BacklogIssueRecord,
    ChangeSet,
    ComponentLease,
    DashboardEvent,
    Finding,
    Run,
    WorktreeLease,
)
from engineering_control.engineering.backlog import BACKLOG_PROJECT_PAIRS
from engineering_control.models.enums import ChangeSetState
from engineering_control.operations.change_set_publication import (
    CORRECTABLE_STATES,
    PUBLISHED_RECOVERY_KEYS,
    published_reason,
    request_changes,
)
from engineering_control.operations.issue_amend import change_set_unstarted
from engineering_control.operations.issue_delivery_approval import (
    correction_attempt_budget,
    correction_lineage,
    validate_change_set_issue_approval,
)
from engineering_control.operations.issue_reapproval import (
    IssueReapprovalBlocked,
    issue_loop_identity,
    loop_paused_on,
)

RESET_EVENT_TYPE = "change_set_reset"
OPERATOR_RESET = "OPERATOR_RESET"
RESET_RECORD_KEY = "operator_reset"
# Only states before any publication (REQUEST_CHANGES and an unstarted CONFIRMED
# ChangeSet are already retryable, see below).
_RESETTABLE_STATES = CORRECTABLE_STATES
# The delivery children whose closure the caller must prove in Temporal (bounded).
MAX_DELIVERY_CHILDREN = 256
# NEW-201: the publication proof moved to change_set_publication (shared with the
# in-place issue amendment); kept here by name.
_PUBLISHED_RECOVERY_KEYS = PUBLISHED_RECOVERY_KEYS
_published = published_reason


class ChangeResetBlocked(ValueError):
    """A refused reset: a stable blocker code and operator guidance."""

    def __init__(self, blocker: str, detail: str) -> None:
        super().__init__(f"{blocker}: {detail}")
        self.blocker = blocker
        self.detail = detail


def reset_phrase(change_set_id: UUID) -> str:
    return f"RESET_CHANGESET_{change_set_id.hex[:8].upper()}"


def _refuse(code: str, detail: str) -> ChangeResetBlocked:
    return ChangeResetBlocked(f"CHANGE_RESET_{code}", detail)


def delivery_children(session: Session, loop_workflow_id: str, change_set_id: UUID) -> list[str]:
    """The workflow IDs of every Run bound to this ChangeSet or started as one of the
    loop's issue children; the caller proves each closed in Temporal (NEW-168 review).

    More than MAX_DELIVERY_CHILDREN cannot be verified and refuses (fail closed)."""
    prefix = f"{loop_workflow_id}-issue-"
    names = session.scalars(
        select(Run.workflow_id)
        .where(
            or_(
                Run.change_set_id == change_set_id,
                Run.workflow_id.startswith(prefix, autoescape=True),
            ),
            Run.workflow_id != loop_workflow_id,
        )
        .distinct()
        .limit(MAX_DELIVERY_CHILDREN + 1)
    ).all()
    if len(names) > MAX_DELIVERY_CHILDREN:
        raise _refuse("DELIVERY_ACTIVE", "too many delivery runs to verify as closed")
    return sorted(str(name) for name in names)


def _active_leases(
    session: Session, change_set_id: UUID, children: list[str], now: datetime
) -> str | None:
    """Why a lease of this ChangeSet or of its delivery runs is still held, or None."""
    worktree = session.scalar(
        select(WorktreeLease.id)
        .where(
            WorktreeLease.change_set_id == change_set_id,
            WorktreeLease.released_at.is_(None),
            WorktreeLease.expires_at > now,
        )
        .limit(1)
    )
    if worktree is not None:
        return "a worktree lease of the ChangeSet is still held"
    owners = select(Run.id).where(Run.workflow_id.in_(children)) if children else None
    if owners is None:
        return None
    component = session.scalar(
        select(ComponentLease.id)
        .where(
            ComponentLease.owner_run_id.in_(owners),
            ComponentLease.released_at.is_(None),
            ComponentLease.expires_at > now,
        )
        .limit(1)
    )
    if component is not None:
        return "a component lease of a delivery run is still held"
    return None


def reset_change_set(
    session: Session,
    *,
    loop_workflow_id: str,
    project: str,
    repository: str,
    issue_number: int,
    change_set_id: UUID,
    confirmation: str,
    actor: str | None,
    current_run_id: str | None,
    awaited_issue: str | None,
    closed_children: Collection[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reset one never-published ChangeSet of a paused issue loop, or raise
    ChangeResetBlocked. Runs in the caller's transaction.

    ``current_run_id`` and ``awaited_issue`` are read from Temporal by the caller (the
    loop's open run and its AWAITING_OPERATOR query), exactly as for issue-reapprove.
    ``closed_children`` are the delivery_children the caller proved closed in Temporal
    (whatever their projected Run status: a terminal row does not prove a child was not
    reset and restarted); any other child refuses."""
    current_time = now or datetime.now(UTC)
    if actor is None:
        raise _refuse("OPERATOR_REQUIRED", "an operator identity is required")
    if confirmation != reset_phrase(change_set_id):
        raise _refuse("CONFIRMATION_MISMATCH", f"confirm with {reset_phrase(change_set_id)}")
    identity = issue_loop_identity(loop_workflow_id, project)
    if identity is None:
        raise _refuse("LOOP_INVALID", "the workflow is not an issue-start loop of that project")
    campaign_id, operation_id = identity
    issue_repository = f"gntik-ai/{repository}"
    target = f"{issue_repository}#{issue_number}"
    if issue_repository not in BACKLOG_PROJECT_PAIRS.get(project, ()):
        raise _refuse("TARGET_MISMATCH", f"{issue_repository} is not a {project} repository")
    # Finding first, then the ChangeSet: the lock order of delivery.load_confirmed_finding
    # (which binds every delivery child) and of the publication custody.
    finding_id = session.scalar(select(ChangeSet.finding_id).where(ChangeSet.id == change_set_id))
    finding = (
        session.scalar(select(Finding).where(Finding.id == finding_id).with_for_update())
        if finding_id is not None
        else None
    )
    change_set = (
        session.scalar(select(ChangeSet).where(ChangeSet.id == change_set_id).with_for_update())
        if finding is not None
        else None
    )
    if (
        finding is None
        or change_set is None
        or change_set.project != project
        or change_set.id != uuid5(NAMESPACE_URL, f"changeset:{finding.id}")
    ):
        raise _refuse("CHANGESET_NOT_FOUND", f"no {project} delivery ChangeSet {change_set_id}")
    try:
        loop_paused_on(session, loop_workflow_id, target, current_run_id, awaited_issue)
    except IssueReapprovalBlocked as error:
        raise _refuse("LOOP_NOT_PAUSED", error.detail) from error
    try:
        reference = validate_change_set_issue_approval(session, change_set.id, current=False)
        if reference is None:
            raise ValueError("ChangeSet has no backlog issue approval")
        origin, request, lineage = correction_lineage(session, change_set)
        budget = correction_attempt_budget(session, change_set)
    except (KeyError, TypeError, ValueError) as error:
        raise _refuse(
            "APPROVAL_INVALID",
            f"the ChangeSet's originating approval is not valid ({str(error)[:160]})",
        ) from error
    value = origin.result_json
    issue = session.scalar(
        select(BacklogIssueRecord).where(
            BacklogIssueRecord.backlog_campaign_id == campaign_id,
            BacklogIssueRecord.repository == issue_repository,
            BacklogIssueRecord.issue_number == issue_number,
        )
    )
    if (
        value.get("parent_workflow_id") != loop_workflow_id
        or value.get("issue_repository") != issue_repository
        or value.get("issue_number") != issue_number
        or request.project != project
        or request.backlog_campaign_id != campaign_id
        or request.issue_operation_id != operation_id
        or issue is None
        or issue.finding_id != change_set.finding_id
    ):
        raise _refuse(
            "TARGET_MISMATCH",
            "the ChangeSet does not belong to that loop's issue operation and finding",
        )
    published = _published(session, change_set)
    if published is not None:
        raise _refuse("PUBLISHED", f"{published}; a published ChangeSet is never reset")
    if change_set.state == ChangeSetState.REQUEST_CHANGES.value:
        raise _refuse("ALREADY_RETRYABLE", "the ChangeSet is already in REQUEST_CHANGES")
    if change_set_unstarted(change_set):
        raise _refuse(
            "ALREADY_RETRYABLE",
            "the ChangeSet never started (a retry starts it on its revalidated heads)",
        )
    if change_set.state not in _RESETTABLE_STATES:
        raise _refuse("STATE", f"a {change_set.state} ChangeSet cannot be reset")
    if budget["remaining"] < 1:
        raise _refuse(
            "LINEAGE_EXHAUSTED",
            f"the ChangeSet used {budget['attempts_used']} of {budget['attempt_bound']} "
            "correction attempts; a reset adds none",
        )
    # NEW-168 review: a paused parent does not prove its children stopped (a child reset
    # and restarted in Temporal keeps running). Re-read under the locks: every delivery
    # run must have been proven closed by the caller, and no lease may still be held.
    children = delivery_children(session, loop_workflow_id, change_set.id)
    unproven = [name for name in children if name not in set(closed_children)]
    if unproven:
        raise _refuse(
            "DELIVERY_ACTIVE",
            f"delivery run {unproven[0]} is not proven closed in Temporal",
        )
    held = _active_leases(session, change_set.id, children, current_time)
    if held is not None:
        raise _refuse("DELIVERY_ACTIVE", f"{held}; wait for its release or expiry")
    from_state = request_changes(change_set, blocker=OPERATOR_RESET, actor=actor, now=current_time)
    recovery = dict(change_set.recovery_json or {})
    prior = recovery.get(RESET_RECORD_KEY)
    record: dict[str, Any] = {
        "actor": actor,
        "at": current_time.isoformat(),
        "from_state": from_state,
        "to_state": change_set.state,
        "version": change_set.version,
        "loop_workflow_id": loop_workflow_id,
        "origin_approval_id": reference["approval_id"],
        "lineage_length": len(lineage),
        "closed_delivery_runs": len(children),
        "count": (prior.get("count", 0) if isinstance(prior, dict) else 0) + 1,
    }
    recovery[RESET_RECORD_KEY] = record
    change_set.recovery_json = recovery
    event = DashboardEvent(
        occurred_at=current_time,
        level="WARNING",
        event_type=RESET_EVENT_TYPE,
        source="controller",
        host=None,
        worker_kind="controller",
        project=project,
        workflow_id=loop_workflow_id,
        temporal_run_id=None,
        issue_number=issue_number,
        campaign_id=str(campaign_id),
        entity_type="change_set",
        entity_id=str(change_set.id),
        current_phase="CHANGESET_RESET",
        message="ChangeSet reset to REQUEST_CHANGES by operator",
        metadata_json={"issue_repository": issue_repository, **record},
    )
    session.add(event)
    session.flush()
    return {
        "status": "CHANGESET_RESET",
        "change_set_id": str(change_set.id),
        "loop_workflow_id": loop_workflow_id,
        "target": target,
        "from_state": from_state,
        "state": change_set.state,
        "version": change_set.version,
        "attempts_remaining": budget["remaining"],
        "reset_by": actor,
        "audit_event_id": event.id,
        "next": f"engineering-ctl backlog-retry {loop_workflow_id} {repository} {issue_number}",
        "production": False,
    }
