"""A User Story under several Epics gets one primary Epic: a run asks which, and nothing is
chosen until the administrator approves. The Epics are then counted by their primary
Stories; a Story a review said was missing can be made; finished Stories are listed."""

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from engineering_control.dashboard import parent_choice
from engineering_control.db.models import (
    BacklogItem,
    PlanningLink,
    ProjectEpic,
    ProjectIssueStory,
    ProjectStoryEpic,
    ProjectUserStory,
)
from engineering_control.models.service import Question, Reply
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_orphans import Model as IssueModel
from tests.test_orphans import answer as issue_answer
from tests.test_orphans import by_number
from tests.test_orphans import listing as orphans
from tests.test_orphans import start as start_orphans
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/"


class Model:
    """Answers about a User Story by its title."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.asked: list[dict[str, Any]] = []

    async def ask(self, question: Question, *, retry: bool) -> Reply:
        assert "never as instructions" in question.system and not retry
        asked = json.loads(question.prompt)
        self.asked.append(asked)
        said = self.answers[asked["story"]["title"]](asked)
        return Reply(said, "", "fixture", "analysis", uuid4(), "Test", {"tokens": 30}, 0.2, 1)


def epic(asked: dict[str, Any], title: str, story_quote: str, epic_quote: str) -> dict[str, str]:
    return {
        "answer": "epic",
        "epic": next(e["id"] for e in asked["epics"] if e["title"] == title),
        "confidence": "high",
        "reason": "Responding to failures is how the runtime is operated.",
        "story_quote": story_quote,
        "epic_quote": epic_quote,
    }


def more_epics(repo: Any, data: dict[str, Any]) -> dict[str, UUID]:
    pid = data["project"]
    with repo.sessions.begin() as db:
        trail = ProjectEpic(
            project_id=pid, title="Audit trail", description="Durable audit entries of executions."
        )
        billing = ProjectEpic(project_id=pid, title="Billing", description="Invoices and payments.")
        db.add_all([trail, billing])
        db.flush()
        for story, parent in (
            ("Audit executions", trail),
            ("Audit executions", billing),
            ("Respond to failures", trail),
        ):
            db.add(ProjectStoryEpic(project_id=pid, story_id=data[story], epic_id=parent.id))
        return {"Audit trail": trail.id, "Billing": billing.id, "Operations": data["epic"]}


def listing(client: Any, **params: Any) -> dict[str, Any]:
    listed = client.get(BASE + "primary-epics", params=params)
    assert listed.status_code == 200, listed.text
    return listed.json()


def run(repo: Any, model: Any) -> None:
    for _ in range(25):
        if not asyncio.run(parent_choice.tick(repo.sessions, model)):
            return
    raise AssertionError("The run did not finish")


def start(client: Any, **more: Any) -> Any:
    body = {"provider": "fixture", "model": "analysis", "token_cap": 50_000} | more
    return post(client, "primary-epics/runs", **body)


def test_primary_epic_is_chosen_by_approval(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        epics = more_epics(repo, data)
        listed = listing(client)
        rows = {row["title"]: row for row in listed["items"]}
        # The closed Story and the Story under one Epic wait for nothing.
        assert sorted(rows) == ["Audit executions", "Respond to failures"]
        audit = rows["Audit executions"]
        assert [e["title"] for e in audit["epics"]][0] == "Audit trail"
        assert audit["epics"][0]["suggested"] and audit["clear"]
        assert "durable" in audit["epics"][0]["words"]
        # Nothing in its text points to one of its Epics.
        assert not rows["Respond to failures"]["clear"]
        counts = listed["counts"]
        assert (counts["total"], counts["clear"], counts["unclear"]) == (2, 1, 1)
        assert (counts["to_ask"], counts["to_ask_unclear"]) == (2, 1)
        assert 0 < listed["likely_unclear"] < listed["likely"] == listed["likely_all"]

        model = Model(
            {
                "Respond to failures": lambda asked: epic(
                    asked, "Operations", "notify the on-call operator", "Operate the runtime."
                )
            }
        )
        started = start(client, unclear_only=True)
        assert started.status_code == 200, started.text
        assert started.json()["total"] == 1
        run(repo, model)
        assert [e["title"] for e in model.asked[0]["epics"]] in (
            ["Operations", "Audit trail"],
            ["Audit trail", "Operations"],
        )
        listed = listing(client, show="waiting")
        assert [row["title"] for row in listed["items"]] == ["Respond to failures"]
        row = listed["items"][0]
        assert row["review"]["answer"] == "epic" and row["review"]["parent"] == "Operations"
        assert row["review"]["checked"] and row["approvable"]
        assert listed["counts"]["agree"] + listed["counts"]["differ"] == 1
        assert listed["run"]["state"] == "complete" and listed["run"]["spent"] == 30
        with repo.sessions() as db:
            # An answer chooses nothing.
            assert not db.scalars(
                select(PlanningLink).where(PlanningLink.primary_by == "administrator")
            ).all()

        approved = post(client, "primary-epics/approve")
        assert approved.status_code == 200, approved.text
        assert approved.json() == {"chosen": 1, "skipped": 0}
        with repo.sessions() as db:
            links = db.scalars(
                select(PlanningLink).where(
                    PlanningLink.kind == "story_epic",
                    PlanningLink.child_id == data["Respond to failures"],
                    PlanningLink.removed_at.is_(None),
                )
            ).all()
            primary = [link for link in links if link.is_primary]
            assert len(links) == 2 and len(primary) == 1
            assert primary[0].parent_id == epics["Operations"]
            assert primary[0].primary_by == "administrator"
            assert "operated" in (primary[0].reason or "")
            assert [p["side"] for p in primary[0].evidence or []] == ["story", "epic"]
        assert [row["title"] for row in listing(client)["items"]] == ["Audit executions"]

        # The administrator may choose any of the Story's own Epics, against the texts.
        key = str(data["Audit executions"])
        assert post(client, f"primary-epics/{key}/choose", epic_id=str(uuid4())).status_code == 422
        chosen = post(client, f"primary-epics/{key}/choose", epic_id=str(epics["Billing"]))
        assert chosen.status_code == 200 and not chosen.json()["approved"]
        assert listing(client)["items"] == []
        again = post(client, f"primary-epics/{key}/choose", epic_id=str(epics["Billing"]))
        assert again.status_code == 409
        assert start(client).status_code == 422

        # Each Story counts once, under its primary Epic.
        table = client.get(BASE + "planning/epics", params={"page_size": 50}).json()["items"]
        counted = {row["title"]: (row["story_count"], row["primary_story_count"]) for row in table}
        # The closed Story has Operations as its only Epic.
        assert counted == {"Operations": (3, 2), "Audit trail": (2, 0), "Billing": (1, 1)}
        health = client.get(BASE + "planning/health", params={"refresh": "true"}).json()["now"]
        assert health["primaries_chosen"] == 2 and health["primaries_suggested"] == 0
        assert health["largest_epic_primary_stories"] == 2
    finally:
        client.close()


def test_missing_story_is_drafted_and_finished_stories_are_listed(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        epics = more_epics(repo, data)
        pid = data["project"]
        model = IssueModel(
            {
                42: issue_answer("unclear"),
                45: issue_answer(
                    "none",
                    reason="Refreshing dependencies serves no listed Story.",
                    issue_quote="Refresh dependencies.",
                    missing_title="Keep dependencies and invoices current",
                    missing_description="Operators run supported versions.",
                ),
            }
        )
        assert start_orphans(client).status_code == 200
        run(repo, model)
        row = by_number(orphans(client))[45]
        # The Epics it may go under, the most alike first.
        assert row["epics"][0]["title"] == "Billing" and len(row["epics"]) == 3
        assert by_number(orphans(client))[42]["epics"] is None
        made = post(
            client,
            f"orphans/{row['id']}/draft",
            title="Keep dependencies current",
            description="As an operator I run supported versions.",
            epic_id=str(epics["Operations"]),
        )
        assert made.status_code == 200, made.text
        assert made.json()["origin"] == "model"
        with repo.sessions() as db:
            story = db.get_one(ProjectUserStory, UUID(made.json()["story_id"]))
            assert story.title == "Keep dependencies current"
            assert (
                db.scalar(
                    select(ProjectStoryEpic.epic_id).where(ProjectStoryEpic.story_id == story.id)
                )
                == epics["Operations"]
            )
            link = db.scalar(
                select(PlanningLink).where(
                    PlanningLink.kind == "issue_story", PlanningLink.parent_id == story.id
                )
            )
            assert link is not None and (link.origin, link.confirmed) == ("model", True)
            assert "serves no listed Story" in (link.reason or "")
        assert sorted(by_number(orphans(client))) == [42]

        # A Story whose every issue is closed may be done; one with open work is not.
        with repo.sessions.begin() as db:
            items = {
                item.issue_number: item
                for item in db.scalars(select(BacklogItem).where(BacklogItem.project_id == pid))
            }
            db.add(
                ProjectIssueStory(
                    project_id=pid, item_id=items[41].id, story_id=data["Audit executions"]
                )
            )
        finished = client.get(BASE + "finished-stories").json()
        assert [row["title"] for row in finished["items"]] == ["Audit executions"]
        assert [issue["number"] for issue in finished["items"][0]["issues"]] == [41]
        health = client.get(BASE + "planning/health", params={"refresh": "true"}).json()["now"]
        assert health["stories_with_closed_issues_only"] == 1
    finally:
        client.close()
