"""Per-worktree project toolchain contract helpers (delivery.prepare_toolchain)."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import yaml

from engineering_control.config import Settings
from engineering_control.engineering.models import (
    DeploymentPinPolicy,
    ProjectManifest,
    ToolchainPolicy,
    ToolchainTestCommand,
)

_PROJECTS = frozenset({"falcone", "musematic", "llmwiki"})
_MAX_MANIFEST_BYTES = 512 * 1024
_CD_PREFIX = re.compile(r"^cd\s+(?P<cwd>\S+)\s*&&\s*(?P<command>.+)$", re.DOTALL)
# NEW-58: makers also report commands as "<cwd>: <command>" (musematic #993 retry 18),
# which fell through as a non-contract command and skipped the NEW-49 baseline.
_CWD_LABEL_PREFIX = re.compile(r"^(?P<cwd>[A-Za-z0-9_./-]+):\s+(?P<command>.+)$", re.DOTALL)


def load_toolchain_policy(project_config_root: Path, project: str) -> ToolchainPolicy | None:
    """Return the project's typed toolchain block, or None when the manifest has none.

    Raises OSError when the manifest is unreadable and ValueError when it is invalid, so
    callers choose whether that is fail-closed (prepare) or advisory (prompts).
    """
    return load_project_manifest(project_config_root, project).toolchain


def load_deployment_pin(project_config_root: Path, project: str) -> DeploymentPinPolicy | None:
    """NEW-118: the project's source CI pin of the deployment repository, if configured."""
    return load_project_manifest(project_config_root, project).deployment_pin


def load_maker_rules(project_config_root: Path, project: str, *, deployment: bool) -> list[str]:
    """The project's standing maker rules for the source or deployment repository."""
    rules = load_project_manifest(project_config_root, project).maker_rules
    if rules is None:
        return []
    return list(rules.deployment if deployment else rules.source)


def load_project_manifest(project_config_root: Path, project: str) -> ProjectManifest:
    """Read one bounded project manifest (no symlinks, size-bounded, identity-checked)."""
    if project not in _PROJECTS:
        raise ValueError("toolchain project is not allowlisted")
    root = project_config_root.resolve()
    unresolved = root / f"{project}.yaml"
    if unresolved.is_symlink():
        raise ValueError("project manifest must not be a symlink")
    path = unresolved.resolve()
    if not path.is_file() or path.parent != root:
        raise FileNotFoundError(f"project manifest is missing: {project}")
    if path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ValueError("project manifest exceeds its size bound")
    manifest = ProjectManifest.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if manifest.project != project:
        raise ValueError("project manifest identity is invalid")
    return manifest


def enabled_toolchain(project_config_root: Path, project: str) -> ToolchainPolicy | None:
    policy = load_toolchain_policy(project_config_root, project)
    return policy if policy is not None and policy.enabled else None


def prepare_executables(settings: Settings) -> dict[str, Path]:
    """The only executables a prepare command may run (argv[0] name -> absolute path)."""
    return {
        "uv": settings.uv_executable,
        "pnpm": settings.toolchain_pnpm_executable,
        # NEW-79a: Go module downloads and the offline Trivy DB for the sandbox.
        "go": settings.go_executable,
        "trivy": settings.trivy_executable,
    }


def render_argv(argv: list[str], settings: Settings) -> list[str]:
    """Substitute the single supported placeholder; the contract rejects any other."""
    python312 = str(settings.python312_executable)
    return [argument.replace("{python312}", python312) for argument in argv]


def toolchain_test_command_lines(policy: ToolchainPolicy) -> list[str]:
    return [
        f"- in `{command.cwd}`: `{shlex.join(command.argv)}` ({command.description})"
        for command in policy.test_commands
    ]


def _reported_cwd(value: str, worktree: Path | None) -> str | None:
    try:
        tokens = shlex.split(value)
    except ValueError:
        return None
    if len(tokens) != 1:
        return None
    cwd = tokens[0]
    if cwd.startswith("/"):
        if worktree is None:
            return None
        path = Path(cwd)
        if not path.is_relative_to(worktree):
            return None
        cwd = path.relative_to(worktree).as_posix()
    while cwd.startswith("./"):
        cwd = cwd[2:]
    cwd = cwd.rstrip("/")
    return cwd or "."


def match_toolchain_test_command(
    policy: ToolchainPolicy, reported: str, settings: Settings, *, worktree: Path | None = None
) -> ToolchainTestCommand | None:
    """NEW-49: map a maker's reported tests[].command back to one contract test command.

    Makers report what the prompt showed them (toolchain_test_command_lines), usually as
    `cd <cwd> && <shlex.join(argv)>`. The match is exact on the argument vector (raw or
    rendered) and on the cwd when one is given; anything else or ambiguous is no match.
    """
    text = reported.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    cwd: str | None = None
    prefix = _CD_PREFIX.match(text) or _CWD_LABEL_PREFIX.match(text)
    if prefix is not None:
        cwd = _reported_cwd(prefix["cwd"], worktree)
        if cwd is None:
            return None
        text = prefix["command"]
    try:
        tokens = shlex.split(text)
    except ValueError:
        return None
    candidates = [
        command
        for command in policy.test_commands
        if tokens in (command.argv, render_argv(command.argv, settings))
        and (cwd is None or command.cwd == cwd)
    ]
    return candidates[0] if len(candidates) == 1 else None
