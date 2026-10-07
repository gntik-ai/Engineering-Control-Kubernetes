from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError

from engineering_control.activities.provider import ProviderActivityHandler
from engineering_control.adapters.auth_probe import AuthProbeError
from engineering_control.agents.instructions import InstructionBundle
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


class FakeInstructionPack:
    def enrich(self, request: AgentRequest, _step: str) -> tuple[AgentRequest, InstructionBundle]:
        digest = "a" * 64
        source = "roles/implementer.yaml"
        tools = ("Read", "Glob", "Grep", "Edit", "Write")
        enriched = AgentRequest.model_validate(
            {
                **request.model_dump(mode="python"),
                "prompt": f"canonical instructions\n\n{request.prompt}",
                "allowed_tools": tools,
                "instruction_bundle_sha256": digest,
                "instruction_bundle_sources": [source],
            }
        )
        return enriched, InstructionBundle(
            prompt_prefix="canonical instructions",
            sha256=digest,
            revision=None,
            sources=(source,),
            allowed_tools=tools,
        )


class InvalidInstructionPack:
    def enrich(self, _request: AgentRequest, _step: str) -> tuple[AgentRequest, InstructionBundle]:
        raise ValueError("unsafe agent pack")


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
        FakeInstructionPack(),
    )

    result = await handler.handle(
        "delivery.invoke_maker",
        {"maker_request": request_for(worktree).model_dump(mode="json")},
    )

    assert result["status"] == "SUCCESS"
    assert result["observability"]["transcript_incomplete"] is False
    assert result["provider"] == "codex"
    assert result["agent_result"]["structured_result"] == {"summary": "ok"}
    assert result["instruction_bundle"]["sha256"] == "a" * 64


@pytest.mark.asyncio
async def test_provider_activity_does_not_echo_prompt_text_back_into_its_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-14: the rendered prompt (up to 200_000 chars) must not be echoed back into the
    activity's own result -- nothing reads it from there, and it dominates Temporal
    history-accretion cost for every maker/checker/agent invocation."""
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
        FakeInstructionPack(),
    )
    original_prompt = "make one bounded synthetic change" * 100

    result = await handler.handle(
        "delivery.invoke_maker",
        {
            "maker_request": request_for(worktree).model_dump(mode="json")
            | {"prompt": original_prompt}
        },
    )

    assert result["status"] == "SUCCESS"
    # Redacted to a short placeholder, not removed entirely (regression test,
    # 2026-09-22): metadata.py's _agent_request() re-validates this exact
    # payload shape via AgentRequest.model_validate() when persisting the
    # agent result, and prompt is a required field on that model -- fully
    # dropping the key broke that reconstruction on the very next real
    # delivery attempt in production.
    assert result["maker_request"]["prompt"] != original_prompt
    assert len(result["maker_request"]["prompt"]) < 200
    # every other field survives the trim untouched
    assert result["maker_request"]["role"] == "implementer"
    assert result["maker_request"]["working_directory"] == str(worktree)

    # The redacted payload must still satisfy AgentRequest's own validation --
    # this is exactly the reconstruction metadata.py's _agent_request() does
    # when persisting the agent result, and it crashed on this in production.
    AgentRequest.model_validate(result["maker_request"])


@pytest.mark.asyncio
async def test_observability_persistence_failure_does_not_fail_provider_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "engineering_control.activities.provider.activity.heartbeat", lambda _: None
    )

    async def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("observability unavailable")

    monkeypatch.setattr(
        "engineering_control.activities.provider.AgentTranscriptRecorder.start", unavailable
    )
    monkeypatch.setattr(
        "engineering_control.activities.provider.AgentTranscriptRecorder.event", unavailable
    )
    monkeypatch.setattr(
        "engineering_control.activities.provider.AgentTranscriptRecorder.finish", unavailable
    )
    worktree_root = tmp_path / "worktrees"
    worktree = worktree_root / "falcone" / "change-set"
    worktree.mkdir(parents=True)
    handler = ProviderActivityHandler(
        Settings(worktree_root=worktree_root, run_root=tmp_path / "runs"),
        "codex",
        FakeAdapter(),
        FakeInstructionPack(),
    )

    result = await handler.handle(
        "delivery.invoke_maker",
        {"maker_request": request_for(worktree).model_dump(mode="json")},
    )

    assert result["status"] == "SUCCESS"
    assert result["observability"]["transcript_incomplete"] is True


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
        FakeInstructionPack(),
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
        FakeInstructionPack(),
    )

    with pytest.raises(ApplicationError, match="bounded worktree"):
        await handler.handle(
            "delivery.invoke_maker",
            {"maker_request": request_for(outside).model_dump(mode="json")},
        )


@pytest.mark.asyncio
async def test_provider_activity_rejects_non_git_capability_outside_canary(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "worktrees"
    worktree = allowed / "falcone" / "change-set"
    worktree.mkdir(parents=True)
    handler = ProviderActivityHandler(
        Settings(worktree_root=allowed, run_root=tmp_path / "runs"),
        "codex",
        FakeAdapter(),
        FakeInstructionPack(),
    )
    request = request_for(worktree, writable=False).model_copy(
        update={"skip_git_repository_check": True}
    )

    with pytest.raises(ApplicationError, match="fixed observability canary"):
        await handler.handle(
            "observability.invoke_codex",
            {"maker_request": request.model_dump(mode="json")},
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
        FakeInstructionPack(),
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
        FakeInstructionPack(),
    )

    result = await handler.handle(
        "delivery.invoke_maker",
        {"maker_request": request_for(worktree).model_dump(mode="json")},
    )

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "MANUAL_AUTH_REQUIRED_CODEX"
    assert result["provider"] == "codex"
    assert result["instruction_bundle"]["sha256"] == "a" * 64
    assert "agent_result" not in result


@pytest.mark.asyncio
async def test_auth_probe_timeout_is_not_reported_as_password_or_quota_failure(
    tmp_path: Path,
) -> None:
    class UnavailableAdapter(FakeAdapter):
        async def authenticated(self) -> bool:
            raise AuthProbeError("TIMED_OUT")

        async def execute(self, request: AgentRequest, on_event: Any = None) -> AgentResult:
            raise AssertionError("model work must not start after an indeterminate auth check")

    root = tmp_path / "worktrees"
    worktree = root / "falcone" / "change-set"
    worktree.mkdir(parents=True)
    handler = ProviderActivityHandler(
        Settings(worktree_root=root, run_root=tmp_path / "runs"),
        "codex",
        UnavailableAdapter(),
        FakeInstructionPack(),
    )
    result = await handler.handle(
        "delivery.invoke_maker", {"maker_request": request_for(worktree).model_dump(mode="json")}
    )
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "AUTH_STATUS_TIMED_OUT_CODEX"
    quota = await handler.handle("quota.refresh", {"provider": "codex"})
    assert quota["authenticated"] is None and quota["cli_present"] is True
    assert quota["status"] == "AUTH_STATUS_TIMED_OUT_CODEX"
    assert quota["remaining_capacity"] is None and quota["rate_limited"] is False


@pytest.mark.asyncio
async def test_provider_activity_fails_closed_on_invalid_agent_pack(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    worktree = worktree_root / "falcone" / "change-set"
    worktree.mkdir(parents=True)
    handler = ProviderActivityHandler(
        Settings(worktree_root=worktree_root, run_root=tmp_path / "runs"),
        "codex",
        FakeAdapter(),
        InvalidInstructionPack(),
    )

    result = await handler.handle(
        "delivery.invoke_maker",
        {"maker_request": request_for(worktree).model_dump(mode="json")},
    )

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "AGENT_PACK_INVALID"
    # NEW-146: the message, not only the type, reaches the operator (bounded, redacted).
    assert result["blocker_detail"] == "ValueError: unsafe agent pack"
    assert "agent_result" not in result
