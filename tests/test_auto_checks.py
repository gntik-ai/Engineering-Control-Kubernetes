"""A check runs by itself where the project's settings ask for it: as records arrive
that have no answer, or once a night, within what the settings allow."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from engineering_control.dashboard import auto_checks
from engineering_control.db.models import BacklogItem, ChoiceReview, ChoiceRun, ProjectRecord
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_judging import Readers
from tests.test_needs_you import SAVE, candidates
from tests.test_orphans import run
from tests.test_review_settings import BASE, ready

# Moments of the test's own clock. A run is created at the real time: the test says when
# each run that started by itself began, by this clock.
NOON = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
NIGHT = datetime(2026, 11, 3, 2, 0, tzinfo=UTC)


def began(repo: Any, when: datetime) -> None:
    """The latest run that started by itself began at this moment."""
    with repo.sessions.begin() as db:
        latest = db.scalars(
            select(ChoiceRun).where(ChoiceRun.auto).order_by(ChoiceRun.created_at.desc())
        ).first()
        assert latest is not None
        latest.created_at = when


def standing(repo: Any, kind: str, now: datetime) -> dict[str, Any]:
    with repo.sessions() as db:
        project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
        return auto_checks.standing(db, project, kind, now)


def test_a_check_starts_by_itself_as_records_arrive(tmp_path: Path, monkeypatch: Any) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        # Nothing runs by itself unless the settings ask for it.
        assert post(client, "review-settings", **SAVE).status_code == 200
        assert repo.auto_checks.start_due(NOON) == []
        shown = client.get(BASE + "review-settings").json()["runs"]
        # The runs, and the checks no run asks. A check a run asks is listed only while
        # it is set to start on its own.
        assert {entry["check"]: entry["mode"] for entry in shown} == {
            "epics_stories": "off",
            "stories_issues": "off",
            "coverage": "off",
            "connection": "off",
            "story_needed": "off",
            "epic_overview": "off",
        }
        unknown = post(client, "review-settings", **SAVE, runs={"no_such_check": "arrive"})
        assert unknown.status_code == 422

        # Asked for, and no model chosen to answer: it says so and starts nothing.
        bare = {"dollar_limit": 5, "makers": ["fixture"], "rounds": 0}
        asked = {"runs": {"issue_story": "arrive"}}
        assert post(client, "review-settings", **bare, **asked).status_code == 200
        assert "No model is chosen" in standing(repo, "issue_story", NOON)["why"]
        assert repo.auto_checks.start_due(NOON) == []

        assert post(client, "review-settings", **SAVE, **asked).status_code == 200
        waits = standing(repo, "issue_story", NOON)
        assert (waits["due"], waits["waiting"]) == (True, 3)
        first = candidates(client)[41][0]
        assert repo.auto_checks.start_due(NOON) == [("falcone", "issue_story")]
        began(repo, NOON)
        started = client.get(BASE + "orphans").json()["run"]
        # It takes the models of the settings, like a run started from the worklist.
        assert (started["auto"], started["model"], started["total"]) == (True, "analysis", 3)
        assert started["judge"]["model"] == "other"
        # While it is under way nothing else starts.
        assert repo.auto_checks.start_due(NOON) == []
        assert "under way" in standing(repo, "issue_story", NOON)["why"]
        run(
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
        # The daily digest says what ran by itself, and what it cost.
        said = auto_checks.digest_lines(repo.sessions, NOON - timedelta(hours=1))
        assert said == [
            "Checks that ran by themselves:",
            "- **falcone**: 1 run, 3 answers, $0.01.",
        ]
        assert auto_checks.digest_lines(repo.sessions, NOON + timedelta(days=30)) == []
        later = NOON + timedelta(minutes=45)
        assert standing(repo, "issue_story", later)["why"] == "Every record has an answer."
        assert repo.auto_checks.start_due(later) == []

        # An issue arrives: the next pass asks about it alone, and not within the half
        # hour after a run that started by itself.
        with repo.sessions.begin() as db:
            issue = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == 43)).one()
            issue.issue_state = "open"
            issue.description = "Show failed runs to the operator and keep an audit entry."
        began(repo, later - timedelta(minutes=10))
        assert "less than half an hour ago" in standing(repo, "issue_story", later)["why"]
        began(repo, NOON)
        assert repo.auto_checks.start_due(later) == [("falcone", "issue_story")]
        began(repo, later)
        assert client.get(BASE + "orphans").json()["run"]["total"] == 1
        run(repo, Readers({("analysis", 43): ["unclear"]}))

        # A record a model gave no usable answer about is not paid for again at once.
        with repo.sessions.begin() as db:
            review = db.scalars(select(ChoiceReview).where(ChoiceReview.child_id == issue.id)).one()
            review.state, review.updated_at = "failed", later + timedelta(hours=1)
        soon = later + timedelta(hours=2)
        assert standing(repo, "issue_story", soon)["due"] is False
        assert standing(repo, "issue_story", soon + timedelta(hours=30))["due"] is True

        # At the project's limit in dollars nothing starts.
        capped = {**SAVE, "dollar_limit": 0.001}
        assert post(client, "review-settings", **capped, **asked).status_code == 200
        held = standing(repo, "issue_story", soon + timedelta(hours=30))
        assert held["due"] is False and "a month" in held["why"]
        assert repo.auto_checks.start_due(soon + timedelta(hours=30)) == []
    finally:
        client.close()


def test_a_nightly_check_starts_once_a_night(tmp_path: Path, monkeypatch: Any) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ready(repo, monkeypatch)
        asked = {"runs": {"issue_story": "night"}}
        assert post(client, "review-settings", **SAVE, **asked).status_code == 200
        assert "in the night" in standing(repo, "issue_story", NOON)["why"]
        assert repo.auto_checks.start_due(NOON) == []
        assert repo.auto_checks.start_due(NIGHT) == [("falcone", "issue_story")]
        run(
            repo,
            Readers(
                {
                    ("analysis", 41): ["unclear"],
                    ("analysis", 42): ["unclear"],
                    ("analysis", 45): ["unclear"],
                }
            ),
        )
        began(repo, NIGHT)
        with repo.sessions.begin() as db:
            issue = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == 43)).one()
            issue.issue_state = "open"
        # Later the same night it does not start again; the next night it does.
        again = NIGHT + timedelta(hours=1)
        assert standing(repo, "issue_story", again)["why"] == "It ran this night already."
        assert standing(repo, "issue_story", NIGHT + timedelta(days=1))["due"] is True
        # Back to off: nothing is asked for.
        off = {"runs": {"issue_story": "off"}}
        assert post(client, "review-settings", **SAVE, **off).status_code == 200
        assert repo.auto_checks.asked_for() == []
    finally:
        client.close()
