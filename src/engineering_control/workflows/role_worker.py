from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from engineering_control.activities.core import ControlActivities
from engineering_control.config import get_settings
from engineering_control.logging import configure_logging
from engineering_control.observability.heartbeat import WorkerHeartbeatPublisher
from engineering_control.workflows.worker_registry import ROLE_TASK_QUEUES, role_worker_spec

QUEUES = ROLE_TASK_QUEUES


async def serve() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    spec = role_worker_spec(settings.worker_kind)
    client = await Client.connect(settings.temporal_target, namespace=settings.temporal_namespace)
    activities = ControlActivities(settings, spec.kind)
    worker = Worker(
        client,
        task_queue=spec.task_queue,
        workflows=[],
        activities=[activities.execute_control_step],
        max_concurrent_activities=spec.max_concurrent_activities,
    )
    heartbeat = WorkerHeartbeatPublisher(settings, f"{spec.kind}-runner")
    heartbeat_task = asyncio.create_task(heartbeat.serve())
    try:
        await worker.run()
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
