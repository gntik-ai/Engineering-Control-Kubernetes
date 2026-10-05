"""NEW-137: a delivery attempt that stopped because a provider call was unavailable.

Pure and deterministic (no I/O, clock or randomness): delivery.release_leases uses it to
record the correction state, and ExistingGitHubBacklogLoopWorkflow to name the pause.

falcone #1012: a retry child's Claude checker call failed with PROVIDER_EXIT_NONZERO
("API Error: 529 Overloaded"); the child ended FAILED with no blocker, the ChangeSet
stayed PLANNED, and the next operator retry was refused ("another delivery child
requires an explicit correction state") until a manual database fix.

Only an explicit allowlist of transient provider signals qualifies, read from
provider-owned diagnostics (never model output). Policy, schema,
authentication, missing-CLI, budget (max turns), sandbox, timeout and every other
failure stays as it is.
"""

from __future__ import annotations

from typing import Any

from engineering_control.provider_error_kinds import TRANSIENT_PROVIDER_ERROR_KINDS

PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

# The delivery provider calls and the payload key of each one's normalized request.
DELIVERY_PROVIDER_REQUESTS: dict[str, str] = {
    "delivery.invoke_spec": "spec_request",
    "delivery.invoke_maker": "maker_request",
    "delivery.invoke_deployment_maker": "deployment_maker_request",
    "delivery.invoke_checker": "checker_request",
}

# Provider result error codes (adapters.process.classify_failure) that may be transient.
# Each qualifies only with a transient provider_error_kind, which the adapter derives
# from provider-owned diagnostics alone (engineering_control.provider_error_kinds): never
# from the summary, which can be model output (Codex keeps the agent's final message).
# The kind gives the terminal result precedence, so a capacity error seen before an
# error_max_turns / structured-output-retries terminal result is not transient.
_TRANSIENT_RESULTS = frozenset(
    {
        ("RATE_LIMITED", "PROVIDER_RATE_LIMIT"),
        ("FAILED", "PROVIDER_MODEL_AT_CAPACITY"),
        ("FAILED", "PROVIDER_EXIT_NONZERO"),
    }
)
# _agent_step's exception path: the activity raised and no provider result exists.
# RATE_LIMITED is the provider's own typed rate-limit error. PROVIDER_ACTIVITY_FAILED
# covers every other activity error (NotConfigured, TypeError, ...), so it qualifies only
# for a provider worker that stopped answering (heartbeat / schedule-to-start timeout
# after the activity retry policy), recorded by the workflow as provider_failure_type.
_TRANSIENT_ACTIVITY_FAILURES = frozenset({"TIMEOUT_HEARTBEAT", "TIMEOUT_SCHEDULE_TO_START"})
_PROVIDERS = frozenset({"claude", "codex"})


def provider_unavailable_stop(payload: Any) -> dict[str, str] | None:
    """The provider stop of a delivery attempt's final context, or None.

    The context must be agent.persist_result's output for the delivery provider call
    that stopped the attempt: the recorded invocation and its status must match.
    """
    if not isinstance(payload, dict):
        return None
    step = payload.get("provider_step")
    if not isinstance(step, str) or step not in DELIVERY_PROVIDER_REQUESTS:
        return None
    request_key = DELIVERY_PROVIDER_REQUESTS[step]
    status = payload.get("status")
    invocation_id = payload.get("agent_invocation_id")
    if not isinstance(status, str) or not invocation_id:
        return None
    if payload.get("agent_invocation_status") != status:
        return None
    request = payload.get(request_key)
    request = request if isinstance(request, dict) else {}
    result = payload.get("agent_result")
    if isinstance(result, dict) and str(result.get("run_id")) == str(invocation_id):
        # The stopping call's own provider result.
        code = result.get("error_code")
        kind = result.get("provider_error_kind")
        if result.get("status") != status or (status, code) not in _TRANSIENT_RESULTS:
            return None
        if kind not in TRANSIENT_PROVIDER_ERROR_KINDS:
            return None  # no provider-owned transient signal: fail closed
        return _stop(step, status, code, kind, result.get("provider"), result.get("role"))
    blocker = payload.get("blocker")
    if status != "FAILED" or str(request.get("run_id")) != str(invocation_id):
        return None
    if blocker == "RATE_LIMITED":
        return _stop(
            step, status, blocker, "RATE_LIMITED", request.get("provider"), request.get("role")
        )
    failure = payload.get("provider_failure_type")
    if blocker == "PROVIDER_ACTIVITY_FAILED" and failure in _TRANSIENT_ACTIVITY_FAILURES:
        return _stop(step, status, blocker, failure, request.get("provider"), request.get("role"))
    return None


def _stop(
    step: str, status: str, code: Any, kind: Any, provider: Any, role: Any
) -> dict[str, str] | None:
    if provider not in _PROVIDERS:
        return None
    return {
        "status": status,
        "blocker": PROVIDER_UNAVAILABLE,
        "provider": str(provider),
        "error_code": str(code)[:120],
        "provider_error_kind": str(kind)[:60],
        "provider_step": step,
        "role": str(role or "")[:80],
    }
