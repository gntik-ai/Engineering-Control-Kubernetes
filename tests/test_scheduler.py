import asyncio
from datetime import timedelta

import pytest

from engineering_control import scheduler
from engineering_control.operations.connection_status import (
    PROVIDER_FRESHNESS_SECONDS,
    PROVIDER_REFRESH_EXECUTION_TIMEOUT_SECONDS,
    PROVIDER_REFRESH_INTERVAL_SECONDS,
)
from engineering_control.scheduler import schedules
from engineering_control.workflows.inputs import CleanupInput


def test_periodic_cleanup_is_dry_run_unless_execution_is_explicitly_enabled() -> None:
    default = schedules("engineering-control")["engineering-safe-cleanup"]
    enabled = schedules("engineering-control", cleanup_execution_enabled=True)[
        "engineering-safe-cleanup"
    ]

    # Temporal Schedule actions expand this template with the scheduled UTC instant.
    assert default.action.id == "cleanup-scheduled"
    assert enabled.action.id == "cleanup-scheduled"
    assert default.action.args == [CleanupInput(dry_run=True)]
    assert enabled.action.args == [CleanupInput(dry_run=False)]


def test_provider_refresh_schedule_stays_inside_provider_freshness_window() -> None:
    refresh = schedules("engineering-control")["engineering-quota-refresh"]

    assert PROVIDER_REFRESH_INTERVAL_SECONDS <= PROVIDER_FRESHNESS_SECONDS - 60
    assert PROVIDER_REFRESH_EXECUTION_TIMEOUT_SECONDS < PROVIDER_REFRESH_INTERVAL_SECONDS
    assert refresh.spec.intervals[0].every == timedelta(seconds=PROVIDER_REFRESH_INTERVAL_SECONDS)
    assert refresh.action.execution_timeout == timedelta(
        seconds=PROVIDER_REFRESH_EXECUTION_TIMEOUT_SECONDS
    )
    assert refresh.policy.overlap is scheduler.ScheduleOverlapPolicy.SKIP
    assert refresh.policy.pause_on_failure is False


@pytest.mark.asyncio
async def test_schedule_registration_has_a_bounded_rpc_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never_connect(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(1)

    monkeypatch.setattr(scheduler, "CONFIGURE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(scheduler.Client, "connect", never_connect)

    with pytest.raises(TimeoutError):
        await scheduler.configure()
