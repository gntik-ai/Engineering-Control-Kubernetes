from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.api.main import create_app as create_api_app
from engineering_control.config import Settings
from engineering_control.dashboard.events import transcript_sse
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.base import Base
from engineering_control.db.models import AgentExecution, AgentLogChunk, DashboardEvent, Run
from engineering_control.models.core import AgentRequest, AgentResult
from engineering_control.models.enums import Provider, RunStatus
from engineering_control.observability.agent import (
    AgentObservabilityRepository,
    ExecutionFinishInput,
    ExecutionHeartbeatInput,
    ExecutionStartInput,
    GatewayEventInput,
    LogBatchInput,
    LogChunkInput,
    TranscriptNormalizer,
    normalize_agent_role,
)


def _repositories(
    tmp_path: Path, *, maximum_bytes: int = 5 * 1024 * 1024
) -> tuple[AgentObservabilityRepository, DashboardRepository, str]:
    url = f"sqlite+pysqlite:///{tmp_path / 'observability.db'}"
    agent = AgentObservabilityRepository(url, maximum_transcript_bytes=maximum_bytes)
    dashboard = DashboardRepository(url)
    Base.metadata.create_all(agent.engine)
    return agent, dashboard, url


def _register(
    repository: AgentObservabilityRepository,
    *,
    provider: str = "codex",
    role: str = "maker",
) -> object:
    execution_id = uuid4()
    with repository.sessions.begin() as session:
        session.add(
            AgentExecution(
                id=execution_id,
                workflow_id="workflow-safe",
                project="falcone",
                provider=provider,
                role=role,
                status="QUEUED",
                attempt=1,
                max_attempts=2,
                output_truncated=False,
                transcript_incomplete=False,
                total_log_bytes=0,
                redaction_count=0,
            )
        )
    return execution_id


def _start(provider: str, role: str = "maker") -> ExecutionStartInput:
    return ExecutionStartInput(
        workflow_id="workflow-safe",
        temporal_run_id="temporal-run",
        activity_id="activity-1",
        provider=provider,  # type: ignore[arg-type]
        role=role,
        host="worker.example",
        worker_kind=provider,  # type: ignore[arg-type]
        task_queue=f"engineering-{provider}",
        phase="maker" if role == "maker" else "checker",
        selection_reason=f"routing selected {provider}",
        cli_path_identifier=provider,  # type: ignore[arg-type]
        cli_version="1.2.3",
        attempt=1,
        max_attempts=2,
        started_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "role"), [("codex", "maker"), ("claude", "checker")])
async def test_execution_registration_heartbeat_and_closeout(
    tmp_path: Path, provider: str, role: str
) -> None:
    repository, _, _ = _repositories(tmp_path)
    execution_id = _register(repository, provider=provider, role=role)
    await repository.start(f"{provider}-runner", execution_id, _start(provider, role))
    heartbeat = datetime.now(UTC) + timedelta(seconds=10)
    await repository.heartbeat(
        f"{provider}-runner",
        execution_id,
        ExecutionHeartbeatInput(observed_at=heartbeat, phase="testing"),
    )
    await repository.finish(
        f"{provider}-runner",
        execution_id,
        ExecutionFinishInput(
            status="SUCCESS",
            phase="testing",
            ended_at=heartbeat,
            exit_code=0,
        ),
    )
    with repository.sessions() as session:
        row = session.get(AgentExecution, execution_id)
        assert row is not None
        assert (row.provider, row.role, row.host, row.task_queue) == (
            provider,
            role,
            "worker.example",
            f"engineering-{provider}",
        )
        assert row.status == "SUCCESS"
        assert row.last_heartbeat_at is not None
        assert row.ended_at is not None
        events = list(session.query(DashboardEvent).order_by(DashboardEvent.id))
        assert [event.event_type for event in events] == [
            "provider.execution_started",
            "provider.execution_completed",
        ]


@pytest.mark.asyncio
async def test_transcript_redacted_before_db_ordered_resumable_and_idempotent(
    tmp_path: Path,
) -> None:
    repository, dashboard, _ = _repositories(tmp_path)
    execution_id = _register(repository)
    await repository.start("codex-runner", execution_id, _start("codex"))
    secret = "Bearer " + bytes(range(65, 91)).decode() + "123456789"
    values = LogBatchInput(
        chunks=[
            LogChunkInput(
                sequence=1,
                stream="assistant",
                created_at=datetime.now(UTC),
                redacted_text=f"visible {secret}",
                byte_count=len(secret),
            ),
            LogChunkInput(
                sequence=2,
                stream="test",
                created_at=datetime.now(UTC),
                redacted_text="pytest completed",
                byte_count=16,
            ),
        ]
    )
    first = await repository.append_logs("codex-runner", execution_id, values)
    second = await repository.append_logs("codex-runner", execution_id, values)
    assert first["inserted"] == 2
    assert second["inserted"] == 0
    with repository.sessions() as session:
        chunks = list(session.query(AgentLogChunk).order_by(AgentLogChunk.sequence))
        assert [chunk.sequence for chunk in chunks] == [1, 2]
        assert "not-a-real-token" not in chunks[0].redacted_text
        assert "[REDACTED]" in chunks[0].redacted_text

    async def connected() -> bool:
        return False

    frames = [
        frame
        async for frame in transcript_sse(
            dashboard.observability,
            execution_id,  # type: ignore[arg-type]
            after_sequence=1,
            disconnected=connected,
            poll_seconds=0,
            heartbeat_seconds=0,
            maximum_iterations=2,
        )
    ]
    assert any("id: 2" in frame and "pytest completed" in frame for frame in frames)
    assert all(secret not in frame for frame in frames)
    assert any("heartbeat" in frame for frame in frames)


@pytest.mark.asyncio
async def test_transcript_limit_marks_once_without_failing_execution(tmp_path: Path) -> None:
    repository, _, _ = _repositories(tmp_path, maximum_bytes=12)
    execution_id = _register(repository)
    await repository.start("codex-runner", execution_id, _start("codex"))
    batch = LogBatchInput(
        chunks=[
            LogChunkInput(
                sequence=1,
                stream="assistant",
                created_at=datetime.now(UTC),
                redacted_text="this visible line is over the configured cap",
                byte_count=44,
            )
        ]
    )
    assert (await repository.append_logs("codex-runner", execution_id, batch))["inserted"] == 0
    assert (await repository.append_logs("codex-runner", execution_id, batch))["inserted"] == 0
    await repository.finish(
        "codex-runner",
        execution_id,
        ExecutionFinishInput(status="SUCCESS", ended_at=datetime.now(UTC), exit_code=0),
    )
    with repository.sessions() as session:
        row = session.get(AgentExecution, execution_id)
        assert row is not None and row.output_truncated is True and row.status == "SUCCESS"
        truncations = (
            session.query(DashboardEvent)
            .filter_by(event_type="provider.transcript_truncated")
            .count()
        )
        assert truncations == 1


def test_normalizer_excludes_reasoning_and_unrestricted_tool_results() -> None:
    normalizer = TranscriptNormalizer(maximum_line_bytes=100)
    assert normalizer.normalize("codex", {"type": "reasoning", "text": "hidden"}) == []
    assert normalizer.normalize("claude", {"type": "thinking", "text": "hidden"}) == []
    tool = normalizer.normalize(
        "claude",
        {"type": "tool_result", "content": "unrestricted source file contents"},
    )
    assert [line.text for line in tool] == ["Tool result: completed"]
    sha = "a" * 40
    visible = normalizer.normalize("claude", {"type": "result", "result": f"commit {sha}"})
    assert sha in visible[0].text


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("implementer", "maker"),
        ("code-reviewer", "checker"),
        ("domain-auditor", "auditor"),
        ("release-plan-reviewer", "reviewer"),
        ("runtime-verifier", "verifier"),
    ],
)
def test_provider_roles_are_canonical(raw: str, normalized: str) -> None:
    assert normalize_agent_role(raw) == normalized


def test_canary_controller_validates_worker_workspace_identity_without_mount(
    tmp_path: Path,
) -> None:
    worktree_root = tmp_path / "worker-only-worktrees"
    workspace_id = uuid4()
    expected = worktree_root / "observability-canary" / str(workspace_id)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'canary-controller.db'}",
        worktree_root=worktree_root,
    )
    metadata = MetadataActivityHandler(settings)
    assert not expected.exists()
    assert (
        metadata._observability_workspace(
            {"workspace_id": str(workspace_id), "workspace": str(expected)}
        )
        == expected
    )
    with pytest.raises(ValueError, match="workspace identity"):
        metadata._observability_workspace(
            {
                "workspace_id": str(workspace_id),
                "workspace": str(worktree_root / "other" / str(workspace_id)),
            }
        )


@pytest.mark.asyncio
async def test_native_canary_registers_queue_and_prepares_read_only_independent_providers(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canary.db"
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    settings = Settings(
        database_url=f"sqlite:///{database}",
        worktree_root=worktree_root,
    )
    metadata = MetadataActivityHandler(settings)
    Base.metadata.create_all(metadata.engine)
    scan = ScanActivityHandler(settings)
    workspace_id = uuid4()
    orchestration = {
        "workflow_id": f"observability-acceptance-{workspace_id}",
        "workflow_run_id": "temporal-run",
        "activity_id": "activity-1",
        "attempt": 1,
        "task_queue": "engineering-control",
    }
    payload: dict[str, Any] = {
        "workspace_id": str(workspace_id),
        "orchestration": orchestration,
    }

    registered = await metadata.handle("observability.register", payload)
    prepared = await scan.handle("observability.prepare_workspace", registered)
    codex = await metadata.handle("observability.prepare_codex", prepared)
    codex_request = AgentRequest.model_validate(codex["maker_request"])

    assert codex_request.provider is Provider.CODEX
    assert codex_request.role == "implementer"
    assert codex_request.writable is False
    assert codex_request.skip_git_repository_check is True
    assert codex_request.allowed_tools == ("Read", "Glob", "Grep")
    assert codex_request.output_schema["properties"]["decision"] == {
        "type": "string",
        "const": "PASS",
    }
    assert codex_request.output_schema["properties"]["provider"] == {
        "type": "string",
        "const": "codex",
    }
    workspace = Path(codex_request.working_directory)
    assert await asyncio.to_thread(workspace.is_dir)
    metadata_bits = await asyncio.to_thread(workspace.stat)
    assert metadata_bits.st_mode & 0o777 == 0o755

    now = datetime.now(UTC)
    codex_result = AgentResult(
        run_id=codex_request.run_id,
        provider=Provider.CODEX,
        role=codex_request.role,
        status=RunStatus.SUCCESS,
        summary="visible output streamed",
        structured_result={
            "decision": "PASS",
            "provider": "codex",
            "summary": "visible output streamed",
        },
        started_at=now,
        finished_at=now,
        exit_code=0,
    )
    claude = await metadata.handle(
        "observability.prepare_claude",
        {**codex, "agent_result": codex_result.model_dump(mode="json")},
    )
    claude_request = AgentRequest.model_validate(claude["checker_request"])
    assert claude_request.provider is Provider.CLAUDE
    assert claude_request.role == "code-reviewer"
    assert claude_request.writable is False
    assert claude_request.skip_git_repository_check is False
    assert claude_request.allowed_tools == ("Read", "Glob", "Grep")
    assert claude_request.run_id != codex_request.run_id

    with metadata.sessions() as session:
        run = session.scalar(
            session.query(Run).filter_by(workflow_id=orchestration["workflow_id"]).statement
        )
        assert run is not None and run.status == "QUEUED"
        events = session.query(DashboardEvent).filter_by(event_type="workflow.queued").all()
        assert len(events) == 1

    cleaned = await scan.handle("observability.cleanup_workspace", claude)
    assert cleaned["workspace_cleanup_status"] == "COMPLETE"
    assert not await asyncio.to_thread(workspace.exists)
    assert await asyncio.to_thread(workspace.parent.is_dir)
    assert not list(workspace.parent.iterdir())


def test_reverse_canary_preparation_steps_are_registered_for_dispatch() -> None:
    assert {
        "observability.prepare_claude_maker",
        "observability.prepare_codex_checker",
    } <= MetadataActivityHandler.supported_steps


@pytest.mark.asyncio
async def test_reverse_native_canary_uses_claude_maker_and_codex_checker(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'reverse-canary.db'}",
        worktree_root=tmp_path / "worktrees",
    )
    settings.worktree_root.mkdir()
    metadata = MetadataActivityHandler(settings)
    Base.metadata.create_all(metadata.engine)
    scan = ScanActivityHandler(settings)
    workspace_id = uuid4()
    payload: dict[str, Any] = {
        "workspace_id": str(workspace_id),
        "provider_direction": "claude-to-codex",
        "orchestration": {
            "workflow_id": f"reverse-observability-acceptance-{workspace_id}",
            "workflow_run_id": "reverse-temporal-run",
            "activity_id": "reverse-activity",
            "attempt": 1,
            "task_queue": "engineering-control",
        },
    }
    registered = await metadata.handle("observability.register", payload)
    prepared = await scan.handle("observability.prepare_workspace", registered)
    claude = await metadata.handle("observability.prepare_claude_maker", prepared)
    claude_request = AgentRequest.model_validate(claude["maker_request"])
    assert claude_request.provider is Provider.CLAUDE
    assert claude_request.role == "implementer"
    assert claude_request.writable is False

    now = datetime.now(UTC)
    claude_result = AgentResult(
        run_id=claude_request.run_id,
        provider=Provider.CLAUDE,
        role=claude_request.role,
        status=RunStatus.SUCCESS,
        summary="bounded maker result",
        structured_result={
            "decision": "PASS",
            "provider": "claude",
            "summary": "bounded maker result",
        },
        started_at=now,
        finished_at=now,
        exit_code=0,
    )
    codex = await metadata.handle(
        "observability.prepare_codex_checker",
        {**claude, "agent_result": claude_result.model_dump(mode="json")},
    )
    codex_request = AgentRequest.model_validate(codex["checker_request"])
    assert codex_request.provider is Provider.CODEX
    assert codex_request.role == "code-reviewer"
    assert codex_request.run_id != claude_request.run_id

    codex_result = AgentResult(
        run_id=codex_request.run_id,
        provider=Provider.CODEX,
        role=codex_request.role,
        status=RunStatus.SUCCESS,
        summary="bounded checker result",
        structured_result={
            "decision": "PASS",
            "provider": "codex",
            "summary": "bounded checker result",
        },
        started_at=now,
        finished_at=now,
        exit_code=0,
    )
    finalized = await metadata.handle(
        "observability.finalize",
        {
            **codex,
            "agent_result": codex_result.model_dump(mode="json"),
            "terminal_status": "COMPLETED",
        },
    )
    assert finalized["status"] == "COMPLETED"
    await scan.handle("observability.cleanup_workspace", finalized)


@pytest.mark.asyncio
async def test_gateway_event_persists_only_safe_bounded_arguments(tmp_path: Path) -> None:
    repository, _, _ = _repositories(tmp_path)
    await repository.gateway_event(
        "hermes-gateway",
        GatewayEventInput(
            event_type="engineering.workflow_returned",
            command="loop",
            project="musematic",
            issue_number=1119,
            mode="full-delivery",
            workflow_id="workflow-1119",
            outcome="COMPLETED",
        ),
    )
    with repository.sessions() as session:
        event = session.query(DashboardEvent).one()
        serialized = str(event.metadata_json)
        assert event.source == "hermes"
        assert event.issue_number == 1119
        assert set(event.metadata_json) == {"command", "mode", "outcome", "gateway"}
        assert "user" not in serialized.lower()
        assert "token" not in serialized.lower()


@pytest.mark.asyncio
async def test_gateway_event_rejects_non_gateway_identity(tmp_path: Path) -> None:
    repository, _, _ = _repositories(tmp_path)
    with pytest.raises(ValueError, match="not authorized"):
        await repository.gateway_event(
            "api-client",
            GatewayEventInput(
                event_type="telegram.command_received",
                command="loop",
            ),
        )


def test_private_control_api_accepts_safe_gateway_event_only_when_authenticated(
    tmp_path: Path,
) -> None:
    repository, _, _ = _repositories(tmp_path)
    token = bytes(range(65, 91)).decode() + "12345678"
    app = create_api_app(
        service=cast(Any, object()),
        token=token,
        gateway_repository=repository,
    )
    payload = {
        "event_type": "telegram.command_recognized",
        "command": "status",
        "outcome": "RECOGNIZED",
    }
    with TestClient(app) as client:
        assert client.post("/v1/observability/gateway-events", json=payload).status_code == 401
        forbidden = client.post(
            "/v1/observability/gateway-events",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Engineering-Actor": "api-client",
            },
        )
        response = client.post(
            "/v1/observability/gateway-events",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Engineering-Actor": "hermes-gateway",
            },
        )
        assert forbidden.status_code == 403
        assert response.status_code == 202
    with repository.sessions() as session:
        assert session.query(DashboardEvent).one().event_type == "telegram.command_recognized"


@pytest.mark.asyncio
async def test_worker_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    repository, _, _ = _repositories(tmp_path)
    execution_id = _register(repository, provider="claude", role="checker")
    with pytest.raises(ValueError, match="identity"):
        await repository.start("codex-runner", execution_id, _start("claude", "checker"))
