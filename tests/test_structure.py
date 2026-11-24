"""The structure of the plan: issues that fit no User Story are grouped into proposed
Stories, an Epic that holds too many Stories is proposed to be split, and nothing is
made before the administrator approves it, in their own words."""

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from engineering_control.dashboard import ranking
from engineering_control.dashboard.structure import groups, parts, plain_title, story_sized
from engineering_control.dashboard.workspace_issues import import_issues
from engineering_control.db.models import (
    BacklogItem,
    PlanningLink,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
)
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_delivery import vector
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/"


def unit(*values: float) -> Any:
    import numpy

    made = numpy.array(values, dtype=numpy.float32)
    return made / numpy.linalg.norm(made)


def test_a_title_loses_its_labels_and_groups_are_the_same_each_time() -> None:
    assert plain_title("Change request: add a quota per tenant") == "Add a quota per tenant"
    assert plain_title("enhancement(console): [draft] dark theme") == "Dark theme"
    assert plain_title("   ") == "User Story"
    keys = [uuid4() for _ in range(6)]
    vectors = {
        ("issue", keys[0]): unit(1, 0.05, 0),
        ("issue", keys[1]): unit(1, 0, 0.05),
        ("issue", keys[2]): unit(1, 0.02, 0.02),
        ("issue", keys[3]): unit(0, 1, 0),
        ("issue", keys[4]): unit(0, 0, 1),
    }
    # Three lie together; two stand alone; one has no vector and is left out.
    [found] = groups(keys, vectors, "issue", 0.9)
    assert set(found) == set(keys[:3])
    assert groups(keys, vectors, "issue", 0.9) == [found]
    assert groups(keys[3:], vectors, "issue", 0.9) == []

    # A group too large for one Story is looked at more strictly until its parts are
    # small enough: here two tight halves of a loose whole.
    many = [uuid4() for _ in range(20)]
    loose = {
        ("issue", key): unit(1, 0.01 * place, 0) if place < 10 else unit(1, 0.01 * place, 0.5)
        for place, key in enumerate(many)
    }
    assert [len(group) for group in groups(many, loose, "issue", 0.85)] == [20]
    halves = story_sized(many, loose, "issue", 0.85)
    assert sorted(map(len, halves)) == [10, 10]
    assert {frozenset(group) for group in halves} == {frozenset(many[:10]), frozenset(many[10:])}

    stories = [uuid4() for _ in range(14)]
    apart = {
        ("story", key): unit(1, 0.02 * place, 0) if place < 7 else unit(0, 0.02 * place, 1)
        for place, key in enumerate(stories)
    }
    split = parts(stories, apart)
    assert sorted(map(len, split)) == [7, 7]
    assert {frozenset(group) for group in split} == {
        frozenset(stories[:7]),
        frozenset(stories[7:]),
    }
    # Too few Stories to make two parts of five: left together.
    assert parts(stories[:8], apart) == [sorted(stories[:8], key=str)]


def issues(repo: Any, pid: UUID) -> dict[int, UUID]:
    with repo.sessions() as db:
        return {
            item.issue_number: item.id
            for item in db.scalars(select(BacklogItem).where(BacklogItem.project_id == pid))
            if item.issue_number
        }


def test_issues_that_fit_no_story_become_a_story_on_approval(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        pid = data["project"]
        with repo.sessions.begin() as db:
            project = db.get_one(ProjectRecord, pid)
            import_issues(
                db,
                project,
                [
                    {
                        "number": number,
                        "state": state,
                        "title": title,
                        "body": "",
                        "labels": ["task"],
                    }
                    for number, state, title in (
                        (71, "open", "Change request: export invoices as PDF"),
                        (72, "open", "Export invoices to the accounting system"),
                        (73, "closed", "enhancement(billing): invoice export job"),
                        (81, "closed", "Old migration, step one"),
                        (82, "closed", "Old migration, step two"),
                        (83, "closed", "Old migration, step three"),
                    )
                ],
            )
        made = issues(repo, pid)
        with repo.sessions.begin() as db:
            for place, number in enumerate((71, 72, 73)):
                ranking.keep_vector(
                    db, pid, "issue", made[number], "m", "d", vector(1, 0.03 * place, 0, 0)
                )
            for place, number in enumerate((81, 82, 83)):
                ranking.keep_vector(
                    db, pid, "issue", made[number], "m", "d", vector(0, 1, 0.03 * place, 0)
                )
            ranking.keep_vector(db, pid, "epic", data["epic"], "m", "d", vector(1, 0.2, 0, 0))

        found = client.get(BASE + "structure")
        assert found.status_code == 200, found.text
        plan = found.json()
        # The three invoice issues are proposed as one Story; the three closed ones are
        # work that was done, and nothing is planned from them.
        [proposal] = plan["stories"]
        assert proposal["title"] == "Export invoices as PDF" and proposal["open"] == 2
        assert {issue["title"][:3] for issue in proposal["issues"]} == {"#71", "#72", "#73"}
        assert proposal["epics"][0]["title"] == "Operations"
        assert plan["closed_groups"] == 1
        assert {epic["title"] for epic in plan["epics"]} == {"Operations"}
        # Reading made nothing.
        with repo.sessions() as db:
            assert not db.scalars(
                select(ProjectUserStory).where(ProjectUserStory.title.like("%invoice%"))
            ).all()

        done = post(
            client,
            "structure/stories/" + proposal["id"],
            title="Export invoices",
            description="A bookkeeper exports the invoices of a period.",
            epic_id=str(data["epic"]),
        )
        assert done.status_code == 200, done.text
        assert (done.json()["linked"], done.json()["skipped"]) == (3, 0)
        story = UUID(done.json()["story_id"])
        with repo.sessions() as db:
            assert db.get_one(ProjectUserStory, story).title == "Export invoices"
            linked = db.scalars(
                select(ProjectIssueStory.item_id).where(ProjectIssueStory.story_id == story)
            ).all()
            assert set(linked) == {made[71], made[72], made[73]}
            notes = db.scalars(
                select(PlanningLink).where(
                    PlanningLink.parent_id == story, PlanningLink.kind == "issue_story"
                )
            ).all()
            assert {note.origin for note in notes} == {"scorer"} and all(n.reason for n in notes)
        after = client.get(BASE + "structure").json()
        assert not after["stories"] and after["approved"] == 1
        # The same proposal cannot be approved twice.
        again = post(
            client,
            "structure/stories/" + proposal["id"],
            title="Again",
            description="",
            epic_id=str(data["epic"]),
        )
        assert again.status_code == 409
    finally:
        client.close()


def test_a_proposal_set_aside_is_not_made_again(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        pid = data["project"]
        with repo.sessions.begin() as db:
            for place, number in enumerate((42, 45)):
                item = db.scalars(
                    select(BacklogItem).where(
                        BacklogItem.project_id == pid, BacklogItem.issue_number == number
                    )
                ).one()
                ranking.keep_vector(
                    db, pid, "issue", item.id, "m", "d", vector(1, 0.02 * place, 0, 0)
                )
            other = db.scalars(
                select(BacklogItem).where(
                    BacklogItem.project_id == pid, BacklogItem.issue_number == 41
                )
            ).one()
            ranking.keep_vector(db, pid, "issue", other.id, "m", "d", vector(1, 0.05, 0, 0))
        [proposal] = client.get(BASE + "structure").json()["stories"]
        assert post(client, f"structure/{proposal['id']}/dismiss").status_code == 200
        after = client.get(BASE + "structure").json()
        assert not after["stories"] and after["dismissed"] == 1
        assert post(client, "structure/" + "0" * 32 + "/dismiss").status_code == 404
    finally:
        client.close()


def test_an_epic_that_holds_too_many_stories_is_split_on_approval(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        pid = data["project"]
        made: list[UUID] = []
        with repo.sessions.begin() as db:
            broad = ProjectEpic(project_id=pid, title="Platform", description="Everything.")
            db.add(broad)
            db.flush()
            for place in range(24):
                topic = "queue" if place < 12 else "billing"
                story = ProjectUserStory(
                    project_id=pid, title=f"{topic} story {place}", description=f"About {topic}."
                )
                db.add(story)
                db.flush()
                db.add(ProjectStoryEpic(project_id=pid, story_id=story.id, epic_id=broad.id))
                ranking.keep_vector(
                    db,
                    pid,
                    "story",
                    story.id,
                    "m",
                    "d",
                    vector(1, 0.01 * place, 0, 0) if place < 12 else vector(0, 0.01 * place, 1, 0),
                )
                made.append(story.id)
            key = broad.id
        plan = client.get(BASE + "structure").json()
        platform = next(epic for epic in plan["epics"] if epic["title"] == "Platform")
        assert platform["broad"] and platform["primary"] == 24 and platform["share"] > 0.1
        [split] = plan["splits"]
        assert split["epic"]["title"] == "Platform" and len(split["parts"]) == 2
        stays, leaves = split["parts"]
        assert stays["stays"] and not leaves["stays"]
        assert len(stays["stories"]) == len(leaves["stories"]) == 12
        assert leaves["title"].startswith("Platform: ")
        # Each new Epic needs a title.
        assert post(client, "structure/splits/" + split["id"], titles=[" "]).status_code == 422

        done = post(client, "structure/splits/" + split["id"], titles=["Billing"])
        assert done.status_code == 200, done.text
        [created] = done.json()["epics"]
        epic = UUID(created["epic"])
        moved = {UUID(story["id"]) for story in leaves["stories"]}
        assert set(map(UUID, created["stories"])) == moved
        with repo.sessions() as db:
            assert db.get_one(ProjectEpic, epic).title == "Billing"
            for story in moved:
                links = db.scalars(
                    select(PlanningLink).where(
                        PlanningLink.child_id == story, PlanningLink.removed_at.is_(None)
                    )
                ).all()
                # The Story stays linked to the Epic it came from; the new one is primary.
                assert {link.parent_id for link in links} == {key, epic}
                chosen = next(link for link in links if link.is_primary)
                assert (chosen.parent_id, chosen.primary_by) == (epic, "administrator")
        after = client.get(BASE + "structure").json()
        assert not after["splits"]
        assert {e["title"]: e["primary"] for e in after["epics"]}["Platform"] == 12
    finally:
        client.close()
