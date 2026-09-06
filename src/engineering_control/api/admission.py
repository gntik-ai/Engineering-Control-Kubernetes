"""Startup-only private API maintenance boundary, not project authorization.

The mode is a reviewed service configuration value. No request can change it.
Persisted Temporal schedules, other processes and already executing work still
require the separate maintenance fence and drain procedure.
"""

from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

SYNTHETIC_POST_PATHS = frozenset(
    {
        "/v1/validation/lifecycle",
        "/v1/validation/observability",
        "/v1/validation/observability/reverse",
    }
)


class MaintenanceAdmission:
    def __init__(
        self,
        app: ASGIApp,
        *,
        mode: Literal["normal", "synthetic-only"],
        authenticate: Callable[..., Awaitable[str]],
    ) -> None:
        if mode not in {"normal", "synthetic-only"}:
            raise ValueError("unknown private API admission mode")
        self.app = app
        self.mode = mode
        self.authenticate = authenticate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.mode == "normal" or scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        allowed = scope.get("method") in {"GET", "HEAD"} or (
            scope.get("method") == "POST" and scope.get("path") in SYNTHETIC_POST_PATHS
        )
        if allowed:
            # Existing authentication, body schemas and acceptance_enabled checks
            # remain mandatory; this is not an authentication or provider bypass.
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        try:
            await self.authenticate(
                authorization=headers.get("authorization"),
                x_engineering_actor=headers.get("x-engineering-actor"),
            )
        except HTTPException as error:
            response = JSONResponse({"detail": error.detail}, status_code=error.status_code)
        else:
            response = JSONResponse(
                {"detail": "private API is restricted to synthetic acceptance during maintenance"},
                status_code=503,
                headers={"Retry-After": "60", "Cache-Control": "no-store"},
            )
        # No request-body read, route dependency, command claim or projection
        # write takes place on this denied path.
        await response(scope, receive, send)
