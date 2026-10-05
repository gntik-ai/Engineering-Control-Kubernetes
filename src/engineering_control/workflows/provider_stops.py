"""NEW-137: a delivery attempt that stopped because a provider call was unavailable.

Pure and deterministic (no I/O, clock or randomness): delivery.release_leases uses it to
record the correction state, and ExistingGitHubBacklogLoopWorkflow to name the pause.

falcone #1012: a retry child's Claude checker call failed with PROVIDER_EXIT_NONZERO
("API Error: 529 Overloaded"); the child ended FAILED with no blocker, the ChangeSet
stayed PLANNED, and the next operator retry was refused ("another delivery child
requires an explicit correction state") until a manual database fix.

Only an explicit allowlist of transient provider signals qualifies. Policy, schema,
authentication, missing-CLI, budget (max turns), sandbox, timeout and every other
failure stays as it is.
"""

from __future__ import annotations

import re
from typing import Any

PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

# The delivery provider calls and the payload key of each one's normalized request.
DELIVERY_PROVIDER_REQUESTS: dict[str, str] = {
    "delivery.invoke_spec": "spec_request",
    "delivery.invoke_maker": "maker_request",
    "delivery.invoke_deployment_maker": "deployment_maker_request",
    "delivery.invoke_checker": "checker_request",
}

# Provider results (adapters.process.classify_failure) that are transient by
# classification: the provider's own rate limit, and a model at capacity after the
# provider activity's bounded same-model retries.
_TRANSIENT_RESULTS = frozenset(
    {
        ("RATE_LIMITED", "PROVIDER_RATE_LIMIT"),
        ("FAILED", "PROVIDER_MODEL_AT_CAPACITY"),
    }
)
# PROVIDER_EXIT_NONZERO is the catch-all of a non-zero CLI exit. It qualifies only when
# the provider's own error text names an unavailable service, and never when it names a
# non-transient cause (max turns, structured-output retries, authentication, budget...).
_EXIT_NONZERO = ("FAILED", "PROVIDER_EXIT_NONZERO")
_UNAVAILABLE_TEXT = re.compile(
    r"\b(?:api error|http|status(?: code)?)\W{0,3}5\d\d\b"
    r"|\boverloaded\b"
    r"|\bservice unavailable\b"
    r"|\bbad gateway\b"
    r"|\bgateway timeout\b"
    r"|\binternal server error\b"
    r"|\btemporarily unavailable\b"
    r"|\bhigh demand\b"
    r"|\bconnection (?:error|reset|refused)\b"
    r"|\beconnreset\b"
    r"|\betimedout\b"
    r"|\bstream disconnected\b",
    re.IGNORECASE,
)
_NON_TRANSIENT_TEXT = re.compile(
    r"error_max_turns|max_turns|error_max_structured_output_retries|structured.output"
    r"|\bbudget\b|\bauth\w*|\bunauthori[sz]ed\b|\bforbidden\b|\bcredential\w*"
    r"|\blog ?in\b|\bapi key\b|\bsandbox\w*|\bpolic(?:y|ies)\b|\bschema\b|\b40[13]\b",
    re.IGNORECASE,
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
        if result.get("status") != status or not isinstance(code, str):
            return None
        if (status, code) not in _TRANSIENT_RESULTS:
            text = str(result.get("summary") or "")
            if (status, code) != _EXIT_NONZERO or not _UNAVAILABLE_TEXT.search(text):
                return None
            if _NON_TRANSIENT_TEXT.search(text):
                return None
        return _stop(step, status, code, result.get("provider"), result.get("role"))
    blocker = payload.get("blocker")
    if status != "FAILED" or str(request.get("run_id")) != str(invocation_id):
        return None
    if blocker == "RATE_LIMITED":
        return _stop(step, status, blocker, request.get("provider"), request.get("role"))
    failure = payload.get("provider_failure_type")
    if blocker == "PROVIDER_ACTIVITY_FAILED" and failure in _TRANSIENT_ACTIVITY_FAILURES:
        return _stop(
            step,
            status,
            f"{blocker}:{failure}",
            request.get("provider"),
            request.get("role"),
        )
    return None


def _stop(step: str, status: str, code: str, provider: Any, role: Any) -> dict[str, str] | None:
    if provider not in _PROVIDERS:
        return None
    return {
        "status": status,
        "blocker": PROVIDER_UNAVAILABLE,
        "provider": str(provider),
        "error_code": code[:120],
        "provider_step": step,
        "role": str(role or "")[:80],
    }
