"""A defect can stand alone or cover a Story through an established link."""

from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from engineering_control.dashboard import issue_coverage, review_graph
from engineering_control.dashboard.workspace_issues import import_issues
from engineering_control.db.models import (
    BacklogItem,
    ProjectRecord,
    ProjectUserStory,
    WorkspaceAction,
    WorkspaceCandidate,
    WorkspaceImport,
)
from tests.test_automatic_planning import Model, configure, drain, empty, post, suggestion
from tests.test_dashboard_web import _authenticated_client
from tests.test_workspace_issue_inventory import BASE, snapshot


def test_bug_sources_optional_links_and_counts(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        pid = repo.projects.identity_of("falcone")
        epic = UUID(repo.planning.save("falcone", "epics", "Billing", None, [])["id"])
        story = repo.planning.save("falcone", "stories", "Receipt", None, [epic])["id"]
        other = repo.planning.save("falcone", "stories", "Refund", None, [epic])["id"]
        with repo.sessions.begin() as db:
            db.add(
                WorkspaceImport(
                    project_id=pid, repository="gntik-ai/falcone", branch="main", stage="complete"
                )
            )
            db.flush()
            import_issues(
                db,
                db.get_one(ProjectRecord, pid),
                [
                    snapshot(1, labels=[{"name": "Type: Bug"}]),
                    snapshot(2, issue_type="Bug", state="closed"),
                    snapshot(3, labels=["bug", "enhancement"]),
                    snapshot(4, title="Fix a bug mentioned in the documentation"),
                    snapshot(5, type={"name": "Bug"}),
                ],
            )
            item = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == 4)).one()
            item.execution_type = "bug"  # An unconfirmed model suggestion.

        rows = {r["number"]: r for r in client.get(BASE).json()["items"]}
        assert {n for n, r in rows.items() if r["standalone_bug"]} == {1, 2, 5}
        assert {n for n, r in rows.items() if r["orphan"]} == {3, 4}
        assert repo.execution_plans.overview("falcone")["orphan_count"] == 2
        assert client.get(BASE, params={"issue_state": "closed"}).json()["items"][0]["number"] == 2

        def check_coverage(linked: bool) -> None:
            with repo.sessions() as db:
                project = db.get_one(ProjectRecord, pid)
                data = issue_coverage.inventory(db, project, {story, other}, "fake", "test")
                assert (rows[1]["id"] in data.eligible(story)) == linked
                assert rows[1]["id"] not in data.eligible(other)
                assert rows[3]["id"] in data.eligible(other)
                assert {
                    i.issue_number
                    for i in issue_coverage.technical_items(db, project, unlinked=True)
                } == {3, 4}
                graph = review_graph.snapshot(db, project, False, "", "", "")
                pairs, _ = review_graph.candidates(graph, 3)
                actual = [p["story"] for p in pairs if p["issue"].endswith(rows[1]["id"])]
                assert actual == (
                    [review_graph.source_key(pid, "stories", UUID(story))] if linked else []
                )

        check_coverage(False)
        path = "planning/issues/" + rows[1]["id"] + "/stories"
        assert post(client, path, story_ids=[story]).status_code == 200
        check_coverage(True)
        assert client.get(BASE.replace("/issues", "/epics")).json()["items"][0]["issue_count"] == 1
        assert post(client, path, story_ids=[]).status_code == 200
        check_coverage(False)
        assert post(client, path, story_ids=[], execution_type="enhancement").status_code == 200
        updated = next(r for r in client.get(BASE).json()["items"] if r["number"] == 1)
        assert updated["orphan"] and not updated["story_optional"] and updated["type_confirmed"]
        assert (
            post(client, "planning/issues/" + rows[3]["id"] + "/stories", story_ids=[]).status_code
            == 422
        )
        with repo.sessions() as db:
            assert [a.operation for a in db.scalars(select(WorkspaceAction))] == ["label"]
    finally:
        client.close()


def test_pending_github_bug_shows_its_established_links(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        pid = repo.projects.identity_of("falcone")
        epic = UUID(repo.planning.save("falcone", "epics", "Payments", None, [])["id"])
        story = repo.planning.save("falcone", "stories", "Receipt", None, [epic])["id"]
        with repo.sessions.begin() as db:
            db.add(
                WorkspaceImport(
                    project_id=pid, repository="gntik-ai/falcone", branch="main", stage="complete"
                )
            )
            db.flush()
            import_issues(db, db.get_one(ProjectRecord, pid), [snapshot(12, labels=["bug"])])
        [issue] = client.get(BASE).json()["items"]
        assert (
            post(
                client, "planning/issues/" + issue["id"] + "/stories", story_ids=[story]
            ).status_code
            == 200
        )
        with repo.sessions.begin() as db:
            candidate = WorkspaceCandidate(
                project_id=pid,
                kind="issues",
                number=12,
                identity="issue:12",
                origin="intake",
                title="Existing defect",
                parents=[],
            )
            db.add(candidate)
            db.flush()
            key = str(candidate.id)
        [mirror] = client.get(BASE).json()["items"]
        assert (
            mirror["id"] == key and mirror["story_ids"] == [story] and not mirror["standalone_bug"]
        )
        result = post(
            client, "planning/issues/" + key + "/proposal", title=mirror["title"], parents=[]
        )
        assert result.status_code == 200, result.text
        [standalone] = client.get(BASE).json()["items"]
        assert not standalone["story_ids"] and standalone["standalone_bug"]
    finally:
        client.close()


def test_draft_bug_can_be_approved_without_a_story(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        key = post(client, "planning/issues", title="Repair crash", execution_type="bug").json()[
            "id"
        ]
        pending = client.get(BASE + "/pending").json()
        assert pending["ids"] == [key]
        [row] = client.get(BASE, params={"issue_state": "draft"}).json()["items"]
        assert row["standalone_bug"] and row["proposal"] and not row["orphan"]
        with repo.sessions() as db:
            assert db.scalar(select(WorkspaceAction)) is None
        assert post(client, "planning/issues/decisions", **pending).status_code == 200
        [row] = client.get(BASE).json()["items"]
        assert row["standalone_bug"] and row["type_confirmed"] and not row["proposal"]
        with repo.sessions() as db:
            [action] = db.scalars(select(WorkspaceAction)).all()
            assert action.operation == "create" and action.number is None
    finally:
        client.close()


def test_automatic_bug_review_does_not_manufacture_a_parent(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        configure(repo, client)
        pid = repo.projects.identity_of("falcone")
        with repo.sessions.begin() as db:
            story = ProjectUserStory(project_id=pid, title="Unrelated Story")
            db.add_all(
                [
                    story,
                    WorkspaceImport(
                        project_id=pid,
                        repository="gntik-ai/falcone",
                        branch="main",
                        stage="complete",
                    ),
                ]
            )
            db.flush()
            other = str(story.id)
            import_issues(
                db,
                db.get_one(ProjectRecord, pid),
                [
                    snapshot(
                        7,
                        issue_type="Bug",
                        body="Reproduce the crash on startup, then verify startup succeeds.",
                    )
                ],
            )
        model = Model(
            lambda _: empty(parents=[other], issues=[suggestion("Invented follow-up", other)])
        )
        drain(repo.planning_analysis, model)
        [review] = model.calls
        assert review["scope"] == "verify" and review["records"] == []
        [row] = client.get(BASE).json()["items"]
        assert row["standalone_bug"] and not row["story_ids"]
        assert row["automatic_review"]["status"] == "covered"
        assert not row["automatic_review"]["questions"] and not row["automatic_review"]["proposals"]
    finally:
        client.close()
