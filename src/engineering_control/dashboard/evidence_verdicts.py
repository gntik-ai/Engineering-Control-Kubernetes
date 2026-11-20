"""Human review of saved comparisons. Reading and deciding a case uses no model calls."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectNotFound,
    active_project,
)
from engineering_control.dashboard.review_evidence import LIVE, passage
from engineering_control.db.models import (
    ProjectConnection,
    ProjectRecord,
    ReviewEvidenceJob,
    ReviewFlowRun,
)

Job = ReviewEvidenceJob


def eligible(db: Session, project: UUID) -> ColumnElement[bool]:
    conn = ProjectConnection
    peers = db.scalars(
        select(ProjectRecord.id)
        .join(
            conn,
            or_(
                and_(conn.first_id == project, conn.second_id == ProjectRecord.id),
                and_(conn.second_id == project, conn.first_id == ProjectRecord.id),
            ),
        )
        .where(conn.archived_at.is_(None), ProjectRecord.archived_at.is_(None))
    )
    allowed = [str(project), *(str(peer) for peer in peers)]
    original, wiki = (Job.result["arms"][i]["scope"].as_string() for i in (0, 1))
    # Frozen source keys start with the canonical project UUID. Filter both endpoints
    # before counting or returning names, including runs made before this review view.
    sample = Job.result["sample"]
    return and_(
        Job.project_id == project,
        Job.state.not_in(LIVE),
        Job.result["cached_from"].as_string().is_(None),
        func.substr(sample["story"].as_string(), 1, 36).in_(allowed),
        func.substr(sample["issue"].as_string(), 1, 36).in_(allowed),
        or_(
            original.in_(("unrelated", "unclear")),
            wiki.in_(("unrelated", "unclear")),
            original != wiki,
        ),
    )


def listing(
    sessions: sessionmaker[Session], name: str, reviewed: bool, page: int
) -> dict[str, Any]:
    with sessions() as db:
        p = active_project(db, name)
        decision = Job.result["human_review"]
        scope = decision["scope"].as_string()
        where = and_(eligible(db, p.id), scope.is_not(None) if reviewed else scope.is_(None))
        count = db.scalar(select(func.count()).select_from(Job).where(where)) or 0
        page = min(page, max(1, (count + 9) // 10))
        rows = db.execute(
            select(
                Job.run_id,
                Job.result["sample_names"],
                Job.result["arms"][0]["scope"].as_string(),
                Job.result["arms"][1]["scope"].as_string(),
                scope,
            )
            .join(ReviewFlowRun, ReviewFlowRun.id == Job.run_id)
            .where(where)
            .order_by(ReviewFlowRun.started_at.desc(), Job.run_id.desc())
            .offset(10 * (page - 1))
            .limit(10)
        )
        return {
            "total": count,
            "page": page,
            "items": [
                {
                    "id": str(run),
                    "issue": names[0],
                    "story": names[1],
                    "original": orig,
                    "wiki": wiki,
                    "decision": human,
                }
                for run, names, orig, wiki, human in rows
            ],
        }


def find(db: Session, project: UUID, run: UUID) -> ReviewEvidenceJob:
    job = db.scalar(select(Job).where(Job.run_id == run, eligible(db, project)))
    if not job:
        raise ProjectNotFound("Evidence comparison")
    return job


def detail(sessions: sessionmaker[Session], name: str, run: UUID) -> dict[str, Any]:
    with sessions() as db:
        job = find(db, active_project(db, name).id, run)
        result = job.result
        return {
            "id": str(run),
            "arms": result["arms"],
            "decision": result.get("human_review"),
            **{
                role: passage(job.source, result, result["sample"][role])
                for role in ("issue", "story")
            },
        }


def decide(
    sessions: sessionmaker[Session],
    name: str,
    run: UUID,
    scope: str,
    note: str,
    expected_revision: UUID | None,
) -> dict[str, Any]:
    with sessions.begin() as db:
        project = active_project(db, name, lock=True)
        job = find(db, project.id, run)
        old = job.result.get("human_review") or {}
        note = note.strip()
        if old.get("scope") == scope and old.get("note") == note:
            return old
        if old.get("revision") != (str(expected_revision) if expected_revision else None):
            raise ProjectConflict("This judgment changed. Reopen the comparison to see it.")
        reviewed = {
            "scope": scope,
            "note": note,
            "at": datetime.now(UTC).isoformat(),
            "revision": str(uuid4()),
        }
        job.result = {**job.result, "human_review": reviewed}
        return reviewed
