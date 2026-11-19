import asyncio
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import select

from engineering_control.dashboard.workspace_issues import import_issues, planning_kind
from engineering_control.dashboard.workspace_publishing import enqueue
from engineering_control.db.models import (
    ProjectRecord,
    WorkspaceAction,
    WorkspaceCandidate,
    WorkspaceImport,
    WorkspaceSource,
)
from engineering_control.github_app import GitHubApp
from tests.test_dashboard_web import _authenticated_client

REPOSITORY = "gntik-ai/falcone"
BASE = "/api/dashboard/projects/falcone/planning/issues"


def snapshot(number: int, *, state: str = "open", **fields: Any) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Technical work {number}",
        "body": "",
        "labels": [],
        "state": state,
        "closed_at": "2026-11-18T12:00:00Z" if state == "closed" else None,
        "updated_at": "2026-11-19T12:00:00Z",
        **fields,
    }


@pytest.mark.parametrize(
    ("issue", "kind"),
    [
        (snapshot(1, labels=["type:epic"]), "epics"),
        (snapshot(1, issue_type="User Story"), "stories"),
        (snapshot(1, type={"name": "User Story"}), "stories"),
        (snapshot(1, labels=[{"name": "kind/user-story"}]), "stories"),
        (snapshot(1, title="[Epic] Runtime migration"), "epics"),
        (snapshot(1, body="kind: story\n\nOverview"), "stories"),
        (snapshot(1, title="Change request: UI cutover", labels=["type:epic"]), None),
        (snapshot(1, body="## Change request\n- Story: #36\n- Epic: #8"), None),
        (snapshot(1, body="## Change request\n- Story: #36", labels=["type:story"]), None),
        (snapshot(1, labels=["bug", "story"]), None),
        (snapshot(1, labels=["type:story", "type:epic"]), None),
        (snapshot(1, title="Link the Epic: runtime to its Story"), None),
    ],
)
def test_classification_uses_the_issue_own_identity(
    issue: dict[str, Any], kind: str | None
) -> None:
    assert planning_kind(issue) == kind


def test_full_inventory_links_orphans_views_and_replays(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        project = repo.projects.identity_of("falcone")
        epic = UUID(repo.planning.save("falcone", "epics", "Runtime", None, [])["id"])
        story = UUID(repo.planning.save("falcone", "stories", "Run functions", None, [epic])["id"])
        draft = repo.planning.save("falcone", "issues", "New local proposal", None, [story])["id"]
        with repo.sessions.begin() as db:
            db.add(
                WorkspaceImport(
                    project_id=project, repository=REPOSITORY, branch="main", stage="complete"
                )
            )
            db.flush()
            db.add(
                WorkspaceSource(
                    project_id=project,
                    key="issue:36",
                    kind="issue",
                    evidence=snapshot(36, labels=["type:story"]),
                )
            )
            db.add(
                WorkspaceCandidate(
                    project_id=project,
                    kind="stories",
                    decision="approved",
                    identity="issue:36",
                    number=36,
                    title="Run functions",
                    parents=[],
                    local_id=story,
                )
            )
            db.add(
                WorkspaceCandidate(
                    project_id=project,
                    kind="epics",
                    identity="issue:19",
                    number=19,
                    title="Legacy mistaken proposal",
                    parents=[],
                )
            )

        class History:
            def __init__(self) -> None:
                self.pages: list[tuple[int, str | None]] = []
                self.reopen = False

            async def workspace_history(
                self, repository: str, page: int, *, since: str | None = None
            ) -> tuple[list[dict[str, Any]], bool]:
                assert repository == REPOSITORY
                self.pages.append((page, since))
                if self.reopen:
                    return [snapshot(20, updated_at="2026-11-20T10:00:00Z")], True
                if page == 1:
                    return [
                        snapshot(
                            19,
                            title="Change request: Choose a runtime",
                            body="- Story: gntik-ai/falcone#36",
                        ),
                        snapshot(36, labels=["type:story"]),
                    ], False
                return [
                    snapshot(20, state="closed", body="- Story: gntik-ai/falcone-serverless#36")
                ], True

        history = History()

        async def refresh() -> None:
            await repo.issue_inventory.refresh("falcone", cast(GitHubApp, history))

        asyncio.run(refresh())
        opened = client.get(BASE + "?issue_state=open").json()
        assert opened["issue_counts"] == {"open": 1, "closed": 1, "draft": 1}
        item = opened["items"][0]
        assert item["number"] == 19 and not item["orphan"] and item["story_ids"] == [str(story)]
        closed = client.get(BASE + "?issue_state=closed").json()["items"][0]
        assert closed["number"] == 20 and closed["orphan"] and closed["github_state"] == "closed"
        assert client.get(BASE + "?issue_state=draft").json()["items"][0]["id"] == draft
        assert client.get(BASE + "/pending?issue_state=open").json()["ids"] == [item["id"]]
        assert client.get(BASE + "/pending?issue_state=closed").json()["ids"] == [closed["id"]]
        assert (
            client.get(BASE + "?issue_state=open&record=" + closed["id"]).json()["issue_state"]
            == "closed"
        )
        csrf = client.get("/api/dashboard/session").json()["csrf_token"]
        assert (
            client.post(
                BASE + "/" + closed["id"] + "/stories",
                json={"csrf_token": csrf, "story_ids": [str(story)]},
            ).status_code
            == 200
        )
        history.reopen = True
        asyncio.run(refresh())
        assert history.pages[0:2] == [(1, None), (2, None)] and history.pages[-1][1]
        with repo.sessions.begin() as db:
            record = db.get(ProjectRecord, project)
            assert record
            import_issues(db, record, [snapshot(20, state="closed")])
            assert not list(db.scalars(select(WorkspaceAction)))
            prior = db.scalars(
                select(WorkspaceCandidate).where(WorkspaceCandidate.identity == "issue:19")
            ).one()
            assert prior.decision == "dismissed"
        reopened = client.get(BASE + "?issue_state=open&search=%2320").json()
        assert reopened["total"] == 1 and reopened["items"][0]["id"] == closed["id"]
        assert (
            reopened["items"][0]["story_ids"] == [str(story)] and not reopened["items"][0]["orphan"]
        )
        assert client.get(BASE + "?issue_state=closed").json()["total"] == 0
    finally:
        client.__exit__(None, None, None)


def test_publisher_retains_retyped_and_legacy_technical_issues(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        project = repo.projects.identity_of("falcone")
        with repo.sessions.begin() as db:
            db.add(
                WorkspaceImport(
                    project_id=project, repository=REPOSITORY, branch="main", stage="issues"
                )
            )

        class Remote:
            async def workspace_issue(self, repository: str, number: int) -> dict[str, Any]:
                return snapshot(number, title="Change request: Technical implementation")

        for n, labels in [(3, ["type:epic"]), (4, [])]:
            uid = UUID(repo.planning.save("falcone", "epics", f"Saved Epic {n}", None, [])["id"])
            with repo.sessions.begin() as db:
                db.add(
                    WorkspaceSource(
                        project_id=project,
                        key=f"issue:{n}",
                        kind="issue",
                        evidence=snapshot(n, labels=labels),
                    )
                )
                db.add(
                    WorkspaceCandidate(
                        project_id=project,
                        kind="epics",
                        number=n,
                        identity=f"issue:{n}",
                        title="Saved",
                        parents=[],
                        decision="approved",
                        local_id=uid,
                    )
                )
                key = enqueue(db, project, "close", uid, REPOSITORY, n).id
            for _ in range(2):
                answer = asyncio.run(
                    repo.publishing.sync("falcone", key, cast(GitHubApp, Remote()))
                )
                assert answer["status"] == "retained"
        assert not repo.publishing.due() and not repo.publishing.outstanding("falcone")
    finally:
        client.__exit__(None, None, None)


def test_review_cannot_promote_a_change_request_or_drop_an_unmatched_issue(tmp_path: Path) -> None:
    import json
    from uuid import uuid4

    from engineering_control.models.service import Models, Question, Reply

    client, repo = _authenticated_client(tmp_path)
    try:
        pid = repo.projects.identity_of("falcone")
        with repo.sessions.begin() as db:
            row = WorkspaceImport(
                project_id=pid,
                repository=REPOSITORY,
                branch="main",
                stage="epics",
                model="fixture",
                provider="fixture",
                progress={},
            )
            db.add(row)
            db.flush()
            project = db.get(ProjectRecord, pid)
            assert project
            import_issues(
                db,
                project,
                [
                    snapshot(19, title="Change request: Runtime", body="- Epic: #8\n- Story: #36"),
                    snapshot(20, state="closed"),
                ],
            )
            db.expunge(row)

        class Model:
            async def ask(self, question: Question, *, retry: bool = True) -> Reply:
                assert question.system.startswith("Review issues.")
                records = [
                    {
                        "number": i["number"],
                        "title": "Model title",
                        "description": "Invented",
                        "include": False,
                        "parents": [],
                    }
                    for i in json.loads(question.prompt)["github_issues"]
                ]
                return Reply(
                    {"records": records}, "", "fixture", "fixture", uuid4(), "fixture", {}, 0, 0
                )

        model = cast(Models, Model())
        for kind in ("epics", "stories"):
            row.stage = kind
            _, proposals = asyncio.run(repo.workspace_imports.review.propose(row, model))
            assert not proposals
        row.stage = "issues"
        _, proposals = asyncio.run(repo.workspace_imports.review.propose(row, model))
        assert {s["number"] for s in proposals} == {19, 20}
        assert all(not s["parents"] and s["title"] != "Model title" for s in proposals)
    finally:
        client.__exit__(None, None, None)
