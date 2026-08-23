from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError

from engineering_control.activities.provider import ProviderActivityHandler
from engineering_control.config import Settings
from engineering_control.models.core import AgentRequest, AgentResult
from engineering_control.models.enums import ModelClass, Project, Provider, RunStatus


class FakeAdapter:
    provider = Provider.CODEX

    def __init__(self, status: RunStatus = RunStatus.SUCCESS, authenticated: bool = True) -> None:
        self.status = status
        self.is_authenticated = authenticated

    async def authenticated(self) -> bool:
        return self.is_authenticated

    async def execute(self, request: AgentRequest, on_event: Any = None) -> AgentResult:
        if on_event is not None:
            await on_event({"type": "item.completed", "secret": "safe-metadata"})
        now = datetime.now(UTC)
        return AgentResult(
            run_id=request.run_id,
            provider=request.provider,
            role=request.role,
            status=self.status,
            summary="bounded result",
            structured_result={"summary": "ok"} if self.status is RunStatus.SUCCESS else {},
            started_at=now,
            finished_at=now,
            error_code="PROVIDER_RATE_LIMIT" if self.status is RunStatus.RATE_LIMITED else None,
        )


def request_for(worktree: Path, *, writable: bool = True) -> AgentRequest:
    return AgentRequest(
        project=Project.FALCONE,
        role="implementer",
        provider=Provider.CODEX,
        model_class=ModelClass.WORKER,
        prompt="make one bounded synthetic change",
        working_directory=str(worktree),
        writable=writable,
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["summary"],
            "properties": {"summary": {"type": "string"}},
        },
    )


@pytest.mark.asyncio
async def test_provider_activity_executes_only_in_bounded_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "engineering_control.activities.provider.activity.heartbeat", lambda _: None
    )
    worktree_root = tmp_path / "worktrees"
    worktree = worktree_root / "falcone" / "change-set"
    worktree.mkdir(parents=True)
    handler = ProviderActivityHandler(
        Settings(worktree_root=worktree_root, run_root=tmp_path / "runs"),
        "codex",
        FakeAdapter(),
    )

    result = await handler.handle(
        "delivery.invoke_maker",
        {"maker_request": request_for(worktree).model_dump(mode="json")},
    )

    assert result["status"] == "SUCCESS"
    assert result["provider"] == "codex"
    assert result["agent_result"]["structured_result"] == {"summary": "ok"}


@pytest.mark.asyncio
async def test_provider_activity_heartbeats_while_cli_emits_no_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeats: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "engineering_control.activities.provider.activity.heartbeat", heartbeats.append
    )

    class QuietAdapter(FakeAdapter):
        async def execute(self, request: AgentRequest, on_event: Any = None) -> AgentResult:
            await asyncio.sleep(0.03)
            now = datetime.now(UTC)
            return AgentResult(
                run_id=request.run_id,
                provider=request.provider,
                role=request.role,
                status=RunStatus.SUCCESS,
                summary="bounded result",
                structured_result={"summary": "ok"},
                started_at=now,
                finished_at=now,
            )

    worktree_root = tmp_path / "worktrees"
    worktree = worktree_root / "falcone" / "change-set"
    worktree.mkdir(parents=True)
    handler = ProviderActivityHandler(
        Settings(worktree_root=worktree_root, run_root=tmp_path / "runs"),
        "codex",
        QuietAdapter(),
    )
    handler._heartbeat_interval_seconds = 0.005

    result = await handler.handle(
        "delivery.invoke_maker",
        {"maker_request": request_for(worktree).model_dump(mode="json")},
    )

    assert result["status"] == "SUCCESS"
    keepalives = [item for item in heartbeats if item["event_type"] == "provider.keepalive"]
    assert len(keepalives) >= 2
    assert all(item["provider"] == "codex" for item in keepalives)


@pytest.mark.asyncio
async def test_provider_activity_rejects_worktree_escape(tmp_path: Path) -> None:
    allowed = tmp_path / "worktrees"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    handler = ProviderActivityHandler(
        Settings(worktree_root=allowed, run_root=tmp_path / "runs"),
        "codex",
        FakeAdapter(),
    )

    with pytest.raises(ApplicationError, match="bounded worktree"):
        await handler.handle(
            "delivery.invoke_maker",
            {"maker_request": request_for(outside).model_dump(mode="json")},
        )


@pytest.mark.asyncio
async def test_provider_activity_persists_rate_limit_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "engineering_control.activities.provider.activity.heartbeat", lambda _: None
    )
    worktree_root = tmp_path / "worktrees"
    worktree = worktree_root / "falcone" / "change-set"
    worktree.mkdir(parents=True)
    run_root = tmp_path / "runs"
    request = request_for(worktree)
    handler = ProviderActivityHandler(
        Settings(worktree_root=worktree_root, run_root=run_root),
        "codex",
        FakeAdapter(RunStatus.RATE_LIMITED),
    )

    result = await handler.handle(
        "delivery.invoke_maker",
        {"maker_request": request.model_dump(mode="json"), "decisions": ["bounded scope"]},
    )

    assert result["status"] == "RATE_LIMITED"
    assert set(result["handoff_hashes"]) == {
        "TASK.md",
        "CONTEXT.json",
        "DECISIONS.md",
        "SOURCE-REFS.json",
        "DIFF.patch",
        "TEST-RESULTS.json",
        "OPEN-QUESTIONS.md",
    }
    assert (run_root / "codex" / str(request.run_id) / "handoff" / "TASK.md").is_file()


@pytest.mark.asyncio
async def test_provider_activity_reports_manual_auth_requirement(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    worktree = worktree_root / "falcone" / "change-set"
    worktree.mkdir(parents=True)
    handler = ProviderActivityHandler(
        Settings(worktree_root=worktree_root, run_root=tmp_path / "runs"),
        "codex",
        FakeAdapter(authenticated=False),
    )

    result = await handler.handle(
        "delivery.invoke_maker",
        {"maker_request": request_for(worktree).model_dump(mode="json")},
    )

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "MANUAL_AUTH_REQUIRED_CODEX"
    assert result["provider"] == "codex"
    assert "agent_result" not in result
