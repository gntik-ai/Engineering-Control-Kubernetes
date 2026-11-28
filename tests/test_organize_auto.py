"""By itself, and told (O6): a run of Organize or Coverage starts by itself where the
settings say so, as records arrive; what it did is told; and the record of a stage
starts anew when its models change, and can be put back."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from engineering_control.dashboard import organize_auto, organize_create, reorg
from engineering_control.dashboard.organize_create import Creator
from engineering_control.dashboard.workspace_issues import import_issues
from engineering_control.db.models import (
    OrganizeLine,
    OrganizeRun,
    PlanningReview,
    ProjectRecord,
    ProjectUserStory,
)
from tests.test_automatic_planning import post
from tests.test_connection_plan import connect
from tests.test_connection_plan import seed as connected
from tests.test_dashboard_web import _authenticated_client
from tests.test_needs_you import SAVE, candidates
from tests.test_organize_create import Hand, carried
from tests.test_review_settings import BASE, ready

ISSUES = BASE + "organize/stories-issues"


@pytest.fixture(autouse=True)
def linking_only(monkeypatch: Any) -> None:
    """These tests are about when a run starts: the stage that creates is left out."""
    monkeypatch.setattr(organize_create, "STEPS", {})


def numbers(client: Any, listed: str) -> set[str]:
    found = client.get(f"{ISSUES}/lines/{listed}")
    assert found.status_code == 200, found.text
    return {str(line["subject"]["ref"]) for line in found.json()["items"]}


def test_a_new_issue_is_organized_without_a_button(tmp_path: Path, monkeypatch: Any) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        now = datetime.now(UTC)
        # Nothing starts by itself unless the settings say so.
        assert post(client, "review-settings", **SAVE).status_code == 200
        assert repo.organize_auto.start_due(now) == []
        standing = client.get(BASE + "reviews/by-itself").json()["runs"]
        assert {entry["check"]: entry["mode"] for entry in standing} == dict.fromkeys(
            organize_auto.RUNS, "off"
        )
        # A check that was set to run by itself goes on as it was set, and lets no run
        # start: a run does more than the check, and starts only on its own setting.
        assert post(client, "review-settings", **SAVE, runs={"issue_story": "arrive"}).status_code
        assert repo.organize_auto.asked_for() == []
        assert repo.auto_checks.asked_for() == [("falcone", "issue_story")]
        listed = {e["check"]: e for e in client.get(BASE + "review-settings").json()["runs"]}
        assert listed["issue_story"]["by_run"] == "Organize: User Stories and Issues"
        # Once the run starts by itself, it asks the check: the check is not started
        # on its own as well.
        both = {"issue_story": "arrive", "stories_issues": "arrive"}
        assert post(client, "review-settings", **SAVE, runs=both).status_code == 200
        assert repo.auto_checks.asked_for() == []
        assert repo.organize_auto.asked_for() == [("falcone", "stories_issues")]
        assert (
            post(client, "review-settings", **SAVE, runs={"stories_issues": "arrive"}).status_code
            == 200
        )
        listed = {e["check"]: e for e in client.get(BASE + "review-settings").json()["runs"]}
        assert listed["stories_issues"]["mode"] == "arrive"
        assert "issue_story" not in listed and "story_needed" in listed

        first = candidates(client)[41][0]
        assert repo.organize_auto.start_due(now) == [("falcone", "stories_issues")]
        with repo.sessions() as db:
            run = db.scalars(select(OrganizeRun).where(OrganizeRun.kind == "stories_issues")).one()
            assert run.auto and run.state == "running"
        # While it is under way, and for half an hour after it started, none is started.
        assert repo.organize_auto.start_due(now) == []
        carried(repo, Hand({41: first, 42: first, 45: "none"}, {}), repo.organize_issues)
        shown = client.get(ISSUES).json()
        assert shown["run"]["state"] == "complete"
        # Every open Issue that had no User Story is under one, or in "Needs you".
        seen = numbers(client, "needs_you") | numbers(client, "done")
        assert {"#41", "#42", "#45"} <= seen
        # As a run that started by itself ends, what it left to do is noted.
        assert repo.organize_auto.settle() == 1 and repo.organize_auto.settle() == 0
        assert repo.organize_auto.start_due(now + timedelta(minutes=10)) == []
        said = repo.organize_auto.standing("falcone", "stories_issues", now + timedelta(minutes=10))
        assert not said["due"] and "half an hour" in said["why"]

        # A new Issue arrives: within the hour a run starts by itself and organizes it.
        later = now + timedelta(hours=1)
        with repo.sessions.begin() as db:
            project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
            import_issues(
                db,
                project,
                [
                    {
                        "number": 46,
                        "state": "open",
                        "title": "Keep the audit entry of a failed run",
                        "body": "Show failed runs to the operator and keep an audit entry.",
                        "labels": ["task"],
                    }
                ],
            )
        assert repo.organize_auto.start_due(later) == [("falcone", "stories_issues")]
        carried(repo, Hand({41: first, 42: first, 45: "none", 46: first}, {}), repo.organize_issues)
        assert "#46" in numbers(client, "needs_you") | numbers(client, "done")
        assert repo.organize_auto.settle() == 1

        # What the runs did by themselves is told: in the digest, on the Dashboard, and
        # the cases that wait are in the chat's inbox, not counted as pressing.
        since = now - timedelta(minutes=5)
        digest = organize_auto.digest_lines(repo.sessions, since)
        assert (
            digest[0] == "Organize and Coverage:"
            and "**falcone**: 2 runs by themselves" in (digest[1])
        )
        waits = client.get(ISSUES).json()["lists"]["needs_you"]
        assert waits and f"{waits:,} under Organize" in digest[1]
        lately = client.get(BASE + "needs-you").json()["lately"]["organized"]
        assert lately["runs"] == 2 and lately["needs"] + lately["done"] >= 4
        cases = [e for e in repo.toolbox.inbox_entries() if e["kind"] == "review_cases"]
        assert [(e["project"], e["page"], e["count"]) for e in cases] == [
            ("falcone", "organize", waits)
        ]

        # A run that left as much to do as it found is not started again within a day.
        again = repo.organize_auto.standing("falcone", "stories_issues", later + timedelta(hours=2))
        assert not again["due"] and again["why"]
        # Each night means between 1 and 5 UTC.
        assert (
            post(client, "review-settings", **SAVE, runs={"stories_issues": "night"}).status_code
            == 200
        )
        noon = (later + timedelta(days=3)).replace(hour=12)
        assert (
            "in the night" in repo.organize_auto.standing("falcone", "stories_issues", noon)["why"]
        )
    finally:
        client.close()


def test_new_records_are_not_reviewed_one_by_one_where_a_run_organizes_them(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        with repo.sessions.begin() as db:
            story = db.scalars(select(ProjectUserStory)).first()
            db.add(
                PlanningReview(
                    project_id=story.project_id,
                    kind="stories",
                    record_id=story.id,
                    title=story.title,
                    scope="verify",
                )
            )
        assert (
            post(client, "review-settings", **SAVE, runs={"epics_stories": "arrive"}).status_code
            == 200
        )
        with repo.sessions() as db:
            assert organize_auto.organized(db, story.project_id)
        # The review of the single record is not asked while the run organizes.
        assert repo.planning_analysis.claim() is None
        assert client.get(BASE + "planning-reviews").json()["by_runs"] is True
        rows = {row["key"] for row in client.get(BASE + "needs-you").json()["rows"]}
        assert "planning:queued" not in rows
        assert post(client, "review-settings", **SAVE, runs={}).status_code == 200
        assert repo.planning_analysis.claim() is not None
    finally:
        client.close()


def test_the_record_of_a_stage_starts_anew_when_its_models_change(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        assert post(client, "review-settings", **SAVE).status_code == 200
        with repo.sessions.begin() as db:
            pid = db.scalars(select(ProjectRecord.id).where(ProjectRecord.name == "falcone")).one()
            run = OrganizeRun(
                project_id=pid, kind="stories_issues", state="complete", stage="done", counts={}
            )
            db.add(run)
            db.flush()
            # A record a run made was taken away again: more than the settings accept.
            db.add(
                OrganizeLine(
                    run_id=run.id,
                    project_id=pid,
                    listed="created",
                    state="undone",
                    case="issue_made",
                    subject_kind="issues",
                    title="Made and taken away",
                    candidates=[],
                    said=[],
                    reason="",
                    doubt=0.0,
                    action={"op": "created"},
                )
            )
        stages = {e["stage"]: e for e in client.get(BASE + "review-settings").json()["stages"]}
        assert stages["decision"]["since"] is None and not stages["decision"]["changed"]
        assert (stages["writing"]["counted"], stages["writing"]["wrong"]) == (1, 1)
        assert stages["writing"]["worse"]
        with repo.sessions() as db:
            assert not Creator.creating(db, pid)["in_force"]
            assert "since the models" not in reorg.alone_standing(db, pid, "place")["why"]

        # The models are changed: the judge answers and the answerer judges.
        swapped = {
            **SAVE,
            "answer_model": "other",
            "judge_model": "analysis",
        }
        assert post(client, "review-settings", **swapped).status_code == 200
        stages = {e["stage"]: e for e in client.get(BASE + "review-settings").json()["stages"]}
        assert stages["decision"]["since"] and stages["decision"]["changed"]
        before = client.get(BASE + "review-settings").json()["models_before"]
        assert before["answer"] == ["fixture", "analysis"] and before["judge"] == [
            "fixture",
            "other",
        ]
        # What was made and judged with the models before says nothing of these.
        assert (stages["writing"]["counted"], stages["writing"]["worse"]) == (0, False)
        with repo.sessions() as db:
            assert Creator.creating(db, pid)["in_force"]
            assert (
                "since the models that decide were changed"
                in (reorg.alone_standing(db, pid, "place")["why"])
            )

        # Put back, the models are those before and the record they had counts again.
        back = post(client, "review-settings/put-back")
        assert back.status_code == 200, back.text
        shown = back.json()
        assert shown["roles"]["answer"]["model"] == "analysis"
        assert shown["roles"]["judge"]["model"] == "other"
        assert shown["models_before"] is None
        stages = {e["stage"]: e for e in shown["stages"]}
        assert stages["decision"]["since"] is None and not stages["decision"]["changed"]
        assert (stages["writing"]["counted"], stages["writing"]["worse"]) == (1, True)
        assert post(client, "review-settings/put-back").status_code == 409
        with repo.sessions() as db:
            assert not Creator.creating(db, pid)["in_force"]
    finally:
        client.close()


def test_a_connection_s_plan_is_carried_out_by_itself_where_the_settings_say_so(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        connected(repo, monkeypatch)
        assert post(client, "review-settings", **SAVE).status_code == 200
        key = connect(client, repo)
        path = f"{BASE}connections/{key}"
        assert client.get(path + "/plan").json()["can_carry_out"]
        # It waits for the word, unless the settings say it is carried out by itself.
        assert repo.organize_auto.start_due() == []
        assert (
            post(client, "review-settings", **SAVE, runs={"connection": "arrive"}).status_code
            == 200
        )
        assert repo.organize_auto.start_due() == [("falcone", "connection")]
        found = client.get(path + "/plan").json()
        assert found["run"]["state"] == "running"
        with repo.sessions() as db:
            assert (
                db.scalars(select(OrganizeRun).where(OrganizeRun.kind == "connection")).one().auto
            )
        assert repo.organize_auto.start_due() == []
    finally:
        client.close()
