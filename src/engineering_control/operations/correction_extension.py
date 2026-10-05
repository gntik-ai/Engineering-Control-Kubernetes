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
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.db.models import (
    AgentInvocation,
    BacklogIssueRecord,
    ChangeSet,
    DashboardEvent,
    Run,
    Step,
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
# Durable record of the issue a backlog loop Run is paused on (NEW-133).
LOOP_PAUSE_TARGET_STEP = "backlog.loop_pause_target"
_ISSUE_REF = re.compile(r"gntik-ai/[a-z][a-z-]{0,60}#[1-9][0-9]{0,9}")


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


def loop_pause_target_id(loop_run_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"backlog-loop-pause-target:{loop_run_id}")


def loop_state_fence(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The scheduling order of one backlog.record_loop_state delivery (NEW-133 review 2).

    The SDK numbers a workflow run's activities in scheduling order, so within one run a
    larger activity id is a later loop state. A late attempt of an older activity (one
    delayed past its timeout and retried) carries its original, smaller id."""
    orchestration = payload.get("orchestration")
    if not isinstance(orchestration, dict):
        return None
    run_id = orchestration.get("workflow_run_id")
    sequence = str(orchestration.get("activity_id") or "")
    if not isinstance(run_id, str) or not run_id or not sequence.isdigit():
        return None
    return {"workflow_run_id": run_id, "activity_seq": int(sequence)}


def loop_state_is_stale(session: Session, run: Run, payload: dict[str, Any]) -> bool:
    """Whether this loop-state delivery is older than the one already applied to the Run.

    Same workflow run only (a new run of the loop supersedes); an equal sequence is the
    same activity's retry, which rewrites the same state."""
    fence = loop_state_fence(payload)
    record = session.get(Step, loop_pause_target_id(run.id))
    if fence is None or record is None or not isinstance(record.result_json, dict):
        return False
    # NEW-133 review 3: a run superseded by a newer one (e.g. after a Temporal reset, which
    # keeps the first execution run id) never writes again.
    seen = record.result_json.get("workflow_runs")
    if (
        isinstance(seen, list)
        and fence["workflow_run_id"] in seen
        and fence["workflow_run_id"] != seen[-1]
    ):
        return True
    applied = record.result_json.get("fence")
    return (
        isinstance(applied, dict)
        and applied.get("workflow_run_id") == fence["workflow_run_id"]
        and isinstance(applied.get("activity_seq"), int)
        and fence["activity_seq"] < applied["activity_seq"]
    )


def record_loop_pause_target(
    session: Session,
    run: Run,
    status: str,
    payload: dict[str, Any],
    now: datetime | None = None,
) -> None:
    """Keep the issue (and blocker) a backlog loop Run is PAUSED on, or that it is not.

    Written by backlog.record_loop_state with every loop-state transition of the loop's
    own Run. The issue comes from the loop's NEW-127 loop-state payload; a pause without a
    well-formed issue records none, so nothing can be granted against it (fail closed)."""
    issue = payload.get("issue")
    blocker = payload.get("blocker")
    value = {
        "version": 1,
        "loop_run_id": str(run.id),
        "loop_status": status,
        "issue": (
            issue
            if status == "PAUSED" and isinstance(issue, str) and _ISSUE_REF.fullmatch(issue)
            else None
        ),
        "blocker": str(blocker)[:120] if status == "PAUSED" and blocker else None,
        "recorded_at": (now or datetime.now(UTC)).isoformat(),
        "fence": loop_state_fence(payload),
    }
    identifier = loop_pause_target_id(run.id)
    record = session.get(Step, identifier)
    # The order in which this loop's workflow runs first wrote a state (bounded).
    seen = record.result_json.get("workflow_runs") if record is not None else None
    runs = [str(item) for item in seen if isinstance(item, str)] if isinstance(seen, list) else []
    fence = value["fence"]
    if isinstance(fence, dict) and fence["workflow_run_id"] not in runs:
        runs = [*runs, fence["workflow_run_id"]][-20:]
    value["workflow_runs"] = runs
    if record is None:
        session.add(
            Step(
                id=identifier,
                run_id=run.id,
                name=LOOP_PAUSE_TARGET_STEP,
                ordinal=0,
                status="COMPLETE",
                idempotency_key=f"backlog-loop-pause-target:{run.id}",
                attempt=1,
                result_json=value,
            )
        )
    elif record.run_id == run.id and record.name == LOOP_PAUSE_TARGET_STEP:
        record.result_json = value
    else:
        raise ValueError("backlog loop pause record binding changed")


def grant_correction_extension(
    session: Session,
    *,
    loop_workflow_id: str,
    project: str,
    issue_number: int,
    change_set_id: UUID,
    confirmation: str,
    actor: str | None,
    current_workflow_run_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Grant one extra correction attempt, or raise CorrectionExtensionBlocked.

    `current_workflow_run_id` is the loop's current (latest) Temporal run, read from
    Temporal by the caller; the pause must have been recorded by that run (NEW-133
    review 3), so no state a superseded run wrote can authorize a grant.

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
    # The loop's own durable pause record names the issue it waits on. Child Runs do not:
    # after a skip the next issue may pause (e.g. APPROVAL_EXPIRED) before registering any.
    target = f"{issue_repository}#{issue_number}"
    pause = session.get(Step, loop_pause_target_id(loops[0].id))
    paused = pause.result_json if pause is not None else None
    if (
        pause is None
        or pause.name != LOOP_PAUSE_TARGET_STEP
        or pause.run_id != loops[0].id
        or not isinstance(paused, dict)
        or paused.get("loop_status") != "PAUSED"
        or paused.get("issue") != target
        or not current_workflow_run_id
        or not isinstance(paused.get("fence"), dict)
        or paused["fence"].get("workflow_run_id") != current_workflow_run_id
    ):
        raise CorrectionExtensionBlocked(
            "CORRECTION_EXTENSION_LOOP_NOT_PAUSED",
            f"the backlog loop is not paused on {target}",
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
