"""NEW-133: one operator-approved extra correction attempt for a validation-blocked ChangeSet.

A backlog delivery's correction lineage is bounded at ``correction_limit + 1`` attempts
(issue_delivery_approval). When the last of them was PASSed by the independent checker
and stopped only by Hermes's own deterministic validation findings (NEW-76), the
operator may grant exactly one more attempt. Every precondition is re-read here from
durable records (ChangeSet, approval Steps, Runs, AgentInvocations), never from the
client; the grant is stored once in ``ChangeSet.recovery_json["correction_extension"]``
and audited as a DashboardEvent. The operator's existing ``backlog-retry`` then resumes
the loop, whose next delivery child binds under the extended bound.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.db.models import (
    AgentInvocation,
    BacklogIssueRecord,
    ChangeSet,
    DashboardEvent,
    Run,
)
from engineering_control.engineering.backlog import BACKLOG_PROJECT_PAIRS, canonical_sha256
from engineering_control.models.enums import ChangeSetState
from engineering_control.operations.issue_delivery_approval import (
    CORRECTION_EXTENSION_KEY,
    claims_ci_repair,
    correction_extension_binding,
    correction_lineage,
    validate_change_set_issue_approval,
)
from engineering_control.operations.issue_start import backlog_issue_signal_blocker

EXTENSION_EVENT_TYPE = "correction_extension_granted"
VALIDATION_BLOCKER = "CHANGESET_VALIDATION_FINDINGS"
CHECKER_ROLE = "code-reviewer"
_LOOP_CHILD = re.compile(r"-issue-(?P<ordinal>[1-9][0-9]{0,5})-")


class CorrectionExtensionBlocked(ValueError):
    """A refused extension: a stable blocker code and operator guidance."""

    def __init__(self, blocker: str, detail: str) -> None:
        super().__init__(f"{blocker}: {detail}")
        self.blocker = blocker
        self.detail = detail


def confirmation_phrase(change_set_id: UUID) -> str:
    return f"EXTEND_CORRECTION_ONCE_{change_set_id.hex[:8].upper()}"


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _issue_ordinal(loop_workflow_id: str, child_workflow_id: str) -> int | None:
    if not child_workflow_id.startswith(loop_workflow_id):
        return None
    match = _LOOP_CHILD.match(child_workflow_id[len(loop_workflow_id) :])
    return int(match["ordinal"]) if match is not None else None


def grant_correction_extension(
    session: Session,
    *,
    loop_workflow_id: str,
    project: str,
    issue_number: int,
    change_set_id: UUID,
    confirmation: str,
    actor: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Grant one extra correction attempt, or raise CorrectionExtensionBlocked.

    Runs in the caller's transaction and locks the ChangeSet row first, so two grants
    (or a grant racing a delivery child's binding) serialize on it."""
    current_time = now or datetime.now(UTC)
    if actor is None:
        raise CorrectionExtensionBlocked(
            "CORRECTION_EXTENSION_OPERATOR_REQUIRED", "an operator identity is required"
        )
    if confirmation != confirmation_phrase(change_set_id):
        raise CorrectionExtensionBlocked(
            "CORRECTION_EXTENSION_CONFIRMATION_MISMATCH",
            f"confirm with {confirmation_phrase(change_set_id)}",
        )
    change_set = session.scalar(
        select(ChangeSet).where(ChangeSet.id == change_set_id).with_for_update()
    )
    if change_set is None or change_set.project != project:
        raise CorrectionExtensionBlocked(
            "CORRECTION_EXTENSION_CHANGESET_NOT_FOUND",
            f"no {project} ChangeSet {change_set_id}",
        )
    recovery = dict(change_set.recovery_json or {})
    if recovery.get(CORRECTION_EXTENSION_KEY) is not None:
        raise CorrectionExtensionBlocked(
            "CORRECTION_EXTENSION_ALREADY_GRANTED",
            "this ChangeSet already received its one extra correction attempt",
        )
    if change_set.state != ChangeSetState.REQUEST_CHANGES.value:
        raise CorrectionExtensionBlocked(
            "CORRECTION_EXTENSION_STATE",
            f"the ChangeSet is {change_set.state}, not REQUEST_CHANGES",
        )
    try:
        reference = validate_change_set_issue_approval(session, change_set.id, current=False)
        if reference is None:
            raise ValueError("ChangeSet has no backlog issue approval")
        origin, request, lineage = correction_lineage(session, change_set)
    except (KeyError, TypeError, ValueError) as error:
        raise CorrectionExtensionBlocked(
            "CORRECTION_EXTENSION_APPROVAL_INVALID",
            f"the ChangeSet's originating approval is not valid ({str(error)[:160]})",
        ) from error
    value = origin.result_json
    issue_repository = str(value["issue_repository"])
    if (
        value["parent_workflow_id"] != loop_workflow_id
        or value["issue_number"] != issue_number
        or issue_repository not in BACKLOG_PROJECT_PAIRS.get(project, ())
        or request.project != project
    ):
        raise CorrectionExtensionBlocked(
            "CORRECTION_EXTENSION_TARGET_MISMATCH",
            "the ChangeSet does not belong to that loop, project and issue",
        )
    issue_blocker = backlog_issue_signal_blocker(
        session,
        loop_workflow_id,
        issue_repository.removeprefix("gntik-ai/"),
        issue_number,
    )
    issue_record = session.scalar(
        select(BacklogIssueRecord).where(
            BacklogIssueRecord.backlog_campaign_id == request.backlog_campaign_id,
            BacklogIssueRecord.finding_id == change_set.finding_id,
            BacklogIssueRecord.repository == issue_repository,
            BacklogIssueRecord.issue_number == issue_number,
        )
    )
    if issue_blocker is not None or issue_record is None or issue_record.state != "OPEN":
        raise CorrectionExtensionBlocked(
            "CORRECTION_EXTENSION_TARGET_MISMATCH",
            "the issue is not an open issue of that loop's campaign",
        )
    attempts = [record for record in lineage if not claims_ci_repair(record)]
    bound = request.correction_limit + 1
    if len(attempts) != bound:
        raise CorrectionExtensionBlocked(
            "CORRECTION_EXTENSION_LINEAGE_NOT_EXHAUSTED",
            f"the ChangeSet used {len(attempts)} of {bound} correction attempts; only an "
            "exhausted lineage can be extended (use backlog-retry)",
        )
    latest = lineage[-1]
    if claims_ci_repair(latest):
        raise CorrectionExtensionBlocked(
            "CORRECTION_EXTENSION_NOT_VALIDATION_BLOCKED",
            "the latest attempt was a CI-repair round",
        )
    latest_run = session.get(Run, latest.run_id)
    requested = recovery.get("correction_requested")
    if (
        latest_run is None
        or not isinstance(requested, dict)
        or requested.get("run_id") != str(latest_run.id)
        or requested.get("status") != "REQUEST_CHANGES"
        or requested.get("blocker") != VALIDATION_BLOCKER
    ):
        raise CorrectionExtensionBlocked(
            "CORRECTION_EXTENSION_NOT_VALIDATION_BLOCKED",
            f"the latest attempt was not stopped by {VALIDATION_BLOCKER}",
        )
    recorded = recovery.get("validation_findings")
    findings = recorded.get("findings") if isinstance(recorded, dict) else None
    recorded_at = recorded.get("recorded_at") if isinstance(recorded, dict) else None
    try:
        recorded_time = _utc(datetime.fromisoformat(str(recorded_at)))
    except ValueError:
        recorded_time = None
    if (
        not isinstance(findings, list)
        or not findings
        or not all(isinstance(item, str) and item for item in findings)
        or recorded_time is None
        or recorded_time < _utc(latest_run.created_at)
    ):
        raise CorrectionExtensionBlocked(
            "CORRECTION_EXTENSION_FINDINGS_MISSING",
            "the latest attempt has no recorded validation findings",
        )
    checker = session.scalar(
        select(AgentInvocation)
        .where(AgentInvocation.run_id == latest_run.id, AgentInvocation.role == CHECKER_ROLE)
        .order_by(AgentInvocation.started_at.desc(), AgentInvocation.id.desc())
        .limit(1)
    )
    if (
        checker is None
        or checker.status != "SUCCESS"
        or not isinstance(checker.structured_result, dict)
        or checker.structured_result.get("decision") != "PASS"
    ):
        raise CorrectionExtensionBlocked(
            "CORRECTION_EXTENSION_CHECKER_NOT_PASS",
            "the independent checker of the latest attempt did not PASS",
        )
    loops = session.scalars(
        select(Run)
        .where(Run.workflow_id == loop_workflow_id, Run.run_type == "existing-backlog-loop")
        .limit(2)
    ).all()
    if len(loops) != 1 or loops[0].status != "PAUSED":
        raise CorrectionExtensionBlocked(
            "CORRECTION_EXTENSION_LOOP_NOT_PAUSED",
            "the backlog loop is not PAUSED",
        )
    ordinal = _issue_ordinal(loop_workflow_id, str(latest_run.workflow_id))
    newest_child = session.scalar(
        select(Run)
        .where(Run.workflow_id.startswith(f"{loop_workflow_id}-issue-", autoescape=True))
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(1)
    )
    if (
        ordinal is None
        or newest_child is None
        or _issue_ordinal(loop_workflow_id, str(newest_child.workflow_id)) != ordinal
    ):
        raise CorrectionExtensionBlocked(
            "CORRECTION_EXTENSION_LOOP_NOT_PAUSED",
            "the backlog loop is not paused on this issue",
        )
    record: dict[str, Any] = {
        "version": 1,
        "change_set_id": str(change_set.id),
        "origin_approval_id": reference["approval_id"],
        "loop_workflow_id": loop_workflow_id,
        "issue_repository": issue_repository,
        "issue_number": issue_number,
        "extends_attempt": bound,
        "extended_attempt_limit": bound + 1,
        "latest_attempt_run_id": str(latest_run.id),
        "latest_attempt_child_workflow_id": str(latest_run.workflow_id),
        "checker_invocation_id": str(checker.id),
        "validation_findings_sha256": canonical_sha256({"findings": findings}),
        "validation_findings_count": len(findings),
        "granted_by": actor,
        "granted_at": current_time.isoformat(),
    }
    record["binding_sha256"] = correction_extension_binding(record)
    recovery[CORRECTION_EXTENSION_KEY] = record
    change_set.recovery_json = recovery
    event = DashboardEvent(
        occurred_at=current_time,
        level="WARNING",
        event_type=EXTENSION_EVENT_TYPE,
        source="controller",
        host=None,
        worker_kind="controller",
        project=project,
        workflow_id=loop_workflow_id,
        temporal_run_id=None,
        issue_number=issue_number,
        campaign_id=str(request.backlog_campaign_id),
        entity_type="change_set",
        entity_id=str(change_set.id),
        current_phase="CORRECTION_EXTENDED",
        message="One extra correction attempt granted by operator",
        metadata_json={
            key: record[key]
            for key in (
                "granted_by",
                "issue_repository",
                "extends_attempt",
                "extended_attempt_limit",
                "latest_attempt_run_id",
                "validation_findings_sha256",
                "validation_findings_count",
                "binding_sha256",
            )
        },
    )
    session.add(event)
    session.flush()
    short = issue_repository.removeprefix("gntik-ai/")
    return {
        "status": "CORRECTION_EXTENDED",
        "change_set_id": str(change_set.id),
        "loop_workflow_id": loop_workflow_id,
        "target": f"{issue_repository}#{issue_number}",
        "extends_attempt": bound,
        "extended_attempt_limit": bound + 1,
        "validation_findings_sha256": record["validation_findings_sha256"],
        "validation_findings_count": len(findings),
        "granted_by": actor,
        "audit_event_id": event.id,
        "binding_sha256": record["binding_sha256"],
        "next": (f"engineering-ctl backlog-retry {loop_workflow_id} {short} {issue_number}"),
        "production": False,
    }
