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
    validate_lifecycle,
    validate_provider,
    validate_telegram,
    validate_usage,
)

OUTPUT = Path("/home/hermes/engineering-bootstrap/C01-ACCEPTANCE.json")
API_TOKEN = Path("/etc/engineering-control/env/api.token")


def _load(path: Path, roots: tuple[Path, ...], label: str) -> dict[str, Any]:
    resolved = path.resolve()
    if not any(resolved.is_relative_to(root.resolve()) for root in roots):
        raise ValueError(f"{label} escaped its allowlisted evidence roots")
    metadata = resolved.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > 2 * 1024 * 1024
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IRWXO)
    ):
        raise PermissionError(f"{label} ownership, mode, link count, or size is unsafe")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not a JSON object")
    return value


def _runtime_checks() -> dict[str, Any]:
    for container in ("engineering-postgresql", "engineering-temporal"):
        result = subprocess.run(  # noqa: S603 - fixed executable and container names
            ["/usr/bin/docker", "inspect", "--format", "{{.State.Health.Status}}", container],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.stdout.strip() != "healthy":
            raise RuntimeError(f"{container} is not healthy")
    token = API_TOKEN.read_text(encoding="utf-8").strip()
    token_metadata = API_TOKEN.stat()
    if (
        not stat.S_ISREG(token_metadata.st_mode)
        or token_metadata.st_nlink != 1
        or token_metadata.st_mode & (stat.S_IWGRP | stat.S_IRWXO)
        or len(token) < 32
    ):
        raise RuntimeError("Engineering API token is not provisioned")
    headers = {"Authorization": f"Bearer {token}", "X-Engineering-Actor": "c01-builder"}
    ready_request = urllib.request.Request(  # noqa: S310 - fixed loopback URL
        "http://127.0.0.1:8740/health/ready"
    )
    with urllib.request.urlopen(ready_request, timeout=5) as response:  # noqa: S310
        ready = json.load(response)
    if ready != {"status": "ready"}:
        raise RuntimeError("Engineering API is not ready")
    usage_request = urllib.request.Request(  # noqa: S310 - fixed loopback URL
        "http://127.0.0.1:8740/v1/usage", headers=headers
    )
    with urllib.request.urlopen(usage_request, timeout=10) as response:  # noqa: S310
        usage = json.load(response)
    if not isinstance(usage, dict):
        raise RuntimeError("Engineering quota response is not an object")
    validate_usage(usage)
    hermes_commands = [
        [
            "/opt/engineering-control/venv/bin/python",
            "/opt/engineering-control/source/scripts/verify-hermes-policy.py",
            "--require-no-omniroute",
        ],
        [
            "env",
            "HOME=/home/hermes",
            "PYTHONPATH=/home/hermes/.hermes/hermes-agent",
            "/home/hermes/.hermes/hermes-agent/venv/bin/python",
            "/opt/engineering-control/source/scripts/verify-hermes-runtime.py",
        ],
    ]
    for command in hermes_commands:
        subprocess.run(  # noqa: S603 - fixed local verifier commands
            ["/usr/sbin/runuser", "-u", "hermes", "--", *command],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
        )
    return usage


def _write_output(value: dict[str, Any]) -> None:
    if OUTPUT.exists() or OUTPUT.is_symlink():
        raise FileExistsError(
            "C01-ACCEPTANCE.json already exists; preserve or remove it explicitly"
        )
    hermes_uid = pwd.getpwnam("hermes").pw_uid
    hermes_gid = pwd.getpwnam("hermes").pw_gid
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=OUTPUT.parent,
        prefix=".C01-ACCEPTANCE.",
        delete=False,
    ) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    os.chown(temporary, hermes_uid, hermes_gid)
    os.replace(temporary, OUTPUT)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-evidence", type=Path, required=True)
    parser.add_argument("--telegram-evidence", type=Path, required=True)
    parser.add_argument("--lifecycle-evidence", type=Path, required=True)
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("run with sudo to validate infrastructure health and persist the gate")
    release_sha = Path("/opt/engineering-control/source").resolve().parent.name
    agent_pack_sha = Path("/opt/engineering-agent-pack/source").resolve().parent.name
    if not FULL_SHA.fullmatch(release_sha) or not FULL_SHA.fullmatch(agent_pack_sha):
        raise RuntimeError("installed release identities are not full Git SHAs")
    provider = _load(
        arguments.provider_evidence,
        (Path("/home/hermes/engineering-bootstrap/provider-acceptance"),),
        "provider evidence",
    )
    telegram = _load(
        arguments.telegram_evidence,
        (Path("/srv/engineering/evidence/acceptance"),),
        "Telegram evidence",
    )
    lifecycle = _load(
        arguments.lifecycle_evidence,
        (Path("/home/hermes/engineering-bootstrap/acceptance-runs"),),
        "lifecycle evidence",
    )
    validate_provider(provider, release_sha, agent_pack_sha)
    validate_telegram(telegram, release_sha)
    validate_lifecycle(lifecycle, release_sha)
    _runtime_checks()
    gates = {
        "engineering_api": "PASS",
        "temporal_restart_resume": "PASS",
        "postgresql": "PASS",
        "codex_structured_call": "PASS",
        "claude_structured_call": "PASS",
        "codex_maker_claude_checker": "PASS",
        "claude_maker_codex_checker": "PASS",
        "quota_broker": "PASS",
        "no_paid_fallback": "PASS",
        "hermes_api": "PASS",
        "telegram_usage_status_validate": "PASS",
        "persistence_evidence_redaction": "PASS",
    }
    payload = {
        "schema_version": 1,
        "status": "PASS",
        "created_at": datetime.now(UTC).isoformat(),
        "release_sha": release_sha,
        "agent_pack_sha": agent_pack_sha,
        "gates": gates,
        "provider_evidence_path": str(arguments.provider_evidence.resolve()),
        "provider_evidence_sha256": digest(arguments.provider_evidence.resolve()),
        "telegram_evidence_path": str(arguments.telegram_evidence.resolve()),
        "telegram_evidence_sha256": digest(arguments.telegram_evidence.resolve()),
        "lifecycle_evidence_path": str(arguments.lifecycle_evidence.resolve()),
        "lifecycle_evidence_sha256": digest(arguments.lifecycle_evidence.resolve()),
    }
    _write_output(payload)
    print(f"PASS C-01 replacement acceptance manifest={OUTPUT}")


if __name__ == "__main__":
    main()
