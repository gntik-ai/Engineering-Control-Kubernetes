"""The console's start page: what was done and what was spent on each day, the runs
under way and the queues."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from engineering_control.dashboard.overview import daily
from engineering_control.db.models import (
    BacklogItem,
    ProjectRecord,
    SandboxRun,
    SandboxStep,
)
from engineering_control.sandbox.runs import CONVERSATION
from tests.test_dashboard_accounts import _console
from tests.test_queues import world  # noqa: F401 - a fixture

# A Tuesday at noon, so that a day before and after is plain to see.
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def _done(world: Any, item_id: UUID, at: datetime | None = None) -> None:  # noqa: F811
    with world.sessions.begin() as session:
        item = session.get(BacklogItem, item_id)
        assert item is not None
        item.state = "done"
        if at is not None:
            item.done_at = at


def _step(world: Any, cost: Any, at: datetime | None, *, state: str = "succeeded") -> UUID:  # noqa: F811
    with world.sessions.begin() as session:
        run = SandboxRun(
            kind="flow",
            state="running" if at is None else "succeeded",
            title="A run",
            project_id=world.project_id,
            cancel_requested=False,
            data={"project": "falcone-workflow"},
        )
        session.add(run)
        session.flush()
        session.add(
            SandboxStep(
                run_id=run.id,
                position=0,
                name="ask",
                state=state,
                usage=None if cost is None else {"cost": cost, "tokens": 10},
                finished_at=at,
            )
        )
        return run.id


def _by_day(found: dict[str, Any]) -> dict[str, tuple[int, float]]:
    return {
        entry["day"]: (entry["resolved"], entry["cost"])
        for entry in found["series"]
        if entry["resolved"] or entry["cost"]
    }


def test_the_day_an_item_was_done_is_kept_with_its_state(world: Any) -> None:  # noqa: F811
    first = world.items[0]
    with world.sessions() as session:
        assert session.get(BacklogItem, first).done_at is None
    before = datetime.now(UTC)
    # Whoever closes it: here the task that merged its pull request.
    world.backlog.note_facts(first, {"pull_request": {"number": 4}}, state="done")
    with world.sessions() as session:
        at = session.get(BacklogItem, first).done_at
    assert at is not None and at.replace(tzinfo=UTC) >= before - timedelta(seconds=1)
    # Saying again that it is done does not move the day.
    with world.sessions.begin() as session:
        session.get(BacklogItem, first).state = "done"
    with world.sessions() as session:
        assert session.get(BacklogItem, first).done_at == at
    # An item that is opened again was not done.
    with world.sessions.begin() as session:
        session.get(BacklogItem, first).state = "accepted"
    with world.sessions() as session:
        assert session.get(BacklogItem, first).done_at is None


def test_what_was_done_and_spent_is_counted_day_by_day(world: Any) -> None:  # noqa: F811
    empty = daily(world.sessions, now=NOW)
    assert empty["days"] == 30 and len(empty["series"]) == 30
    # Oldest first, today last, and no day left out.
    assert empty["series"][0]["day"] == "2026-10-19"
    assert empty["series"][-1] == {"day": "2026-11-17", "resolved": 0, "cost": 0.0}
    assert (empty["resolved"], empty["cost"]) == (0, 0.0)

    _done(world, world.items[0], NOW - timedelta(hours=2))
    _done(world, world.items[1], NOW - timedelta(hours=3))
    _done(world, world.items[2], NOW - timedelta(days=3))
    # Longer ago than is shown.
    _done(world, world.items[3], NOW - timedelta(days=40))
    _step(world, 0.25, NOW - timedelta(hours=1))
    _step(world, 0.5, NOW - timedelta(hours=5))
    _step(world, 1.125, NOW - timedelta(days=3))
    _step(world, 9.0, NOW - timedelta(days=40))
    # What has no price adds nothing: a step on a subscription, one without usage, and
    # one whose usage is not a number.
    _step(world, 0, NOW - timedelta(hours=1))
    _step(world, None, NOW - timedelta(hours=1))
    _step(world, "soon", NOW - timedelta(hours=1))
    found = daily(world.sessions, now=NOW)
    assert _by_day(found) == {"2026-11-14": (1, 1.125), "2026-11-17": (2, 0.75)}
    assert (found["resolved"], found["cost"]) == (3, 1.875)

    # A week only, and never more than a month.
    week = daily(world.sessions, days=7, now=NOW)
    assert [entry["day"] for entry in week["series"]][0] == "2026-11-11"
    assert len(week["series"]) == 7 and week["resolved"] == 3
    assert len(daily(world.sessions, days=400, now=NOW)["series"]) == 31
    assert len(daily(world.sessions, days=0, now=NOW)["series"]) == 1


def test_a_day_is_a_day_where_the_administrator_is(world: Any) -> None:  # noqa: F811
    # Half past eleven in the evening in UTC is the next day already, an hour ahead.
    late = datetime(2026, 11, 16, 23, 30, tzinfo=UTC)
    _done(world, world.items[0], late)
    _step(world, 0.5, late)
    assert _by_day(daily(world.sessions, now=NOW)) == {"2026-11-16": (1, 0.5)}
    assert _by_day(daily(world.sessions, minutes=60, now=NOW)) == {"2026-11-17": (1, 0.5)}
    # As far behind UTC as a clock can be, it is still the 16th: that is the last day.
    behind = daily(world.sessions, minutes=-840, now=NOW)
    assert behind["series"][-1] == {"day": "2026-11-16", "resolved": 1, "cost": 0.5}
    # A clock that cannot be is taken as the furthest one that can.
    assert daily(world.sessions, minutes=99_999, now=NOW)["series"][-1]["day"] == "2026-11-18"


def test_a_step_at_work_counts_today_and_an_archived_project_counts_nothing(
    world: Any,  # noqa: F811
) -> None:
    # A step that has not ended is dated by its last change: now.
    _step(world, 0.75, None, state="running")
    found = daily(world.sessions)
    assert found["series"][-1]["cost"] == 0.75 and found["cost"] == 0.75
    _done(world, world.elsewhere)
    assert daily(world.sessions)["resolved"] == 1
    with world.sessions.begin() as session:
        other = session.query(ProjectRecord).filter_by(name="falcone").one()
        other.archived_at = datetime.now(UTC)
    assert daily(world.sessions)["resolved"] == 0


def test_the_runs_that_have_not_ended_and_the_queues_that_have_not_finished(
    world: Any,  # noqa: F811
) -> None:
    assert world.runs.under_way() == {"items": [], "total": 0}
    assert world.queues.not_finished() == 0
    ended = _step(world, 0.5, NOW)
    working = _step(world, None, None, state="running")
    with world.sessions.begin() as session:
        for state in ("queued", "waiting", "failed", "cancelled"):
            session.add(
                SandboxRun(kind="flow", state=state, title=state, cancel_requested=False, data={})
            )
        # The supervisor's turns are a conversation, not work.
        session.add(
            SandboxRun(kind=CONVERSATION, state="running", title="A turn", cancel_requested=False)
        )
    found = world.runs.under_way()
    assert found["total"] == 3
    assert {run["state"] for run in found["items"]} == {"queued", "running", "waiting"}
    assert str(working) in {run["id"] for run in found["items"]}
    assert str(ended) not in {run["id"] for run in found["items"]}
    # More than are listed are still counted.
    assert world.runs.under_way(limit=1)["total"] == 3
    assert len(world.runs.under_way(limit=1)["items"]) == 1

    world.queue(0, 1)
    world.queue(2, name="Later")
    assert world.queues.not_finished() == 2


def test_the_start_page_reads_it_all_at_once(tmp_path: Path) -> None:
    client: TestClient = _console(tmp_path, None)
    try:
        found = client.get("/api/dashboard/start").json()
        assert found["days"] == 30 and len(found["series"]) == 30
        assert set(found["series"][0]) == {"day", "resolved", "cost"}
        assert (found["resolved"], found["cost"]) == (0, 0.0)
        assert found["runs"] == {"items": [], "total": 0}
        assert found["queues"] == {"items": [], "total": 0, "not_finished": 0}

        repository = client.app.state.dashboard_repository  # type: ignore[attr-defined]
        with repository.sessions.begin() as session:
            project = session.query(ProjectRecord).filter_by(name="falcone").one()
            item = BacklogItem(
                project_id=project.id,
                title="Wrong total",
                source="manual",
                state="accepted",
                issue_labels=[],
            )
            session.add(item)
            session.add(
                SandboxRun(
                    kind="flow",
                    state="running",
                    title="Wrong total",
                    project_id=project.id,
                    cancel_requested=False,
                    data={"project": "falcone", "project_name": "Falcone"},
                )
            )
            session.flush()
            item_id = item.id
        repository.backlog.note_facts(item_id, {}, state="done")
        found = client.get("/api/dashboard/start", params={"days": 7, "minutes": 60}).json()
        assert len(found["series"]) == 7 and found["resolved"] == 1
        assert found["series"][-1]["resolved"] == 1
        [run] = found["runs"]["items"]
        assert (run["title"], run["state"], run["project"]) == ("Wrong total", "running", "falcone")
        # What is not a number is refused, and nobody reads it without a session.
        assert client.get("/api/dashboard/start", params={"days": "many"}).status_code == 422
        client.cookies.clear()
        assert client.get("/api/dashboard/start").status_code == 401
    finally:
        client.__exit__(None, None, None)
