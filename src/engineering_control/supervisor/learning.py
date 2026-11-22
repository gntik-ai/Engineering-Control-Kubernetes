"""Project-scoped feedback from saved executions. Reading it never schedules work."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.config import get_settings
from engineering_control.dashboard.backlog_items import clean_title
from engineering_control.dashboard.planning_queue import queue_review
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectRecords,
    active_project,
)
from engineering_control.db.models import (
    BacklogItem,
    ProjectRecord,
    ProjectSection,
    ReviewFlowRun,
    SandboxRun,
    SandboxStep,
    SupervisorAction,
    SupervisorIncident,
)
from engineering_control.flow_library import WORK_KINDS
from engineering_control.supervisor.reviews import operational_review

SAMPLE_SIZE = 50
PATTERNS_PER_PAGE = 8
WINDOW_DAYS = 30
SLOW_MINUTES = 20
BRIEF = {
    "component": "Affected component",
    "observed": "Observed behavior and impact",
    "change": "Implementation scope",
    "acceptance": "Acceptance criteria",
    "tests": "Regression tests",
    "rollout": "Deployment steps",
    "verification": "Post-deployment verification",
    "rollback": "Rollback",
}


def required_text(data: dict[str, Any], key: str, maximum: int = 1200) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not 8 <= len(value.strip()) <= maximum:
        raise ProjectInvalid(f"Supply a specific {key} between 8 and {maximum} characters.")
    if value.strip().lower().strip(".") in {
        "to be determined",
        "to be confirmed",
        "to be decided",
        "placeholder",
        "not applicable",
    }:
        raise ProjectInvalid(f"Replace the placeholder for {key}.")
    return value.strip()


def delivery_brief(data: dict[str, Any]) -> str:
    return "\n\n".join(f"## {label}\n{required_text(data, key)}" for key, label in BRIEF.items())


def draft_issue(
    db: Session,
    target: ProjectRecord,
    *,
    title: str,
    body: str,
    origin: str,
    category: str | None,
    facts: dict[str, Any],
    new_workspace: bool = False,
) -> dict[str, Any]:
    target = active_project(db, target.name, lock=True)
    section = db.scalar(
        select(ProjectSection).where(
            ProjectSection.project_id == target.id, ProjectSection.key == "issues"
        )
    )
    if new_workspace and section:
        section.flow_key = "deliver-issue"
    item = db.scalar(
        select(BacklogItem)
        .where(
            BacklogItem.project_id == target.id,
            (BacklogItem.origin_ref == origin)
            | (
                (func.lower(BacklogItem.title) == title.lower())
                & BacklogItem.state.in_(("proposed", "accepted"))
                & (
                    BacklogItem.workspace_review.in_(("pending", "publishing", "approved"))
                    | BacklogItem.issue_number.is_not(None)
                )
            ),
        )
        .order_by((BacklogItem.origin_ref == origin).desc().nulls_last(), BacklogItem.id)
        .limit(1)
    )
    existed = item is not None
    if item is None:
        item = BacklogItem(
            project_id=target.id,
            title=title,
            description=body,
            section_id=section.id if section else None,
            source="manual",
            state="proposed",
            workspace_review="pending",
            execution_type=category,
            proposed_by="supervisor",
            routed_by="supervisor" if section else None,
            origin_ref=origin,
            facts=facts,
        )
        db.add(item)
        db.flush()
        queue_review(db, target.id, "issues", item.id, item.title)
    return {
        "project": target.name,
        "repository": target.source_repository,
        "id": str(item.id),
        "title": item.title,
        "existing": existed,
        "state": item.workspace_review or item.state,
        "url": f"/console/workspace/{target.name}?tab=issues&record={item.id}",
    }


def _seconds(start: datetime | None, end: datetime | None) -> int:
    if not start or not end:
        return 0
    return max(0, int((end - start).total_seconds()))


def _receipts(db: Session, pid: UUID, source: str, key: UUID | None = None) -> list[dict[str, Any]]:
    if source == "run":
        run = SandboxRun
        query = select(
            run.id,
            func.coalesce(run.data["flow"]["key"].as_string(), run.kind).label("flow"),
            run.data["flow"]["version"].as_integer().label("version"),
            run.state.label("state"),
            run.outcome.label("outcome"),
            run.started_at,
            run.finished_at,
        ).where(
            run.project_id == pid,
            run.kind.in_((*WORK_KINDS, "supervisor_turn")),
            run.state.in_(("succeeded", "failed")),
            run.finished_at.is_not(None),
        )
        id_col, finished = run.id, run.finished_at
    elif source == "review":
        review = ReviewFlowRun
        query = select(
            review.id,
            review.flow_key.label("flow"),
            review.version.label("version"),
            review.outcome.label("state"),
            review.outcome.label("outcome"),
            review.started_at,
            review.finished_at,
        ).where(
            review.project_id == pid,
            operational_review(),
            review.finished_at.is_not(None),
            review.outcome.in_(("succeeded", "failed")),
        )
        id_col, finished = review.id, review.finished_at
    else:
        raise ProjectInvalid("Choose a saved run or review.")
    if key:
        query = query.where(id_col == key)
    else:
        query = query.where(finished >= datetime.now(UTC) - timedelta(days=WINDOW_DAYS))
    rows = db.execute(query.order_by(finished.desc(), id_col.desc()).limit(SAMPLE_SIZE)).all()
    steps: dict[UUID, list[Any]] = defaultdict(list)
    if source == "run" and rows:
        step = SandboxStep
        for s in db.execute(
            select(
                step.run_id, step.name, step.state, step.outcome, step.started_at, step.finished_at
            )
            .where(step.run_id.in_([r.id for r in rows]), step.state.in_(("failed", "succeeded")))
            .order_by(step.run_id, step.position.desc())
        ):
            steps[s.run_id].append(s)
    receipts = []
    for r in rows:
        elapsed = _seconds(r.started_at, r.finished_at)
        completed = steps[r.id]
        slowest = max(completed, key=lambda s: _seconds(s.started_at, s.finished_at), default=None)
        failed = next((s for s in completed if s.state == "failed"), None)
        signal, phase, outcome = "completed", "", r.state
        if r.state == "failed":
            signal, phase = "failure", failed.name if failed else ""
            outcome = (failed.outcome if failed else None) or r.outcome or "failed"
        elif elapsed >= SLOW_MINUTES * 60:
            signal = "bottleneck"
            if slowest and _seconds(slowest.started_at, slowest.finished_at) >= SLOW_MINUTES * 60:
                phase = slowest.name
        pattern = hashlib.sha256(
            "\n".join((source, r.flow, signal, phase, outcome or "")).encode()
        ).hexdigest()[:24]
        receipts.append(
            dict(
                source=source,
                id=str(r.id),
                flow=r.flow,
                version=r.version,
                signal=signal,
                step=phase,
                outcome=outcome,
                seconds=elapsed,
                pattern=pattern,
                at=r.finished_at.isoformat(),
            )
        )
    return receipts


def evidence(db: Session, project: ProjectRecord, args: dict[str, Any]) -> dict[str, Any]:
    try:
        key = UUID(str(args.get("evidence_id")))
    except ValueError:
        raise ProjectInvalid("Use a saved evidence ID from process learning.") from None
    records = _receipts(db, project.id, str(args.get("source")), key)
    if not records:
        raise ProjectInvalid("This project has no eligible completed process with that ID.")
    return records[0]


def controller_project(db: Session) -> ProjectRecord | None:
    return db.scalar(
        select(ProjectRecord).where(
            ProjectRecord.archived_at.is_(None),
            func.lower(ProjectRecord.source_repository)
            == get_settings().controller_repository.lower(),
        )
    )


def guidance(db: Session, project_ids: list[UUID]) -> list[dict[str, Any]]:
    """The latest approved approach per pattern, with bounded context per project."""
    if not project_ids:
        return []
    action = SupervisorAction
    ranked = (
        select(
            action.result.label("result"),
            action.project_id.label("pid"),
            action.decided_at.label("at"),
            func.row_number()
            .over(
                partition_by=(action.project_id, action.result["pattern"].as_string()),
                order_by=(action.decided_at.desc(), action.id.desc()),
            )
            .label("revision"),
        )
        .where(
            action.project_id.in_(project_ids),
            action.tool == "remember_approach",
            action.outcome == "accepted",
        )
        .subquery()
    )
    recent = (
        select(
            ranked.c.result,
            ranked.c.at,
            func.row_number()
            .over(
                partition_by=ranked.c.pid,
                order_by=(ranked.c.at.desc(), ranked.c.result["pattern"].as_string()),
            )
            .label("position"),
        )
        .where(ranked.c.revision == 1)
        .subquery()
    )
    return [
        dict(row)
        for row in db.scalars(
            select(recent.c.result)
            .where(recent.c.position <= 4)
            .order_by(recent.c.at.desc())
            .limit(12)
        )
        if row
    ]


def improvements(db: Session, source: UUID) -> list[dict[str, Any]]:
    """Follow accepted issue proposals through publication and actual delivery runs."""
    accepted = db.scalars(
        select(SupervisorAction.result)
        .where(
            SupervisorAction.project_id == source,
            SupervisorAction.tool == "propose_process_issue",
            SupervisorAction.outcome == "accepted",
        )
        .order_by(SupervisorAction.decided_at.desc(), SupervisorAction.id.desc())
        .limit(20)
    )
    keys = list(dict.fromkeys(UUID(r["id"]) for r in accepted if r and r.get("id")))
    if not keys:
        return []
    rows = db.execute(
        select(BacklogItem, ProjectRecord.name, ProjectSection.flow_key)
        .join(ProjectRecord, ProjectRecord.id == BacklogItem.project_id)
        .outerjoin(ProjectSection, ProjectSection.id == BacklogItem.section_id)
        .where(BacklogItem.id.in_(keys))
    ).all()
    latest = (
        select(
            SandboxRun.item_id,
            SandboxRun.id,
            SandboxRun.state,
            SandboxRun.outcome,
            func.row_number()
            .over(
                partition_by=SandboxRun.item_id,
                order_by=(SandboxRun.created_at.desc(), SandboxRun.id.desc()),
            )
            .label("position"),
        )
        .where(SandboxRun.item_id.in_(keys), SandboxRun.kind.in_(WORK_KINDS))
        .subquery()
    )
    runs = {r.item_id: r for r in db.execute(select(latest).where(latest.c.position == 1))}
    items = {}
    for item, name, flow in rows:
        run = runs.get(item.id)
        items[item.id] = {
            "title": item.title,
            "project": name,
            "id": str(item.id),
            "url": f"/console/workspace/{name}?tab=issues&record={item.id}",
            "github_url": item.issue_url,
            "issue_state": item.issue_state or item.workspace_review,
            "flow": flow,
            "run_id": str(run.id) if run else None,
            "run_state": run.state if run else None,
            "outcome": run.outcome if run else None,
        }
    return [items[key] for key in keys if key in items]


class ProcessLearning:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def summary(self, name: str, *, page: int = 1) -> dict[str, Any]:
        if type(page) is not int or page < 1:
            raise ProjectInvalid("Choose a positive page of process observations.")
        with self.sessions() as db:
            project = active_project(db, name)
            receipts = _receipts(db, project.id, "run") + _receipts(db, project.id, "review")
            groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            successes: dict[tuple[str, str], dict[str, Any]] = {}
            totals = Counter((row["source"], row["flow"]) for row in receipts)
            for row in receipts:
                if row["outcome"] == "succeeded":
                    successes.setdefault((row["source"], row["flow"]), row)
                if row["signal"] != "completed":
                    groups[row["pattern"]].append(row)
            signals = [
                {
                    **group[0],
                    "occurrences": len(group),
                    "samples": totals[group[0]["source"], group[0]["flow"]],
                    "examples": [r["id"] for r in group[:3]],
                    "latest_success": successes.get((group[0]["source"], group[0]["flow"])),
                }
                for group in groups.values()
            ]
            signals.sort(key=lambda s: (s["occurrences"], s["at"]), reverse=True)
            controller = controller_project(db)
            return {
                "project": name,
                "window_days": WINDOW_DAYS,
                "limit_per_source": SAMPLE_SIZE,
                "slow_minutes": SLOW_MINUTES,
                "sampled": len(receipts),
                "pattern_count": len(signals),
                "page": page,
                "pages": (len(signals) + PATTERNS_PER_PAGE - 1) // PATTERNS_PER_PAGE,
                "patterns": signals[(page - 1) * PATTERNS_PER_PAGE : page * PATTERNS_PER_PAGE],
                "approaches": guidance(db, [project.id]),
                "improvements": improvements(db, project.id),
                "controller_project": controller.name if controller else None,
                "controller_repository": get_settings().controller_repository,
            }

    def turn_guidance(self, run_data: dict[str, Any]) -> list[dict[str, Any]]:
        names = (run_data.get("scope") or {}).get("projects") or [run_data.get("thread")]
        with self.sessions() as db:
            if run_data.get("incidents"):
                ids = db.scalars(
                    select(SupervisorIncident.project_id).where(
                        SupervisorIncident.id.in_([UUID(i) for i in run_data["incidents"]]),
                        SupervisorIncident.project_id.is_not(None),
                    )
                )
            elif any(names):
                ids = db.scalars(select(ProjectRecord.id).where(ProjectRecord.name.in_(names)))
            else:
                return []
            return guidance(db, [pid for pid in ids if pid is not None])

    def approach(self, args: dict[str, Any]) -> dict[str, Any]:
        with self.sessions() as db:
            project = active_project(db, str(args.get("project")))
            receipt = evidence(db, project, args)
            return {
                "project": project.name,
                **receipt,
                "approach": required_text(args, "approach", 600),
            }

    def issue(self, args: dict[str, Any], *, create: bool = False) -> dict[str, Any]:
        with self.sessions.begin() as db:
            source = active_project(db, str(args.get("project")))
            receipt = evidence(db, source, args)
            destination = args.get("target")
            if destination not in ("project", "engineering_control"):
                raise ProjectInvalid("Choose the source project or Engineering Control.")
            target = source if destination == "project" else controller_project(db)
            settings = get_settings()
            repo = target.source_repository if target else settings.controller_repository
            target_name = target.name if target else "engineering-control"
            branch = target.default_branch if target else settings.controller_branch
            category = args.get("category")
            if category not in ("bug", "enhancement"):
                raise ProjectInvalid("Choose bug or enhancement.")
            title = clean_title(required_text(args, "title", 300))
            body = delivery_brief(args)
            link = (
                f"/console/run?id={receipt['id']}"
                if receipt["source"] == "run"
                else f"/console/workspace/{source.name}/supervisor?review={receipt['id']}"
            )
            address = (settings.console_public_url or "") + link
            body += (
                f"\n\n## Execution evidence\n{source.name} · {receipt['flow']} · "
                f"{receipt['outcome']} · {receipt['seconds']} seconds\n{address}"
            )
            result = dict(
                project=target_name,
                repository=repo,
                branch=branch,
                register=target is None,
                title=title,
                body=body,
                project_id=str(target.id) if target else None,
            )
            if not create:
                return result
            # An approved proposal is bound to the exact destination that the administrator saw.
            if [target_name, repo, branch, str(target.id) if target else None] != args.get(
                "_destination"
            ):
                raise ProjectConflict("The delivery project changed. Review a fresh proposal.")
            if not target:
                target = ProjectRecords.register(
                    db,
                    name=target_name,
                    display_name="Engineering Control",
                    repository=repo,
                    default_branch=branch,
                )
            component = " ".join(args["component"].lower().split())
            digest = hashlib.sha256(component.encode()).hexdigest()[:16]
            origin = f"supervisor-learning:{source.id}:{receipt['pattern']}:{digest}"
            return draft_issue(
                db,
                target,
                title=title,
                body=body,
                origin=origin,
                category=category,
                facts={"process_evidence": receipt, "source_project": source.name},
                new_workspace=bool(result["register"]),
            )
