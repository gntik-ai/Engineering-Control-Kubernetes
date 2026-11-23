"""Human review of saved comparisons. Reading and deciding a case uses no model calls."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import batched
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, and_, case, cast, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, array
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectNotFound,
    active_project,
)
from engineering_control.dashboard.review_evidence import LIVE, passage, result_fields
from engineering_control.db.models import (
    ProjectConnection,
    ProjectRecord,
    ReviewEvidenceJob,
    ReviewFlowRun,
)
from engineering_control.table_sort import Direction, sql_order

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
        func.substr(sample["story"].as_string(), 1, 36).in_(allowed),
        func.substr(sample["issue"].as_string(), 1, 36).in_(allowed),
        Job.result["repeat_of"].as_string().is_(None),
        or_(
            Job.result["advisory"]["status"].as_string().in_(("needs_review", "advisory")),
            and_(
                Job.result["native"]["rubric"].as_string().is_not(None),
                Job.result["native"]["cached_from"].as_string().is_(None),
            ),
            and_(
                Job.result["cached_from"].as_string().is_(None),
                or_(
                    original.in_(("unrelated", "unclear")),
                    wiki.in_(("unrelated", "unclear")),
                    original != wiki,
                ),
            ),
        ),
    )


def listing(
    sessions: sessionmaker[Session],
    name: str,
    reviewed: bool,
    page: int,
    advisory: bool = False,
    sort: str = "",
    direction: Direction = "asc",
) -> dict[str, Any]:
    with sessions() as db:
        p = active_project(db, name)
        decision = Job.result["human_review"]
        scope = decision["scope"].as_string()
        disposition = Job.result["advisory"]["status"].as_string()
        where = and_(eligible(db, p.id), scope.is_not(None) if reviewed else scope.is_(None))
        if not reviewed:
            where = and_(
                where,
                disposition == "advisory"
                if advisory
                else or_(disposition.is_(None), disposition == "needs_review"),
            )
        count = db.scalar(select(func.count()).select_from(Job).where(where)) or 0
        page = min(page, max(1, (count + 9) // 10))
        values = {
            "issue": Job.result["sample_names"][0].as_string(),
            "story": Job.result["sample_names"][1].as_string(),
            "original": Job.result["arms"][0]["scope"].as_string(),
            "wiki": Job.result["arms"][1]["scope"].as_string(),
            "native": Job.result["native"]["scope"].as_string(),
            "second": Job.result["second_opinion"]["scope"].as_string(),
            "decision": scope,
        }
        order = ReviewFlowRun.started_at.desc()
        if sort:
            value = values[sort]
            if sort not in ("issue", "story"):
                value = case(
                    {"full": "All", "partial": "Some", "unrelated": "None", "unclear": "Unclear"},
                    value=value,
                    else_=value,
                )
            order = sql_order(func.lower(value), direction)
        rows = db.execute(
            select(
                Job.run_id,
                Job.result["sample_names"],
                Job.result["arms"][0]["scope"].as_string(),
                Job.result["arms"][1]["scope"].as_string(),
                Job.result["native"]["scope"].as_string(),
                Job.result["second_opinion"]["scope"].as_string(),
                disposition,
                scope,
            )
            .join(ReviewFlowRun, ReviewFlowRun.id == Job.run_id)
            .where(where)
            .order_by(order, Job.run_id.desc())
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
                    "native": native,
                    "second_opinion": second,
                    "disposition": status,
                    "decision": human,
                }
                for run, names, orig, wiki, native, second, status, human in rows
            ],
        }


def receipt(db: Session, project: UUID, run: UUID, *keys: str) -> dict[str, Any]:
    row = db.execute(
        select(result_fields(db, *keys))
        .select_from(Job)
        .where(Job.run_id == run, eligible(db, project))
    ).one_or_none()
    if row is None:
        raise ProjectNotFound("Evidence comparison")
    return row[0] or {}


def detail(sessions: sessionmaker[Session], name: str, run: UUID) -> dict[str, Any]:
    with sessions() as db:
        project = active_project(db, name).id
        result = receipt(
            db,
            project,
            run,
            "arms",
            "native",
            "second_opinion",
            "advisory",
            "human_review",
            "sample",
        )
        sample = result["sample"]
        roles = ("issue", "story")
        # Each saved graph can contain thousands of unrelated records. Read only
        # the two passages, their wiki cards and the Epic titles needed by the dialog.
        columns = [
            field
            for role in roles
            for field in (
                Job.source[("records", sample[role])],
                Job.result[("cards", sample[role])],
            )
        ]
        values = db.execute(select(*columns).where(Job.run_id == run)).one()
        records = {sample[role]: values[2 * i] for i, role in enumerate(roles)}
        cards = {sample[role]: values[2 * i + 1] for i, role in enumerate(roles)}
        epics = sorted(
            {key for row in records.values() for key in row["parents"] if "/epics/" in key}
        )
        for group in batched(epics, 64):
            titles = db.execute(
                select(*(Job.source[("records", key, "title")].as_string() for key in group)).where(
                    Job.run_id == run
                )
            ).one()
            records.update(
                {
                    key: {"kind": "epics", "title": title}
                    for key, title in zip(group, titles, strict=True)
                    if title is not None
                }
            )
        return {
            "id": str(run),
            "arms": result.get("arms") or [],
            "native": result.get("native"),
            "second_opinion": result.get("second_opinion"),
            "advisory": result.get("advisory"),
            "decision": result.get("human_review"),
            "prerequisites": sample.get("prerequisites", []),
            **{
                role: passage({"records": records}, {"cards": cards}, sample[role])
                for role in roles
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
        old = receipt(db, project.id, run, "human_review").get("human_review") or {}
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
        value = literal(reviewed, type_=JSON)
        changed: ColumnElement[Any]
        if db.get_bind().dialect.name == "postgresql":
            changed = cast(
                func.jsonb_set(
                    cast(Job.result, JSONB), array(["human_review"]), cast(value, JSONB)
                ),
                JSON,
            )
        else:
            changed = func.json_set(Job.result, "$.human_review", func.json(value))
        db.execute(
            update(Job)
            .where(Job.run_id == run, Job.project_id == project.id)
            .values(result=changed)
        )
        return reviewed
