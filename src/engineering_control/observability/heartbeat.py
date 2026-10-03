from __future__ import annotations

import asyncio
import ipaddress
import os
from urllib.parse import urlsplit, urlunsplit

import httpx
import structlog

from engineering_control.config import Settings

logger = structlog.get_logger()


class WorkerHeartbeatPublisher:
    def __init__(self, settings: Settings, identity: str) -> None:
        self.settings = settings
        self.identity = identity
        if settings.evidence_ingest_url is None:
            self.url = None
        else:
            parsed = urlsplit(settings.evidence_ingest_url)
            try:
                address = ipaddress.ip_address(parsed.hostname or "")
            except ValueError as error:
                raise ValueError("worker heartbeat requires an explicit private IP") from error
            if (
                parsed.scheme not in {"http", "https"}
                or not (address.is_private or address.is_loopback)
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path.rstrip("/") != "/v1/evidence"
            ):
                raise ValueError("worker heartbeat origin is not a private evidence ingest")
            self.url = urlunsplit((parsed.scheme, parsed.netloc, "/v1/workers/heartbeat", "", ""))

    @staticmethod
    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False)

    async def publish_once(self, *, client: httpx.AsyncClient | None = None) -> None:
        if self.url is None:
            if self.settings.environment == "production":
                raise ValueError("production worker heartbeat is not configured")
            return
        token = self.settings.read_evidence_ingest_token()
        owned_client = client is None
        active_client = self._client() if client is None else client
        try:
            response = await active_client.post(
                self.url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Engineering-Actor": self.identity,
                    "X-Worker-Hostname": os.uname().nodename,
                },
            )
            response.raise_for_status()
        finally:
            if owned_client:
                await active_client.aclose()

    async def serve(self) -> None:
        client: httpx.AsyncClient | None = None
        try:
            while True:
                try:
                    # Keep one TLS context/connection pool for this publisher's lifetime.
                    # Construct lazily inside the retry boundary and never cache credentials.
                    if self.url is not None and client is None:
                        client = self._client()
                    await self.publish_once(client=client)
                except Exception as error:  # noqa: BLE001 - health reporting must not kill work
                    logger.warning(
                        "worker_heartbeat_failed",
                        worker=self.identity,
                        error_type=type(error).__name__,
                    )
                await asyncio.sleep(self.settings.worker_heartbeat_seconds)
        finally:
            if client is not None:
                await client.aclose()
