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
from uuid import NAMESPACE_URL, UUID, uuid5

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
    OperationRequest,
    Run,
    Step,
    WorktreeLease,
)
from engineering_control.engineering.backlog import (
    BACKLOG_PROJECT_PAIRS,
    OPERATOR_R1_LABEL,
    calculated_issue_risk,
    canonical_sha256,
)
from engineering_control.models.enums import ChangeSetState
from engineering_control.operations.change_set_publication import (
    AMENDMENT_BLOCKER,
    AMENDMENT_CUTOFF_KEY,
    CORRECTABLE_STATES,
    published_repairable_blocker,
    request_changes,
    started_unpublished_blocker,
)
from engineering_control.operations.issue_start import (
    backlog_issue_signal_blocker,
    issue_request_from_operation,
    issue_workflow_id,
)
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
# NEW-205: the loop pauses after which backlog-retry re-delivers a published ChangeSet by a
# CI repair (issue_delivery_approval.CI_REPAIR_REGRESSIONS, plus every CI_REPAIR_* stop;
# that module imports this one), or rechecks it (a merge refused because the issue body
# changed, gitops._issue_revision_gate; the recheck then repairs the pending amendment).
_CI_REPAIR_PAUSES = frozenset(
    {
        "SOURCE_PR_CI_REGRESSION",
        "DEPLOYMENT_PR_CI_REGRESSION",
        "ISSUE_BODY_CHANGED_SINCE_APPROVAL",
        # NEW-214a: merge-forward repairs.
        "SOURCE_PR_BEHIND_BASE",
        "DEPLOYMENT_PR_BEHIND_BASE",
    }
)
_CI_REPAIR_PAUSE_PREFIX = "CI_REPAIR_"
# Runs that can no longer use a worktree lease they still hold (metadata's NEW-64 set): a
# published ChangeSet keeps its publishing run's worktree lease for the release path.
_TERMINAL_RUN_STATUSES = frozenset(
    {"SUCCESS", "FAILED", "CANCELLED", "TIMED_OUT", "BLOCKED", "RATE_LIMITED"}
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


def locate_issue_target(
    session: Session,
    project: str,
    repository: str,
    issue_number: int,
    *,
    lock: bool = False,
    code: str = "ISSUE_AMEND",
) -> tuple[BacklogIssueRecord, GitHubBacklogCampaign, Finding, ChangeSet | None]:
    """Find the approved, non-R3 backlog record of one issue and its ChangeSet.

    With ``lock`` the rows are locked in the delivery-startup order (Finding, then the
    issue record, then the ChangeSet). Refusals use ``code`` as the blocker prefix."""
    full_repository = f"gntik-ai/{repository}"
    pairs = BACKLOG_PROJECT_PAIRS.get(project)
    if pairs is None or full_repository not in pairs:
        raise IssueAmendBlocked(
            f"{code}_TARGET_INVALID",
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
            f"{code}_NOT_IN_APPROVED_BACKLOG",
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
    # NEW-211: a historical R3 approval no longer binds an issue the operator relabelled
    # hermes:risk-r1 and re-imported as R1 (its current record and finding say R1).
    overridden = OPERATOR_R1_LABEL in {
        str(label).strip().lower() for label in (issue.labels_json or [])
    }
    if (
        issue.calculated_risk == "R3"
        or finding.risk_class == "R3"
        or (r3_approved is not None and not overridden)
    ):
        raise IssueAmendBlocked(
            f"{code}_R3_REQUIRES_REAPPROVAL",
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
    return issue, campaign, finding, change_set


def resolve_amend_target(
    session: Session,
    project: str,
    repository: str,
    issue_number: int,
    *,
    lock: bool = False,
    now: datetime | None = None,
    closed_run_ids: frozenset[UUID] = frozenset(),
    running_runs: list[tuple[UUID, str]] | None = None,
) -> tuple[BacklogIssueRecord, GitHubBacklogCampaign, Finding, ChangeSet | None]:
    """Find the approved backlog delivery of one issue, or refuse with a blocker.

    A Run projected RUNNING blocks the amendment unless the caller verified in Temporal
    that its workflow is closed (``closed_run_ids``): runs stopped at the body refresh
    before NEW-115 were never terminalized by release_leases. With ``running_runs`` the
    RUNNING runs are collected for that verification instead of refused."""
    issue, campaign, finding, change_set = locate_issue_target(
        session, project, repository, issue_number, lock=lock
    )
    target = f"{issue.repository}#{issue_number}"
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
        # NEW-115: a delivery stopped before any work (CONFIRMED, e.g. blocked by
        # ISSUE_BODY_CHANGED_SINCE_APPROVAL at the body refresh) is amended. NEW-201: so is
        # one that started (PLANNED, IMPLEMENTING, REQUEST_CHANGES) but never published;
        # its commits are kept and reviewed against the amended text by the next maker.
        unpublished = (
            None
            if change_set_unstarted(change_set)
            else started_unpublished_blocker(session, change_set)
        )
        # NEW-205: published work still only in its open PRs is amended too, but only for
        # a loop paused on a CI repair (checked below): the next repair re-delivers the PRs
        # against the amended text, and nothing merges before it (published_amendment).
        published = unpublished is not None
        repairable = published_repairable_blocker(session, change_set) if published else None
        if repairable is not None:
            raise IssueAmendBlocked(
                "ISSUE_AMEND_DELIVERY_ACTIVE",
                f"the delivery of {target} already published work (ChangeSet "
                f"{change_set.state}: {unpublished}) and {repairable}; re-import the backlog "
                "and start the issue again",
            )
        current = now or datetime.now(UTC)
        projected = session.execute(
            select(Run.id, Run.workflow_id).where(
                Run.change_set_id == change_set.id, Run.status == "RUNNING"
            )
        ).all()
        unverified = [(row[0], row[1]) for row in projected if row[0] not in closed_run_ids]
        if running_runs is not None:
            running_runs.extend(unverified)
            unverified = []
        running = unverified[0][0] if unverified else None
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
        # NEW-201 review: the worktree lease too (as change_reset): a started ChangeSet's
        # worktree may still be held by a delivery that no Run projection shows. NEW-205: a
        # published ChangeSet keeps its publishing run's lease for the release path
        # (NEW-32/64); only a lease whose owner run has not ended blocks it.
        worktree_query = (
            select(WorktreeLease.id)
            .where(
                WorktreeLease.change_set_id == change_set.id,
                WorktreeLease.released_at.is_(None),
                WorktreeLease.expires_at > current,
            )
            .limit(1)
        )
        if published:
            worktree_query = worktree_query.join(Run, Run.id == WorktreeLease.owner_run_id).where(
                Run.status.not_in(_TERMINAL_RUN_STATUSES)
            )
        worktree = session.scalar(worktree_query)
        if running is not None or leased is not None or worktree is not None:
            raise IssueAmendBlocked(
                "ISSUE_AMEND_DELIVERY_ACTIVE",
                f"a delivery of {target} is running; amend it after it stops",
            )
        if not change_set_unstarted(change_set):
            loop = paused_issue_loop(session, change_set, issue, lock=lock)
            pause = _loop_pause_blocker(session, loop) if published and loop else None
            if published and not _ci_repair_pause(pause):
                # NEW-205: only the paused loop's CI-repair retry re-delivers published PRs.
                raise IssueAmendBlocked(
                    "ISSUE_AMEND_DELIVERY_ACTIVE",
                    f"the delivery of {target} already published work; it is amended only "
                    "while its loop is paused on a CI repair ("
                    + (f"paused on {pause}" if pause else "no loop is paused on this issue")
                    + "); re-import the backlog and start the issue again",
                )
            if loop is None:
                # NEW-201: kept work is resumed only by the same loop (backlog-retry of the
                # loop paused on this issue binds the same ChangeSet under its own custody).
                # A cancelled loop's ChangeSet is never handed to another operation; that
                # case still needs a re-import and a new issue-start (NEW-202).
                raise IssueAmendBlocked(
                    "ISSUE_AMEND_NO_PAUSED_LOOP",
                    f"no backlog loop is paused on {target}, so its kept work could not be "
                    "resumed; re-import the backlog and start the issue again",
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
    closed_run_ids: frozenset[UUID] = frozenset(),
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
        closed_run_ids=closed_run_ids,
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
        if (
            isinstance(amendment, dict)
            and amendment.get("body_sha256") == current_sha256
            # Only a rerun of a confirmation of this very revision reconciles (round 2): a
            # stale prefix from an earlier preview never retries another operator's amendment.
            and confirmation is not None
            and CONFIRMATION_PATTERN.fullmatch(confirmation) is not None
            and current_sha256.startswith(confirmation)
        ):
            # A rerun after an unknown outcome (e.g. a client timeout) reconciles: the
            # recorded amendment is this revision, so a requested retry is still sent.
            result["amended_by"] = amendment.get("amended_by")
            result["retry_workflow_id"] = _retry_target(session, change_set, issue)
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
    # NEW-201: a started, never-published ChangeSet keeps its commits. Checked before any
    # write; the cut-off, the REQUEST_CHANGES move and the amendment commit together.
    cutoff = (
        _record_amendment_cutoff(
            session,
            change_set,
            value,
            actor,
            current_time,
            target,
            # NEW-205: resolve_amend_target admitted published work only for a loop paused
            # on a CI repair.
            published=started_unpublished_blocker(session, change_set) is not None,
        )
        if change_set is not None and not change_set_unstarted(change_set)
        else None
    )
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
            **({"change_set_cutoff": cutoff} if cutoff is not None else {}),
        },
    )
    session.add(event)
    session.flush()
    return {
        **base,
        **({"change_set_cutoff": cutoff} if cutoff is not None else {}),
        "status": "AMENDED",
        "change": change,
        "amended_by": actor,
        "reason": AMEND_REASON,
        "previous_body_sha256": approved_sha256,
        "audit_event_id": event.id,
        "retry_workflow_id": _retry_target(session, change_set, issue),
        "retry_key": value["binding_sha256"][:32],
    }


def _record_amendment_cutoff(
    session: Session,
    change_set: ChangeSet,
    amendment: dict[str, Any],
    actor: str,
    now: datetime,
    target: str,
    *,
    published: bool = False,
) -> dict[str, Any]:
    """NEW-201: amend in place a started, never-published ChangeSet (re-proven by
    resolve_amend_target under the Finding/ChangeSet locks).

    A PLANNED/IMPLEMENTING ChangeSet moves to REQUEST_CHANGES exactly as a NEW-168 reset
    does (version bump, the next child counted in the bounded correction lineage, refused
    when the lineage has no attempt left), so the next delivery child of the same paused
    loop (backlog-retry) may bind it under its existing custody. The cut-off is recorded
    on the ChangeSet: checker and validation context recorded before it is never shown to
    a later maker, and the next resumed maker is told to review the kept commits against
    the regenerated OpenSpec.

    NEW-205: a ``published`` ChangeSet (its PRs open, its loop paused on a CI repair) keeps
    its state and lineage; the record also names the published heads and stays
    ``publication_pending`` until a publication of the amended text."""
    # issue_delivery_approval imports this module (change_set_unstarted).
    from engineering_control.operations.issue_delivery_approval import (
        correction_attempt_budget,
        validate_change_set_issue_approval,
    )

    from_state = change_set.state
    if published:
        # NEW-205: published PRs keep their state; the loop's CI-repair retry restores them
        # (NEW-102) and re-delivers them against the amended text. publication_pending
        # holds every merge until a publication of that text clears it (published_amendment).
        change_set.version += 1
    elif from_state in CORRECTABLE_STATES:
        try:
            if validate_change_set_issue_approval(session, change_set.id, current=False) is None:
                raise ValueError("ChangeSet has no backlog issue approval")
            budget = correction_attempt_budget(session, change_set)
        except (KeyError, TypeError, ValueError) as error:
            raise IssueAmendBlocked(
                "ISSUE_AMEND_APPROVAL_INVALID",
                f"the delivery approval of {target} is not valid ({str(error)[:160]}); "
                "re-import the backlog and start the issue again",
            ) from error
        if budget["remaining"] < 1:
            raise IssueAmendBlocked(
                "ISSUE_AMEND_LINEAGE_EXHAUSTED",
                f"the delivery of {target} used {budget['attempts_used']} of "
                f"{budget['attempt_bound']} correction attempts; an amendment adds none",
            )
        request_changes(change_set, blocker=AMENDMENT_BLOCKER, actor=actor, now=now)
    else:
        # REQUEST_CHANGES already lets the next child bind; the new basis still bumps the
        # version so no stale writer of the previous basis matches it.
        change_set.version += 1
    recovery = dict(change_set.recovery_json or {})
    prior = recovery.get(AMENDMENT_CUTOFF_KEY)
    record: dict[str, Any] = {
        "version": 1,
        "at": now.isoformat(),
        "amendment_binding_sha256": amendment["binding_sha256"],
        "body_sha256": amendment["body_sha256"],
        "previous_body_sha256": amendment["previous_body_sha256"],
        "amended_by": actor,
        "from_state": from_state,
        "to_state": change_set.state,
        "change_set_version": change_set.version,
        "count": (int(prior.get("count", 0)) if isinstance(prior, dict) else 0) + 1,
    }
    if published:
        record.update(
            published=True,
            publication_pending=True,
            source_pr_url=change_set.source_pr_url,
            source_head_sha=change_set.source_head_sha,
            deployment_pr_url=change_set.deployment_pr_url,
            deployment_head_sha=change_set.deployment_head_sha,
        )
    recovery[AMENDMENT_CUTOFF_KEY] = record
    change_set.recovery_json = recovery
    return record


def consumed_issue_revision(
    session: Session, change_set: ChangeSet, run: Run, issue: BacklogIssueRecord
) -> tuple[str, datetime]:
    """The issue revision the publishing delivery run actually loaded (and refreshed to).

    delivery.load_confirmed_finding records it under the delivery-startup lock; a later
    amendment never re-binds a run that started from the previous text. ChangeSets loaded
    before this record existed keep the imported revision. NEW-150 operator touch
    refreshes of this run (:func:`touch_refreshed_revision`) move only its revision time."""
    recorded = (change_set.recovery_json or {}).get(ISSUE_BODY_REVISION_KEY)
    if recorded is None:
        return touch_refreshed_revision(
            session, change_set, run, issue.body_sha256, issue.github_updated_at
        )
    if (
        not isinstance(recorded, dict)
        or recorded.get("run_id") != str(run.id)
        or not isinstance(recorded.get("body_sha256"), str)
        or not _SHA256.fullmatch(recorded["body_sha256"])
        or not isinstance(recorded.get("updated_at"), str)
    ):
        raise ValueError("publication run did not load the approved issue revision")
    return touch_refreshed_revision(
        session,
        change_set,
        run,
        recorded["body_sha256"],
        datetime.fromisoformat(recorded["updated_at"]),
    )


# NEW-150: one insert-only Step per operator touch refresh of one delivery run. Kept out of
# the shared ChangeSet.recovery_json, whose read-modify-write writers could drop it.
TOUCH_REFRESH_STEP = "delivery.issue_touch_refresh"
_TOUCH_REFRESH_TRUSTED = ("change_set_id", "run_id", "step_id")


def touch_refresh_step_id(change_set_id: UUID, run_id: UUID, updated_at: datetime) -> UUID:
    """The deterministic record of one refresh of one run to one GitHub revision."""
    return uuid5(
        NAMESPACE_URL,
        f"issue-touch-refresh:{change_set_id}:{run_id}:{_utc(updated_at).isoformat()}",
    )


def touch_refresh_binding(
    change_set_id: UUID, run_id: UUID, step_id: UUID, value: dict[str, Any]
) -> str:
    """The binding of one touch refresh, over the trusted row identity.

    The trusted ids come last, so a copied record can never supply its own; the reader also
    requires the record's copies of them to equal the row it was read from."""
    return canonical_sha256(
        {
            **{
                key: item
                for key, item in value.items()
                if key != "binding_sha256" and key not in _TOUCH_REFRESH_TRUSTED
            },
            "change_set_id": str(change_set_id),
            "run_id": str(run_id),
            "step_id": str(step_id),
        }
    )


def touch_refreshed_revision(
    session: Session, change_set: ChangeSet, run: Run, body_sha256: str, updated_at: datetime
) -> tuple[str, datetime]:
    """NEW-150: a run's consumed revision with the operator's touch refreshes applied.

    A GitHub edit that restores the exact text (same body hash) still moves the issue's
    ``updated_at``. Each refresh (``engineering-ctl issue-touch-refresh``) is an
    insert-only Step of the run that consumed the revision, naming the body hash it
    re-observed; it moves that run's revision time forward for exactly that body, never
    the body. The run's own record stays as loaded; a malformed refresh fails closed."""
    current = _utc(updated_at)
    rows = session.scalars(
        select(Step)
        .where(Step.run_id == run.id, Step.name == TOUCH_REFRESH_STEP)
        .order_by(Step.created_at, Step.id)
    ).all()
    for row in rows:
        value = row.result_json if isinstance(row.result_json, dict) else {}
        try:
            refreshed = _utc(datetime.fromisoformat(str(value.get("updated_at"))))
        except ValueError as error:
            raise ValueError("issue touch refresh record is invalid") from error
        if (
            row.status != "COMPLETE"
            or value.get("version") != 1
            or value.get("change_set_id") != str(change_set.id)
            or value.get("run_id") != str(run.id)
            or value.get("step_id") != str(row.id)
            or row.id != touch_refresh_step_id(change_set.id, run.id, refreshed)
            or not isinstance(value.get("body_sha256"), str)
            or not _SHA256.fullmatch(value["body_sha256"])
            or value.get("binding_sha256")
            != touch_refresh_binding(change_set.id, run.id, row.id, value)
        ):
            raise ValueError("issue touch refresh record is invalid")
        if value["body_sha256"] == body_sha256 and refreshed > current:
            current = refreshed
    return body_sha256, current


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


def retry_workflow_id(
    session: Session,
    change_set: ChangeSet | None,
    issue: BacklogIssueRecord | None = None,
) -> str | None:
    """The backlog loop that holds this issue's delivery, if durably recorded.

    The ChangeSet's latest delivery child names its parent loop. NEW-128: a loop paused
    before any ChangeSet exists (e.g. CURRENT_REVALIDATION_FLAKY) is resolved from the
    issue's durable records too: the parents of its registered delivery-child approvals,
    the approved issue operation's deterministic loop and the campaign's active loops.
    Every candidate must hold this issue (the retry preflight's campaign check, an
    issue-start loop's own target, and no later child registered for another issue);
    the newest active one wins, and nothing is guessed when none matches."""
    recorded = _change_set_loop(session, change_set)
    if issue is None:
        return recorded
    names = {*_issue_loop_candidates(session, issue)}
    if recorded is not None:
        names.add(recorded)
    active = session.execute(
        select(Run.workflow_id)
        .where(
            Run.workflow_id.in_(sorted(names)),
            Run.run_type == _LOOP_RUN_TYPE,
            Run.status.in_(_ACTIVE_LOOP_STATUSES),
            Run.finished_at.is_(None),
        )
        .order_by(desc(Run.created_at), desc(Run.id))
    ).all()
    for (workflow_id,) in active:
        if _loop_holds_issue(session, workflow_id, issue):
            return str(workflow_id)
    # A recorded loop without an active loop Run (e.g. a legacy row) is kept only while it
    # still holds the issue; the retry preflight then reports its live state.
    if recorded is not None and _loop_holds_issue(session, recorded, issue):
        return recorded
    return None


def paused_issue_loop(
    session: Session, change_set: ChangeSet, issue: BacklogIssueRecord, *, lock: bool = False
) -> str | None:
    """NEW-201: the loop that holds this ChangeSet's validated custody, if it is recorded
    PAUSED (not finished) on exactly this issue; else None.

    Only that loop's backlog-retry can bind the ChangeSet again (any other operation's
    child is refused by custody), so it is also the retry target. Review 3: the custody
    parent (the latest recorded delivery child's loop, after the custody validates), never
    another operation of the same issue; and the loop's durable pause target
    (backlog.loop_pause_target) must name this repository and issue (a missing or other
    target refuses: a campaign loop may be paused on another issue's revalidation)."""
    # Both import this module (issue_delivery_approval directly, correction_extension
    # through it).
    from engineering_control.operations.correction_extension import (
        LOOP_PAUSE_TARGET_STEP,
        loop_pause_target_id,
    )
    from engineering_control.operations.issue_delivery_approval import (
        validate_change_set_issue_approval,
    )

    try:
        if validate_change_set_issue_approval(session, change_set.id, current=False) is None:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    loop = _change_set_loop(session, change_set)
    if loop is None:
        return None
    query = select(Run).where(Run.workflow_id == loop, Run.run_type == _LOOP_RUN_TYPE).limit(2)
    if lock:
        # NEW-205 review: hold the loop's Run row (after the Finding, issue and ChangeSet
        # locks) until the amendment commits. A concurrent resume records RUNNING on this
        # row (backlog.record_loop_state) before it restores, rechecks or merges anything,
        # so it either waits for the amendment or is seen here and refused.
        query = query.with_for_update().execution_options(populate_existing=True)
    runs = session.scalars(query).all()
    if len(runs) != 1 or runs[0].status != "PAUSED" or runs[0].finished_at is not None:
        return None
    pause = session.get(Step, loop_pause_target_id(runs[0].id))
    value = pause.result_json if pause is not None else None
    if (
        pause is None
        or pause.name != LOOP_PAUSE_TARGET_STEP
        or pause.run_id != runs[0].id
        or not isinstance(value, dict)
        or value.get("loop_status") != "PAUSED"
        or value.get("issue") != f"{issue.repository}#{issue.issue_number}"
        or not _loop_holds_issue(session, loop, issue)
    ):
        return None
    return loop


def _loop_pause_blocker(session: Session, loop: str) -> str | None:
    """The blocker recorded with the PAUSED loop's durable pause target (NEW-133), if any.

    Called only for a loop paused_issue_loop has just proven PAUSED on the issue."""
    from engineering_control.operations.correction_extension import loop_pause_target_id

    run = session.scalar(
        select(Run).where(Run.workflow_id == loop, Run.run_type == _LOOP_RUN_TYPE).limit(1)
    )
    pause = session.get(Step, loop_pause_target_id(run.id)) if run is not None else None
    value = pause.result_json if pause is not None else None
    blocker = value.get("blocker") if isinstance(value, dict) else None
    return blocker if isinstance(blocker, str) and blocker else None


def _ci_repair_pause(blocker: str | None) -> bool:
    """NEW-205: whether the loop's backlog-retry re-delivers its published PRs by a CI
    repair: a recorded PR CI regression, or a stopped CI repair (CI_REPAIR_*), whose retry
    rechecks CI first (NEW-101)."""
    return blocker is not None and (
        blocker in _CI_REPAIR_PAUSES or blocker.startswith(_CI_REPAIR_PAUSE_PREFIX)
    )


def _retry_target(
    session: Session, change_set: ChangeSet | None, issue: BacklogIssueRecord
) -> str | None:
    """The loop an amendment's --retry signals: for kept work (a started ChangeSet) only
    the paused custody loop (NEW-201), otherwise the NEW-128 resolution."""
    if change_set is not None and not change_set_unstarted(change_set):
        return paused_issue_loop(session, change_set, issue)
    return retry_workflow_id(session, change_set, issue)


def _change_set_loop(session: Session, change_set: ChangeSet | None) -> str | None:
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


# operations.issue_delivery_approval.STEP_NAME (that module imports this one).
_CHILD_APPROVAL_STEP = "delivery.issue_approval"
_LOOP_RUN_TYPE = "existing-backlog-loop"
_ACTIVE_LOOP_STATUSES = ("RUNNING", "PAUSED")


_ISSUE_START_LOOP = re.compile(
    r"backlog-loop-[a-z]+-[0-9a-f-]{36}-operation-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)


def _loop_holds_issue(session: Session, workflow_id: str, issue: BacklogIssueRecord) -> bool:
    """Whether a backlog loop still owns (or waits on) this issue, from durable records."""
    repository = issue.repository.removeprefix("gntik-ai/")
    if backlog_issue_signal_blocker(session, workflow_id, repository, issue.issue_number):
        return False
    issue_start = _ISSUE_START_LOOP.fullmatch(workflow_id)
    if issue_start is not None:
        operation = session.get(OperationRequest, UUID(issue_start.group(1)))
        if operation is None:
            return False
        try:
            request = issue_request_from_operation(operation)
        except ValueError:
            return False
        if (
            issue_workflow_id(request) != workflow_id
            or request.target_repository != issue.repository
            or request.target_issue_number != issue.issue_number
        ):
            return False
    # A loop that registered a delivery child for another issue after this one moved on.
    latest = session.scalar(
        select(Step)
        .where(
            Step.name == _CHILD_APPROVAL_STEP,
            Step.result_json["parent_workflow_id"].as_string() == workflow_id,
        )
        .order_by(desc(Step.created_at), desc(Step.id))
        .limit(1)
    )
    if latest is not None:
        value = latest.result_json if isinstance(latest.result_json, dict) else {}
        if (value.get("issue_repository"), value.get("issue_number")) != (
            issue.repository,
            issue.issue_number,
        ):
            return False
    return True


def _issue_loop_candidates(session: Session, issue: BacklogIssueRecord) -> list[str]:
    campaign = session.get(GitHubBacklogCampaign, issue.backlog_campaign_id)
    if campaign is None:
        return []
    campaign_id = str(issue.backlog_campaign_id)
    names: list[str] = []
    # Parents of this issue's registered delivery children.
    if issue.finding_id is not None:
        for approval in session.scalars(
            select(Step).where(
                Step.name == _CHILD_APPROVAL_STEP,
                Step.status == "COMPLETE",
                Step.result_json["finding_id"].as_string() == str(issue.finding_id),
            )
        ).all():
            value = approval.result_json if isinstance(approval.result_json, dict) else {}
            request = value.get("request")
            parent = value.get("parent_workflow_id")
            if (
                isinstance(request, dict)
                and str(request.get("backlog_campaign_id")) == campaign_id
                and isinstance(parent, str)
                and parent
            ):
                names.append(parent)
    # The approved issue operations' deterministic loops (/loop issue-start).
    for operation in session.scalars(
        select(OperationRequest).where(
            OperationRequest.action == "solve_issue",
            OperationRequest.project == campaign.project,
            OperationRequest.target_id == f"{issue.repository}#{issue.issue_number}",
        )
    ).all():
        try:
            request = issue_request_from_operation(operation)
        except ValueError:
            continue
        if str(request.backlog_campaign_id) == campaign_id:
            names.append(issue_workflow_id(request))
    # NEW-128 review 2: no campaign-membership fallback. A campaign loop that has not
    # registered a delivery child for this issue (nor is its issue-start loop) does not
    # prove ownership; without positive proof no loop is returned and the operator
    # retries explicitly (/loop retry <workflow-id> <repository> <issue>).
    return names
