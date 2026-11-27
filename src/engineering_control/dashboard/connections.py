"""Administrative decisions about dependencies between independently owned projects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import (
    connection_lines,
    review_budget,
    review_flows,
    review_settings,
)
from engineering_control.dashboard.backlog_items import clean_description, clean_title
from engineering_control.dashboard.connection_records import (
    KINDS,
    RECORD_MODELS,
    Catalog,
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
    OrganizeLine,
    OrganizeRun,
    ProjectConnection,
    ProjectEpic,
    ProjectRecord,
    ProjectUserStory,
    WorkspaceCandidate,
)

Relation = Literal["depends_on", "used_by", "related"]
# For how long what a run applied can be undone.
UNDO = timedelta(days=30)
LISTS = {"needs_you": "Needs you", "done": "Done", "created": "Created"}
STAGES = {"read": "Read", "match": "Match", "verify": "Verify", "create": "Create", "done": "Done"}


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
    if lock and session.get_bind().dialect.name == "sqlite":
        # SQLite ignores FOR UPDATE. Acquire its write lock before reading decisions,
        # without changing the connection timestamp observed by the analysis worker.
        row = session.scalar(
            update(ProjectConnection)
            .where(ProjectConnection.id.in_(query.with_only_columns(ProjectConnection.id)))
            .values(updated_at=ProjectConnection.updated_at)
            .returning(ProjectConnection)
        )
    else:
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
        "budget_held": bool(review_budget.status(session, c.owner_id)["required"]),
        "control_id": str(c.progress.get("run_id") or c.updated_at.isoformat()),
        "detail": c.detail,
        "checked": instant(c.last_checked),
        "archived": bool(c.archived_at),
        "provider": c.provider,
        "model": c.model,
        "readiness": c.progress.get("readiness", {}),
        "processed": len(c.progress.get("done", {})),
        "total": c.progress.get("total", 0),
        "counts": counts,
        # What the connection's runs left in their three lists.
        "lists": connection_lines.counts(session, c.id),
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


def _pending_issue_links(key: UUID) -> Select[tuple[ConnectionFinding]]:
    f = ConnectionFinding
    return select(f).where(
        f.connection_id == key,
        f.status == "pending",
        f.action == "link",
        (f.source_kind == "issues")
        | (f.target_kind == "issues")
        | (f.target_kind.is_(None) & f.target_number.is_not(None)),
    )


def _reviewed_endpoints(
    f: ConnectionFinding, sources: Catalog, targets: Catalog
) -> tuple[Item, Item | None]:
    source = sources.find(f.source_id, f.source_number)
    target = targets.find(f.target_id, f.target_number)
    if not source or (f.target_id and (not target or not target.approved)):
        raise ProjectConflict("A referenced record has changed. Run the analysis again.")
    if f.action == "link":
        if (f.source_id and source.id != f.source_id) or (
            f.target_id and target and target.id != f.target_id
        ):
            raise ProjectConflict("A referenced record has changed. Run the analysis again.")
        if f.origin == "model" and (
            not target
            or f.evidence.get("input_version") != digest([source.summary(), target.summary()])
        ):
            raise ProjectConflict("A linked record changed. Run the analysis again.")
        if (
            f.origin == "reference"
            and f.target_number
            and explicit_references(source, targets.project.source_repository).get(f.target_number)
            != f.evidence.get("reason")
        ):
            raise ProjectConflict("The GitHub reference changed. Run the analysis again.")
    return source, target


def _record_decision(
    db: Session, row: ProjectConnection, f: ConnectionFinding, decision: str
) -> None:
    f.status = decision
    audit(
        db,
        row,
        f"{decision.capitalize()} {f.action} from {f.evidence.get('source_title', 'record')} "
        f"to {f.evidence.get('target_title') or f.target_kind or 'project'}.",
    )


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
                if row.status != "paused":
                    schedule(row)
                # A changed connection gets a new plan.
                row.progress = {**(row.progress or {}), "planned": None}
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
            review_flows.finish(db, row, "cancelled")
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
            # What a judge did not suggest too comes first: it is in doubt.
            ordered = sorted(
                db.scalars(
                    query.order_by(ConnectionFinding.created_at.desc(), ConnectionFinding.id)
                ).all(),
                key=lambda f: (
                    0
                    if f.status == "pending"
                    and ((f.evidence or {}).get("judge") or {}).get("agreed") is False
                    else 1
                ),
            )
            findings = ordered[(page - 1) * 25 : page * 25]
            projects = {p.id: p, peer.id: peer}
            # The button covers all pages. Its cutoff keeps later or revised suggestions
            # pending, including when the same request is retried after a lost response.
            link_count, through = db.execute(
                _pending_issue_links(key).with_only_columns(
                    func.count(ConnectionFinding.id), func.max(ConnectionFinding.updated_at)
                )
            ).one()
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
                "pending_issue_links": {"count": link_count, "through": instant(through)},
                "history": [{"at": instant(e.recorded_at), "message": e.message} for e in events],
            }

    def analyze(
        self, name: str, key: UUID, provider: str | None, model: str | None
    ) -> dict[str, str]:
        """The model that answers for the connection, and its plan prepared again. No
        model is asked: that needs the plan to be carried out."""
        with self.sessions.begin() as db:
            p = active_project(db, name)
            row = _get(db, p.id, key, lock=True)
            _peer(db, row, p.id)
            review_budget.require_available(db, row.owner_id)
            if row.status == "paused":
                raise ProjectConflict("Resume the stopped connection first.")
            if provider and model:
                row.provider, row.model = provider, model
            if connection_lines.carried(db, row.id) is None:
                review_flows.finish(db, row, "cancelled")
                schedule(row)
                row.progress = {**row.progress, "planned": None, "run_id": str(uuid4())}
            return {"id": str(key)}

    def pause_analysis(self, name: str, key: UUID, control_id: str, paused: bool) -> dict[str, str]:
        with self.sessions.begin() as db:
            project = active_project(db, name)
            row = _get(db, project.id, key, lock=True)
            _peer(db, row, project.id)
            if (
                row.archived_at
                or str(row.progress.get("run_id") or row.updated_at.isoformat()) != control_id
            ):
                raise ProjectConflict("This connection analysis has changed. Reload its status.")
            if not paused:
                review_budget.require_available(db, row.owner_id)
            if (row.status == "paused") == paused:
                raise ProjectConflict("The connection analysis is no longer in that state.")
            run = connection_lines.carried(db, row.id)
            if paused:
                row.status, row.lease = "paused", None
                if run is not None:
                    run.state, run.detail = "held", "Stopped by the administrator."
            else:
                schedule(row)
                if run is not None:
                    run.state, run.detail = "running", ""
            row.progress = {**row.progress, "run_id": str(uuid4())}
            return {"status": row.status}

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

    def connect_all_issues(self, name: str, key: UUID, through: datetime) -> dict[str, int]:
        through = through.astimezone(UTC)
        if through > datetime.now(UTC):
            raise ProjectInvalid("Use the review time returned by this connection")
        with self.sessions.begin() as db:
            p = active_project(db, name)
            row = _get(db, p.id, key, lock=True)
            peer = _peer(db, row, p.id)
            pending = _pending_issue_links(key)
            newer = (
                db.scalar(
                    pending.with_only_columns(func.count(ConnectionFinding.id)).where(
                        ConnectionFinding.updated_at > through
                    )
                )
                or 0
            )
            links = db.scalars(
                pending.where(ConnectionFinding.updated_at <= through)
                .order_by(ConnectionFinding.id)
                .with_for_update()
            ).all()
            if not links:
                return {"connected": 0, "stale": 0, "newer": newer}
            catalogs = {project.id: catalog(db, project) for project in (p, peer)}
            connected = stale = 0
            for f in links:
                try:
                    _reviewed_endpoints(
                        f, catalogs[f.source_project], catalogs[counterpart(row, f.source_project)]
                    )
                except ProjectConflict:
                    stale += 1
                    continue
                _record_decision(db, row, f, "approved")
                connection_lines.decided(db, f, "approved", None)
                connected += 1
            return {"connected": connected, "stale": stale, "newer": newer}

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
            _decide(db, p, row, peer, f, accept)
            return {"id": str(f.id)}

    # -- the plan of a connection, and what its runs found ---------------------------

    def plan(self, name: str, key: UUID) -> dict[str, Any]:
        """The connection's plan as it waits for the word, the run that is carrying a
        plan out or the last that did, and how many lines the three lists hold."""
        with self.sessions() as db:
            p = active_project(db, name)
            row = _get(db, p.id, key, archived=True)
            peer = _peer(db, row, p.id)
            waiting = connection_lines.proposed(db, row.id)
            runs = [run for run in connection_lines.runs_of(db, row.id) if run.state != "proposed"]
            run = runs[0] if runs else None
            plan = dict(waiting.estimate or {}) if waiting else None
            live = run is not None and run.state in ("running", "held")
            if plan is None and not live and row.status == "paused" and not row.archived_at:
                # The worker does not wake a stopped connection: its plan is worked out
                # here, when it is asked for.
                from engineering_control.dashboard.connection_analysis import plan_now

                plan = plan_now(db, row)
            why = None
            if run is not None and run.state in ("running", "held"):
                why = "The plan is being carried out."
            elif plan is None and run is not None and row.status == "ready":
                why = "Nothing is left to check: the plan is carried out."
            elif plan is None:
                why = "The plan is being prepared. It is here within a minute."
            elif plan.get("blocked"):
                why = str(plan["blocked"])
            elif not (plan["ask"]["records"] or plan.get("stated")):
                why = "Nothing is left to check: the plan is carried out."
            elif plan["ask"]["records"] and review_settings.over(db, row.owner_id):
                # The month's limit is reached: it says so before the word is given.
                why = review_settings.over(db, row.owner_id)
            counts = run.counts or {} if run else {}
            reached = list(STAGES).index(run.stage) if run and run.stage in STAGES else -1
            return {
                "connection": str(row.id),
                "projects": {str(p.id): _names(p), str(peer.id): _names(peer)},
                "owner": p.name if row.owner_id == p.id else peer.name,
                # The plan that waits for the word; else the one that was carried out.
                "plan": plan if plan is not None else dict(run.estimate or {}) if run else None,
                "waits": plan is not None,
                "can_carry_out": why is None,
                "why_not": why,
                "run": {
                    "id": str(run.id),
                    "state": run.state,
                    "stage": run.stage,
                    "detail": run.detail,
                    "spent": run.spent,
                    "cost": run.cost,
                    "started_at": instant(run.started_at),
                    "ended_at": instant(run.ended_at),
                    "stages": [
                        {
                            "stage": stage,
                            "name": title,
                            "reached": index <= reached,
                            **{
                                key: int((counts.get(stage) or {}).get(key, 0))
                                for key in ("records", "settled", "in_doubt", "none")
                            },
                        }
                        for index, (stage, title) in enumerate(STAGES.items())
                    ],
                }
                if run
                else None,
                "runs": len(runs),
                "lists": connection_lines.counts(db, row.id),
                "undo_days": UNDO.days,
            }

    def carry_out(self, name: str, key: UUID, *, within_limit: bool = False) -> dict[str, str]:
        """The administrator's word: the plan is carried out. Where it is estimated to
        cost more than is left of the month's limit, it is refused with what it would
        cost, unless the word is to go as far as the limit allows."""
        with self.sessions.begin() as db:
            p = active_project(db, name)
            row = _get(db, p.id, key, lock=True)
            _peer(db, row, p.id)
            if connection_lines.carried(db, row.id) is not None:
                raise ProjectConflict("The plan is being carried out.")
            run = connection_lines.proposed(db, row.id)
            if run is None and row.status == "paused":
                from engineering_control.dashboard.connection_analysis import plan_now

                made = plan_now(db, row)
                if made is not None:
                    run = OrganizeRun(
                        project_id=row.owner_id,
                        kind="connection",
                        connection_id=row.id,
                        scope=[str(row.first_id), str(row.second_id)],
                        state="proposed",
                        stage="read",
                        counts={},
                        estimate=made,
                    )
                    db.add(run)
                    db.flush()
            if run is None:
                raise ProjectConflict("The plan is being prepared. It is here within a minute.")
            plan = run.estimate or {}
            if plan.get("blocked"):
                raise ProjectInvalid(str(plan["blocked"]))
            ask = plan.get("ask") or {}
            if not (ask.get("records") or plan.get("stated")):
                raise ProjectConflict("Nothing is left to check: the plan is carried out.")
            if ask.get("records"):
                review_budget.require_available(db, row.owner_id)
            if ask.get("over") and not within_limit:
                left = max(0.0, float(ask["limit"]) - float(ask["spent"]))
                raise ProjectConflict(
                    f"Carrying out the plan is estimated at {float(ask['dollars']):.2f} US "
                    f"dollars, and {left:.2f} are left of this month's limit. Raise the limit "
                    "under Reviews, Settings, or carry it out as far as the limit allows."
                )
            run.state, run.started_at, run.stage, run.detail = (
                "running",
                datetime.now(UTC),
                "match",
                "",
            )
            schedule(row)
            row.progress = {**row.progress, "stated": False, "run_id": str(uuid4())}
            audit(db, row, "The plan is carried out, at the administrator's word.")
            return {"id": str(run.id)}

    def _line(self, db: Session, row: ProjectConnection, key: UUID) -> OrganizeLine:
        line = db.get(OrganizeLine, key)
        run = db.get(OrganizeRun, line.run_id) if line else None
        if line is None or run is None or run.connection_id != row.id:
            raise ProjectNotFound(str(key))
        return line

    def lines(
        self, name: str, key: UUID, listed: str, page: int = 1, page_size: int = 25
    ) -> dict[str, Any]:
        """One of the connection's three lists, over all its runs. "Needs you" shows
        what still waits, the most doubtful first; the others what stands, newest first."""
        from engineering_control.dashboard.organize import shown_line

        if listed not in LISTS:
            raise ProjectInvalid("That is no list of a run")
        now = datetime.now(UTC)
        page, page_size = max(1, page), min(100, max(1, page_size))
        with self.sessions() as db:
            p = active_project(db, name)
            row = _get(db, p.id, key, archived=True)
            peer = _peer(db, row, p.id)
            names = {str(p.id): _names(p), str(peer.id): _names(peer)}
            where = [
                OrganizeLine.run_id.in_(
                    select(OrganizeRun.id).where(OrganizeRun.connection_id == row.id)
                ),
                OrganizeLine.listed == listed,
                OrganizeLine.state == ("open" if listed == "needs_you" else "applied"),
            ]
            total = int(
                db.scalar(select(func.count()).select_from(OrganizeLine).where(*where)) or 0
            )
            order = (
                (OrganizeLine.doubt.desc(), OrganizeLine.created_at, OrganizeLine.id)
                if listed == "needs_you"
                else (OrganizeLine.answered_at.desc(), OrganizeLine.id)
            )
            items = []
            for line in db.scalars(
                select(OrganizeLine)
                .where(*where)
                .order_by(*order)
                .offset((page - 1) * page_size)
                .limit(page_size)
            ):
                shown = shown_line(line, now)
                shown["subject"]["in"] = names.get(str(line.subject_project))
                for candidate in shown["candidates"]:
                    candidate["in"] = names.get(str(candidate.get("project")))
                if line.case == "text" and line.state == "open":
                    # A text that would change is shown as it stands beside what is proposed.
                    target = (line.candidates or [{}])[0]
                    model = RECORD_MODELS.get(str(target.get("kind")))
                    record = (
                        db.get(model, UUID(target["id"])) if model and target.get("id") else None
                    )
                    shown["now"] = (
                        {"title": record.title, "description": record.description or ""}
                        if isinstance(record, (BacklogItem, ProjectEpic, ProjectUserStory))
                        else None
                    )
                items.append(shown)
            return {
                "list": listed,
                "name": LISTS[listed],
                "total": total,
                "page": page,
                "pages": max(1, -(-total // page_size)),
                "items": items,
            }

    def answer(self, name: str, key: UUID, line: UUID, accept: bool) -> dict[str, str]:
        """The administrator's answer to a case in doubt. Where the records changed
        since it was proposed, it is set aside and says so."""
        with self.sessions.begin() as db:
            p = active_project(db, name)
            row = _get(db, p.id, key, lock=True)
            peer = _peer(db, row, p.id)
            entry = self._line(db, row, line)
            if entry.listed != "needs_you" or entry.state != "open":
                raise ProjectConflict("This case was decided already.")
            action = entry.action or {}
            f = db.get(ConnectionFinding, UUID(str(action.get("finding"))))
            problem = None
            if f is None or f.status != "pending" or f.fingerprint != action.get("version"):
                problem = "The records changed since this was proposed."
            else:
                try:
                    _decide(db, p, row, peer, f, accept)
                except ProjectConflict as changed:
                    problem = str(changed)
            if problem:
                entry.state, entry.answered_at = "skipped", datetime.now(UTC)
                entry.reason = f"{entry.reason} Set aside: {problem}".strip()
                return {"id": str(entry.id), "state": "skipped", "detail": problem}
            return {"id": str(entry.id), "state": entry.state}

    def undo(self, name: str, key: UUID, line: UUID) -> dict[str, str]:
        """What a line applied is taken back, and the case waits for the administrator
        again."""
        with self.sessions.begin() as db:
            p = active_project(db, name)
            row = _get(db, p.id, key, lock=True)
            peer = _peer(db, row, p.id)
            _undo(db, row, {p.id: p, peer.id: peer}, self._line(db, row, line))
            return {"id": str(line)}

    def undo_run(self, name: str, key: UUID, run: UUID) -> dict[str, int]:
        """Everything a run applied is taken back, as far as it still can be."""
        with self.sessions.begin() as db:
            p = active_project(db, name)
            row = _get(db, p.id, key, lock=True)
            peer = _peer(db, row, p.id)
            found = db.get(OrganizeRun, run)
            if found is None or found.connection_id != row.id:
                raise ProjectNotFound(str(run))
            undone = kept = 0
            for entry in db.scalars(
                select(OrganizeLine).where(
                    OrganizeLine.run_id == found.id, OrganizeLine.state == "applied"
                )
            ):
                try:
                    with db.begin_nested():
                        _undo(db, row, {p.id: p, peer.id: peer}, entry)
                    undone += 1
                except ProjectConflict:
                    kept += 1
            found.undone_at = datetime.now(UTC)
            audit(db, row, f"A run was undone: {undone} changes taken back, {kept} kept.")
            return {"undone": undone, "kept": kept}


def _between(db: Session, one: UUID, two: UUID) -> ProjectConnection:
    first, second = sorted([one, two])
    row = db.scalar(
        select(ProjectConnection).where(
            ProjectConnection.first_id == first,
            ProjectConnection.second_id == second,
            ProjectConnection.archived_at.is_(None),
        )
    )
    if row is None:
        raise ProjectConflict("The two projects are not connected.")
    return row


def link_issue_across(
    db: Session, project: ProjectRecord, item: UUID, story: UUID, reason: str, *, by: str
) -> ConnectionFinding:
    """Put an issue of this project under a User Story of a connected project: the link
    is a link of their connection. ``by`` says who made it: the ``administrator``, or a
    ``model`` whose answer a second model agreed with."""
    target = db.get(ProjectUserStory, story)
    peer = db.get(ProjectRecord, target.project_id) if target else None
    if target is None or peer is None or target.archived_at is not None:
        raise ProjectConflict("The User Story is gone.")
    row = _between(db, project.id, peer.id)
    source, dest = catalog(db, project).local.get(item), catalog(db, peer).local.get(story)
    if source is None or dest is None:
        raise ProjectConflict("The issue or the User Story is no longer listed.")
    why = reason.strip() or "Linked by the administrator."
    if by == "administrator":
        f = store_link(db, row, source, dest, None, "manual", why)
    else:
        f = store_link(db, row, source, dest, None, "model", why)
        f.evidence = {**f.evidence, "input_version": digest([source.summary(), dest.summary()])}
        if f.status != "approved":
            _record_decision(db, row, f, "approved")
    audit(db, row, f"Linked {source.title} to {dest.title} of {peer.display_name}.")
    return f


def link_story_across(
    db: Session, project: ProjectRecord, story: UUID, epic: UUID, reason: str
) -> ConnectionFinding:
    """Put a User Story of this project under an Epic of a connected project, at the
    administrator's word: the link is a link of their connection."""
    target = db.get(ProjectEpic, epic)
    peer = db.get(ProjectRecord, target.project_id) if target else None
    if target is None or peer is None or target.archived_at is not None:
        raise ProjectConflict("The Epic is gone.")
    row = _between(db, project.id, peer.id)
    source, dest = catalog(db, project).local.get(story), catalog(db, peer).local.get(epic)
    if source is None or dest is None:
        raise ProjectConflict("The User Story or the Epic is no longer listed.")
    f = store_link(
        db, row, source, dest, None, "manual", reason.strip() or "Linked by the administrator."
    )
    audit(db, row, f"Linked {source.title} to {dest.title} of {peer.display_name}.")
    return f


def unlink_issue_across(db: Session, project: ProjectRecord, item: UUID, story: UUID) -> bool:
    """Take the issue off the User Story of a connected project again; or a User Story
    off an Epic of one."""
    f = db.scalar(
        select(ConnectionFinding)
        .join(ProjectConnection, ProjectConnection.id == ConnectionFinding.connection_id)
        .where(
            (ProjectConnection.first_id == project.id)
            | (ProjectConnection.second_id == project.id),
            ProjectConnection.archived_at.is_(None),
            ConnectionFinding.action == "link",
            ConnectionFinding.status == "approved",
            ConnectionFinding.source_id == item,
            ConnectionFinding.target_id == story,
        )
        .limit(1)
    )
    if f is None:
        return False
    row = db.get(ProjectConnection, f.connection_id)
    if row is not None:
        _record_decision(db, row, f, "dismissed")
    return True


def _decide(
    db: Session,
    p: ProjectRecord,
    row: ProjectConnection,
    peer: ProjectRecord,
    f: ConnectionFinding,
    accept: bool,
) -> None:
    """Approve or dismiss a finding, and say so on the line of the run that holds it."""
    decision = "approved" if accept else "dismissed"
    if f.status == decision:
        return
    if f.status != "pending" and not (f.action == "link" and not accept):
        raise ProjectConflict("This suggestion already has a decision")
    before: dict[str, Any] | None = None
    if accept:
        source_project = p if f.source_project == p.id else peer
        owner = peer if source_project == p else p
        targets = catalog(db, owner)
        source, target = _reviewed_endpoints(f, catalog(db, source_project), targets)
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
                # How the text stood, to put it back.
                before = {"title": item.title, "description": item.description}
                item.title = clean_title(proposal["title"])
                item.description = clean_description(proposal["description"])
            else:
                parents = proposal["parents"]
                parent_kind = {"stories": "epics", "issues": "stories"}.get(f.target_kind or "")
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
    _record_decision(db, row, f, decision)
    connection_lines.decided(db, f, decision, before)


def _undo(
    db: Session,
    row: ProjectConnection,
    projects: dict[UUID, ProjectRecord],
    line: OrganizeLine,
) -> None:
    """Take back what the line applied: a link comes off, a changed text is put back as
    it stood, a proposal that was sent to a project is withdrawn."""
    since = line.answered_at or line.created_at
    since = since if since.tzinfo else since.replace(tzinfo=UTC)
    if line.listed == "needs_you" or line.state != "applied":
        raise ProjectConflict("There is nothing to undo here.")
    if datetime.now(UTC) - since >= UNDO:
        raise ProjectConflict(f"This can be undone for {UNDO.days} days, and they are over.")
    f = db.get(ConnectionFinding, UUID(str((line.action or {}).get("finding"))))
    if f is None or f.connection_id != row.id or f.status != "approved":
        raise ProjectConflict("This was taken back already.")
    if f.action == "edit":
        model = RECORD_MODELS.get(f.target_kind or "")
        item = db.get(model, f.target_id) if model and f.target_id else None
        proposal, before = f.evidence, line.before or {}
        if (
            not isinstance(item, (BacklogItem, ProjectEpic, ProjectUserStory))
            or "title" not in before
            or item.title != clean_title(proposal["title"])
            or item.description != clean_description(proposal["description"])
        ):
            raise ProjectConflict("The text was changed again since. Correct it by hand.")
        item.title, item.description = before["title"], before.get("description")
    elif f.action == "create":
        candidate = db.get(WorkspaceCandidate, f.proposal_id) if f.proposal_id else None
        if candidate is None or candidate.decision != "pending":
            where = projects.get(candidate.project_id) if candidate else None
            raise ProjectConflict(
                "The proposal was decided in the planning tables"
                + (f" of {where.display_name}" if where else "")
                + ". Change the record there."
            )
        db.delete(candidate)
        f.proposal_id = None
    f.status = "pending"
    audit(
        db,
        row,
        f"Undone: {f.action} from {f.evidence.get('source_title', 'record')} "
        f"to {f.evidence.get('target_title') or f.target_kind or 'project'}.",
    )
    connection_lines.put_back(db, f)
