from __future__ import annotations

from datetime import UTC, datetime
from typing import TypeGuard

_STATUSES = {
    "CONNECTED",
    "DEGRADED",
    "DISCONNECTED",
    "STALE",
    "BLOCKED_BY_POLICY",
    "NOT_CONFIGURED",
    "NOT_INSTALLED",
    "PRESENT",
}


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def projection_connection_status(
    *,
    status: str,
    checked_at: datetime | None,
    last_success_at: datetime | None,
    detail: object,
    now: datetime,
    interval_seconds: int = 30,
    cycle_seconds: int = 30,
    max_sweep_age_seconds: int = 300,
) -> str:
    if not _is_int(interval_seconds) or not (5 <= interval_seconds <= 300):
        raise ValueError("interval_seconds must be an int in 5..300")
    if not _is_int(cycle_seconds) or not (10 <= cycle_seconds <= 120):
        raise ValueError("cycle_seconds must be an int in 10..120")
    if not _is_int(max_sweep_age_seconds) or not (30 <= max_sweep_age_seconds <= 3600):
        raise ValueError("max_sweep_age_seconds must be an int in 30..3600")

    if not isinstance(checked_at, datetime) or not isinstance(now, datetime):
        return "DEGRADED"

    checked_utc = _to_utc(checked_at)
    now_utc = _to_utc(now)
    checked_age = (now_utc - checked_utc).total_seconds()
    if checked_age < -5 or checked_age > interval_seconds + cycle_seconds + 15:
        return "STALE"

    if not isinstance(status, str) or status not in _STATUSES:
        return "DEGRADED"
    if status != "CONNECTED":
        return status

    if not isinstance(detail, dict):
        return "DEGRADED"
    version = detail.get("version")
    if not _is_int(version) or version != 1:
        return "DEGRADED"
    error_count = detail.get("error_count")
    unresolved_count = detail.get("unresolved_count")
    if not _is_int(error_count) or error_count != 0:
        return "DEGRADED"
    if not _is_int(unresolved_count) or unresolved_count != 0:
        return "DEGRADED"

    last_sweep = detail.get("last_sweep")
    if not isinstance(last_sweep, dict):
        return "DEGRADED"
    if last_sweep.get("healthy") is not True:
        return "DEGRADED"

    processed = last_sweep.get("processed")
    repairs = last_sweep.get("repairs")
    unresolved = last_sweep.get("unresolved")
    errors = last_sweep.get("errors")
    for value in (processed, repairs, unresolved, errors):
        if not _is_int(value) or not 0 <= value <= 1_000_000_000:
            return "DEGRADED"
    if unresolved != 0 or errors != 0:
        return "DEGRADED"
    if not _is_int(repairs) or not _is_int(processed) or repairs > processed:
        return "DEGRADED"

    completed_raw = last_sweep.get("completed_at_utc")
    if not isinstance(completed_raw, str):
        return "DEGRADED"
    try:
        completed_dt = datetime.fromisoformat(completed_raw)
    except ValueError:
        return "DEGRADED"
    if completed_dt.tzinfo is None:
        return "DEGRADED"
    completed_utc = _to_utc(completed_dt)

    if not isinstance(last_success_at, datetime):
        return "DEGRADED"
    success_utc = _to_utc(last_success_at)

    if completed_utc != success_utc:
        return "DEGRADED"

    sweep_age = (now_utc - completed_utc).total_seconds()
    if sweep_age < -5 or sweep_age > max_sweep_age_seconds:
        return "STALE"

    return "CONNECTED"
