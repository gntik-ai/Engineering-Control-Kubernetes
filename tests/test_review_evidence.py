"""Project preview and isolated metered flow runs go through the normal console entry points."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select

from engineering_control.dashboard import review_evidence
from engineering_control.dashboard.issue_coverage import Coverage, IssueCoverage, inventory
from engineering_control.db.models import (
    BacklogItem,
    ProjectConnection,
    ProjectRecord,
    ProjectReviewBudget,
    ProjectUserStory,
    ReviewEvidenceJob,
    ReviewFlowRun,
    ReviewWikiPage,
    WorkspaceCandidate,
    WorkspaceModelUsage,
)
from engineering_control.engine import reviews
from engineering_control.engine.builtin import BUILTIN
from engineering_control.engine.document import parse
from engineering_control.engine.validate import errors
from engineering_control.models.service import ModelError, Question, Reply
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_project_coverage import seed


def test_designer_preview_is_scoped_validated_and_free(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        seed(repo)
        doc = BUILTIN["review-evidence"].as_dict()
        assert not errors(parse(doc))
        response = post(
            client, "evidence/preview", document=doc, provider="fixture", model="analysis"
        )
        assert response.status_code == 200, response.text
        view = response.json()
        assert view["model_calls"] == 0 and view["eligible"] == 13 and view["selected"] > 0
        assert [s["id"] for s in view["steps"]] == ["sources", "rank", "reuse", "pages", "compare"]
        assert all(p["issue"]["number"] != 999 for p in view["items"])
        locked = post(client, "evidence/preview", document=doc, search="Locked issue").json()
        assert locked["total"] == 1 and locked["items"][0]["reason"] == "manual parent"
        r = locked["items"][0]["story"]
        assert r["body"] and r["card"]["revision"] == r["revision"]
        doc["start"] = "rank"
        assert post(client, "evidence/preview", document=doc).status_code == 422
        with repo.sessions() as db:
            assert db.scalar(select(func.count()).select_from(WorkspaceModelUsage)) == 0
            assert db.scalar(select(func.count()).select_from(ReviewEvidenceJob)) == 0
    finally:
        client.close()


def test_paid_arms_pin_checkpoint_stop_and_meter_once(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        first = uuid4()
        with repo.sessions.begin() as db:
            db.add(
                ProjectReviewBudget(
                    project_id=data["project"], token_limit=30000, used=0, reserved=0
                )
            )
            proposals = db.scalar(select(func.count()).select_from(WorkspaceCandidate))

        def launch() -> Any:
            return post(
                client,
                "evidence/runs",
                request_id=str(first),
                provider="fixture",
                model="analysis",
                token_cap=12000,
            )

        assert launch().status_code == 200
        assert launch().json()["id"] == str(first)
        for _ in range(4):
            assert review_evidence.advance(repo.sessions, first) is None
        call = review_evidence.advance(repo.sessions, first)
        assert (
            call and call.arm == "original" and call.question.choices == (("fixture", "analysis"),)
        )
        assert review_evidence.advance(repo.sessions, first) is None
        assert (
            post(client, "evidence/control", run_id=str(first), action="stop").json()["state"]
            == "stopping"
        )
        reply = Reply(
            {"scope": "partial", "evidence": "One acceptance criterion."},
            "",
            "fixture",
            "analysis",
            uuid4(),
            "Fixture",
            {"input": 12, "output": 3, "tokens": 15},
            0.1,
            1,
        )
        review_evidence.settle(repo.sessions, call, reply)
        review_evidence.settle(repo.sessions, call, reply)
        assert review_evidence.status(repo.sessions, "falcone")["run"]["state"] == "paused"
        assert (
            post(client, "evidence/control", run_id=str(first), action="resume").status_code == 200
        )

        class Model:
            async def ask(self, q: Question, *, retry: bool) -> Reply:
                assert not retry and q.prompt != call.question.prompt
                return reply

        assert asyncio.run(review_evidence.tick(repo.sessions, Model()))  # type: ignore[arg-type]
        review_evidence.advance(repo.sessions, first)
        saved = review_evidence.status(repo.sessions, "falcone")["run"]
        assert saved["state"] == "complete" and saved["spent"] == 30
        assert [a["arm"] for a in saved["arms"]] == ["original", "wiki"]
        warm = uuid4()
        review_evidence.start(repo.sessions, "falcone", warm, "fixture", "analysis", 12000)
        for _ in range(6):
            assert review_evidence.advance(repo.sessions, warm) is None
        repeat = review_evidence.status(repo.sessions, "falcone")["run"]
        assert (
            repeat["state"] == "complete"
            and repeat["cached_from"] == str(first)
            and repeat["spent"] == 0
        )
        wiki = next(step for step in repeat["steps"] if step["id"] == "pages")
        assert wiki["reused_pages"] == wiki["pages"] > 0
        with repo.sessions.begin() as db:
            story = db.get_one(ProjectUserStory, data["Audit executions"])
            story.description += " Also cite the source timestamps."
            before = db.scalar(select(func.count()).select_from(ReviewWikiPage))
        view = review_evidence.preview(repo.sessions, "falcone", None, "fixture", "analysis", 1, "")
        wiki = next(step for step in view["steps"] if step["id"] == "pages")
        assert wiki["reused_pages"] == wiki["pages"] - 1
        with repo.sessions() as db:
            assert db.scalar(select(func.count()).select_from(ReviewWikiPage)) == before
        with repo.sessions() as db:
            run = db.get_one(ReviewFlowRun, first)
            assert run.settings["document"]["start"] == "sources" and run.outcome == "succeeded"
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(WorkspaceModelUsage)
                    .where(WorkspaceModelUsage.review_run_id == first)
                )
                == 2
            )
            budget = db.get_one(ProjectReviewBudget, data["project"])
            assert (budget.used, budget.reserved) == (30, 0)
            assert db.scalar(select(func.count()).select_from(WorkspaceCandidate)) == proposals
    finally:
        client.close()


def test_connected_repository_references_and_archived_scope(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        with repo.sessions.begin() as db:
            home = db.get_one(ProjectRecord, data["project"])
            other = db.scalar(select(ProjectRecord).where(ProjectRecord.id != home.id))
            assert other
            story = ProjectUserStory(
                project_id=other.id, title="Remote timing", description="Respect elapsed times."
            )
            db.add(story)
            db.flush()
            db.add(
                WorkspaceCandidate(
                    project_id=other.id,
                    identity="remote",
                    kind="stories",
                    number=377,
                    title=story.title,
                    decision="approved",
                    local_id=story.id,
                )
            )
            item = db.scalar(
                select(BacklogItem).where(
                    BacklogItem.project_id == home.id, BacklogItem.issue_number == 41
                )
            )
            assert item
            item.description = f"Story: {other.source_repository}#377"
            left, right = sorted([home.id, other.id])
            conn = ProjectConnection(first_id=left, second_id=right, owner_id=home.id)
            db.add(conn)
            db.flush()
            cid = conn.id
        doc = BUILTIN["review-evidence"].as_dict()
        view = review_evidence.preview(
            repo.sessions, "falcone", doc, "fixture", "analysis", 1, "Remote timing"
        )
        assert any(
            r["issue"]["number"] == 41 and r["reason"] == "declared parent" for r in view["items"]
        )
        run = uuid4()
        review_evidence.start(repo.sessions, "falcone", run, "fixture", "analysis", 12000)
        for _ in range(4):
            review_evidence.advance(repo.sessions, run)
        with repo.sessions.begin() as db:
            db.get_one(ProjectConnection, cid).archived_at = datetime.now(UTC)
        assert review_evidence.advance(repo.sessions, run) is None
        assert review_evidence.status(repo.sessions, "falcone")["run"]["state"] == "failed"
        assert (
            review_evidence.preview(
                repo.sessions, "falcone", doc, "fixture", "analysis", 1, "Remote timing"
            )["total"]
            == 0
        )
    finally:
        client.close()


def test_pinned_graph_orders_regular_matching_and_checks_remaining_pairs(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        raw = BUILTIN["review-coverage"].as_dict()
        raw["start"] = "graph"
        raw["tasks"] = [
            {
                "id": "graph",
                "task": reviews.GRAPH,
                "with": {"connected": False},
                "next": {"ok": "rank"},
            },
            {"id": "rank", "task": reviews.RANK, "with": {"limit": 1}, "next": {"ok": "review"}},
            *raw["tasks"],
        ]
        doc = parse(raw)
        assert not errors(doc)
        run_id = uuid4()
        with repo.sessions.begin() as db:
            db.add(
                ReviewFlowRun(
                    id=run_id,
                    project_id=data["project"],
                    action="coverage",
                    flow_key=doc.key,
                    flow_name=doc.name,
                    version=2,
                    settings=reviews.Policy.from_document(doc).settings(),
                )
            )
        priority = review_evidence.matching_priority(
            repo.sessions, data["project"], run_id, "fixture", "analysis"
        )
        assert priority
        with repo.sessions() as db:
            assert (
                db.get_one(ReviewFlowRun, run_id).evidence_trace["steps"][1]["task"] == reviews.RANK
            )
        targets = {
            str(data[k])
            for k in ("Audit executions", "Respond to failures", "Retired delivery history")
        }
        link = IssueCoverage(repo.sessions)

        def guard(_db: Any) -> None:
            pass

        for _ in range(20):
            page = link.page(
                "falcone", targets, "fixture", "analysis", guard, True, priority=priority
            )
            if isinstance(page, Coverage):
                break
            link.persist("falcone", "fixture", "analysis", page, [], guard)
        else:
            raise AssertionError("Matching did not complete")
        with repo.sessions() as db:
            inv = inventory(
                db, db.get_one(ProjectRecord, data["project"]), targets, "fixture", "analysis"
            )
            cache = inv.checkpoints(db, data["project"])
            assert all(set(inv.known(s, cache[s])) == inv.eligible(s) for s in targets)
    finally:
        client.close()


def test_project_hold_and_interrupted_provider_do_not_retry(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        run = uuid4()
        with repo.sessions.begin() as db:
            db.add(
                ProjectReviewBudget(project_id=data["project"], token_limit=1, used=0, reserved=0)
            )
        review_evidence.start(repo.sessions, "falcone", run, "fixture", "analysis", 12000)
        for _ in range(5):
            assert review_evidence.advance(repo.sessions, run) is None
        with repo.sessions.begin() as db:
            j = db.get_one(ReviewEvidenceJob, run)
            assert j.state == "held" and not j.reserved
            b = db.get_one(ProjectReviewBudget, data["project"])
            assert b.required is None
            b.token_limit = 30000
        review_evidence.control(repo.sessions, "falcone", run, "resume")
        assert review_evidence.advance(repo.sessions, run)
        with repo.sessions.begin() as db:
            db.get_one(ReviewEvidenceJob, run).claimed_at = datetime.now(UTC) - timedelta(hours=1)
        assert review_evidence.next_job(repo.sessions) is None
        with repo.sessions() as db:
            j = db.get_one(ReviewEvidenceJob, run)
            assert j.state == "uncertain" and j.reserved > 0
            assert db.get_one(ProjectReviewBudget, data["project"]).reserved == j.reserved
        assert post(client, "evidence/control", run_id=str(run), action="resume").status_code == 409
    finally:
        client.close()


def test_metered_stop_settles_once_and_keeps_explicit_failure(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        run = uuid4()
        with repo.sessions.begin() as db:
            db.add(
                ProjectReviewBudget(
                    project_id=data["project"], token_limit=30000, used=0, reserved=0
                )
            )
        review_evidence.start(repo.sessions, "falcone", run, "fixture", "analysis", 12000)
        for _ in range(4):
            review_evidence.advance(repo.sessions, run)
        receipt = Reply(
            {},
            "",
            "fixture",
            "analysis",
            uuid4(),
            "Fixture",
            {"tokens": 30, "input": 25, "output": 5, "provider_ref": "req_example"},
            0.1,
            1,
        )

        class Cut:
            async def ask(self, q: Question, *, retry: bool) -> Reply:
                assert not retry
                raise ModelError("cut", "Output limit reached.", metered=receipt)

        assert asyncio.run(review_evidence.tick(repo.sessions, Cut()))  # type: ignore[arg-type]
        assert not asyncio.run(review_evidence.tick(repo.sessions, Cut()))  # type: ignore[arg-type]
        value = review_evidence.status(repo.sessions, "falcone")["run"]
        assert value["state"] == "failed" and value["spent"] == 30 and value["reserved"] == 0
        assert value["failed_request"] == {
            "arm": "original",
            "reason": "cut",
            "provider_ref": "req_example",
        }
        assert value["arms"][0]["scope"] is None and value["arms"][0]["tokens"] == 30
        with repo.sessions() as db:
            b = db.get_one(ProjectReviewBudget, data["project"])
            assert (b.used, b.reserved) == (30, 0)
            assert db.scalar(select(func.count()).select_from(WorkspaceModelUsage)) == 1
    finally:
        client.close()


def test_admin_can_correct_a_saved_unrelated_comparison_without_another_call(
    tmp_path: Path,
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        seed(repo)
        run = uuid4()
        review_evidence.start(repo.sessions, "falcone", run, "fixture", "analysis", 12000)
        for _ in range(4):
            review_evidence.advance(repo.sessions, run)
        reply = Reply(
            {"scope": "unrelated", "evidence": "No shared criterion."},
            "",
            "fixture",
            "analysis",
            uuid4(),
            "Fixture",
            {"input": 10, "output": 2, "tokens": 12},
            0.1,
            1,
        )
        for arm in ("original", "wiki"):
            call = review_evidence.advance(repo.sessions, run)
            assert call and call.arm == arm
            review_evidence.settle(repo.sessions, call, reply)
        review_evidence.advance(repo.sessions, run)
        cached = uuid4()
        review_evidence.start(repo.sessions, "falcone", cached, "fixture", "analysis", 12000)
        for _ in range(6):
            assert review_evidence.advance(repo.sessions, cached) is None
        url = "/api/dashboard/projects/falcone/evidence/reviews"
        one = f"evidence/reviews/{run}"
        assert client.get(url).json()["total"] == 1
        detail = client.get(f"{url}/{run}").json()
        assert detail["issue"]["body"] and detail["story"]["card"]["text"]
        assert client.get(f"{url}/{cached}").status_code == 404
        assert (
            client.get(f"/api/dashboard/projects/other/evidence/reviews/{run}").status_code == 404
        )
        args = {"scope": "partial", "note": "One criterion is shared.", "expected_revision": None}
        result = post(client, one, **args)
        assert result.status_code == 200, result.text
        assert post(client, one, **args).json() == result.json()
        assert post(client, one, scope="full", expected_revision=None).status_code == 409
        assert client.get(url).json()["total"] == 0
        history = client.get(url + "?state=reviewed").json()
        assert history["total"] == 1 and history["items"][0]["decision"] == "partial"
        assert [a["scope"] for a in client.get(f"{url}/{run}").json()["arms"]] == ["unrelated"] * 2
        assert (
            post(client, one, scope="full", expected_revision=result.json()["revision"]).status_code
            == 200
        )
        with repo.sessions() as db:
            assert db.scalar(select(func.count()).select_from(WorkspaceModelUsage)) == 2
    finally:
        client.close()
