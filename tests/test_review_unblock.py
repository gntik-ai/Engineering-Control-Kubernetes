"""A review run that is cut off does not stay in the way: what a provider refused is given
back at once, what stays unknown is released by the administrator, and one run that cannot
be continued does not keep the others waiting."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from engineering_control.dashboard import review_evidence
from engineering_control.db.models import ProjectReviewBudget, ReviewEvidenceJob, ReviewFlowRun
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_project_coverage import seed


def _calling(repo: Any, project: UUID) -> tuple[UUID, review_evidence.Call]:
    """A run of the project that has reserved its first paid call."""
    run = uuid4()
    with repo.sessions.begin() as db:
        if db.get(ProjectReviewBudget, project) is None:
            db.add(ProjectReviewBudget(project_id=project, token_limit=60000, used=0, reserved=0))
    review_evidence.start(repo.sessions, "falcone", run, "fixture", "analysis", 12000)
    for _ in range(8):
        call = review_evidence.advance(repo.sessions, run)
        if call:
            return run, call
    raise AssertionError("The run never reached a paid call")


def _held(repo: Any, project: UUID, run: UUID) -> tuple[str, int, int, str]:
    with repo.sessions() as db:
        job = db.get_one(ReviewEvidenceJob, run)
        budget = db.get_one(ProjectReviewBudget, project)
        return job.state, job.reserved, budget.reserved, job.result.get("detail") or ""


def test_the_administrator_releases_a_run_whose_usage_stays_unknown(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        run, call = _calling(repo, data["project"])
        assert call.project_reserved > 0
        # The call is cut off: nothing ever comes back for it.
        with repo.sessions.begin() as db:
            db.get_one(ReviewEvidenceJob, run).claimed_at = datetime.now(UTC) - timedelta(hours=1)
        assert review_evidence.next_job(repo.sessions) is None
        state, reserved, of_project, _detail = _held(repo, data["project"], run)
        assert state == "uncertain" and reserved == of_project == call.project_reserved

        # The page says which runs wait, and how much each holds.
        shown = client.get("/api/dashboard/projects/falcone/evidence").json()
        [waiting] = shown["unsettled"]
        assert waiting["id"] == str(run) and waiting["reserved"] == reserved
        assert "Reservation retained" in waiting["detail"]
        # It cannot be resumed, only released.
        assert post(client, "evidence/control", run_id=str(run), action="resume").status_code == 409
        released = post(client, "evidence/control", run_id=str(run), action="release")
        assert released.status_code == 200, released.text
        state, reserved, of_project, detail = _held(repo, data["project"], run)
        assert (state, reserved, of_project) == ("interrupted", 0, 0)
        assert f"released {call.project_reserved:,} reserved tokens" in detail
        with repo.sessions() as db:
            ended = db.get_one(ReviewFlowRun, run)
            assert ended.outcome == "interrupted" and ended.finished_at is not None
            assert db.get_one(ReviewEvidenceJob, run).lease is None
        assert client.get("/api/dashboard/projects/falcone/evidence").json()["unsettled"] == []
        # Once is enough, and a reply that still arrives is not counted twice.
        assert (
            post(client, "evidence/control", run_id=str(run), action="release").status_code == 409
        )
        review_evidence.settle(repo.sessions, call, None, "transport")
        assert _held(repo, data["project"], run)[:3] == ("interrupted", 0, 0)
        # The project can start another review at once.
        again, _call = _calling(repo, data["project"])
        assert again != run
    finally:
        client.close()


@pytest.mark.parametrize(
    ("error", "said"),
    [("refused", "did not accept the account's key"), ("invalid", "rejected the request itself")],
)
def test_what_a_provider_refused_before_any_work_is_given_back(
    tmp_path: Path, error: str, said: str
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        run, call = _calling(repo, data["project"])
        review_evidence.settle(repo.sessions, call, None, error)
        state, reserved, of_project, detail = _held(repo, data["project"], run)
        assert (state, reserved, of_project) == ("failed", 0, 0)
        assert said in detail and "Nothing was used" in detail
        shown = client.get("/api/dashboard/projects/falcone/evidence").json()
        assert shown["unsettled"] == [] and shown["run"]["failed_request"]["reason"] == error
    finally:
        client.close()


def test_a_call_that_may_have_been_served_still_waits_for_its_usage(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        # A provider that was busy, or did not answer, may have done the work.
        run, call = _calling(repo, data["project"])
        review_evidence.settle(repo.sessions, call, None, "busy")
        state, reserved, of_project, _detail = _held(repo, data["project"], run)
        assert state == "uncertain" and reserved == of_project == call.project_reserved
    finally:
        client.close()


def test_a_run_that_cannot_be_continued_does_not_keep_the_others_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        seed(repo)
        run = uuid4()
        review_evidence.start(repo.sessions, "falcone", run, "fixture", "analysis", 12000)
        monkeypatch.setattr(review_evidence, "_TROUBLE", {})

        def broken(_sessions: Any, _key: UUID) -> None:
            raise RuntimeError("the database went away")

        monkeypatch.setattr(review_evidence, "advance", broken)
        models: Any = None
        # A passing fault is tried again; the same fault every time ends the run.
        for _ in range(review_evidence.TRIES - 1):
            with pytest.raises(RuntimeError):
                asyncio.run(review_evidence.tick(repo.sessions, models))
            with repo.sessions() as db:
                assert db.get_one(ReviewEvidenceJob, run).state == "queued"
        assert asyncio.run(review_evidence.tick(repo.sessions, models)) is True
        with repo.sessions() as db:
            job = db.get_one(ReviewEvidenceJob, run)
            assert job.state == "failed"
            assert "could not be continued after several tries" in job.result["detail"]
        # Nothing is left in the queue, and the count starts again for the next run.
        assert asyncio.run(review_evidence.tick(repo.sessions, models)) is False
        assert review_evidence._TROUBLE == {}
    finally:
        client.close()


def test_the_polled_discovery_status_leaves_the_long_texts_out(tmp_path: Path) -> None:
    from engineering_control.db.models import WorkspaceCandidate, WorkspaceImport

    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        with repo.sessions.begin() as db:
            row = db.get(WorkspaceImport, data["project"])
            assert row is not None
            row.stage = "epics"
            db.add(
                WorkspaceCandidate(
                    project_id=data["project"],
                    kind="epics",
                    identity="gap:long",
                    title="A long proposal",
                    description="x" * 40000,
                    parents=[],
                )
            )
        whole = client.get("/api/dashboard/projects/falcone/import")
        short = client.get("/api/dashboard/projects/falcone/import", params={"brief": "true"})
        [full] = [
            c for c in whole.json()["review"]["candidates"] if c["title"] == "A long proposal"
        ]
        [brief] = [
            c for c in short.json()["review"]["candidates"] if c["title"] == "A long proposal"
        ]
        assert len(full["description"]) == 40000 and "description" not in brief
        # Everything the page counts by is still there.
        assert {k: v for k, v in full.items() if k != "description"} == brief
        assert len(short.content) < len(whole.content) - 39000
    finally:
        client.close()
