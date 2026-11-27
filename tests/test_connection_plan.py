"""A connection gets a plan before any model is asked: what of the two projects it
touches and how that will be verified. At the administrator's word the plan is carried
out, a second model reviews each proposal, and the results are three lists that can be
answered and undone."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from engineering_control.dashboard import connection_plan, ranking, review_settings
from engineering_control.db.models import (
    BacklogItem,
    ConnectionFinding,
    DocFile,
    ProjectRecord,
    ProjectUserStory,
    ProviderAccount,
    ReviewSettings,
    WorkspaceCandidate,
    WorkspaceImport,
)
from engineering_control.models.service import Question, Reply
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_delivery import vector
from tests.test_needs_you import SAVE
from tests.test_project_connections import ROOT, F, check, due, write

LONG = (
    "Scheduled functions are started by the triggers of falcone-serverless, which keeps "
    "the timer and calls the function when it is due, on every node of the cluster."
)


class Two:
    """Two models behind one account. Each answers a question about some records with
    what it was told to say of each, by the record's title."""

    def __init__(self, says: dict[str, dict[str, list[dict[str, Any]]]]) -> None:
        self.says = says
        self.asked: list[tuple[str, list[str]]] = []

    async def ask(self, question: Question, *, retry: bool) -> Reply:
        ((_provider, model),) = question.choices
        prompt = json.loads(question.prompt)
        found: list[dict[str, Any]] = []
        titles = []
        for record in prompt["records"]:
            source = record["source"]
            titles.append(source["title"])
            options = {option["title"]: option for option in record["target_options"]}
            for said in self.says.get(model, {}).get(source["title"], []):
                target = options[said["to"]]["id"] if said.get("to") else ""
                found.append(
                    {
                        "source": source["id"],
                        "action": said["action"],
                        "target": target,
                        "kind": said["kind"],
                        "parents": [options[title]["id"] for title in said.get("parents", [])],
                        "title": said.get("title", ""),
                        "description": said.get("description", ""),
                        "reason": said.get("reason", "The two texts describe the same timer."),
                    }
                )
        self.asked.append((model, titles))
        usage = {"tokens": 60, "input": 50, "output": 10, "cost": 0.001}
        return Reply({"suggestions": found}, "", "fixture", model, uuid4(), "Test", usage, 0.1, 1)


def seed(repo: Any, monkeypatch: Any) -> dict[str, Any]:
    """Two projects with a few records each, read by the ranking service: one Story of
    falcone names the other project and lies close to one of its Stories, one is
    recorded in both, and two have nothing to do with the other project. An issue of
    falcone names an issue of falcone-serverless."""
    for module in (review_settings, connection_plan):
        monkeypatch.setattr(module, "family_of", lambda provider, model: model)
    ids: dict[str, Any] = {}
    for project, kind, title, text, parent in (
        ("falcone", "epics", "Functions", "Run code on the platform.", None),
        (
            "falcone",
            "stories",
            "Run a function on a schedule",
            "Uses the triggers of falcone-serverless.",
            "Functions",
        ),
        ("falcone", "stories", "Console theme", "Colours of the console.", "Functions"),
        ("falcone", "stories", "Durable timers", "A timer survives a restart.", "Functions"),
        ("falcone-serverless", "epics", "Triggers", "What starts a function.", None),
        ("falcone-serverless", "stories", "Cron triggers", "Start by a cron line.", "Triggers"),
        ("falcone-serverless", "stories", "Timers that last", "Timers are kept.", "Triggers"),
    ):
        parents = [UUID(ids[parent])] if parent else []
        ids[title] = repo.planning.save(project, kind, title, text, parents)["id"]
    with repo.sessions.begin() as db:
        here = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
        there = db.scalars(
            select(ProjectRecord).where(ProjectRecord.name == "falcone-serverless")
        ).one()
        ids["falcone"], ids["falcone-serverless"] = here.id, there.id
        db.add(
            ProviderAccount(
                name="Test",
                provider="fixture",
                state="ready",
                models=["analysis", "other"],
                prices={m: {"input": 1.0, "output": 2.0} for m in ("analysis", "other")},
                sealed_key="test",
            )
        )
        for project in (here, there):
            db.add(
                WorkspaceImport(
                    project_id=project.id,
                    repository=project.source_repository,
                    branch="main",
                    stage="complete",
                )
            )
        for project, number, title, text in (
            (here, 7, "Call the trigger service", "Depends on gntik-ai/falcone-serverless#12."),
            (there, 12, "Trigger endpoint", "Accept a schedule."),
        ):
            item = BacklogItem(
                project_id=project.id,
                source="github",
                state="proposed",
                repository=project.source_repository,
                issue_number=number,
                issue_state="open",
                title=title,
                description=text,
                workspace_review="approved",
            )
            db.add(item)
            db.flush()
            ids[title] = str(item.id)
        db.add(
            DocFile(
                project_id=here.id,
                path="docs/functions.md",
                blob_sha="a" * 40,
                text=f"# Functions\n{LONG}\n",
                sections=[{"level": 1, "title": "Functions", "line": 1}],
                facts={},
            )
        )
        for project, kind, title, values in (
            (here, "epic", "Functions", (0, 0, 0, 1)),
            (here, "story", "Run a function on a schedule", (1, 0, 0, 0)),
            (here, "story", "Console theme", (0, 0, 1, 0)),
            (here, "story", "Durable timers", (0, 1, 0, 0)),
            (here, "issue", "Call the trigger service", (0, 0, 1, 0)),
            (there, "epic", "Triggers", (0.6, 0.8, 0, 0)),
            (there, "story", "Cron triggers", (0.8, 0.6, 0, 0)),
            (there, "story", "Timers that last", (0, 1, 0, 0)),
            (there, "issue", "Trigger endpoint", (0, 0, 0, -1)),
        ):
            ranking.keep_vector(db, project.id, kind, UUID(ids[title]), "m", "d", vector(*values))
    return ids


def connect(client: Any, repo: Any) -> str:
    key: str = write(client, F, other="falcone-serverless", relation="depends_on").json()["id"]
    due(repo, key)
    check(repo)
    return key


def lines(client: Any, path: str, listed: str) -> list[dict[str, Any]]:
    found = client.get(f"{path}/lines/{listed}")
    assert found.status_code == 200, found.text
    items: list[dict[str, Any]] = found.json()["items"]
    return items


def test_a_new_connection_gets_its_plan_and_no_model_is_asked(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        seed(repo, monkeypatch)
        assert post(client, "review-settings", **SAVE).status_code == 200
        key = connect(client, repo)
        path = f"{F}/{key}"
        shown = client.get(path).json()
        assert shown["status"] == "planned" and shown["lists"] == {
            "needs_you": 0,
            "done": 0,
            "created": 0,
        }
        found = client.get(path + "/plan").json()
        plan = found["plan"]
        assert found["can_carry_out"] and found["run"] is None and not plan["blocked"]
        assert plan["relation"] == "falcone depends on falcone-serverless"
        assert plan["meaning"] and sorted(p["read"] for p in plan["projects"]) == [4, 5]
        update = {entry["key"]: entry for entry in plan["update"]}
        # What the text of one project states of the other is a link by rule.
        assert update["stated"]["count"] == 1 and update["stated"]["by"] == "rule"
        # The Story and the issue that name the other project.
        assert update["names"]["count"] == 2
        assert update["names"]["by_project"] == {"falcone": 2, "falcone-serverless": 0}
        # Close by meaning: the timers of both, and the Epic and Story of triggers.
        assert update["close"]["count"] == 4
        # The same thing recorded in both.
        assert update["twins"]["count"] == 1
        twin = update["twins"]["examples"][0]["title"]
        assert (
            "Durable timers (falcone)" in twin and "Timers that last (falcone-serverless)" in twin
        )
        # What nothing ties to the other project is not put before a model.
        assert update["untouched"]["count"] == 3 and update["untouched"]["by"] == "nobody"
        # The part of the documentation that names the other project is listed.
        assert update["documents"]["count"] == 1
        assert update["documents"]["examples"][0]["ref"] == "docs/functions.md#1"
        # How it will be verified: the models, of two makers, and what is applied.
        ask = plan["ask"]
        assert (ask["records"], ask["questions"]) == (6, 1)
        assert ask["answer"]["model"] == "analysis" and ask["judge"]["model"] == "other"
        assert ask["dollars"] is not None and not ask["over"] and ask["limit"] == 5
        steps = {step["step"]: step["says"] for step in plan["verify"]}
        # What counts as close is said, and that nothing was learned yet.
        measured = plan["measured"]
        assert (measured["threshold"], measured["learned_from"]) == (0.6, 0)
        assert measured["nearest"]["95"] == 1.0 and measured["linked"] is None
        assert "from a closeness of 0.60" in steps["Candidates"]
        assert "analysis (fixture) is asked about 6 records" in steps["Answer"]
        assert "other (fixture), of another maker" in steps["Review"]
        assert "never applied" in steps["What is applied"]
        # Nothing was asked, and nothing changed.
        with repo.sessions() as db:
            assert [f.status for f in db.scalars(select(ConnectionFinding))] == ["pending"]
        # The plan is the same from the other project's page.
        other = client.get(f"{ROOT}falcone-serverless/connections/{key}/plan").json()
        assert other["plan"]["ask"] == ask and other["owner"] == "falcone"
    finally:
        client.close()


SAYS: dict[str, dict[str, list[dict[str, Any]]]] = {
    "analysis": {
        "Run a function on a schedule": [
            {"action": "link", "kind": "stories", "to": "Cron triggers"},
            {
                "action": "create",
                "kind": "issues",
                "parents": ["Cron triggers"],
                "title": "Expose the next run time",
                "description": "The platform shows when a schedule fires next.",
            },
        ],
        "Cron triggers": [
            {
                "action": "edit",
                "kind": "stories",
                "to": "Run a function on a schedule",
                "title": "Run a function on a cron schedule",
                "description": "Uses the cron triggers of falcone-serverless.",
            }
        ],
        "Durable timers": [{"action": "link", "kind": "stories", "to": "Timers that last"}],
    },
    "other": {
        "Run a function on a schedule": [
            {"action": "link", "kind": "stories", "to": "Cron triggers"}
        ],
        "Cron triggers": [
            {
                "action": "edit",
                "kind": "stories",
                "to": "Run a function on a schedule",
                "title": "Run a function on a cron schedule",
                "description": "Uses the cron triggers of falcone-serverless.",
            }
        ],
    },
}


def test_the_plan_is_carried_out_into_three_lists_that_can_be_undone(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ids = seed(repo, monkeypatch)
        assert post(client, "review-settings", **SAVE).status_code == 200
        key = connect(client, repo)
        path = f"{F}/{key}"
        models = Two(SAYS)
        assert write(client, path + "/carry-out").status_code == 200
        assert write(client, path + "/carry-out").status_code == 409
        check(repo, models)
        # Both models were asked about the records of each project, and about no other.
        assert sorted(model for model, _ in models.asked) == [
            "analysis",
            "analysis",
            "other",
            "other",
        ]
        asked = {title for _, titles in models.asked for title in titles}
        assert "Console theme" not in asked and "Functions" not in asked
        assert "Run a function on a schedule" in asked and "Cron triggers" in asked
        shown = client.get(path + "/plan").json()
        run = shown["run"]
        assert run["state"] == "complete" and run["stage"] == "done" and run["cost"] == 0.004
        assert shown["lists"] == {"needs_you": 3, "done": 2, "created": 0}
        assert all(stage["reached"] for stage in run["stages"])
        stages = {stage["stage"]: stage for stage in run["stages"]}
        assert (stages["read"]["records"], stages["read"]["none"]) == (9, 3)
        assert (stages["verify"]["settled"], stages["verify"]["in_doubt"]) == (2, 3)
        # Nothing is left to carry out.
        assert not shown["can_carry_out"] and "Nothing is left" in shown["why_not"]
        assert client.get(path).json()["status"] == "ready"

        # Done: what the text states, and the link both models made. Each names the
        # project of its records.
        done = {line["subject"]["title"]: line for line in lines(client, path, "done")}
        assert set(done) == {"Call the trigger service", "Run a function on a schedule"}
        stated = done["Call the trigger service"]
        assert stated["said"][0]["by"] == "the text" and stated["can_undo"]
        both = done["Run a function on a schedule"]
        assert both["subject"]["in"]["name"] == "falcone"
        assert both["candidates"][0]["in"]["name"] == "falcone-serverless"
        assert both["candidates"][0]["title"] == "Cron triggers"
        assert [said["answer"] for said in both["said"]] == ["link", "the same"]

        # Needs you, the most doubtful first: what only the first model said, then the
        # changed text both proposed, which is never applied by itself.
        needs = lines(client, path, "needs_you")
        assert [line["case"] for line in needs] == ["missing", "link", "text"] or [
            line["case"] for line in needs
        ] == ["link", "missing", "text"]
        by_case = {line["case"]: line for line in needs}
        assert by_case["missing"]["doubt"] == 0.8 and by_case["text"]["doubt"] == 0.3
        assert by_case["missing"]["action"]["title"] == "Expose the next run time"
        text = by_case["text"]
        assert text["now"] == {
            "title": "Run a function on a schedule",
            "description": "Uses the triggers of falcone-serverless.",
        }
        assert text["action"]["title"] == "Run a function on a cron schedule"
        assert text["subject"]["in"]["name"] == "falcone-serverless"

        def answer(line: dict[str, Any], accept: bool) -> Any:
            return write(client, f"{path}/lines/{line['id']}", accept=accept)

        # A changed text is applied at the administrator's word, and put back by undo.
        assert answer(text, True).json()["state"] == "applied"
        with repo.sessions() as db:
            story = db.get(ProjectUserStory, UUID(ids["Run a function on a schedule"]))
            assert story.title == "Run a function on a cron schedule"
        (changed,) = [line for line in lines(client, path, "done") if line["case"] == "text"]
        assert write(client, f"{path}/lines/{changed['id']}/undo").status_code == 200
        with repo.sessions() as db:
            story = db.get(ProjectUserStory, UUID(ids["Run a function on a schedule"]))
            assert story.title == "Run a function on a schedule"
            assert story.description == "Uses the triggers of falcone-serverless."
        assert "text" in {line["case"] for line in lines(client, path, "needs_you")}

        # A record that is missing is sent to the project that has to make it, as a
        # proposal: it is listed under Created, and undoing withdraws it.
        assert answer(by_case["missing"], True).status_code == 200
        (made,) = lines(client, path, "created")
        assert made["made"]["kind"] == "issues" and made["made"]["sources"][0]["title"] == (
            "Run a function on a schedule"
        )
        with repo.sessions() as db:
            (candidate,) = db.scalars(
                select(WorkspaceCandidate).where(WorkspaceCandidate.origin == "connection")
            )
            assert candidate.project_id == ids["falcone-serverless"]
            assert candidate.title == "Expose the next run time"
        assert write(client, f"{path}/lines/{made['id']}/undo").status_code == 200
        with repo.sessions() as db:
            assert not list(
                db.scalars(
                    select(WorkspaceCandidate).where(WorkspaceCandidate.origin == "connection")
                )
            )
        assert lines(client, path, "created") == []

        # Dismissed, it leaves the list; answered twice, it is refused.
        assert answer(by_case["link"], False).status_code == 200
        assert answer(by_case["link"], False).status_code == 409
        assert "link" not in {line["case"] for line in lines(client, path, "needs_you")}

        # A link that was recorded comes off by undo and waits for the administrator.
        assert write(client, f"{path}/lines/{both['id']}/undo").status_code == 200
        with repo.sessions() as db:
            found = [(f.origin, f.status) for f in db.scalars(select(ConnectionFinding))]
        assert ("reference", "approved") in found
        assert sorted(status for _, status in found) == [
            "approved",
            "dismissed",
            "pending",
            "pending",
            "pending",
        ]
        # The whole run is undone: what still stands of it is taken back.
        undone = write(client, f"{path}/runs/{run['id']}/undo")
        assert undone.json() == {"undone": 1, "kept": 0}
        assert client.get(path + "/plan").json()["lists"]["done"] == 0
        assert write(client, f"{path}/lines/{uuid4()}/undo").status_code == 404
    finally:
        client.close()


def test_a_change_brings_a_new_plan_and_asks_nobody(tmp_path: Path, monkeypatch: Any) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ids = seed(repo, monkeypatch)
        assert post(client, "review-settings", **SAVE).status_code == 200
        key = connect(client, repo)
        path = f"{F}/{key}"
        models = Two(SAYS)
        assert write(client, path + "/carry-out").status_code == 200
        check(repo, models)
        asked = len(models.asked)
        # A record of one project changes: the connection gets a new plan.
        repo.planning.save(
            "falcone",
            "stories",
            "Console theme",
            "Colours of the console, as falcone-serverless names them.",
            [UUID(ids["Functions"])],
            UUID(ids["Console theme"]),
        )
        due(repo, key)
        check(repo, models)
        assert len(models.asked) == asked
        shown = client.get(path + "/plan").json()
        assert client.get(path).json()["status"] == "planned" and shown["can_carry_out"]
        # It names what a model was asked about already, and what the run before left.
        # The changed Story is asked about, and so are the records it is offered to;
        # what did not change is not asked about again.
        assert shown["plan"]["ask"]["records"] == 4 and shown["plan"]["ask"]["already"] == 3
        assert shown["run"]["state"] == "complete" and shown["lists"]["needs_you"] == 3
        # Carried out, only what changed is asked about.
        assert write(client, path + "/carry-out").status_code == 200
        check(repo, models)
        again = {title for _, titles in models.asked[asked:] for title in titles}
        assert again == {"Console theme", "Triggers", "Cron triggers", "Timers that last"}
    finally:
        client.close()


def test_a_plan_says_when_it_cannot_be_carried_out(tmp_path: Path, monkeypatch: Any) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ids = seed(repo, monkeypatch)
        # No model is chosen to answer, here or in Settings.
        key = connect(client, repo)
        path = f"{F}/{key}"
        shown = client.get(path + "/plan").json()
        assert not shown["can_carry_out"] and "Choose the model" in shown["why_not"]
        assert client.get(path).json()["status"] == "needs_model"
        assert write(client, path + "/carry-out").status_code == 422
        # With a model and no judge, it says that every suggestion will wait.
        assert (
            write(client, path + "/analyze", provider="fixture", model="analysis").status_code
            == 200
        )
        due(repo, key)
        check(repo)
        plan = client.get(path + "/plan").json()["plan"]
        steps = {step["step"]: step["says"] for step in plan["verify"]}
        assert plan["ask"]["judge"] is None and "No judge is set" in steps["Review"]
        assert "every suggestion is in doubt" in steps["Review"]
        # Text of two projects goes only to a maker both allow.
        with repo.sessions.begin() as db:
            db.add(ReviewSettings(project_id=ids["falcone-serverless"], makers=["anthropic"]))
        review_settings.forget(ids["falcone-serverless"])
        write(client, path + "/analyze")
        due(repo, key)
        check(repo)
        shown = client.get(path + "/plan").json()
        assert not shown["can_carry_out"]
        assert "falcone-serverless may not be sent to" in shown["why_not"]
        assert write(client, path + "/carry-out").status_code == 422
    finally:
        client.close()


def test_a_plan_over_the_limit_waits_for_the_word(tmp_path: Path, monkeypatch: Any) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        seed(repo, monkeypatch)
        with repo.sessions.begin() as db:
            account = db.scalars(select(ProviderAccount)).one()
            account.prices = {m: {"input": 9000.0, "output": 9000.0} for m in account.models}
        assert post(client, "review-settings", **SAVE).status_code == 200
        key = connect(client, repo)
        path = f"{F}/{key}"
        ask = client.get(path + "/plan").json()["plan"]["ask"]
        assert ask["over"] and ask["dollars"] > ask["limit"]
        refused = write(client, path + "/carry-out")
        assert refused.status_code == 409 and "left of this month's limit" in refused.text
        assert write(client, path + "/carry-out", within_limit=True).status_code == 200
        # Stopped, the run is held and nothing is asked; resumed, it goes on.
        control = client.get(path).json()["control_id"]
        assert write(client, path + "/pause", control_id=control, paused=True).status_code == 200
        models = Two(SAYS)
        due(repo, key)
        check(repo, models)
        assert not models.asked and client.get(path + "/plan").json()["run"]["state"] == "held"
        control = client.get(path).json()["control_id"]
        assert write(client, path + "/pause", control_id=control, paused=False).status_code == 200
        check(repo, models)
        assert models.asked and client.get(path + "/plan").json()["run"]["state"] == "complete"
    finally:
        client.close()


@pytest.mark.parametrize("meaning", [True, False])
def test_records_are_offered_what_is_nearest_in_the_other_project(
    tmp_path: Path, monkeypatch: Any, meaning: bool
) -> None:
    from engineering_control.dashboard.connection_records import catalog

    client, repo = _authenticated_client(tmp_path)
    try:
        ids = seed(repo, monkeypatch)
        with repo.sessions() as db:
            projects = [
                db.get(ProjectRecord, ids[name]) for name in ("falcone", "falcone-serverless")
            ]
            sides = [catalog(db, project) for project in projects]
            across = (
                connection_plan.Across.read(db, sides) if meaning else connection_plan.Across(sides)
            )
        assert across.meaning is meaning
        story = across.items[0][ids["Run a function on a schedule"]]
        theme = across.items[0][ids["Console theme"]]
        offered = [item.title for item in across.near(0, story)]
        if meaning:
            # By meaning the Story of cron triggers comes first of its kind.
            assert offered.index("Cron triggers") < offered.index("Timers that last")
            assert across.touched(0, story) == "names" and across.touched(0, theme) is None
            assert across.closest(0, story)[1].title == "Cron triggers"
            # What "close" is, is learned from the links that were approved: with
            # thirty known pairs that lie as close as can be, a record whose nearest
            # lies at 0.8 is no longer close. It is never set above 0.95.
            epic = across.items[1][ids["Triggers"]]
            assert across.touched(1, epic) == "close" and across.close == connection_plan.CLOSE
            known = (0, ids["Durable timers"], ids["Timers that last"])
            assert across.learn([known] * 29) and across.learned == 0
            spread = across.learn([known] * 30)
            assert across.learned == 30 and across.close == 0.95
            assert [round(value, 2) for value in spread[:2]] == [1.0, 1.0]
            assert across.touched(1, epic) is None
            assert across.touched(1, across.items[1][ids["Timers that last"]]) == "close"
        else:
            # Without the ranking service every record is put before the models.
            assert across.touched(0, theme) == "words"
            assert {"Cron triggers", "Triggers"} <= set(offered)
    finally:
        client.close()


def test_a_run_of_one_project_reads_the_projects_connected_to_it(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ids = seed(repo, monkeypatch)
        base = ROOT + "falcone/organize/"
        story = ids["Run a function on a schedule"]
        # Not connected yet: only the project's own Epics are offered.
        alone = client.get(f"{base}candidates/{story}").json()
        assert alone["scope"] == [] and {c["project"] for c in alone["candidates"]} == {"falcone"}
        assert client.get(base + "ground?fresh=true").json()["scope"] == []
        key = connect(client, repo)
        # Connected, the Epic of the other project is offered too, and comes first: it
        # lies closest by meaning. Each candidate says which project it belongs to.
        found = client.get(f"{base}candidates/{story}").json()
        assert found["scope"] == ["falcone-serverless"]
        first = found["candidates"][0]
        assert (first["project"], first["title"], first["own"]) == (
            "falcone-serverless",
            "Triggers",
            False,
        )
        assert first["closeness"] == 0.6
        assert {c["title"] for c in found["candidates"]} == {"Triggers", "Functions"}
        # The count says what the connected project brings.
        (brings,) = client.get(base + "ground?fresh=true").json()["scope"]
        assert (brings["project"], brings["relation"], brings["connection"]) == (
            "falcone-serverless",
            "depends_on",
            key,
        )
        assert brings["records"] == {"epics": 1, "stories": 2, "issues": 1}
        assert brings["links"] == {"approved": 0, "pending": 1}
        # From the other side the relation reads the other way.
        (back,) = client.get(ROOT + "falcone-serverless/organize/ground?fresh=true").json()["scope"]
        assert (back["project"], back["relation"]) == ("falcone", "used_by")
        assert client.get(f"{base}candidates/{uuid4()}").status_code == 404
        # A run of one project carries the scope it reads.
        run = repo.organize.propose("falcone", "stories_issues")
        shown = client.get(base + "stories_issues").json()["run"]
        assert shown["id"] == str(run) and shown["scope"] == [
            str(ids["falcone"]),
            str(ids["falcone-serverless"]),
        ]
    finally:
        client.close()


def test_a_stopped_connection_is_not_woken_and_still_shows_its_plan(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        seed(repo, monkeypatch)
        assert post(client, "review-settings", **SAVE).status_code == 200
        key = write(client, F, other="falcone-serverless", relation="depends_on").json()["id"]
        path = f"{F}/{key}"
        # Stopped before the worker ever prepared its plan.
        control = client.get(path).json()["control_id"]
        assert write(client, path + "/pause", control_id=control, paused=True).status_code == 200
        due(repo, key)
        assert repo.connection_analysis.claim() is None
        # The plan is worked out when it is asked for, and nothing is written for it.
        shown = client.get(path + "/plan").json()
        assert shown["waits"] and shown["can_carry_out"] and shown["plan"]["ask"]["records"] == 6
        assert client.get(path).json()["status"] == "paused"
        with repo.sessions() as db:
            assert not list(db.scalars(select(ConnectionFinding)))
        # Carrying it out is the administrator's word to go on.
        assert write(client, path + "/carry-out").status_code == 200
        models = Two(SAYS)
        check(repo, models)
        assert models.asked and client.get(path + "/plan").json()["run"]["state"] == "complete"
        assert client.get(path + "/plan").json()["lists"]["done"] == 2
    finally:
        client.close()
