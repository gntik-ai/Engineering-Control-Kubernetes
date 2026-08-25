#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from uuid import uuid4

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.worker import Worker

from engineering_control.config import get_settings
from engineering_control.validation.github_write import (
    GitHubWriteDenialAcceptanceWorkflow,
    classify_write_denial,
)


async def validate() -> dict[str, object]:
    settings = get_settings()
    client = await Client.connect(
        settings.temporal_target,
        namespace=settings.temporal_namespace,
    )
    suffix = uuid4().hex
    workflow_id = f"github-write-denial-acceptance-{suffix}"
    task_queue = f"engineering-github-denial-acceptance-{suffix}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[GitHubWriteDenialAcceptanceWorkflow],
    ):
        handle = await client.start_workflow(
            GitHubWriteDenialAcceptanceWorkflow.run,
            id=workflow_id,
            task_queue=task_queue,
            execution_timeout=timedelta(minutes=3),
        )
        result = await handle.result()
    if not isinstance(result, dict):
        raise RuntimeError("GitHub write-denial workflow returned a non-object result")
    activity_starts = 0
    activity_failures = 0
    async for event in handle.fetch_history_events():
        event_name = EventType.Name(event.event_type)
        if event_name == "EVENT_TYPE_ACTIVITY_TASK_STARTED":
            activity_starts += 1
        elif event_name == "EVENT_TYPE_ACTIVITY_TASK_FAILED":
            activity_failures += 1
    if activity_failures:
        raise RuntimeError("GitHub write-denial activity failed instead of returning its result")
    acceptance = classify_write_denial(result, activity_starts=activity_starts)
    return {
        "status": acceptance,
        "workflow_id": workflow_id,
        "github_task_queue": settings.github_task_queue,
        "activity_starts": activity_starts,
        "activity_retries": activity_starts - 1,
        "external_command_count": result["external_command_count"],
        "external_mutation_attempt_count": result["external_mutation_attempt_count"],
        "mutating_command_construction_count": result["mutating_command_construction_count"],
        "reason_code": result["reason_code"],
    }


def main() -> None:
    print(json.dumps(asyncio.run(validate()), sort_keys=True))


if __name__ == "__main__":
    main()
