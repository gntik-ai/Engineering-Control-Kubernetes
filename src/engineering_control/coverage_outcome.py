from __future__ import annotations

_PASSED = frozenset({"NO_FINDING", "VERIFIED_FIXED", "PASS", "PASSED"})
_BLOCKED = frozenset({"BLOCKED"})
_FAILED = frozenset({"FAILED", "ERROR", "TIMED_OUT"})
_SKIPPED = frozenset({"SKIPPED", "EXCLUDED"})
_FINDING = frozenset({"CANDIDATE_FOUND", "CONFIRMED_FINDING"})
# BLOCKED is deliberately excluded: it can be a durable operator-facing wait whose
# coverage cells must still determine BLOCKED, PARTIAL_BLOCKED, or IN_PROGRESS truth.
_TERMINAL_EXECUTION = frozenset(
    {"COMPLETE", "COMPLETED", "SUCCESS", "FAILED", "CANCELLED", "CANCELED", "SKIPPED"}
)


def coverage_outcome(statuses: list[str]) -> str:
    values = [value.upper() for value in statuses]
    if not values or all(value in _SKIPPED for value in values):
        return "NOT_RUN"
    if any(value in _FAILED for value in values):
        return "FAILED"
    if all(value in _BLOCKED | _SKIPPED for value in values):
        return "BLOCKED"
    terminal = _PASSED | _BLOCKED | _FAILED | _SKIPPED | _FINDING
    if all(value in terminal for value in values):
        if any(value in _BLOCKED for value in values):
            return "PARTIAL_BLOCKED"
        if any(value in _FINDING for value in values):
            return "COMPLETED_WITH_FINDINGS"
        return "PASSED"
    return "IN_PROGRESS"


def truthful_campaign_status(execution_status: str, outcome: str) -> str:
    if execution_status.upper() not in _TERMINAL_EXECUTION:
        return execution_status
    if outcome == "IN_PROGRESS":
        return "BLOCKED"
    if outcome in {"NOT_RUN", "BLOCKED", "FAILED", "PARTIAL_BLOCKED"}:
        return outcome
    return execution_status


def normalized_coverage_status(execution_status: str, outcome: str) -> str:
    if execution_status.upper() in _TERMINAL_EXECUTION and outcome == "IN_PROGRESS":
        return "blocked"
    if outcome in {"BLOCKED", "PARTIAL_BLOCKED"}:
        return "blocked"
    if outcome == "FAILED":
        return "failed"
    if outcome == "NOT_RUN":
        return "skipped"
    if outcome in {"PASSED", "COMPLETED_WITH_FINDINGS"}:
        return "completed"
    return execution_status
