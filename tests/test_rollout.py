"""The reorganization across projects and as records arrive: where each project stands
and its next step, where an issue that arrived was put, and what moved since the day
before."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from engineering_control.dashboard import ranking
from engineering_control.dashboard.planning_drift import digest_lines, drift
from engineering_control.db.models import BacklogItem, PlanningHealth, ScorerDecision
from tests.test_dashboard_web import _authenticated_client
from tests.test_delivery import vector
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/"


def test_what_moved_since_the_day_before_is_said_in_a_line(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        pid = data["project"]
        # Never measured: nothing to say.
        assert digest_lines(repo.sessions) == []
        today = datetime.now(UTC).date()
        with repo.sessions.begin() as db:
            db.add(
                PlanningHealth(
                    project_id=pid,
                    day=today - timedelta(days=1),
                    measured_at=datetime.now(UTC) - timedelta(days=1),
                    numbers={"changes_ready": 0, "orphans_open": 2, "stories_unsupported": 4},
                )
            )
            db.add(
                PlanningHealth(
                    project_id=pid,
                    day=today,
                    measured_at=datetime.now(UTC),
                    numbers={"changes_ready": 3, "orphans_open": 5, "stories_unsupported": 4},
                )
            )
        with repo.sessions() as db:
            found = drift(db, pid)
        assert found is not None and found["before"] == (today - timedelta(days=1)).isoformat()
        assert {entry["key"]: (entry["was"], entry["now"]) for entry in found["moved"]} == {
            "changes_ready": (0, 3),
            "orphans_open": (2, 5),
        }
        assert found["waiting"] == ["3 changes wait for approval"]
        assert digest_lines(repo.sessions) == [
            "Planning:",
            "- **falcone**: 3 changes wait for approval; 5 open issues have no User Story (+3).",
        ]
    finally:
        client.close()


def test_every_project_has_a_row_and_a_next_step(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        pid = data["project"]
        found = client.get("/api/dashboard/rollout")
        assert found.status_code == 200, found.text
        rows = {row["project"]: row for row in found.json()["projects"]}
        # A project that was never through Discovery has nothing to reorganize.
        assert not rows["llmwiki"]["planned"]
        assert [step["view"] for step in rows["llmwiki"]["steps"]] == ["discovery"]
        falcone = rows["falcone"]
        assert falcone["planned"] and (falcone["epics"], falcone["stories"]) == (1, 2)
        assert "ranking service" in falcone["steps"][0]["says"]

        with repo.sessions.begin() as db:
            item = db.scalars(
                select(BacklogItem).where(
                    BacklogItem.project_id == pid, BacklogItem.issue_number == 42
                )
            ).one()
            ranking.keep_vector(db, pid, "issue", item.id, "m", "d", vector(1, 0, 0, 0))
        numbers = repo.planning_health.record("falcone")
        assert {"changes_ready", "structure_proposals", "issues_unplaced"} <= set(numbers)
        row = client.get(BASE + "rollout").json()
        said = [step["says"] for step in row["steps"]]
        assert any("Judge the pairs" in text for text in said)
        assert any("open issues have no User Story" in text for text in said)
        assert row["drift"]["numbers"]["orphans_open"] == 2
        assert client.get("/api/dashboard/projects/unknown/rollout").status_code == 404
    finally:
        client.close()


def test_an_issue_that_arrived_is_shown_where_the_cascade_put_it(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        pid = data["project"]
        with repo.sessions.begin() as db:
            items: dict[int, Any] = {
                item.issue_number: item
                for item in db.scalars(select(BacklogItem).where(BacklogItem.project_id == pid))
            }
            items[41].title = "bug: the ledger loses entries"
            ranking.keep_vector(db, pid, "issue", items[42].id, "m", "d", vector(1, 0, 0, 0))
            ranking.keep_vector(db, pid, "issue", items[43].id, "m", "d", vector(0, 1, 0, 0))
            ranking.keep_vector(db, pid, "issue", items[45].id, "m", "d", vector(0, 0, 1, 0))
            for number, outcome, share in ((42, "place", 0.9), (45, "several", 0.3)):
                db.add(
                    ScorerDecision(
                        project_id=pid,
                        kind="issue_story",
                        child_id=items[number].id,
                        parent_id=data["Respond to failures"],
                        outcome=outcome,
                        confidence=share,
                        model="light",
                        detail={},
                    )
                )
            # An issue that came in long ago is no arrival.
            items[43].created_at = datetime.now(UTC) - timedelta(days=40)
        found = client.get(BASE + "arrivals")
        assert found.status_code == 200, found.text
        arrivals = found.json()
        assert arrivals["total"] == 4
        assert arrivals["counts"] == {"bug": 1, "batch": 1, "linked": 1, "waiting": 1}
        by_number = {item["title"].split()[0]: item for item in arrivals["items"]}
        assert "#43" not in by_number
        assert by_number["#44"]["stands"] == "linked"
        assert by_number["#44"]["story"]["origin"] == "administrator"
        assert by_number["#42"]["stands"] == "batch" and by_number["#42"]["share"] == 0.9
        assert by_number["#45"]["story"]["title"] == "Respond to failures"
        # Before the scorer has spoken, an issue is said to wait for it.
        with repo.sessions.begin() as db:
            for row in db.scalars(select(ScorerDecision)).all():
                db.delete(row)
        counts = client.get(BASE + "arrivals", params={"days": 90}).json()["counts"]
        assert counts == {"bug": 1, "linked": 1, "unscored": 3}
    finally:
        client.close()
