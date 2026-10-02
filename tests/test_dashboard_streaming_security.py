from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest

from engineering_control.dashboard.events import sse_events, transcript_sse
from engineering_control.dashboard.streaming import RevocableStreamingResponse, StreamAuthorization


class FixtureEvents:
    async def recent_events(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"id": 1, "message": "inert first"}, {"id": 2, "message": "inert second"}]

    def execution_logs(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "items": [
                {"sequence": 1, "message": "inert first"},
                {"sequence": 2, "message": "inert second"},
            ]
        }


async def connected() -> bool:
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["activity", "transcript"])
async def test_default_authorization_window_rechecks_inside_buffered_batch(kind: str) -> None:
    allowed = True
    checks = 0

    async def authorized() -> bool:
        nonlocal checks
        checks += 1
        return allowed

    parameters = dict(disconnected=connected, authorized=authorized, maximum_iterations=1)
    repository = FixtureEvents()
    generator = (
        sse_events(repository, after_id=0, **parameters)
        if kind == "activity"
        else transcript_sse(repository, UUID(int=1), after_sequence=0, **parameters)
    )
    assert "inert first" in await anext(generator)
    allowed = False
    await asyncio.sleep(5.1)
    with pytest.raises(StopAsyncIteration):
        await anext(generator)
    assert checks == 2
    await generator.aclose()


@pytest.mark.asyncio
async def test_fetch_that_outlives_authorization_is_rechecked_before_payload() -> None:
    allowed = True

    async def authorized() -> bool:
        return allowed

    class SlowRepository:
        async def recent_events(self, **_kwargs: Any) -> list[dict[str, Any]]:
            nonlocal allowed
            await asyncio.sleep(0.02)
            allowed = False
            return [{"id": 1, "message": "must not leave the iterator"}]

    generator = sse_events(
        SlowRepository(),
        after_id=0,
        disconnected=connected,
        authorized=authorized,
        authorization_seconds=0.01,
        maximum_iterations=1,
    )
    assert [frame async for frame in generator] == []


@pytest.mark.asyncio
async def test_iterator_authorization_timeout_fails_closed() -> None:
    callback_cancelled = False

    async def blocked() -> bool:
        nonlocal callback_cancelled
        try:
            await asyncio.Event().wait()
        finally:
            callback_cancelled = True
        return True

    generator = sse_events(
        FixtureEvents(),
        after_id=0,
        disconnected=connected,
        authorized=blocked,
        maximum_iterations=1,
    )
    async with asyncio.timeout(2):
        assert [frame async for frame in generator] == []
    assert callback_cancelled


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["denied", "error", "timeout", "wrong-type"])
async def test_authorization_failure_is_sticky_and_bounded(failure: str) -> None:
    calls = 0

    async def authorized() -> bool:
        nonlocal calls
        calls += 1
        if failure == "error":
            raise RuntimeError("inert internal error must not become stream content")
        if failure == "timeout":
            await asyncio.Event().wait()
        if failure == "wrong-type":
            return "true"  # type: ignore[return-value]
        return False

    authorization = StreamAuthorization(authorized)
    async with asyncio.timeout(2):
        assert await authorization.refresh() is False
    assert authorization.revoked
    assert await authorization.check() is False
    assert await authorization.refresh() is False
    assert calls == 1


def scope(version: str) -> dict[str, Any]:
    return {"type": "http", "asgi": {"spec_version": version}, "method": "GET"}


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["2.0", "2.4"])
@pytest.mark.parametrize("blocked_at", ["iterator", "send", "authorization"])
async def test_response_closes_when_revoked_during_idle_fetch_or_backpressure(
    version: str,
    blocked_at: str,
) -> None:
    allowed = True
    first_payload = asyncio.Event()
    owned_iterator_closed = False
    payloads = []

    async def authorized() -> bool:
        if not allowed and blocked_at == "authorization":
            await asyncio.Event().wait()
        return allowed

    async def body() -> AsyncIterator[str]:
        nonlocal owned_iterator_closed
        try:
            yield "event: inert\ndata: first\n\n"
            await asyncio.Event().wait()
            yield "must never be sent"
        finally:
            owned_iterator_closed = True

    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            payloads.append(message["body"])
            first_payload.set()
            if blocked_at == "send":
                await asyncio.Event().wait()

    response = RevocableStreamingResponse(body(), StreamAuthorization(authorized))
    task = asyncio.create_task(response(scope(version), receive, send))
    try:
        async with asyncio.timeout(2):
            await first_payload.wait()
        allowed = False
        # Same maximum exposure target, not a loosened test to obtain green.
        async with asyncio.timeout(5):
            await task
        assert payloads == [b"event: inert\ndata: first\n\n"]
        assert owned_iterator_closed
        assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("dashboard-sse-")]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_normal_response_completes_and_joins_its_watch_task() -> None:
    async def allowed() -> bool:
        return True

    async def body() -> AsyncIterator[str]:
        yield "data: inert\n\n"

    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    sent = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    response = RevocableStreamingResponse(body(), StreamAuthorization(allowed))
    await response(scope("2.4"), receive, send)
    assert sent[0]["type"] == "http.response.start"
    assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("dashboard-sse-")]
