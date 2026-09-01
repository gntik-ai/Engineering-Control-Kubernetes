from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import NotificationOutbox
from engineering_control.notifier import NotificationService
from engineering_control.retention_limits import (
    NOTIFICATION_OUTBOX_METRIC_STATUSES,
    NOTIFICATION_OUTBOX_UNKNOWN_STATUS,
    NOTIFICATION_OUTBOX_WRITE_STATUSES,
)


def test_cleanup_limit_alert_uses_durable_notification_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "notification.token"
    token_file.write_text("synthetic-audit-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    service = NotificationService(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'notifier.sqlite'}",
            notification_sink_url="https://notification.invalid/events",
            notification_token_file=token_file,
        )
    )
    Base.metadata.create_all(service.engine)
    now = datetime.now(UTC)
    with Session(service.engine) as session, session.begin():
        session.add(
            NotificationOutbox(
                topic="alerts.dashboard-session-revocation-cleanup-truncated",
                status="PENDING",
                payload_json={
                    "workflow_id": "synthetic-cleanup",
                    "severity": "warning",
                    "reason": "SESSION_REVOCATION_CLEANUP_LIMIT_EXCEEDED",
                },
                attempts=0,
                next_attempt_at=now,
                delivered_at=None,
                last_error_code=None,
            )
        )

    requests: list[dict[str, Any]] = []

    def deliver(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "url": str(request.url),
                "authorization": request.headers.get("authorization"),
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(deliver))
    monkeypatch.setattr("engineering_control.notifier.httpx.Client", lambda timeout: client)

    assert service.run_once() == 1
    assert requests == [
        {
            "url": "https://notification.invalid/events",
            "authorization": "Bearer synthetic-audit-token",
            "body": {
                "topic": "alerts.dashboard-session-revocation-cleanup-truncated",
                "payload": {
                    "workflow_id": "synthetic-cleanup",
                    "severity": "warning",
                    "reason": "SESSION_REVOCATION_CLEANUP_LIMIT_EXCEEDED",
                },
            },
        }
    ]
    with Session(service.engine) as session:
        alert = session.scalar(select(NotificationOutbox))
        assert alert is not None
        assert alert.status == "DELIVERED"
        assert alert.delivered_at is not None
        assert alert.attempts == 0


def test_notification_failure_remains_pending_with_bounded_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "notification.token"
    token_file.write_text("synthetic-audit-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    service = NotificationService(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'notifier-failure.sqlite'}",
            notification_sink_url="https://notification.invalid/events",
            notification_token_file=token_file,
        )
    )
    Base.metadata.create_all(service.engine)
    now = datetime.now(UTC)
    with Session(service.engine) as session, session.begin():
        session.add(
            NotificationOutbox(
                topic="alerts.dashboard-session-revocation-cleanup-truncated",
                status="PENDING",
                payload_json={"workflow_id": "synthetic-cleanup"},
                attempts=0,
                next_attempt_at=now,
                delivered_at=None,
                last_error_code=None,
            )
        )

    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(503)))
    monkeypatch.setattr("engineering_control.notifier.httpx.Client", lambda timeout: client)

    assert service.run_once() == 0
    with Session(service.engine) as session:
        alert = session.scalar(select(NotificationOutbox))
        assert alert is not None
        assert alert.status == "PENDING"
        assert alert.attempts == 1
        assert alert.last_error_code == "SINK_HTTP_STATUS_ERROR"
        next_attempt_at = alert.next_attempt_at
        if next_attempt_at.tzinfo is None:
            next_attempt_at = next_attempt_at.replace(tzinfo=UTC)
        assert next_attempt_at > now
        assert next_attempt_at <= now + timedelta(seconds=5)


def test_notification_transport_failure_remains_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "notification.token"
    token_file.write_text("synthetic-audit-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    service = NotificationService(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'notifier-transport.sqlite'}",
            notification_sink_url="https://notification.invalid/events",
            notification_token_file=token_file,
        )
    )
    Base.metadata.create_all(service.engine)
    now = datetime.now(UTC)
    with Session(service.engine) as session, session.begin():
        session.add(
            NotificationOutbox(
                topic="alerts.example",
                status="PENDING",
                payload_json={"safe": True},
                attempts=0,
                next_attempt_at=now,
                delivered_at=None,
                last_error_code=None,
            )
        )

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic transport unavailable", request=request)

    client = httpx.Client(transport=httpx.MockTransport(unavailable))
    monkeypatch.setattr("engineering_control.notifier.httpx.Client", lambda timeout: client)

    assert service.run_once() == 0
    with Session(service.engine) as session:
        alert = session.scalar(select(NotificationOutbox))
        assert alert is not None
        assert alert.status == "PENDING"
        assert alert.attempts == 1
        assert alert.last_error_code == "SINK_TRANSPORT_ERROR"
        next_attempt_at = alert.next_attempt_at
        if next_attempt_at.tzinfo is None:
            next_attempt_at = next_attempt_at.replace(tzinfo=UTC)
        assert next_attempt_at > now
        assert next_attempt_at <= now + timedelta(seconds=5)


def test_notifier_writable_status_domain_is_metric_bounded() -> None:
    assert NOTIFICATION_OUTBOX_WRITE_STATUSES == ("PENDING", "DELIVERED")
    assert NOTIFICATION_OUTBOX_UNKNOWN_STATUS == "UNKNOWN"
    assert NOTIFICATION_OUTBOX_METRIC_STATUSES == ("PENDING", "DELIVERED", "UNKNOWN")
