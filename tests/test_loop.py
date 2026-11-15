"""The loop: a model chooses the next step, the platform carries it out, and the model is
asked again with what came of it, until it says done."""

# Long expected texts and fixed paths of the sandbox come with what is tested here.
# ruff: noqa: E501, S105, S108

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any
from uuid import UUID

import pytest

from engineering_control.engine import asking, validate
from engineering_control.engine.builtin import BUILTIN
from engineering_control.engine.catalogue import BUDGETS, TASKS, WAYS, loop_steps
from engineering_control.engine.document import parse
from engineering_control.engine.records import FlowRecords
from engineering_control.engine.routing import follow
from engineering_control.engine.simulate import simulate
from tests.test_model_edits import EDITS, _refused, stage  # noqa: F401 - ``stage`` is a fixture
from tests.test_models_api import said
from tests.test_sandbox_delivery import (  # noqa: F401 - ``world`` is a fixture
    HEAD,
    _log,
    _prepared,
    _ran,
    world,
)
from tests.test_workbench import _bench_pod, _found, _read, _running, _tool_log

MODEL = {"provider": "deepseek", "model": "model-a"}


def _looping(**settings: Any) -> dict[str, Any]:
    """Deliver issue in which a model works in a loop: it searches, reads, has edits
    written and applied, and checks, until it says done."""
    raw = copy.deepcopy(BUILTIN["deliver-issue"].as_dict())
    tasks = {task["id"]: task for task in raw["tasks"]}
    tasks["tests"]["next"] = {"passed": "decide", "failed": "decide"}
    tasks["review"]["with"] = {**tasks["review"].get("with", {}), "other_than": "apply"}
    tasks["review"]["next"] = {"pass": "verify", "changes": "decide"}
    tasks["verify"]["next"] = {**tasks["verify"]["next"], "regression": "decide"}
    raw["tasks"] = [task for task in raw["tasks"] if task["id"] != "implement"]
    ways = {"search": "find", "read": "read", "edit": "write", "check": "look", "done": "review"}
    for way in settings.pop("without", ()):
        ways.pop(way)
    raw["tasks"] += [
        {
            "id": "decide",
            "task": "model.choose",
            "with": {**MODEL, "instruction": "Resolve the issue.", **settings},
            "next": ways,
        },
        {
            "id": "find",
            "task": "code.find",
            "with": {},
            "next": {"found": "decide", "none": "decide"},
        },
        {"id": "read", "task": "code.read", "with": {}, "next": {"ok": "decide"}},
        {
            "id": "write",
            "task": "model.edit",
            "with": {**MODEL, "model": "model-strong", "instruction": "Write the change."},
            "next": {"edits": "apply", "nothing": "decide"},
        },
        {
            "id": "apply",
            "task": "code.apply",
            "with": {},
            "next": {"applied": "decide", "refused": "decide"},
        },
        {
            "id": "look",
            "task": "code.run",
            "with": {"commands": ["pnpm lint", "pnpm test"]},
            "next": {"passed": "decide", "failed": "decide"},
        },
    ]
    return raw


def _rules(raw: dict[str, Any]) -> list[str]:
    return [problem["text"] for problem in validate.errors(parse(raw))]


def _choice(way: str, why: str, **more: Any) -> dict[str, Any]:
    return {
        "way": way,
        "why": why,
        "plan": [],
        "notes": "",
        "words": "",
        "paths": [],
        "command": 0,
        **more,
    }


# --- what it is ------------------------------------------------------------------------------


def test_a_task_lets_a_model_choose_the_way_and_coming_back_to_it_is_no_round() -> None:
    choose = TASKS["model.choose"]
    assert (choose.where, choose.group, choose.loops) == ("model", "Models", True)
    assert choose.outcomes == WAYS == ("done", "search", "read", "edit", "check", "other")
    assert not (choose.account or choose.writes or choose.publishes or choose.approval)
    names = [param.name for param in choose.params]
    assert "steps" in names and "with_change" not in names and "instruction" in names
    assert [key for key, task in TASKS.items() if task.loops] == ["model.choose"]
    assert (
        loop_steps({}),
        loop_steps({"steps": 7}),
        loop_steps({"steps": 5000}),
        loop_steps({"steps": "x"}),
    ) == (40, 7, 200, 40)
    assert BUDGETS["dollars"].default is None and (
        BUDGETS["dollars"].low,
        BUDGETS["dollars"].high,
    ) == (1, 1000)

    assert _rules(_looping()) == []
    assert _rules(_looping(steps=120, then_provider="kimi", then_model="model-b")) == []
    for wrong, why in (
        ({"steps": 1}, "steps of decide is between 2 and 200"),
        ({"model": ""}, "decide does not name the model it asks"),
        ({"without": ("search", "read", "edit", "check")}, "decide offers its model nothing to do"),
    ):
        assert any(why in text for text in _rules(_looping(**wrong))), (
            wrong,
            _rules(_looping(**wrong)),
        )
    # What the loop changes is a change like any other: without the review it is refused.
    raw = _looping()
    by_id = {task["id"]: task for task in raw["tasks"]}
    by_id["decide"]["next"]["done"] = "verify"
    assert any("review" in text for text in _rules(raw))
    budgeted = _looping()
    budgeted["budgets"] = {"dollars": 5000}
    assert any("dollars" in text for text in _rules(budgeted))

    # On paper the model says done at once unless told otherwise; told, it goes round.
    usual = [step["task"] for step in simulate(parse(_looping()))["steps"]]
    assert usual[usual.index("tests") :][:3] == ["tests", "decide", "review"]
    ways = ["search", "read", "edit", "check", "edit", "check", "done"]
    round_trip = simulate(parse(_looping()), outcomes={"decide": ways})
    walked = [step["task"] for step in round_trip["steps"]]
    assert walked[walked.index("decide") :][:16] == [
        "decide", "find", "decide", "read", "decide", "write", "apply", "decide", "look",
        "decide", "write", "apply", "decide", "look", "decide", "review",
    ]  # fmt: skip
    assert round_trip["totals"]["rounds"] == 1, "going round the loop uses up no round"
    short = simulate(parse(_looping(steps=2)), outcomes={"decide": ["search", "search", "search"]})
    assert (
        short["ends"]["how"] == "failed"
        and "once more than its loop allows" in short["ends"]["text"]
    )

    # The engine's own count: every time the task is come to, and never a round.
    document = parse(_looping(steps=3))
    find, decide = document.node("find"), document.node("decide")
    assert find is not None and decide is not None
    first = follow(
        document,
        find,
        "found",
        seen=["prepare", "decide", "find"],
        each={},
        round_number=1,
        known={},
        rounds=3,
        loops={"decide": 1},
    )
    assert (first.kind, first.target, first.round, first.again) == ("sandbox", "decide", 1, False)
    assert first.loops == {"decide": 2} and first.seen == ("prepare",), (
        "what the loop holds is gone through afresh"
    )
    third = follow(
        document,
        find,
        "found",
        seen=["decide", "find"],
        each={},
        round_number=1,
        known={},
        rounds=3,
        loops={"decide": 3},
    )
    assert (third.kind, third.target) == ("loop_limit", "decide")


def test_the_files_in_hand_are_the_latest_seen_and_the_oldest_are_let_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def read(*files: tuple[str, str]) -> dict[str, Any]:
        return {
            "state": "succeeded",
            "output": {"files": [{"path": path, "text": text} for path, text in files]},
        }

    happened = [
        read(("a.ts", "a one"), ("b.ts", "b one")),
        {
            "state": "succeeded",
            "output": {"files": [{"path": "found.ts", "score": 3}]},
        },  # found, not read
        {"state": "failed", "output": {"files": [{"path": "c.ts", "text": "never"}]}},
        read(("a.ts", "a two")),
        {
            "state": "succeeded",
            "output": {
                "applied": [{"path": "b.ts", "action": "deleted"}],
                "files": [{"path": "d.ts", "text": "d new"}],
            },
        },
    ]
    held, let_go = asking.in_hand(happened)
    assert [(entry["path"], entry["text"]) for entry in held] == [
        ("a.ts", "a two"),
        ("d.ts", "d new"),
    ]
    assert let_go == []
    monkeypatch.setattr(asking, "HAND_CHARACTERS", 8)
    held, let_go = asking.in_hand(
        [read(("old.ts", "12345"), ("mid.ts", "12345"), ("new.ts", "12345"))]
    )
    assert [entry["path"] for entry in held] == ["new.ts"] and let_go == ["old.ts", "mid.ts"]
    # One file that is too much by itself is kept: there is nothing older to let go.
    assert asking.in_hand([read(("big.ts", "x" * 50))])[1] == []


# --- a run -----------------------------------------------------------------------------------


async def _into_the_loop(stage: Any, raw: dict[str, Any]) -> dict[str, Any]:  # noqa: F811
    flows = FlowRecords(stage.sessions)
    in_force = flows.document("deliver-issue", flows.version_in_force("deliver-issue") or 0)
    if in_force is None or in_force.as_dict() != parse(raw).as_dict():
        flows.save_draft("deliver-issue", raw)
        flows.publish("deliver-issue")
    run: dict[str, Any] = stage.runs.start_delivery(stage.item_id, toolchain=stage.toolchain)
    await stage.step("prepare", _prepared())
    return run


async def _answered(stage: Any) -> None:  # noqa: F811
    calls = list(stage.launcher._calls.values())
    if calls:
        await asyncio.wait(calls, timeout=10)
    await stage.launcher.tick()
    pod = _bench_pod(stage, stage.runs.listing()["items"][0]["id"])
    if pod is not None and pod.get("status", {}).get("phase") == "Pending":
        _running(pod)
        await stage.launcher.tick()


def _applied_with(path: str, text: str) -> str:
    answer = {
        "applied": [{"path": path, "action": "changed"}],
        "refused": [],
        "edits": 2,
        "files": [{"path": path, "text": text, "lines": 1, "shown": 1, "cut": False}],
    }
    return _tool_log("apply", answer).replace(
        _log([_ran("apply", "{}")]),
        _log([_ran("apply", "{}"), _ran("commit", f"{HEAD}\n"), _ran("touched", f"1\n{path}")]),
    )


def _names(stage: Any, run_id: str) -> list[str]:  # noqa: F811
    return [step["name"] for step in stage.run(run_id)["steps"]]


def _asked_of(stage: Any, model: str) -> list[str]:  # noqa: F811
    """The questions put to that model, in the order they were put."""
    return [
        body["messages"][1]["content"]
        for method, _url, body, _key in stage.provider.asked
        if method == "POST" and body["model"] == model
    ]


@pytest.mark.asyncio
async def test_a_model_works_through_a_loop_until_it_says_done(stage: Any) -> None:  # noqa: F811
    plan = [
        {"step": "Find the cookie check", "state": "done"},
        {"step": "Fix the expiry", "state": "doing"},
    ]
    stage.provider.answers = [
        said(
            _choice(
                "search",
                "I need to find where the cookie is checked.",
                words="expired cookie session",
                plan=[{"step": "Find the cookie check", "state": "doing"}],
                notes="Nothing known yet.",
            )
        ),
        said(
            _choice(
                "read",
                "The session module is where it is checked.",
                paths=["src/session.ts"],
                plan=plan,
                notes="The check is in src/session.ts.",
            )
        ),
        said(
            _choice(
                "edit",
                "Compare the expiry with now in isValid of src/session.ts.",
                plan=plan,
                notes="The check is in src/session.ts.",
            )
        ),
        said(EDITS),
        said(
            _choice(
                "check",
                "See whether the tests pass.",
                command=2,
                plan=plan,
                notes="Changed isValid.",
            )
        ),
        said(
            _choice(
                "edit",
                "The test expects 60 seconds: use seconds, not milliseconds.",
                plan=plan,
                notes="Changed isValid; unit is seconds.",
            )
        ),
        said(EDITS),
        said(_choice("check", "Run the tests again.", command=2, plan=plan, notes="Unit fixed.")),
        said(
            _choice(
                "done",
                "The tests pass.",
                plan=[{**step, "state": "done"} for step in plan],
                notes="Done.",
            )
        ),
    ]
    stage.door.answers = [
        _found("src/session.ts", "src/cookie.ts"),
        _read("src/session.ts"),
        _applied_with("src/session.ts", "export const isValid = (c) => c.expires > Date.now();"),
        _log([_ran("check 1", exit_code=1, stderr="AssertionError: expected 60 to equal 60000")]),
        _applied_with(
            "src/session.ts", "export const isValid = (c) => c.expires > Date.now() / 1000;"
        ),
        _log([_ran("check 1")]),
    ]
    run = await _into_the_loop(stage, _looping())
    for _call in range(12):
        await _answered(stage)
        if "review" in _names(stage, run["id"]):
            break
    names = _names(stage, run["id"])
    assert names[names.index("decide") :] == [
        "decide", "find", "decide", "read", "decide", "write", "apply", "decide", "look",
        "decide", "write", "apply", "decide", "look", "decide", "review",
    ]  # fmt: skip
    steps = stage.run(run["id"])["steps"]
    by_name: dict[str, list[dict[str, Any]]] = {}
    for step in steps:
        by_name.setdefault(step["name"], []).append(step)
    # Going round the loop is no round, and how often the model was asked is counted.
    assert {step["input"].get("round") for step in steps if step["input"]} <= {1, None}
    assert [step["input"]["loops"]["decide"] for step in by_name["decide"]] == [1, 2, 3, 4, 5, 6, 7]
    assert [step["outcome"] for step in by_name["decide"]] == [
        "chose_search", "chose_read", "chose_edit", "chose_check", "chose_edit", "chose_check", "chose_done",
    ]  # fmt: skip
    assert by_name["decide"][0]["detail"] == "search: I need to find where the cookie is checked."

    # Each tool was given what the model asked for, and nothing it did not.
    jobs = [job for _key, job in stage.door.handed]
    assert json.loads(jobs[0]["files"][1]["content"])["text"] == "expired cookie session"
    assert json.loads(jobs[1]["files"][1]["content"])["paths"] == ["src/session.ts"]
    assert [command["argv"] for command in jobs[3]["commands"]] == [["pnpm", "test"]], (
        "the second of its commands"
    )
    assert by_name["look"][0]["detail"] == "1 of 1 command(s) failed: pnpm test"

    choices, edits = _asked_of(stage, "model-a"), _asked_of(stage, "model-strong")
    assert len(choices) == 7 and len(edits) == 2
    first = choices[0]
    assert "You are doing a piece of work step by step." in first
    assert "What you are asked:\nResolve the issue." in first
    # The ways it is offered are the ones the flow leads somewhere, each said in words.
    assert (
        "The ways you can go:\n- done: the work that was asked for is finished, and goes on to Review the change."
        in first
    )
    assert "- search: look for files of the repository by words. Give the words in words" in first
    assert "- read: read files, to have their text in hand. Give their paths in paths" in first
    assert "- edit: have the change written as edits and applied to the files." in first
    assert "Give its number in command, or 0 for all: 1. pnpm lint; 2. pnpm test." in first
    assert "- other:" not in first, "a way that leads nowhere is not offered"
    assert all(f'"{way}"' in first for way in ("done", "search", "read", "edit", "check"))
    assert '"other"' not in first, "nor can it be answered"
    assert "This is step 1 of at most 40." in first
    assert (
        "Your plan:\n(none yet: make one)" in first
        and "Nothing yet: this is the first step." in first
    )
    assert "No change has been collected" not in first

    # What it wrote down is given back to it, with what happened and what is in hand.
    third = choices[2]
    assert "This is step 3 of at most 40." in third
    assert "Your plan:\n- [done] Find the cookie check\n- [doing] Fix the expiry" in third
    assert "Your notes:\n<<<material\nThe check is in src/session.ts.\nmaterial>>>" in third
    assert (
        "What happened so far:\n"
        "1. decide chose search: I need to find where the cookie is checked.\n"
        "2. find (found): 2 files: src/session.ts, src/cookie.ts. The map has 80 files of code and 210 imports between them.\n"
        "3. decide chose read: The session module is where it is checked.\n"
        "4. read (read): 1 files, "
    ) in third
    assert (
        "The file src/session.ts:\n<<<material\n// src/session.ts\nexport const value = 1;\nmaterial>>>"
        in third
    )
    # After a change, what is in hand is the file as it is now, not as it was read.
    fourth = choices[3]
    assert "export const isValid = (c) => c.expires > Date.now();" in fourth
    assert "export const value = 1;" not in fourth
    assert "6. write (edits_written): 2 edits to 2 files: Rounds half a cent up." in fourth
    assert "7. apply (applied): 1 files changed: src/session.ts" in fourth
    # What a check printed is put before it, once.
    fifth = choices[4]
    assert "What the last check printed:\n<<<material\npnpm test\n" in fifth
    assert "AssertionError: expected 60 to equal 60000" in fifth
    assert "What the last check printed" not in choices[5]
    # Whoever writes the edits is told what the change is to be, and why the last ones failed.
    assert (
        "What is to be changed, as the one who chose this step put it:\n<<<material\nCompare the expiry with now in isValid of src/session.ts."
        in edits[0]
    )
    assert (
        "The test expects 60 seconds" in edits[1]
        and "AssertionError: expected 60 to equal 60000" in edits[1]
    )
    assert "export const isValid = (c) => c.expires > Date.now();" in edits[1]

    # The plan and the notes stay with the run, and each choice with its step.
    data = stage.run(run["id"])["data"]
    assert data["loop"]["decide"] == {
        "plan": [{**step, "state": "done"} for step in plan],
        "notes": "Done.",
    }
    assert by_name["decide"][1]["output"]["plan"] == plan and by_name["decide"][1]["output"][
        "paths"
    ] == ["src/session.ts"]
    # What was asked counts like any other use. Sandboxes of their own were started for the
    # clone, the installation, the tests and the review, and none for the loop.
    usage = stage.run(run["id"])["usage"]
    assert (usage["asked"], usage["tokens"], usage["sandboxes"]) == (9, 9 * 1280, 4), usage
    # Done leads on, to the review of what was changed, by an agent of a subscription.
    review = by_name["review"][0]
    assert review["state"] in ("pending", "running") and review["input"]["round"] == 1
    await stage.http.aclose()
    await stage.github.aclose()


@pytest.mark.asyncio
async def test_a_loop_ends_at_its_limit_of_steps_or_of_dollars_and_tells_of_refused_edits(
    stage: Any,  # noqa: F811
) -> None:
    # Two steps at most: the third time the model would be asked, the run gives up.
    stage.provider.answers = [
        said(_choice("search", "Look.", words="cookie")),
        said(_choice("search", "Look again.", words="session")),
    ]
    stage.door.answers = [_found("src/session.ts"), _found("src/cookie.ts")]
    run = await _into_the_loop(stage, _looping(steps=2))
    for _call in range(4):
        await _answered(stage)
    ended = stage.run(run["id"])
    assert (ended["state"], ended["outcome"]) == ("failed", "loop_limit")
    assert ended["detail"] == (
        "decide was asked for the next step 2 times, which is as often as its flow allows, "
        "and the work was not done."
    )
    second = _asked_of(stage, "model-a")[1]
    assert "This is step 2 of at most 2. It is the last time you are asked" in second

    # A run whose questions cost more than its flow allows starts no further task.
    stage.fake.pods.clear()
    stage.door.handed.clear()
    stage.door.done.clear()
    account = next(a for a in stage.accounts.listing()["items"] if a["provider"] == "deepseek")
    stage.accounts.update(UUID(account["id"]), prices={"model-a": {"input": 900, "output": 900}})
    costly = _looping()
    costly["budgets"] = {"dollars": 1}
    stage.provider.answers = [
        said(_choice("search", "Look.", words="cookie")),
        said(_choice("read", "Read.", paths=["a.ts"])),
    ]
    stage.door.answers = [_found("a.ts")]
    spent = await _into_the_loop(stage, costly)
    for _call in range(4):
        await _answered(stage)
    over = stage.run(spent["id"])
    assert (over["state"], over["outcome"]) == ("failed", "over_budget")
    assert over["detail"].startswith(
        "The run's questions to models have cost $1.15; its flow allows $1."
    )

    # Edits that do not fit come back to the model that chose, and go on to the writer.
    stage.fake.pods.clear()
    stage.door.handed.clear()
    stage.door.done.clear()
    stage.accounts.update(UUID(account["id"]), prices={})
    stage.provider.answers = [
        said(_choice("edit", "Fix the expiry.")),
        said(EDITS),
        said(_choice("edit", "Try again with the exact text.")),
        said({"summary": "Nothing to change.", "edits": []}),
        said(_choice("done", "It cannot be done.")),
    ]
    stage.door.answers = [
        _refused(("src/session.ts", "the text to replace is not in the file as it is now"))
    ]
    again = await _into_the_loop(stage, _looping())
    for _call in range(8):
        await _answered(stage)
        if "review" in _names(stage, again["id"]):
            break
    told = _asked_of(stage, "model-a")[-2]
    assert (
        "The edits could not be applied, and nothing was changed. Why:\n- src/session.ts (edit 1, replace): the text to replace is not in the file as it is now"
        in told
    )
    writer = _asked_of(stage, "model-strong")[-1]
    assert "Your edits could not be applied, and nothing was changed." in writer
    assert "Try again with the exact text." in writer
    names = _names(stage, again["id"])
    assert names[names.index("decide") :] == [
        "decide",
        "write",
        "apply",
        "decide",
        "write",
        "decide",
        "review",
    ]
    await stage.http.aclose()
    await stage.github.aclose()
