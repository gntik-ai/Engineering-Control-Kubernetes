from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import structlog
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from engineering_control.config import Settings, get_settings
from engineering_control.db.models import NotificationOutbox
from engineering_control.logging import configure_logging
from engineering_control.security.redaction import SecretRedactor

logger = structlog.get_logger()


class NotificationService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
        self.redactor = SecretRedactor()

    def _token(self, path: Path) -> str:
        metadata = path.stat()
        if metadata.st_mode & 0o077:
            raise PermissionError("notification token file permissions are too broad")
        return path.read_text(encoding="utf-8").strip()

    def run_once(self) -> int:
        if not self.settings.notification_sink_url:
            return 0
        if not self.settings.notification_token_file:
            raise ValueError("notification sink requires a token file")
        delivered = 0
        with Session(self.engine) as session, session.begin():
            rows = session.scalars(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.status == "PENDING",
                    NotificationOutbox.next_attempt_at <= datetime.now(UTC),
                )
                .order_by(NotificationOutbox.created_at)
                .limit(20)
                .with_for_update(skip_locked=True)
            ).all()
            token = self._token(self.settings.notification_token_file)
            with httpx.Client(timeout=10) as client:
                for row in rows:
                    payload: dict[str, Any] = self.redactor.redact(row.payload_json).value
                    try:
                        response = client.post(
                            self.settings.notification_sink_url,
                            headers={"Authorization": f"Bearer {token}"},
                            json={"topic": row.topic, "payload": payload},
                        )
                        response.raise_for_status()
                    except httpx.HTTPError:
                        row.attempts += 1
                        row.last_error_code = "SINK_HTTP_ERROR"
                        row.next_attempt_at = datetime.now(UTC) + timedelta(
                            seconds=min(3600, 2 ** min(row.attempts, 10))
                        )
                    else:
                        row.status = "DELIVERED"
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
