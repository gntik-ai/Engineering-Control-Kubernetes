"""Exercise full-text batching through the coverage Flow's pinned task settings."""

import asyncio
import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from engineering_control.dashboard import coverage_preview, issue_coverage
from engineering_control.dashboard.workspace_issues import import_issues
from engineering_control.db.models import (
    ProjectRecord,
    ProjectUserStory,
    ProviderAccount,
    ReviewFlowRun,
    WorkspaceImport,
)
from engineering_control.engine import reviews
from engineering_control.engine.document import parse
from engineering_control.engine.validate import errors
from engineering_control.models.service import Models, Question, Reply
from tests.test_dashboard_web import _authenticated_client
from tests.test_project_coverage import seed


@pytest.mark.parametrize("eight", [False, True])
def test_pinned_coverage_task_groups_the_same_complete_pairs(tmp_path: Path, eight: bool) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        pid = seed(repo)["project"]
        raw = reviews.defaults()["review-coverage"].as_dict()
        raw["tasks"][0]["with"] = {"max_output": 1200}
        if eight:
            raw["tasks"][0]["with"]["eight_story_batches"] = True
        doc = parse(raw)
        assert not errors(doc)
        settings = reviews.Policy.from_document(doc).settings()
        assert settings.get("eight_story_batches", False) is eight
        run_id = uuid4()
        stories = {str(uuid4()): f"Complete acceptance criterion {n}" for n in range(8)}
        with repo.sessions.begin() as db:
            account = db.scalars(
                select(ProviderAccount.id).where(ProviderAccount.provider == "fixture")
            ).one()
            db.add(
                ReviewFlowRun(
                    id=run_id,
                    project_id=pid,
                    action="coverage",
                    flow_key=doc.key,
                    flow_name=doc.name,
                    version=1,
                    settings=settings,
                )
            )
            for story, body in stories.items():
                db.add(
                    ProjectUserStory(id=UUID(story), project_id=pid, title=story, description=body)
                )
            import_issues(
                db,
                db.get_one(ProjectRecord, pid),
                [
                    {"number": n, "state": "open", "title": f"Test {n}", "body": f"Whole issue {n}"}
                    for n in range(50, 58)
                ],
            )
            db.flush()
            inv = issue_coverage.inventory(
                db, db.get_one(ProjectRecord, pid), set(stories), "fixture", "analysis"
            )
            issues = {
                r["number"]: r["description"] for r in inv.issues if r["manual_stories"] is None
            }
            assert len(issues) == 12
        requests = []
        seen: set[tuple[str, int]] = set()

        class Recorder:
            async def ask(self, q: Question, **_kwargs: Any) -> Reply:
                assert q.max_tokens == 1200
                payload = json.loads(q.prompt)
                described, imported = payload["stories"], payload["imported_issues"]
                assert all(r["description"] == stories[r["id"]] for r in described)
                assert all(r["description"] == issues[r["number"]] for r in imported)
                pairs = {(s["id"], i["number"]) for s in described for i in imported}
                assert len(pairs) <= 72 and not seen & pairs
                seen.update(pairs)
                requests.append((len(described), len(imported)))
                return Reply(
                    {"matches": []},
                    "",
                    "fixture",
                    "analysis",
                    account,
                    "Test",
                    {"input": 5, "output": 5, "tokens": 10},
                    0.1,
                    1,
                )

        def check() -> None:
            asyncio.run(
                issue_coverage.IssueCoverage(repo.sessions).check(
                    "falcone",
                    pid,
                    set(stories),
                    "fixture",
                    "analysis",
                    cast(Models, Recorder()),
                    lambda _db: None,
                    historical=True,
                    run_id=run_id,
                )
            )

        check()
        assert seen == {(s, n) for s in stories for n in issues}
        assert requests == ([(8, 9), (8, 3)] if eight else [(6, 12), (2, 12)])
        requests.clear()
        seen.clear()
        sid = min(stories)
        stories[sid] += " with an additional criterion"
        with repo.sessions.begin() as db:
            db.get_one(ProjectUserStory, UUID(sid)).description = stories[sid]
        check()
        assert requests == [(1, 12)] and seen == {(sid, n) for n in issues}
        requests.clear()
        with repo.sessions.begin() as db:
            project = db.get_one(ProjectRecord, pid)
            wizard = db.get_one(WorkspaceImport, pid)
            wizard.review_run_id = run_id
            before, _ = coverage_preview.snapshot(db, project, wizard, "fixture", "analysis")
            run_id = uuid4()
            db.add(
                ReviewFlowRun(
                    id=run_id,
                    project_id=pid,
                    action="coverage",
                    flow_key=doc.key,
                    flow_name=doc.name,
                    version=2,
                    settings={**settings, "eight_story_batches": not eight},
                )
            )
            wizard.review_run_id = run_id
            db.flush()
            after, _ = coverage_preview.snapshot(db, project, wizard, "fixture", "analysis")
            assert before == after
        check()
        assert requests == []
    finally:
        client.close()
