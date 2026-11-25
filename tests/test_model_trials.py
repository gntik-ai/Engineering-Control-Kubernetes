"""Another model is tried on the questions a check already has an answer to. What it says
is kept as the trial's lines: the answer on record, what waits for approval and the
counts of the check stay as they were."""

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from engineering_control.db.models import (
    ChoiceReview,
    ModelTrialLine,
    PlanningLink,
    ProjectReviewBudget,
)
from engineering_control.models.service import Question, Reply
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_epic_links import Judge
from tests.test_orphans import run
from tests.test_primary_epics import more_epics
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/"
START = {"provider": "fixture", "model": "analysis", "token_cap": 60_000}


class Cheaper(Judge):
    """Another model: it says what it costs, and answers by its own table."""

    async def ask(self, question: Question, *, retry: bool) -> Reply:
        reply = await super().ask(question, retry=retry)
        return replace(reply, model="cheaper", usage={"tokens": 50, "cost": 0.0002})


class Silent:
    async def ask(self, question: Question, *, retry: bool) -> Reply:
        asked = json.loads(question.prompt)
        said = {"story_quote": asked["story"]["text"], "epics": []}
        return Reply(said, "", "fixture", "cheaper", uuid4(), "Test", {"tokens": 40}, 0.2, 1)


def test_a_trial_is_compared_with_the_answer_on_record_and_changes_nothing(
    tmp_path: Path,
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        more_epics(repo, data)
        audit = data["Audit executions"]
        with repo.sessions.begin() as db:
            db.add(
                ProjectReviewBudget(
                    project_id=data["project"], token_limit=200_000, used=0, reserved=0
                )
            )
        repo.planning_links.reconcile_project("falcone")
        with repo.sessions.begin() as db:
            for link in db.scalars(select(PlanningLink).where(PlanningLink.kind == "story_epic")):
                if link.child_id == audit:
                    link.origin, link.confirmed = "model", True
                elif not link.is_primary:
                    link.origin, link.confirmed = "administrator", True

        # No check has an answer yet: there is nothing to try a model on.
        shown = client.get(BASE + "model-trials").json()
        assert {check["check"]: check["answers"] for check in shown["checks"]} == {
            "story_needed": 0,
            "epic_links": 0,
            "story_epic": 0,
            "issue_story": 0,
        }
        assert shown["trials"] == [] and shown["sample"] == 100
        refused = post(client, "model-trials/epic_links/runs", **START)
        assert refused.status_code == 422 and "Run the check first" in refused.text
        assert post(client, "model-trials/epic_overview/runs", **START).status_code == 422

        # The check itself: one Story, two links judged.
        assert post(client, "epic-links/runs", **START).status_code == 200
        run(
            repo,
            Judge(
                {
                    "Billing": ("unrelated", "Invoices and payments."),
                    "Operations": ("part", "Operate the runtime."),
                }
            ),
        )
        before = client.get(BASE + "epic-links").json()
        assert before["answers"] == {"part": 1, "unrelated": 1, "unclear": 0}
        assert before["ready"] == 1

        shown = client.get(BASE + "model-trials").json()
        check = next(entry for entry in shown["checks"] if entry["check"] == "epic_links")
        assert (check["answers"], check["sample"], check["models"]) == (1, 1, ["analysis"])
        # What the answer on record used is what the trial is likely to use.
        assert check["likely"] == 70 and check["largest"] > 0 and check["run"] is None

        # Another model says otherwise about one of the two links.
        started = post(client, "model-trials/epic_links/runs", **START)
        assert started.status_code == 200, started.text
        run(
            repo,
            Cheaper(
                {
                    "Billing": ("unrelated", "Invoices and payments."),
                    "Operations": ("unrelated", "Operate the runtime."),
                }
            ),
        )
        shown = client.get(BASE + "model-trials").json()
        (trial,) = shown["trials"]
        assert trial["check"] == "epic_links" and trial["run"]["state"] == "complete"
        assert (trial["answered"], trial["same"], trial["same_share"]) == (1, 0, 0.0)
        assert trial["links"] == {"total": 2, "same": 1}
        assert (trial["checked"], trial["unclear"]) == (1, 0)
        assert (trial["tokens_each"], trial["cost"], trial["cost_each"]) == (50, 0.0002, 0.0002)
        # The check's own calls did not say what they cost.
        assert trial["record"] == {
            "model": "analysis",
            "models": 1,
            "unclear": 0,
            "checked": 1,
            "tokens_each": 70,
            "cost_each": None,
        }
        assert trial["decided"] is None
        assert trial["table"] == [{"record": "unrelated", "said": "unrelated", "count": 1}]

        lines = BASE + "model-trials/runs/" + trial["run"]["id"] + "/records"
        (line,) = client.get(lines, params={"show": "differs"}).json()["items"]
        assert line["title"] == "Audit executions" and not line["same"]
        assert line["differs"] == [{"title": "Operations", "said": "unrelated", "record": "part"}]
        assert line["record"]["model"] == "analysis"
        assert client.get(lines, params={"show": "same"}).json()["total"] == 0

        # Nothing of the check changed: its answer, what is ready, what it would ask.
        after = client.get(BASE + "epic-links").json()
        assert after["answers"] == before["answers"] and after["ready"] == before["ready"]
        assert after["check"]["to_ask"] == before["check"]["to_ask"] == 0
        assert after["check"]["run"] == before["check"]["run"]
        with repo.sessions() as db:
            (record,) = db.scalars(select(ChoiceReview).where(ChoiceReview.kind == "epic_links"))
            assert record.model == "analysis" and record.answer == "unrelated"
        # A batch is made from the check's answer, not from the trial's.
        made = post(client, "reorganize/batches", kind="extra_epics")
        (item,) = client.get(BASE + "reorganize/batches/" + made.json()["id"]).json()["items"]
        assert "Billing" in json.dumps(item) and "Operations" not in json.dumps(item["parent"])

        # A second model on the same question: it says the same, and both trials stay.
        assert post(client, "model-trials/epic_links/runs", **START).status_code == 200
        run(
            repo,
            Cheaper(
                {
                    "Billing": ("unrelated", "Invoices and payments."),
                    "Operations": ("part", "Operate the runtime."),
                }
            ),
        )
        newest, older = client.get(BASE + "model-trials").json()["trials"]
        assert (newest["same"], newest["same_share"], newest["links"]["same"]) == (1, 1.0, 2)
        assert older["same"] == 0
        with repo.sessions() as db:
            assert len(db.scalars(select(ModelTrialLine)).all()) == 2

        # A model that judges nothing gives no usable answer: the trial counts it so.
        assert post(client, "model-trials/epic_links/runs", **START).status_code == 200
        run(repo, Silent())
        failed = client.get(BASE + "model-trials").json()["trials"][0]
        assert (failed["answered"], failed["run"]["failed"]) == (0, 1)
        assert failed["same_share"] is None

        unknown = post(client, "model-trials/nothing/runs/" + trial["run"]["id"] + "/control")
        assert unknown.status_code in (400, 404, 422)
    finally:
        client.close()
