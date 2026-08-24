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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from engineering_control.validation.c01 import FULL_SHA

HOME = Path("/home/hermes")
PATTERN = re.compile(
    rb"(?i)omni\.musematic\.ai|(?:localhost|127\.0\.0\.1):201(?:28|31|32)|\bomniroute\b"
)
RUNTIME_SUFFIXES = {
    ".conf",
    ".ini",
    ".json",
    ".py",
    ".service",
    ".sh",
    ".toml",
    ".yaml",
    ".yml",
}
RUNTIME_NAMES = {"Caddyfile", "Dockerfile", "compose.yml", "docker-compose.yml"}
REPOSITORY_NAMES = {
    "falcone",
    "falcone-charts",
    "musematic",
    "musematic-deploy",
    "musematic-flowbench",
}
ALLOWLISTED_ENGINEERING_SCRIPTS = {
    "build-c01-acceptance.py",
    "execute-c01-cutover.py",
    "build-c01-post-stop-acceptance.py",
    "prepare-hermes-c01-cutover.py",
    "verify-hermes-policy.py",
    "collect-c02-consumers.py",
    "validate-c02-readiness.py",
    "execute-c02-cutover.py",
    "build-c02-post-remove-acceptance.py",
}
MAX_SCAN_FILE_BYTES = 64 * 1024 * 1024
SCAN_CHUNK_BYTES = 64 * 1024
PATTERN_OVERLAP_BYTES = 256


def _contains_pattern(path: Path, maximum: int = MAX_SCAN_FILE_BYTES) -> bool:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > maximum:
        raise PermissionError(f"unsafe or oversized consumer-scan input: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or current.st_size > maximum
            or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise PermissionError(f"consumer-scan input changed during validation: {path}")
        overlap = b""
        total = 0
        while True:
            chunk = os.read(descriptor, min(SCAN_CHUNK_BYTES, maximum + 1 - total))
            if not chunk:
                return False
            total += len(chunk)
            if total > maximum:
                raise ValueError(f"consumer-scan input exceeded its size bound: {path}")
            candidate = overlap + chunk
            if PATTERN.search(candidate):
                return True
            overlap = candidate[-PATTERN_OVERLAP_BYTES:]
    finally:
        os.close(descriptor)


def _candidate(path: Path) -> bool:
    return path.name in RUNTIME_NAMES or path.suffix.lower() in RUNTIME_SUFFIXES


def _scan_file(path: Path, matches: set[str]) -> None:
    if _candidate(path) and _contains_pattern(path):
        matches.add(str(path))


def _scan_tree(root: Path, matches: set[str]) -> None:
    if not root.exists():
        return
    if root.is_file():
        _scan_file(root, matches)
        return
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = [
            name
            for name in names
            if not name.startswith(".")
            and name not in {"__pycache__", "backups", "evidence", "logs"}
        ]
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                _scan_file(path, matches)


def _repository_roots() -> tuple[Path, ...]:
    discovered: set[Path] = set()
    for parent in (HOME / "projects", HOME / "repos", HOME / "engineering"):
        if not parent.is_dir():
            continue
        for path in parent.rglob(".git"):
            if path.exists() and path.parent.name in REPOSITORY_NAMES:
                discovered.add(path.parent)
    return tuple(sorted(discovered))


def _scan_repository(root: Path, matches: set[str]) -> None:
    result = subprocess.run(  # noqa: S603 - fixed Git query against discovered repository
        [
            "/usr/bin/git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"repository returned unsafe path beneath {root.name}")
        path = root / relative
        if path.is_file() and not path.is_symlink():
            _scan_file(path, matches)


def _scan_engineering_scripts(root: Path, matches: set[str], excluded_targets: set[str]) -> None:
    if not root.is_dir():
        return
    for path in root.iterdir():
        if not path.is_file() or path.is_symlink():
            continue
        if path.name in ALLOWLISTED_ENGINEERING_SCRIPTS:
            if _candidate(path) and _contains_pattern(path):
                excluded_targets.add(str(path))
            continue
        _scan_file(path, matches)


def _container_matches(scope: str) -> list[str]:
    listing = subprocess.run(  # noqa: S603 - fixed Docker query
        ["/usr/bin/docker", "ps", "-a", "--format", "{{.Names}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    matches: list[str] = []
    for name in sorted(line for line in listing.stdout.splitlines() if line):
        inspection = subprocess.run(  # noqa: S603 - fixed Docker query, discovered name
            ["/usr/bin/docker", "inspect", name],
            check=True,
            capture_output=True,
            timeout=30,
        )
        if PATTERN.search(inspection.stdout) and not (
            scope == "controller" and name == "omniroute-caddy"
        ):
            matches.append(name)
    return matches


def _legacy_ports() -> list[int]:
    result = subprocess.run(  # noqa: S603 - fixed socket inventory
        ["/usr/bin/ss", "-H", "-ltn"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    found: set[int] = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        try:
            port = int(fields[3].rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        if port in {20128, 20131, 20132}:
            found.add(port)
    return sorted(found)


def _write(path: Path, value: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"consumer evidence already exists: {path.name}")
    account = pwd.getpwnam("hermes")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    os.chown(temporary, account.pw_uid, account.pw_gid)
    os.replace(temporary, path)


def collect(scope: str) -> dict[str, Any]:
    release = Path("/opt/engineering-control/source").resolve().parent.name
    if not FULL_SHA.fullmatch(release):
        raise RuntimeError("installed release is not an immutable Git SHA")
    matches: set[str] = set()
    excluded_targets: set[str] = set()
    for root in (
        Path("/etc/systemd/system"),
        HOME / ".config/systemd/user",
        Path("/etc/engineering-control"),
        HOME / ".hermes/config.yaml",
        HOME / ".hermes/project-profiles",
        HOME / ".hermes/skills",
        HOME / ".hermes/commands",
        HOME / ".hermes/tools",
        HOME / ".hermes/hooks/engineering-control",
        HOME / ".hermes/plugins/engineering-control",
        Path("/etc/cron.d"),
        Path("/etc/crontab"),
        Path("/var/spool/cron/crontabs/hermes"),
    ):
        _scan_tree(root, matches)
    scripts = HOME / "engineering/engineering-control/scripts"
    _scan_engineering_scripts(scripts, matches, excluded_targets)
    repositories = _repository_roots()
    for repository in repositories:
        _scan_repository(repository, matches)
    containers = _container_matches(scope)
    ports = _legacy_ports()
    retired_unit = str(HOME / ".config/systemd/user/omniroute.service")
    if scope == "controller" and retired_unit in matches:
        matches.remove(retired_unit)
        excluded_targets.add(retired_unit)
    locations = sorted(matches)
    return {
        "schema_version": 1,
        "status": "PASS" if not locations and not containers and not ports else "BLOCKED",
        "created_at": datetime.now(UTC).isoformat(),
        "release_sha": release,
        "scope": scope,
        "consumer_match_locations": locations,
        "container_consumer_names": containers,
        "legacy_listening_ports": ports,
        "repositories_scanned": [str(path) for path in repositories],
        "excluded_reviewed_cleanup_targets": sorted(excluded_targets),
        "collector_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "maximum_scanned_file_bytes": MAX_SCAN_FILE_BYTES,
        "secret_values_persisted": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("controller", "worker"), required=True)
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("run with sudo to inspect protected runtime configuration safely")
    output = HOME / f"engineering-bootstrap/C02-CONSUMERS-{arguments.scope}.json"
    value = collect(arguments.scope)
    _write(output, value)
    if value["status"] != "PASS":
        raise SystemExit(f"BLOCKED C-02 {arguments.scope} consumers; inspect {output}")
    print(f"PASS C-02 {arguments.scope} consumer evidence={output}")


if __name__ == "__main__":
    main()
