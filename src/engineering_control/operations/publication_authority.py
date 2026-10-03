"""Durable publication intent and fail-closed effect custody on the controller.

No GitHub command or provider work runs here. An unresolved effect is not a lease
that may be silently reassigned merely because a heartbeat or timeout expired.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.db.models import (
    BacklogIssueRecord,
    ChangeSet,
    ComponentLease,
    Finding,
    Run,
    Step,
    WorktreeLease,
)
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.leases.component import ComponentLeaseManager
from engineering_control.models.publication_authority import (
    PublicationEffectClaim,
    PublicationEffectReceipt,
)
from engineering_control.operations.issue_amend import consumed_issue_revision
from engineering_control.operations.issue_delivery_approval import (
    approval_reference,
    validate_change_set_issue_approval,
    validate_child_issue_approval,
)

INTENT_STEP = "delivery.publication_intent"
EFFECT_STEP = "delivery.publication_effect"
ATTEMPT_STEP = "delivery.publication_attempt"
UNRESOLVED_EFFECT_STATES = ("STARTED", "UNKNOWN")
# A non-attempt does not grant unlimited retries. This bound is independent of
# activity retry numbering, which can change across workflow recovery.
MAX_EFFECT_ADMISSIONS = 5


class PublicationEffectUnresolved(ValueError):
    """The durable effect cannot be reported as safely unperformed or retried."""


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _bounded_transaction(session: Session) -> None:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(text("SET LOCAL lock_timeout = '5s'"))
        session.execute(text("SET LOCAL statement_timeout = '10s'"))


def _lock_changeset(session: Session, identifier: UUID) -> ChangeSet:
    # Read only scalar locator values first; do not preload ORM authority objects
    # before acquiring the same Finding-first lock used by child registration.
    finding_id = session.scalar(select(ChangeSet.finding_id).where(ChangeSet.id == identifier))
    if finding_id is None:
        raise ValueError("publication ChangeSet is unavailable")
    finding = session.scalar(select(Finding).where(Finding.id == finding_id).with_for_update())
    if finding is None:
        raise ValueError("publication Finding is unavailable")
    ComponentLeaseManager()._serialize_scope(session, f"implementation:{finding.project}")
    change_set = session.scalar(
        select(ChangeSet).where(ChangeSet.id == identifier).with_for_update()
    )
    if change_set is None:
        raise ValueError("publication ChangeSet is unavailable")
    return change_set


def _current_authority(
    session: Session, change_set: ChangeSet, value: dict[str, Any], now: datetime
) -> tuple[Run, BacklogIssueRecord]:
    run = session.get(Run, UUID(value["run_id"]))
    if (
        run is None
        or run.change_set_id != change_set.id
        or run.status != "RUNNING"
        or run.finished_at is not None
        or run.workflow_id != value["workflow_id"]
    ):
        raise ValueError("publication run is not the active bound owner")
    reference = validate_change_set_issue_approval(session, change_set.id)
    if reference is None or reference != value["origin_approval"]:
        raise ValueError("publication requires its current durable issue approval")
    finding = session.get(Finding, change_set.finding_id)
    if finding is None:
        raise ValueError("publication Finding is unavailable")
    child = validate_child_issue_approval(
        session, UUID(value["child_approval_id"]), finding=finding, run=run
    )
    if approval_reference(child) not in change_set.recovery_json.get(
        "issue_execution_approval_children", []
    ):
        raise ValueError("publication child is outside the approved correction lineage")
    request = child.result_json["request"]
    issue = session.scalar(
        select(BacklogIssueRecord).where(
            BacklogIssueRecord.backlog_campaign_id == UUID(request["backlog_campaign_id"]),
            BacklogIssueRecord.finding_id == finding.id,
        )
    )
    if issue is None:
        raise ValueError("publication issue snapshot is unavailable")
    lease = session.get(ComponentLease, UUID(value["component_lease_id"]))
    if (
        lease is None
        or lease.owner_run_id != run.id
        or lease.mode != "implementation"
        or lease.released_at is not None
        or _utc(lease.expires_at) <= now
        or lease.component_key != value["component_key"]
    ):
        raise ValueError("publication component ownership expired or changed")
    for publication in value["plan"]["publications"]:
        worktree = session.scalar(
            select(WorktreeLease).where(
                WorktreeLease.change_set_id == change_set.id,
                WorktreeLease.repository == publication["repository_slug"],
            )
        )
        if (
            worktree is None
            or worktree.owner_run_id != run.id
            or worktree.released_at is not None
            or _utc(worktree.expires_at) <= now
            or worktree.worktree_path != publication["working_directory"]
            or worktree.branch != publication["branch"]
        ):
            raise ValueError("publication worktree ownership expired or changed")
    return run, issue


def persist_publication_intent(
    session: Session, payload: dict[str, Any], plan: dict[str, Any]
) -> dict[str, str]:
    # Late import avoids coupling the worker's model imports to a controller DB
    # connection. The typed validator is the exact worker publication contract.
    from engineering_control.activities.delivery import PublicationPlan

    _bounded_transaction(session)
    typed_plan = PublicationPlan.model_validate(plan).model_dump(mode="json")
    change_set = _lock_changeset(session, UUID(str(payload["change_set_id"])))
    if str(change_set.id) != typed_plan["change_set_id"]:
        raise ValueError("publication plan ChangeSet differs from its owner")
    context = payload.get("orchestration")
    if not isinstance(context, dict):
        raise ValueError("publication requires actual workflow context")
    value = dict(
        version=1,
        change_set_id=str(change_set.id),
        run_id=str(UUID(str(payload["run_id"]))),
        workflow_id=str(context["workflow_id"]),
        workflow_run_id=str(UUID(str(context["workflow_run_id"]))),
        child_approval_id=str(UUID(str(payload["issue_approval_id"]))),
        origin_approval=payload["issue_execution_approval"],
        component_lease_id=str(UUID(str(payload["component_lease_id"]))),
        component_key=str(payload["component_key"]),
        plan=typed_plan,
        plan_sha256=canonical_sha256(typed_plan),
        environment="staging",
        production=False,
    )
    run, issue = _current_authority(session, change_set, value, datetime.now(UTC))
    # Do not resolve a paired deployment issue number against the source repo.
    value.update(issue_repository=issue.repository, issue_number=issue.issue_number)
    value["binding_sha256"] = canonical_sha256(value)
    identifier = uuid5(NAMESPACE_URL, f"publication-intent:{run.id}:{value['plan_sha256']}")
    previous = session.get(Step, identifier)
    if previous is not None:
        if (
            previous.name != INTENT_STEP
            or previous.run_id != run.id
            or previous.status != "PREPARED"
            or previous.result_json != value
        ):
            raise ValueError("publication intent differs from its immutable predecessor")
    else:
        session.add(
            Step(
                id=identifier,
                run_id=run.id,
                name=INTENT_STEP,
                ordinal=0,
                status="PREPARED",
                idempotency_key=f"publication-intent:{identifier}",
                attempt=1,
                result_json=value,
            )
        )
        session.flush()
    return {
        "intent_id": str(identifier),
        "intent_binding": value["binding_sha256"],
        "plan_sha256": value["plan_sha256"],
        "issue_repository": issue.repository,
        "issue_number": str(issue.issue_number),
    }


class PublicationAuthorityRepository:
    """Only scoped delivery identities may claim/acknowledge existing intents."""

    def __init__(self, database_url: str) -> None:
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=1,
            connect_args={"connect_timeout": 5} if database_url.startswith("postgresql") else {},
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _subject(subject: str) -> None:
        if subject not in {"delivery-runner", "github-runner"}:
            raise PermissionError("publication authority requires a scoped delivery identity")

    def claim(self, subject: str, request: PublicationEffectClaim) -> dict[str, Any]:
        self._subject(subject)
        with self.sessions.begin() as session:
            _bounded_transaction(session)
            # Locator only; authority objects are loaded after the Finding lock.
            raw = session.scalar(
                select(Step.result_json).where(
                    Step.id == request.intent_id, Step.name == INTENT_STEP
                )
            )
            if not isinstance(raw, dict):
                raise ValueError("publication intent is unavailable")
            change_set = _lock_changeset(session, UUID(raw["change_set_id"]))
            intent = session.get(Step, request.intent_id)
            if intent is None or intent.status != "PREPARED":
                raise ValueError("publication intent is unavailable")
            value = intent.result_json
            expected_hash = canonical_sha256(
                {key: item for key, item in value.items() if key != "binding_sha256"}
            )
            if (
                value["binding_sha256"] != expected_hash
                or request.intent_binding != expected_hash
                or value["plan_sha256"] != canonical_sha256(value["plan"])
                or request.plan_sha256 != value["plan_sha256"]
                or request.execution.workflow_id != value["workflow_id"]
                or str(request.execution.workflow_run_id) != value["workflow_run_id"]
                or value["environment"] != "staging"
                or value["production"] is not False
            ):
                raise ValueError("publication intent binding changed")
            if request.repository not in {
                item["repository"] for item in value["plan"]["publications"]
            }:
                raise ValueError("publication effect target is outside its intent")
            effect_id = uuid5(
                NAMESPACE_URL,
                f"publication-effect:{intent.id}:{request.repository}:{request.action}",
            )
            previous = session.scalar(select(Step).where(Step.id == effect_id).with_for_update())
            # Time sampled before waiting for ownership or receipt locks can admit an
            # already expired lease. Re-evaluate against post-lock wall time.
            now = datetime.now(UTC)
            run, issue = _current_authority(session, change_set, value, now)
            # NEW-115: the issue revision this run loaded (an operator amendment applies
            # only to runs loaded after it).
            approved_body_sha256, approved_updated_at = consumed_issue_revision(
                change_set, run, issue
            )
            observation = request.observation
            if (
                not -2 <= (now - observation.observed_at).total_seconds() <= 10
                or observation.repository != issue.repository
                or observation.number != issue.issue_number
                or observation.state != issue.state
                or issue.state != "OPEN"
                or observation.body_sha256 != approved_body_sha256
                or observation.title_sha256 != hashlib.sha256(issue.title.encode()).hexdigest()
                or _utc(observation.updated_at) != _utc(approved_updated_at)
            ):
                raise ValueError("fresh GitHub issue observation differs from approval")
            admission_number = 1
            if previous is not None:
                if previous.name != EFFECT_STEP or previous.run_id != run.id:
                    raise ValueError("publication effect custody changed")
                if previous.status == "COMPLETE":
                    return {"status": "ALREADY_COMPLETE", "effect_id": str(effect_id)}
                # Includes lost claim/ack response, worker death and stale attempt.
                # Do not infer absence of an external effect from a timeout.
                if previous.status != "NOT_ATTEMPTED":
                    raise PublicationEffectUnresolved(
                        "publication effect requires reconciliation before retry"
                    )
                terminal = PublicationEffectReceipt.model_validate(
                    previous.result_json.get("receipt")
                )
                if (
                    terminal.outcome != "NOT_ATTEMPTED"
                    or terminal.effect_id != effect_id
                    or str(terminal.claim_id) != previous.result_json.get("claim_id")
                    or previous.result_json.get("subject") != subject
                ):
                    raise ValueError("publication non-attempt custody changed")
                count = previous.result_json.get("admission_number", 1)
                if type(count) is not int or not 1 <= count < MAX_EFFECT_ADMISSIONS:
                    raise ValueError("publication admission budget is exhausted or invalid")
                admission_number = count + 1
            unresolved = session.scalar(
                select(Step.id)
                .join(Run, Run.id == Step.run_id)
                .where(
                    Run.project == run.project,
                    Step.name == EFFECT_STEP,
                    Step.status.in_(UNRESOLVED_EFFECT_STATES),
                )
                .limit(1)
            )
            if unresolved is not None:
                raise PublicationEffectUnresolved(
                    "an unresolved publication effect still owns this project"
                )
            claim_id = uuid4()
            deadline = now + timedelta(seconds=15)
            if previous is not None:
                # Preserve the complete predecessor observation before replacing
                # only the current projection. Old claim IDs cannot acknowledge
                # this new attempt, including delayed worker completions.
                archive_id = uuid5(
                    NAMESPACE_URL,
                    f"publication-attempt:{effect_id}:{previous.result_json['claim_id']}",
                )
                if session.get(Step, archive_id) is not None:
                    raise ValueError("publication attempt archive already exists")
                session.add(
                    Step(
                        id=archive_id,
                        run_id=previous.run_id,
                        name=ATTEMPT_STEP,
                        ordinal=previous.ordinal,
                        status=previous.status,
                        idempotency_key=f"publication-attempt:{archive_id}",
                        attempt=previous.attempt,
                        result_json=previous.result_json,
                    )
                )
                effect = previous
            else:
                effect = Step(
                    id=effect_id,
                    run_id=run.id,
                    name=EFFECT_STEP,
                    ordinal=0,
                    idempotency_key=f"publication-effect:{effect_id}",
                )
                session.add(effect)
            effect.status = "STARTED"
            effect.attempt = request.execution.attempt
            effect.result_json = dict(
                intent_id=str(intent.id),
                subject=subject,
                claim_id=str(claim_id),
                request=request.model_dump(mode="json"),
                admitted_at=now.isoformat(),
                start_before=deadline.isoformat(),
                admission_number=admission_number,
            )
            session.flush()
            return {
                "status": "CLAIMED",
                "effect_id": str(effect_id),
                "claim_id": str(claim_id),
                "start_before": deadline.isoformat(),
            }

    def receipt(self, subject: str, request: PublicationEffectReceipt) -> dict[str, str]:
        self._subject(subject)
        with self.sessions.begin() as session:
            _bounded_transaction(session)
            effect = session.scalar(
                select(Step).where(Step.id == request.effect_id).with_for_update()
            )
            if (
                effect is None
                or effect.name != EFFECT_STEP
                or effect.result_json.get("subject") != subject
            ):
                raise ValueError("publication receipt does not own the effect")
            value = request.model_dump(mode="json")
            if effect.result_json.get("claim_id") != str(request.claim_id):
                archive_id = uuid5(
                    NAMESPACE_URL, f"publication-attempt:{effect.id}:{request.claim_id}"
                )
                archived = session.get(Step, archive_id)
                if (
                    archived is None
                    or archived.name != ATTEMPT_STEP
                    or archived.run_id != effect.run_id
                    or archived.status != "NOT_ATTEMPTED"
                    or archived.result_json.get("subject") != subject
                    or archived.result_json.get("claim_id") != str(request.claim_id)
                    or archived.result_json.get("receipt") != value
                ):
                    raise ValueError("publication receipt does not own this attempt")
                # Exact delayed receipt replay is harmless; never overwrite the
                # retained terminal observation or the newer current claim.
                return {"status": "NOT_ATTEMPTED", "effect_id": str(effect.id)}
            previous = effect.result_json.get("receipt")
            if previous is not None:
                if previous != value:
                    raise ValueError("publication effect receipt cannot be rewritten")
                return {"status": effect.status, "effect_id": str(effect.id)}
            if effect.status != "STARTED":
                raise ValueError("publication effect cannot accept this receipt")
            # Persist actual terminal observations even if approval was revoked
            # after admission. Never falsify a completed effect as not performed.
            effect.result_json = {
                **effect.result_json,
                "receipt": value,
                "received_at": datetime.now(UTC).isoformat(),
            }
            effect.status = request.outcome
            return {"status": effect.status, "effect_id": str(effect.id)}
