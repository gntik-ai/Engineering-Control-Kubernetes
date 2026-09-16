from datetime import UTC, datetime, timedelta, timezone

import pytest

from engineering_control.dashboard.time_projection import project_duration

NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)


def test_recorded_interval_with_utc_offset():
    start = datetime(2026, 9, 16, 4, 0, tzinfo=timezone(timedelta(hours=-5)))
    end = datetime(2026, 9, 16, 5, 30, tzinfo=timezone(timedelta(hours=-5)))
    r = project_duration(start, end, running=False, now=NOW)
    assert r == {
        "duration_seconds": 5400,
        "duration_basis": "recorded_interval",
        "finish_time_status": "recorded",
    }


def test_naive_datetime_treated_as_utc():
    start = datetime(2026, 9, 16, 10, 0)
    end = datetime(2026, 9, 16, 11, 0)
    r = project_duration(start, end, running=False, now=NOW)
    assert r == {
        "duration_seconds": 3600,
        "duration_basis": "recorded_interval",
        "finish_time_status": "recorded",
    }


def test_missing_both_start_and_end():
    r = project_duration(None, None, running=False, now=NOW)
    assert r == {
        "duration_seconds": None,
        "duration_basis": "unknown",
        "finish_time_status": "not_recorded",
    }


def test_terminal_missing_end_not_running():
    start = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
    r = project_duration(start, None, running=False, now=NOW)
    assert r == {
        "duration_seconds": None,
        "duration_basis": "unknown",
        "finish_time_status": "not_recorded",
    }


def test_future_start_running_is_unknown_pending():
    start = NOW + timedelta(hours=1)
    r = project_duration(start, None, running=True, now=NOW)
    assert r == {
        "duration_seconds": None,
        "duration_basis": "unknown",
        "finish_time_status": "pending",
    }


def test_inverted_interval_returns_unknown_but_recorded():
    start = datetime(2026, 9, 16, 11, 0, tzinfo=UTC)
    end = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
    r = project_duration(start, end, running=False, now=NOW)
    assert r == {
        "duration_seconds": None,
        "duration_basis": "unknown",
        "finish_time_status": "recorded",
    }


def test_zero_duration_recorded_interval_stays_zero():
    t = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
    r = project_duration(t, t, running=False, now=NOW)
    assert r == {
        "duration_seconds": 0,
        "duration_basis": "recorded_interval",
        "finish_time_status": "recorded",
    }


def test_known_end_without_start_is_unknown_but_recorded():
    end = datetime(2026, 9, 16, 11, 0, tzinfo=UTC)
    r = project_duration(None, end, running=False, now=NOW)
    assert r == {
        "duration_seconds": None,
        "duration_basis": "unknown",
        "finish_time_status": "recorded",
    }


def test_active_elapsed_running_since_start():
    start = NOW - timedelta(minutes=30)
    r = project_duration(start, None, running=True, now=NOW)
    assert r == {
        "duration_seconds": 1800,
        "duration_basis": "running_since_recorded_start",
        "finish_time_status": "pending",
    }


def test_finished_with_running_flag_true_uses_bounded_recorded_interval():
    start = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)
    end = datetime(2026, 9, 16, 9, 15, tzinfo=UTC)
    r = project_duration(start, end, running=True, now=NOW)
    assert r == {
        "duration_seconds": 4500,
        "duration_basis": "recorded_interval",
        "finish_time_status": "recorded",
    }


def test_missing_start_while_running_stays_unknown_not_zero():
    r = project_duration(None, None, running=True, now=NOW)
    assert r == {
        "duration_seconds": None,
        "duration_basis": "unknown",
        "finish_time_status": "not_recorded",
    }


def test_invalid_running_type_raises_value_error():
    with pytest.raises(ValueError):
        project_duration(None, None, running=1, now=NOW)


def test_invalid_start_type_returns_unknown_without_raising():
    r = project_duration("not-a-date", None, running=False, now=NOW)
    assert r == {
        "duration_seconds": None,
        "duration_basis": "unknown",
        "finish_time_status": "not_recorded",
    }
