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

from engineering_control.validation.c01 import (
    FULL_SHA,
    digest,
    validate_c01_manifest,
    validate_post_stop_manifest,
)

HERMES_HOME = Path("/home/hermes")
PRE_ACCEPTANCE = HERMES_HOME / "engineering-bootstrap/C01-ACCEPTANCE.json"
STOPPED_STATE = HERMES_HOME / "engineering-bootstrap/C01-STOPPED.json"
POST_ACCEPTANCE = HERMES_HOME / "engineering-bootstrap/C01-POST-STOP-ACCEPTANCE.json"
COMPLETE_STATE = HERMES_HOME / "engineering-bootstrap/C01-COMPLETE.json"
UNINSTALLING_STATE = HERMES_HOME / "engineering-bootstrap/C01-UNINSTALLING.json"
BACKUP_ROOT = HERMES_HOME / "engineering-backups"
UNIT = HERMES_HOME / ".config/systemd/user/omniroute.service"
WRAPPER = HERMES_HOME / ".local/bin/omniroute"
NODE_PREFIX = HERMES_HOME / ".local/node-v24"
NPM = NODE_PREFIX / "bin/npm"
PACKAGE = NODE_PREFIX / "lib/node_modules/omniroute"
LAUNCHER = NODE_PREFIX / "bin/omniroute"
PACKAGE_VERSION = "3.8.49"
WORKER_SERVICES = (
    "engineering-worker@codex.service",
    "engineering-worker@claude.service",
    "engineering-worker@browser.service",
    "engineering-worker@scan.service",
    "engineering-worker@delivery.service",
    "engineering-release-worker.service",
)
STOP_CONFIRMATION = "STOP C01 OMNIROUTE ONLY"
UNINSTALL_CONFIRMATION = "UNINSTALL C01 OMNIROUTE PACKAGE"
DIRECT_SECRET = re.compile(rb"(?i)(?:token|password|secret|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]+")


def _identity() -> tuple[int, int]:
    account = pwd.getpwnam("hermes")
    if os.geteuid() != account.pw_uid:
        raise PermissionError("run C-01 cutover as the hermes account, never as root")
    return account.pw_uid, account.pw_gid


def _release_identities() -> tuple[str, str]:
    release = Path("/opt/engineering-control/source").resolve().parent.name
    pack = Path("/opt/engineering-agent-pack/source").resolve().parent.name
    if not FULL_SHA.fullmatch(release) or not FULL_SHA.fullmatch(pack):
        raise ValueError("installed releases are not immutable Git SHAs")
    return release, pack


def _read_safe(path: Path, *, maximum: int, owner: int) -> bytes:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != owner
        or metadata.st_nlink != 1
        or metadata.st_size > maximum
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise PermissionError(f"unsafe C-01 input: {path.name}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise PermissionError(f"C-01 input changed during validation: {path.name}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise ValueError(f"C-01 input exceeded its size bound: {path.name}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _load_json(path: Path, *, root: Path, owner: int) -> dict[str, Any]:
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("C-01 manifest escaped its canonical root")
    value = json.loads(_read_safe(path, maximum=2 * 1024 * 1024, owner=owner))
    if not isinstance(value, dict):
        raise ValueError("C-01 manifest is not a JSON object")
    return value


def _write_exclusive(path: Path, content: bytes, mode: int) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write while persisting C-01 state")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True).encode() + b"\n"
    _write_exclusive(path, rendered, 0o600)


def _systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["/usr/bin/systemctl", "--user", *arguments]
    return subprocess.run(  # noqa: S603 - fixed executable and bounded arguments
        command,
        check=check,
        capture_output=True,
        text=True,
        timeout=45,
        env={
            "HOME": str(HERMES_HOME),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "XDG_RUNTIME_DIR": f"/run/user/{os.geteuid()}",
        },
    )


def _service_state() -> tuple[bool, bool]:
    active = _systemctl("is-active", "omniroute.service", check=False).stdout.strip()
    enabled = _systemctl("is-enabled", "omniroute.service", check=False).stdout.strip()
    if active not in {"active", "inactive", "failed"}:
        raise RuntimeError("OmniRoute active state could not be determined")
    if enabled not in {"enabled", "disabled"}:
        raise RuntimeError("OmniRoute enablement state could not be determined")
    return active == "active", enabled == "enabled"


def _user_active_enter_monotonic(unit: str) -> int:
    result = _systemctl("show", unit, "--property=ActiveEnterTimestampMonotonic", "--value")
    value = result.stdout.strip()
    if not value.isdigit() or int(value) <= 0:
        raise RuntimeError(f"user service {unit} has no active-enter marker")
    return int(value)


def _worker_service_markers() -> dict[str, int]:
    markers: dict[str, int] = {}
    for unit in WORKER_SERVICES:
        result = subprocess.run(  # noqa: S603 - fixed SSH host, command, unit allowlist
            [
                "/usr/bin/ssh",
                "-o",
                "BatchMode=yes",
                "worker",
                "systemctl",
                "show",
                unit,
                "--property=ActiveEnterTimestampMonotonic",
                "--value",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        value = result.stdout.strip()
        if not value.isdigit() or int(value) <= 0:
            raise RuntimeError(f"worker service {unit} has no active-enter marker")
        markers[unit] = int(value)
    return markers


def _package_version(owner: int) -> str:
    value = json.loads(_read_safe(PACKAGE / "package.json", maximum=256 * 1024, owner=owner))
    version = value.get("version") if isinstance(value, dict) else None
    if not isinstance(version, str):
        raise ValueError("OmniRoute package metadata has no version")
    return version


def _validate_paths(owner: int) -> tuple[bytes, bytes]:
    unit = _read_safe(UNIT, maximum=64 * 1024, owner=owner)
    wrapper = _read_safe(WRAPPER, maximum=4096, owner=owner)
    if DIRECT_SECRET.search(unit) or DIRECT_SECRET.search(wrapper):
        raise ValueError("C-01 backup candidate appears to contain an inline credential")
    expected_paths = (
        b"/home/hermes/.local/node-v24/bin/node",
        b"/home/hermes/.local/node-v24/lib/node_modules/omniroute/bin/omniroute.mjs",
    )
    if any(value not in wrapper for value in expected_paths):
        raise ValueError("independent OmniRoute wrapper no longer matches its recorded behavior")
    launcher = LAUNCHER.lstat()
    if not stat.S_ISLNK(launcher.st_mode) or LAUNCHER.resolve() != PACKAGE / "bin/omniroute.mjs":
        raise ValueError("npm OmniRoute launcher does not resolve to the reviewed package")
    if _package_version(owner) != PACKAGE_VERSION:
        raise ValueError("installed OmniRoute package version changed after review")
    return unit, wrapper


def stop_phase(confirmation: str) -> Path:
    if confirmation != STOP_CONFIRMATION:
        raise ValueError(f"exact confirmation required: {STOP_CONFIRMATION}")
    owner, _ = _identity()
    release, pack = _release_identities()
    if STOPPED_STATE.exists() or STOPPED_STATE.is_symlink():
        raise FileExistsError("C01-STOPPED.json already exists")
    manifest = _load_json(PRE_ACCEPTANCE, root=PRE_ACCEPTANCE.parent, owner=owner)
    validate_c01_manifest(manifest, release, pack)
    active, enabled = _service_state()
    if not active or not enabled:
        raise RuntimeError("OmniRoute is not in the reviewed active/enabled pre-stop state")
    unit, wrapper = _validate_paths(owner)
    hermes_marker = _user_active_enter_monotonic("hermes-gateway.service")
    worker_markers = _worker_service_markers()

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUP_ROOT / stamp / "controller/omniroute"
    backup.mkdir(parents=True, mode=0o700)
    backup.chmod(0o700)
    _write_exclusive(backup / "omniroute.service", unit, 0o600)
    _write_exclusive(backup / "omniroute-wrapper.pre-stop-copy", wrapper, 0o600)
    backup_manifest = (
        "# C-01 OmniRoute pre-stop backup\n\n"
        f"- Created: {datetime.now(UTC).isoformat()}\n"
        f"- Unit SHA256: `{hashlib.sha256(unit).hexdigest()}`\n"
        f"- Wrapper SHA256: `{hashlib.sha256(wrapper).hexdigest()}`\n"
        f"- Package version: `{PACKAGE_VERSION}`\n"
        "- `.omniroute` was not copied or modified.\n"
        "- The original independent wrapper is moved here only after post-stop acceptance.\n"
    ).encode()
    _write_exclusive(backup / "BACKUP-MANIFEST.md", backup_manifest, 0o600)
    restore = (
        b"# Restore\n\n"
        b"Restore the unit and wrapper, reinstall exactly `omniroute@3.8.49` under the Node 24 "
        b"prefix if removed, run user daemon-reload, then enable/start `omniroute.service`.\n"
    )
    _write_exclusive(backup / "RESTORE.md", restore, 0o600)

    try:
        _systemctl("disable", "--now", "omniroute.service")
        active, enabled = _service_state()
        if active or enabled:
            raise RuntimeError("OmniRoute remained active or enabled after phase A")
        stopped_at = datetime.now(UTC).isoformat()
        state = {
            "schema_version": 1,
            "status": "STOPPED_NOT_UNINSTALLED",
            "stopped_at": stopped_at,
            "release_sha": release,
            "agent_pack_sha": pack,
            "pre_acceptance_sha256": digest(PRE_ACCEPTANCE),
            "backup_path": str(backup),
            "backup_manifest_sha256": digest(backup / "BACKUP-MANIFEST.md"),
            "package_version": PACKAGE_VERSION,
            "hermes_active_enter_monotonic_before_stop": hermes_marker,
            "worker_active_enter_monotonic_before_stop": worker_markers,
            "wrapper_preserved": WRAPPER.is_file(),
            "package_preserved": PACKAGE.is_dir(),
            "omniroute_state_preserved": (HERMES_HOME / ".omniroute").is_dir(),
        }
        if not all(
            state[key]
            for key in ("wrapper_preserved", "package_preserved", "omniroute_state_preserved")
        ):
            raise RuntimeError("phase A did not preserve every rollback artifact")
        _write_json_exclusive(STOPPED_STATE, state)
    except Exception:
        _systemctl("enable", "--now", "omniroute.service", check=False)
        raise
    return backup


def uninstall_phase(confirmation: str) -> Path:
    if confirmation != UNINSTALL_CONFIRMATION:
        raise ValueError(f"exact confirmation required: {UNINSTALL_CONFIRMATION}")
    owner, _ = _identity()
    release, pack = _release_identities()
    if COMPLETE_STATE.exists() or COMPLETE_STATE.is_symlink():
        raise FileExistsError("C01-COMPLETE.json already exists")
    stopped = _load_json(STOPPED_STATE, root=STOPPED_STATE.parent, owner=owner)
    if (
        stopped.get("status") != "STOPPED_NOT_UNINSTALLED"
        or stopped.get("release_sha") != release
        or stopped.get("agent_pack_sha") != pack
    ):
        raise ValueError("C-01 stopped state does not bind to the installed release")
    post = _load_json(POST_ACCEPTANCE, root=POST_ACCEPTANCE.parent, owner=owner)
    validate_post_stop_manifest(post, release, pack, digest(STOPPED_STATE))
    if any(_service_state()):
        raise RuntimeError("OmniRoute must remain stopped and disabled before uninstall")
    _, wrapper = _validate_paths(owner)
    backup = Path(str(stopped.get("backup_path", ""))).resolve()
    if not backup.is_dir() or not backup.is_relative_to(BACKUP_ROOT.resolve()):
        raise ValueError("C-01 backup path is invalid")
    moved_wrapper = backup / "omniroute-wrapper"
    if moved_wrapper.exists() or moved_wrapper.is_symlink():
        raise FileExistsError("C-01 moved-wrapper target already exists")
    if UNINSTALLING_STATE.exists() or UNINSTALLING_STATE.is_symlink():
        raise FileExistsError("C01-UNINSTALLING.json already exists; inspect before retry")
    _write_json_exclusive(
        UNINSTALLING_STATE,
        {
            "schema_version": 1,
            "status": "STARTED",
            "started_at": datetime.now(UTC).isoformat(),
            "release_sha": release,
            "stopped_state_sha256": digest(STOPPED_STATE),
            "post_stop_acceptance_sha256": digest(POST_ACCEPTANCE),
            "backup_path": str(backup),
        },
    )
    os.replace(WRAPPER, moved_wrapper)
    moved_wrapper.chmod(0o700)
    if moved_wrapper.read_bytes() != wrapper:
        raise RuntimeError("moved wrapper content changed")
    try:
        subprocess.run(  # noqa: S603 - exact reviewed npm executable/package
            [str(NPM), "uninstall", "--global", "omniroute"],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
            env={
                "HOME": str(HERMES_HOME),
                "PATH": f"{NODE_PREFIX / 'bin'}:/usr/local/bin:/usr/bin:/bin",
            },
        )
    except Exception:
        if not WRAPPER.exists():
            os.replace(moved_wrapper, WRAPPER)
        raise
    if PACKAGE.exists() or LAUNCHER.exists() or WRAPPER.exists():
        raise RuntimeError("OmniRoute package, launcher, or wrapper remains after uninstall")
    if not (HERMES_HOME / ".omniroute").is_dir():
        raise RuntimeError("protected .omniroute rollback state disappeared")
    state = {
        "schema_version": 1,
        "status": "COMPLETE",
        "completed_at": datetime.now(UTC).isoformat(),
        "release_sha": release,
        "agent_pack_sha": pack,
        "stopped_state_sha256": digest(STOPPED_STATE),
        "post_stop_acceptance_sha256": digest(POST_ACCEPTANCE),
        "backup_path": str(backup),
        "moved_wrapper_sha256": digest(moved_wrapper),
        "omniroute_state_preserved": True,
    }
    _write_json_exclusive(COMPLETE_STATE, state)
    return backup


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("stop", "uninstall"), required=True)
    parser.add_argument("--confirmation", default="")
    arguments = parser.parse_args()
    if arguments.phase == "stop":
        backup = stop_phase(arguments.confirmation)
        print(f"PASS C-01 phase A stopped/disabled only; backup={backup}")
    else:
        backup = uninstall_phase(arguments.confirmation)
        print(f"PASS C-01 phase B package uninstalled; rollback={backup}")


if __name__ == "__main__":
    main()
