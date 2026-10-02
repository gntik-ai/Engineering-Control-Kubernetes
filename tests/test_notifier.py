"""NEW-112a: the notifier signs gateway webhooks (HMAC-SHA256 V2) as release 2b678d9 did."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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

SINK = "http://127.0.0.1:8644/webhooks/engineering-alerts"
TOKEN = "s" * 40


def _token_file(tmp_path: Path, mode: int = 0o600, content: str = TOKEN) -> Path:
    path = tmp_path / "notification.secret"
    path.write_text(content + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": f"sqlite+pysqlite:///{tmp_path / 'notify.sqlite3'}",
        "notification_sink_url": SINK,
        "notification_token_file": _token_file(tmp_path),
    }
    values.update(overrides)
    return Settings(**values)


def _service_with_row(tmp_path: Path, **overrides: Any) -> NotificationService:
    service = NotificationService(_settings(tmp_path, **overrides))
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


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    real_client = httpx.Client

    def factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(notifier_module.httpx, "Client", factory)


def _rows(service: NotificationService) -> list[NotificationOutbox]:
    with Session(service.engine) as session:
        return list(session.scalars(select(NotificationOutbox)))


def test_outbox_delivery_is_hmac_v2_signed_and_acknowledged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "delivered"})

    _patch_transport(monkeypatch, gateway)
    service = _service_with_row(tmp_path)
    assert service.run_once() == 1

    request = seen[0]
    assert "authorization" not in request.headers
    timestamp = request.headers["X-Webhook-Timestamp"]
    expected = hmac.new(
        TOKEN.encode(), timestamp.encode() + b"." + request.content, hashlib.sha256
    ).hexdigest()
    # The gateway's generic V2 check: hex HMAC-SHA256 of "<timestamp>.<body>".
    assert request.headers["X-Webhook-Signature-V2"] == expected
    assert request.headers["X-Request-ID"].startswith("notification-")
    body = json.loads(request.content)
    assert body["topic"] == "telegram.engineering-operational-watchdog"
    assert body["payload"]["status"] == "FAILED_OR_MISSED"
    assert [row.status for row in _rows(service)] == ["DELIVERED"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": "Invalid signature"}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"status": "ignored"}),
    ],
)
def test_unacknowledged_delivery_stays_pending_with_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    _patch_transport(monkeypatch, lambda request: response)
    service = _service_with_row(tmp_path)
    assert service.run_once() == 0
    (row,) = _rows(service)
    assert row.status == "PENDING"
    assert row.attempts == 1
    assert row.last_error_code == "SINK_HTTP_ERROR"


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
        NotificationService(_settings(tmp_path, notification_sink_url=url))


@pytest.mark.parametrize(
    "url", ["http://sink.example.com/notify", "https://user:pw@sink.example.com/notify"]
)
def test_bearer_sink_requires_credential_free_https(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        NotificationService(
            _settings(tmp_path, notification_sink_url=url, notification_auth_mode="bearer")
        )


def test_bearer_mode_still_sends_a_bearer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []

    def sink(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    _patch_transport(monkeypatch, sink)
    service = _service_with_row(
        tmp_path,
        notification_sink_url="https://sink.example.com/notify",
        notification_auth_mode="bearer",
    )
    assert service.run_once() == 1
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert "X-Webhook-Signature-V2" not in seen[0].headers


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_owner_only_token_modes_are_accepted(tmp_path: Path, mode: int) -> None:
    service = NotificationService(_settings(tmp_path))
    assert service._token(_token_file(tmp_path, mode)) == TOKEN


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o604, 0o666])
def test_broad_token_modes_are_refused(tmp_path: Path, mode: int) -> None:
    service = NotificationService(_settings(tmp_path))
    with pytest.raises(PermissionError, match="too broad"):
        service._token(_token_file(tmp_path, mode))


def test_group_readable_token_must_be_root_owned(tmp_path: Path) -> None:
    # 0640 custody is valid only as root:<this service's group>; a test file owned by the
    # current (non-root) user must therefore be refused.
    if os.geteuid() == 0:
        pytest.skip("root owns every file it creates")
    service = NotificationService(_settings(tmp_path))
    with pytest.raises(PermissionError, match="group-readable"):
        service._token(_token_file(tmp_path, 0o640))


def test_token_symlinks_and_short_hmac_keys_are_refused(tmp_path: Path) -> None:
    service = NotificationService(_settings(tmp_path))
    target = _token_file(tmp_path)
    link = tmp_path / "link.secret"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="non-symlink"):
        service._token(link)
    with pytest.raises(ValueError, match="32 characters"):
        service._token(_token_file(tmp_path, content="short"))


def test_default_auth_mode_is_hmac() -> None:
    assert Settings().notification_auth_mode == "hmac-sha256-v2"
