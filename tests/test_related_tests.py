"""Run the related tests: the tests that cover what was changed are found on the map of
the code and run by the repository's own runner."""

# The files of a small repository are written out here, lines as long as they are.
# ruff: noqa: E501, S603, S607

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from engineering_control.engine import validate
from engineering_control.engine.builtin import STARTERS
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.document import parse
from engineering_control.sandbox import deliver
from tests.test_model_edits import (  # noqa: F401 - ``stage`` is a fixture
    EDITS,
    _answered,
    _applied,
    _editing,
    _to_the_model,
    stage,
)
from tests.test_models_api import said
from tests.test_sandbox_delivery import world  # noqa: F401 - a fixture
from tests.test_workbench import _found, _load, _read, _steps, _tool_log

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
FILES = {
    ".gitignore": ".venv/\nnode_modules/\n",
    # A Python service, tested with pytest.
    "svc/pyproject.toml": '[project]\nname = "svc"\nversion = "0"\n',
    "svc/app/__init__.py": "",
    "svc/app/price.py": "def round_price(cents):\n    return round(cents)\n",
    "svc/app/tax.py": "from app.price import round_price\n\n\ndef with_tax(cents):\n    return round_price(cents * 1.2)\n",
    "svc/app/format.py": "def as_text(cents):\n    return f'{cents / 100:.2f}'\n",
    "svc/tests/test_price.py": "from app.price import round_price\n\n\ndef test_rounds():\n    assert round_price(10.4) == 10\n",
    "svc/tests/test_tax.py": "from app.tax import with_tax\n\n\ndef test_tax():\n    assert with_tax(100) == 120\n",
    # Named after the file it tests, without importing it.
    "svc/tests/test_format.py": "def test_nothing_much():\n    assert True\n",
    "svc/tests/test_slow.py": "import time\n\n\ndef test_takes_long():\n    time.sleep(60)\n",
    # A package tested with Node's own runner.
    "web/package.json": '{"name": "web", "type": "module", "scripts": {"test": "node --test"}}\n',
    "web/src/cart.mjs": "export const total = (items) => items.reduce((sum, item) => sum + item, 0);\n",
    "web/src/cart.test.mjs": "import assert from 'node:assert';\nimport { test } from 'node:test';\nimport { total } from './cart.mjs';\n\ntest('adds', () => assert.equal(total([1, 2]), 3));\n",
    # A package that names vitest.
    "ui/package.json": '{"name": "ui", "devDependencies": {"vitest": "^3"}}\n',
    "ui/src/button.ts": "export const label = (text: string) => text.trim();\n",
    "ui/src/button.test.ts": "import { label } from './button';\n\ntest('trims', () => expect(label(' a ')).toBe('a'));\n",
    # A Go module.
    "go/go.mod": "module example.com/shop\n\ngo 1.22\n",
    "go/pkg/price/price.go": "package price\n\nfunc Round(c int) int { return c }\n",
    "go/pkg/price/price_test.go": 'package price\n\nimport "testing"\n\nfunc TestRound(t *testing.T) {\n\tif Round(2) != 2 {\n\t\tt.Fatal("wrong")\n\t}\n}\n',
    "README.md": "# The shop\n",
}


@pytest.fixture
def shop(tmp_path: Path) -> dict[str, Any]:
    root = tmp_path / "repo"
    for name, text in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run([*GIT, "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run([*GIT, "add", "-A"], cwd=root, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", "first"], cwd=root, check=True)
    base = (
        subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True)
        .stdout.decode()
        .strip()
    )
    # What the project's installation would leave: an environment with pytest in it.
    (root / "svc" / ".venv" / "bin").mkdir(parents=True)
    python = root / "svc" / ".venv" / "bin" / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    tools = _load(
        "tools", {"BENCH_REPO": str(root), "BENCH_INDEX": str(tmp_path / "cache" / "index.json")}
    )
    return {"root": root, "base": base, "tools": tools}


def test_the_tests_that_cover_a_change_are_found_the_surest_first(shop: dict[str, Any]) -> None:
    tools, root = shop["tools"], shop["root"]
    files = tools.index()["files"]
    assert tools.tested_name("a/price.test.ts") == tools.tested_name("b/test_price.py") == "price"
    assert tools.tested_name("pkg/price_test.go") == "price" and tools.tested_name("price.py") == ""

    related = tools.related_tests(["svc/app/price.py"], files, 20)
    assert related == [
        {"path": "svc/tests/test_price.py", "why": "it imports svc/app/price.py"},
        {
            "path": "svc/tests/test_tax.py",
            "why": "it imports svc/app/tax.py, which imports svc/app/price.py",
        },
    ]
    # A test that does not import what it tests is found by its name.
    assert tools.related_tests(["svc/app/format.py"], files, 20) == [
        {"path": "svc/tests/test_format.py", "why": "it is named after svc/app/format.py"}
    ]
    # A Go test is of its package; a test that was changed is run itself.
    assert tools.related_tests(["go/pkg/price/price.go"], files, 20) == [
        {"path": "go/pkg/price/price_test.go", "why": "it is named after go/pkg/price/price.go"}
    ]
    assert tools.related_tests(["web/src/cart.test.mjs", "README.md"], files, 20) == [
        {"path": "web/src/cart.test.mjs", "why": "it was changed"}
    ]
    assert tools.related_tests(["README.md"], files, 20) == []
    assert len(tools.related_tests(["svc/app/price.py"], files, 1)) == 1

    # What a run changed is what differs from where it started, committed or not, and
    # what is new; what is ignored is not.
    assert tools.changed_since(shop["base"]) == []
    (root / "svc/app/price.py").write_text("def round_price(cents):\n    return int(cents)\n")
    (root / "web/src/new.mjs").write_text("export const one = 1;\n")
    subprocess.run([*GIT, "commit", "-q", "-am", "price"], cwd=root, check=True)
    (root / "web/src/cart.mjs").write_text("export const total = () => 0;\n")
    assert tools.changed_since(shop["base"]) == [
        "svc/app/price.py",
        "web/src/cart.mjs",
        "web/src/new.mjs",
    ]


def test_each_kind_of_test_is_run_by_the_repositorys_own_runner(shop: dict[str, Any]) -> None:
    tools, root = shop["tools"], shop["root"]
    python = str(root / "svc" / ".venv" / "bin" / "python")
    assert tools.runner_of("svc/tests/test_price.py") == (
        "pytest",
        "svc",
        [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        "tests/test_price.py",
    )
    assert tools.runner_of("web/src/cart.test.mjs") == (
        "node --test",
        "",
        ["node", "--test"],
        "web/src/cart.test.mjs",
    )
    assert tools.runner_of("go/pkg/price/price_test.go") == (
        "go test",
        "go",
        ["go", "test"],
        "./pkg/price",
    )
    # A runner a package names has to be installed for it.
    assert tools.runner_of("ui/src/button.test.ts") == "vitest is not installed for ui"
    program = root / "ui" / "node_modules" / ".bin" / "vitest"
    program.parent.mkdir(parents=True)
    program.write_text('#!/bin/sh\necho ran "$@" in $(basename $PWD)\n')
    program.chmod(0o755)
    assert tools.runner_of("ui/src/button.test.ts") == (
        "vitest",
        "ui",
        [str(program), "run"],
        "src/button.test.ts",
    )
    # A TypeScript test of a package that names no runner, and a file of no known kind.
    (root / "web/src/typed.test.ts").write_text("export {};\n")
    assert (
        tools.runner_of("web/src/typed.test.ts") == "no package.json above it names vitest or jest"
    )
    assert tools.runner_of("docs/check.test.lua") == "no runner is known for this kind of file"


def test_the_related_tests_run_and_say_what_failed(shop: dict[str, Any]) -> None:
    tools, root = shop["tools"], shop["root"]
    # Nothing changed: nothing to run.
    nothing = tools.tests({"base": shop["base"], "seconds": 60})
    assert nothing == {"changed": [], "tests": [], "ran": [], "unrunnable": [], "passed": None}

    # A change that breaks the price: its own test and the test of what uses it run, and
    # nothing else of the repository's does.
    (root / "svc/app/price.py").write_text("def round_price(cents):\n    return cents + 1\n")
    (root / "web/src/cart.mjs").write_text("export const total = (items) => items.length;\n")
    (root / "ui/src/button.ts").write_text("export const label = (text: string) => text;\n")
    answer = tools.tests({"base": shop["base"], "seconds": 120})
    assert answer["changed"] == ["svc/app/price.py", "ui/src/button.ts", "web/src/cart.mjs"]
    assert [entry["path"] for entry in answer["tests"]] == [
        "svc/tests/test_price.py",
        "ui/src/button.test.ts",
        "web/src/cart.test.mjs",
        "svc/tests/test_tax.py",
    ]
    assert answer["unrunnable"] == [
        {"path": "ui/src/button.test.ts", "why": "vitest is not installed for ui"}
    ]
    by_runner = {run["runner"]: run for run in answer["ran"]}
    assert set(by_runner) == {"pytest", "node --test"} and answer["passed"] is False
    pytest_run = by_runner["pytest"]
    assert pytest_run["folder"] == "svc" and pytest_run["files"] == 2
    assert pytest_run["line"] == (
        "[svc] python -m pytest -q -p no:cacheprovider tests/test_price.py tests/test_tax.py"
    )
    assert pytest_run["exit_code"] == 1 and not pytest_run["timed_out"]
    assert "2 failed" in pytest_run["said"] and "test_slow" not in pytest_run["said"]
    node = by_runner["node --test"]
    assert node["exit_code"] != 0 and "web/src/cart.test.mjs" in node["line"]

    # Put right, they pass; files the task names stand in for what was changed.
    (root / "svc/app/price.py").write_text(FILES["svc/app/price.py"])
    (root / "web/src/cart.mjs").write_text(FILES["web/src/cart.mjs"])
    passing = tools.tests({"paths": ["svc/app/tax.py", "web/src/cart.mjs"], "seconds": 120})
    assert (
        passing["changed"] == ["svc/app/tax.py", "web/src/cart.mjs"] and passing["passed"] is True
    )
    assert {run["runner"]: (run["files"], run["exit_code"]) for run in passing["ran"]} == {
        "pytest": (1, 0),
        "node --test": (1, 0),
    }

    # Tests that take longer than they may are stopped, and said to be.
    slow = tools.tests({"paths": ["svc/tests/test_slow.py"], "seconds": 7})
    (stopped,) = slow["ran"]
    assert stopped["timed_out"] is True and stopped["exit_code"] != 0
    assert 5 <= stopped["seconds"] < 30, "it ran, and was stopped"
    assert slow["passed"] is False


def test_a_flow_can_say_how_the_related_tests_are_run(shop: dict[str, Any]) -> None:
    tools, root = shop["tools"], shop["root"]
    python = str(root / "svc" / ".venv" / "bin" / "python")
    asked = {
        "paths": ["svc/app/price.py", "web/src/cart.mjs"],
        "seconds": 120,
        "commands": [
            # Something to do first, whatever the tests are.
            {"argv": ["sh", "-c", "echo built > built.txt"], "cwd": "."},
            # A command in a folder is given the tests of that folder, as it names them.
            {
                "argv": [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "{files}"],
                "cwd": "svc",
            },
            {"argv": ["sh", "-c", "node --test {files} && echo ran {files}"], "cwd": "web"},
            # No related test is in this folder: the command is left out.
            {"argv": ["false", "{files}"], "cwd": "ui"},
        ],
    }
    answer = tools.tests(asked)
    assert (root / "built.txt").read_text() == "built\n"
    assert answer["passed"] is True and answer["unrunnable"] == []
    assert [(run["folder"], run["files"], run["exit_code"]) for run in answer["ran"]] == [
        ("", 0, 0),
        ("svc", 2, 0),
        ("web", 1, 0),
    ]
    assert (
        answer["ran"][1]["line"] == f"[svc] {python} -m pytest -q -p no:cacheprovider '{{files}}'"
    )
    assert "2 passed" in answer["ran"][1]["said"]
    assert answer["ran"][2]["said"].rstrip().endswith("ran src/cart.test.mjs")
    assert {run["runner"] for run in answer["ran"]} == {"the flow's own command"}

    # The first command that fails ends it: a build that fails leaves nothing to test.
    asked["commands"] = [
        {"argv": ["sh", "-c", "echo no build; exit 3"], "cwd": "."},
        *asked["commands"],
    ]
    failed = tools.tests(asked)
    (only,) = failed["ran"]
    assert only["exit_code"] == 3 and "no build" in only["said"] and failed["passed"] is False
    # With nothing to test, nothing is run, a build included.
    (root / "built.txt").unlink()
    nothing = tools.tests({**asked, "paths": ["README.md"]})
    assert nothing["ran"] == [] and nothing["passed"] is None and not (root / "built.txt").exists()


@pytest.mark.skipif(shutil.which("go") is None, reason="Go is not installed here")
def test_a_go_test_runs_for_its_package(shop: dict[str, Any]) -> None:
    answer = shop["tools"].tests({"paths": ["go/pkg/price/price.go"], "seconds": 120})
    (ran,) = answer["ran"]
    assert (ran["runner"], ran["folder"], ran["line"]) == (
        "go test",
        "go",
        "[go] go test ./pkg/price",
    )
    assert ran["exit_code"] == 0 and answer["passed"] is True


def _checking() -> dict[str, Any]:
    """A model writes the change, the platform applies it, and the related tests run
    before the review."""
    raw = _editing()
    tasks = {task["id"]: task for task in raw["tasks"]}
    tasks["apply"]["next"] = {"applied": "check", "refused": "write"}
    raw["tasks"].append(
        {
            "id": "check",
            "task": "code.tests",
            "with": {"minutes": 4, "limit": 12},
            "next": {"passed": "review", "failed": "write", "none": "review"},
        }
    )
    return raw


def test_the_task_and_where_it_stands_in_a_flow() -> None:
    kind = TASKS["code.tests"]
    assert (kind.where, kind.account, kind.writes) == ("bench", False, False)
    assert kind.outcomes == ("passed", "failed", "none")
    assert [param.name for param in kind.params] == ["paths", "limit", "commands", "minutes"]
    assert validate.errors(parse(_checking())) == []
    # It is not the tests a change has to pass: a flow that has only these is refused.
    raw = _checking()
    tasks = {task["id"]: task for task in raw["tasks"]}
    tasks["review"]["next"] = {"pass": "scan", "changes": "write"}
    raw["tasks"] = [task for task in raw["tasks"] if task["id"] != "verify"]
    assert any("tests" in problem["text"].lower() for problem in validate.errors(parse(raw)))
    # The loop to begin from looks at the related tests when its model wants a check.
    look = STARTERS["starter-model-loop"].node("look")
    assert look is not None and look.task == "code.tests"
    assert look.next == {"passed": "decide", "failed": "decide", "none": "decide"}


@pytest.mark.asyncio
async def test_what_the_related_tests_say_goes_back_to_the_model_that_wrote_the_change(
    stage: Any,  # noqa: F811
) -> None:
    stage.provider.answers = [said(EDITS), said(EDITS)]
    run = await _to_the_model(stage, _checking())
    stage.door.answers = [
        _found("packages/billing/src/money.ts"),
        _read("packages/billing/src/money.ts"),
    ]
    await stage.launcher.tick()
    failed = {
        "changed": ["packages/billing/src/money.ts"],
        "tests": [
            {
                "path": "packages/billing/src/money.test.ts",
                "why": "it imports packages/billing/src/money.ts",
            }
        ],
        "ran": [
            {
                "runner": "vitest",
                "folder": "packages/billing",
                "files": 1,
                "line": "[packages/billing] vitest run src/money.test.ts",
                "exit_code": 1,
                "seconds": 2.4,
                "timed_out": False,
                "said": "FAIL src/money.test.ts > rounds half a cent up\nexpected 3 to be 2",
            }
        ],
        "unrunnable": [],
        "passed": False,
    }
    stage.door.answers = [
        _applied("packages/billing/src/money.ts"),
        _tool_log("tests", failed),
    ]
    await _answered(stage)
    steps = _steps(stage, run["id"])
    check = steps["check"]
    assert (check["state"], check["outcome"]) == ("succeeded", "failed")
    assert check["detail"] == (
        "1 of 1 groups of related tests failed: [packages/billing] vitest run src/money.test.ts"
    )
    # What the tool was asked: what the run changed since it started, within the task's
    # limits, with the caches the project's own commands have.
    key, job = next(pair for pair in stage.door.handed if pair[0] == check["id"])
    import json

    question = json.loads(job["files"][1]["content"])
    assert question == {
        "paths": [],
        "base": stage.run(run["id"])["data"]["base_sha"],
        "limit": 12,
        "seconds": 240,
    }
    command = job["commands"][0]
    assert command["argv"][-3] == "tests" and command["env"] == deliver.TOOLS
    assert command["timeout"] == 300
    # The model is asked again, and is told what failed.
    again = stage.provider.asked[-1][2]["messages"][1]["content"]
    assert "[packages/billing] vitest run src/money.test.ts" in again
    assert "expected 3 to be 2" in again

    # Passing, or with no test to run, the work goes on to the review.
    for answer, outcome, detail in (
        (
            {**failed, "ran": [{**failed["ran"][0], "exit_code": 0}], "passed": True},
            "passed",
            "1 related test files passed.",
        ),
        (
            {**failed, "tests": [], "ran": [], "passed": None},
            "no_tests",
            "No test covers the 1 files that were changed.",
        ),
    ):
        stage.door.answers = [
            _applied("packages/billing/src/money.ts"),
            _tool_log("tests", answer),
        ]
        await _answered(stage)
        latest = [s for s in stage.run(run["id"])["steps"] if s["name"] == "check"][-1]
        assert (latest["outcome"], latest["detail"]) == (outcome, detail)
        following = stage.run(run["id"])["steps"][-1]
        assert following["name"] == "review"
        if outcome == "passed":
            break
    await stage.http.aclose()
    await stage.github.aclose()
