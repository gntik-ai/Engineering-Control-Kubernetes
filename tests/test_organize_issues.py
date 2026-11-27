"""Organize, User Stories and Issues: one button checks every open Issue that has no
User Story, applies what is settled where the project's record allows, and puts the
rest before the administrator, who answers a case or takes an applied one back."""

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from engineering_control.dashboard import review_settings
from engineering_control.db.models import (
    BacklogItem,
    ConnectionFinding,
    ProjectRecord,
    ReorgBatch,
    ReorgLine,
    ReviewSettings,
)
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_judging import Readers
from tests.test_needs_you import SAVE, candidates, placed
from tests.test_orphans import run
from tests.test_project_connections import F, write
from tests.test_review_settings import BASE, ready

HERE = BASE + "organize/stories-issues"


def shown(client: Any) -> dict[str, Any]:
    found = client.get(HERE)
    assert found.status_code == 200, found.text
    body: dict[str, Any] = found.json()
    return body


def lines(client: Any, listed: str) -> list[dict[str, Any]]:
    found = client.get(f"{HERE}/lines/{listed}")
    assert found.status_code == 200, found.text
    items: list[dict[str, Any]] = found.json()["items"]
    return items


def asked(repo: Any, readers: Readers) -> None:
    """The check asks, and the run gathers what it answered."""
    run(repo, readers)
    assert repo.organize_issues.tick()


def test_one_button_checks_the_issues_and_puts_what_is_in_doubt_before_you(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        assert post(client, "review-settings", **SAVE).status_code == 200
        before = shown(client)
        # Four open Issues that are no bug: one is under a User Story, three have none.
        assert before["stands"] == {
            "issues": 4,
            "settled": 1,
            "in_doubt": 0,
            "none": 3,
            "left": 0,
            "bugs_left_out": 0,
        }
        plan = before["plan"]
        assert (plan["to_ask"], plan["settled"], plan["unsupported"]) == (3, 0, 0)
        assert plan["answer"]["model"] == "analysis" and plan["judge"]["model"] == "other"
        # What is settled is proposed, not applied, until the project's record allows it.
        assert plan["applies"]["in_force"] is False and plan["applies"]["setting"] == "propose"
        assert before["can_start"] and before["run"] is None

        offered = candidates(client)
        first, other = offered[41][0], offered[41][1]
        assert post(client, "organize/stories-issues/runs").status_code == 200
        under_way = shown(client)
        assert under_way["run"]["state"] == "running" and not under_way["can_start"]
        assert under_way["run"]["asking"]["total"] == 3
        assert post(client, "organize/stories-issues/runs").status_code == 409
        # While the check asks, nothing is gathered.
        assert not repo.organize_issues.tick()
        asked(
            repo,
            Readers(
                {
                    ("analysis", 41): [first],
                    ("other", 41): [first],
                    ("analysis", 42): [other],
                    ("other", 42): [other],
                    ("analysis", 45): ["unclear"],
                }
            ),
        )
        after = shown(client)
        run_ = after["run"]
        assert run_["state"] == "complete" and all(stage["reached"] for stage in run_["stages"])
        stages = {stage["stage"]: stage for stage in run_["stages"]}
        assert (stages["read"]["records"], stages["read"]["settled"]) == (4, 1)
        assert (stages["match"]["records"], stages["verify"]["in_doubt"]) == (3, 3)
        assert after["lists"] == {"needs_you": 3, "done": 0, "created": 0}
        assert after["stands"]["in_doubt"] == 3 and after["stands"]["none"] == 0
        assert run_["cost"] > 0

        # Needs you, the most doubtful first: what could not tell, what the texts speak
        # against, and what both models and the texts agree on.
        needs = lines(client, "needs_you")
        assert [(line["subject"]["ref"], line["case"]) for line in needs] == [
            ("#45", "unclear"),
            ("#42", "texts"),
            ("#41", "settled"),
        ]
        settled = needs[2]
        assert "not applied by itself" in settled["reason"]
        chosen = [entry for entry in settled["candidates"] if entry["chosen"]]
        assert [entry["title"] for entry in chosen] == [first] and chosen[0]["judge"]
        assert [said["by"] for said in settled["said"]] == ["analysis", "other"]
        assert settled["said"][0]["story"] == first and settled["said"][0]["passages"]
        assert settled["subject"]["url"].endswith("/issues/41")

        def answer(line: dict[str, Any], choice: str, story: str | None = None) -> Any:
            body = {"choice": choice, **({"story_id": story} if story else {})}
            return post(client, f"organize/stories-issues/lines/{line['id']}", **body)

        # The administrator's word puts the Issue under the Story, and undo takes it back.
        assert answer(settled, "story", str(uuid4())).status_code == 422
        assert answer(settled, "story", chosen[0]["id"]).status_code == 200
        assert placed(repo, 41) and answer(settled, "story", chosen[0]["id"]).status_code == 409
        (done,) = lines(client, "done")
        assert (done["subject"]["ref"], done["case"], done["can_undo"]) == ("#41", "placed", True)
        assert shown(client)["stands"]["settled"] == 2
        assert post(client, f"organize/stories-issues/lines/{done['id']}/undo").status_code == 200
        assert not placed(repo, 41) and lines(client, "done") == []
        back = next(line for line in lines(client, "needs_you") if line["subject"]["ref"] == "#41")
        assert back["case"] == "undone" and "You took this back" in back["reason"]

        # Left as it is, a case leaves the list; the Issue still has no User Story.
        assert answer(needs[0], "leave").status_code == 200
        assert "#45" not in {line["subject"]["ref"] for line in lines(client, "needs_you")}
        # Another Story than the one the models chose can be chosen.
        (other_story,) = [e for e in needs[1]["candidates"] if e["title"] == first]
        assert answer(needs[1], "story", other_story["id"]).status_code == 200
        assert placed(repo, 42)

        # A second run asks nobody again, and keeps what the administrator took back.
        again = shown(client)
        assert again["plan"]["to_ask"] == 0 and again["stands"]["left"] == 2
        assert post(client, f"organize/stories-issues/runs/{run_['id']}/undo").json() == {
            "undone": 1,
            "kept": 0,
        }
        assert not placed(repo, 42)
        assert client.get(f"{HERE}/lines/elsewhere").status_code == 422
        assert post(client, f"organize/stories-issues/lines/{uuid4()}/undo").status_code == 404
    finally:
        client.close()


def test_what_is_settled_is_applied_where_the_record_allows(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        numbers = {"alone_from": 1, "sample_least": 1, "alone_sample": 50}
        saved = post(client, "review-settings", **SAVE, numbers=numbers, alone={"place": "apply"})
        assert saved.status_code == 200, saved.text
        # The project's record: one sampled line of a batch was judged right.
        with repo.sessions.begin() as db:
            project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
            batch = ReorgBatch(project_id=project.id, kind="place", state="approved")
            db.add(batch)
            db.flush()
            db.add(
                ReorgLine(
                    batch_id=batch.id,
                    project_id=project.id,
                    link_kind="issue_story",
                    child_id=uuid4(),
                    parent_id=uuid4(),
                    confidence=1.0,
                    reason="Judged before.",
                    sampled=True,
                    verdict="right",
                    state="skipped",
                )
            )
        assert shown(client)["plan"]["applies"]["in_force"] is True
        first = candidates(client)[41][0]
        assert post(client, "organize/stories-issues/runs").status_code == 200
        asked(
            repo,
            Readers(
                {
                    ("analysis", 41): [first],
                    ("other", 41): [first],
                    ("analysis", 42): [first],
                    ("other", 42): [first],
                    ("analysis", 45): ["unclear"],
                }
            ),
        )
        # Both Issues the models agree on are under their Story, without the
        # administrator; the one that could not be told waits.
        assert placed(repo, 41) and placed(repo, 42)
        after = shown(client)
        assert after["lists"] == {"needs_you": 1, "done": 2, "created": 0}
        assert after["stands"] == {
            "issues": 4,
            "settled": 3,
            "in_doubt": 1,
            "none": 0,
            "left": 0,
            "bugs_left_out": 0,
        }
        stages = {stage["stage"]: stage for stage in after["run"]["stages"]}
        assert (stages["verify"]["settled"], stages["verify"]["in_doubt"]) == (2, 1)
        done = lines(client, "done")
        assert {line["subject"]["ref"] for line in done} == {"#41", "#42"}
        assert all(line["can_undo"] and line["candidates"][0]["title"] == first for line in done)
        assert "each chose this without seeing the other's answer" in done[0]["reason"]

        # Taken back, the link comes off, the line counts as judged wrong on the
        # record, and it is not applied again by itself.
        target = next(line for line in done if line["subject"]["ref"] == "#41")
        assert post(client, f"organize/stories-issues/lines/{target['id']}/undo").status_code == 200
        assert not placed(repo, 41) and placed(repo, 42)
        with repo.sessions() as db:
            judged = [
                line.verdict
                for line in db.scalars(select(ReorgLine).where(ReorgLine.state == "undone"))
            ]
        assert judged == ["wrong"]
        assert post(client, "organize/stories-issues/runs").status_code == 200
        assert not placed(repo, 41)
        waiting = {line["subject"]["ref"]: line for line in lines(client, "needs_you")}
        assert set(waiting) == {"#41", "#45"}
        # It says why: taken back before, or, where the undone line was one of the
        # sampled ones, that the record no longer allows applying at all.
        reason = waiting["#41"]["reason"]
        assert "not applied again by itself" in reason or "It is not applied by itself" in reason
    finally:
        client.close()


def test_an_issue_is_offered_and_put_under_a_story_of_a_connected_project(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        assert post(client, "review-settings", **SAVE).status_code == 200
        # A connected project has the User Story that issue 45 is about.
        epic = repo.planning.save("falcone-serverless", "epics", "Upkeep", "Keep it current.", [])
        story = repo.planning.save(
            "falcone-serverless",
            "stories",
            "Dependency refresh",
            "Refresh dependencies every week.",
            [UUID(epic["id"])],
        )["id"]
        assert "Dependency refresh" not in candidates(client)[45]
        connected = write(client, F, other="falcone-serverless", relation="depends_on")
        assert connected.status_code == 200, connected.text
        offered = candidates(client)
        assert offered[45][0] == "Dependency refresh"
        assert shown(client)["plan"]["scope"] == ["falcone-serverless"]

        first = offered[41][0]
        assert post(client, "organize/stories-issues/runs").status_code == 200
        readers = Readers(
            {
                ("analysis", 41): [first],
                ("other", 41): [first],
                ("analysis", 42): ["unclear"],
                ("analysis", 45): ["Dependency refresh"],
                ("other", 45): ["Dependency refresh"],
            }
        )
        asked(repo, readers)
        case = next(line for line in lines(client, "needs_you") if line["subject"]["ref"] == "#45")
        # The line names the project the Story belongs to.
        chosen = next(entry for entry in case["candidates"] if entry["chosen"])
        assert (chosen["title"], chosen["project"], chosen["id"]) == (
            "Dependency refresh",
            "falcone-serverless",
            story,
        )
        assert case["case"] == "settled"
        accepted = post(
            client, f"organize/stories-issues/lines/{case['id']}", choice="story", story_id=story
        )
        assert accepted.status_code == 200, accepted.text
        # The link is a link of the connection, and the Issue has its Story.
        with repo.sessions() as db:
            (finding,) = db.scalars(select(ConnectionFinding))
            issue = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == 45)).one()
            assert (finding.status, finding.origin, finding.source_id) == (
                "approved",
                "manual",
                issue.id,
            )
            assert str(finding.target_id) == story
        assert 45 not in candidates(client)
        done = next(line for line in lines(client, "done") if line["subject"]["ref"] == "#45")
        assert done["action"]["across"] is True
        assert post(client, f"organize/stories-issues/lines/{done['id']}/undo").status_code == 200
        with repo.sessions() as db:
            assert db.scalars(select(ConnectionFinding.status)).one() == "dismissed"
        assert 45 in candidates(client)
    finally:
        client.close()


def test_a_run_can_be_stopped_and_says_when_it_cannot_start(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        # No model is set to answer.
        nothing = shown(client)
        assert not nothing["can_start"] and "answer" in nothing["why_not"].lower()
        assert post(client, "organize/stories-issues/runs").status_code == 409
        assert post(client, "review-settings", **SAVE).status_code == 200
        assert post(client, "organize/stories-issues/control", action="stop").status_code == 409
        assert post(client, "organize/stories-issues/runs").status_code == 200
        # Stopped, the run is held; resumed, it asks on; ended, it keeps what it has.
        assert post(client, "organize/stories-issues/control", action="stop").status_code == 200
        held = shown(client)["run"]
        assert held["state"] == "held" and "stopped" in held["detail"].lower()
        assert post(client, "organize/stories-issues/control", action="resume").status_code == 200
        assert shown(client)["run"]["state"] == "running"
        assert post(client, "organize/stories-issues/control", action="end").status_code == 200
        ended = shown(client)
        assert ended["run"]["state"] == "stopped" and ended["can_start"]
        assert "3 have no answer yet" in ended["run"]["detail"]
        assert ended["lists"]["needs_you"] == 0
    finally:
        client.close()


def test_what_needs_no_model_is_gathered_when_none_can_be_asked(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        assert post(client, "review-settings", **SAVE).status_code == 200
        assert post(client, "organize/stories-issues/runs").status_code == 200
        unclear = {("analysis", number): ["unclear"] for number in (41, 42, 45)}
        asked(repo, Readers(unclear))
        assert shown(client)["lists"]["needs_you"] == 3
        # One Issue changes, so it has no answer to its text; and no model is set any more.
        with repo.sessions.begin() as db:
            issue = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == 41)).one()
            issue.description = "Write every invocation to the ledger, once."
            project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
            for settings in db.scalars(
                select(ReviewSettings).where(ReviewSettings.project_id == project.id)
            ):
                settings.answer_provider = settings.answer_model = None
            pid = project.id
        review_settings.forget(pid)
        now = shown(client)
        # It says that nobody is asked and why, and still gathers what waits.
        assert now["plan"]["to_ask"] == 1 and "No model" in now["plan"]["ask_blocked"]
        assert now["can_start"] and now["why_not"] is None
        assert post(client, "organize/stories-issues/runs").status_code == 200
        after = shown(client)
        assert after["run"]["state"] == "complete" and after["run"]["asking"] is None
        assert "1 have no answer yet" in after["run"]["detail"]
        assert after["lists"]["needs_you"] == 2
    finally:
        client.close()
