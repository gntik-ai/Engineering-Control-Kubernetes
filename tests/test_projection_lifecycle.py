"""Owned API lifetime and existing watchdog delivery path, fixture evidence only."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from engineering_control.api.main import create_app
from engineering_control.config import Settings
from engineering_control.db.models import ConnectionSnapshot
from engineering_control.db.repository import EmptyMetadataRepository
from engineering_control.watchdog import OperationalWatchdog


@pytest.mark.parametrize(
    "enabled,mode,expected",
    [
        (False, "normal", False),
        (True, "diagnostics-only", True),
        (True, "synthetic-only", False),
        (True, "normal", True),
    ],
)
def test_api_owns_reconciliation_lifetime(monkeypatch, enabled, mode, expected):
    entered, exited = [], []

    class Reconciler:
        def __init__(self, *args, **kwargs):
            pass

        async def serve(self, stop):
            entered.append(True)
            await stop.wait()
            exited.append(True)

    monkeypatch.setattr("engineering_control.api.main.RecurringRunReconciler", Reconciler)
    bus = Mock(dispatch_pending=AsyncMock(return_value=False))
    app = create_app(
        settings=Settings(api_admission_mode=mode, projection_reconciliation_enabled=enabled),
        token="fixture-not-a-secret",  # noqa: S106 - isolated test constant
        service=AsyncMock(),
        repository=EmptyMetadataRepository(),
        gateway_repository=Mock(),
        operations_repository=Mock(),
        command_bus=bus,
    )
    with TestClient(app) as client:
        client.get("/health/live")
        task = app.state.projection_reconciler
        assert (task is not None) is expected
    assert bool(entered) is expected
    assert bool(exited) is expected
    if task is not None:
        assert task.done() and not task.cancelled() and task.exception() is None


def test_projection_watchdog_alert_dedupe_and_recovery(tmp_path):
    now = [datetime(2026, 9, 16, 12, tzinfo=UTC)]
    sink = Mock()
    watchdog = OperationalWatchdog(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'watchdog.db'}",
            watchdog_state_file=tmp_path / "watchdog.json",
            projection_reconciliation_enabled=True,
        ),
        sink=sink,
        now=lambda: now[0],
    )
    row = ConnectionSnapshot(
        component="projection-reconciler",
        logical_target="fixture",
        status="CONNECTED",
        checked_at=now[0],
        last_success_at=now[0],
        detail_json={
            "version": 1,
            "last_sweep": {
                "healthy": True,
                "completed_at_utc": now[0].isoformat(),
                "processed": 0,
                "repairs": 0,
                "unresolved": 0,
                "errors": 0,
            },
            "unresolved_count": 0,
            "error_count": 0,
        },
    )
    watchdog.probe_provider = lambda: [watchdog._projection_reconciliation(row, now[0])]
    assert watchdog.run_once()["delivered_count"] == 0
    now[0] += timedelta(seconds=100)
    assert watchdog.run_once()["delivered_count"] == 1
    assert sink.deliver.call_args.kwargs["payload"]["event_type"] == "ALERT"
    assert watchdog.run_once()["delivered_count"] == 0
    row.checked_at = now[0]
    row.last_success_at = now[0]
    row.detail_json["last_sweep"]["completed_at_utc"] = now[0].isoformat()
    assert watchdog.run_once()["delivered_count"] == 1
    assert sink.deliver.call_args.kwargs["payload"]["event_type"] == "RECOVERY"
    row.detail_json = {**row.detail_json, "version": True}
    assert watchdog._projection_reconciliation(row, now[0]).healthy is False
    assert watchdog._projection_reconciliation(None, now[0]).healthy is False
    watchdog.engine.dispose()
