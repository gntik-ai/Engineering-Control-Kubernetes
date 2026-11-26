"""Project-wide and incremental planning reuse local GitHub issues before drafting work."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from engineering_control.dashboard import issue_coverage
from engineering_control.dashboard.project_records import ProjectInvalid
from engineering_control.dashboard.workspace_import import WorkspaceImports
from engineering_control.dashboard.workspace_issues import import_issues
from engineering_control.db.models import (
    BacklogItem,
    PlanningReview,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
    ProviderAccount,
    ReviewSettings,
    StoryIssueCoverage,
    WorkspaceAction,
    WorkspaceCandidate,
    WorkspaceImport,
)
from engineering_control.models.service import Models, Question, Reply
from tests.test_dashboard_web import _authenticated_client
from tests.test_workspace_progress import BASE, post


class CoverageModel:
    def __init__(self) -> None:
        self.comparisons: list[list[int]] = []
        self.issue_drafts = 0
        self.fail_once = False

    async def aclose(self) -> None:
        pass

    async def ask(self, q: Question, *, retry: bool = True) -> Reply:
        ctx = json.loads(q.prompt)
        assert q.choices == (("fixture", "analysis"),)
        result: dict[str, Any]
        if "imported_issues" in ctx:
            batch = ctx["imported_issues"]
            self.comparisons.append([i["number"] for i in batch])
            matches = []
            for issue in batch:
                for story in ctx["stories"]:
                    pairs = {
                        (41, "Audit executions"): True,
                        (42, "Respond to failures"): False,
                        (42, "See a crash"): True,
                        (43, "Retired delivery history"): True,
                        (44, "Audit executions"): True,  # Admin explicitly chose other parents.
                    }
                    pair = (issue["number"], story["title"])
                    if pair in pairs:
                        matches.append(
                            {
                                "issue": issue["id"],
                                "story": story["id"],
                                "evidence": issue["description"],
                                "full": pairs[pair],
                            }
                        )
            if self.fail_once and len(self.comparisons) == 2:
                self.fail_once = False
                matches.append(
                    {
                        "issue": str(uuid4()),
                        "story": next(
                            s["id"] for s in ctx["stories"] if s["title"] == "Respond to failures"
                        ),
                        "full": True,
                        "evidence": "Invalid outside this batch",
                    }
                )
            result = {"matches": matches}
        elif "source" in ctx:
            assert ctx["imported_issue_coverage"]
            s = ctx["source"]
            result = {
                "parents": s["parents"],
                "questions": [],
                "stories": [],
                "issues": [
                    {
                        "title": "Generate an audit trail",
                        "description": "Record every run.",
                        "reason": "Overlap",
                        "parents": [s["id"]],
                    }
                ],
            }
        elif "epics" in q.schema["properties"]:
            result = {
                "complete": True,
                "epics": []
                if "Reliability" in ctx["existing"]
                else [{"title": "Reliability", "description": "Expose failed runs."}],
            }
        elif "stories" in q.schema["properties"]:
            e = next(p for p in ctx["parents"] if p["title"] == "Reliability")
            result = {
                "complete": True,
                "stories": []
                if "See a crash" in ctx["existing"]
                else [
                    {
                        "title": "See a crash",
                        "description": "As an operator I can see execution failures.",
                        "parents": [e["id"]],
                    }
                ],
            }
        else:
            self.issue_drafts += 1
            parents = {s["title"]: s["id"] for s in ctx["parents"]}
            assert parents["Audit executions"] in ctx["fully_covered_stories"]
            assert any(
                m["number"] == 42
                for m in ctx["imported_issue_coverage"][parents["Respond to failures"]]
            )
            residual = {
                "title": "Notify the on-call operator",
                "description": "Send a page for each failed run.",
                "parents": [parents["Respond to failures"]],
            }
            if "issues" in q.schema["properties"]:
                result = {
                    "complete": True,
                    "issues": []
                    if residual["title"] in ctx["existing"]
                    else [
                        {
                            "title": "Another audit ticket",
                            "description": "Duplicate",
                            "parents": [parents["Audit executions"]],
                        },
                        residual,
                    ],
                }
            else:
                result = {"complete": False, **residual}
        return Reply(result, "", "fixture", "analysis", uuid4(), "Test", {"tokens": 12}, 0.1, 1)


def seed(repo: Any) -> dict[str, Any]:
    pid = repo.projects.identity_of("falcone")
    with repo.sessions.begin() as db:
        project = db.get(ProjectRecord, pid)
        db.add(
            ProviderAccount(
                name="Test",
                provider="fixture",
                state="ready",
                models=["analysis"],
                sealed_key="test",
            )
        )
        # The test account's models are of the maker "fixture": the project allows it.
        db.add(ReviewSettings(project_id=pid, makers=["fixture"]))
        db.add(
            WorkspaceImport(
                project_id=pid,
                repository=project.source_repository,
                branch="main",
                stage="complete",
                provider="fixture",
                model="analysis",
                version=7,
                draft="Run serverless jobs.",
                progress={},
            )
        )
        epic = ProjectEpic(project_id=pid, title="Operations", description="Operate the runtime.")
        db.add(epic)
        db.flush()
        ids = {}
        for title, description in (
            ("Audit executions", "Keep a durable audit entry for each execution."),
            ("Respond to failures", "Show failed runs and notify the on-call operator."),
            ("Retired delivery history", "Read the legacy delivery history."),
        ):
            story = ProjectUserStory(project_id=pid, title=title, description=description)
            if title.startswith("Retired"):
                story.closed_at = datetime.now(UTC)
            db.add(story)
            db.flush()
            db.add(ProjectStoryEpic(project_id=pid, story_id=story.id, epic_id=epic.id))
            ids[title] = story.id
        data = [
            (
                41,
                "closed",
                "Append the invocation ledger",
                "A durable audit entry is saved for each invocation.",
            ),
            (
                42,
                "open",
                "Record error events",
                "Operators can see failed runs; notification is separate.",
            ),
            (43, "closed", "Legacy receipts", "Expose the legacy delivery history."),
            (44, "open", "Locked issue", "Audit processing."),
            (45, "open", "Unrelated work", "Refresh dependencies."),
        ]
        import_issues(
            db,
            project,
            [
                {"number": n, "state": s, "title": t, "body": b, "labels": ["task"]}
                for n, s, t, b in data
            ],
        )
        db.flush()
        items = {
            i.issue_number: i
            for i in db.scalars(select(BacklogItem).where(BacklogItem.project_id == pid))
        }
        items[44].facts = {"manual_story_links": True}
        db.add(
            ProjectIssueStory(
                project_id=pid, item_id=items[44].id, story_id=ids["Respond to failures"]
            )
        )
        # The same GitHub number can exist in a different project.
        other = repo.projects.identity_of("llmwiki")
        db.add(
            BacklogItem(
                project_id=other,
                repository="other/elsewhere",
                source="github",
                state="proposed",
                issue_number=999,
                title="Foreign issue",
                workspace_review="approved",
            )
        )
        return {**ids, "project": pid, "epic": epic.id}


async def run_available(imports: WorkspaceImports, model: CoverageModel) -> None:
    for _ in range(30):
        worked = await imports.tick_coverage(cast(Models, model))
        if imports.tasks:
            await asyncio.gather(*imports.tasks)
        if not worked:
            return
    pytest.fail("Coverage did not stop at completion or an approval gate")


def test_dashboard_checks_orphans_then_resumes_after_each_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, repo = _authenticated_client(tmp_path)
    ids = seed(repo)
    monkeypatch.setattr(issue_coverage, "ISSUE_PAGE", 2)
    model = CoverageModel()
    try:

        def state() -> dict[str, Any]:
            return client.get(BASE + "import").json()

        def drive() -> dict[str, Any]:
            asyncio.run(run_available(WorkspaceImports(repo.sessions), model))
            return state()

        def approve(kind: str, result: dict[str, Any]) -> None:
            keys = [c["id"] for c in result["review"]["candidates"] if c["decision"] == "pending"]
            response = post(
                client, "planning/" + kind + "/decisions", ids=keys, version=result["version"]
            )
            assert response.status_code == 200, response.text

        assert (
            client.post(
                BASE + "import/coverage", json={"version": 7, "csrf_token": "x" * 40}
            ).status_code
            == 403
        )
        started = post(client, "import/coverage", version=7).json()
        assert started["coverage_run"]["state"] == "running"
        assert post(client, "import/coverage", version=7).json()["version"] == started["version"]
        # A fabricated ID leaves the affected pairs unresolved and blocks new proposals.
        model.fail_once = True
        failed = drive()
        assert failed["coverage_run"]["state"] == "failed"
        assert "supplied issue" in failed["coverage_run"]["detail"]
        assert not failed["review"]["candidates"]
        assert post(client, "import/coverage", version=failed["version"]).status_code == 200
        epics = drive()
        assert epics["stage"] == "epics" and epics["coverage_run"]["state"] == "waiting"
        assert epics["coverage_run"]["links"] == 3
        assert [41, 42] == model.comparisons[0] and model.comparisons.count([41, 42]) == 1
        assert all(999 not in batch for batch in model.comparisons)
        # Existing open and historical work is linked before even the first approval.
        rows = repo.planning.listing("falcone")["issues"]
        by_number = {i["number"]: i for i in rows}
        assert by_number[41]["story_ids"] == [str(ids["Audit executions"])]
        assert by_number[41]["github_state"] == "closed"
        assert by_number[43]["story_ids"] == [str(ids["Retired delivery history"])]
        assert by_number[44]["story_ids"] == [str(ids["Respond to failures"])]
        assert not by_number[45]["story_ids"]
        approve("epics", epics)
        stories = drive()
        assert stories["stage"] == "stories" and stories["coverage_run"]["state"] == "waiting"
        approve("stories", stories)
        done = drive()
        summary = done["coverage_run"]
        assert (
            summary["state"],
            summary["links"],
            summary["epics"],
            summary["stories"],
            summary["issues"],
        ) == ("completed", 4, 1, 1, 1)
        drafts = [c for c in done["review"]["candidates"] if c["number"] is None]
        assert [(c["title"], c["parents"], c["decision"]) for c in drafts] == [
            ("Notify the on-call operator", [str(ids["Respond to failures"])], "pending")
        ]
        with repo.sessions() as db:
            assert db.scalar(select(func.count()).select_from(WorkspaceAction)) == 0
            assert db.scalar(select(func.count()).select_from(ProjectIssueStory)) == 5
        assert summary["orphans"] == 1
        old_checks, old_drafts = len(model.comparisons), model.issue_drafts
        preview = client.get(BASE + "import/coverage/preview").json()
        assert preview["baseline"] and preview["stages"] == []
        assert preview["remaining"] == 0
        assert (
            post(client, "import/coverage", version=done["version"]).json()["coverage_run"]["state"]
            == "completed"
        )
        in_progress = repo.planning.page("falcone", "issues", 1, 10, issue_state="draft")
        assert [(d["title"], d["review"]) for d in in_progress["items"]] == [
            ("Notify the on-call operator", "proposal")
        ]
        assert in_progress["pending"] == 1
        assert len(repo.planning.pending("falcone", "issues", issue_state="draft")["ids"]) == 1
        again = drive()["coverage_run"]
        assert again["state"] == "completed" and all(
            again[k] == 0 for k in ("links", "epics", "stories", "issues")
        )
        assert (len(model.comparisons), model.issue_drafts) == (old_checks, old_drafts)
        with repo.sessions.begin() as db:
            project = db.get(ProjectRecord, ids["project"])
            import_issues(
                db,
                project,
                [
                    {
                        "number": 93,
                        "title": "New runtime task",
                        "body": "Audit.",
                        "labels": [],
                        "state": "open",
                    }
                ],
            )
        changed = client.get(BASE + "import/coverage/preview").json()
        assert changed["stages"] == ["issues"] and changed["remaining"] == changed["stories"]
        started = post(client, "import/coverage", version=state()["version"]).json()
        assert started["stage"] == "issues" and started["coverage_run"]["state"] == "running"
        # A concurrent edit to an earlier stage must stay visible in the next preview.
        with repo.sessions.begin() as db:
            db.get(ProjectEpic, ids["epic"]).description = "A revised scope that needs review."
        assert drive()["coverage_run"]["state"] == "completed"
        assert client.get(BASE + "import/coverage/preview").json()["stages"] == [
            "epics",
            "stories",
            "issues",
        ]
    finally:
        client.__exit__(None, None, None)


def test_single_issue_and_new_story_use_imported_coverage(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    ids = seed(repo)
    model = CoverageModel()
    client.app.state.models = model
    try:
        with repo.sessions.begin() as db:
            wizard = db.get(WorkspaceImport, ids["project"])
            wizard.stage, wizard.progress = "issues", {"review_mode": "gaps"}
            # Only the new Story's review should run in this test.
            for job in db.scalars(select(PlanningReview)):
                job.status = "covered"
            db.add(
                PlanningReview(
                    project_id=ids["project"],
                    kind="stories",
                    record_id=ids["Audit executions"],
                    title="Audit executions",
                    scope="children",
                )
            )
            item = db.scalar(select(BacklogItem).where(BacklogItem.issue_number == 41))
            db.add(
                WorkspaceCandidate(
                    project_id=ids["project"],
                    kind="issues",
                    number=41,
                    identity="issue:41",
                    title=item.title,
                    parents=[],
                )
            )
        assert asyncio.run(repo.planning_analysis.tick(cast(Models, model)))
        with repo.sessions() as db:
            assert not db.scalar(
                select(WorkspaceCandidate.id).where(WorkspaceCandidate.number.is_(None))
            )
            mirror = db.scalar(select(WorkspaceCandidate).where(WorkspaceCandidate.number == 41))
            assert mirror.parents == [str(ids["Audit executions"])]
        result = post(client, "import/advance", version=7, model="analysis", provider="fixture")
        assert result.status_code == 200, result.text
        candidates = result.json()["review"]["candidates"]
        assert [c["title"] for c in candidates if not c["number"]] == [
            "Notify the on-call operator"
        ]
        assert any(
            c["number"] == 41 and c["parents"] == [str(ids["Audit executions"])] for c in candidates
        )
        assert model.issue_drafts == 1
        with repo.sessions() as db:
            assert not db.scalar(select(WorkspaceAction.id))
    finally:
        client.__exit__(None, None, None)


def test_concurrent_import_invalidates_generation_and_releases_the_step(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    ids = seed(repo)

    class ConcurrentImport(CoverageModel):
        imported = False

        async def ask(self, q: Question, *, retry: bool = True) -> Reply:
            ctx = json.loads(q.prompt)
            reply = await super().ask(q, retry=retry)
            if "imported_issues" in ctx:
                for issue in ctx["imported_issues"]:
                    if issue["number"] == 46:
                        reply.data["matches"].append(
                            {
                                "issue": issue["id"],
                                "story": str(ids["Respond to failures"]),
                                "full": True,
                                "evidence": "Includes both failed-run history and paging.",
                            }
                        )
            elif not self.imported:
                self.imported = True
                with repo.sessions.begin() as db:
                    project = db.get(ProjectRecord, ids["project"])
                    import_issues(
                        db,
                        project,
                        [
                            {
                                "number": 46,
                                "state": "open",
                                "title": "Page on runtime failure",
                                "body": "Display failures and page the on-call operator.",
                            }
                        ],
                    )
            return reply

    model = ConcurrentImport()
    client.app.state.models = model
    try:
        with repo.sessions.begin() as db:
            row = db.get(WorkspaceImport, ids["project"])
            row.stage, row.progress = "issues", {"review_mode": "gaps"}
        result = post(client, "import/advance", version=7, model="analysis", provider="fixture")
        assert result.status_code == 409 and "changed" in result.text
        with repo.sessions() as db:
            assert db.get(WorkspaceImport, ids["project"]).lease is None
            assert not db.scalar(select(WorkspaceCandidate.id))
        state = client.get(BASE + "import").json()
        result = post(
            client, "import/advance", version=state["version"], model="analysis", provider="fixture"
        )
        assert result.status_code == 200, result.text
        with repo.sessions() as db:
            issue = db.scalar(select(BacklogItem).where(BacklogItem.issue_number == 46))
            assert (
                db.get(ProjectIssueStory, (ids["project"], issue.id, ids["Respond to failures"]))
                is not None
            )
            assert not db.scalar(select(WorkspaceCandidate.id))
            assert not db.scalar(select(WorkspaceAction.id))
        assert model.issue_drafts == 1
    finally:
        client.__exit__(None, None, None)


def test_combined_partial_coverage_and_abandoned_history(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    ids = seed(repo)

    class CombinedIssues(CoverageModel):
        async def ask(self, q: Question, *, retry: bool = True) -> Reply:
            ctx = json.loads(q.prompt)
            if "imported_issues" in ctx:
                reply = await super().ask(q, retry=retry)
                for issue in ctx["imported_issues"]:
                    if issue["number"] in (46, 47) and any(
                        s["id"] == str(ids["Respond to failures"]) for s in ctx["stories"]
                    ):
                        reply.data["matches"].append(
                            {
                                "issue": issue["id"],
                                "story": str(ids["Respond to failures"]),
                                "full": issue["number"] == 47,
                                "evidence": issue["description"],
                            }
                        )
                return reply
            self.issue_drafts += 1
            key = str(ids["Respond to failures"])
            assert key not in ctx["fully_covered_stories"]
            matches = {m["number"]: m for m in ctx["imported_issue_coverage"][key]}
            assert not matches[47]["full"] and matches[47]["state_reason"] == "not_planned"
            assert matches[42]["evidence"] and matches[46]["evidence"]
            # Open work for visibility together with delivered paging covers the Story.
            return Reply(
                {"complete": True, "issues": []},
                "",
                "fixture",
                "analysis",
                uuid4(),
                "Test",
                {},
                0.1,
                1,
            )

    try:
        with repo.sessions.begin() as db:
            project = db.get(ProjectRecord, ids["project"])
            import_issues(
                db,
                project,
                [
                    {
                        "number": 46,
                        "title": "Pager integration",
                        "body": "Sends pages for each failure.",
                        "state": "closed",
                        "state_reason": "completed",
                    },
                    {
                        "number": 47,
                        "title": "Prototype for incident handling",
                        "body": "Abandoned prototype for alerts and visibility.",
                        "state": "closed",
                        "state_reason": "not_planned",
                    },
                ],
            )
            row = db.get(WorkspaceImport, ids["project"])
            row.stage, row.progress = "issues", {"review_mode": "gaps"}
        model = CombinedIssues()
        client.app.state.models = model
        result = post(
            client,
            "import/advance",
            version=7,
            all_issues=True,
            model="analysis",
            provider="fixture",
        )
        assert result.status_code == 200, result.text
        assert result.json()["review"]["bulk_complete"] and model.issue_drafts == 1
        with repo.sessions() as db:
            assert not db.scalar(select(WorkspaceCandidate.id))
            linked = db.scalars(
                select(BacklogItem.issue_number)
                .join(ProjectIssueStory, ProjectIssueStory.item_id == BacklogItem.id)
                .where(ProjectIssueStory.story_id == ids["Respond to failures"])
            ).all()
            assert set(linked) == {42, 44, 46, 47}
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("bad_issue", "bad_story", "blank", "expected"),
    [
        (True, False, False, "issue ID outside"),
        (False, True, False, "Story ID outside"),
        (False, False, True, "empty evidence"),
        (True, True, True, "empty evidence"),
    ],
)
def test_bad_citation_only_retries_ambiguous_pairs(
    tmp_path: Path, bad_issue: bool, bad_story: bool, blank: bool, expected: str
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ids = seed(repo)
        service = issue_coverage.IssueCoverage(repo.sessions)
        stories = {str(ids["Audit executions"]), str(ids["Respond to failures"])}

        def next_page() -> issue_coverage.Page | issue_coverage.Coverage:
            return service.page("falcone", stories, "fixture", "analysis", lambda _: None, False)

        page = next_page()
        assert isinstance(page, issue_coverage.Page)
        by_number = {row["number"]: row["id"] for row in page.issues}
        audit, respond = str(ids["Audit executions"]), str(ids["Respond to failures"])
        good = {
            "story": audit,
            "issue": by_number[41],
            "evidence": "Durable audit entry",
            "full": True,
        }
        invalid = {
            "story": "redacted-foreign-story" if bad_story else respond,
            "issue": "redacted-foreign-issue" if bad_issue else by_number[42],
            "evidence": "   " if blank else "Redacted criteria",
            "full": True,
        }
        with pytest.raises(ProjectInvalid, match=expected) as exc:
            service.persist("falcone", "fixture", "analysis", page, [good, invalid], lambda _: None)
        assert "match 2" in str(exc.value) and "redacted" not in str(exc.value)
        eligible = {
            (s, i["id"])
            for s in stories
            for i in page.issues
            if i["manual_stories"] is None or s in i["manual_stories"]
        }
        uncertain = {
            (s, i)
            for s, i in eligible
            if (bad_story or s == respond) and (bad_issue or i == by_number[42])
        }
        uncertain.discard((audit, by_number[41]))
        with repo.sessions() as db:
            checked = {
                (str(row.story_id), i)
                for row in db.scalars(select(StoryIssueCoverage))
                for i in (row.issue_versions or {})
            }
        assert checked == eligible - uncertain
        remaining = next_page()
        while isinstance(remaining, issue_coverage.Page):
            pairs = {
                (s, i["id"])
                for s in remaining.story_versions
                for i in remaining.issues
                if i["manual_stories"] is None or s in i["manual_stories"]
            }
            assert pairs and pairs <= uncertain
            uncertain -= pairs
            service.persist("falcone", "fixture", "analysis", remaining, [], lambda _: None)
            remaining = next_page()
        assert not uncertain
        assert [m["number"] for m in remaining.matches[audit]] == [41]
        with repo.sessions.begin() as db:
            project = db.get(ProjectRecord, ids["project"])
            assert remaining.link(db, project) == 1
            assert remaining.link(db, project) == 0
    finally:
        client.__exit__(None, None, None)
