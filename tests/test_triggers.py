"""Triggers: a section's flow started at set times, by a moved branch, or by an item
arriving, under the same conditions as a start by hand."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    ProjectRecords,
)
from engineering_control.db.models import BacklogItem, FlowTrigger, ProjectRecord, ProjectSection
from engineering_control.sandbox.queues import Budgets
from engineering_control.sandbox.triggers import Triggers, clean_spec, describe, next_due
from tests.test_sandbox_delivery import REPOSITORY, world  # noqa: F401 - ``world`` is a fixture

PROJECT = "falcone-workflow"


def _triggers(world: Any) -> Triggers:  # noqa: F811
    return Triggers(world.sessions, world.runs, Budgets(world.sessions))


def _flows(world: Any, **flows: str) -> None:  # noqa: F811
    with world.sessions.begin() as session:
        project = session.query(ProjectRecord).filter_by(name=PROJECT).one()
        for section in session.query(ProjectSection).filter_by(project_id=project.id):
            if section.key.replace("-", "_") in flows:
                section.flow_key = flows[section.key.replace("-", "_")]


def _set(world: Any, trigger_id: str, **fields: Any) -> None:  # noqa: F811
    with world.sessions.begin() as session:
        trigger = session.get(FlowTrigger, UUID(trigger_id))
        for name, value in fields.items():
            setattr(trigger, name, value)


def _item(world: Any, title: str, *, section: str = "issues", issue: int | None = None) -> UUID:  # noqa: F811
    with world.sessions.begin() as session:
        project = session.query(ProjectRecord).filter_by(name=PROJECT).one()
        found = session.query(ProjectSection).filter_by(project_id=project.id, key=section).one()
        item = BacklogItem(
            project_id=project.id,
            section_id=found.id,
            title=title,
            source="github" if issue else "manual",
            state="proposed",
            routed_by="label" if issue else "administrator",
            repository=REPOSITORY,
            issue_number=issue,
            issue_url=f"https://github.com/{REPOSITORY}/issues/{issue}" if issue else None,
            description=None if issue else "Do it.",
        )
        session.add(item)
        session.flush()
        return item.id


def _ended(world: Any, run_id: str) -> None:  # noqa: F811
    world.runs.run_finished(UUID(run_id), state="cancelled", outcome="cancelled", detail=None)


def test_what_a_trigger_says_is_bounded_and_read_back_in_words() -> None:
    assert clean_spec(
        "schedule", {"every": "week", "weekday": 9, "hour": "4", "scope": " a  b "}
    ) == {
        "every": "week",
        "weekday": 6,
        "hour": 4,
        "scope": "a b",
    }
    assert clean_spec("schedule", None) == {"every": "day", "hour": 3, "scope": ""}
    assert clean_spec("push", {"hours": 0}) == {"hours": 1, "scope": ""}
    assert clean_spec("item", {"source": "elsewhere", "per_day": 500}) == {
        "source": "any",
        "per_day": 50,
    }
    with pytest.raises(ProjectInvalid):
        clean_spec("webhook", {})
    assert describe("schedule", {"every": "week", "weekday": 2, "hour": 4}) == (
        "Every Wednesday at 04:00 UTC"
    )
    assert describe("schedule", {"every": "day", "hour": 23}) == "Every day at 23:00 UTC"
    assert describe("push", {"hours": 24}) == (
        "When the default branch has moved, at most once in a day"
    )
    assert describe("item", {"source": "github", "per_day": 3}) == (
        "When an issue from GitHub arrives in the section, at most 3 in a day"
    )
    friday = datetime(2026, 11, 13, 14, 30, tzinfo=UTC)
    assert next_due({"every": "day", "hour": 15}, friday) == friday.replace(hour=15, minute=0)
    assert next_due({"every": "day", "hour": 14}, friday) == datetime(2026, 11, 14, 14, tzinfo=UTC)
    assert next_due({"every": "week", "weekday": 0, "hour": 3}, friday) == datetime(
        2026, 11, 16, 3, tzinfo=UTC
    )
    assert next_due({"every": "week", "weekday": 4, "hour": 3}, friday) == datetime(
        2026, 11, 20, 3, tzinfo=UTC
    ), "this Friday's hour has passed"


def test_a_trigger_fits_what_its_sections_flow_is(world: Any) -> None:  # noqa: F811
    triggers = _triggers(world)
    _flows(world, code_audit="audit-code")
    with pytest.raises(ProjectInvalid, match="works on items"):
        triggers.create(PROJECT, "issues", kind="schedule", spec={})
    with pytest.raises(ProjectInvalid, match="has no flow that runs here"):
        triggers.create(PROJECT, "enhancement", kind="item", spec={})
    with pytest.raises(ProjectNotFound):
        triggers.create(PROJECT, "nowhere", kind="item", spec={})
    with pytest.raises(ProjectInvalid):
        triggers.create(PROJECT, "code-audit", kind="hook", spec={})
    made = triggers.create(
        PROJECT, "code-audit", kind="schedule", spec={"every": "week", "weekday": 0, "hour": 2}
    )
    assert made["says"] == "Every Monday at 02:00 UTC" and made["enabled"] is True
    assert made["section_name"] == "Code audit" and made["next_due_at"] is not None
    triggers.create(PROJECT, "issues", kind="item", spec={"source": "github"})
    listed = triggers.of_project(PROJECT)["items"]
    assert [(row["section"], row["kind"]) for row in listed] == [
        ("code-audit", "schedule"),
        ("issues", "item"),
    ]
    for _ in range(5):
        triggers.create(PROJECT, "code-audit", kind="push", spec={})
    with pytest.raises(ProjectConflict, match="at most 6"):
        triggers.create(PROJECT, "code-audit", kind="push", spec={})
    off = triggers.update(UUID(made["id"]), enabled=False, spec={"every": "day", "hour": 5})
    assert (off["enabled"], off["says"]) == (False, "Every day at 05:00 UTC")
    assert triggers.remove(UUID(made["id"]))["id"] == made["id"]
    with pytest.raises(ProjectNotFound):
        triggers.remove(UUID(made["id"]))


def test_a_schedule_starts_its_sections_flow_when_it_is_due(world: Any) -> None:  # noqa: F811
    triggers = _triggers(world)
    _flows(world, code_audit="audit-code")
    made = triggers.create(
        PROJECT, "code-audit", kind="schedule", spec={"every": "day", "hour": 3, "scope": "The API"}
    )
    assert triggers.advance() == 0, "not due yet"
    assert world.runs.listing()["items"] == []
    past = datetime.now(UTC) - timedelta(minutes=5)
    _set(world, made["id"], next_due_at=past)
    assert triggers.advance() == 1
    (run,) = world.runs.listing()["items"]
    assert (run["kind"], run["title"]) == ("audit_code", "Audit: The API")
    (row,) = triggers.of_project(PROJECT)["items"]
    assert row["last_result"] == "Started “Audit: The API”." and row["fired"] == 1
    assert datetime.fromisoformat(row["next_due_at"]) > datetime.now(UTC)
    assert triggers.advance() == 0, "once"

    # Due again while the first is still under way: it says so and waits for its next time.
    _set(world, made["id"], next_due_at=past)
    assert triggers.advance() == 0
    (row,) = triggers.of_project(PROJECT)["items"]
    assert row["last_result"] == "Not started: Audit code is already under way for this project"
    assert row["fired"] == 1 and datetime.fromisoformat(row["next_due_at"]) > datetime.now(UTC)

    # A paused project starts nothing, and a trigger that is switched off is not looked at.
    _ended(world, run["id"])
    ProjectRecords(world.sessions).pause(PROJECT, reason="Release week", by="administrator")
    _set(world, made["id"], next_due_at=past)
    assert triggers.advance() == 0
    assert (
        "was paused by you: Release week" in triggers.of_project(PROJECT)["items"][0]["last_result"]
    )
    ProjectRecords(world.sessions).resume(PROJECT)
    triggers.update(UUID(made["id"]), enabled=False)
    _set(world, made["id"], next_due_at=past)
    assert triggers.advance() == 0
    # Switched on again, the times it missed are not made up for.
    on = triggers.update(UUID(made["id"]), enabled=True)
    assert datetime.fromisoformat(on["next_due_at"]) > datetime.now(UTC)
    # By hand, now.
    fired = triggers.fire(UUID(made["id"]))
    assert fired["fired"] == 2 and fired["last_result"].startswith("Started")
    with pytest.raises(ProjectConflict, match="already under way"):
        triggers.fire(UUID(made["id"]))


@pytest.mark.asyncio
async def test_a_moved_branch_starts_one_run_and_not_more_often_than_its_pause(world: Any) -> None:  # noqa: F811
    triggers = _triggers(world)
    _flows(world, code_audit="audit-code")
    made = triggers.create(PROJECT, "code-audit", kind="push", spec={"hours": 6})
    heads = ["a" * 40]
    asked: list[tuple[str, str]] = []

    async def head(_github: Any, repository: str, branch: str) -> str | None:
        asked.append((repository, branch))
        return heads[-1]

    triggers._head = head  # type: ignore[method-assign]
    # The first look only notes where the branch is.
    assert await triggers.look(world.github) == 0
    assert asked == [(REPOSITORY, "main")]
    assert triggers.of_project(PROJECT)["items"][0]["last_result"] == "Watching the branch."
    assert await triggers.look(world.github) == 0, "it has not moved"
    heads.append("b" * 40)
    assert await triggers.look(world.github) == 1
    (run,) = world.runs.listing()["items"]
    assert run["title"] == "Audit: the whole repository"
    assert await triggers.look(world.github) == 0, "that move was acted on"
    # It moves again within the pause: nothing starts until the pause is over, then once.
    _ended(world, run["id"])
    heads.append("c" * 40)
    assert await triggers.look(world.github) == 0
    _set(world, made["id"], last_fired_at=datetime.now(UTC) - timedelta(hours=7))
    assert await triggers.look(world.github) == 1
    assert triggers.of_project(PROJECT)["items"][0]["fired"] == 2
    # GitHub not answering is not a move.
    heads.append(None)  # type: ignore[arg-type]
    assert await triggers.look(world.github) == 0
    await world.github.aclose()


def test_items_that_arrive_in_a_section_are_started_within_the_triggers_day(world: Any) -> None:  # noqa: F811
    triggers = _triggers(world)
    made = triggers.create(PROJECT, "issues", kind="item", spec={"source": "github", "per_day": 2})
    # What was in the section before the trigger was made is left alone.
    assert triggers.advance() == 0 and world.runs.listing()["items"] == []
    assert triggers.of_project(PROJECT)["items"][0]["last_result"] is None
    _item(world, "Written here, not an issue")
    first = _item(world, "Export fails for long names", issue=21)
    assert triggers.advance() == 1
    (run,) = world.runs.listing()["items"]
    assert run["item_id"] == str(first) and run["kind"] == "deliver_issue"
    row = triggers.of_project(PROJECT)["items"][0]
    assert row["last_result"] == "Started 1: Export fails for long names."
    assert triggers.advance() == 0, "an item that has a run is not started again"
    # Three more arrive: one fits in the day, two wait for tomorrow.
    for number in (22, 23, 24):
        _item(world, f"Issue {number}", issue=number)
    assert triggers.advance() == 1
    assert triggers.of_project(PROJECT)["items"][0]["last_result"] == (
        "Started 1: Issue 22. 2 more wait."
    )
    assert triggers.advance() == 0
    assert triggers.of_project(PROJECT)["items"][0]["last_result"] == (
        "Not started: 2 wait; the trigger's day is used up."
    )
    with world.sessions.begin() as session:
        trigger = session.get(FlowTrigger, UUID(made["id"]))
        trigger.state = {**trigger.state, "day": "2026-01-01"}
    assert triggers.advance() == 1, "a new day"
    assert len(world.runs.listing()["items"]) == 4, "both that waited"


def test_triggers_are_kept_on_the_projects_page(world: Any) -> None:  # noqa: F811
    from fastapi.testclient import TestClient

    from engineering_control.config import Settings
    from engineering_control.dashboard.app import create_app
    from engineering_control.dashboard.repository import DashboardRepository

    password = "correct horse battery staple"  # noqa: S105 - deterministic test fixture
    repository = DashboardRepository(str(world.sessions.kw["bind"].url))
    repository.initialize_password_sync(password)
    _flows(world, code_audit="audit-code")
    client = TestClient(
        create_app(
            settings=Settings(database_url=str(repository.engine.url)),
            repository=repository,
            session_key=b"g" * 48,
        ),
        base_url="https://ade.musematic.ai",
        headers={"Origin": "https://ade.musematic.ai"},
    )
    with client:
        token = client.get("/login/challenge").json()["csrf_token"]
        client.post("/login", data={"csrf_token": token, "password": password})

        def post(path: str, **fields: Any) -> httpx.Response:
            csrf = client.get("/api/dashboard/session").json()["csrf_token"]
            return client.post(f"/api/dashboard{path}", json={"csrf_token": csrf, **fields})

        assert client.get(f"/api/dashboard/projects/{PROJECT}/triggers").json() == {"items": []}
        assert client.get("/api/dashboard/projects/nothing/triggers").status_code == 404
        refused = post(f"/projects/{PROJECT}/triggers", section="issues", kind="push")
        assert refused.status_code == 422 and "works on items" in refused.json()["detail"]
        made = post(
            f"/projects/{PROJECT}/triggers",
            section="code-audit",
            kind="schedule",
            spec={"every": "week", "weekday": 6, "hour": 1, "scope": "The API"},
        )
        assert made.status_code == 201 and made.json()["says"] == "Every Sunday at 01:00 UTC"
        trigger = made.json()["id"]
        fired = post(f"/triggers/{trigger}/fire")
        assert fired.status_code == 200 and fired.json()["last_result"].startswith("Started")
        assert post(f"/triggers/{trigger}/fire").status_code == 409
        changed = post(f"/triggers/{trigger}/update", enabled=False)
        assert changed.status_code == 200 and changed.json()["enabled"] is False
        (row,) = client.get(f"/api/dashboard/projects/{PROJECT}/triggers").json()["items"]
        assert row["fired"] == 1 and row["enabled"] is False
        assert post(f"/triggers/{trigger}/remove").status_code == 200
        assert post(f"/triggers/{trigger}/remove").status_code == 404
        assert "trigger fired" in client.get("/api/dashboard/activity/recent").text
