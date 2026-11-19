import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from engineering_control.cli import main
from engineering_control.dashboard.project_records import ProjectConflict
from engineering_control.dashboard.workspace_costs import ECB_URL, ecb_rates
from engineering_control.dashboard.workspace_metrics import BilledDay
from engineering_control.db.models import (
    BacklogItem,
    ProjectRecord,
    ProviderAccount,
    SandboxRun,
    SandboxStep,
    WorkspaceImport,
    WorkspaceModelUsage,
)
from engineering_control.models.batches import plus
from tests.test_dashboard_web import _authenticated_client


def test_finalized_eur_feed_project_attribution_and_missing_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        metrics = repo.workspace_metrics
        now = datetime(2026, 11, 18, 12, tzinfo=UTC)
        yesterday = now.date() - timedelta(days=1)
        day = {
            "project": "falcone",
            "day": yesterday.isoformat(),
            "currency": "EUR",
            "status": "finalized",
            "basis": "project_day_total",
            "amount": "2.125",
            "source": "test billing feed",
            "reference": "fixture-F-17",
            "revision": 1,
        }
        for bad in (
            {"currency": "USD"},
            {"status": "estimated"},
            {"basis": "model_price"},
            {"amount": "0.0000001"},
        ):
            with pytest.raises(ValidationError):
                BilledDay.model_validate({**day, **bad})
        data = tmp_path / "billing.jsonl"
        data.write_text(
            json.dumps(day)
            + "\n"
            + json.dumps(
                {**day, "day": now.date().isoformat(), "reference": "fixture-F-18", "amount": "0"}
            )
        )
        monkeypatch.setenv("ENGINEERING_DATABASE_URL", str(repo.engine.url))
        main(["billing", "import-finalized", "--file", str(data)])
        assert metrics.load_finalized([BilledDay.model_validate(day)])["changed"] == 0
        metrics.load_finalized(
            [BilledDay.model_validate({**day, "amount": "2.015", "revision": 2})]
        )
        with pytest.raises(ProjectConflict, match="reference"):
            metrics.load_finalized([BilledDay.model_validate({**day, "project": "llmwiki"})])
        with pytest.raises(ProjectConflict, match="same billing revision"):
            metrics.load_finalized(
                [BilledDay.model_validate({**day, "revision": 2, "amount": "60"})]
            )
        assert metrics.load_finalized([BilledDay.model_validate(day)])["changed"] == 0
        for project, tokens in (("falcone", 120), ("llmwiki", 800)):
            with repo.sessions.begin() as db:
                pid = db.scalar(select(ProjectRecord.id).where(ProjectRecord.name == project))
                run = SandboxRun(project_id=pid, kind="flow", title=project, state="succeeded")
                db.add(run)
                db.flush()
                db.add(
                    SandboxStep(
                        run_id=run.id,
                        position=0,
                        name="test",
                        state="succeeded",
                        finished_at=now - timedelta(days=1),
                        usage={"tokens": tokens, "cost": 999, "client_cost": 88},
                    )
                )
        epic = repo.planning.save("falcone", "epics", "Test", None, [])
        story = repo.planning.save("falcone", "stories", "Test", None, [UUID(epic["id"])])
        for state, age, reason in (
            ("approved", 1, "completed"),
            ("approved", 1, "not_planned"),
            ("pending", 1, "completed"),
            ("approved", 8, "completed"),
        ):
            i = repo.planning.save("falcone", "issues", "Changes", None, [UUID(story["id"])])
            with repo.sessions.begin() as db:
                record = db.get(BacklogItem, UUID(i["id"]))
                assert record is not None
                record.workspace_review = state
                record.issue_state = "closed"
                record.state = "done"
                record.done_at = now
                record.github_closed_at = now - timedelta(days=age)
                record.facts = {"github_state_reason": reason}
        seven = metrics.daily("falcone", 7, now)["series"]
        assert len(seven) == 7 and seven[-1]["billed_eur"] == "0"
        assert seven[-2]["billed_eur"] == "2.015" and seven[-2]["tokens"] == 120
        assert seven[-2]["resolved"] == 1 and sum(d["resolved"] for d in seven) == 1
        assert all(d["billed_eur"] is None for d in seven[:-2])
        assert seven[-2]["billing_reference"] == "fixture-F-17"
        assert sum(d["resolved"] for d in metrics.daily("falcone", 30, now)["series"]) == 2
        other = metrics.daily("llmwiki", 7, now)["series"]
        assert other[-2]["tokens"] == 800 and all(d["billed_eur"] is None for d in other)
        assert client.get("/api/dashboard/projects/falcone/statistics?days=8").status_code == 422
        assert client.get("/api/dashboard/projects/unknown/statistics").status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_project_estimates_include_historical_totals_snapshots_and_unpriced_tokens(
    tmp_path: Path,
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        now = datetime(2026, 11, 16, 12, tzinfo=UTC)
        weekend = now - timedelta(days=2)
        metrics = repo.workspace_metrics
        metrics.exchange_rates = {date(2026, 11, 13): Decimal("1.25"), now.date(): Decimal("1.6")}
        with repo.sessions.begin() as db:
            project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
            account = ProviderAccount(
                name="Test prices",
                provider="openai",
                state="ready",
                prices={
                    "tariff": {"input": 1.0, "output": 3.0, "cached": 0.5},
                    "free": {"input": 0, "output": 0},
                },
            )
            run = SandboxRun(project_id=project.id, kind="flow", title="Demo", state="succeeded")
            db.add_all([run, account])
            db.flush()
            db.add(
                WorkspaceImport(
                    project_id=project.id,
                    repository=project.source_repository,
                    branch="main",
                    stage="complete",
                    model="other-model",
                    provider="openai",
                    progress={
                        "usage_daily": {weekend.date().isoformat(): 1_000_000},
                        "legacy_price_model": {"provider": "openai", "model": "tariff"},
                    },
                )
            )
            db.add_all(
                [
                    SandboxStep(
                        run_id=run.id,
                        position=0,
                        name="saved",
                        state="succeeded",
                        finished_at=weekend,
                        usage={"cost": 1.25, "tokens": 10},
                    ),
                    SandboxStep(
                        run_id=run.id,
                        position=1,
                        name="missing",
                        state="succeeded",
                        finished_at=weekend,
                        usage={"tokens": 2500},
                    ),
                    SandboxStep(
                        run_id=run.id,
                        position=2,
                        name="free",
                        state="succeeded",
                        account_id=account.id,
                        finished_at=now,
                        input={"model": {"model": "free"}},
                        usage={"input": 10, "output": 10, "tokens": 20},
                    ),
                    WorkspaceModelUsage(
                        project_id=project.id,
                        recorded_at=weekend,
                        usage={
                            "tokens": 1_500_000,
                            "input": 1_000_000,
                            "output": 500_000,
                            "cached": 200_000,
                            "account_id": str(account.id),
                            "requested_model": "tariff",
                            "model": "versioned",
                        },
                    ),
                    WorkspaceModelUsage(
                        project_id=project.id,
                        recorded_at=weekend - timedelta(days=1),
                        usage={"tokens": 80, "model": "no price"},
                    ),
                ]
            )
        days = metrics.daily("falcone", 7, now)["series"]
        assert days[-3]["tokens"] == 2_502_510
        assert days[-3]["estimated_eur"] == "4.680000"
        assert days[-3]["unpriced_tokens"] == 2500 and days[-3]["assumed_mix_tokens"] == 1_000_000
        assert days[-3]["exchange_rate_date"] == "2026-11-13"
        assert days[-3]["billed_eur"] is None
        assert days[-4]["estimated_eur"] is None and days[-4]["unpriced_tokens"] == 80
        assert days[-1]["estimated_eur"] == days[-2]["estimated_eur"] == "0.000000"
        assert days[-1]["unpriced_tokens"] == 0
        assert all(r["tokens"] == 0 for r in metrics.daily("llmwiki", 30, now)["series"])
        with repo.sessions.begin() as db:
            db.add(
                ProviderAccount(
                    name="Different contract",
                    provider="openai",
                    state="ready",
                    prices={"tariff": {"input": 10, "output": 30}},
                )
            )
        ambiguous = metrics.daily("falcone", 7, now)["series"][-3]
        assert ambiguous["unpriced_tokens"] == 1_002_500 and ambiguous["assumed_mix_tokens"] == 0
        assert ambiguous["estimated_eur"] == "3.080000"
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_ecb_reference_and_mixed_step_coverage() -> None:
    def response(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(ECB_URL)
        return httpx.Response(
            200,
            text="CURRENCY,CURRENCY_DENOM,TIME_PERIOD,OBS_VALUE\nUSD,EUR,2025-05-23,1.1357\nJPY,EUR,2025-05-23,161\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        assert await ecb_rates(http) == {date(2025, 5, 23): Decimal("1.1357")}
    mixed = plus({"tokens": 20, "cost": 0}, {"tokens": 5})
    assert mixed is not None and mixed["cost"] == 0 and mixed["unpriced_tokens"] == 5
    mixed = plus(mixed, {"tokens": 2, "cost": 1})
    assert mixed is not None and mixed["tokens"] == 27 and mixed["unpriced_tokens"] == 5
