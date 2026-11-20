"""Rules shared by the Epic plan editor and every path that starts an Issue run."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from heapq import heapify, heappop, heappush
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.dashboard.project_records import ProjectConflict, ProjectInvalid
from engineering_control.db.models import (
    BacklogItem,
    EpicExecutionPlan,
    ProjectSection,
    SandboxRun,
    WorkQueue,
    WorkQueueEntry,
)
from engineering_control.engine.library import plan_for_item


class QueueChanged(ProjectConflict):
    """A concurrent pause, move or dispatch won the queue lock."""


def ordered(preferred: Sequence[str], requires: Mapping[str, list[str]]) -> list[str]:
    """Stable topological order. External prerequisites are checked at dispatch."""
    rank = {key: index for index, key in enumerate(preferred)}
    children: dict[str, list[str]] = {key: [] for key in rank}
    remaining = dict.fromkeys(rank, 0)
    for child in rank:
        for parent in set(requires.get(child, [])) & rank.keys():
            children[parent].append(child)
            remaining[child] += 1
    ready = [(rank[key], key) for key in rank if not remaining[key]]
    heapify(ready)
    result = []
    while ready:
        _, key = heappop(ready)
        result.append(key)
        for child in children[key]:
            remaining[child] -= 1
            if not remaining[child]:
                heappush(ready, (rank[child], child))
    if len(result) != len(rank):
        raise ProjectInvalid("The prerequisites form a cycle. Remove a dependency before starting.")
    return result


def validate_graph(db: Session, plan: EpicExecutionPlan, proposed: dict[str, list[str]]) -> None:
    edges: dict[str, list[str]] = {}
    for other in db.scalars(
        select(EpicExecutionPlan)
        .join(WorkQueue)
        .where(WorkQueue.state != "finished", EpicExecutionPlan.queue_id != plan.queue_id)
    ):
        for item, deps in other.data.get("requires", {}).items():
            edges.setdefault(item, []).extend(deps)
    for item, deps in proposed.items():
        edges.setdefault(item, []).extend(deps)
    ordered(list(edges), edges)


def eligible(
    db: Session,
    item: BacklogItem,
    section: ProjectSection | None,
    flows: dict[tuple[str | None, int | None, bool], bool] | None = None,
) -> str | None:
    if item.issue_state == "closed" or item.state == "done":
        return "The Issue has been closed."
    if item.workspace_review not in (None, "approved") or not item.issue_number:
        return "Approve and publish the Issue in the Issues tab."
    if item.state not in ("proposed", "accepted"):
        return "The Issue is no longer open for work."
    if not item.type_confirmed:
        return "Confirm the Issue classification."
    key = section.flow_key if section else None
    produced = item.source_run_id is not None or bool((item.facts or {}).get("kind"))
    cache = flows if flows is not None else {}
    version = section.flow_version if section else None
    cache_key = (key, version, produced)
    if cache_key not in cache:
        cache[cache_key] = bool(plan_for_item(db, key, produced=produced, version=version))
    if not cache[cache_key]:
        return "Assign an administrator-defined workflow to the project section."
    return None


def dispatch_blocker(db: Session, plan: EpicExecutionPlan, item: BacklogItem) -> str | None:
    key = str(item.id)
    for ref in plan.data.get("requires", {}).get(key, []):
        parent = db.get(BacklogItem, UUID(ref))
        if parent is None:
            return "A prerequisite was removed. Review the execution plan."
        entry = db.scalar(
            select(WorkQueueEntry).where(
                WorkQueueEntry.queue_id == plan.queue_id, WorkQueueEntry.item_id == parent.id
            )
        )
        run = db.get(SandboxRun, entry.run_id) if entry and entry.run_id else None
        if not entry:
            run = db.scalars(
                select(SandboxRun)
                .where(SandboxRun.item_id == parent.id)
                .order_by(SandboxRun.created_at.desc())
                .limit(1)
            ).first()
        if run and run.state == "succeeded":
            continue
        if (
            (not run or run.state not in ("queued", "running", "waiting"))
            and (parent.state == "done" or parent.issue_state == "closed")
            and (parent.facts or {}).get("github_state_reason") != "not_planned"
        ):
            continue
        return f"Waiting for prerequisite {parent.title[:160]}."
    return None


def reserve_dispatch(
    db: Session, item: BacklogItem, entry_id: UUID | None
) -> WorkQueueEntry | None:
    """Serialize dispatch with queue edits and bind the run to its entry in one transaction."""
    if entry_id is None:
        reserved = db.scalar(
            select(WorkQueueEntry.id)
            .join(WorkQueue)
            .join(EpicExecutionPlan)
            .where(
                WorkQueueEntry.item_id == item.id,
                WorkQueue.state != "finished",
                WorkQueueEntry.state.in_(("waiting", "running")),
            )
            .limit(1)
        )
        if reserved:
            raise ProjectConflict("This Issue is reserved by the project execution plan.")
        return None
    row = db.execute(
        select(WorkQueueEntry, WorkQueue)
        .join(WorkQueue)
        .where(WorkQueueEntry.id == entry_id, WorkQueueEntry.item_id == item.id)
        .with_for_update(of=WorkQueue)
    ).first()
    if not row:
        raise QueueChanged("The queue entry no longer exists.")
    entry, queue = row._tuple()
    if queue.state != "running" or entry.state != "waiting" or entry.run_id:
        raise QueueChanged("The queue entry is no longer ready.")
    slots = list(
        db.scalars(
            select(WorkQueueEntry)
            .where(WorkQueueEntry.queue_id == queue.id)
            .order_by(WorkQueueEntry.position)
        )
    )
    room = queue.at_once - sum(e.state == "running" for e in slots)
    if entry.id not in [e.id for e in slots if e.state == "waiting"][: max(room, 0)]:
        raise QueueChanged("Waiting for an earlier queue entry.")
    plan = db.get(EpicExecutionPlan, queue.id)
    if plan:
        reason = dispatch_blocker(db, plan, item)
        if reason:
            raise ProjectConflict(reason)
    return entry
