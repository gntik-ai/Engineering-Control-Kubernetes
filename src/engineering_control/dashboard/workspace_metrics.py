"""Daily project resolutions, measured tokens, estimated costs and finalized charges."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    active_project,
)
from engineering_control.dashboard.workspace_costs import RELEASE_RATES, AccountPrices, DailyUsage
from engineering_control.db.models import (
    BacklogItem,
    ProjectIssueStory,
    SandboxRun,
    SandboxStep,
    WorkspaceBilledDay,
    WorkspaceImport,
    WorkspaceModelUsage,
)

MICROS = 1_000_000


def _utc_day(at: datetime) -> date:
    return at.astimezone(UTC).date() if at.tzinfo else at.date()


class BilledDay(BaseModel):
    """A normalized project-day total from a finalized billing feed."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    project: str = Field(min_length=1, max_length=32)
    day: date
    currency: Literal["EUR"]
    status: Literal["finalized"]
    basis: Literal["project_day_total"]
    amount: Decimal = Field(max_digits=17, decimal_places=6, allow_inf_nan=False)
    source: str = Field(min_length=1, max_length=100)
    reference: str = Field(min_length=1, max_length=200)
    revision: int = Field(ge=1, le=2_147_483_647, strict=True)


class WorkspaceMetrics:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions
        self.exchange_rates = RELEASE_RATES.copy()

    def load_finalized(self, rows: Iterable[BilledDay]) -> dict[str, int]:
        changed = 0
        # Lock project rows in consistent order so concurrent overlapping exports can replay.
        records = sorted(rows, key=lambda r: (r.project, r.day, r.revision))
        try:
            with self.sessions.begin() as session:
                projects = {
                    name: active_project(session, name, lock=True).id
                    for name in sorted({r.project for r in records})
                }
                for row in records:
                    micros = int(row.amount * MICROS)
                    project = projects[row.project]
                    saved = session.get(WorkspaceBilledDay, (project, row.day))
                    if saved is None:
                        session.add(
                            WorkspaceBilledDay(
                                project_id=project,
                                day=row.day,
                                source=row.source,
                                reference=row.reference,
                                revision=row.revision,
                                euro_micros=micros,
                            )
                        )
                        session.flush()
                        changed += 1
                        continue
                    if (saved.source, saved.reference) != (row.source, row.reference):
                        raise ProjectConflict(
                            "This project-day already has a different billing source reference"
                        )
                    if row.revision == saved.revision and micros != saved.euro_micros:
                        raise ProjectConflict(
                            "Two different amounts have the same billing revision"
                        )
                    if row.revision > saved.revision:
                        saved.revision = row.revision
                        saved.euro_micros = micros
                        changed += 1
        except IntegrityError as error:
            raise ProjectConflict(
                "A billing reference is already attributed to another project-day"
            ) from error
        return {"records": len(records), "changed": changed}

    def daily(self, name: str, days: int, now: datetime | None = None) -> dict[str, Any]:
        if days not in (7, 30):
            raise ProjectInvalid("Choose 7 or 30 days")
        today = (now or datetime.now(UTC)).astimezone(UTC).date()
        first = today - timedelta(days=days - 1)
        since = datetime.combine(first, time.min, UTC)
        until = datetime.combine(today + timedelta(days=1), time.min, UTC)
        resolved: Counter[date] = Counter()
        usage_by_day: defaultdict[date, DailyUsage] = defaultdict(DailyUsage)
        rates = self.exchange_rates
        with self.sessions() as session:
            project = active_project(session, name)
            dates = session.execute(
                select(BacklogItem.github_closed_at, BacklogItem.facts).where(
                    BacklogItem.project_id == project.id,
                    BacklogItem.workspace_review == "approved",
                    BacklogItem.issue_state == "closed",
                    BacklogItem.github_closed_at >= since,
                    BacklogItem.github_closed_at < until,
                    BacklogItem.id.in_(
                        select(ProjectIssueStory.item_id).where(
                            ProjectIssueStory.project_id == project.id
                        )
                    ),
                )
            )
            for at, facts in dates:
                if at and (facts or {}).get("github_state_reason") != "not_planned":
                    resolved[_utc_day(at)] += 1
            ended = func.coalesce(SandboxStep.finished_at, SandboxStep.updated_at)
            prices = AccountPrices(session)
            for usage, at, account, provider, model in session.execute(
                select(
                    SandboxStep.usage,
                    ended,
                    SandboxStep.account_id,
                    SandboxStep.provider,
                    SandboxStep.input["model"]["model"].as_string(),
                )
                .join(SandboxRun, SandboxRun.id == SandboxStep.run_id)
                .where(
                    SandboxRun.project_id == project.id,
                    SandboxStep.usage.is_not(None),
                    ended >= since,
                    ended < until,
                )
            ):
                if isinstance(usage, dict) and at:
                    usage_by_day[_utc_day(at)].add(
                        usage,
                        prices,
                        provider=provider or "",
                        model=model or "",
                        account=str(account) if account else None,
                    )
            for usage, at in session.execute(
                select(WorkspaceModelUsage.usage, WorkspaceModelUsage.recorded_at).where(
                    WorkspaceModelUsage.project_id == project.id,
                    WorkspaceModelUsage.recorded_at >= since,
                    WorkspaceModelUsage.recorded_at < until,
                )
            ):
                usage_by_day[_utc_day(at)].add(usage, prices)
            # Older releases retained one token total per UTC day, before per-call metering.
            scan = session.get(WorkspaceImport, project.id)
            if scan:
                legacy = scan.progress.get(
                    "legacy_price_model", {"model": scan.model, "provider": scan.provider}
                )
                for stamp, measured in scan.progress.get("usage_daily", {}).items():
                    day = date.fromisoformat(stamp)
                    if first <= day <= today:
                        usage_by_day[day].add(
                            {"tokens": measured},
                            prices,
                            provider=legacy.get("provider") or "",
                            model=legacy.get("model") or "",
                        )
            billed = {
                b.day: b
                for b in session.scalars(
                    select(WorkspaceBilledDay).where(
                        WorkspaceBilledDay.project_id == project.id,
                        WorkspaceBilledDay.day >= first,
                        WorkspaceBilledDay.day <= today,
                    )
                )
            }
            series = []
            for offset in range(days):
                day = first + timedelta(days=offset)
                bill = billed.get(day)
                usage = usage_by_day[day]
                rate_day = max((d for d in rates if d <= day), default=min(rates))
                rate = rates[rate_day]
                series.append(
                    {
                        "day": day.isoformat(),
                        "resolved": resolved[day],
                        "tokens": usage.tokens,
                        "estimated_eur": usage.euros(rate),
                        "unpriced_tokens": usage.unpriced,
                        "assumed_mix_tokens": usage.assumed,
                        "usd_per_eur": str(rate),
                        "exchange_rate_date": rate_day.isoformat(),
                        "billed_eur": format(Decimal(bill.euro_micros) / MICROS, "f")
                        if bill
                        else None,
                        "billing_reference": bill.reference if bill else None,
                        "billing_source": bill.source if bill else None,
                    }
                )
            return {"days": days, "timezone": "UTC", "series": series}
