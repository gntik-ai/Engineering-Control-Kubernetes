from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from engineering_control.config import Settings, get_settings
from engineering_control.db.models import NotificationOutbox
from engineering_control.logging import configure_logging
from engineering_control.retention_limits import (
    NOTIFICATION_OUTBOX_DELIVERED_STATUS,
    NOTIFICATION_OUTBOX_PENDING_STATUS,
)
from engineering_control.security.redaction import SecretRedactor

logger = structlog.get_logger()


class NotificationSink:
    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self.settings = settings
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=10, trust_env=False))
        self._validate_sink()

    def _validate_sink(self) -> None:
        if not self.settings.notification_sink_url:
            return
        if not self.settings.notification_token_file:
            raise ValueError("notification sink requires a token file")
        parsed = urlsplit(self.settings.notification_sink_url)
        credential_free = (
            parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
        if self.settings.notification_auth_mode == "bearer":
            if parsed.scheme != "https" or not credential_free:
                raise ValueError("bearer notification sink must use credential-free HTTPS")
            return
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.port != 8644
            or parsed.path != "/webhooks/engineering-alerts"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("HMAC notification sink must be a credential-free loopback HTTP URL")

    def _token(self, path: Path) -> str:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("notification token path must be a non-symlink regular file")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("notification token path must remain a regular file")
            if (metadata.st_dev, metadata.st_ino) != (before.st_dev, before.st_ino):
                raise RuntimeError("notification token file changed during protected read")
            mode = stat.S_IMODE(metadata.st_mode)
            if mode in {0o400, 0o600}:
                if metadata.st_uid not in {0, os.geteuid()}:
                    raise PermissionError("notification token owner is not trusted")
            elif mode in {0o440, 0o640}:
                if metadata.st_uid != 0 or metadata.st_gid != os.getegid():
                    raise PermissionError("group-readable notification token custody is invalid")
            else:
                raise PermissionError("notification token file permissions are too broad")
            raw = os.read(descriptor, 4097)
        finally:
            os.close(descriptor)
        if len(raw) > 4096:
            raise ValueError("notification token is too large")
        token = raw.decode("utf-8").strip()
        if self.settings.notification_auth_mode == "hmac-sha256-v2" and len(token) < 32:
            raise ValueError("HMAC notification token must contain at least 32 characters")
        return token

    def _request(
        self,
        *,
        topic: str,
        request_id: str,
        token: str,
        payload: dict[str, Any],
    ) -> tuple[bytes, dict[str, str]]:
        body = json.dumps(
            {"topic": topic, "payload": payload},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Request-ID": request_id}
        if self.settings.notification_auth_mode == "hmac-sha256-v2":
            timestamp = str(int(datetime.now(UTC).timestamp()))
            signature = hmac.new(
                token.encode("utf-8"),
                timestamp.encode("ascii") + b"." + body,
                hashlib.sha256,
            ).hexdigest()
            headers.update(
                {
                    "X-Webhook-Timestamp": timestamp,
                    "X-Webhook-Signature-V2": signature,
                }
            )
        else:
            headers["Authorization"] = f"Bearer {token}"
        return body, headers

    def deliver(
        self,
        *,
        topic: str,
        request_id: str,
        payload: dict[str, Any],
        client: httpx.Client | None = None,
    ) -> None:
        if not self.settings.notification_sink_url:
            raise RuntimeError("notification sink is disabled")
        if not self.settings.notification_token_file:
            raise RuntimeError("notification token configuration changed after validation")
        if not re.fullmatch(r"[a-z][a-z0-9.-]{0,119}", topic):
            raise ValueError("notification topic is outside its bounded syntax")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", request_id):
            raise ValueError("notification request ID is outside its bounded syntax")
        token = self._token(self.settings.notification_token_file)
        body, headers = self._request(
            topic=topic,
            request_id=request_id,
            token=token,
            payload=payload,
        )
        owned_client = client is None
        active_client = client or self.client_factory()
        try:
            response = active_client.post(
                self.settings.notification_sink_url,
                headers=headers,
                content=body,
            )
            response.raise_for_status()
            if self.settings.notification_auth_mode == "hmac-sha256-v2":
                try:
                    acknowledgement = response.json()
                except ValueError as error:
                    raise httpx.HTTPError(
                        "notification sink returned a malformed acknowledgement"
                    ) from error
                if not isinstance(acknowledgement, dict) or acknowledgement.get("status") not in {
                    "delivered",
                    "duplicate",
                }:
                    raise httpx.HTTPError("notification sink did not acknowledge delivery")
        finally:
            if owned_client:
                active_client.close()


class NotificationService:
    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self.settings = settings
        self.engine = create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
        self.redactor = SecretRedactor()
        self.sink = NotificationSink(settings, client_factory=client_factory)

    def run_once(self) -> int:
        if not self.settings.notification_sink_url:
            return 0
        if not self.settings.notification_token_file:
            raise RuntimeError("notification token configuration changed after validation")
        delivered = 0
        with Session(self.engine) as session, session.begin():
            rows = session.scalars(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.status == NOTIFICATION_OUTBOX_PENDING_STATUS,
                    NotificationOutbox.next_attempt_at <= datetime.now(UTC),
                )
                .order_by(NotificationOutbox.created_at)
                .limit(20)
                .with_for_update(skip_locked=True)
            ).all()
            with self.sink.client_factory() as client:
                for row in rows:
                    payload: dict[str, Any] = self.redactor.redact(row.payload_json).value
                    try:
                        self.sink.deliver(
                            topic=row.topic,
                            # Main's (NEW-112a) deployed idempotency key: a delivery whose
                            # acknowledgement was lost is re-sent under the same key.
                            request_id=f"notification-{row.id}",
                            payload=payload,
                            client=client,
                        )
                    except httpx.HTTPError as error:
                        row.attempts += 1
                        row.last_error_code = (
                            "SINK_HTTP_STATUS_ERROR"
                            if isinstance(error, httpx.HTTPStatusError)
                            else "SINK_TRANSPORT_ERROR"
                        )
                        row.next_attempt_at = datetime.now(UTC) + timedelta(
                            seconds=min(3600, 2 ** min(row.attempts, 10))
                        )
                    else:
                        row.status = NOTIFICATION_OUTBOX_DELIVERED_STATUS
                        row.delivered_at = datetime.now(UTC)
                        delivered += 1
        return delivered


async def serve() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    service = NotificationService(settings)
    while True:
        try:
            await asyncio.to_thread(service.run_once)
        except Exception as error:  # noqa: BLE001 - persistent service retries safely
            logger.error("notification_poll_failed", error_type=type(error).__name__)
        await asyncio.sleep(settings.notification_poll_seconds)


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
