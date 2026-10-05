"""NEW-137: a delivery attempt stopped by a transient provider failure leaves a correction
state, so a bounded retry binds after a provider outage without a database fix."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from temporalio.exceptions import ApplicationError, TimeoutError, TimeoutType
from test_issue_delivery_approval import _child, _dispose
from test_new_128_correction_state import _next_child, _planned, _state, _validated

from engineering_control.db.models import ChangeSet
from engineering_control.delivery_notifications import explain_blocker
from engineering_control.workflows.definitions import _provider_failure_type
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
                loaded, status="RATE_LIMITED", error_code="PROVIDER_RATE_LIMIT", summary=""
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
        lambda loaded: _provider_result(loaded, summary="claude result error_max_turns"),
        id="max-turns-budget",
    ),
    pytest.param(
        lambda loaded: _provider_result(
            loaded, summary="claude result error_max_structured_output_retries"
        ),
        id="structured-output-schema",
    ),
    pytest.param(
        lambda loaded: _provider_result(
            loaded, summary="API Error: 401 authentication_error: invalid credentials"
        ),
        id="auth-401",
    ),
    pytest.param(
        lambda loaded: _provider_result(
            loaded, summary="API Error: 500 sandbox denied the write (policy)"
        ),
        id="sandbox-policy-text",
    ),
    pytest.param(
        lambda loaded: _provider_result(loaded, summary="process exited with code 1"),
        id="exit-without-provider-text",
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
    assert _provider_failure_type(None) == "UNKNOWN"
