from __future__ import annotations

import asyncio
import signal

import pytest

from engineering_control.workflows import supervision
from engineering_control.workflows.supervision import run_worker_until_signal


class _Worker:
    def __init__(self) -> None:
        self.running = asyncio.Event()
        self.stopped = asyncio.Event()
        self.shutdown_calls = 0
        self.run_cancelled = False

    async def run(self) -> None:
        self.running.set()
        try:
            await self.stopped.wait()
        except asyncio.CancelledError:
            self.run_cancelled = True
            raise

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
    callback()
    await asyncio.wait_for(task, timeout=1)

    assert worker.shutdown_calls == 1
    assert set(removed) == {signal.SIGTERM, signal.SIGINT}


@pytest.mark.asyncio
async def test_signal_received_during_handler_install_still_drains_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    invoked = False

    def add(selected: signal.Signals, callback: object, *args: object) -> None:
        nonlocal invoked
        if selected is signal.SIGTERM and not invoked:
            invoked = True
            assert callable(callback)
            callback()

    monkeypatch.setattr(loop, "add_signal_handler", add)
    monkeypatch.setattr(loop, "remove_signal_handler", lambda *_args: None)
    worker = _Worker()

    await asyncio.wait_for(run_worker_until_signal(worker), timeout=1)

    assert worker.shutdown_calls == 1
    assert worker.run_cancelled is False


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


@pytest.mark.asyncio
async def test_outer_cancellation_collects_the_worker_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *_args: None)
    monkeypatch.setattr(loop, "remove_signal_handler", lambda *_args: None)
    worker = _Worker()
    task = asyncio.create_task(run_worker_until_signal(worker))
    await worker.running.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert worker.run_cancelled is True


@pytest.mark.asyncio
async def test_shutdown_deadline_collects_worker_and_removes_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    callbacks: dict[signal.Signals, object] = {}
    removed: list[signal.Signals] = []
    shutdown_cancelled = asyncio.Event()

    class SlowWorker(_Worker):
        async def shutdown(self) -> None:
            self.shutdown_calls += 1
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                shutdown_cancelled.set()
                raise

    monkeypatch.setattr(
        loop,
        "add_signal_handler",
        lambda selected, callback, *_args: callbacks.__setitem__(selected, callback),
    )
    monkeypatch.setattr(loop, "remove_signal_handler", lambda value: removed.append(value))
    monkeypatch.setattr(supervision, "SHUTDOWN_TIMEOUT_SECONDS", 0.01)
    worker = SlowWorker()
    task = asyncio.create_task(run_worker_until_signal(worker))
    await worker.running.wait()
    callback = callbacks[signal.SIGTERM]
    assert callable(callback)
    callback()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task, timeout=1)

    assert shutdown_cancelled.is_set()
    assert worker.run_cancelled is True
    assert worker.shutdown_calls == 1
    assert set(removed) == {signal.SIGTERM, signal.SIGINT}
