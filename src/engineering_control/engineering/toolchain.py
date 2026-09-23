"""Per-worktree project toolchain contract helpers (delivery.prepare_toolchain)."""

from __future__ import annotations

import shlex
from pathlib import Path

import yaml

from engineering_control.config import Settings
from engineering_control.engineering.models import ProjectManifest, ToolchainPolicy

_PROJECTS = frozenset({"falcone", "musematic", "llmwiki"})
_MAX_MANIFEST_BYTES = 512 * 1024


def load_toolchain_policy(project_config_root: Path, project: str) -> ToolchainPolicy | None:
    """Return the project's typed toolchain block, or None when the manifest has none.

    Raises OSError when the manifest is unreadable and ValueError when it is invalid, so
    callers choose whether that is fail-closed (prepare) or advisory (prompts).
    """
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
    return manifest.toolchain


def enabled_toolchain(project_config_root: Path, project: str) -> ToolchainPolicy | None:
    policy = load_toolchain_policy(project_config_root, project)
    return policy if policy is not None and policy.enabled else None


def prepare_executables(settings: Settings) -> dict[str, Path]:
    """The only executables a prepare command may run (argv[0] name -> absolute path)."""
    return {"uv": settings.uv_executable, "pnpm": settings.toolchain_pnpm_executable}


def render_argv(argv: list[str], settings: Settings) -> list[str]:
    """Substitute the single supported placeholder; the contract rejects any other."""
    python312 = str(settings.python312_executable)
    return [argument.replace("{python312}", python312) for argument in argv]


def toolchain_test_command_lines(policy: ToolchainPolicy) -> list[str]:
    return [
        f"- in `{command.cwd}`: `{shlex.join(command.argv)}` ({command.description})"
        for command in policy.test_commands
    ]
