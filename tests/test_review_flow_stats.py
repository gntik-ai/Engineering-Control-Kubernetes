"""Project scope, pinned execution, and global reporting through the console entry points."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from engineering_control.dashboard import review_flows
from engineering_control.db.models import (
    ProjectRecord,
    ProviderAccount,
    ReviewFlowRun,
    SandboxRun,
    SandboxStep,
    WorkspaceImport,
    WorkspaceModelUsage,
)
from engineering_control.engine.builtin import BUILTIN
from engineering_control.models.service import Question, Reply
from tests.test_automatic_planning import configure, empty, post
from tests.test_dashboard_web import _authenticated_client


def test_published_project_review_is_executed_and_libraries_page_independently(
    tmp_path: Path,
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        raw = BUILTIN["review-automatic"].as_dict()
        raw.update(key="evidence-review", name="Evidence review", start="wording")
        raw["tasks"].insert(
            0,
            {
                "id": "wording",
                "task": "review.guidance",
                "with": {"stage": "epics", "instruction": "Cite a release criterion."},
                "next": {"ok": "review"},
            },
        )
        raw["tasks"][1]["with"] = {"instruction": "Use repository evidence.", "max_output": 700}
        local = repo.flows.create(raw, project="falcone")["flow"]
        assert repo.flows.publish(local)["errors"] == 0
        library = "/api/dashboard/flows"
        assert client.get("/console/settings/stats").status_code == 200
        first = client.get(library, params={"page": 1}).json()
        second = client.get(library, params={"page": 2}).json()
        assert first["total"] > 10 and len(first["items"]) == 10
        assert not {r["key"] for r in first["items"]} & {r["key"] for r in second["items"]}
        by_tasks = [
            item
            for page in (1, 2)
            for item in client.get(
                library, params={"page": page, "sort": "tasks", "direction": "desc"}
            ).json()["items"]
        ]
        task_counts = [r["engine"]["tasks"] for r in by_tasks if r["engine"]]
        assert task_counts == sorted(task_counts, reverse=True) and task_counts[0] > 1
        assert len({r["key"] for r in by_tasks}) == len(by_tasks)
        filtered = client.get(
            library, params={"page": 100, "search": "evidence review", "scope_project": "falcone"}
        ).json()
        assert filtered["page"] == 1 and [r["key"] for r in filtered["items"]] == [local]
        assert not client.get(library, params={"page": 1, "search": "evidence review"}).json()[
            "items"
        ]
        choices = client.get(
            library, params={"scope_project": "falcone", "include_shared": "true"}
        ).json()["items"]
        assert {local, "review-automatic", "review-coverage", "deliver-issue"} <= {
            r["key"] for r in choices
        }
        assert post(client, "review-flows/coverage", flow=local).status_code == 422
        assert post(client, "review-flows/automatic", flow=local, version=1).status_code == 200
        with repo.sessions() as db:
            other = db.scalar(select(ProjectRecord).where(ProjectRecord.name != "falcone"))
            assert (
                other
                and review_flows.resolve(db, other.id, "automatic")[0].key == "review-automatic"
            )
        configure(repo, client)
        repo.planning.save("falcone", "epics", "Reporting", "Make delivery progress visible.", [])
        questions = []

        class Recorder:
            async def ask(self, question: Question, **kwargs: Any) -> Reply:
                questions.append(question)
                return Reply(
                    empty(), "", "fixture", "chosen", uuid4(), "Test", {"tokens": 25}, 0.01, 1
                )

        assert asyncio.run(repo.planning_analysis.tick(Recorder()))
        assert len(questions) == 1 and questions[0].max_tokens == 700
        assert questions[0].system.endswith("Cite a release criterion.\nUse repository evidence.")
        with repo.sessions() as db:
            execution = db.scalars(select(ReviewFlowRun)).one()
            usage = db.scalars(select(WorkspaceModelUsage)).one()
            assert (
                execution.flow_key,
                execution.version,
                execution.outcome,
                usage.review_run_id,
            ) == (local, 1, "succeeded", execution.id)
        assert post(client, "review-flows/automatic", flow=None).status_code == 200
        assert any(
            r["action"] == "automatic" and r["inherited"]
            for r in client.get("/api/dashboard/projects/falcone/review-flows").json()["items"]
        )
    finally:
        client.__exit__(None, None, None)


def test_resume_keeps_flow_and_metrics_reconcile_logical_runs_and_legacy(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    now = datetime.now(UTC)
    try:
        pid = repo.projects.identity_of("falcone")
        with repo.sessions.begin() as db:
            api = ProviderAccount(
                name="Calls",
                provider="openai",
                state="ready",
                prices={"priced": {"input": 2, "output": 2}},
            )
            sub = ProviderAccount(name="Claude seat", provider="claude", state="ready")
            scan = WorkspaceImport(
                project_id=pid,
                stage="issues",
                repository="gntik-ai/falcone",
                branch="main",
                progress={"usage_daily": {now.date().isoformat(): 10}},
            )
            db.add_all([api, sub, scan])
            db.flush()
            original = review_flows.start(db, scan, pid, "coverage")
            api_id, sub_id = str(api.id), sub.id
            scan.lease, scan.lease_until = uuid4(), now + timedelta(minutes=2)
        assert client.get("/api/dashboard/stats/live").json()["count"] == 1
        raw = BUILTIN["review-coverage"].as_dict()
        raw["tasks"][0]["with"] = {"instruction": "Newly published only."}
        repo.flows.save_draft("review-coverage", raw)
        repo.flows.publish("review-coverage")
        with repo.sessions.begin() as db:
            scan = db.get(WorkspaceImport, pid)
            assert scan
            assert original == review_flows.start(db, scan, pid, "coverage", resume=True)
            assert not review_flows.matching_revision(db, pid, "coverage", original)
            assert review_flows.matching_revision(db, pid, "coverage") == "Newly published only."
            scan.lease = scan.lease_until = None
            for _ in range(100):
                db.add(
                    WorkspaceModelUsage(
                        project_id=pid,
                        review_run_id=original,
                        usage={
                            "account_id": api_id,
                            "provider": "openai",
                            "requested_model": "priced",
                            "input": 6000,
                            "output": 4000,
                            "cached": 250,
                        },
                    )
                )
            review_flows.finish(db, scan, "succeeded")
            run = SandboxRun(
                project_id=pid,
                kind="deliver_issue",
                title="Running step",
                state="running",
                started_at=now,
                data={
                    "flow": {"key": "deliver-issue", "version": 2},
                    "evaluation": {"id": "fixture"},
                },
            )
            db.add_all(
                [
                    run,
                    SandboxRun(
                        project_id=pid, kind="deliver_issue", title="Waiting", state="waiting"
                    ),
                ]
            )
            db.flush()
            db.add(
                SandboxStep(
                    run_id=run.id,
                    name="editor",
                    state="running",
                    position=1,
                    started_at=now,
                    account_id=sub_id,
                    provider="claude",
                    pod_name="editor",
                    usage={"tokens": 75},
                )
            )
        live = client.get("/api/dashboard/stats/live").json()
        assert live["count"] == 1 and live["items"][0]["title"] == "Running step"
        repo.workspace_metrics.exchange_rates = {now.date(): Decimal("2")}
        response = client.get("/api/dashboard/stats?days=7")
        assert response.status_code == 200, response.text
        report = response.json()
        assert len(report["days"]) == 7 and report["timezone"] == "UTC"
        coverage = next(f for f in report["flows"] if f["key"] == "review-coverage")
        assert (
            coverage["started"],
            coverage["succeeded"],
            coverage["calls"],
            coverage["tokens"],
            coverage["cached"],
            coverage["version"],
        ) == (1, 1, 100, 1_000_000, 25_000, 0)
        assert (
            coverage["eur"] == 1.025 and coverage["daily"][now.date().isoformat()]["calls"] == 100
        )
        assert report["overall"]["tokens"] == 1_000_085 and report["overall"]["unpriced"] == 85
        assert (
            sum(p["tokens"] for p in report["projects"])
            == sum(a["tokens"] for a in report["accounts"])
            == sum(f["tokens"] for f in report["flows"])
            == 1_000_085
        )
        assert any(f["evaluation"] and f["tokens"] == 75 for f in report["flows"])
        assert any(
            f["version"] is None and f["started"] == 0 and f["tokens"] == 10
            for f in report["flows"]
        )
        assert any(
            a["kind"] == "Subscription" and a["eur"] is None and a["tokens"] == 75
            for a in report["accounts"]
        )
        assert client.get("/api/dashboard/stats?days=2").status_code == 422
        with repo.sessions.begin() as db:
            scan = db.get(WorkspaceImport, pid)
            assert scan
            new = review_flows.start(db, scan, pid, "coverage", fresh=True)
            assert new != original and db.get(ReviewFlowRun, new).version == 1
    finally:
        client.__exit__(None, None, None)
