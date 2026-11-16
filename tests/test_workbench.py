"""The workbench: the sandbox a run keeps, the program that runs in it, the map of the
code, and the tasks that find and read files and run a command."""

# The repository the tools are tried on is written out here, and the programs are started
# as they are in a sandbox: long lines, fixed paths and subprocesses come with that.
# ruff: noqa: E501, S105, S108, S603, S607

from __future__ import annotations

import copy
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import yaml

from engineering_control.engine import bench_tasks, validate
from engineering_control.engine.builtin import BUILTIN
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.document import parse
from engineering_control.engine.records import FlowRecords
from engineering_control.engine.simulate import simulate
from engineering_control.sandbox import deliver, pods, workbench
from engineering_control.sandbox.launcher import Launcher
from tests.test_sandbox_delivery import (  # noqa: F401 - ``world`` is a fixture
    _log,
    _prepared,
    _ran,
    world,
)
from tests.test_sandbox_runs import SANDBOXES

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "src" / "engineering_control" / "sandbox" / "bench"
RUNNER = f"{sys.executable} {ROOT / 'images' / 'sandbox' / 'sandbox-step'}"
TOKEN = "t" * 64  # noqa: S105 - a test's own token


def _load(name: str, environment: dict[str, str]) -> Any:
    """One of the programs that run in the sandbox, loaded as it is there: by its file."""
    before = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    try:
        spec = importlib.util.spec_from_file_location(
            f"bench_{name}_{uuid4().hex}", BENCH / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return module


FILES = {
    "package.json": '{"name": "shop", "workspaces": ["packages/*"]}',
    "packages/billing/package.json": '{"name": "@shop/billing", "main": "dist/index.js"}',
    "packages/billing/src/index.ts": "export { chargeInvoice } from './invoice.js';\n",
    "packages/billing/src/invoice.ts": (
        "import { roundCents } from './money';\n"
        "export async function chargeInvoice(id: string) {\n  return roundCents(1);\n}\n"
        "export class InvoiceLedger {}\n"
    ),
    "packages/billing/src/money.ts": "export const roundCents = (value: number) => Math.round(value);\n",
    "packages/billing/src/invoice.test.ts": (
        "import { chargeInvoice } from './invoice';\ntest('charges', () => chargeInvoice('1'));\n"
    ),
    "apps/web/checkout.tsx": (
        "import { chargeInvoice } from '@shop/billing';\n"
        "import {\n  roundCents,\n} from '@shop/billing/money';\n"
        "const lazy = () => import('./cart');\n"
        "export function Checkout() { return chargeInvoice('1'); }\n"
    ),
    "apps/web/cart.tsx": "export const Cart = () => null;\n",
    "services/api/app/__init__.py": "",
    "services/api/app/quota.py": (
        "from .store import load\nfrom app import limits\n\n\n"
        "class QuotaGuard:\n    def enforce(self, tenant):\n        return load(tenant) < limits.MONTHLY\n"
    ),
    "services/api/app/store.py": "import json\n\n\ndef load(tenant):\n    return 0\n",
    "services/api/app/limits.py": "MONTHLY = 100\n",
    "services/api/tests/test_quota.py": (
        "from app.quota import QuotaGuard\n\n\ndef test_enforce():\n    assert QuotaGuard().enforce('a')\n"
    ),
    "services/meter/go.mod": "module example.com/meter\n\ngo 1.22\n",
    "services/meter/main.go": (
        'package main\n\nimport (\n\t"fmt"\n\t"example.com/meter/internal/count"\n)\n\n'
        "func main() { fmt.Println(count.Total()) }\n"
    ),
    "services/meter/internal/count/count.go": "package count\n\ntype Tally struct{}\n\nfunc Total() int { return 1 }\n",
    "services/meter/internal/count/count_test.go": "package count\n\nfunc TestTotal(t *testing.T) {}\n",
    "README.md": "The shop. Every file here says invoice once: invoice.\n",
    ".env.example": "TOKEN=not-a-secret\n",
    "logo.bin": "",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for name, text in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name == "logo.bin":
            path.write_bytes(b"\x89PNG\x00\x00binary")
        else:
            path.write_text(text)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run([*git, "add", "-A"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "first"], cwd=root, check=True)
    return root


@pytest.fixture
def tools(repo: Path, tmp_path: Path) -> Any:
    return _load(
        "tools", {"BENCH_REPO": str(repo), "BENCH_INDEX": str(tmp_path / "cache" / "index.json")}
    )


# --- the map of the code ---------------------------------------------------------------------


def test_the_map_says_what_each_file_defines_imports_and_is_tested_by(
    tools: Any, repo: Path
) -> None:
    document = tools.index()
    files = document["files"]
    assert document["summary"]["files"] == len(FILES) and document["summary"]["reused"] == 0
    # What a file defines, by language.
    assert files["packages/billing/src/invoice.ts"]["defs"] == ["chargeInvoice", "InvoiceLedger"]
    assert files["services/api/app/quota.py"]["defs"] == ["QuotaGuard", "enforce"]
    assert files["services/meter/internal/count/count.go"]["defs"] == ["Tally", "Total"]
    assert files["README.md"]["lang"] == "" and files["README.md"]["defs"] == []
    # Which file an import names: beside it, with the ending the source has, in a
    # package of the repository by its name, or loaded when it is needed.
    assert files["packages/billing/src/invoice.ts"]["imports"] == ["packages/billing/src/money.ts"]
    assert files["packages/billing/src/index.ts"]["imports"] == ["packages/billing/src/invoice.ts"]
    assert files["apps/web/checkout.tsx"]["imports"] == [
        "packages/billing/src/index.ts",
        "packages/billing/src/money.ts",
        "apps/web/cart.tsx",
    ]
    # Python: beside it, and by the package's name. What is not in the repository is not.
    assert files["services/api/app/quota.py"]["imports"] == [
        "services/api/app/store.py",
        "services/api/app/__init__.py",
        "services/api/app/limits.py",
    ]
    assert files["services/api/app/store.py"]["imports"] == []
    assert files["services/api/tests/test_quota.py"]["imports"] == ["services/api/app/quota.py"]
    # Go: the files of the package a path names, in the module the file is in.
    assert files["services/meter/main.go"]["imports"] == ["services/meter/internal/count/count.go"]
    # What is a test, by its name or its folder.
    tests = sorted(path for path, entry in files.items() if entry["test"])
    assert tests == [
        "packages/billing/src/invoice.test.ts",
        "services/api/tests/test_quota.py",
        "services/meter/internal/count/count_test.go",
    ]
    # Only what changed is read again.
    (repo / "packages/billing/src/money.ts").write_text(
        "export const roundCents = (v: number) => v;\nexport const toCents = 1;\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    again = tools.index()
    assert again["summary"]["reused"] == len(FILES) - 1
    assert again["files"]["packages/billing/src/money.ts"]["defs"] == ["roundCents", "toCents"]
    # A file that cannot be parsed is on the map all the same, with nothing known of it.
    (repo / "services/api/app/broken.py").write_text("def broken(:\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    assert tools.index()["files"]["services/api/app/broken.py"]["defs"] == []


def test_files_are_found_by_the_words_of_a_question_with_their_neighbours_and_why(
    tools: Any,
) -> None:
    found = tools.find({"text": "Charging an invoice rounds the cents wrong", "limit": 6})
    paths = [entry["path"] for entry in found["files"]]
    assert paths[0] == "packages/billing/src/invoice.ts"
    by_path = {entry["path"]: entry for entry in found["files"]}
    # Its test and what it imports come with it, and each says why it is there.
    assert "packages/billing/src/invoice.test.ts" in paths
    assert (
        "it tests packages/billing/src/invoice.ts"
        in by_path["packages/billing/src/invoice.test.ts"]["why"]
    )
    assert "packages/billing/src/money.ts" in paths
    assert any(
        "its path has invoice" in why for why in by_path["packages/billing/src/invoice.ts"]["why"]
    )
    assert "apps/web/checkout.tsx" in paths, "what uses it, through the package's entry"
    # Nothing of another part of the repository is offered, and nothing that is not code.
    assert not any(path.startswith("services/") or path == "README.md" for path in paths)
    assert {term["term"] for term in found["terms"]} >= {"charging", "invoice", "rounds", "cents"}
    assert found["indexed"]["files"] == len(FILES)

    # A name as it is written in the code is found as a whole and by its parts.
    guard = tools.find(
        {"text": "QuotaGuard lets a tenant over the monthly limit", "limit": 4, "neighbours": False}
    )
    assert guard["files"][0]["path"] == "services/api/app/quota.py"
    assert "it defines quotaguard" in guard["files"][0]["why"]
    assert len(guard["files"]) <= 4
    # A file the question names is first, whatever else it says.
    named = tools.find(
        {"text": "Look at services/meter/internal/count/count.go: the total is off", "limit": 3}
    )
    assert named["files"][0]["path"] == "services/meter/internal/count/count.go"
    assert "the question names it" in named["files"][0]["why"]
    # A question none of whose words are in the code finds nothing.
    assert tools.find({"text": "zebra umbrella", "limit": 5})["files"] == []
    assert tools.terms_of("the fix for this bug should be used when the test fails") == []
    assert tools.terms_of("parseHTTPResponse and snake_case_name") == [
        "parsehttpresponse",
        "parse",
        "http",
        "response",
        "snake_case_name",
        "snake",
        "case",
        "name",
    ]


def test_files_are_read_as_text_and_what_is_cut_or_left_out_is_said(tools: Any, repo: Path) -> None:
    (repo / ".env").write_text("TOKEN=a-real-secret\n")
    (repo / "long.py").write_text("\n".join(f"line_{n} = {n}" for n in range(1, 201)) + "\n")
    outside = repo.parent / "outside.txt"
    outside.write_text("not of the repository")
    answer = tools.read(
        {
            "paths": [
                "services/api/app/quota.py",
                "long.py",
                "../outside.txt",
                "/etc/passwd",
                ".env",
                "config/server.pem",
                "logo.bin",
                "nowhere.ts",
                "services/api/app/quota.py",
            ],
            "lines": 50,
            "characters": 5000,
        }
    )
    given = {entry["path"]: entry for entry in answer["files"]}
    assert list(given) == ["services/api/app/quota.py", "long.py"], "each file once"
    whole = given["services/api/app/quota.py"]
    assert (
        whole["text"] == FILES["services/api/app/quota.py"].rstrip("\n") and whole["cut"] is False
    )
    cut = given["long.py"]
    assert (cut["lines"], cut["shown"], cut["cut"]) == (200, 50, True)
    assert cut["text"].endswith("line_50 = 50")
    left = {entry["path"]: entry["why"] for entry in answer["left_out"]}
    assert left == {
        "../outside.txt": "it is not in the repository",
        "/etc/passwd": "it is not in the repository",
        ".env": "its name is one a secret is kept under",
        "config/server.pem": "its name is one a secret is kept under",
        "logo.bin": "it is not text",
        "nowhere.ts": "there is no such file",
    }
    assert "a-real-secret" not in json.dumps(answer) and "not of the repository" not in json.dumps(
        answer
    )
    # When there is no room left, what was not read is said, not cut silently.
    tight = tools.read(
        {"paths": ["long.py", "services/api/app/quota.py"], "lines": 5000, "characters": 1000}
    )
    assert [entry["path"] for entry in tight["files"]] == ["long.py"]
    assert tight["left_out"] == [
        {
            "path": "services/api/app/quota.py",
            "why": "no room left: read fewer files, or fewer lines of each",
        }
    ]


# --- the program in the sandbox --------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Bench:
    """The workbench's program, started as it is in a sandbox, on this machine."""

    def __init__(self, tmp_path: Path, token: str = TOKEN, **environment: str) -> None:
        self.port = _free_port()
        self.folder = tmp_path / f"bench-{self.port}"
        self.folder.mkdir()
        self.address = f"http://127.0.0.1:{self.port}"
        self.process = subprocess.Popen(  # noqa: S603 - the platform's own program
            [sys.executable, str(BENCH / "server.py")],
            env={
                **os.environ,
                "BENCH_PORT": str(self.port),
                "BENCH_TOKEN": token,
                "BENCH_RUNNER": RUNNER,
                "BENCH_WRITABLE": f"{tmp_path}/",
                **environment,
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.headers = {"Authorization": f"Bearer {token}"}
        for _try in range(100):
            try:
                if httpx.get(f"{self.address}/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.05)
        raise AssertionError("the workbench did not start")

    def stop(self) -> None:
        self.process.terminate()
        self.process.wait(timeout=10)

    def hand(self, key: str, job: dict[str, Any]) -> httpx.Response:
        return httpx.post(
            f"{self.address}/job", headers=self.headers, json={"key": key, "job": job}, timeout=10
        )

    def result(self, key: str, wait: float = 10) -> dict[str, Any]:
        answer = httpx.get(
            f"{self.address}/job/{key}",
            headers=self.headers,
            params={"wait": wait},
            timeout=wait + 5,
        )
        assert answer.status_code == 200, answer.text
        body: dict[str, Any] = answer.json()
        return body


@pytest.fixture
def bench(tmp_path: Path) -> Any:
    started = Bench(tmp_path)
    yield started
    started.stop()


def _job(*argv: str, name: str = "say", **more: Any) -> dict[str, Any]:
    return {"commands": [{"name": name, "argv": list(argv), "timeout": 30, **more}]}


def test_the_workbench_carries_out_jobs_one_at_a_time_for_who_has_its_token(
    bench: Bench, tmp_path: Path
) -> None:
    from engineering_control.sandbox import steps

    # Nothing is done without the token, and nothing is said about why.
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
        refused = httpx.post(
            f"{bench.address}/job", headers=headers, json={"key": "k", "job": _job("true")}
        )
        assert refused.status_code == 401
        assert httpx.get(f"{bench.address}/job/k", headers=headers).status_code == 401
    assert httpx.get(f"{bench.address}/job/unknown", headers=bench.headers).status_code == 404
    for wrong in (
        {"key": "", "job": _job("true")},
        {"key": "k"},
        {"key": "k", "job": {"commands": "no"}},
    ):
        assert (
            httpx.post(f"{bench.address}/job", headers=bench.headers, json=wrong).status_code == 400
        )

    # A job is what a sandbox of its own is given: files, commands, what is handed back.
    counted = bench.folder / "count.txt"
    note = bench.folder / "note.txt"
    answer = bench.folder / "answer.json"
    job = {
        "files": [{"path": str(note), "content": "written before the commands run"}],
        "commands": [
            {
                "name": "count",
                "argv": ["sh", "-c", f"echo x >> {counted}; cat {note}"],
                "timeout": 30,
            },
            {
                "name": "answer",
                "argv": ["sh", "-c", f"echo '{{\"ok\": true}}' > {answer}"],
                "timeout": 30,
            },
        ],
        "artifacts": [{"name": "answer", "path": str(answer), "max_bytes": 1000}],
    }
    assert bench.hand("first", job).status_code == 202
    done = bench.result("first")
    assert done["done"] is True
    commands = steps.parse_result(done["log"])
    assert commands is not None and [c["name"] for c in commands] == ["count", "answer"]
    assert commands[0]["stdout"].strip() == "written before the commands run"
    assert steps.parse_artifact(done["log"], "answer") == {"ok": True}
    assert "BENCH_TOKEN" not in done["log"] and TOKEN not in done["log"]

    # Asked for again under the same key, what was done stands: it is not done twice.
    again = bench.hand("first", job)
    assert again.status_code == 200 and again.json()["known"] is True
    assert bench.result("first")["log"] == done["log"]
    assert counted.read_text() == "x\n"

    # One job at a time; the second is told to wait, and the first is not disturbed.
    assert bench.hand("slow", _job("sleep", "1.5")).status_code == 202
    assert bench.hand("other", _job("true")).status_code == 409
    assert bench.result("slow", wait=0) == {"done": False}
    assert bench.result("slow")["done"] is True
    assert (
        bench.hand("other", _job("true")).status_code == 202
        and bench.result("other")["done"] is True
    )

    # A job may write only where a sandbox may: what it asks beyond that is its answer.
    assert (
        bench.hand(
            "escape", {"files": [{"path": "/etc/planted", "content": "x"}], **_job("true")}
        ).status_code
        == 202
    )
    refused = bench.result("escape")
    assert (
        "a job may not write /etc/planted" in refused["log"]
        and steps.parse_result(refused["log"]) is None
    )
    # The token is not given to what a job runs.
    assert bench.hand("env", _job("sh", "-c", "echo token=[$BENCH_TOKEN]")).status_code == 202
    printed = steps.parse_result(bench.result("env")["log"])
    assert printed is not None and printed[0]["stdout"].strip() == "token=[]"


def test_what_a_job_leaves_running_is_ended_with_it() -> None:
    server = _load("server", {"BENCH_TOKEN": TOKEN})
    kept = server._ancestors()
    assert os.getpid() in kept and os.getppid() in kept
    stray = subprocess.Popen(["sleep", "60"])  # noqa: S603, S607
    try:
        # What the sweep would end: every process but the workbench and where it came
        # from. Here it is only looked at, since other processes share this machine.
        assert stray.pid not in kept
    finally:
        stray.kill()
        stray.wait()
    assert server.SWEEP is False, "off unless the sandbox turns it on"


# --- the pod and the network -----------------------------------------------------------------


def test_the_workbench_is_a_sandbox_without_a_way_out_that_only_the_launcher_reaches() -> None:
    run_id = uuid4()
    pod = pods.bench_pod(
        SANDBOXES,
        run_id=run_id,
        workspace="run-x",
        server="print('door')",
        token=TOKEN,
        deadline_seconds=3600,
    )
    assert pod["metadata"]["name"] == f"bench-{run_id.hex[:16]}" == pods.bench_name(run_id)
    labels = pod["metadata"]["labels"]
    assert (
        labels["engineering-control/bench"] == "true"
        and labels["engineering-control/egress"] == "none"
    )
    assert "engineering-control/account" not in labels
    spec = pod["spec"]
    container = spec["containers"][0]
    # Unprivileged like every sandbox, with the run's volume and no account.
    assert (
        spec["automountServiceAccountToken"] is False
        and container["securityContext"]["readOnlyRootFilesystem"]
    )
    assert [volume["name"] for volume in spec["volumes"]] == ["home", "tmp", "workspace"]
    job = json.loads(container["env"][0]["value"])
    assert job["files"] == [{"path": "/tmp/workbench.py", "content": "print('door')"}]
    assert job["commands"][0]["env"] == {
        "BENCH_TOKEN": TOKEN,
        "BENCH_SWEEP": "1",
        "BENCH_PORT": "8791",
    }
    assert not any(entry["name"].lower().endswith("_proxy") for entry in container["env"])
    # The program it is started with is the one in the package, whole.
    assert (
        "ThreadingHTTPServer" in workbench.server_source()
        and "def find(" in workbench.tools_source()
    )

    policies = {
        policy["metadata"]["name"]: policy
        for policy in yaml.safe_load_all(
            (ROOT / "deploy" / "sandboxes" / "network-policy.yaml").read_text()
        )
    }
    door = policies["workbench"]["spec"]
    assert door["podSelector"] == {"matchLabels": {"engineering-control/bench": "true"}}
    assert door["policyTypes"] == ["Ingress"], "it opens a way in, and no way out"
    (rule,) = door["ingress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": pods.BENCH_PORT}]
    assert rule["from"] == [
        {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": "engineering-control"}
            },
            "podSelector": {
                "matchLabels": {
                    "app.kubernetes.io/name": "engineering-control",
                    "app.kubernetes.io/component": "launcher",
                }
            },
        }
    ]
    # Every sandbox is still denied everything unless a rule says otherwise.
    assert policies["deny-everything"]["spec"]["policyTypes"] == ["Ingress", "Egress"]


# --- the tasks -------------------------------------------------------------------------------


def _looking(**settings: Any) -> dict[str, Any]:
    """Deliver issue that finds and reads the files and runs a command before the agent
    implements."""
    raw = copy.deepcopy(BUILTIN["deliver-issue"].as_dict())
    tasks = {task["id"]: task for task in raw["tasks"]}
    tasks["tests"]["next"] = {"passed": "find", "failed": "find"}
    raw["tasks"] += [
        {
            "id": "find",
            "task": "code.find",
            "with": settings.get("find", {}),
            "next": {"found": "read", "none": "implement"},
        },
        {
            "id": "read",
            "task": "code.read",
            "with": settings.get("read", {}),
            "next": {"ok": "look"},
        },
        {
            "id": "look",
            "task": "code.run",
            "with": settings.get("look", {"commands": ["git status --short"]}),
            "next": {"passed": "implement", "failed": "implement"},
        },
    ]
    return raw


def _rules(raw: dict[str, Any]) -> list[str]:
    return [problem["text"] for problem in validate.errors(parse(raw))]


def test_three_tasks_look_at_code_in_the_workbench_without_an_agent() -> None:
    for key in ("code.find", "code.read", "code.run"):
        task = TASKS[key]
        assert (task.where, task.group) == ("bench", "Code")
        assert not (task.account or task.writes or task.publishes or task.approval or task.gate)
        assert not (task.reviewed or task.tested or task.scanned), "none stands in for a gate"
        assert task.needs == ("checkout",)
    assert TASKS["code.find"].outcomes == ("found", "none")
    assert TASKS["code.run"].outcomes == ("passed", "failed")
    assert set(bench_tasks.BENCH) == {
        "code.find",
        "code.read",
        "code.run",
        "code.tests",
        "code.digest",
        "code.apply",
    }

    assert _rules(_looking()) == []
    for wrong, why in (
        ({"find": {"limit": 0}}, "limit of find is between 1 and 80"),
        ({"read": {"lines": 5}}, "lines of read is between 20 and 5000"),
        ({"read": {"characters": 10}}, "characters of read is between 1000 and 600000"),
        ({"look": {"commands": ['echo "unfinished']}}, "commands of look"),
    ):
        assert any(why in text for text in _rules(_looking(**wrong))), (
            wrong,
            _rules(_looking(**wrong)),
        )
    # Before there is a checkout there is nothing to look at.
    early = _looking()
    early["start"] = "find"
    assert any("needs checkout" in text for text in _rules(early))
    # Running a command is not the tests a change has to pass: dropping them is refused.
    lax = _looking()
    by_id = {task["id"]: task for task in lax["tasks"]}
    by_id["review"]["next"]["pass"] = "scan"
    assert any("test" in text for text in _rules(lax))

    walked = [step["task"] for step in simulate(parse(_looking()))["steps"]]
    assert walked[walked.index("tests") :][:5] == ["tests", "find", "read", "look", "implement"]
    job = bench_tasks.tool_job("find", {"text": "a question"}, max_bytes=10)
    assert [entry["path"] for entry in job["files"]] == [
        "/tmp/bench/tools.py",
        "/tmp/bench/find.json",
    ]
    assert job["commands"][0]["argv"][:4] == [
        "python3",
        "/tmp/bench/tools.py",
        "find",
        "/tmp/bench/find.json",
    ]
    assert job["artifacts"] == [
        {"name": "find", "path": "/workspace/.run/find.json", "max_bytes": 10}
    ]
    assert bench_tasks.paths_of(" a/b.py, c.ts  d.go ") == ["a/b.py", "c.ts", "d.go"]


# --- a run, with a workbench behind a stand-in -----------------------------------------------


class Door:
    """A workbench as the launcher reaches it: what it was handed, and what it says."""

    def __init__(self) -> None:
        self.handed: list[tuple[str, dict[str, Any]]] = []
        self.done: dict[str, str] = {}
        self.answers: list[str] = []
        self.busy = False
        self.forgets = False
        self.health = 200
        self.tokens: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(self.health, json={"ok": True})
        self.tokens.add(request.headers.get("authorization", ""))
        if request.method == "POST":
            body = json.loads(request.content)
            if self.busy:
                return httpx.Response(409, json={})
            if body["key"] in self.done:
                return httpx.Response(200, json={"known": True})
            self.handed.append((body["key"], body["job"]))
            if self.answers:
                self.done[body["key"]] = self.answers.pop(0)
            return httpx.Response(202, json={"known": False})
        key = path.removeprefix("/job/")
        if self.forgets or (key not in self.done and key not in [k for k, _ in self.handed]):
            return httpx.Response(404, json={})
        if key in self.done:
            return httpx.Response(200, json={"done": True, "log": self.done[key]})
        return httpx.Response(200, json={"done": False})


def _found(*paths: str) -> str:
    answer = {
        "files": [
            {"path": path, "score": 9.5, "lines": 40, "test": False, "why": ["it defines cookie"]}
            for path in paths
        ],
        "terms": [{"term": "cookie", "files": 3, "common": False}],
        "indexed": {
            "files": 120,
            "code": 80,
            "tests": 30,
            "imports": 210,
            "reused": 0,
            "seconds": 0.4,
        },
    }
    return _tool_log("find", answer)


def _read(*paths: str, cut: bool = False) -> str:
    answer = {
        "files": [
            {
                "path": path,
                "text": f"// {path}\nexport const value = 1;",
                "lines": 900 if cut else 2,
                "shown": 2,
                "cut": cut,
            }
            for path in paths
        ],
        "left_out": [],
    }
    return _tool_log("read", answer)


def _tool_log(name: str, answer: dict[str, Any]) -> str:
    from engineering_control.sandbox import steps

    return f"\n{steps.ARTIFACT}{name}::{json.dumps(answer, separators=(',', ':'))}\n" + _log(
        [_ran(name, "{}")]
    )


@pytest.fixture
def door(world: Any) -> Any:  # noqa: F811
    stand_in = Door()
    world.launcher = Launcher(
        world.runs,
        world.launcher.cluster,
        SANDBOXES,
        world.github,
        bench_transport=httpx.MockTransport(stand_in),
        grant_key=b"k" * 48,
    )
    return stand_in


async def _prepared_run(world: Any, raw: dict[str, Any]) -> dict[str, Any]:  # noqa: F811
    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    flows = FlowRecords(world.sessions)
    flows.save_draft("deliver-issue", raw)
    flows.publish("deliver-issue")
    run: dict[str, Any] = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    await world.step("prepare", _prepared())
    return run


def _steps(world: Any, run_id: str) -> dict[str, dict[str, Any]]:  # noqa: F811
    return {step["name"]: step for step in world.run(run_id)["steps"]}


def _bench_pod(world: Any, run_id: str) -> dict[str, Any] | None:  # noqa: F811
    found: dict[str, Any] | None = world.fake.pods.get(pods.bench_name(UUID(run_id)))
    return found


def _running(pod: dict[str, Any]) -> None:
    pod["status"] = {"phase": "Running", "podIP": "10.42.7.9"}


@pytest.mark.asyncio
async def test_a_run_keeps_one_workbench_and_gets_through_its_small_steps_in_one_pass(
    world: Any,  # noqa: F811
    door: Door,
) -> None:  # noqa: F811
    run = await _prepared_run(world, _looking())
    # The first task of the workbench starts it: a sandbox of the run's own, kept.
    await world.launcher.tick()
    pod = _bench_pod(world, run["id"])
    assert pod is not None and list(world.fake.pods) == [pod["metadata"]["name"]]
    assert pod["spec"]["volumes"][2]["persistentVolumeClaim"]["claimName"] == pods.workspace_claim(
        UUID(run["id"])
    )
    waiting = _steps(world, run["id"])["find"]
    assert (waiting["state"], waiting["detail"]) == ("pending", "The workbench is starting.")
    await world.launcher.tick()
    assert _steps(world, run["id"])["find"]["state"] == "pending" and not door.handed

    # Once it answers, the three steps go by in one pass: none waits for the next one.
    _running(pod)
    door.answers = [
        _found("src/session.ts", "src/cookie.ts"),
        _read("src/session.ts", "src/cookie.ts"),
        _log([_ran("check 1", " M src/x")]),
    ]
    await world.launcher.tick()
    steps = _steps(world, run["id"])
    assert [steps[name]["state"] for name in ("find", "read", "look")] == ["succeeded"] * 3
    assert [key for key, _job in door.handed] == [
        steps[name]["id"] for name in ("find", "read", "look")
    ]
    assert door.tokens == {f"Bearer {world.launcher.workbench.token(UUID(run['id']))}"}
    assert len(next(iter(door.tokens))) == len("Bearer ") + 64

    # Find: what the run is about is what is looked for; what is found goes on.
    find_job = door.handed[0][1]
    question = json.loads(find_job["files"][1]["content"])
    assert (
        question["text"].startswith("Signing in fails with an expired cookie")
        and question["limit"] == 20
    )
    assert steps["find"]["outcome"] == "found"
    assert steps["find"]["detail"] == (
        "2 files: src/session.ts, src/cookie.ts. The map has 80 files of code and 210 imports between them."
    )
    data = world.run(run["id"])["data"]
    assert data["found"] == ["src/session.ts", "src/cookie.ts"]
    # Read: the files found, and what was read is kept with the step, not with the run.
    assert json.loads(door.handed[1][1]["files"][1]["content"]) == {
        "paths": ["src/session.ts", "src/cookie.ts"],
        "lines": 600,
        "characters": 200000,
    }
    assert steps["read"]["detail"].startswith("2 files, ")
    assert data["sources"] == [
        {"path": "src/session.ts", "lines": 2, "shown": 2, "cut": False},
        {"path": "src/cookie.ts", "lines": 2, "shown": 2, "cut": False},
    ]
    assert "export const value" not in json.dumps(data)
    # Run a command: the task's own command, in the checkout, without the network.
    look_job = door.handed[2][1]
    assert look_job["commands"][0]["argv"] == ["git", "status", "--short"]
    assert look_job["commands"][0]["cwd"] == deliver.REPO
    assert (steps["look"]["outcome"], steps["look"]["detail"]) == ("passed", "1 command(s) passed.")

    # The agent's step is next, in a sandbox of its own, started in the same pass; the
    # workbench stays beside it.
    assert _steps(world, run["id"])["implement"]["state"] == "running"
    assert sorted(world.fake.pods) == sorted(
        [pod["metadata"]["name"], *[n for n in world.fake.pods if n.startswith("run-")]]
    )
    assert len(world.fake.pods) == 2
    # When the run ends, the workbench goes before the volume it holds.
    world.runs.request_cancel(UUID(run["id"]))
    await world.launcher.tick()
    assert world.fake.pods == {}
    assert pods.workspace_claim(UUID(run["id"])) not in world.fake.claims
    gone = world.fake.deleted
    assert gone.index(pod["metadata"]["name"]) < gone.index(pods.workspace_claim(UUID(run["id"])))
    await world.github.aclose()


@pytest.mark.asyncio
async def test_a_workbench_that_is_busy_lost_or_never_starts_is_waited_for_or_replaced(
    world: Any,  # noqa: F811
    door: Door,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = await _prepared_run(world, _looking())
    await world.launcher.tick()
    pod = _bench_pod(world, run["id"])
    assert pod is not None
    _running(pod)
    # Busy with another job: the step waits and says so.
    door.busy = True
    await world.launcher.tick()
    find = _steps(world, run["id"])["find"]
    assert (find["state"], find["detail"]) == ("pending", "The workbench is busy with another job.")
    door.busy = False
    # A job that takes long is not waited for within the pass: the other runs go on.
    await world.launcher.tick()
    find = _steps(world, run["id"])["find"]
    assert (find["state"], find["attempt"]) == ("running", 1) and len(door.handed) == 1
    await world.launcher.tick()
    assert _steps(world, run["id"])["find"]["state"] == "running"
    # The workbench forgets the job (it was started anew): the step is run again.
    door.forgets = True
    await world.launcher.tick()
    find = _steps(world, run["id"])["find"]
    assert (find["state"], find["detail"]) == (
        "pending",
        "The workbench was lost; the step is run again.",
    )
    door.forgets = False
    door.handed.clear()
    # The pod itself is gone: a new one is started, and the step waits for it.
    world.fake.pods.clear()
    await world.launcher.tick()
    again = _bench_pod(world, run["id"])
    assert (
        again is not None
        and _steps(world, run["id"])["find"]["detail"] == "The workbench is starting."
    )
    # A workbench that ended (its time was up) is deleted and started again.
    again["status"] = {"phase": "Failed", "reason": "DeadlineExceeded"}
    await world.launcher.tick()
    assert _bench_pod(world, run["id"]) is None
    await world.launcher.tick()
    third = _bench_pod(world, run["id"])
    assert third is not None
    # A tool that fails fails the run, with what it said.
    _running(third)
    door.answers = [_log([_ran("find", exit_code=1, stderr="fatal: not a git repository")])]
    await world.launcher.tick()
    failed = world.run(run["id"])
    assert (failed["state"], failed["outcome"]) == ("failed", "find_failed")
    assert "not a git repository" in failed["detail"]
    assert world.fake.pods == {}, "the workbench is released with the run"

    # One that never answers is given up on after its time.
    monkeypatch.setattr(workbench, "START_SECONDS", -1)
    stuck = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    await world.step("prepare", _prepared())
    await world.launcher.tick()
    door.health = 503
    _running(_bench_pod(world, stuck["id"]))  # type: ignore[arg-type]
    await world.launcher.tick()
    ended = world.run(stuck["id"])
    assert (ended["state"], ended["outcome"]) == ("failed", "no_workbench")
    assert ended["detail"] == "The workbench did not start in time."
    await world.github.aclose()


@pytest.mark.asyncio
async def test_nothing_found_goes_its_own_way_and_a_workbench_with_nothing_to_do_is_let_go(
    world: Any,  # noqa: F811
    door: Door,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = await _prepared_run(
        world, _looking(find={"words": "expiry", "limit": 5, "neighbours": False})
    )
    await world.launcher.tick()
    pod = _bench_pod(world, run["id"])
    assert pod is not None
    _running(pod)
    door.answers = [_found()]
    await world.launcher.tick()
    find = _steps(world, run["id"])["find"]
    assert (find["outcome"], find["state"]) == ("nothing_found", "succeeded")
    assert find["detail"].startswith("No file of the repository matches what the run is about.")
    question = json.loads(door.handed[0][1]["files"][1]["content"])
    assert question["text"].startswith("expiry\n") and (
        question["limit"],
        question["neighbours"],
    ) == (5, False)
    # The flow says where nothing found leads: here, straight to the agent.
    assert "read" not in _steps(world, run["id"])
    assert _steps(world, run["id"])["implement"]["state"] in ("pending", "running")
    # A workbench that has had nothing to do for a while is let go; the run goes on.
    monkeypatch.setattr(workbench, "IDLE_SECONDS", -1)
    await world.launcher.tick()
    assert _bench_pod(world, run["id"]) is None
    assert world.run(run["id"])["state"] == "running"
    assert pods.workspace_claim(UUID(run["id"])) in world.fake.claims, "its volume stays"
    await world.github.aclose()


# --- a run, with the real program and the real tools -----------------------------------------


@pytest.mark.asyncio
async def test_the_tasks_work_on_a_real_repository_through_the_real_workbench(
    world: Any,  # noqa: F811
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The sandbox's paths, on this machine.
    answers = tmp_path / "answers"
    monkeypatch.setattr(deliver, "REPO", str(repo))
    monkeypatch.setattr(bench_tasks, "_FOLDER", str(tmp_path / "jobs"))
    monkeypatch.setattr(bench_tasks, "_ANSWERS", str(answers))
    started: list[Bench] = []
    create = world.fake.__call__

    def cluster(request: httpx.Request) -> httpx.Response:
        answer = create(request)
        if request.method == "POST" and request.url.path.endswith("/pods"):
            body = json.loads(request.content)
            if body["metadata"]["labels"].get("engineering-control/bench") == "true":
                # The cluster starts the pod: here, the program it would run.
                job = json.loads(body["spec"]["containers"][0]["env"][0]["value"])
                assert job["files"][0]["content"] == workbench.server_source()
                bench = Bench(
                    tmp_path,
                    token=job["commands"][0]["env"]["BENCH_TOKEN"],
                    BENCH_REPO=str(repo),
                    BENCH_INDEX=str(tmp_path / "cache" / "index.json"),
                )
                started.append(bench)
                monkeypatch.setattr(pods, "BENCH_PORT", bench.port)
                world.fake.pods[body["metadata"]["name"]]["status"] = {
                    "phase": "Running",
                    "podIP": "127.0.0.1",
                }
        return answer

    world.launcher.cluster._client._transport = httpx.MockTransport(cluster)
    with world.sessions.begin() as session:
        from engineering_control.db.models import BacklogItem

        item = session.get(BacklogItem, world.item_id)
        item.title = "Charging an invoice rounds the cents wrong"
    try:
        run = await _prepared_run(
            world,
            _looking(
                find={"limit": 8, "words": "Charging an invoice rounds the cents wrong"},
                read={"lines": 40},
                look={"commands": ["git status --short", "git ls-files nowhere --error-unmatch"]},
            ),
        )
        began = time.monotonic()
        await world.launcher.tick()  # starts the workbench
        await world.launcher.tick()  # find, read and the commands, in one pass
        took = time.monotonic() - began
        steps = _steps(world, run["id"])
        assert [steps[name]["state"] for name in ("find", "read", "look")] == ["succeeded"] * 3, (
            steps
        )
        assert took < 20, "three steps in seconds, where three sandboxes would take half a minute"

        data = world.run(run["id"])["data"]
        assert "packages/billing/src/invoice.ts" in data["found"][:3]
        assert "packages/billing/src/invoice.test.ts" in data["found"]
        assert steps["find"]["detail"].endswith(
            "The map has 14 files of code and 11 imports between them."
        )
        read = world.runs.step_outputs(UUID(run["id"]), "read")[-1]
        by_path = {entry["path"]: entry for entry in read["files"]}
        assert by_path["packages/billing/src/invoice.ts"]["text"].startswith(
            "import { roundCents } from './money';"
        )
        assert [entry["path"] for entry in data["sources"]] == data["found"]
        # What the console is given of a step that read files is their names.
        shown = steps["read"]["output"]
        assert "text" not in json.dumps(shown) and shown["files"][0]["path"] == data["found"][0]
        # The second command fails, and what failed is given to the task that follows.
        assert (steps["look"]["outcome"], steps["look"]["state"]) == ("failed", "succeeded")
        assert (
            steps["look"]["detail"]
            == "1 of 2 command(s) failed: git ls-files nowhere --error-unmatch"
        )
        following = world.run(run["id"])["steps"][-1]
        assert following["name"] == "implement"
        assert following["input"]["failures"][0].startswith(
            "git ls-files nowhere --error-unmatch\n"
        )
        assert (tmp_path / "cache" / "index.json").exists(), "the map is kept for the next task"
    finally:
        for bench in started:
            bench.stop()
    assert len(started) == 1, "one workbench for the run"
    await world.github.aclose()
