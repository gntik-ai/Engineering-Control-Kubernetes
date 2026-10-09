"""NEW-168: operator reset of a stuck, never-published delivery ChangeSet.

A delivery attempt can stop with its ChangeSet in a state no bounded retry child may
start from (PLANNED/IMPLEMENTING), so every ``backlog-retry`` was refused with "another
delivery child requires an explicit correction state" and the operator hand-wrote a
guarded SQL UPDATE (ops-history db-fix-2026100*-cs*.log). ``change-reset`` replaces that
UPDATE for an issue-start loop that is PAUSED on the ChangeSet's issue:

- ``retry`` moves the ChangeSet to REQUEST_CHANGES (as NEW-137 does after a provider
  outage), so the loop's next backlog-retry binds a child counted in the bounded
  correction lineage (no bypass).
- ``rebase`` resets it to its unstarted state (NEW-115 change_set_unstarted) on the
  heads of its finding's current-source revalidation (NEW-117), with its worktree leases
  released, so the next child allocates a fresh worktree there and the maker starts from
  scratch. Only a move the revalidation proves is a fast-forward is made: anything else
  is refused with CHANGE_RESET_REVALIDATION_REQUIRED (a newer base needs a new
  revalidation, i.e. a fresh backlog import + issue-start).

Every precondition is re-read here from durable records under the Finding and ChangeSet
row locks (the order delivery.load_confirmed_finding and publication custody use); the
reset is audited as a DashboardEvent in the same transaction.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.db.models import (
    BacklogIssueRecord,
    ChangeSet,
    DashboardEvent,
    Deployment,
    Finding,
    Run,
    Step,
    WorktreeLease,
)
from engineering_control.engineering.backlog import BACKLOG_PROJECT_PAIRS
from engineering_control.models.enums import ChangeSetState
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
from engineering_control.operations.publication_authority import (
    ATTEMPT_STEP,
    EFFECT_STEP,
    INTENT_STEP,
)

RESET_EVENT_TYPE = "change_set_reset"
OPERATOR_RESET = "OPERATOR_RESET"
RESET_MODES = ("retry", "rebase")
RESET_RECORD_KEY = "operator_reset"
# The states each mode may reset: only before any publication (REQUEST_CHANGES and an
# unstarted CONFIRMED ChangeSet are already retryable, see below).
_RESETTABLE_STATES = {
    "retry": frozenset({ChangeSetState.PLANNED.value, ChangeSetState.IMPLEMENTING.value}),
    "rebase": frozenset(
        {
            ChangeSetState.CONFIRMED.value,
            ChangeSetState.PLANNED.value,
            ChangeSetState.IMPLEMENTING.value,
            ChangeSetState.REQUEST_CHANGES.value,
        }
    ),
}
# recovery_json records written only from publication on (persist_publication, CI,
# candidate/release, merge, staging, closure): any of them means work left the worktree.
_PUBLISHED_RECOVERY_KEYS = frozenset(
    {
        "provider_chain",
        "independent_review",
        "change_evidence",
        "release_impact",
        "ci",
        "ci_repair_restore",
        "candidate",
        "source_merge",
        "promotion",
        "promotion_component_lease",
        "staging_gitops",
        "release_evidence",
        "release_plan_review",
        "rollback_plan",
        "final_closure",
        "last_recovery_result",
    }
)
_SHA = re.compile(r"[a-f0-9]{40}")


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


def revalidated_delivery_heads(finding: Finding) -> tuple[str, str] | None:
    """The heads the finding's current-source revalidation confirmed it at (NEW-117).

    The same record MetadataActivityHandler._delivery_base_heads starts a delivery from;
    the scan worker verified there that they fast-forward from the import heads."""
    heads = (finding.impact_json or {}).get("revalidated_heads")
    if (
        isinstance(heads, dict)
        and heads.get("import_source_sha") == finding.affected_source_sha
        and heads.get("import_deployment_sha") == finding.affected_deployment_sha
        and isinstance(heads.get("source_sha"), str)
        and isinstance(heads.get("deployment_sha"), str)
        and _SHA.fullmatch(heads["source_sha"])
        and _SHA.fullmatch(heads["deployment_sha"])
    ):
        return str(heads["source_sha"]), str(heads["deployment_sha"])
    return None


def _published(session: Session, change_set: ChangeSet) -> str | None:
    """Why the ChangeSet may have published work, or None when nothing was published."""
    if change_set.source_pr_url is not None or change_set.deployment_pr_url is not None:
        return "a pull request was published"
    if change_set.image_digests or change_set.chart_version is not None:
        return "release artifacts are recorded"
    recorded = sorted(_PUBLISHED_RECOVERY_KEYS.intersection(change_set.recovery_json or {}))
    if recorded:
        return f"publication records exist ({', '.join(recorded)})"
    publication = session.scalar(
        select(Step.id)
        .join(Run, Run.id == Step.run_id)
        .where(
            Run.change_set_id == change_set.id,
            Step.name.in_((INTENT_STEP, EFFECT_STEP, ATTEMPT_STEP)),
        )
        .limit(1)
    )
    if publication is not None:
        return "a publication intent or effect is recorded"
    deployment = session.scalar(
        select(Deployment.id).where(Deployment.change_set_id == change_set.id).limit(1)
    )
    if deployment is not None:
        return "a deployment is recorded"
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
    mode: str,
    actor: str | None,
    current_run_id: str | None,
    awaited_issue: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reset one never-published ChangeSet of a paused issue loop, or raise
    ChangeResetBlocked. Runs in the caller's transaction.

    ``current_run_id`` and ``awaited_issue`` are read from Temporal by the caller (the
    loop's open run and its AWAITING_OPERATOR query), exactly as for issue-reapprove."""
    current_time = now or datetime.now(UTC)
    if actor is None:
        raise _refuse("OPERATOR_REQUIRED", "an operator identity is required")
    if mode not in RESET_MODES:
        raise _refuse("MODE_INVALID", f"mode must be one of {', '.join(RESET_MODES)}")
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
    if change_set.state == ChangeSetState.REQUEST_CHANGES.value and mode == "retry":
        raise _refuse("ALREADY_RETRYABLE", "the ChangeSet is already in REQUEST_CHANGES")
    if change_set_unstarted(change_set):
        raise _refuse(
            "ALREADY_RETRYABLE",
            "the ChangeSet never started (a retry starts it on its revalidated heads)",
        )
    if change_set.state not in _RESETTABLE_STATES[mode]:
        raise _refuse("STATE", f"a {change_set.state} ChangeSet cannot be reset")
    if budget["remaining"] < 1:
        raise _refuse(
            "LINEAGE_EXHAUSTED",
            f"the ChangeSet used {budget['attempts_used']} of {budget['attempt_bound']} "
            "correction attempts; a reset adds none",
        )
    previous_bases = (change_set.source_base_sha, change_set.deployment_base_sha)
    new_bases = previous_bases
    if mode == "rebase":
        heads = revalidated_delivery_heads(finding)
        imported = (finding.affected_source_sha, finding.affected_deployment_sha)
        # Proven newer only when the ChangeSet still sits on the import heads and the
        # revalidation moved past them (verified as a fast-forward by the scan worker).
        if heads is None or heads == previous_bases or previous_bases != imported:
            raise _refuse(
                "REVALIDATION_REQUIRED",
                "the finding's latest revalidation gives no heads proven newer than the "
                "ChangeSet's base; start the issue from a fresh revalidation (backlog-import "
                "+ issue-start), or reset with --mode retry on the current base",
            )
        new_bases = heads
    from_state = change_set.state
    recovery = dict(change_set.recovery_json or {})
    previous_request = recovery.get("correction_requested")
    previous = previous_request if isinstance(previous_request, dict) else {}
    run_ids = [str(item) for item in previous.get("run_ids") or [] if item]
    if previous.get("run_id"):
        run_ids.append(str(previous["run_id"]))
    released: list[str] = []
    if mode == "retry":
        change_set.state = ChangeSetState.REQUEST_CHANGES.value
        # Same record a stopped attempt writes (NEW-128/137); the stopped runs stay listed
        # so a delayed release of one of them never writes again.
        recovery["correction_requested"] = {
            "run_ids": list(dict.fromkeys(run_ids))[-128:],
            "status": ChangeSetState.REQUEST_CHANGES.value,
            "blocker": OPERATOR_RESET,
            "actor": actor,
            "at": current_time.isoformat(),
            "recorded_at": current_time.isoformat(),
        }
    else:
        # change_set_unstarted: the next child binds, allocates a fresh worktree on the
        # new base (the old branch head does not descend from it, so it is archived, not
        # resumed) and runs the maker from scratch.
        change_set.state = ChangeSetState.CONFIRMED.value
        change_set.source_branch = None
        change_set.deployment_branch = None
        change_set.source_head_sha = None
        change_set.deployment_head_sha = None
        change_set.source_base_sha, change_set.deployment_base_sha = new_bases
        # Findings about the old base's diff would misdirect a from-scratch maker.
        recovery.pop("validation_findings", None)
        for lease in session.scalars(
            select(WorktreeLease).where(
                WorktreeLease.change_set_id == change_set.id,
                WorktreeLease.released_at.is_(None),
            )
        ).all():
            lease.released_at = current_time
            released.append(str(lease.id))
    change_set.version += 1
    prior = recovery.get(RESET_RECORD_KEY)
    record: dict[str, Any] = {
        "mode": mode,
        "actor": actor,
        "at": current_time.isoformat(),
        "from_state": from_state,
        "to_state": change_set.state,
        "version": change_set.version,
        "loop_workflow_id": loop_workflow_id,
        "origin_approval_id": reference["approval_id"],
        "lineage_length": len(lineage),
        "previous_bases": list(previous_bases),
        "bases": list(new_bases),
        "released_worktree_leases": released,
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
        message=f"ChangeSet reset by operator ({mode})",
        metadata_json={"issue_repository": issue_repository, **record},
    )
    session.add(event)
    session.flush()
    return {
        "status": "CHANGESET_RESET",
        "mode": mode,
        "change_set_id": str(change_set.id),
        "loop_workflow_id": loop_workflow_id,
        "target": target,
        "from_state": from_state,
        "state": change_set.state,
        "version": change_set.version,
        "bases": list(new_bases),
        "released_worktree_leases": released,
        "attempts_remaining": budget["remaining"],
        "reset_by": actor,
        "audit_event_id": event.id,
        "next": f"engineering-ctl backlog-retry {loop_workflow_id} {repository} {issue_number}",
        "production": False,
    }
