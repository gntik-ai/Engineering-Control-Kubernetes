"""Administrative decisions about dependencies between independently owned projects."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.backlog_items import clean_description, clean_title
from engineering_control.dashboard.connection_records import (
    KINDS,
    RECORD_MODELS,
    Item,
    catalog,
    counterpart,
    digest,
    explicit_references,
    link_identity,
)
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.db.models import (
    BacklogItem,
    ConnectionEvent,
    ConnectionFinding,
    ProjectConnection,
    ProjectEpic,
    ProjectRecord,
    ProjectUserStory,
    WorkspaceCandidate,
)

Relation = Literal["depends_on", "used_by", "related"]


def audit(session: Session, row: ProjectConnection, message: str) -> None:
    session.add(ConnectionEvent(connection_id=row.id, message=message))


def schedule(row: ProjectConnection) -> None:
    row.status, row.lease, row.detail, row.due_at = "queued", None, None, datetime.now(UTC)


def instant(value: datetime | None) -> str | None:
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat() if value else None


def _names(project: ProjectRecord) -> dict[str, str]:
    return {
        "name": project.name,
        "label": project.display_name,
        "repository": project.source_repository,
    }


def _get(
    session: Session, project: UUID, key: UUID, *, lock: bool = False, archived: bool = False
) -> ProjectConnection:
    query = select(ProjectConnection).where(
        ProjectConnection.id == key,
        (ProjectConnection.first_id == project) | (ProjectConnection.second_id == project),
    )
    if not archived:
        query = query.where(ProjectConnection.archived_at.is_(None))
    row = session.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise ProjectNotFound(str(key))
    return row


def _peer(session: Session, row: ProjectConnection, pid: UUID) -> ProjectRecord:
    peer = session.get(ProjectRecord, counterpart(row, pid))
    if not peer or peer.archived_at:
        raise ProjectNotFound(str(row.id))
    return peer


def _connection(
    session: Session, p: ProjectRecord, c: ProjectConnection, peer: ProjectRecord
) -> dict[str, Any]:
    counts = {
        status: count
        for status, count in session.execute(
            select(ConnectionFinding.status, func.count())
            .where(ConnectionFinding.connection_id == c.id)
            .group_by(ConnectionFinding.status)
        )
    }
    return {
        "id": str(c.id),
        "peer": _names(peer),
        "relation": "related"
        if not c.dependent_id
        else "depends_on"
        if c.dependent_id == p.id
        else "used_by",
        "purpose": c.purpose,
        "status": c.status,
        "detail": c.detail,
        "checked": instant(c.last_checked),
        "archived": bool(c.archived_at),
        "provider": c.provider,
        "model": c.model,
        "readiness": c.progress.get("readiness", {}),
        "processed": len(c.progress.get("done", {})),
        "total": c.progress.get("total", 0),
        "counts": counts,
    }


def _finding(
    f: ConnectionFinding, projects: dict[UUID, ProjectRecord], c: ProjectConnection
) -> dict[str, Any]:
    source = projects[f.source_project]
    target = projects[counterpart(c, f.source_project)]

    def endpoint(
        p: ProjectRecord, kind: str | None, key: UUID | None, number: int | None, title: str | None
    ) -> dict[str, Any]:
        return {
            **_names(p),
            "kind": kind,
            "id": str(key) if key else None,
            "number": number,
            "title": title,
        }

    return {
        "id": str(f.id),
        "origin": f.origin,
        "action": f.action,
        "status": f.status,
        "source": endpoint(
            source, f.source_kind, f.source_id, f.source_number, f.evidence.get("source_title")
        ),
        "target": endpoint(
            target, f.target_kind, f.target_id, f.target_number, f.evidence.get("target_title")
        ),
        "evidence": f.evidence,
        "version": f.fingerprint,
        "proposal": str(f.proposal_id) if f.proposal_id else None,
    }


def store_link(
    session: Session,
    c: ProjectConnection,
    source: Item,
    target: Item | None,
    number: int | None,
    origin: str,
    reason: str,
    previous: ConnectionFinding | None = None,
) -> ConnectionFinding:
    ident = link_identity(source, target, number)
    f = previous or session.scalar(
        select(ConnectionFinding).where(
            ConnectionFinding.connection_id == c.id, ConnectionFinding.identity == ident
        )
    )
    if not f and source.id:
        # An existing local issue can acquire a GitHub number after its link was reviewed.
        dest_key = target.id if target else None
        dest_number = target.number if target and target.number else number
        f = session.scalar(
            select(ConnectionFinding)
            .where(
                ConnectionFinding.connection_id == c.id,
                ConnectionFinding.source_id == source.id,
                ConnectionFinding.action == "link",
                ConnectionFinding.target_id == dest_key,
                ConnectionFinding.target_number == dest_number,
            )
            .limit(1)
        )
        if not f and dest_key:
            f = session.scalar(
                select(ConnectionFinding)
                .where(
                    ConnectionFinding.connection_id == c.id,
                    ConnectionFinding.source_id == source.id,
                    ConnectionFinding.action == "link",
                    ConnectionFinding.target_id == dest_key,
                )
                .limit(1)
            )
    evidence = {
        "reason": reason,
        "source_title": source.title,
        "target_title": target.title if target else None,
    }
    resolved_id = target.id if target and target.approved else None
    signature = digest([origin, reason, source.key, resolved_id])
    if not f:
        f = ConnectionFinding(
            connection_id=c.id,
            source_project=source.project,
            identity=ident,
            action="link",
            origin=origin,
            status="pending",
            evidence=evidence,
            fingerprint=signature,
        )
        session.add(f)
    elif f.origin == "manual" and origin != "manual":
        return f
    elif f.origin == "reference" and origin == "model":
        return f
    elif origin == "model" and f.fingerprint == signature:
        return f
    elif f.status == "outdated" or (
        f.status in ("approved", "dismissed")
        and (f.evidence.get("reason") != reason or (f.target_id and f.target_id != resolved_id))
    ):
        audit(session, c, "Evidence changed for " + source.title + ". Review the link again.")
        f.status = "pending"
    f.source_kind, f.source_id, f.source_number = source.kind, source.id, source.number
    f.target_id = resolved_id
    f.target_kind = target.kind if target and target.approved else None
    f.target_number = target.number if target and target.number else number
    f.origin, f.evidence, f.fingerprint = origin, evidence, signature
    if origin == "manual":
        f.status = "approved"
    session.flush()
    return f


class Connections:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def listing(self, name: str) -> dict[str, Any]:
        with self.sessions() as db:
            p = active_project(db, name)
            items = []
            for row in db.scalars(
                select(ProjectConnection)
                .where((ProjectConnection.first_id == p.id) | (ProjectConnection.second_id == p.id))
                .order_by(ProjectConnection.created_at)
            ):
                peer = db.get(ProjectRecord, counterpart(row, p.id))
                if peer and not peer.archived_at:
                    items.append(_connection(db, p, row, peer))
            return {"items": items}

    def save(
        self, name: str, other: str, relation: Relation, purpose: str, key: UUID | None = None
    ) -> dict[str, Any]:
        if name == other:
            raise ProjectInvalid("Choose a different project")
        try:
            with self.sessions.begin() as db:
                pair = {
                    p.name: p
                    for p in db.scalars(
                        select(ProjectRecord)
                        .where(
                            ProjectRecord.name.in_([name, other]),
                            ProjectRecord.archived_at.is_(None),
                        )
                        .order_by(ProjectRecord.id)
                        .with_for_update()
                    )
                }
                if len(pair) != 2:
                    raise ProjectNotFound(other)
                p, peer = pair[name], pair[other]
                first, second = sorted([p.id, peer.id])
                row = db.scalar(
                    select(ProjectConnection)
                    .where(
                        ProjectConnection.first_id == first, ProjectConnection.second_id == second
                    )
                    .with_for_update()
                )
                if key and (not row or row.id != key or row.archived_at):
                    raise ProjectNotFound(str(key))
                if row and not key and not row.archived_at:
                    raise ProjectConflict("These projects are already connected")
                if not row:
                    row = ProjectConnection(first_id=first, second_id=second, owner_id=p.id)
                    db.add(row)
                row.dependent_id = (
                    p.id if relation == "depends_on" else peer.id if relation == "used_by" else None
                )
                row.purpose = purpose.strip()
                row.archived_at = None
                schedule(row)
                db.flush()
                audit(
                    db,
                    row,
                    f"{p.display_name} {relation.replace('_', ' ')} {peer.display_name}. "
                    f"{row.purpose}",
                )
                return {"id": str(row.id)}
        except IntegrityError as error:
            raise ProjectConflict("These projects are already connected") from error

    def disconnect(self, name: str, key: UUID) -> dict[str, Any]:
        with self.sessions.begin() as db:
            p = active_project(db, name)
            row = _get(db, p.id, key, lock=True)
            row.archived_at = datetime.now(UTC)
            row.lease = None
            row.status = "disconnected"
            audit(db, row, "Disconnected by the administrator.")
            return {"id": str(key)}

    def detail(self, name: str, key: UUID, page: int = 1, status: str = "") -> dict[str, Any]:
        with self.sessions() as db:
            p = active_project(db, name)
            row = _get(db, p.id, key, archived=True)
            peer = _peer(db, row, p.id)
            query = select(ConnectionFinding).where(ConnectionFinding.connection_id == key)
            if status:
                query = query.where(ConnectionFinding.status == status)
            total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
            pages = max(1, (total + 24) // 25)
            page = min(page, pages)
            findings = db.scalars(
                query.order_by(ConnectionFinding.created_at.desc(), ConnectionFinding.id)
                .offset((page - 1) * 25)
                .limit(25)
            )
            projects = {p.id: p, peer.id: peer}
            events = db.scalars(
                select(ConnectionEvent)
                .where(ConnectionEvent.connection_id == key)
                .order_by(ConnectionEvent.recorded_at.desc())
                .limit(30)
            )
            return {
                **_connection(db, p, row, peer),
                "owner": projects[row.owner_id].name,
                "findings": [_finding(f, projects, row) for f in findings],
                "page": page,
                "pages": pages,
                "total_findings": total,
                "history": [{"at": instant(e.recorded_at), "message": e.message} for e in events],
            }

    def analyze(
        self, name: str, key: UUID, provider: str | None, model: str | None
    ) -> dict[str, str]:
        with self.sessions.begin() as db:
            p = active_project(db, name)
            row = _get(db, p.id, key, lock=True)
            _peer(db, row, p.id)
            if provider and model:
                row.provider, row.model = provider, model
            if row.status != "running":
                schedule(row)
                row.progress = {**row.progress, "watermark": None}
            return {"id": str(key)}

    def options(
        self, name: str, key: UUID, side: str, kind: str, search: str, page: int
    ) -> dict[str, Any]:
        with self.sessions() as db:
            p = active_project(db, name)
            row = _get(db, p.id, key)
            peer = _peer(db, row, p.id)
            owner = p if side == "source" else peer
            model = RECORD_MODELS[kind]
            q = select(model.id, model.title).where(model.project_id == owner.id)
            if model is BacklogItem:
                q = q.where(
                    BacklogItem.workspace_review.in_(
                        ("approved", "publishing", "pending")
                        if side == "source"
                        else ("approved", "publishing")
                    )
                )
            else:
                active_model = ProjectEpic if kind == "epics" else ProjectUserStory
                q = q.where(active_model.archived_at.is_(None))
            if search:
                q = q.where(model.title.icontains(search, autoescape=True))
            rows = db.execute(
                q.order_by(model.title, model.id).offset((page - 1) * 25).limit(26)
            ).all()
            return {
                "items": [{"id": str(k), "title": t} for k, t in rows[:25]],
                "more": len(rows) > 25,
            }

    def manual(
        self,
        name: str,
        key: UUID,
        source_id: UUID,
        target_kind: str | None,
        target_id: UUID | None,
        reason: str,
    ) -> dict[str, str]:
        with self.sessions.begin() as db:
            p = active_project(db, name)
            row = _get(db, p.id, key, lock=True)
            peer = _peer(db, row, p.id)
            source = catalog(db, p).local.get(source_id)
            target = catalog(db, peer).local.get(target_id) if target_id else None
            if (
                not source
                or source.kind != "issues"
                or (target_id and (not target or target.kind != target_kind or not target.approved))
            ):
                raise ProjectInvalid(
                    "Choose an issue in this project and an approved record in the connected "
                    "project"
                )
            f = store_link(
                db,
                row,
                source,
                target,
                None,
                "manual",
                reason.strip() or "Linked by the administrator.",
            )
            audit(
                db,
                row,
                "Linked " + source.title + " to " + (target.title if target else peer.display_name),
            )
            return {"id": str(f.id)}

    def decide(
        self, name: str, key: UUID, finding: UUID, version: str, accept: bool
    ) -> dict[str, str]:
        with self.sessions.begin() as db:
            p = active_project(db, name)
            row = _get(db, p.id, key, lock=True)
            peer = _peer(db, row, p.id)
            f = db.get(ConnectionFinding, finding)
            if not f or f.connection_id != row.id:
                raise ProjectNotFound(str(finding))
            if f.fingerprint != version or f.status == "outdated":
                raise ProjectConflict("The evidence changed. Reload the connection.")
            decision = "approved" if accept else "dismissed"
            if f.status == decision:
                return {"id": str(f.id)}
            if f.status != "pending" and not (f.action == "link" and not accept):
                raise ProjectConflict("This suggestion already has a decision")
            if accept:
                source_project = p if f.source_project == p.id else peer
                owner = peer if source_project == p else p
                source = catalog(db, source_project).find(f.source_id, f.source_number)
                targets = catalog(db, owner)
                target = targets.find(f.target_id, f.target_number)
                if not source or (f.target_id and (not target or not target.approved)):
                    raise ProjectConflict(
                        "A referenced record has changed. Run the analysis again."
                    )
                if f.action == "link":
                    if (
                        f.origin == "model"
                        and target
                        and f.evidence.get("input_version")
                        != digest([source.summary(), target.summary()])
                    ):
                        raise ProjectConflict("A linked record changed. Run the analysis again.")
                    if (
                        f.origin == "reference"
                        and f.target_number
                        and explicit_references(source, owner.source_repository).get(
                            f.target_number
                        )
                        != f.evidence.get("reason")
                    ):
                        raise ProjectConflict(
                            "The GitHub reference changed. Run the analysis again."
                        )
                if f.action in ("create", "edit"):
                    proposal = f.evidence
                    if proposal.get("source_version") != digest(source.summary()):
                        raise ProjectConflict("The source changed. Run the analysis again.")
                    if f.action == "edit":
                        if not target or proposal.get("target_version") != digest(target.summary()):
                            raise ProjectConflict("The target changed. Run the analysis again.")
                        cls = RECORD_MODELS[target.kind]
                        item = db.get(cls, target.id)
                        if not isinstance(item, (BacklogItem, ProjectEpic, ProjectUserStory)):
                            raise ProjectConflict("The target is no longer available")
                        item.title = clean_title(proposal["title"])
                        item.description = clean_description(proposal["description"])
                    else:
                        parents = proposal["parents"]
                        parent_kind = {"stories": "epics", "issues": "stories"}.get(
                            f.target_kind or ""
                        )
                        if f.target_kind not in KINDS or (
                            parent_kind
                            and (
                                not parents
                                or not all(
                                    UUID(k) in targets.local
                                    and targets.local[UUID(k)].kind == parent_kind
                                    and targets.local[UUID(k)].approved
                                    for k in parents
                                )
                            )
                        ):
                            raise ProjectConflict("A local parent changed. Run the analysis again.")
                        candidate = WorkspaceCandidate(
                            project_id=owner.id,
                            kind=f.target_kind,
                            origin="connection",
                            identity="connection-" + str(f.id),
                            title=clean_title(proposal["title"]),
                            description=clean_description(proposal["description"]),
                            parents=parents,
                        )
                        db.add(candidate)
                        db.flush()
                        f.proposal_id = candidate.id
            f.status = decision
            audit(
                db,
                row,
                f"{decision.capitalize()} {f.action} "
                f"from {f.evidence.get('source_title', 'record')} "
                f"to {f.evidence.get('target_title') or f.target_kind or 'project'}.",
            )
            return {"id": str(f.id)}
