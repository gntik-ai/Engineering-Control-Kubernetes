"""Project review allowances govern provider dispatch across independent workers."""

import asyncio
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import select

from engineering_control.dashboard import review_budget
from engineering_control.dashboard.workspace_import import WorkspaceImports
from engineering_control.db.models import PlanningReview, ProjectConnection, WorkspaceModelUsage
from engineering_control.models.service import ModelError, Models, Question, Reply
from tests.test_dashboard_web import _authenticated_client
from tests.test_project_connections import F, write
from tests.test_project_coverage import CoverageModel, run_available, seed
from tests.test_workspace_progress import BASE, post

QUESTION = Question(
    choices=(("fixture", "analysis"),),
    system="Review.",
    prompt="Job",
    schema={"type": "object"},
    max_tokens=50,
)


def test_concurrent_workflows_share_admission_and_metering(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    pid = repo.projects.identity_of("falcone")
    peer = repo.projects.identity_of("llmwiki")
    reserve = review_budget.reservation_size(QUESTION)
    pending, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Provider:
        async def ask(self, question: Question, *, retry: bool) -> Reply:
            calls.append(retry)
            pending.set()
            await release.wait()
            return Reply(
                {}, "", "fixture", "analysis", uuid4(), "Test", {"input": 3, "output": 4}, 0, 1
            )

    model = cast(Models, Provider())

    async def ask(project, workflow):
        return await review_budget.ask_review(
            repo.sessions, project, model, QUESTION, workflow, retry=True
        )

    async def exercise() -> None:
        first = asyncio.create_task(ask(pid, "project_coverage"))
        await asyncio.wait_for(pending.wait(), 3)
        try:
            with pytest.raises(review_budget.ReviewBudgetHeld):
                await ask(pid, "connection_review")
            assert calls == [False]
        finally:
            release.set()
            await first
        with pytest.raises(review_budget.ReviewBudgetHeld):
            await ask(pid, "automatic_review_linking")
        await ask(peer, "connection_review")
        assert calls == [False, True]  # Unconfigured projects retain their former retry policy.

    try:
        assert post(client, "review-budget", expected_limit=None, token_limit=-1).status_code == 422
        forbidden = client.post(
            BASE + "review-budget",
            json={"token_limit": reserve, "expected_limit": None, "csrf_token": "x" * 40},
        )
        assert forbidden.status_code == 403
        assert (
            post(client, "review-budget", expected_limit=None, token_limit=reserve).status_code
            == 200
        )
        asyncio.run(exercise())
        data = client.get(BASE + "planning-reviews").json()["review_budget"]
        assert data == {"limit": reserve, "used": 7, "reserved": 0, "required": reserve}
        assert (
            post(client, "review-budget", expected_limit=None, token_limit=999).status_code == 409
        )
        assert (
            post(client, "review-budget", expected_limit=reserve, token_limit=reserve + 7).json()[
                "required"
            ]
            is None
        )
        with repo.sessions() as db:
            ledger = [
                (r.project_id, r.usage["workflow"]) for r in db.scalars(select(WorkspaceModelUsage))
            ]
            assert ledger == [(pid, "project_coverage"), (peer, "connection_review")]
    finally:
        client.__exit__(None, None, None)


def test_budget_holds_workers_until_authorized_and_preserves_manual_stops(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    ids = seed(repo)
    try:
        con = write(client, F, other="falcone-serverless", relation="depends_on").json()["id"]
        with repo.sessions.begin() as db:
            db.add(
                PlanningReview(
                    project_id=ids["project"],
                    kind="stories",
                    record_id=ids["Respond to failures"],
                    title="Respond",
                    scope="full",
                )
            )
        assert post(client, "review-budget", expected_limit=None, token_limit=2).status_code == 200
        assert post(client, "import/coverage", version=7, links_only=True).status_code == 200
        models = CoverageModel()
        imports = WorkspaceImports(repo.sessions)
        asyncio.run(run_available(imports, models))
        assert not models.comparisons
        held = imports.status("falcone")
        assert (
            held["budget_held"] and not held["busy"] and held["coverage_run"]["state"] == "running"
        )
        assert imports._due_coverage() is None
        assert repo.planning_analysis.claim() is None
        assert repo.connection_analysis.claim() is None
        assert client.get(F + "/" + con).json()["budget_held"]
        assert post(client, "planning-reviews/pause", paused=True).status_code == 200
        conn = client.get(F + "/" + con).json()
        assert (
            write(
                client, F + "/" + con + "/pause", control_id=conn["control_id"], paused=True
            ).status_code
            == 200
        )
        assert (
            post(client, "review-budget", expected_limit=2, token_limit=200_000).json()["required"]
            is None
        )
        assert repo.planning_analysis.claim() is None and repo.connection_analysis.claim() is None
        asyncio.run(run_available(WorkspaceImports(repo.sessions), models))
        assert (
            models.comparisons and imports.status("falcone")["coverage_run"]["state"] == "completed"
        )
        with repo.sessions() as db:
            assert review_budget.status(db, ids["project"])["used"] == len(models.comparisons) * 12
            assert db.scalar(select(ProjectConnection.status)) == "paused"
    finally:
        client.__exit__(None, None, None)


def test_ambiguous_provider_failure_keeps_capacity_reserved(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    pid = repo.projects.identity_of("falcone")
    size = review_budget.reservation_size(QUESTION)

    class Broken:
        def __init__(self, reason: str):
            self.reason = reason

        async def ask(self, q: Question, *, retry: bool):
            raise ModelError(self.reason, "No confirmed usage")

    try:
        review_budget.authorize(repo.sessions, "falcone", None, size)
        for reason, expected in (("no_account", 0), ("no_answer", size)):
            with pytest.raises(ModelError):
                asyncio.run(
                    review_budget.ask_review(
                        repo.sessions,
                        pid,
                        cast(Models, Broken(reason)),
                        QUESTION,
                        "automatic_review",
                    )
                )
            with repo.sessions() as db:
                assert review_budget.status(db, pid)["reserved"] == expected
        with pytest.raises(review_budget.ReviewBudgetHeld):
            review_budget.reserve(repo.sessions, pid, size)
    finally:
        client.__exit__(None, None, None)


def test_metered_invalid_reply_settles_the_reservation(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    pid = repo.projects.identity_of("falcone")

    class Invalid:
        async def ask(self, q: Question, *, retry: bool):
            raise ModelError(
                "no_answer",
                "Invalid output",
                metered=Reply({}, "", "fixture", "analysis", uuid4(), "Test", {"tokens": 13}, 0, 1),
            )

    try:
        limit = review_budget.reservation_size(QUESTION)
        review_budget.authorize(repo.sessions, "falcone", None, limit)
        with pytest.raises(ModelError):
            asyncio.run(
                review_budget.ask_review(
                    repo.sessions, pid, cast(Models, Invalid()), QUESTION, "connection_review"
                )
            )
        with repo.sessions() as db:
            assert review_budget.status(db, pid) == {
                "limit": limit,
                "required": None,
                "used": 13,
                "reserved": 0,
            }
            assert db.scalar(select(WorkspaceModelUsage.usage))["tokens"] == 13
    finally:
        client.__exit__(None, None, None)
