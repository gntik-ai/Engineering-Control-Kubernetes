"""Another model is tried on the questions a check already has an answer to. What it says
is kept as the trial's lines: the answer on record, what waits for approval and the
counts of the check stay as they were."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from engineering_control.dashboard import epic_links, parent_choice
from engineering_control.db.models import (
    ChoiceReview,
    ModelTrialLine,
    PlanningLink,
    ProjectRecord,
    ProjectReviewBudget,
    ProviderAccount,
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


class Two:
    """Two models behind one account: each answers by its own table, per User Story."""

    COST = {"cheaper": (50, 0.0002), "analysis": (70, 0.004)}

    def __init__(self, says: dict[tuple[str, str], dict[str, tuple[str, str]]]) -> None:
        self.says = says
        self.calls: list[tuple[str, str]] = []

    async def ask(self, question: Question, *, retry: bool) -> Reply:
        ((_provider, model),) = question.choices
        asked = json.loads(question.prompt)
        story = asked["story"]["title"]
        self.calls.append((model, story))
        answers = self.says[model, story]
        said = {
            "story_quote": asked["story"]["text"],
            "epics": [
                {
                    "id": entry["id"],
                    "answer": answers[entry["title"]][0],
                    "reason": "It reads so.",
                    "epic_quote": answers[entry["title"]][1],
                }
                for entry in asked["epics"]
                if entry["title"] in answers
            ],
        }
        tokens, cost = self.COST[model]
        usage = {"tokens": tokens, "cost": cost}
        return Reply(said, "", "fixture", model, uuid4(), "Test", usage, 0.2, 1)


BILLING = ("unrelated", "Invoices and payments.")
PART = ("part", "Operate the runtime.")
AUDIT, FAILURES = "Audit executions", "Respond to failures"
# Which Epic is the second Story's primary one is not fixed: the other is judged.
BOTH_PART = {"Operations": PART, "Audit trail": PART}
BOTH_NOT = {"Operations": BILLING, "Audit trail": BILLING}


def two_stories_to_judge(repo: Any) -> None:
    """Both open Stories have links to judge, and the account offers a second model."""
    data = seed(repo)
    more_epics(repo, data)
    with repo.sessions.begin() as db:
        db.add(
            ProjectReviewBudget(project_id=data["project"], token_limit=200_000, used=0, reserved=0)
        )
        db.scalars(select(ProviderAccount)).one().models = ["analysis", "cheaper"]
    repo.planning_links.reconcile_project("falcone")
    with repo.sessions.begin() as db:
        for link in db.scalars(select(PlanningLink).where(PlanningLink.kind == "story_epic")):
            if not link.is_primary:
                link.origin, link.confirmed = "model", True


def test_a_first_model_keeps_what_it_settles_and_the_rest_goes_on(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        two_stories_to_judge(repo)
        both = {"first_provider": "fixture", "first_model": "cheaper"}
        # The first model is another model, and one that can be asked.
        same = post(
            client, "epic-links/runs", **START, first_provider="fixture", first_model="analysis"
        )
        assert same.status_code == 422 and "own model" in same.text
        gone = post(
            client, "epic-links/runs", **START, first_provider="fixture", first_model="none"
        )
        assert gone.status_code == 422
        # A trial asks one model: that is what it is for.
        assert post(client, "epic-links/runs", **START, **both).status_code == 200
        model = Two(
            {
                # It cannot tell about one link of the first Story: that Story goes on.
                ("cheaper", AUDIT): {"Billing": BILLING, "Operations": ("unclear", "")},
                ("analysis", AUDIT): {"Billing": BILLING, "Operations": PART},
                ("cheaper", FAILURES): BOTH_PART,
            }
        )
        run(repo, model)
        assert model.calls == [("cheaper", AUDIT), ("analysis", AUDIT), ("cheaper", FAILURES)]
        shown = client.get(BASE + "epic-links").json()
        done = shown["check"]["run"]
        assert (done["state"], done["done"], done["total"], done["failed"]) == ("complete", 2, 2, 0)
        assert done["spent"] == 170
        assert done["first"] == {
            "provider": "fixture",
            "model": "cheaper",
            "asked": 2,
            "settled": 1,
            "tokens": 100,
            "cost": 0.0004,
        }
        # What it cost, and what the run's own model would have cost for both.
        assert (done["cost"], done["alone"]) == (0.0044, 0.008)
        assert shown["answers"] == {"part": 2, "unrelated": 1, "unclear": 0}
        with repo.sessions() as db:
            by = {review.model: review.passed for review in db.scalars(select(ChoiceReview))}
        # Each answer says which model gave it.
        assert by == {"analysis": True, "cheaper": False}

        # A run with one model reports no first model.
        assert post(client, "epic-links/runs", **START, again=True).status_code == 200
        run(repo, Two({("analysis", AUDIT): {"Billing": BILLING}, ("analysis", FAILURES): {}}))
        plain = client.get(BASE + "epic-links").json()["check"]["run"]
        assert plain["first"] is None and plain["alone"] is None
    finally:
        client.close()


def test_a_model_whose_trial_agreed_is_the_first_model_by_default(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        two_stories_to_judge(repo)
        # The project asks less of a trial than the platform does: two answers settled.
        gate = {"dollar_limit": 3, "makers": ["fixture"], "rounds": 0}
        saved = post(client, "review-settings", **gate, numbers={"gate_settled": 2})
        assert saved.status_code == 200, saved.text
        record = {
            ("analysis", AUDIT): {"Billing": BILLING, "Operations": PART},
            ("analysis", FAILURES): BOTH_PART,
        }
        assert post(client, "epic-links/runs", **START).status_code == 200
        run(repo, Two(record))
        assert client.get(BASE + "epic-links").json()["check"]["first"] is None

        trial = {"provider": "fixture", "model": "cheaper", "token_cap": 60_000}
        # It cannot tell about one Story: one answer settled is too few to go by.
        assert post(client, "model-trials/epic_links/runs", **trial).status_code == 200
        run(
            repo,
            Two(
                {
                    ("cheaper", AUDIT): {"Billing": BILLING, "Operations": ("unclear", "")},
                    ("cheaper", FAILURES): BOTH_PART,
                }
            ),
        )
        report = client.get(BASE + "model-trials").json()
        (first,) = report["trials"]
        assert (first["answered"], first["settled"], first["settled_same"]) == (2, 1, 1)
        assert not first["gate"] and report["gate"] == {"settled": 2, "same": 0.9}
        assert client.get(BASE + "epic-links").json()["check"]["first"] is None

        # It settles both and says the same as on record: it is asked first from now on.
        assert post(client, "model-trials/epic_links/runs", **trial).status_code == 200
        run(repo, Two({("cheaper", story): says for (_m, story), says in record.items()}))
        report = client.get(BASE + "model-trials").json()
        assert report["trials"][0]["gate"]
        chosen = {"provider": "fixture", "model": "cheaper", "answers": 2, "settled": 2, "same": 2}
        assert client.get(BASE + "epic-links").json()["check"]["first"] == chosen
        check = next(c for c in report["checks"] if c["check"] == "epic_links")
        assert check["first"] == chosen
        # Only for the check it was tried on.
        assert client.get(BASE + "cleanup").json()["check"]["first"] is None

        # Its latest trial counts: one that says otherwise takes the default away.
        assert post(client, "model-trials/epic_links/runs", **trial).status_code == 200
        run(
            repo,
            Two(
                {
                    ("cheaper", AUDIT): {"Billing": PART, "Operations": BILLING},
                    ("cheaper", FAILURES): BOTH_NOT,
                }
            ),
        )
        assert client.get(BASE + "epic-links").json()["check"]["first"] is None
    finally:
        client.close()


def test_a_judge_of_epic_links_is_compared_link_by_link(tmp_path: Path, monkeypatch: Any) -> None:
    # In this test each model is a family of its own.
    monkeypatch.setattr(parent_choice, "family_of", lambda provider, model: model)
    client, repo = _authenticated_client(tmp_path)
    try:
        two_stories_to_judge(repo)
        judge = {"judge_provider": "fixture", "judge_model": "cheaper"}
        assert post(client, "epic-links/runs", **START, **judge).status_code == 200
        gone = {
            "Operations": ("unrelated", "Operate the runtime."),
            "Audit trail": ("unrelated", "Durable audit entries of executions."),
        }
        run(
            repo,
            Two(
                {
                    # Both say the same of both links of the first Story.
                    ("analysis", AUDIT): {"Billing": BILLING, "Operations": PART},
                    ("cheaper", AUDIT): {"Billing": BILLING, "Operations": PART},
                    # Of the second Story's link the judge says otherwise.
                    ("analysis", FAILURES): gone,
                    ("cheaper", FAILURES): BOTH_PART,
                }
            ),
        )
        shown = client.get(BASE + "epic-links").json()
        ran = shown["check"]["run"]
        assert (ran["state"], ran["done"]) == ("complete", 2)
        assert (ran["judge"]["asked"], ran["judge"]["agreed"]) == (2, 1)
        # Two links were found unrelated; only the one the judge calls unrelated too
        # is proposed.
        assert shown["answers"]["unrelated"] == 2 and shown["ready"] == 1
        records = client.get(BASE + "epic-links/records").json()["items"]
        by = {row["title"]: row for row in records}
        assert by[AUDIT]["agreed"] is True and by[AUDIT]["judge"] == "cheaper"
        assert {link["epic"]["title"]: link["second"] for link in by[AUDIT]["links"]} == {
            "Billing": "unrelated",
            "Operations": "part",
        }
        assert by[FAILURES]["agreed"] is False
        (doubted,) = by[FAILURES]["links"]
        assert (doubted["answer"], doubted["second"]) == ("unrelated", "part")
        with repo.sessions() as db:
            project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
            (line,) = epic_links.ready(db, project)
            assert "said the same" in line.detail["why"]
    finally:
        client.close()
