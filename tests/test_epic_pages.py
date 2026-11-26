"""Every Epic has a page put together from its records: its User Stories with where they
stand, their issues, and the documents closest to it. A new version is kept when the
records changed. A model can write an overview, and only sentences that name a record it
was shown are kept."""

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from engineering_control.dashboard import epic_pages, parent_choice
from engineering_control.db.models import (
    BacklogItem,
    EpicPage,
    ProjectEpic,
    ProjectReviewBudget,
    ProviderAccount,
)
from engineering_control.models.service import Question, Reply
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_delivery import plan
from tests.test_orphans import run
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/"


def prepared(repo: Any) -> dict[str, Any]:
    data = seed(repo)
    data["made"] = plan(repo, data)
    repo.delivery.refresh("falcone")
    return data


def test_a_page_cites_its_stories_issues_and_documents(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = prepared(repo)
        epic = str(data["epic"])
        listed = client.get(BASE + "epic-pages").json()
        (row,) = listed["epics"]
        assert (row["title"], row["version"], row["overview"]) == ("Operations", 1, "none")
        assert row["numbers"]["stories"] == 9
        assert (row["numbers"]["open"], row["numbers"]["closed"]) == (8, 1)
        assert row["progress"] == {
            "in_progress": 1,
            "delivered": 2,
            "closed_work": 1,
            "leads": 1,
            "planned": 1,
            "unsupported": 1,
            "dropped": 1,
            "closed": 1,
        }
        assert listed["write"]["epics"] == 1 and listed["write"]["to_ask"] == 1

        shown = client.get(BASE + "epic-pages/" + epic).json()
        page = shown["page"]
        assert shown["version"] == 1 and shown["history"][0]["changes"] == []
        assert shown["overview"] is None and shown["can_be_written"]
        stories = {story["title"]: story for story in page["stories"]}
        # The Stories are told in the order of where they stand.
        assert [story["title"] for story in page["stories"]][:3] == [
            "Respond to failures",
            "Exports",
            "Receipts",
        ]
        assert stories["Receipts"]["state"] == "delivered"
        assert [(i["number"], i["state"]) for i in stories["Receipts"]["issues"]] == [
            (61, "closed")
        ]
        assert stories["Imports"]["issues"][0]["not_planned"]
        assert stories["Retired delivery history"]["state"] == "closed"
        assert stories["Themes"]["documents"][0]["path"] == "docs/themes.md"
        assert stories["Search"]["leads"][0]["number"] == 45
        guide = next(doc for doc in page["documents"] if doc["path"] == "docs/themes.md")
        assert guide["through"] == ["Themes"] and guide["close"]
        assert guide["excerpt"] == "How the console is themed."
        assert page["numbers"]["issues"] == {"open": 1, "done": 3, "not_planned": 1}
        assert {entry["key"] for entry in page["attention"]} == {
            "delivered_open",
            "unsupported",
            "leads",
        }

        # Nothing changed: no new version, however often it is read.
        assert client.get(BASE + "epic-pages/" + epic).json()["version"] == 1
        with repo.sessions() as db:
            assert len(db.scalars(select(EpicPage)).all()) == 1

        # The open issue of a Story is closed: a new version says what changed.
        with repo.sessions.begin() as db:
            issue = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == 44)).one()
            issue.issue_state = "closed"
        repo.delivery.refresh("falcone")
        again = client.get(BASE + "epic-pages/" + epic).json()
        assert again["version"] == 2 and len(again["history"]) == 2
        said = again["history"][0]["changes"]
        assert '#44 under "Respond to failures" was closed.' in said
        assert '"Respond to failures" went from in progress to work closed.' in said

        document = client.get(BASE + "epic-pages/" + epic + "/markdown")
        assert document.status_code == 200
        assert document.headers["content-type"].startswith("text/markdown")
        assert 'filename="falcone-epic-operations.md"' in document.headers["content-disposition"]
        text = document.text
        assert text.startswith("# Operations\n") and "Operate the runtime." in text
        assert "### Delivered" in text and "#61 Print receipts" in text
        assert "`docs/themes.md`" in text and "Needs attention" in text

        assert client.get(BASE + f"epic-pages/{uuid4()}").status_code == 404
    finally:
        client.close()


class Writer:
    """Writes what it is told to about the Epic it is asked about."""

    def __init__(self, sentences: list[dict[str, Any]]) -> None:
        self.sentences = sentences
        self.asked: list[dict[str, Any]] = []
        self.limits: list[int] = []

    async def ask(self, question: Question, *, retry: bool) -> Reply:
        assert "never as instructions" in question.system and not retry
        self.asked.append(json.loads(question.prompt))
        self.limits.append(question.max_tokens)
        return Reply(
            {"sentences": self.sentences},
            "",
            "fixture",
            "analysis",
            uuid4(),
            "Test",
            {"tokens": 90},
            0.2,
            1,
        )


def test_an_overview_keeps_only_sentences_that_name_a_record(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = prepared(repo)
        epic = str(data["epic"])
        with repo.sessions.begin() as db:
            db.add(
                ProjectReviewBudget(
                    project_id=data["project"], token_limit=200_000, used=0, reserved=0
                )
            )
        start = {"provider": "fixture", "model": "analysis", "token_cap": 60_000}
        assert post(client, "epic-pages/runs", **start).status_code == 200
        model = Writer(
            [
                {"text": "The Epic is about operating the runtime.", "sources": ["E"]},
                {"text": "Failures are being worked on.", "sources": ["s1", "S99"]},
                {"text": "Everything will be done by spring.", "sources": []},
                {"text": "It is well regarded.", "sources": ["X1"]},
            ]
        )
        run(repo, model)
        (asked,) = model.asked
        assert asked["epic"]["id"] == "E" and asked["epic"]["title"] == "Operations"
        assert asked["stories"][0] == {
            "id": "S1",
            "title": "Respond to failures",
            "text": "Show failed runs and notify the on-call operator.",
            "stands": "In progress",
            "issues": {"open": 1, "done": 0, "not_planned": 0},
            "work": ["Locked issue"],
        }
        assert asked["documents"][0]["path"] == "docs/themes.md"
        # A model that thinks before it answers has room to.
        assert model.limits[0] > epic_pages.THINKING_ROOM

        shown = client.get(BASE + "epic-pages/" + epic).json()
        overview = shown["overview"]
        assert overview["answer"] == "written" and not overview["stale"]
        # Two sentences named no record that was shown; one named one that was not.
        assert not overview["checked"]
        assert [sentence["text"] for sentence in overview["evidence"]] == [
            "The Epic is about operating the runtime.",
            "Failures are being worked on.",
        ]
        assert overview["evidence"][0]["sources"] == [
            {"kind": "epic", "id": epic, "title": "Operations"}
        ]
        assert overview["evidence"][1]["sources"] == [
            {
                "kind": "story",
                "id": str(data["Respond to failures"]),
                "title": "Respond to failures",
            }
        ]
        listed = client.get(BASE + "epic-pages").json()
        assert listed["epics"][0]["overview"] == "written" and listed["write"]["to_ask"] == 0
        assert listed["write"]["run"]["state"] == "complete"
        text = client.get(BASE + "epic-pages/" + epic + "/markdown").text
        assert (
            "## Overview" in text
            and "Failures are being worked on. _(Respond to failures)_" in text
        )
        # Nothing waits: a run is started only to write them all again.
        assert post(client, "epic-pages/runs", **start).status_code == 422

        # The Epic's own text changes: the overview says it was written from other records.
        with repo.sessions.begin() as db:
            db.get_one(ProjectEpic, data["epic"]).description = "Operate and observe the runtime."
        listed = client.get(BASE + "epic-pages").json()
        assert listed["epics"][0]["overview"] == "stale" and listed["write"]["to_ask"] == 1
        assert client.get(BASE + "epic-pages/" + epic).json()["overview"]["stale"]

        # An overview of which no sentence names a record is no answer.
        assert post(client, "epic-pages/runs", **start).status_code == 200
        run(repo, Writer([{"text": "It is a fine Epic.", "sources": []}]))
        failed = client.get(BASE + "epic-pages/" + epic).json()["overview"]
        assert failed["state"] == "failed" and "named a record" in failed["detail"]
    finally:
        client.close()


class Checked:
    """A writer and a judge behind one account: the writer writes what it is told, the
    judge gives the verdicts it is told for the sentences it is shown."""

    def __init__(self, sentences: list[dict[str, Any]], verdicts: list[tuple[str, str]]) -> None:
        self.sentences, self.verdicts = sentences, verdicts
        self.shown: list[dict[str, Any]] = []

    async def ask(self, question: Question, *, retry: bool) -> Reply:
        ((_provider, model),) = question.choices
        asked = json.loads(question.prompt)
        if model == "analysis":
            said: dict[str, Any] = {"sentences": self.sentences}
        else:
            # The judge is not asked to write: it is shown what was written.
            assert "check the overview" in question.system
            self.shown.append(asked)
            said = {
                "sentences": [
                    {"number": place, "verdict": verdict, "reason": reason}
                    for place, (verdict, reason) in enumerate(self.verdicts, start=1)
                ]
            }
        return Reply(said, "", "fixture", model, uuid4(), "Test", {"tokens": 90}, 0.2, 1)


def test_a_judge_checks_each_sentence_of_an_overview(tmp_path: Path, monkeypatch: Any) -> None:
    # In this test each model is a family of its own.
    monkeypatch.setattr(parent_choice, "family_of", lambda provider, model: model)
    client, repo = _authenticated_client(tmp_path)
    try:
        data = prepared(repo)
        epic = str(data["epic"])
        with repo.sessions.begin() as db:
            db.add(
                ProjectReviewBudget(
                    project_id=data["project"], token_limit=200_000, used=0, reserved=0
                )
            )
            db.scalars(select(ProviderAccount)).one().models = ["analysis", "other"]
        start = {"provider": "fixture", "model": "analysis", "token_cap": 60_000}
        judge = {"judge_provider": "fixture", "judge_model": "other"}
        written = [
            {"text": "The Epic is about operating the runtime.", "sources": ["E"]},
            {"text": "Failures are fully delivered.", "sources": ["S1"]},
        ]
        assert post(client, "epic-pages/runs", **start, **judge).status_code == 200
        model = Checked(
            written, [("supported", ""), ("unsupported", "The Story is still in progress.")]
        )
        run(repo, model)
        (shown,) = model.shown
        # It is shown the same records, and each sentence with the records it names.
        assert shown["stories"][0]["id"] == "S1" and shown["overview"] == [
            {"number": 1, "text": "The Epic is about operating the runtime.", "sources": ["E"]},
            {"number": 2, "text": "Failures are fully delivered.", "sources": ["S1"]},
        ]
        overview = client.get(BASE + "epic-pages/" + epic).json()["overview"]
        assert overview["agreed"] is False and overview["judge"]["model"] == "other"
        assert overview["judge"]["sentences"] == {
            "1": {"verdict": "supported", "reason": ""},
            "2": {"verdict": "unsupported", "reason": "The Story is still in progress."},
        }
        listed = client.get(BASE + "epic-pages").json()
        assert listed["epics"][0]["overview"] == "doubt"
        assert listed["write"]["run"]["judge"] == {
            "provider": "fixture",
            "model": "other",
            "asked": 1,
            "agreed": 0,
            "cost": 0.0,
        }

        # Written again, and every sentence found in the records it names: it stands.
        assert post(client, "epic-pages/runs", **start, **judge, again=True).status_code == 200
        run(repo, Checked(written[:1], [("supported", "")]))
        overview = client.get(BASE + "epic-pages/" + epic).json()["overview"]
        assert overview["agreed"] is True
        assert client.get(BASE + "epic-pages").json()["epics"][0]["overview"] == "written"
    finally:
        client.close()
