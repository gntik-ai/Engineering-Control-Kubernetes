from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import Run, WorkerHeartbeat
from engineering_control.watchdog import OperationalWatchdog, ProbeResult
from engineering_control.workflows.worker_registry import ROLE_WORKER_REGISTRY


class RecordingSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    def deliver(self, *, topic: str, request_id: str, payload: dict[str, object]) -> None:
        self.calls.append({"topic": topic, "request_id": request_id, "payload": payload})
        if self.fail:
            raise RuntimeError("fixture delivery failure")


def test_watchdog_alerts_once_repeats_on_bound_and_sends_recovery(tmp_path: Path) -> None:
    observed = [datetime(2026, 9, 3, 10, 0, tzinfo=UTC)]
    probes = [ProbeResult("worker-capacity", True, "CURRENT", "workers current")]
    sink = RecordingSink()
    watchdog = OperationalWatchdog(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'watchdog.sqlite'}",
            watchdog_state_file=tmp_path / "state" / "watchdog.json",
            watchdog_repeat_seconds=300,
        ),
        sink=sink,  # type: ignore[arg-type] - bounded fixture implements the sink contract
        probe_provider=lambda: probes,
        now=lambda: observed[0],
    )

    assert watchdog.run_once()["delivered_count"] == 0
    probes[0] = ProbeResult("worker-capacity", False, "MISSING_OR_STALE", "worker heartbeat stale")
    observed[0] += timedelta(seconds=1)
    assert watchdog.run_once()["delivered_count"] == 1
    assert sink.calls[-1]["payload"]["event_type"] == "ALERT"  # type: ignore[index]

    observed[0] += timedelta(seconds=60)
    assert watchdog.run_once()["delivered_count"] == 0
    observed[0] += timedelta(seconds=301)
    assert watchdog.run_once()["delivered_count"] == 1

    probes[0] = ProbeResult("worker-capacity", True, "CURRENT", "workers current")
    observed[0] += timedelta(seconds=1)
    assert watchdog.run_once()["delivered_count"] == 1
    assert sink.calls[-1]["payload"]["event_type"] == "RECOVERY"  # type: ignore[index]
    assert (tmp_path / "state" / "watchdog.json").stat().st_mode & 0o777 == 0o600


def test_watchdog_retries_failed_delivery_without_suppressing_alert(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 11, 0, tzinfo=UTC)
    sink = RecordingSink(fail=True)
    watchdog = OperationalWatchdog(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'watchdog.sqlite'}",
            watchdog_state_file=tmp_path / "state.json",
        ),
        sink=sink,  # type: ignore[arg-type] - bounded fixture implements the sink contract
        probe_provider=lambda: [
            ProbeResult("database", False, "UNREACHABLE", "database probe failed")
        ],
        now=lambda: now,
    )
    first = watchdog.run_once()
    second = watchdog.run_once()
    assert first["delivery_failure_count"] == second["delivery_failure_count"] == 1
    assert first["delivered_count"] == second["delivered_count"] == 0
    assert len(sink.calls) == 2
    assert sink.calls[0]["request_id"] == sink.calls[1]["request_id"]


def test_watchdog_reminder_uses_a_new_delivery_id_after_repeat_bound(tmp_path: Path) -> None:
    observed = [datetime(2026, 9, 3, 11, 30, tzinfo=UTC)]
    sink = RecordingSink()
    watchdog = OperationalWatchdog(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'watchdog.sqlite'}",
            watchdog_state_file=tmp_path / "state.json",
            watchdog_repeat_seconds=300,
        ),
        sink=sink,  # type: ignore[arg-type]
        probe_provider=lambda: [
            ProbeResult("database", False, "UNREACHABLE", "database probe failed")
        ],
        now=lambda: observed[0],
    )

    assert watchdog.run_once()["delivered_count"] == 1
    first_id = sink.calls[-1]["request_id"]
    observed[0] += timedelta(seconds=301)
    assert watchdog.run_once()["delivered_count"] == 1
    assert sink.calls[-1]["request_id"] != first_id


def test_watchdog_retries_a_failed_process_generation_change(tmp_path: Path) -> None:
    observed = [datetime(2026, 9, 3, 11, 45, tzinfo=UTC)]
    probes = [ProbeResult("unit:engineering-api.service", True, "ACTIVE", "active", "a")]
    sink = RecordingSink()
    watchdog = OperationalWatchdog(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'watchdog.sqlite'}",
            watchdog_state_file=tmp_path / "state.json",
        ),
        sink=sink,  # type: ignore[arg-type]
        probe_provider=lambda: probes,
        now=lambda: observed[0],
    )
    assert watchdog.run_once()["delivered_count"] == 0

    probes[0] = ProbeResult("unit:engineering-api.service", True, "ACTIVE", "active", "b")
    sink.fail = True
    observed[0] += timedelta(seconds=1)
    assert watchdog.run_once()["delivery_failure_count"] == 1
    failed_id = sink.calls[-1]["request_id"]

    sink.fail = False
    observed[0] += timedelta(seconds=60)
    assert watchdog.run_once()["delivered_count"] == 1
    assert sink.calls[-1]["request_id"] == failed_id
    assert sink.calls[-1]["payload"]["event_type"] == "CHANGE"  # type: ignore[index]


def test_watchdog_quarantines_corrupt_state_and_emits_recovery(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text("not-json", encoding="utf-8")
    state.chmod(0o600)
    sink = RecordingSink()
    watchdog = OperationalWatchdog(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'watchdog.sqlite'}", watchdog_state_file=state
        ),
        sink=sink,  # type: ignore[arg-type]
        probe_provider=lambda: [],
        now=lambda: datetime(2026, 9, 3, 11, 50, tzinfo=UTC),
    )

    assert watchdog.run_once()["delivered_count"] == 1
    assert sink.calls[-1]["payload"]["event_type"] == "ALERT"  # type: ignore[index]
    assert len(list(tmp_path.glob("state.json.invalid-*"))) == 1
    assert watchdog.run_once()["delivered_count"] == 1
    assert sink.calls[-1]["payload"]["event_type"] == "RECOVERY"  # type: ignore[index]


def test_watchdog_health_flip_drops_stale_pending_label(tmp_path: Path) -> None:
    observed = [datetime(2026, 9, 3, 11, 55, tzinfo=UTC)]
    probes = [ProbeResult("database", False, "UNREACHABLE", "database failed")]
    sink = RecordingSink(fail=True)
    watchdog = OperationalWatchdog(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'watchdog.sqlite'}",
            watchdog_state_file=tmp_path / "state.json",
        ),
        sink=sink,  # type: ignore[arg-type]
        probe_provider=lambda: probes,
        now=lambda: observed[0],
    )

    assert watchdog.run_once()["delivery_failure_count"] == 1
    probes[0] = ProbeResult("database", True, "REACHABLE", "database passed")
    sink.fail = False
    observed[0] += timedelta(seconds=60)
    assert watchdog.run_once()["delivered_count"] == 1
    assert sink.calls[-1]["payload"]["event_type"] == "ALERT"  # type: ignore[index]
    assert sink.calls[-1]["payload"]["status"] == "UNREACHABLE"  # type: ignore[index]
    observed[0] += timedelta(seconds=60)
    assert watchdog.run_once()["delivered_count"] == 1
    assert sink.calls[-1]["payload"]["event_type"] == "RECOVERY"  # type: ignore[index]


def test_watchdog_new_failure_supersedes_failed_recovery_label(tmp_path: Path) -> None:
    observed = [datetime(2026, 9, 3, 11, 57, tzinfo=UTC)]
    probes = [ProbeResult("database", False, "UNREACHABLE", "database failed")]
    sink = RecordingSink()
    watchdog = OperationalWatchdog(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'watchdog.sqlite'}",
            watchdog_state_file=tmp_path / "state.json",
        ),
        sink=sink,  # type: ignore[arg-type]
        probe_provider=lambda: probes,
        now=lambda: observed[0],
    )
    assert watchdog.run_once()["delivered_count"] == 1
    probes[0] = ProbeResult("database", True, "REACHABLE", "database passed")
    sink.fail = True
    observed[0] += timedelta(seconds=60)
    assert watchdog.run_once()["delivery_failure_count"] == 1
    probes[0] = ProbeResult("database", False, "UNREACHABLE", "database failed again")
    sink.fail = False
    observed[0] += timedelta(seconds=60)
    assert watchdog.run_once()["delivered_count"] == 1
    assert [call["payload"]["event_type"] for call in sink.calls] == [  # type: ignore[index]
        "ALERT",
        "RECOVERY",
        "ALERT",
    ]


def test_unit_probe_failure_is_bounded(monkeypatch) -> None:
    def unavailable(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("systemctl", 5)

    monkeypatch.setattr(subprocess, "run", unavailable)
    result = OperationalWatchdog._unit("engineering-api.service")
    assert result.healthy is False
    assert result.status == "UNKNOWN"


def test_backup_probe_requires_current_transfer_and_restore(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    status_file = tmp_path / "backup-status.json"
    status_file.write_text(
        json.dumps(
            {
                "version": 1,
                "status": "PASS",
                "last_backup_completed_at_utc": (now - timedelta(minutes=30)).isoformat(),
                "last_transfer_verified_at_utc": (now - timedelta(minutes=29)).isoformat(),
                "last_restore_verified_at_utc": (now - timedelta(days=1)).isoformat(),
                "worker_mirror_verified": True,
            }
        ),
        encoding="utf-8",
    )
    status_file.chmod(0o600)
    watchdog = OperationalWatchdog(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'watchdog.sqlite'}",
            watchdog_state_file=tmp_path / "state.json",
            watchdog_backup_status_file=status_file,
        ),
        sink=RecordingSink(),  # type: ignore[arg-type]
        probe_provider=lambda: [],
        now=lambda: now,
    )

    assert watchdog._backup(now).healthy is True
    stale = json.loads(status_file.read_text(encoding="utf-8"))
    stale["last_transfer_verified_at_utc"] = (now - timedelta(hours=2)).isoformat()
    status_file.write_text(json.dumps(stale), encoding="utf-8")
    assert watchdog._backup(now).status == "STALE_OR_FAILED"

    link = tmp_path / "backup-link.json"
    link.symlink_to(status_file)
    linked_watchdog = OperationalWatchdog(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'linked.sqlite'}",
            watchdog_state_file=tmp_path / "linked-state.json",
            watchdog_backup_status_file=link,
        ),
        sink=RecordingSink(),  # type: ignore[arg-type]
        probe_provider=lambda: [],
        now=lambda: now,
    )
    assert linked_watchdog._backup(now).status == "MISSING_OR_INVALID"


def test_database_probe_requires_current_cleanup_and_all_role_workers(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    watchdog = OperationalWatchdog(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'watchdog.sqlite'}",
            watchdog_state_file=tmp_path / "state.json",
        ),
        sink=RecordingSink(),  # type: ignore[arg-type]
        probe_provider=lambda: [],
        now=lambda: now,
    )
    Base.metadata.create_all(watchdog.engine)
    initial = {probe.key: probe for probe in watchdog._database(now)}
    assert initial["database"].healthy is True
    assert initial["cleanup-schedule"].healthy is False
    assert initial["worker-capacity"].healthy is False

    with watchdog.engine.begin() as connection:
        connection.execute(
            Run.__table__.insert(),
            [
                {
                    "id": uuid4(),
                    "workflow_id": "cleanup-current-fixture",
                    "run_type": "cleanup",
                    "status": "SUCCESS",
                    "project": "platform",
                    "started_at": now - timedelta(minutes=2),
                    "finished_at": now - timedelta(minutes=1),
                    "created_at": now - timedelta(minutes=2),
                    "updated_at": now - timedelta(minutes=1),
                }
            ],
        )
        connection.execute(
            WorkerHeartbeat.__table__.insert(),
            [
                {
                    "id": uuid4(),
                    "identity": f"{kind}-fixture",
                    "hostname": "fixture-worker",
                    "worker_kind": kind,
                    "observed_at": now - timedelta(seconds=10),
                    "created_at": now - timedelta(seconds=10),
                    "updated_at": now - timedelta(seconds=10),
                }
                for kind in ROLE_WORKER_REGISTRY
            ],
        )

    accepted = {probe.key: probe for probe in watchdog._database(now)}
    assert accepted["cleanup-schedule"].healthy is True
    assert accepted["worker-capacity"].healthy is True

    with watchdog.engine.begin() as connection:
        connection.execute(
            Run.__table__.update()
            .where(Run.workflow_id == "cleanup-current-fixture")
            .values(status="RUNNING", finished_at=None, updated_at=now - timedelta(minutes=5))
        )
    running = {probe.key: probe for probe in watchdog._database(now)}
    assert running["cleanup-schedule"].healthy is True

    with watchdog.engine.begin() as connection:
        connection.execute(
            Run.__table__.update()
            .where(Run.workflow_id == "cleanup-current-fixture")
            .values(started_at=now - timedelta(hours=2), updated_at=now)
        )
    wedged = {probe.key: probe for probe in watchdog._database(now)}
    assert wedged["cleanup-schedule"].healthy is False

    duplicate_kind = next(iter(ROLE_WORKER_REGISTRY))
    with watchdog.engine.begin() as connection:
        connection.execute(
            WorkerHeartbeat.__table__.insert(),
            {
                "id": uuid4(),
                "identity": f"{duplicate_kind}-duplicate-fixture",
                "hostname": "fixture-worker-two",
                "worker_kind": duplicate_kind,
                "observed_at": now - timedelta(seconds=5),
                "created_at": now - timedelta(seconds=5),
                "updated_at": now - timedelta(seconds=5),
            },
        )
    duplicated = {probe.key: probe for probe in watchdog._database(now)}
    assert duplicated["worker-capacity"].healthy is False

    with watchdog.engine.begin() as connection:
        connection.execute(
            WorkerHeartbeat.__table__.update()
            .where(WorkerHeartbeat.identity == f"{duplicate_kind}-duplicate-fixture")
            .values(observed_at=now - timedelta(hours=1))
        )
    stale_predecessor = {probe.key: probe for probe in watchdog._database(now)}
    assert stale_predecessor["worker-capacity"].healthy is True


def test_probe_provider_exception_becomes_an_alert(tmp_path: Path) -> None:
    def failed_probe_provider() -> list[ProbeResult]:
        raise RuntimeError("synthetic probe failure")

    sink = RecordingSink()
    watchdog = OperationalWatchdog(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'watchdog.sqlite'}",
            watchdog_state_file=tmp_path / "state.json",
        ),
        sink=sink,  # type: ignore[arg-type]
        probe_provider=failed_probe_provider,
        now=lambda: datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
    )
    result = watchdog.run_once()
    assert result["status"] == "PASS"
    assert result["unhealthy_count"] == 1
    assert result["delivered_count"] == 1
    assert sink.calls[-1]["payload"]["probe"] == "probe-provider"  # type: ignore[index]
