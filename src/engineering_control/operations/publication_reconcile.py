"""NEW-145: bounded, operator-confirmed reconciliation of an uncertain publication effect.

A ``delivery.publication_effect`` Step left UNKNOWN blocks every implementation
lease of its project (leases/component.py) and keeps its run's leases (metadata release):
the controller cannot know whether the push or PR change happened. This module resolves
one such effect only by authority: the live heads of the ChangeSet's bound pull requests,
read by the GitHub worker, must be exactly the heads recorded at the ChangeSet's last
confirmed publication (``recovery_json.change_evidence``), so the uncertain effect left no
unrecorded change. The effect then moves to the terminal status RECONCILED with a
``reconciliation`` record; its immutable receipt is kept. Nothing here talks to GitHub.

Only the provably safe case is covered (NEW-145 review): an effect whose publisher
returned a committed UNKNOWN receipt for its current claim (nothing still runs under that
claim) and whose action is a ``git_push`` of the recorded branch of an already-bound,
pre-existing PR. A STARTED effect (a publisher may still be running) and any PR creation
(a replacement PR the bound URLs would never show) need another, fenced resolution.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.db.models import ChangeSet, ComponentLease, DashboardEvent, Run, Step
from engineering_control.operations.publication_authority import (
    EFFECT_STEP,
    INTENT_STEP,
    RECONCILED_EFFECT_STATE,
    _lock_changeset,
)

RECONCILED = RECONCILED_EFFECT_STATE
# NEW-145 review 3: a push already received by GitHub can still update the ref after the
# local client is killed. No remote completion evidence exists, so the PR heads may only be
# read once this long has passed since the publisher's restart (the latest the client can
# have died); it bounds GitHub's processing of a received push by a wide margin.
REMOTE_SETTLE = timedelta(minutes=30)
RECONCILE_EVENT_TYPE = "publication_effect_reconciled"
RECONCILE_METHOD = "LIVE_PR_HEADS_EQUAL_RECORDED_PUBLICATION"
# The only action whose effect the bound PRs' live heads and branches fully show.
RECONCILABLE_ACTION = "git_push"
_ACTIVE_RUN_STATES = frozenset({"QUEUED", "RUNNING", "RATE_LIMITED", "AWAITING_APPROVAL"})
_SHA = re.compile(r"[0-9a-f]{40}")
_PR_URL = re.compile(
    r"https://github\.com/(gntik-ai/(?:falcone|falcone-charts|musematic|musematic-deploy|llmwiki))"
    r"/pull/[1-9][0-9]{0,9}"
)


class PublicationReconcileBlocked(ValueError):
    """A refused reconciliation: a stable blocker code and operator guidance."""

    def __init__(self, blocker: str, detail: str) -> None:
        super().__init__(f"{blocker}: {detail}")
        self.blocker = blocker
        self.detail = detail


def reconcile_phrase(effect_id: UUID) -> str:
    return f"RECONCILE_PUBLICATION_{effect_id.hex[:8].upper()}"


def _refuse(code: str, detail: str) -> PublicationReconcileBlocked:
    return PublicationReconcileBlocked(f"PUBLICATION_RECONCILE_{code}", detail)


def _run_terminal(run: Run | None) -> bool:
    return run is not None and run.status not in _ACTIVE_RUN_STATES and run.finished_at is not None


def _recorded_pull_requests(change_set: ChangeSet) -> dict[str, dict[str, str]]:
    """The ChangeSet's bound PRs with the head and branch recorded at its last confirmed
    publication."""
    recovery = change_set.recovery_json or {}
    evidence = recovery.get("change_evidence")
    recorded: dict[str, dict[str, str]] = {}
    for url, side, branch in (
        (change_set.source_pr_url, "source", change_set.source_branch),
        (change_set.deployment_pr_url, "deployment", change_set.deployment_branch),
    ):
        if not url:
            continue
        entry = evidence.get(side) if isinstance(evidence, dict) else None
        head = entry.get("head_sha") if isinstance(entry, dict) else None
        if (
            not isinstance(head, str)
            or not _SHA.fullmatch(head)
            or not _PR_URL.fullmatch(url)
            or not branch
        ):
            raise _refuse(
                "PUBLICATION_BINDING_MISSING",
                f"the {side} PR has no recorded published head and branch to compare with",
            )
        recorded[str(url)] = {"head_sha": head, "branch": str(branch)}
    if not change_set.source_pr_url:
        raise _refuse("PUBLICATION_BINDING_MISSING", "the ChangeSet has no published source PR")
    return recorded


def plan_publication_reconcile(
    session: Session, effect_id: UUID, confirmation: str
) -> dict[str, Any]:
    """Every precondition except the live GitHub read, from durable records.

    ``status`` is RECONCILE (read these PRs, then call reconcile_publication_effect) or
    ALREADY_RECONCILED (an idempotent rerun: nothing to do)."""
    if confirmation != reconcile_phrase(effect_id):
        raise _refuse("CONFIRMATION_MISMATCH", f"confirm with {reconcile_phrase(effect_id)}")
    effect = session.get(Step, effect_id)
    if effect is None or effect.name != EFFECT_STEP or not isinstance(effect.result_json, dict):
        raise _refuse("EFFECT_NOT_FOUND", f"no publication effect {effect_id}")
    if effect.status == RECONCILED:
        return {
            "status": "ALREADY_RECONCILED",
            "effect_id": str(effect.id),
            "reconciliation": effect.result_json.get("reconciliation"),
        }
    run = session.get(Run, effect.run_id)
    if not _run_terminal(run):
        raise _refuse(
            "RUN_ACTIVE", "the effect's run is still active; let it stop (or cancel it) first"
        )
    if effect.status == "STARTED":
        raise _refuse(
            "EFFECT_STARTED",
            "a STARTED effect's publisher may still be running; it needs a fenced resolution, "
            "not a PR-head reconciliation",
        )
    if effect.status != "UNKNOWN":
        raise _refuse("EFFECT_NOT_UNRESOLVED", f"the effect is {effect.status}, not unresolved")
    receipt = effect.result_json.get("receipt")
    if (
        not isinstance(receipt, dict)
        or receipt.get("outcome") != "UNKNOWN"
        or receipt.get("effect_id") != str(effect.id)
        or not effect.result_json.get("claim_id")
        or receipt.get("claim_id") != effect.result_json.get("claim_id")
    ):
        raise _refuse(
            "EFFECT_RECEIPT_INVALID",
            "the effect has no committed UNKNOWN receipt for its current claim",
        )
    intent = session.get(Step, UUID(str(effect.result_json.get("intent_id"))))
    if (
        intent is None
        or intent.name != INTENT_STEP
        or intent.run_id != effect.run_id
        or not isinstance(intent.result_json, dict)
    ):
        raise _refuse("EFFECT_CUSTODY_INVALID", "the effect's publication intent is unavailable")
    change_set = session.get(ChangeSet, UUID(str(intent.result_json["change_set_id"])))
    if change_set is None or run is None or run.change_set_id != change_set.id:
        raise _refuse("EFFECT_CUSTODY_INVALID", "the effect's ChangeSet binding changed")
    recorded = _recorded_pull_requests(change_set)
    request = effect.result_json.get("request")
    action = request.get("action") if isinstance(request, dict) else None
    target = request.get("repository") if isinstance(request, dict) else None
    plan = intent.result_json.get("plan")
    publications = plan.get("publications") if isinstance(plan, dict) else None
    if not isinstance(publications, list) or not all(
        isinstance(item, dict) for item in publications
    ):
        raise _refuse("EFFECT_CUSTODY_INVALID", "the effect's publication plan is unavailable")
    # Any PR creation under this intent (this effect's or another's) may have left a
    # replacement PR that the recorded PR URLs never show.
    created = [
        item["repository"]
        for item in publications
        if session.get(
            Step,
            uuid5(
                NAMESPACE_URL,
                f"publication-effect:{intent.id}:{item.get('repository')}:pr_create",
            ),
        )
        is not None
    ]
    if action != RECONCILABLE_ACTION or created:
        raise _refuse(
            "EFFECT_ACTION_UNSUPPORTED",
            "only a git push to an already-bound PR is reconcilable; this publication "
            f"{'created a PR' if created else f'is a {action}'}",
        )
    by_repository = {
        match.group(1): (url, value)
        for url, value in recorded.items()
        if (match := _PR_URL.fullmatch(url)) is not None
    }
    publication = next((item for item in publications if item.get("repository") == target), None)
    bound = by_repository.get(str(target))
    if (
        bound is None
        or publication is None
        or publication.get("branch") != bound[1]["branch"]
        or set(by_repository) != {item.get("repository") for item in publications}
    ):
        raise _refuse(
            "EFFECT_TARGET_UNBOUND",
            f"the effect's push target ({target}) is not exactly the ChangeSet's recorded PRs",
        )
    return {
        "status": "RECONCILE",
        "effect_id": str(effect.id),
        "effect_status": effect.status,
        "change_set_id": str(change_set.id),
        "change_set_version": change_set.version,
        "recorded": recorded,
        "base_branch": {
            by_repository[str(item.get("repository"))][0]: str(item.get("base_branch") or "")
            for item in publications
        },
        "pull_request_urls": list(recorded),
        # NEW-145 review 2: who published, and when its UNKNOWN receipt committed; the
        # publisher must have restarted (its cgroup killed) after that before reconciling.
        "subject": str(effect.result_json.get("subject") or ""),
        "receipt_received_at": str(effect.result_json.get("received_at") or ""),
    }


def _require_publisher_settled(plan: dict[str, Any], settlement: dict[str, Any] | None) -> None:
    """NEW-145 review 2: a committed UNKNOWN receipt does not prove the push is over (a
    timed-out command kills only its parent; a transport child may survive). Settlement is
    proven only by the publishing service having restarted after the receipt under systemd
    KillMode=control-group, which kills every process of the old service cgroup.

    Review 3: the restart must also precede the PR-head read by REMOTE_SETTLE, so no local
    process survived into the observation and GitHub has finished any push it received.
    ``heads_read_from`` is stamped by the API, before it asks the worker for the heads."""
    if not isinstance(settlement, dict):
        raise _refuse(
            "PUBLISHER_SETTLEMENT_UNVERIFIABLE",
            f"the {plan['subject'] or 'unknown'} publisher's restart cannot be verified here",
        )
    if settlement.get("kill_mode") != "control-group":
        raise _refuse(
            "PUBLISHER_SETTLEMENT_UNVERIFIABLE",
            "the publisher's service does not kill its whole cgroup on restart",
        )
    try:
        started = datetime.fromisoformat(str(settlement.get("started_at")))
        received = datetime.fromisoformat(plan["receipt_received_at"])
        heads_read_from = datetime.fromisoformat(str(settlement.get("heads_read_from")))
    except ValueError as error:
        raise _refuse(
            "PUBLISHER_SETTLEMENT_UNVERIFIABLE", "the restart or receipt time is unreadable"
        ) from error
    if started <= received:
        raise _refuse(
            "PUBLISHER_NOT_SETTLED",
            "the publisher has not restarted since the UNKNOWN receipt; restart its service "
            "(or wait for the next deploy) and run again",
        )
    if heads_read_from < started + REMOTE_SETTLE:
        raise _refuse(
            "PUBLISHER_NOT_SETTLED",
            f"the PR heads were read less than {REMOTE_SETTLE} after the publisher's restart "
            f"({started.isoformat()}); run again after {(started + REMOTE_SETTLE).isoformat()}",
        )


def reconcile_publication_effect(
    session: Session,
    effect_id: UUID,
    confirmation: str,
    actor: str,
    planned: dict[str, Any],
    observed: list[dict[str, Any]],
    now: datetime | None = None,
    settlement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-check under the publication locks, compare the live heads, and resolve."""
    current = now or datetime.now(UTC)
    _lock_changeset(session, UUID(str(planned["change_set_id"])))
    session.scalar(select(Step).where(Step.id == effect_id).with_for_update())
    plan = plan_publication_reconcile(session, effect_id, confirmation)
    if plan["status"] == "ALREADY_RECONCILED":
        return plan
    if any(plan[key] != planned[key] for key in plan):
        raise _refuse(
            "CHANGED", "the effect or its ChangeSet changed since the PR heads were read; run again"
        )
    _require_publisher_settled(plan, settlement)
    by_url = {str(item.get("url")): item for item in observed if isinstance(item, dict)}
    checked: list[dict[str, Any]] = []
    for url, recorded in plan["recorded"].items():
        head = recorded["head_sha"]
        item = by_url.get(url)
        if item is None:
            raise _refuse("PR_MISSING", f"{url} was not observed")
        state = str(item.get("state") or "")
        live = str(item.get("head_sha") or "")
        if state not in {"OPEN", "MERGED"}:
            raise _refuse("PR_NOT_OPEN", f"{url} is {state or 'unknown'}")
        if (
            item.get("head_branch") != recorded["branch"]
            or item.get("base_branch") != plan["base_branch"][url]
        ):
            # A retargeted or re-headed PR is no longer the target the push was bound to.
            raise _refuse(
                "PR_RETARGETED",
                f"{url} is {item.get('head_branch')} -> {item.get('base_branch')}, not the "
                f"recorded {recorded['branch']} -> {plan['base_branch'][url]}",
            )
        if live != head:
            raise _refuse(
                "HEAD_MISMATCH",
                f"{url} is at {live[:12] or 'none'}, not the recorded published {head[:12]}; "
                "reset the PR branch to the recorded head (or let a new delivery publish)",
            )
        checked.append(
            {
                "url": url,
                "branch": recorded["branch"],
                "recorded_head_sha": head,
                "observed_head_sha": live,
                "state": state,
            }
        )
    effect = session.get(Step, effect_id)
    run = session.get(Run, effect.run_id) if effect is not None else None
    if effect is None or run is None:
        raise _refuse("EFFECT_NOT_FOUND", f"no publication effect {effect_id}")
    record: dict[str, Any] = {
        "version": 1,
        "reconciled_by": actor,
        "reconciled_at": current.isoformat(),
        "method": RECONCILE_METHOD,
        "previous_status": effect.status,
        "change_set_id": plan["change_set_id"],
        "change_set_version": plan["change_set_version"],
        "pull_requests": checked,
    }
    event = DashboardEvent(
        occurred_at=current,
        level="WARNING",
        event_type=RECONCILE_EVENT_TYPE,
        source="controller",
        host=None,
        worker_kind="controller",
        project=run.project,
        workflow_id=run.workflow_id,
        temporal_run_id=None,
        issue_number=None,
        campaign_id=None,
        entity_type="publication_effect",
        entity_id=str(effect.id),
        current_phase=RECONCILED,
        message="Uncertain publication effect reconciled against live PR heads by operator",
        metadata_json={
            "reconciled_by": actor,
            "previous_status": effect.status,
            "method": RECONCILE_METHOD,
            "pull_requests": checked,
        },
    )
    session.add(event)
    session.flush()
    record["audit_event_id"] = int(event.id)
    # The immutable receipt (if any) stays; only the status and this record are added.
    effect.result_json = {**effect.result_json, "reconciliation": record}
    effect.status = RECONCILED
    released = 0
    if _run_terminal(run):
        # The run kept its leases only because its effect was unresolved.
        for lease in session.scalars(
            select(ComponentLease).where(
                ComponentLease.owner_run_id == run.id, ComponentLease.released_at.is_(None)
            )
        ):
            lease.released_at = current
            released += 1
    session.flush()
    return {
        "status": RECONCILED,
        "effect_id": str(effect.id),
        "previous_status": record["previous_status"],
        "pull_requests": checked,
        "released_leases": released,
        "audit_event_id": int(event.id),
        "production": False,
    }
