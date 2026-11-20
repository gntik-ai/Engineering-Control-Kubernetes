"""Estimate project usage using metered USD costs and account-specific model tariffs."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import case, select, update
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.db.models import (
    ProjectReviewBudget,
    ProviderAccount,
    WorkspaceImport,
    WorkspaceModelUsage,
)
from engineering_control.models.providers import BY_KEY, cost_of
from engineering_control.models.service import Reply

ECB_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
# Published ECB observations shipped for cold starts without network access.
# Dates and USD per EUR are also returned with each estimate.
RELEASE_RATES = {
    date(2026, 10, 19): Decimal("1.1225"),
    date(2026, 11, 18): Decimal("1.0968"),
}


async def ecb_rates(client: httpx.AsyncClient) -> dict[date, Decimal]:
    response = await client.get(ECB_URL, params={"lastNObservations": 45, "format": "csvdata"})
    response.raise_for_status()
    today = datetime.now(UTC).date()
    rates = {}
    for row in csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))):
        if row["CURRENCY"] != "USD" or row["CURRENCY_DENOM"] != "EUR" or not row["OBS_VALUE"]:
            continue
        day, rate = date.fromisoformat(row["TIME_PERIOD"]), Decimal(row["OBS_VALUE"])
        if day <= today and rate.is_finite() and rate > 0:
            rates[day] = rate
    if not rates:
        raise ValueError("ECB returned no USD per EUR observations")
    return rates


def record_model_usage(sessions: sessionmaker[Session], row: WorkspaceImport, reply: Reply) -> None:
    record_project_usage(sessions, row.project_id, row.model, reply, "repository_review")


def record_project_usage(
    sessions: sessionmaker[Session],
    project: UUID,
    model: str | None,
    reply: Reply,
    workflow: str = "project_planning",
    reserved: int = 0,
    run_id: UUID | None = None,
) -> None:
    # Save the metered response independently of a later proposal validation or stage retry.
    with sessions.begin() as session:
        add_project_usage(session, project, model, reply, workflow, reserved, run_id)


def add_project_usage(
    session: Session,
    project: UUID,
    model: str | None,
    reply: Reply,
    workflow: str,
    reserved: int,
    run_id: UUID | None,
) -> None:
    tokens = reply.usage.get("tokens")
    if tokens is None and all(
        type(reply.usage.get(k)) is int and reply.usage[k] >= 0 for k in ("input", "output")
    ):
        tokens = reply.usage["input"] + reply.usage["output"]
    if reserved and type(tokens) is int and tokens >= 0:
        budget = ProjectReviewBudget
        session.execute(
            update(budget)
            .where(budget.project_id == project)
            .values(
                used=budget.used + tokens,
                reserved=budget.reserved - reserved,
                required=case(
                    (
                        (budget.required.is_(None)) & (budget.used + tokens >= budget.token_limit),
                        1,
                    ),
                    else_=budget.required,
                ),
            )
        )
    session.add(
        WorkspaceModelUsage(
            project_id=project,
            review_run_id=run_id,
            usage={
                **reply.usage,
                "workflow": workflow,
                "provider": reply.provider,
                "account_id": str(reply.account_id),
                "requested_model": model,
                "model": reply.model,
            },
        )
    )


class AccountPrices:
    def __init__(self, session: Session) -> None:
        self.by_id: dict[str, dict[str, Any]] = {}
        self.by_provider: dict[str, list[dict[str, Any]]] = {}
        for key, provider, prices, archived in session.execute(
            select(
                ProviderAccount.id,
                ProviderAccount.provider,
                ProviderAccount.prices,
                ProviderAccount.archived_at,
            )
        ):
            self.by_id[str(key)] = prices or {}
            if archived is None:
                self.by_provider.setdefault(provider, []).append(prices or {})

    def tariff(self, account: str | None, provider: str, model: str) -> dict[str, Any] | None:
        if account:
            entry = self.by_id.get(account, {}).get(model)
            return entry if isinstance(entry, dict) else None
        # Older import totals name a provider and model, but do not identify the account.
        matches = [p[model] for p in self.by_provider.get(provider, []) if model in p]
        if matches and isinstance(matches[0], dict) and all(p == matches[0] for p in matches):
            return matches[0]
        return None


@dataclass
class DailyUsage:
    tokens: int = 0
    usd: Decimal = Decimal(0)
    unpriced: int = 0
    assumed: int = 0

    def add(
        self,
        usage: dict[str, Any],
        prices: AccountPrices,
        *,
        provider: str = "",
        model: str = "",
        account: str | None = None,
    ) -> None:
        measured = all(type(usage.get(k)) is int and usage[k] >= 0 for k in ("input", "output"))
        quantity = usage.get("tokens")
        if type(quantity) is not int or quantity < 0:
            quantity = usage["input"] + usage["output"] if measured else 0
        self.tokens += quantity
        missing = usage.get("unpriced_tokens", 0)
        missing = min(quantity, max(0, missing)) if type(missing) is int else 0
        for key in ("cost", "client_cost"):
            cost = usage.get(key)
            if type(cost) in (int, float):
                amount = Decimal(str(cost))
                if amount.is_finite() and amount >= 0:
                    self.usd += amount
                    self.unpriced += missing
                    return
        if missing or (not quantity and not usage.get("cached")):
            self.unpriced += quantity
            return

        provider = usage.get("provider") or provider
        model = usage.get("requested_model") or model or usage.get("model") or ""
        tariff = prices.tariff(usage.get("account_id") or account, provider, model)
        if not tariff:
            self.unpriced += quantity
            return
        used = usage
        if not measured:
            # Legacy totals do not separate input and output. Use an explicit equal split.
            used = {"input": (quantity + 1) // 2, "output": quantity // 2}
        engine = BY_KEY.get(provider)
        estimate = cost_of(
            {model: tariff},
            model,
            used,
            write=engine.cache_write[0] if engine else 1.0,
            part=engine.batch_price if engine and usage.get("batch") else 1.0,
        )
        if estimate is None:
            self.unpriced += quantity
            return
        self.usd += Decimal(str(estimate))
        if not measured:
            self.assumed += quantity

    def euros(self, usd_per_eur: Decimal) -> str | None:
        if self.tokens and self.tokens == self.unpriced and not self.usd:
            return None
        return format((self.usd / usd_per_eur).quantize(Decimal("0.000001")), "f")
