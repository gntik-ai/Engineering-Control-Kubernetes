from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from engineering_control.db.base import Base
from engineering_control.db.models import DashboardSessionRevocation, NotificationOutbox
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.observability.metrics import ControlPlaneMetrics
from engineering_control.retention_limits import (
    DASHBOARD_SESSION_REVOCATION_CLEANUP_LIMIT,
    NOTIFICATION_OUTBOX_METRIC_STATUSES,
)


def test_revocation_and_notification_backlog_metrics_are_postgresql_derived(
    tmp_path: Path,
) -> None:
    repository = SQLMetadataRepository(f"sqlite:///{tmp_path / 'metrics.sqlite'}")
    Base.metadata.create_all(repository.engine)
    now = datetime.now(UTC)
    with repository.sessions.begin() as session:
        session.add_all(
            [
                DashboardSessionRevocation(
                    session_binding_hash="a" * 64,
                    password_version=1,
                    revoked_at=now - timedelta(days=2),
                    expires_at=now - timedelta(days=1),
                ),
                DashboardSessionRevocation(
                    session_binding_hash="b" * 64,
                    password_version=1,
                    revoked_at=now,
                    expires_at=now + timedelta(days=1),
                ),
                NotificationOutbox(
                    topic="alerts.example",
                    status="PENDING",
                    payload_json={"safe": True},
                    attempts=0,
                    next_attempt_at=now,
                    delivered_at=None,
                    last_error_code=None,
                ),
            ]
        )
    snapshot = repository._metrics_snapshot()
    assert snapshot["dashboard_session_revocations"] == [["active", 1], ["expired", 1]]
    assert snapshot["dashboard_session_revocation_cleanup_truncated"] == [[0]]
    assert DASHBOARD_SESSION_REVOCATION_CLEANUP_LIMIT == 10_000
    assert snapshot["notification_outbox"] == [
        ["PENDING", 1],
        ["DELIVERED", 0],
        ["UNKNOWN", 0],
    ]
    assert tuple(row[0] for row in snapshot["notification_outbox"]) == (
        NOTIFICATION_OUTBOX_METRIC_STATUSES
    )
    assert snapshot["notification_oldest_pending_age_seconds"][0][0] >= 0

    metrics = ControlPlaneMetrics()
    populated_snapshot = {
        **snapshot,
        "dashboard_session_revocation_cleanup_truncated": [[1]],
        "notification_oldest_pending_age_seconds": [[15.0]],
    }
    payload, _content_type = metrics.render(populated_snapshot, heartbeat_ttl_seconds=60)
    output = payload.decode()
    assert 'engineering_dashboard_session_revocations{state="active"} 1.0' in output
    assert 'engineering_dashboard_session_revocations{state="expired"} 1.0' in output
    assert "engineering_dashboard_session_revocation_cleanup_truncated 1.0" in output
    assert 'engineering_notification_outbox{status="PENDING"} 1.0' in output
    assert "engineering_notification_oldest_pending_age_seconds 15.0" in output

    empty_payload, _ = metrics.render({}, heartbeat_ttl_seconds=60)
    empty_output = empty_payload.decode()
    assert "engineering_dashboard_session_revocation_cleanup_truncated 0.0" in empty_output
    assert "engineering_notification_oldest_pending_age_seconds 0.0" in empty_output
