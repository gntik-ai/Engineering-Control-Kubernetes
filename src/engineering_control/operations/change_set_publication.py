"""NEW-201: shared predicates for a delivery ChangeSet that started but never published.

NEW-168 (change_reset) first proved "never published" for an operator reset. NEW-201 needs
the same proof for an in-place issue amendment (issue_amend), so the predicate lives here,
below both modules: it imports only the database models, never those modules (change_reset
imports issue_amend). The REQUEST_CHANGES move is the one change_reset made, now shared.
A cancelled loop's ChangeSet is never transferred to another operation (NEW-202).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.db.models import ChangeSet, Deployment, Run, Step
from engineering_control.models.enums import ChangeSetState

# operations.publication_authority.EFFECT_STEP / ATTEMPT_STEP (that module imports
# issue_amend, which imports this one; test_new_201 asserts they stay equal).
PUBLICATION_EFFECT_STEP = "delivery.publication_effect"
PUBLICATION_ATTEMPT_STEP = "delivery.publication_attempt"
# recovery_json records written only from publication on (persist_publication, CI,
# candidate/release, merge, staging, closure): any of them means work left the worktree.
# NEW-168 review 2: not provider_chain, which the local handoff (_persist_delivery_handoff,
# e.g. SOURCE_CHANGES_NOT_COMMITTED) also writes before any publication.
PUBLISHED_RECOVERY_KEYS = frozenset(
    {
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
# A ChangeSet whose delivery started (an OpenSpec or maker work exists) and may still be
# corrected before any publication.
STARTED_UNPUBLISHED_STATES = frozenset(
    {
        ChangeSetState.PLANNED.value,
        ChangeSetState.IMPLEMENTING.value,
        ChangeSetState.REQUEST_CHANGES.value,
    }
)
# The states an operator move to REQUEST_CHANGES starts from (NEW-168 reset, NEW-201 amend).
CORRECTABLE_STATES = frozenset({ChangeSetState.PLANNED.value, ChangeSetState.IMPLEMENTING.value})
# ChangeSet.recovery_json: when an operator amendment re-approved the issue text after the
# ChangeSet's existing work (NEW-201); context recorded before it is never shown again.
AMENDMENT_CUTOFF_KEY = "issue_amendment_cutoff"
AMENDMENT_BLOCKER = "OPERATOR_ISSUE_AMENDMENT"


def published_reason(session: Session, change_set: ChangeSet) -> str | None:
    """Why the ChangeSet may have published work, or None when nothing was published."""
    if change_set.source_pr_url is not None or change_set.deployment_pr_url is not None:
        return "a pull request was published"
    if change_set.image_digests or change_set.chart_version is not None:
        return "release artifacts are recorded"
    recorded = sorted(PUBLISHED_RECOVERY_KEYS.intersection(change_set.recovery_json or {}))
    if recorded:
        return f"publication records exist ({', '.join(recorded)})"
    # NEW-168 review 3: a PREPARED intent alone (persisted before delivery.publish, e.g. a
    # GitHub preflight then failed) is not publication, nor is an attempt or effect durably
    # acknowledged NOT_ATTEMPTED (publication_authority's own proof that its command never
    # ran). Anything else (COMPLETE, UNKNOWN, reconciled, claimed) may have published.
    for name, status, result in session.execute(
        select(Step.name, Step.status, Step.result_json)
        .join(Run, Run.id == Step.run_id)
        .where(
            Run.change_set_id == change_set.id,
            Step.name.in_((PUBLICATION_EFFECT_STEP, PUBLICATION_ATTEMPT_STEP)),
        )
    ):
        receipt = result.get("receipt") if isinstance(result, dict) else None
        outcome = receipt.get("outcome") if isinstance(receipt, dict) else None
        if status != "NOT_ATTEMPTED" or (
            name == PUBLICATION_EFFECT_STEP and outcome != "NOT_ATTEMPTED"
        ):
            return "a publication effect or attempt is recorded"
    deployment = session.scalar(
        select(Deployment.id).where(Deployment.change_set_id == change_set.id).limit(1)
    )
    if deployment is not None:
        return "a deployment is recorded"
    return None


def started_unpublished_blocker(session: Session, change_set: ChangeSet) -> str | None:
    """NEW-201: None for a ChangeSet that started (PLANNED, IMPLEMENTING or
    REQUEST_CHANGES) and never published; otherwise why it is not one."""
    if change_set.state not in STARTED_UNPUBLISHED_STATES:
        return f"the ChangeSet is {change_set.state}"
    return published_reason(session, change_set)


# NEW-205: a published ChangeSet whose PRs wait for a CI repair: SOURCE_PR (where a CI-repair
# child binds, issue_delivery_approval._CI_REPAIR_START_STATES), or moved back by a CI-repair
# attempt that stopped (NEW-66), which the loop's retry restores to SOURCE_PR (NEW-102,
# activities/engineering._REPAIR_INTERMEDIATE_STATES) before its recheck. CI_VALIDATED is
# neither: no repair child binds from it and the restore refuses it.
CI_REPAIRABLE_STATES = frozenset({ChangeSetState.SOURCE_PR.value, *STARTED_UNPUBLISHED_STATES})
# recovery_json records that mean published work went past its open PRs: merged,
# promoted, released or closed. A later issue amendment cannot change that work.
PAST_PULL_REQUEST_KEYS = frozenset(
    {
        "candidate",
        "source_merge",
        "promotion",
        "promotion_component_lease",
        "release_evidence",
        "release_plan_review",
        "rollback_plan",
        "final_closure",
    }
)


def published_repairable_blocker(session: Session, change_set: ChangeSet) -> str | None:
    """NEW-205: None for a published ChangeSet whose work is still only its open PRs,
    which a CI repair re-delivers; otherwise why it is not one."""
    if change_set.state not in CI_REPAIRABLE_STATES:
        return f"the ChangeSet is {change_set.state}"
    if change_set.source_pr_url is None:
        return "no source pull request is recorded"
    if change_set.image_digests or change_set.chart_version is not None:
        return "release artifacts are recorded"
    recovery = change_set.recovery_json or {}
    recorded = sorted(PAST_PULL_REQUEST_KEYS.intersection(recovery))
    if recorded:
        return f"the work went past its pull requests ({', '.join(recorded)})"
    staging = recovery.get("staging_gitops")
    if isinstance(staging, dict) and (
        staging.get("merge_sha") or staging.get("status") in {"STAGING_VERIFIED", "MERGE_DELIVERED"}
    ):
        return "the work was merged"
    deployment = session.scalar(
        select(Deployment.id).where(Deployment.change_set_id == change_set.id).limit(1)
    )
    if deployment is not None:
        return "a deployment is recorded"
    return None


def request_changes(change_set: ChangeSet, *, blocker: str, actor: str, now: datetime) -> str:
    """Move a PLANNED/IMPLEMENTING ChangeSet to REQUEST_CHANGES as an operator correction
    (NEW-168 reset, NEW-201 amendment); returns the state it left.

    Same record a stopped attempt writes (NEW-128/137); the stopped runs stay listed so a
    delayed release of one of them never writes again. The next child binding is counted
    in the bounded correction lineage (bind_change_set_issue_approval): no bypass."""
    if change_set.state not in CORRECTABLE_STATES:
        raise ValueError(f"a {change_set.state} ChangeSet cannot move to REQUEST_CHANGES")
    from_state = change_set.state
    recovery = dict(change_set.recovery_json or {})
    previous_request = recovery.get("correction_requested")
    previous = previous_request if isinstance(previous_request, dict) else {}
    run_ids = [str(item) for item in previous.get("run_ids") or [] if item]
    if previous.get("run_id"):
        run_ids.append(str(previous["run_id"]))
    change_set.state = ChangeSetState.REQUEST_CHANGES.value
    change_set.version += 1
    recovery["correction_requested"] = {
        "run_ids": list(dict.fromkeys(run_ids))[-128:],
        "status": ChangeSetState.REQUEST_CHANGES.value,
        "blocker": blocker,
        "actor": actor,
        "at": now.isoformat(),
        "recorded_at": now.isoformat(),
    }
    change_set.recovery_json = recovery
    return from_state


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def parse_time(value: Any) -> datetime | None:
    """A recorded ISO timestamp as an aware UTC time, or None if absent or malformed."""
    if isinstance(value, datetime):
        return _utc(value)
    if not isinstance(value, str):
        return None
    try:
        return _utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def amendment_cutoff(change_set: ChangeSet | None) -> datetime | None:
    """NEW-201: the time of the ChangeSet's latest in-place issue amendment, if any.

    A malformed record fails closed (ValueError): the maker must never mix context from
    before an amendment it cannot place in time."""
    record = (change_set.recovery_json or {}).get(AMENDMENT_CUTOFF_KEY) if change_set else None
    if record is None:
        return None
    at = parse_time(record.get("at")) if isinstance(record, dict) else None
    if at is None or not isinstance(record, dict) or record.get("version") != 1:
        raise ValueError("ChangeSet issue amendment cut-off is malformed")
    return at


def published_amendment_pending(change_set: ChangeSet) -> bool:
    """NEW-205: whether the issue was amended after the ChangeSet's PRs were published and
    no publication of the amended text has happened since. Such PRs carry work made for
    the previous text: they must be re-delivered by a CI repair, never merged.

    A malformed record fails closed (pending)."""
    return _amendment_pending((change_set.recovery_json or {}).get(AMENDMENT_CUTOFF_KEY))


def _amendment_pending(record: Any) -> bool:
    if record is None:
        return False
    if not isinstance(record, dict) or record.get("version") != 1:
        return True
    if "published" not in record:
        # A NEW-201 cut-off: the ChangeSet had published nothing when it was amended.
        return False
    # Anything but an exact NEW-205 record (booleans, pending unless explicitly delivered)
    # fails closed, an explicit null included.
    return record["published"] is not True or record.get("publication_pending") is not False


def clear_published_amendment(
    recovery: dict[str, Any], run_id: str, consumed_body_sha256: Any, now: datetime
) -> bool:
    """NEW-205: in ``recovery`` (a ChangeSet's recovery_json being written), mark a pending
    published amendment delivered when the publishing run ``run_id`` loaded exactly the
    amended text (``consumed_body_sha256``; publication custody proved it equal to the
    live issue). Returns whether it did."""
    record = recovery.get(AMENDMENT_CUTOFF_KEY)
    if (
        not _amendment_pending(record)
        or not isinstance(record, dict)
        or record.get("version") != 1
        or not isinstance(consumed_body_sha256, str)
        or record.get("body_sha256") != consumed_body_sha256
    ):
        return False
    recovery[AMENDMENT_CUTOFF_KEY] = {
        **record,
        "publication_pending": False,
        "republished_by_run_id": run_id,
        "republished_at": now.isoformat(),
    }
    return True


def recorded_after(record: Any, cutoff: datetime | None) -> bool:
    """Whether a recovery record (with ``recorded_at``) postdates the amendment cut-off.

    Without a cut-off every record counts; with one, a record without a readable time is
    treated as older (excluded)."""
    if cutoff is None:
        return True
    at = parse_time(record.get("recorded_at")) if isinstance(record, dict) else None
    return at is not None and at > cutoff
