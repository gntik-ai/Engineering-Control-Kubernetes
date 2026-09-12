import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from engineering_control.config import Settings
from engineering_control.watchdog import OperationalWatchdog


class RecordingSink:
    def __init__(self):
        self.calls = []

    def deliver(self, **kwargs):
        self.calls.append(kwargs)


def watchdog_at(tmp_path: Path, now: datetime):
    captured = datetime(2026, 9, 12, 10, tzinfo=UTC)
    path = tmp_path / "backup.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "identifier": "20260912T100000Z-0123456789ab",
                "status": "PASS",
                "last_backup_completed_at_utc": (captured + timedelta(minutes=2)).isoformat(),
                "last_transfer_verified_at_utc": (captured + timedelta(minutes=3)).isoformat(),
                "last_restore_verified_at_utc": (captured + timedelta(minutes=4)).isoformat(),
                "worker_mirror_verified": True,
                "worker_receipt_signature_verified": True,
            }
        )
    )
    path.chmod(0o600)
    sink = RecordingSink()
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.sqlite'}",
        watchdog_state_file=tmp_path / "state.json",
        watchdog_backup_status_file=path,
    )
    watchdog = OperationalWatchdog(
        settings, sink=sink, now=lambda: now, probe_provider=lambda: [watchdog._backup(now)]
    )
    return watchdog, sink, path


def test_default_backup_freshness_ends_at_one_hour(tmp_path: Path):
    boundary = datetime(2026, 9, 12, 11, tzinfo=UTC)
    watchdog, sink, _ = watchdog_at(tmp_path, boundary + timedelta(seconds=1))
    try:
        assert watchdog._backup(boundary).healthy
        assert not watchdog._backup(boundary + timedelta(seconds=1)).healthy
        result = watchdog.run_once()
        assert result["unhealthy_count"] == result["delivered_count"] == 1
        assert sink.calls[0]["payload"]["event_type"] == "ALERT"
        assert sink.calls[0]["payload"]["status"] == "STALE_OR_FAILED"
    finally:
        watchdog.close()


def test_recent_restore_does_not_refresh_an_old_capture(tmp_path: Path):
    now = datetime(2026, 9, 12, 11, 0, 1, tzinfo=UTC)
    watchdog, _, path = watchdog_at(tmp_path, now)
    try:
        value = json.loads(path.read_text())
        for field in (
            "last_backup_completed_at_utc",
            "last_transfer_verified_at_utc",
            "last_restore_verified_at_utc",
        ):
            value[field] = now.isoformat()
        path.write_text(json.dumps(value))
        assert watchdog._backup(now).status == "STALE_OR_FAILED"
    finally:
        watchdog.close()
