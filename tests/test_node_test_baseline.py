"""NEW-161: a maker FAIL in a non-contract `node --test` suite that already fails at base."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.adapters.command import CommandResult
from engineering_control.config import Settings
from engineering_control.engineering.node_test_baseline import (
    EVIDENCE_TEST_NAMES,
    NodeTestCommand,
    bounded_names,
    expand_test_paths,
    parse_node_test_command,
    parse_reporter_output,
    sandbox_argv,
)
from engineering_control.worktrees.manager import WorktreeError

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT / "config" / "projects"
GIT = shutil.which("git") or "/usr/bin/git"
PREFIXES = ("tests/", "test/")
# falcone #978 (2026-10-08), as the maker reported it.
REPORTED_978 = (
    "node --test --experimental-test-isolation=none "
    "tests/integration/097-plan-entity-tenant-assignment/*.test.mjs "
    "tests/integration/098-plan-base-limits/*.test.mjs"
)
FAILING_098 = [f"plan base limit case {index}" for index in range(9)]


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed Git executable in the test harness
        [GIT, *arguments], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


# --- parser ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reported", "cwd", "flags", "paths"),
    [
        (
            REPORTED_978,
            ".",
            ("--experimental-test-isolation=none",),
            (
                "tests/integration/097-plan-entity-tenant-assignment/*.test.mjs",
                "tests/integration/098-plan-base-limits/*.test.mjs",
            ),
        ),
        ("node --test tests/a.test.mjs", ".", (), ("tests/a.test.mjs",)),
        ("timeout 900 node --test tests/a.test.mjs", ".", (), ("tests/a.test.mjs",)),
        (
            "timeout --kill-after=30 900s node --test ./tests/a.test.mjs",
            ".",
            (),
            ("tests/a.test.mjs",),
        ),
        ("(node --test tests/a.test.mjs)", ".", (), ("tests/a.test.mjs",)),
        (
            "cd apps/web-console && node --test test/x/*.test.mjs",
            "apps/web-console",
            (),
            ("test/x/*.test.mjs",),
        ),
        ("cd /wt/source && node --test tests/a.test.mjs", ".", (), ("tests/a.test.mjs",)),
        ("cd /wt/source/apps/x/ && node --test tests/a?.mjs", "apps/x", (), ("tests/a?.mjs",)),
        # The maker's output format is dropped; the scan worker's reporter replaces it.
        (
            "node --test --test-reporter=tap --test-reporter-destination=stdout tests/a.mjs",
            ".",
            (),
            ("tests/a.mjs",),
        ),
        (
            "node --test --test-concurrency=1 '--test-name-pattern=plan (base) limits' "
            "--test-timeout=60000 --test-isolation=process tests/a.mjs",
            ".",
            (
                "--test-concurrency=1",
                "--test-name-pattern=plan (base) limits",
                "--test-timeout=60000",
                "--test-isolation=process",
            ),
            ("tests/a.mjs",),
        ),
    ],
)
def test_parser_accepts_only_the_plain_node_test_shape(
    reported: str, cwd: str, flags: tuple[str, ...], paths: tuple[str, ...]
) -> None:
    command = parse_node_test_command(reported, prefixes=PREFIXES, worktree=Path("/wt/source"))

    assert command == NodeTestCommand(cwd, flags, paths)
    assert command.argv == ["node", "--test", *flags, *paths]


@pytest.mark.parametrize(
    "reported",
    [
        "node --test tests/a.mjs; rm -rf /",
        "node --test tests/a.mjs && curl https://example.invalid",
        "node --test tests/a.mjs | tee out.txt",
        "node --test tests/a.mjs > out.txt",
        "node --test tests/a.mjs\nrm -rf /",
        "node --test $(curl example.invalid) tests/a.mjs",
        "node --test tests/$(id).mjs",
        "node --test `id` tests/a.mjs",
        "node --test ${HOME}/tests/a.mjs",
        "node --test ~/tests/a.mjs",
        "node --test 'tests/a.mjs;id'",
        "node --test tests/../../etc/passwd",
        "node --test tests/./a.mjs",
        "node --test /etc/passwd",
        "node --test /wt/source/tests/a.mjs",
        "node --test src/a.test.mjs",
        "node --test tests",
        "node --test tests/.hidden/a.mjs",
        "node --test 'tests/a b.mjs'",
        "node --test tests/[ab].mjs",
        "node --test tests/**/a.mjs",
        "node --test tests/-a.mjs/x",
        "NODE_OPTIONS=--require=/tmp/x.js node --test tests/a.mjs",
        "env FOO=1 node --test tests/a.mjs",
        "npx node --test tests/a.mjs",
        "/usr/bin/node --test tests/a.mjs",
        "./node --test tests/a.mjs",
        "pnpm test",
        "corepack pnpm test",
        "node tests/a.mjs",
        "node --test",
        "node --test -- tests/a.mjs",
        "node --test --require=./evil.cjs tests/a.mjs",
        "node --test --import=./evil.mjs tests/a.mjs",
        "node --test --experimental-test-coverage tests/a.mjs",
        "node --test --test-reporter=./evil.mjs tests/a.mjs",
        "node --test --test-reporter-destination=/tmp/x tests/a.mjs",
        "node --test --test-concurrency=0 tests/a.mjs",
        "node --test --test-name-pattern tests/a.mjs",
        "node --test tests/a.mjs --test-name-pattern=a",
        "node --test --test-concurrency=1 --test-concurrency=1 tests/a.mjs",
        "node --test --inspect tests/a.mjs",
        "node --test 'tests/a.mjs",
        "timeout 900; node --test tests/a.mjs",
        "timeout abc node --test tests/a.mjs",
        "timeout --signal=KILL 900 node --test tests/a.mjs",
        "timeout node --test tests/a.mjs",
        "cd ../other && node --test tests/a.mjs",
        "cd /etc && node --test tests/a.mjs",
        "cd apps/../.. && node --test tests/a.mjs",
        "cd apps && cd .. && node --test tests/a.mjs",
        "cd $(id) && node --test tests/a.mjs",
        "apps/web: node --test tests/a.mjs",
        "",
        "   ",
    ],
)
def test_parser_rejects_everything_else(reported: str) -> None:
    assert parse_node_test_command(reported, prefixes=PREFIXES, worktree=Path("/wt/source")) is None


def test_parser_respects_the_configured_prefixes() -> None:
    assert parse_node_test_command("node --test spec/a.mjs", prefixes=PREFIXES) is None
    assert parse_node_test_command("node --test spec/a.mjs", prefixes=("spec/",)) is not None


def test_path_prefix_setting_is_validated(tmp_path: Path) -> None:
    assert Settings(node_test_path_prefixes=("spec/", "a/b/")).node_test_path_prefixes
    for prefixes in [(), ("tests",), ("/tests/",), ("../tests/",), ("tests/../x/",), ("*/",)]:
        with pytest.raises(ValueError, match="node test path prefixes"):
            Settings(node_test_path_prefixes=prefixes)


# --- glob expansion, reporter output, sandbox ------------------------------------------


def test_paths_are_expanded_by_a_bounded_walk_inside_the_checkout(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "tests" / "098").mkdir(parents=True)
    for name in ("b.test.mjs", "a.test.mjs", ".hidden.test.mjs", "helper.mjs"):
        (checkout / "tests" / "098" / name).write_text("")
    command = NodeTestCommand(".", (), ("tests/098/*.test.mjs", "tests/098/a.test.mjs"))

    assert expand_test_paths(checkout, command) == ["tests/098/a.test.mjs", "tests/098/b.test.mjs"]
    assert expand_test_paths(checkout, NodeTestCommand(".", (), ("tests/*/a.test.mjs",))) == [
        "tests/098/a.test.mjs"
    ]
    assert expand_test_paths(checkout, NodeTestCommand(".", (), ("tests/097/*.mjs",))) is None
    assert expand_test_paths(checkout, NodeTestCommand("missing", (), ("tests/*",))) is None


def test_symlinked_files_directories_and_loops_are_rejected(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "tests" / "a").mkdir(parents=True)
    (checkout / "tests" / "a" / "x.test.mjs").write_text("")
    outside = tmp_path / "outside.test.mjs"
    outside.write_text("")
    (checkout / "tests" / "a" / "z.test.mjs").symlink_to(outside)
    (checkout / "tests" / "loop").symlink_to(checkout / "tests")
    (checkout / "apps").symlink_to(checkout)

    assert expand_test_paths(checkout, NodeTestCommand(".", (), ("tests/a/*.mjs",))) is None
    assert expand_test_paths(checkout, NodeTestCommand(".", (), ("tests/a/x.test.mjs",))) == [
        "tests/a/x.test.mjs"
    ]
    # A symlink loop is never followed: the walk stops at the link, no blow-up.
    deep = "/".join(["*"] * 30)
    assert expand_test_paths(checkout, NodeTestCommand(".", (), (f"tests/{deep}",))) is None
    assert expand_test_paths(checkout, NodeTestCommand(".", (), ("tests/loop/a/*.mjs",))) is None
    assert expand_test_paths(checkout, NodeTestCommand("apps", (), ("tests/a/*.mjs",))) is None


def test_glob_walk_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import engineering_control.engineering.node_test_baseline as module

    checkout = tmp_path / "checkout"
    for index in range(30):
        (checkout / "tests" / f"d{index:02}").mkdir(parents=True)
        (checkout / "tests" / f"d{index:02}" / "a.test.mjs").write_text("")
    command = NodeTestCommand(".", (), ("tests/*/*.mjs",))
    assert expand_test_paths(checkout, command) is not None

    monkeypatch.setattr(module, "MAX_TEST_FILES", 10)
    assert expand_test_paths(checkout, command) is None
    monkeypatch.setattr(module, "MAX_TEST_FILES", 400)
    monkeypatch.setattr(module, "MAX_GLOB_VISITS", 20)
    assert expand_test_paths(checkout, command) is None


NONCE = "0123456789abcdef0123456789abcdef"
FILES = ["tests/a.test.mjs", "tests/b.test.mjs"]


def _fail(
    name: str,
    *,
    nesting: int = 0,
    file: str | None = "/work/tests/a.test.mjs",
    kind: str = "test",
    failure: str | None = "testCodeFailure",
    cause: str = "error",
    codes: tuple[str, ...] = (),
) -> str:
    record = {
        "name": name,
        "nesting": nesting,
        "file": file,
        "type": kind,
        "failure": failure,
        "error_cause": cause,
        "error_codes": list(codes),
    }
    return f"{NONCE} FAIL {json.dumps(record)}"


def _block(*lines: str, passed: int = 1, nonce: str = NONCE) -> str:
    begin = f"{nonce} BEGIN " + json.dumps({"hermes": 3, "passed": passed, "truncated": False})
    count = sum(" FAIL " in line for line in lines)
    return "\n".join([begin, *lines, f"{nonce} END {count}"])


def test_reporter_output_separates_test_level_from_file_level_failures() -> None:
    text = "console noise\n" + _block(
        _fail("dup", nesting=1),
        "interleaved test output",
        _fail("suite", kind="suite", failure="subtestsFailed", cause="string"),
        _fail("dup", file="/work/tests/b.test.mjs", failure="testTimeoutFailure"),
        # file-level: import/syntax error (isolation=none: no file) or a process exit
        _fail("tests/b.test.mjs", file=None, failure=None, cause="undefined"),
        _fail("tests/a.test.mjs", cause="string"),
        _fail("with hook", failure="hookFailed"),
        _fail("elsewhere", file="/work/tests/c.test.mjs"),
    )

    report = parse_reporter_output(text, NONCE, FILES, ".")

    assert report is not None
    assert report.passed == 1
    assert report.test_failures == Counter(
        {"tests/a.test.mjs#1: dup": 1, "tests/b.test.mjs#0: dup": 1}
    )
    assert report.other_failures == Counter(
        {
            "tests/a.test.mjs#0: suite": 1,
            "#0: tests/b.test.mjs": 1,
            "tests/a.test.mjs#0: tests/a.test.mjs": 1,
            "tests/a.test.mjs#0: with hook": 1,
            "tests/c.test.mjs#0: elsewhere": 1,
        }
    )


def test_reporter_output_needs_this_runs_complete_block() -> None:
    assert parse_reporter_output(_block(), NONCE, FILES, ".") is not None
    other = "f" * 32
    for text in [
        "",
        _block(nonce=other),  # another nonce (e.g. printed by test code)
        _block() + "\n" + _block(),  # two blocks
        _block(_fail("x")).rsplit("\n", 1)[0],  # no END marker: truncated tail
        _block(f"{NONCE} FAIL not json"),
        _block(f"{NONCE} FAIL []"),
        f"{NONCE} BEGIN " + json.dumps({"hermes": 3, "passed": 0, "truncated": True}),
        f"{NONCE} BEGIN " + json.dumps({"hermes": 1, "passed": 0, "truncated": False}),
    ]:
        assert parse_reporter_output(text, NONCE, FILES, ".") is None
    assert parse_reporter_output(_block(), "", FILES, ".") is None


def test_reporter_targets_are_relative_to_the_command_cwd() -> None:
    text = _block(_fail("x", file="/work/apps/web/test/a.test.mjs"))

    report = parse_reporter_output(text, NONCE, ["test/a.test.mjs"], "apps/web")

    assert report is not None
    assert report.test_failures == Counter({"apps/web/test/a.test.mjs#0: x": 1})


def test_evidence_names_are_bounded() -> None:
    names = bounded_names(Counter({f"t#0: {'x' * 500} {index:03}": 1 for index in range(80)}))

    assert len(names) == EVIDENCE_TEST_NAMES
    assert all(len(name) == 200 for name in names)


def test_sandbox_is_read_only_but_a_size_capped_tmpfs(tmp_path: Path) -> None:
    node = tmp_path / "node-22" / "bin" / "node"
    arguments = sandbox_argv(
        checkout=tmp_path / "checkout",
        command=NodeTestCommand(
            "apps/x", ("--experimental-test-isolation=none",), ("tests/*.mjs",)
        ),
        files=["tests/a.mjs"],
        node=node,
        reporter=tmp_path / "reporter.mjs",
        nonce=NONCE,
        variables={"npm_config_x": "false"},
    )

    assert arguments[:3] == ["--unshare-all", "--die-with-parent", "--new-session"]
    assert "--share-net" not in arguments
    # The only writable mount is a 256 MiB tmpfs; no host path is bound writable.
    assert "--bind" not in arguments and "--overlay-src" not in arguments
    index = arguments.index("--size")
    assert arguments[index : index + 4] == ["--size", str(256 * 1024 * 1024), "--tmpfs", "/tmp"]  # noqa: S108
    assert arguments.count("--tmpfs") == 1
    index = arguments.index(str(tmp_path / "checkout"))
    assert arguments[index - 1 : index + 2] == ["--ro-bind", str(tmp_path / "checkout"), "/work"]
    assert arguments[arguments.index("--chdir") - 4 : arguments.index("--chdir")] == [
        "--remount-ro",
        "/dev",
        "--remount-ro",
        "/",
    ]
    assert arguments[arguments.index("--chdir") + 1] == "/work/apps/x"
    installation = str(tmp_path / "node-22")
    index = arguments.index(installation)
    assert arguments[index - 1 : index + 2] == ["--ro-bind", installation, installation]
    assert "--clearenv" in arguments
    index = arguments.index("HERMES_NODE_TEST_NONCE")
    assert arguments[index - 1 : index + 2] == ["--setenv", "HERMES_NODE_TEST_NONCE", NONCE]
    assert arguments[arguments.index("--") :] == [
        "--",
        str(node),
        "--test",
        "--experimental-test-isolation=none",
        "--test-reporter=/run/hermes-node-test/reporter.mjs",
        "--test-reporter-destination=stdout",
        "tests/a.mjs",
    ]
    for bad in [{"checkout": Path("relative")}, {"nonce": "short"}]:
        values: dict[str, Any] = {
            "checkout": tmp_path / "checkout",
            "command": NodeTestCommand(".", (), ("tests/a.mjs",)),
            "files": ["tests/a.mjs"],
            "node": node,
            "reporter": tmp_path / "r.mjs",
            "nonce": NONCE,
            "variables": {},
            **bad,
        }
        with pytest.raises(ValueError, match="node test"):
            sandbox_argv(**values)


# --- delivery.validate_changeset -------------------------------------------------------

LIMITS = "tests/integration/098-plan-base-limits/limits.test.mjs"


def _write_suite(repo: Path, failing: list[str], *, passing: int = 7, prelude: str = "") -> None:
    for name in ("097-plan-entity-tenant-assignment", "098-plan-base-limits"):
        (repo / "tests" / "integration" / name).mkdir(parents=True, exist_ok=True)
    (
        repo / "tests" / "integration" / "097-plan-entity-tenant-assignment" / "a.test.mjs"
    ).write_text("import { test } from 'node:test';\ntest('assigns', () => {});\n")
    cases = [
        f"test({json.dumps(name)}, () => {{ throw new Error('Unhandled fake SQL'); }});"
        for name in failing
    ]
    cases += [f"test('passes {index}', () => {{}});" for index in range(passing)]
    (repo / LIMITS).write_text(
        "import { test } from 'node:test';\n" + prelude + "\n".join(cases) + "\n"
    )


def _repository(path: Path, failing: list[str], prelude: str = "") -> str:
    path.mkdir(parents=True)
    _write_suite(path, failing, prelude=prelude)
    (path / ".gitignore").write_text("node_modules/\n")
    _git(path, "init", "-q")
    _git(path, "add", ".")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "b")
    return _git(path, "rev-parse", "HEAD")


class Manager:
    def __init__(self, mirrors: dict[str, Path]) -> None:
        self.mirrors = mirrors
        self.mirror_error: WorktreeError | None = None

    def verify_mirror(self, repository: str) -> Path:
        if self.mirror_error is not None:
            raise self.mirror_error
        return self.mirrors[repository]

    def change_evidence(self, path: Path, *, base_sha: str) -> tuple[dict[str, Any], str]:
        return {"changed_file_count": 1, "changed_paths": ["apps/x.mjs"]}, "+fixed\n"

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


class SandboxRunner:
    """bwrap double: prints this run's report block for the failures it is given."""

    def __init__(self, failures: list[str], *, exit_code: int = 1) -> None:
        self.failures = [_fail(name, file=f"/work/{LIMITS}") for name in failures]
        self.exit_code = exit_code
        self.calls: list[dict[str, Any]] = []
        self.raises: BaseException | None = None
        self.write_report = True

    def run(self, executable: str, arguments: Any, *, cwd: Path, **kwargs: Any) -> CommandResult:
        arguments = list(arguments)
        checkout = Path(
            arguments[arguments.index("--overlay-src") + 1]
            if "--overlay-src" in arguments
            else arguments[arguments.index("/work") - 1]
        )
        self.calls.append(
            {"executable": executable, "arguments": arguments, "checkout": checkout, **kwargs}
        )
        if self.raises is not None:
            raise self.raises
        nonce = arguments[arguments.index("HERMES_NODE_TEST_NONCE") + 1]
        stdout = "not ok 1 - ghp_" + "A" * 36 + "\n"
        if self.write_report:
            stdout += _block(*self.failures, nonce=nonce).replace(NONCE, nonce)
        return CommandResult(self.exit_code, stdout, "")


def _settings(tmp_path: Path, node_root: Path | None = None) -> Settings:
    node_root = node_root or tmp_path / "node-22"
    if not (node_root / "bin" / "node").exists():
        (node_root / "bin").mkdir(parents=True)
        (node_root / "bin" / "node").write_text("#!/bin/sh\n")
    return Settings(
        worktree_root=tmp_path / "worktrees",
        evidence_root=tmp_path / "evidence",
        project_config_root=PROJECTS,
        toolchain_cache_root=tmp_path / "cache" / "toolchain",
        toolchain_node_roots={"22": node_root},
    )


def _setup(
    tmp_path: Path,
    runner: Any,
    *,
    reported: str = REPORTED_978,
    settings: Settings | None = None,
    base_failing: list[str] = FAILING_098,
    prelude: str = "",
) -> tuple[ScanActivityHandler, dict[str, Any]]:
    base_sha = _repository(tmp_path / "mirrors" / "falcone", base_failing, prelude)
    charts_sha = _repository(tmp_path / "mirrors" / "falcone-charts", [])
    source = tmp_path / "worktrees" / "falcone" / str(uuid4())
    deployment = tmp_path / "worktrees" / "falcone-charts" / str(uuid4())
    source.parent.mkdir(parents=True)
    deployment.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", str(tmp_path / "mirrors" / "falcone"), str(source))
    _git(tmp_path, "clone", "-q", str(tmp_path / "mirrors" / "falcone-charts"), str(deployment))
    handler = ScanActivityHandler(
        settings or _settings(tmp_path),
        Manager(
            {
                "falcone": tmp_path / "mirrors" / "falcone",
                "falcone-charts": tmp_path / "mirrors" / "falcone-charts",
            }
        ),  # type: ignore[arg-type]
        scanner=Scanner(),  # type: ignore[arg-type]
        publisher=Publisher(),  # type: ignore[arg-type]
        baseline_runner=runner,
    )
    payload = {
        "project": "falcone",
        "change_set_id": str(uuid4()),
        "source_worktree": str(source),
        "deployment_worktree": str(deployment),
        "source_repository": "falcone",
        "deployment_repository": "falcone-charts",
        "source_base_sha": base_sha,
        "deployment_base_sha": charts_sha,
        "agent_result": {"structured_result": {"decision": "PASS"}},
        "source_maker_result": {
            "structured_result": {
                "tests": [
                    {"command": "corepack pnpm test", "status": "PASS"},
                    {"command": reported, "status": "FAIL", "summary": "9 of 16 failed"},
                ]
            }
        },
        "deployment_maker_result": {"structured_result": {"tests": []}},
    }
    return handler, payload


def _baseline_root(tmp_path: Path, sha: str, *parts: str) -> Path:
    return tmp_path / "cache" / "toolchain" / "baselines" / "falcone" / Path(*parts) / sha


def test_falcone_978_preexisting_node_suite_failure_passes_with_evidence(tmp_path: Path) -> None:
    runner = SandboxRunner(FAILING_098)
    handler, payload = _setup(tmp_path, runner)

    validated = handler._validate_changeset(payload)

    assert validated["status"] == "LOCAL_VALIDATION_PASS"
    assert validated["blocker"] is None
    [evidence] = validated["preexisting_test_failures"]
    # shlex.join, as for NEW-49: the globs are shown quoted (Python expanded them).
    assert evidence["command"] == (
        "cd . && node --test --experimental-test-isolation=none "
        "'tests/integration/097-plan-entity-tenant-assignment/*.test.mjs' "
        "'tests/integration/098-plan-base-limits/*.test.mjs'"
    )
    assert evidence["kind"] == "node_test"
    assert evidence["origin"] == "source"
    assert evidence["base_sha"] == payload["source_base_sha"]
    assert evidence["base_exit_code"] == 1
    assert evidence["base_failing_count"] == 9
    assert evidence["base_failing_tests"][0] == f"{LIMITS}#0: plan base limit case 0"
    assert evidence["base_test_files"] == [
        "tests/integration/097-plan-entity-tenant-assignment/a.test.mjs",
        LIMITS,
    ]
    assert evidence["base_other_failure_count"] == 0
    assert evidence["maker_reported"] == {
        "command": REPORTED_978,
        "status": "FAIL",
        "summary": "9 of 16 failed",
    }
    assert evidence["head_reexecuted"] is False
    assert "not re-executed" in evidence["note"] and "regression" in evidence["note"]
    assert "ghp_" not in evidence["base_output_tail"]
    assert NONCE not in evidence["base_output_tail"]
    [call] = runner.calls  # the base only: maker-authored head code never runs
    assert call["executable"] == "bwrap"
    assert 900 <= call["timeout_seconds"] <= 1200
    assert call["check"] is False
    assert call["max_output_bytes"] == 4 * 1024 * 1024
    assert _git(call["checkout"], "rev-parse", "HEAD") == payload["source_base_sha"]
    assert call["checkout"] == _baseline_root(tmp_path, payload["source_base_sha"]) / "worktree"
    assert payload["source_worktree"] not in " ".join(call["arguments"])


@pytest.mark.parametrize(
    ("failures", "exit_code"),
    [
        ([], 0),  # passes at the base
        ([], 1),  # failed at the base without a reported failure
    ],
)
def test_suite_without_base_test_failures_still_blocks(
    tmp_path: Path, failures: list[str], exit_code: int
) -> None:
    runner = SandboxRunner(failures, exit_code=exit_code)
    handler, payload = _setup(tmp_path, runner)

    assert handler._validate_changeset(payload)["blocker"] == "MAKER_TEST_FAILURE"


def test_file_level_only_base_failures_are_not_eligible(tmp_path: Path) -> None:
    runner = SandboxRunner([])
    runner.failures = [
        _fail(LIMITS, file=None, failure=None, cause="undefined"),  # missing dependency
        _fail(LIMITS, file=f"/work/{LIMITS}", cause="string"),  # process exit
        _fail("setup", file=f"/work/{LIMITS}", failure="hookFailed"),
    ]
    handler, payload = _setup(tmp_path, runner)

    assert handler._validate_changeset(payload)["blocker"] == "MAKER_TEST_FAILURE"


def test_unparseable_report_still_blocks(tmp_path: Path) -> None:
    runner = SandboxRunner(FAILING_098)
    runner.write_report = False  # no block for this run's nonce
    handler, payload = _setup(tmp_path, runner)

    assert handler._validate_changeset(payload)["blocker"] == "MAKER_TEST_FAILURE"


def test_ineligible_commands_never_run_anything(tmp_path: Path) -> None:
    for reported in ["corepack pnpm test", "node --test tests/a.mjs; id", "node --test src/x.mjs"]:
        runner = SandboxRunner(FAILING_098)
        handler, payload = _setup(tmp_path / str(uuid4()), runner, reported=reported)

        validated = handler._validate_changeset(payload)

        assert validated["blocker"] == "MAKER_TEST_FAILURE"
        assert "baseline_error" not in validated
        assert runner.calls == []


def test_paths_missing_at_base_are_not_preexisting(tmp_path: Path) -> None:
    runner = SandboxRunner(FAILING_098)
    handler, payload = _setup(
        tmp_path, runner, reported="node --test tests/integration/103-new/*.mjs"
    )

    assert handler._validate_changeset(payload)["blocker"] == "MAKER_TEST_FAILURE"
    assert runner.calls == []


def test_at_most_three_node_commands(tmp_path: Path) -> None:
    runner = SandboxRunner(FAILING_098)
    handler, payload = _setup(tmp_path, runner)
    payload["source_maker_result"]["structured_result"]["tests"] = [
        {"command": f"node --test tests/integration/{index}/*.mjs", "status": "FAIL"}
        for index in range(4)
    ]

    assert handler._validate_changeset(payload)["blocker"] == "MAKER_TEST_FAILURE"
    assert runner.calls == []


def test_deployment_maker_failures_run_against_the_deployment_base(tmp_path: Path) -> None:
    runner = SandboxRunner(["chart render"])
    handler, payload = _setup(tmp_path, runner)
    payload["source_maker_result"] = {"structured_result": {"tests": []}}
    payload["deployment_maker_result"] = {
        "structured_result": {"tests": [{"command": REPORTED_978, "status": "FAIL"}]}
    }

    validated = handler._validate_changeset(payload)

    assert validated["status"] == "LOCAL_VALIDATION_PASS"
    [evidence] = validated["preexisting_test_failures"]
    assert evidence["origin"] == "deployment"
    assert evidence["base_sha"] == payload["deployment_base_sha"]
    [call] = runner.calls
    checkout = _baseline_root(tmp_path, payload["deployment_base_sha"], "deployment") / "worktree"
    assert call["checkout"] == checkout
    assert _git(checkout, "rev-parse", "HEAD") == payload["deployment_base_sha"]
    assert (checkout.parent / "prepared").read_text().endswith("unprepared\n")


def test_same_command_from_both_makers_runs_against_each_base(tmp_path: Path) -> None:
    runner = SandboxRunner(FAILING_098)
    handler, payload = _setup(tmp_path, runner)
    payload["deployment_maker_result"] = {
        "structured_result": {"tests": [{"command": REPORTED_978, "status": "FAIL"}]}
    }

    validated = handler._validate_changeset(payload)

    assert [item["origin"] for item in validated["preexisting_test_failures"]] == [
        "source",
        "deployment",
    ]
    assert [call["checkout"].parent.name for call in runner.calls] == [
        payload["source_base_sha"],
        payload["deployment_base_sha"],
    ]


def test_deployment_failure_without_a_deployment_checkout_is_not_eligible(
    tmp_path: Path,
) -> None:
    runner = SandboxRunner(FAILING_098)
    handler, payload = _setup(tmp_path, runner)
    payload["deployment_maker_result"] = {
        "structured_result": {"tests": [{"command": REPORTED_978, "status": "FAIL"}]}
    }
    payload["deployment_base_sha"] = "not-a-sha"

    assert handler._validate_changeset(payload)["blocker"] == "MAKER_TEST_FAILURE"
    assert runner.calls == []


def test_base_result_is_cached(tmp_path: Path) -> None:
    runner = SandboxRunner(FAILING_098)
    handler, payload = _setup(tmp_path, runner)

    assert handler._validate_changeset(payload)["status"] == "LOCAL_VALIDATION_PASS"
    assert handler._validate_changeset({**payload})["status"] == "LOCAL_VALIDATION_PASS"

    assert len(runner.calls) == 1
    root = _baseline_root(tmp_path, payload["source_base_sha"])
    cached = json.loads((root / "results.json").read_text())
    [(key, value)] = cached.items()
    assert json.loads(key) == {
        "argv": [
            "node",
            "--test",
            "--experimental-test-isolation=none",
            "tests/integration/097-plan-entity-tenant-assignment/*.test.mjs",
            "tests/integration/098-plan-base-limits/*.test.mjs",
        ],
        "cwd": ".",
        "kind": "node_test",
        "prepare": None,
        "reporter": 3,
    }
    assert value["exit_code"] == 1 and len(value["test_failures"]) == 9
    assert (root / "prepared").read_text() == f"{payload['source_base_sha']} unprepared\n"
    # A torn cache entry is recomputed, never trusted.
    cached[key]["test_failures"] = ["not", "a", "mapping"]
    (root / "results.json").write_text(json.dumps(cached))
    assert handler._validate_changeset({**payload})["status"] == "LOCAL_VALIDATION_PASS"
    assert len(runner.calls) == 2


def test_cache_is_keyed_by_the_carried_node_selection(tmp_path: Path) -> None:
    node_24 = tmp_path / "node-24"
    (node_24 / "bin").mkdir(parents=True)
    (node_24 / "bin" / "node").write_text("#!/bin/sh\n")
    (node_24 / "bin" / "node").chmod(0o755)
    settings = _settings(tmp_path)
    settings = settings.model_copy(
        update={"toolchain_node_roots": {"22": tmp_path / "node-22", "24": node_24}}
    )
    runner = SandboxRunner(FAILING_098)
    handler, payload = _setup(tmp_path, runner, settings=settings)

    assert handler._validate_changeset(payload)["status"] == "LOCAL_VALIDATION_PASS"
    carried = {**payload, "toolchain_preparation": {"node_toolchain": {"node": "24", "pnpm": None}}}
    assert handler._validate_changeset(carried)["status"] == "LOCAL_VALIDATION_PASS"

    executed = [call["arguments"][call["arguments"].index("--") + 1] for call in runner.calls]
    assert executed == [
        str((tmp_path / "node-22/bin/node").resolve()),
        str((node_24 / "bin/node").resolve()),
    ]
    assert "npm_config_manage_package_manager_versions" in runner.calls[1]["arguments"]


def test_timeout_and_sandbox_failure_block_with_a_baseline_error(tmp_path: Path) -> None:
    runner = SandboxRunner(FAILING_098)
    runner.raises = subprocess.TimeoutExpired(["bwrap"], 1200)
    handler, payload = _setup(tmp_path, runner)

    validated = handler._validate_changeset(payload)

    assert validated["blocker"] == "MAKER_TEST_FAILURE"
    assert validated["baseline_error"].startswith("TimeoutExpired")
    runner.raises = FileNotFoundError("/usr/bin/bwrap")
    assert handler._validate_changeset(payload)["baseline_error"].startswith("FileNotFoundError")


def test_budget_exhaustion_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import engineering_control.activities.scan as scan

    monkeypatch.setattr(scan, "_NODE_TEST_BUDGET_SECONDS", 600)
    runner = SandboxRunner(FAILING_098)
    handler, payload = _setup(tmp_path, runner)

    validated = handler._validate_changeset(payload)

    assert validated["blocker"] == "MAKER_TEST_FAILURE"
    assert "budget" in validated["baseline_error"]
    assert runner.calls == []


# --- the real sandbox (bubblewrap + node) ------------------------------------------------


def _real_node() -> Path | None:
    found = shutil.which("node")
    return Path(found).resolve() if found else None


def _sandbox_available() -> bool:
    node = _real_node()
    if node is None or not Path("/usr/bin/bwrap").is_file():
        return False
    probe = subprocess.run(  # noqa: S603 - fixed executable, probe only
        [
            "/usr/bin/bwrap",
            "--unshare-all",
            "--ro-bind",
            "/usr",
            "/usr",
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib64",
            "/lib64",
            "--remount-ro",
            "/",
            "/bin/true",
        ],
        check=False,
        capture_output=True,
    )
    return probe.returncode == 0


REAL_SANDBOX = pytest.mark.skipif(not _sandbox_available(), reason="bubblewrap or node missing")


@REAL_SANDBOX
def test_real_sandbox_runs_the_978_shape_at_the_base_only(tmp_path: Path) -> None:
    node = _real_node()
    assert node is not None
    runner_marker = tmp_path / "head-ran"
    handler, payload = _setup(tmp_path, None, settings=_settings(tmp_path, node.parent.parent))
    # Maker-authored head code that would leave a trace if it were ever executed.
    _write_suite(
        Path(payload["source_worktree"]),
        FAILING_098,
        prelude=(
            "import { writeFileSync } from 'node:fs';\n"
            f"writeFileSync({str(runner_marker)!r}, 'x');\n"
        ),
    )

    validated = handler._validate_changeset(payload)

    assert validated["status"] == "LOCAL_VALIDATION_PASS", validated.get("baseline_error")
    [evidence] = validated["preexisting_test_failures"]
    assert evidence["base_failing_count"] == 9
    assert evidence["base_failing_tests"] == [
        f"{LIMITS}#0: plan base limit case {index}" for index in range(9)
    ]
    assert not runner_marker.exists()


@REAL_SANDBOX
def test_real_sandbox_file_level_base_failure_is_not_eligible(tmp_path: Path) -> None:
    node = _real_node()
    assert node is not None
    handler, payload = _setup(
        tmp_path,
        None,
        settings=_settings(tmp_path, node.parent.parent),
        prelude="import 'missing-dependency';\n",
    )

    validated = handler._validate_changeset(payload)

    assert validated["blocker"] == "MAKER_TEST_FAILURE", validated.get("baseline_error")
    assert "baseline_error" not in validated


@REAL_SANDBOX
def test_real_sandbox_tmpfs_is_capped_and_the_checkout_read_only(tmp_path: Path) -> None:
    node = _real_node()
    assert node is not None
    prelude = (
        "import { writeFileSync, mkdtempSync } from 'node:fs';\n"
        "import { tmpdir } from 'node:os';\n"
        "import { join } from 'node:path';\n"
        "test('fills tmp', () => { writeFileSync(join(mkdtempSync(join(tmpdir(), 'x')), 'big'),"
        " Buffer.alloc(300 * 1024 * 1024)); });\n"
        "test('writes checkout', () => { writeFileSync('written.txt', 'x'); });\n"
    )
    handler, payload = _setup(
        tmp_path, None, settings=_settings(tmp_path, node.parent.parent), prelude=prelude
    )

    validated = handler._validate_changeset(payload)

    # Review 2: failures the sandbox itself causes (ENOSPC on the capped /tmp, EROFS on the
    # read-only checkout) are never pre-existing evidence; only genuine failures count.
    [evidence] = validated["preexisting_test_failures"]
    assert f"{LIMITS}#0: fills tmp" not in evidence["base_failing_tests"]
    assert f"{LIMITS}#0: writes checkout" not in evidence["base_failing_tests"]
    assert evidence["base_failing_tests"]
    checkout = _baseline_root(tmp_path, payload["source_base_sha"]) / "worktree"
    assert not (checkout / "written.txt").exists()
    assert not os.listdir(checkout / "tests" / "integration" / "098-plan-base-limits")[1:]


@pytest.mark.parametrize("code", ["EROFS", "ENOSPC", "EACCES", "EPERM", "EDQUOT", "ENOTFOUND"])
def test_review_2_sandbox_error_codes_are_never_test_level(code: str) -> None:
    text = _block(_fail("writes", codes=(code,)), _fail("real"))
    report = parse_reporter_output(text, NONCE, ["tests/a.test.mjs"], ".")
    assert report is not None
    assert list(report.test_failures) == ["tests/a.test.mjs#0: real"]
    assert "tests/a.test.mjs#0: writes" in report.other_failures


def test_review_2_records_without_error_codes_are_malformed() -> None:
    record = {
        "name": "x",
        "nesting": 0,
        "file": "/work/tests/a.test.mjs",
        "type": "test",
        "failure": "testCodeFailure",
        "error_cause": "error",
    }
    text = _block(f"{NONCE} FAIL {json.dumps(record)}")
    assert parse_reporter_output(text, NONCE, ["tests/a.test.mjs"], ".") is None


@REAL_SANDBOX
def test_review_3_base_code_that_swallows_write_errors_passes(tmp_path: Path) -> None:
    # Trusted base code that catches its own write failure and only asserts the result:
    # with a read-only checkout it failed as a plain assertion (no error code) and was
    # counted as pre-existing. The copy-on-write layer lets it pass, as on a real machine.
    node = _real_node()
    assert node is not None
    prelude = (
        "import assert from 'node:assert';\n"
        "import { writeFileSync } from 'node:fs';\n"
        "function save() { try { writeFileSync('state.json', '{}'); return true; }"
        " catch { return false; } }\n"
        "test('saves state', () => { assert.equal(save(), true); });\n"
    )
    handler, payload = _setup(
        tmp_path, None, settings=_settings(tmp_path, node.parent.parent), prelude=prelude
    )

    validated = handler._validate_changeset(payload)

    for evidence in validated.get("preexisting_test_failures") or []:
        assert f"{LIMITS}#0: saves state" not in evidence["base_failing_tests"]
    checkout = _baseline_root(tmp_path, payload["source_base_sha"]) / "worktree"
    assert not (checkout / "state.json").exists()  # the layer was discarded
    overlay = handler.settings.toolchain_cache_root / "node-test-overlay"
    assert not list(overlay.iterdir())  # and removed
