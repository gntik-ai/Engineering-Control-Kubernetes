"""Each link between planning records says who made it and why, and each child has one
primary parent. The records follow the link tables, whoever writes those."""

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from engineering_control.dashboard.planning_health import measure
from engineering_control.db.models import (
    BacklogItem,
    PlanningLink,
    ProjectEpic,
    ProjectIssueStory,
    ProjectStoryEpic,
    ProjectUserStory,
    StoryIssueCoverage,
    WorkspaceCandidate,
)
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/planning/"


def _items(db: Any, pid: UUID) -> dict[int, BacklogItem]:
    return {
        item.issue_number: item
        for item in db.scalars(select(BacklogItem).where(BacklogItem.project_id == pid))
    }


def _records(repo: Any, kind: str, child: UUID, *, gone: bool = False) -> list[PlanningLink]:
    with repo.sessions() as db:
        query = select(PlanningLink).where(
            PlanningLink.kind == kind, PlanningLink.child_id == child
        )
        if not gone:
            query = query.where(PlanningLink.removed_at.is_(None))
        return list(db.scalars(query.order_by(PlanningLink.created_at, PlanningLink.id)))


def _listed(client: Any, kind: str, key: str = "state", view: str = "open") -> dict[str, Any]:
    listed = client.get(BASE + kind, params={key: view, "page_size": 100})
    assert listed.status_code == 200, listed.text
    return {row["title"]: row for row in listed.json()["items"]}


def test_a_link_says_who_made_it_from_what_is_stored(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        pid, audit, respond = data["project"], data["Audit executions"], data["Respond to failures"]
        with repo.sessions.begin() as db:
            items = _items(db, pid)
            # The Story was imported from GitHub issue 77, and an issue's text names it.
            db.add(
                WorkspaceCandidate(
                    project_id=pid,
                    kind="stories",
                    identity="issue:77",
                    number=77,
                    title="Audit executions",
                    decision="approved",
                    local_id=audit,
                    parents=[],
                )
            )
            items[41].description = "Save the ledger.\n\n- Story: #77\n"
            db.add(ProjectIssueStory(project_id=pid, item_id=items[41].id, story_id=audit))
            # A review matched another issue with a Story, and said why.
            db.add(
                StoryIssueCoverage(
                    project_id=pid,
                    story_id=respond,
                    revision="r",
                    checked=1,
                    matches=[
                        {
                            "id": str(items[42].id),
                            "number": 42,
                            "full": False,
                            "evidence": "The issue shows failed runs;  notification is left out.",
                        }
                    ],
                )
            )
            db.add(ProjectIssueStory(project_id=pid, item_id=items[42].id, story_id=respond))
            # Nothing stored explains a third.
            db.add(ProjectIssueStory(project_id=pid, item_id=items[45].id, story_id=audit))
            ids = {number: item.id for number, item in items.items()}
        rows = {
            **_listed(client, "issues", "issue_state"),
            **_listed(client, "issues", "issue_state", "closed"),
        }

        [declared] = rows["Append the invocation ledger"]["parent_links"]
        assert (declared["origin"], declared["confirmed"]) == ("declared", False)
        assert declared["evidence"] == [{"side": "issue", "text": "- Story: #77"}]
        assert (declared["primary"], declared["primary_by"]) == (True, "only")

        [matched] = rows["Record error events"]["parent_links"]
        assert (matched["origin"], matched["strength"]) == ("model", "partial")
        assert matched["reason"] == "The issue shows failed runs; notification is left out."
        assert matched["parent"] == str(respond)

        # The administrator had chosen this issue's Stories by hand.
        [manual] = rows["Locked issue"]["parent_links"]
        assert (manual["origin"], manual["confirmed"]) == ("administrator", True)

        [unknown] = rows["Unrelated work"]["parent_links"]
        assert (unknown["origin"], unknown["reason"], unknown["evidence"]) == ("unknown", None, [])
        # Reading again records nothing twice.
        _listed(client, "issues", "issue_state")
        assert len(_records(repo, "issue_story", ids[45])) == 1

        # A Story proposed by a review and approved by the administrator, with its Epic.
        with repo.sessions.begin() as db:
            story = ProjectUserStory(project_id=pid, title="Export reports", description="Export.")
            db.add(story)
            db.flush()
            db.add(ProjectStoryEpic(project_id=pid, story_id=story.id, epic_id=data["epic"]))
            db.add(
                WorkspaceCandidate(
                    project_id=pid,
                    kind="stories",
                    identity="gap:export",
                    title="Export reports",
                    decision="approved",
                    local_id=story.id,
                    parents=[str(data["epic"])],
                    review_reason="The description promises exports and no Story covers them.",
                )
            )
        stories = _listed(client, "stories")
        [proposed] = stories["Export reports"]["parent_links"]
        assert (proposed["origin"], proposed["confirmed"]) == ("model", True)
        assert proposed["reason"].startswith("The description promises exports")
        # The Stories that were there from the start: nothing says who put them under the Epic.
        assert [link["origin"] for link in stories["Audit executions"]["parent_links"]] == [
            "unknown"
        ]
        # An Epic has no parent.
        assert "parent_links" not in _listed(client, "epics")["Operations"]

        health = measure(repo.sessions, "falcone")
        assert (health["links_declared"], health["links_by_administrator"]) == (1, 1)
        assert (health["links_by_model"], health["links_unknown"]) == (2, 4)
        # Declared, chosen by the administrator, and two with a written reason.
        assert (health["links_with_reason"], health["links_without_reason"]) == (4, 4)
    finally:
        client.close()


def test_what_the_administrator_links_is_theirs_and_what_goes_is_kept_as_history(
    tmp_path: Path,
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        pid, audit, respond = data["project"], data["Audit executions"], data["Respond to failures"]
        with repo.sessions.begin() as db:
            issue = _items(db, pid)[45].id
            db.add(ProjectIssueStory(project_id=pid, item_id=issue, story_id=audit))
        path = f"issues/{issue}/stories"
        # A second Story is added by hand: the first keeps what was known of it.
        saved = post(client, "planning/" + path, story_ids=[str(audit), str(respond)])
        assert saved.status_code == 200, saved.text
        first, second = _records(repo, "issue_story", issue)
        assert (first.parent_id, first.origin, first.confirmed) == (audit, "unknown", False)
        assert (second.parent_id, second.origin, second.confirmed) == (
            respond,
            "administrator",
            True,
        )
        # It was the only parent and is one of two now: it stays primary, as a suggestion.
        assert (first.is_primary, first.primary_by) == (True, "suggested")
        assert (second.is_primary, second.primary_by) == (False, None)

        # The administrator chooses the primary one, among the record's own parents.
        chosen = post(client, f"planning/issues/{issue}/primary", parent_id=str(respond))
        assert chosen.status_code == 200, chosen.text
        assert [(link["parent"], link["primary"]) for link in chosen.json()["parent_links"]] == [
            (str(respond), True),
            (str(audit), False),
        ]
        first, second = _records(repo, "issue_story", issue)
        assert (second.is_primary, second.primary_by, first.is_primary) == (
            True,
            "administrator",
            False,
        )
        other = post(client, f"planning/issues/{issue}/primary", parent_id=str(data["epic"]))
        assert other.status_code == 422 and "own parents" in other.json()["detail"]
        assert (
            post(client, f"planning/epics/{data['epic']}/primary", parent_id=str(audit)).status_code
            == 422
        )
        assert (
            post(client, f"planning/issues/{uuid4()}/primary", parent_id=str(audit)).status_code
            == 404
        )

        # The primary Story is taken away: its record is closed, and the one left is primary.
        saved = post(client, "planning/" + path, story_ids=[str(audit)])
        assert saved.status_code == 200, saved.text
        [left] = _records(repo, "issue_story", issue)
        assert (left.parent_id, left.is_primary, left.primary_by) == (audit, True, "only")
        history = client.get(BASE + f"issues/{issue}/links").json()["items"]
        assert [(row["title"], row["removed_at"] is None) for row in history] == [
            ("Audit executions", True),
            ("Respond to failures", False),
        ]
        assert history[1]["origin"] == "administrator" and history[1]["primary"] is False
        # Linked again, it is a new link with its own record; the old one stays as history.
        post(client, "planning/" + path, story_ids=[str(audit), str(respond)])
        assert len(_records(repo, "issue_story", issue)) == 2
        assert len(_records(repo, "issue_story", issue, gone=True)) == 3
        assert client.get(BASE + f"epics/{data['epic']}/links").status_code == 422
    finally:
        client.close()


def test_of_several_parents_the_one_most_alike_is_suggested_as_primary(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        pid = data["project"]
        with repo.sessions.begin() as db:
            billing = ProjectEpic(
                project_id=pid, title="Billing", description="Invoices and payments."
            )
            audit = ProjectEpic(
                project_id=pid,
                title="Audit trail",
                description="Durable audit entries of executions.",
            )
            db.add_all([billing, audit])
            db.flush()
            story = data["Audit executions"]
            db.add_all(
                [
                    ProjectStoryEpic(project_id=pid, story_id=story, epic_id=billing.id),
                    ProjectStoryEpic(project_id=pid, story_id=story, epic_id=audit.id),
                ]
            )
            wanted = audit.id
        links = _listed(client, "stories")["Audit executions"]["parent_links"]
        assert len(links) == 3
        # The primary one is listed first: the Epic whose words the Story shares.
        assert (links[0]["parent"], links[0]["primary"], links[0]["primary_by"]) == (
            str(wanted),
            True,
            "suggested",
        )
        assert [link["primary"] for link in links[1:]] == [False, False]
        health = measure(repo.sessions, "falcone")
        assert health["primaries_suggested"] == 1 and health["stories_under_several_epics"] == 1

        # A Story's Epics are edited by the administrator: what is added is theirs.
        with repo.sessions() as db:
            title = db.get_one(ProjectUserStory, story).title
        saved = post(
            client,
            f"planning/stories/{story}/update",
            title=title,
            description="Keep a durable audit entry for each execution.",
            epic_ids=[str(wanted)],
        )
        assert saved.status_code == 200, saved.text
        [kept] = _records(repo, "story_epic", story)
        assert (kept.parent_id, kept.is_primary, kept.primary_by) == (wanted, True, "only")
        assert len(_records(repo, "story_epic", story, gone=True)) == 3
    finally:
        client.close()
