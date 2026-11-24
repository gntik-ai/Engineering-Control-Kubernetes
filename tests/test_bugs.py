"""A bug stands apart from the plan: it has no User Story and no Epic. It is known by the
administrator's word, its GitHub type, a bug label or its title; nothing links it to a
Story, no review looks for one, and a link it still carries is taken off on approval."""

from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

from engineering_control.dashboard.issue_types import is_bug
from engineering_control.dashboard.workspace_issues import add_story_links
from engineering_control.db.models import BacklogItem, GoldPair, PlanningLink, ProjectIssueStory
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_primary_epics import more_epics
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/"


def issue(repo: Any, number: int) -> BacklogItem:
    with repo.sessions() as db:
        return db.scalars(
            select(BacklogItem).where(
                BacklogItem.issue_number == number, BacklogItem.repository != "other/elsewhere"
            )
        ).one()


def retitle(repo: Any, number: int, title: str) -> UUID:
    with repo.sessions.begin() as db:
        row = db.get(BacklogItem, issue(repo, number).id)
        row.title = title
        return row.id


def numbers(client: Any, bugs: str) -> set[int]:
    listed = client.get(BASE + "planning/issues", params={"bugs": bugs, "page_size": 100})
    assert listed.status_code == 200, listed.text
    return {row["number"] for row in listed.json()["items"]}


def test_a_title_that_calls_the_issue_a_bug_makes_it_one(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        assert numbers(client, "only") == set()
        counts = client.get(BASE + "planning/counts").json()
        assert counts["bugs"] == 0
        before = counts["issues"]
        for number, title in (
            (42, "bug(console): operators cannot see failed runs"),
            (45, "[Bug] dependencies are stale"),
        ):
            retitle(repo, number, title)
        # A word about fixing is no claim that the issue is a bug.
        retitle(repo, 41, "Fix the ledger: append invocations")
        assert numbers(client, "only") == {42, 45}
        assert not {42, 45} & numbers(client, "without")
        counts = client.get(BASE + "planning/counts").json()
        assert (counts["bugs"], counts["issues"]) == (2, before - 2)
        # No review looks for the User Story of a bug.
        orphans = client.get(BASE + "orphans").json()
        assert all(row["number"] not in (42, 45) for row in orphans["items"])
        # The administrator's word comes first: said to be an enhancement, it is no bug.
        story = str(data["Respond to failures"])
        key = issue(repo, 42).id
        said = post(
            client,
            f"planning/issues/{key}/stories",
            story_ids=[story],
            execution_type="enhancement",
        )
        assert said.status_code == 200, said.text
        assert numbers(client, "only") == {45}
    finally:
        client.close()


def test_a_bug_is_given_no_user_story(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        story = data["Respond to failures"]
        key = retitle(repo, 42, "Bug: operators cannot see failed runs")
        refused = post(client, f"planning/issues/{key}/stories", story_ids=[str(story)])
        assert refused.status_code == 422 and "no User Story" in refused.text
        named = post(
            client, f"planning/issues/{key}/stories", story_ids=[str(story)], execution_type="bug"
        )
        assert named.status_code == 422
        # What the platform finds by itself is not linked either.
        with repo.sessions.begin() as db:
            assert is_bug(db, data["project"], key)
            assert add_story_links(db, data["project"], key, {story}) == 0
            other = issue(repo, 45).id
            assert add_story_links(db, data["project"], other, {story}) == 1
        with repo.sessions() as db:
            assert not db.scalars(
                select(ProjectIssueStory).where(ProjectIssueStory.item_id == key)
            ).all()
        # A bug may be saved with no Story at all.
        alone = post(client, f"planning/issues/{key}/stories", story_ids=[])
        assert alone.status_code == 200, alone.text
    finally:
        client.close()


def test_a_bug_still_linked_is_listed_and_unlinked_on_approval(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        # Issue 44 was linked by the administrator before its title called it a bug.
        key = retitle(repo, 44, "bug(audit): processing stops")
        listed = client.get(BASE + "bugs/linked")
        assert listed.status_code == 200, listed.text
        found = listed.json()
        assert (found["bugs"], found["links"]) == (1, 1)
        row = found["items"][0]
        assert (row["number"], row["state"], row["confirmed"]) == (44, "open", False)
        assert row["stories"][0]["id"] == str(data["Respond to failures"])
        health = client.get(BASE + "planning/health").json()["now"]
        assert (health["bugs"], health["bugs_linked"]) == (1, 1)
        # Reading changed nothing.
        with repo.sessions() as db:
            assert db.scalars(
                select(ProjectIssueStory).where(ProjectIssueStory.item_id == key)
            ).all()

        done = post(client, "bugs/unlink")
        assert done.status_code == 200, done.text
        assert done.json() == {"unlinked": 1, "links": 1}
        with repo.sessions() as db:
            assert not db.scalars(
                select(ProjectIssueStory).where(ProjectIssueStory.item_id == key)
            ).all()
            # The link's record is closed, not lost; the issue's type was not touched.
            record = db.scalars(select(PlanningLink).where(PlanningLink.child_id == key)).one()
            assert record.removed_at is not None
            item = db.get(BacklogItem, key)
            assert item is not None and not item.type_confirmed
        assert client.get(BASE + "bugs/linked").json()["bugs"] == 0
        assert post(client, "bugs/unlink").json() == {"unlinked": 0, "links": 0}
    finally:
        client.close()


def test_a_bug_is_left_out_of_the_pairs_to_judge(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        more_epics(repo, data)
        drawn = post(client, "labels/draw").json()
        with repo.sessions() as db:
            pair = db.scalars(select(GoldPair).where(GoldPair.kind == "issue_story")).first()
            assert pair is not None
            pair_id, child = pair.id, pair.child_id
            number = db.get(BacklogItem, child).issue_number  # type: ignore[union-attr]
        # The issue of a drawn pair turns out to be a bug: the pair is set aside.
        retitle(repo, number, "Bug: this was never part of a User Story")
        report = client.get(BASE + "signals").json()["labels"]
        assert report["set_aside"] == 1 and report["drawn"] == drawn["drawn"] - 1
        seen = set()
        after = None
        for _ in range(drawn["drawn"] + 1):
            shown = client.get(BASE + "labels/next", params={"after": after} if after else {})
            shown_pair = shown.json()["pair"]
            if shown_pair is None or shown_pair["id"] in seen:
                break
            seen.add(shown_pair["id"])
            after = shown_pair["id"]
            assert post(client, f"labels/{after}", label="unsure").status_code == 200
        assert str(pair_id) not in seen
        refused = post(client, f"labels/{pair_id}", label="yes")
        assert refused.status_code == 422 and "bug" in refused.text
        # Drawing again never draws a pair for a bug.
        post(client, "labels/draw")
        with repo.sessions() as db:
            pairs = db.scalars(select(GoldPair).where(GoldPair.kind == "issue_story")).all()
            assert [p.id for p in pairs if p.child_id == child] == [pair_id]
    finally:
        client.close()
