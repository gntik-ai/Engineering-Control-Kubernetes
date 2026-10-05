"""NEW-137 review: a typed provider error kind from provider-owned diagnostics only.

The Claude and Codex adapters set AgentResult.provider_error_kind for a failed call from
their own parsing of the CLI's diagnostics: the terminal result's subtype and structured
error fields (Claude ``api_error_status``, ``errors``, ``error``, and the CLI's own
``API Error: ...`` result of an ``is_error`` envelope), Claude ``system/api_retry`` events,
Codex ``error`` / ``turn.failed`` events, and the adapter's captured stderr. Model output
(an agent's final message, a Codex last-message file) is never read.

The terminal state wins: an excluded terminal subtype (max turns, structured-output
retries, budget, ...) or any non-transient cause anywhere in these diagnostics
(authentication, permission, policy, sandbox, schema, ...) yields a non-transient kind,
whatever transient errors the stream showed before. No provider-owned signal: None.

Pure (re/json only) so the workflow sandbox can import the kind constants.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

OVERLOADED = "OVERLOADED"
RATE_LIMITED = "RATE_LIMITED"
SERVER_ERROR = "SERVER_ERROR"
CONNECTION_ERROR = "CONNECTION_ERROR"
NON_TRANSIENT = "NON_TRANSIENT"
TRANSIENT_PROVIDER_ERROR_KINDS = frozenset(
    {OVERLOADED, RATE_LIMITED, SERVER_ERROR, CONNECTION_ERROR}
)

# Claude terminal subtypes that do not by themselves name a cause; any other non-success
# terminal subtype (error_max_turns, error_max_structured_output_retries,
# error_max_budget_usd, ...) is the cause and is never transient.
_NEUTRAL_TERMINAL_SUBTYPES = frozenset({"", "success", "error_during_execution"})
_NON_TRANSIENT_TEXT = re.compile(
    r"\bauth\w*|\bunauthori[sz]ed\b|\bforbidden\b|\bpermission\w*|\bcredential\w*"
    r"|\blog ?in\b|\bapi[ _-]?key\b|\binvalid_request\w*|\bnot_found_error\b|\bbudget\b"
    r"|max_turns|structured.output|\bschema\b|\bsandbox\w*|\bpolic(?:y|ies)\b"
    r"|\bbilling\b|\bcontext (?:length|window)\b|\bprompt is too long\b",
    re.IGNORECASE,
)
_STATUS_TEXT = re.compile(
    r"\b(?:api error|status(?: code)?|http(?:/\d(?:\.\d)?)?)\W{0,3}(\d{3})\b", re.IGNORECASE
)
_OVERLOADED_TEXT = re.compile(r"overloaded|at capacity|high demand", re.IGNORECASE)
_RATE_LIMIT_TEXT = re.compile(
    r"rate[ _]limit|usage limit|quota exceeded|too many requests", re.IGNORECASE
)
_SERVER_TEXT = re.compile(
    r"\bapi_error\b|service unavailable|bad gateway|gateway timeout|internal server error"
    r"|temporarily unavailable|server error",
    re.IGNORECASE,
)
_CONNECTION_TEXT = re.compile(
    r"connection (?:error|reset|refused|closed)|econnreset|econnrefused|etimedout"
    r"|stream disconnected|network error|socket hang up",
    re.IGNORECASE,
)


def provider_error_kind(
    events: Iterable[dict[str, Any]], stderr: str, *, timed_out: bool = False
) -> str | None:
    """The typed kind of a failed provider call, or None without a provider-owned signal."""
    if timed_out:
        return None
    texts: list[str] = []
    statuses: list[int] = []
    terminal: str | None = None
    for event in list(events)[-500:]:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type", ""))
        if event_type == "result":
            # Only the last terminal envelope counts.
            terminal = str(event.get("subtype") or "")
            if event.get("is_error") is not True:
                continue
            _status(event.get("api_error_status"), statuses)
            _collect(event.get("errors"), texts)
            _collect(event.get("error"), texts)
            result = event.get("result")
            if isinstance(result, str) and result.startswith("API Error:"):
                # The CLI's own synthetic API-error message, never agent output.
                texts.append(result[:2000])
        elif event_type == "system" and event.get("subtype") == "api_retry":
            _status(event.get("error_status"), statuses)
            _collect(event.get("error"), texts)
        elif event_type in {"error", "turn.failed"}:
            _collect(event.get("error"), texts)
            _collect(event.get("message"), texts)
    if terminal is not None and terminal not in _NEUTRAL_TERMINAL_SUBTYPES:
        return NON_TRANSIENT
    if stderr:
        texts.append(stderr[-4000:])
    joined = "\n".join(texts)
    if not joined and not statuses:
        return None
    if _NON_TRANSIENT_TEXT.search(joined):
        return NON_TRANSIENT
    statuses.extend(int(match) for match in _STATUS_TEXT.findall(joined))
    if any(400 <= status < 500 and status not in {408, 409, 429} for status in statuses):
        return NON_TRANSIENT
    if 529 in statuses or _OVERLOADED_TEXT.search(joined):
        return OVERLOADED
    if 429 in statuses or _RATE_LIMIT_TEXT.search(joined):
        return RATE_LIMITED
    if any(500 <= status < 600 for status in statuses) or _SERVER_TEXT.search(joined):
        return SERVER_ERROR
    if 408 in statuses or _CONNECTION_TEXT.search(joined):
        return CONNECTION_ERROR
    return None


def _status(value: Any, statuses: list[int]) -> None:
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
        statuses.append(value)


def _collect(value: Any, texts: list[str]) -> None:
    if value is None or value == "" or value == {} or value == []:
        return
    if isinstance(value, list):
        for item in value[:20]:
            _collect(item, texts)
        return
    if isinstance(value, dict):
        texts.append(json.dumps(value, sort_keys=True, default=str)[:2000])
        return
    texts.append(str(value)[:2000])
