from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engineering_control.config import Settings
from engineering_control.watchdog import OperationalWatchdog, ProbeResult

NOW = datetime(2026, 9, 5, 20, 0, tzinfo=UTC)


def status_value(started: datetime) -> dict[str, object]:
    return {
        "version": 1,
        "identifier": started.strftime("%Y%m%dT%H%M%SZ") + "-0123456789ab",
        "status": "PASS",
        "last_backup_completed_at_utc": (started + timedelta(minutes=2)).isoformat(),
        "last_transfer_verified_at_utc": (started + timedelta(minutes=1)).isoformat(),
        "last_restore_verified_at_utc": (started + timedelta(seconds=100)).isoformat(),
        "worker_mirror_verified": True,
        "worker_receipt_signature_verified": True,
    }


def probe(tmp_path: Path, value: dict[str, object]) -> ProbeResult:
    path = tmp_path / "status.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    watchdog = OperationalWatchdog(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'fixture.sqlite'}",
            watchdog_backup_status_file=path,
            watchdog_state_file=tmp_path / "watchdog.json",
            watchdog_backup_max_age_seconds=3600,
        ),
        probe_provider=lambda: [],
    )
    try:
        return watchdog._backup(NOW)
    finally:
        watchdog.engine.dispose()


def test_backup_completion_must_not_refresh_an_old_recovery_point(tmp_path: Path) -> None:
    result = probe(tmp_path, status_value(NOW - timedelta(seconds=3601)))
    assert result.status == "STALE_OR_FAILED"
    assert not result.healthy


@pytest.mark.parametrize("age,healthy", [(1800, True), (3600, True), (3601, False)])
def test_recovery_lower_bound_uses_exact_configured_maximum(tmp_path: Path, age, healthy) -> None:
    assert probe(tmp_path, status_value(NOW - timedelta(seconds=age))).healthy is healthy


@pytest.mark.parametrize(
    "key",
    [
        "last_backup_completed_at_utc",
        "last_transfer_verified_at_utc",
        "last_restore_verified_at_utc",
    ],
)
def test_future_observations_fail_closed_instead_of_clamping_to_zero(tmp_path: Path, key) -> None:
    value = status_value(NOW - timedelta(minutes=30))
    value[key] = (NOW + timedelta(seconds=1)).isoformat()
    assert probe(tmp_path, value).status == "MISSING_OR_INVALID"


@pytest.mark.parametrize("signed", [False, None, "true"])
def test_mirror_acceptance_requires_verified_receipt_signature(tmp_path: Path, signed) -> None:
    value = status_value(NOW - timedelta(minutes=30))
    value["worker_receipt_signature_verified"] = signed
    assert not probe(tmp_path, value).healthy


@pytest.mark.parametrize("identifier", [None, "", "20261399T250000Z-0123456789ab", "../status"])
def test_missing_or_malformed_recovery_identity_is_unknown(tmp_path: Path, identifier) -> None:
    value = status_value(NOW - timedelta(minutes=30))
    value["identifier"] = identifier
    assert probe(tmp_path, value).status == "MISSING_OR_INVALID"


def test_naive_timestamp_is_not_assumed_to_be_utc(tmp_path: Path) -> None:
    value = status_value(NOW - timedelta(minutes=30))
    value["last_backup_completed_at_utc"] = "2026-09-05T19:32:00"
    assert probe(tmp_path, value).status == "MISSING_OR_INVALID"


def test_snapshot_cannot_postdate_transfer(tmp_path: Path) -> None:
    value = status_value(NOW - timedelta(minutes=30))
    value["last_transfer_verified_at_utc"] = (NOW - timedelta(minutes=31)).isoformat()
    assert probe(tmp_path, value).status == "MISSING_OR_INVALID"


def test_separately_current_restore_rehearsal_can_predate_latest_backup(tmp_path: Path) -> None:
    value = status_value(NOW - timedelta(minutes=30))
    value["last_restore_verified_at_utc"] = (NOW - timedelta(hours=12)).isoformat()
    assert probe(tmp_path, value).healthy
