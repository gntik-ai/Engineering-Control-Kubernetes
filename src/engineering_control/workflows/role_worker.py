from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from engineering_control.activities.core import ControlActivities
from engineering_control.adapters.run_temp import (
    clear_run_temporaries,
    live_run_temporaries,
    retry_unremoved,
    wait_for_removals,
)
from engineering_control.config import Settings, get_settings
from engineering_control.logging import configure_logging
from engineering_control.observability.heartbeat import WorkerHeartbeatPublisher
from engineering_control.workflows import tmp_sweep
from engineering_control.workflows.supervision import run_worker_until_signal
from engineering_control.workflows.worker_registry import (
    PROVIDER_WORKER_KINDS,
    ROLE_TASK_QUEUES,
    RoleWorkerSpec,
    role_worker_spec,
    worker_deployment_kwargs,
)

QUEUES = ROLE_TASK_QUEUES
LOGGER = logging.getLogger(__name__)


def worker_slots(settings: Settings, spec: RoleWorkerSpec) -> int:
    """NEW-226: a provider worker's concurrent activities are set per deployment."""
    if spec.kind == "codex":
        return settings.codex_worker_slots
    if spec.kind == "claude":
        return settings.claude_worker_slots
    return spec.max_concurrent_activities


def sweep_temporaries(settings: Settings, *, protected: frozenset[Path]) -> None:
    """NEW-226: one pass of the private /tmp sweep and of the run-directory retries."""
    retry_unremoved()
    if not tmp_sweep.is_private_tmp():
        return
    result = tmp_sweep.sweep(
        Path("/tmp"),  # noqa: S108 - the unit's PrivateTmp, checked just above
        idle_seconds=settings.private_tmp_sweep_idle_minutes * 60,
        protected=protected,
    )
    if result.removed or result.failed:
        LOGGER.info("private /tmp sweep; removed=%s failed=%s", result.removed, result.failed)


async def _sweep_forever(settings: Settings) -> None:
    while True:
        try:
            # Read here, in the event loop: the sweep thread never sees it change.
            await asyncio.to_thread(sweep_temporaries, settings, protected=live_run_temporaries())
        except Exception:  # noqa: BLE001 - a backstop never stops the worker
            LOGGER.exception("private /tmp sweep failed")
        await asyncio.sleep(settings.private_tmp_sweep_interval_seconds)


async def serve() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    spec = role_worker_spec(settings.worker_kind)
    if spec.kind in PROVIDER_WORKER_KINDS:
        # NEW-226: no run has started yet, so every run directory left is stale.
        try:
            clear_run_temporaries(settings.temporary_root / spec.kind)
        except (OSError, ValueError):
            LOGGER.exception("stale run temporary directories not removed")
    if not tmp_sweep.is_private_tmp():
        LOGGER.warning("private /tmp sweep disabled: /tmp is not a systemd PrivateTmp")
    client = await Client.connect(settings.temporal_target, namespace=settings.temporal_namespace)
    activities = ControlActivities(settings, spec.kind)
    worker = Worker(
        client,
        task_queue=spec.task_queue,
        workflows=[],
        activities=[activities.execute_control_step],
        max_concurrent_activities=worker_slots(settings, spec),
        **worker_deployment_kwargs(settings.build_id),
    )
    heartbeat = WorkerHeartbeatPublisher(settings, f"{spec.kind}-runner")
    heartbeat_task = asyncio.create_task(heartbeat.serve())
    sweep_task = asyncio.create_task(_sweep_forever(settings))
    try:
        await run_worker_until_signal(worker)
    finally:
        for task in (heartbeat_task, sweep_task):
            task.cancel()
        await asyncio.gather(heartbeat_task, sweep_task, return_exceptions=True)
        # NEW-226: within TimeoutStopSec; what is left is cleared at the next start.
        await asyncio.to_thread(wait_for_removals, 10.0)


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
