"""NEW-115: re-approve one backlog issue whose GitHub body changed after delivery approval.

NEW-112 binds an approved delivery to the issue text it was approved for (the imported
``BacklogIssueRecord.body_sha256``). That hash is also part of every durable approval
snapshot (operation binding, child approval, R3 candidate), so it is never rewritten.
An operator amendment is recorded next to it instead, in the record's GitHub snapshot:
it names the imported hash it extends, the newly approved body hash and GitHub revision,
and who approved it. The delivery's approved-body check and publication custody read the
effective revision through :func:`effective_issue_revision`; everything else keeps the
immutable import. R3 issues are never amended here: their approval is the exact text.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from engineering_control.db.models import (
    ActionCandidate,
    BacklogIssueRecord,
    ChangeSet,
    ComponentLease,
    DashboardEvent,
    Finding,
    GitHubBacklogCampaign,
    Run,
    Step,
)
from engineering_control.engineering.backlog import (
    BACKLOG_PROJECT_PAIRS,
    calculated_issue_risk,
    canonical_sha256,
)
from engineering_control.models.enums import ChangeSetState
from engineering_control.security.redaction import SecretRedactor

AMENDMENT_KEY = "approved_body_amendment"
AMEND_REASON = "issue amended"
AMEND_EVENT_TYPE = "issue_body_amended"
# ChangeSet.recovery_json: the issue revision the current delivery run loaded.
ISSUE_BODY_REVISION_KEY = "issue_body_revision"
_RISK_RANK = {"R0": 0, "R1": 1, "R2": 2, "R3": 3}
CONFIRMATION_PATTERN = re.compile(r"[a-f0-9]{12,64}")
DIFF_LINE_LIMIT = 60
DIFF_LINE_CHARS = 200
# The import keeps these bounds (GitHubIssueSnapshot.body_excerpt, Finding.actual_behavior).
SNAPSHOT_EXCERPT_LIMIT = 6000
FINDING_EXCERPT_LIMIT = 4000
_SHA256 = re.compile(r"[a-f0-9]{64}")
_DELIVERED_STATES = frozenset(
    {
        ChangeSetState.SOURCE_MERGED.value,
        ChangeSetState.FINAL_ARTIFACT_BUILT.value,
        ChangeSetState.DEPLOY_PR.value,
        ChangeSetState.AWAITING_STAGING_APPROVAL.value,
        ChangeSetState.STAGING_APPROVED.value,
        ChangeSetState.STAGING_DEPLOYED.value,
        ChangeSetState.RUNTIME_VERIFIED.value,
        ChangeSetState.ISSUE_CLOSED.value,
    }
)


class IssueAmendBlocked(ValueError):
    """A refused amendment: a stable blocker code and operator guidance."""

    def __init__(self, blocker: str, detail: str) -> None:
        super().__init__(f"{blocker}: {detail}")
        self.blocker = blocker
        self.detail = detail


def _surface(project: str, repository: str) -> str:
    return "source" if BACKLOG_PROJECT_PAIRS[project][0] == repository else "deployment"


def _amendment_binding(issue: BacklogIssueRecord, value: dict[str, Any]) -> str:
    return canonical_sha256(
        {
            "backlog_issue_record_id": str(issue.id),
            "backlog_campaign_id": str(issue.backlog_campaign_id),
            "finding_id": str(issue.finding_id),
            "repository": issue.repository,
            "issue_number": issue.issue_number,
            **{key: item for key, item in value.items() if key != "binding_sha256"},
        }
    )


def effective_issue_revision(issue: BacklogIssueRecord) -> tuple[str, datetime]:
    """The approved body hash and GitHub revision time of one backlog issue.

    Without an amendment this is the immutable import. A malformed amendment fails
    closed (ValueError) rather than silently falling back to either revision."""
    snapshot = issue.github_snapshot_json if isinstance(issue.github_snapshot_json, dict) else {}
    value = snapshot.get(AMENDMENT_KEY)
    if value is None:
        return issue.body_sha256, issue.github_updated_at
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or value.get("base_body_sha256") != issue.body_sha256
        or not isinstance(value.get("body_sha256"), str)
        or not _SHA256.fullmatch(value["body_sha256"])
        or not isinstance(value.get("github_updated_at"), str)
        or value.get("binding_sha256") != _amendment_binding(issue, value)
    ):
        raise ValueError("backlog issue body amendment binding is invalid")
    try:
        updated_at = datetime.fromisoformat(value["github_updated_at"])
    except ValueError as error:
        raise ValueError("backlog issue body amendment binding is invalid") from error
    return value["body_sha256"], updated_at


def diff_summary(approved: str, current: str) -> tuple[list[str], bool]:
    """A bounded, redacted unified diff of the approved text against the current text."""
    redactor = SecretRedactor()
    before = str(redactor.redact_text(approved).value).splitlines()
    after = str(redactor.redact_text(current).value).splitlines()
    lines = [
        line if len(line) <= DIFF_LINE_CHARS else line[: DIFF_LINE_CHARS - 1] + "…"
        for line in difflib.unified_diff(
            before, after, fromfile="approved", tofile="current", lineterm="", n=1
        )
    ]
    if len(lines) <= DIFF_LINE_LIMIT:
        return lines, False
    kept = lines[: DIFF_LINE_LIMIT - 1]
    return [*kept, f"... {len(lines) - len(kept)} more diff lines omitted"], True


def resolve_amend_target(
    session: Session,
    project: str,
    repository: str,
    issue_number: int,
    *,
    lock: bool = False,
    now: datetime | None = None,
) -> tuple[BacklogIssueRecord, GitHubBacklogCampaign, Finding, ChangeSet | None]:
    """Find the approved backlog delivery of one issue, or refuse with a blocker."""
    full_repository = f"gntik-ai/{repository}"
    pairs = BACKLOG_PROJECT_PAIRS.get(project)
    if pairs is None or full_repository not in pairs:
        raise IssueAmendBlocked(
            "ISSUE_AMEND_TARGET_INVALID",
            f"{full_repository} is not a {project} backlog repository",
        )
    target = f"{full_repository}#{issue_number}"
    query = (
        select(BacklogIssueRecord)
        .join(
            GitHubBacklogCampaign,
            GitHubBacklogCampaign.id == BacklogIssueRecord.backlog_campaign_id,
        )
        .where(
            GitHubBacklogCampaign.project == project,
            GitHubBacklogCampaign.approval_binding.is_not(None),
            BacklogIssueRecord.repository == full_repository,
            BacklogIssueRecord.issue_number == issue_number,
            BacklogIssueRecord.finding_id.is_not(None),
        )
        .order_by(desc(GitHubBacklogCampaign.imported_at))
        .limit(1)
    )
    issue = session.scalar(query)
    finding = None
    if issue is not None and issue.finding_id is not None:
        # The same lock order as delivery startup (delivery.load_confirmed_finding locks
        # the Finding, then its ChangeSet): a confirm and a delivery load serialize, so a
        # delivery consumes either the previous or the amended revision, never a mix.
        finding = session.get(
            Finding, issue.finding_id, with_for_update=lock, populate_existing=lock
        )
        if lock:
            issue = session.get(
                BacklogIssueRecord, issue.id, with_for_update=True, populate_existing=True
            )
    campaign = session.get(GitHubBacklogCampaign, issue.backlog_campaign_id) if issue else None
    if issue is None or campaign is None or finding is None:
        raise IssueAmendBlocked(
            "ISSUE_AMEND_NOT_IN_APPROVED_BACKLOG",
            f"{target} has no approved delivery in a {project} backlog campaign; import and "
            f"approve it first (/loop import {project} github-open ..., then /loop "
            f"issue-start {project} {_surface(project, full_repository)} {issue_number})",
        )
    surface = _surface(project, full_repository)
    r3_approved = session.scalar(
        select(ActionCandidate.id)
        .where(
            ActionCandidate.action == "r3_issue_approval",
            ActionCandidate.target_id == target,
            ActionCandidate.used_at.is_not(None),
        )
        .limit(1)
    )
    if issue.calculated_risk == "R3" or finding.risk_class == "R3" or r3_approved is not None:
        raise IssueAmendBlocked(
            "ISSUE_AMEND_R3_REQUIRES_REAPPROVAL",
            f"{target} is R3 (approved for its exact text); re-import the backlog, then "
            f"/loop r3-candidate {project} {surface} {issue_number} and /loop approve-r3 "
            f"{project} {surface} {issue_number} <binding> <phrase>",
        )
    change_set = session.scalar(
        select(ChangeSet)
        .where(ChangeSet.finding_id == finding.id)
        .with_for_update(of=ChangeSet)
        .execution_options(populate_existing=True)
        if lock
        else select(ChangeSet).where(ChangeSet.finding_id == finding.id)
    )
    if (
        issue.state != "OPEN"
        or issue.delivery_status == "COMPLETE"
        or issue.final_closure_status == "FIXED_CLOSURE_COMPLETE"
        or (change_set is not None and change_set.state in _DELIVERED_STATES)
    ):
        raise IssueAmendBlocked(
            "ISSUE_AMEND_ALREADY_DELIVERED",
            f"{target} is already delivered or closed; open a new issue or change request",
        )
    if change_set is not None:
        if not change_set_unstarted(change_set):
            # Only a delivery stopped before any work (CONFIRMED, e.g. blocked by
            # ISSUE_BODY_CHANGED_SINCE_APPROVAL at the body refresh) is amended.
            raise IssueAmendBlocked(
                "ISSUE_AMEND_DELIVERY_ACTIVE",
                f"the delivery of {target} already produced work (ChangeSet "
                f"{change_set.state}); re-import the backlog and start the issue again",
            )
        current = now or datetime.now(UTC)
        running = session.scalar(
            select(Run.id)
            .where(Run.change_set_id == change_set.id, Run.status == "RUNNING")
            .limit(1)
        )
        leased = session.scalar(
            select(ComponentLease.id)
            .join(Run, Run.id == ComponentLease.owner_run_id)
            .where(
                Run.change_set_id == change_set.id,
                ComponentLease.released_at.is_(None),
                ComponentLease.expires_at > current,
            )
            .limit(1)
        )
        if running is not None or leased is not None:
            raise IssueAmendBlocked(
                "ISSUE_AMEND_DELIVERY_ACTIVE",
                f"a delivery of {target} is running; amend it after it stops",
            )
    return issue, campaign, finding, change_set


def _observed(observation: dict[str, Any], target: str) -> dict[str, Any]:
    status = observation.get("status")
    if status == "ISSUE_BODY_UNAVAILABLE" and observation.get("reason") == "ISSUE_BODY_EMPTY":
        raise IssueAmendBlocked(
            "ISSUE_AMEND_BODY_EMPTY", f"the GitHub body of {target} is empty; nothing to approve"
        )
    current = observation.get("source_body_sha256")
    excerpt = observation.get("body_excerpt")
    updated_at = observation.get("updated_at")
    if (
        status != "ISSUE_BODY_REFRESHED"
        or not isinstance(current, str)
        or not _SHA256.fullmatch(current)
        or not isinstance(excerpt, str)
        or not excerpt.strip()
        or not isinstance(updated_at, str)
    ):
        raise IssueAmendBlocked(
            "ISSUE_AMEND_BODY_UNAVAILABLE",
            f"the GitHub body of {target} could not be read "
            f"({str(observation.get('reason') or status)[:80]}); try again",
        )
    try:
        parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise IssueAmendBlocked(
            "ISSUE_AMEND_BODY_UNAVAILABLE", f"the GitHub revision of {target} is malformed"
        ) from error
    if observation.get("issue_state") not in {None, "OPEN"}:
        raise IssueAmendBlocked(
            "ISSUE_AMEND_ALREADY_DELIVERED", f"{target} is no longer open on GitHub"
        )
    return {
        "body_sha256": current,
        "body_excerpt": excerpt,
        "body_chars": int(observation.get("body_chars") or len(excerpt)),
        "updated_at": parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC),
        "title_sha256": observation.get("title_sha256"),
    }


def amend_issue(
    session: Session,
    *,
    project: str,
    repository: str,
    issue_number: int,
    observation: dict[str, Any],
    confirmation: str | None,
    actor: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Preview (no confirmation) or apply (confirmation) one issue body amendment.

    Apply runs in the caller's transaction under the delivery-startup lock order: it
    re-resolves the target, requires the freshly read body hash to start with the
    operator's confirmation (the hash the preview showed), and records the amendment, the
    imported excerpt, the Finding's copy and the audit event together. A body whose
    import risk class is higher than the approved one is refused (never re-classified)."""
    current_time = now or datetime.now(UTC)
    issue, campaign, finding, change_set = resolve_amend_target(
        session,
        project,
        repository,
        issue_number,
        lock=confirmation is not None,
        now=current_time,
    )
    target = f"{issue.repository}#{issue.issue_number}"
    observed = _observed(observation, target)
    if observed["title_sha256"] is not None and observed["title_sha256"] != (
        hashlib.sha256(issue.title.encode()).hexdigest()
    ):
        raise IssueAmendBlocked(
            "ISSUE_AMEND_TITLE_CHANGED",
            f"the title of {target} changed too; re-import the backlog (/loop import "
            f"{project} github-open ...) and start the issue again",
        )
    approved_sha256, approved_at = effective_issue_revision(issue)
    snapshot = dict(issue.github_snapshot_json or {})
    approved_text = str(snapshot.get("body_excerpt") or finding.actual_behavior or "")
    amendment = snapshot.get(AMENDMENT_KEY)
    approved_chars = (
        int(amendment["body_chars"])
        if isinstance(amendment, dict) and isinstance(amendment.get("body_chars"), int)
        else len(approved_text)
    )
    current_sha256 = observed["body_sha256"]
    current_at: datetime = observed["updated_at"]
    surface = _surface(project, issue.repository)
    base = {
        "target": target,
        "project": project,
        "surface": surface,
        "backlog_campaign_id": str(campaign.id),
        "finding_id": str(finding.id),
        "approved_body_sha256": approved_sha256,
        "current_body_sha256": current_sha256,
        "approved_body_chars": approved_chars,
        "current_body_chars": observed["body_chars"],
        "approved_updated_at": _utc(approved_at).isoformat(),
        "current_updated_at": current_at.isoformat(),
        "risk_class": finding.risk_class,
        "production": False,
    }
    same_body = approved_sha256 == current_sha256
    if same_body and _utc(approved_at) == current_at:
        result: dict[str, Any] = {
            **base,
            "status": "NO_CHANGE",
            "detail": "the GitHub issue is the approved revision; nothing to amend",
        }
        if isinstance(amendment, dict) and amendment.get("body_sha256") == current_sha256:
            # A rerun after an unknown outcome (e.g. a client timeout) reconciles: the
            # recorded amendment is this revision, so a requested retry is still sent.
            result["amended_by"] = amendment.get("amended_by")
            result["retry_workflow_id"] = retry_workflow_id(session, change_set)
            result["retry_key"] = str(amendment.get("binding_sha256", ""))[:32]
        return result
    excerpt = str(observed["body_excerpt"])
    if not same_body:
        new_risk = calculated_issue_risk(issue.repository, issue.labels_json, issue.title, excerpt)
        approved_risk = max(
            (issue.calculated_risk, finding.risk_class), key=lambda value: _RISK_RANK[value]
        )
        if _RISK_RANK[new_risk] > _RISK_RANK[approved_risk]:
            raise IssueAmendBlocked(
                "ISSUE_AMEND_RISK_ESCALATION",
                f"the new text of {target} classifies as {new_risk}, above the approved "
                f"{approved_risk}; re-import the backlog (/loop import {project} github-open "
                f"...) and, for R3, /loop r3-candidate {project} {surface} {issue_number} "
                f"and /loop approve-r3",
            )
    change = "BODY_CHANGED" if not same_body else "REVISION_TIME_CHANGED"
    short = issue.repository.removeprefix("gntik-ai/")
    if confirmation is None:
        diff, truncated = diff_summary(approved_text, excerpt) if not same_body else ([], False)
        prefix = current_sha256[:12]
        return {
            **base,
            "status": "PREVIEW" if not same_body else "REVISION_TIME_CHANGED",
            "change": change,
            "diff": diff,
            "diff_truncated": truncated,
            "confirm_with": prefix,
            "telegram_command": f"/loop amend {project} {short} {issue_number} {prefix} retry",
            "cli_command": (
                f"engineering-ctl issue-amend {project} {short} {issue_number} "
                f"--confirm {prefix} --retry"
            ),
        }
    if not CONFIRMATION_PATTERN.fullmatch(confirmation):
        raise IssueAmendBlocked(
            "ISSUE_AMEND_CONFIRMATION_INVALID",
            "confirm with at least 12 lowercase hex characters of the current body hash",
        )
    if not current_sha256.startswith(confirmation):
        raise IssueAmendBlocked(
            "ISSUE_AMEND_CONFIRMATION_MISMATCH",
            f"the GitHub body of {target} changed since the preview (now "
            f"{current_sha256[:12]}); preview it again before confirming",
        )
    if actor is None:
        raise IssueAmendBlocked("ISSUE_AMEND_OPERATOR_REQUIRED", "an operator identity is required")
    value: dict[str, Any] = {
        "version": 1,
        "base_body_sha256": issue.body_sha256,
        "previous_body_sha256": approved_sha256,
        "previous_updated_at": _utc(approved_at).isoformat(),
        "body_sha256": current_sha256,
        "github_updated_at": current_at.isoformat(),
        "body_chars": int(observed["body_chars"]),
        "body_excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
        "change": change,
        "amended_by": actor,
        "amended_at": current_time.isoformat(),
        "reason": AMEND_REASON,
        "amendment_count": (
            int(amendment.get("amendment_count", 1)) + 1 if isinstance(amendment, dict) else 1
        ),
    }
    value["binding_sha256"] = _amendment_binding(issue, value)
    snapshot["body_excerpt"] = excerpt[:SNAPSHOT_EXCERPT_LIMIT]
    snapshot[AMENDMENT_KEY] = value
    issue.github_snapshot_json = snapshot
    finding.actual_behavior = excerpt[:FINDING_EXCERPT_LIMIT]
    event = DashboardEvent(
        occurred_at=current_time,
        level="WARNING",
        event_type=AMEND_EVENT_TYPE,
        source="controller",
        host=None,
        worker_kind="controller",
        project=project,
        workflow_id=None,
        temporal_run_id=None,
        issue_number=issue_number,
        campaign_id=str(campaign.id),
        entity_type="backlog_issue_record",
        entity_id=str(issue.id),
        current_phase="ISSUE_AMENDED",
        message=f"Issue body re-approved by operator: {AMEND_REASON}",
        metadata_json={
            "amended_by": actor,
            "reason": AMEND_REASON,
            "change": change,
            "repository": issue.repository,
            "finding_id": str(finding.id),
            "change_set_id": str(change_set.id) if change_set is not None else None,
            "previous_body_sha256": approved_sha256,
            "body_sha256": current_sha256,
            "previous_updated_at": value["previous_updated_at"],
            "github_updated_at": value["github_updated_at"],
            "base_body_sha256": issue.body_sha256,
            "binding_sha256": value["binding_sha256"],
        },
    )
    session.add(event)
    session.flush()
    return {
        **base,
        "status": "AMENDED",
        "change": change,
        "amended_by": actor,
        "reason": AMEND_REASON,
        "previous_body_sha256": approved_sha256,
        "audit_event_id": event.id,
        "retry_workflow_id": retry_workflow_id(session, change_set),
        "retry_key": value["binding_sha256"][:32],
    }


def consumed_issue_revision(
    change_set: ChangeSet, run: Run, issue: BacklogIssueRecord
) -> tuple[str, datetime]:
    """The issue revision the publishing delivery run actually loaded (and refreshed to).

    delivery.load_confirmed_finding records it under the delivery-startup lock; a later
    amendment never re-binds a run that started from the previous text. ChangeSets loaded
    before this record existed keep the imported revision."""
    recorded = (change_set.recovery_json or {}).get(ISSUE_BODY_REVISION_KEY)
    if recorded is None:
        return issue.body_sha256, issue.github_updated_at
    if (
        not isinstance(recorded, dict)
        or recorded.get("run_id") != str(run.id)
        or not isinstance(recorded.get("body_sha256"), str)
        or not _SHA256.fullmatch(recorded["body_sha256"])
        or not isinstance(recorded.get("updated_at"), str)
    ):
        raise ValueError("publication run did not load the approved issue revision")
    return recorded["body_sha256"], datetime.fromisoformat(recorded["updated_at"])


def record_consumed_issue_revision(
    change_set: ChangeSet, run: Run, issue: BacklogIssueRecord
) -> str:
    """Bind this delivery run to the effective approved revision; return its body hash."""
    body_sha256, updated_at = effective_issue_revision(issue)
    recovery = dict(change_set.recovery_json or {})
    recovery[ISSUE_BODY_REVISION_KEY] = {
        "run_id": str(run.id),
        "body_sha256": body_sha256,
        "updated_at": _utc(updated_at).isoformat(),
    }
    change_set.recovery_json = recovery
    return body_sha256


def change_set_unstarted(change_set: ChangeSet) -> bool:
    """A ChangeSet whose delivery stopped before producing anything.

    A delivery blocked before its worktrees (e.g. ISSUE_BODY_CHANGED_SINCE_APPROVAL at the
    issue-body refresh) leaves it CONFIRMED with no branch, head or PR."""
    return (
        change_set.state == "CONFIRMED"
        and change_set.source_branch is None
        and change_set.deployment_branch is None
        and change_set.source_head_sha is None
        and change_set.deployment_head_sha is None
        and change_set.source_pr_url is None
        and change_set.deployment_pr_url is None
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def retry_workflow_id(session: Session, change_set: ChangeSet | None) -> str | None:
    """The backlog loop that started this issue's latest delivery child, if recorded."""
    if change_set is None:
        return None
    children = (change_set.recovery_json or {}).get("issue_execution_approval_children")
    if not isinstance(children, list) or not children or not isinstance(children[-1], dict):
        return None
    try:
        record = session.get(Step, UUID(str(children[-1].get("approval_id"))))
    except ValueError:
        return None
    parent = record.result_json.get("parent_workflow_id") if record is not None else None
    return parent if isinstance(parent, str) and parent else None
