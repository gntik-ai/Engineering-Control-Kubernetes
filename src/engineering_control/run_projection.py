from __future__ import annotations

from datetime import UTC, datetime

TERMINAL_RUN_OUTCOMES = frozenset(
    {
        "BLOCKED",
        "CANCELLED",
        "CANCELED",
        "COMPLETE",
        "COMPLETED",
        "FAILED",
        "SKIPPED",
        "STOPPED",
        "STOPPED_AFTER_CURRENT",
        "SUCCESS",
        "TERMINATED",
        "TIMED_OUT",
        "EMPTY",
        "NOT_RUN",
        "DRY_RUN",
        "INCOMPLETE",
        "VERIFICATION_REQUIRED",
        "PARTIAL_BLOCKED",
        "COMPLETED_WITH_FINDINGS",
    }
)
_DURABLE_WAIT = frozenset(
    {
        "AWAITING_APPROVAL",
        "AWAITING_BUILD",
        "AWAITING_CI",
        "AWAITING_HUMAN",
        "EXTERNAL_WAIT",
        "PAUSED",
    }
)


def classify_run_projection(
    *,
    status: str,
    started_at: datetime | None,
    finished_at: datetime | None,
    updated_at: datetime,
    now: datetime,
    queued_stale_seconds: int = 900,
) -> str:
    normalized = status.upper()
    if finished_at is not None or normalized in TERMINAL_RUN_OUTCOMES:
        return "TERMINAL"
    if normalized in _DURABLE_WAIT:
        return "DURABLE_WAIT"
    observed = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=UTC)
    if (
        normalized in {"PLANNED", "QUEUED"}
        and started_at is None
        and (now - observed).total_seconds() > queued_stale_seconds
    ):
        return "ORPHANED_START_INTENT"
    return "ACTIVE_REQUIRES_TEMPORAL_CORRELATION"
