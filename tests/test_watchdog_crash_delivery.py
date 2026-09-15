"""Crash-window regression using an owned child and durable idempotent sink."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engineering_control.config import Settings
from engineering_control.watchdog import OperationalWatchdog, ProbeResult


def scenario(root: Path, event: str, stage: str) -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    if stage == "restart":
        now += timedelta(seconds=1)
    healthy = event != "ALERT"
    fingerprint = "b" if event == "CHANGE" else None
    probe = ProbeResult(
        "owned-fixture", healthy, "CURRENT" if healthy else "FAILED", "fixture", fingerprint
    )
    settings = Settings(database_url="sqlite://", watchdog_state_file=root / "state.json")

    class Sink:
        def deliver(self, *, topic, request_id, payload):
            record = {"id": request_id, "event": payload["event_type"]}
            descriptor = os.open(
                root / "attempts.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
            )
            try:
                os.write(descriptor, (json.dumps(record) + "\n").encode())
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            # Stable IDs represent one effect at a deduplicating external sink.
            if stage == "crash":
                os._exit(91)

    watchdog = OperationalWatchdog(
        settings, sink=Sink(), probe_provider=lambda: [probe], now=lambda: now
    )
    if stage == "crash" and event != "ALERT":
        watchdog._save_state(
            {
                "version": 1,
                "probes": {
                    probe.key: {
                        "active": event == "RECOVERY",
                        "fingerprint": "a" if event == "CHANGE" else None,
                        "last_alerted_at": (now - timedelta(seconds=5)).isoformat()
                        if event == "RECOVERY"
                        else None,
                        "episode_started_at": (now - timedelta(seconds=5)).isoformat()
                        if event == "RECOVERY"
                        else None,
                    }
                },
            }
        )
    try:
        watchdog.run_once()
    finally:
        watchdog.close()


@pytest.mark.parametrize("event", ["ALERT", "RECOVERY", "CHANGE"])
def test_restart_reuses_id_after_external_effect_before_ack(tmp_path: Path, event: str) -> None:
    entry = Path(__file__).resolve()
    first = subprocess.run(  # noqa: S603 - fixed interpreter and this owned parametrized fixture
        [sys.executable, str(entry), str(tmp_path), event, "crash"], timeout=15
    )
    assert first.returncode == 91
    state = (
        json.loads((tmp_path / "state.json").read_text())
        if (tmp_path / "state.json").exists()
        else {}
    )
    pending = state.get("probes", {}).get("owned-fixture", {}).get("pending_request_id")
    second = subprocess.run(  # noqa: S603 - fixed interpreter and this owned parametrized fixture
        [sys.executable, str(entry), str(tmp_path), event, "restart"], timeout=15
    )
    assert second.returncode == 0
    calls = [json.loads(line) for line in (tmp_path / "attempts.jsonl").read_text().splitlines()]
    assert len(calls) == 2
    assert [call["event"] for call in calls] == [event, event]
    assert pending == calls[0]["id"] == calls[1]["id"], (
        "delivery intent/ID must survive the crash before acknowledgement"
    )


def test_write_ahead_failure_prevents_external_send(tmp_path: Path, monkeypatch) -> None:
    calls = []

    class Sink:
        def deliver(self, **kwargs):
            calls.append(kwargs)

    watchdog = OperationalWatchdog(
        Settings(database_url="sqlite://", watchdog_state_file=tmp_path / "state.json"),
        sink=Sink(),
        probe_provider=lambda: [ProbeResult("owned-fixture", False, "FAILED", "fixture")],
    )

    def denied(_state):
        raise OSError("owned fixture storage unavailable")

    monkeypatch.setattr(watchdog, "_save_state", denied)
    try:
        with pytest.raises(OSError):
            watchdog.run_once()
        assert calls == []
    finally:
        watchdog.close()


def test_state_writer_handles_short_writes(tmp_path: Path, monkeypatch) -> None:
    watchdog = OperationalWatchdog(
        Settings(database_url="sqlite://", watchdog_state_file=tmp_path / "state.json"),
        sink=object(),
        probe_provider=lambda: [],
    )
    original = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: original(fd, data[:7]))
    state = {"version": 1, "probes": {}, "sentinel": "bounded non-secret fixture"}
    try:
        watchdog._save_state(state)
        assert json.loads((tmp_path / "state.json").read_text()) == state
    finally:
        watchdog.close()


def test_isolation_denies_live_address_egress() -> None:
    import _socket

    # The enclosing kernel network-none container supplies the independent boundary.
    for host, port in [("138.199.173.27", 443), ("10.10.0.3", 8740), ("127.0.0.1", 8740)]:
        # Bypass only the optional Python attempt recorder for these intentional
        # negative sentinels: test the independent kernel boundary itself.
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
            sock.settimeout(0.1)
            with pytest.raises(OSError):
                sock.connect((host, port))


def test_pending_generation_and_release_are_retried_before_new_change(
    tmp_path: Path, monkeypatch
) -> None:
    observed = [datetime(2026, 9, 15, 12, tzinfo=UTC)]
    probes = [ProbeResult("owned-fixture", True, "CURRENT", "generation a", "a")]
    calls = []
    fail = [False]
    release = ["a" * 40]

    class Sink:
        def deliver(self, **kwargs):
            calls.append(kwargs)
            if fail[0]:
                raise OSError("fixture response lost")

    watchdog = OperationalWatchdog(
        Settings(database_url="sqlite://", watchdog_state_file=tmp_path / "state.json"),
        sink=Sink(),
        probe_provider=lambda: probes,
        now=lambda: observed[0],
    )
    monkeypatch.setattr(watchdog, "_release", lambda: release[0])
    try:
        watchdog.run_once()
        observed[0] += timedelta(seconds=1)
        probes[0] = ProbeResult("owned-fixture", True, "CURRENT", "generation b", "b")
        fail[0] = True
        assert watchdog.run_once()["delivery_failure_count"] == 1
        pending = calls[-1]
        observed[0] += timedelta(seconds=1)
        release[0] = "b" * 40
        probes[0] = ProbeResult("owned-fixture", True, "CURRENT", "generation c", "c")
        fail[0] = False
        assert watchdog.run_once()["delivered_count"] == 1
        assert calls[-1] == pending
        observed[0] += timedelta(seconds=1)
        assert watchdog.run_once()["delivered_count"] == 1
        assert calls[-1]["request_id"] != pending["request_id"]
        assert calls[-1]["payload"]["summary"] == "generation c"
    finally:
        watchdog.close()


def test_legacy_pending_without_payload_is_preserved_not_reconstructed(tmp_path: Path) -> None:
    calls = []

    class Sink:
        def deliver(self, **kwargs):
            calls.append(kwargs)

    watchdog = OperationalWatchdog(
        Settings(database_url="sqlite://", watchdog_state_file=tmp_path / "state.json"),
        sink=Sink(),
        probe_provider=lambda: [ProbeResult("legacy", False, "FAILED", "fixture")],
    )
    legacy = {"active": True, "pending_event": "ALERT", "pending_request_id": "retained-fixture-id"}
    watchdog._save_state({"version": 1, "probes": {"legacy": legacy}})
    try:
        result = watchdog.run_once()
        assert result["status"] == "DEGRADED"
        assert result["unverifiable_pending_count"] == 1
        assert calls == []
        assert watchdog._load_state()["probes"]["legacy"] == legacy
    finally:
        watchdog.close()


def test_service_loop_retries_after_state_error_without_process_restart(monkeypatch) -> None:
    import engineering_control.watchdog as module

    cycles = []
    errors = []
    closed = []
    sleeps = []

    class Watchdog:
        def run_once(self):
            cycles.append(1)
            if len(cycles) == 1:
                raise OSError("owned state-save failure")
            return {"status": "PASS"}

        def close(self):
            closed.append(True)

    class Logger:
        def error(self, _event, **kwargs):
            errors.append(kwargs)

        def info(self, *_args, **_kwargs):
            pass

    def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(module, "get_settings", lambda: Settings(database_url="sqlite://"))
    monkeypatch.setattr(module, "configure_logging", lambda _level: None)
    monkeypatch.setattr(module, "OperationalWatchdog", lambda _settings: Watchdog())
    monkeypatch.setattr(module, "_systemd_notify", lambda _message: None)
    monkeypatch.setattr(module, "logger", Logger())
    monkeypatch.setattr(module.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        module.serve()
    assert len(cycles) == 2 and closed == [True]
    assert errors == [{"error_type": "OSError"}]
    assert all(delay >= 1 for delay in sleeps)


if __name__ == "__main__":
    scenario(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
