#!/usr/bin/env python3
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
    validate_lifecycle,
    validate_post_stop_freshness,
    validate_provider,
    validate_telegram,
    validate_usage,
)

HOME = Path("/home/hermes")
C02_COMPLETE = HOME / "engineering-bootstrap/C02-COMPLETE.json"
OUTPUT = HOME / "engineering-bootstrap/C02-POST-REMOVE-ACCEPTANCE.json"
API_TOKEN = Path("/etc/engineering-control/env/api.token")
PROVIDER_ROOT = HOME / "engineering-bootstrap/provider-acceptance"
LIFECYCLE_ROOT = HOME / "engineering-bootstrap/acceptance-runs"
TELEGRAM_ROOT = Path("/srv/engineering/evidence/acceptance")


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


def _run_as_hermes(*command: str) -> None:
    subprocess.run(  # noqa: S603 - fixed runuser and bounded command
        ["/usr/sbin/runuser", "-u", "hermes", "--", *command],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=45,
    )


def _runtime_checks() -> None:
    for container in ("engineering-postgresql", "engineering-temporal"):
        result = subprocess.run(  # noqa: S603 - fixed Docker health query
            ["/usr/bin/docker", "inspect", "--format", "{{.State.Health.Status}}", container],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.stdout.strip() != "healthy":
            raise RuntimeError(f"{container} is not healthy")
    absent = subprocess.run(  # noqa: S603 - fixed Caddy absence check
        ["/usr/bin/docker", "inspect", "omniroute-caddy"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    if absent.returncode == 0:
        raise RuntimeError("OmniRoute Caddy container reappeared")
    sockets = subprocess.run(  # noqa: S603 - fixed public-listener query
        ["/usr/bin/ss", "-H", "-ltn"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    public_ports: set[int] = set()
    for line in sockets.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        try:
            port = int(fields[3].rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        if port in {80, 443}:
            public_ports.add(port)
    if public_ports:
        raise RuntimeError("ports 80/443 still have listeners after C-02")
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
            "X-Engineering-Actor": "c02-post-remove-builder",
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


def _write(value: dict[str, Any]) -> None:
    if OUTPUT.exists() or OUTPUT.is_symlink():
        raise FileExistsError("C02-POST-REMOVE-ACCEPTANCE.json already exists")
    account = pwd.getpwnam("hermes")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=OUTPUT.parent,
        prefix=".C02-POST-REMOVE-ACCEPTANCE.",
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
        raise SystemExit("run with sudo to verify and persist the post-C-02 regression")
    release = Path("/opt/engineering-control/source").resolve().parent.name
    pack = Path("/opt/engineering-agent-pack/source").resolve().parent.name
    if not FULL_SHA.fullmatch(release) or not FULL_SHA.fullmatch(pack):
        raise RuntimeError("installed releases are not immutable Git SHAs")
    complete = _load(C02_COMPLETE, (C02_COMPLETE.parent,), "C-02 completion")
    if complete.get("status") != "COMPLETE" or complete.get("release_sha") != release:
        raise ValueError("C-02 completion does not bind to the installed release")
    completed_at = parse_time(complete.get("completed_at"), "C-02 completion")
    provider = _load(arguments.provider_evidence, (PROVIDER_ROOT,), "provider evidence")
    telegram = _load(arguments.telegram_evidence, (TELEGRAM_ROOT,), "Telegram evidence")
    lifecycle = _load(arguments.lifecycle_evidence, (LIFECYCLE_ROOT,), "lifecycle evidence")
    validate_provider(provider, release, pack)
    validate_telegram(telegram, release)
    validate_lifecycle(lifecycle, release)
    validate_post_stop_freshness(provider, telegram, lifecycle, completed_at)
    _runtime_checks()
    payload = {
        "schema_version": 1,
        "status": "PASS",
        "created_at": datetime.now(UTC).isoformat(),
        "release_sha": release,
        "agent_pack_sha": pack,
        "c02_complete_sha256": digest(C02_COMPLETE),
        "provider_evidence_path": str(arguments.provider_evidence.resolve()),
        "provider_evidence_sha256": digest(arguments.provider_evidence.resolve()),
        "telegram_evidence_path": str(arguments.telegram_evidence.resolve()),
        "telegram_evidence_sha256": digest(arguments.telegram_evidence.resolve()),
        "lifecycle_evidence_path": str(arguments.lifecycle_evidence.resolve()),
        "lifecycle_evidence_sha256": digest(arguments.lifecycle_evidence.resolve()),
        "gates": {
            "caddy_container_absent": "PASS",
            "public_ports_80_443_absent": "PASS",
            "provider_matrix_after_c02": "PASS",
            "temporal_restart_resume_after_c02": "PASS",
            "telegram_round_trip_after_c02": "PASS",
            "runtime_health_after_c02": "PASS",
        },
    }
    _write(payload)
    print(f"PASS post-C-02 regression manifest={OUTPUT}")


if __name__ == "__main__":
    main()
