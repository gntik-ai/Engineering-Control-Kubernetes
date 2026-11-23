"""How a project's Epics, User Stories and Issues hang together, counted as the planning
tables show it and kept once a day."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from engineering_control.dashboard.planning_health import FOLLOWED, measure
from engineering_control.db.models import (
    BacklogItem,
    PlanningHealth,
    ProjectEpic,
    ProjectIssueStory,
    ProjectStoryEpic,
    ProjectUserStory,
    ReviewEvidenceJob,
    ReviewFlowRun,
)
from tests.test_dashboard_web import _authenticated_client
from tests.test_project_coverage import seed

URL = "/api/dashboard/projects/falcone/planning/health"


def _orphans(client: Any, state: str) -> int:
    listed = client.get(
        "/api/dashboard/projects/falcone/planning/issues",
        params={"issue_state": state, "page_size": 100},
    ).json()
    return sum(1 for row in listed["items"] if row.get("orphan"))


def test_the_numbers_are_the_ones_the_planning_tables_show(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        now = measure(repo.sessions, "falcone")
        assert (now["epics"], now["stories"], now["issues"]) == (1, 3, 5)
        assert (now["issues_open"], now["issues_closed"]) == (3, 2)
        # An issue is an orphan here exactly when its row in the Issues table says so.
        assert now["orphans_open"] == _orphans(client, "open") == 2
        assert now["orphans_closed"] == _orphans(client, "closed") == 2
        assert (now["issue_story_links"], now["story_epic_links"]) == (1, 3)
        # One link was made by the administrator; nothing explains the Stories' Epics.
        assert (now["links_with_reason"], now["links_without_reason"]) == (1, 3)
        assert now["stories_without_issue"] == 2 and now["stories_under_several_epics"] == 0
        assert (now["largest_epic_title"], now["largest_epic_stories"]) == ("Operations", 3)
        # With three Stories in all, one Epic that has them all is no catch-all.
        assert now["catch_all_epics"] == 0

        with repo.sessions.begin() as db:
            pid = data["project"]
            items = {
                item.issue_number: item
                for item in db.scalars(select(BacklogItem).where(BacklogItem.project_id == pid))
            }
            story = data["Audit executions"]
            # An orphan gets its Story; another issue is said to be a bug by the administrator.
            db.add(ProjectIssueStory(project_id=pid, item_id=items[45].id, story_id=story))
            items[42].execution_type, items[42].type_confirmed = "bug", True
            # A Story is put under a second Epic.
            other = ProjectEpic(project_id=pid, title="Compliance", description="Keep records.")
            db.add(other)
            db.flush()
            db.add(ProjectStoryEpic(project_id=pid, story_id=story, epic_id=other.id))
            # A run that was cut off waits to be released.
            run = ReviewFlowRun(
                project_id=pid,
                action="evidence",
                flow_key="review-evidence",
                flow_name="Review related work",
                version=0,
                settings={},
            )
            db.add(run)
            db.flush()
            db.add(
                ReviewEvidenceJob(
                    run_id=run.id,
                    project_id=pid,
                    state="uncertain",
                    provider="fixture",
                    model="analysis",
                    token_cap=1,
                )
            )
        after = measure(repo.sessions, "falcone")
        assert after["orphans_open"] == _orphans(client, "open") == 0
        assert (after["bugs_without_story"], after["bugs_confirmed"]) == (1, 1)
        assert after["issue_story_links"] == 2 and after["stories_without_issue"] == 1
        assert (after["epics"], after["stories_under_several_epics"]) == (2, 1)
        assert (after["epics_per_story"], after["most_epics_of_a_story"]) == (1.3, 2)
        assert after["epics_without_story"] == 0
        assert after["runs_cut_off"] == 1
        assert after["reviews_waiting"] == now["reviews_waiting"] + 1
    finally:
        client.close()


def test_one_measurement_a_day_is_kept_and_the_first_is_what_later_ones_compare_with(
    tmp_path: Path,
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        health = repo.planning_health
        day = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
        first = health.report("falcone", now=day)
        assert first["baseline"] is None and [row["day"] for row in first["history"]] == [
            "2026-11-20"
        ]
        assert set(first["history"][0]) == {"day", *FOLLOWED}

        # A few minutes later the same measurement is used again, unless one is asked for.
        with repo.sessions.begin() as db:
            story = ProjectUserStory(
                project_id=data["project"], title="Export reports", description="Export them."
            )
            db.add(story)
        again = health.report("falcone", now=day + timedelta(minutes=5))
        assert again["now"]["stories"] == 3 and again["measured_at"] == first["measured_at"]
        fresh = health.report("falcone", now=day + timedelta(minutes=5), refresh=True)
        assert fresh["now"]["stories"] == 4 and fresh["measured_at"] != first["measured_at"]
        with repo.sessions() as db:
            assert len(db.scalars(select(PlanningHealth)).all()) == 1

        # Days later: the first day is what today is compared with, and both are listed.
        later = health.report("falcone", now=day + timedelta(days=3))
        assert later["baseline"]["day"] == "2026-11-20"
        assert later["baseline"]["numbers"]["stories"] == 4
        assert [row["day"] for row in later["history"]] == ["2026-11-20", "2026-11-23"]
        # Only the days asked for are listed; the first measurement stays the baseline.
        short = health.report("falcone", days=2, now=day + timedelta(days=3))
        assert [row["day"] for row in short["history"]] == ["2026-11-23"]
        assert short["baseline"]["day"] == "2026-11-20"
    finally:
        client.close()


def test_every_project_with_planning_is_measured_once_a_day(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        health = repo.planning_health
        day = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
        # A project nothing was planned for has nothing to measure.
        assert health.missing_today(now=day) == []
        seed(repo)
        assert health.missing_today(now=day) == ["falcone"]
        health.record("falcone", now=day)
        assert health.missing_today(now=day) == []
        assert health.missing_today(now=day + timedelta(days=1)) == ["falcone"]
    finally:
        client.close()


def test_the_console_reads_the_health_of_its_projects_only(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        seed(repo)
        shown = client.get(URL, params={"days": 7})
        assert shown.status_code == 200 and shown.json()["now"]["issues"] == 5
        assert client.get(URL.replace("falcone", "nowhere")).status_code == 404
        assert client.get(URL, params={"days": 0}).status_code == 422
        client.cookies.clear()
        assert client.get(URL).status_code == 401
    finally:
        client.close()
