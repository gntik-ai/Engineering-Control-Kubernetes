"""NEW-168: a provider's pre-invocation auth-probe timeout is a clean, retryable stop.

falcone #975 (2026-10-09 09:34): the maker's `codex login status` probe exceeded its
15 s deadline on a CPU-starved worker. The BLOCKED result still carried the spec's
agent_result, agent.persist_result refused it as a foreign result (PolicyViolation), the
child FAILED and the ChangeSet stayed PLANNED until a manual database fix.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from temporalio import workflow
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer
from test_issue_delivery_approval import _child, _dispose
from test_new_128_correction_state import _next_child, _planned, _state
from test_new_137_replay import _RETRY_THEN_SKIP, _TEMPORAL_CLI
from test_provider_activity import FakeAdapter, FakeInstructionPack, request_for
from test_workflow_replay import (
    _full_delivery_loop_script,
    _markers,
    _paused_blockers,
    _run_loop,
)

from engineering_control.activities.provider import ProviderActivityHandler
from engineering_control.adapters.auth_probe import AuthProbeError
from engineering_control.config import Settings
from engineering_control.db.models import AgentInvocation
from engineering_control.models.core import AgentRequest, AgentResult
from engineering_control.workflows.definitions import (
    ExistingGitHubBacklogLoopWorkflow,
)
from engineering_control.workflows.inputs import IssueDeliveryInput
from engineering_control.workflows.provider_stops import provider_unavailable_stop


class _ProbeTimedOut(FakeAdapter):
    async def authenticated(self) -> bool:
        raise AuthProbeError("TIMED_OUT")

    async def execute(self, request: AgentRequest, on_event: Any = None) -> AgentResult:
        raise AssertionError("no model work after an indeterminate auth check")


def _stale_spec_result() -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "run_id": str(uuid4()),
        "provider": "claude",
        "role": "product-spec-architect",
        "status": "SUCCESS",
        "usage": {},
        "started_at": now,
        "finished_at": now,
    }


@pytest.mark.asyncio
async def test_a_maker_auth_probe_timeout_is_persisted_and_the_retry_binds(
    tmp_path: Path,
) -> None:
    values = await _child(tmp_path)
    _operations, _metadata, engineering, handler, payload, _registered, child = values
    first = handler._load_confirmed_finding(child)
    change_set_id = first["change_set_id"]
    _planned(handler, change_set_id)
    worktree = tmp_path / "worktrees" / "falcone" / "synthetic"
    worktree.mkdir(parents=True)
    stale = _stale_spec_result()
    context = {
        **first,
        # The spec call's result is still in the context the maker step receives.
        "agent_result": stale,
        "agent_invocation_id": stale["run_id"],
        "agent_invocation_status": "SUCCESS",
        "observability": {"execution_id": stale["run_id"], "transcript_incomplete": False},
        "maker_request": request_for(worktree).model_dump(mode="json"),
    }
    started = await handler.handle(
        "agent.record_start", {**context, "provider_step": "delivery.invoke_maker"}
    )
    provider = ProviderActivityHandler(
        Settings(worktree_root=tmp_path / "worktrees", run_root=tmp_path / "runs"),  # type: ignore[call-arg]
        "codex",
        _ProbeTimedOut(),
        FakeInstructionPack(),
    )
    blocked = await provider.handle("delivery.invoke_maker", started)

    assert (blocked["status"], blocked["blocker"]) == ("BLOCKED", "AUTH_STATUS_TIMED_OUT_CODEX")
    assert "agent_result" not in blocked and "observability" not in blocked
    # Before the fix the stale result reached persist_result, which refused it.
    with pytest.raises(ApplicationError, match="PolicyViolation"):
        await handler.handle("agent.persist_result", {**blocked, "agent_result": stale})
    persisted = await handler.handle("agent.persist_result", blocked)
    assert persisted["agent_invocation_status"] == "BLOCKED"
    with handler.sessions() as session:
        invocation = session.get(AgentInvocation, UUID(persisted["agent_invocation_id"]))
        assert (invocation.status, invocation.error_code) == (
            "BLOCKED",
            "AUTH_STATUS_TIMED_OUT_CODEX",
        )
    handler._release_delivery_leases(persisted)
    state, _, recovery = _state(handler, change_set_id)
    assert state == "REQUEST_CHANGES"
    requested = recovery["correction_requested"]
    assert (requested["status"], requested["blocker"]) == ("BLOCKED", "PROVIDER_UNAVAILABLE")
    assert requested["error_code"] == "AUTH_STATUS_TIMED_OUT_CODEX"
    assert requested["provider_error_kind"] == "AUTH_STATUS_TIMED_OUT"
    assert (requested["provider"], requested["provider_step"]) == (
        "codex",
        "delivery.invoke_maker",
    )
    retried = _next_child(
        engineering, handler, payload, child, "backlog-fixture-issue-1-retry-1-attempt-1"
    )
    assert retried["status"] == "CONFIRMED_LOADED"
    _dispose(values)


def _blocked_context(blocker: str, *, provider: str = "codex", status: str = "BLOCKED") -> Any:
    invocation = str(uuid4())
    return {
        "status": status,
        "blocker": blocker,
        "provider": provider,
        "provider_step": "delivery.invoke_maker",
        "maker_request": {"run_id": invocation, "provider": provider, "role": "implementer"},
        "agent_invocation_id": invocation,
        "agent_invocation_status": status,
    }


def test_only_the_timed_out_auth_probe_is_a_transient_stop() -> None:
    for provider in ("codex", "claude"):
        stop = provider_unavailable_stop(
            _blocked_context(f"AUTH_STATUS_TIMED_OUT_{provider.upper()}", provider=provider)
        )
        assert stop is not None and stop["blocker"] == "PROVIDER_UNAVAILABLE"
        assert stop["provider"] == provider
    for blocker in (
        "MANUAL_AUTH_REQUIRED_CODEX",
        "PROVIDER_CLI_MISSING_CODEX",
        "AUTH_STATUS_OUTPUT_LIMIT_CODEX",
        "AUTH_STATUS_INVALID_RESPONSE_CODEX",
        "AGENT_PACK_INVALID",
        "TOOLCHAIN_CONTRACT_INVALID",
    ):
        assert provider_unavailable_stop(_blocked_context(blocker)) is None
    # Bound to the request's provider, the BLOCKED status and this invocation.
    assert provider_unavailable_stop(_blocked_context("AUTH_STATUS_TIMED_OUT_CLAUDE")) is None
    assert (
        provider_unavailable_stop(_blocked_context("AUTH_STATUS_TIMED_OUT_CODEX", status="FAILED"))
        is None
    )
    foreign = _blocked_context("AUTH_STATUS_TIMED_OUT_CODEX")
    foreign["agent_invocation_id"] = str(uuid4())
    assert provider_unavailable_stop(foreign) is None
    mislabeled = _blocked_context("AUTH_STATUS_TIMED_OUT_CODEX")
    mislabeled["provider"] = "claude"
    assert provider_unavailable_stop(mislabeled) is None


_AUTH_TIMEOUT_CONTEXT = {
    **_blocked_context("AUTH_STATUS_TIMED_OUT_CODEX"),
    "change_set_id": "7221c489-0000-4000-8000-000000000168",
}


@workflow.defn(name="IssueDeliveryWorkflow", sandboxed=False)
class _AuthTimeoutDeliveryStub:
    """falcone #975 after NEW-168: the maker's auth probe timed out (a clean BLOCKED)."""

    @workflow.run
    async def run(self, request: IssueDeliveryInput) -> dict[str, Any]:
        return {"status": "BLOCKED", "context": _AUTH_TIMEOUT_CONTEXT}


@pytest.mark.skipif(_TEMPORAL_CLI is None, reason="requires the `temporal` CLI on PATH")
@pytest.mark.asyncio
async def test_a_blocked_auth_timeout_child_keeps_the_loop_decisions_and_replays() -> None:
    workflows: list[type] = [ExistingGitHubBacklogLoopWorkflow, _AuthTimeoutDeliveryStub]
    history, result, _children = await _run_loop(
        _full_delivery_loop_script(), _RETRY_THEN_SKIP, workflows=workflows
    )
    # The workflow is unchanged: a BLOCKED child pauses exactly as before (only the
    # activity-side correction state differs), and no patch marker is recorded.
    assert result["status"] == "COMPLETE"
    assert _paused_blockers(history) == ["BLOCKED", "BLOCKED"]
    assert [m for m in _markers(history) if b"provider-unavailable" in m] == []
    await Replayer(workflows=workflows).replay_workflow(history)
