from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from engineering_control.activities.core import ControlActivities
from engineering_control.config import get_settings
from engineering_control.logging import configure_logging
from engineering_control.workflows.definitions import ALL_WORKFLOWS


async def serve() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    client = await Client.connect(settings.temporal_target, namespace=settings.temporal_namespace)
    activities = ControlActivities(settings)
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=ALL_WORKFLOWS,
        activities=[activities.execute_control_step],
        max_concurrent_workflow_tasks=50,
        max_concurrent_activities=8,
    )
    await worker.run()


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
