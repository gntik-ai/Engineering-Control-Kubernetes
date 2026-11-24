"""Each open User Story gets a state from evidence: what is linked decides, what only
looks alike is a lead; delivered Stories are closed by an approved batch, and reopened
when the batch is undone."""

from pathlib import Path
from typing import Any
from uuid import UUID

import numpy
from sqlalchemy import select

from engineering_control.dashboard import ranking
from engineering_control.dashboard.planning_health import measure
from engineering_control.dashboard.workspace_issues import import_issues
from engineering_control.db.models import (
    BacklogItem,
    DocFile,
    PlanningLink,
    ProjectIssueStory,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
    ScorerDecision,
    StoryDelivery,
    WorkspaceAction,
)
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/"
STORIES = ("Receipts", "Refunds", "Exports", "Imports", "Search", "Themes")


def vector(*values: float) -> bytes:
    made = numpy.array(values, dtype=numpy.float32)
    return bytes((made / numpy.linalg.norm(made)).tobytes())


def plan(repo: Any, data: dict[str, Any]) -> dict[str, UUID]:
    pid = data["project"]
    made: dict[str, UUID] = {}
    with repo.sessions.begin() as db:
        project = db.get_one(ProjectRecord, pid)
        for title in STORIES:
            story = ProjectUserStory(project_id=pid, title=title, description=f"About {title}.")
            db.add(story)
            db.flush()
            db.add(ProjectStoryEpic(project_id=pid, story_id=story.id, epic_id=data["epic"]))
            made[title] = story.id
        import_issues(
            db,
            project,
            [
                {
                    "number": 61,
                    "state": "closed",
                    "title": "Print receipts",
                    "body": "",
                    "labels": [],
                },
                {
                    "number": 62,
                    "state": "closed",
                    "title": "Refund a charge",
                    "body": "",
                    "labels": [],
                },
                {
                    "number": 63,
                    "state": "closed",
                    "title": "Export to CSV",
                    "body": "",
                    "labels": [],
                },
                {
                    "number": 64,
                    "state": "closed",
                    "state_reason": "not_planned",
                    "title": "Import from a spreadsheet",
                    "body": "",
                    "labels": [],
                },
            ],
        )
        db.flush()
        items = {
            item.issue_number: item.id
            for item in db.scalars(select(BacklogItem).where(BacklogItem.project_id == pid))
        }
        for number, title in ((61, "Receipts"), (62, "Refunds"), (63, "Exports"), (64, "Imports")):
            db.add(ProjectIssueStory(project_id=pid, item_id=items[number], story_id=made[title]))
        # The OpenSpec change named after issue 63 has every task done.
        db.add(
            DocFile(
                project_id=pid,
                path="openspec/changes/add-63-csv-export/tasks.md",
                blob_sha="a" * 40,
                title="Tasks",
                text="- [x] one\n- [x] two\n",
                facts={
                    "openspec": {"change": "add-63-csv-export", "archived": False, "issue": 63},
                    "tasks": {"done": 2, "total": 2},
                },
            )
        )
        guide = DocFile(
            project_id=pid,
            path="docs/themes.md",
            blob_sha="b" * 40,
            title="Themes of the console",
            text="How the console is themed.",
            facts={},
        )
        db.add(guide)
        db.flush()
        # A document lies close to the Story about themes, and to no other.
        ranking.keep_vector(db, pid, "story", made["Themes"], "m", "d", vector(1, 0, 0, 0))
        ranking.keep_vector(db, pid, "doc", guide.id, "m", "d", vector(0.95, 0.1, 0, 0))
        ranking.keep_vector(db, pid, "story", made["Search"], "m", "d", vector(0, 1, 0, 0))
        ranking.keep_vector(
            db, pid, "story", data["Audit executions"], "m", "d", vector(0, 0, 1, 0)
        )
        # The scorer gives the Story about search a real share for an issue with no Story.
        db.add(
            ScorerDecision(
                project_id=pid,
                kind="issue_story",
                child_id=items[45],
                parent_id=made["Search"],
                outcome="several",
                confidence=0.4,
                model="light",
                detail={
                    "first": [[str(made["Search"]), 0.4], [str(made["Themes"]), 0.05]],
                    "signals": {},
                },
            )
        )
    repo.planning_links.reconcile_project("falcone")
    with repo.sessions.begin() as db:
        # A review found that issue 61 covers its Story in full.
        link = db.scalars(select(PlanningLink).where(PlanningLink.child_id == items[61])).one()
        link.strength = "full"
    return made


def states(repo: Any) -> dict[UUID, str]:
    with repo.sessions() as db:
        return {row.story_id: row.state for row in db.scalars(select(StoryDelivery))}


def test_each_story_stands_where_its_evidence_puts_it(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        made = plan(repo, data)
        assert repo.delivery.refresh("falcone") == 8
        found = states(repo)
        assert {title: found[key] for title, key in made.items()} == {
            "Receipts": "delivered",
            "Refunds": "closed_work",
            "Exports": "delivered",
            "Imports": "dropped",
            "Search": "leads",
            "Themes": "planned",
        }
        # An open issue under it; and nothing at all. A closed Story is not looked at.
        assert found[data["Respond to failures"]] == "in_progress"
        assert found[data["Audit executions"]] == "unsupported"
        assert data["Retired delivery history"] not in found

        overview = client.get(BASE + "delivery").json()
        counts = {row["state"]: row["count"] for row in overview["states"]}
        assert counts == {
            "delivered": 2,
            "closed_work": 1,
            "in_progress": 1,
            "dropped": 1,
            "leads": 1,
            "planned": 1,
            "unsupported": 1,
        }
        listed = client.get(BASE + "delivery/stories", params={"state": "delivered"}).json()
        by_title = {row["title"]: row for row in listed["items"]}
        assert "review" in by_title["Receipts"]["shown_by"]
        assert "add-63-csv-export" in by_title["Exports"]["shown_by"]
        assert by_title["Exports"]["openspec"][0]["done"] == 2
        [lead] = client.get(BASE + "delivery/stories", params={"state": "leads"}).json()["items"]
        assert lead["leads"][0]["number"] == 45 and lead["leads"][0]["share"] == 0.4
        [planned] = client.get(BASE + "delivery/stories", params={"state": "planned"}).json()[
            "items"
        ]
        assert planned["documents"][0]["path"] == "docs/themes.md"
        health = measure(repo.sessions, "falcone")
        assert (health["stories_unsupported"], health["stories_delivered_open"]) == (1, 2)
        assert health["stories_with_leads"] == 1
    finally:
        client.close()


def test_delivered_stories_are_closed_by_a_batch_and_reopened_when_it_is_undone(
    tmp_path: Path,
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        made = plan(repo, data)
        repo.delivery.refresh("falcone")
        ready = {
            kind["kind"]: kind["ready"]
            for kind in client.get(BASE + "reorganize/batches").json()["kinds"]
        }
        assert ready["close"] == 2
        batch = post(client, "reorganize/batches", kind="close").json()
        path = "reorganize/batches/" + batch["id"]
        lines = client.get(BASE + path).json()
        assert lines["batch"]["sample"] == 2 and lines["batch"]["measured"]["rule"]
        assert {line["child"]["title"] for line in lines["items"]} == {"Receipts", "Exports"}
        assert all("closed as done" in line["reason"] for line in lines["items"])
        for line in lines["items"]:
            assert post(client, f"{path}/lines/{line['id']}", verdict="right").status_code == 200
        done = post(client, path + "/approve")
        assert done.status_code == 200 and done.json()["applied"] == 2
        with repo.sessions() as db:
            closed = {
                row.title
                for row in db.scalars(
                    select(ProjectUserStory).where(ProjectUserStory.closed_at.is_not(None))
                )
            }
            # Nothing is asked of GitHub: the Stories' issues were closed already.
            assert db.scalar(select(WorkspaceAction)) is None
        assert {"Receipts", "Exports"} <= closed and "Refunds" not in closed
        repo.delivery.refresh("falcone")
        assert made["Receipts"] not in states(repo)

        assert post(client, path + "/undo").status_code == 200
        with repo.sessions() as db:
            assert db.get_one(ProjectUserStory, made["Receipts"]).closed_at is None
            assert db.get_one(ProjectUserStory, made["Exports"]).closed_at is None
    finally:
        client.close()
