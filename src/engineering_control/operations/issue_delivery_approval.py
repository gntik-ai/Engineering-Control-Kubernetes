"""Durable approval custody for backlog delivery children; no external effects."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from engineering_control.api.schemas import ExistingBacklogLoopStart
from engineering_control.db.models import (
    BacklogIssueRecord,
    ChangeSet,
    Finding,
    GitHubBacklogCampaign,
    Run,
    Step,
)
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.engineering.backlog_authority import (
    _current_backlog_approval,
    require_alternative_issue_authority,
)
from engineering_control.operations.issue_approval import (
    require_backlog_issue_approval,
    require_issue_request_approval,
)

STEP_NAME = "delivery.issue_approval"


def _current_snapshot(
    session: Session, request: ExistingBacklogLoopStart, finding: Finding
) -> tuple[BacklogIssueRecord, GitHubBacklogCampaign, str]:
    request = ExistingBacklogLoopStart.model_validate(request.model_dump(mode="json"))
    campaign = session.get(GitHubBacklogCampaign, request.backlog_campaign_id)
    issue = session.execute(
        select(BacklogIssueRecord).where(
            BacklogIssueRecord.backlog_campaign_id == request.backlog_campaign_id,
            BacklogIssueRecord.finding_id == finding.id,
        )
    ).scalar_one_or_none()
    if (
        campaign is None
        or issue is None
        or finding.project != request.project
        or campaign.project != request.project
        or issue.state != "OPEN"
        or issue.eligibility_status == "DEFERRED"
        or finding.risk_class != issue.calculated_risk
        or (
            request.target_repository is not None
            and (
                request.target_repository != issue.repository
                or request.target_issue_number != issue.issue_number
            )
        )
    ):
        raise ValueError("delivery issue and authoritative finding do not match")
    if request.issue_operation_id is not None:
        require_issue_request_approval(session, request)
        approval = _current_backlog_approval(
            campaign,
            delivery_mode=request.delivery_mode,
            maximum_risk=request.maximum_risk.value,
            now=datetime.now(UTC),
        )
        if (
            approval is None
            or approval.issue_approval is None
            or approval.issue_approval.operation_id != str(request.issue_operation_id)
            or approval.issue_approval.binding_sha256 != request.issue_approval_binding
        ):
            raise ValueError("direct child campaign approval differs from its operation")
        require_backlog_issue_approval(session, approval)
    elif request.global_campaign_id is not None or request.r3_approval_binding is not None:
        require_alternative_issue_authority(session, request)
    elif request.target_repository is not None:
        raise ValueError("direct delivery child requires an originating issue approval")
    else:
        approval = _current_backlog_approval(
            campaign,
            delivery_mode=request.delivery_mode,
            maximum_risk=request.maximum_risk.value,
            now=datetime.now(UTC),
        )
        if approval is None or campaign.status == "APPROVED_INVENTORY":
            raise ValueError("delivery child requires its current campaign approval")
        require_backlog_issue_approval(session, approval)
        if request.maximum_items > approval.maximum_items:
            raise ValueError("delivery child exceeds the approved campaign bound")
    ranks = {"R0": 0, "R1": 1, "R2": 2, "R3": 3}
    if ranks.get(issue.calculated_risk, 99) > ranks[request.maximum_risk.value]:
        raise ValueError("delivery child exceeds approved risk")
    if issue.calculated_risk == "R3" and request.r3_approval_binding is None:
        raise ValueError("delivery child requires exact R3 approval")
    snapshot = canonical_sha256(
        {
            "finding_id": str(finding.id),
            "repository": issue.repository,
            "issue_number": issue.issue_number,
            "body_sha256": issue.body_sha256,
            "github_updated_at": issue.github_updated_at.isoformat(),
            "state": issue.state,
            "title": issue.title,
            "risk": issue.calculated_risk,
            "source_repository": campaign.source_repository,
            "deployment_repository": campaign.deployment_repository,
            "source_head": campaign.source_head_at_import,
            "deployment_head": campaign.deployment_head_at_import,
            "source_branch": campaign.source_default_branch,
            "deployment_branch": campaign.deployment_default_branch,
            "plan_hash": campaign.plan_hash,
            "environment": "staging",
            "production": False,
        }
    )
    return issue, campaign, snapshot


def register_child_issue_approval(
    session: Session, payload: dict[str, Any], run: Run, finding: Finding
) -> Step:
    request = ExistingBacklogLoopStart.model_validate(
        {key: payload[key] for key in ExistingBacklogLoopStart.model_fields if key in payload}
    )
    orchestration = payload.get("orchestration")
    parent = orchestration.get("workflow_id") if isinstance(orchestration, dict) else None
    if not isinstance(parent, str) or not parent or len(parent) > 240 or parent == run.workflow_id:
        raise ValueError("delivery child requires its actual parent workflow identity")
    issue, campaign, snapshot = _current_snapshot(session, request, finding)
    value = {
        "version": 1,
        "request": request.model_dump(mode="json"),
        "parent_workflow_id": parent,
        "child_workflow_id": run.workflow_id,
        "finding_id": str(finding.id),
        "issue_repository": issue.repository,
        "issue_number": issue.issue_number,
        "issue_snapshot_sha256": snapshot,
        "campaign_approval_binding": campaign.approval_binding,
    }
    value["binding_sha256"] = canonical_sha256(value)
    identifier = uuid5(NAMESPACE_URL, f"delivery-issue-approval:{run.id}")
    existing = session.get(Step, identifier)
    if existing is not None:
        if (
            existing.run_id != run.id
            or existing.name != STEP_NAME
            or existing.status != "COMPLETE"
            or existing.result_json != value
        ):
            raise ValueError("delivery child approval changed during idempotent retry")
        return existing
    record = Step(
        id=identifier,
        run_id=run.id,
        name=STEP_NAME,
        ordinal=0,
        status="COMPLETE",
        idempotency_key=f"delivery-issue-approval:{run.id}",
        attempt=1,
        result_json=value,
    )
    session.add(record)
    session.flush()
    return record


def validate_child_issue_approval(
    session: Session, approval_id: UUID, *, finding: Finding, run: Run
) -> Step:
    record = session.get(Step, approval_id)
    if (
        record is None
        or record.name != STEP_NAME
        or record.status != "COMPLETE"
        or record.run_id != run.id
        or record.id != uuid5(NAMESPACE_URL, f"delivery-issue-approval:{run.id}")
        or not isinstance(record.result_json, dict)
    ):
        raise ValueError("durable delivery child approval is unavailable")
    value = record.result_json
    keys = {
        "version",
        "request",
        "parent_workflow_id",
        "child_workflow_id",
        "finding_id",
        "issue_repository",
        "issue_number",
        "issue_snapshot_sha256",
        "campaign_approval_binding",
        "binding_sha256",
    }
    if (
        set(value) != keys
        or value["version"] != 1
        or value["child_workflow_id"] != run.workflow_id
        or value["finding_id"] != str(finding.id)
        or value["binding_sha256"]
        != canonical_sha256({key: item for key, item in value.items() if key != "binding_sha256"})
    ):
        raise ValueError("durable delivery child approval binding changed")
    request = ExistingBacklogLoopStart.model_validate(value["request"])
    issue, campaign, snapshot = _current_snapshot(session, request, finding)
    if (
        value["issue_repository"] != issue.repository
        or value["issue_number"] != issue.issue_number
        or value["issue_snapshot_sha256"] != snapshot
        or value["campaign_approval_binding"] != campaign.approval_binding
    ):
        raise ValueError("delivery issue revision or campaign approval changed")
    return record


def approval_reference(record: Step) -> dict[str, str]:
    return {"approval_id": str(record.id), "binding_sha256": record.result_json["binding_sha256"]}


def _approval_scope(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if key not in {"child_workflow_id", "binding_sha256"}
    }


# Main's backlog loop (NEW-99/101/102/103) re-delivers a published ChangeSet after a
# source or deployment PR CI regression: two automatic repair rounds, then one more round
# per operator retry. Every round is durably recorded by the loop (no fixed ceiling on
# operator rounds), but a ChangeSet may record at most this many rounds in total, a
# runaway guard only: falcone #980 needed seven rounds (3-7 operator-started), and each
# round beyond the automatic two needs a separate operator retry, so 20 is about three
# times the worst observed case while still bounding the correction lineage
# (20 x (correction_limit + 1) children at most).
MAX_CI_REPAIR_ROUNDS = 20
# Published states a CI-repair child may start from (it reuses the published branch).
_CI_REPAIR_START_STATES = frozenset({"SOURCE_PR", "SOURCE_MERGED"})
# Durable record the parent loop writes when it decides to start one CI-repair round.
CI_REPAIR_AUTHORIZATION_STEP = "backlog.ci_repair_authorization"
CI_REPAIR_REGRESSIONS = frozenset({"SOURCE_PR_CI_REGRESSION", "DEPLOYMENT_PR_CI_REGRESSION"})
_CI_REPAIR_MARKER = "-ci-repair-"
_CI_REPAIR_SUFFIX = re.compile(
    r"-issue-(?P<ordinal>[1-9][0-9]{0,5})-ci-repair-(?P<round>[1-9][0-9]{0,2})"
    r"-attempt-(?P<attempt>[1-9][0-9]{0,2})"
)


def ci_repair_authorization_id(
    parent_run_id: UUID, change_set_id: UUID, ordinal: int, repair_round: int
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"ci-repair-authorization:{parent_run_id}:{change_set_id}:{ordinal}:{repair_round}",
    )


def _claims_ci_repair(value: dict[str, Any]) -> bool:
    child = value.get("child_workflow_id")
    return isinstance(child, str) and _CI_REPAIR_MARKER in child


def _verify_ci_repair_child(
    session: Session, change_set: ChangeSet, origin: Step, value: dict[str, Any]
) -> None:
    """Prove a CI-repair child from durable facts; its workflow ID only locates them.

    The parent loop must exist with a verified execution binding, must be the loop that
    registered (and so published) this ChangeSet's origin delivery, must have recorded
    the exact repair round for this issue ordinal after a recorded CI regression, and the
    attempt must stay within the origin approval's correction bound.
    """
    parent = value.get("parent_workflow_id")
    child = value.get("child_workflow_id")
    origin_value = origin.result_json
    if (
        not isinstance(parent, str)
        or not isinstance(child, str)
        or origin_value.get("parent_workflow_id") != parent
        or not child.startswith(parent)
    ):
        raise ValueError("CI-repair child is not owned by the loop that published the ChangeSet")
    match = _CI_REPAIR_SUFFIX.fullmatch(child[len(parent) :])
    if match is None:
        raise ValueError("CI-repair child identity is malformed")
    ordinal, repair_round, attempt = (
        int(match["ordinal"]),
        int(match["round"]),
        int(match["attempt"]),
    )
    parents = session.scalars(
        select(Run)
        .where(Run.workflow_id == parent, Run.run_type == "existing-backlog-loop")
        .limit(2)
    ).all()
    if len(parents) != 1 or parents[0].temporal_first_execution_run_id is None:
        raise ValueError("CI-repair parent loop Run is missing or unverified")
    parent_run = parents[0]
    origin_request = ExistingBacklogLoopStart.model_validate(origin_value["request"])
    record = session.get(
        Step, ci_repair_authorization_id(parent_run.id, change_set.id, ordinal, repair_round)
    )
    expected = {
        "version": 1,
        "parent_workflow_id": parent,
        "parent_run_id": str(parent_run.id),
        "parent_first_execution_run_id": parent_run.temporal_first_execution_run_id,
        "backlog_campaign_id": str(origin_request.backlog_campaign_id),
        "change_set_id": str(change_set.id),
        "origin_approval_id": str(origin.id),
        "ordinal": ordinal,
        "repair_round": repair_round,
    }
    if (
        record is None
        or record.name != CI_REPAIR_AUTHORIZATION_STEP
        or record.status != "COMPLETE"
        or record.run_id != parent_run.id
        or not isinstance(record.result_json, dict)
        or {key: record.result_json.get(key) for key in expected} != expected
        or record.result_json.get("regression") not in CI_REPAIR_REGRESSIONS
    ):
        raise ValueError("CI-repair round was not recorded by its parent loop")
    if repair_round > MAX_CI_REPAIR_ROUNDS or attempt > origin_request.correction_limit + 1:
        raise ValueError("CI-repair child exceeds the approved repair bound")
    recorded_rounds = session.scalar(
        select(func.count())
        .select_from(Step)
        .where(
            Step.run_id == parent_run.id,
            Step.name == CI_REPAIR_AUTHORIZATION_STEP,
            Step.result_json["change_set_id"].as_string() == str(change_set.id),
            Step.result_json["ordinal"].as_integer() == ordinal,
        )
    )
    if not recorded_rounds or recorded_rounds > MAX_CI_REPAIR_ROUNDS:
        raise ValueError("CI-repair child exceeds the approved repair bound")


def _lineage_within_bounds(records: list[Step], request: ExistingBacklogLoopStart) -> bool:
    attempts = request.correction_limit + 1
    repairs = sum(1 for record in records if _claims_ci_repair(record.result_json))
    return len(records) - repairs <= attempts and repairs <= MAX_CI_REPAIR_ROUNDS * attempts


def bind_change_set_issue_approval(
    session: Session, change_set: ChangeSet, record: Step
) -> dict[str, str]:
    """Retain the originating approval while recording bounded correction children.

    The caller holds the ChangeSet row lock. Temporal serializes correction attempts;
    this record is custody, not a replacement for active-owner/component fencing.
    """
    reference = approval_reference(record)
    recovery = dict(change_set.recovery_json or {})
    previous = recovery.get("issue_execution_approval")
    if previous is None:
        if change_set.state != "CONFIRMED":
            raise ValueError("in-flight legacy ChangeSet requires explicit safe adoption")
        recovery["issue_execution_approval"] = reference
        recovery["issue_execution_approval_children"] = [reference]
        change_set.recovery_json = recovery
        return reference
    try:
        validated = validate_change_set_issue_approval(session, change_set.id)
    except ValueError as error:
        raise ValueError("ChangeSet already belongs to another issue approval") from error
    if validated is None or validated != previous:
        raise ValueError("ChangeSet already belongs to another issue approval")
    origin = session.get(Step, UUID(previous["approval_id"]))
    if origin is None:
        raise ValueError("ChangeSet originating approval is missing")
    # Child/run IDs change for bounded corrections; their authority must not.
    if _approval_scope(origin.result_json) != _approval_scope(record.result_json):
        raise ValueError("ChangeSet already belongs to another issue approval")
    children = recovery.get("issue_execution_approval_children")
    if not isinstance(children, list) or not children or children[0] != previous:
        raise ValueError("ChangeSet correction approval lineage is malformed")
    request = ExistingBacklogLoopStart.model_validate(origin.result_json["request"])
    if reference not in children:
        recorded = [session.get(Step, UUID(str(child["approval_id"]))) for child in children]
        if any(item is None for item in recorded) or not _lineage_within_bounds(
            [*[item for item in recorded if item is not None], record], request
        ):
            raise ValueError("ChangeSet exceeds the approved correction limit")
        # A correction child starts from REQUEST_CHANGES. A CI-repair child, proven from
        # its parent loop's durable records (never from its ID alone), may also start from
        # the published states a source/deployment PR CI regression leaves.
        repair = _claims_ci_repair(record.result_json)
        if repair:
            _verify_ci_repair_child(session, change_set, origin, record.result_json)
        if change_set.state != "REQUEST_CHANGES" and not (
            repair and change_set.state in _CI_REPAIR_START_STATES
        ):
            raise ValueError("another delivery child requires an explicit correction state")
        children = [*children, reference]
    recovery["issue_execution_approval_children"] = children
    change_set.recovery_json = recovery
    return validated


def validate_change_set_issue_approval(
    session: Session, change_set_id: UUID
) -> dict[str, str] | None:
    change_set = session.get(ChangeSet, change_set_id)
    if change_set is None:
        raise ValueError("delivery ChangeSet is unavailable")
    finding = session.get(Finding, change_set.finding_id)
    if finding is None:
        raise ValueError("delivery ChangeSet finding is unavailable")
    reference = (change_set.recovery_json or {}).get("issue_execution_approval")
    if reference is None:
        if (
            session.scalar(
                select(BacklogIssueRecord.id)
                .where(BacklogIssueRecord.finding_id == finding.id)
                .limit(1)
            )
            is not None
        ):
            raise ValueError("backlog ChangeSet is missing its durable issue approval")
        return None
    if not isinstance(reference, dict) or set(reference) != {"approval_id", "binding_sha256"}:
        raise ValueError("ChangeSet issue approval reference is malformed")
    record = session.get(Step, UUID(str(reference["approval_id"])))
    run = session.get(Run, record.run_id) if record is not None else None
    if run is None or run.change_set_id != change_set.id:
        raise ValueError("ChangeSet issue approval run binding changed")
    valid = validate_child_issue_approval(
        session, UUID(reference["approval_id"]), finding=finding, run=run
    )
    if approval_reference(valid) != reference:
        raise ValueError("ChangeSet issue approval digest changed")
    children = (change_set.recovery_json or {}).get("issue_execution_approval_children")
    request = ExistingBacklogLoopStart.model_validate(valid.result_json["request"])
    if (
        not isinstance(children, list)
        or not children
        or children[0] != reference
        or len(children) > (MAX_CI_REPAIR_ROUNDS + 1) * (request.correction_limit + 1)
    ):
        raise ValueError("ChangeSet correction approval lineage is malformed")
    seen = set()
    lineage: list[Step] = []
    for child in children:
        if not isinstance(child, dict) or set(child) != {"approval_id", "binding_sha256"}:
            raise ValueError("ChangeSet correction approval reference is malformed")
        child_id = UUID(str(child["approval_id"]))
        if child_id in seen:
            raise ValueError("ChangeSet correction approval lineage is duplicated")
        seen.add(child_id)
        child_record = session.get(Step, child_id)
        child_run = session.get(Run, child_record.run_id) if child_record is not None else None
        if child_run is None or child_run.change_set_id != change_set.id:
            raise ValueError("ChangeSet correction run binding changed")
        child_record = validate_child_issue_approval(
            session, child_id, finding=finding, run=child_run
        )
        if approval_reference(child_record) != child or _approval_scope(
            child_record.result_json
        ) != _approval_scope(valid.result_json):
            raise ValueError("ChangeSet correction authority changed")
        if _claims_ci_repair(child_record.result_json):
            _verify_ci_repair_child(session, change_set, valid, child_record.result_json)
        lineage.append(child_record)
    if not _lineage_within_bounds(lineage, request):
        raise ValueError("ChangeSet correction approval lineage is malformed")
    return approval_reference(valid)
