"""NEW-161: a maker FAIL in a non-contract `node --test` suite that already fails at base."""

from __future__ import annotations

import json
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
    regressions,
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


# --- glob expansion, reporter output, subset rule, sandbox ---------------------------


def test_paths_are_expanded_by_python_inside_the_checkout(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "tests" / "098").mkdir(parents=True)
    for name in ("b.test.mjs", "a.test.mjs", ".hidden.test.mjs", "helper.mjs"):
        (checkout / "tests" / "098" / name).write_text("")
    outside = tmp_path / "outside.test.mjs"
    outside.write_text("")
    (checkout / "tests" / "098" / "z.test.mjs").symlink_to(outside)
    command = NodeTestCommand(".", (), ("tests/098/*.test.mjs", "tests/098/a.test.mjs"))

    assert expand_test_paths(checkout, command) == ["tests/098/a.test.mjs", "tests/098/b.test.mjs"]
    assert expand_test_paths(checkout, NodeTestCommand(".", (), ("tests/097/*.mjs",))) is None
    assert expand_test_paths(checkout, NodeTestCommand(".", (), ("tests/098/z.test.mjs",))) is None
    assert expand_test_paths(checkout, NodeTestCommand("missing", (), ("tests/*",))) is None


def _line(event: str, name: str, *, nesting: int = 0, file: str | None = None) -> str:
    return json.dumps({"hermes": 1, "event": event, "name": name, "nesting": nesting, "file": file})


def test_reporter_output_identifies_tests_by_file_nesting_and_name() -> None:
    text = "\n".join(
        [
            _line("fail", "dup", nesting=1, file="/work/tests/a.test.mjs"),
            _line("pass", "ok", nesting=1, file="/work/tests/a.test.mjs"),
            _line("fail", "suite", file="/work/tests/a.test.mjs"),
            _line("fail", "dup", file="/work/tests/b.test.mjs"),
            _line("fail", "tests/c.test.mjs"),
            "",
        ]
    )

    report = parse_reporter_output(text)

    assert report is not None
    assert report.passed == 1
    assert report.failing == Counter(
        {
            "tests/a.test.mjs#1: dup": 1,
            "tests/a.test.mjs#0: suite": 1,
            "tests/b.test.mjs#0: dup": 1,
            "#0: tests/c.test.mjs": 1,
        }
    )
    assert parse_reporter_output("") is not None
    for malformed in ["not json", "[]", json.dumps({"event": "fail"}), _line("skip", "x")]:
        assert parse_reporter_output(malformed) is None


def test_subset_rule_separates_preexisting_regression_and_mixed() -> None:
    base = Counter({"a#0: x": 1, "a#0: y": 1})

    assert regressions(base, Counter({"a#0: x": 1, "a#0: y": 1})) == []  # identical
    assert regressions(base, Counter({"a#0: x": 1})) == []  # the change fixed one
    assert regressions(base, Counter()) == []
    assert regressions(base, Counter({"a#0: z": 1})) == ["a#0: z"]  # regression
    assert regressions(base, Counter({"a#0: x": 1, "b#0: x": 1})) == ["b#0: x"]  # mixed
    assert regressions(base, Counter({"a#0: x": 2})) == ["a#0: x"]  # a duplicate is new


def test_evidence_names_are_bounded() -> None:
    names = bounded_names(Counter({f"t#0: {'x' * 500} {index:03}": 1 for index in range(80)}))

    assert len(names) == EVIDENCE_TEST_NAMES
    assert all(len(name) == 200 for name in names)


def test_sandbox_has_no_network_and_a_discarded_checkout_overlay(tmp_path: Path) -> None:
    node = tmp_path / "node-22" / "bin" / "node"
    arguments = sandbox_argv(
        checkout=tmp_path / "checkout",
        command=NodeTestCommand(
            "apps/x", ("--experimental-test-isolation=none",), ("tests/*.mjs",)
        ),
        files=["tests/a.mjs"],
        node=node,
        reporter=tmp_path / "reporter.mjs",
        output=tmp_path / "out",
        variables={"npm_config_x": "false"},
    )

    assert arguments[:3] == ["--unshare-all", "--die-with-parent", "--new-session"]
    assert "--share-net" not in arguments
    index = arguments.index("--overlay-src")
    assert arguments[index : index + 4] == [
        "--overlay-src",
        str(tmp_path / "checkout"),
        "--tmp-overlay",
        "/work",
    ]
    assert arguments[arguments.index("--chdir") + 1] == "/work/apps/x"
    assert ["--ro-bind", str(tmp_path / "node-22"), str(tmp_path / "node-22")] == arguments[
        arguments.index(str(tmp_path / "node-22")) - 1 : arguments.index(str(tmp_path / "node-22"))
        + 2
    ]
    assert "--clearenv" in arguments
    assert arguments[arguments.index("--") :] == [
        "--",
        str(node),
        "--test",
        "--experimental-test-isolation=none",
        "--test-reporter=/run/hermes-node-test/reporter.mjs",
        "--test-reporter-destination=/run/hermes-node-test/out/report.jsonl",
        "tests/a.mjs",
    ]
    with pytest.raises(ValueError, match="plain absolute path"):
        sandbox_argv(
            checkout=Path("relative"),
            command=NodeTestCommand(".", (), ("tests/a.mjs",)),
            files=["tests/a.mjs"],
            node=node,
            reporter=tmp_path / "r.mjs",
            output=tmp_path / "out",
            variables={},
        )


# --- delivery.validate_changeset -------------------------------------------------------


def _write_suite(repo: Path, failing: list[str], *, passing: int = 7) -> None:
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
    (repo / "tests" / "integration" / "098-plan-base-limits" / "limits.test.mjs").write_text(
        "import { test } from 'node:test';\n" + "\n".join(cases) + "\n"
    )


def _mirror(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "mirrors" / "falcone"
    repo.mkdir(parents=True)
    _write_suite(repo, FAILING_098)
    (repo / ".gitignore").write_text("node_modules/\n")
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
        assert repository == "falcone"
        return self.mirror

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
    """bwrap double: reports the failing tests of whichever checkout it was given."""

    def __init__(self, base: list[str], head: list[str], *, base_exit: int = 1) -> None:
        self.failing = {"base": base, "head": head}
        self.base_exit = base_exit
        self.calls: list[dict[str, Any]] = []
        self.raises: BaseException | None = None
        self.write_report = True

    def run(self, executable: str, arguments: Any, *, cwd: Path, **kwargs: Any) -> CommandResult:
        arguments = list(arguments)
        checkout = Path(arguments[arguments.index("--overlay-src") + 1])
        side = "head" if "worktrees" in checkout.parts else "base"
        self.calls.append(
            {"executable": executable, "arguments": arguments, "side": side, **kwargs}
        )
        if self.raises is not None:
            raise self.raises
        output = Path(arguments[arguments.index("/run/hermes-node-test/out") - 1])
        failing = self.failing[side]
        if self.write_report:
            lines = [
                _line(
                    "fail",
                    name,
                    file="/work/tests/integration/098-plan-base-limits/limits.test.mjs",
                )
                for name in failing
            ] + [_line("pass", "passes")]
            (output / "report.jsonl").write_text("\n".join(lines) + "\n")
        code = self.base_exit if side == "base" else (1 if failing else 0)
        return CommandResult(code, "not ok 1 - ghp_" + "A" * 36, "")


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
    tmp_path: Path, runner: Any, *, reported: str = REPORTED_978, settings: Settings | None = None
) -> tuple[ScanActivityHandler, dict[str, Any]]:
    mirror, base_sha = _mirror(tmp_path)
    source = tmp_path / "worktrees" / "falcone" / str(uuid4())
    source.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", str(mirror), str(source))
    handler = ScanActivityHandler(
        settings or _settings(tmp_path),
        Manager(mirror),  # type: ignore[arg-type]
        scanner=Scanner(),  # type: ignore[arg-type]
        publisher=Publisher(),  # type: ignore[arg-type]
        baseline_runner=runner,
    )
    payload = {
        "project": "falcone",
        "change_set_id": str(uuid4()),
        "source_worktree": str(source),
        "deployment_worktree": str(source),
        "source_repository": "falcone",
        "deployment_repository": "falcone-charts",
        "source_base_sha": base_sha,
        "deployment_base_sha": "b" * 40,
        "agent_result": {"structured_result": {"decision": "PASS"}},
        "source_maker_result": {
            "structured_result": {
                "tests": [
                    {"command": "corepack pnpm test", "status": "PASS"},
                    {"command": reported, "status": "FAIL"},
                ]
            }
        },
        "deployment_maker_result": {"structured_result": {"tests": []}},
    }
    return handler, payload


def test_falcone_978_preexisting_node_suite_failure_passes_with_evidence(tmp_path: Path) -> None:
    runner = SandboxRunner(base=FAILING_098, head=FAILING_098)
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
    assert evidence["base_sha"] == payload["source_base_sha"]
    assert evidence["base_exit_code"] == 1
    assert evidence["head_exit_code"] == 1
    assert evidence["base_failing_count"] == evidence["head_failing_count"] == 9
    assert evidence["head_failing_tests"] == evidence["base_failing_tests"]
    assert evidence["base_failing_tests"][0] == (
        "tests/integration/098-plan-base-limits/limits.test.mjs#0: plan base limit case 0"
    )
    assert evidence["base_test_files"] == evidence["head_test_files"] == 2
    assert "ghp_" not in evidence["base_output_tail"] + evidence["head_output_tail"]
    assert [call["side"] for call in runner.calls] == ["base", "head"]
    for call in runner.calls:
        assert call["executable"] == "bwrap"
        assert 900 <= call["timeout_seconds"] <= 1200
        assert call["check"] is False
        files = call["arguments"][call["arguments"].index("--") + 1 :]
        assert files[-2:] == [
            "tests/integration/097-plan-entity-tenant-assignment/a.test.mjs",
            "tests/integration/098-plan-base-limits/limits.test.mjs",
        ]
    checkout = Path(
        runner.calls[0]["arguments"][runner.calls[0]["arguments"].index("--overlay-src") + 1]
    )
    assert _git(checkout, "rev-parse", "HEAD") == payload["source_base_sha"]
    assert (
        Path(runner.calls[1]["arguments"][runner.calls[1]["arguments"].index("--overlay-src") + 1])
        == Path(payload["source_worktree"]).resolve()
    )


def test_head_failure_that_passes_at_base_still_blocks(tmp_path: Path) -> None:
    runner = SandboxRunner(base=FAILING_098, head=[*FAILING_098, "tenant isolation"])
    handler, payload = _setup(tmp_path, runner)

    validated = handler._validate_changeset(payload)

    assert validated["status"] == "BLOCKED"
    assert validated["blocker"] == "MAKER_TEST_FAILURE"
    assert "preexisting_test_failures" not in validated


def test_head_fixing_some_preexisting_failures_is_still_preexisting(tmp_path: Path) -> None:
    runner = SandboxRunner(base=FAILING_098, head=FAILING_098[:3])
    handler, payload = _setup(tmp_path, runner)

    validated = handler._validate_changeset(payload)

    assert validated["status"] == "LOCAL_VALIDATION_PASS"
    assert validated["preexisting_test_failures"][0]["head_failing_count"] == 3


def test_suite_that_passes_at_base_still_blocks(tmp_path: Path) -> None:
    runner = SandboxRunner(base=[], head=["plan base limit case 0"], base_exit=0)
    handler, payload = _setup(tmp_path, runner)

    assert handler._validate_changeset(payload)["blocker"] == "MAKER_TEST_FAILURE"


@pytest.mark.parametrize("base_exit", [1, 0])
def test_unparseable_or_testless_failures_still_block(tmp_path: Path, base_exit: int) -> None:
    runner = SandboxRunner(base=FAILING_098, head=FAILING_098, base_exit=base_exit)
    runner.write_report = False  # no report: a crash, or a reporter the test code broke
    handler, payload = _setup(tmp_path, runner)

    assert handler._validate_changeset(payload)["blocker"] == "MAKER_TEST_FAILURE"


def test_ineligible_commands_never_run_anything(tmp_path: Path) -> None:
    for reported in ["corepack pnpm test", "node --test tests/a.mjs; id", "node --test src/x.mjs"]:
        runner = SandboxRunner(base=FAILING_098, head=FAILING_098)
        handler, payload = _setup(tmp_path / str(uuid4()), runner, reported=reported)

        validated = handler._validate_changeset(payload)

        assert validated["blocker"] == "MAKER_TEST_FAILURE"
        assert "baseline_error" not in validated
        assert runner.calls == []


def test_paths_missing_at_base_are_not_preexisting(tmp_path: Path) -> None:
    runner = SandboxRunner(base=FAILING_098, head=FAILING_098)
    handler, payload = _setup(
        tmp_path, runner, reported="node --test tests/integration/103-new/*.mjs"
    )

    assert handler._validate_changeset(payload)["blocker"] == "MAKER_TEST_FAILURE"
    assert runner.calls == []  # neither checkout has the suite: nothing was run


def test_at_most_three_node_commands(tmp_path: Path) -> None:
    runner = SandboxRunner(base=FAILING_098, head=FAILING_098)
    handler, payload = _setup(tmp_path, runner)
    payload["source_maker_result"]["structured_result"]["tests"] = [
        {"command": f"node --test tests/integration/{index}/*.mjs", "status": "FAIL"}
        for index in range(4)
    ]

    assert handler._validate_changeset(payload)["blocker"] == "MAKER_TEST_FAILURE"
    assert runner.calls == []


def test_base_result_is_cached_and_head_always_reruns(tmp_path: Path) -> None:
    runner = SandboxRunner(base=FAILING_098, head=FAILING_098)
    handler, payload = _setup(tmp_path, runner)

    assert handler._validate_changeset(payload)["status"] == "LOCAL_VALIDATION_PASS"
    assert handler._validate_changeset({**payload})["status"] == "LOCAL_VALIDATION_PASS"

    assert [call["side"] for call in runner.calls] == ["base", "head", "head"]
    root = tmp_path / "cache" / "toolchain" / "baselines" / "falcone" / payload["source_base_sha"]
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
        "reporter": 1,
    }
    assert value["exit_code"] == 1 and len(value["failing_tests"]) == 9
    assert (root / "prepared").read_text() == f"{payload['source_base_sha']} unprepared\n"
    # A torn cache entry is recomputed, never trusted.
    cached[key]["failing_tests"] = ["not", "a", "mapping"]
    (root / "results.json").write_text(json.dumps(cached))
    assert handler._validate_changeset({**payload})["status"] == "LOCAL_VALIDATION_PASS"
    assert [call["side"] for call in runner.calls][-2:] == ["base", "head"]


def test_cache_is_keyed_by_the_carried_node_selection(tmp_path: Path) -> None:
    node_24 = tmp_path / "node-24"
    (node_24 / "bin").mkdir(parents=True)
    (node_24 / "bin" / "node").write_text("#!/bin/sh\n")
    (node_24 / "bin" / "node").chmod(0o755)
    settings = _settings(tmp_path)
    settings = settings.model_copy(
        update={"toolchain_node_roots": {"22": tmp_path / "node-22", "24": node_24}}
    )
    runner = SandboxRunner(base=FAILING_098, head=FAILING_098)
    handler, payload = _setup(tmp_path, runner, settings=settings)

    assert handler._validate_changeset(payload)["status"] == "LOCAL_VALIDATION_PASS"
    carried = {**payload, "toolchain_preparation": {"node_toolchain": {"node": "24", "pnpm": None}}}
    assert handler._validate_changeset(carried)["status"] == "LOCAL_VALIDATION_PASS"

    assert [call["side"] for call in runner.calls] == ["base", "head", "base", "head"]
    executed = [call["arguments"][call["arguments"].index("--") + 1] for call in runner.calls]
    assert (
        executed
        == [str((tmp_path / "node-22/bin/node").resolve())] * 2
        + [str((node_24 / "bin/node").resolve())] * 2
    )
    assert any(
        "npm_config_manage_package_manager_versions" in call["arguments"]
        for call in runner.calls[2:]
    )


def test_timeout_and_sandbox_failure_block_with_a_baseline_error(tmp_path: Path) -> None:
    runner = SandboxRunner(base=FAILING_098, head=FAILING_098)
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
    runner = SandboxRunner(base=FAILING_098, head=FAILING_098)
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
            "--tmpfs",
            "/tmp",  # noqa: S108 - inside the probe sandbox
            "--overlay-src",
            "/usr",
            "--tmp-overlay",
            "/mnt",
            "/bin/true",
        ],
        check=False,
        capture_output=True,
    )
    return probe.returncode == 0


@pytest.mark.skipif(not _sandbox_available(), reason="bubblewrap with overlays or node missing")
@pytest.mark.parametrize("regression", [False, True])
def test_real_sandbox_reruns_the_978_shape(tmp_path: Path, regression: bool) -> None:
    node = _real_node()
    assert node is not None
    settings = _settings(tmp_path, node.parent.parent)
    handler, payload = _setup(tmp_path, None, settings=settings)
    source = Path(payload["source_worktree"])
    head_failing = [*FAILING_098, "tenant isolation"] if regression else FAILING_098[:4]
    _write_suite(source, head_failing)
    # The head suite also writes into the checkout: the overlay discards it.
    limits = source / "tests" / "integration" / "098-plan-base-limits" / "limits.test.mjs"
    limits.write_text(
        limits.read_text()
        + "import { writeFileSync } from 'node:fs';\n"
        + "test('writes', () => { writeFileSync('written-by-test.txt', 'x'); });\n"
    )
    before = _git(source, "status", "--porcelain=v1", "--untracked-files=all")

    validated = handler._validate_changeset(payload)

    assert _git(source, "status", "--porcelain=v1", "--untracked-files=all") == before
    assert not (source / "written-by-test.txt").exists()
    if regression:
        assert validated["blocker"] == "MAKER_TEST_FAILURE", validated.get("baseline_error")
        return
    assert validated["status"] == "LOCAL_VALIDATION_PASS", validated.get("baseline_error")
    [evidence] = validated["preexisting_test_failures"]
    assert evidence["base_failing_count"] == 9
    assert evidence["head_failing_count"] == 4
    assert evidence["head_failing_tests"] == [
        f"tests/integration/098-plan-base-limits/limits.test.mjs#0: plan base limit case {index}"
        for index in range(4)
    ]
