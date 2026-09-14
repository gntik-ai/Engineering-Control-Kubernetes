"""Credential-free, bounded observation of the local API's admission state."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx

CACHE_SECONDS = 2.0
REQUEST_SECONDS = 1.0
MAX_BODY_BYTES = 4096


def unknown_admission() -> dict[str, Any]:
    return {
        "scope": "private-api-only",
        "status": "UNKNOWN",
        "mode": None,
        "pending_operation_dispatcher_running": None,
        "observed_at": None,
        "freshness_seconds": None,
        "source": "private-api-health",
        "detail": "Admission is not currently verified. Inventory is not execution approval.",
    }


class AdmissionReader:
    """No credentials, arbitrary URLs, redirects, proxy env or fixture fallback."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if host not in {"127.0.0.1", "::1", "localhost"} or not 1024 <= port <= 65535:
            raise ValueError("dashboard admission observation requires a loopback API")
        # Never resolve a hostname from DNS, environment or browser input.
        address = "[::1]" if host == "::1" else "127.0.0.1"
        self.url = f"http://{address}:{port}/health/ready"
        self.client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
            timeout=REQUEST_SECONDS,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )
        self._lock = asyncio.Lock()
        self._until = 0.0
        self._observed = 0.0
        self._value = unknown_admission()

    async def aclose(self) -> None:
        await self.client.aclose()

    async def read(self) -> dict[str, Any]:
        async with self._lock:
            now = time.monotonic()
            if now >= self._until:
                self._value = await self._fetch()
                self._observed = time.monotonic()
                self._until = self._observed + CACHE_SECONDS
            value = dict(self._value)
            if value["observed_at"] is not None:
                value["freshness_seconds"] = round(time.monotonic() - self._observed, 3)
            return value

    async def _fetch(self) -> dict[str, Any]:
        try:
            async with asyncio.timeout(REQUEST_SECONDS):
                async with self.client.stream("GET", self.url) as response:
                    if (
                        response.status_code != 200
                        or response.headers.get("Content-Encoding", "identity") != "identity"
                    ):
                        return unknown_admission()
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=MAX_BODY_BYTES + 1):
                        body.extend(chunk)
                        if len(body) > MAX_BODY_BYTES:
                            return unknown_admission()
            payload = json.loads(body)
            if not isinstance(payload, dict) or payload.get("status") != "ready":
                return unknown_admission()
            admission = payload.get("admission")
            if not isinstance(admission, dict) or set(admission) != {
                "scope",
                "mode",
                "pending_operation_dispatcher_running",
            }:
                return unknown_admission()
            mode = admission["mode"]
            running = admission["pending_operation_dispatcher_running"]
            if (
                admission["scope"] != "private-api-only"
                or mode not in ("normal", "synthetic-only", "diagnostics-only")
                or type(running) is not bool
                or (mode == "synthetic-only" and running)
            ):
                return unknown_admission()
            state = (
                "SYNTHETIC_ONLY"
                if mode == "synthetic-only"
                else ("DIAGNOSTICS_ONLY" if running else "DIAGNOSTICS_STOPPED")
                if mode == "diagnostics-only"
                else ("DISPATCHING" if running else "DISPATCHER_STOPPED")
            )
            return {
                "scope": "private-api-only",
                "status": state,
                "mode": mode,
                "pending_operation_dispatcher_running": running,
                "observed_at": datetime.now(UTC).isoformat(),
                "freshness_seconds": 0,
                "source": "private-api-health",
                "detail": "Private API only; per-action policy and approval still apply.",
            }
        except (httpx.HTTPError, TimeoutError, ValueError, RecursionError):
            # Do not persist URLs, exception text, response content or headers.
            return unknown_admission()
