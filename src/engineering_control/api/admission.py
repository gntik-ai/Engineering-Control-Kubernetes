"""Startup-only private API maintenance boundary, not project authorization.

The mode is a reviewed service configuration value. No request can change it.
Persisted Temporal schedules, other processes and already executing work still
require the separate maintenance fence and drain procedure.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from engineering_control.operations.models import RefreshConnectionsCommand

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
        mode: Literal["normal", "synthetic-only", "diagnostics-only"],
        authenticate: Callable[..., Awaitable[str]],
    ) -> None:
        if mode not in {"normal", "synthetic-only", "diagnostics-only"}:
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
        if (
            self.mode == "diagnostics-only"
            and scope.get("method") == "POST"
            and scope.get("path") in {"/v1/commands", "/v1/observability/gateway-events"}
        ):
            await self._diagnostic_request(scope, receive, send)
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

    async def _diagnostic_request(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Authenticate before bounded body reading; never admit an arbitrary command."""
        headers = Headers(scope=scope)
        try:
            subject = await self.authenticate(
                authorization=headers.get("authorization"),
                x_engineering_actor=headers.get("x-engineering-actor"),
            )
        except HTTPException as error:
            await JSONResponse({"detail": error.detail}, status_code=error.status_code)(
                scope, receive, send
            )
            return
        gateway = scope["path"] == "/v1/observability/gateway-events"
        if (gateway and subject != "hermes-gateway") or (
            not gateway
            and subject != "engineering-ctl:hermes"
            and not subject.startswith("telegram:")
        ):
            await JSONResponse({"detail": "diagnostic channel is not allowed"}, status_code=403)(
                scope, receive, send
            )
            return
        body = bytearray()
        oversized = False
        try:
            async with asyncio.timeout(2):
                while True:
                    message = await receive()
                    if message["type"] != "http.request":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > 16 * 1024:
                        oversized = True
                        break
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await JSONResponse({"detail": "diagnostic request timed out"}, status_code=408)(
                scope, receive, send
            )
            return
        if oversized:
            await JSONResponse({"detail": "diagnostic request too large"}, status_code=413)(
                scope, receive, send
            )
            return
        if not gateway:
            try:

                def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
                    value: dict[str, object] = {}
                    for key, item in pairs:
                        if key in value:
                            raise ValueError("duplicate key")
                        value[key] = item
                    return value

                value = json.loads(body, object_pairs_hook=unique)
                if not isinstance(value, dict) or value.get("action") != "refresh_connections":
                    raise ValueError("explicit diagnostic action required")
                # This model rejects all extra fields, provider/repository/URL overrides,
                # unsupported scopes and every product/workflow action.
                RefreshConnectionsCommand.model_validate(value)
            except (ValueError, TypeError, RecursionError):
                await JSONResponse(
                    {"detail": "diagnostics-only admission permits refresh_connections only"},
                    status_code=503,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return
        # Replay exactly the bounded original bytes. Existing route-level authentication,
        # strict schema, idempotency key, gateway receipt and redaction checks still apply.
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
