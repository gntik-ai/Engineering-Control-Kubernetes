"""Per-worktree project toolchain contract helpers (delivery.prepare_toolchain)."""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from engineering_control.adapters.process import SandboxToolchain
from engineering_control.config import Settings
from engineering_control.engineering.models import (
    DEFAULT_TOOLCHAIN_NODE,
    DeploymentPinPolicy,
    ProjectManifest,
    ToolchainPolicy,
    ToolchainTestCommand,
)
from engineering_control.projects import PROJECT_SET

_PROJECTS = PROJECT_SET
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


TOOLCHAIN_NODE_UNAVAILABLE = "TOOLCHAIN_NODE_UNAVAILABLE"
TOOLCHAIN_PNPM_UNAVAILABLE = "TOOLCHAIN_PNPM_UNAVAILABLE"
# NEW-159: only for a non-default selection. pnpm 11 reads pnpm_config_* (pm-on-fail
# replaces manage-package-manager-versions); pnpm 10 reads the npm_config_* form. None
# of them may download or switch to another pnpm (packageManager / devEngines).
_NON_DEFAULT_NODE_VARIABLES: tuple[tuple[str, str], ...] = (
    ("npm_config_manage_package_manager_versions", "false"),
    ("pnpm_config_manage_package_manager_versions", "false"),
    ("pnpm_config_pm_on_fail", "ignore"),
    ("COREPACK_ENABLE_DOWNLOAD_PROMPT", "0"),
    ("COREPACK_ENABLE_AUTO_PIN", "0"),
)


class ToolchainUnavailableError(ValueError):
    """The manifest selects a node or pnpm that is not installed on this host."""

    def __init__(self, blocker: str, detail: str) -> None:
        super().__init__(detail)
        self.blocker = blocker


@dataclass(frozen=True)
class NodeToolchain:
    """NEW-159: the node/pnpm a project's toolchain runs with on this host."""

    node: str
    pnpm: str | None
    node_bin: Path
    pnpm_executable: Path

    @property
    def default(self) -> bool:
        return self.node == DEFAULT_TOOLCHAIN_NODE and self.pnpm is None

    @property
    def path_prefix(self) -> tuple[str, ...]:
        """PATH entries to prepend: none for the default (byte-identical PATH)."""
        if self.default:
            return ()
        # A pinned pnpm goes first so it wins over /usr/local/bin/pnpm even on node 22; the
        # worker pnpm's directory (/usr/local/bin, which also holds node 22) is never added.
        entries = [str(self.pnpm_executable.parent)] if self.pnpm is not None else []
        return tuple(dict.fromkeys((*entries, str(self.node_bin))))

    @property
    def variables(self) -> dict[str, str]:
        return {} if self.default else dict(_NON_DEFAULT_NODE_VARIABLES)

    def path(self, base: str) -> str:
        return ":".join((*self.path_prefix, base))

    def sandbox(self) -> SandboxToolchain | None:
        if self.default:
            return None
        return SandboxToolchain(self.path_prefix, _NON_DEFAULT_NODE_VARIABLES)

    def selection(self) -> dict[str, str | None] | None:
        """The manifest selection as carried in payloads; None for the default."""
        return None if self.default else {"node": self.node, "pnpm": self.pnpm}


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_node_toolchain(settings: Settings, policy: ToolchainPolicy | None) -> NodeToolchain:
    """The project's node/pnpm; a non-default selection fails closed when not installed.

    The default (no policy, node "22", no pinned pnpm) resolves to today's executables
    without touching the filesystem, so existing projects behave exactly as before.
    """
    node = policy.node if policy is not None else DEFAULT_TOOLCHAIN_NODE
    pnpm = policy.pnpm if policy is not None else None
    root = settings.toolchain_node_roots.get(node)
    if node == DEFAULT_TOOLCHAIN_NODE and pnpm is None:
        node_bin = root / "bin" if root is not None else settings.node_executable.parent
        return NodeToolchain(node, None, node_bin, settings.toolchain_pnpm_executable)
    if root is None or not _executable(root / "bin" / "node"):
        where = "no root is configured" if root is None else f"{root / 'bin' / 'node'} is missing"
        raise ToolchainUnavailableError(
            TOOLCHAIN_NODE_UNAVAILABLE, f"Node.js {node} is not installed on this host ({where})"
        )
    pnpm_executable = settings.toolchain_pnpm_executable
    if pnpm is not None:
        pinned = settings.toolchain_pnpm_executables.get(pnpm)
        if pinned is None or not _executable(pinned):
            where = "no executable is configured" if pinned is None else f"{pinned} is missing"
            raise ToolchainUnavailableError(
                TOOLCHAIN_PNPM_UNAVAILABLE, f"pnpm {pnpm} is not installed on this host ({where})"
            )
        pnpm_executable = pinned
    return NodeToolchain(node, pnpm, root / "bin", pnpm_executable)


def project_node_toolchain(settings: Settings, project: str) -> NodeToolchain:
    """resolve_node_toolchain for a project manifest; a missing manifest is the default.

    Raises ToolchainUnavailableError (fail closed) when the selection is not installed
    here, and OSError/ValueError when the manifest is invalid.
    """
    try:
        policy = load_toolchain_policy(settings.project_config_root, project)
    except FileNotFoundError:
        policy = None
    return resolve_node_toolchain(settings, policy)


def payload_node_toolchain(settings: Settings, payload: Mapping[str, Any]) -> NodeToolchain:
    """NEW-159 review 1: the selection delivery.prepare_toolchain put in the payload.

    Absent (every default project, every non-delivery flow, in-flight runs) is the
    default toolchain, exactly as before. A present selection is validated and must be
    installed on this host (ToolchainUnavailableError); a malformed one is ValueError.
    """
    preparation = payload.get("toolchain_preparation")
    # Review 2: only an absent key is the default; a present null is malformed.
    if not isinstance(preparation, Mapping) or "node_toolchain" not in preparation:
        return resolve_node_toolchain(settings, None)
    raw = preparation["node_toolchain"]
    if not isinstance(raw, Mapping) or set(raw) - {"node", "pnpm"}:
        raise ValueError("toolchain_preparation.node_toolchain is malformed")
    try:
        policy = ToolchainPolicy.model_validate({"node": raw.get("node"), "pnpm": raw.get("pnpm")})
    except ValidationError as error:
        raise ValueError("toolchain_preparation.node_toolchain is invalid") from error
    return resolve_node_toolchain(settings, policy)


def prepare_executables(
    settings: Settings, toolchain: NodeToolchain | None = None
) -> dict[str, Path]:
    """The only executables a prepare command may run (argv[0] name -> absolute path)."""
    return {
        "uv": settings.uv_executable,
        "pnpm": (
            settings.toolchain_pnpm_executable if toolchain is None else toolchain.pnpm_executable
        ),
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
