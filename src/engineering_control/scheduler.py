from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
)
from temporalio.service import RPCError, RPCStatusCode

from engineering_control.config import get_settings
from engineering_control.logging import configure_logging
from engineering_control.operations.connection_status import (
    PROVIDER_REFRESH_EXECUTION_TIMEOUT_SECONDS,
)
from engineering_control.temporal_connection import connect as connect_temporal
from engineering_control.workflows.inputs import (
    CleanupInput,
    ConnectionsRefreshInput,
    QuotaRefreshInput,
)
from engineering_control.workflows.names import WorkflowName
from engineering_control.workflows.schedule_intervals import (
    CONNECTIONS_REFRESH_INTERVAL,
    QUOTA_REFRESH_INTERVAL,
)

CONFIGURE_TIMEOUT_SECONDS = 30.0


def schedules(task_queue: str, *, cleanup_execution_enabled: bool = False) -> dict[str, Schedule]:
    return {
        "engineering-quota-refresh": Schedule(
            action=ScheduleActionStartWorkflow(
                WorkflowName.QUOTA_REFRESH.value,
                QuotaRefreshInput(),
                id="quota-refresh-scheduled",
                task_queue=task_queue,
                execution_timeout=timedelta(seconds=PROVIDER_REFRESH_EXECUTION_TIMEOUT_SECONDS),
            ),
            spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=QUOTA_REFRESH_INTERVAL)]),
            policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, pause_on_failure=False),
            state=ScheduleState(paused=False, note="managed provider readiness refresh"),
        ),
        "engineering-connections-refresh": Schedule(
            action=ScheduleActionStartWorkflow(
                WorkflowName.CONNECTIONS_REFRESH.value,
                ConnectionsRefreshInput(),
                id="connections-refresh-scheduled",
                task_queue=task_queue,
            ),
            spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=CONNECTIONS_REFRESH_INTERVAL)]),
            policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, pause_on_failure=True),
        ),
        "engineering-safe-cleanup": Schedule(
            action=ScheduleActionStartWorkflow(
                WorkflowName.CLEANUP.value,
                CleanupInput(dry_run=not cleanup_execution_enabled),
                id="cleanup-scheduled",
                task_queue=task_queue,
            ),
            spec=ScheduleSpec(
                intervals=[ScheduleIntervalSpec(every=timedelta(days=1), offset=timedelta(hours=3))]
            ),
            policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, pause_on_failure=True),
        ),
    }


async def ensure_schedule(client: Client, schedule_id: str, desired: Schedule) -> None:
    handle = client.get_schedule_handle(schedule_id)
    try:
        await handle.describe()
    except RPCError as error:
        if error.status is not RPCStatusCode.NOT_FOUND:
            raise
        await client.create_schedule(schedule_id, desired)
        return
    await handle.update(lambda _input: ScheduleUpdate(schedule=desired))


async def configure() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    async with asyncio.timeout(CONFIGURE_TIMEOUT_SECONDS):
        client = await connect_temporal(settings)
        for schedule_id, desired in schedules(
            settings.temporal_task_queue,
            cleanup_execution_enabled=settings.cleanup_execution_enabled,
        ).items():
            await ensure_schedule(client, schedule_id, desired)


def run() -> None:
    asyncio.run(configure())


if __name__ == "__main__":
    run()
