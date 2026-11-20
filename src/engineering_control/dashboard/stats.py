"""Read-only UTC activity reporting across sandbox runs and metered review workers."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.workspace_costs import AccountPrices, DailyUsage
from engineering_control.db.models import (
    BacklogItem,
    EpicExecutionPlan,
    PlanningReview,
    ProjectConnection,
    ProjectRecord,
    ProviderAccount,
    ReviewEvidenceJob,
    ReviewFlowRun,
    SandboxRun,
    SandboxStep,
    WorkspaceImport,
    WorkspaceModelUsage,
)
from engineering_control.engine.builtin import BUILTIN, FLOW_OF_KIND
from engineering_control.engine.reviews import default_key
from engineering_control.models.providers import BY_KEY
from engineering_control.sandbox.pods import BENCH_PREFIX

Row = dict[str, Any]
HISTORICAL = {
    "automatic_review": "automatic",
    "automatic_review_screen": "automatic",
    "automatic_review_linking": "automatic",
    "connection_review": "connections",
    "project_coverage": "coverage",
    "execution_planning": "execution",
}


def account_kind(provider: str) -> str:
    if provider in BY_KEY:
        return "API"
    return "Subscription" if provider in ("claude", "chatgpt") else "Unknown"


def utc(at: datetime) -> datetime:
    return at.astimezone(UTC) if at.tzinfo else at.replace(tzinfo=UTC)


@dataclass
class Counts:
    tokens: int = 0
    cached: int = 0
    unpriced: int = 0
    assumed: int = 0
    eur: Decimal = Decimal(0)
    calls: int = 0
    seconds: float = 0
    sandboxes: int = 0
    started: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    resolved: int = 0

    def merge(self, other: Counts) -> None:
        for field in vars(self):
            setattr(self, field, getattr(self, field) + getattr(other, field))

    def json(self) -> Row:
        amount = (
            None
            if self.unpriced and self.tokens == self.unpriced and not self.eur
            else round(float(self.eur), 6)
        )
        return {**vars(self), "eur": amount, "minutes": round(self.seconds / 60, 2)}


class Series:
    def __init__(self, **identity: Any) -> None:
        self.identity = identity
        self.days: dict[str, Counts] = defaultdict(Counts)
        self.total = Counts()

    def add(self, day: str, counts: Counts) -> None:
        self.days[day].merge(counts)
        self.total.merge(counts)

    def json(self) -> Row:
        return {
            **self.identity,
            **self.total.json(),
            "daily": {d: c.json() for d, c in self.days.items()},
        }


def sandbox_flow(kind: str, flow: Row | None) -> tuple[str, str, int | None]:
    flow = flow or {}
    key = str(flow.get("key") or FLOW_OF_KIND.get(kind) or kind)
    name = str(flow.get("name") or (BUILTIN[key].name if key in BUILTIN else key))
    return key, name, flow.get("version", 0 if key in BUILTIN else None)


def legacy_flow(usage: Row) -> tuple[str, str, None]:
    action = HISTORICAL.get(usage.get("workflow", ""))
    if action:
        key = default_key(action)
        return key, BUILTIN[key].name, None
    return "unattributed", "Unattributed historical reviews", None


def report(
    sessions: sessionmaker[Session],
    days: int,
    rates: dict[date, Decimal],
    *,
    now: datetime | None = None,
) -> Row:
    today = utc(now or datetime.now(UTC)).date()
    first = today - timedelta(days=days - 1)
    since, until = [datetime.combine(d, time.min, UTC) for d in (first, today + timedelta(days=1))]
    labels = [(first + timedelta(days=i)).isoformat() for i in range(days)]
    fx = {
        d: rates[max((r for r in rates if r.isoformat() <= d), default=min(rates))] for d in labels
    }
    accounts: dict[str, Series] = {}
    flows: dict[tuple[str, int | None, bool], Series] = {}
    overall = Series()
    with sessions() as db:
        prices = AccountPrices(db)
        projects = {
            str(pid): Series(id=str(pid), key=key, name=name)
            for pid, key, name in db.execute(
                select(ProjectRecord.id, ProjectRecord.name, ProjectRecord.display_name)
            )
        }
        for aid, name, provider, archived in db.execute(
            select(
                ProviderAccount.id,
                ProviderAccount.name,
                ProviderAccount.provider,
                ProviderAccount.archived_at,
            )
        ):
            accounts[str(aid)] = Series(
                id=str(aid),
                name=name,
                provider=provider,
                kind=account_kind(provider),
                archived=bool(archived),
            )

        def add(
            day: str,
            pid: UUID | None,
            flow: tuple[str, str, int | None],
            evaluation: bool,
            counts: Counts,
        ) -> None:
            key, name, version = flow
            fid = (key, version, evaluation)
            if fid not in flows:
                flows[fid] = Series(
                    id=f"{key}/{version}/{evaluation}",
                    key=key,
                    name=name,
                    version=version,
                    evaluation=evaluation,
                )
            flows[fid].add(day, counts)
            project = str(pid or "unattributed")
            if project not in projects:
                projects[project] = Series(id=project, key="", name="Unattributed project")
            projects[project].add(day, counts)
            overall.add(day, counts)

        def metered(
            day: str, usage: Row, account: str, provider: str, model: str, calls: int
        ) -> Counts:
            cost = DailyUsage()
            cost.add(usage, prices, provider=provider, model=model, account=account or None)
            counts = Counts(
                tokens=cost.tokens,
                cached=int(usage.get("cached") or 0),
                eur=cost.usd / fx[day],
                unpriced=cost.unpriced,
                assumed=cost.assumed,
                calls=calls,
            )
            aid = account or "unattributed/" + provider
            if aid not in accounts:
                accounts[aid] = Series(
                    id=aid,
                    name="Unattributed account",
                    provider=provider,
                    kind=account_kind(provider),
                    archived=False,
                )
            accounts[aid].add(day, counts)
            return counts

        run = SandboxRun
        step = SandboxStep
        flow_data = run.data["flow"].label("flow")
        evaluation = run.data["evaluation"].as_string().label("evaluation")
        ended = func.coalesce(step.finished_at, step.updated_at)
        steps = (
            select(
                run.project_id,
                run.kind,
                flow_data,
                evaluation,
                ended,
                step.usage,
                step.provider,
                step.account_id,
                step.input["model"]["model"].as_string(),
                step.pod_name,
                step.started_at,
                step.finished_at,
            )
            .join(step, step.run_id == run.id)
            .where(ended >= since, ended < until)
        )
        for (
            pid,
            kind,
            flow,
            ev,
            at,
            usage,
            provider,
            account,
            model,
            pod,
            started,
            finished,
        ) in db.execute(steps.execution_options(yield_per=500)):
            day = utc(at).date().isoformat()
            usage = usage or {}
            direct = bool(started and not pod and usage.get("model"))
            counts = Counts()
            for component in (usage.get("components") or [usage]) if usage else []:
                counts.merge(
                    metered(
                        day,
                        component,
                        str(component.get("account_id") or account or ""),
                        component.get("provider") or provider or "",
                        model or "",
                        component.get("responses", 1) if direct else 0,
                    )
                )
            if started and not direct and not (pod or "").startswith(BENCH_PREFIX):
                counts.sandboxes = 1
                counts.seconds = (
                    max(0, (utc(finished) - utc(started)).total_seconds()) if finished else 0
                )
            if usage or counts.sandboxes:
                add(day, pid, sandbox_flow(kind, flow), bool(ev), counts)

        review = ReviewFlowRun
        ledger = WorkspaceModelUsage
        usages = (
            select(
                ledger.project_id,
                ledger.recorded_at,
                ledger.usage,
                review.flow_key,
                review.flow_name,
                review.version,
            )
            .outerjoin(review, review.id == ledger.review_run_id)
            .where(ledger.recorded_at >= since, ledger.recorded_at < until)
        )
        for pid, at, usage, key, name, version in db.execute(
            usages.execution_options(yield_per=500)
        ):
            day = utc(at).date().isoformat()
            add(
                day,
                pid,
                (key, name, version) if key else legacy_flow(usage),
                False,
                metered(
                    day,
                    usage,
                    usage.get("account_id") or "",
                    usage.get("provider") or "",
                    "",
                    usage.get("responses", 1),
                ),
            )

        for pid, history, tariff, provider, model in db.execute(
            select(
                WorkspaceImport.project_id,
                WorkspaceImport.progress["usage_daily"],
                WorkspaceImport.progress["legacy_price_model"],
                WorkspaceImport.provider,
                WorkspaceImport.model,
            )
        ):
            for day, tokens in (history or {}).items():
                if day in fx and tokens:
                    add(
                        day,
                        pid,
                        legacy_flow({}),
                        False,
                        metered(
                            day,
                            {"tokens": tokens},
                            "",
                            (tariff or {}).get("provider") or provider or "",
                            (tariff or {}).get("model") or model or "",
                            0,
                        ),
                    )

        def lifecycle(
            pid: UUID | None,
            identity: tuple[str, str, int | None],
            ev: bool,
            start: datetime | None,
            end: datetime | None,
            state: str | None,
        ) -> None:
            for at, event in ((start, "started"), (end, state)):
                if (
                    at
                    and event in ("started", "succeeded", "failed", "cancelled")
                    and since <= utc(at) < until
                ):
                    counts = Counts()
                    setattr(counts, event, 1)
                    add(utc(at).date().isoformat(), pid, identity, ev, counts)

        for pid, kind, flow, ev, start, end, state in db.execute(
            select(
                run.project_id,
                run.kind,
                flow_data,
                evaluation,
                run.started_at,
                run.finished_at,
                run.state,
            )
            .where(or_(run.started_at >= since, run.finished_at >= since))
            .execution_options(yield_per=500)
        ):
            lifecycle(pid, sandbox_flow(kind, flow), bool(ev), start, end, state)
        for pid, key, name, version, start, end, state in db.execute(
            select(
                review.project_id,
                review.flow_key,
                review.flow_name,
                review.version,
                review.started_at,
                review.finished_at,
                review.outcome,
            )
            .where(or_(review.started_at >= since, review.finished_at >= since))
            .execution_options(yield_per=500)
        ):
            lifecycle(pid, (key, name, version), False, start, end, state)
        for at in db.scalars(
            select(BacklogItem.done_at)
            .join(ProjectRecord, ProjectRecord.id == BacklogItem.project_id)
            .where(
                ProjectRecord.archived_at.is_(None),
                BacklogItem.state == "done",
                BacklogItem.done_at >= since,
                BacklogItem.done_at < until,
            )
        ):
            if at:
                overall.add(utc(at).date().isoformat(), Counts(resolved=1))

    def rows(groups: Any) -> list[Row]:
        return [
            s.json()
            for s in sorted(
                groups, key=lambda s: (-s.total.tokens, s.identity["name"], s.identity["id"])
            )
        ]

    return {
        "days": labels,
        "timezone": "UTC",
        "overall": overall.json(),
        "accounts": rows(accounts.values()),
        "flows": rows(flows.values()),
        "projects": rows(projects.values()),
    }


def live(sessions: sessionmaker[Session], *, now: datetime | None = None) -> Row:
    stamp = utc(now or datetime.now(UTC))
    rows: list[Row] = []
    with sessions() as db:
        run = SandboxRun
        for key, title, kind, flow, start, project in db.execute(
            select(
                run.id, run.title, run.kind, run.data["flow"], run.started_at, ProjectRecord.name
            )
            .outerjoin(ProjectRecord, ProjectRecord.id == run.project_id)
            .where(
                run.state == "running",
                select(SandboxStep.id)
                .where(SandboxStep.run_id == run.id, SandboxStep.state == "running")
                .exists(),
            )
        ):
            _, name, version = sandbox_flow(kind, flow)
            rows.append(
                {
                    "id": str(key),
                    "title": title,
                    "flow": name,
                    "version": version,
                    "project": project,
                    "kind": "sandbox",
                    "started_at": utc(start).isoformat() if start else None,
                }
            )
        active = (
            select(WorkspaceImport.review_run_id)
            .where(WorkspaceImport.lease.is_not(None), WorkspaceImport.lease_until > stamp)
            .union_all(
                select(ReviewEvidenceJob.run_id).where(
                    ReviewEvidenceJob.state.in_(("calling", "stopping"))
                ),
                select(PlanningReview.review_run_id).where(
                    PlanningReview.lease.is_not(None), PlanningReview.due_at > stamp
                ),
                select(ProjectConnection.review_run_id).where(
                    ProjectConnection.archived_at.is_(None),
                    ProjectConnection.status == "running",
                    ProjectConnection.lease.is_not(None),
                    ProjectConnection.due_at > stamp,
                ),
                select(EpicExecutionPlan.review_run_id).where(
                    EpicExecutionPlan.lease.is_not(None), EpicExecutionPlan.due_at > stamp
                ),
            )
        )
        for review, project in db.execute(
            select(ReviewFlowRun, ProjectRecord.name)
            .join(ProjectRecord, ProjectRecord.id == ReviewFlowRun.project_id)
            .where(ReviewFlowRun.finished_at.is_(None), ReviewFlowRun.id.in_(active))
        ):
            rows.append(
                {
                    "id": str(review.id),
                    "title": review.flow_name,
                    "flow": review.flow_key,
                    "version": review.version,
                    "project": project,
                    "kind": "review",
                    "started_at": utc(review.started_at).isoformat(),
                }
            )
    rows.sort(key=lambda r: (r["started_at"] or "", r["id"]))
    return {"count": len(rows), "items": rows, "as_of": stamp.isoformat()}
