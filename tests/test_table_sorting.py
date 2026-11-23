"""Sorting happens before pagination and preserves hierarchy, focus and source order."""

from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.planning import Kind, PlanningRecords
from engineering_control.dashboard.review_evidence import selection
from engineering_control.db.models import (
    BacklogItem,
    PlanningReview,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
    WorkspaceCandidate,
    WorkspaceImport,
)
from engineering_control.table_sort import Direction
from tests.test_dashboard_web import _authenticated_client


def sample(sessions: sessionmaker[Session]) -> list[UUID]:
    with sessions.begin() as db:
        project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
        ids = []
        for n in range(16):
            title = f"Epic {n:02d}"
            epic = ProjectEpic(
                project_id=project.id, title=title, description=title + " description"
            )
            db.add(epic)
            db.flush()
            ids.append(epic.id)
            for index in range(n % 4 + 1):
                story = ProjectUserStory(project_id=project.id, title=f"Story {n:02d}.{index}")
                db.add(story)
                db.flush()
                db.add(ProjectStoryEpic(project_id=project.id, epic_id=epic.id, story_id=story.id))
                task = BacklogItem(
                    project_id=project.id,
                    title=f"Issue {n:02d}.{index}",
                    source="github",
                    state="accepted",
                    workspace_review="approved",
                    issue_number=100 + n * 4 + index,
                    issue_state="open",
                    repository=project.source_repository,
                )
                db.add(task)
                db.flush()
                db.add(ProjectIssueStory(project_id=project.id, story_id=story.id, item_id=task.id))
        db.add(
            WorkspaceCandidate(
                project_id=project.id,
                kind="stories",
                title="Proposed Story",
                identity="sort:proposal",
                parents=[str(ids[-1]), "repository:#17"],
                origin="review",
            )
        )
        return ids


def exercise(sessions: sessionmaker[Session]) -> None:
    ids = sample(sessions)
    planning = PlanningRecords(sessions)

    def page(kind: Kind, sort: str, n: int = 1, **kwargs: Any) -> dict[str, Any]:
        return planning.page("falcone", kind, n, 10, sort=sort, **kwargs)

    first = page("epics", "title", direction="desc")
    second = page("epics", "title", 2, direction="desc")
    assert len(first["items"]) == 10 and len(second["items"]) == 6
    assert [r["id"] for r in first["items"] + second["items"]] == [str(x) for x in reversed(ids)]
    assert page("epics", "title", direction="desc", record=ids[0])["page"] == 2
    for key in ("issue_count", "story_count"):
        by_count = (
            page("epics", key, direction="desc")["items"]
            + page("epics", key, 2, direction="desc")["items"]
        )
        counts = [row[key] for row in by_count]
        assert counts == sorted(counts, reverse=True) and counts[0] == 4 and counts[-1] == 1
    for kind in ("stories", "issues"):
        by_parent = page(kind, "parents", direction="desc")
        assert by_parent["items"][0]["title"].startswith(("Story 15", "Issue 15", "Proposed Story"))
        assert page(kind, "connections")["total"] == by_parent["total"]
        assert page(kind, "status")["total"] == by_parent["total"]
    assert page("epics", "title", direction="desc", search="Epic 01")["total"] == 1
    assert planning.page("falcone-serverless", "epics", 1, 10, sort="title")["total"] == 0


def test_all_result_pages_sort_and_keep_their_filters(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        exercise(repo.sessions)
        base = "/api/dashboard/projects/falcone/planning/epics"
        assert client.get(base + "?sort=actions").status_code == 422
        assert client.get(base + "?sort=parents").status_code == 422
        assert client.get(base + "?sort=title&direction=sideways").status_code == 422
        assert (
            client.get(base + "?sort=title&direction=desc").json()["items"][0]["title"] == "Epic 15"
        )
        items = client.get(
            "/api/dashboard/items?project=falcone&sort=title&direction=desc&page_size=10"
        ).json()
        assert items["total"] == 40 and items["items"][0]["title"] == "Issue 15.3"
    finally:
        client.__exit__(None, None, None)


def exercise_planning_column(sessions: sessionmaker[Session]) -> None:
    with sessions.begin() as db:
        project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
        issues = db.scalars(
            select(BacklogItem)
            .where(BacklogItem.project_id == project.id)
            .order_by(BacklogItem.issue_number)
            .limit(3)
        ).all()
        for issue, status in zip(issues, ("paused", "covered", "needs_input"), strict=True):
            db.add(
                PlanningReview(
                    project_id=project.id,
                    kind="issues",
                    record_id=issue.id,
                    scope="full",
                    title=issue.title,
                    status=status,
                )
            )
        db.add(
            WorkspaceImport(
                project_id=project.id,
                repository=project.source_repository,
                branch="main",
                stage="epics",
                progress={"coverage_run": {"state": "running"}},
            )
        )
        db.add_all(
            [
                WorkspaceCandidate(
                    project_id=project.id,
                    kind="issues",
                    identity="source:mirror",
                    origin="review",
                    title="Mirrored issue",
                    number=issues[1].issue_number,
                ),
                WorkspaceCandidate(
                    project_id=project.id,
                    kind="issues",
                    identity="source:waiting",
                    origin="import",
                    title="Waiting for the Issues stage",
                    number=900,
                ),
            ]
        )
        proposal = db.scalars(
            select(WorkspaceCandidate).where(WorkspaceCandidate.kind == "stories")
        ).one()
        db.add(
            PlanningReview(
                project_id=project.id,
                kind="stories",
                record_id=proposal.id,
                scope="intake",
                title=proposal.title,
                status="matched",
            )
        )
    planning = PlanningRecords(sessions)
    directions: tuple[Direction, ...] = ("asc", "desc")
    for direction in directions:
        rows = [
            row
            for page in (1, 2, 3)
            for row in planning.page(
                "falcone", "issues", page, 2, sort="planning", direction=direction
            )["items"]
        ]
        labels = [
            "Awaiting Issues review"
            if row["review"] == "awaiting_stage"
            else (row["automatic_review"] or {}).get("status_label")
            for row in rows
        ]
        names = ["Already covered", "Awaiting Issues review", "Needs input", "Stopped"]
        assert labels == (names if direction == "asc" else names[::-1]) + [None, None]
        assert (
            next(r for r in rows if r["title"] == "Mirrored issue")["automatic_review"]["status"]
            == "covered"
        )
    first_story = planning.page("falcone", "stories", 1, 1, sort="planning")["items"][0]
    assert (
        first_story["proposal"]
        and first_story["automatic_review"]["status_label"] == "Ready for approval"
    )


def test_planning_column_sorts_visible_review_labels_before_pagination(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        sample(repo.sessions)
        exercise_planning_column(repo.sessions)
        response = client.get(
            "/api/dashboard/projects/falcone/planning/issues?sort=planning&page_size=1"
        )
        assert response.status_code == 200
        assert response.json()["items"][0]["title"] == "Mirrored issue"
    finally:
        client.__exit__(None, None, None)


def test_evidence_sorts_before_slicing_pairs_and_preserves_the_inference_order() -> None:
    fields = {
        "project": "example",
        "repository": "example/service",
        "number": None,
        "state": "open",
        "parents": [],
        "revision": "1",
        "body": "Source text",
    }
    records = {"story": {**fields, "kind": "stories", "title": "One Story"}}
    pairs = []
    for number in range(12):
        issue = f"Issue {number:02d}"
        records[issue] = {**fields, "kind": "issues", "title": issue, "number": number}
        pairs.append(
            {
                "issue": issue,
                "story": "story",
                "why": f"Reason {number:02d}",
                "decision": "unchecked",
            }
        )
    src, result = {"records": records}, {"pairs": pairs}
    descending = [
        row["reason"]
        for page in (1, 2)
        for row in selection(src, result, page, "", "reason", "desc")["items"]
    ]
    assert descending == [pair["why"] for pair in reversed(pairs)]
    assert selection(src, result, 1, "", "issue", "desc")["items"][0]["issue"]["number"] == 11
    assert selection(src, result, 1, "Issue 00", "reason", "asc")["total"] == 1
    assert selection(src, result, 1, "")["items"][0]["issue"]["number"] == 0
