from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Iterable
from typing import Protocol

LOGGER = logging.getLogger(__name__)
SHUTDOWN_TIMEOUT_SECONDS = 30.0


class ShutdownWorker(Protocol):
    async def run(self) -> None: ...

    async def shutdown(self) -> None: ...


async def run_worker_until_signal(
    worker: ShutdownWorker,
    *,
    shutdown_signals: Iterable[signal.Signals] = (signal.SIGTERM, signal.SIGINT),
) -> None:
    """Translate service termination into an orderly Temporal worker shutdown.

    Temporal's ``Worker.run`` does not install a SIGTERM handler. Systemd uses
    SIGTERM by default, so an unhandled signal can orphan a native CLI process
    group and force the server to discover worker loss only by heartbeat timeout.
    """

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    installed: list[signal.Signals] = []
    for selected in shutdown_signals:
        try:
            loop.add_signal_handler(selected, stop.set)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(selected)
    if not installed:
        LOGGER.warning("Temporal worker signal supervision is unavailable")

    worker_task = asyncio.create_task(worker.run(), name="temporal-worker")
    stop_task = asyncio.create_task(stop.wait(), name="temporal-worker-stop")
    try:
        done, _pending = await asyncio.wait(
            {worker_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if worker_task in done:
            await worker_task
            return
        LOGGER.info("Temporal worker shutdown requested")
        await asyncio.wait_for(worker.shutdown(), timeout=SHUTDOWN_TIMEOUT_SECONDS)
        await worker_task
        LOGGER.info("Temporal worker drain complete")
    finally:
        stop_task.cancel()
        if not worker_task.done():
            worker_task.cancel()
        await asyncio.gather(stop_task, worker_task, return_exceptions=True)
        for selected in installed:
            loop.remove_signal_handler(selected)
