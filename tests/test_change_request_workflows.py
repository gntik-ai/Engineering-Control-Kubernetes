"""NEW-108: change-request workflows on a real local Temporal server.

Controller steps run the real ChangeRequestActivityHandler against SQLite; snapshot,
provider and GitHub steps are scripted. Each recorded history is replayed.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from engineering_control.activities.change_requests import ChangeRequestActivityHandler
from engineering_control.activities.core import ControlActivities
from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db import models  # noqa: F401
from engineering_control.db.base import Base
from engineering_control.db.change_requests import ChangeRequestRepository
from engineering_control.engineering.change_requests import (
    change_request_workflow_id,
    prepare_intake,
)
from engineering_control.workflows.definitions import (
    ChangeRequestWorkflow,
)
from engineering_control.workflows.inputs import ChangeRequestInput
from engineering_control.workflows.worker_registry import (
    CONTROL_TASK_QUEUE,
    GITHUB_TASK_QUEUE,
    ROLE_TASK_QUEUES,
)

_TEMPORAL_CLI = shutil.which("temporal")
ROOT = Path(__file__).parents[1]
PROPOSAL: dict[str, Any] = {
    "title": "Add CSV export to the audit log",
    "kind": "feature",
    "why": "Operators need to share audit history offline.",
    "what_changes": ["Add a CSV export endpoint (src/audit/api.py:40)."],
    "out_of_scope": [],
    "affected_repositories": [{"repository": "gntik-ai/falcone", "role": "source"}],
    "acceptance_criteria": ["GET /audit/export.csv returns 200 with a header row"],
    "risks": [],
    "rollout": [],
    "kill_switch": ["Disable the audit export flag"],
    "open_questions": [],
    "proposed_risk": "R1",
    "risk_rationale": "A bounded read-only endpoint.",
}
REVISE = {
    "decision": "REVISE",
    "summary": "A criterion is not testable.",
    "findings": [{"category": "testability", "detail": "Define the header row.", "reference": ""}],
}
APPROVE = {"decision": "APPROVE", "summary": "Scope matches the request.", "findings": []}


@pytest.fixture
def repository(tmp_path: Path) -> ChangeRequestRepository:
    engine = create_engine(f"sqlite:///{tmp_path / 'workflow.sqlite'}")
    Base.metadata.create_all(engine)
    return ChangeRequestRepository(engine=engine)


def scripted_activity(
    handler: ChangeRequestActivityHandler,
    responses: dict[str, list[Callable[[dict[str, Any]], dict[str, Any]]]],
    calls: list[str],
    metadata: MetadataActivityHandler | None = None,
    persisted: list[dict[str, Any]] | None = None,
) -> Any:
    """Real controller steps (and, when given, the real provider telemetry handler);
    every other step pops its next scripted response."""

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        step = request["step"]
        payload = ControlActivities._attach_orchestration_context(request["payload"])
        calls.append(step)
        if step in handler.supported_steps:
            return await handler.handle(step, payload)
        if step in {"agent.record_start", "agent.persist_result"}:
            if step == "agent.persist_result" and persisted is not None:
                persisted.append(payload)
            if metadata is not None:
                return await metadata.handle(step, payload)
            return payload
        queue = responses.get(step)
        if not queue:
            raise AssertionError(f"unscripted step {step!r}")
        return queue.pop(0)(payload)

    return execute_control_step


def snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "status": "SNAPSHOT_VERIFIED", "source_worktree": "/srv/wt/falcone/x"}


def agent(structured: dict[str, Any]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """A complete provider result bound to the invocation's own normalized request."""

    def respond(payload: dict[str, Any]) -> dict[str, Any]:
        key = {"change_request.draft": "draft_request", "change_request.review": "review_request"}
        request = payload[key[payload["provider_step"]]]
        now = datetime.now(UTC).isoformat()
        result = {
            "run_id": request["run_id"],
            "provider": request["provider"],
            "role": request["role"],
            "status": "SUCCESS",
            "structured_result": structured,
            "started_at": now,
            "finished_at": now,
        }
        return {**payload, "status": "SUCCESS", "agent_result": result}

    return respond


def provider_unavailable(payload: dict[str, Any]) -> dict[str, Any]:
    raise ApplicationError(
        "codex needs manual authentication", type="MANUAL_AUTH_REQUIRED_CODEX", non_retryable=True
    )


def schema_violation(payload: dict[str, Any]) -> dict[str, Any]:
    raise ApplicationError(
        "provider result failed its immutable output schema",
        type="SchemaViolation",
        non_retryable=True,
    )


def dedupe(payload: dict[str, Any]) -> dict[str, Any]:
    match = {"number": 3, "title": "CSV export", "url": "https://x/3", "score": 0.5}
    return {**payload, "dedupe": {"status": "MATCHES", "matches": [match]}}


async def run_workflow(
    workflow_cls: type, argument: Any, activity_fn: Any, workflow_id: str
) -> tuple[Any, Any]:
    assert _TEMPORAL_CLI is not None
    queues = (
        CONTROL_TASK_QUEUE,
        GITHUB_TASK_QUEUE,
        ROLE_TASK_QUEUES["claude"],
        ROLE_TASK_QUEUES["codex"],
        ROLE_TASK_QUEUES["scan"],
    )
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=_TEMPORAL_CLI) as env:
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
                execution_timeout=timedelta(minutes=1),
            )
            result = await handle.result()
            history = await handle.fetch_history()
    await Replayer(workflows=[workflow_cls]).replay_workflow(history)
    return result, history


def new_request(repository: ChangeRequestRepository) -> UUID:
    created = repository.create_sync(
        prepare_intake("falcone", "Add a CSV export of the tenant audit log for operators."),
        "telegram:42",
    )
    return UUID(created["id"])


pytestmark_temporal = pytest.mark.skipif(
    _TEMPORAL_CLI is None,
    reason="requires the `temporal` CLI on PATH to run a local dev server",
)


@pytestmark_temporal
async def test_draft_review_approve_dedupe_persist_reaches_candidate(
    repository: ChangeRequestRepository,
) -> None:
    change_request_id = new_request(repository)
    handler = ChangeRequestActivityHandler(Settings(), repository=repository)
    calls: list[str] = []
    activity_fn = scripted_activity(
        handler,
        {
            "project.snapshot": [snapshot],
            "change_request.draft": [agent(PROPOSAL)],
            "change_request.review": [agent(APPROVE)],
            "change_request.github_dedupe": [dedupe],
        },
        calls,
    )
    result, _ = await run_workflow(
        ChangeRequestWorkflow,
        ChangeRequestInput(change_request_id=str(change_request_id), revision=1),
        activity_fn,
        change_request_workflow_id(str(change_request_id), 1),
    )
    assert result["status"] == "CANDIDATE"
    assert result["review_decision"] == "APPROVE"
    assert [step for step in calls if not step.startswith("agent.")] == [
        "change_request.load",
        "project.snapshot",
        "change_request.prepare_draft",
        "change_request.draft",
        "change_request.prepare_review",
        "change_request.review",
        "change_request.record_review",
        "change_request.github_dedupe",
        "change_request.persist",
        "change_request.finalize_run",
    ]
    view = repository.get_sync(change_request_id)
    assert view is not None
    assert view["status"] == "CANDIDATE"
    assert view["approval_binding"] == result["approval_binding"]
    assert view["fingerprint"].startswith("sha256:")
    assert view["review_findings"] == []
    assert view["dedupe_warnings"][0]["number"] == 3
    assert view["issue_body"].startswith("kind: feature\nRisk: R1\n")


@pytestmark_temporal
async def test_unresolved_review_redrafts_once_and_persists_findings_as_candidate(
    repository: ChangeRequestRepository,
) -> None:
    change_request_id = new_request(repository)
    handler = ChangeRequestActivityHandler(Settings(), repository=repository)
    calls: list[str] = []
    prompts: list[str] = []

    def redraft(payload: dict[str, Any]) -> dict[str, Any]:
        prompts.append(str(payload["draft_request"]["prompt"]))
        return agent({**PROPOSAL, "acceptance_criteria": ["The CSV has a header row"]})(payload)

    activity_fn = scripted_activity(
        handler,
        {
            "project.snapshot": [snapshot],
            "change_request.draft": [agent(PROPOSAL), redraft],
            "change_request.review": [agent(REVISE), agent(REVISE)],
            "change_request.github_dedupe": [dedupe],
        },
        calls,
    )
    result, _ = await run_workflow(
        ChangeRequestWorkflow,
        ChangeRequestInput(change_request_id=str(change_request_id), revision=1),
        activity_fn,
        change_request_workflow_id(str(change_request_id), 1),
    )
    assert result["status"] == "CANDIDATE"
    assert result["review_decision"] == "REVISE"
    assert calls.count("change_request.draft") == 2
    assert calls.count("change_request.review") == 2
    assert "Define the header row." in prompts[0]
    view = repository.get_sync(change_request_id)
    assert view is not None
    assert view["status"] == "CANDIDATE"
    assert view["review"]["rounds"] == 2
    assert view["review_findings"] == REVISE["findings"]
    assert view["proposal"]["acceptance_criteria"] == ["The CSV has a header row"]
    assert "- Unresolved findings:" in view["issue_body"]


@pytestmark_temporal
async def test_draft_schema_failure_marks_the_request_failed_with_a_safe_code(
    repository: ChangeRequestRepository,
) -> None:
    change_request_id = new_request(repository)
    handler = ChangeRequestActivityHandler(Settings(), repository=repository)
    calls: list[str] = []
    activity_fn = scripted_activity(
        handler,
        {"project.snapshot": [snapshot], "change_request.draft": [schema_violation]},
        calls,
    )
    # The real _agent_step records the provider failure through agent.persist_result.
    result, _ = await run_workflow(
        ChangeRequestWorkflow,
        ChangeRequestInput(change_request_id=str(change_request_id), revision=1),
        activity_fn,
        change_request_workflow_id(str(change_request_id), 1),
    )
    assert result["status"] == "FAILED"
    assert result["error_code"] == "DRAFT_SCHEMA_INVALID"
    assert "change_request.review" not in calls
    view = repository.get_sync(change_request_id)
    assert view is not None and view["status"] == "FAILED"
    assert view["error_code"] == "DRAFT_SCHEMA_INVALID"


@pytestmark_temporal
async def test_invalid_draft_repository_fails_closed_before_review(
    repository: ChangeRequestRepository,
) -> None:
    change_request_id = new_request(repository)
    handler = ChangeRequestActivityHandler(Settings(), repository=repository)
    calls: list[str] = []
    outside = {
        **PROPOSAL,
        "affected_repositories": [{"repository": "gntik-ai/musematic", "role": "source"}],
    }
    activity_fn = scripted_activity(
        handler,
        {"project.snapshot": [snapshot], "change_request.draft": [agent(outside)]},
        calls,
    )
    result, _ = await run_workflow(
        ChangeRequestWorkflow,
        ChangeRequestInput(change_request_id=str(change_request_id), revision=1),
        activity_fn,
        change_request_workflow_id(str(change_request_id), 1),
    )
    assert result["error_code"] == "DRAFT_SCHEMA_INVALID"
    assert "change_request.review" not in calls


def test_migration_0014_creates_exactly_the_model_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'migration.sqlite3'}"
    monkeypatch.setenv("ENGINEERING_DATABASE_URL", database_url)
    config = Config(ROOT / "alembic.ini")
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")
    command.downgrade(config, "0013")
    engine = create_engine(database_url)
    assert "change_requests" not in inspect(engine).get_table_names()
    command.upgrade(config, "0014")  # the explicit (non-bootstrap) migration path
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("change_requests")}
    assert columns == set(Base.metadata.tables["change_requests"].columns.keys())
    indexes = {
        index["name"]: bool(index["unique"]) for index in inspector.get_indexes("change_requests")
    }
    assert indexes["ix_change_requests_request_key"] is True
    repository = ChangeRequestRepository(engine=engine)
    created = new_request(repository)
    assert repository.get_sync(created) is not None
    engine.dispose()
    assert uuid.UUID(str(created))


def control_runs(repository: ChangeRequestRepository, workflow_id: str) -> list[Any]:
    from sqlalchemy import select

    from engineering_control.db.models import AgentInvocation, Run

    with repository.sessions() as session:
        runs = session.scalars(select(Run).where(Run.workflow_id == workflow_id)).all()
        invocations = session.scalars(
            select(AgentInvocation).where(AgentInvocation.run_id.in_([run.id for run in runs]))
        ).all()
        return [
            (run.status, run.finished_at is not None, run.error_code, run.run_type) for run in runs
        ] + sorted((item.role, item.status) for item in invocations)


@pytestmark_temporal
async def test_a_candidate_closes_its_control_run_and_invocations(
    repository: ChangeRequestRepository,
) -> None:
    change_request_id = new_request(repository)
    handler = ChangeRequestActivityHandler(Settings(), repository=repository)
    metadata = MetadataActivityHandler(Settings(database_url=str(repository.engine.url)))
    activity_fn = scripted_activity(
        handler,
        {
            "project.snapshot": [snapshot],
            "change_request.draft": [agent(PROPOSAL)],
            "change_request.review": [agent(APPROVE)],
            "change_request.github_dedupe": [dedupe],
        },
        [],
        metadata,
    )
    workflow_id = change_request_workflow_id(str(change_request_id), 1)
    result, _ = await run_workflow(
        ChangeRequestWorkflow,
        ChangeRequestInput(change_request_id=str(change_request_id), revision=1),
        activity_fn,
        workflow_id,
    )
    assert result["status"] == "CANDIDATE"
    assert control_runs(repository, workflow_id) == [
        ("SUCCESS", True, None, "change-request"),
        ("code-reviewer", "SUCCESS"),
        ("product-spec-architect", "SUCCESS"),
    ]


@pytestmark_temporal
async def test_a_review_provider_failure_is_recorded_as_its_own_and_closes_the_run(
    repository: ChangeRequestRepository,
) -> None:
    change_request_id = new_request(repository)
    handler = ChangeRequestActivityHandler(Settings(), repository=repository)
    metadata = MetadataActivityHandler(Settings(database_url=str(repository.engine.url)))
    persisted: list[dict[str, Any]] = []
    activity_fn = scripted_activity(
        handler,
        {
            "project.snapshot": [snapshot],
            "change_request.draft": [agent(PROPOSAL)],
            "change_request.review": [provider_unavailable],
        },
        [],
        metadata,
        persisted,
    )
    workflow_id = change_request_workflow_id(str(change_request_id), 1)
    result, _ = await run_workflow(
        ChangeRequestWorkflow,
        ChangeRequestInput(change_request_id=str(change_request_id), revision=1),
        activity_fn,
        workflow_id,
    )
    assert result["status"] == "FAILED"
    assert result["error_code"] == "REVIEW_PROVIDER_UNAVAILABLE"
    # The Codex failure was persisted without Claude's earlier result.
    assert "agent_result" not in persisted[-1]
    assert persisted[-1]["blocker"] == "MANUAL_AUTH_REQUIRED_CODEX"
    assert control_runs(repository, workflow_id) == [
        ("FAILED", True, "REVIEW_PROVIDER_UNAVAILABLE", "change-request"),
        ("code-reviewer", "FAILED"),
        ("product-spec-architect", "SUCCESS"),
    ]
    view = repository.get_sync(change_request_id)
    assert view is not None and view["error_code"] == "REVIEW_PROVIDER_UNAVAILABLE"


def test_each_provider_invocation_starts_without_earlier_results() -> None:
    from unittest.mock import Mock

    handler = ChangeRequestActivityHandler(Settings(), repository=Mock())
    stale = {
        "agent_result": {"structured_result": PROPOSAL},
        "agent_invocation_id": "x",
        "agent_invocation_status": "SUCCESS",
        "instruction_bundle": {"sha256": "a" * 64},
        "observability": {"execution_id": "x"},
        "handoff_hashes": {},
    }
    base = {
        "change_request_id": str(uuid.uuid4()),
        "revision": 1,
        "project": "falcone",
        "target_repository": "gntik-ai/falcone",
        "source_worktree": "/srv/wt/falcone/x",
        "description_redacted": "Add a CSV export of the tenant audit log for operators.",
        "review_rounds": [REVISE],
        "proposal": PROPOSAL,
    }
    review = handler._prepare_review({**base, **stale, "draft_attempt": 1})
    redraft = handler._prepare_draft({**base, **stale, "draft_attempt": 2})
    assert review["status"] == "REVIEW_READY" and redraft["status"] == "DRAFT_READY"
    for prepared in (review, redraft):
        assert not set(stale) & set(prepared)
