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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from engineering_control.validation.c01 import FULL_SHA, digest

HOME = Path("/home/hermes")
C01_COMPLETE = HOME / "engineering-bootstrap/C01-COMPLETE.json"
READINESS = HOME / "engineering-bootstrap/C02-READINESS.json"
REMOVING = HOME / "engineering-bootstrap/C02-REMOVING.json"
COMPLETE = HOME / "engineering-bootstrap/C02-COMPLETE.json"
BACKUP_ROOT = HOME / "engineering-backups"
CADDYFILE = HOME / ".config/caddy/Caddyfile"
CADDY_DATA = HOME / ".local/share/caddy"
CADDY_STATE = HOME / ".local/state/caddy"
CONTAINER = "omniroute-caddy"
CONFIRMATION = "REMOVE C02 OMNIROUTE CADDY ONLY"
DIRECT_SECRET = re.compile(rb"(?i)(?:token|password|secret|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]+")


def _identity() -> tuple[int, int]:
    account = pwd.getpwnam("hermes")
    if os.geteuid() != account.pw_uid:
        raise PermissionError("run C-02 cutover as the hermes account, never as root")
    return account.pw_uid, account.pw_gid


def _read_safe(path: Path, maximum: int = 2 * 1024 * 1024) -> bytes:
    owner, _ = _identity()
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != owner
        or metadata.st_nlink != 1
        or metadata.st_size > maximum
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise PermissionError(f"unsafe C-02 input: {path.name}")
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
                raise ValueError(f"C-02 input exceeded its size bound: {path.name}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _load(path: Path) -> dict[str, Any]:
    if path.resolve().parent != (HOME / "engineering-bootstrap").resolve():
        raise ValueError("C-02 gate escaped the canonical bootstrap root")
    value = json.loads(_read_safe(path))
    if not isinstance(value, dict):
        raise ValueError("C-02 gate is not a JSON object")
    return value


def _write_exclusive(path: Path, value: dict[str, Any]) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True).encode() + b"\n"
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        remaining = memoryview(rendered)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write while persisting C-02 state")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_file(path: Path, value: bytes, mode: int = 0o600) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        remaining = memoryview(value)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write while persisting C-02 backup")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _inspect() -> dict[str, Any]:
    result = subprocess.run(  # noqa: S603 - fixed Docker query and container
        ["/usr/bin/docker", "inspect", CONTAINER],
        check=True,
        capture_output=True,
        timeout=30,
    )
    values = json.loads(result.stdout)
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise ValueError("Docker inspect returned invalid Caddy metadata")
    value = values[0]
    config = value.get("Config")
    host = value.get("HostConfig")
    state = value.get("State")
    if not isinstance(config, dict) or not isinstance(host, dict) or not isinstance(state, dict):
        raise ValueError("Caddy container metadata is incomplete")
    mount_destinations: list[str] = []
    mounts = value.get("Mounts")
    if isinstance(mounts, list):
        for mount in mounts:
            if isinstance(mount, dict) and isinstance(mount.get("Destination"), str):
                mount_destinations.append(mount["Destination"])
    sanitized = {
        "image": config.get("Image"),
        "image_id": value.get("Image"),
        "network_mode": host.get("NetworkMode"),
        "restart_policy": host.get("RestartPolicy", {}).get("Name"),
        "running": state.get("Running"),
        "mount_destinations": sorted(mount_destinations),
    }
    expected = {
        "image": "caddy:2.11.4-alpine",
        "network_mode": "host",
        "restart_policy": "unless-stopped",
        "running": True,
    }
    if any(sanitized.get(key) != expected_value for key, expected_value in expected.items()):
        raise ValueError("Caddy container drifted from its reviewed configuration")
    return sanitized


def execute(confirmation: str) -> Path:
    if confirmation != CONFIRMATION:
        raise ValueError(f"exact confirmation required: {CONFIRMATION}")
    _identity()
    release = Path("/opt/engineering-control/source").resolve().parent.name
    if not FULL_SHA.fullmatch(release):
        raise ValueError("installed release is not an immutable Git SHA")
    if REMOVING.exists() or REMOVING.is_symlink() or COMPLETE.exists() or COMPLETE.is_symlink():
        raise FileExistsError("C-02 mutation or completion state already exists")
    c01 = _load(C01_COMPLETE)
    readiness = _load(READINESS)
    if (
        c01.get("status") != "COMPLETE"
        or c01.get("release_sha") != release
        or readiness.get("status") != "PASS"
        or readiness.get("release_sha") != release
        or readiness.get("c01_complete_sha256") != digest(C01_COMPLETE)
        or readiness.get("secret_values_persisted") is not False
    ):
        raise ValueError("C-02 readiness does not bind to completed C-01 and the installed release")
    traffic = readiness.get("traffic")
    if (
        not isinstance(traffic, dict)
        or traffic.get("potentially_legitimate_requests") != 0
        or traffic.get("unparseable_lines") != 0
    ):
        raise ValueError("C-02 traffic evidence is not safe for endpoint removal")
    caddyfile = _read_safe(CADDYFILE, 64 * 1024)
    if DIRECT_SECRET.search(caddyfile):
        raise ValueError("Caddyfile contains an inline credential and cannot be backed up")
    if b"omni.musematic.ai" not in caddyfile or b"127.0.0.1:20128" not in caddyfile:
        raise ValueError("Caddyfile no longer matches the approved OmniRoute-only target")
    inspection = _inspect()
    if not CADDY_DATA.is_dir() or not CADDY_STATE.is_dir():
        raise RuntimeError("Caddy TLS/config state directories are missing before backup")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUP_ROOT / stamp / "controller/caddy"
    backup.mkdir(parents=True, mode=0o700)
    backup.chmod(0o700)
    _write_file(backup / "Caddyfile", caddyfile)
    metadata = json.dumps(inspection, indent=2, sort_keys=True).encode() + b"\n"
    _write_file(backup / "container-metadata.json", metadata)
    manifest = (
        "# C-02 Caddy backup\n\n"
        f"- Created: {datetime.now(UTC).isoformat()}\n"
        f"- Caddyfile SHA256: `{hashlib.sha256(caddyfile).hexdigest()}`\n"
        f"- Metadata SHA256: `{hashlib.sha256(metadata).hexdigest()}`\n"
        "- TLS `/data` and runtime `/config` state remain in place and were not copied.\n"
    ).encode()
    _write_file(backup / "BACKUP-MANIFEST.md", manifest)
    restore = (
        b"# Restore\n\n"
        b"Run the reviewed `caddy:2.11.4-alpine` host-network container with the retained "
        b"configuration, `/data`, and `/config` mounts, then verify ports 80/443.\n"
    )
    _write_file(backup / "RESTORE.md", restore)
    _write_exclusive(
        REMOVING,
        {
            "schema_version": 1,
            "status": "STARTED",
            "started_at": datetime.now(UTC).isoformat(),
            "release_sha": release,
            "readiness_sha256": digest(READINESS),
            "backup_path": str(backup),
        },
    )
    try:
        subprocess.run(  # noqa: S603 - exact approved container stop
            ["/usr/bin/docker", "stop", CONTAINER],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=90,
        )
        subprocess.run(  # noqa: S603 - exact approved container removal
            ["/usr/bin/docker", "rm", CONTAINER],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except Exception:
        subprocess.run(  # noqa: S603 - bounded compensation if container still exists
            ["/usr/bin/docker", "start", CONTAINER],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        raise
    absent = subprocess.run(  # noqa: S603 - fixed absence check
        ["/usr/bin/docker", "inspect", CONTAINER],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    if absent.returncode == 0:
        raise RuntimeError("Caddy container remains after approved removal")
    if not CADDY_DATA.is_dir() or not CADDY_STATE.is_dir():
        raise RuntimeError("protected Caddy TLS/config state disappeared")
    _write_exclusive(
        COMPLETE,
        {
            "schema_version": 1,
            "status": "COMPLETE",
            "completed_at": datetime.now(UTC).isoformat(),
            "release_sha": release,
            "readiness_sha256": digest(READINESS),
            "backup_path": str(backup),
            "backup_manifest_sha256": digest(backup / "BACKUP-MANIFEST.md"),
            "caddy_data_preserved": True,
            "caddy_state_preserved": True,
        },
    )
    return backup


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirmation", default="")
    arguments = parser.parse_args()
    backup = execute(arguments.confirmation)
    print(f"PASS C-02 Caddy container removed; rollback={backup}")


if __name__ == "__main__":
    main()
