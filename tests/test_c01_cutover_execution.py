from __future__ import annotations

import importlib.util
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engineering_control.validation.c01 import (
    REQUIRED_GATES,
    validate_c01_manifest,
    validate_post_stop_freshness,
    validate_post_stop_manifest,
)

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "execute-c01-cutover.py"
SPEC = importlib.util.spec_from_file_location("execute_c01_cutover", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_pre_and_post_manifests_require_every_bound_gate() -> None:
    release = "a" * 40
    pack = "b" * 40
    pre = {
        "schema_version": 1,
        "status": "PASS",
        "release_sha": release,
        "agent_pack_sha": pack,
        "gates": {gate: "PASS" for gate in REQUIRED_GATES},
        "provider_evidence_sha256": "c" * 64,
        "telegram_evidence_sha256": "d" * 64,
        "lifecycle_evidence_sha256": "e" * 64,
    }
    validate_c01_manifest(pre, release, pack)
    pre["gates"]["telegram_usage_status_validate"] = "PENDING"
    with pytest.raises(ValueError, match="incomplete"):
        validate_c01_manifest(pre, release, pack)

    post = {
        "schema_version": 1,
        "status": "PASS",
        "release_sha": release,
        "agent_pack_sha": pack,
        "stopped_state_sha256": "f" * 64,
        "provider_evidence_sha256": "c" * 64,
        "telegram_evidence_sha256": "d" * 64,
        "lifecycle_evidence_sha256": "e" * 64,
        "gates": {
            "omniroute_stopped_disabled": "PASS",
            "hermes_restarted": "PASS",
            "worker_services_restarted": "PASS",
            "provider_matrix_after_stop": "PASS",
            "temporal_restart_resume_after_stop": "PASS",
            "telegram_round_trip_after_stop": "PASS",
            "runtime_health_after_stop": "PASS",
        },
    }
    validate_post_stop_manifest(post, release, pack, "f" * 64)
    post["gates"]["worker_services_restarted"] = "PENDING"
    with pytest.raises(ValueError, match="post-stop"):
        validate_post_stop_manifest(post, release, pack, "f" * 64)


def test_post_stop_evidence_must_be_fresh() -> None:
    stopped = datetime.now(UTC)
    later = (stopped + timedelta(seconds=1)).isoformat()
    provider = {"created_at": later}
    lifecycle = {"created_at": later}
    telegram = {"events": [{"recorded_at": later} for _ in range(3)]}

    validate_post_stop_freshness(provider, telegram, lifecycle, stopped)
    provider["created_at"] = stopped.isoformat()
    with pytest.raises(ValueError, match="predates"):
        validate_post_stop_freshness(provider, telegram, lifecycle, stopped)


def test_executor_refuses_wrong_confirmation_before_mutation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exact confirmation"):
        MODULE.stop_phase("wrong")
    with pytest.raises(ValueError, match="exact confirmation"):
        MODULE.uninstall_phase("wrong")

    target = tmp_path / "state.json"
    MODULE._write_json_exclusive(target, {"status": "first"})
    with pytest.raises(FileExistsError):
        MODULE._write_json_exclusive(target, {"status": "second"})


def test_safe_input_rejects_symlink_and_writable_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_text("safe\n", encoding="utf-8")
    source.chmod(0o600)
    assert MODULE._read_safe(source, maximum=128, owner=os.geteuid()) == b"safe\n"

    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(PermissionError, match="unsafe"):
        MODULE._read_safe(link, maximum=128, owner=os.geteuid())
    source.chmod(0o620)
    with pytest.raises(PermissionError, match="unsafe"):
        MODULE._read_safe(source, maximum=128, owner=os.geteuid())


def test_service_state_requires_exact_systemd_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    def result(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        del check
        value = "active\n" if arguments[0] == "is-active" else "enabled\n"
        return subprocess.CompletedProcess(arguments, 0, value, "")

    monkeypatch.setattr(MODULE, "_systemctl", result)
    assert MODULE._service_state() == (True, True)

    def unknown(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        del check
        return subprocess.CompletedProcess(arguments, 1, "unknown\n", "")

    monkeypatch.setattr(MODULE, "_systemctl", unknown)
    with pytest.raises(RuntimeError, match="could not be determined"):
        MODULE._service_state()
