"""NEW-159: a project manifest selects its Node.js major and a pinned pnpm.

The default (node "22", no pnpm) must stay byte-identical to the pre-NEW-159 toolchain;
a non-default selection prepends the side-by-side install to PATH everywhere the project
toolchain runs, and fails closed when it is not installed on the host.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from test_delivery_toolchain import (
    FakeToolchainRunner,
    _dependency_setup,
    _handler,
    _payload,
    _source_repo,
)
from test_maker_test_baseline import MYPY, _baseline_root
from test_maker_test_baseline import _setup as _baseline_setup
from test_provider_activity import FakeAdapter, FakeInstructionPack, request_for

from engineering_control.activities.provider import ProviderActivityHandler
from engineering_control.activities.scan import _DEFAULT_PATH, ScanActivityHandler
from engineering_control.adapters.process import (
    SandboxToolchain,
    sandbox_toolchain_environment,
    subscription_only_environment,
)
from engineering_control.config import Settings
from engineering_control.engineering.models import ProjectManifest, ToolchainPolicy
from engineering_control.engineering.toolchain import (
    TOOLCHAIN_NODE_UNAVAILABLE,
    TOOLCHAIN_PNPM_UNAVAILABLE,
    ToolchainUnavailableError,
    payload_node_toolchain,
    prepare_executables,
    project_node_toolchain,
    resolve_node_toolchain,
)
from engineering_control.models.core import AgentRequest, AgentResult
from engineering_control.projects import PROJECT_SET

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT / "config" / "projects"
BOOTSTRAP = ROOT / "scripts" / "bootstrap-worker.sh"
VERIFY = ROOT / "scripts" / "verify-worker.sh"
BASH = shutil.which("bash") or "/bin/bash"
NON_DEFAULT_VARIABLES = {
    "npm_config_manage_package_manager_versions": "false",
    "pnpm_config_manage_package_manager_versions": "false",
    "pnpm_config_pm_on_fail": "ignore",
    "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
    "COREPACK_ENABLE_AUTO_PIN": "0",
}


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def _installs(tmp_path: Path, *, node: bool = True, pnpm: bool = True) -> dict[str, Any]:
    """Settings overrides pointing at fake side-by-side installs under tmp_path."""
    node_root = tmp_path / "toolchain" / "node-24"
    pnpm_executable = tmp_path / "toolchain" / "pnpm-11.28.3" / "pnpm"
    node22_root = tmp_path / "toolchain" / "node-22"
    _executable(node22_root / "bin" / "node")  # the worker's node 22 (absent on CI hosts)
    if node:
        _executable(node_root / "bin" / "node")
    if pnpm:
        _executable(pnpm_executable)
    return {
        "toolchain_node_roots": {"22": node22_root, "24": node_root},
        "toolchain_pnpm_executables": {"11.28.3": pnpm_executable},
    }


def _manifests(tmp_path: Path, project: str, toolchain: dict[str, Any]) -> Path:
    """A copy of the real manifests whose `project` selects the given toolchain fields."""
    root = tmp_path / "projects"
    shutil.copytree(PROJECTS, root)
    path = root / f"{project}.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    manifest["toolchain"] = {**(manifest.get("toolchain") or {}), **toolchain}
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return root


# -- manifest contract ------------------------------------------------------------------


def test_policy_defaults_to_todays_node_22_and_worker_pnpm() -> None:
    policy = ToolchainPolicy()
    assert policy.node == "22" and policy.pnpm is None
    assert policy.default_node_toolchain
    assert ToolchainPolicy.model_validate({"node": "22"}).default_node_toolchain


@pytest.mark.parametrize("node", ["24", 24])
def test_policy_accepts_node_24_as_string_or_yaml_integer(node: object) -> None:
    policy = ToolchainPolicy.model_validate({"node": node, "pnpm": "11.28.3"})
    assert policy.node == "24" and policy.pnpm == "11.28.3"
    assert not policy.default_node_toolchain
    assert not ToolchainPolicy.model_validate({"pnpm": "11.28.3"}).default_node_toolchain


@pytest.mark.parametrize("node", ["20", "23", "25", "lts", "v24", 24.0, True, None, ""])
def test_policy_rejects_unsupported_node_majors(node: object) -> None:
    with pytest.raises(ValidationError):
        ToolchainPolicy.model_validate({"node": node})


@pytest.mark.parametrize(
    "pnpm", ["^11.28.3", "11.28", "11.28.3-rc.1", "latest", "v11.28.3", "011.2.3", "11.x.3", ""]
)
def test_policy_rejects_unpinned_pnpm_versions(pnpm: str) -> None:
    with pytest.raises(ValidationError):
        ToolchainPolicy.model_validate({"pnpm": pnpm})


def test_existing_project_manifests_keep_the_default_toolchain() -> None:
    # NEW-157 registry: every shipped project stays on Node 22 and the worker pnpm, except
    # NEW-160's gntik-ui (Node 24 and pnpm 11.28.3, NEW-159) and falcone-serverless (Node 22
    # with a pinned Rust and Go: tests/test_language_toolchain.py).
    gntik_ui = ProjectManifest.model_validate(
        yaml.safe_load((PROJECTS / "gntik-ui.yaml").read_text())
    )
    assert gntik_ui.toolchain is not None
    assert (gntik_ui.toolchain.node, gntik_ui.toolchain.pnpm) == ("24", "11.28.3")
    for project in sorted(PROJECT_SET - {"gntik-ui", "falcone-serverless"}):
        path = PROJECTS / f"{project}.yaml"
        manifest = ProjectManifest.model_validate(yaml.safe_load(path.read_text()))
        toolchain = project_node_toolchain(Settings(project_config_root=PROJECTS), project)
        assert manifest.toolchain is None or manifest.toolchain.default_node_toolchain, project
        assert toolchain.default, project
        assert toolchain.node_bin == Path("/usr/local/bin")
        assert toolchain.pnpm_executable == Path("/usr/local/bin/pnpm")
        assert toolchain.path_prefix == () and toolchain.variables == {}
        assert toolchain.sandbox() is None


# -- settings and resolution ------------------------------------------------------------


def test_settings_defaults_name_the_side_by_side_installs() -> None:
    settings = Settings()
    assert settings.toolchain_node_roots == {
        "22": Path("/usr/local"),
        "24": Path("/opt/engineering-toolchain/node-24"),
    }
    assert settings.toolchain_pnpm_executables == {
        "11.28.3": Path("/opt/engineering-toolchain/pnpm-11.28.3/pnpm")
    }
    assert settings.toolchain_pnpm_executable == Path("/usr/local/bin/pnpm")


@pytest.mark.parametrize(
    "overrides",
    [
        {"toolchain_node_roots": {"24": Path("relative/node-24")}},
        {"toolchain_node_roots": {"20": Path("/opt/node-20")}},
        {"toolchain_pnpm_executables": {"latest": Path("/opt/pnpm/pnpm")}},
        {"toolchain_pnpm_executables": {"11.28.3": Path("pnpm")}},
        {"toolchain_pnpm_executables": {"11.28.3": Path("/opt/pnpm/node")}},
    ],
)
def test_settings_reject_unsafe_toolchain_paths(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(**overrides)


def test_default_resolution_never_touches_the_filesystem(tmp_path: Path) -> None:
    # Even when nothing is installed where the defaults point, the default resolves.
    settings = Settings(
        toolchain_node_roots={"22": tmp_path / "missing"},
        toolchain_pnpm_executable=tmp_path / "missing" / "pnpm",
    )
    toolchain = resolve_node_toolchain(settings, None)
    assert toolchain.default and toolchain.path_prefix == ()
    assert resolve_node_toolchain(settings, ToolchainPolicy()).default


def test_node_24_resolution_prepends_only_the_node_24_bin(tmp_path: Path) -> None:
    settings = Settings(**_installs(tmp_path))
    toolchain = resolve_node_toolchain(settings, ToolchainPolicy(node="24"))
    node_bin = tmp_path / "toolchain" / "node-24" / "bin"
    assert not toolchain.default
    # The worker pnpm (/usr/local/bin, which also holds node 22) is never prepended.
    assert toolchain.path_prefix == (str(node_bin),)
    assert toolchain.pnpm_executable == Path("/usr/local/bin/pnpm")
    assert toolchain.path("/usr/bin:/bin") == f"{node_bin}:/usr/bin:/bin"
    assert toolchain.variables == NON_DEFAULT_VARIABLES


def test_pinned_pnpm_goes_first_on_path(tmp_path: Path) -> None:
    settings = Settings(**_installs(tmp_path))
    pnpm_dir = tmp_path / "toolchain" / "pnpm-11.28.3"
    node_bin = tmp_path / "toolchain" / "node-24" / "bin"
    pinned = resolve_node_toolchain(settings, ToolchainPolicy(node="24", pnpm="11.28.3"))
    assert pinned.path_prefix == (str(pnpm_dir), str(node_bin))
    assert pinned.pnpm_executable == pnpm_dir / "pnpm"
    assert prepare_executables(settings, pinned)["pnpm"] == pnpm_dir / "pnpm"
    assert prepare_executables(settings)["pnpm"] == Path("/usr/local/bin/pnpm")
    # A pinned pnpm on node 22 still wins over the worker pnpm next to node 22.
    node22 = resolve_node_toolchain(settings, ToolchainPolicy(pnpm="11.28.3"))
    assert node22.path_prefix == (str(pnpm_dir), str(tmp_path / "toolchain" / "node-22" / "bin"))


@pytest.mark.parametrize(
    ("installs", "policy", "blocker"),
    [
        ({"node": False}, {"node": "24"}, TOOLCHAIN_NODE_UNAVAILABLE),
        ({"pnpm": False}, {"node": "24", "pnpm": "11.28.3"}, TOOLCHAIN_PNPM_UNAVAILABLE),
        ({}, {"node": "24", "pnpm": "11.29.0"}, TOOLCHAIN_PNPM_UNAVAILABLE),
        ({"pnpm": False}, {"pnpm": "11.28.3"}, TOOLCHAIN_PNPM_UNAVAILABLE),
    ],
)
def test_missing_selection_fails_closed(
    tmp_path: Path, installs: dict[str, bool], policy: dict[str, str], blocker: str
) -> None:
    settings = Settings(**_installs(tmp_path, **installs))
    with pytest.raises(ToolchainUnavailableError) as raised:
        resolve_node_toolchain(settings, ToolchainPolicy.model_validate(policy))
    assert raised.value.blocker == blocker


def test_unconfigured_or_non_executable_node_fails_closed(tmp_path: Path) -> None:
    unconfigured = Settings(toolchain_node_roots={"22": Path("/usr/local")})
    with pytest.raises(ToolchainUnavailableError, match="no root is configured"):
        resolve_node_toolchain(unconfigured, ToolchainPolicy(node="24"))
    overrides = _installs(tmp_path)
    (tmp_path / "toolchain" / "node-24" / "bin" / "node").chmod(0o644)
    with pytest.raises(ToolchainUnavailableError) as raised:
        resolve_node_toolchain(Settings(**overrides), ToolchainPolicy(node="24"))
    assert raised.value.blocker == TOOLCHAIN_NODE_UNAVAILABLE


# -- maker / checker sandbox ------------------------------------------------------------


def test_default_sandbox_environment_is_byte_identical() -> None:
    assert subscription_only_environment("codex") == subscription_only_environment(
        "codex", toolchain=None
    )
    base = {"PATH": "/usr/local/bin:/usr/bin:/bin"}
    assert sandbox_toolchain_environment(base) == sandbox_toolchain_environment(base, None)


def test_node_24_sandbox_environment_prepends_the_selection(tmp_path: Path) -> None:
    settings = Settings(**_installs(tmp_path))
    sandbox = resolve_node_toolchain(settings, ToolchainPolicy(node="24", pnpm="11.28.3")).sandbox()
    assert sandbox is not None
    default = subscription_only_environment("claude")
    selected = subscription_only_environment("claude", toolchain=sandbox)
    assert selected["PATH"] == ":".join((*sandbox.path_prefix, default["PATH"]))
    assert {key: selected[key] for key in NON_DEFAULT_VARIABLES} == NON_DEFAULT_VARIABLES
    untouched = set(default) - {"PATH", *NON_DEFAULT_VARIABLES}
    assert set(selected) - set(default) <= set(NON_DEFAULT_VARIABLES)
    assert {key: selected[key] for key in untouched} == {key: default[key] for key in untouched}


@pytest.mark.parametrize(
    ("prefix", "variables"),
    [
        ((), ()),
        (("relative/bin",), ()),
        (("/a:/b",), ()),
        (("/opt/x",), (("PATH", "/usr/bin"),)),
    ],
)
def test_sandbox_toolchain_rejects_unsafe_entries(
    prefix: tuple[str, ...], variables: tuple[tuple[str, str], ...]
) -> None:
    with pytest.raises(ValueError, match="sandbox toolchain"):
        SandboxToolchain(prefix, variables)


class RecordingAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.toolchains: list[object] = []

    async def execute(
        self, request: AgentRequest, on_event: Any = None, **kwargs: Any
    ) -> AgentResult:
        self.toolchains.append(kwargs.get("toolchain", "NOT_PASSED"))
        return await super().execute(request, on_event)


async def _provider_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings_overrides: dict[str, Any],
    extra_payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], RecordingAdapter]:
    monkeypatch.setattr(
        "engineering_control.activities.provider.activity.heartbeat", lambda _: None
    )
    worktree_root = tmp_path / "worktrees"
    worktree = worktree_root / "falcone" / "change-set"
    worktree.mkdir(parents=True)
    adapter = RecordingAdapter()
    handler = ProviderActivityHandler(
        Settings(worktree_root=worktree_root, run_root=tmp_path / "runs", **settings_overrides),
        "codex",
        adapter,
        FakeInstructionPack(),
    )
    result = await handler.handle(
        "delivery.invoke_maker",
        {"maker_request": request_for(worktree).model_dump(mode="json"), **(extra_payload or {})},
    )
    return result, adapter


def _prepared(selection: object) -> dict[str, Any]:
    return {"toolchain_preparation": {"status": "TOOLCHAIN_READY", "node_toolchain": selection}}


async def test_provider_default_project_calls_the_adapter_exactly_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, adapter = await _provider_result(
        tmp_path, monkeypatch, {}, {"toolchain_preparation": {"status": "TOOLCHAIN_READY"}}
    )
    assert result["status"] == "SUCCESS"
    assert adapter.toolchains == ["NOT_PASSED"]


async def test_provider_never_reads_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review 1: the provider worker may be unable to read projects.d; only the payload
    # selection (from delivery.prepare_toolchain) counts. Absent means the default.
    overrides = {
        **_installs(tmp_path),
        "project_config_root": _manifests(tmp_path, "falcone", {"node": "24"}),
    }
    result, adapter = await _provider_result(tmp_path, monkeypatch, overrides)
    assert result["status"] == "SUCCESS"
    assert adapter.toolchains == ["NOT_PASSED"]
    unreadable = {"project_config_root": tmp_path / "unreadable"}
    (tmp_path / "unreadable").mkdir(mode=0o000)
    try:
        result, adapter = await _provider_result(
            tmp_path / "second", monkeypatch, unreadable, _prepared({"node": "24", "pnpm": None})
        )
    finally:
        (tmp_path / "unreadable").chmod(0o700)
    assert result["status"] == "BLOCKED"  # nothing installed under the default /opt path
    assert result["blocker"] == TOOLCHAIN_NODE_UNAVAILABLE


async def test_provider_passes_the_payload_selection_to_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, adapter = await _provider_result(
        tmp_path, monkeypatch, _installs(tmp_path), _prepared({"node": "24", "pnpm": "11.28.3"})
    )
    assert result["status"] == "SUCCESS"
    [sandbox] = adapter.toolchains
    assert isinstance(sandbox, SandboxToolchain)
    assert sandbox.path_prefix == (
        str(tmp_path / "toolchain" / "pnpm-11.28.3"),
        str(tmp_path / "toolchain" / "node-24" / "bin"),
    )


@pytest.mark.parametrize(
    ("installs", "selection", "blocker"),
    [
        ({"node": False}, {"node": "24", "pnpm": None}, TOOLCHAIN_NODE_UNAVAILABLE),
        ({"pnpm": False}, {"node": "24", "pnpm": "11.28.3"}, TOOLCHAIN_PNPM_UNAVAILABLE),
        ({}, {"node": "20", "pnpm": None}, "TOOLCHAIN_CONTRACT_INVALID"),
        ({}, {"node": "24", "pnpm": "latest"}, "TOOLCHAIN_CONTRACT_INVALID"),
        ({}, {"node": "24", "path": "/tmp/x"}, "TOOLCHAIN_CONTRACT_INVALID"),  # noqa: S108
        ({}, "24", "TOOLCHAIN_CONTRACT_INVALID"),
    ],
)
async def test_provider_fails_closed_on_an_unusable_payload_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installs: dict[str, bool],
    selection: object,
    blocker: str,
) -> None:
    result, adapter = await _provider_result(
        tmp_path, monkeypatch, _installs(tmp_path, **installs), _prepared(selection)
    )
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == blocker
    assert adapter.toolchains == []


# -- scan worker: prepare, dependency resolution, baseline ------------------------------


def test_default_prepare_environment_is_unchanged(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path / "worktrees")
    runner = FakeToolchainRunner()
    handler = _handler(tmp_path, runner)
    result = handler._prepare_toolchain(_payload(repo))
    assert result["status"] == "TOOLCHAIN_READY"
    assert "node_toolchain" not in result["toolchain_preparation"]
    expected = handler._toolchain_environment()
    assert all(call["environment"] == expected for call in runner.calls)
    assert "PATH" not in expected  # SafeCommandRunner's fixed default PATH applies


def test_node_24_prepare_prepends_the_selection(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path / "worktrees")
    runner = FakeToolchainRunner()
    root = _manifests(tmp_path, "musematic", {"node": "24", "pnpm": "11.28.3"})
    handler = _handler(tmp_path, runner, project_config_root=root, **_installs(tmp_path))
    result = handler._prepare_toolchain(_payload(repo))
    assert result["status"] == "TOOLCHAIN_READY"
    assert result["toolchain_preparation"]["node_toolchain"] == {
        "node": "24",
        "pnpm": "11.28.3",
    }
    toolchain = tmp_path / "toolchain"
    expected = {
        **handler._toolchain_environment(),
        **NON_DEFAULT_VARIABLES,
        "PATH": f"{toolchain / 'pnpm-11.28.3'}:{toolchain / 'node-24' / 'bin'}:{_DEFAULT_PATH}",
    }
    assert runner.calls and all(call["environment"] == expected for call in runner.calls)


def test_skipped_toolchain_still_carries_a_non_default_selection(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path / "worktrees")
    runner = FakeToolchainRunner()
    root = _manifests(tmp_path, "musematic", {"enabled": False, "node": "24"})
    handler = _handler(tmp_path, runner, project_config_root=root, **_installs(tmp_path))
    result = handler._prepare_toolchain(_payload(repo))
    assert result["status"] == "TOOLCHAIN_SKIPPED"
    assert result["toolchain_preparation"] == {
        "status": "TOOLCHAIN_SKIPPED",
        "node_toolchain": {"node": "24", "pnpm": None},
    }
    assert runner.calls == []
    default = _handler(tmp_path, runner, project_config_root=PROJECTS)._prepare_toolchain(
        {**_payload(repo), "project": "llmwiki"}
    )
    assert default["toolchain_preparation"] == {"status": "TOOLCHAIN_SKIPPED"}


def test_node_24_prepare_uses_an_allowlist_with_the_pinned_pnpm(tmp_path: Path) -> None:
    root = _manifests(tmp_path, "musematic", {"node": "24", "pnpm": "11.28.3"})
    settings = Settings(project_config_root=root, **_installs(tmp_path))
    handler = ScanActivityHandler(settings)
    toolchain = project_node_toolchain(settings, "musematic")
    runner = handler._toolchain_runner_for(toolchain)
    assert runner is not handler.toolchain_runner
    assert runner.allowed_executables["pnpm"] == tmp_path / "toolchain" / "pnpm-11.28.3" / "pnpm"
    dependency = handler._dependency_runner_for(toolchain)
    assert dependency.allowed_executables["pnpm"] == runner.allowed_executables["pnpm"]
    default = project_node_toolchain(Settings(project_config_root=PROJECTS), "musematic")
    assert handler._toolchain_runner_for(default) is handler.toolchain_runner
    assert handler._dependency_runner_for(default) is handler.dependency_runner


def test_prepare_fails_closed_when_the_selected_node_is_missing(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path / "worktrees")
    runner = FakeToolchainRunner()
    root = _manifests(tmp_path, "musematic", {"node": "24"})
    handler = _handler(
        tmp_path, runner, project_config_root=root, **_installs(tmp_path, node=False)
    )
    result = handler._prepare_toolchain(_payload(repo))
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == TOOLCHAIN_NODE_UNAVAILABLE
    assert result["toolchain_preparation"]["blocker"] == TOOLCHAIN_NODE_UNAVAILABLE
    assert runner.calls == []


def test_dependency_resolution_uses_the_selection_and_fails_closed(tmp_path: Path) -> None:
    requests = [{"ecosystem": "pnpm", "directory": "."}]
    handler, dependencies, _, payload, _ = _dependency_setup(tmp_path / "a", requests)
    handler.settings = handler.settings.model_copy(
        update={
            "project_config_root": _manifests(tmp_path / "a", "musematic", {"node": "24"}),
            **_installs(tmp_path / "a"),
        }
    )
    # Review 2: the carried selection decides, not the manifest.
    payload = {**payload, "toolchain_preparation": {"node_toolchain": {"node": "24"}}}
    assert handler._resolve_dependencies(payload)["status"] == "DEPENDENCIES_RESOLVED"
    environment = dependencies.calls[0]["environment"]
    node_bin = tmp_path / "a" / "toolchain" / "node-24" / "bin"
    assert environment["PATH"] == f"{node_bin}:{_DEFAULT_PATH}"
    assert environment["pnpm_config_pm_on_fail"] == "ignore"

    handler, dependencies, _, payload, _ = _dependency_setup(tmp_path / "b", requests)
    handler.settings = handler.settings.model_copy(
        update={
            "project_config_root": _manifests(tmp_path / "b", "musematic", {"node": "24"}),
            **_installs(tmp_path / "b", node=False),
        }
    )
    payload = {**payload, "toolchain_preparation": {"node_toolchain": {"node": "24"}}}
    blocked = handler._resolve_dependencies(payload)
    assert blocked["status"] == "BLOCKED"
    assert blocked["blocker"] == TOOLCHAIN_NODE_UNAVAILABLE
    assert dependencies.calls == []


def test_default_dependency_environment_is_unchanged(tmp_path: Path) -> None:
    requests = [{"ecosystem": "pnpm", "directory": "."}]
    handler, dependencies, _, payload, _ = _dependency_setup(tmp_path, requests)
    assert handler._resolve_dependencies(payload)["status"] == "DEPENDENCIES_RESOLVED"
    assert dependencies.calls[0]["environment"] == handler._dependency_environment()


def test_default_baseline_environment_is_unchanged(tmp_path: Path) -> None:
    handler, _, _, baseline, payload = _baseline_setup(tmp_path, {tuple(MYPY): 1})
    assert handler._validate_changeset(payload)["status"] == "LOCAL_VALIDATION_PASS"
    [call] = baseline.calls
    assert call["environment"] == {
        **handler._toolchain_environment(),
        "PATH": f"/usr/local/bin:{_DEFAULT_PATH}",
        "MYPY_CACHE_DIR": "/dev/null",
    }


def _carrying_node_24(payload: dict[str, Any]) -> dict[str, Any]:
    """Review 2: a delivery whose prepare step selected Node 24 carries it in the payload."""
    preparation = dict(payload.get("toolchain_preparation") or {})
    return {**payload, "toolchain_preparation": {**preparation, "node_toolchain": {"node": "24"}}}


def test_node_24_baseline_prepends_the_selection(tmp_path: Path) -> None:
    handler, _, prepare, baseline, payload = _baseline_setup(tmp_path, {tuple(MYPY): 1})
    handler.settings = handler.settings.model_copy(
        update={
            "project_config_root": _manifests(tmp_path, "musematic", {"node": "24"}),
            **_installs(tmp_path),
        }
    )
    assert handler._validate_changeset(_carrying_node_24(payload))["status"] == (
        "LOCAL_VALIDATION_PASS"
    )
    [call] = baseline.calls
    node_bin = tmp_path / "toolchain" / "node-24" / "bin"
    assert call["environment"]["PATH"] == f"{node_bin}:/usr/local/bin:{_DEFAULT_PATH}"
    assert call["environment"]["MYPY_CACHE_DIR"] == "/dev/null"
    assert {key: call["environment"][key] for key in NON_DEFAULT_VARIABLES} == (
        NON_DEFAULT_VARIABLES
    )
    assert prepare.calls  # the baseline checkout was prepared with the same selection


def test_default_baseline_cache_identity_is_unchanged() -> None:
    default = resolve_node_toolchain(Settings(), None)
    argv = ["timeout", "900", "pytest"]
    assert ScanActivityHandler._baseline_key("apps", argv, default) == (
        '{"argv": ["timeout", "900", "pytest"], "cwd": "apps"}'
    )
    assert ScanActivityHandler._baseline_key("apps", argv) == (
        ScanActivityHandler._baseline_key("apps", argv, default)
    )
    assert ScanActivityHandler._baseline_prepared_marker("a" * 40, default) == "a" * 40 + "\n"


def test_changing_the_selection_at_the_same_base_misses_the_cache(tmp_path: Path) -> None:
    handler, _, prepare, baseline, payload = _baseline_setup(tmp_path, {tuple(MYPY): 1})
    default_settings = handler.settings
    assert handler._validate_changeset(payload)["status"] == "LOCAL_VALIDATION_PASS"
    assert len(baseline.calls) == 1
    prepared_default = len(prepare.calls)

    payload24 = _carrying_node_24(payload)
    handler.settings = default_settings.model_copy(
        update={
            "project_config_root": _manifests(tmp_path, "musematic", {"node": "24"}),
            **_installs(tmp_path),
        }
    )
    assert handler._validate_changeset(payload24)["status"] == "LOCAL_VALIDATION_PASS"
    assert len(baseline.calls) == 2  # not served from the node 22 result
    assert len(prepare.calls) == 2 * prepared_default  # the checkout was re-prepared
    node_bin = str(tmp_path / "toolchain" / "node-24" / "bin")
    assert baseline.calls[1]["environment"]["PATH"].startswith(node_bin + ":")
    assert handler._validate_changeset(payload24)["status"] == "LOCAL_VALIDATION_PASS"
    assert len(baseline.calls) == 2  # the node 24 result is cached under its own key

    handler.settings = default_settings
    assert handler._validate_changeset(payload)["status"] == "LOCAL_VALIDATION_PASS"
    assert len(baseline.calls) == 2  # the default result is still cached
    marker = _baseline_root(tmp_path, payload) / "prepared"
    assert marker.read_text().startswith(payload["source_base_sha"] + " ")


def test_baseline_fails_closed_before_any_checkout(tmp_path: Path) -> None:
    handler, _, prepare, baseline, payload = _baseline_setup(tmp_path, {tuple(MYPY): 1})
    handler.settings = handler.settings.model_copy(
        update={
            "project_config_root": _manifests(tmp_path, "musematic", {"node": "24"}),
            **_installs(tmp_path, node=False),
        }
    )
    validated = handler._validate_changeset(_carrying_node_24(payload))
    assert validated["status"] == "BLOCKED"
    assert validated["blocker"] == "MAKER_TEST_FAILURE"
    assert "Node.js 24 is not installed" in validated["baseline_error"]
    assert prepare.calls == [] and baseline.calls == []


# -- operator scripts -------------------------------------------------------------------


def _assignment(text: str, name: str) -> str:
    match = re.search(rf"^{name}=(\S+)$", text, re.MULTILINE)
    assert match is not None, name
    return match.group(1)


def test_scripts_are_valid_bash() -> None:
    for script in (BOOTSTRAP, VERIFY):
        subprocess.run([BASH, "-n", str(script)], check=True)  # noqa: S603
        assert script.stat().st_mode & 0o111


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_scripts_pass_shellcheck() -> None:
    for script in (BOOTSTRAP, VERIFY):
        subprocess.run(  # noqa: S603 - fixed linter
            [str(shutil.which("shellcheck")), "--severity=warning", str(script)], check=True
        )


def test_scripts_pin_the_toolchains_the_settings_select() -> None:
    bootstrap = BOOTSTRAP.read_text()
    verify = VERIFY.read_text()
    settings = Settings()
    root = _assignment(bootstrap, "TOOLCHAIN_ROOT")
    assert root == _assignment(verify, "TOOLCHAIN_ROOT") == "/opt/engineering-toolchain"
    node = _assignment(bootstrap, "NODE24_VERSION")
    pnpm = _assignment(bootstrap, "PNPM_PINNED_VERSION")
    assert node == _assignment(verify, "NODE24_VERSION") and node.startswith("24.")
    assert pnpm == _assignment(verify, "PNPM_PINNED_VERSION") == "11.28.3"
    assert settings.toolchain_node_roots["24"] == Path(root) / "node-24"
    assert settings.toolchain_pnpm_executables[pnpm] == Path(root) / f"pnpm-{pnpm}" / "pnpm"
    for name in ("NODE24_SHA256", "PNPM_PINNED_SHA256"):
        assert re.fullmatch(r"[0-9a-f]{64}", _assignment(bootstrap, name)), name
    assert "TODO" not in bootstrap


def test_bootstrap_toolchain_section_is_opt_in_and_never_replaces_node_22() -> None:
    bootstrap = BOOTSTRAP.read_text()
    assert "NODE_TOOLCHAIN_MODE=none" in bootstrap
    # Only the explicit modes call the installer; the default path never does.
    assert bootstrap.count("\n  install_node_toolchains\n") == 1
    assert bootstrap.count("\n  install_project_toolchains\n") == 2
    assert "if [[ ${NODE_TOOLCHAIN_MODE} == only ]]; then" in bootstrap
    assert "if [[ ${NODE_TOOLCHAIN_MODE} == also ]]; then" in bootstrap
    section = bootstrap[bootstrap.index("install_pinned_toolchain() {") :]
    section = section[: section.index("\nif [[ ${NODE_TOOLCHAIN_MODE} == only ]]")]
    assert "/usr/local/bin" not in section
    assert "sha256sum --check" in section and "--proto '=https'" in section
    assert "chown -R root:root" in section and "go-w" in section
    # Review 1: immutable releases, atomic symlink publication, staging-only trap.
    assert 'local release_name="${name}.${sha256:0:16}"' in section
    assert 'mv -Tf -- "${TOOLCHAIN_ROOT}/.${name}.link.new" "${link}"' in section
    assert "trap \"rm -rf -- '${staging}'\" EXIT" in section
    # staging (trap + success), unpublished release, the Rust installer's extraction
    assert section.count("rm -rf --") == 4
    assert "previous" not in section.replace("A previous release", "")
    # The Node 22 contract for the full bootstrap is unchanged.
    assert '[[ $("${NODE_BIN}" --version) == v22.* ]]' in bootstrap


def test_verify_checks_the_toolchain_only_when_installed_or_required() -> None:
    verify = VERIFY.read_text()
    assert (
        "if [[ -e ${TOOLCHAIN_ROOT} || ${ENGINEERING_VERIFY_NODE_TOOLCHAIN:-0} == 1 ]]; then"
        in verify
    )
    assert "SKIP node toolchain" in verify
    assert "755:root:root" in verify
    assert "[[ -L ${link} ]]" in verify
    assert "for identity in codex-runner claude-runner scan-runner; do" in verify


def test_review_2_manifest_edits_never_change_a_running_delivery(tmp_path: Path) -> None:
    # The manifest now says Node 24, but the delivery carried no selection (default).
    requests = [{"ecosystem": "pnpm", "directory": "."}]
    handler, dependencies, _, payload, _ = _dependency_setup(tmp_path, requests)
    handler.settings = handler.settings.model_copy(
        update={
            "project_config_root": _manifests(tmp_path, "musematic", {"node": "24"}),
            **_installs(tmp_path),
        }
    )
    payload = {**payload, "toolchain_preparation": {"status": "TOOLCHAIN_READY"}}
    resolved = handler._resolve_dependencies(payload)
    assert resolved["status"] == "DEPENDENCIES_RESOLVED"
    assert dependencies.calls[0]["environment"] == handler._dependency_environment()
    # Review 3: the re-preparation keeps the carried (default) selection for later steps.
    assert "node_toolchain" not in resolved["toolchain_preparation"]


def test_review_3_re_preparation_keeps_a_carried_node_24(tmp_path: Path) -> None:
    requests = [{"ecosystem": "pnpm", "directory": "."}]
    handler, _, _, payload, _ = _dependency_setup(tmp_path, requests)
    handler.settings = handler.settings.model_copy(update=_installs(tmp_path))
    # The manifest says default; the delivery carries Node 24 from its first preparation.
    payload = _carrying_node_24(payload)
    resolved = handler._resolve_dependencies(payload)
    assert resolved["status"] == "DEPENDENCIES_RESOLVED"
    assert resolved["toolchain_preparation"]["node_toolchain"]["node"] == "24"


def test_review_2_baseline_runs_under_the_carried_selection(tmp_path: Path) -> None:
    handler, _, _, baseline, payload = _baseline_setup(tmp_path, {tuple(MYPY): 1})
    handler.settings = handler.settings.model_copy(
        update={
            "project_config_root": _manifests(tmp_path / "m", "musematic", {"node": "24"}),
            **_installs(tmp_path),
        }
    )
    # The manifest says Node 24 but the delivery carried the default: baseline stays default.
    assert handler._validate_changeset(payload)["status"] == "LOCAL_VALIDATION_PASS"
    [call] = baseline.calls
    assert call["environment"]["PATH"] == f"/usr/local/bin:{_DEFAULT_PATH}"


def test_review_2_present_null_selection_is_malformed(tmp_path: Path) -> None:
    settings = Settings()
    with pytest.raises(ValueError):
        payload_node_toolchain(settings, {"toolchain_preparation": {"node_toolchain": None}})
    assert payload_node_toolchain(
        settings, {"toolchain_preparation": {}}
    ) == payload_node_toolchain(settings, {})
