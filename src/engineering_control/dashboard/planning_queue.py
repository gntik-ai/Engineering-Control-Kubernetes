"""Transactional entry points shared by manual planning, approvals, and GitHub inventory."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.db.models import PlanningReview, WorkspaceCandidate


def reset_review(job: PlanningReview) -> None:
    job.status, job.lease, job.attempts = "queued", None, 0
    job.progress = {}
    job.result = {"proposals": job.result["proposals"]} if job.result.get("proposals") else {}
    job.due_at = datetime.now(UTC)


def queue_review(
    db: Session, project: UUID, kind: str, key: UUID, title: str, scope: str = "full"
) -> None:
    # Callers hold the project lock. Approval of a submitted, unparented Story
    # promotes the same identity from intake to its first full coverage review.
    job = db.get(PlanningReview, (project, kind, key))
    if job is None:
        db.add(
            PlanningReview(project_id=project, kind=kind, record_id=key, title=title, scope=scope)
        )
    elif job.status == "dismissed" or (job.scope == "intake" and scope != "intake"):
        job.scope, job.title = scope, title
        reset_review(job)


def record_changed(db: Session, project: UUID, kind: str, key: UUID, title: str) -> None:
    job = db.get(PlanningReview, (project, kind, key))
    if job:
        job.title = title
        if job.status in ("needs_input", "failed", "queued", "running") or (
            job.scope == "intake" and job.status == "matched"
        ):
            reset_review(job)


def review_results(
    db: Session, project: UUID, jobs: Sequence[PlanningReview]
) -> dict[UUID, dict[str, Any]]:
    """Resolve review history against later approvals, edits and dismissals."""
    ids = {UUID(ref["id"]) for job in jobs for ref in job.result.get("proposals", [])}
    candidates = (
        {
            str(c.id): c
            for c in db.scalars(
                select(WorkspaceCandidate).where(
                    WorkspaceCandidate.project_id == project, WorkspaceCandidate.id.in_(ids)
                )
            )
        }
        if ids
        else {}
    )
    results = {}
    for job in jobs:
        links = []
        for ref in job.result.get("proposals", []):
            candidate = candidates.get(ref["id"])
            if candidate:
                links.append(
                    {
                        **ref,
                        "id": str(candidate.local_id or candidate.id),
                        "title": candidate.title,
                        "decision": candidate.decision,
                    }
                )
        results[job.record_id] = {
            "status": job.status,
            "id": str(job.record_id),
            "scanned": job.progress.get("scanned", 0),
            **job.result,
            "proposals": links,
        }
    return results
