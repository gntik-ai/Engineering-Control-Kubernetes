"""The If of a flow: what it can ask, what the rules say about it, and how a run goes by it."""

from __future__ import annotations

import copy
import json
from typing import Any
from uuid import UUID

import pytest

from engineering_control.engine import conditions, validate
from engineering_control.engine.builtin import BUILTIN
from engineering_control.engine.catalogue import TASKS, catalogue
from engineering_control.engine.document import parse
from engineering_control.engine.records import FlowRecords
from engineering_control.engine.simulate import simulate
from engineering_control.sandbox import deliver
from tests.test_sandbox_delivery import (  # noqa: F401 - ``world`` is a fixture
    HEAD,
    MAKER_DONE,
    _claude,
    _log,
    _prepared,
    _ran,
    _reviewed,
    world,
)


def _test(fact: str, how: str, value: Any = None, **more: Any) -> dict[str, Any]:
    return {"fact": fact, "is": how, **({} if value is None else {"value": value}), **more}


def _if(*tests: dict[str, Any], match: str = "all") -> dict[str, Any]:
    return {"match": match, "tests": list(tests)}


def _holds(
    data: dict[str, Any], *tests: dict[str, Any], match: str = "all", round_number: int = 1
) -> bool:
    condition = _if(*tests, match=match)
    assert conditions.problems(condition) == [], conditions.problems(condition)
    return conditions.holds(condition, data, round_number=round_number)


def _docs_first() -> dict[str, Any]:
    """Deliver issue with an If after the implementation: a change to the documentation
    alone goes straight to review, any other is tested first."""
    raw = copy.deepcopy(BUILTIN["deliver-issue"].as_dict())
    tasks = {task["id"]: task for task in raw["tasks"]}
    tasks["implement"]["next"] = {"done": "docs"}
    raw["tasks"] += [
        {
            "id": "docs",
            "task": "control.when",
            "with": {"condition": _if(_test("change.paths", "all_match", "docs/**, *.md"))},
            "next": {"yes": "review", "no": "early"},
        },
        {
            "id": "early",
            "task": "check.change",
            "next": {"ok": "review", "regression": "implement"},
        },
    ]
    return raw


def _rules(raw: dict[str, Any]) -> set[str]:
    return {problem["rule"] for problem in validate.errors(parse(raw))}


def _path(result: dict[str, Any]) -> list[str]:
    """The way a simulation went, from the clone on, without the installation and the
    tests before any change, which every one of these goes through."""
    return [
        f"{step['task']}:{step['outcome']}"
        for step in result["steps"]
        if step["task"] not in ("install", "tests")
    ]


# --- what is asked ------------------------------------------------------------------------


def test_an_if_is_a_task_that_only_asks() -> None:
    task = TASKS["control.when"]
    assert (task.title, task.where, task.outcomes) == ("If", "control", ("yes", "no"))
    assert not task.account and not task.writes and not task.publishes
    said = next(entry for entry in catalogue() if entry["key"] == "control.when")
    assert [param["kind"] for param in said["params"]] == ["condition"]
    shown = conditions.catalogue()
    # Every fact is compared in ways its kind has, and says what it is.
    for fact in shown["facts"]:
        assert fact["title"] and fact["doc"] and shown["operators"][fact["kind"]]
    assert {"item.labels", "change.paths", "round", "outcome", "project"} <= {
        fact["key"] for fact in shown["facts"]
    }


def test_what_a_flow_said_before_conditions_had_a_shape_still_means_the_same() -> None:
    # The platform's own audit says it the old way, and passes every rule.
    assert validate.errors(BUILTIN["audit-code"]) == []
    assert conditions.normal("scope_given") == _if(_test("scope", "yes"))
    assert conditions.holds("scope_given", {"scope": {"text": "the login page"}})
    assert not conditions.holds("scope_given", {"scope": {"title": " ", "text": ""}})
    assert not conditions.holds(None, {})
    assert conditions.words("scope_given") == "The run was told where to look"


def test_each_kind_of_fact_is_compared_in_its_own_ways() -> None:
    data = {
        "project": "falcone-workflow",
        "issue": {"number": 11, "title": "Signing in FAILS", "body": "With an expired cookie."},
        "item": {
            "labels": ["Bug", "area: auth"],
            "by": "github",
            "kind": "finding",
            "severity": "high",
        },
        "toolchain": {"tests": [{"argv": ["pnpm", "test"]}]},
        "baseline": {"1": 0, "2": 1},
        "maker": "chatgpt",
        "touched": {"count": 3, "paths": ["docs/guide/a.md", "README.md", "src/app/login.ts"]},
        "areas": [{}, {}],
        "went": {"review": "changes"},
    }
    # Yes and no.
    assert _holds(data, _test("item.issue", "yes")) and not _holds(data, _test("scope", "yes"))
    assert _holds(data, _test("project.tests", "yes"), _test("tests.failed_before", "yes"))
    assert not _holds({"baseline": {"1": 0}}, _test("tests.failed_before", "yes"))
    # Texts, whatever their capitals.
    assert _holds(data, _test("item.title", "contains", "fails"))
    assert _holds(data, _test("item.text", "contains", "expired COOKIE"))
    assert _holds(data, _test("item.title", "does_not_contain", "cookie"))
    assert _holds(data, _test("project", "is", "Falcone-Workflow"))
    assert not _holds(data, _test("project", "is_not", "falcone-workflow"))
    # Lists.
    assert _holds(data, _test("item.labels", "includes", "bug"))
    assert _holds(data, _test("item.labels", "does_not_include", "auth"))
    assert _holds({}, _test("item.labels", "is_empty")) and _holds(
        data, _test("item.labels", "is_not_empty")
    )
    # Choices, and choices with an order.
    assert _holds(data, _test("item.by", "is", "github"), _test("item.kind", "is_not", "clean-up"))
    assert _holds(data, _test("change.by", "is", "chatgpt"))
    assert _holds(data, _test("item.severity", "at_least", "medium"))
    assert not _holds(data, _test("item.severity", "at_least", "critical"))
    assert _holds(data, _test("item.severity", "at_most", "high"))
    # An item that is not a finding has no severity: only "is not" holds.
    assert not _holds({}, _test("item.severity", "at_most", "critical"))
    assert _holds({}, _test("item.severity", "is_not", "low"))
    # Numbers.
    assert _holds(data, _test("change.files", "is", 3), _test("areas", "more_than", 1))
    assert _holds(data, _test("change.files", "at_most", 3)) and not _holds(
        data, _test("change.files", "less_than", 3)
    )
    assert _holds(data, _test("round", "at_least", 2), round_number=2)
    assert not _holds(data, _test("round", "at_least", 2))
    # How a task ended; one that has not run did not end with anything.
    assert _holds(data, _test("outcome", "is", "changes", of="review"))
    assert not _holds(data, _test("outcome", "is", "ok", of="verify"))
    assert _holds(data, _test("outcome", "is_not", "ok", of="verify"))
    # All of them, or any.
    both = (_test("item.labels", "includes", "bug"), _test("project", "is", "another"))
    assert not _holds(data, *both) and _holds(data, *both, match="any")


def test_the_files_a_change_touches_are_compared_with_patterns() -> None:
    def touches(paths: list[str], how: str, value: str) -> bool:
        return _holds(
            {"touched": {"count": len(paths), "paths": paths}}, _test("change.paths", how, value)
        )

    docs = ["docs/guide/a.md", "README.md"]
    mixed = [*docs, "src/app/login.ts"]
    assert touches(docs, "all_match", "docs/**, *.md") and not touches(
        mixed, "all_match", "docs/**, *.md"
    )
    assert touches(mixed, "any_match", "src/") and touches(docs, "none_match", "src/")
    # A star stays within a folder; two cross folders; no slash is a file name anywhere.
    assert touches(["src/a.ts"], "all_match", "src/*.ts") and not touches(
        ["src/app/a.ts"], "all_match", "src/*.ts"
    )
    assert touches(["src/app/deep/a.ts"], "all_match", "src/**/*.ts") and touches(
        ["src/a.ts"], "all_match", "src/**/*.ts"
    )
    assert touches(["a/b/package.json"], "any_match", "package.json")
    assert not touches(["a/b/package.json.bak"], "any_match", "package.json")
    assert touches(["web/x.test.tsx"], "all_match", "*.test.ts?") and not touches(
        ["a.b"], "any_match", "a?b/"
    )
    # A dot is a dot, not any character.
    assert not touches(["READMExmd"], "any_match", "README.md")
    # A change that touches nothing does not "all match", and has none that matches.
    assert not touches([], "all_match", "**") and touches([], "none_match", "**")
    # What was collected is gone by when the implementation left no list.
    collected = {"change": {"files": [{"path": "docs/a.md", "status": "M"}]}}
    assert _holds(
        collected, _test("change.paths", "all_match", "docs/"), _test("change.files", "is", 1)
    )


def test_a_condition_reads_as_a_sentence() -> None:
    said = _if(
        _test("change.paths", "all_match", "docs/**"),
        _test("round", "at_most", 1),
        _test("outcome", "is_not", "pass", of="review"),
        _test("item.issue", "no"),
        match="any",
    )
    assert conditions.words(said) == (
        "The files the change touches all match docs/** or the round is at most 1 or "
        "review did not end with pass or the item is not an issue on GitHub"
    )
    assert conditions.words({"tests": []}) == "Nothing is asked"


def test_a_condition_that_cannot_be_read_is_refused() -> None:
    for wrong in (
        "anything",
        {"tests": []},
        {"tests": [_test("scope", "yes")] * 7},
        {"match": "most", "tests": [_test("scope", "yes")]},
        {"tests": [_test("scope", "yes")], "code": "1 == 1"},
        _if(_test("the weather", "is", "fine")),
        _if(_test("round", "contains", "2")),
        _if(_test("round", "at_least", "2")),
        _if(_test("round", "at_least", -1)),
        _if(_test("round", "at_least", True)),
        _if(_test("item.title", "contains", "")),
        _if(_test("item.title", "contains", "x" * 201)),
        _if(_test("item.by", "is", "somebody")),
        _if(_test("item.severity", "at_least", "enormous")),
        _if(_test("change.paths", "any_match", " , ")),
        _if(_test("change.paths", "any_match", "*a*b*c*d*e*f*g")),
        _if(_test("change.paths", "any_match", ", ".join(f"p{n}" for n in range(11)))),
        _if(_test("outcome", "is", "ok")),
        _if(_test("scope", "yes", "now")),
        _if(_test("round", "is", 1, of="review")),
    ):
        assert conditions.problems(wrong), wrong
        assert not conditions.holds(wrong, {"scope": {"text": "x"}}), wrong
        raw = _docs_first()
        next(task for task in raw["tasks"] if task["id"] == "docs")["with"]["condition"] = wrong
        assert "setting" in _rules(raw), wrong


# --- the rules ----------------------------------------------------------------------------


def test_an_if_may_ask_only_what_the_run_can_know_where_it_stands() -> None:
    assert validate.errors(parse(_docs_first())) == []

    def with_condition(condition: dict[str, Any], after: str = "implement") -> set[str]:
        raw = _docs_first()
        tasks = {task["id"]: task for task in raw["tasks"]}
        tasks["docs"]["with"]["condition"] = condition
        if after == "prepare":
            # The If stands before anything is written.
            tasks["prepare"]["next"] = {"ok": "docs"}
            tasks["docs"]["next"] = {"yes": "implement", "no": "implement"}
            tasks["implement"]["next"] = {"done": "early"}
        return _rules(raw)

    assert "condition" in with_condition(_if(_test("change.files", "at_most", 3)), after="prepare")
    assert "condition" in with_condition(_if(_test("areas", "at_least", 1)))
    # The tests before any change are known when a task before the If runs them, as
    # one does here.
    assert with_condition(_if(_test("tests.failed_before", "yes"))) == set()
    raw = _docs_first()
    tasks = {task["id"]: task for task in raw["tasks"]}
    tasks["install"]["next"] = {"ok": "implement"}
    raw["tasks"] = [task for task in raw["tasks"] if task["id"] != "tests"]
    tasks["docs"]["with"]["condition"] = _if(_test("tests.failed_before", "yes"))
    assert "condition" in _rules(raw)
    # The task that was there before the three still says it, when it runs the tests.
    old = _docs_first()
    old["tasks"] = [task for task in old["tasks"] if task["id"] not in ("install", "tests")]
    kept = {task["id"]: task for task in old["tasks"]}
    kept["prepare"].update(task="workspace.checkout", next={"ok": "implement"})
    kept["docs"]["with"]["condition"] = _if(_test("tests.failed_before", "yes"))
    kept["prepare"]["with"] = {"baseline": False}
    assert "condition" in _rules(old)
    kept["prepare"]["with"] = {"baseline": True}
    assert _rules(old) == set()
    # How a task ended: one that runs before, and an outcome it has.
    assert with_condition(_if(_test("outcome", "is", "done", of="implement"))) == set()
    assert "condition" in with_condition(_if(_test("outcome", "is", "pass", of="publish")))
    assert "condition" in with_condition(_if(_test("outcome", "is", "pass", of="nobody")))
    assert "condition" in with_condition(_if(_test("outcome", "is", "splendid", of="implement")))
    # Going back to the implementation makes the review a task that ran before.
    assert with_condition(_if(_test("outcome", "is", "changes", of="review"))) == set()


def test_no_if_leads_a_change_past_the_review_the_tests_or_the_administrator() -> None:
    for skipped, rule in (("review", "publish_reviewed"), ("verify", "publish_tested")):
        raw = _docs_first()
        tasks = {task["id"]: task for task in raw["tasks"]}
        # The yes way goes round the task; the no way still passes it.
        tasks["docs"]["next"] = {
            "yes": tasks[skipped]["next"]["pass" if skipped == "review" else "passed"],
            "no": "review",
        }
        assert rule in _rules(raw) or "collect_after_tests" in _rules(raw), skipped
    raw = _docs_first()
    tasks = {task["id"]: task for task in raw["tasks"]}
    tasks["docs"]["next"] = {"yes": "docs", "no": "review"}
    assert "loop" in _rules(raw)


# --- on paper -----------------------------------------------------------------------------


def test_a_simulation_answers_an_if_from_what_it_knows_or_as_it_is_told() -> None:
    flow = parse(_docs_first())
    # Nothing is known of a change on paper: the If says no, and says what it asked.
    usual = simulate(flow)
    assert _path(usual)[:5] == [
        "prepare:ok",
        "implement:done",
        "docs:no",
        "early:ok",
        "review:pass",
    ]
    asked = next(step for step in usual["steps"] if step["task"] == "docs")
    assert asked["note"] == "The files the change touches all match docs/**, *.md"
    # Told to go by yes, it does.
    told = simulate(flow, outcomes={"docs": ["yes"]})
    assert _path(told)[:4] == ["prepare:ok", "implement:done", "docs:yes", "review:pass"]
    # Gone by a run that happened, it is answered from what that run knew.
    knew = {"touched": {"count": 1, "paths": ["docs/a.md"]}, "went": {"review": "changes"}}
    assert _path(simulate(flow, known=knew))[2] == "docs:yes"
    # The round is the simulation's own: the second time round the If says otherwise.
    raw = _docs_first()
    next(t for t in raw["tasks"] if t["id"] == "docs")["with"]["condition"] = _if(
        _test("round", "is", 1)
    )
    twice = simulate(parse(raw), outcomes={"review": ["changes"]})
    assert [step for step in _path(twice) if step.startswith("docs")] == ["docs:yes", "docs:no"]
    # And so is how the tasks ended: what a past run's tasks did is not carried in.
    raw = _docs_first()
    next(t for t in raw["tasks"] if t["id"] == "docs")["with"]["condition"] = _if(
        _test("outcome", "is", "changes", of="review")
    )
    again = simulate(parse(raw), outcomes={"review": ["changes"]}, known=knew)
    assert [step for step in _path(again) if step.startswith("docs")] == ["docs:no", "docs:yes"]


# --- a run --------------------------------------------------------------------------------


def _implemented(*paths: str) -> str:
    listed = "\n".join([str(len(paths)), *paths]) + "\n"
    return _log(
        [_ran("agent", _claude(MAKER_DONE)), _ran("commit", f"{HEAD}\n"), _ran("touched", listed)]
    )


@pytest.mark.asyncio
async def test_a_run_goes_by_what_the_change_touches(world: Any) -> None:  # noqa: F811
    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    flows = FlowRecords(world.sessions)
    raw = _docs_first()
    next(t for t in raw["tasks"] if t["id"] == "implement")["with"]["providers"] = ["claude"]
    flows.save_draft("deliver-issue", raw)
    flows.publish("deliver-issue")

    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    # The run carries what the item is, for an If to ask about.
    assert run["data"]["item"] == {"labels": ["bug"], "by": "github"}
    await world.step("prepare", _prepared())
    await world.launcher.tick()
    listing = world.job()["commands"][-1]
    assert listing["name"] == "touched" and listing["allow_failure"] is True
    assert listing["env"]["BASE"] == world.run(run["id"])["data"]["base_sha"]
    world.fake.finish(_implemented("docs/guide.md", "README.md"))
    await world.launcher.tick()
    after = world.run(run["id"])
    # Documentation alone: straight to review, and the step says what was asked.
    assert after["data"]["touched"] == {"count": 2, "paths": ["docs/guide.md", "README.md"]}
    assert after["data"]["went"] == {
        "prepare": "ok",
        "install": "ok",
        "tests": "passed",
        "implement": "done",
        "docs": "yes",
    }
    implemented = next(step for step in after["steps"] if step["name"] == "implement")
    assert implemented["input"]["chose"] == [
        {
            "task": "docs",
            "way": "yes",
            "asked": "The files the change touches all match docs/**, *.md",
        }
    ]
    assert after["steps"][-1]["name"] == "review"
    world.runs.request_cancel(UUID(run["id"]))
    await world.launcher.tick()

    # Code as well: the tests come first.
    second = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    await world.step("prepare", _prepared())
    await world.step("implement", _implemented("docs/guide.md", "src/login.ts"))
    after = world.run(second["id"])
    assert after["data"]["went"]["docs"] == "no" and after["steps"][-1]["name"] == "early"
    await world.github.aclose()


@pytest.mark.asyncio
async def test_an_implementation_that_could_not_list_its_files_still_goes_on(world: Any) -> None:  # noqa: F811
    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    await world.step("prepare", _prepared())
    failed = _log(
        [_ran("agent", '{"type":"turn"}\n'), _ran("answer", json.dumps(MAKER_DONE)),
         _ran("commit", f"{HEAD}\n"), _ran("touched", "", exit_code=128, stderr="fatal")]
    )  # fmt: skip
    await world.step("implement", failed)
    after = world.run(run["id"])
    assert "touched" not in after["data"] and after["steps"][-1]["name"] == "review"
    assert deliver.touched_files([_ran("touched", "not a number\n")]) is None
    await world.launcher.tick()
    world.fake.finish(_reviewed())
    await world.launcher.tick()
    assert world.run(run["id"])["steps"][-1]["name"] == "verify"
    await world.github.aclose()
