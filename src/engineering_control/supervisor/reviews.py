"""Read-only inventory shared by project chat, its tools and the incident monitor."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import String, case, cast, func, literal, or_, select, union_all
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from engineering_control.dashboard import evidence_verdicts
from engineering_control.dashboard.planning_queue import review_results
from engineering_control.dashboard.project_records import (
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.db.models import (
    ConnectionFinding,
    EpicExecutionPlan,
    FlowVersion,
    PlanningReview,
    ProjectConnection,
    ProjectRecord,
    ReviewEvidenceJob,
    ReviewFlowRun,
    SandboxRun,
    SandboxStep,
    SupervisorAdvice,
    WorkQueue,
    WorkspaceCandidate,
    WorkspaceImport,
)
from engineering_control.engine.builtin import BUILTIN
from engineering_control.engine.catalogue import REVIEW_TASKS
from engineering_control.engine.reviews import ACTIONS
from engineering_control.flow_library import AUDIT, DELIVER, DISCOVER
from engineering_control.table_sort import Direction, sql_order

ATTENTION = (
    "awaiting_decision",
    "failed",
    "stalled",
    "needs_input",
    "review",
    "held",
    "uncertain",
    "waiting",
)
PAGE_SIZE = 20


def destination(project: str, action: str) -> str:
    path = {
        "execution": "queue",
        "connections": "settings/connections",
        "automatic": "reviews/planning",
        "evidence": "reviews/related-work",
        "supervisor": "supervisor",
    }.get(action, "reviews/discovery")
    return f"/console/workspace/{project}/{path}"


def operational_review() -> ColumnElement[bool]:
    """Model evaluations share review accounting but have no project work to recover."""
    return or_(
        ReviewFlowRun.action != "supervisor",
        ReviewFlowRun.findings["benchmark"].as_string().is_(None),
    )


def inventory(now: datetime) -> Any:
    r, e, plan, conn, job, imp = (
        ReviewFlowRun,
        ReviewEvidenceJob,
        EpicExecutionPlan,
        ProjectConnection,
        PlanningReview,
        WorkspaceImport,
    )
    coverage = imp.progress["coverage_run"]["state"].as_string()
    scanning = imp.progress["scan_state"].as_string()
    operation = imp.progress["operation"]["state"].as_string()
    import_state = case(
        (r.action.in_(("epics", "stories", "issues")), operation),
        (r.action == "coverage", func.coalesce(coverage, scanning)),
        else_=scanning,
    )
    # An elapsed worker lease is evidence of a stall. A queued or paused job can be old by design.
    stalled = or_(
        (job.lease.is_not(None)) & (job.due_at < now),
        (conn.lease.is_not(None)) & (conn.due_at < now),
        (plan.lease.is_not(None)) & (plan.due_at < now),
        (imp.lease.is_not(None)) & (imp.lease_until < now),
    )
    outstanding = (
        select(WorkspaceCandidate.id)
        .where(WorkspaceCandidate.review_run_id == r.id, WorkspaceCandidate.decision == "pending")
        .exists()
    )
    state = case(
        (outstanding, "awaiting_decision"),
        (job.status == "needs_input", "needs_input"),
        (r.finished_at.is_not(None), func.coalesce(r.outcome, "finished")),
        (stalled, "stalled"),
        else_=func.coalesce(
            SupervisorAdvice.state,
            e.state,
            job.status,
            conn.status,
            plan.status,
            import_state,
            "running",
        ),
    )
    flow = (
        select(
            literal("review").label("source"),
            r.id.label("id"),
            r.project_id.label("pid"),
            r.action.label("action"),
            r.flow_name.label("title"),
            state.label("state"),
            r.version.label("version"),
            r.started_at.label("at"),
            operational_review().label("operational"),
        )
        .outerjoin(SupervisorAdvice, SupervisorAdvice.run_id == r.id)
        .outerjoin(e, e.run_id == r.id)
        .outerjoin(job, job.review_run_id == r.id)
        .outerjoin(conn, conn.review_run_id == r.id)
        .outerjoin(plan, plan.review_run_id == r.id)
        .outerjoin(imp, imp.review_run_id == r.id)
    )
    # A custom delivery can name the node freely. The pinned flow document records its task type.
    step = (
        select(SandboxStep.id)
        .where(
            SandboxStep.run_id == SandboxRun.id,
            or_(
                SandboxStep.name.ilike("%review%"),
                SandboxStep.name.ilike("%audit%"),
                SandboxStep.input["task"].as_string().in_(REVIEW_TASKS),
            ),
        )
        .exists()
    )
    key = SandboxRun.data["flow"]["key"].as_string()
    version = SandboxRun.data["flow"]["version"].as_integer()
    checked = SandboxRun.data["flow"]["has_review"].as_boolean()
    builtin_reviews = [
        k for k, d in BUILTIN.items() if any(n.task in REVIEW_TASKS for n in d.nodes)
    ]
    saved_tasks = cast(FlowVersion.document["tasks"], String)
    historical = (
        select(FlowVersion.id)
        .where(
            FlowVersion.flow_key == key,
            FlowVersion.version == version,
            or_(*(saved_tasks.contains(f'"{task}"') for task in REVIEW_TASKS)),
        )
        .exists()
    )
    sandbox = select(
        literal("sandbox"),
        SandboxRun.id,
        SandboxRun.project_id,
        func.coalesce(key, SandboxRun.kind),
        SandboxRun.title,
        SandboxRun.state,
        SandboxRun.data["flow"]["version"].as_integer(),
        SandboxRun.created_at,
        literal(True),
    ).where(
        or_(
            SandboxRun.kind.in_((AUDIT, DISCOVER)),
            (SandboxRun.kind == DELIVER) & key.is_(None),
            checked.is_(True),
            checked.is_(None) & (((version == 0) & key.in_(builtin_reviews)) | historical),
            key.in_(("ui-audit", "ux-audit", "audit-code")),
            step,
        )
    )
    # Owners exist before the worker takes its first lease. Switch to the run identity when claimed.
    empty_version = cast(literal(None), r.version.type)
    pending_job = select(
        literal("planning"),
        job.record_id,
        job.project_id,
        literal("automatic"),
        job.title,
        job.status,
        empty_version,
        job.created_at,
        literal(True),
    ).where(job.review_run_id.is_(None))
    pending_conn = select(
        literal("connection"),
        conn.id,
        conn.owner_id,
        literal("connections"),
        literal("Review project connection"),
        conn.status,
        empty_version,
        conn.created_at,
        literal(True),
    ).where(conn.review_run_id.is_(None), conn.archived_at.is_(None))
    pending_plan = (
        select(
            literal("plan"),
            plan.queue_id,
            WorkQueue.project_id,
            literal("execution"),
            literal("Review execution order"),
            plan.status,
            empty_version,
            plan.created_at,
            literal(True),
        )
        .join(WorkQueue, WorkQueue.id == plan.queue_id)
        .where(plan.review_run_id.is_(None))
    )
    pending_import = select(
        literal("import"),
        imp.project_id,
        imp.project_id,
        case((imp.stage.in_(("epics", "stories", "issues")), imp.stage), else_="description"),
        imp.repository,
        func.coalesce(imp.progress["scan_state"].as_string(), imp.stage),
        empty_version,
        imp.created_at,
        literal(True),
    ).where(
        imp.review_run_id.is_(None),
        or_(
            imp.stage != "inventory",
            imp.lease.is_not(None),
            imp.commit_sha.is_not(None),
            scanning.is_not(None),
            operation.is_not(None),
        ),
    )
    return union_all(
        flow, sandbox, pending_job, pending_conn, pending_plan, pending_import
    ).subquery()


class ReviewHistory:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def listing(
        self,
        projects: list[str] | None,
        *,
        page: int = 1,
        state: str = "",
        action: str = "",
        sort: str = "",
        direction: Direction = "asc",
    ) -> dict[str, Any]:
        if page < 1:
            raise ProjectInvalid("Choose a positive review page.")
        rows = inventory(datetime.now(UTC))
        with self.sessions() as db:
            names = {
                pid: name
                for pid, name in db.execute(
                    select(ProjectRecord.id, ProjectRecord.name).where(
                        ProjectRecord.name.in_(projects) if projects is not None else literal(True)
                    )
                ).all()
            }
            query = select(rows).where(rows.c.pid.in_(names))
            summary = query.subquery()
            counts = {
                state: count
                for state, count in db.execute(
                    select(summary.c.state, func.count()).group_by(summary.c.state)
                )
            }
            seen = set(db.execute(select(summary.c.pid, summary.c.action).distinct()).all())
            if state == "attention":
                query = query.where(rows.c.operational, rows.c.state.in_(ATTENTION))
            elif state:
                query = query.where(rows.c.state == state)
            if action:
                query = query.where(rows.c.action == action)
            total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
            values = {
                "title": func.lower(rows.c.title),
                "project": select(ProjectRecord.name)
                .where(ProjectRecord.id == rows.c.pid)
                .correlate(rows)
                .scalar_subquery(),
                "state": rows.c.state,
                "version": rows.c.version,
            }
            order = sql_order(values[sort], direction) if sort else rows.c.at.desc()
            result = db.execute(
                query.order_by(order, rows.c.source, rows.c.id.desc())
                .limit(PAGE_SIZE)
                .offset((page - 1) * PAGE_SIZE)
            ).mappings()
            items = []
            for row in result:
                project = names[row["pid"]]
                items.append(
                    {
                        "id": str(row["id"]),
                        "project": project,
                        "source": row["source"],
                        "action": row["action"],
                        "title": row["title"],
                        "state": row["state"],
                        "version": row["version"],
                        "at": row["at"].isoformat(),
                        "url": f"/console/run?id={row['id']}"
                        if row["source"] == "sandbox"
                        else destination(project, row["action"]),
                    }
                )
            return {
                "total": total,
                "page": page,
                "pages": (total + PAGE_SIZE - 1) // PAGE_SIZE,
                "items": items,
                "counts": counts,
                "never_started": {
                    name: [key for key in ACTIONS if (pid, key) not in seen]
                    for pid, name in names.items()
                },
            }

    def findings(self, project: str, *, page: int = 1) -> dict[str, Any]:
        """Current discovery and planning proposals retain their own administrator decisions."""
        with self.sessions() as db:
            pid = active_project(db, project).id
            c = WorkspaceCandidate
            where = (c.project_id == pid) & (c.decision == "pending")
            total = db.scalar(select(func.count()).select_from(c).where(where)) or 0
            items = [
                {
                    "id": str(row.id),
                    "kind": row.kind,
                    "title": row.title,
                    "reason": row.review_reason,
                    "url": (
                        f"/console/workspace/{project}?tab={row.kind}"
                        f"&record={row.local_id or row.id}"
                    ),
                }
                for row in db.scalars(
                    select(c)
                    .where(where)
                    .order_by(c.created_at.desc(), c.id)
                    .limit(PAGE_SIZE)
                    .offset((max(1, page) - 1) * PAGE_SIZE)
                )
            ]
            return {"total": total, "page": page, "items": items}

    def detail(self, project: str, source: str, key: UUID, *, page: int = 1) -> dict[str, Any]:
        from engineering_control.sandbox.runs import Runs

        with self.sessions() as db:
            # Archived projects retain their historical receipts in the all-project review view.
            pid = db.scalar(select(ProjectRecord.id).where(ProjectRecord.name == project))
            if pid is None:
                raise ProjectNotFound(project)
            if source == "sandbox":
                run = db.get(SandboxRun, key)
                rows = inventory(datetime.now(UTC))
                if (
                    not run
                    or run.project_id != pid
                    or not db.scalar(
                        select(rows.c.id).where(rows.c.source == "sandbox", rows.c.id == key)
                    )
                ):
                    raise ProjectNotFound(str(key))
                data = Runs(self.sessions).get(key)
                return {
                    "title": data["title"],
                    "state": data["state"],
                    "result": (data.get("data") or {}).get("result"),
                    "steps": [
                        {k: s.get(k) for k in ("position", "name", "state", "outcome", "detail")}
                        for s in data["steps"]
                    ],
                    "url": f"/console/run?id={key}",
                }
            result: dict[str, Any] = {}
            if source == "review":
                record = db.get(ReviewFlowRun, key)
                if not record or record.project_id != pid:
                    raise ProjectNotFound(str(key))
                result.update(
                    title=record.flow_name,
                    action=record.action,
                    version=record.version,
                    outcome=record.outcome,
                    evidence=record.evidence_trace,
                    findings=record.findings,
                    url=destination(project, record.action),
                )
                query = select(WorkspaceCandidate).where(
                    WorkspaceCandidate.review_run_id == record.id,
                    WorkspaceCandidate.project_id == pid,
                )
                result["proposals"] = [
                    {
                        "id": str(c.id),
                        "title": c.title,
                        "kind": c.kind,
                        "decision": c.decision,
                        "evidence": c.review_reason,
                    }
                    for c in db.scalars(
                        query.order_by(WorkspaceCandidate.id)
                        .limit(PAGE_SIZE)
                        .offset((max(1, page) - 1) * PAGE_SIZE)
                    )
                ]
                if record.action == "supervisor":
                    advice = db.get(SupervisorAdvice, key)
                    if advice:
                        result["advice"] = {
                            "state": advice.state,
                            "request": advice.request["request"],
                            "result": advice.result,
                        }
                if record.action == "evidence":
                    try:
                        result["verdict"] = evidence_verdicts.detail(self.sessions, project, key)
                    except ProjectNotFound:
                        pass
                result["workers"] = self._workers(db, pid, key, source, page)
            elif source in ("planning", "connection", "plan", "import"):
                result["workers"] = self._workers(db, pid, key, source, page)
                if not result["workers"]:
                    raise ProjectNotFound(str(key))
            else:
                raise ProjectInvalid("Choose a review source from the inventory.")
            return result

    @staticmethod
    def _workers(db: Session, pid: UUID, key: UUID, source: str, page: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        job, conn, plan, imp = PlanningReview, ProjectConnection, EpicExecutionPlan, WorkspaceImport
        if source in ("planning", "review"):
            for j in db.scalars(
                select(job).where(
                    job.project_id == pid,
                    (job.record_id if source == "planning" else job.review_run_id) == key,
                )
            ):
                result["planning"] = review_results(db, pid, [j])[j.record_id]
        if source in ("connection", "review"):
            c = db.scalar(
                select(conn).where(
                    conn.owner_id == pid,
                    (conn.id if source == "connection" else conn.review_run_id) == key,
                )
            )
            if c:
                found = db.scalars(
                    select(ConnectionFinding)
                    .where(ConnectionFinding.connection_id == c.id)
                    .order_by(ConnectionFinding.id)
                    .limit(PAGE_SIZE)
                    .offset((max(1, page) - 1) * PAGE_SIZE)
                )
                result["connection"] = {
                    "status": c.status,
                    "detail": c.detail,
                    "findings": [
                        {"id": str(f.id), "status": f.status, "evidence": f.evidence} for f in found
                    ],
                }
        if source in ("plan", "review"):
            p = db.scalar(
                select(plan)
                .join(WorkQueue, WorkQueue.id == plan.queue_id)
                .where(
                    WorkQueue.project_id == pid,
                    (plan.queue_id if source == "plan" else plan.review_run_id) == key,
                )
            )
            if p:
                result["execution"] = {
                    "status": p.status,
                    "detail": p.detail,
                    "queue": str(p.queue_id),
                }
        if source in ("import", "review"):
            w = db.scalar(
                select(imp).where(
                    imp.project_id == pid,
                    (imp.project_id if source == "import" else imp.review_run_id) == key,
                )
            )
            if w:
                result["discovery"] = {
                    "stage": w.stage,
                    "repository": w.repository,
                    "revision": w.commit_sha,
                    "description": w.draft,
                    "progress": {
                        k: v for k, v in w.progress.items() if isinstance(v, (int, float, bool))
                    },
                }
        return result
