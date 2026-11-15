"""Waiting for GitHub's checks and merging the pull request a run opened: two tasks of
the platform's own, without an agent, and the rule that holds them together."""

from __future__ import annotations

import copy
import time
from typing import Any
from uuid import UUID

import pytest

from engineering_control.db.models import BacklogItem
from engineering_control.engine import validate
from engineering_control.engine.builtin import BUILTIN
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.document import parse
from engineering_control.engine.records import FlowRecords
from engineering_control.engine.simulate import simulate
from engineering_control.engine.tasks import Approval, Checks
from engineering_control.github_app import GitHubAppError
from tests.test_sandbox_delivery import (  # noqa: F401 - ``world`` is a fixture
    BUNDLE,
    REPOSITORY,
    _implemented,
    _log,
    _prepared,
    _ran,
    _reviewed,
    _verified,
    world,
)

SHA = "e" * 40


def _merging(**settings: Any) -> dict[str, Any]:
    """Deliver issue that goes on, after the pull request, to GitHub's checks and the merge."""
    raw = copy.deepcopy(BUILTIN["deliver-issue"].as_dict())
    tasks = {task["id"]: task for task in raw["tasks"]}
    tasks["publish"]["next"] = {"ok": "checks"}
    raw["tasks"] += [
        {
            "id": "checks",
            "task": "github.checks",
            "with": {key: value for key, value in settings.items() if key != "method"},
            "next": {"passed": "merge"},
        },
        {
            "id": "merge",
            "task": "github.merge",
            "with": {"method": settings["method"]} if "method" in settings else {},
            "next": {},
        },
    ]
    return raw


def _rules(raw: dict[str, Any]) -> set[str]:
    return {problem["rule"] for problem in validate.errors(parse(raw))}


def _task(raw: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(task for task in raw["tasks"] if task["id"] == node_id)


def _check(
    name: str, status: str = "completed", conclusion: str | None = "success"
) -> dict[str, Any]:
    return {"name": name, "status": status, "conclusion": conclusion}


async def _published(world: Any, raw: dict[str, Any]) -> dict[str, Any]:  # noqa: F811
    """A delivery by that flow, decided and published: the pull request is open."""
    if not getattr(world, "accounts_made", False):
        world.account("ChatGPT one", "chatgpt")
        world.account("Claude one", "claude")
        world.accounts_made = True
    flows = FlowRecords(world.sessions)
    in_force = flows.document("deliver-issue", flows.version_in_force("deliver-issue") or 0)
    if in_force is None or in_force.as_dict() != parse(raw).as_dict():
        flows.save_draft("deliver-issue", raw)
        flows.publish("deliver-issue")
    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    for name, log in (
        ("prepare", _prepared()),
        ("implement", _implemented()),
        ("review", _reviewed()),
        ("verify", _verified()),
        ("bundle", _log([_ran("bundle", "2 files")], artifact=BUNDLE)),
    ):
        await world.step(name, log)
    waiting = world.run(run["id"])
    assert waiting["state"] == "waiting"
    world.runs.decide(UUID(run["id"]), "publish")
    await world.launcher.tick()
    shown: dict[str, Any] = world.run(run["id"])
    return shown


@pytest.fixture(autouse=True)
def _ask_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub is asked at every pass, and checks that have not appeared are none at once."""
    monkeypatch.setattr(Checks, "POLL_SECONDS", 0)
    monkeypatch.setattr(Checks, "APPEAR_SECONDS", 0)


# --- what they are ------------------------------------------------------------------------


def test_two_tasks_of_the_platforms_own_wait_for_the_checks_and_merge() -> None:
    checks, merge = TASKS["github.checks"], TASKS["github.merge"]
    for task in (checks, merge):
        assert task.where == "platform" and task.group == "GitHub"
        assert not (task.account or task.writes or task.approval) and task.prompt is None
    assert (checks.outcomes, checks.checked, checks.publishes) == (
        ("passed", "failed"),
        ("passed",),
        False,
    )
    assert (merge.merges, merge.publishes, merge.needs) == (True, True, ("pull_request", "checks"))
    assert [param.name for param in checks.params] == ["minutes", "allow_none"]
    assert merge.params[0].choices == ("squash", "merge", "rebase")
    assert not any(task.merges for key, task in TASKS.items() if key != "github.merge")
    # The platform's own flows merge nothing: that is a flow's to add.
    assert not any(TASKS[node.task].merges for flow in BUILTIN.values() for node in flow.nodes)


def test_a_pull_request_is_merged_only_by_way_of_checks_that_passed() -> None:
    assert _rules(_merging()) == set()
    assert _rules(_merging(minutes=60, allow_none=True, method="rebase")) == set()
    for wrong in (
        {"minutes": 2},
        {"minutes": 500},
        {"allow_none": "yes"},
        {"method": "fast-forward"},
    ):
        assert "setting" in _rules(_merging(**wrong)), wrong
    # Straight from the pull request to the merge: nothing checked it.
    raw = _merging()
    _task(raw, "publish")["next"] = {"ok": "merge"}
    assert "merge_checked" in _rules(raw)
    # By the way the checks' failure leads.
    raw = _merging()
    _task(raw, "checks")["next"] = {"passed": "merge", "failed": "merge"}
    assert "merge_checked" in _rules(raw)
    # Before there is a pull request there is nothing to check or to merge.
    raw = _merging()
    _task(raw, "approve")["next"] = {"publish": "checks"}
    assert "needs" in _rules(raw)
    # Nothing in a sandbox follows what reached GitHub: checks that fail end the run.
    raw = _merging()
    _task(raw, "checks")["next"] = {"passed": "merge", "failed": "implement"}
    assert "platform_last" in _rules(raw)
    # A merge is a way to GitHub like the pull request: what holds for one holds for it.
    raw = _merging()
    _task(raw, "review")["next"]["changes"] = "verify"
    assert "publish_reviewed" in _rules(raw)

    # On paper: the run waits at the checks, and ends there when they fail.
    usual = simulate(parse(_merging()))
    assert [step["task"] for step in usual["steps"]][-4:] == [
        "approve",
        "publish",
        "checks",
        "merge",
    ]
    assert usual["ends"] == {"how": "done", "text": "The pull request is merged."}
    failed = simulate(parse(_merging()), outcomes={"checks": ["failed"]})
    assert failed["steps"][-1]["task"] == "checks" and failed["ends"]["how"] == "failed"
    assert "nothing is merged" in failed["ends"]["text"]

    # The decision says what it decides.
    document = parse(_merging())
    assert (
        "merged once GitHub's checks pass" in Approval.wait(document, document.node("approve"))[1]
    )  # type: ignore[arg-type]
    plain = BUILTIN["deliver-issue"]
    assert Approval.wait(plain, plain.node("approve")) == Approval.WAIT  # type: ignore[arg-type]


# --- what GitHub says ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_checks_of_a_commit_are_read_and_a_merge_names_its_commit(world: Any) -> None:  # noqa: F811
    hub, github = world.hub, world.github
    assert (await github.checks(REPOSITORY, SHA)).state == "none"
    assert hub.tokens[-1]["permissions"] == {"checks": "read", "statuses": "read"}
    assert hub.revoked[-1] == f"token-{len(hub.tokens)}", "the token ends with the question"
    hub.check_runs = [_check("build"), _check("unit tests", "in_progress", None)]
    found = await github.checks(REPOSITORY, SHA)
    assert (found.state, found.total, found.done, found.pending) == (
        "pending",
        2,
        1,
        ("unit tests",),
    )
    hub.check_runs = [
        _check("build"),
        _check("lint", conclusion="skipped"),
        _check("docs", conclusion="neutral"),
    ]
    assert (await github.checks(REPOSITORY, SHA)).state == "passed"
    # One that ended badly decides, whatever the others still do.
    hub.check_runs = [_check("build", conclusion="failure"), _check("e2e", "queued", None)]
    found = await github.checks(REPOSITORY, SHA)
    assert (found.state, found.failed) == ("failed", ("build",))
    for conclusion in ("cancelled", "timed_out", "action_required", "startup_failure", "stale"):
        hub.check_runs = [_check("build", conclusion=conclusion)]
        assert (await github.checks(REPOSITORY, SHA)).state == "failed", conclusion
    # What other services report counts beside the workflows' jobs.
    hub.check_runs = [_check("build")]
    hub.statuses = [{"context": "coverage", "state": "pending"}]
    assert (await github.checks(REPOSITORY, SHA)).pending == ("coverage",)
    hub.statuses = [{"context": "coverage", "state": "error"}]
    assert (await github.checks(REPOSITORY, SHA)).failed == ("coverage",)
    hub.statuses = [{"context": "coverage", "state": "success"}]
    assert (await github.checks(REPOSITORY, SHA)).state == "passed"
    with pytest.raises(GitHubAppError, match="not a commit"):
        await github.checks(REPOSITORY, "main")
    # An app that may not read checks is told what to grant.
    hub.denied = {"checks:read"}
    with pytest.raises(GitHubAppError, match=r"not allowed what this needs.*checks: read"):
        await github.checks(REPOSITORY, SHA)
    hub.denied = set()

    assert await github.merge(REPOSITORY, 77, sha=SHA, method="squash") == "f" * 40
    assert hub.merged == [{"sha": SHA, "merge_method": "squash"}]
    assert hub.tokens[-1]["permissions"] == {"contents": "write", "pull_requests": "write"}
    hub.merge_refused = (405, 'Required status check   "build" is expected.')
    with pytest.raises(GitHubAppError, match='405: Required status check "build" is expected.'):
        await github.merge(REPOSITORY, 77, sha=SHA, method="merge")
    for wrong in ({"sha": "main", "method": "squash"}, {"sha": SHA, "method": "octopus"}):
        with pytest.raises(GitHubAppError, match="cannot|not a pull request"):
            await github.merge(REPOSITORY, 77, **wrong)
    await github.aclose()


# --- a run ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_waits_for_githubs_checks_and_merges_what_they_passed(world: Any) -> None:  # noqa: F811
    world.hub.check_runs = [_check("build", "queued", None), _check("unit tests", "queued", None)]
    run = await _published(world, _merging())
    # The decision was asked for what it decides.
    with world.sessions() as session:
        assert session.get(BacklogItem, world.item_id).state == "accepted"
    assert run["data"]["merges"] is True
    # Published, and the run goes on by itself: it stands at the checks and says so.
    assert (run["state"], run["outcome"]) == ("running", "checking")
    assert run["detail"] == "Waiting for GitHub's checks on pull request #77: 0 of 2 done."
    assert run["data"]["at"] == "checks" and run["data"]["pull_request"]["sha"] == SHA
    assert world.fake.pods == {}, "no sandbox runs while GitHub works"
    assert world.hub.merged == []

    world.hub.check_runs = [_check("build"), _check("unit tests", "in_progress", None)]
    await world.launcher.tick()
    assert world.run(run["id"])["detail"].endswith("1 of 2 done.")
    await world.launcher.tick()
    assert world.hub.merged == [], "nothing is merged while a check runs"

    world.hub.check_runs = [_check("build"), _check("unit tests")]
    await world.launcher.tick()
    done = world.run(run["id"])
    assert (done["state"], done["outcome"]) == ("succeeded", "merged")
    assert done["detail"] == "Pull request #77 is merged into main."
    # Merged as it stood when the checks passed it, the way the flow says, and once.
    assert world.hub.merged == [{"sha": SHA, "merge_method": "squash"}]
    assert done["data"]["pull_request"]["merged"] == "f" * 40
    assert done["data"]["checks"]["result"] == "passed"
    # The pull request was opened once, however often the run was asked again.
    assert [call for call in world.hub.calls if call[1].endswith("/pulls")] == [
        ("POST", f"/repos/{REPOSITORY}/pulls")
    ]
    assert await world.launcher.tick() == 0, "a run that ended is left alone"
    assert world.hub.merged == [{"sha": SHA, "merge_method": "squash"}]
    await world.github.aclose()


@pytest.mark.asyncio
async def test_checks_that_fail_or_do_not_finish_leave_the_pull_request_open(world: Any) -> None:  # noqa: F811
    world.hub.check_runs = [
        _check("build", conclusion="failure"),
        _check("e2e", "in_progress", None),
    ]
    run = await _published(world, _merging(method="rebase"))
    assert (run["state"], run["outcome"]) == ("failed", "checks_failed")
    assert run["detail"] == (
        "1 of 2 checks of GitHub's failed on pull request #77: build. Pull request #77 "
        "stays open; nothing is merged."
    )
    assert world.hub.merged == [] and run["data"]["pull_request"]["number"] == 77

    # Checks that are still running when their time is over count as failed.
    world.hub.check_runs = [_check("e2e", "in_progress", None)]
    slow = await _published(world, _merging(minutes=10))
    assert slow["outcome"] == "checking"
    kept = world.run(slow["id"])["data"]["checks"]
    world.runs.merge_data(UUID(slow["id"]), {"checks": {**kept, "since": time.time() - 11 * 60}})
    await world.launcher.tick()
    ended = world.run(slow["id"])
    assert (ended["state"], ended["outcome"]) == ("failed", "checks_failed")
    assert "did not finish within 10 minutes: e2e." in ended["detail"]
    assert world.hub.merged == []
    await world.github.aclose()


@pytest.mark.asyncio
async def test_a_pull_request_nothing_checked_is_not_merged_unless_the_flow_says_so(
    world: Any,  # noqa: F811
) -> None:
    run = await _published(world, _merging())
    assert (run["state"], run["outcome"]) == ("failed", "checks_failed")
    assert run["detail"].startswith(
        "GitHub runs no checks on pull request #77. Nothing checked it, so it does not count "
        "as passed."
    )
    assert world.hub.merged == []
    allowed = await _published(world, _merging(allow_none=True, method="merge"))
    assert (allowed["state"], allowed["outcome"]) == ("succeeded", "merged")
    assert world.hub.merged == [{"sha": SHA, "merge_method": "merge"}]
    await world.github.aclose()


@pytest.mark.asyncio
async def test_checks_that_cannot_be_read_wait_for_the_administrator(world: Any) -> None:  # noqa: F811
    world.hub.denied = {"checks:read"}
    world.hub.check_runs = [_check("build")]
    run = await _published(world, _merging())
    assert (run["state"], run["outcome"]) == ("waiting", "checks_unreadable")
    assert "Pull request #77 is open, but GitHub's checks cannot be read" in run["detail"]
    assert "checks: read" in run["detail"]
    (entry,) = world.runs.inbox()
    assert entry["outcome"] == "checks_unreadable"
    # Once the app may read them, deciding again looks at the checks and publishes nothing.
    world.hub.denied = set()
    world.runs.decide(UUID(run["id"]), "publish")
    await world.launcher.tick()
    done = world.run(run["id"])
    assert (done["state"], done["outcome"]) == ("succeeded", "merged")
    assert len([call for call in world.hub.calls if call[1].endswith("/pulls")]) == 1

    # Deciding to discard at that point stops the run and leaves the pull request.
    world.hub.denied = {"checks:read"}
    second = await _published(world, _merging())
    assert second["outcome"] == "checks_unreadable"
    world.runs.decide(UUID(second["id"]), "discard")
    await world.launcher.tick()
    stopped = world.run(second["id"])
    assert (stopped["state"], stopped["outcome"]) == ("cancelled", "stopped")
    assert stopped["detail"] == "Stopped. Pull request #77 stays open."
    assert len(world.hub.merged) == 1
    await world.github.aclose()


@pytest.mark.asyncio
async def test_a_merge_github_refuses_fails_the_run_in_githubs_words(world: Any) -> None:  # noqa: F811
    world.hub.check_runs = [_check("build")]
    world.hub.merge_refused = (
        405,
        "At least 1 approving review is required by reviewers with write access.",
    )
    run = await _published(world, _merging())
    assert (run["state"], run["outcome"]) == ("failed", "merge_refused")
    assert run["detail"] == (
        "Pull request #77 was not merged: GitHub answered 405: At least 1 approving review "
        "is required by reviewers with write access.. It stays open."
    )
    assert run["data"]["pull_request"].get("merged") is None
    await world.github.aclose()
