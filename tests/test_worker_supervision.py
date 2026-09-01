from __future__ import annotations

import asyncio
import signal

import pytest

from engineering_control.workflows.supervision import run_worker_until_signal


class _Worker:
    def __init__(self) -> None:
        self.running = asyncio.Event()
        self.stopped = asyncio.Event()
        self.shutdown_calls = 0

    async def run(self) -> None:
        self.running.set()
        await self.stopped.wait()

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.stopped.set()


@pytest.mark.asyncio
async def test_service_sigterm_requests_orderly_temporal_worker_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    callbacks: dict[signal.Signals, object] = {}

    def add(selected: signal.Signals, callback: object, *args: object) -> None:
        callbacks[selected] = callback

    removed: list[signal.Signals] = []
    monkeypatch.setattr(loop, "add_signal_handler", add)
    monkeypatch.setattr(loop, "remove_signal_handler", lambda value: removed.append(value))
    worker = _Worker()
    task = asyncio.create_task(run_worker_until_signal(worker))
    await worker.running.wait()

    callback = callbacks[signal.SIGTERM]
    assert callable(callback)
    callback()
    await asyncio.wait_for(task, timeout=1)

    assert worker.shutdown_calls == 1
    assert set(removed) == {signal.SIGTERM, signal.SIGINT}


@pytest.mark.asyncio
async def test_natural_worker_completion_does_not_request_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *_args: None)
    monkeypatch.setattr(loop, "remove_signal_handler", lambda *_args: None)
    worker = _Worker()
    worker.stopped.set()

    await run_worker_until_signal(worker)

    assert worker.shutdown_calls == 0
