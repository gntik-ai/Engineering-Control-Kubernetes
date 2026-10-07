"""NEW-150: re-observe an issue whose GitHub revision time moved but whose text did not.

Publication custody (NEW-115) requires the fresh GitHub observation to equal the consumed
issue revision: the body hash *and* GitHub ``updated_at``. Any GitHub edit of an issue
under delivery moves ``updated_at``, even one that restores the text byte for byte, and
from then on every publication claim is refused. ``issue-amend`` cannot help while the
delivery is active, and amending is for content changes anyway.

``issue-touch-refresh`` reads the issue fresh through the GitHub worker and, only when
the body and title hashes equal the approved revision and the issue is open, records the
new revision time:

* on the backlog issue record, as the effective approved revision (the NEW-115 amendment
  record, with ``change: TOUCH_REFRESHED``), so later delivery loads consume it;
* on the ChangeSet, as an append-only touch refresh entry, so the runs that already
  consumed exactly that body publish against the new time (see
  :func:`~engineering_control.operations.issue_amend.touch_refreshed_revision`).

The immutable import (``body_sha256``/``github_updated_at``, bound into every approval
snapshot) and each run's loaded record are never rewritten. A different body or title is
refused: content changes go through ``issue-amend``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from engineering_control.db.models import (
    BacklogIssueRecord,
    ChangeSet,
    DashboardEvent,
    Finding,
    GitHubBacklogCampaign,
)
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.models.enums import ChangeSetState
from engineering_control.operations.issue_amend import (
    _SHA256,
    AMENDMENT_KEY,
    CONFIRMATION_PATTERN,
    ISSUE_BODY_REVISION_KEY,
    ISSUE_TOUCH_REFRESHES_KEY,
    IssueAmendBlocked,
    _amendment_binding,
    _utc,
    effective_issue_revision,
    locate_issue_target,
    touch_refresh_binding,
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
    return {
        "body_sha256": current,
        "title_sha256": title_sha256,
        "updated_at": _utc(parsed),
        "body_chars": observation.get("body_chars"),
        "body_excerpt": observation.get("body_excerpt"),
    }


def revision_token(target: str, body_sha256: str, title_sha256: str, updated_at: datetime) -> str:
    """The confirmation of one observed touch: bound to the exact GitHub revision."""
    return canonical_sha256(
        {
            "action": "issue_touch_refresh",
            "target": target,
            "body_sha256": body_sha256,
            "title_sha256": title_sha256,
            "updated_at": _utc(updated_at).isoformat(),
        }
    )


def _consumed(change_set: ChangeSet, issue: BacklogIssueRecord) -> tuple[str, datetime] | None:
    """The revision the ChangeSet's current run consumed, with touch refreshes applied."""
    recorded = (change_set.recovery_json or {}).get(ISSUE_BODY_REVISION_KEY)
    if recorded is None:
        # Loaded before NEW-115 recorded it: publication falls back to the import.
        return touch_refreshed_revision(change_set, issue.body_sha256, issue.github_updated_at)
    if (
        not isinstance(recorded, dict)
        or not isinstance(recorded.get("body_sha256"), str)
        or not isinstance(recorded.get("updated_at"), str)
    ):
        return None
    return touch_refreshed_revision(
        change_set, recorded["body_sha256"], datetime.fromisoformat(recorded["updated_at"])
    )


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
    consumed = _consumed(change_set, issue) if change_set is not None else None
    consumed_at = _utc(consumed[1]) if consumed is not None else None
    if consumed is None:
        consumed_status = "NO_CHANGE_SET" if change_set is None else "UNREADABLE"
    elif consumed[0] != current_sha256:
        # The run loaded another text (before an amendment); its publication is refused
        # on the body anyway and a refresh never re-binds it.
        consumed_status = "BODY_DIFFERS"
    elif consumed_at == current_at:
        consumed_status = "CURRENT"
    elif consumed_at is not None and consumed_at > current_at:
        consumed_status = "NEWER"
    else:
        consumed_status = "STALE"
    issue_stale = _utc(approved_at) != current_at
    change_set_stale = consumed_status == "STALE"
    token = revision_token(target, current_sha256, observed["title_sha256"], current_at)
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
        "approved_updated_at": _utc(approved_at).isoformat(),
        "consumed_updated_at": consumed_at.isoformat() if consumed_at is not None else None,
        "consumed_revision": consumed_status,
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
    refresh: dict[str, Any] | None = None
    if change_set_stale and change_set is not None and consumed_at is not None:
        recovery = dict(change_set.recovery_json or {})
        recorded = recovery.get(ISSUE_BODY_REVISION_KEY)
        refresh = {
            "version": 1,
            "run_id": recorded.get("run_id") if isinstance(recorded, dict) else None,
            "body_sha256": current_sha256,
            "title_sha256": observed["title_sha256"],
            "previous_updated_at": consumed_at.isoformat(),
            "updated_at": current_at.isoformat(),
            "refreshed_by": actor,
            "refreshed_at": current_time.isoformat(),
            "reason": TOUCH_REASON,
        }
        refresh["binding_sha256"] = touch_refresh_binding(change_set.id, refresh)
        existing = recovery.get(ISSUE_TOUCH_REFRESHES_KEY)
        recovery[ISSUE_TOUCH_REFRESHES_KEY] = [
            *(existing if isinstance(existing, list) else []),
            refresh,
        ]
        change_set.recovery_json = recovery
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
            "consumed_revision_refreshed": refresh is not None,
            "consumed_revision": consumed_status,
            "refresh_binding_sha256": refresh["binding_sha256"] if refresh else None,
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
        "consumed_revision_refreshed": refresh is not None,
        "audit_event_id": event.id,
    }
