"""NEW-49: a maker test FAIL that already fails at the base commit is not a blocker."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.adapters.command import CommandResult
from engineering_control.config import Settings
from engineering_control.engineering.toolchain import (
    load_toolchain_policy,
    match_toolchain_test_command,
)
from engineering_control.worktrees.manager import WorktreeError

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT / "config" / "projects"
GIT = shutil.which("git") or "/usr/bin/git"
MYPY = ["timeout", "--kill-after=30", "900", ".venv/bin/mypy", "src/platform"]
PYTEST = [
    "timeout",
    "--kill-after=30",
    "900",
    ".venv/bin/pytest",
    "--import-mode=importlib",
    "--ignore=tests/integration",
    "--ignore=tests/e2e",
    "-q",
]
MYPY_COMMAND = "timeout --kill-after=30 900 .venv/bin/mypy src/platform"
MYPY_REPORTED = f"cd apps/control-plane && {MYPY_COMMAND}"
PYTEST_REPORTED = "cd apps/control-plane && " + " ".join(PYTEST)


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed Git executable in the test harness
        [GIT, *arguments], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _mirror(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "mirrors" / "musematic"
    for directory in ("apps/control-plane", "apps/web"):
        (repo / directory).mkdir(parents=True)
        (repo / directory / "README").write_text("x\n")
    (repo / ".gitignore").write_text(".venv/\nnode_modules/\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "b")
    return repo, _git(repo, "rev-parse", "HEAD")


class Manager:
    def __init__(self, mirror: Path) -> None:
        self.mirror = mirror
        self.mirror_error: WorktreeError | None = None

    def verify_mirror(self, repository: str) -> Path:
        if self.mirror_error is not None:
            raise self.mirror_error
        assert repository == "musematic"
        return self.mirror

    def change_evidence(self, path: Path, *, base_sha: str) -> tuple[dict[str, Any], str]:
        return {"changed_file_count": 1, "changed_paths": ["app.py"]}, "+fixed\n"

    def grant_delivery_read(self, repository: str, path: Path) -> None:
        pass

    def grant_release_read(self, repository: str, path: Path) -> None:
        pass


class Scanner:
    def secret_scan(self, worktree: Path, output: Path) -> dict[str, Any]:
        output.mkdir(parents=True, exist_ok=True)
        (output / "gitleaks.json").write_text("[]\n")
        return {"status": "PASS", "findings": []}

    def vulnerability_scan(self, worktree: Path, output: Path) -> dict[str, Any]:
        return {"Results": []}

    def semgrep(self, worktree: Path, output: Path) -> dict[str, Any]:
        return {"results": []}

    def sbom(self, worktree: Path, output: Path) -> dict[str, Any]:
        return {}


class Publisher:
    def publish(self, path: Path, **_: Any) -> dict[str, Any]:
        return {"id": str(uuid4()), "filename": path.name}


class PrepareRunner:
    """uv/pnpm double: creates the virtualenv the baseline test commands need."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []
        self.return_code = 0

    def run(self, executable: str, arguments: Any, *, cwd: Path, **_: Any) -> CommandResult:
        self.calls.append((executable, cwd))
        if executable == "uv":
            (cwd / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        return CommandResult(self.return_code, "", "resolver failed" if self.return_code else "")


class BaselineRunner:
    """Contract test command double: exit code per argv, as run at the base commit."""

    def __init__(self, exit_codes: dict[tuple[str, ...], int]) -> None:
        self.exit_codes = exit_codes
        self.calls: list[dict[str, Any]] = []
        self.raises: BaseException | None = None

    def run(self, executable: str, arguments: Any, *, cwd: Path, **kwargs: Any) -> CommandResult:
        argv = (executable, *arguments)
        self.calls.append({"argv": list(argv), "cwd": cwd, **kwargs})
        if self.raises is not None:
            raise self.raises
        code = self.exit_codes[argv]
        return CommandResult(code, "src/platform/x.py:1: error: ghp_" + "A" * 36, "")


def _setup(
    tmp_path: Path, exit_codes: dict[tuple[str, ...], int]
) -> tuple[ScanActivityHandler, Manager, PrepareRunner, BaselineRunner, dict[str, Any]]:
    mirror, base_sha = _mirror(tmp_path)
    worktrees = tmp_path / "worktrees"
    source = worktrees / "musematic" / str(uuid4())
    source.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", str(mirror), str(source))
    manager = Manager(mirror)
    prepare = PrepareRunner()
    baseline = BaselineRunner(exit_codes)
    handler = _handler(tmp_path, manager, prepare, baseline)
    maker = {"structured_result": {"tests": [{"command": MYPY_REPORTED, "status": "FAIL"}]}}
    payload = {
        "project": "musematic",
        "change_set_id": str(uuid4()),
        "source_worktree": str(source),
        "deployment_worktree": str(source),
        "source_repository": "musematic",
        "deployment_repository": "musematic-deploy",
        "source_base_sha": base_sha,
        "deployment_base_sha": "b" * 40,
        "agent_result": {"structured_result": {"decision": "PASS"}},
        "source_maker_result": maker,
        "deployment_maker_result": {"structured_result": {"tests": []}},
    }
    return handler, manager, prepare, baseline, payload


def _handler(
    tmp_path: Path, manager: Manager, prepare: PrepareRunner, baseline: BaselineRunner
) -> ScanActivityHandler:
    return ScanActivityHandler(
        Settings(
            worktree_root=tmp_path / "worktrees",
            evidence_root=tmp_path / "evidence",
            project_config_root=PROJECTS,
            toolchain_cache_root=tmp_path / "cache" / "toolchain",
        ),
        manager,  # type: ignore[arg-type]
        scanner=Scanner(),  # type: ignore[arg-type]
        publisher=Publisher(),  # type: ignore[arg-type]
        toolchain_runner=prepare,  # type: ignore[arg-type]
        baseline_runner=baseline,  # type: ignore[arg-type]
    )


def _baseline_root(tmp_path: Path, payload: dict[str, Any]) -> Path:
    return tmp_path / "cache" / "toolchain" / "baselines" / "musematic" / payload["source_base_sha"]


def test_preexisting_contract_failure_does_not_block_a_checker_pass(tmp_path: Path) -> None:
    # 2026-09-24, musematic #993: the checker PASSed, but `mypy src/platform` failed on
    # an error in an untouched file that already exists on the base commit.
    handler, _, prepare, baseline, payload = _setup(tmp_path, {tuple(MYPY): 1})

    validated = handler._validate_changeset(payload)

    assert validated["status"] == "LOCAL_VALIDATION_PASS"
    assert validated["blocker"] is None
    [evidence] = validated["preexisting_test_failures"]
    assert evidence["command"] == MYPY_REPORTED
    assert evidence["base_sha"] == payload["source_base_sha"]
    assert evidence["base_exit_code"] == 1
    assert "ghp_" not in evidence["base_output_tail"]
    checkout = _baseline_root(tmp_path, payload) / "worktree"
    assert _git(checkout, "rev-parse", "HEAD") == payload["source_base_sha"]
    assert [executable for executable, _ in prepare.calls] == ["uv", "pnpm"]
    [call] = baseline.calls
    assert call["argv"] == MYPY
    assert call["cwd"] == (checkout / "apps" / "control-plane").resolve()
    assert call["timeout_seconds"] == 1200
    assert call["environment"]["UV_PYTHON_DOWNLOADS"] == "never"


def test_failure_that_passes_at_the_base_still_blocks(tmp_path: Path) -> None:
    handler, _, _, baseline, payload = _setup(tmp_path, {tuple(MYPY): 0})

    validated = handler._validate_changeset(payload)

    assert validated["status"] == "BLOCKED"
    assert validated["blocker"] == "MAKER_TEST_FAILURE"
    assert "preexisting_test_failures" not in validated
    assert "baseline_error" not in validated
    assert len(baseline.calls) == 1


def test_one_new_failure_among_preexisting_ones_still_blocks(tmp_path: Path) -> None:
    handler, _, _, _, payload = _setup(tmp_path, {tuple(MYPY): 1, tuple(PYTEST): 0})
    payload["deployment_maker_result"] = {
        "structured_result": {"tests": [{"command": PYTEST_REPORTED, "status": "FAIL"}]}
    }

    validated = handler._validate_changeset(payload)

    assert validated["blocker"] == "MAKER_TEST_FAILURE"


def test_checker_request_changes_is_unchanged_and_never_runs_the_baseline(
    tmp_path: Path,
) -> None:
    handler, _, prepare, baseline, payload = _setup(tmp_path, {tuple(MYPY): 1})
    payload["agent_result"] = {"structured_result": {"decision": "REQUEST_CHANGES"}}

    validated = handler._validate_changeset(payload)

    assert validated["status"] == "REQUEST_CHANGES"
    assert validated["blocker"] == "INDEPENDENT_CHECKER_DID_NOT_PASS"
    assert prepare.calls == [] and baseline.calls == []
    assert not (tmp_path / "cache" / "toolchain" / "baselines").exists()


@pytest.mark.parametrize(
    ("project", "command"),
    [
        ("musematic", "cd apps/control-plane && .venv/bin/pytest tests/test_new.py"),
        ("musematic", "cd apps/web && timeout --kill-after=30 900 .venv/bin/mypy src/platform"),
        ("musematic", "make test"),
        ("falcone", MYPY_REPORTED),  # toolchain disabled: no contract commands
    ],
)
def test_non_contract_failure_blocks_without_running_the_baseline(
    tmp_path: Path, project: str, command: str
) -> None:
    handler, _, prepare, baseline, payload = _setup(tmp_path, {tuple(MYPY): 1})
    payload["project"] = project
    payload["source_maker_result"] = {
        "structured_result": {"tests": [{"command": command, "status": "FAIL"}]}
    }

    validated = handler._validate_changeset(payload)

    assert validated["blocker"] == "MAKER_TEST_FAILURE"
    assert prepare.calls == [] and baseline.calls == []


def test_cached_baseline_is_reused_by_retries_and_corrections(tmp_path: Path) -> None:
    handler, manager, prepare, baseline, payload = _setup(tmp_path, {tuple(MYPY): 1})
    first = handler._validate_changeset(payload)
    # A correction attempt of the same issue: a new ChangeSet and a new handler process.
    retry = _handler(tmp_path, manager, prepare, baseline)
    second = retry._validate_changeset({**payload, "change_set_id": str(uuid4())})

    assert first["preexisting_test_failures"] == second["preexisting_test_failures"]
    assert len(baseline.calls) == 1
    assert len(prepare.calls) == 2
    cached = json.loads((_baseline_root(tmp_path, payload) / "results.json").read_text())
    assert [entry["exit_code"] for entry in cached.values()] == [1]


def test_prepared_baseline_checkout_is_reused_for_another_command(tmp_path: Path) -> None:
    handler, _, prepare, baseline, payload = _setup(tmp_path, {tuple(MYPY): 1, tuple(PYTEST): 2})
    handler._validate_changeset(payload)
    payload["source_maker_result"] = {
        "structured_result": {"tests": [{"command": PYTEST_REPORTED, "status": "FAIL"}]}
    }

    validated = handler._validate_changeset(payload)

    assert validated["preexisting_test_failures"][0]["base_exit_code"] == 2
    assert [call["argv"] for call in baseline.calls] == [MYPY, PYTEST]
    assert len(prepare.calls) == 2  # the base checkout was prepared only once


@pytest.mark.parametrize("failure", ["mirror", "prepare", "timeout", "not_allowed", "base_sha"])
def test_baseline_errors_fail_closed(tmp_path: Path, failure: str) -> None:
    handler, manager, prepare, baseline, payload = _setup(tmp_path, {tuple(MYPY): 1})
    if failure == "mirror":
        manager.mirror_error = WorktreeError("canonical mirror does not exist")
    elif failure == "prepare":
        prepare.return_code = 1
    elif failure == "timeout":
        baseline.raises = subprocess.TimeoutExpired(MYPY, 1200)
    elif failure == "not_allowed":
        baseline.raises = ValueError("executable is not allowed: timeout")
    else:
        payload["source_base_sha"] = "not-a-commit"

    validated = handler._validate_changeset(payload)

    assert validated["status"] == "BLOCKED"
    assert validated["blocker"] == "MAKER_TEST_FAILURE"
    assert validated["baseline_error"]
    assert "preexisting_test_failures" not in validated
    results = _baseline_root(tmp_path, payload) / "results.json"
    assert not results.exists()  # errors are never cached


def test_skipped_maker_tests_are_unchanged(tmp_path: Path) -> None:
    handler, _, _, baseline, payload = _setup(tmp_path, {tuple(MYPY): 1})
    payload["source_maker_result"] = {
        "structured_result": {"tests": [{"command": MYPY_REPORTED, "status": "SKIPPED"}]}
    }

    validated = handler._validate_changeset(payload)

    assert validated["status"] == "LOCAL_VALIDATION_PASS"
    assert "preexisting_test_failures" not in validated
    assert baseline.calls == []


def test_baseline_executables_stay_inside_the_checkout(tmp_path: Path) -> None:
    handler, _, _, _, _ = _setup(tmp_path, {})
    root = tmp_path / "checkout"
    (root / "app" / ".venv" / "bin").mkdir(parents=True)
    (root / "app" / ".venv" / "bin" / "pytest").write_text("x\n")

    allowed = handler._baseline_executables(root, root / "app", ".venv/bin/pytest")

    assert allowed[".venv/bin/pytest"] == root / "app" / ".venv" / "bin" / "pytest"
    assert allowed["timeout"] == Path("/usr/bin/timeout")
    with pytest.raises(ValueError, match="not in the checkout"):
        handler._baseline_executables(root, root / "app", "../../outside/bin/tool")


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        (MYPY_REPORTED, MYPY),
        (MYPY_COMMAND, MYPY),
        (f"cd ./apps/control-plane/ && {MYPY_COMMAND}", MYPY),
        (f"(cd apps/control-plane && {MYPY_COMMAND})", MYPY),
        (f"cd /wt/apps/control-plane && {MYPY_COMMAND}", MYPY),
        (f"cd /elsewhere/apps/control-plane && {MYPY_COMMAND}", None),
        (".venv/bin/mypy src/platform", None),
        (f"{MYPY_COMMAND}; true", None),
        ("cd apps/control-plane && timeout 900 .venv/bin/mypy 'src/platform", None),
        # NEW-58: the "<cwd>: <command>" form a maker used on musematic #993.
        (f"apps/control-plane: {MYPY_COMMAND}", MYPY),
        (f"apps/web: {MYPY_COMMAND}", None),
    ],
)
def test_reported_commands_match_contract_commands_exactly(
    reported: str, expected: list[str] | None
) -> None:
    policy = load_toolchain_policy(PROJECTS, "musematic")
    assert policy is not None

    command = match_toolchain_test_command(policy, reported, Settings(), worktree=Path("/wt"))

    assert (command.argv if command is not None else None) == expected
