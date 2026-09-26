"""delivery.prepare_toolchain: per-worktree project toolchain for offline makers/checker."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.adapters.command import CommandResult
from engineering_control.config import Settings
from engineering_control.engineering.models import (
    ProjectManifest,
    ToolchainPolicy,
    ToolchainTestCommand,
)
from engineering_control.engineering.toolchain import load_toolchain_policy, render_argv

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT / "config" / "projects"
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


GIT = shutil.which("git") or "/usr/bin/git"


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed Git executable in the test harness
        [GIT, *arguments], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _source_repo(worktree_root: Path) -> Path:
    repo = worktree_root / "musematic" / str(uuid4())
    (repo / "apps" / "control-plane").mkdir(parents=True)
    (repo / "apps" / "control-plane" / "pyproject.toml").write_text("[project]\n")
    (repo / "package.json").write_text("{}\n")
    (repo / ".gitignore").write_text(".venv/\nnode_modules/\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "-m",
        "base",
    )
    return repo


class FakeToolchainRunner:
    """Installer double: creates what uv/pnpm would, or fails as configured."""

    def __init__(self, *, creates: dict[str, list[str]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.creates = creates or {"uv": [".venv/bin/python"], "pnpm": ["node_modules/a.js"]}
        self.result: CommandResult | None = None
        self.raises: BaseException | None = None

    def run(self, executable: str, arguments: Any, *, cwd: Path, **kwargs: Any) -> CommandResult:
        self.calls.append({"executable": executable, "arguments": list(arguments), "cwd": cwd})
        self.calls[-1].update(kwargs)
        if self.raises is not None:
            raise self.raises
        if self.result is not None:
            return self.result
        for relative in self.creates.get(executable, []):
            target = cwd / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x\n")
        return CommandResult(0, "", "")


def _handler(tmp_path: Path, runner: FakeToolchainRunner, **overrides: Any) -> ScanActivityHandler:
    settings = Settings(
        worktree_root=tmp_path / "worktrees",
        evidence_root=tmp_path / "evidence",
        project_config_root=overrides.pop("project_config_root", PROJECTS),
        toolchain_cache_root=tmp_path / "cache" / "toolchain",
        python312_executable=Path("/opt/python/bin/python3.12"),
        **overrides,
    )
    return ScanActivityHandler(settings, toolchain_runner=runner)  # type: ignore[arg-type]


def _payload(repo: Path, project: str = "musematic") -> dict[str, Any]:
    return {"project": project, "change_set_id": str(uuid4()), "source_worktree": str(repo)}


# -- contract -------------------------------------------------------------------------


@pytest.mark.parametrize("cwd", ["../outside", "/abs/path", "apps/../..", "apps//web", "~/x"])
def test_toolchain_contract_rejects_paths_outside_the_repository(cwd: str) -> None:
    with pytest.raises(ValidationError, match="repository-relative"):
        ToolchainPolicy.model_validate(
            {"enabled": True, "prepare": [{"cwd": cwd, "argv": ["uv", "sync"]}]}
        )


@pytest.mark.parametrize(
    "argv",
    [["bash", "-c", "uv sync"], ["npm", "ci"], ["/usr/local/bin/uv", "sync"], ["curl", "x"]],
)
def test_toolchain_contract_rejects_executables_outside_the_allowlist(argv: list[str]) -> None:
    with pytest.raises(ValidationError, match="must be one of pnpm, uv"):
        ToolchainPolicy.model_validate({"enabled": True, "prepare": [{"cwd": ".", "argv": argv}]})


def test_toolchain_contract_rejects_unknown_placeholders_shells_and_unbounded_budgets() -> None:
    with pytest.raises(ValidationError, match="python312"):
        ToolchainPolicy.model_validate(
            {"enabled": True, "prepare": [{"cwd": ".", "argv": ["uv", "{home}"]}]}
        )
    with pytest.raises(ValidationError, match="non-shell"):
        ToolchainPolicy.model_validate(
            {"test_commands": [{"cwd": ".", "argv": ["bash", "-c", "x"], "description": "d"}]}
        )
    with pytest.raises(ValidationError, match="at least one prepare"):
        ToolchainPolicy.model_validate({"enabled": True})
    with pytest.raises(ValidationError, match="budget"):
        ToolchainPolicy.model_validate(
            {
                "enabled": True,
                "prepare": [{"cwd": ".", "argv": ["uv", "sync"], "timeout_seconds": 3600}] * 2,
            }
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        ToolchainPolicy.model_validate({"enabled": False, "shell": "make"})


def test_musematic_manifest_declares_a_valid_toolchain_and_real_test_command() -> None:
    policy = load_toolchain_policy(PROJECTS, "musematic")
    assert policy is not None and policy.enabled
    assert [command.argv[0] for command in policy.prepare] == ["uv", "pnpm"]
    rendered = render_argv(policy.prepare[0].argv, Settings())
    assert rendered[-2:] == ["--python", "/usr/local/bin/python3.12"]
    # NEW-64: the OpenAPI snapshot regeneration runs from the repository root.
    assert {command.cwd for command in policy.test_commands} == {
        "apps/control-plane",
        "apps/web",
        ".",
    }
    manifest = ProjectManifest.model_validate(
        yaml.safe_load((PROJECTS / "musematic.yaml").read_text())
    )
    assert ["make", "test"] not in manifest.source_test_commands
    assert "pytest" in manifest.source_test_commands[0][0]
    assert load_toolchain_policy(PROJECTS, "falcone") is None


# -- prepare --------------------------------------------------------------------------


def test_prepare_installs_in_the_source_worktree_with_private_offline_caches(
    tmp_path: Path,
) -> None:
    repo = _source_repo(tmp_path / "worktrees")
    runner = FakeToolchainRunner()
    handler = _handler(tmp_path, runner)

    result = handler._prepare_toolchain(_payload(repo))

    assert result["status"] == "TOOLCHAIN_READY"
    assert result["blocker"] is None
    summary = result["toolchain_preparation"]
    assert summary["ignored_paths_created"] == ["apps/control-plane/.venv/", "node_modules/"]
    assert [item["status"] for item in summary["commands"]] == ["PASS", "PASS"]
    assert summary["test_commands"][0]["argv"][:4] == [
        "timeout",
        "--kill-after=30",
        "900",
        ".venv/bin/pytest",
    ]
    uv, pnpm = runner.calls
    assert uv["cwd"] == repo / "apps" / "control-plane"
    assert uv["arguments"][-2:] == ["--python", "/opt/python/bin/python3.12"]
    assert pnpm["cwd"] == repo and pnpm["arguments"] == ["install", "--frozen-lockfile"]
    environment = uv["environment"]
    cache = tmp_path / "cache" / "toolchain"
    assert environment["UV_PYTHON_DOWNLOADS"] == "never"
    assert environment["CI"] == "true"
    assert environment["UV_CACHE_DIR"] == str(cache / "uv")
    assert environment["npm_config_store_dir"] == str(cache / "pnpm-store")
    assert environment["HOME"] == str(cache / "home")
    assert not any("TOKEN" in key or "SSH" in key for key in environment)
    assert stat.S_IMODE(os.stat(cache).st_mode) == 0o750
    assert uv["timeout_seconds"] == 1200 and uv["check"] is False
    assert _git(repo, "status", "--porcelain") == ""


def test_prepare_failure_blocks_with_a_redacted_bounded_tail(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path / "worktrees")
    runner = FakeToolchainRunner()
    runner.result = CommandResult(2, "", "x" * 5000 + f"\nerror: auth {TOKEN} rejected\n")
    handler = _handler(tmp_path, runner)

    result = handler._prepare_toolchain(_payload(repo))

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "TOOLCHAIN_PREPARE_FAILED"
    detail = result["toolchain_preparation"]["detail"]
    assert len(detail) <= 2000 and "rejected" in detail
    assert TOKEN not in detail
    assert result["toolchain_preparation"]["commands"][0]["status"] == "FAILED"
    assert len(runner.calls) == 1  # fail fast: pnpm never ran


def test_prepare_timeout_and_missing_executable_block(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path / "worktrees")
    runner = FakeToolchainRunner()
    runner.raises = subprocess.TimeoutExpired(["uv"], 1200)
    assert _handler(tmp_path, runner)._prepare_toolchain(_payload(repo))["blocker"] == (
        "TOOLCHAIN_PREPARE_FAILED"
    )
    runner.raises = FileNotFoundError("/usr/local/bin/uv")
    blocked = _handler(tmp_path, runner)._prepare_toolchain(_payload(repo))
    assert blocked["blocker"] == "TOOLCHAIN_PREPARE_FAILED"
    assert blocked["toolchain_preparation"]["commands"][0]["status"] == "NOT_STARTED"


def test_prepare_fails_closed_when_created_paths_are_not_git_ignored(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path / "worktrees")
    runner = FakeToolchainRunner(
        creates={"uv": [".venv/bin/python"], "pnpm": ["node_modules/a.js", "build-cache/x"]}
    )

    result = _handler(tmp_path, runner)._prepare_toolchain(_payload(repo))

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "TOOLCHAIN_ARTIFACTS_NOT_IGNORED"
    assert "build-cache/" in result["toolchain_preparation"]["detail"]


def test_prepare_fails_closed_when_a_tracked_file_is_modified(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path / "worktrees")
    runner = FakeToolchainRunner(creates={"uv": [".venv/bin/python"], "pnpm": ["package.json"]})

    result = _handler(tmp_path, runner)._prepare_toolchain(_payload(repo))

    assert result["blocker"] == "TOOLCHAIN_MODIFIED_TRACKED_FILES"


def test_prepare_rejects_a_worktree_outside_the_bounded_root(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path / "elsewhere")
    (tmp_path / "worktrees").mkdir()
    with pytest.raises(ValueError, match="bounded root"):
        _handler(tmp_path, FakeToolchainRunner())._prepare_toolchain(_payload(repo))


def test_prepare_is_skipped_when_the_project_has_no_enabled_toolchain(tmp_path: Path) -> None:
    runner = FakeToolchainRunner()
    handler = _handler(tmp_path, runner)

    result = handler._prepare_toolchain({"project": "falcone", "source_worktree": "/nonexistent"})

    assert result["status"] == "TOOLCHAIN_SKIPPED"
    assert runner.calls == []


def test_prepare_blocks_on_an_unreadable_contract(tmp_path: Path) -> None:
    handler = _handler(tmp_path, FakeToolchainRunner(), project_config_root=tmp_path / "none")
    result = handler._prepare_toolchain({"project": "musematic", "source_worktree": "/x"})
    assert result["blocker"] == "TOOLCHAIN_CONTRACT_INVALID"


# -- postcheck cleanup -------------------------------------------------------------------


class RecordingScanner:
    def __init__(self) -> None:
        self.seen: dict[str, bool] = {}

    def secret_scan(self, worktree: Path, output: Path) -> dict[str, Any]:
        output.mkdir(parents=True, exist_ok=True)
        (output / "gitleaks.json").write_text("[]\n")
        self.seen[str(worktree)] = any(
            part in {".venv", "node_modules"} for path in worktree.rglob("*") for part in path.parts
        )
        return {"status": "PASS", "findings": []}

    def vulnerability_scan(self, worktree: Path, output: Path) -> dict[str, Any]:
        return {"Results": []}

    def semgrep(self, worktree: Path, output: Path) -> dict[str, Any]:
        return {"results": []}

    def sbom(self, worktree: Path, output: Path) -> dict[str, Any]:
        return {}


class ChangeManager:
    def change_evidence(self, path: Path, *, base_sha: str) -> tuple[dict[str, Any], str]:
        return {"changed_file_count": 1, "changed_paths": ["app.py"]}, "+fixed\n"

    def grant_delivery_read(self, repository: str, path: Path) -> None:
        pass

    def grant_release_read(self, repository: str, path: Path) -> None:
        pass


class Publisher:
    def publish(self, path: Path, **_: Any) -> dict[str, Any]:
        return {"id": str(uuid4()), "filename": path.name}


def _validation(tmp_path: Path, project: str) -> tuple[ScanActivityHandler, RecordingScanner, Path]:
    worktrees = tmp_path / "worktrees"
    repo = _source_repo(worktrees)
    (repo / "apps" / "control-plane" / ".venv" / "bin").mkdir(parents=True)
    (repo / "apps" / "control-plane" / ".venv" / "bin" / "python").write_text("x\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "a.js").write_text("x\n")
    (repo / "new_module.py").write_text("print('maker work')\n")  # untracked, not ignored
    (repo / "package.json").write_text('{"changed": true}\n')  # tracked, modified
    scanner = RecordingScanner()
    handler = ScanActivityHandler(
        Settings(
            worktree_root=worktrees,
            evidence_root=tmp_path / "evidence",
            project_config_root=PROJECTS,
        ),
        ChangeManager(),  # type: ignore[arg-type]
        scanner=scanner,  # type: ignore[arg-type]
        publisher=Publisher(),  # type: ignore[arg-type]
    )
    return handler, scanner, repo


def _validation_payload(repo: Path, project: str) -> dict[str, Any]:
    maker = {"structured_result": {"tests": [{"command": "pytest", "status": "PASS"}]}}
    return {
        "project": project,
        "change_set_id": str(uuid4()),
        "source_worktree": str(repo),
        "deployment_worktree": str(repo),
        "source_repository": "musematic",
        "deployment_repository": "musematic-deploy",
        "source_base_sha": "a" * 40,
        "deployment_base_sha": "b" * 40,
        "agent_result": {"structured_result": {"decision": "PASS"}},
        "source_maker_result": maker,
        "deployment_maker_result": maker,
    }


def test_postchecks_remove_only_ignored_toolchain_files_before_scanning(tmp_path: Path) -> None:
    handler, scanner, repo = _validation(tmp_path, "musematic")

    handler._validate_changeset(_validation_payload(repo, "musematic"))

    assert scanner.seen == {str(repo): False}
    assert not (repo / "node_modules").exists()
    assert not (repo / "apps" / "control-plane" / ".venv").exists()
    assert (repo / "new_module.py").read_text() == "print('maker work')\n"
    assert (repo / "package.json").read_text() == '{"changed": true}\n'
    assert (repo / ".gitignore").exists()


def test_postchecks_do_not_clean_when_the_toolchain_is_disabled(tmp_path: Path) -> None:
    handler, scanner, repo = _validation(tmp_path, "falcone")

    handler._validate_changeset(_validation_payload(repo, "falcone"))

    assert scanner.seen == {str(repo): True}
    assert (repo / "node_modules" / "a.js").exists()


@pytest.mark.parametrize(
    ("argv", "allowed"),
    [
        (["timeout", "--kill-after=30", "900", ".venv/bin/pytest", "-q"], True),
        (["timeout", "900", "pnpm", "exec", "vitest", "run"], True),
        (["timeout", "--kill-after=30", "900", "bash", "-c", "true"], False),
        (["timeout", "900", "/usr/bin/pytest"], False),
        (["timeout", "900"], False),
    ],
)
def test_timeout_wrapped_test_commands_check_the_wrapped_command(
    argv: list[str], allowed: bool
) -> None:
    """NEW-46: a `timeout` wrapper must not hide a shell or absolute executable."""
    command = {"cwd": ".", "argv": argv, "description": "tests"}
    if allowed:
        assert ToolchainTestCommand.model_validate(command).argv == argv
    else:
        with pytest.raises(ValidationError):
            ToolchainTestCommand.model_validate(command)


# -- NEW-75: platform dependency resolution ----------------------------------------------


class FakeDependencyRunner:
    """go / pnpm double: rewrites the lockfile the real command would regenerate."""

    def __init__(self, writes: dict[str, str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.writes = writes or {"pnpm": "pnpm-lock.yaml", "go": "go.sum"}
        self.result: CommandResult | None = None

    def run(self, executable: str, arguments: Any, *, cwd: Path, **kwargs: Any) -> CommandResult:
        self.calls.append(
            {"executable": executable, "arguments": list(arguments), "cwd": cwd, **kwargs}
        )
        if self.result is not None:
            return self.result
        (cwd / self.writes[executable]).write_text(f"resolved by {executable}\n")
        return CommandResult(0, "", "")


def _dependency_setup(
    tmp_path: Path, requests: list[dict[str, str]], runner: FakeDependencyRunner | None = None
) -> tuple[ScanActivityHandler, FakeDependencyRunner, FakeToolchainRunner, dict[str, Any], Path]:
    repo = _source_repo(tmp_path / "worktrees")
    _git(repo, "config", "user.name", "Engineering Control")
    _git(repo, "config", "user.email", "engineering-control@local.invalid")
    (repo / "services" / "api").mkdir(parents=True)
    (repo / "services" / "api" / "go.mod").write_text("module example.invalid/api\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "go module")
    dependencies = runner or FakeDependencyRunner()
    toolchain = FakeToolchainRunner()
    settings = Settings(
        worktree_root=tmp_path / "worktrees",
        evidence_root=tmp_path / "evidence",
        project_config_root=PROJECTS,
        toolchain_cache_root=tmp_path / "cache" / "toolchain",
        python312_executable=Path("/opt/python/bin/python3.12"),
    )
    handler = ScanActivityHandler(
        settings,
        toolchain_runner=toolchain,  # type: ignore[arg-type]
        dependency_runner=dependencies,  # type: ignore[arg-type]
    )
    payload = {
        **_payload(repo),
        "agent_result": {
            "structured_result": {
                "decision": "DEPENDENCIES_REQUIRED",
                "dependency_resolution": requests,
            }
        },
    }
    return handler, dependencies, toolchain, payload, repo


def test_dependency_resolution_regenerates_and_commits_only_lockfiles(tmp_path: Path) -> None:
    # NEW-75: musematic #1125's offline maker could not bump grpc/Next.js because it
    # cannot regenerate go.sum or pnpm-lock.yaml; the scan worker (network) does it.
    handler, dependencies, toolchain, payload, repo = _dependency_setup(
        tmp_path,
        [{"ecosystem": "go", "directory": "services/api"}, {"ecosystem": "pnpm", "directory": "."}],
    )
    base = _git(repo, "rev-parse", "HEAD").strip()

    resolved = handler._resolve_dependencies(payload)

    assert resolved["status"] == "DEPENDENCIES_RESOLVED"
    result = resolved["dependency_resolution_result"]
    assert result["files"] == ["pnpm-lock.yaml", "services/api/go.sum"]
    assert result["commit"] == _git(repo, "rev-parse", "HEAD").strip() != base
    assert _git(repo, "status", "--porcelain").strip() == ""
    assert [(call["executable"], call["arguments"]) for call in dependencies.calls] == [
        ("go", ["mod", "tidy"]),
        ("pnpm", ["install", "--lockfile-only", "--ignore-scripts"]),
    ]
    go_env = dependencies.calls[0]["environment"]
    assert go_env["GOTOOLCHAIN"] == "local" and go_env["GOMODCACHE"].startswith(str(tmp_path))
    assert go_env["HOME"].startswith(str(tmp_path))  # never the runner's real home
    assert toolchain.calls  # the toolchain is reinstalled for the maker's next round


def test_dependency_resolution_rejects_other_file_changes_and_bad_requests(
    tmp_path: Path,
) -> None:
    runner = FakeDependencyRunner(writes={"pnpm": "apps/control-plane/pyproject.toml"})
    handler, _, _, payload, repo = _dependency_setup(
        tmp_path, [{"ecosystem": "pnpm", "directory": "."}], runner
    )
    touched = handler._resolve_dependencies(payload)
    assert touched["blocker"] == "DEPENDENCY_RESOLUTION_TOUCHED_OTHER_FILES"

    for bad in (
        [{"ecosystem": "cargo", "directory": "."}],
        [{"ecosystem": "go", "directory": "../outside"}],
        [{"ecosystem": "go", "directory": "apps/control-plane"}],  # no go.mod there
        [],
    ):
        other, _, _, other_payload, _ = _dependency_setup(tmp_path / str(len(bad)) / "x", bad)
        assert other._resolve_dependencies(other_payload)["blocker"] == "DEPENDENCY_REQUEST_INVALID"


def test_dependency_resolution_blocks_a_dirty_worktree_and_a_failed_command(tmp_path: Path) -> None:
    handler, _, _, payload, repo = _dependency_setup(
        tmp_path, [{"ecosystem": "go", "directory": "services/api"}]
    )
    (repo / "package.json").write_text('{"dirty": true}\n')
    assert handler._resolve_dependencies(payload)["blocker"] == "DEPENDENCY_WORKTREE_DIRTY"

    failing = FakeDependencyRunner()
    failing.result = CommandResult(1, "", "go: module lookup disabled")
    handler, _, _, payload, _ = _dependency_setup(
        tmp_path / "failing", [{"ecosystem": "go", "directory": "services/api"}], failing
    )
    blocked = handler._resolve_dependencies(payload)
    assert blocked["blocker"] == "DEPENDENCY_RESOLUTION_FAILED"
    assert "module lookup disabled" in blocked["dependency_resolution_result"]["detail"]
