"""A change written by a model asked over an API: the edits it writes, how the platform
applies them all or not at all, and who reviews what a model wrote."""

# The repository the edits are tried on is written out in tests/test_workbench.py, and the
# workbench's program is started as it is in a sandbox.
# ruff: noqa: ASYNC221, E501, S105, S108, S603, S607

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from engineering_control.engine import bench_tasks, validate
from engineering_control.engine.builtin import BUILTIN
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.document import parse
from engineering_control.engine.records import FlowRecords
from engineering_control.engine.routing import provider_for
from engineering_control.engine.simulate import simulate
from engineering_control.models.sealing import seal
from engineering_control.models.service import Models
from engineering_control.sandbox import deliver, pods, steps, workbench
from engineering_control.sandbox.launcher import Launcher
from tests.test_models_api import OTHER_KEY, SEALING, Provider, said
from tests.test_sandbox_delivery import (  # noqa: F401 - ``world`` is a fixture
    HEAD,
    _log,
    _prepared,
    _ran,
    world,
)
from tests.test_sandbox_runs import SANDBOXES
from tests.test_workbench import (  # noqa: F401 - fixtures
    FILES,
    Bench,
    Door,
    _bench_pod,
    _read,
    _running,
    _steps,
    _tool_log,
    repo,
    tools,
)

EDITS = {
    "summary": "Rounds half a cent up.",
    "edits": [
        {
            "path": "packages/billing/src/money.ts",
            "action": "replace",
            "find": "Math.round(value)",
            "replace": "Math.floor(value + 0.5)",
        },
        {
            "path": "packages/billing/src/money.test.ts",
            "action": "write",
            "find": "",
            "replace": "import { roundCents } from './money';\ntest('half', () => roundCents(0.5));\n",
        },
    ],
}


def _edit(path: str, action: str = "replace", find: str = "", replace: str = "") -> dict[str, str]:
    return {"path": path, "action": action, "find": find, "replace": replace}


def _status(repo: Path) -> str:  # noqa: F811
    done = subprocess.run(["git", "status", "--short"], cwd=repo, capture_output=True, check=True)
    return done.stdout.decode()


# --- applying edits ---------------------------------------------------------------------------


def test_edits_are_applied_in_order_and_files_are_written_and_deleted(
    tools: Any,  # noqa: F811
    repo: Path,  # noqa: F811
) -> None:
    answer = tools.apply(
        {
            "edits": [
                *EDITS["edits"],
                # A second edit to a file works on what the first one left.
                _edit(
                    "packages/billing/src/money.ts",
                    find="Math.floor(value + 0.5)",
                    replace="Math.floor(value + 0.5) | 0",
                ),
                _edit("apps/web/cart.tsx", "delete"),
                _edit("docs/notes/rounding.md", "write", replace="Half a cent is rounded up.\n"),
            ],
            "protected": list(deliver.PROTECTED),
        }
    )
    assert answer["refused"] == [] and answer["edits"] == 5
    assert answer["applied"] == [
        {"path": "packages/billing/src/money.ts", "action": "changed"},
        {"path": "packages/billing/src/money.test.ts", "action": "added"},
        {"path": "apps/web/cart.tsx", "action": "deleted"},
        {"path": "docs/notes/rounding.md", "action": "added"},
    ]
    assert (repo / "packages/billing/src/money.ts").read_text() == (
        "export const roundCents = (value: number) => Math.floor(value + 0.5) | 0;\n"
    )
    assert (repo / "docs/notes/rounding.md").read_text() == "Half a cent is rounded up.\n"
    assert not (repo / "apps/web/cart.tsx").exists()


def test_an_edit_that_does_not_fit_is_refused_with_why_and_nothing_is_written(
    tools: Any,  # noqa: F811
    repo: Path,  # noqa: F811
) -> None:
    (repo / "twice.ts").write_text("const a = 1;\nconst b = 1;\n")
    good = _edit(
        "packages/billing/src/money.ts", find="Math.round(value)", replace="Math.trunc(value)"
    )
    cases = [
        (
            _edit("packages/billing/src/money.ts", find="Math.ceil(value)", replace="x"),
            "the text to replace is not in the file as it is now",
        ),
        (
            _edit(
                "packages/billing/src/invoice.ts",
                find="export async function chargeInvoice(id: string)  {",
                replace="x",
            ),
            "with other spacing or line breaks: copy it exactly",
        ),
        (
            _edit("twice.ts", find=" = 1;", replace=" = 2;"),
            "is in the file 2 times: give more of the lines around it",
        ),
        (
            _edit("packages/billing/src/money.ts", find="", replace="x"),
            "it does not say which text to replace",
        ),
        (
            _edit("nowhere/new.ts", find="a", replace="b"),
            "there is no such file: write it whole instead",
        ),
        (_edit("nowhere/old.ts", "delete"), "there is no such file to delete"),
        (
            _edit("packages/billing/src/money.ts", "rename"),
            "it says neither replace, write nor delete",
        ),
        (_edit("../outside.ts", "write", replace="x"), "it is not a file of the repository"),
        (_edit("/etc/passwd", "write", replace="x"), "it is not a file of the repository"),
        (_edit("", "write", replace="x"), "it is not a file of the repository"),
        (_edit(".git/config", "write", replace="x"), "it is not a file of the repository's own"),
        (
            _edit(".github/workflows/ci.yml", "write", replace="x"),
            "it is a file that may not be changed here",
        ),
        (_edit("CLAUDE.md", "write", replace="x"), "it is a file that may not be changed here"),
        (_edit(".env", "write", replace="TOKEN=x"), "its name is one a secret is kept under"),
        (
            _edit("logo.bin", find="PNG", replace="GIF"),
            "it is not a text file that can be edited here",
        ),
        (_edit("packages", "write", replace="x"), "it is not a plain file"),
    ]
    for edit, why in cases:
        answer = tools.apply({"edits": [good, edit], "protected": list(deliver.PROTECTED)})
        assert answer["applied"] == [], edit
        (refused,) = answer["refused"]
        assert why in refused["why"], (edit, refused)
        assert (refused["edit"], refused["path"]) == (2, edit["path"])
        # The edit that fitted was not written either: all of them, or none.
        assert _status(repo) == "?? twice.ts\n", edit
    # A file that would no longer be what its name says is refused as well.
    broken = tools.apply(
        {
            "edits": [
                _edit("services/api/app/limits.py", "write", replace="MONTHLY = (100\n"),
                _edit("package.json", find='"name": "shop",', replace='"name": "shop"'),
            ]
        }
    )
    assert broken["applied"] == []
    assert [(entry["path"], entry["why"][:32]) for entry in broken["refused"]] == [
        ("services/api/app/limits.py", "it would not be Python any more "),
        ("package.json", "it would not be JSON any more (E"),
    ]
    assert _status(repo) == "?? twice.ts\n"
    assert tools.apply({"edits": []}) == {"applied": [], "refused": [], "edits": 0}


# --- the tasks and the rules ------------------------------------------------------------------


def _editing(provider: str = "deepseek", **settings: Any) -> dict[str, Any]:
    """Deliver issue in which a model writes the change and the platform applies it, in
    place of the agent that implements."""
    raw = copy.deepcopy(BUILTIN["deliver-issue"].as_dict())
    tasks = {task["id"]: task for task in raw["tasks"]}
    tasks["tests"]["next"] = {"passed": "find", "failed": "find"}
    tasks["review"]["with"] = {**tasks["review"].get("with", {}), "other_than": "apply"}
    tasks["review"]["next"] = {"pass": "verify", "changes": "write"}
    tasks["verify"]["next"] = {**tasks["verify"]["next"], "regression": "write"}
    raw["tasks"] = [task for task in raw["tasks"] if task["id"] != "implement"]
    raw["tasks"] += [
        {"id": "find", "task": "code.find", "with": {}, "next": {"found": "read", "none": "write"}},
        {"id": "read", "task": "code.read", "with": {}, "next": {"ok": "write"}},
        {
            "id": "write",
            "task": "model.edit",
            "with": {
                "provider": provider,
                "model": "model-a",
                "instruction": "Resolve the issue.",
                **settings,
            },
            "next": {"edits": "apply"},
        },
        {
            "id": "apply",
            "task": "code.apply",
            "with": {},
            "next": {"applied": "review", "refused": "write"},
        },
    ]
    return raw


def _rules(raw: dict[str, Any]) -> list[str]:
    return [problem["text"] for problem in validate.errors(parse(raw))]


def test_a_change_a_model_wrote_is_held_to_what_an_agents_is() -> None:
    edit, apply = TASKS["model.edit"], TASKS["code.apply"]
    assert (edit.where, edit.outcomes, edit.produces, edit.writes) == (
        "model",
        ("edits", "nothing"),
        ("edits",),
        False,
    )
    assert (apply.where, apply.outcomes, apply.writes) == ("bench", ("applied", "refused"), True)
    assert apply.needs == ("checkout", "edits") and not apply.account and not apply.publishes
    assert "code.apply" in bench_tasks.BENCH

    assert _rules(_editing()) == []
    # The edits have to come from somewhere, and what is applied has to be reviewed,
    # tested, scanned, collected and decided on like any change.
    raw = _editing()
    raw["tasks"] = [task for task in raw["tasks"] if task["id"] != "write"]
    for task in raw["tasks"]:
        task["next"] = {way: "apply" if to == "write" else to for way, to in task["next"].items()}
    assert any("apply (Apply the edits) needs edits" in text for text in _rules(raw)), _rules(raw)
    raw = _editing()
    by_id = {task["id"]: task for task in raw["tasks"]}
    by_id["apply"]["next"] = {"applied": "verify", "refused": "write"}
    assert any("review" in text for text in _rules(raw))
    raw = _editing()
    by_id = {task["id"]: task for task in raw["tasks"]}
    by_id["review"]["next"]["pass"] = "scan"
    assert any("test" in text for text in _rules(raw))
    # The reviewer says whose work it judges: an agent's, or what the platform applied.
    raw = _editing()
    by_id = {task["id"]: task for task in raw["tasks"]}
    by_id["review"]["with"]["other_than"] = "find"
    assert any(
        "names a task an agent works in, or one that changes the code" in text
        for text in _rules(raw)
    )

    walked = [step["task"] for step in simulate(parse(_editing()))["steps"]]
    assert walked[walked.index("find") :][:6] == [
        "find",
        "read",
        "write",
        "apply",
        "review",
        "verify",
    ]
    again = simulate(parse(_editing()), outcomes={"apply": ["refused", "applied"]})
    assert [step["task"] for step in again["steps"]].count("write") == 2

    # The judge is never of the family that did the work: an API and the subscription
    # of the same company are one family.
    review = parse(_editing()).node("review")
    assert review is not None
    assert provider_for(review, {"apply": "anthropic"}, again=False) == "chatgpt"
    assert provider_for(review, {"apply": "openai"}, again=False) == "claude"
    assert provider_for(review, {"apply": "claude"}, again=False) == "chatgpt"
    for other in ("deepseek", "qwen", "glm", "kimi"):
        assert provider_for(review, {"apply": other}, again=False) == "chatgpt,claude", other


# --- a run, with the workbench and the provider behind stand-ins -------------------------------


@pytest.fixture
def stage(world: Any) -> Any:  # noqa: F811
    """The run's world with a workbench and a provider behind stand-ins."""
    door, provider = Door(), Provider()
    world.http = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    world.launcher = Launcher(
        world.runs,
        world.launcher.cluster,
        SANDBOXES,
        world.github,
        bench_transport=httpx.MockTransport(door),
        grant_key=b"k" * 48,
        models=Models(world.sessions, SEALING, http=world.http),
    )
    world.door, world.provider = door, provider
    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    for name, kind in (("DeepSeek one", "deepseek"), ("Anthropic one", "anthropic")):
        account_id = UUID(world.accounts.create(name=name, provider=kind)["id"])
        key = OTHER_KEY if kind == "deepseek" else "sk-ant-api03-" + "aB3_-" * 20
        world.accounts.key_stored(account_id, seal(SEALING, account_id, key))
    return world


async def _to_the_model(stage: Any, raw: dict[str, Any]) -> dict[str, Any]:
    """A delivery by that flow as far as the model that writes: the files are read."""
    flows = FlowRecords(stage.sessions)
    in_force = flows.document("deliver-issue", flows.version_in_force("deliver-issue") or 0)
    if in_force is None or in_force.as_dict() != parse(raw).as_dict():
        flows.save_draft("deliver-issue", raw)
        flows.publish("deliver-issue")
    run: dict[str, Any] = stage.runs.start_delivery(stage.item_id, toolchain=stage.toolchain)
    await stage.step("prepare", _prepared())
    await stage.launcher.tick()
    pod = _bench_pod(stage, run["id"])
    assert pod is not None
    _running(pod)
    return run


async def _answered(stage: Any) -> None:
    import asyncio

    calls = list(stage.launcher._calls.values())
    if calls:
        await asyncio.wait(calls, timeout=10)
    await stage.launcher.tick()


def _applied(*paths: str) -> str:
    answer = {
        "applied": [{"path": path, "action": "changed"} for path in paths],
        "refused": [],
        "edits": len(paths),
    }
    listed = f"{len(paths)}\n" + "\n".join(paths)
    return _tool_log("apply", answer).replace(
        _log([_ran("apply", "{}")]),
        _log([_ran("apply", "{}"), _ran("commit", f"{HEAD}\n"), _ran("touched", listed)]),
    )


def _refused(*reasons: tuple[str, str]) -> str:
    answer = {
        "applied": [],
        "refused": [
            {"edit": n, "path": path, "action": "replace", "why": why}
            for n, (path, why) in enumerate(reasons, 1)
        ],
        "edits": 2,
    }
    return _tool_log("apply", answer).replace(
        _log([_ran("apply", "{}")]),
        _log([_ran("apply", "{}"), _ran("commit", "0" * 40), _ran("touched", "0\n")]),
    )


@pytest.mark.asyncio
async def test_a_model_writes_edits_the_platform_applies_them_and_another_family_reviews(
    stage: Any,
) -> None:
    from tests.test_workbench import _found

    stage.provider.answers = [said(EDITS)]
    run = await _to_the_model(stage, _editing())
    stage.door.answers = [
        _found("packages/billing/src/money.ts"),
        _read("packages/billing/src/money.ts"),
    ]
    await stage.launcher.tick()  # find and read; the model is asked
    assert _steps(stage, run["id"])["write"]["state"] == "running"
    stage.door.answers = [
        _applied("packages/billing/src/money.ts", "packages/billing/src/money.test.ts")
    ]
    await _answered(stage)  # the edits are written, applied and committed in one pass

    steps_now = _steps(stage, run["id"])
    written = steps_now["write"]
    assert (written["state"], written["outcome"]) == ("succeeded", "edits_written")
    assert written["detail"] == "2 edits to 2 files: Rounds half a cent up."
    assert written["usage"]["provider"] == "deepseek" and written["usage"]["tokens"] == 1280
    # The model was told how an edit is written, and given the file it is to change.
    rules, question = (m["content"] for m in stage.provider.asked[-1][2]["messages"])
    assert "You have no tools" in rules
    assert "Write the change that is asked for below, as edits" in question
    assert "find is a text that is in the file exactly once" in question
    assert "What you are asked:\nResolve the issue." in question
    assert "The file packages/billing/src/money.ts:\n<<<material\n" in question
    assert all(f'"{action}"' in question for action in ("replace", "write", "delete"))

    # The edits went to the workbench as they were written, with what may not be changed,
    # and the platform committed.
    key, job = stage.door.handed[-1]
    assert key == steps_now["apply"]["id"]
    handed = json.loads(job["files"][1]["content"])
    assert handed == {"edits": EDITS["edits"], "protected": list(deliver.PROTECTED)}
    assert [command["name"] for command in job["commands"]] == ["apply", "commit", "touched"]
    assert job["commands"][1]["env"]["MESSAGE"].startswith(
        "Signing in fails with an expired cookie"
    )
    assert job["commands"][1]["env"]["GIT_AUTHOR_NAME"] == "Engineering Control"
    applied = steps_now["apply"]
    assert (applied["state"], applied["outcome"]) == ("succeeded", "applied")
    assert applied["detail"] == (
        "2 files changed: packages/billing/src/money.ts, packages/billing/src/money.test.ts"
    )
    data = stage.run(run["id"])["data"]
    assert (data["head"], data["maker"]) == (HEAD, "deepseek")
    assert data["ran"]["apply"] == "deepseek" and data["touched"]["count"] == 2

    # What follows is the review by an agent of a subscription, like after any change.
    review = stage.run(run["id"])["steps"][-1]
    # Either subscription may review what DeepSeek wrote; the first that is free does.
    assert review["name"] == "review" and review["provider"] in ("chatgpt,claude", "chatgpt")
    assert review["state"] in ("pending", "running")
    await stage.http.aclose()
    await stage.github.aclose()


@pytest.mark.asyncio
async def test_a_change_an_anthropic_model_wrote_is_not_reviewed_by_claude(
    stage: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx2

    from engineering_control.models import anthropic_api
    from tests.test_models_api import Anthropic, streamed
    from tests.test_workbench import _found

    monkeypatch.setattr(anthropic_api, "RETRIES", 0)
    hub = Anthropic()
    hub.answers = [streamed(json.dumps(EDITS))]
    stage.launcher.models._anthropic_http = hub.factory
    assert isinstance(hub.factory(), httpx2.AsyncClient)
    run = await _to_the_model(stage, _editing("anthropic", model="claude-opus-5-5"))
    stage.door.answers = [_found("a.ts"), _read("a.ts")]
    await stage.launcher.tick()
    stage.door.answers = [_applied("a.ts")]
    await _answered(stage)
    data = stage.run(run["id"])["data"]
    assert data["ran"]["apply"] == "anthropic"
    review = stage.run(run["id"])["steps"][-1]
    assert (review["name"], review["provider"]) == ("review", "chatgpt")
    await stage.http.aclose()
    await stage.github.aclose()


@pytest.mark.asyncio
async def test_edits_that_do_not_fit_go_back_to_the_model_with_why(stage: Any) -> None:
    from tests.test_workbench import _found

    second = {**EDITS, "summary": "Rounds half a cent up, this time for real."}
    nothing = {"summary": "The file with the rounding was not among the ones given.", "edits": []}
    stage.provider.answers = [said(EDITS), said(second)]
    run = await _to_the_model(stage, _editing())
    stage.door.answers = [
        _found("packages/billing/src/money.ts"),
        _read("packages/billing/src/money.ts"),
    ]
    await stage.launcher.tick()
    stage.door.answers = [
        _refused(
            (
                "packages/billing/src/money.ts",
                "the text to replace is not in the file as it is now",
            ),
            ("packages/billing/src/x.ts", "there is no such file: write it whole instead"),
        )
    ]
    await _answered(stage)
    apply = _steps(stage, run["id"])["apply"]
    assert (apply["state"], apply["outcome"]) == ("succeeded", "refused")
    assert apply["detail"].startswith("2 of 2 edits do not fit, so nothing was changed: ")
    data = stage.run(run["id"])["data"]
    assert "head" not in data or data["head"] == data.get("base_sha"), "nothing was committed"
    # The model is asked again, in the next round, and told what did not fit.
    again = stage.run(run["id"])["steps"][-1]
    assert (again["name"], again["input"]["round"]) == ("write", 2)
    assert again["input"]["refused"] == [
        "packages/billing/src/money.ts (edit 1, replace): the text to replace is not in the file as it is now",
        "packages/billing/src/x.ts (edit 2, replace): there is no such file: write it whole instead",
    ]
    stage.door.answers = [_applied("packages/billing/src/money.ts")]
    await _answered(stage)
    question = stage.provider.asked[-1][2]["messages"][1]["content"]
    assert "Your edits could not be applied, and nothing was changed. Why:" in question
    assert "- packages/billing/src/x.ts (edit 2, replace): there is no such file" in question
    assert "Write the whole change again, with these put right." in question
    assert [
        step["outcome"] for step in stage.run(run["id"])["steps"] if step["name"] == "apply"
    ] == ["refused", "applied"]

    # A model that writes no edits says why, and the flow ends there when it draws no way on.
    stage.runs.request_cancel(UUID(run["id"]))
    await stage.launcher.tick()
    stage.fake.pods.clear()
    stage.provider.answers = [said(nothing)]
    empty = await _to_the_model(stage, _editing())
    stage.door.handed.clear()
    stage.door.done.clear()
    stage.door.answers = [_found("a.ts"), _read("a.ts")]
    await stage.launcher.tick()
    await _answered(stage)
    wrote = _steps(stage, empty["id"])["write"]
    assert (wrote["state"], wrote["outcome"]) == ("succeeded", "no_edits")
    assert (
        wrote["detail"]
        == "The model wrote no edits: The file with the rounding was not among the ones given."
    )
    ended = stage.run(empty["id"])
    assert (ended["state"], ended["outcome"]) == ("succeeded", "no_edits")
    await stage.http.aclose()
    await stage.github.aclose()


# --- a run, with the real program, the real tools and a real repository ------------------------


@pytest.mark.asyncio
async def test_a_models_edits_change_a_real_checkout_and_the_platform_commits(
    stage: Any,
    repo: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deliver, "REPO", str(repo))
    monkeypatch.setattr(bench_tasks, "_FOLDER", str(tmp_path / "jobs"))
    monkeypatch.setattr(bench_tasks, "_ANSWERS", str(tmp_path / "answers"))
    base = (
        subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, check=True)
        .stdout.decode()
        .strip()
    )
    started: list[Bench] = []
    create = stage.fake.__call__

    def cluster(request: httpx.Request) -> httpx.Response:
        answer = create(request)
        if request.method == "POST" and request.url.path.endswith("/pods"):
            body = json.loads(request.content)
            if body["metadata"]["labels"].get(pods.BENCH_LABEL) == "true":
                job = json.loads(body["spec"]["containers"][0]["env"][0]["value"])
                bench = Bench(
                    tmp_path,
                    token=job["commands"][0]["env"]["BENCH_TOKEN"],
                    BENCH_REPO=str(repo),
                    BENCH_INDEX=str(tmp_path / "cache" / "index.json"),
                )
                started.append(bench)
                monkeypatch.setattr(pods, "BENCH_PORT", bench.port)
                stage.fake.pods[body["metadata"]["name"]]["status"] = {
                    "phase": "Running",
                    "podIP": "127.0.0.1",
                }
        return answer

    stage.launcher.cluster._client._transport = httpx.MockTransport(cluster)
    # The launcher talks to the real program from here on.
    stage.launcher.workbench = workbench.Workbench(
        stage.launcher.cluster, stage.launcher._sandboxes, b"k" * 48
    )
    bad = {
        "summary": "A first try.",
        "edits": [
            EDITS["edits"][0],
            _edit("packages/billing/src/invoice.ts", find="no such text", replace="x"),
        ],
    }
    stage.provider.answers = [said(bad), said(EDITS)]
    raw = _editing()
    by_id = {task["id"]: task for task in raw["tasks"]}
    by_id["find"]["with"] = {"words": "roundCents money rounding", "limit": 4}
    try:
        flows = FlowRecords(stage.sessions)
        flows.save_draft("deliver-issue", raw)
        flows.publish("deliver-issue")
        run = stage.runs.start_delivery(stage.item_id, toolchain=stage.toolchain)
        await stage.step("prepare", _prepared())
        # The run was cloned elsewhere in this test's world: it starts from the fixture.
        stage.runs.merge_data(UUID(run["id"]), {"base_sha": base})
        await stage.launcher.tick()  # the workbench starts
        await stage.launcher.tick()  # find, read; the model is asked
        assert "packages/billing/src/money.ts" in stage.run(run["id"])["data"]["found"]
        given = stage.runs.step_outputs(UUID(run["id"]), "read")[-1]["files"]
        assert any(entry["text"].startswith("export const roundCents") for entry in given)
        await _answered(stage)  # the first edits: one does not fit, nothing is written
        first = [step for step in stage.run(run["id"])["steps"] if step["name"] == "apply"][0]
        assert (
            first["outcome"] == "refused"
            and "packages/billing/src/invoice.ts (edit 2, replace)" in first["detail"]
        )
        assert (
            _status(repo) == ""
            and "Math.round" in (repo / "packages/billing/src/money.ts").read_text()
        )
        await _answered(stage)  # the second edits fit: applied and committed
        steps_now = _steps(stage, run["id"])
        assert (steps_now["apply"]["state"], steps_now["apply"]["outcome"]) == (
            "succeeded",
            "applied",
        ), steps_now["apply"]
        assert (
            (repo / "packages/billing/src/money.ts")
            .read_text()
            .endswith("Math.floor(value + 0.5);\n")
        )
        assert (repo / "packages/billing/src/money.test.ts").exists()
        log = (
            subprocess.run(
                ["git", "log", "-1", "--format=%an|%s|%H"],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        author, subject, head = log.split("|")
        assert (
            author == "Engineering Control" and subject == "Signing in fails with an expired cookie"
        )
        data = stage.run(run["id"])["data"]
        assert data["head"] == head != base and _status(repo) == ""
        assert data["touched"] == {
            "count": 2,
            "paths": ["packages/billing/src/money.test.ts", "packages/billing/src/money.ts"],
        }
        assert steps.parse_result(stage.run(run["id"])["steps"][-2]["log"]) is not None or True
    finally:
        for bench in started:
            bench.stop()
    await stage.http.aclose()
    await stage.github.aclose()
