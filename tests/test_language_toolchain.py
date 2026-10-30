"""Pinned Rust and Go toolchains (ToolchainPolicy.rust / .go) for falcone-serverless.

Every existing project stays byte-identical (no rust, no go); a selection prepends the
side-by-side install to PATH everywhere the project toolchain runs (prepare, dependency
resolution, the NEW-49 baseline, the maker/checker sandbox) and fails closed when it is
not installed on the host.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from changeset_worktree import link_changeset_worktree
from pydantic import ValidationError
from test_delivery_toolchain import FakeToolchainRunner, _dependency_setup, _git, _handler
from test_node_toolchain import _executable, _installs, _prepared, _provider_result

from engineering_control.activities.scan import (
    _DEFAULT_PATH,
    DEPENDENCY_RESOLUTION_UNSUPPORTED_FOR_TOOLCHAIN,
)
from engineering_control.adapters.process import (
    TOOLCHAIN_CACHE_ROOT,
    SandboxToolchain,
    subscription_only_environment,
)
from engineering_control.config import Settings
from engineering_control.engineering.backlog import repository_r3_keyword_exemptions
from engineering_control.engineering.models import (
    ProjectManifest,
    ToolchainPolicy,
    ToolchainTestCommand,
)
from engineering_control.engineering.toolchain import (
    CARGO_HOME_CACHE,
    TOOLCHAIN_BASELINE_SANDBOX_REQUIRED,
    TOOLCHAIN_CARGO_CONFIG_REFUSED,
    TOOLCHAIN_GO_UNAVAILABLE,
    TOOLCHAIN_RUST_UNAVAILABLE,
    ToolchainUnavailableError,
    baseline_language_executables,
    baseline_requires_sandbox,
    cargo_config_violations,
    payload_node_toolchain,
    platform_language_variables,
    prepare_executables,
    project_node_toolchain,
    resolve_node_toolchain,
)
from engineering_control.projects import GLOBAL_PROJECTS, PROJECT_SET, SELF_CONTAINED

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT / "config" / "projects"
BOOTSTRAP = ROOT / "scripts" / "bootstrap-worker.sh"
VERIFY = ROOT / "scripts" / "verify-worker.sh"
RUST_TOOLS = ("cargo", "rustc", "rustfmt", "cargo-fmt", "cargo-clippy", "clippy-driver")
SELECTED = {"rust": "1.99.1", "go": "1.26"}


def HARDENED_RUST(rust_bin: Path) -> dict[str, str]:  # noqa: N802 - a constant's shape
    """Review 1: what every cargo command Hermes runs outside the sandbox is forced to."""
    return {
        "RUSTC": str(rust_bin / "rustc"),
        "RUSTDOC": str(rust_bin / "rustdoc"),
        "CARGO_BUILD_RUSTC": str(rust_bin / "rustc"),
        "CARGO_BUILD_RUSTDOC": str(rust_bin / "rustdoc"),
        "RUSTC_WRAPPER": "",
        "RUSTC_WORKSPACE_WRAPPER": "",
        "CARGO_BUILD_RUSTC_WRAPPER": "",
        "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER": "",
        "CARGO_REGISTRY_GLOBAL_CREDENTIAL_PROVIDERS": "cargo:token",
        "CARGO_NET_GIT_FETCH_WITH_CLI": "false",
    }


HARDENED_GO = {
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


def _language_installs(
    tmp_path: Path, *, rust: bool = True, go: bool = True, skip: str | None = None
) -> dict[str, Any]:
    rust_root = tmp_path / "toolchain" / "rust-1.99.1"
    go_root = tmp_path / "toolchain" / "go-1.26"
    if rust:
        for tool in RUST_TOOLS:
            if tool != skip:
                _executable(rust_root / "bin" / tool)
    if go:
        for tool in ("go", "gofmt"):
            if tool != skip:
                _executable(go_root / "bin" / tool)
    return {
        **_installs(tmp_path),
        "toolchain_rust_roots": {"1.99.1": rust_root},
        "toolchain_go_roots": {"1.26": go_root},
    }


def _manifest(project: str) -> ProjectManifest:
    return ProjectManifest.model_validate(
        yaml.safe_load((PROJECTS / f"{project}.yaml").read_text(encoding="utf-8"))
    )


# -- the falcone-serverless project -------------------------------------------------------


def test_falcone_serverless_is_a_self_contained_merge_only_project() -> None:
    manifest = _manifest("falcone-serverless")
    assert "falcone-serverless" in PROJECT_SET and "falcone-serverless" in SELF_CONTAINED
    assert "falcone-serverless" not in GLOBAL_PROJECTS
    assert not manifest.has_runtime()
    assert manifest.deployment.release is None  # type: ignore[union-attr]
    merge = manifest.merge_delivery()
    assert merge is not None and merge.enabled and manifest.merge_delivery_blockers() == []
    assert merge.allow_r3_merge is False and merge.maximum_concurrency == 1
    assert {".github/", ".claude/", ".codex/", ".mcp.json"} <= set(merge.protected_paths)
    assert not merge.changeset.enabled
    assert str(manifest.repositories.source.full_name) == "gntik-ai/falcone-serverless"
    assert manifest.repositories.source.default_branch == "main"


def test_falcone_serverless_toolchain_runs_the_ci_jobs() -> None:
    toolchain = _manifest("falcone-serverless").toolchain
    assert toolchain is not None and toolchain.enabled
    assert (toolchain.rust, toolchain.go) == ("1.99.1", "1.26")
    assert toolchain.default_node_toolchain
    assert [(c.cwd, c.argv) for c in toolchain.prepare] == [
        (".", ["cargo", "fetch", "--locked"]),
        ("controller", ["go", "mod", "download"]),
    ]
    assert [(c.cwd, c.argv) for c in toolchain.test_commands] == [
        (".", ["cargo", "fmt", "--all", "--check"]),
        (
            ".",
            ["cargo", "clippy", "--workspace", "--all-targets", "--locked", "--", "-D", "warnings"],
        ),
        (".", ["cargo", "test", "--workspace", "--locked"]),
        ("controller", ["gofmt", "-l", "."]),
        ("controller", ["go", "vet", "./..."]),
        ("controller", ["go", "test", "./..."]),
    ]


def test_falcone_serverless_maker_rules_carry_the_hard_rules() -> None:
    rules = _manifest("falcone-serverless").maker_rules
    assert rules is not None and rules.deployment == []
    text = " ".join(rules.source)
    for phrase in (
        "AGENTS.md",
        "--locked",
        "Cargo.lock",
        ".github/",
        ".claude/",
        ".codex/",
        ".mcp.json",
        "restricted pods",
        "native code",
        "deserialized",
        "WIT capability host",
        "gntik-ai/falcone",
        "gntik-ai/falcone-charts",
    ):
        assert phrase in text, phrase
    assert all(len(rule) <= 600 for rule in rules.source)


def test_falcone_serverless_exempts_only_production_from_the_r3_keywords() -> None:
    assert repository_r3_keyword_exemptions("gntik-ai/falcone-serverless") == {"production"}
    assert repository_r3_keyword_exemptions("gntik-ai/falcone") == frozenset()


# -- manifest contract ----------------------------------------------------------------------


def test_policy_defaults_select_no_rust_or_go() -> None:
    policy = ToolchainPolicy()
    assert policy.rust is None and policy.go is None


def test_existing_projects_keep_no_rust_or_go() -> None:
    for project in sorted(PROJECT_SET - {"falcone-serverless"}):
        toolchain = _manifest(project).toolchain
        assert toolchain is None or (toolchain.rust, toolchain.go) == (None, None), project
        if toolchain is None or toolchain.default_node_toolchain:
            resolved = project_node_toolchain(Settings(project_config_root=PROJECTS), project)
            assert resolved.default and resolved.sandbox() is None, project
            assert resolved.rust_bin is None and resolved.go_bin is None, project


@pytest.mark.parametrize(
    "fields",
    [
        {"go": 1.26},  # YAML float: must be quoted
        {"go": "1.26.1"},
        {"go": "go1.26"},
        {"go": "2.0"},
        {"rust": "1.99"},
        {"rust": "stable"},
        {"rust": "1.99.1-nightly"},
        {"rust": 1},
    ],
)
def test_policy_rejects_unpinned_rust_and_go(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ToolchainPolicy.model_validate(fields)


def test_cargo_prepare_requires_a_pinned_rust() -> None:
    prepare = [{"cwd": ".", "argv": ["cargo", "fetch", "--locked"]}]
    with pytest.raises(ValidationError, match="requires a pinned toolchain.rust"):
        ToolchainPolicy.model_validate({"enabled": True, "prepare": prepare})
    assert ToolchainPolicy.model_validate({"enabled": True, "prepare": prepare, "rust": "1.99.1"})


# -- settings and resolution ------------------------------------------------------------


def test_settings_defaults_name_the_side_by_side_installs() -> None:
    settings = Settings()
    assert settings.toolchain_rust_roots == {
        "1.99.1": Path("/opt/engineering-toolchain/rust-1.99.1")
    }
    assert settings.toolchain_go_roots == {"1.26": Path("/opt/engineering-toolchain/go-1.26")}


@pytest.mark.parametrize(
    "overrides",
    [
        {"toolchain_rust_roots": {"1.99.1": Path("relative")}},
        {"toolchain_rust_roots": {"stable": Path("/opt/rust")}},
        {"toolchain_go_roots": {"1.26.10": Path("/opt/go")}},
        {"toolchain_go_roots": {"1.26": Path("go")}},
    ],
)
def test_settings_reject_unsafe_language_roots(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(**overrides)


def test_rust_and_go_resolution_prepends_only_their_bins(tmp_path: Path) -> None:
    settings = Settings(**_language_installs(tmp_path))
    toolchain = resolve_node_toolchain(settings, ToolchainPolicy.model_validate(SELECTED))
    rust_bin = tmp_path / "toolchain" / "rust-1.99.1" / "bin"
    go_bin = tmp_path / "toolchain" / "go-1.26" / "bin"
    assert not toolchain.default and toolchain.default_node
    # Node stays the default: neither its bin nor the worker pnpm is prepended.
    assert toolchain.path_prefix == (str(rust_bin), str(go_bin))
    assert toolchain.variables == {
        "CARGO_INCREMENTAL": "0",
        "CARGO_TERM_COLOR": "never",
        "CARGO_BUILD_JOBS": "2",
    }
    assert toolchain.selection() == {"node": "22", "pnpm": None, **SELECTED}
    go_only = resolve_node_toolchain(settings, ToolchainPolicy(go="1.26"))
    assert go_only.path_prefix == (str(go_bin),) and go_only.variables == {}
    assert go_only.selection() == {"node": "22", "pnpm": None, "go": "1.26"}


def test_node_only_selection_is_unchanged(tmp_path: Path) -> None:
    settings = Settings(**_language_installs(tmp_path))
    toolchain = resolve_node_toolchain(settings, ToolchainPolicy(node="24", pnpm="11.28.3"))
    assert toolchain.selection() == {"node": "24", "pnpm": "11.28.3"}
    sandbox = toolchain.sandbox()
    assert sandbox is not None and sandbox.run_directories == ()
    assert prepare_executables(settings, toolchain) == {
        **prepare_executables(settings),
        "pnpm": tmp_path / "toolchain" / "pnpm-11.28.3" / "pnpm",
    }
    assert baseline_language_executables(toolchain) == {}


@pytest.mark.parametrize(
    ("installs", "policy", "blocker"),
    [
        ({"rust": False}, {"rust": "1.99.1"}, TOOLCHAIN_RUST_UNAVAILABLE),
        ({"skip": "cargo-clippy"}, {"rust": "1.99.1"}, TOOLCHAIN_RUST_UNAVAILABLE),
        ({"skip": "rustfmt"}, {"rust": "1.99.1"}, TOOLCHAIN_RUST_UNAVAILABLE),
        ({}, {"rust": "1.98.0"}, TOOLCHAIN_RUST_UNAVAILABLE),
        ({"go": False}, {"go": "1.26"}, TOOLCHAIN_GO_UNAVAILABLE),
        ({"skip": "gofmt"}, {"go": "1.26"}, TOOLCHAIN_GO_UNAVAILABLE),
        ({}, {"go": "1.25"}, TOOLCHAIN_GO_UNAVAILABLE),
    ],
)
def test_missing_language_selection_fails_closed(
    tmp_path: Path, installs: dict[str, Any], policy: dict[str, str], blocker: str
) -> None:
    settings = Settings(**_language_installs(tmp_path, **installs))
    with pytest.raises(ToolchainUnavailableError) as raised:
        resolve_node_toolchain(settings, ToolchainPolicy.model_validate(policy))
    assert raised.value.blocker == blocker


def test_selected_rust_and_go_extend_the_executable_allowlists(tmp_path: Path) -> None:
    settings = Settings(**_language_installs(tmp_path))
    default = prepare_executables(settings)
    assert "cargo" not in default and "gofmt" not in default  # unchanged for every project
    assert default["go"] == settings.go_executable
    toolchain = resolve_node_toolchain(settings, ToolchainPolicy.model_validate(SELECTED))
    selected = prepare_executables(settings, toolchain)
    assert selected["cargo"] == tmp_path / "toolchain" / "rust-1.99.1" / "bin" / "cargo"
    assert selected["go"] == tmp_path / "toolchain" / "go-1.26" / "bin" / "go"
    assert {k: v for k, v in selected.items() if k not in {"cargo", "go"}} == {
        k: v for k, v in default.items() if k != "go"
    }
    assert baseline_language_executables(toolchain) == {
        "gofmt": tmp_path / "toolchain" / "go-1.26" / "bin" / "gofmt"
    }


# -- payload selection ----------------------------------------------------------------------


def test_payload_selection_carries_rust_and_go(tmp_path: Path) -> None:
    settings = Settings(**_language_installs(tmp_path))
    carried = payload_node_toolchain(settings, _prepared({"node": "22", "pnpm": None, **SELECTED}))
    assert (carried.rust, carried.go) == ("1.99.1", "1.26")
    for malformed in (
        {"node": "22", "pnpm": None, "rust": "stable"},
        {"node": "22", "pnpm": None, "cargo": "1.99.1"},
    ):
        with pytest.raises(ValueError):
            payload_node_toolchain(settings, _prepared(malformed))


async def test_provider_passes_rust_and_go_to_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, adapter = await _provider_result(
        tmp_path,
        monkeypatch,
        _language_installs(tmp_path),
        _prepared({"node": "22", "pnpm": None, **SELECTED}),
    )
    assert result["status"] == "SUCCESS"
    [sandbox] = adapter.toolchains
    assert isinstance(sandbox, SandboxToolchain)
    assert sandbox.path_prefix == (
        str(tmp_path / "toolchain" / "rust-1.99.1" / "bin"),
        str(tmp_path / "toolchain" / "go-1.26" / "bin"),
    )


async def test_provider_blocks_when_the_carried_rust_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, adapter = await _provider_result(
        tmp_path,
        monkeypatch,
        _language_installs(tmp_path, rust=False),
        _prepared({"node": "22", "pnpm": None, **SELECTED}),
    )
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == TOOLCHAIN_RUST_UNAVAILABLE
    assert adapter.toolchains == []


# -- maker / checker sandbox ------------------------------------------------------------


def test_sandbox_environment_is_offline_with_per_run_build_directories(tmp_path: Path) -> None:
    settings = Settings(**_language_installs(tmp_path))
    sandbox = resolve_node_toolchain(settings, ToolchainPolicy.model_validate(SELECTED)).sandbox()
    assert sandbox is not None
    run_temp = tmp_path / "run-tmp"
    overrides = {"TMPDIR": str(run_temp), "TMP": str(run_temp), "TEMP": str(run_temp)}
    default = subscription_only_environment("codex", overrides)
    selected = subscription_only_environment("codex", overrides, toolchain=sandbox)
    assert selected["PATH"] == ":".join((*sandbox.path_prefix, default["PATH"]))
    assert selected["CARGO_HOME"] == str(TOOLCHAIN_CACHE_ROOT / CARGO_HOME_CACHE)
    assert selected["CARGO_NET_OFFLINE"] == "true"
    assert selected["CARGO_TARGET_DIR"] == str(run_temp / "cargo-target")
    assert selected["GOCACHE"] == str(run_temp / "go-build")
    assert selected["GOPATH"] == str(run_temp / "go-path")
    # The prefilled module cache and the offline Go settings are unchanged.
    for key in ("GOMODCACHE", "GOPROXY", "GOTOOLCHAIN", "GOFLAGS", "TMPDIR"):
        assert selected[key] == default[key], key
    changed = {"PATH", "GOCACHE", "GOPATH"} | {key for key in selected if key.startswith("CARGO_")}
    assert {key: selected[key] for key in set(default) - changed} == {
        key: default[key] for key in set(default) - changed
    }
    # No node switch variables for a default node.
    assert "pnpm_config_pm_on_fail" not in selected


@pytest.mark.parametrize(
    ("variables", "run_directories"),
    [
        ((("TMPDIR", "/x"),), ()),
        ((), (("PATH", "bin"),)),
        ((), (("CARGO_TARGET_DIR", "../escape"),)),
        ((), (("CARGO_TARGET_DIR", "a/b"),)),
        ((), (("CARGO_TARGET_DIR", ""),)),
    ],
)
def test_sandbox_toolchain_rejects_unsafe_run_directories(
    variables: tuple[tuple[str, str], ...], run_directories: tuple[tuple[str, str], ...]
) -> None:
    with pytest.raises(ValueError, match="sandbox toolchain"):
        SandboxToolchain(("/opt/x",), variables, run_directories)


# -- scan worker: prepare -------------------------------------------------------------------


def _serverless_repo(worktree_root: Path) -> Path:
    repo = worktree_root / "falcone-serverless" / str(uuid4())
    (repo / "worker" / "src").mkdir(parents=True)
    (repo / "controller").mkdir()
    (repo / "Cargo.toml").write_text('[workspace]\nmembers = ["worker"]\n')
    (repo / "controller" / "go.mod").write_text("module example.invalid/controller\n\ngo 1.26\n")
    (repo / ".gitignore").write_text("/target/\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "b")
    link_changeset_worktree(repo, worktree_root.parent / "worktree-git")
    return repo


def test_prepare_runs_cargo_fetch_and_go_with_the_selection(tmp_path: Path) -> None:
    repo = _serverless_repo(tmp_path / "worktrees")
    runner = FakeToolchainRunner(creates={})
    handler = _handler(tmp_path, runner, **_language_installs(tmp_path))
    payload = {
        "project": "falcone-serverless",
        "change_set_id": str(uuid4()),
        "source_worktree": str(repo),
    }
    result = handler._prepare_toolchain(payload)
    assert result["status"] == "TOOLCHAIN_READY", result
    assert result["toolchain_preparation"]["node_toolchain"] == {
        "node": "22",
        "pnpm": None,
        **SELECTED,
    }
    assert [(call["executable"], call["arguments"]) for call in runner.calls] == [
        ("cargo", ["fetch", "--locked"]),
        ("go", ["mod", "download"]),
    ]
    cargo_home = tmp_path / "cache" / "toolchain" / CARGO_HOME_CACHE
    assert cargo_home.is_dir() and (cargo_home.stat().st_mode & 0o777) == 0o750
    rust_bin = tmp_path / "toolchain" / "rust-1.99.1" / "bin"
    go_bin = tmp_path / "toolchain" / "go-1.26" / "bin"
    expected = {
        **handler._toolchain_environment(),
        "CARGO_INCREMENTAL": "0",
        "CARGO_TERM_COLOR": "never",
        "CARGO_BUILD_JOBS": "2",
        "CARGO_HOME": str(cargo_home),
        **HARDENED_RUST(rust_bin),
        **HARDENED_GO,
        "PATH": f"{rust_bin}:{go_bin}:{_DEFAULT_PATH}",
    }
    assert all(call["environment"] == expected for call in runner.calls)
    # Network-enabled prepare: never the sandbox's offline switch.
    assert "CARGO_NET_OFFLINE" not in expected


def test_prepare_fails_closed_when_the_selected_rust_is_missing(tmp_path: Path) -> None:
    repo = _serverless_repo(tmp_path / "worktrees")
    runner = FakeToolchainRunner(creates={})
    handler = _handler(tmp_path, runner, **_language_installs(tmp_path, rust=False))
    payload = {
        "project": "falcone-serverless",
        "change_set_id": str(uuid4()),
        "source_worktree": str(repo),
    }
    result = handler._prepare_toolchain(payload)
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == TOOLCHAIN_RUST_UNAVAILABLE
    assert runner.calls == []


# -- review 1: repository cargo configuration never runs outside the sandbox ------------


def _serverless_payload(repo: Path) -> dict[str, Any]:
    return {
        "project": "falcone-serverless",
        "change_set_id": str(uuid4()),
        "source_worktree": str(repo),
    }


def _commit_all(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "c")


@pytest.mark.parametrize(
    ("relative", "symlink"),
    [
        (".cargo/config.toml", False),
        (".cargo/config", False),
        (".cargo/config.toml", True),  # a dangling symlink still counts
    ],
)
def test_prepare_refuses_a_repository_cargo_config(
    tmp_path: Path, relative: str, symlink: bool
) -> None:
    repo = _serverless_repo(tmp_path / "worktrees")
    config = repo / relative
    config.parent.mkdir(parents=True, exist_ok=True)
    if symlink:
        config.symlink_to(tmp_path / "missing-config.toml")
    else:
        config.write_text('[build]\nrustc-wrapper = "/tmp/pwn.sh"\n')
    _commit_all(repo)
    runner = FakeToolchainRunner(creates={})
    handler = _handler(tmp_path, runner, **_language_installs(tmp_path))
    result = handler._prepare_toolchain(_serverless_payload(repo))
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == TOOLCHAIN_CARGO_CONFIG_REFUSED
    assert relative in result["toolchain_preparation"]["detail"]
    assert runner.calls == []  # cargo never started


def test_prepare_refuses_a_cargo_config_on_the_working_directory_path(tmp_path: Path) -> None:
    # A Cargo workspace below the repository root: config in that directory (an ancestor of
    # nothing else) is read only when cargo runs there, so a cwd: crates prepare is refused.
    repo = _serverless_repo(tmp_path / "worktrees")
    (repo / "crates" / ".cargo").mkdir(parents=True)
    (repo / "crates" / ".cargo" / "config.toml").write_text("[build]\nrustc = 'x'\n")
    (repo / "crates" / "Cargo.toml").write_text("[workspace]\n")
    _commit_all(repo)
    assert cargo_config_violations(repo, repo) == []
    assert cargo_config_violations(repo, repo / "crates") == ["crates/.cargo/config.toml"]
    assert cargo_config_violations(repo, repo / "controller") == []
    with pytest.raises(ValueError, match="outside the checkout"):
        cargo_config_violations(repo, tmp_path)
    root = tmp_path / "projects"
    root.mkdir()
    manifest = yaml.safe_load((PROJECTS / "falcone-serverless.yaml").read_text())
    manifest["toolchain"]["prepare"][0]["cwd"] = "crates"
    (root / "falcone-serverless.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    runner = FakeToolchainRunner(creates={})
    handler = _handler(tmp_path, runner, project_config_root=root, **_language_installs(tmp_path))
    result = handler._prepare_toolchain(_serverless_payload(repo))
    assert result["blocker"] == TOOLCHAIN_CARGO_CONFIG_REFUSED
    assert runner.calls == []


def test_go_prepare_is_not_affected_by_a_cargo_config_elsewhere(tmp_path: Path) -> None:
    # Only cargo commands are refused; the check is never a reason to skip the Go step's
    # own hardening, which applies to every go command Hermes runs (HARDENED_GO).
    repo = _serverless_repo(tmp_path / "worktrees")
    settings = Settings(**_language_installs(tmp_path))
    toolchain = resolve_node_toolchain(settings, ToolchainPolicy.model_validate(SELECTED))
    rust_bin = tmp_path / "toolchain" / "rust-1.99.1" / "bin"
    assert platform_language_variables(toolchain) == {**HARDENED_RUST(rust_bin), **HARDENED_GO}
    go_only = resolve_node_toolchain(settings, ToolchainPolicy(go="1.26"))
    assert platform_language_variables(go_only) == HARDENED_GO
    default = resolve_node_toolchain(settings, None)
    assert platform_language_variables(default) == {}
    handler = _handler(tmp_path, FakeToolchainRunner(), **_language_installs(tmp_path))
    environment = handler._node_toolchain_environment(
        {"GOFLAGS": "-toolexec=/tmp/pwn", "GOPROXY": "direct", "GOTOOLCHAIN": "auto"},
        toolchain,
        _DEFAULT_PATH,
    )
    for key, value in HARDENED_GO.items():
        assert environment[key] == value, key
    assert repo.is_dir()


# -- review 2: Rust/Go baselines never run outside a sandbox ----------------------------


@pytest.mark.parametrize(
    ("argv", "rust_refused", "go_refused"),
    [
        (["cargo", "test", "--workspace", "--locked"], True, False),
        (["cargo", "fetch", "--locked"], True, False),
        (["timeout", "--kill-after=30", "900", "cargo", "test"], True, False),
        (["timeout", "--kill-after=30", "900", "go", "test", "./..."], False, True),
        (["go", "vet", "./..."], False, True),
        (["gofmt", "-l", "."], False, True),
        (["timeout", "900"], True, True),  # malformed wrapper under a selection: fail closed
        (["timeout", "--kill-after=30", "900", "pnpm", "test"], False, False),
        ([".venv/bin/pytest", "-q"], False, False),
        (["node", "--test", "a.test.mjs"], False, False),
    ],
)
def test_baseline_sandbox_requirement(
    tmp_path: Path, argv: list[str], rust_refused: bool, go_refused: bool
) -> None:
    settings = Settings(**_language_installs(tmp_path))
    rust = resolve_node_toolchain(settings, ToolchainPolicy(rust="1.99.1"))
    go = resolve_node_toolchain(settings, ToolchainPolicy(go="1.26"))
    both = resolve_node_toolchain(settings, ToolchainPolicy.model_validate(SELECTED))
    assert baseline_requires_sandbox(argv, rust) is rust_refused
    assert baseline_requires_sandbox(argv, go) is go_refused
    assert baseline_requires_sandbox(argv, both) is (rust_refused or go_refused)
    # No pinned Rust/Go selection (musematic, falcone, gntik-ui, llmwiki): never refused.
    assert baseline_requires_sandbox(argv, resolve_node_toolchain(settings, None)) is False
    assert baseline_requires_sandbox(argv, None) is False


@pytest.mark.parametrize("selection", [None, {"rust": "1.99.1"}])
def test_a_musematic_style_go_baseline_still_runs_without_a_go_selection(
    tmp_path: Path, selection: dict[str, str] | None
) -> None:
    # Byte-identical for existing projects: with toolchain.go None the worker-Go `go test`
    # base rerun runs exactly as on origin/main (the runner is called).
    checkout = tmp_path / "baseline" / "worktree"
    checkout.mkdir(parents=True)
    runner = FakeToolchainRunner()
    handler = _handler(tmp_path, runner, **_language_installs(tmp_path))
    handler.baseline_runner = runner  # type: ignore[assignment]
    policy = None if selection is None else ToolchainPolicy.model_validate(selection)
    toolchain = resolve_node_toolchain(handler.settings, policy)
    argv = ["timeout", "--kill-after=30", "900", "go", "test", "./..."]
    command = ToolchainTestCommand(cwd=".", argv=argv, description="go tests")
    result = handler._run_baseline_test(checkout, command, {}, toolchain)
    assert result["exit_code"] == 0
    assert [(call["executable"], call["arguments"]) for call in runner.calls] == [
        ("timeout", argv[1:])
    ]


@pytest.mark.parametrize("config", [True, False])
def test_baseline_test_never_runs_cargo_or_go(tmp_path: Path, config: bool) -> None:
    checkout = tmp_path / "baseline" / "worktree"
    checkout.mkdir(parents=True)
    if config:
        (checkout / ".cargo").mkdir()
        (checkout / ".cargo" / "config.toml").write_text('[build]\nrustc-wrapper = "/tmp/x"\n')
    runner = FakeToolchainRunner()
    handler = _handler(tmp_path, runner, **_language_installs(tmp_path))
    handler.baseline_runner = runner  # type: ignore[assignment]
    toolchain = resolve_node_toolchain(handler.settings, ToolchainPolicy.model_validate(SELECTED))
    for argv in (
        ["cargo", "test", "--workspace", "--locked"],
        ["timeout", "--kill-after=30", "900", "cargo", "test"],
        ["go", "test", "./..."],
    ):
        command = ToolchainTestCommand(cwd=".", argv=argv, description="tests")
        with pytest.raises(ValueError, match=TOOLCHAIN_BASELINE_SANDBOX_REQUIRED):
            handler._run_baseline_test(checkout, command, {}, toolchain)
    assert runner.calls == []


def _changeset_payload(repo: Path, reported: str) -> dict[str, Any]:
    return {
        "project": "falcone-serverless",
        "change_set_id": str(uuid4()),
        "source_worktree": str(repo),
        "deployment_worktree": str(repo),
        "source_repository": "falcone-serverless",
        "deployment_repository": "falcone-serverless",
        "source_base_sha": "a" * 40,
        "deployment_base_sha": "a" * 40,
        "agent_result": {"structured_result": {"decision": "PASS"}},
        "source_maker_result": {
            "structured_result": {"tests": [{"command": reported, "status": "FAIL"}]}
        },
        "deployment_maker_result": {"structured_result": {"tests": []}},
        "toolchain_preparation": {
            "status": "TOOLCHAIN_READY",
            "node_toolchain": {"node": "22", "pnpm": None, **SELECTED},
        },
    }


@pytest.mark.parametrize(
    "reported",
    [
        "cargo test --workspace --locked",
        "cargo clippy --workspace --all-targets --locked -- -D warnings",
        "cd controller && go test ./...",
        "cd controller && go vet ./...",
    ],
)
def test_a_rust_or_go_maker_failure_is_never_rerun_and_stays_a_failure(
    tmp_path: Path, reported: str
) -> None:
    repo = _serverless_repo(tmp_path / "worktrees")
    runner = FakeToolchainRunner(creates={})
    handler = _handler(tmp_path, runner, **_language_installs(tmp_path))
    handler.baseline_runner = runner  # type: ignore[assignment]
    validated = handler._validate_changeset(_changeset_payload(repo, reported))
    assert validated["status"] == "BLOCKED"
    assert validated["blocker"] == "MAKER_TEST_FAILURE"  # not pre-existing
    assert TOOLCHAIN_BASELINE_SANDBOX_REQUIRED in validated["baseline_error"]
    assert runner.calls == []  # no checkout prepare, no baseline run
    baselines = tmp_path / "cache" / "toolchain" / "baselines"
    assert not baselines.exists()  # nothing cached, nothing checked out
    # A retry is refused again, never served from a cache.
    again = handler._validate_changeset(_changeset_payload(repo, reported))
    assert again["blocker"] == "MAKER_TEST_FAILURE"
    assert TOOLCHAIN_BASELINE_SANDBOX_REQUIRED in again["baseline_error"]
    assert runner.calls == []


def test_dependency_runner_uses_the_selected_go(tmp_path: Path) -> None:
    handler = _handler(tmp_path, FakeToolchainRunner(), **_language_installs(tmp_path))
    handler._runners_injected = (False, False)
    toolchain = resolve_node_toolchain(handler.settings, ToolchainPolicy.model_validate(SELECTED))
    runner = handler._dependency_runner_for(toolchain)
    assert runner.allowed_executables["go"] == tmp_path / "toolchain" / "go-1.26" / "bin" / "go"


# -- operator scripts -------------------------------------------------------------------


def _assignment(text: str, name: str) -> str:
    match = re.search(rf"^{name}=(\S+|\"[^\"]*\")$", text, re.MULTILINE)
    assert match is not None, name
    return match.group(1).strip('"')


def test_scripts_pin_the_language_toolchains_the_settings_select() -> None:
    bootstrap = BOOTSTRAP.read_text()
    verify = VERIFY.read_text()
    settings = Settings()
    root = Path(_assignment(bootstrap, "TOOLCHAIN_ROOT"))
    rust = _assignment(bootstrap, "RUST_PINNED_VERSION")
    go_line = _assignment(bootstrap, "GO_PINNED_LINE")
    go_version = _assignment(bootstrap, "GO_PINNED_VERSION")
    assert rust == _assignment(verify, "RUST_PINNED_VERSION") == "1.99.1"
    assert go_line == _assignment(verify, "GO_PINNED_LINE") == "1.26"
    assert go_version == _assignment(verify, "GO_PINNED_VERSION")
    assert go_version.startswith(go_line + ".")
    assert _assignment(bootstrap, "RUST_PINNED_RUSTC") == _assignment(verify, "RUST_PINNED_RUSTC")
    assert settings.toolchain_rust_roots[rust] == root / f"rust-{rust}"
    assert settings.toolchain_go_roots[go_line] == root / f"go-{go_line}"
    for name in ("RUST_PINNED_SHA256", "GO_PINNED_SHA256"):
        assert re.fullmatch(r"[0-9a-f]{64}", _assignment(bootstrap, name)), name
    for name in ("RUST_PINNED_URL", "GO_PINNED_URL"):
        assert _assignment(bootstrap, name).startswith("https://"), name
    # The installer adds exactly the components resolve_node_toolchain requires.
    assert _assignment(bootstrap, "RUST_COMPONENTS") == (
        "rustc,cargo,rust-std-x86_64-unknown-linux-gnu,rustfmt-preview,clippy-preview"
    )


def test_language_toolchains_are_opt_in_and_verified() -> None:
    bootstrap = BOOTSTRAP.read_text()
    assert "LANGUAGE_TOOLCHAINS=0" in bootstrap
    assert bootstrap.count("\n  install_project_toolchains\n") == 2
    assert bootstrap.count("\n    install_language_toolchains\n") == 1
    assert "--project-toolchains-only) NODE_TOOLCHAIN_MODE=only LANGUAGE_TOOLCHAINS=1 ;;" in (
        bootstrap
    )
    section = bootstrap[bootstrap.index("install_language_toolchains() {") :]
    section = section[: section.index("\n}\n")]
    assert "rustup" not in section and "/usr/local/go" not in section
    for tool in (*RUST_TOOLS, "gofmt"):
        assert tool in section, tool
    verify = VERIFY.read_text()
    assert "ENGINEERING_VERIFY_LANGUAGE_TOOLCHAINS:-0" in verify
    assert "SKIP language toolchains" in verify and "PASS language toolchains" in verify


def test_bootstrap_installs_falcone_serverless_without_runtime_material() -> None:
    worker = BOOTSTRAP.read_text()
    assert (
        "if [[ ${project} == gntik-ui || ${project} == falcone-serverless"
        " || ${project} == falcone-workflow ]]; then" in worker
    )
    result = subprocess.run(  # noqa: S603 - fixed shell, syntax check only
        ["/bin/bash", "-n", str(BOOTSTRAP)], check=False
    )
    assert result.returncode == 0


def test_shared_toolchain_caches_are_read_only_for_makers() -> None:
    # Review 1: makers and the checker run as codex-runner / claude-runner and must read the
    # crate and module caches scan-runner fills, including entries created later.
    bootstrap = BOOTSTRAP.read_text()
    section = bootstrap[bootstrap.index("grant_shared_toolchain_caches() {") :]
    section = section[: section.index("\n}\n")]
    assert "SHARED_TOOLCHAIN_CACHES=(go-mod cargo-home trivy)" in bootstrap
    assert "u:codex-runner:--x,u:claude-runner:--x" in section  # ancestors: traverse only
    assert "setfacl -R --modify u:codex-runner:rX,u:claude-runner:rX" in section
    assert "d:u::rwx,d:u:codex-runner:rx,d:u:claude-runner:rx,d:g::---,d:m::rwx,d:o::---" in (
        section
    )
    assert "rwx" not in section.replace("d:u::rwx", "").replace("d:m::rwx", "")
    # The full bootstrap grants after the cache chmods; --project-toolchains-only too.
    full = bootstrap[bootstrap.index('"${PLATFORM_ROOT}/cache/scan/worktree-git"') :]
    assert "\ngrant_shared_toolchain_caches\n" in full
    assert "\n    grant_shared_toolchain_caches\n" in bootstrap
    verify = VERIFY.read_text()
    assert "PASS shared toolchain caches" in verify
    assert "default:user:codex-runner:r-x" in verify


# -- final review: no platform dependency resolution under a pinned Rust/Go ---------------


@pytest.mark.parametrize("ecosystem", ["pnpm", "go"])
@pytest.mark.parametrize("carried", [True, False])
def test_dependency_resolution_is_refused_for_a_pinned_rust_go_project(
    tmp_path: Path, ecosystem: str, carried: bool
) -> None:
    # A maker could commit package.json + .pnpmfile.cjs and ask for pnpm resolution; pnpm
    # runs pnpmfile hooks despite --ignore-scripts, outside the sandbox. Refused before any
    # command, whether the selection is carried or only in the manifest.
    directory = "." if ecosystem == "pnpm" else "services/api"
    handler, dependencies, toolchain, payload, repo = _dependency_setup(
        tmp_path, [{"ecosystem": ecosystem, "directory": directory}]
    )
    (repo / ".pnpmfile.cjs").write_text("module.exports = {hooks: {}}\n")
    _commit_all(repo)
    handler.settings = handler.settings.model_copy(update=_language_installs(tmp_path))
    payload = {**payload, "project": "falcone-serverless"}
    if carried:
        payload["toolchain_preparation"] = {
            "status": "TOOLCHAIN_READY",
            "node_toolchain": {"node": "22", "pnpm": None, **SELECTED},
        }
    blocked = handler._resolve_dependencies(payload)
    assert blocked["status"] == "BLOCKED"
    assert blocked["blocker"] == DEPENDENCY_RESOLUTION_UNSUPPORTED_FOR_TOOLCHAIN
    assert "DEPENDENCY_CHANGE_REQUIRED" in blocked["dependency_resolution_result"]["detail"]
    assert dependencies.calls == [] and toolchain.calls == []


def test_dependency_resolution_is_unchanged_for_a_node_project(tmp_path: Path) -> None:
    handler, dependencies, _, payload, _ = _dependency_setup(
        tmp_path, [{"ecosystem": "pnpm", "directory": "."}]
    )
    assert handler._resolve_dependencies(payload)["status"] == "DEPENDENCIES_RESOLVED"
    assert dependencies.calls and dependencies.calls[0]["environment"] == (
        handler._dependency_environment()
    )
