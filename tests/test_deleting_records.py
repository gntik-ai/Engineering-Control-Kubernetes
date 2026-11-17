"""Deleting what the platform keeps: items of the backlog, runs that have ended,
evaluations and batches. What is still at work is never deleted."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from engineering_control.dashboard.project_records import ProjectConflict, ProjectNotFound
from engineering_control.db.models import (
    BacklogItem,
    Evaluation,
    EvaluationCase,
    ModelBatch,
    ModelBatchRequest,
    ProjectRecord,
    SandboxRun,
    SandboxStep,
    WorkQueueEntry,
)
from engineering_control.engine.evaluations import Evaluations
from engineering_control.models.batches import remove as remove_batch
from engineering_control.sandbox.runs import CONVERSATION
from tests.test_dashboard_accounts import _console
from tests.test_queues import world  # noqa: F401 - a fixture

NOW = datetime.now(UTC)


def _run(world: Any, state: str, *, item: UUID | None = None, **more: Any) -> UUID:  # noqa: F811
    with world.sessions.begin() as session:
        run = SandboxRun(
            kind=more.pop("kind", "flow"),
            state=state,
            title=more.pop("title", "A run"),
            project_id=more.pop("project_id", world.project_id),
            item_id=item,
            cancel_requested=False,
            data={"project": "falcone-workflow"},
            finished_at=None if state in ("queued", "running", "waiting") else NOW,
            **more,
        )
        session.add(run)
        session.flush()
        session.add(
            SandboxStep(run_id=run.id, position=0, name="ask", state="succeeded", log="said")
        )
        return run.id


def _count(world: Any, table: Any) -> int:  # noqa: F811
    with world.sessions() as session:
        return len(session.scalars(select(table)).all())


def test_an_item_is_deleted_with_its_places_in_queues_and_its_runs_stay(world: Any) -> None:  # noqa: F811
    first, second = world.items[0], world.items[1]
    world.queue(0, 1)
    ended = _run(world, "succeeded", item=first)
    gone = world.backlog.remove(first)
    assert (gone["title"], gone["project"]) == ("Expired cookie", "falcone-workflow")
    # Whether it comes back with the next import: only an issue that is open on GitHub.
    assert gone["returns"] is False
    with world.sessions() as session:
        assert session.get(BacklogItem, first) is None
        assert [entry.item_id for entry in session.scalars(select(WorkQueueEntry))] == [second]
        # The run that worked on it stays, without it.
        assert session.get(SandboxRun, ended).item_id is None
    with pytest.raises(ProjectNotFound):
        world.backlog.remove(first)

    # An item a run is working on is not deleted.
    busy = _run(world, "running", item=second)
    with pytest.raises(ProjectConflict, match="A run of the item has not ended"):
        world.backlog.remove(second)
    with world.sessions.begin() as session:
        session.get(SandboxRun, busy).state = "failed"
    assert world.backlog.remove(second)["title"] == "Export to CSV"
    assert _count(world, WorkQueueEntry) == 0


def test_the_items_a_filter_matches_are_deleted_together(world: Any) -> None:  # noqa: F811
    _run(world, "waiting", item=world.items[3])
    with pytest.raises(Exception, match="exactly one change"):
        world.backlog.bulk(
            "falcone-workflow",
            section=None,
            state=None,
            search=None,
            expected=4,
            remove=True,
            state_to="skipped",
        )
    with pytest.raises(ProjectConflict, match="now matches 4 items, not 3"):
        world.backlog.bulk(
            "falcone-workflow", section=None, state=None, search=None, expected=3, remove=True
        )
    done = world.backlog.bulk(
        "falcone-workflow", section=None, state=None, search=None, expected=4, remove=True
    )
    # The one a run still waits on stays.
    assert done == {"project": "falcone-workflow", "changed": 3, "unchanged": 1}
    with world.sessions() as session:
        left = session.scalars(select(BacklogItem).order_by(BacklogItem.title)).all()
    # The other project's item was never matched.
    assert [item.title for item in left] == ["Of another project", "Wrong total"]


def test_a_run_that_has_ended_is_deleted_with_what_is_its_own(world: Any) -> None:  # noqa: F811
    ended = _run(world, "failed")
    with world.sessions.begin() as session:
        step = session.scalars(select(SandboxStep).where(SandboxStep.run_id == ended)).one()
        batch = ModelBatch(
            step_id=step.id,
            run_id=ended,
            provider="anthropic",
            model="m",
            state="ended",
            requests=1,
            answered=1,
            deadline_at=NOW,
        )
        session.add(batch)
        session.flush()
        session.add(
            ModelBatchRequest(
                batch_id=batch.id, name="question", position=0, question={}, state="waiting"
            )
        )
    entry_queue = world.queue(0)
    with world.sessions.begin() as session:
        entry = session.scalars(select(WorkQueueEntry)).one()
        entry.run_id = ended
    gone = world.runs.remove(ended)
    assert (gone["id"], gone["state"]) == (str(ended), "failed")
    assert _count(world, SandboxRun) == 0 and _count(world, SandboxStep) == 0
    assert _count(world, ModelBatch) == 0 and _count(world, ModelBatchRequest) == 0
    # What only pointed at the run is kept, without it.
    with world.sessions() as session:
        assert session.scalars(select(WorkQueueEntry)).one().run_id is None
    assert world.queues.get(UUID(entry_queue["id"]))["name"] == "Next"
    with pytest.raises(ProjectNotFound):
        world.runs.remove(ended)

    # A run that has not ended is stopped first, and a turn of the supervisor is no run.
    for state in ("queued", "running", "waiting"):
        with pytest.raises(ProjectConflict, match="has not ended: stop it first"):
            world.runs.remove(_run(world, state))
    with pytest.raises(ProjectNotFound):
        world.runs.remove(_run(world, "succeeded", kind=CONVERSATION, project_id=None))


def test_the_runs_that_ended_long_enough_ago_are_deleted_together(world: Any) -> None:  # noqa: F811
    with world.sessions() as session:
        other = session.query(ProjectRecord).filter_by(name="falcone").one().id
    old = _run(world, "succeeded")
    recent = _run(world, "cancelled")
    elsewhere = _run(world, "failed", project_id=other)
    working = _run(world, "running")
    turn = _run(world, "succeeded", kind=CONVERSATION, project_id=None)
    with world.sessions.begin() as session:
        for run_id in (old, elsewhere):
            session.get(SandboxRun, run_id).finished_at = NOW - timedelta(days=40)

    def left() -> set[UUID]:
        with world.sessions() as session:
            return set(session.scalars(select(SandboxRun.id)))

    assert world.runs.remove_ended(project="falcone-workflow", days=30)["removed"] == 1
    assert left() == {recent, elsewhere, working, turn}
    assert world.runs.remove_ended(days=30)["removed"] == 1
    assert left() == {recent, working, turn}
    # Every run that has ended: what is at work and the supervisor's turns stay.
    assert world.runs.remove_ended()["removed"] == 1
    assert left() == {working, turn}
    assert world.runs.remove_ended() == {"removed": 0, "project": None, "days": 0}


def test_an_evaluation_is_deleted_with_the_runs_it_made(world: Any) -> None:  # noqa: F811
    past = _run(world, "succeeded", title="Past work")
    tried = _run(world, "succeeded", kind="evaluation", title="Evaluation: past work")
    with world.sessions.begin() as session:
        evaluation = Evaluation(
            flow_key="deliver-issue",
            candidate_version=1,
            baseline_version=0,
            state="running",
            document={},
            sha256="0" * 64,
        )
        session.add(evaluation)
        session.flush()
        session.add(
            EvaluationCase(evaluation_id=evaluation.id, arm="candidate", subject={}, run_id=tried)
        )
        session.add(
            EvaluationCase(evaluation_id=evaluation.id, arm="baseline", subject={}, run_id=past)
        )
        evaluation_id = evaluation.id
    lab = Evaluations(world.sessions, world.runs)
    with pytest.raises(ProjectConflict, match="under way: stop it first"):
        lab.remove(evaluation_id)
    with world.sessions.begin() as session:
        session.get(Evaluation, evaluation_id).state = "stopped"
    gone = lab.remove(evaluation_id)
    assert (gone["evaluation"], gone["runs"]) == ("deliver-issue version 1", 1)
    assert _count(world, Evaluation) == 0 and _count(world, EvaluationCase) == 0
    # The run it made went with it; the past work it was compared with stays.
    with world.sessions() as session:
        assert [run.id for run in session.scalars(select(SandboxRun))] == [past]
    with pytest.raises(ProjectNotFound):
        lab.remove(evaluation_id)


def test_what_is_kept_of_a_batch_that_ended_is_deleted(world: Any) -> None:  # noqa: F811
    working = _run(world, "running")

    def batch(state: str, run_id: UUID | None = None) -> UUID:
        with world.sessions.begin() as session:
            row = ModelBatch(
                run_id=run_id,
                provider="openai",
                model="m",
                state=state,
                requests=1,
                answered=0,
                deadline_at=NOW,
            )
            session.add(row)
            session.flush()
            session.add(
                ModelBatchRequest(
                    batch_id=row.id, name="q", position=0, question={}, state="waiting"
                )
            )
            return row.id

    for state in ("submitting", "waiting", "stopping"):
        with pytest.raises(ProjectConflict, match="still out at the provider"):
            remove_batch(world.sessions, batch(state))
    with pytest.raises(ProjectConflict, match="has not ended"):
        remove_batch(world.sessions, batch("ended", working))
    before = _count(world, ModelBatch)
    assert remove_batch(world.sessions, batch("failed"))["batch"] == "openai m"
    assert remove_batch(world.sessions, batch("ended"))["batch"] == "openai m"
    assert _count(world, ModelBatch) == before
    assert _count(world, ModelBatchRequest) == before
    with pytest.raises(ProjectNotFound):
        remove_batch(world.sessions, UUID(int=1))


def test_the_console_deletes_items_runs_and_batches(tmp_path: Path) -> None:
    client: TestClient = _console(tmp_path, None)
    try:
        repository = client.app.state.dashboard_repository  # type: ignore[attr-defined]
        with repository.sessions.begin() as session:
            project = session.query(ProjectRecord).filter_by(name="falcone").one()
            items = [
                BacklogItem(
                    project_id=project.id,
                    title=title,
                    source="manual",
                    state="accepted",
                    issue_labels=[],
                )
                for title in ("Wrong total", "Slow search", "Export to CSV")
            ]
            session.add_all(items)
            runs = [
                SandboxRun(
                    kind="flow",
                    state=state,
                    title=state,
                    project_id=project.id,
                    cancel_requested=False,
                    data={"project": "falcone"},
                    finished_at=None if state == "running" else NOW,
                )
                for state in ("succeeded", "failed", "running")
            ]
            session.add_all(runs)
            batch = ModelBatch(
                provider="anthropic", model="m", state="ended", requests=1, deadline_at=NOW
            )
            session.add(batch)
            session.flush()
            item_ids = [str(item.id) for item in items]
            run_ids = [str(run.id) for run in runs]
            batch_id = str(batch.id)

        def post(path: str, **fields: Any) -> httpx.Response:
            csrf = client.get("/api/dashboard/session").json()["csrf_token"]
            return client.post(f"/api/dashboard{path}", json={"csrf_token": csrf, **fields})

        gone = post(f"/items/{item_ids[0]}/remove")
        assert gone.status_code == 200 and gone.json()["title"] == "Wrong total"
        assert post(f"/items/{item_ids[0]}/remove").status_code == 404
        # Many at once need the password again.
        together = {"expected": 2, "remove": True}
        assert post("/projects/falcone/items/bulk", **together).status_code == 422
        wrong = post("/projects/falcone/items/bulk", current_password="not it", **together)  # noqa: S106
        assert wrong.status_code == 403
        from tests.test_dashboard_accounts import PASSWORD

        done = post("/projects/falcone/items/bulk", current_password=PASSWORD, **together)
        assert done.status_code == 200 and done.json()["changed"] == 2
        assert client.get("/api/dashboard/items?project=falcone").json()["total"] == 0

        assert post(f"/runs/{run_ids[0]}/remove").status_code == 200
        assert post(f"/runs/{run_ids[2]}/remove").status_code == 409
        assert post("/runs/remove-ended").status_code == 422
        swept = post("/runs/remove-ended", current_password=PASSWORD, project="falcone")
        assert swept.status_code == 200 and swept.json()["removed"] == 1
        assert [run["id"] for run in client.get("/api/dashboard/runs").json()["items"]] == [
            run_ids[2]
        ]

        assert post(f"/batches/{batch_id}/remove").status_code == 200
        assert post(f"/batches/{batch_id}/remove").status_code == 404
        assert client.get("/api/dashboard/batches").json()["items"] == []
        assert post(f"/evaluations/{batch_id}/remove").status_code == 404
    finally:
        client.__exit__(None, None, None)
