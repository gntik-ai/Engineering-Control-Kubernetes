from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from engineering_control.activities.core import ControlActivities
from engineering_control.config import get_settings
from engineering_control.logging import configure_logging

QUEUES = {
    "codex": "engineering-codex",
    "claude": "engineering-claude",
    "browser": "engineering-browser",
    "scan": "engineering-scan",
    "release": "engineering-release",
}


async def serve() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.worker_kind not in QUEUES:
        raise ValueError("ENGINEERING_WORKER_KIND must name a bounded worker role")
    client = await Client.connect(settings.temporal_target, namespace=settings.temporal_namespace)
    activities = ControlActivities(settings, settings.worker_kind)
    worker = Worker(
        client,
        task_queue=QUEUES[settings.worker_kind],
        workflows=[],
        activities=[activities.execute_control_step],
        max_concurrent_activities=2 if settings.worker_kind in {"codex", "claude"} else 4,
    )
    await worker.run()


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
