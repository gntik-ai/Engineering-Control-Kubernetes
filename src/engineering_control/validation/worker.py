from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from temporalio.worker import Worker

from engineering_control.config import get_settings
from engineering_control.logging import configure_logging
from engineering_control.temporal_connection import connect as connect_temporal
from engineering_control.validation.lifecycle import AcceptanceActivities
from engineering_control.workflows.supervision import run_worker_until_signal


async def serve() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.acceptance_enabled:
        raise RuntimeError("bounded acceptance worker is not enabled")
    root = settings.acceptance_root.resolve()
    temporary_root = settings.temporary_root.resolve()
    if root == temporary_root or not root.is_relative_to(temporary_root):
        raise ValueError("acceptance root must be a child of the configured temporary root")
    activities = AcceptanceActivities(root)
    client = await connect_temporal(settings)
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="acceptance-activity")
    worker = Worker(
        client,
        task_queue="engineering-acceptance",
        workflows=[],
        activities=activities.activities,
        activity_executor=executor,
        max_concurrent_activities=1,
    )
    try:
        await run_worker_until_signal(worker)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
