"""Operator controls revoke only the selected review and retain its committed work."""

import asyncio
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select, update

from engineering_control.dashboard.project_records import ProjectConflict
from engineering_control.dashboard.workspace_import import Update, WorkspaceImports
from engineering_control.dashboard.workspace_issues import import_issues
from engineering_control.db.models import (
    BacklogItem,
    PlanningReview,
    ProjectRecord,
    ProjectUserStory,
    StoryIssueCoverage,
)
from engineering_control.models.service import Question, Reply
from tests.test_dashboard_web import _authenticated_client
from tests.test_project_connections import F, write
from tests.test_project_coverage import CoverageModel, run_available, seed
from tests.test_workspace_progress import BASE, post
from tests.test_workspace_progress import seed as seed_pages
from tests.test_workspace_review import ReviewModel


def test_stop_visible_after_reload_and_resume_only_unfinished_pages(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    seed_pages(repo, 10)
    sent: list[list[int]] = []
    blocked = threading.Event()
    cancelled = threading.Event()

    class Slow(ReviewModel):
        async def ask(self, question: Question, *, retry: bool = True) -> Reply:
            result = await super().ask(question, retry=retry)
            sent.append([r["number"] for r in result.data["records"]])
            if len(sent) == 2:
                blocked.set()
                try:
                    await asyncio.sleep(25)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return result

    client.app.state.models = Slow()
    try:
        started = post(
            client,
            "import/advance",
            version=1,
            provider="fixture",
            model="analysis",
            background=True,
        ).json()
        assert blocked.wait(10)
        control = client.get(BASE + "import?progress_only=true").json()["control_id"]
        assert control == started["control_id"]
        stopped = post(client, "import/stop", control_id=control).json()
        assert not stopped["busy"] and stopped["operation"]["state"] == "paused"
        assert cancelled.wait(3)
        assert stopped["run_found"] == len(sent[0]) and not repo.workspace_imports._due_coverage()
        assert (
            post(client, "import/stop", control_id=control).json()["version"] == stopped["version"]
        )
        assert (
            post(client, "import/advance", version=stopped["version"], background=True).status_code
            == 409
        )
        assert post(client, "import/resume", control_id=control).status_code == 200
        assert post(client, "import/stop", control_id=control).status_code == 409
        for _ in range(60):
            state = client.get(BASE + "import").json()
            if not state["busy"]:
                break
            time.sleep(0.05)
        assert state["operation"]["state"] == "completed" and state["run_found"] == 10
        assert len(sent) == 3 and sent[1] == sent[2] and set(sent[0]).isdisjoint(sent[2])
    finally:
        client.__exit__(None, None, None)


def test_durable_coverage_stop_rejects_old_worker_and_other_project(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    seed(repo)
    try:
        imports = WorkspaceImports(repo.sessions)
        state = post(client, "import/coverage", version=7).json()
        claimed = imports._claim(
            "falcone",
            state["version"],
            "fixture",
            "analysis",
            None,
            background=True,
            automatic=True,
        )
        assert claimed
        stopped = post(client, "import/stop", control_id=state["control_id"]).json()
        assert stopped["coverage_run"]["state"] == "paused" and imports._due_coverage() is None
        with pytest.raises(ProjectConflict):
            imports._release("falcone", claimed.lease, Update("epics", claimed.progress))
        with pytest.raises(ProjectConflict):
            asyncio.run(imports.control("llmwiki", state["control_id"], None, resume=False))
        resumed = post(client, "import/resume", control_id=state["control_id"]).json()
        assert (
            resumed["coverage_run"]["state"] == "running"
            and resumed["control_id"] != state["control_id"]
        )
        assert imports._due_coverage()
    finally:
        client.__exit__(None, None, None)


def test_preview_and_link_only_remember_unchanged_positive_and_negative_pairs(
    tmp_path: Path,
) -> None:
    client, repo = _authenticated_client(tmp_path)
    ids = seed(repo)
    model = CoverageModel()
    imports = WorkspaceImports(repo.sessions)

    def preview() -> dict[str, Any]:
        reply = client.get(BASE + "import/coverage/preview")
        assert reply.status_code == 200
        return reply.json()

    def link() -> dict[str, Any]:
        state = imports.status("falcone")
        assert (
            post(client, "import/coverage", version=state["version"], links_only=True).status_code
            == 200
        )
        asyncio.run(run_available(imports, cast(Any, model)))
        return imports.status("falcone")

    try:
        assert preview()["remaining"] == 13 and preview()["reusable"] == 0
        assert preview()["excluded_by_manual_links"] == 2
        assert not model.comparisons and imports.status("falcone")["version"] == 7
        first = link()
        assert first["stage"] == "complete" and first["coverage_run"]["state"] == "completed"
        assert first["coverage_run"]["links"] and model.issue_drafts == 0
        assert preview()["reusable"] == 13 and preview()["remaining"] == 0
        # A compatible pre-upgrade positional checkpoint can be promoted without a model call.
        with repo.sessions.begin() as db:
            legacy = db.get(StoryIssueCoverage, (ids["project"], ids["Audit executions"]))
            legacy.story_revision, legacy.issue_versions = None, None
            legacy.checked = 3
        assert preview()["reusable"] == 12
        link()
        assert model.comparisons[-1] == [45]
        with repo.sessions.begin() as db:
            project = db.get(ProjectRecord, ids["project"])
            import_issues(
                db,
                project,
                [
                    {
                        "number": 55,
                        "title": "Unrelated issue",
                        "body": "New proposal.",
                        "labels": [],
                        "state": "open",
                    }
                ],
            )
        assert preview()["remaining"] == 3 and preview()["reusable"] == 13
        link()
        assert model.comparisons[-1] == [55]
        with repo.sessions.begin() as db:
            db.get(
                ProjectUserStory, ids["Respond to failures"]
            ).description = "Changed acceptance criteria"
            db.scalar(
                select(BacklogItem).where(
                    BacklogItem.project_id == ids["project"], BacklogItem.issue_number == 42
                )
            ).description = "Changed issue"
        assert preview()["remaining"] == 8 and preview()["reusable"] == 8
        link()
        calls = len(model.comparisons)
        link()
        assert len(model.comparisons) == calls and model.issue_drafts == 0
        assert preview()["remaining"] == 0
    finally:
        client.__exit__(None, None, None)


def test_stop_automatic_queue_and_individual_reviews_preserves_progress(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    ids = seed(repo)
    key = ids["Audit executions"]
    try:
        with repo.sessions.begin() as db:
            db.execute(
                update(PlanningReview)
                .where(PlanningReview.project_id == ids["project"])
                .values(status="covered")
            )
            db.add(
                PlanningReview(
                    project_id=ids["project"],
                    kind="stories",
                    record_id=key,
                    title="Audit",
                    scope="full",
                    progress={"scanned": 9},
                )
            )
        analysis = repo.planning_analysis
        assert analysis.claim()
        assert post(client, "planning-reviews/pause", paused=True).json() == {"paused": True}
        assert analysis.claim() is None
        status = client.get(BASE + "planning-reviews").json()
        item = next(j for j in status["items"] if j["id"] == str(key))
        assert status["paused"] and item["status"] == "queued" and item["scanned"] == 9
        action = f"planning-reviews/stories/{key}/pause"
        assert post(client, action, control_id=item["control_id"], paused=True).status_code == 200
        assert post(client, "planning-reviews/pause", paused=False).status_code == 200
        assert analysis.claim() is None
        item = next(
            j for j in client.get(BASE + "planning-reviews").json()["items"] if j["id"] == str(key)
        )
        assert item["status"] == "paused" and item["scanned"] == 9
        assert post(client, action, control_id=item["control_id"], paused=False).status_code == 200
        assert analysis.claim()
    finally:
        client.__exit__(None, None, None)


def test_connection_stop_excludes_wakeups_and_preserves_checkpoint(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        key = write(client, F, other="falcone-serverless", relation="depends_on").json()["id"]
        path = F + "/" + key
        c = client.get(path).json()
        assert (
            write(client, path + "/pause", paused=True, control_id=c["control_id"]).status_code
            == 200
        )
        assert repo.connection_analysis.claim() is None
        assert write(client, path + "/analyze").status_code == 409
        c = client.get(path).json()
        assert c["status"] == "paused"
        assert (
            write(client, path + "/pause", paused=False, control_id=c["control_id"]).status_code
            == 200
        )
        assert repo.connection_analysis.claim()
    finally:
        client.__exit__(None, None, None)


def test_scan_stop_between_batches_requires_explicit_resume(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        started = post(client, "import/start").json()
        imports = repo.workspace_imports
        first = imports._claim("falcone", started["version"], None, None, None)
        assert first
        imports._release("falcone", first.lease, Update("files", {**first.progress, "scanned": 8}))
        state = imports.status("falcone")
        assert not state["busy"] and state["scan_state"] == "running"
        paused = post(client, "import/stop", control_id=state["control_id"]).json()
        assert paused["scan_state"] == "paused" and paused["stage"] == "files"
        assert post(client, "import/advance", version=paused["version"]).status_code == 409
        resumed = post(client, "import/resume", control_id=state["control_id"]).json()
        assert resumed["stage"] == "files" and resumed["scan_state"] == "ready"
        next_page = imports._claim("falcone", resumed["version"], None, None, None)
        assert next_page and next_page.progress["scanned"] == 8
    finally:
        client.__exit__(None, None, None)
