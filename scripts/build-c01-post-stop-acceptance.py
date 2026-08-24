#!/opt/engineering-control/venv/bin/python
from __future__ import annotations

import argparse
import json
import os
import pwd
import stat
import subprocess
import tempfile
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from engineering_control.validation.c01 import (
    FULL_SHA,
    digest,
    parse_time,
    validate_c01_manifest,
    validate_lifecycle,
    validate_post_stop_freshness,
    validate_provider,
    validate_telegram,
    validate_usage,
)

HOME = Path("/home/hermes")
PRE_ACCEPTANCE = HOME / "engineering-bootstrap/C01-ACCEPTANCE.json"
STOPPED_STATE = HOME / "engineering-bootstrap/C01-STOPPED.json"
OUTPUT = HOME / "engineering-bootstrap/C01-POST-STOP-ACCEPTANCE.json"
API_TOKEN = Path("/etc/engineering-control/env/api.token")
PROVIDER_ROOT = HOME / "engineering-bootstrap/provider-acceptance"
LIFECYCLE_ROOT = HOME / "engineering-bootstrap/acceptance-runs"
TELEGRAM_ROOT = Path("/srv/engineering/evidence/acceptance")
WORKER_SERVICES = (
    "engineering-worker@codex.service",
    "engineering-worker@claude.service",
    "engineering-worker@browser.service",
    "engineering-worker@scan.service",
    "engineering-worker@delivery.service",
    "engineering-release-worker.service",
)


def _load(path: Path, roots: tuple[Path, ...], label: str) -> dict[str, Any]:
    resolved = path.resolve()
    if not any(resolved.is_relative_to(root.resolve()) for root in roots):
        raise ValueError(f"{label} escaped its allowlisted evidence roots")
    metadata = resolved.stat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > 2 * 1024 * 1024
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IRWXO)
    ):
        raise PermissionError(f"{label} permissions, link count, or size are unsafe")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not a JSON object")
    return value


def _run_as_hermes(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed runuser and bounded command arguments
        ["/usr/sbin/runuser", "-u", "hermes", "--", *command],
        check=True,
        capture_output=True,
        text=True,
        timeout=45,
    )


def _user_service_marker(unit: str) -> int:
    result = _run_as_hermes(
        "env",
        "HOME=/home/hermes",
        "XDG_RUNTIME_DIR=/run/user/1000",
        "/usr/bin/systemctl",
        "--user",
        "show",
        unit,
        "--property=ActiveEnterTimestampMonotonic",
        "--value",
    )
    value = result.stdout.strip()
    if not value.isdigit() or int(value) <= 0:
        raise RuntimeError(f"user service {unit} has no active-enter marker")
    return int(value)


def _omniroute_stopped() -> bool:
    expected = {"is-active": "inactive", "is-enabled": "disabled"}
    for operation, expected_value in expected.items():
        result = subprocess.run(  # noqa: S603 - fixed user service query
            [
                "/usr/sbin/runuser",
                "-u",
                "hermes",
                "--",
                "env",
                "HOME=/home/hermes",
                "XDG_RUNTIME_DIR=/run/user/1000",
                "/usr/bin/systemctl",
                "--user",
                operation,
                "omniroute.service",
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.stdout.strip() != expected_value:
            return False
    return True


def _worker_markers() -> dict[str, int]:
    markers: dict[str, int] = {}
    for unit in WORKER_SERVICES:
        result = _run_as_hermes(
            "env",
            "HOME=/home/hermes",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "/usr/bin/ssh",
            "-o",
            "BatchMode=yes",
            "worker",
            "systemctl",
            "show",
            unit,
            "--property=ActiveEnterTimestampMonotonic",
            "--value",
        )
        value = result.stdout.strip()
        if not value.isdigit() or int(value) <= 0:
            raise RuntimeError(f"worker service {unit} has no active-enter marker")
        markers[unit] = int(value)
    return markers


def _runtime_checks() -> dict[str, Any]:
    for container in ("engineering-postgresql", "engineering-temporal"):
        result = subprocess.run(  # noqa: S603 - fixed Docker executable and containers
            ["/usr/bin/docker", "inspect", "--format", "{{.State.Health.Status}}", container],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.stdout.strip() != "healthy":
            raise RuntimeError(f"{container} is not healthy")
    token = API_TOKEN.read_text(encoding="utf-8").strip()
    metadata = API_TOKEN.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IRWXO)
        or len(token) < 32
    ):
        raise RuntimeError("Engineering API token is unsafe or unavailable")
    ready = urllib.request.Request("http://127.0.0.1:8740/health/ready")  # noqa: S310
    with urllib.request.urlopen(ready, timeout=5) as response:  # noqa: S310
        if json.load(response) != {"status": "ready"}:
            raise RuntimeError("Engineering API is not ready")
    request = urllib.request.Request(  # noqa: S310
        "http://127.0.0.1:8740/v1/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Engineering-Actor": "c01-post-stop-builder",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        usage = json.load(response)
    if not isinstance(usage, dict):
        raise RuntimeError("Engineering quota response is not an object")
    validate_usage(usage)
    _run_as_hermes(
        "/opt/engineering-control/venv/bin/python",
        "/opt/engineering-control/source/scripts/verify-hermes-policy.py",
        "--require-no-omniroute",
    )
    _run_as_hermes(
        "env",
        "HOME=/home/hermes",
        "PYTHONPATH=/home/hermes/.hermes/hermes-agent",
        "/home/hermes/.hermes/hermes-agent/venv/bin/python",
        "/opt/engineering-control/source/scripts/verify-hermes-runtime.py",
    )
    return usage


def _write(value: dict[str, Any]) -> None:
    if OUTPUT.exists() or OUTPUT.is_symlink():
        raise FileExistsError("C01-POST-STOP-ACCEPTANCE.json already exists")
    account = pwd.getpwnam("hermes")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=OUTPUT.parent,
        prefix=".C01-POST-STOP-ACCEPTANCE.",
        delete=False,
    ) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    os.chown(temporary, account.pw_uid, account.pw_gid)
    os.replace(temporary, OUTPUT)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-evidence", type=Path, required=True)
    parser.add_argument("--telegram-evidence", type=Path, required=True)
    parser.add_argument("--lifecycle-evidence", type=Path, required=True)
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("run with sudo to verify infrastructure and persist the post-stop gate")
    release = Path("/opt/engineering-control/source").resolve().parent.name
    pack = Path("/opt/engineering-agent-pack/source").resolve().parent.name
    if not FULL_SHA.fullmatch(release) or not FULL_SHA.fullmatch(pack):
        raise RuntimeError("installed releases are not immutable Git SHAs")
    pre = _load(PRE_ACCEPTANCE, (PRE_ACCEPTANCE.parent,), "pre-stop acceptance")
    validate_c01_manifest(pre, release, pack)
    stopped = _load(STOPPED_STATE, (STOPPED_STATE.parent,), "stopped state")
    if (
        stopped.get("status") != "STOPPED_NOT_UNINSTALLED"
        or stopped.get("release_sha") != release
        or stopped.get("agent_pack_sha") != pack
        or stopped.get("pre_acceptance_sha256") != digest(PRE_ACCEPTANCE)
    ):
        raise ValueError("stopped state does not bind to the current pre-stop acceptance")
    stopped_at = parse_time(stopped.get("stopped_at"), "OmniRoute stop")
    provider = _load(arguments.provider_evidence, (PROVIDER_ROOT,), "provider evidence")
    telegram = _load(arguments.telegram_evidence, (TELEGRAM_ROOT,), "Telegram evidence")
    lifecycle = _load(arguments.lifecycle_evidence, (LIFECYCLE_ROOT,), "lifecycle evidence")
    validate_provider(provider, release, pack)
    validate_telegram(telegram, release)
    validate_lifecycle(lifecycle, release)
    validate_post_stop_freshness(provider, telegram, lifecycle, stopped_at)
    if not _omniroute_stopped():
        raise RuntimeError("OmniRoute is not both stopped and disabled")
    hermes_before = stopped.get("hermes_active_enter_monotonic_before_stop")
    hermes_after = _user_service_marker("hermes-gateway.service")
    if not isinstance(hermes_before, int) or hermes_after <= hermes_before:
        raise RuntimeError("Hermes was not restarted after OmniRoute stopped")
    worker_before = stopped.get("worker_active_enter_monotonic_before_stop")
    worker_after = _worker_markers()
    if not isinstance(worker_before, dict) or any(
        not isinstance(worker_before.get(unit), int) or worker_after[unit] <= worker_before[unit]
        for unit in WORKER_SERVICES
    ):
        raise RuntimeError("every worker service was not restarted after OmniRoute stopped")
    _runtime_checks()
    payload = {
        "schema_version": 1,
        "status": "PASS",
        "created_at": datetime.now(UTC).isoformat(),
        "release_sha": release,
        "agent_pack_sha": pack,
        "stopped_state_sha256": digest(STOPPED_STATE),
        "provider_evidence_path": str(arguments.provider_evidence.resolve()),
        "provider_evidence_sha256": digest(arguments.provider_evidence.resolve()),
        "telegram_evidence_path": str(arguments.telegram_evidence.resolve()),
        "telegram_evidence_sha256": digest(arguments.telegram_evidence.resolve()),
        "lifecycle_evidence_path": str(arguments.lifecycle_evidence.resolve()),
        "lifecycle_evidence_sha256": digest(arguments.lifecycle_evidence.resolve()),
        "hermes_active_enter_monotonic_after_stop": hermes_after,
        "worker_active_enter_monotonic_after_stop": worker_after,
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
    _write(payload)
    print(f"PASS post-stop C-01 acceptance manifest={OUTPUT}")


if __name__ == "__main__":
    main()
