import asyncio
from datetime import timedelta

import pytest

from engineering_control import scheduler
from engineering_control.operations.connection_status import (
    PROVIDER_FRESHNESS_SECONDS,
    PROVIDER_REFRESH_EXECUTION_TIMEOUT_SECONDS,
    PROVIDER_REFRESH_INTERVAL_SECONDS,
)
from engineering_control.operations.repository import _PROVIDER_HEALTH_STALE_AFTER
from engineering_control.scheduler import schedules
from engineering_control.workflows.inputs import CleanupInput, ConnectionsRefreshInput
from engineering_control.workflows.schedule_intervals import QUOTA_REFRESH_INTERVAL


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


def test_connections_refresh_is_scheduled_frequently_enough_to_stay_ahead_of_staleness() -> None:
    """NEW-05: connections decay to DEGRADED within about an hour of the last refresh, so the
    schedule needs a real margin under that, not just "some" periodic job.
    """
    entry = schedules("engineering-control")["engineering-connections-refresh"]

    assert entry.action.args == [ConnectionsRefreshInput()]
    assert entry.spec.intervals[0].every <= timedelta(minutes=20)
    assert entry.policy.pause_on_failure is True


def test_quota_refresh_schedule_stays_well_under_the_provider_health_staleness_threshold() -> None:
    """NEW-16, 2026-09-22: a hardcoded 5-minute staleness threshold against a 30-minute
    refresh schedule made solve_backlog/solve_issue's preflight fail ~83% of the time in
    production. operations/repository.py now derives its threshold from
    QUOTA_REFRESH_INTERVAL with a buffer -- this asserts the schedule actually configured
    here still has real margin under that threshold, not just that the two are related by
    construction (a future edit to either could still reintroduce the same bug).
    """
    entry = schedules("engineering-control")["engineering-quota-refresh"]
    scheduled_interval = entry.spec.intervals[0].every

    assert scheduled_interval == QUOTA_REFRESH_INTERVAL
    assert _PROVIDER_HEALTH_STALE_AFTER > scheduled_interval + timedelta(minutes=5)
    # Merged with 2b678d9: the managed provider refresh never pauses on failure.
    assert entry.policy.pause_on_failure is False


def test_provider_refresh_schedule_stays_inside_provider_freshness_window() -> None:
    refresh = schedules("engineering-control")["engineering-quota-refresh"]

    assert PROVIDER_REFRESH_INTERVAL_SECONDS <= PROVIDER_FRESHNESS_SECONDS - 60
    assert (
        PROVIDER_REFRESH_INTERVAL_SECONDS + PROVIDER_REFRESH_EXECUTION_TIMEOUT_SECONDS
        <= PROVIDER_FRESHNESS_SECONDS
    )
    assert refresh.spec.intervals[0].every == timedelta(seconds=PROVIDER_REFRESH_INTERVAL_SECONDS)
    assert refresh.action.execution_timeout == timedelta(
        seconds=PROVIDER_REFRESH_EXECUTION_TIMEOUT_SECONDS
    )
    assert refresh.policy.overlap is scheduler.ScheduleOverlapPolicy.SKIP
    assert refresh.policy.pause_on_failure is False
    assert refresh.state.paused is False


@pytest.mark.asyncio
async def test_existing_provider_schedule_is_reconciled_in_place_and_unpaused() -> None:
    updates: list[object] = []

    class Handle:
        async def describe(self) -> object:
            return object()

        async def update(self, callback: object) -> None:
            updates.append(callback(None))  # type: ignore[operator]

    class Client:
        def get_schedule_handle(self, _schedule_id: str) -> Handle:
            return Handle()

    desired = schedules("engineering-control")["engineering-quota-refresh"]
    await scheduler.ensure_schedule(Client(), "engineering-quota-refresh", desired)  # type: ignore[arg-type]

    assert len(updates) == 1
    update = updates[0]
    assert isinstance(update, scheduler.ScheduleUpdate)
    assert update.schedule is desired
    assert update.schedule.spec.intervals[0].every == timedelta(seconds=240)
    assert update.schedule.state.paused is False


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
