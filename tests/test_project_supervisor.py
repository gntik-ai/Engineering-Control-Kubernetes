"""Project-scoped Supervisor regressions across the shared chat/queue/review boundaries."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from engineering_control.dashboard.project_records import ProjectConflict, ProjectRecords
from engineering_control.db.models import (
    BacklogItem,
    PlanningReview,
    ProjectRecord,
    ReviewFlowRun,
    SandboxRun,
    WorkspaceCandidate,
)
from engineering_control.engine.builtin import BUILTIN
from engineering_control.models import decisions
from engineering_control.models.openrouter import DIRECT_MODEL
from engineering_control.supervisor import advice
from engineering_control.supervisor.operations import Operations
from tests.test_dashboard_web import _authenticated_client

pytest_plugins = ("tests.test_supervisor",)


def test_project_scope_replay_and_scoped_stop(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        csrf = client.get("/api/dashboard/session").json()["csrf_token"]

        def endpoint(name: str) -> str:
            return "/api/dashboard/projects/" + name + "/supervisor"

        def post(name: str, path: str, **data: Any) -> Any:
            return client.post(endpoint(name) + path, json={"csrf_token": csrf, **data})

        key = str(uuid4())
        first = post("falcone", "/messages", text="Inspect issue 42", request_id=key)
        assert first.status_code == 201, first.text
        assert (
            post("falcone", "/messages", text="Inspect issue 42", request_id=key).json()["id"]
            == first.json()["id"]
        )
        assert post("falcone", "/messages", text="Different", request_id=key).status_code == 409
        second = post("falcone-serverless", "/messages", text="Only my queue", request_id=key)
        assert second.status_code == 201
        a = client.get(endpoint("falcone")).json()
        b = client.get(endpoint("falcone-serverless")).json()
        assert [m["text"] for m in a["items"]] == ["Inspect issue 42"]
        assert [m["text"] for m in b["items"]] == ["Only my queue"]
        assert client.get("/api/dashboard/supervisor").json()["turn"] is None
        assert post("falcone", "/stop", run_id=b["turn"]["run_id"]).status_code == 409
        assert post("falcone", "/stop", run_id=a["turn"]["run_id"]).status_code == 200
        assert (
            client.get(endpoint("falcone-serverless")).json()["turn"]["run_id"]
            == b["turn"]["run_id"]
        )
    finally:
        client.__exit__(None, None, None)


def test_scope_enforced_at_tool_and_approval(world: Any) -> None:
    other = "other-project"
    ProjectRecords(world.sessions).create(
        name=other, display_name="Other", repository="example/other", default_branch="main"
    )
    conv = world.conversation
    conv.post("Classify only my issue", channel="console", thread=other)
    rid = UUID(conv.listing(thread=other)["turn"]["run_id"])
    definition = world.definitions.current()

    async def call(run: UUID, name: str, args: dict[str, Any]) -> Any:
        return await world.toolbox.call(run, definition, name, args)

    fields = {
        "project": "falcone-workflow",
        "item": str(world.item_id),
        "type": "bug",
        "reason": "Existing sign-in fails.",
    }
    assert asyncio.run(call(rid, "classify_issue", fields)).error
    assert asyncio.run(call(rid, "get_item", {"item": str(world.item_id)})).error
    assert not world.toolbox.pending(thread=other)
    conv.post(
        "Classify the identified issue",
        channel="console",
        thread="falcone-workflow",
        projects=[other],
    )
    allowed = UUID(conv.listing(thread="falcone-workflow")["turn"]["run_id"])
    assert not asyncio.run(call(allowed, "classify_issue", fields)).error
    entry = world.toolbox.pending(thread="falcone-workflow")[0]
    assert entry["arguments"]["type"] == "bug"
    assert not world.toolbox.pending(thread=other)
    world.toolbox.decide(
        UUID(entry["action_id"]), accept=True, via="console", thread="falcone-workflow"
    )
    with world.sessions() as db:
        assert db.get(BacklogItem, world.item_id).type_confirmed
    before, asked = conv.history(allowed)
    assert [m["text"] for m in asked] == ["Classify the identified issue"]
    assert all("only my issue" not in m["text"] for m in before)


def test_review_history_pages_attention_and_idle_monitor(world: Any) -> None:
    baseline = world.toolbox.reviews.listing(["falcone-workflow"])["total"]
    assert baseline == 0  # Connecting a repository has not started its first review.
    with world.sessions.begin() as db:
        project = db.scalar(select(ProjectRecord))
        runs = [
            ReviewFlowRun(
                project_id=project.id,
                action="epics",
                flow_name="Review Epics",
                flow_key="review-epics",
                version=0,
                settings={},
                finished_at=datetime.now(UTC),
                outcome="succeeded",
            )
            for _ in range(56)
        ]
        db.add_all(runs)
        db.flush()
        run_id = runs[-1].id
        db.add(
            WorkspaceCandidate(
                project_id=project.id,
                review_run_id=run_id,
                kind="epics",
                title="Restore sessions",
                identity="restore",
                decision="pending",
            )
        )
        db.add(
            PlanningReview(
                project_id=project.id,
                kind="issues",
                record_id=uuid4(),
                title="Paused intake",
                status="paused",
                scope="intake",
            )
        )
        db.add(
            SandboxRun(
                project_id=project.id,
                kind="audit_code",
                title="Code audit",
                state="succeeded",
                data={},
            )
        )
    history = world.toolbox.reviews
    rows = history.listing(["falcone-workflow"])
    assert rows["total"] == baseline + 58 and rows["pages"] == 3 and len(rows["items"]) == 20
    assert len(history.listing(["falcone-workflow"], page=3)["items"]) == baseline + 18
    assert history.listing(["falcone-workflow"], state="attention")["total"] == 1
    details = history.detail("falcone-workflow", "review", run_id)
    assert details["proposals"][0]["title"] == "Restore sessions"
    assert "issues" in rows["never_started"]["falcone-workflow"]
    watcher = Operations(world.sessions, world.conversation)
    now = datetime.now(UTC)
    assert watcher.watch(now) == 1 and watcher.watch(now + timedelta(minutes=2)) == 0
    assert watcher.dispatch(now) is None
    with world.sessions.begin() as db:
        db.scalar(select(WorkspaceCandidate)).decision = "accepted"
    watcher.watch(now + timedelta(minutes=4))
    assert watcher.listing(project="falcone-workflow")["items"] == []
    with world.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ReviewFlowRun)) == 56
    with world.sessions.begin() as db:
        db.scalar(select(ProjectRecord)).archived_at = now
    assert (
        history.detail("falcone-workflow", "review", run_id)["proposals"][0]["decision"]
        == "accepted"
    )


def test_project_flow_creation_archive_and_restore(world: Any) -> None:
    p = "falcone-workflow"
    conv = world.conversation
    conv.post("Draft a project flow", channel="console", thread=p)
    rid = UUID(conv.listing(thread=p)["turn"]["run_id"])

    def change(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        answer = asyncio.run(world.toolbox.call(rid, world.definitions.current(), tool, args))
        assert not answer.error, answer.text
        action = world.toolbox.pending(thread=p)[0]
        return world.toolbox.decide(
            UUID(action["action_id"]), accept=True, via="console", thread=p
        )["result"]

    doc = BUILTIN["audit-code"].as_dict()
    doc.update(key="local-audit", name="Project audit")
    saved = change("propose_flow", {"document": doc, "reason": "Audit this project."})
    key = saved["flow"]
    assert world.toolbox.flows.owner(key) == p
    assert world.toolbox.flows.impact(key)["versions"]
    with world.sessions.begin() as db:
        project = db.scalar(select(ProjectRecord).where(ProjectRecord.name == p))
        running = ReviewFlowRun(
            project_id=project.id,
            action="issues",
            flow_key=key,
            flow_name="Active review",
            version=0,
            settings={},
        )
        db.add(running)
        db.flush()
        review_id = running.id
    assert str(review_id) in world.toolbox.flows.impact(key)["active_runs"]
    with pytest.raises(ProjectConflict, match="has not ended"):
        world.toolbox.flows.remove(key, archive=True)
    with world.sessions.begin() as db:
        db.get(ReviewFlowRun, review_id).finished_at = datetime.now(UTC)

    change(
        "retire_flow",
        {"flow": key, "operation": "archive", "reason": "Hide it and keep its versions."},
    )
    assert not world.toolbox.flows.exists(key)
    world.toolbox.flows.put_back(key)
    assert world.toolbox.flows.exists(key)
    change("retire_flow", {"flow": key, "operation": "delete", "reason": "Remove unused workflow."})
    assert not world.toolbox.flows.exists(key)


def test_specialist_cache_is_bound_to_source_and_flow(world: Any) -> None:
    with world.sessions.begin() as db:
        p = db.scalar(select(ProjectRecord))
        p.planning_provider = "openai"
        p.planning_model = "example-model"
    ids = [str(world.item_id)]
    first = advice.enqueue(world.sessions, "falcone-workflow", "Classify sign-in", ids)
    same = advice.enqueue(world.sessions, "falcone-workflow", "Classify sign-in", ids)
    assert same["reused"] and first["review"] == same["review"]
    with world.sessions.begin() as db:
        db.get(BacklogItem, world.item_id).description = "New evidence changes the question"
    assert (
        advice.enqueue(world.sessions, "falcone-workflow", "Classify sign-in", ids)["review"]
        != same["review"]
    )
    for provider, model in (("openai", DIRECT_MODEL), ("openrouter", "vendor/choice")):
        body = decisions.body(provider, model, "instructions", "source", False, {}, advice.RUBRIC)
        qs = body["questions"]
        assert (set(qs) if isinstance(qs, dict) else {q["name"] for q in qs}) == {
            "target",
            "category",
        }
