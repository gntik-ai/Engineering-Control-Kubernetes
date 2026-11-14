"""Cloning, installing and running the tests as three tasks of their own, and a project's
own commands. No agent takes part in any of them."""

from __future__ import annotations

import copy
from typing import Any
from uuid import UUID

import httpx
import pytest

from engineering_control.dashboard.project_records import ProjectInvalid, ProjectRecords
from engineering_control.engine import validate
from engineering_control.engine.builtin import BUILTIN
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.document import parse
from engineering_control.engine.records import FlowRecords
from engineering_control.engine.task_library import TaskLibrary
from engineering_control.sandbox import deliver
from tests.test_sandbox_delivery import (  # noqa: F401 - ``world`` is a fixture
    BASE,
    HEAD,
    MAKER_DONE,
    PASSED,
    REPOSITORY,
    _claude,
    _log,
    _ran,
    _reviewed,
    world,
)


def _raw() -> dict[str, Any]:
    return copy.deepcopy(BUILTIN["deliver-issue"].as_dict())


def _task(raw: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(task for task in raw["tasks"] if task["id"] == node_id)


def _rules(raw: dict[str, Any]) -> set[str]:
    return {problem["rule"] for problem in validate.errors(parse(raw))}


def _in_force(world: Any, raw: dict[str, Any]) -> None:  # noqa: F811
    flows = FlowRecords(world.sessions)
    flows.save_draft("deliver-issue", raw)
    flows.publish("deliver-issue")


async def _job(world: Any, name: str) -> dict[str, Any]:  # noqa: F811
    """Start the pending step, which is the one expected, and give back what it runs."""
    await world.launcher.tick()
    pod = world.fake.only_pod()
    assert pod["metadata"]["labels"]["engineering-control/step"] == name
    job: dict[str, Any] = world.job()
    return job


async def _end(world: Any, log: str) -> None:  # noqa: F811
    world.fake.finish(log)
    await world.launcher.tick()


CLONED = _log([_ran("checkout", f"{BASE}\n")])


# --- what they are --------------------------------------------------------------------------


def test_three_tasks_stand_where_one_stood_and_no_agent_is_in_any() -> None:
    clone, install, tests = (
        TASKS[key] for key in ("workspace.clone", "workspace.install", "check.tests")
    )
    for task in (clone, install, tests):
        assert task.where == "sandbox" and task.prompt is None
        assert not (task.account or task.writes or task.publishes or task.approval or task.gate)
        assert "No agent takes part" in task.summary
    assert clone.produces == ("checkout",) and [p.kind for p in clone.params] == ["line"]
    assert install.needs == ("checkout",) and [p.kind for p in install.params] == ["commands"]
    assert tests.outcomes == ("passed", "failed") and tests.produces == ("baseline",)
    # The task they took the place of is kept for the flows that have it.
    old = TASKS["workspace.checkout"]
    assert old.retired == "Clone the repository, Install the project and Run the tests"
    # The platform's own flows are built from the three.
    kinds = [TASKS[node.task].key for node in BUILTIN["deliver-issue"].nodes[:3]]
    assert kinds == ["workspace.clone", "workspace.install", "check.tests"]
    for document in BUILTIN.values():
        assert not any(node.task == "workspace.checkout" for node in document.nodes)
        assert validate.problems(document) == []


def test_a_command_is_a_line_split_into_words_and_run_without_a_shell() -> None:
    plain = deliver.command_of("pnpm install --frozen-lockfile")
    assert plain.argv == ("pnpm", "install", "--frozen-lockfile") and plain.cwd == "."
    quoted = deliver.command_of('  bash -c "pnpm run lint && pnpm run test"  ')
    assert quoted.argv == ("bash", "-c", "pnpm run lint && pnpm run test")
    inside = deliver.command_of("[web/console] npm test -- --run")
    assert (inside.cwd, inside.argv) == ("web/console", ("npm", "test", "--", "--run"))
    # A command reads back as the line that says it.
    for line in ("pnpm run test", "bash -c 'a && b'", "[web/console] npm test -- --run"):
        assert deliver.line_of(deliver.command_of(line)) == line
    for wrong in ("", "   ", 'echo "unclosed', "[/etc] ls", "[../other] ls", "x" * 601, "a\nb", 7):
        with pytest.raises(ValueError):
            deliver.command_of(wrong)
    assert deliver.commands_of([]) == ()
    with pytest.raises(ValueError, match="Command 2 has a quote that is not closed"):
        deliver.commands_of(["pnpm i", "echo 'x"])
    with pytest.raises(ValueError, match="at most 12"):
        deliver.commands_of(["true"] * 13)
    with pytest.raises(ValueError):
        deliver.commands_of("pnpm install")


def test_a_branch_and_the_commands_of_a_task_are_checked_before_a_flow_runs() -> None:
    assert _rules(_raw()) == set()
    for branch in ("develop", "release/2026.11", "feature_x-1", ""):
        raw = _raw()
        _task(raw, "prepare")["with"] = {"branch": branch}
        assert _rules(raw) == set(), branch
    for branch in ("-force", "a..b", "has space", "x" * 101, "a\nb", 7, None):
        raw = _raw()
        _task(raw, "prepare")["with"] = {"branch": branch}
        assert "setting" in _rules(raw), branch
    for node in ("install", "tests"):
        raw = _raw()
        _task(raw, node)["with"] = {"commands": ["pnpm install", "[web] npm ci"]}
        assert _rules(raw) == set()
        for commands in ("pnpm install", ['echo "x'], ["true"] * 13, [7], [""]):
            raw = _raw()
            _task(raw, node)["with"] = {"commands": commands}
            assert "setting" in _rules(raw), commands
    # A task needs the clone before it, like every task that works in the checkout.
    raw = _raw()
    raw["start"] = "install"
    assert "needs" in _rules(raw)


# --- a run ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_branch_named_by_the_flow_is_cloned_and_is_what_a_pull_request_goes_against(
    world: Any,  # noqa: F811
) -> None:
    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    raw = _raw()
    _task(raw, "prepare")["with"] = {"branch": " develop "}
    _in_force(world, raw)
    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    assert run["data"]["base_branch"] == "main", "the project's own, until the clone"
    job = await _job(world, "prepare")
    assert [command["name"] for command in job["commands"]] == ["checkout"]
    assert job["commands"][0]["env"]["BASE"] == "develop"
    assert job["commands"][0]["env"]["BRANCH"] == run["data"]["branch"]
    await _end(world, CLONED)
    after = world.run(run["id"])
    assert after["data"]["base_branch"] == "develop" and after["data"]["base_sha"] == BASE
    assert after["steps"][0]["detail"] == f"Cloned {REPOSITORY} at develop ({BASE[:12]})."
    assert after["steps"][0]["provider"] is None, "no account works in it"
    # A branch that is not there ends the run at the clone, in git's words.
    world.runs.request_cancel(UUID(run["id"]))
    await world.launcher.tick()
    again = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    await _job(world, "prepare")
    await _end(
        world, _log([_ran("checkout", exit_code=128, stderr="Remote branch develop not found")])
    )
    ended = world.run(again["id"])
    assert (ended["state"], ended["outcome"]) == ("failed", "checkout_failed")
    assert "Remote branch develop not found" in ended["detail"]
    await world.github.aclose()


@pytest.mark.asyncio
async def test_the_commands_of_a_task_are_run_in_place_of_the_projects(world: Any) -> None:  # noqa: F811
    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    raw = _raw()
    _task(raw, "install")["with"] = {"commands": ["npm ci", "[web] npm run build"]}
    _task(raw, "tests")["with"] = {"commands": ["npm run lint", "npm test -- --run"]}
    _task(raw, "implement")["with"]["providers"] = ["claude"]
    _in_force(world, raw)
    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    await _job(world, "prepare")
    await _end(world, CLONED)

    job = await _job(world, "install")
    assert [(c["name"], c["argv"]) for c in job["commands"]] == [
        ("install 1", ["npm", "ci"]),
        ("install 2", ["npm", "run", "build"]),
    ]
    assert job["commands"][1]["cwd"].endswith("/repo/web")
    assert not any(command.get("allow_failure") for command in job["commands"])
    await _end(world, _log([_ran("install 1"), _ran("install 2")]))
    installed = world.run(run["id"])["steps"][1]
    assert installed["detail"] == "Installed with 2 commands of the flow's own."
    assert installed["output"] == {"commands": ["npm ci", "[web] npm run build"], "own": True}

    # The tests before any change: every command is run, and one already fails.
    job = await _job(world, "tests")
    assert [(c["name"], c["argv"]) for c in job["commands"][1:-1]] == [
        ("baseline 1", ["npm", "run", "lint"]),
        ("baseline 2", ["npm", "test", "--", "--run"]),
    ]
    assert [job["commands"][0]["name"], job["commands"][-1]["name"]] == ["note", "restore"]
    assert world.fake.only_pod()["metadata"]["labels"]["engineering-control/egress"] == "none"
    await _end(world, _log([_ran("baseline 1", exit_code=1, stderr="lint"), _ran("baseline 2")]))
    after = world.run(run["id"])
    tested = after["steps"][2]
    assert (tested["outcome"], after["data"]["went"]["tests"]) == ("tests_failing", "failed")
    assert tested["detail"] == (
        "1 of 2 test commands fail before anything is changed: npm run lint"
    )
    assert after["data"]["baseline"] == {"1": 1, "2": 0}
    assert tested["output"]["failing"] == ["npm run lint"]

    # The implementer is told of the same commands, and Test and scan runs the same.
    job = await _job(world, "implement")
    prompt = job["commands"][0]["stdin"]
    assert "- npm run lint\n- npm test -- --run" in prompt and "pnpm run test" not in prompt
    await _end(
        world,
        _log(
            [
                _ran("agent", _claude(MAKER_DONE)),
                _ran("commit", f"{HEAD}\n"),
                _ran("touched", "1\na\n"),
            ]
        ),
    )
    await world.step("review", _reviewed(PASSED, provider="chatgpt"))
    job = await _job(world, "verify")
    assert [c["name"] for c in job["commands"]] == [
        "note",
        "test 1",
        "test 2",
        "restore",
        "secrets",
    ]
    assert [c["argv"] for c in job["commands"][1:3]] == [
        ["npm", "run", "lint"],
        ["npm", "test", "--", "--run"],
    ]
    # What failed before still fails and is no regression; what passed before and
    # fails now is one.
    await _end(
        world,
        _log(
            [
                _ran("test 1", exit_code=1),
                _ran("test 2", exit_code=1, stderr="boom"),
                _ran("secrets"),
            ]
        ),
    )
    verified = next(s for s in world.run(run["id"])["steps"] if s["name"] == "verify")
    assert verified["outcome"] == "tests_failed"
    assert verified["detail"].startswith("1 test command(s) that passed before the change fail")
    await world.github.aclose()


@pytest.mark.asyncio
async def test_an_installation_that_fails_ends_the_run_and_nothing_to_do_is_said(
    world: Any,  # noqa: F811
) -> None:
    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    await _job(world, "prepare")
    await _end(world, CLONED)
    await _job(world, "install")
    await _end(world, _log([_ran("install 1", exit_code=1, stderr="ERR_PNPM_OUTDATED_LOCKFILE")]))
    ended = world.run(run["id"])
    assert (ended["state"], ended["outcome"]) == ("failed", "install_failed")
    assert "ERR_PNPM_OUTDATED_LOCKFILE" in ended["detail"]

    # A project that names nothing: both steps run nothing and say so.
    bare = world.runs.start_delivery(world.item_id, toolchain={})
    await _job(world, "prepare")
    await _end(world, CLONED)
    job = await _job(world, "install")
    assert [(c["name"], c["argv"]) for c in job["commands"]] == [("nothing", ["true"])]
    await _end(world, _log([_ran("nothing")]))
    job = await _job(world, "tests")
    assert [c["name"] for c in job["commands"]] == ["nothing"]
    await _end(world, _log([_ran("nothing")]))
    steps = world.run(bare["id"])["steps"]
    assert (steps[1]["outcome"], steps[1]["detail"]) == (
        "nothing_to_install",
        "The project names nothing to install.",
    )
    assert (steps[2]["outcome"], steps[2]["detail"]) == (
        "no_tests",
        "The project names no test commands, so nothing was run.",
    )
    assert world.run(bare["id"])["data"]["baseline"] == {}
    await world.github.aclose()


@pytest.mark.asyncio
async def test_a_flow_that_still_has_the_one_task_runs_as_before(world: Any) -> None:  # noqa: F811
    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    raw = _raw()
    raw["tasks"] = [task for task in raw["tasks"] if task["id"] not in ("install", "tests")]
    _task(raw, "prepare").update(
        task="workspace.checkout", next={"ok": "implement"}, **{"with": {"baseline": True}}
    )
    assert _rules(raw) == set()
    _in_force(world, raw)
    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    job = await _job(world, "prepare")
    assert [c["name"] for c in job["commands"]] == [
        "checkout",
        "install 1",
        "baseline 1",
        "baseline 2",
    ]
    await _end(
        world,
        _log(
            [
                _ran("checkout", f"{BASE}\n"),
                _ran("install 1"),
                _ran("baseline 1"),
                _ran("baseline 2"),
            ]
        ),
    )
    await _job(world, "implement")
    assert world.run(run["id"])["data"]["baseline"] == {"1": 0, "2": 0}
    await world.github.aclose()


# --- a project's own commands -----------------------------------------------------------------


def test_a_projects_commands_are_the_platforms_file_until_the_administrator_writes_them(
    world: Any,  # noqa: F811
) -> None:
    projects = ProjectRecords(world.sessions)
    usual = projects.commands("falcone-workflow")
    assert usual["own"] is False and usual["install"] == ["pnpm install --frozen-lockfile"]
    assert usual["tests"] == ["pnpm run typecheck", "pnpm run test"]
    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    assert [c["argv"] for c in run["data"]["toolchain"]["tests"]] == [
        ["pnpm", "run", "typecheck"],
        ["pnpm", "run", "test"],
    ]
    world.runs.request_cancel(UUID(run["id"]))

    for install, tests, said in (
        (['echo "x'], [], "To install: Command 1 has a quote that is not closed"),
        ([], ["[/etc] ls"], "To test: Command 1 names a folder that is not inside"),
        (["true"] * 13, [], "To install: The commands are a list of at most 12 lines"),
    ):
        with pytest.raises(ProjectInvalid, match=said):
            projects.set_commands("falcone-workflow", install=install, tests=tests)
    written = projects.set_commands(
        "falcone-workflow", install=["  npm ci ", ""], tests=["npm test", "[web] npm run lint"]
    )
    assert written["own"] is True and written["install"] == ["npm ci"]
    assert projects.commands("falcone-workflow")["tests"] == ["npm test", "[web] npm run lint"]
    # A run that starts now is told of them, whatever the caller read from the file.
    with world.sessions.begin() as session:
        from engineering_control.db.models import SandboxRun

        for active in session.query(SandboxRun).filter(SandboxRun.state == "queued"):
            active.state = "cancelled"
    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    told = run["data"]["toolchain"]
    assert [c["argv"] for c in told["prepare"]] == [["npm", "ci"]]
    assert [(c["cwd"], c["argv"]) for c in told["tests"]] == [
        (".", ["npm", "test"]),
        ("web", ["npm", "run", "lint"]),
    ]
    assert told["rules"] == world.toolchain["rules"], "what the file says of the rules stays"
    # No commands at all is a choice too: nothing is installed and nothing is run.
    assert projects.set_commands("falcone-workflow", install=[], tests=[])["tests"] == []
    assert projects.commands("falcone-workflow")["own"] is True
    back = projects.forget_commands("falcone-workflow")
    assert back["own"] is False and back["tests"] == ["pnpm run typecheck", "pnpm run test"]


def test_the_console_shows_and_changes_a_projects_commands_with_the_password(
    world: Any,  # noqa: F811
) -> None:
    from fastapi.testclient import TestClient

    from engineering_control.config import Settings
    from engineering_control.dashboard.app import create_app
    from engineering_control.dashboard.repository import DashboardRepository

    password = "correct horse battery staple"  # noqa: S105 - deterministic test fixture
    repository = DashboardRepository(str(world.sessions.kw["bind"].url))
    repository.initialize_password_sync(password)
    client = TestClient(
        create_app(
            settings=Settings(database_url=str(repository.engine.url)),
            repository=repository,
            session_key=b"g" * 48,
        ),
        base_url="https://ade.musematic.ai",
        headers={"Origin": "https://ade.musematic.ai"},
    )
    with client:
        assert client.get("/api/dashboard/projects/falcone-workflow/commands").status_code == 401
        token = client.get("/login/challenge").json()["csrf_token"]
        client.post("/login", data={"csrf_token": token, "password": password})

        def post(path: str, **fields: Any) -> httpx.Response:
            csrf = client.get("/api/dashboard/session").json()["csrf_token"]
            return client.post(f"/api/dashboard{path}", json={"csrf_token": csrf, **fields})

        path = "/projects/falcone-workflow/commands"
        shown = client.get(f"/api/dashboard{path}").json()
        assert shown["own"] is False and shown["tests"] == ["pnpm run typecheck", "pnpm run test"]
        assert client.get("/api/dashboard/projects/nobody/commands").status_code == 404
        assert post(path, install=["npm ci"], tests=["npm test"]).status_code == 422
        mistaken = "not the password"  # noqa: S105 - deterministic test fixture
        wrong = post(path, install=["npm ci"], tests=["npm test"], current_password=mistaken)
        assert wrong.status_code == 403
        assert client.get(f"/api/dashboard{path}").json()["own"] is False
        refused = post(path, install=['echo "x'], tests=[], current_password=password)
        assert refused.status_code == 422 and "quote" in refused.json()["detail"]
        done = post(path, install=["npm ci"], tests=["npm test"], current_password=password)
        assert done.status_code == 200
        assert done.json() == {
            "project": "falcone-workflow",
            "install": ["npm ci"],
            "tests": ["npm test"],
            "own": True,
            "note": None,
        }
        back = post(path, platform=True, current_password=password)
        assert back.status_code == 200 and back.json()["own"] is False
        # The designer is given the three tasks and not the one they took the place of.
        catalogue = client.get("/api/dashboard/flows/catalogue").json()
        assert catalogue["hidden"] == ["workspace.checkout"]
        kinds = {task["key"]: task for task in catalogue["tasks"]}
        assert kinds["workspace.clone"]["params"][0]["kind"] == "line"
        assert kinds["workspace.checkout"]["retired"]
        csrf = client.get("/api/dashboard/session").json()["csrf_token"]
        assert (
            client.post(
                "/api/dashboard/task-kinds/workspace.checkout/hidden",
                json={"csrf_token": csrf, "hidden": False},
            ).status_code
            == 422
        )


def test_the_task_that_was_replaced_is_listed_and_not_offered(world: Any) -> None:  # noqa: F811
    tasks = TaskLibrary(world.sessions, FlowRecords(world.sessions))
    assert tasks.hidden() == ["workspace.checkout"]
    kinds = {kind["key"]: kind for kind in tasks.listing()["kinds"]}
    assert kinds["workspace.checkout"]["hidden"] is True and kinds["workspace.checkout"]["retired"]
    assert kinds["workspace.clone"]["hidden"] is False
    assert [use["flow"] for use in kinds["workspace.clone"]["used_by"]] == list(BUILTIN)
    assert kinds["workspace.checkout"]["used_by"] == []
    with pytest.raises(ProjectInvalid, match="took its place"):
        tasks.hide("workspace.checkout", hidden=False)
