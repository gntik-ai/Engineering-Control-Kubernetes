#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import tempfile
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

OUTPUT = Path("/home/hermes/engineering-bootstrap/C01-ACCEPTANCE.json")
API_TOKEN = Path("/etc/engineering-control/env/api.token")
FULL_SHA = re.compile(r"^[a-f0-9]{40}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
REQUIRED_PROVIDER_RUNS = {
    "codex_maker": ("codex", "implementer"),
    "claude_checker": ("claude", "code-reviewer"),
    "claude_maker": ("claude", "implementer"),
    "codex_checker": ("codex", "code-reviewer"),
}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def validate_provider(value: dict[str, Any], release_sha: str, agent_pack_sha: str) -> None:
    if (
        value.get("status") != "PASS"
        or value.get("release_sha") != release_sha
        or value.get("redaction_status") != "REDACTED"
        or value.get("authentication_mode") != "native-subscription-cli"
        or value.get("paid_api_fallback") is not False
        or value.get("directions") != {"codex_to_claude": "PASS", "claude_to_codex": "PASS"}
    ):
        raise ValueError("provider evidence does not prove the native replacement matrix")
    runs = value.get("provider_runs")
    if not isinstance(runs, dict) or set(runs) != set(REQUIRED_PROVIDER_RUNS):
        raise ValueError("provider evidence has an incomplete invocation matrix")
    for key, (provider, role) in REQUIRED_PROVIDER_RUNS.items():
        run = runs[key]
        if (
            not isinstance(run, dict)
            or run.get("provider") != provider
            or run.get("role") != role
            or run.get("status") != "SUCCESS"
            or run.get("agent_pack_revision") != agent_pack_sha
            or not DIGEST.fullmatch(str(run.get("instruction_bundle_sha256", "")))
        ):
            raise ValueError(f"provider evidence run {key} is invalid")


def validate_lifecycle(
    value: dict[str, Any],
    release_sha: str,
    evidence_root: Path = Path("/home/hermes/engineering-bootstrap/acceptance-runs"),
) -> None:
    if (
        value.get("status") != "PASS"
        or value.get("release_sha") != release_sha
        or value.get("change_set_state") != "AWAITING_STAGING_APPROVAL"
        or value.get("workflow_worker_restarted") is not True
        or value.get("authenticated_api_path") is not True
        or value.get("maker_provider") == value.get("checker_provider")
        or value.get("redaction_status") not in {"REDACTED", "SCANNED_CLEAN"}
        or not DIGEST.fullmatch(str(value.get("evidence_sha256", "")))
    ):
        raise ValueError("lifecycle evidence does not prove restart/resume and persistence")
    evidence_path = Path(str(value.get("evidence_path", ""))).resolve()
    if (
        not evidence_path.is_file()
        or not evidence_path.is_relative_to(evidence_root.resolve())
        or _digest(evidence_path) != value["evidence_sha256"]
    ):
        raise ValueError("lifecycle artifact path or digest does not match")


def validate_telegram(value: dict[str, Any], release_sha: str) -> None:
    events = value.get("events")
    if (
        value.get("status") != "PASS"
        or value.get("release_sha") != release_sha
        or value.get("commands") != ["usage", "validate", "status"]
        or not isinstance(events, list)
        or len(events) != 3
        or any(event.get("release_sha") != release_sha for event in events)
    ):
        raise ValueError("Telegram evidence is not a release-bound three-command sequence")


def validate_usage(value: dict[str, Any]) -> None:
    for provider in ("codex", "claude"):
        snapshot = value.get(provider)
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("status") != "AVAILABLE"
            or snapshot.get("authenticated") is not True
            or snapshot.get("cli_present") is not True
            or snapshot.get("rate_limited") is not False
        ):
            raise ValueError(f"quota snapshot for {provider} is not operational")
        for unknown_safe in ("remaining_capacity", "total_capacity", "reset_at"):
            if unknown_safe not in snapshot:
                raise ValueError(f"quota snapshot for {provider} omits {unknown_safe}")


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
        "provider_evidence_sha256": _digest(arguments.provider_evidence.resolve()),
        "telegram_evidence_path": str(arguments.telegram_evidence.resolve()),
        "telegram_evidence_sha256": _digest(arguments.telegram_evidence.resolve()),
        "lifecycle_evidence_path": str(arguments.lifecycle_evidence.resolve()),
        "lifecycle_evidence_sha256": _digest(arguments.lifecycle_evidence.resolve()),
    }
    _write_output(payload)
    print(f"PASS C-01 replacement acceptance manifest={OUTPUT}")


if __name__ == "__main__":
    main()
