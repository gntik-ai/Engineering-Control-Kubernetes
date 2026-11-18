import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from engineering_control.cli import main
from engineering_control.dashboard.project_records import ProjectConflict
from engineering_control.dashboard.workspace_metrics import BilledDay
from engineering_control.db.models import BacklogItem, ProjectRecord, SandboxRun, SandboxStep
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
