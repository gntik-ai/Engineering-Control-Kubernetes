"""Fixing a bug (B3): the flow "Fix a bug" writes a test that fails because of the bug
and fixes the bug so that it passes; bugs have a section of their own in the engine; a
fix is started from a confirmed bug or the confirmed bugs are queued by severity; and
where the bug stands follows the run and the pull request."""

# The files of a small repository are written out here, lines as long as they are; and
# fixtures of other test modules are named as parameters.
# ruff: noqa: E501, F811

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select

from engineering_control.dashboard.project_sections import _ISSUES_BEFORE
from engineering_control.db.models import (
    BacklogItem,
    ProjectRecord,
    ProjectSection,
    SandboxRun,
)
from engineering_control.engine import routing, validate
from engineering_control.engine.bench_tasks import BENCH
from engineering_control.engine.builtin import BUILTIN
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.library import traits
from engineering_control.flow_library import FLOWS
from engineering_control.sandbox import deliver
from tests.test_bug_section import GitHub, listed, post
from tests.test_dashboard_web import _authenticated_client
from tests.test_model_edits import stage  # noqa: F401 - a fixture
from tests.test_related_tests import GIT, shop  # noqa: F401 - ``shop`` is a fixture
from tests.test_sandbox_delivery import (  # noqa: F401 - ``world`` is a fixture
    BUNDLE,
    _implemented,
    _log,
    _prepared,
    _ran,
    _reviewed,
    _verified,
    world,
)
from tests.test_workbench import _bench_pod, _running, _steps, _tool_log

BUG_TEST = (
    "from app.price import round_price\n\n\n"
    "def test_half_a_cent_is_rounded_up():\n    assert round_price(2.5) == 3\n"
)


# --- the task that runs the bug's test ------------------------------------------------------


def _commit(root: Path) -> None:
    """What the platform does with what an agent wrote: it commits it."""
    for command in (["add", "-A"], ["commit", "-q", "-m", "the agent's work"]):
        subprocess.run([*GIT, *command], cwd=root, check=True)  # noqa: S603


def test_only_the_tests_the_run_wrote_are_run_for_the_bug(shop: dict[str, Any]) -> None:  # noqa: F811
    tools, root = shop["tools"], shop["root"]
    # The run wrote a test for the bug, and touched a file that other tests cover.
    (root / "svc/tests/test_bug_7.py").write_text(BUG_TEST)
    (root / "svc/app/tax.py").write_text((root / "svc/app/tax.py").read_text() + "\n# a note\n")
    _commit(root)
    first = tools.tests({"base": shop["base"], "only_changed": True, "seconds": 120})
    # The test that was written, and none that only covers what was changed.
    assert [entry["path"] for entry in first["tests"]] == ["svc/tests/test_bug_7.py"]
    (ran,) = first["ran"]
    assert ran["exit_code"] != 0 and "test_bug_7.py" in ran["line"] and first["whole"] is False

    # Fixed, the same file passes when it is named.
    (root / "svc/app/price.py").write_text(
        "import math\n\n\ndef round_price(cents):\n    return math.floor(cents + 0.5)\n"
    )
    fixed = tools.tests(
        {"paths": ["svc/tests/test_bug_7.py"], "base": shop["base"], "seconds": 120}
    )
    assert fixed["passed"] is True and [e["path"] for e in fixed["tests"]] == [
        "svc/tests/test_bug_7.py"
    ]


def test_where_no_test_runs_alone_the_projects_own_commands_are_run(shop: dict[str, Any]) -> None:  # noqa: F811
    tools, root = shop["tools"], shop["root"]
    # A test of a kind that cannot be run here: vitest is not installed for it.
    (root / "ui/src/bug.test.ts").write_text("test('bug', () => expect(1).toBe(2));\n")
    _commit(root)
    otherwise = [
        {"argv": ["true"], "cwd": ".", "timeout": 30, "line": "true"},
        {"argv": ["false"], "cwd": "svc", "timeout": 30, "line": "[svc] false"},
    ]
    answer = tools.tests(
        {"base": shop["base"], "only_changed": True, "seconds": 60, "otherwise": otherwise}
    )
    assert answer["whole"] is True and [e["path"] for e in answer["unrunnable"]] == [
        "ui/src/bug.test.ts"
    ]
    # Every command is run, whatever the one before it did, and told by its line.
    assert [(run["line"], run["exit_code"]) for run in answer["ran"]] == [
        ("true", 0),
        ("[svc] false", 1),
    ]
    # With nothing to fall back on, nothing ran.
    nothing = tools.tests({"base": shop["base"], "only_changed": True, "seconds": 60})
    assert nothing["ran"] == [] and nothing["whole"] is False


def _ctx(expect: str, data: dict[str, Any]) -> Any:
    return SimpleNamespace(params={"expect": expect}, data=data, given={})


def _concluded(expect: str, data: dict[str, Any], answer: dict[str, Any]) -> Any:
    log = _tool_log("tests", answer)
    return asyncio.run(
        BENCH["check.reproduction"].conclude(_ctx(expect, data), [{"exit_code": 0}], log)
    )


def _group(code: int, line: str = "[svc] python -m pytest tests/test_bug_7.py") -> dict[str, Any]:
    return {
        "runner": "pytest",
        "folder": "svc",
        "files": 1,
        "line": line,
        "exit_code": code,
        "seconds": 1.0,
        "timed_out": False,
        "said": "assert 2 == 3",
    }


def test_the_bugs_test_has_to_fail_before_the_fix_and_pass_after_it() -> None:
    kind = TASKS["check.reproduction"]
    assert (kind.where, kind.account, kind.writes, kind.gate) == ("bench", False, False, False)
    assert kind.outcomes == ("fails", "passes", "none")
    found = {
        "changed": ["svc/tests/test_bug_7.py"],
        "tests": [{"path": "svc/tests/test_bug_7.py", "why": "it was changed"}],
        "unrunnable": [],
        "whole": False,
    }
    # Before the fix, a failing test is what shows the bug: it is kept for later, and
    # what it said is given to the agent that fixes.
    shown = _concluded("fail", {}, {**found, "ran": [_group(1)], "passed": False})
    assert (shown.outcome, shown.label) == ("fails", "reproduced")
    assert shown.data["bug"] == {
        "tests": ["svc/tests/test_bug_7.py"],
        "by": "tests",
        "shown": True,
    }
    assert "assert 2 == 3" in shown.given["failures"][0]
    # A test that passes shows no bug.
    absent = _concluded("fail", {}, {**found, "ran": [_group(0)], "passed": True})
    assert (absent.outcome, absent.label) == ("passes", "not_reproduced")
    assert absent.data["bug"]["shown"] is False and "not shown" in absent.detail
    # No test at all: nothing to go by.
    none = _concluded("fail", {}, {**found, "tests": [], "ran": [], "passed": None})
    assert (none.outcome, none.label) == ("none", "no_test")
    assert none.data["bug"] == {"tests": [], "shown": False}

    # After the fix the same files have to pass.
    known = {"bug": shown.data["bug"]}
    still = _concluded("pass", known, {**found, "ran": [_group(1)], "passed": False})
    assert (still.outcome, still.label) == ("fails", "still_failing")
    assert still.data["bug"]["fixed"] is False and still.data["bug"]["shown"] is True
    fixed = _concluded("pass", known, {**found, "ran": [_group(0)], "passed": True})
    assert (fixed.outcome, fixed.label) == ("passes", "fixed")
    assert fixed.data["bug"] == {**shown.data["bug"], "fixed": True}

    # The project's own commands, where no test runs alone: one that failed before
    # anything was changed says nothing of the bug.
    whole = {**found, "whole": True}
    before = {"baseline_by_command": {"make test": 0, "make lint": 1}}
    lint = _concluded(
        "fail", before, {**whole, "ran": [_group(0, "make test"), _group(1, "make lint")]}
    )
    assert lint.outcome == "passes" and lint.data["bug"]["by"] == "commands"
    broke = _concluded(
        "fail", before, {**whole, "ran": [_group(1, "make test"), _group(1, "make lint")]}
    )
    assert broke.outcome == "fails" and "make test" in broke.detail
    assert "make lint" not in broke.detail


def test_the_task_asks_for_the_runs_own_tests_and_the_projects_commands() -> None:
    import json

    toolchain = {"tests": [{"argv": ["make", "test"], "cwd": "svc", "timeout": 900}]}

    def asked(data: dict[str, Any]) -> dict[str, Any]:
        ctx = SimpleNamespace(
            params={"minutes": 5},
            data={"base_sha": "a" * 40, "toolchain": toolchain, **data},
            given={},
            toolchain=lambda: deliver.Toolchain.from_dict(toolchain),
        )
        plan = asyncio.run(BENCH["check.reproduction"].plan(ctx, None))
        return cast(dict[str, Any], json.loads(plan.job["files"][1]["content"]))

    first = asked({})
    assert (first["paths"], first["only_changed"], first["seconds"]) == ([], True, 300)
    assert first["otherwise"] == [
        {"argv": ["make", "test"], "cwd": "svc", "timeout": 900, "line": "[svc] make test"}
    ]
    assert first["otherwise_seconds"] == 900
    # Once the bug's tests are known, they are what is run.
    later = asked({"bug": {"tests": ["svc/tests/test_bug_7.py"], "shown": True}})
    assert (later["paths"], later["only_changed"]) == (["svc/tests/test_bug_7.py"], False)


# --- the flow ---------------------------------------------------------------------------------


def test_the_flow_fix_a_bug_holds_together_and_is_safe() -> None:
    flow = BUILTIN["fix-bug"]
    assert validate.problems(flow) == []
    assert FLOWS["fix-bug"].name == flow.name == "Fix a bug"
    kind = traits(flow)
    assert kind["writes"] and kind["publishes"] and not kind["produces"]
    order = [node.id for node in flow.nodes]
    assert order.index("reproduce") < order.index("shown") < order.index("fix")
    # The two agents that write are told what to do, and the reviewer is of the other
    # subscription than the one that fixed.
    reproduce, fix, review = (flow.node(name) for name in ("reproduce", "fix", "review"))
    assert reproduce is not None and fix is not None and review is not None
    assert "Do not fix it" in reproduce.params["instruction"]
    assert "answer BLOCKED" in reproduce.params["instruction"]
    assert "do not weaken it" in fix.params["instruction"]
    assert review.params["other_than"] == "fix"

    def after(node_id: str, outcome: str, seen: list[str]) -> routing.Next:
        node = flow.node(node_id)
        assert node is not None
        return routing.follow(
            flow, node, outcome, seen=seen, each={}, round_number=1, known={}, rounds=3
        )

    # A test that fails before the fix leads on to the fix; one that passes, or none,
    # ends the flow there: no bug was shown.
    assert after("shown", "fails", ["reproduce"]).target == "fix"
    assert after("shown", "passes", ["reproduce"]).kind == "end"
    assert after("shown", "none", ["reproduce"]).kind == "end"
    # After the fix the test has to pass before the review; failing sends it back,
    # which is a round.
    assert after("fixed", "passes", ["fix"]).target == "review"
    back = after("fixed", "fails", ["fix", "fixed"])
    assert (back.target, back.again, back.round) == ("fix", True, 2)
    # Like Deliver issue it ends at the pull request: it merges nothing.
    assert after("bundle", "ok", []).kind == "platform"
    assert [node.id for node in flow.nodes][-2:] == ["approve", "publish"]
    assert not any(TASKS[node.task].merges for node in flow.nodes)
    # Without the review a change of this flow could not be published.
    raw = flow.as_dict()
    tasks = {task["id"]: task for task in raw["tasks"]}
    tasks["fixed"]["next"] = {"passes": "verify", "fails": "fix", "none": "verify"}
    raw["tasks"] = [task for task in raw["tasks"] if task["id"] != "review"]
    from engineering_control.engine.document import parse

    assert validate.errors(parse(raw))


# --- the section of the engine ------------------------------------------------------------------


def test_bugs_have_a_section_of_their_own(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        sections = {
            s["key"]: s for s in client.get("/api/dashboard/projects/llmwiki").json()["sections"]
        }
        assert sections["bugs"]["labels"] == ["bug", "defect", "regression"]
        assert sections["bugs"]["kind"] == "core" and sections["issues"]["labels"] == ["fix"]
        assert sections["bugs"]["flow"]["key"] == "fix-bug"

        # A project from before: its Issues section had the labels that mean a bug, in
        # the platform's own words, and it had no Bugs section.
        with repo.sessions.begin() as db:
            project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "llmwiki")).one()
            rows = {
                row.key: row
                for row in db.scalars(
                    select(ProjectSection).where(ProjectSection.project_id == project.id)
                )
            }
            db.delete(rows["bugs"])
            issues = rows["issues"]
            issues.labels = ["bug", "defect", "regression", "fix"]
            for field, before in _ISSUES_BEFORE.items():
                setattr(issues, field, before)
            # What the administrator wrote themselves is theirs.
            issues.use_when = "Whatever I say."
        assert repo.sections.sync_core() == 1
        sections = {
            s["key"]: s for s in client.get("/api/dashboard/projects/llmwiki").json()["sections"]
        }
        assert sections["bugs"]["labels"] == ["bug", "defect", "regression"]
        assert sections["issues"]["labels"] == ["fix"]
        assert "no bug" in sections["issues"]["description"]
        assert sections["issues"]["use_when"] == "Whatever I say."
        assert "use Bugs" in sections["issues"]["avoid_when"]
        # Done once: nothing is added or changed again.
        assert repo.sections.sync_core() == 0
    finally:
        client.close()


# --- from the Bugs page ---------------------------------------------------------------------------


def _bug(repo: Any, number: int) -> BacklogItem:
    with repo.sessions() as db:
        return cast(
            BacklogItem,
            db.scalars(select(BacklogItem).where(BacklogItem.issue_number == number)).one(),
        )


def _run_of(repo: Any, number: int) -> SandboxRun:
    with repo.sessions() as db:
        item = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == number)).one()
        return cast(
            SandboxRun,
            db.scalars(
                select(SandboxRun)
                .where(SandboxRun.item_id == item.id)
                .order_by(SandboxRun.created_at.desc())
            ).first(),
        )


def test_a_fix_is_started_from_a_confirmed_bug_and_the_bug_follows_it(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        github = GitHub()
        cast(Any, client.app).state.github_app = github
        report = {"title": "Rounding loses half a cent", "happens": "2.5 becomes 2."}
        assert post(client, "llmwiki", "bugs", **report).status_code == 200
        (bug,) = listed(client, "llmwiki")["items"]
        overview = client.get("/api/dashboard/projects/llmwiki/bugs").json()
        assert overview["fixing"] == {
            "section": "Bugs",
            "flow": {"key": "fix-bug", "name": "Fix a bug"},
            "refused": None,
            "ready": 0,
        }
        # A new bug is triaged first: a fix starts from a confirmed one.
        assert "severity first" in bug["fix"]["refused"]
        early = post(client, "llmwiki", f"bugs/{bug['id']}/fix")
        assert early.status_code == 409 and "severity first" in early.text
        assert (
            post(client, "llmwiki", f"bugs/{bug['id']}/triage", severity="high").status_code == 200
        )
        (bug,) = listed(client, "llmwiki")["items"]
        assert bug["fix"]["refused"] is None and bug["fix"]["run"] is None

        # One button: the bug goes to the section Bugs, and that section's flow runs.
        started = post(client, "llmwiki", f"bugs/{bug['id']}/fix")
        assert started.status_code == 201, started.text
        run = _run_of(repo, 100)
        assert started.json()["id"] == str(run.id)
        assert run.data["flow"]["key"] == "fix-bug" and run.data["issue"]["number"] == 100
        with repo.sessions() as db:
            item = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == 100)).one()
            section = db.get(ProjectSection, item.section_id)
            assert section is not None and section.key == "bugs"
            assert item.routed_by == "administrator"
        (bug,) = listed(client, "llmwiki", state="in_progress")["items"]
        assert (bug["triage"], bug["says"]) == ("in_progress", "In progress")
        assert bug["fix"]["under_way"] and bug["fix"]["says"] == "A fix is being worked on."
        assert "under way" in bug["fix"]["refused"]
        assert post(client, "llmwiki", f"bugs/{bug['id']}/fix").status_code == 409

        # It waits for the administrator: the bug says so.
        with repo.sessions.begin() as db:
            row = db.get(SandboxRun, run.id)
            assert row is not None
            row.state, row.detail = "waiting", "A change is ready. Publish it, or discard it."
        (bug,) = listed(client, "llmwiki")["items"]
        assert bug["fix"]["waits"] and bug["fix"]["says"].startswith(
            "A fix waits for you: A change"
        )

        # No test could show the bug: it says so, with what the run said, and is
        # confirmed again; a fix can be started anew.
        with repo.sessions.begin() as db:
            row = db.get(SandboxRun, run.id)
            assert row is not None
            row.state, row.outcome = "succeeded", "not_reproduced"
            row.detail = "The bug is not shown: what was written for it passes."
            row.finished_at = datetime.now(UTC)
            row.data = {**row.data, "bug": {"tests": ["t.py"], "shown": False}}
        (bug,) = listed(client, "llmwiki")["items"]
        assert bug["triage"] == "confirmed" and bug["fix"]["not_reproduced"]
        assert bug["fix"]["says"] == (
            "No test could show the bug: The bug is not shown: what was written for it passes."
        )
        assert bug["fix"]["refused"] is None
        # So does an agent that could not write the test at all.
        with repo.sessions.begin() as db:
            row = db.get(SandboxRun, run.id)
            assert row is not None
            row.state, row.outcome = "failed", "blocked"
            row.detail = "The agent could not do its task: the report names no steps."
            row.data = {key: value for key, value in row.data.items() if key != "bug"}
        (bug,) = listed(client, "llmwiki")["items"]
        assert bug["fix"]["not_reproduced"] and "names no steps" in bug["fix"]["says"]
        # A fix that failed after the bug was shown did not fail to show it.
        with repo.sessions.begin() as db:
            row = db.get(SandboxRun, run.id)
            assert row is not None
            row.data = {**row.data, "bug": {"tests": ["t.py"], "shown": True}}
        (bug,) = listed(client, "llmwiki")["items"]
        assert not bug["fix"]["not_reproduced"]
        assert bug["fix"]["says"].startswith("The last fix did not finish: The agent")

        # A pull request is open: in progress. Merged, the issue is closed by GitHub and
        # the bug is fixed, by that pull request.
        pull = {"number": 7, "url": "https://github.com/gntik-ai/llmwiki/pull/7", "sha": "b" * 40}
        with repo.sessions.begin() as db:
            row = db.get(SandboxRun, run.id)
            item = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == 100)).one()
            assert row is not None
            row.state, row.outcome, row.detail = (
                "failed",
                "checks_failed",
                "GitHub's checks failed.",
            )
            row.data = {**row.data, "pull_request": pull}
            item.facts = {**(item.facts or {}), "pull_request": pull}
        (bug,) = listed(client, "llmwiki", state="in_progress")["items"]
        assert bug["fix"]["pull_request"] == {"number": 7, "url": pull["url"], "merged": False}
        assert not bug["fix"]["not_reproduced"]
        assert bug["fix"]["says"] == "Pull request #7 is open: the bug is fixed when it is merged."
        # Merged on GitHub, by whoever: the issue is closed as completed, and the next
        # reading of the repository says so here.
        with repo.sessions.begin() as db:
            item = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == 100)).one()
            item.facts = {**(item.facts or {}), "github_state_reason": "completed"}
            item.issue_state, item.state = "closed", "done"
        (bug,) = listed(client, "llmwiki", state="fixed")["items"]
        assert bug["triage"] == "fixed" and bug["fix"]["says"] == "Fixed by pull request #7."
        assert client.get("/api/dashboard/projects/unknown/bugs").status_code == 404
    finally:
        client.close()


def test_the_confirmed_bugs_are_queued_the_most_severe_first(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        github = GitHub()
        cast(Any, client.app).state.github_app = github
        for title, severity in (("Low one", "low"), ("Critical one", "critical"), ("New one", "")):
            body = {"title": title, "happens": "It is wrong."}
            if severity:
                body["severity"] = severity
            assert post(client, "llmwiki", "bugs", **body).status_code == 200
        assert (
            post(
                client, "llmwiki", "bugs", title="High one", happens="x", severity="high"
            ).status_code
            == 200
        )
        overview = client.get("/api/dashboard/projects/llmwiki/bugs").json()
        assert overview["fixing"]["ready"] == 3

        made = post(client, "llmwiki", "bugs/queue")
        assert made.status_code == 201, made.text
        queue = made.json()
        # A draft: nothing starts until the administrator starts it. The most severe
        # first; the bug nobody confirmed is not in it.
        assert queue["state"] == "draft" and queue["name"].startswith("Bugs by severity")
        assert [entry["title"] for entry in queue["entries"]] == [
            "Critical one",
            "High one",
            "Low one",
        ]
        with repo.sessions() as db:
            keys = {
                db.get(ProjectSection, item.section_id).key  # type: ignore[union-attr]
                for item in db.scalars(
                    select(BacklogItem).where(BacklogItem.title.in_(("Low one", "High one")))
                )
            }
            assert keys == {"bugs"}
            assert not db.scalars(select(SandboxRun)).all()
        # They are in a queue now: there is nothing left to queue.
        again = post(client, "llmwiki", "bugs/queue")
        assert again.status_code == 409 and "No confirmed bug waits" in again.text
    finally:
        client.close()


# --- a run of the flow, as far as the administrator's decision ------------------------------------


def _to_bugs(stage: Any) -> None:
    """The item is a bug of the section Bugs, whose flow is Fix a bug."""
    with stage.sessions.begin() as db:
        item = db.get(BacklogItem, stage.item_id)
        assert item is not None
        bugs = db.scalars(
            select(ProjectSection).where(
                ProjectSection.project_id == item.project_id, ProjectSection.key == "bugs"
            )
        ).one()
        bugs.flow_key = "fix-bug"
        item.section_id = bugs.id


async def _bugs_test(stage: Any, run_id: str, name: str, answer: dict[str, Any]) -> dict[str, Any]:
    """The bug's test is run in the workbench: no sandbox of its own, and no agent."""
    stage.door.answers = [_tool_log("tests", answer)]
    await stage.launcher.tick()
    pod = _bench_pod(stage, run_id)
    assert pod is not None
    _running(pod)
    await stage.launcher.tick()
    step = _steps(stage, run_id)[name]
    if step["state"] != "succeeded":
        await stage.launcher.tick()
        step = _steps(stage, run_id)[name]
    return cast(dict[str, Any], step)


def _step_pod(stage: Any, name: str) -> dict[str, Any]:
    """The sandbox of a step. The run's workbench stays beside it."""
    (pod,) = [
        pod
        for pod in stage.fake.pods.values()
        if pod["metadata"]["labels"].get("engineering-control/step") == name
    ]
    return cast(dict[str, Any], pod)


async def _sandbox(stage: Any, name: str, *logs: str) -> dict[str, Any]:
    """Start the step that is due, end its sandbox with a log, and let the launcher read
    it; with more logs, the steps that follow by themselves likewise. The first step's
    job is handed back."""
    await stage.launcher.tick()
    first = _step_pod(stage, name)
    job = cast(dict[str, Any], json.loads(first["spec"]["containers"][0]["env"][0]["value"]))
    for place, log in enumerate(logs):
        if place:
            await stage.launcher.tick()
        (pod,) = [pod for key, pod in stage.fake.pods.items() if key.startswith("run-")]
        pod["status"] = {"phase": "Succeeded"}
        stage.fake.logs[pod["metadata"]["name"]] = log
        await stage.launcher.tick()
    return job


FOUND = {
    "changed": ["src/session.bug.test.ts"],
    "tests": [{"path": "src/session.bug.test.ts", "why": "it was changed"}],
    "unrunnable": [],
    "whole": False,
}


@pytest.mark.asyncio
async def test_a_bug_is_fixed_by_way_of_a_test_that_fails_first(stage: Any) -> None:  # noqa: F811
    _to_bugs(stage)
    run = stage.runs.start_item(stage.item_id, toolchain=stage.toolchain)
    assert run["data"]["flow"]["key"] == "fix-bug"
    await stage.step("prepare", _prepared())

    # An agent writes the test, told to fix nothing. It is the bug's report it reads.
    await stage.launcher.tick()
    job = stage.job()
    asked = job["commands"][0]["stdin"]
    assert "Do not fix it. Write one automated test that shows it" in asked
    assert "Signing in fails with an expired cookie" in asked
    stage.fake.finish(_implemented())
    await stage.launcher.tick()
    assert _steps(stage, run["id"])["reproduce"]["state"] == "succeeded"

    # The test has to fail before the fix. What it is asked: the tests the run wrote.
    shown = await _bugs_test(
        stage, run["id"], "shown", {**FOUND, "ran": [_ran_test(1)], "passed": False}
    )
    assert (shown["state"], shown["outcome"]) == ("succeeded", "reproduced")
    assert shown["detail"].startswith("The bug is shown: its test fails before the fix")
    question = json.loads(stage.door.handed[-1][1]["files"][1]["content"])
    assert (question["paths"], question["only_changed"]) == ([], True)
    assert stage.run(run["id"])["data"]["bug"] == {
        "tests": ["src/session.bug.test.ts"],
        "by": "tests",
        "shown": True,
    }

    # The fix is told what the test said.
    fix = (await _sandbox(stage, "fix", _implemented()))["commands"][0]["stdin"]
    assert "Fix the bug in the code, so that this test passes" in fix
    assert "expected 401, got 200" in fix

    # After the fix the same files have to pass; then the review, the tests, the scan
    # and the collection, as for any change, and the administrator's decision.
    fixed = await _bugs_test(
        stage, run["id"], "fixed", {**FOUND, "ran": [_ran_test(0)], "passed": True}
    )
    assert (fixed["outcome"], fixed["detail"]) == (
        "fixed",
        "The bug's test passes after the fix (1 test file of its own).",
    )
    question = json.loads(stage.door.handed[-1][1]["files"][1]["content"])
    assert (question["paths"], question["only_changed"]) == (["src/session.bug.test.ts"], False)
    await _sandbox(stage, "review", _reviewed())
    await _sandbox(stage, "verify", _verified(), _verified())
    await _sandbox(stage, "bundle", _log([_ran("bundle", "2 files")], artifact=BUNDLE))
    waiting = stage.run(run["id"])
    assert (waiting["state"], waiting["outcome"]) == ("waiting", "ready")
    # The decision is to publish a pull request: the platform merges nothing.
    assert waiting["detail"] == "A change is ready. Publish it as a pull request, or discard it."
    assert [step["name"] for step in waiting["steps"]] == [
        "prepare",
        "install",
        "tests",
        "reproduce",
        "shown",
        "fix",
        "fixed",
        "review",
        "verify",
        "scan",
        "bundle",
    ]
    assert waiting["data"]["bug"]["fixed"] is True
    assert "pull" not in stage.hub.created, "nothing reached GitHub yet"
    await stage.http.aclose()
    await stage.github.aclose()


@pytest.mark.asyncio
async def test_a_bug_no_test_shows_ends_the_run_and_says_so(stage: Any) -> None:  # noqa: F811
    _to_bugs(stage)
    run = stage.runs.start_item(stage.item_id, toolchain=stage.toolchain)
    await stage.step("prepare", _prepared())
    await stage.step("reproduce", _implemented())
    # What was written for the bug passes: it shows nothing, and nothing is fixed.
    shown = await _bugs_test(
        stage, run["id"], "shown", {**FOUND, "ran": [_ran_test(0)], "passed": True}
    )
    assert (shown["state"], shown["outcome"]) == ("succeeded", "not_reproduced")
    await stage.launcher.tick()
    ended = stage.run(run["id"])
    assert (ended["state"], ended["outcome"]) == ("succeeded", "not_reproduced")
    assert ended["detail"].startswith("The bug is not shown: what was written for it passes")
    assert [step["name"] for step in ended["steps"]][-2:] == ["reproduce", "shown"]
    assert ended["data"]["bug"]["shown"] is False
    await stage.http.aclose()
    await stage.github.aclose()


def _ran_test(code: int) -> dict[str, Any]:
    return {
        "runner": "vitest",
        "folder": "",
        "files": 1,
        "line": "vitest run src/session.bug.test.ts",
        "exit_code": code,
        "seconds": 2.0,
        "timed_out": False,
        "said": "expected 401, got 200" if code else "1 passed",
    }
