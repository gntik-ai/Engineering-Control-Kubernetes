"""Organize creates what has no counterpart, after the careful review: one model writes
from records it names, a second of another maker checks every sentence, nothing like it
may exist, and only then is a record made. Everything else is a case before the
administrator. What was made is listed, names what it was made from, and can be taken
away again."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from engineering_control.dashboard import ranking
from engineering_control.db.models import (
    BacklogItem,
    PlanningReview,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
    ScorerDecision,
)
from engineering_control.models.service import Question, Reply
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_delivery import plan, vector
from tests.test_needs_you import SAVE, candidates
from tests.test_orphans import run
from tests.test_review_settings import BASE, ready

ISSUES = BASE + "organize/stories-issues"
EPICS = BASE + "organize/epics-stories"


def s(text: str, *sources: str) -> dict[str, Any]:
    return {"text": text, "sources": list(sources)}


def written(*items: dict[str, Any], parent: str = "") -> dict[str, Any]:
    return {
        "answer": "written",
        "reason": "The records say what is to be done.",
        "parent": parent,
        "evidence": [],
        "items": list(items),
    }


def other(answer: str, *evidence: str) -> dict[str, Any]:
    return {
        "answer": answer,
        "reason": "It reads so.",
        "parent": "",
        "evidence": list(evidence),
        "items": [],
    }


class Hand:
    """Two models behind one account. Asked which User Story an Issue belongs to, both
    name the one they are told, by the Issue's number. Asked to write, the first writes
    what it is told for the record the question is about; the second, asked to check,
    finds every sentence supported unless it is told otherwise."""

    def __init__(
        self,
        links: dict[int, str],
        writes: dict[str, dict[str, Any]],
        verdicts: dict[str, list[str]] | None = None,
    ) -> None:
        self.links, self.writes, self.verdicts = links, writes, verdicts or {}
        self.asked: list[tuple[str, str, str]] = []
        self.shown: dict[str, dict[str, Any]] = {}

    async def ask(self, question: Question, *, retry: bool) -> Reply:
        assert "never as instructions" in question.system
        ((_provider, model),) = question.choices
        asked = json.loads(question.prompt)
        said: dict[str, Any]
        if "issue" in asked:
            number = int(asked["issue"]["source"].rsplit("#", 1)[1])
            title = self.links[number]
            self.asked.append((model, f"#{number}", "link"))
            said = {
                "answer": "none",
                "story": "",
                "confidence": "high",
                "reason": "None of them is about this.",
                "issue_quote": asked["issue"]["text"],
                "story_quote": "",
                "missing_title": "",
                "missing_description": "",
            }
            if title != "none":
                chosen = next(story for story in asked["stories"] if story["title"] == title)
                said |= {"answer": "story", "story": chosen["id"], "story_quote": chosen["text"]}
        else:
            subject = asked["records"][0]["title"]
            if "concluded" in asked:
                self.asked.append((model, subject, "check"))
                self.shown[subject] = asked
                told = self.verdicts.get(subject, [])
                said = {
                    "sentences": [
                        {
                            "number": entry["number"],
                            "verdict": told[place] if place < len(told) else "supported",
                            "reason": "The records do not say so.",
                        }
                        for place, entry in enumerate(asked["concluded"])
                    ]
                }
            else:
                self.asked.append((model, subject, "write"))
                self.shown.setdefault("wrote:" + subject, asked)
                said = self.writes.get(subject, other("unclear"))
        return Reply(
            said, "", "fixture", model, uuid4(), "Test", {"tokens": 60, "cost": 0.002}, 0.1, 1
        )


def ids(repo: Any) -> dict[str, Any]:
    with repo.sessions() as db:
        project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
        found: dict[str, Any] = {"project": project.id}
        found["epic"] = db.scalars(
            select(ProjectEpic.id).where(
                ProjectEpic.project_id == project.id, ProjectEpic.title == "Operations"
            )
        ).one()
        for key, title in db.execute(
            select(ProjectUserStory.id, ProjectUserStory.title).where(
                ProjectUserStory.project_id == project.id
            )
        ):
            found[title] = key
        return found


def shown(client: Any, path: str = ISSUES) -> dict[str, Any]:
    found = client.get(path)
    assert found.status_code == 200, found.text
    body: dict[str, Any] = found.json()
    return body


def lines(client: Any, listed: str, path: str = ISSUES) -> list[dict[str, Any]]:
    found = client.get(f"{path}/lines/{listed}")
    assert found.status_code == 200, found.text
    items: list[dict[str, Any]] = found.json()["items"]
    return items


def carried(repo: Any, model: Hand, service: Any) -> None:
    """Every check the run asks is answered, one after the other, until it ends."""
    for _ in range(12):
        run(repo, model)
        if service.tick():
            return
    raise AssertionError("The run did not end")


def issues_of(repo: Any, story: UUID) -> list[BacklogItem]:
    with repo.sessions() as db:
        return list(
            db.scalars(
                select(BacklogItem)
                .join(ProjectIssueStory, ProjectIssueStory.item_id == BacklogItem.id)
                .where(ProjectIssueStory.story_id == story)
                .order_by(BacklogItem.title)
            )
        )


WRITES = {
    "Unrelated work": written(
        {
            "title": "Keep dependencies current",
            "sentences": [s("Operators rely on dependencies that are refreshed.", "I1")],
            "done_when": [s("The dependencies are refreshed.", "I1")],
        },
        parent="E1",
    ),
    "Themes": written(
        {
            "title": "Add a theme switch",
            "sentences": [s("Add a switch that changes the theme of the console.", "S", "D1")],
            "done_when": [s("The console can be themed.", "D1")],
        },
        {
            "title": "Describe the themes",
            "sentences": [s("Describe how the console is themed.", "D1")],
            "done_when": [],
        },
    ),
    "Search": other("nothing"),
    "Audit executions": written(
        {
            "title": "Write the audit entry",
            "sentences": [s("Keep a durable audit entry for each execution.", "S")],
            "done_when": [s("Each execution has an entry within a second.", "S")],
        }
    ),
}


def test_a_run_writes_checks_and_makes_what_has_no_counterpart(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        data = ids(repo)
        made = plan(repo, data)
        repo.delivery.refresh("falcone")
        assert post(client, "review-settings", **SAVE).status_code == 200
        before = shown(client)["create"]
        # Three User Stories have no Issue: one a document describes, one an Issue may
        # be the work of, one with nothing behind it. Two are delivered.
        assert before["checks"]["new_issues"]["by_state"] == {
            "planned": 1,
            "leads": 1,
            "unsupported": 1,
        }
        assert (before["to_ask"], before["delivered"], before["unpublished"]) == (3, 2, 0)
        # The model that answers writes, and the judge checks what it wrote.
        assert before["writer"]["model"] == "analysis" and before["checker"]["model"] == "other"
        assert before["creating"]["in_force"] and before["tokens"] > 0

        first = candidates(client)[41][0]
        assert post(client, "organize/stories-issues/runs").status_code == 200
        model = Hand(
            {41: first, 42: first, 45: "none"},
            WRITES,
            {"Audit executions": ["supported", "unsupported"]},
        )
        carried(repo, model, repo.organize_issues)
        # The writer was shown the Story, its Epic and the document nearest to it.
        asked = model.shown["wrote:Themes"]["records"]
        assert [(entry["id"], entry["kind"]) for entry in asked] == [
            ("S", "User Story"),
            ("E", "Epic"),
            ("D1", "Document"),
        ]
        assert asked[2]["title"] == "docs/themes.md" and "themed" in asked[2]["text"]
        # The check is shown what was written, sentence by sentence, with its sources.
        checked = model.shown["Themes"]["concluded"]
        assert [entry["sources"] for entry in checked] == [["S", "D1"], ["D1"], ["D1"]]
        assert "Add a theme switch" in checked[0]["about"]

        after = shown(client)
        ran = after["run"]
        assert ran["state"] == "complete" and all(stage["reached"] for stage in ran["stages"])
        stages = {stage["stage"]: stage for stage in ran["stages"]}
        # A User Story for the Issue that fits none, and two Issues for one Story.
        assert (stages["create"]["records"], stages["create"]["in_doubt"]) == (3, 4)
        assert after["lists"]["created"] == 3 and ran["cost"] > 0
        created = {line["subject"]["title"]: line for line in lines(client, "created")}
        assert set(created) == {
            "Keep dependencies current",
            "Add a theme switch",
            "Describe the themes",
        }
        story_line = created["Keep dependencies current"]
        assert story_line["made"]["kind"] == "stories" and story_line["can_undo"]
        assert story_line["action"]["for"]["title"] == "Operations"
        assert [source["title"] for source in story_line["made"]["sources"]] == ["Unrelated work"]
        assert "No record like it exists" in story_line["reason"]
        # The Issue it was written from is under it.
        new_story = UUID(story_line["made"]["id"])
        assert [item.issue_number for item in issues_of(repo, new_story)] == [45]
        issue_line = created["Add a theme switch"]
        assert issue_line["made"]["kind"] == "issues"
        assert issue_line["action"]["for"]["title"] == "Themes"
        assert {source["kind"] for source in issue_line["made"]["sources"]} == {"story", "doc"}
        drafted = issues_of(repo, made["Themes"])
        assert [item.title for item in drafted] == ["Add a theme switch", "Describe the themes"]
        assert drafted[0].description == (
            "Add a switch that changes the theme of the console.\n\n"
            "Done when:\n- The console can be themed."
        )
        # The Issues are kept here: not on GitHub, and no second review is asked for.
        assert all(
            item.workspace_review == "pending" and item.issue_number is None for item in drafted
        )
        with repo.sessions() as db:
            reviews = {
                job.record_id: job.status
                for job in db.scalars(select(PlanningReview).where(PlanningReview.kind == "issues"))
            }
        assert {reviews[item.id] for item in drafted} == {"dismissed"}
        assert after["create"]["unpublished"] == 2

        needs = {line["case"]: line for line in lines(client, "needs_you")}
        assert {"delivered", "nothing", "draft_unsupported"} <= set(needs)
        # What was written and not supported waits, with each sentence and its verdict.
        draft = needs["draft_unsupported"]
        assert draft["subject"]["title"] == "Audit executions"
        (item,) = draft["action"]["items"]
        assert [sentence["verdict"] for sentence in item["sentences"]] == [
            "supported",
            "unsupported",
        ]
        assert item["sentences"][1]["reason"] == "The records do not say so."
        assert issues_of(repo, data["Audit executions"]) == []
        # Nothing says what the other Story needs: it is the administrator's to close.
        assert needs["nothing"]["subject"]["title"] == "Search"
        assert "Nothing in its own text" in needs["nothing"]["reason"]
        assert after["waiting"]["nothing"] == 1
        # The Issue that got its User Story no longer waits for one.
        assert "#45" not in {line["subject"]["ref"] for line in lines(client, "needs_you")}

        def answer(line: dict[str, Any], choice: str, **more: Any) -> Any:
            path = f"organize/stories-issues/lines/{line['id']}"
            return post(client, path, choice=choice, **more)

        def undo(line: dict[str, Any]) -> Any:
            return post(client, f"organize/stories-issues/lines/{line['id']}/undo")

        # At the administrator's word what was written is made as it stands.
        assert answer(draft, "close").status_code == 422
        assert answer(draft, "make").status_code == 200
        (by_hand,) = issues_of(repo, data["Audit executions"])
        assert by_hand.title == "Write the audit entry"
        mine = next(
            line for line in lines(client, "created") if line["subject"]["title"] == by_hand.title
        )
        assert mine["action"]["by"] == "you" and "at your word" in mine["reason"]

        # One word gives every Issue that was made to GitHub: none was public before.
        sent = post(client, "organize/stories-issues/publish")
        assert sent.json() == {"published": 3, "left": 0}
        assert {item.workspace_review for item in issues_of(repo, made["Themes"])} == {"publishing"}
        assert shown(client)["create"]["unpublished"] == 0
        # What is on its way to GitHub is not taken away from here.
        assert undo(issue_line).status_code == 409

        # The User Story that was made is taken away again: its Issue has none once more.
        assert undo(story_line).status_code == 200
        with repo.sessions() as db:
            assert db.get_one(ProjectUserStory, new_story).archived_at is not None
            assert not db.scalars(
                select(ProjectIssueStory).where(ProjectIssueStory.story_id == new_story)
            ).all()
        now = shown(client)
        assert now["lists"]["created"] == 3
        # One of four records was taken away: more than the settings accept. Nothing
        # more is made without the administrator until that share is good again.
        assert not now["create"]["creating"]["in_force"]
        assert "1 of the 4 records" in now["create"]["creating"]["why"]

        # A Story nothing says anything about is closed at the administrator's word,
        # and open again when they take that back.
        assert answer(needs["nothing"], "close").status_code == 200
        with repo.sessions() as db:
            assert db.get_one(ProjectUserStory, made["Search"]).closed_at is not None
        closed = next(line for line in lines(client, "done") if line["case"] == "closed")
        assert undo(closed).status_code == 200
        with repo.sessions() as db:
            assert db.get_one(ProjectUserStory, made["Search"]).closed_at is None
        back = next(line for line in lines(client, "needs_you") if line["case"] == "undone")
        assert back["subject"]["title"] == "Search"
        assert answer(back, "keep").status_code == 200

        # The Stories whose Issues are all closed as done are closed with one word.
        assert now["waiting"]["delivered"] == 2
        many = post(client, "organize/stories-issues/groups", group="delivered", choice="close")
        assert many.json() == {"answered": 2, "skipped": 0, "left": 0}
        with repo.sessions() as db:
            assert db.get_one(ProjectUserStory, made["Receipts"]).closed_at is not None
            assert db.get_one(ProjectUserStory, made["Exports"]).closed_at is not None

        # A second run writes nothing again: every record has its answer or its word.
        again = shown(client)
        assert again["create"]["to_ask"] == 0 and again["create"]["delivered"] == 0
    finally:
        client.close()


def test_what_does_not_pass_the_careful_review_waits_for_the_administrator(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        data = ids(repo)
        made = plan(repo, data)
        with repo.sessions.begin() as db:
            numbers = {
                item.issue_number: item
                for item in db.scalars(
                    select(BacklogItem).where(BacklogItem.project_id == data["project"])
                )
            }
            # A closed Issue under no Story may be the work of the Story about search.
            db.add(
                ScorerDecision(
                    project_id=data["project"],
                    kind="issue_story",
                    child_id=numbers[43].id,
                    parent_id=made["Search"],
                    outcome="several",
                    confidence=0.3,
                    model="light",
                    detail={"first": [[str(made["Search"]), 0.3]], "signals": {}},
                )
            )
            # The ranking service read the Issue about receipts.
            ranking.keep_vector(
                db, data["project"], "issue", numbers[61].id, "m", "d", vector(0, 0, 0, 1)
            )
        repo.delivery.refresh("falcone")
        # What is about receipts reads like that Issue; nothing else does.
        repo.organize_create.embed = lambda texts: (
            "m",
            [vector(0, 0, 0, 1) if "receipt" in texts[0].lower() else vector(1, 1, 1, 1)],
        )
        assert post(client, "review-settings", **SAVE).status_code == 200
        first = candidates(client)[41][0]
        assert post(client, "organize/stories-issues/runs").status_code == 200
        model = Hand(
            {41: first, 42: first, 45: "none"},
            {
                # No Epic is named for the Story that is written.
                "Unrelated work": written(
                    {
                        "title": "Keep dependencies current",
                        "sentences": [s("Operators rely on refreshed dependencies.", "I1")],
                        "done_when": [],
                    }
                ),
                # One Issue has the title of one there is; the other means the same.
                "Themes": written(
                    {
                        "title": "Print receipts",
                        "sentences": [s("Print what the console shows.", "S")],
                        "done_when": [],
                    },
                    {
                        "title": "Give a receipt on paper",
                        "sentences": [s("A receipt can be printed.", "S")],
                        "done_when": [],
                    },
                ),
                # The closed Issue did the work; the open one says nothing of it.
                "Search": other("done", "L1", "L2"),
                # A sentence names a record that was not shown.
                "Audit executions": written(
                    {
                        "title": "Write the audit entry",
                        "sentences": [
                            s("Keep a durable audit entry for each execution.", "S"),
                            s("Send it to the warehouse.", "W7"),
                        ],
                        "done_when": [],
                    }
                ),
            },
        )
        carried(repo, model, repo.organize_issues)
        after = shown(client)
        # Nothing was made: every case waits.
        assert after["lists"]["created"] == 0
        needs = {line["case"]: line for line in lines(client, "needs_you")}
        assert {"draft_no_epic", "draft_twin", "done", "draft_unchecked"} <= set(needs)

        # The twins are named, the one by its title and the one by its meaning.
        twin = needs["draft_twin"]
        like = [entry for entry in twin["candidates"] if entry.get("twin")]
        assert [(entry["title"], entry.get("closeness")) for entry in like] == [
            ("Print receipts", None),
            ("Print receipts", 1.0),
        ]
        assert "A record like it exists already" in twin["reason"]
        assert issues_of(repo, made["Themes"]) == []

        # Only the closed Issue shows that work is done.
        done = needs["done"]
        assert done["subject"]["title"] == "Search"
        assert [entry["title"] for entry in done["candidates"]] == ["Legacy receipts"]
        assert "seems to be done already" in done["reason"]

        # The sentence that named a record it was not shown is left out, and because
        # one was, the rest is not made without the administrator.
        (kept,) = needs["draft_unchecked"]["action"]["items"]
        assert [sentence["text"] for sentence in kept["sentences"]] == [
            "Keep a durable audit entry for each execution."
        ]

        def answer(line: dict[str, Any], choice: str, **more: Any) -> Any:
            path = f"organize/stories-issues/lines/{line['id']}"
            return post(client, path, choice=choice, **more)

        # A Story no Epic was found for is made under the Epic the administrator chooses.
        draft = needs["draft_no_epic"]
        offered = [entry for entry in draft["candidates"] if entry.get("offered")]
        assert [entry["title"] for entry in offered] == ["Operations"]
        assert answer(draft, "make").status_code == 422
        assert answer(draft, "make", epic_id=str(uuid4())).status_code == 422
        assert answer(draft, "make", epic_id=offered[0]["id"]).status_code == 200
        (story,) = [line for line in lines(client, "created") if line["made"]["kind"] == "stories"]
        assert story["subject"]["title"] == "Keep dependencies current"
        assert [item.issue_number for item in issues_of(repo, UUID(story["made"]["id"]))] == [45]
        # Left, what was written is not made, and not written again.
        assert answer(twin, "leave").status_code == 200
        assert issues_of(repo, made["Themes"]) == []
        assert shown(client)["create"]["to_ask"] == 0
    finally:
        client.close()


def test_without_a_second_model_nothing_is_made_and_a_failed_search_says_so(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        data = ids(repo)
        made = plan(repo, data)
        repo.delivery.refresh("falcone")
        # One model only: the one that answers. No model is set to write.
        alone = {**SAVE, "judge_provider": "", "judge_model": ""}
        assert post(client, "review-settings", **alone).status_code == 200
        before = shown(client)["create"]
        assert before["writer"]["model"] == "analysis" and before["checker"] is None
        first = candidates(client)[41][0]
        assert post(client, "organize/stories-issues/runs").status_code == 200
        model = Hand({41: first, 42: first, 45: "none"}, {"Themes": WRITES["Themes"]})
        carried(repo, model, repo.organize_issues)
        assert not [entry for entry in model.asked if entry[2] == "check"]
        after = shown(client)
        assert after["lists"]["created"] == 0 and after["waiting"]["drafts"] == 1
        draft = next(line for line in lines(client, "needs_you") if line["case"] == "draft_single")
        assert "No second model of another maker" in draft["reason"]
        # One word makes what only waited for a word.
        many = post(client, "organize/stories-issues/groups", group="drafts", choice="make")
        assert many.json() == {"answered": 1, "skipped": 0, "left": 0}
        assert len(issues_of(repo, made["Themes"])) == 2

        # A model named to write is the writer, and the one that answers checks it.
        named = {**alone, "writer_provider": "fixture", "writer_model": "other"}
        assert post(client, "review-settings", **named).status_code == 200
        roles = shown(client)["create"]
        assert (roles["writer"]["model"], roles["checker"]["model"]) == ("other", "analysis")

        # Where the ranking service cannot read a record, nothing is made on words alone.
        def broken(texts: list[str]) -> tuple[str, list[bytes]]:
            raise RuntimeError("The ranking service did not answer.")

        repo.organize_create.embed = broken
        with repo.sessions.begin() as db:
            story = db.get_one(ProjectUserStory, data["Audit executions"])
            story.description = "Keep a durable audit entry for each execution, for a year."
        assert shown(client)["create"]["to_ask"] >= 1
        assert post(client, "organize/stories-issues/runs").status_code == 200
        carried(
            repo, Hand({}, {"Audit executions": WRITES["Audit executions"]}), repo.organize_issues
        )
        unverified = next(
            line for line in lines(client, "needs_you") if line["case"] == "draft_unverified"
        )
        assert "could not be made" in unverified["reason"]
        assert issues_of(repo, data["Audit executions"]) == []
    finally:
        client.close()


def test_an_epic_with_no_story_gets_its_stories_and_a_broad_epic_is_split_at_your_word(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        data = ids(repo)
        pid = data["project"]
        first_half: list[UUID] = []
        second_half: list[UUID] = []
        with repo.sessions.begin() as db:
            billing = ProjectEpic(project_id=pid, title="Billing", description="Invoices.")
            reports = ProjectEpic(project_id=pid, title="Reports", description="")
            broad = ProjectEpic(project_id=pid, title="Platform", description="Everything.")
            db.add_all([billing, reports, broad])
            db.flush()
            for place in range(24):
                topic = "queue" if place < 12 else "ledger"
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
                (first_half if place < 12 else second_half).append(story.id)
            keys = {"Billing": billing.id, "Reports": reports.id, "Platform": broad.id}
        repo.planning_links.reconcile_project("falcone")
        assert post(client, "review-settings", **SAVE).status_code == 200
        before = shown(client, EPICS)
        assert before["stands"]["epics_with_no_story"] == 2
        assert before["create"]["checks"]["new_stories"]["to_ask"] == 2
        assert before["create"]["splits"] == 1 and before["can_start"]

        assert post(client, "organize/epics-stories/runs").status_code == 200
        model = Hand(
            {},
            {
                "Billing": written(
                    {
                        "title": "Send an invoice",
                        "sentences": [s("A customer receives an invoice.", "E")],
                        "done_when": [s("An invoice is sent.", "E")],
                    }
                ),
                "Reports": other("nothing"),
            },
        )
        carried(repo, model, repo.organize_epics)
        after = shown(client, EPICS)
        assert after["run"]["state"] == "complete" and after["lists"]["created"] == 1
        (line,) = lines(client, "created", EPICS)
        assert line["subject"]["title"] == "Send an invoice" and line["made"]["kind"] == "stories"
        assert line["action"]["for"]["title"] == "Billing"
        with repo.sessions() as db:
            under = db.scalars(
                select(ProjectStoryEpic.epic_id).where(
                    ProjectStoryEpic.story_id == UUID(line["made"]["id"])
                )
            ).all()
        assert under == [keys["Billing"]]
        assert shown(client, EPICS)["stands"]["epics_with_no_story"] == 1

        def answer(case: dict[str, Any], choice: str, **more: Any) -> Any:
            path = f"organize/epics-stories/lines/{case['id']}"
            return post(client, path, choice=choice, **more)

        needs = {case["case"]: case for case in lines(client, "needs_you", EPICS)}
        # Nothing says what the Epic should hold: no Story is invented for it.
        empty = needs["nothing"]
        assert empty["subject"]["title"] == "Reports"
        assert answer(empty, "close").status_code == 200
        with repo.sessions() as db:
            assert db.get_one(ProjectEpic, keys["Reports"]).closed_at is not None

        # The Epic that holds too many Stories is split only at the administrator's word.
        split = needs["split"]
        assert split["subject"]["title"] == "Platform"
        stays, leaves = split["candidates"]
        assert stays["stays"] and not leaves["stays"] and leaves["stories"] == 12
        assert answer(split, "split", titles=["Ledger"]).status_code == 200
        with repo.sessions() as db:
            ledger = db.scalars(select(ProjectEpic).where(ProjectEpic.title == "Ledger")).one()
            under_new = set(
                db.scalars(
                    select(ProjectStoryEpic.story_id).where(ProjectStoryEpic.epic_id == ledger.id)
                )
            )
            made_epic = ledger.id
        # One of the two groups went to the new Epic, whole.
        assert under_new in (set(first_half), set(second_half))
        done = next(case for case in lines(client, "done", EPICS) if case["case"] == "split_made")
        # Taken back, the new Epic is gone and the Stories stand as they did.
        undone = post(client, f"organize/epics-stories/lines/{done['id']}/undo")
        assert undone.status_code == 200, undone.text
        with repo.sessions() as db:
            assert db.get_one(ProjectEpic, made_epic).archived_at is not None
            assert {
                epic
                for epic in db.scalars(
                    select(ProjectStoryEpic.epic_id).where(
                        ProjectStoryEpic.story_id.in_([*first_half, *second_half])
                    )
                )
            } == {keys["Platform"]}
    finally:
        client.close()
