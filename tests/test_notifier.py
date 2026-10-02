from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control import notifier as notifier_module
from engineering_control.config import Settings
from engineering_control.db import models  # noqa: F401
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


def test_notification_token_accepts_read_only_owner_mode_and_rejects_broad_mode(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "notification.token"
    token_file.write_text("fixture-hmac-secret-0123456789abcdef", encoding="utf-8")
    token_file.chmod(0o400)
    service = NotificationService(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'token.sqlite'}",
            notification_sink_url="http://127.0.0.1:8644/webhooks/engineering-alerts",
            notification_token_file=token_file,
        )
    )
    assert service.sink._token(token_file) == "fixture-hmac-secret-0123456789abcdef"
    token_file.chmod(0o644)
    with pytest.raises(PermissionError, match="too broad"):
        service.sink._token(token_file)


# NEW-112a (main) cases, kept alongside the 2b678d9 notifier tests.
NEW112A_SINK = "http://127.0.0.1:8644/webhooks/engineering-alerts"
NEW112A_TOKEN = "s" * 40


def _new112a_token_file(tmp_path: Path, mode: int = 0o600, content: str = NEW112A_TOKEN) -> Path:
    path = tmp_path / "notification.secret"
    path.write_text(content + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _new112a_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": f"sqlite+pysqlite:///{tmp_path / 'notify.sqlite3'}",
        "notification_sink_url": NEW112A_SINK,
        "notification_token_file": _new112a_token_file(tmp_path),
    }
    values.update(overrides)
    return Settings(**values)


def _new112a_service_with_row(tmp_path: Path, **overrides: Any) -> NotificationService:
    service = NotificationService(_new112a_settings(tmp_path, **overrides))
    Base.metadata.create_all(service.engine)
    with Session(service.engine) as session, session.begin():
        session.add(
            NotificationOutbox(
                topic="telegram.engineering-operational-watchdog",
                status="PENDING",
                attempts=0,
                next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
                payload_json={"summary": "cleanup failed", "status": "FAILED_OR_MISSED"},
            )
        )
    return service


def _new112a_patch_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    real_client = httpx.Client

    def factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(notifier_module.httpx, "Client", factory)


def _new112a_rows(service: NotificationService) -> list[NotificationOutbox]:
    with Session(service.engine) as session:
        return list(session.scalars(select(NotificationOutbox)))


def test_outbox_delivery_is_hmac_v2_signed_and_acknowledged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "delivered"})

    _new112a_patch_transport(monkeypatch, gateway)
    service = _new112a_service_with_row(tmp_path)
    assert service.run_once() == 1

    request = seen[0]
    assert "authorization" not in request.headers
    timestamp = request.headers["X-Webhook-Timestamp"]
    expected = hmac.new(
        NEW112A_TOKEN.encode(), timestamp.encode() + b"." + request.content, hashlib.sha256
    ).hexdigest()
    # The gateway's generic V2 check: hex HMAC-SHA256 of "<timestamp>.<body>".
    assert request.headers["X-Webhook-Signature-V2"] == expected
    # Merged with 2b678d9: the request ID is the outbox row ID (the deployed contract).
    (row,) = _new112a_rows(service)
    assert request.headers["X-Request-ID"] == str(row.id)
    body = json.loads(request.content)
    assert body["topic"] == "telegram.engineering-operational-watchdog"
    assert body["payload"]["status"] == "FAILED_OR_MISSED"
    assert [row.status for row in _new112a_rows(service)] == ["DELIVERED"]


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        # Merged with 2b678d9: status failures and unacknowledged deliveries are distinct.
        (httpx.Response(401, json={"error": "Invalid signature"}), "SINK_HTTP_STATUS_ERROR"),
        (httpx.Response(200, text="not json"), "SINK_TRANSPORT_ERROR"),
        (httpx.Response(200, json={"status": "ignored"}), "SINK_TRANSPORT_ERROR"),
    ],
)
def test_unacknowledged_delivery_stays_pending_with_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: httpx.Response, error_code: str
) -> None:
    _new112a_patch_transport(monkeypatch, lambda request: response)
    service = _new112a_service_with_row(tmp_path)
    assert service.run_once() == 0
    (row,) = _new112a_rows(service)
    assert row.status == "PENDING"
    assert row.attempts == 1
    assert row.last_error_code == error_code


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8644/webhooks/engineering-alerts",
        "http://example.com:8644/webhooks/engineering-alerts",
        "http://127.0.0.1:9000/webhooks/engineering-alerts",
        "http://127.0.0.1:8644/webhooks/other",
        "http://user:pw@127.0.0.1:8644/webhooks/engineering-alerts",
        "http://127.0.0.1:8644/webhooks/engineering-alerts?x=1",
    ],
)
def test_hmac_sink_must_be_the_loopback_gateway_route(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        NotificationService(_new112a_settings(tmp_path, notification_sink_url=url))


@pytest.mark.parametrize(
    "url", ["http://sink.example.com/notify", "https://user:pw@sink.example.com/notify"]
)
def test_bearer_sink_requires_credential_free_https(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        NotificationService(
            _new112a_settings(tmp_path, notification_sink_url=url, notification_auth_mode="bearer")
        )


def test_bearer_mode_still_sends_a_bearer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []

    def sink(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    _new112a_patch_transport(monkeypatch, sink)
    service = _new112a_service_with_row(
        tmp_path,
        notification_sink_url="https://sink.example.com/notify",
        notification_auth_mode="bearer",
    )
    assert service.run_once() == 1
    assert seen[0].headers["Authorization"] == f"Bearer {NEW112A_TOKEN}"
    assert "X-Webhook-Signature-V2" not in seen[0].headers


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_owner_only_token_modes_are_accepted(tmp_path: Path, mode: int) -> None:
    service = NotificationService(_new112a_settings(tmp_path))
    assert service.sink._token(_new112a_token_file(tmp_path, mode)) == NEW112A_TOKEN


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o604, 0o666])
def test_broad_token_modes_are_refused(tmp_path: Path, mode: int) -> None:
    service = NotificationService(_new112a_settings(tmp_path))
    with pytest.raises(PermissionError, match="too broad"):
        service.sink._token(_new112a_token_file(tmp_path, mode))


def test_group_readable_token_must_be_root_owned(tmp_path: Path) -> None:
    # 0640 custody is valid only as root:<this service's group>; a test file owned by the
    # current (non-root) user must therefore be refused.
    if os.geteuid() == 0:
        pytest.skip("root owns every file it creates")
    service = NotificationService(_new112a_settings(tmp_path))
    with pytest.raises(PermissionError, match="group-readable"):
        service.sink._token(_new112a_token_file(tmp_path, 0o640))


def test_token_symlinks_and_short_hmac_keys_are_refused(tmp_path: Path) -> None:
    service = NotificationService(_new112a_settings(tmp_path))
    target = _new112a_token_file(tmp_path)
    link = tmp_path / "link.secret"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="non-symlink"):
        service.sink._token(link)
    with pytest.raises(ValueError, match="32 characters"):
        service.sink._token(_new112a_token_file(tmp_path, content="short"))


def test_default_auth_mode_is_hmac() -> None:
    assert Settings().notification_auth_mode == "hmac-sha256-v2"
