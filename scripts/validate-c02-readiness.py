#!/opt/engineering-control/venv/bin/python
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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from engineering_control.validation.c01 import FULL_SHA, digest, parse_time

HOME = Path("/home/hermes")
C01_COMPLETE = HOME / "engineering-bootstrap/C01-COMPLETE.json"
OUTPUT = HOME / "engineering-bootstrap/C02-READINESS.json"
CADDYFILE = HOME / ".config/caddy/Caddyfile"
PATTERN = re.compile(
    rb"(?i)omni\.musematic\.ai|(?:localhost|127\.0\.0\.1):201(?:28|31|32)|\bomniroute\b"
)
DIRECT_SECRET = re.compile(rb"(?i)(?:token|password|secret|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]+")
API_PATH = re.compile(
    r"(?i)^/(?:v\d+|api|health|models|chat|completions|messages|embeddings)(?:/|$)"
)
CONTROLLER_CONSUMERS = HOME / "engineering-bootstrap/C02-CONSUMERS-controller.json"
WORKER_CONSUMERS = HOME / "engineering-bootstrap/C02-CONSUMERS-worker.json"


def _read_bounded(path: Path, maximum: int = 2 * 1024 * 1024) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > maximum:
        raise PermissionError(f"unsafe or oversized readiness input: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise ValueError(f"readiness input exceeded its size bound: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _load_protected(path: Path) -> dict[str, Any]:
    metadata = path.lstat()
    account = pwd.getpwnam("hermes")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != account.pw_uid
        or metadata.st_nlink != 1
        or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise PermissionError(f"protected C-02 evidence is unsafe: {path.name}")
    value = json.loads(_read_bounded(path))
    if not isinstance(value, dict):
        raise ValueError(f"protected C-02 evidence is not an object: {path.name}")
    return value


def _container_consumers() -> tuple[list[str], dict[str, Any]]:
    listing = subprocess.run(  # noqa: S603 - fixed Docker query
        ["/usr/bin/docker", "ps", "-a", "--format", "{{.Names}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    matches: list[str] = []
    caddy: dict[str, Any] | None = None
    for name in sorted(line for line in listing.stdout.splitlines() if line):
        inspection = subprocess.run(  # noqa: S603 - fixed Docker query, discovered name
            ["/usr/bin/docker", "inspect", name],
            check=True,
            capture_output=True,
            timeout=30,
        )
        values = json.loads(inspection.stdout)
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            raise ValueError(f"Docker inspect returned invalid metadata for {name}")
        value = values[0]
        if name == "omniroute-caddy":
            config = value.get("Config")
            host = value.get("HostConfig")
            state = value.get("State")
            caddy = {
                "image": config.get("Image") if isinstance(config, dict) else None,
                "network_mode": host.get("NetworkMode") if isinstance(host, dict) else None,
                "restart_policy": (
                    host.get("RestartPolicy", {}).get("Name") if isinstance(host, dict) else None
                ),
                "running": state.get("Running") if isinstance(state, dict) else None,
            }
            continue
        if PATTERN.search(inspection.stdout):
            matches.append(name)
    if caddy is None:
        raise RuntimeError("reviewed OmniRoute Caddy container is absent before C-02")
    return matches, caddy


def analyze_caddy_logs(content: bytes) -> dict[str, int | str]:
    requests = 0
    scanner_only = 0
    potentially_legitimate = 0
    non_request_events = 0
    unparseable = 0
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            unparseable += 1
            continue
        if not isinstance(value, dict):
            unparseable += 1
            continue
        request = value.get("request")
        if not isinstance(request, dict):
            non_request_events += 1
            continue
        requests += 1
        method = str(request.get("method", ""))
        uri = str(request.get("uri", ""))
        path = uri.split("?", 1)[0]
        status = value.get("status")
        if (
            method not in {"GET", "HEAD", "OPTIONS"}
            or not isinstance(status, int)
            or status < 400
            or API_PATH.search(path)
        ):
            potentially_legitimate += 1
        else:
            scanner_only += 1
    return {
        "requests": requests,
        "scanner_only_requests": scanner_only,
        "potentially_legitimate_requests": potentially_legitimate,
        "non_request_events": non_request_events,
        "unparseable_lines": unparseable,
        "logs_sha256": hashlib.sha256(content).hexdigest(),
    }


def _write(value: dict[str, Any]) -> None:
    if OUTPUT.exists() or OUTPUT.is_symlink():
        raise FileExistsError("C02-READINESS.json already exists")
    account = pwd.getpwnam("hermes")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=OUTPUT.parent,
        prefix=".C02-READINESS.",
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
    parser.add_argument("--minimum-observation-minutes", type=int, default=15)
    parser.add_argument("--controller-consumers", type=Path, default=CONTROLLER_CONSUMERS)
    parser.add_argument("--worker-consumers", type=Path, default=WORKER_CONSUMERS)
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("run with sudo for the approved secret-safe consumer inventory")
    if not 15 <= arguments.minimum_observation_minutes <= 1440:
        raise ValueError("observation window must be between 15 minutes and 24 hours")
    release = Path("/opt/engineering-control/source").resolve().parent.name
    if not FULL_SHA.fullmatch(release):
        raise RuntimeError("installed release is not an immutable Git SHA")
    c01 = _load_protected(C01_COMPLETE)
    if (
        not isinstance(c01, dict)
        or c01.get("status") != "COMPLETE"
        or c01.get("release_sha") != release
    ):
        raise ValueError("C-01 is not complete for the installed release")
    completed_at = parse_time(c01.get("completed_at"), "C-01 completion")
    observation = datetime.now(UTC) - completed_at
    if observation < timedelta(minutes=arguments.minimum_observation_minutes):
        raise RuntimeError("Caddy observation window after C-01 is too short")

    consumer_evidence: dict[str, dict[str, Any]] = {}
    for scope, path in (
        ("controller", arguments.controller_consumers),
        ("worker", arguments.worker_consumers),
    ):
        value = _load_protected(path)
        if (
            not isinstance(value, dict)
            or value.get("status") != "PASS"
            or value.get("release_sha") != release
            or value.get("scope") != scope
            or value.get("secret_values_persisted") is not False
            or parse_time(value.get("created_at"), f"{scope} consumer evidence") <= completed_at
        ):
            raise ValueError(f"{scope} consumer evidence is incomplete, stale, or mismatched")
        consumer_evidence[scope] = value

    container_matches, caddy = _container_consumers()
    caddyfile = _read_bounded(CADDYFILE, 64 * 1024)
    if DIRECT_SECRET.search(caddyfile):
        raise ValueError("Caddyfile contains an inline credential and cannot be backed up safely")
    if b"omni.musematic.ai" not in caddyfile or b"127.0.0.1:20128" not in caddyfile:
        raise ValueError("Caddyfile no longer matches the reviewed OmniRoute-only endpoint")
    if caddy != {
        "image": "caddy:2.11.4-alpine",
        "network_mode": "host",
        "restart_policy": "unless-stopped",
        "running": True,
    }:
        raise ValueError("OmniRoute Caddy container drifted from the reviewed configuration")
    logs = subprocess.run(  # noqa: S603 - fixed Docker logs query
        [
            "/usr/bin/docker",
            "logs",
            "--since",
            completed_at.isoformat(),
            "omniroute-caddy",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    combined_logs = logs.stdout + logs.stderr
    if len(combined_logs) > 64 * 1024 * 1024:
        raise RuntimeError("Caddy log evidence exceeded its safety bound")
    traffic = analyze_caddy_logs(combined_logs)
    status = (
        "PASS"
        if not container_matches
        and traffic["potentially_legitimate_requests"] == 0
        and traffic["unparseable_lines"] == 0
        else "BLOCKED"
    )
    payload = {
        "schema_version": 1,
        "status": status,
        "created_at": datetime.now(UTC).isoformat(),
        "release_sha": release,
        "c01_complete_sha256": digest(C01_COMPLETE),
        "observation_seconds": int(observation.total_seconds()),
        "consumer_evidence": {
            scope: {
                "path": str(path.resolve()),
                "sha256": digest(path.resolve()),
                "repositories_scanned": consumer_evidence[scope].get("repositories_scanned", []),
            }
            for scope, path in (
                ("controller", arguments.controller_consumers),
                ("worker", arguments.worker_consumers),
            )
        },
        "container_consumer_names": container_matches,
        "traffic": traffic,
        "caddyfile_sha256": hashlib.sha256(caddyfile).hexdigest(),
        "caddy_container": caddy,
        "secret_values_persisted": False,
    }
    _write(payload)
    if status != "PASS":
        raise SystemExit("BLOCKED C-02 readiness; inspect only the secret-free readiness manifest")
    print(f"PASS C-02 consumer and traffic readiness={OUTPUT}")


if __name__ == "__main__":
    main()
