from datetime import UTC, datetime, timedelta

import pytest

from engineering_control.operations.projection_health import projection_connection_status

NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
CHECKED = NOW - timedelta(seconds=5)
COMPLETED = NOW - timedelta(seconds=60)


def make_detail(**sweep_overrides):
    last_sweep = {
        "completed_at_utc": COMPLETED.isoformat(),
        "processed": 10,
        "repairs": 1,
        "unresolved": 0,
        "errors": 0,
        "healthy": True,
    }
    last_sweep.update(sweep_overrides)
    return {
        "version": 1,
        "cursor": None,
        "sweep_started_at_utc": (COMPLETED - timedelta(seconds=1)).isoformat(),
        "counts": {},
        "processed_count": 0,
        "repair_count": 0,
        "unresolved_count": 0,
        "error_count": 0,
        "last_sweep": last_sweep,
    }


def call(**overrides):
    kwargs = dict(
        status="CONNECTED",
        checked_at=CHECKED,
        last_success_at=COMPLETED,
        detail=make_detail(),
        now=NOW,
    )
    kwargs.update(overrides)
    return projection_connection_status(**kwargs)


def test_healthy_connected():
    assert call() == "CONNECTED"


def test_checked_at_stale_too_old():
    assert call(checked_at=NOW - timedelta(seconds=1000)) == "STALE"


def test_checked_at_stale_future():
    assert call(checked_at=NOW + timedelta(seconds=100)) == "STALE"


def test_partial_new_checked_old_success_stale():
    detail = make_detail(completed_at_utc=(NOW - timedelta(seconds=1000)).isoformat())
    assert (
        call(
            checked_at=NOW - timedelta(seconds=1),
            last_success_at=NOW - timedelta(seconds=1000),
            detail=detail,
        )
        == "STALE"
    )


def test_missing_last_sweep():
    detail = make_detail()
    del detail["last_sweep"]
    assert call(detail=detail) == "DEGRADED"


def test_invalid_counter_bool():
    assert call(detail=make_detail(processed=True)) == "DEGRADED"


def test_invalid_version_bool():
    detail = make_detail()
    detail["version"] = True
    assert call(detail=detail) == "DEGRADED"


def test_healthy_not_true():
    assert call(detail=make_detail(healthy=1)) == "DEGRADED"


def test_repairs_exceeds_processed():
    assert call(detail=make_detail(processed=1, repairs=5)) == "DEGRADED"


def test_completed_stale_too_old():
    old = NOW - timedelta(seconds=1000)
    assert (
        call(detail=make_detail(completed_at_utc=old.isoformat()), last_success_at=old) == "STALE"
    )


def test_completed_future():
    future = NOW + timedelta(seconds=100)
    assert (
        call(detail=make_detail(completed_at_utc=future.isoformat()), last_success_at=future)
        == "STALE"
    )


def test_mismatched_success():
    other = COMPLETED - timedelta(seconds=30)
    assert call(last_success_at=other) == "DEGRADED"


def test_utc_naive_db_accepted():
    naive_checked = CHECKED.replace(tzinfo=None)
    naive_now = NOW.replace(tzinfo=None)
    naive_completed = COMPLETED.replace(tzinfo=None)
    detail = make_detail(completed_at_utc=COMPLETED.isoformat())
    assert (
        call(
            checked_at=naive_checked, now=naive_now, last_success_at=naive_completed, detail=detail
        )
        == "CONNECTED"
    )


def test_exact_boundary_checked_age():
    interval, cycle = 30, 30
    boundary = NOW - timedelta(seconds=interval + cycle + 15)
    assert call(checked_at=boundary) == "CONNECTED"


def test_nondefault_policy():
    detail = make_detail(completed_at_utc=(NOW - timedelta(seconds=50)).isoformat())
    assert (
        call(
            checked_at=NOW - timedelta(seconds=2),
            last_success_at=NOW - timedelta(seconds=50),
            detail=detail,
            interval_seconds=10,
            cycle_seconds=10,
            max_sweep_age_seconds=60,
        )
        == "CONNECTED"
    )


def test_bad_config_raises():
    with pytest.raises(ValueError):
        call(interval_seconds=1)


def test_unknown_status_degraded():
    assert call(status="WEIRD") == "DEGRADED"


def test_non_connected_status_preserved():
    assert call(status="DISCONNECTED") == "DISCONNECTED"


def test_missing_checked_at_degraded():
    assert call(checked_at=None) == "DEGRADED"


def test_inputs_not_mutated():
    checked_copy = CHECKED
    call(checked_at=checked_copy)
    assert checked_copy == CHECKED
