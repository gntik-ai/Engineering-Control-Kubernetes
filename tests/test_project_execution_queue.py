"""The Epic planner shares the existing delivery engine and GitHub approval boundary."""

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from engineering_control.dashboard.project_records import ProjectConflict, ProjectInvalid
from engineering_control.dashboard.workspace_publishing import schedule_classification
from engineering_control.db.models import (
    BacklogItem,
    ConnectionFinding,
    ProjectConnection,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
    SandboxRun,
    WorkQueueEntry,
    WorkspaceAction,
    WorkspaceCandidate,
)
from engineering_control.github_app import GitHubApp
from tests.test_automatic_planning import Model, configure, post
from tests.test_dashboard_web import _authenticated_client

ROOT = "/api/dashboard/projects/falcone/"
FAKE_CREDENTIAL = str(uuid4())


def seed(repo: Any) -> tuple[str, list[str], list[str]]:
    with repo.sessions.begin() as db:
        p = db.scalar(select(ProjectRecord).where(ProjectRecord.name == "falcone"))
        epic = ProjectEpic(
            project_id=p.id, title="Runtime migration", description="Migrate the worker runtime."
        )
        db.add(epic)
        db.flush()
        stories = [
            ProjectUserStory(project_id=p.id, title=t)
            for t in ("Provision workers", "Process jobs")
        ]
        db.add_all(stories)
        db.flush()
        db.add_all(
            [ProjectStoryEpic(project_id=p.id, epic_id=epic.id, story_id=s.id) for s in stories]
        )
        items = [
            BacklogItem(
                project_id=p.id,
                source="github",
                repository=p.source_repository,
                title=t,
                state="proposed",
                issue_state="open",
                issue_number=n,
                workspace_review="approved",
                issue_labels=["triage"],
                description=t,
            )
            for n, t in [(71, "Add the worker API"), (72, "Fix worker startup")]
        ]
        db.add_all(items)
        db.flush()
        for i, s in [(0, 0), (1, 0), (1, 1)]:
            db.add(ProjectIssueStory(project_id=p.id, item_id=items[i].id, story_id=stories[s].id))
        return str(epic.id), [str(s.id) for s in stories], [str(i.id) for i in items]


def evaluate(ctx: dict[str, Any]) -> dict[str, Any]:
    if "issues" in ctx:
        root = next((c["id"] for c in ctx["candidates"] if c["number"] == 71), None)
        return {
            "issues": [
                {
                    "id": r["id"],
                    "type": "bug" if r["number"] == 72 else "enhancement",
                    "reason": "Compare expected behavior with the supported API.",
                    "ambiguous": False,
                    "priority": 2 if r["number"] == 71 else 1,
                    "requires": [root] if root and r["number"] == 72 else [],
                    "order_reason": "Provide the worker API first.",
                }
                for r in ctx["issues"]
            ]
        }
    return {
        "stories": [
            {"id": s["id"], "reason": "Make the runtime available to consumers.", "priority": idx}
            for idx, s in enumerate(ctx["stories"], 1)
        ]
    }


def analyze(repo: Any) -> None:
    for _ in range(10):
        if not asyncio.run(repo.execution_plans.tick(Model(evaluate))):
            return
    pytest.fail("The Epic plan did not reach review")


def test_epic_plan_project_scope_approval_labels_order_and_execution(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    with client:
        configure(repo, client)
        epic, stories, items = seed(repo)
        ready = client.get(ROOT + "execution").json()
        assert any(e["id"] == epic for e in ready["epics"])
        assert client.get("/console/workspace/falcone/queue").status_code == 200
        with repo.sessions.begin() as db:
            db.add(
                WorkspaceCandidate(
                    project_id=repo.projects.identity_of("falcone"),
                    kind="issues",
                    identity="gap-proposal",
                    origin="model",
                    title="Add recovery probes",
                    parents=[stories[0]],
                )
            )
        preview = client.get(ROOT + "execution/" + epic).json()
        assert (preview["stories"], preview["issues"], preview["proposals"][0]["title"]) == (
            2,
            2,
            "Add recovery probes",
        )
        q = post(client, "plans", epic=epic).json()["id"]
        assert post(client, "plans", epic=epic).json()["id"] == q
        assert client.get("/api/dashboard/projects/falcone-workflow/queues/" + q).status_code == 404
        assert client.get("/api/dashboard/projects/falcone-workflow/plans/" + q).status_code == 404
        assert post(client, "queues/" + q + "/start").status_code == 409
        with pytest.raises(ProjectConflict, match="reserved"):
            repo.runs.start_item(UUID(items[0]), toolchain={})
        analyze(repo)
        path = "plans/" + q

        def detail() -> dict[str, Any]:
            return client.get(ROOT + path).json()

        def action(operation: str, **body: Any) -> Any:
            return post(client, path + "/" + operation, version=detail()["version"], **body)

        plan = detail()
        assert plan["status"] == "review" and [i["id"] for i in plan["issues"]] == items
        assert set(plan["issues"][1]["stories"]) == set(stories)
        assert action("start").status_code == 409
        assert action("save", order=list(reversed(items))).status_code == 422
        assert (
            action("save", requires={items[0]: [items[1]], items[1]: [items[0]]}).status_code == 422
        )
        assert (
            action(
                "classify", types=dict(zip(items, ["enhancement", "bug"], strict=True))
            ).status_code
            == 200
        )
        assert post(client, path + "/save", version=plan["version"]).status_code == 409
        with repo.sessions() as db:
            labels = list(
                db.scalars(select(WorkspaceAction).where(WorkspaceAction.operation == "label"))
            )
            assert {r.number for r in labels} == {71, 72}
            assert all(r.status == "pending" for r in labels)
        assert action("refresh").status_code == 200
        analyze(repo)
        assert all(i["confirmed"] for i in detail()["issues"])
        started = action("start")
        assert started.status_code == 200, started.text
        assert repo.queues.advance() == 1
        with repo.sessions.begin() as db:
            first = db.scalar(select(SandboxRun).where(SandboxRun.item_id == UUID(items[0])))
            assert first and first.data["flow"]["key"] == "deliver-issue"
            first.state = "waiting"
        assert repo.queues.advance() == 0
        assert repo.queues.get(UUID(q))["entries"][0]["detail"] == "Awaiting approval"
        with repo.sessions.begin() as db:
            first = db.scalar(select(SandboxRun).where(SandboxRun.item_id == UUID(items[0])))
            first.state = "failed"
        assert repo.queues.advance() == 0
        assert "prerequisite" in repo.queues.get(UUID(q))["entries"][1]["detail"]
        assert post(client, f"queues/{q}/items/{items[0]}/retry").status_code == 200
        assert repo.queues.advance() == 1
        with repo.sessions.begin() as db:
            entry = db.scalar(
                select(WorkQueueEntry).where(
                    WorkQueueEntry.queue_id == UUID(q), WorkQueueEntry.item_id == UUID(items[0])
                )
            )
            db.get(SandboxRun, entry.run_id).state = "succeeded"
        assert repo.queues.advance() == 1


def test_draft_label_waits_for_publication_and_changed_decisions_converge(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    with client:
        epic, stories, items = seed(repo)
        with repo.sessions.begin() as db:
            p = db.scalar(select(ProjectRecord).where(ProjectRecord.name == "falcone"))
            draft = BacklogItem(
                project_id=p.id,
                execution_type="bug",
                type_confirmed=True,
                source="manual",
                title="Draft",
                state="proposed",
                workspace_review="pending",
            )
            db.add(draft)
            db.flush()
            key = draft.id
            schedule_classification(db, p, draft)
            assert db.scalar(select(WorkspaceAction.id)) is None
        approval = repo.publishing.approve("falcone", key)
        github = AsyncMock()
        github.repository_token.return_value = FAKE_CREDENTIAL
        github.create_workspace_issue.return_value = 81
        result = asyncio.run(repo.publishing.sync("falcone", approval, github))
        assert result["status"] == "done"
        with repo.sessions() as db:
            action = db.scalar(select(WorkspaceAction).where(WorkspaceAction.operation == "label"))
            assert action and action.number == 81
            label_id = action.id
        claimed, category, _ = repo.publishing._claim("falcone", label_id)
        assert category == "bug"
        with repo.sessions.begin() as db:
            draft = db.get(BacklogItem, key)
            draft.execution_type = "enhancement"
            schedule_classification(db, db.get(ProjectRecord, draft.project_id), draft)
        repo.publishing._update(
            "falcone", claimed, number=81, category=category, labels=["bug", "triage"]
        )
        github.classify_workspace_issue.return_value = ["triage", "enhancement"]
        assert asyncio.run(repo.publishing.sync("falcone", label_id, github))["status"] == "done"
        github.classify_workspace_issue.assert_awaited_once_with(
            "gntik-ai/falcone", 81, "enhancement", token=FAKE_CREDENTIAL
        )
        with repo.sessions() as db:
            assert db.get(BacklogItem, key).issue_labels == ["triage", "enhancement"]


@pytest.mark.asyncio
async def test_github_classification_only_mutates_the_two_type_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []

    def mock(r: httpx.Request) -> httpx.Response:
        seen.append((r.method, r.url.path))
        if r.method == "GET":
            return httpx.Response(200, json={"number": 71, "state": "open"})
        if r.method == "POST":
            assert json.loads(r.content) == {"labels": ["bug"]}
            return httpx.Response(
                200, json=[{"name": n} for n in ("bug", "enhancement", "security", "release")]
            )
        assert r.method == "DELETE" and r.url.path.endswith("/labels/enhancement")
        return httpx.Response(200, json=[{"name": n} for n in ("bug", "security", "release")])

    app = GitHubApp(
        app_id=1,
        installation_id=1,
        private_key_file=tmp_path / "unused",
        transport=httpx.MockTransport(mock),
    )
    monkeypatch.setattr(app, "_installation_token", AsyncMock(return_value=FAKE_CREDENTIAL))
    try:
        assert set(
            await app.classify_workspace_issue("gntik-ai/falcone", 71, "bug", token=FAKE_CREDENTIAL)
        ) == {"bug", "security", "release"}
        assert len(seen) == 3
    finally:
        await app.aclose()


def test_external_prerequisites_hold_only_the_owning_project_and_detect_cycles(
    tmp_path: Path,
) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        configure(repo, client)
        local_epic, _, local_items = seed(repo)
        with repo.sessions.begin() as db:
            p = db.scalar(select(ProjectRecord).where(ProjectRecord.name == "falcone"))
            peer = db.scalar(
                select(ProjectRecord).where(ProjectRecord.name == "falcone-serverless")
            )
            peer.planning_provider, peer.planning_model = "fixture", "chosen"
            epic = ProjectEpic(project_id=peer.id, title="Provide FaaS API")
            story = ProjectUserStory(project_id=peer.id, title="Invoke a function")
            prerequisite = BacklogItem(
                project_id=peer.id,
                source="github",
                repository=peer.source_repository,
                title="Expose invocation endpoint",
                state="proposed",
                workspace_review="approved",
                issue_number=81,
                issue_state="open",
            )
            db.add_all([epic, story, prerequisite])
            db.flush()
            db.add(ProjectStoryEpic(project_id=peer.id, epic_id=epic.id, story_id=story.id))
            db.add(
                ProjectIssueStory(project_id=peer.id, item_id=prerequisite.id, story_id=story.id)
            )
            first, second = sorted([peer.id, p.id])
            conn = ProjectConnection(
                first_id=first, second_id=second, owner_id=p.id, dependent_id=p.id
            )
            db.add(conn)
            db.flush()
            db.add(
                ConnectionFinding(
                    connection_id=conn.id,
                    identity="link",
                    fingerprint="observed",
                    source_project=p.id,
                    source_kind="issues",
                    source_id=UUID(local_items[0]),
                    target_kind="issues",
                    target_id=prerequisite.id,
                    action="link",
                    origin="model",
                    status="approved",
                    evidence={"target_title": prerequisite.title},
                )
            )
            external, peer_epic = str(prerequisite.id), epic.id
        plans = repo.execution_plans
        local_q = UUID(plans.create("falcone", UUID(local_epic))["id"])
        peer_q = UUID(plans.create("falcone-serverless", peer_epic)["id"])
        analyze(repo)

        def change(name: str, q: UUID, op: str, **fields: Any) -> Any:
            version = plans.detail(name, q)["version"]
            args = {"types": {}, "stories": None, "order": None, "requires": None, **fields}
            return plans.change(name, q, version, op, **args)

        assert plans.detail("falcone", local_q)["issues"][0]["external"][0]["record"] == external
        change(
            "falcone",
            local_q,
            "save",
            requires={local_items[0]: [external], local_items[1]: [local_items[0]]},
        )
        with pytest.raises(ProjectInvalid, match="cycle"):
            change("falcone-serverless", peer_q, "save", requires={external: [local_items[0]]})
        change("falcone", local_q, "classify", types=dict.fromkeys(local_items, "bug"))
        change("falcone-serverless", peer_q, "classify", types={external: "enhancement"})
        change("falcone", local_q, "start")
        assert repo.queues.advance() == 0
        change("falcone-serverless", peer_q, "start")
        assert repo.queues.advance() == 1
        with repo.sessions.begin() as db:
            remote = db.get(BacklogItem, UUID(external))
            remote.issue_state = "closed"
            db.scalar(select(SandboxRun).where(SandboxRun.item_id == remote.id)).state = "waiting"
        assert repo.queues.advance() == 0
        with repo.sessions.begin() as db:
            db.scalar(
                select(SandboxRun).where(SandboxRun.item_id == UUID(external))
            ).state = "succeeded"
        assert repo.queues.advance() == 1
        with repo.sessions.begin() as db:
            db.get(BacklogItem, UUID(local_items[1])).issue_state = "closed"
            db.scalar(
                select(SandboxRun).where(SandboxRun.item_id == UUID(local_items[0]))
            ).state = "succeeded"
        assert repo.queues.advance() == 0
        assert repo.queues.get(local_q)["entries"][1]["state"] == "skipped"
    finally:
        client.__exit__(None, None, None)


def test_plan_checkpoints_across_batches_without_losing_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engineering_control.dashboard import execution_plans

    monkeypatch.setattr(execution_plans, "BATCH", 1)
    client, repo = _authenticated_client(tmp_path)
    try:
        configure(repo, client)
        epic, stories, items = seed(repo)
        key = UUID(repo.execution_plans.create("falcone", UUID(epic))["id"])
        assert asyncio.run(repo.execution_plans.tick(Model(evaluate)))
        assert repo.execution_plans.detail("falcone", key)["processed"] == 1
        restarted = execution_plans.ExecutionPlans(repo.sessions)
        for _ in range(3):
            assert asyncio.run(restarted.tick(Model(evaluate)))
        plan = restarted.detail("falcone", key)
        assert plan["status"] == "review" and {s["id"] for s in plan["stories"]} == set(stories)
        assert [i["id"] for i in plan["issues"]] == items
        assert not asyncio.run(restarted.tick(Model(evaluate)))
    finally:
        client.__exit__(None, None, None)
