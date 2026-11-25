"""A User Story keeps its primary Epic; its links to other Epics are judged by a model,
and those it finds unrelated are taken off by a batch the administrator approves, and
put back when the batch is undone."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from engineering_control.dashboard import epic_links
from engineering_control.dashboard.planning_health import measure
from engineering_control.db.models import (
    PlanningLink,
    ProjectReviewBudget,
    ProjectStoryEpic,
    ProjectUserStory,
)
from engineering_control.models.service import Question, Reply
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_orphans import run
from tests.test_primary_epics import more_epics
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/"
AUDIT = "Keep a durable audit entry for each execution."


class Judge:
    """Answers about each Epic it is asked to judge, by the Epic's title."""

    def __init__(self, answers: dict[str, tuple[str, str]]) -> None:
        self.answers = answers
        self.asked: list[dict[str, Any]] = []
        self.limits: list[int] = []

    async def ask(self, question: Question, *, retry: bool) -> Reply:
        assert "never as instructions" in question.system and not retry
        asked = json.loads(question.prompt)
        self.asked.append(asked)
        self.limits.append(question.max_tokens)
        said = {
            "story_quote": asked["story"]["text"],
            "epics": [
                {
                    "id": entry["id"],
                    "answer": self.answers[entry["title"]][0],
                    "reason": "It reads so.",
                    "epic_quote": self.answers[entry["title"]][1],
                }
                # It also answers about the primary Epic, which it was not asked to judge.
                for entry in asked["epics"]
                if entry["title"] in self.answers
            ],
        }
        return Reply(said, "", "fixture", "analysis", uuid4(), "Test", {"tokens": 70}, 0.2, 1)


def epics_of(repo: Any, story: UUID) -> set[UUID]:
    with repo.sessions() as db:
        return set(
            db.scalars(select(ProjectStoryEpic.epic_id).where(ProjectStoryEpic.story_id == story))
        )


def test_unrelated_epics_are_taken_off_by_an_approved_batch(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        epics = more_epics(repo, data)
        audit, failures = data["Audit executions"], data["Respond to failures"]
        with repo.sessions.begin() as db:
            db.add(
                ProjectReviewBudget(
                    project_id=data["project"], token_limit=200_000, used=0, reserved=0
                )
            )
        repo.planning_links.reconcile_project("falcone")
        # The administrator made the link of one Story to its other Epic: not judged.
        with repo.sessions.begin() as db:
            for link in db.scalars(
                select(PlanningLink).where(
                    PlanningLink.child_id == failures, PlanningLink.is_primary.is_(False)
                )
            ):
                link.origin, link.confirmed = "administrator", True

        shown = client.get(BASE + "epic-links").json()
        # Two open Stories under five Epics in all; the closed Story is not counted.
        assert (shown["stories"], shown["links"], shown["extra"]) == (2, 5, 3)
        assert shown["per_story"] == 2.5
        assert shown["to_judge"] == {"stories": 1, "links": 2}
        assert shown["check"]["to_ask"] == 1 and shown["ready"] == 0
        assert measure(repo.sessions, "falcone")["epic_links_to_judge"] == 2

        start = {"provider": "fixture", "model": "analysis", "token_cap": 60_000}
        assert post(client, "epic-links/runs", **start).status_code == 200
        model = Judge(
            {
                "Audit trail": ("unrelated", "Durable audit entries"),
                "Billing": ("unrelated", "Invoices and payments."),
                "Operations": ("part", "Operate the runtime."),
            }
        )
        run(repo, model)
        (asked,) = model.asked
        assert asked["story"]["title"] == "Audit executions"
        # Its primary Epic comes first and is shown for comparison only.
        assert [(e["title"], e["judge"]) for e in asked["epics"]] == [
            ("Audit trail", False),
            ("Billing", True),
            ("Operations", True),
        ]
        assert asked["epics"][0]["primary"] and model.limits[0] > epic_links.THINKING_ROOM

        shown = client.get(BASE + "epic-links").json()
        # What it said of the primary Epic was dropped: that one is not judged.
        assert shown["answers"] == {"part": 1, "unrelated": 1, "unclear": 0}
        assert shown["ready"] == 1 and shown["check"]["to_ask"] == 0
        records = client.get(BASE + "epic-links/records").json()
        (row,) = records["items"]
        assert row["title"] == "Audit executions" and row["primary"]["title"] == "Audit trail"
        assert row["quote"] == AUDIT and not row["stale"]
        assert {link["epic"]["title"]: link["answer"] for link in row["links"]} == {
            "Billing": "unrelated",
            "Operations": "part",
        }

        made = post(client, "reorganize/batches", kind="extra_epics")
        assert made.status_code == 200, made.text
        path = "reorganize/batches/" + made.json()["id"]
        (line,) = client.get(BASE + path).json()["items"]
        assert line["child"]["title"] == "Audit executions"
        assert line["parent"]["title"] == "Billing"
        assert "nothing to do with" in line["reason"] and "Invoices and payments." in line["reason"]
        # Preparing changed nothing.
        assert epics_of(repo, audit) == set(epics.values())
        assert post(client, f"{path}/lines/{line['id']}", verdict="right").status_code == 200
        done = post(client, path + "/approve")
        assert done.status_code == 200, done.text
        assert (done.json()["state"], done.json()["applied"]) == ("approved", 1)
        assert epics_of(repo, audit) == {epics["Audit trail"], epics["Operations"]}
        with repo.sessions() as db:
            gone = db.scalars(
                select(PlanningLink).where(
                    PlanningLink.child_id == audit, PlanningLink.parent_id == epics["Billing"]
                )
            ).one()
            assert gone.removed_at is not None
        after = client.get(BASE + "epic-links").json()
        assert (after["extra"], after["ready"]) == (2, 0)

        undone = post(client, path + "/undo")
        assert undone.status_code == 200, undone.text
        assert epics_of(repo, audit) == set(epics.values())
        with repo.sessions() as db:
            back = db.scalars(
                select(PlanningLink).where(
                    PlanningLink.child_id == audit,
                    PlanningLink.parent_id == epics["Billing"],
                    PlanningLink.removed_at.is_(None),
                )
            ).one()
            # It stands as it stood: nobody's, and not the primary one.
            assert (back.origin, back.confirmed, back.is_primary) == ("unknown", False, False)
            primary = db.scalars(
                select(PlanningLink).where(
                    PlanningLink.child_id == audit,
                    PlanningLink.is_primary,
                    PlanningLink.removed_at.is_(None),
                )
            ).one()
            assert primary.parent_id == epics["Audit trail"]
    finally:
        client.close()


def test_a_quote_that_is_not_in_the_text_proposes_nothing(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        more_epics(repo, data)
        with repo.sessions.begin() as db:
            db.add(
                ProjectReviewBudget(
                    project_id=data["project"], token_limit=200_000, used=0, reserved=0
                )
            )
        start = {"provider": "fixture", "model": "analysis", "token_cap": 60_000}
        assert post(client, "epic-links/runs", **start).status_code == 200
        run(
            repo,
            Judge(
                {
                    "Billing": ("unrelated", "Billing is about something else entirely."),
                    "Operations": ("unclear", ""),
                    "Audit trail": ("unrelated", "Durable audit entries of executions."),
                }
            ),
        )
        shown = client.get(BASE + "epic-links").json()
        assert shown["answers"]["unrelated"] >= 1 and shown["ready"] == 1
        # Only the link whose Epic's passage is in the Epic's text can be taken off.
        made = post(client, "reorganize/batches", kind="extra_epics")
        lines = client.get(BASE + "reorganize/batches/" + made.json()["id"]).json()["items"]
        assert [(e["child"]["title"], e["parent"]["title"]) for e in lines] == [
            ("Respond to failures", "Audit trail")
        ]
        # A line judged wrong withdraws the batch and is never proposed again.
        path = "reorganize/batches/" + made.json()["id"]
        judged = post(client, f"{path}/lines/{lines[0]['id']}", verdict="wrong")
        assert judged.json()["state"] == "withdrawn"
        assert client.get(BASE + "epic-links").json()["ready"] == 1
        overview = client.get(BASE + "reorganize/batches").json()
        mine = next(kind for kind in overview["kinds"] if kind["kind"] == "extra_epics")
        assert mine["ready"] == 0 and mine["measured"]["rule"]

        # The Story's text changes: what was said of its links no longer stands.
        with repo.sessions.begin() as db:
            for story in (data["Audit executions"], data["Respond to failures"]):
                db.get_one(ProjectUserStory, story).description = "Something else now."
        stale = client.get(BASE + "epic-links").json()
        assert stale["answers"] == {"part": 0, "unrelated": 0, "unclear": 0}
        assert stale["check"]["to_ask"] == stale["to_judge"]["stories"]
    finally:
        client.close()
