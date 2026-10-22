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

from engineering_control.adapters.process import TOOLCHAIN_CACHE_ROOT, SandboxToolchain
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
TOOLCHAIN_RUST_UNAVAILABLE = "TOOLCHAIN_RUST_UNAVAILABLE"
TOOLCHAIN_GO_UNAVAILABLE = "TOOLCHAIN_GO_UNAVAILABLE"
# The shared crate cache under the toolchain cache root: `cargo fetch --locked` (prepare,
# scan worker, network) fills it; makers and checkers only read it, offline (cargo falls
# back to a shared read lock on a read-only CARGO_HOME), exactly like go-mod for Go.
CARGO_HOME_CACHE = "cargo-home"
# A selected Rust: no network in the sandbox, no incremental state, two build jobs (the
# worker has little RAM, as GOFLAGS -p=2), and build output in the run's TMPDIR, never in
# the shared worktree (another runner uid could not remove or overwrite it).
_RUST_COMMON_VARIABLES: tuple[tuple[str, str], ...] = (
    ("CARGO_INCREMENTAL", "0"),
    ("CARGO_TERM_COLOR", "never"),
    ("CARGO_BUILD_JOBS", "2"),
)
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
    """NEW-159: the node/pnpm (and optional pinned Rust and Go) a project's toolchain runs
    with on this host. The name is historical: Rust and Go selections are carried too."""

    node: str
    pnpm: str | None
    node_bin: Path
    pnpm_executable: Path
    rust: str | None = None
    rust_bin: Path | None = None
    go: str | None = None
    go_bin: Path | None = None

    @property
    def default_node(self) -> bool:
        return self.node == DEFAULT_TOOLCHAIN_NODE and self.pnpm is None

    @property
    def default(self) -> bool:
        return self.default_node and self.rust is None and self.go is None

    @property
    def path_prefix(self) -> tuple[str, ...]:
        """PATH entries to prepend: none for the default (byte-identical PATH)."""
        entries: list[str] = []
        if not self.default_node:
            # A pinned pnpm goes first so it wins over /usr/local/bin/pnpm even on node 22;
            # the worker pnpm's directory (/usr/local/bin, which also holds node 22) is
            # never added.
            if self.pnpm is not None:
                entries.append(str(self.pnpm_executable.parent))
            entries.append(str(self.node_bin))
        # A selected Rust or Go wins over /usr/local/go/bin and anything on the base PATH.
        entries.extend(str(path) for path in (self.rust_bin, self.go_bin) if path is not None)
        return tuple(dict.fromkeys(entries))

    @property
    def variables(self) -> dict[str, str]:
        """Variables of the platform-run commands (prepare, dependency resolution and the
        baseline; scan worker): the node no-switch settings for a non-default node, and
        the cargo settings for a selected Rust. Empty for the default."""
        variables = {} if self.default_node else dict(_NON_DEFAULT_NODE_VARIABLES)
        if self.rust is not None:
            # The scan worker adds CARGO_HOME (its Settings.toolchain_cache_root).
            variables.update(_RUST_COMMON_VARIABLES)
        return variables

    def path(self, base: str) -> str:
        return ":".join((*self.path_prefix, base))

    def sandbox(self) -> SandboxToolchain | None:
        if self.default:
            return None
        variables: list[tuple[str, str]] = (
            [] if self.default_node else list(_NON_DEFAULT_NODE_VARIABLES)
        )
        run_directories: list[tuple[str, str]] = []
        if self.rust is not None:
            # Offline and read-only crate cache; target/ in the run's TMPDIR.
            variables.extend(_RUST_COMMON_VARIABLES)
            variables.append(("CARGO_HOME", str(TOOLCHAIN_CACHE_ROOT / CARGO_HOME_CACHE)))
            variables.append(("CARGO_NET_OFFLINE", "true"))
            run_directories.append(("CARGO_TARGET_DIR", "cargo-target"))
        if self.go is not None:
            # GOMODCACHE stays the prefilled read-only module cache (GOPROXY=off); the
            # build cache and GOPATH are the run's own.
            run_directories.extend((("GOCACHE", "go-build"), ("GOPATH", "go-path")))
        return SandboxToolchain(self.path_prefix, tuple(variables), tuple(run_directories))

    def selection(self) -> dict[str, str | None] | None:
        """The manifest selection as carried in payloads; None for the default. Rust and
        Go appear only when selected, so a node-only selection is byte-identical."""
        if self.default:
            return None
        selection: dict[str, str | None] = {"node": self.node, "pnpm": self.pnpm}
        if self.rust is not None:
            selection["rust"] = self.rust
        if self.go is not None:
            selection["go"] = self.go
        return selection


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


TOOLCHAIN_CARGO_CONFIG_REFUSED = "TOOLCHAIN_CARGO_CONFIG_REFUSED"
# Cargo reads .cargo/config(.toml) from the working directory and every ancestor. Inside a
# worktree those files are repository (or maker) controlled and can name programs cargo
# runs (build.rustc-wrapper, credential providers, target runners), so Hermes never runs
# cargo outside the sandbox below one.
_CARGO_CONFIG_NAMES = ("config", "config.toml")


def cargo_config_violations(root: Path, cwd: Path) -> list[str]:
    """Repository-relative .cargo/config(.toml) paths cargo would read for ``cwd``.

    Every directory from ``root`` down to ``cwd`` is checked; a symlink (even dangling)
    or any other entry under either name counts. ``cwd`` must be inside ``root``."""
    root = root.resolve()
    cwd = cwd.resolve()
    if not cwd.is_relative_to(root):
        raise ValueError("cargo working directory is outside the checkout")
    found: list[str] = []
    directory = root
    for part in ("", *cwd.relative_to(root).parts):
        directory = directory / part if part else directory
        for name in _CARGO_CONFIG_NAMES:
            candidate = directory / ".cargo" / name
            if candidate.is_symlink() or candidate.exists():
                found.append(candidate.relative_to(root).as_posix())
    return found


TOOLCHAIN_BASELINE_SANDBOX_REQUIRED = "TOOLCHAIN_BASELINE_SANDBOX_REQUIRED"
# Review 2: compiling or testing Rust/Go runs repository code (build.rs, procedural
# macros, test binaries); the NEW-49 base rerun runs unsandboxed as the network-enabled
# scan worker, so it never runs them under a pinned toolchain.rust / toolchain.go
# selection. Without a selection (musematic, falcone, gntik-ui, llmwiki) the base rerun is
# exactly as before. Follow-ups: sandbox cargo/go baselines like the node sandbox (NEW-161
# sandbox_argv: no network, read-only caches, no scan credentials); musematic's
# pre-existing `go test` baseline (worker Go, no selection) still runs unsandboxed.
_RUST_BASELINE_EXECUTABLES = frozenset({"cargo", "rustc", "rustdoc"})
_GO_BASELINE_EXECUTABLES = frozenset({"go", "gofmt"})


def baseline_requires_sandbox(argv: list[str], toolchain: NodeToolchain | None) -> bool:
    """True when a contract test command (timeout-wrapped or not) runs a tool of the
    selected pinned Rust or Go, which the unsandboxed NEW-49 baseline must never execute.
    Always False without such a selection (unchanged behaviour). With one, a malformed
    timeout wrapper also counts (fail closed)."""
    if toolchain is None:
        return False
    executables = (_RUST_BASELINE_EXECUTABLES if toolchain.rust is not None else frozenset()) | (
        _GO_BASELINE_EXECUTABLES if toolchain.go is not None else frozenset()
    )
    if not executables:
        return False
    command = argv[0] if argv else ""
    if Path(command).name == "timeout":
        operands = [value for value in argv[1:] if not value.startswith("-")]
        if len(operands) < 2:
            return True
        command = operands[1]
    return Path(command).name in executables


def platform_language_variables(toolchain: NodeToolchain) -> dict[str, str]:
    """Fixed settings for the cargo and go commands Hermes itself runs outside the sandbox
    (prepare, dependency resolution, the NEW-49 baseline) with a selected Rust or Go.

    Environment variables win over every Cargo configuration file, so the pinned rustc and
    rustdoc are used and no compiler wrapper or credential-provider program can be named.
    Go never switches toolchains, reads no go.env file or go.work, never runs version
    control commands (proxy only, no ``direct``) and always verifies checksums."""
    variables: dict[str, str] = {}
    if toolchain.rust_bin is not None:
        variables.update(
            {
                "RUSTC": str(toolchain.rust_bin / "rustc"),
                "RUSTDOC": str(toolchain.rust_bin / "rustdoc"),
                "CARGO_BUILD_RUSTC": str(toolchain.rust_bin / "rustc"),
                "CARGO_BUILD_RUSTDOC": str(toolchain.rust_bin / "rustdoc"),
                "RUSTC_WRAPPER": "",
                "RUSTC_WORKSPACE_WRAPPER": "",
                "CARGO_BUILD_RUSTC_WRAPPER": "",
                "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER": "",
                "CARGO_REGISTRY_GLOBAL_CREDENTIAL_PROVIDERS": "cargo:token",
                "CARGO_NET_GIT_FETCH_WITH_CLI": "false",
            }
        )
    if toolchain.go_bin is not None:
        variables.update(
            {
                "GOTOOLCHAIN": "local",
                "GOENV": "off",
                "GOWORK": "off",
                "GOFLAGS": "-modcacherw",
                "GOPROXY": "https://proxy.golang.org",
                "GOSUMDB": "sum.golang.org",
                "GONOSUMDB": "",
                "GOPRIVATE": "",
                "GONOPROXY": "",
                "GOINSECURE": "",
                "GOVCS": "*:off",
            }
        )
    return variables


def _language_bin(
    roots: Mapping[str, Path],
    version: str,
    executables: tuple[str, ...],
    blocker: str,
    label: str,
) -> Path:
    root = roots.get(version)
    missing = (
        None
        if root is None
        else next((name for name in executables if not _executable(root / "bin" / name)), None)
    )
    if root is None or missing is not None:
        where = (
            "no root is configured" if root is None else f"{root / 'bin' / str(missing)} is missing"
        )
        raise ToolchainUnavailableError(
            blocker, f"{label} {version} is not installed on this host ({where})"
        )
    return root / "bin"


def resolve_node_toolchain(settings: Settings, policy: ToolchainPolicy | None) -> NodeToolchain:
    """The project's node/pnpm (and Rust/Go); a non-default selection fails closed when it
    is not installed.

    The default (no policy, node "22", no pinned pnpm, no Rust or Go) resolves to today's
    executables without touching the filesystem, so existing projects behave exactly as
    before.
    """
    node = policy.node if policy is not None else DEFAULT_TOOLCHAIN_NODE
    pnpm = policy.pnpm if policy is not None else None
    rust = policy.rust if policy is not None else None
    go = policy.go if policy is not None else None
    root = settings.toolchain_node_roots.get(node)
    rust_bin = (
        None
        if rust is None
        else _language_bin(
            settings.toolchain_rust_roots,
            rust,
            ("cargo", "rustc", "rustfmt", "cargo-fmt", "cargo-clippy", "clippy-driver"),
            TOOLCHAIN_RUST_UNAVAILABLE,
            "Rust",
        )
    )
    go_bin = (
        None
        if go is None
        else _language_bin(
            settings.toolchain_go_roots, go, ("go", "gofmt"), TOOLCHAIN_GO_UNAVAILABLE, "Go"
        )
    )
    if node == DEFAULT_TOOLCHAIN_NODE and pnpm is None:
        node_bin = root / "bin" if root is not None else settings.node_executable.parent
        return NodeToolchain(
            node,
            None,
            node_bin,
            settings.toolchain_pnpm_executable,
            rust=rust,
            rust_bin=rust_bin,
            go=go,
            go_bin=go_bin,
        )
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
    return NodeToolchain(
        node,
        pnpm,
        root / "bin",
        pnpm_executable,
        rust=rust,
        rust_bin=rust_bin,
        go=go,
        go_bin=go_bin,
    )


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
    if not isinstance(raw, Mapping) or set(raw) - {"node", "pnpm", "rust", "go"}:
        raise ValueError("toolchain_preparation.node_toolchain is malformed")
    try:
        policy = ToolchainPolicy.model_validate(
            {
                "node": raw.get("node"),
                "pnpm": raw.get("pnpm"),
                "rust": raw.get("rust"),
                "go": raw.get("go"),
            }
        )
    except ValidationError as error:
        raise ValueError("toolchain_preparation.node_toolchain is invalid") from error
    return resolve_node_toolchain(settings, policy)


def prepare_executables(
    settings: Settings, toolchain: NodeToolchain | None = None
) -> dict[str, Path]:
    """The only executables a prepare command may run (argv[0] name -> absolute path).

    A selected Go replaces the worker's go; a selected Rust adds cargo. Without either the
    map is exactly the pre-existing one (no cargo: a cargo command is refused)."""
    executables = {
        "uv": settings.uv_executable,
        "pnpm": (
            settings.toolchain_pnpm_executable if toolchain is None else toolchain.pnpm_executable
        ),
        # NEW-79a: Go module downloads and the offline Trivy DB for the sandbox.
        "go": settings.go_executable,
        "trivy": settings.trivy_executable,
    }
    if toolchain is not None and toolchain.go_bin is not None:
        executables["go"] = toolchain.go_bin / "go"
    if toolchain is not None and toolchain.rust_bin is not None:
        executables["cargo"] = toolchain.rust_bin / "cargo"
    return executables


def baseline_language_executables(toolchain: NodeToolchain | None) -> dict[str, Path]:
    """Extra contract test executables of a selected Rust/Go for the NEW-49 baseline
    (gofmt; cargo and go come from prepare_executables). Empty for the default."""
    if toolchain is None or toolchain.go_bin is None:
        return {}
    return {"gofmt": toolchain.go_bin / "gofmt"}


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
