from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

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
            notification_auth_mode="bearer",
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
    monkeypatch.setattr("engineering_control.notifier.httpx.Client", lambda **_kwargs: client)

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
            notification_auth_mode="bearer",
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
    monkeypatch.setattr("engineering_control.notifier.httpx.Client", lambda **_kwargs: client)

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
            notification_auth_mode="bearer",
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
    monkeypatch.setattr("engineering_control.notifier.httpx.Client", lambda **_kwargs: client)

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


def test_hmac_v2_notification_is_signed_idempotent_and_redacted(tmp_path: Path) -> None:
    token_file = tmp_path / "notification.token"
    secret = "fixture-hmac-secret-0123456789abcdef"  # noqa: S105 - synthetic canary
    token_file.write_text(secret, encoding="utf-8")
    token_file.chmod(0o600)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, json={"status": "delivered"})

    service = NotificationService(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'hmac.sqlite'}",
            notification_sink_url="http://127.0.0.1:8644/webhooks/engineering-alerts",
            notification_token_file=token_file,
            notification_auth_mode="hmac-sha256-v2",
        ),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(respond), trust_env=False
        ),
    )
    Base.metadata.create_all(service.engine)
    row_id = uuid4()
    sensitive = "fixture-sensitive-value"
    with Session(service.engine) as session, session.begin():
        session.add(
            NotificationOutbox(
                id=row_id,
                topic="telegram.engineering-cleanup-failed",
                status="PENDING",
                payload_json={"error_code": "CLEANUP_ACTIVITY_FAILED", "token": sensitive},
                attempts=0,
                next_attempt_at=datetime.now(UTC),
                delivered_at=None,
                last_error_code=None,
            )
        )

    assert service.run_once() == 1
    assert service.run_once() == 0
    assert len(requests) == 1
    request = requests[0]
    assert request.headers["X-Request-ID"] == str(row_id)
    assert "Authorization" not in request.headers
    assert sensitive.encode() not in request.content
    timestamp = request.headers["X-Webhook-Timestamp"]
    expected = hmac.new(
        secret.encode(),
        timestamp.encode() + b"." + request.content,
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(request.headers["X-Webhook-Signature-V2"], expected)


def test_hmac_sink_is_loopback_only_and_token_is_not_a_symlink(tmp_path: Path) -> None:
    token_file = tmp_path / "notification.token"
    token_file.write_text("fixture-hmac-secret-0123456789abcdef", encoding="utf-8")
    token_file.chmod(0o600)
    with pytest.raises(ValueError, match="loopback"):
        NotificationService(
            Settings(
                database_url=f"sqlite:///{tmp_path / 'external.sqlite'}",
                notification_sink_url="https://example.invalid/engineering-alerts",
                notification_token_file=token_file,
                notification_auth_mode="hmac-sha256-v2",
            )
        )

    link = tmp_path / "link.token"
    link.symlink_to(token_file)
    service = NotificationService(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'symlink.sqlite'}",
            notification_sink_url="http://127.0.0.1:8644/webhooks/engineering-alerts",
            notification_token_file=link,
            notification_auth_mode="hmac-sha256-v2",
        )
    )
    Base.metadata.create_all(service.engine)
    with Session(service.engine) as session, session.begin():
        session.add(
            NotificationOutbox(
                topic="telegram.test",
                status="PENDING",
                payload_json={},
                attempts=0,
                next_attempt_at=datetime.now(UTC),
            )
        )
    with pytest.raises(ValueError, match="non-symlink"):
        service.run_once()


def test_default_auth_mode_cannot_send_a_token_to_a_remote_sink(tmp_path: Path) -> None:
    token_file = tmp_path / "notification.token"
    token_file.write_text("fixture-hmac-secret-0123456789abcdef", encoding="utf-8")
    token_file.chmod(0o600)

    with pytest.raises(ValueError, match="loopback"):
        NotificationService(
            Settings(
                database_url=f"sqlite:///{tmp_path / 'remote.sqlite'}",
                notification_sink_url="https://notification.invalid/events",
                notification_token_file=token_file,
            )
        )


def test_bearer_sink_rejects_cleartext_or_embedded_credentials(tmp_path: Path) -> None:
    token_file = tmp_path / "notification.token"
    token_file.write_text("fixture-bearer-token", encoding="utf-8")
    token_file.chmod(0o600)
    for url in (
        "http://notification.invalid/events",
        "https://user@notification.invalid/events",
        "https://notification.invalid/events?token=fixture",
    ):
        with pytest.raises(ValueError, match="credential-free HTTPS"):
            NotificationService(
                Settings(
                    database_url=f"sqlite:///{tmp_path / 'remote.sqlite'}",
                    notification_sink_url=url,
                    notification_token_file=token_file,
                    notification_auth_mode="bearer",
                )
            )


def test_hmac_sink_requires_explicit_delivery_acknowledgement(tmp_path: Path) -> None:
    token_file = tmp_path / "notification.token"
    token_file.write_text("fixture-hmac-secret-0123456789abcdef", encoding="utf-8")
    token_file.chmod(0o600)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json={"status": "ignored"})

    service = NotificationService(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'ack.sqlite'}",
            notification_sink_url="http://127.0.0.1:8644/webhooks/engineering-alerts",
            notification_token_file=token_file,
            notification_auth_mode="hmac-sha256-v2",
        ),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(respond), trust_env=False
        ),
    )
    Base.metadata.create_all(service.engine)
    with Session(service.engine) as session, session.begin():
        session.add(
            NotificationOutbox(
                topic="telegram.engineering-cleanup-failed",
                status="PENDING",
                payload_json={"error_code": "CLEANUP_ACTIVITY_FAILED"},
                attempts=0,
                next_attempt_at=datetime.now(UTC),
            )
        )
    assert service.run_once() == 0
    with Session(service.engine) as session:
        stored = session.scalar(select(NotificationOutbox))
        assert stored is not None
        assert stored.status == "PENDING"
        assert stored.attempts == 1
