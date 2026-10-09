"""NEW-137: a delivery attempt stopped by a transient provider failure leaves a correction
state, so a bounded retry binds after a provider outage without a database fix."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from temporalio.exceptions import ApplicationError, TimeoutError, TimeoutType
from test_claude_adapter import _execute_with_events
from test_issue_delivery_approval import _child, _dispose
from test_new_128_correction_state import _next_child, _planned, _state, _validated

from engineering_control.adapters.codex import client as codex_module
from engineering_control.adapters.codex.client import CodexCLIAdapter
from engineering_control.adapters.process import ProcessResult
from engineering_control.db.models import ChangeSet
from engineering_control.delivery_notifications import explain_blocker
from engineering_control.models.core import AgentRequest
from engineering_control.models.enums import ModelClass, Project, Provider, RunStatus
from engineering_control.provider_error_kinds import provider_error_kind
from engineering_control.workflows.definitions import (
    _agent_failure_payload,
    _provider_failure_type,
)
from engineering_control.workflows.provider_stops import provider_unavailable_stop

_REQUEST_KEYS = {
    "delivery.invoke_spec": "spec_request",
    "delivery.invoke_maker": "maker_request",
    "delivery.invoke_deployment_maker": "deployment_maker_request",
    "delivery.invoke_checker": "checker_request",
}


def _provider_result(
    loaded: dict[str, Any],
    *,
    status: str = "FAILED",
    error_code: str = "PROVIDER_EXIT_NONZERO",
    summary: str = "API Error: 529 Overloaded",
    provider: str = "claude",
    step: str = "delivery.invoke_checker",
    kind: str | None = "OVERLOADED",
) -> dict[str, Any]:
    """agent.persist_result's output for a provider call that returned a result."""
    invocation = str(uuid4())
    role = "code-reviewer" if step == "delivery.invoke_checker" else "implementer"
    return {
        **loaded,
        "status": status,
        "provider": provider,
        "provider_step": step,
        _REQUEST_KEYS[step]: {"run_id": invocation, "provider": provider, "role": role},
        "agent_invocation_id": invocation,
        "agent_invocation_status": status,
        "agent_result": {
            "run_id": invocation,
            "provider": provider,
            "role": role,
            "status": status,
            "error_code": error_code,
            "summary": summary,
            "provider_error_kind": kind,
        },
    }


def _activity_failure(
    loaded: dict[str, Any],
    blocker: str,
    *,
    failure_type: str | None = None,
    step: str = "delivery.invoke_maker",
    provider: str = "codex",
) -> dict[str, Any]:
    """agent.persist_result's output after _agent_step caught an ActivityError."""
    invocation = str(uuid4())
    payload: dict[str, Any] = {
        **loaded,
        "status": "FAILED",
        "blocker": blocker,
        "provider_step": step,
        _REQUEST_KEYS[step]: {"run_id": invocation, "provider": provider, "role": "implementer"},
        "agent_invocation_id": invocation,
        "agent_invocation_status": "FAILED",
        # The previous provider call's result is still in the context (a different id).
        "agent_result": {
            "run_id": str(uuid4()),
            "provider": "claude",
            "status": "SUCCESS",
            "error_code": None,
        },
    }
    if failure_type is not None:
        payload["provider_failure_type"] = failure_type
    return payload


@pytest.mark.asyncio
async def test_a_checker_529_records_provider_unavailable_and_the_retry_binds(
    tmp_path: Path,
) -> None:
    # falcone #1012 / ChangeSet 43b4dc63: attempt 1 was asked for a correction, the
    # correction attempt's Claude checker failed with 529 and the operator retry bound
    # only after a manual PLANNED -> REQUEST_CHANGES fix.
    values = await _child(tmp_path)
    _operations, _metadata, engineering, handler, payload, _registered, child = values
    first = handler._load_confirmed_finding(child)
    change_set_id = first["change_set_id"]
    _planned(handler, change_set_id)
    handler._release_delivery_leases(_validated(tmp_path, first, checker="REQUEST_CHANGES"))
    second = _next_child(engineering, handler, payload, child, "backlog-fixture-issue-1-attempt-2")
    _planned(handler, change_set_id)
    _, version, before = _state(handler, change_set_id)
    assert before["correction_requested"]["run_id"] == first["run_id"]

    stopped = _provider_result(second)
    assert handler._release_delivery_leases(stopped)["status"] == "LEASES_RELEASED"
    state, after, recovery = _state(handler, change_set_id)
    assert (state, after) == ("REQUEST_CHANGES", version + 1)
    requested = recovery["correction_requested"]
    assert requested["run_id"] == second["run_id"]
    assert requested["run_ids"] == [first["run_id"], second["run_id"]]
    assert (requested["status"], requested["blocker"]) == ("FAILED", "PROVIDER_UNAVAILABLE")
    assert requested["provider"] == "claude"
    assert requested["error_code"] == "PROVIDER_EXIT_NONZERO"
    assert requested["provider_error_kind"] == "OVERLOADED"
    assert requested["provider_step"] == "delivery.invoke_checker"
    # A duplicate release changes nothing.
    handler._release_delivery_leases(stopped)
    assert _state(handler, change_set_id)[:2] == ("REQUEST_CHANGES", version + 1)

    retried = _next_child(
        engineering, handler, payload, child, "backlog-fixture-issue-1-retry-1-attempt-1"
    )
    assert retried["status"] == "CONFIRMED_LOADED"
    assert retried["change_set_id"] == change_set_id
    _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stop",
    [
        pytest.param(
            lambda loaded: _provider_result(
                loaded,
                status="RATE_LIMITED",
                error_code="PROVIDER_RATE_LIMIT",
                summary="",
                kind="RATE_LIMITED",
            ),
            id="rate-limited-result",
        ),
        pytest.param(
            lambda loaded: _provider_result(
                loaded,
                error_code="PROVIDER_MODEL_AT_CAPACITY",
                summary="Selected model is at capacity",
                provider="codex",
                step="delivery.invoke_maker",
            ),
            id="maker-at-capacity",
        ),
        pytest.param(
            lambda loaded: _activity_failure(loaded, "RATE_LIMITED"), id="rate-limited-error"
        ),
        pytest.param(
            lambda loaded: _activity_failure(
                loaded,
                "PROVIDER_ACTIVITY_FAILED",
                failure_type="TIMEOUT_HEARTBEAT",
                step="delivery.invoke_checker",
            ),
            id="worker-heartbeat-timeout",
        ),
        pytest.param(
            lambda loaded: _activity_failure(
                loaded, "PROVIDER_ACTIVITY_FAILED", failure_type="WORKER_SHUTDOWN"
            ),
            id="worker-shutdown",  # NEW-166: gntik-ui #25's maker
        ),
    ],
)
async def test_rate_limits_and_unavailable_workers_behave_the_same(
    tmp_path: Path, stop: Any
) -> None:
    values = await _child(tmp_path)
    _operations, _metadata, engineering, handler, payload, _registered, child = values
    first = handler._load_confirmed_finding(child)
    _planned(handler, first["change_set_id"])
    handler._release_delivery_leases(stop(first))
    state, _, recovery = _state(handler, first["change_set_id"])
    assert state == "REQUEST_CHANGES"
    assert recovery["correction_requested"]["blocker"] == "PROVIDER_UNAVAILABLE"
    retried = _next_child(
        engineering, handler, payload, child, "backlog-fixture-issue-1-retry-1-attempt-1"
    )
    assert retried["status"] == "CONFIRMED_LOADED"
    _dispose(values)


_NOT_CONVERTED = [
    pytest.param(lambda loaded: _activity_failure(loaded, "PolicyViolation"), id="policy"),
    pytest.param(lambda loaded: _activity_failure(loaded, "SchemaViolation"), id="schema"),
    pytest.param(
        lambda loaded: _activity_failure(loaded, "MANUAL_AUTH_REQUIRED_CLAUDE"), id="auth"
    ),
    pytest.param(
        lambda loaded: _activity_failure(loaded, "PROVIDER_CLI_MISSING_CODEX"), id="cli-missing"
    ),
    pytest.param(
        lambda loaded: _activity_failure(
            loaded, "PROVIDER_ACTIVITY_FAILED", failure_type="NOT_CONFIGURED"
        ),
        id="activity-not-configured",
    ),
    pytest.param(
        lambda loaded: _activity_failure(
            loaded, "PROVIDER_ACTIVITY_FAILED", failure_type="TIMEOUT_START_TO_CLOSE"
        ),
        id="activity-start-to-close",
    ),
    pytest.param(
        lambda loaded: _activity_failure(loaded, "PROVIDER_ACTIVITY_FAILED"),
        id="activity-untyped",
    ),
    pytest.param(
        lambda loaded: _provider_result(loaded, kind=None),
        id="exit-without-provider-owned-signal",
    ),
    pytest.param(
        lambda loaded: _provider_result(loaded, summary="API Error: 500 retry later", kind=None),
        id="spoofed-5xx-in-model-summary",
    ),
    pytest.param(
        lambda loaded: _provider_result(loaded, kind="NON_TRANSIENT"),
        id="non-transient-kind",
    ),
    pytest.param(
        lambda loaded: _provider_result(
            loaded, error_code="PROVIDER_MODEL_AT_CAPACITY", kind="NON_TRANSIENT"
        ),
        id="capacity-code-with-non-transient-terminal",
    ),
    pytest.param(
        lambda loaded: _provider_result(loaded, kind="SOMETHING_ELSE"),
        id="unknown-kind",
    ),
    pytest.param(
        lambda loaded: _provider_result(
            loaded, status="TIMED_OUT", error_code="PROVIDER_TIMEOUT", summary="overloaded"
        ),
        id="provider-timeout",
    ),
    pytest.param(
        lambda loaded: {
            **_provider_result(loaded),
            "agent_invocation_id": str(uuid4()),
        },
        id="result-of-another-invocation",
    ),
    pytest.param(
        lambda loaded: {
            **_provider_result(loaded),
            "provider_step": "observability.invoke_claude",
        },
        id="not-a-delivery-call",
    ),
    pytest.param(
        lambda loaded: {**_provider_result(loaded), "agent_invocation_status": "SUCCESS"},
        id="invocation-status-mismatch",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", _NOT_CONVERTED)
async def test_non_transient_failures_are_not_converted(tmp_path: Path, stop: Any) -> None:
    values = await _child(tmp_path)
    handler, child = values[3], values[6]
    first = handler._load_confirmed_finding(child)
    _planned(handler, first["change_set_id"])
    before = _state(handler, first["change_set_id"])
    handler._release_delivery_leases(stop(first))
    assert _state(handler, first["change_set_id"]) == before
    _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["CONFIRMED", "SOURCE_PR", "SOURCE_MERGED"])
async def test_only_planned_or_implementing_change_sets_move(tmp_path: Path, state: str) -> None:
    values = await _child(tmp_path)
    handler, child = values[3], values[6]
    first = handler._load_confirmed_finding(child)
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, UUID(first["change_set_id"]))
        change_set.state = state
        if state != "CONFIRMED":
            change_set.source_pr_url = "https://github.com/gntik-ai/falcone/pull/7"
    before = _state(handler, first["change_set_id"])
    handler._release_delivery_leases(_provider_result(first))
    assert _state(handler, first["change_set_id"]) == before
    _dispose(values)


@pytest.mark.asyncio
async def test_a_superseded_attempts_provider_release_changes_nothing(tmp_path: Path) -> None:
    values = await _child(tmp_path)
    _operations, _metadata, engineering, handler, payload, _registered, child = values
    first = handler._load_confirmed_finding(child)
    change_set_id = first["change_set_id"]
    _planned(handler, change_set_id)
    stopped = _provider_result(first)
    handler._release_delivery_leases(stopped)
    assert _state(handler, change_set_id)[0] == "REQUEST_CHANGES"
    second = _next_child(
        engineering, handler, payload, child, "backlog-fixture-issue-1-retry-1-attempt-1"
    )
    _planned(handler, change_set_id)
    before = _state(handler, change_set_id)
    # Attempt 1's release is delivered again after the retry moved the ChangeSet on.
    handler._release_delivery_leases(stopped)
    assert _state(handler, change_set_id) == before
    # A first release from a superseded attempt never writes either.
    handler._release_delivery_leases(_provider_result(first, error_code="PROVIDER_RATE_LIMIT"))
    assert _state(handler, change_set_id) == before
    # The current attempt's own provider stop is recorded, once.
    handler._release_delivery_leases(_provider_result(second))
    state, version, recovery = _state(handler, change_set_id)
    assert (state, version) == ("REQUEST_CHANGES", before[1] + 1)
    assert recovery["correction_requested"]["run_ids"] == [first["run_id"], second["run_id"]]
    _dispose(values)


@pytest.mark.asyncio
async def test_provider_retries_stay_within_the_bounded_lineage(tmp_path: Path) -> None:
    values = await _child(tmp_path)
    _operations, _metadata, engineering, handler, payload, _registered, child = values
    limit = int(payload["correction_limit"])
    loaded = handler._load_confirmed_finding(child)
    for attempt in range(2, limit + 3):
        _planned(handler, loaded["change_set_id"])
        handler._release_delivery_leases(_provider_result(loaded))
        assert _state(handler, loaded["change_set_id"])[0] == "REQUEST_CHANGES"
        name = f"backlog-fixture-issue-1-attempt-{attempt}"
        if attempt <= limit + 1:
            loaded = _next_child(engineering, handler, payload, child, name)
            assert loaded["status"] == "CONFIRMED_LOADED"
        else:
            with pytest.raises(ValueError, match="exceeds the approved correction limit"):
                _next_child(engineering, handler, payload, child, name)
    _dispose(values)


def test_the_classifier_reads_the_live_529_result() -> None:
    loaded = {"run_id": "r", "change_set_id": "c"}
    stop = provider_unavailable_stop(_provider_result(loaded))
    assert stop is not None
    assert stop["blocker"] == "PROVIDER_UNAVAILABLE"
    assert (stop["provider"], stop["error_code"]) == ("claude", "PROVIDER_EXIT_NONZERO")
    assert stop["role"] == "code-reviewer"
    assert provider_unavailable_stop(None) is None
    assert provider_unavailable_stop({"status": "FAILED"}) is None
    # The stale result of the previous call never qualifies the exception path.
    failed = _activity_failure(loaded, "PROVIDER_ACTIVITY_FAILED", failure_type="NOT_CONFIGURED")
    assert provider_unavailable_stop(failed) is None
    assert "transient provider reason" in explain_blocker("DELIVERY_FAILED:PROVIDER_UNAVAILABLE")


def test_provider_failure_type_is_bounded() -> None:
    heartbeat = TimeoutError("lost", type=TimeoutType.HEARTBEAT, last_heartbeat_details=[])
    assert _provider_failure_type(heartbeat) == "TIMEOUT_HEARTBEAT"
    closing = TimeoutError("slow", type=TimeoutType.START_TO_CLOSE, last_heartbeat_details=[])
    assert _provider_failure_type(closing) == "TIMEOUT_START_TO_CLOSE"
    assert _provider_failure_type(ApplicationError("x", type="NotConfigured")) == "NOT_CONFIGURED"
    assert _provider_failure_type(RuntimeError("x")) == "RUNTIME_ERROR"
    # NEW-166: the type temporalio gives an activity whose worker shut down mid-run.
    shutdown = ApplicationError("Worker is shutting down", type="WorkerShutdown")
    assert _provider_failure_type(shutdown) == "WORKER_SHUTDOWN"
    assert _provider_failure_type(None) == "UNKNOWN"


# -- NEW-137 review round 1 ---------------------------------------------------------------
# Finding 1: only provider-owned diagnostics; finding 2: the terminal result wins.

_CLAUDE_529 = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "result": "API Error: 529 Overloaded",
}


@pytest.mark.parametrize(
    ("events", "stderr", "kind"),
    [
        pytest.param((_CLAUDE_529,), "", "OVERLOADED", id="claude-live-529"),
        pytest.param(
            (
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "api_error_status": 529,
                },
            ),
            "",
            "OVERLOADED",
            id="claude-api-error-status",
        ),
        pytest.param(
            (
                {
                    "type": "system",
                    "subtype": "api_retry",
                    "error_status": 503,
                    "error": "server_error",
                },
                {"type": "result", "subtype": "error_during_execution", "is_error": True},
            ),
            "",
            "SERVER_ERROR",
            id="claude-api-retry-5xx",
        ),
        pytest.param(
            (
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "result": "API Error: 429 rate_limit_error",
                },
            ),
            "",
            "RATE_LIMITED",
            id="claude-429",
        ),
        pytest.param(
            (
                {
                    "type": "turn.failed",
                    "error": {
                        "message": "exceeded retry limit, last status: 503 Service Unavailable"
                    },
                },
            ),
            "",
            "SERVER_ERROR",
            id="codex-turn-failed-503",
        ),
        # NEW-137 review 2: stderr is not provider-owned (tool errors, quoted patch lines),
        # so it never classifies, in either direction.
        pytest.param((), "Error: read ECONNRESET", None, id="stderr-ignored"),
        pytest.param((), "API Error: 500 Internal Server Error", None, id="stderr-quote-ignored"),
        pytest.param(
            (
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "API Error: 529 Overloaded",
                },
            ),
            "invalid patch line: schema",
            "OVERLOADED",
            id="stderr-cannot-hide-529",
        ),
        # Finding 2: transient errors before an excluded terminal subtype.
        pytest.param(
            (
                {
                    "type": "system",
                    "subtype": "api_retry",
                    "error_status": 529,
                    "error": "overloaded_error",
                },
                {"type": "result", "subtype": "error_max_turns", "is_error": True},
            ),
            "",
            "NON_TRANSIENT",
            id="overloaded-then-max-turns",
        ),
        pytest.param(
            (
                {
                    "type": "result",
                    "subtype": "error_max_structured_output_retries",
                    "is_error": True,
                    "errors": [{"type": "overloaded_error"}],
                },
            ),
            "",
            "NON_TRANSIENT",
            id="overloaded-with-structured-output-terminal",
        ),
        pytest.param(
            (
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "result": "API Error: 401 authentication_error: invalid x-api-key",
                },
            ),
            "",
            "NON_TRANSIENT",
            id="claude-auth",
        ),
        pytest.param(
            ({"type": "turn.failed", "error": {"message": "unexpected status 401 Unauthorized"}},),
            "",
            "NON_TRANSIENT",
            id="codex-auth",
        ),
        pytest.param(
            ({"type": "error", "message": "stream error: 503 but sandbox denied write"},),
            "",
            "NON_TRANSIENT",
            id="any-excluded-cause-wins",
        ),
        # Model text is never read.
        pytest.param(
            (
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "API Error: 529 Overloaded",
                },
            ),
            "",
            None,
            id="non-error-result-text",
        ),
        pytest.param(
            (
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "result": "The upstream API was overloaded (HTTP 503)",
                },
            ),
            "",
            None,
            id="error-result-model-text",
        ),
        pytest.param(
            (
                {"type": "assistant", "message": {"content": "API Error: 500"}},
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "status 503 overloaded"},
                },
            ),
            "",
            None,
            id="agent-messages",
        ),
        pytest.param((), "", None, id="no-diagnostics"),
    ],
)
def test_provider_error_kind_reads_provider_owned_diagnostics_only(
    events: tuple[dict[str, Any], ...], stderr: str, kind: str | None
) -> None:
    assert provider_error_kind(events) == kind
    assert provider_error_kind(events, timed_out=True) is None
    del stderr  # deliberately ignored (review 2)


def _from_agent_result(loaded: dict[str, Any], result: Any) -> dict[str, Any]:
    """agent.persist_result's output for a real adapter result (checker call)."""
    agent_result = result.model_dump(mode="json")
    invocation = agent_result["run_id"]
    return {
        **loaded,
        "status": agent_result["status"],
        "provider_step": "delivery.invoke_checker",
        "checker_request": {
            "run_id": invocation,
            "provider": agent_result["provider"],
            "role": agent_result["role"],
        },
        "agent_invocation_id": invocation,
        "agent_invocation_status": agent_result["status"],
        "agent_result": agent_result,
    }


async def _codex_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    final_message: str,
    events: tuple[dict[str, Any], ...],
) -> Any:
    working_directory = tmp_path / "codex-worktree"
    working_directory.mkdir(exist_ok=True)

    async def fake_run(command: list[str], **kwargs: Any) -> ProcessResult:
        final_path = Path(command[command.index("--output-last-message") + 1])
        final_path.write_text(final_message, encoding="utf-8")  # noqa: ASYNC240
        return ProcessResult(exit_code=1, events=events, stderr="")

    monkeypatch.setattr(codex_module, "run_jsonl_process", fake_run)
    request = AgentRequest(
        project=Project.FALCONE,
        role="code-reviewer",
        provider=Provider.CODEX,
        model_class=ModelClass.JUDGE,
        prompt="review a bounded synthetic change",
        working_directory=str(working_directory),
        output_schema={"type": "object"},
        skip_git_repository_check=True,
    )
    return await CodexCLIAdapter(temp_root=str(tmp_path / "codex-temp")).execute(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "converted"),
    [
        pytest.param(
            ({"type": "turn.failed", "error": {"message": "unexpected status 401 Unauthorized"}},),
            False,
            id="auth-failure",
        ),
        pytest.param((), False, id="no-provider-diagnostics"),
        pytest.param(
            ({"type": "error", "message": "exceeded retry limit, last status: 503"},),
            True,
            id="real-503",
        ),
    ],
)
async def test_a_model_summary_claiming_a_5xx_is_never_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: Any, converted: bool
) -> None:
    # Codex keeps the agent's final message as the summary: a model claiming "API
    # Error: 500" on an authentication failure must not become a retryable correction.
    result = await _codex_result(
        tmp_path,
        monkeypatch,
        final_message="API Error: 500 Internal Server Error, the provider is overloaded",
        events=events,
    )
    assert result.status is RunStatus.FAILED
    assert result.error_code == "PROVIDER_EXIT_NONZERO"
    assert "API Error: 500" in result.summary
    values = await _child(tmp_path)
    handler, child = values[3], values[6]
    first = handler._load_confirmed_finding(child)
    _planned(handler, first["change_set_id"])
    handler._release_delivery_leases(_from_agent_result(first, result))
    state, _, recovery = _state(handler, first["change_set_id"])
    if converted:
        assert state == "REQUEST_CHANGES"
        requested = recovery["correction_requested"]
        assert requested["error_code"] == "PROVIDER_EXIT_NONZERO"
        assert requested["provider_error_kind"] == "SERVER_ERROR"
    else:
        assert state == "PLANNED"
    _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "error_code", "converted"),
    [
        pytest.param((_CLAUDE_529,), "PROVIDER_EXIT_NONZERO", True, id="live-529"),
        pytest.param(
            (
                {"type": "system", "subtype": "api_retry", "error": "overloaded_error"},
                {
                    "type": "result",
                    "subtype": "error_max_turns",
                    "is_error": True,
                    "errors": [{"type": "overloaded_error", "message": "Overloaded"}],
                },
            ),
            "PROVIDER_MODEL_AT_CAPACITY",
            False,
            id="capacity-then-max-turns",
        ),
        pytest.param(
            (
                {
                    "type": "result",
                    "subtype": "error_max_structured_output_retries",
                    "is_error": True,
                    "errors": [{"type": "overloaded_error", "message": "Overloaded"}],
                },
            ),
            "PROVIDER_MODEL_AT_CAPACITY",
            False,
            id="capacity-then-structured-output-retries",
        ),
    ],
)
async def test_the_terminal_claude_result_wins_over_earlier_transient_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: Any,
    error_code: str,
    converted: bool,
) -> None:
    result, _ = await _execute_with_events(tmp_path, monkeypatch, events)
    assert result.status is RunStatus.FAILED
    assert result.error_code == error_code
    values = await _child(tmp_path)
    handler, child = values[3], values[6]
    first = handler._load_confirmed_finding(child)
    _planned(handler, first["change_set_id"])
    handler._release_delivery_leases(_from_agent_result(first, result))
    state = _state(handler, first["change_set_id"])[0]
    assert state == ("REQUEST_CHANGES" if converted else "PLANNED")
    _dispose(values)


# Finding 3: the real agent.persist_result after a maker/checker activity error.


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


def _provider_request(step: str, provider: str, role: str, model_class: str) -> dict[str, Any]:
    return {
        "run_id": str(uuid4()),
        "project": "falcone",
        "role": role,
        "provider": provider,
        "model_class": model_class,
        "prompt": "Make one bounded synthetic change.",
        "working_directory": "/srv/engineering/worktrees/falcone/synthetic",
        "output_schema": {"type": "object"},
        "timeout_seconds": 60,
        "writable": step != "delivery.invoke_checker",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step", "provider", "role", "model_class"),
    [
        ("delivery.invoke_maker", "codex", "implementer", "worker"),
        ("delivery.invoke_deployment_maker", "codex", "implementer", "worker"),
        ("delivery.invoke_checker", "claude", "code-reviewer", "judge"),
    ],
)
async def test_a_heartbeat_timeout_is_persisted_by_the_real_activity_and_recorded(
    tmp_path: Path, step: str, provider: str, role: str, model_class: str
) -> None:
    values = await _child(tmp_path)
    _operations, _metadata, engineering, handler, payload, _registered, child = values
    first = handler._load_confirmed_finding(child)
    change_set_id = first["change_set_id"]
    _planned(handler, change_set_id)
    stale = _stale_spec_result()
    context = {
        **first,
        # The previous provider call (the spec) left its result in the context.
        "agent_result": stale,
        "agent_invocation_id": stale["run_id"],
        "agent_invocation_status": "SUCCESS",
        _REQUEST_KEYS[step]: _provider_request(step, provider, role, model_class),
    }
    started = await handler.handle("agent.record_start", {**context, "provider_step": step})
    heartbeat = TimeoutError("lost", type=TimeoutType.HEARTBEAT, last_heartbeat_details=[])
    # Before the fix the stale result travelled into persist_result, which refused it.
    with pytest.raises(ApplicationError):
        await handler.handle(
            "agent.persist_result",
            {**started, "status": "FAILED", "blocker": "PROVIDER_ACTIVITY_FAILED"},
        )
    failed = _agent_failure_payload(started, heartbeat)
    assert "agent_result" not in failed
    assert failed["provider_failure_type"] == "TIMEOUT_HEARTBEAT"
    persisted = await handler.handle("agent.persist_result", failed)
    assert persisted["agent_invocation_status"] == "FAILED"
    handler._release_delivery_leases(persisted)
    state, _, recovery = _state(handler, change_set_id)
    assert state == "REQUEST_CHANGES"
    requested = recovery["correction_requested"]
    assert requested["blocker"] == "PROVIDER_UNAVAILABLE"
    assert requested["error_code"] == "PROVIDER_ACTIVITY_FAILED"
    assert requested["provider_error_kind"] == "TIMEOUT_HEARTBEAT"
    assert (requested["provider"], requested["provider_step"]) == (provider, step)
    retried = _next_child(
        engineering, handler, payload, child, "backlog-fixture-issue-1-retry-1-attempt-1"
    )
    assert retried["status"] == "CONFIRMED_LOADED"
    _dispose(values)
