"""Describe saved project inputs and model comparisons without contacting a provider."""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.dashboard.connection_records import digest
from engineering_control.dashboard.issue_coverage import INSTRUCTION, MATCHES, Inventory, inventory
from engineering_control.dashboard.workspace_issues import planning_kind
from engineering_control.db.models import (
    BacklogItem,
    ProjectEpic,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
    WorkspaceCandidate,
    WorkspaceImport,
    WorkspaceSource,
)

KINDS = ("epics", "stories", "issues")
Snapshot = dict[str, dict[str, str]]


def record_version(title: str, description: str | None, closed: bool, parents: list[str]) -> str:
    return digest([title, description, closed, sorted(parents)])


def proposal_version(c: WorkspaceCandidate) -> str:
    return digest([c.title, c.description, sorted(c.parents), c.number, c.decision])


def remember_proposal(
    row: WorkspaceImport, candidate: WorkspaceCandidate, *, prior: str | None = None
) -> None:
    """Fold only this run's own writes into its original, immutable input inventory."""
    snapshot = row.progress.get("coverage_inputs")
    if not snapshot:
        return
    kind = candidate.kind
    key = "proposal:" + str(candidate.id)
    if snapshot[kind].get(key) != prior:
        return
    records = {**snapshot[kind], key: proposal_version(candidate)}
    if candidate.decision == "approved" and kind in ("epics", "stories") and candidate.local_id:
        # These records were just materialized from the approved proposal.
        records[str(candidate.local_id)] = record_version(
            candidate.title, candidate.description, False, candidate.parents
        )
    row.progress = {**row.progress, "coverage_inputs": {**snapshot, kind: records}}


def snapshot(
    db: Session, project: ProjectRecord, row: WorkspaceImport, provider: str, model: str
) -> tuple[Snapshot, Inventory]:
    scope: Snapshot = {k: {} for k in KINDS}
    scope["context"] = {
        "repository": digest(
            [
                1,
                INSTRUCTION,
                MATCHES,
                project.source_repository,
                row.branch,
                row.commit_sha,
                project.description or row.draft,
                row.progress.get("evidence_notes", []),
                provider,
                model,
            ]
        )
    }
    pid = project.id
    parents: dict[UUID, list[str]] = defaultdict(list)
    for story, epic in db.execute(
        select(ProjectStoryEpic.story_id, ProjectStoryEpic.epic_id).where(
            ProjectStoryEpic.project_id == pid
        )
    ):
        parents[story].append(str(epic))
    for kind, table in (("epics", ProjectEpic), ("stories", ProjectUserStory)):
        for key, title, description, closed in db.execute(
            select(table.id, table.title, table.description, table.closed_at).where(
                table.project_id == pid, table.archived_at.is_(None)
            )
        ):
            scope[kind][str(key)] = record_version(
                title, description, bool(closed), parents[key] if kind == "stories" else []
            )
    data = inventory(db, project, set(scope["stories"]), provider, model)
    scope["issues"].update(data.issue_versions)
    for item in db.scalars(
        select(BacklogItem).where(
            BacklogItem.project_id == pid,
            BacklogItem.issue_number.is_(None),
            BacklogItem.workspace_review.in_(("pending", "publishing", "approved")),
        )
    ):
        scope["issues"][str(item.id)] = digest(
            [item.title, item.description, item.workspace_review]
        )
    for candidate in db.scalars(
        select(WorkspaceCandidate).where(WorkspaceCandidate.project_id == pid)
    ):
        scope[candidate.kind]["proposal:" + str(candidate.id)] = proposal_version(candidate)
    for key, evidence in db.execute(
        select(WorkspaceSource.key, WorkspaceSource.evidence).where(
            WorkspaceSource.project_id == pid, WorkspaceSource.kind == "issue"
        )
    ):
        scope[planning_kind(evidence) or "issues"][key] = digest(evidence)
    return scope, data


def changes(scope: Snapshot, previous: Snapshot | None) -> dict[str, dict[str, int]]:
    result = {}
    for kind, now in scope.items():
        old = (previous or {}).get(kind, {})
        result[kind] = {
            "added": len(now.keys() - old.keys()),
            "changed": sum(now[key] != old[key] for key in now.keys() & old.keys()),
            "removed": len(old.keys() - now.keys()),
        }
    return result


def stages(changed: dict[str, dict[str, int]], baseline: bool) -> list[str]:
    if not baseline or any(changed["context"].values()):
        return list(KINDS)
    for n, kind in enumerate(KINDS):
        if any(changed[kind].values()):
            return list(KINDS[n:])
    return []


def plan(
    db: Session, project: ProjectRecord, row: WorkspaceImport, provider: str, model: str
) -> tuple[Snapshot, dict[str, Any]]:
    current, data = snapshot(db, project, row, provider, model)
    baseline = row.progress.get("coverage_baseline")
    changed = changes(current, baseline)
    saved = data.checkpoints(db, project.id)
    reused = sum(len(data.known(s, saved.get(s))) for s in data.stories)
    pairs = len(data.issues) * len(data.stories)
    required = stages(changed, bool(baseline))
    if not required and pairs > reused:
        required = ["issues"]
    return current, {
        "baseline": bool(baseline),
        "stages": required,
        "changes": changed,
        "stories": len(data.stories),
        "issues": len(data.issues),
        "reusable": reused,
        "remaining": pairs - reused,
        "provider": provider,
        "model": model,
    }
