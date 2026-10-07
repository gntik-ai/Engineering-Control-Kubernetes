"""NEW-150: re-observe an issue whose GitHub revision time moved but whose text did not.

Publication custody (NEW-115) requires the fresh GitHub observation to equal the consumed
issue revision: the body hash *and* GitHub ``updated_at``. Any GitHub edit of an issue
under delivery moves ``updated_at``, even one that restores the text byte for byte, and
from then on every publication claim is refused. ``issue-amend`` cannot help while the
delivery is active, and amending is for content changes anyway.

``issue-touch-refresh`` reads the issue fresh through the GitHub worker and, only when
the body and title hashes and the label set (the risk classification inputs) equal the
approved revision and the issue is open, records the new revision time:

* on the backlog issue record, as the effective approved revision (the NEW-115 amendment
  record, with ``change: TOUCH_REFRESHED``), so later delivery loads consume it;
* for each delivery run that consumed exactly that body, as an insert-only Step
  (``delivery.issue_touch_refresh``), so it publishes against the new time (see
  :func:`~engineering_control.operations.issue_amend.touch_refreshed_revision`). It is
  kept out of ``ChangeSet.recovery_json``, whose unlocked read-modify-write writers could
  otherwise drop it.

The immutable import (``body_sha256``/``github_updated_at``, bound into every approval
snapshot) and each run's loaded record are never rewritten. A different body or title is
refused: content changes go through ``issue-amend``; a different label set is refused too
(it can change the risk class), as is an R3 issue.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.db.models import (
    BacklogIssueRecord,
    ChangeSet,
    DashboardEvent,
    Finding,
    GitHubBacklogCampaign,
    Run,
    Step,
)
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.models.enums import ChangeSetState
from engineering_control.operations.issue_amend import (
    _SHA256,
    AMENDMENT_KEY,
    CONFIRMATION_PATTERN,
    ISSUE_BODY_REVISION_KEY,
    TOUCH_REFRESH_STEP,
    IssueAmendBlocked,
    _amendment_binding,
    _utc,
    effective_issue_revision,
    locate_issue_target,
    touch_refresh_binding,
    touch_refresh_step_id,
    touch_refreshed_revision,
)

CODE = "ISSUE_TOUCH_REFRESH"
TOUCH_REASON = "issue touch refreshed"
TOUCH_EVENT_TYPE = "issue_revision_touch_refreshed"
TOUCH_CHANGE = "TOUCH_REFRESHED"


def _blocked(suffix: str, detail: str) -> IssueAmendBlocked:
    return IssueAmendBlocked(f"{CODE}_{suffix}", detail)


def resolve_touch_target(
    session: Session, project: str, repository: str, issue_number: int, *, lock: bool = False
) -> tuple[BacklogIssueRecord, GitHubBacklogCampaign, Finding, ChangeSet | None]:
    """The approved backlog delivery of one open issue; an active delivery is allowed."""
    issue, campaign, finding, change_set = locate_issue_target(
        session, project, repository, issue_number, lock=lock, code=CODE
    )
    if (
        issue.state != "OPEN"
        or issue.delivery_status == "COMPLETE"
        or issue.final_closure_status == "FIXED_CLOSURE_COMPLETE"
        or (change_set is not None and change_set.state == ChangeSetState.ISSUE_CLOSED.value)
    ):
        raise _blocked(
            "ISSUE_CLOSED", f"{issue.repository}#{issue_number} is closed or already delivered"
        )
    return issue, campaign, finding, change_set


def _observed(observation: dict[str, Any], target: str) -> dict[str, Any]:
    current = observation.get("source_body_sha256")
    updated_at = observation.get("updated_at")
    if (
        observation.get("status") != "ISSUE_BODY_REFRESHED"
        or not isinstance(current, str)
        or not _SHA256.fullmatch(current)
        or not isinstance(updated_at, str)
    ):
        raise _blocked(
            "BODY_UNAVAILABLE",
            f"the GitHub issue {target} could not be read "
            f"({str(observation.get('reason') or observation.get('status'))[:80]}); try again",
        )
    title_sha256 = observation.get("title_sha256")
    if not isinstance(title_sha256, str) or not _SHA256.fullmatch(title_sha256):
        raise _blocked("TITLE_UNAVAILABLE", f"the GitHub title of {target} could not be read")
    try:
        parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise _blocked(
            "BODY_UNAVAILABLE", f"the GitHub revision of {target} is malformed"
        ) from error
    if observation.get("issue_state") != "OPEN":
        raise _blocked("ISSUE_CLOSED", f"{target} is not open on GitHub")
    labels = observation.get("labels")
    if not isinstance(labels, list) or not all(isinstance(item, str) for item in labels):
        raise _blocked(
            "LABELS_UNAVAILABLE",
            f"the GitHub labels of {target} could not be read (the GitHub worker must run "
            f"NEW-150); try again",
        )
    return {
        "body_sha256": current,
        "title_sha256": title_sha256,
        "labels": normalized_labels(labels),
        "updated_at": _utc(parsed),
        "body_chars": observation.get("body_chars"),
        "body_excerpt": observation.get("body_excerpt"),
    }


def normalized_labels(values: list[str]) -> list[str]:
    """Labels as the backlog import classifies and records them (labels_json)."""
    return sorted({value[:120].strip().lower() for value in values if value[:120].strip()})


def labels_sha256(labels: list[str]) -> str:
    return canonical_sha256(normalized_labels(labels))


def revision_token(
    target: str, body_sha256: str, title_sha256: str, labels: list[str], updated_at: datetime
) -> str:
    """The confirmation of one observed touch: bound to the exact GitHub revision."""
    return canonical_sha256(
        {
            "action": "issue_touch_refresh",
            "target": target,
            "body_sha256": body_sha256,
            "title_sha256": title_sha256,
            "labels_sha256": labels_sha256(labels),
            "state": "OPEN",
            "updated_at": _utc(updated_at).isoformat(),
        }
    )


_STATUS_ORDER = ("STALE", "BODY_DIFFERS", "NEWER", "CURRENT")


def _consumers(
    session: Session,
    change_set: ChangeSet,
    issue: BacklogIssueRecord,
    current: tuple[str, datetime],
) -> list[dict[str, Any]]:
    """Each delivery run bound to a consumed revision, with touch refreshes applied.

    The run recorded by the delivery load (NEW-115); for a ChangeSet loaded before that
    record existed, its RUNNING runs, which publish against the import."""
    recorded = (change_set.recovery_json or {}).get(ISSUE_BODY_REVISION_KEY)
    if recorded is None:
        runs = session.scalars(
            select(Run)
            .where(Run.change_set_id == change_set.id, Run.status == "RUNNING")
            .order_by(Run.created_at, Run.id)
        ).all()
        loaded = [(run, issue.body_sha256, issue.github_updated_at) for run in runs]
    else:
        run = None
        if isinstance(recorded, dict):
            try:
                run = session.get(Run, UUID(str(recorded.get("run_id"))))
            except ValueError:
                run = None
        if (
            run is None
            or run.change_set_id != change_set.id
            or not isinstance(recorded.get("body_sha256"), str)
            or not isinstance(recorded.get("updated_at"), str)
        ):
            raise _blocked(
                "CONSUMED_REVISION_INVALID",
                "the delivery's consumed issue revision record is invalid",
            )
        loaded = [(run, recorded["body_sha256"], datetime.fromisoformat(recorded["updated_at"]))]
    consumers = []
    for run, body, at in loaded:
        sha, consumed_at = touch_refreshed_revision(session, change_set, run, body, at)
        consumed_at = _utc(consumed_at)
        if sha != current[0]:
            # The run loaded another text (before an amendment); its publication is
            # refused on the body anyway and a refresh never re-binds it.
            status = "BODY_DIFFERS"
        elif consumed_at == current[1]:
            status = "CURRENT"
        elif consumed_at > current[1]:
            status = "NEWER"
        else:
            status = "STALE"
        consumers.append({"run": run, "updated_at": consumed_at, "status": status})
    return consumers


def touch_refresh_issue(
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
    """Preview (no confirmation) or apply (confirmation) one touch refresh.

    Apply runs in the caller's transaction under the issue-amend lock order (Finding,
    issue record, ChangeSet) and requires the confirmation to be a prefix of the token of
    the revision read now; a rerun once applied reports NO_CHANGE."""
    current_time = now or datetime.now(UTC)
    issue, campaign, finding, change_set = resolve_touch_target(
        session, project, repository, issue_number, lock=confirmation is not None
    )
    target = f"{issue.repository}#{issue.issue_number}"
    observed = _observed(observation, target)
    if observed["title_sha256"] != hashlib.sha256(issue.title.encode()).hexdigest():
        raise _blocked(
            "TITLE_CHANGED",
            f"the title of {target} differs from the approved one; restore it, or re-import "
            f"the backlog and start the issue again",
        )
    approved_labels = normalized_labels([str(item) for item in issue.labels_json or []])
    if observed["labels"] != approved_labels:
        added = sorted(set(observed["labels"]) - set(approved_labels))
        removed = sorted(set(approved_labels) - set(observed["labels"]))
        raise _blocked(
            "LABELS_CHANGED",
            f"the labels of {target} differ from the ones its risk class was approved with "
            f"(added {added[:10]}, removed {removed[:10]}); restore them, or re-import the "
            f"backlog and approve the issue again",
        )
    approved_sha256, approved_at = effective_issue_revision(issue)
    current_sha256: str = observed["body_sha256"]
    current_at: datetime = observed["updated_at"]
    if current_sha256 != approved_sha256:
        raise _blocked(
            "BODY_CHANGED",
            f"the GitHub body of {target} differs from the approved revision (approved "
            f"{approved_sha256[:12]}, now {current_sha256[:12]}); a content change needs "
            f"issue-amend, or restore the approved text",
        )
    if current_at < _utc(approved_at):
        raise _blocked(
            "REVISION_STALE",
            f"GitHub reports {target} at {current_at.isoformat()}, before the approved "
            f"revision {_utc(approved_at).isoformat()}; try again",
        )
    consumers = (
        _consumers(session, change_set, issue, (current_sha256, current_at))
        if change_set is not None
        else []
    )
    consumers.sort(key=lambda item: _STATUS_ORDER.index(item["status"]))
    if consumers:
        consumed_status = consumers[0]["status"]
        consumed_at: datetime | None = consumers[0]["updated_at"]
    else:
        consumed_status = "NO_CHANGE_SET" if change_set is None else "NO_CONSUMING_RUN"
        consumed_at = None
    stale_runs = [item for item in consumers if item["status"] == "STALE"]
    issue_stale = _utc(approved_at) != current_at
    change_set_stale = bool(stale_runs)
    token = revision_token(
        target, current_sha256, observed["title_sha256"], observed["labels"], current_at
    )
    short = issue.repository.removeprefix("gntik-ai/")
    base: dict[str, Any] = {
        "target": target,
        "project": project,
        "backlog_campaign_id": str(campaign.id),
        "finding_id": str(finding.id),
        "change_set_id": str(change_set.id) if change_set is not None else None,
        "change_set_state": change_set.state if change_set is not None else None,
        "body_sha256": current_sha256,
        "title_sha256": observed["title_sha256"],
        "labels": observed["labels"],
        "approved_updated_at": _utc(approved_at).isoformat(),
        "consumed_updated_at": consumed_at.isoformat() if consumed_at is not None else None,
        "consumed_revision": consumed_status,
        "consumed_runs": [
            {
                "run_id": str(item["run"].id),
                "updated_at": item["updated_at"].isoformat(),
                "status": item["status"],
            }
            for item in consumers
        ],
        "current_updated_at": current_at.isoformat(),
        "production": False,
    }
    if not issue_stale and not change_set_stale:
        return {
            **base,
            "status": "NO_CHANGE",
            "detail": "the approved and consumed revisions already match GitHub",
        }
    if confirmation is None:
        return {
            **base,
            "status": "PREVIEW",
            "change": TOUCH_CHANGE,
            "confirm_with": token[:12],
            "cli_command": (
                f"engineering-ctl issue-touch-refresh {project} {short} {issue_number} "
                f"--confirm {token[:12]}"
            ),
        }
    if not CONFIRMATION_PATTERN.fullmatch(confirmation):
        raise _blocked(
            "CONFIRMATION_INVALID",
            "confirm with at least 12 lowercase hex characters of the preview's token",
        )
    if not token.startswith(confirmation):
        raise _blocked(
            "CONFIRMATION_MISMATCH",
            f"the GitHub revision of {target} changed since the preview (now "
            f"{current_at.isoformat()}, token {token[:12]}); preview it again",
        )
    if actor is None:
        raise _blocked("OPERATOR_REQUIRED", "an operator identity is required")
    snapshot = dict(issue.github_snapshot_json or {})
    previous = snapshot.get(AMENDMENT_KEY)
    if issue_stale:
        excerpt = observed["body_excerpt"]
        value: dict[str, Any] = {
            "version": 1,
            "base_body_sha256": issue.body_sha256,
            "previous_body_sha256": approved_sha256,
            "previous_updated_at": _utc(approved_at).isoformat(),
            "body_sha256": current_sha256,
            "github_updated_at": current_at.isoformat(),
            "change": TOUCH_CHANGE,
            "amended_by": actor,
            "amended_at": current_time.isoformat(),
            "reason": TOUCH_REASON,
            "amendment_count": (
                int(previous.get("amendment_count", 1)) if isinstance(previous, dict) else 0
            ),
            "labels_sha256": labels_sha256(observed["labels"]),
            "touch_refresh_count": (
                int(previous.get("touch_refresh_count", 0)) + 1 if isinstance(previous, dict) else 1
            ),
        }
        if isinstance(previous, dict) and isinstance(previous.get("body_chars"), int):
            # The same text: keep the amendment's own measurements.
            value["body_chars"] = previous["body_chars"]
            if isinstance(previous.get("body_excerpt_sha256"), str):
                value["body_excerpt_sha256"] = previous["body_excerpt_sha256"]
        elif isinstance(observed["body_chars"], int) and isinstance(excerpt, str):
            value["body_chars"] = observed["body_chars"]
            value["body_excerpt_sha256"] = hashlib.sha256(excerpt.encode()).hexdigest()
        value["binding_sha256"] = _amendment_binding(issue, value)
        snapshot[AMENDMENT_KEY] = value
        issue.github_snapshot_json = snapshot
    refreshes: list[dict[str, Any]] = []
    for item in stale_runs:
        if change_set is None:  # stale runs exist only for a ChangeSet
            break
        run = item["run"]
        step_id = touch_refresh_step_id(change_set.id, run.id, current_at)
        if session.get(Step, step_id) is not None:
            raise _blocked("CONSUMED_REVISION_INVALID", "a refresh of this revision exists")
        refresh: dict[str, Any] = {
            "version": 1,
            "change_set_id": str(change_set.id),
            "run_id": str(run.id),
            "step_id": str(step_id),
            "backlog_issue_record_id": str(issue.id),
            "target": target,
            "body_sha256": current_sha256,
            "title_sha256": observed["title_sha256"],
            "labels_sha256": labels_sha256(observed["labels"]),
            "previous_updated_at": item["updated_at"].isoformat(),
            "updated_at": current_at.isoformat(),
            "refreshed_by": actor,
            "refreshed_at": current_time.isoformat(),
            "reason": TOUCH_REASON,
        }
        refresh["binding_sha256"] = touch_refresh_binding(change_set.id, run.id, step_id, refresh)
        session.add(
            Step(
                id=step_id,
                run_id=run.id,
                name=TOUCH_REFRESH_STEP,
                ordinal=0,
                status="COMPLETE",
                idempotency_key=f"issue-touch-refresh:{step_id}",
                attempt=1,
                result_json=refresh,
            )
        )
        refreshes.append(refresh)
    event = DashboardEvent(
        occurred_at=current_time,
        level="WARNING",
        event_type=TOUCH_EVENT_TYPE,
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
        current_phase="ISSUE_TOUCH_REFRESHED",
        message=f"Issue revision time re-observed by operator: {TOUCH_REASON}",
        metadata_json={
            "refreshed_by": actor,
            "reason": TOUCH_REASON,
            "repository": issue.repository,
            "finding_id": str(finding.id),
            "change_set_id": str(change_set.id) if change_set is not None else None,
            "body_sha256": current_sha256,
            "title_sha256": observed["title_sha256"],
            "previous_approved_updated_at": _utc(approved_at).isoformat(),
            "previous_consumed_updated_at": (
                consumed_at.isoformat() if consumed_at is not None else None
            ),
            "github_updated_at": current_at.isoformat(),
            "approved_revision_refreshed": issue_stale,
            "labels_sha256": labels_sha256(observed["labels"]),
            "consumed_revision_refreshed": bool(refreshes),
            "consumed_revision": consumed_status,
            "refreshed_runs": [
                {"run_id": item["run_id"], "step_id": item["step_id"]} for item in refreshes
            ],
        },
    )
    session.add(event)
    session.flush()
    return {
        **base,
        "status": "REFRESHED",
        "change": TOUCH_CHANGE,
        "refreshed_by": actor,
        "reason": TOUCH_REASON,
        "approved_revision_refreshed": issue_stale,
        "consumed_revision_refreshed": bool(refreshes),
        "refreshed_runs": [item["run_id"] for item in refreshes],
        "audit_event_id": event.id,
    }
