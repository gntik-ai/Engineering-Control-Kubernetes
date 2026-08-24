#!/usr/bin/env python3
"""Fail closed when an active OmniRoute dependency is present."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
from pathlib import Path
from typing import Any

INDICATORS = {
    "omniroute": re.compile(r"omniroute", re.IGNORECASE),
    "omni.musematic.ai": re.compile(r"omni\.musematic\.ai", re.IGNORECASE),
    "custom:omniroute": re.compile(r"custom:omniroute", re.IGNORECASE),
    "port-20128": re.compile(r"(?<!\d)20128(?!\d)"),
    "port-20131": re.compile(r"(?<!\d)20131(?!\d)"),
    "port-20132": re.compile(r"(?<!\d)20132(?!\d)"),
}
MAX_FILE_BYTES = 2 * 1024 * 1024
TEXT_SUFFIXES = {
    "",
    ".conf",
    ".env",
    ".ini",
    ".json",
    ".py",
    ".service",
    ".sh",
    ".toml",
    ".yaml",
    ".yml",
}
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "vendor",
    "backups",
    "evidence",
    "reports",
    "quarantine",
    "engineering-backups",
    "historical-update-bundles",
}
LEGACY_SCRIPT_NAMES = {
    "build-c01-acceptance.py",
    "build-c01-post-stop-acceptance.py",
    "build-c02-post-remove-acceptance.py",
    "collect-c02-consumers.py",
    "execute-c01-cutover.py",
    "execute-c02-cutover.py",
    "prepare-hermes-c01-cutover.py",
    "validate-c02-readiness.py",
    "verify-hermes-policy.py",
    "verify-no-omniroute.py",
    "verify-no-omniroute.sh",
}
LEGACY_SOURCE_PATHS = {
    "src/engineering_control/validation/c01.py",
}


def _run(argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - argv is a fixed local inspection command
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(argv, 127, "", type(error).__name__)


def _indicators(value: str | bytes) -> list[str]:
    text = value.decode(errors="replace") if isinstance(value, bytes) else value
    return sorted(name for name, pattern in INDICATORS.items() if pattern.search(text))


def _prefixed(root: Path, value: str) -> Path:
    return root / value.lstrip("/")


class Validator:
    def __init__(self, scope: str, root: Path) -> None:
        self.scope = scope
        self.root = root.resolve()
        self.real_host = self.root == Path("/")
        self.findings: list[dict[str, Any]] = []
        self.blockers: list[str] = []
        self.scanned_files = 0

    def finding(self, kind: str, target: str, indicators: list[str]) -> None:
        item = {"kind": kind, "target": target, "indicators": indicators}
        if item not in self.findings:
            self.findings.append(item)

    @staticmethod
    def _excluded(path: Path) -> bool:
        return bool(set(path.parts) & EXCLUDED_PARTS) or "state-snapshots" in path.parts

    @staticmethod
    def _legacy_script(path: Path) -> bool:
        rendered = str(path)
        return (
            path.name in LEGACY_SCRIPT_NAMES
            or any(rendered.endswith(value) for value in LEGACY_SOURCE_PATHS)
            or "/tests/" in rendered
            or "/docs/" in rendered
        )

    @staticmethod
    def _without_policy_invocations(data: bytes) -> bytes:
        """Ignore only calls to this negative policy, not the rest of an active script."""
        return b"\n".join(
            line
            for line in data.splitlines()
            if b"verify-no-omniroute.py" not in line and b"verify-no-omniroute.sh" not in line
        )

    def scan_file(self, path: Path) -> None:
        if self._excluded(path) or self._legacy_script(path):
            return
        try:
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > MAX_FILE_BYTES
                or path.suffix.lower() not in TEXT_SUFFIXES
            ):
                return
            data = path.read_bytes()
        except (OSError, PermissionError):
            self.blockers.append(f"unreadable active file: {path}")
            return
        self.scanned_files += 1
        matched = _indicators(self._without_policy_invocations(data))
        if matched:
            self.finding("active-file", str(path), matched)

    def scan_tree(self, path: Path) -> None:
        if not path.exists():
            return
        if path.is_file():
            self.scan_file(path)
            return
        for directory, names, files in os.walk(path, followlinks=False):
            names[:] = [
                name
                for name in names
                if name not in EXCLUDED_PARTS and name not in {"docs", "tests", "state-snapshots"}
            ]
            for name in files:
                candidate = Path(directory) / name
                if not candidate.is_symlink():
                    self.scan_file(candidate)

    def static_paths(self) -> None:
        common = [
            "/etc/systemd/system",
            "/etc/systemd/user",
            "/etc/engineering-control",
            "/etc/cron.d",
            "/etc/cron.hourly",
            "/etc/cron.daily",
            "/etc/cron.weekly",
            "/etc/cron.monthly",
            "/etc/crontab",
            "/etc/hosts",
            "/var/spool/cron",
            "/usr/local/bin",
            "/usr/bin",
            "/home/hermes/.local/bin",
            "/home/hermes/engineering/engineering-control/src",
            "/home/hermes/engineering/engineering-control/scripts",
        ]
        controller = [
            "/home/hermes/.config/systemd/user",
            "/home/hermes/.hermes/config.yaml",
            "/home/hermes/.hermes/.env",
            "/home/hermes/.hermes/auth.json",
            "/home/hermes/.hermes/context_length_cache.yaml",
            "/home/hermes/.hermes/provider_models_cache.json",
            "/home/hermes/.hermes/project-profiles",
            "/home/hermes/.hermes/skills",
            "/home/hermes/.hermes/flows",
            "/home/hermes/.hermes/workflows",
            "/home/hermes/.hermes/commands",
            "/home/hermes/.hermes/cron",
            "/home/hermes/.hermes/hooks",
            "/home/hermes/.hermes/plugins",
            "/home/hermes/.hermes/tools",
            "/home/hermes/.bashrc",
            "/home/hermes/.profile",
            "/home/hermes/.bash_profile",
        ]
        worker_hermes_markers = [
            "/home/hermes/.local/bin/hermes",
            "/usr/local/bin/hermes",
            "/usr/bin/hermes",
            "/home/hermes/.config/systemd/user/hermes-gateway.service",
        ]
        node_bin_root = _prefixed(self.root, "/home/hermes/.local")
        worker_has_hermes = any(
            _prefixed(self.root, value).exists() for value in worker_hermes_markers
        ) or any(node_bin_root.glob("node-v*/bin/hermes"))
        inspect_hermes = self.scope == "controller" or worker_has_hermes
        for raw in common + (controller if inspect_hermes else []):
            self.scan_tree(_prefixed(self.root, raw))

    def residual_paths(self) -> None:
        targets = [
            "/home/hermes/.omniroute",
            "/opt/omniroute",
            "/home/hermes/.local/bin/omniroute",
            "/home/hermes/.local/bin/omniroute-reset-password",
            "/home/hermes/.config/systemd/user/omniroute.service",
            "/etc/systemd/system/omniroute-migration-test.service",
        ]
        for raw in targets:
            path = _prefixed(self.root, raw)
            if path.exists() or path.is_symlink():
                self.finding("active-residual-path", raw, _indicators(raw))
        package_parents = [
            "/usr/lib/node_modules",
            "/usr/local/lib/node_modules",
            "/home/hermes/.local/lib/node_modules",
        ]
        node_root = _prefixed(self.root, "/home/hermes/.local")
        if node_root.is_dir():
            package_parents.extend(
                "/" + str(path.relative_to(self.root))
                for path in node_root.glob("node-v*/lib/node_modules")
            )
        for raw in package_parents:
            path = _prefixed(self.root, raw) / "omniroute"
            if path.exists() or path.is_symlink():
                self.finding("installed-package-path", str(path), ["omniroute"])

    def runtime_units(self) -> None:
        for argv, label in (
            (["systemctl", "list-unit-files", "--all", "--no-legend"], "system-unit"),
            (["systemctl", "list-timers", "--all", "--no-legend"], "system-timer"),
            (["systemctl", "list-sockets", "--all", "--no-legend"], "system-socket"),
        ):
            result = _run(argv)
            if result.returncode != 0:
                self.blockers.append(f"{label} inspection failed")
            for line in result.stdout.splitlines():
                matched = _indicators(line)
                if matched:
                    self.finding(label, line.split()[0], matched)
        user_names = ["hermes"]
        if self.scope == "worker":
            user_names.extend(
                [
                    "codex-runner",
                    "claude-runner",
                    "browser-runner",
                    "scan-runner",
                    "delivery-runner",
                    "release-runner",
                ]
            )
        for user in user_names:
            try:
                account = pwd.getpwnam(user)
            except KeyError:
                continue
            home = Path(account.pw_dir)
            self.scan_tree(home / ".config/systemd/user")
            runtime = f"/run/user/{account.pw_uid}"
            if Path(runtime).is_dir():
                for subcommand, label in (
                    (["list-unit-files", "--all", "--no-legend"], "user-unit"),
                    (["list-timers", "--all", "--no-legend"], "user-timer"),
                    (["list-sockets", "--all", "--no-legend"], "user-socket"),
                ):
                    result = _run(
                        [
                            "runuser",
                            "-u",
                            user,
                            "--",
                            "env",
                            f"XDG_RUNTIME_DIR={runtime}",
                            "systemctl",
                            "--user",
                            *subcommand,
                        ]
                    )
                    if result.returncode != 0:
                        self.blockers.append(f"{label} inspection failed for {user}")
                    for line in result.stdout.splitlines():
                        matched = _indicators(line)
                        if matched:
                            self.finding(label, f"user={user} {line.split()[0]}", matched)
        if shutil.which("crontab"):
            for user in ["root", *user_names]:
                result = _run(["crontab", "-u", user, "-l"])
                if result.returncode not in {0, 1}:
                    self.blockers.append(f"crontab inspection failed for {user}")
                for line in result.stdout.splitlines():
                    matched = _indicators(line)
                    if matched:
                        self.finding("cron", f"{user} crontab", matched)
        atq = _run(["atq"])
        if shutil.which("atq") and atq.returncode != 0:
            self.blockers.append("at job inspection failed")

    def runtime_processes(self) -> None:
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            try:
                cmdline = (path / "cmdline").read_bytes().replace(b"\0", b" ")
                if b"verify-no-omniroute" not in cmdline:
                    matched = _indicators(cmdline)
                    if matched:
                        self.finding("process", f"pid={path.name}", matched)
                for raw in (path / "environ").read_bytes().split(b"\0"):
                    if b"=" not in raw:
                        continue
                    key, value = raw.split(b"=", 1)
                    matched = _indicators(value)
                    if matched:
                        self.finding(
                            "process-environment",
                            f"pid={path.name} key={key.decode(errors='replace')}",
                            matched,
                        )
            except (OSError, PermissionError, ProcessLookupError):
                continue

    def runtime_network_and_containers(self) -> None:
        sockets = _run(["ss", "-H", "-lntup"])
        if sockets.returncode != 0:
            self.blockers.append("listener inspection failed")
        for line in sockets.stdout.splitlines():
            matched = [
                indicator
                for indicator in _indicators(line)
                if indicator in {"port-20128", "port-20131", "port-20132"}
            ]
            if matched:
                self.finding("listener", "OmniRoute legacy port", matched)
        if not shutil.which("docker"):
            if Path("/var/run/docker.sock").exists():
                self.blockers.append("Docker socket exists but Docker CLI is unavailable")
            return
        listing = _run(["docker", "ps", "-a", "--format", "{{.Names}}"])
        if listing.returncode != 0:
            self.blockers.append("container inspection failed")
            return
        for name in [line for line in listing.stdout.splitlines() if line]:
            inspection = _run(["docker", "inspect", name])
            matched = _indicators(inspection.stdout)
            if matched:
                self.finding("container", name, matched)

    def runtime_packages_and_commands(self) -> None:
        users = ["root", "hermes"]
        users.extend(
            name
            for name in (
                "codex-runner",
                "claude-runner",
                "browser-runner",
                "scan-runner",
                "delivery-runner",
                "release-runner",
            )
            if self.scope == "worker"
        )
        for user in users:
            try:
                account = pwd.getpwnam(user)
            except KeyError:
                continue
            home = Path(account.pw_dir)
            bin_candidates = [
                home / ".local/bin",
                home / ".bun/bin",
                home / ".local/share/pnpm",
                Path("/usr/local/bin"),
                Path("/usr/bin"),
            ]
            bin_candidates[1:1] = sorted((home / ".local").glob("node-v*/bin"))
            bin_candidates[1:1] = sorted((home / ".nvm/versions/node").glob("*/bin"))
            for parent in bin_candidates:
                if not parent.is_dir():
                    continue
                for path in parent.glob("omniroute*"):
                    if path.exists() or path.is_symlink():
                        self.finding(
                            "executable-or-wrapper",
                            f"user={user} path={path}",
                            ["omniroute"],
                        )
            result = _run(
                [
                    "runuser",
                    "-u",
                    user,
                    "--",
                    "env",
                    "PATH=/home/hermes/.local/bin:/home/hermes/.local/node-v24/bin:/usr/local/bin:/usr/bin:/bin",
                    "bash",
                    "--noprofile",
                    "--norc",
                    "-c",
                    "command -v omniroute",
                ]
            )
            if result.returncode == 0 and result.stdout.strip():
                self.finding("executable-resolution", f"user={user}", ["omniroute"])
            manager_commands = {
                "npm": ["npm", "list", "-g", "--depth=0", "--json"],
                "pnpm": ["pnpm", "list", "-g", "--depth=0", "--json"],
                "bun": ["bun", "pm", "ls", "-g"],
                "pip": ["python3", "-m", "pip", "list", "--format=json"],
                "pipx": ["pipx", "list", "--json"],
            }
            search_path = ":".join(str(path) for path in bin_candidates)
            for manager, command in manager_commands.items():
                probe = _run(
                    [
                        "runuser",
                        "-u",
                        user,
                        "--",
                        "env",
                        f"HOME={home}",
                        f"PATH={search_path}:/bin",
                        *command,
                    ],
                    60,
                )
                if probe.returncode != 127 and _indicators(probe.stdout):
                    self.finding("package-manager", f"user={user} manager={manager}", ["omniroute"])

    def validate(self) -> dict[str, Any]:
        self.static_paths()
        self.residual_paths()
        if self.real_host:
            if os.geteuid() != 0:
                self.blockers.append("root authority required for complete runtime inspection")
            else:
                self.runtime_units()
                self.runtime_processes()
                self.runtime_network_and_containers()
                self.runtime_packages_and_commands()
        status = "FAIL" if self.findings else "BLOCKED" if self.blockers else "PASS"
        return {
            "status": status,
            "scope": self.scope,
            "host": socket.gethostname(),
            "scanned_files": self.scanned_files,
            "findings": sorted(self.findings, key=lambda item: (item["kind"], item["target"])),
            "blockers": sorted(set(self.blockers)),
            "secret_values_persisted": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("controller", "worker"), required=True)
    parser.add_argument("--root", type=Path, default=Path("/"), help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    result = Validator(arguments.scope, arguments.root).validate()
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] == "FAIL":
        raise SystemExit(1)
    if result["status"] == "BLOCKED":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
