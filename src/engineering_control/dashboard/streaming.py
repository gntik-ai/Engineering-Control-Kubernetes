"""Bounded stream authorization, including an idle or backpressured response."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

AUTHORIZATION_TIMEOUT_SECONDS = 1.0
SEND_TIMEOUT_SECONDS = 1.0


class StreamAuthorization:
    """A short process-local observation lease; never revives a denied stream."""

    def __init__(self, authorized: Callable[[], Awaitable[bool]]) -> None:
        self.authorized = authorized
        self.valid_until = 0.0
        self.revoked = False
        self.lock = asyncio.Lock()

    async def check(self, *, force: bool = False) -> bool:
        if self.revoked:
            return False
        loop = asyncio.get_running_loop()
        if not force and loop.time() < self.valid_until:
            return True
        try:
            async with asyncio.timeout(AUTHORIZATION_TIMEOUT_SECONDS):
                async with self.lock:
                    if self.revoked:
                        return False
                    if not force and loop.time() < self.valid_until:
                        return True
                    started = loop.time()
                    allowed = await self.authorized()
                    # Query time belongs to the lease, rather than extending it.
                    self.valid_until = started + 1.0
                    if allowed is not True or loop.time() >= self.valid_until:
                        self.revoked = True
        except Exception:  # noqa: BLE001 - unavailable authorization must fail closed
            self.revoked = True
        return not self.revoked

    async def refresh(self) -> bool:
        return await self.check(force=True)

    async def watch(self) -> None:
        loop = asyncio.get_running_loop()
        # A local Event cannot observe expiry or a revocation on another process.
        while await self.check():  # noqa: ASYNC110
            await asyncio.sleep(max(0.01, self.valid_until - loop.time()))


class _StreamRevoked(Exception):
    pass


class RevocableStreamingResponse(StreamingResponse):
    """Own and join a revocation watcher even while the iterator/send is stalled."""

    def __init__(self, content: AsyncIterator[str], authorization: StreamAuthorization) -> None:
        super().__init__(content, media_type="text/event-stream")
        self.authorization = authorization

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = False
        finished = False

        async def bounded_send(message: Message) -> None:
            nonlocal started, finished
            if message["type"] == "http.response.body" and message.get("body"):
                if not await self.authorization.check():
                    raise _StreamRevoked()
            async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                await send(message)
            if message["type"] == "http.response.start":
                started = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                finished = True

        stream = asyncio.create_task(
            super().__call__(scope, receive, bounded_send), name="dashboard-sse-response"
        )
        watcher = asyncio.create_task(
            self.authorization.watch(), name="dashboard-sse-authorization"
        )
        try:
            done, _pending = await asyncio.wait(
                (stream, watcher), return_when=asyncio.FIRST_COMPLETED
            )
            if stream in done:
                try:
                    await stream
                except (_StreamRevoked, TimeoutError, ClientDisconnect):
                    pass
        finally:
            for task in (stream, watcher):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stream, watcher, return_exceptions=True)
            closer = getattr(self.body_iterator, "aclose", None)
            if closer is not None:
                try:
                    async with asyncio.timeout(1):
                        await closer()
                except (TimeoutError, RuntimeError):
                    pass
            # Best-effort protocol completion; never block revocation indefinitely
            # behind a client that stopped reading. No new application payload.
            if started and not finished:
                try:
                    async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                        await send({"type": "http.response.body", "body": b"", "more_body": False})
                except (OSError, TimeoutError):
                    pass
