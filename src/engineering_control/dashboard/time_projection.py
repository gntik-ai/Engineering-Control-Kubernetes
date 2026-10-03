"""Pure duration projection for dashboard/execution views. No I/O."""

from datetime import UTC, datetime


def _normalize(value: object) -> tuple[datetime | None, bool]:
    if value is None:
        return None, True
    if not isinstance(value, datetime) or isinstance(value, bool):
        return None, False
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC), True
    return value.astimezone(UTC), True


def _result(duration: int | None, basis: str, finish: str) -> dict[str, int | str | None]:
    return {"duration_seconds": duration, "duration_basis": basis, "finish_time_status": finish}


def project_duration(
    start: datetime | None, end: datetime | None, *, running: bool, now: datetime
) -> dict[str, int | str | None]:
    if not isinstance(running, bool):
        raise ValueError("running must be a bool")

    start_n, start_ok = _normalize(start)
    end_n, end_ok = _normalize(end)
    now_n, now_ok = _normalize(now)

    if not (start_ok and end_ok and now_ok):
        return _result(None, "unknown", "not_recorded")

    if start_n is not None and end_n is not None:
        if end_n >= start_n:
            seconds = int((end_n - start_n).total_seconds())
            return _result(seconds, "recorded_interval", "recorded")
        return _result(None, "unknown", "recorded")

    if end_n is not None:
        return _result(None, "unknown", "recorded")

    if start_n is not None:
        if running:
            if now_n is None or now_n < start_n:
                return _result(None, "unknown", "pending")
            seconds = int((now_n - start_n).total_seconds())
            return _result(seconds, "running_since_recorded_start", "pending")
        return _result(None, "unknown", "not_recorded")

    return _result(None, "unknown", "not_recorded")
