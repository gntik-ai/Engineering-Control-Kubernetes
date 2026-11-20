"""Full matching evidence, in-flight validation, legacy checkpoints and bounded batches."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from engineering_control.dashboard import (
    coverage_preview,
    issue_coverage,
    review_evidence,
    review_graph,
)
from engineering_control.dashboard.connection_records import digest
from engineering_control.dashboard.project_records import ProjectConflict
from engineering_control.db.models import (
    BacklogItem,
    ProjectRecord,
    ProjectUserStory,
    ReviewEvidenceJob,
    StoryIssueCoverage,
    WorkspaceImport,
)
from engineering_control.engine import reviews
from tests.test_dashboard_web import _authenticated_client
from tests.test_project_coverage import seed
from tests.test_workspace_progress import BASE


def long_body(criterion: str) -> str:
    return "Detailed opening.\n" * 175 + criterion + "\n" + "Detailed ending.\n" * 110


def guard(_db: Any) -> None:
    pass


@pytest.mark.parametrize("kind", ["issues", "stories"])
def test_middle_edit_reaches_reviewer_and_invalidates_in_flight_work(
    tmp_path: Path, kind: str
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ids = seed(repo)
        pid = ids["project"]
        targets = {
            str(ids[k])
            for k in ("Audit executions", "Respond to failures", "Retired delivery history")
        }
        matcher = issue_coverage.IssueCoverage(repo.sessions)
        story = str(ids["Audit executions"])
        with repo.sessions() as db:
            issue = db.scalar(
                select(BacklogItem.id).where(
                    BacklogItem.project_id == pid, BacklogItem.issue_number == 41
                )
            )

        def edit(criterion: str) -> None:
            with repo.sessions.begin() as db:
                row = (
                    db.get_one(BacklogItem, issue)
                    if kind == "issues"
                    else db.get_one(ProjectUserStory, UUID(story))
                )
                row.description = long_body(criterion)

        def page() -> issue_coverage.Page | issue_coverage.Coverage:
            return matcher.page("falcone", targets, "fixture", "analysis", guard, True)

        edit("First requirement")
        first = page()
        assert isinstance(first, issue_coverage.Page)
        matcher.persist(
            "falcone",
            "fixture",
            "analysis",
            first,
            [{"issue": str(issue), "story": story, "evidence": "First requirement", "full": True}],
            guard,
        )
        warm = page()
        assert isinstance(warm, issue_coverage.Coverage)
        with repo.sessions.begin() as db:
            project = db.get_one(ProjectRecord, pid)
            wizard = db.get_one(WorkspaceImport, pid)
            baseline, _ = coverage_preview.plan(db, project, wizard, "fixture", "analysis")
            wizard.progress = {"coverage_baseline": baseline}
        edit("Edited requirement")
        assert issue_coverage.excerpt(long_body("First requirement")) == issue_coverage.excerpt(
            long_body("Edited requirement")
        )
        preview = client.get(BASE + "import/coverage/preview").json()
        assert preview["changes"][kind]["changed"] == 1
        assert preview["remaining"] == (3 if kind == "issues" else 4)
        assert preview["remaining"] + preview["reusable"] == 13
        with repo.sessions() as db, pytest.raises(ProjectConflict):
            warm.validate(db, db.get_one(ProjectRecord, pid), generating=False)
        pending = page()
        assert isinstance(pending, issue_coverage.Page)
        field = "imported_issues" if kind == "issues" else "stories"
        assert [r["description"] for r in json.loads(pending.prompt())[field]] == [
            long_body("Edited requirement")
        ]
        edit("Second edit during the request")
        with pytest.raises(ProjectConflict):
            matcher.persist("falcone", "fixture", "analysis", pending, [], guard)
        assert (
            client.get(BASE + "import/coverage/preview").json()["remaining"] == preview["remaining"]
        )
        retry = page()
        assert isinstance(retry, issue_coverage.Page)
        assert json.loads(retry.prompt())[field][0]["description"] == long_body(
            "Second edit during the request"
        )
        matcher.persist("falcone", "fixture", "analysis", retry, [], guard)
        complete = page()
        assert isinstance(complete, issue_coverage.Coverage) and not complete.matches[story]
        assert client.get(BASE + "import/coverage/preview").json()["remaining"] == 0

        with repo.sessions() as db:
            source = review_graph.snapshot(
                db, db.get_one(ProjectRecord, pid), False, "fixture", "analysis", ""
            )
        story_key = review_graph.source_key(pid, "stories", UUID(story))
        issue_key = review_graph.source_key(pid, "issues", issue)
        step = {"task": reviews.REUSE, "version": 1, "params": {}}
        pair = {"issue": issue_key, "story": story_key, "decision": "unchecked"}
        assert review_graph.node_result(source, {"pairs": [pair]}, step) == {
            "reusable": 1,
            "unchecked": 0,
        }
        assert pair["decision"] == "no match"
        pilot = ReviewEvidenceJob(provider="fixture", model="analysis")

        def original_prompt() -> str:
            question = review_evidence.question(pilot, source, {}, pair, "original")
            return json.loads(question.prompt)["issue" if kind == "issues" else "story"]["text"]

        assert original_prompt() == long_body("Second edit during the request")
        # Preserve the evidence and admission size of a previously frozen pilot.
        del source["matching_evidence"]
        assert original_prompt() == issue_coverage.excerpt(
            long_body("Second edit during the request")
        )
        assert (
            review_graph.node_result(source, {"pairs": [{**pair, "decision": "unchecked"}]}, step)[
                "reusable"
            ]
            == 0
        )
    finally:
        client.close()


@pytest.mark.parametrize("positional", [True, False])
def test_mixed_legacy_cache_reuses_only_complete_evidence(tmp_path: Path, positional: bool) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ids = seed(repo)
        pid = ids["project"]
        audit = str(ids["Audit executions"])
        respond = str(ids["Respond to failures"])
        targets = {audit, respond}
        with repo.sessions.begin() as db:
            db.get_one(ProjectUserStory, UUID(respond)).description = long_body("private middle")
            item = db.scalars(
                select(BacklogItem).where(
                    BacklogItem.project_id == pid, BacklogItem.issue_number == 42
                )
            ).one()
            item.description = long_body("specific scope")
            db.flush()
            inv = issue_coverage.inventory(
                db, db.get_one(ProjectRecord, pid), targets, "fixture", "analysis"
            )
            old_issues = [
                {**r, "description": issue_coverage.excerpt(r["description"])} for r in inv.issues
            ]
            context = (db.get_one(ProjectRecord, pid).source_repository, "fixture", "analysis")
            common = digest([*context, old_issues])
            scope = digest([*context, issue_coverage.INSTRUCTION, issue_coverage.MATCHES])
            for sid, current in inv.stories.items():
                row = {**current, "description": issue_coverage.excerpt(current["description"])}
                db.add(
                    StoryIssueCoverage(
                        project_id=pid,
                        story_id=UUID(sid),
                        revision=digest([common, row]),
                        checked=len(old_issues),
                        story_revision=None if positional else digest([scope, row]),
                        issue_versions=None
                        if positional
                        else {
                            i["id"]: digest(i) for i in old_issues if i["id"] in inv.eligible(sid)
                        },
                        matches=[],
                    )
                )
            eligible = {(s, i) for s in targets for i in inv.eligible(s)}
            expected = {(s, i) for s, i in eligible if s == respond or i == str(item.id)}
        matcher = issue_coverage.IssueCoverage(repo.sessions)
        seen = set()
        for _ in range(8):
            nxt = matcher.page("falcone", targets, "fixture", "analysis", guard, True)
            if isinstance(nxt, issue_coverage.Coverage):
                break
            batch = {
                (s, i["id"])
                for s in nxt.story_versions
                for i in nxt.issues
                if (s, i["id"]) in eligible
            }
            assert batch and not batch & seen
            seen.update(batch)
            matcher.persist("falcone", "fixture", "analysis", nxt, [], guard)
        assert seen == expected and isinstance(nxt, issue_coverage.Coverage)
    finally:
        client.close()


def test_full_sources_fit_batch_ceiling_and_preserve_manual_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ids = seed(repo)
        pid = ids["project"]
        targets = {
            str(ids[k])
            for k in ("Audit executions", "Respond to failures", "Retired delivery history")
        }
        with repo.sessions.begin() as db:
            for item in db.scalars(select(BacklogItem).where(BacklogItem.project_id == pid)):
                item.description = "café\n" * (300 if item.issue_number == 44 else 12)
            db.flush()
            inv = issue_coverage.inventory(
                db, db.get_one(ProjectRecord, pid), targets, "fixture", "analysis"
            )
            expected = {(s, i) for s in targets for i in inv.eligible(s)}
            bodies = {i["id"]: i["description"] for i in inv.issues}
        monkeypatch.setattr(issue_coverage, "MATCH_PAGE_BYTES", 900)
        matcher = issue_coverage.IssueCoverage(repo.sessions)
        seen = set()
        for _ in range(len(expected) + 1):
            nxt = matcher.page("falcone", targets, "fixture", "analysis", guard, True)
            if isinstance(nxt, issue_coverage.Coverage):
                break
            prompt = nxt.prompt()
            if len(prompt.encode()) > 900:
                assert len(nxt.stories) == len(nxt.issues) == 1
            for r in json.loads(prompt)["imported_issues"]:
                assert r["description"] == bodies[r["id"]]
            pairs = {
                (s, i["id"])
                for s in nxt.story_versions
                for i in nxt.issues
                if (s, i["id"]) in expected
            }
            assert pairs and not pairs & seen
            seen.update(pairs)
            matcher.persist("falcone", "fixture", "analysis", nxt, [], guard)
        assert seen == expected and isinstance(nxt, issue_coverage.Coverage)
    finally:
        client.close()
