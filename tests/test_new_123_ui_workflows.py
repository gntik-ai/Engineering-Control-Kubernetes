"""NEW-123: UiMigrationPlanWorkflow and UiBlueprintFanOutWorkflow on a real local Temporal.

Controller steps run the real UiPlanActivityHandler and ChangeRequestActivityHandler against
SQLite; snapshot, scan, provider and GitHub steps are scripted. Every recorded history is
replayed against the current workflow code.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from new_123_fixtures import (
    INVENTORY_SHA,
    KIT_SHA,
    KIT_VERSION,
    SOURCE_SHA,
    blueprint,
    build_fake_pack,
    draft_output,
    gap_output,
    review_output,
)
from sqlalchemy import create_engine, select
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from engineering_control.activities.change_requests import ChangeRequestActivityHandler
from engineering_control.activities.core import ControlActivities
from engineering_control.activities.ui_plan import UiPlanActivityHandler
from engineering_control.config import Settings
from engineering_control.db import models
from engineering_control.db.base import Base
from engineering_control.db.change_requests import ChangeRequestRepository
from engineering_control.db.ui_blueprints import UiBlueprintRepository
from engineering_control.engineering.ui_blueprint import (
    APPROVAL_PHRASE,
    fanout_items,
    item_change_request_id,
    ui_fanout_workflow_id,
    ui_plan_workflow_id,
)
from engineering_control.workflows.definitions import (
    UiBlueprintFanOutWorkflow,
    UiMigrationPlanWorkflow,
)
from engineering_control.workflows.inputs import UiBlueprintFanOutInput, UiMigrationPlanInput
from engineering_control.workflows.worker_registry import (
    CONTROL_TASK_QUEUE,
    GITHUB_TASK_QUEUE,
    ROLE_TASK_QUEUES,
)

_TEMPORAL_CLI = shutil.which("temporal")
pytestmark = pytest.mark.skipif(
    _TEMPORAL_CLI is None, reason="requires the `temporal` CLI on PATH (local dev server)"
)
Responder = Callable[[dict[str, Any]], dict[str, Any]]
_REQUEST_KEYS = {
    "ui_plan.draft": "draft_request",
    "ui_fanout.gap_draft": "draft_request",
    "ui_plan.review": "review_request",
    "change_request.review": "review_request",
}


class Harness:
    def __init__(self, tmp_path: Path, *, pack: Path | None = None) -> None:
        self.engine = create_engine(f"sqlite:///{tmp_path / 'ui.sqlite'}")
        Base.metadata.create_all(self.engine)
        self.repository = UiBlueprintRepository(engine=self.engine)
        self.change_requests = ChangeRequestRepository(engine=self.engine)
        settings = Settings(agent_pack_root=pack or build_fake_pack(tmp_path / "pack"))
        self.ui = UiPlanActivityHandler(
            settings, repository=self.repository, change_requests=self.change_requests
        )
        self.change = ChangeRequestActivityHandler(settings, repository=self.change_requests)
        self.calls: list[str] = []
        self.payloads: dict[str, list[dict[str, Any]]] = {}
        self.responses: dict[str, list[Responder] | Responder] = {}

    def activity(self) -> Any:
        @activity.defn(name="execute_control_step")
        async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
            step = request["step"]
            payload = ControlActivities._attach_orchestration_context(request["payload"])
            self.calls.append(step)
            self.payloads.setdefault(step, []).append(payload)
            if step in self.ui.supported_steps:
                return await self.ui.handle(step, payload)
            if step in self.change.supported_steps:
                return await self.change.handle(step, payload)
            if step in {"agent.record_start", "agent.persist_result"}:
                return payload
            scripted = self.responses.get(step)
            if scripted is None:
                raise AssertionError(f"unscripted step {step!r}")
            if callable(scripted):
                return scripted(payload)
            if not scripted:
                raise AssertionError(f"no response left for {step!r}")
            return scripted.pop(0)(payload)

        return execute_control_step

    async def run(self, workflow_cls: type, argument: Any, workflow_id: str) -> Any:
        assert _TEMPORAL_CLI is not None
        queues = (
            CONTROL_TASK_QUEUE,
            GITHUB_TASK_QUEUE,
            ROLE_TASK_QUEUES["claude"],
            ROLE_TASK_QUEUES["codex"],
            ROLE_TASK_QUEUES["scan"],
        )
        activity_fn = self.activity()
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=_TEMPORAL_CLI
        ) as env:
            async with AsyncExitStack() as stack:
                for queue in queues:
                    await stack.enter_async_context(
                        Worker(
                            env.client,
                            task_queue=queue,
                            workflows=[workflow_cls],
                            activities=[activity_fn],
                        )
                    )
                handle = await env.client.start_workflow(
                    workflow_cls.run,
                    argument,
                    id=workflow_id,
                    task_queue=CONTROL_TASK_QUEUE,
                    execution_timeout=timedelta(minutes=3),
                )
                result = await handle.result()
                history = await handle.fetch_history()
        await Replayer(workflows=[workflow_cls]).replay_workflow(history)
        return result

    def notifications(self) -> list[dict[str, Any]]:
        with self.repository.sessions() as session:
            rows = session.scalars(select(models.NotificationOutbox)).all()
            return [dict(row.payload_json) for row in rows]


def agent(structured: Any) -> Responder:
    def respond(payload: dict[str, Any]) -> dict[str, Any]:
        request = payload[_REQUEST_KEYS[payload["provider_step"]]]
        now = datetime.now(UTC).isoformat()
        value = structured(payload) if callable(structured) else structured
        result = {
            "run_id": request["run_id"],
            "provider": request["provider"],
            "role": request["role"],
            "status": "SUCCESS",
            "structured_result": value,
            "started_at": now,
            "finished_at": now,
        }
        return {**payload, "status": "SUCCESS", "agent_result": result}

    return respond


def snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **payload,
        "status": "SNAPSHOT_VERIFIED",
        "source_worktree": "/srv/engineering/worktrees/falcone/snapshot",
        "source_sha": SOURCE_SHA,
    }


def inventory(payload: dict[str, Any]) -> dict[str, Any]:
    assert payload["snapshot"]["source_sha"] == SOURCE_SHA
    return {
        "status": "UI_INVENTORY_PASS",
        "inventory_sha256": INVENTORY_SHA,
        "summary": {"routed_pages": 2, "orphan_pages": 1},
        "evidence": [{"relative_path": "falcone/x/ui-inventory.json"}],
    }


def kit(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "UI_KIT_SNAPSHOT_PASS",
        "snapshot_sha256": KIT_SHA,
        "gntik_ui_version": KIT_VERSION,
        "image": "ghcr.io/gntik-ai/gntik-ui-mcp@sha256:" + "d" * 64,
        "evidence": [],
    }


def kit_unavailable(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "status": "BLOCKED", "blocker": "UI_MCP_UNAVAILABLE", "reason": "no docker"}


def staged(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "STAGED",
        "input_directory": "/srv/engineering/worktrees/ui-plan-inputs/x",
        "files": {
            "inventory": {"path": "/in/ui-inventory.json", "sha256": "1" * 64, "bytes": 10},
            "kit": {"path": "/in/ui-kit-snapshot.json", "sha256": "2" * 64, "bytes": 10},
        },
    }


def stage_document(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("publish"):
        assert payload["markdown"].startswith("# UI migration blueprint: falcone")
        return {"status": "PUBLISHED", "evidence": [{"relative_path": "falcone/bp.json"}]}
    name = payload["name"]
    return {"status": "STAGED", "document": {"path": f"/in/{name}", "sha256": "3" * 64, "bytes": 1}}


def reviewer(*decisions: str) -> Responder:
    queue = list(decisions)

    def respond(payload: dict[str, Any]) -> dict[str, Any]:
        return agent(review_output(payload["draft_sha256"], queue.pop(0)))(payload)

    return respond


def plan_harness(tmp_path: Path, **kwargs: Any) -> tuple[Harness, UiMigrationPlanInput, str]:
    harness = Harness(tmp_path, **kwargs)
    created = harness.repository.create_sync("falcone", "cli:engineering-ctl", None)
    request = UiMigrationPlanInput(
        blueprint_id=created["id"], project="falcone", requested_by="cli:engineering-ctl"
    )
    harness.responses.update(
        {
            "project.snapshot": snapshot,
            "ui.inventory": inventory,
            "ui.kit_snapshot": kit,
            "ui_plan.stage_inputs": staged,
            "ui_plan.stage_document": stage_document,
            "ui_plan.draft": agent(draft_output()),
        }
    )
    return harness, request, ui_plan_workflow_id("falcone", created["id"])


def provider_steps(calls: list[str]) -> list[str]:
    return [step for step in calls if not step.startswith("agent.")]


# Flow A ------------------------------------------------------------------------------------


async def test_plan_happy_path_persists_an_approvable_candidate(tmp_path: Path) -> None:
    harness, request, workflow_id = plan_harness(tmp_path)
    harness.responses["ui_plan.review"] = reviewer("APPROVE")
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert result["status"] == "CANDIDATE"
    assert provider_steps(harness.calls) == [
        "ui_plan.register",
        "project.snapshot",
        "ui.inventory",
        "ui.kit_snapshot",
        "ui_plan.record_inputs",
        "ui_plan.stage_inputs",
        "ui_plan.prepare_draft",
        "ui_plan.draft",
        "ui_plan.record_draft",
        "ui_plan.stage_document",
        "ui_plan.prepare_review",
        "ui_plan.review",
        "ui_plan.record_review",
        "ui_plan.stage_document",
        "ui_plan.persist",
        "ui_plan.finalize_run",
    ]
    draft_request = harness.payloads["ui_plan.draft"][0]["draft_request"]
    assert (draft_request["provider"], draft_request["role"]) == (
        "claude",
        "ui-migration-architect",
    )
    assert draft_request["effort"] == "high"
    assert draft_request["allowed_tools"] == ["Read", "Glob", "Grep"]
    assert "/in/ui-inventory.json" in draft_request["prompt"]
    assert "No MCP server is configured" in draft_request["prompt"]
    review_request = harness.payloads["ui_plan.review"][0]["review_request"]
    assert (review_request["provider"], review_request["role"]) == (
        "codex",
        "ui-migration-architect",
    )
    view = harness.repository.get_sync(UUID(request.blueprint_id))
    assert view is not None
    assert view["status"] == "CANDIDATE"
    assert view["approval_binding"] == result["approval_binding"]
    assert view["source_sha"] == SOURCE_SHA and view["kit_snapshot_sha256"] == KIT_SHA
    assert view["evidence"]["blueprint"] == [{"relative_path": "falcone/bp.json"}]
    assert view["pages"] == 3 and view["gaps"] == 3
    markdown = harness.repository.markdown_sync(UUID(request.blueprint_id))
    assert markdown is not None and "## u5 cutover" in markdown
    messages = [item["message"] for item in harness.notifications()]
    assert any("UI blueprint ready for approval" in message for message in messages)
    commands = harness.notifications()[0]["commands"]
    assert any(APPROVAL_PHRASE in command for command in commands)
    with harness.engine.connect() as connection:
        runs = connection.execute(select(models.Run.status, models.Run.run_type)).all()
    assert runs == [("SUCCESS", "ui-migration-plan")]


async def test_kit_unavailable_blocks_before_any_draft(tmp_path: Path) -> None:
    harness, request, workflow_id = plan_harness(tmp_path)
    harness.responses["ui.kit_snapshot"] = kit_unavailable
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert (result["status"], result["error_code"]) == ("BLOCKED", "UI_MCP_UNAVAILABLE")
    assert "ui_plan.draft" not in harness.calls
    assert "ui_plan.prepare_draft" not in harness.calls
    view = harness.repository.get_sync(UUID(request.blueprint_id))
    assert view is not None
    assert (view["status"], view["error_code"]) == ("BLOCKED", "UI_MCP_UNAVAILABLE")


async def test_missing_agent_pack_skill_blocks_at_step_start(tmp_path: Path) -> None:
    pack = build_fake_pack(tmp_path / "old-pack", version="1.3.1")
    harness, request, workflow_id = plan_harness(tmp_path, pack=pack)
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert (result["status"], result["error_code"]) == ("BLOCKED", "AGENT_PACK_SKILL_MISSING")
    assert result["blockers"] == ["profile version 1.3.1 is older than 1.4.0"]
    assert provider_steps(harness.calls) == ["ui_plan.register", "ui_plan.finalize_run"]


async def test_review_requests_changes_then_one_redraft_passes(tmp_path: Path) -> None:
    harness, request, workflow_id = plan_harness(tmp_path)
    harness.responses["ui_plan.draft"] = [agent(draft_output()), agent(draft_output())]
    harness.responses["ui_plan.review"] = reviewer("REVISE", "APPROVE")
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert result["status"] == "CANDIDATE"
    assert result["review_rounds"] == 2
    assert harness.calls.count("ui_plan.draft") == 2
    assert harness.calls.count("ui_plan.review") == 2
    redraft = harness.payloads["ui_plan.draft"][1]["draft_request"]["prompt"]
    assert "The 403 state is missing." in redraft
    assert "/in/review-1.json" in redraft and "/in/draft-1.json" in redraft
    names = [p.get("name") for p in harness.payloads["ui_plan.stage_document"]]
    assert names == ["draft-1.json", "review-1.json", "draft-2.json", None]


async def test_two_review_failures_block_with_the_findings(tmp_path: Path) -> None:
    harness, request, workflow_id = plan_harness(tmp_path)
    harness.responses["ui_plan.draft"] = [agent(draft_output()), agent(draft_output())]
    harness.responses["ui_plan.review"] = reviewer("REVISE", "REVISE")
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert (result["status"], result["error_code"]) == ("BLOCKED", "BLUEPRINT_REVIEW_UNRESOLVED")
    assert harness.calls.count("ui_plan.review") == 2
    assert "ui_plan.persist" not in harness.calls
    view = harness.repository.get_sync(UUID(request.blueprint_id))
    assert view is not None
    assert view["status"] == "BLOCKED"
    assert any("The 403 state is missing." in item for item in view["blockers"])
    assert view["approve_command"] is None


async def test_a_draft_bound_to_other_inputs_is_blocked(tmp_path: Path) -> None:
    harness, request, workflow_id = plan_harness(tmp_path)
    wrong = draft_output()
    wrong["inputs"] = {**wrong["inputs"], "inventory_sha256": "9" * 64}
    harness.responses["ui_plan.draft"] = agent(wrong)
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert (result["status"], result["error_code"]) == ("BLOCKED", "DRAFT_BINDING_MISMATCH")
    assert "ui_plan.review" not in harness.calls


# Fan-out -----------------------------------------------------------------------------------


def approved_blueprint(harness: Harness) -> tuple[str, str]:
    repository = harness.repository
    created = repository.create_sync("falcone", "cli:engineering-ctl", None)
    blueprint_id = UUID(created["id"])
    repository.register_sync(blueprint_id, created["workflow_id"])
    repository.record_inputs_sync(
        blueprint_id,
        {
            "source_sha": SOURCE_SHA,
            "inventory_sha256": INVENTORY_SHA,
            "kit_snapshot_sha256": KIT_SHA,
            "gntik_ui_version": KIT_VERSION,
        },
    )
    digest = repository.record_draft_sync(blueprint_id, 1, blueprint(), [])
    persisted = repository.persist_candidate_sync(blueprint_id, digest, [], {})
    repository.approve_sync(
        blueprint_id, persisted["approval_binding"], APPROVAL_PHRASE, "telegram:42"
    )
    return str(blueprint_id), digest


def gap_drafter(payload: dict[str, Any]) -> dict[str, Any]:
    key = payload["item_key"]
    kit_gap = key == "gap:gap-datatable-pagination"
    repository = "gntik-ai/gntik-ui" if kit_gap else "gntik-ai/falcone"
    finding = "KIT_GAP" if kit_gap else "BACKEND_GAP"
    return agent(gap_output(key, finding, repository))(payload)


def item_reviewer(revise_first: set[str]) -> Responder:
    seen: set[str] = set()

    def respond(payload: dict[str, Any]) -> dict[str, Any]:
        key = payload["item_key"]
        if key in revise_first and key not in seen:
            seen.add(key)
            verdict = {
                "decision": "REVISE",
                "summary": "Tighten a criterion.",
                "findings": [{"category": "testability", "detail": "Name the fixture.",
                              "reference": "acceptance_criteria"}],
            }  # fmt: skip
        else:
            verdict = {"decision": "APPROVE", "summary": "Matches the item.", "findings": []}
        return agent(verdict)(payload)

    return respond


def dedupe(payload: dict[str, Any]) -> dict[str, Any]:
    assert payload["target_repository"] == "gntik-ai/falcone"
    return {**payload, "dedupe": {"status": "CLEAR", "matches": []}}


def fanout_harness(tmp_path: Path) -> tuple[Harness, str, str]:
    harness = Harness(tmp_path)
    blueprint_id, digest = approved_blueprint(harness)
    harness.responses.update(
        {
            "project.snapshot": snapshot,
            "ui_fanout.gap_draft": gap_drafter,
            "change_request.review": item_reviewer(set()),
            "change_request.github_dedupe": dedupe,
        }
    )
    return harness, blueprint_id, digest


async def test_fanout_drafts_every_item_with_labels_and_dependencies(tmp_path: Path) -> None:
    harness, blueprint_id, digest = fanout_harness(tmp_path)
    harness.responses["change_request.review"] = item_reviewer(
        {"gap:gap-plans-api", "page:workspaces"}
    )
    result = await harness.run(
        UiBlueprintFanOutWorkflow,
        UiBlueprintFanOutInput(blueprint_id=blueprint_id),
        ui_fanout_workflow_id(blueprint_id, 1),
    )
    items = fanout_items(blueprint())
    assert (result["status"], result["drafted"], result["failed"]) == ("COMPLETED", 13, 0)
    assert result["pending"] == 0
    # Gaps are drafted by Claude (triage-ui-gap); the gap the review revised is redrafted once.
    assert harness.calls.count("ui_fanout.gap_draft") == 4
    gap_request = harness.payloads["ui_fanout.gap_draft"][0]["draft_request"]
    assert (gap_request["role"], gap_request["provider"]) == ("ui-gap-triager", "claude")
    # Epics, pages and the cutover are rendered deterministically and reviewed by Codex.
    assert harness.calls.count("change_request.review") == 14
    review_request = harness.payloads["change_request.review"][0]["review_request"]
    assert (review_request["provider"], review_request["role"]) == ("codex", "code-reviewer")
    # The gntik-ui gap is never searched by the Hermes GitHub worker.
    assert harness.calls.count("change_request.github_dedupe") == 12
    for item in items:
        view = harness.change_requests.get_sync(item_change_request_id(digest, item.key))
        assert view is not None, item.key
        assert view["status"] == "CANDIDATE"
        assert view["target_repository"] == item.target_repository
        assert view["ui_context"]["labels"] == list(item.labels)
        assert view["requested_by"] == "telegram:42"
        assert "## UI migration" in view["issue_body"]
    tenants = harness.change_requests.get_sync(item_change_request_id(digest, "page:tenants"))
    assert tenants is not None
    blocked = [entry["item_key"] for entry in tenants["ui_context"]["blocked_by"]]
    assert blocked == ["gap:gap-plans-api", "epic:u0-scaffold"]
    gap_id = str(item_change_request_id(digest, "gap:gap-plans-api"))
    assert f"change request `{gap_id}` (`gap:gap-plans-api`)" in tenants["issue_body"]
    workspaces = harness.change_requests.get_sync(item_change_request_id(digest, "page:workspaces"))
    assert workspaces is not None
    assert workspaces["review"]["rounds"] == 1
    assert workspaces["review_findings"][0]["detail"] == "Name the fixture."
    kit = harness.change_requests.get_sync(
        item_change_request_id(digest, "gap:gap-datatable-pagination")
    )
    assert kit is not None and kit["target_repository"] == "gntik-ai/gntik-ui"
    assert kit["publish_command"] is None
    messages = [item["message"] for item in harness.notifications()]
    assert any("UI fan-out done" in message and "13 drafts" in message for message in messages)
    view = harness.repository.get_sync(UUID(blueprint_id))
    assert view is not None and view["fanout"]["last_run"]["drafted"] == 13


async def test_fanout_is_capped_resumable_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("engineering_control.activities.ui_plan.FANOUT_BATCH", 2)
    harness, blueprint_id, digest = fanout_harness(tmp_path)
    first = await harness.run(
        UiBlueprintFanOutWorkflow,
        UiBlueprintFanOutInput(blueprint_id=blueprint_id, max_items=5),
        ui_fanout_workflow_id(blueprint_id, 1),
    )
    # Three batches (continue-as-new) stop at the cap of five items.
    assert (first["processed"], first["drafted"], first["pending"]) == (5, 5, 8)
    assert harness.calls.count("ui_fanout.plan") == 3
    ids = [item_change_request_id(digest, item.key) for item in fanout_items(blueprint())]
    assert len(harness.change_requests.item_states_sync(ids)) == 5
    second = await harness.run(
        UiBlueprintFanOutWorkflow,
        UiBlueprintFanOutInput(blueprint_id=blueprint_id, max_items=80),
        ui_fanout_workflow_id(blueprint_id, 2),
    )
    assert (second["processed"], second["drafted"], second["pending"]) == (8, 8, 0)
    states = harness.change_requests.item_states_sync(ids)
    assert len(states) == 13
    assert {state["status"] for state in states.values()} == {"CANDIDATE"}
    third = await harness.run(
        UiBlueprintFanOutWorkflow,
        UiBlueprintFanOutInput(blueprint_id=blueprint_id),
        ui_fanout_workflow_id(blueprint_id, 3),
    )
    assert (third["processed"], third["drafted"]) == (0, 0)
    with harness.change_requests.sessions() as session:
        assert len(session.scalars(select(models.ChangeRequest)).all()) == 13


async def test_fanout_of_an_unapproved_blueprint_is_blocked(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    created = harness.repository.create_sync("falcone", "cli:engineering-ctl", None)
    result = await harness.run(
        UiBlueprintFanOutWorkflow,
        UiBlueprintFanOutInput(blueprint_id=created["id"]),
        ui_fanout_workflow_id(created["id"], 1),
    )
    assert (result["status"], result["error_code"]) == ("BLOCKED", "UI_BLUEPRINT_NOT_APPROVED")
    assert provider_steps(harness.calls) == ["ui_fanout.plan", "ui_fanout.finalize"]
