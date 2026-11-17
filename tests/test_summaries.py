"""Summaries of the code: what a model said each file is for, kept with the project by
the content it was written from, made a batch at a time, and used where files are found."""

# Long expected texts come with what is tested here.
# ruff: noqa: E501, S105

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from engineering_control.dashboard.project_records import ProjectConflict
from engineering_control.db.models import CodeSummary
from engineering_control.engine import validate
from engineering_control.engine.asking import _found
from engineering_control.engine.builtin import summarise_code
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.summaries import Summaries
from tests.test_model_edits import (  # noqa: F401 - fixtures
    EDITS,
    _editing,
    _to_the_model,
    stage,
)
from tests.test_models_api import said
from tests.test_sandbox_delivery import BASE, _log, _ran, world  # noqa: F401 - fixtures
from tests.test_workbench import (  # noqa: F401 - fixtures
    _bench_pod,
    _running,
    _steps,
    _tool_log,
    repo,
    tools,
)

PROJECT = "falcone-workflow"


# --- the records -----------------------------------------------------------------------------


def test_a_summary_is_kept_by_file_with_the_content_it_was_written_from(world: Any) -> None:  # noqa: F811
    summaries = Summaries(world.sessions)
    assert summaries.known(PROJECT) == {} and summaries.state(PROJECT)["files"] == 0
    run = uuid4()
    stored = summaries.store(
        PROJECT,
        [
            {
                "path": "src/session.ts",
                "blob": "a" * 40,
                "summary": "Keeps a signed-in session in a cookie and renews it before it expires.",
            },
            {
                "path": "src/price.ts",
                "blob": "b" * 40,
                "summary": "Rounds prices in cents and formats them as text.",
            },
            {"path": "src/odd.ts", "blob": "c" * 40, "summary": ""},
        ],
        provider="deepseek",
        model="model-a",
        run_id=run,
    )
    assert stored == 3
    assert summaries.known(PROJECT) == {
        "src/session.ts": "a" * 40,
        "src/price.ts": "b" * 40,
        "src/odd.ts": "c" * 40,
    }
    # A file that was asked about and got no summary is known, and has none to show.
    assert summaries.of(PROJECT, ["src/session.ts", "src/odd.ts", "src/none.ts"]) == {
        "src/session.ts": {
            "blob": "a" * 40,
            "summary": "Keeps a signed-in session in a cookie and renews it before it expires.",
        }
    }
    state = summaries.state(PROJECT)
    assert (state["files"], state["without"], state["models"]) == (2, 1, ["model-a"])
    assert state["latest"] is not None

    # A file summarised again keeps one row, with the new content's name.
    summaries.store(
        PROJECT,
        [{"path": "src/price.ts", "blob": "d" * 40, "summary": "Rounds prices half up."}],
        provider="kimi",
        model="kimi-k2",
        run_id=None,
    )
    assert summaries.known(PROJECT)["src/price.ts"] == "d" * 40
    with world.sessions() as session:
        assert session.query(CodeSummary).count() == 3

    # What a run is about is looked for in the summaries: a word a summary has counts
    # for its file, with the words it was.
    hints = summaries.hints(PROJECT, "Signing in fails with an expired cookie")
    assert [hint["path"] for hint in hints] == ["src/session.ts"]
    assert set(hints[0]["terms"]) >= {"cookie"} and 0 < hints[0]["score"] <= 12
    assert summaries.hints(PROJECT, "nothing of the kind") == []
    assert summaries.hints("no-such-project", "cookie") == [] and summaries.known("nope") == {}

    assert summaries.forget(PROJECT, ["src/odd.ts", "src/never.ts"]) == 1
    assert "src/odd.ts" not in summaries.known(PROJECT)
    assert summaries.clear(PROJECT) == 2 and summaries.known(PROJECT) == {}


# --- the tool --------------------------------------------------------------------------------


def test_the_files_without_a_summary_are_picked_the_most_imported_first(
    tools: Any,  # noqa: F811
    repo: Any,  # noqa: F811
) -> None:
    files = tools.index()["files"]
    first = tools.digest({"known": {}, "limit": 3})
    code = sorted(
        path
        for path, entry in files.items()
        if entry["lang"] and not entry["test"] and entry.get("lines", 0) >= 3
    )
    assert first["total"] == len(code) and first["fresh"] == 0 and first["gone"] == []
    assert len(first["files"]) == 3 and first["left"] == len(code) - 3
    # What most of the code imports comes first: it is what a summary helps most with.
    users = {
        path: sum(1 for entry in files.values() if path in entry.get("imports", []))
        for path in code
    }
    picked = [entry["path"] for entry in first["files"]]
    assert [users[path] for path in picked] == sorted((users[path] for path in code), reverse=True)[
        :3
    ]
    entry = first["files"][0]
    assert entry["hash"] == files[entry["path"]]["hash"] and entry["text"] and not entry["cut"]
    assert all(not files[path]["test"] for path in picked)

    # What has a summary of what it now says is not picked again; what changed since is.
    known = {path: files[path]["hash"] for path in picked[:2]} | {
        picked[2]: "0" * 40,
        "gone/away.ts": "1" * 40,
    }
    again = tools.digest({"known": known, "limit": 40})
    again_paths = [entry["path"] for entry in again["files"]]
    assert (
        picked[0] not in again_paths and picked[1] not in again_paths and picked[2] in again_paths
    )
    assert again["fresh"] == 2 and again["gone"] == ["gone/away.ts"] and again["left"] == 0
    # Tests are summarised only when asked for; a long file is read from its start.
    with_tests = tools.digest({"known": {}, "limit": 40, "tests": True})
    assert with_tests["total"] > first["total"]
    short = tools.digest({"known": {}, "limit": 40, "lines": 20, "characters": 2000})
    assert sum(len(entry["text"]) for entry in short["files"]) <= 2000 + max(
        len(e["text"]) for e in short["files"]
    )
    # Told to stop, it picks nothing and says what waits.
    stopped = tools.digest({"known": {}, "stop": True})
    assert stopped["files"] == [] and stopped["left"] == len(code)

    # Find the files counts what a file's summary says, and names the content it found.
    found = tools.find(
        {
            "text": "where is the monthly allowance checked",
            "limit": 5,
            "hints": [
                {
                    "path": "services/api/app/quota.py",
                    "score": 9.0,
                    "terms": ["allowance", "monthly"],
                },
                {"path": "not/there.py", "score": 9.0, "terms": ["allowance"]},
                {"path": "README.md", "score": 9.0, "terms": ["allowance"]},
            ],
        }
    )
    by_path = {entry["path"]: entry for entry in found["files"]}
    assert "its summary speaks of allowance, monthly" in by_path["services/api/app/quota.py"]["why"]
    assert (
        by_path["services/api/app/quota.py"]["hash"] == files["services/api/app/quota.py"]["hash"]
    )
    assert "not/there.py" not in by_path and "README.md" not in by_path


# --- the tasks -------------------------------------------------------------------------------


def test_two_tasks_make_the_summaries_and_a_run_of_a_project_carries_them() -> None:
    pick, summarise = TASKS["code.digest"], TASKS["model.summarise"]
    assert (pick.where, pick.loops, pick.outcomes) == ("bench", True, ("files", "none"))
    assert (summarise.where, summarise.outcomes) == ("model", ("written", "nothing"))
    assert [param.name for param in summarise.params] == [
        "provider",
        "model",
        "then_provider",
        "then_model",
        "effort",
        "tokens",
        "minutes",
        "agent",
    ]
    document = summarise_code(provider="deepseek", model="deepseek-chat", steps=8, dollars=3)
    assert validate.errors(document) == []
    assert [node.task for node in document.nodes] == [
        "workspace.clone",
        "code.digest",
        "model.summarise",
    ]
    assert document.budgets["dollars"] == 3 and document.node("pick").params["steps"] == 8  # type: ignore[union-attr]
    # Nothing in it writes, reviews or publishes: it changes no code.
    assert not any(TASKS[node.task].writes or TASKS[node.task].publishes for node in document.nodes)
    # Without a model, or an agent that has one, it cannot run.
    wrong = [p["text"] for p in validate.errors(summarise_code(provider="deepseek", model=""))]
    assert wrong == [
        "summarise does not name the model it asks: write the name the provider gives it"
    ]
    assert validate.errors(summarise_code(provider="deepseek", model="", agent=str(uuid4()))) == []


def _batch(
    *paths: str, left: int = 0, total: int = 4, fresh: int = 0, gone: tuple[str, ...] = ()
) -> str:
    answer = {
        "files": [
            {
                "path": path,
                "hash": f"{number}" * 40,
                "lines": 12,
                "shown": 12,
                "cut": False,
                "text": f"// {path}\nexport const value = {number};",
            }
            for number, path in enumerate(paths, 1)
        ],
        "left": left,
        "total": total,
        "fresh": fresh,
        "gone": list(gone),
    }
    return _tool_log("digest", answer)


@pytest.mark.asyncio
async def test_a_run_summarises_a_projects_code_batch_by_batch(stage: Any) -> None:  # noqa: F811
    summaries = Summaries(stage.sessions)
    summaries.store(
        PROJECT,
        [{"path": "old/gone.ts", "blob": "9" * 40, "summary": "Was here."}],
        provider=None,
        model=None,
        run_id=None,
    )
    account = next(a for a in stage.accounts.listing()["items"] if a["provider"] == "deepseek")
    stage.accounts.update(UUID(account["id"]), prices={"model-a": {"input": 100, "output": 400}})
    document = summarise_code(provider="deepseek", model="model-a", steps=3, dollars=5)
    run = stage.runs.start_summaries(PROJECT, document=document, toolchain=stage.toolchain)
    assert run["title"] == "Summarising the code of Falcone workflow"
    with pytest.raises(ProjectConflict, match="being summarised already"):
        stage.runs.start_summaries(PROJECT, document=document, toolchain=stage.toolchain)

    await stage.launcher.tick()
    # The clone, without the history: nothing here looks at what changes together.
    assert [command["name"] for command in stage.job()["commands"]] == ["checkout"]
    stage.fake.finish(_log([_ran("checkout", f"{BASE}\n")]))
    await stage.launcher.tick()
    await stage.launcher.tick()
    bench = _bench_pod(stage, run["id"])
    assert bench is not None
    _running(bench)

    # The first batch: two files, of which the model summarises one.
    stage.door.answers = [_batch("src/session.ts", "src/price.ts", left=2, gone=("old/gone.ts",))]
    stage.provider.answers = [
        said(
            {
                "summaries": [
                    {"path": "src/session.ts", "summary": "Keeps a signed-in session in a cookie."},
                    {"path": "not/asked.ts", "summary": "Made up."},
                    {"path": "src/session.ts", "summary": "Said twice."},
                ]
            }
        )
    ]
    for _pass in range(3):
        await stage.launcher.tick()
        # A question that is out is waited for, so that the next pass is not made
        # before the provider was asked.
        out = list(stage.launcher._calls.values())
        if out:
            await asyncio.wait(out, timeout=10)
        if stage.provider.asked:
            break
    key, job = stage.door.handed[-1]
    question = json.loads(job["files"][1]["content"])
    assert question == {
        "known": {"old/gone.ts": "9" * 40},
        "limit": 12,
        "lines": 300,
        "characters": 60000,
        "tests": False,
        "stop": False,
    }
    asked = stage.provider.asked[-1][2]["messages"][1]["content"]
    # What the run is about comes first, the question after it, and the files last.
    assert asked.startswith("The repository is gntik-ai/falcone-workflow.")
    assert asked.index("Summarise each of the files below") < asked.index("The file src/session.ts")
    assert "The file src/session.ts:\n<<<material\n// src/session.ts" in asked
    assert "The file src/price.ts:" in asked and "What you are asked" not in asked

    # The second batch is picked knowing what the first left, and the summarised file
    # that is gone from the repository is forgotten.
    stage.door.answers = [_batch("src/cart.ts", left=1, fresh=2)]
    stage.provider.answers = [
        said({"summaries": [{"path": "src/cart.ts", "summary": "Holds the cart."}]})
    ]
    await asyncio.wait(list(stage.launcher._calls.values()), timeout=10)
    await stage.launcher.tick()
    question = json.loads(stage.door.handed[-1][1]["files"][1]["content"])
    assert question["known"] == {"src/session.ts": "1" * 40, "src/price.ts": "2" * 40}
    assert question["stop"] is False

    # The third time it is come to is the last it may be: it picks nothing, and the run ends.
    stage.door.answers = [_batch(left=1, fresh=3)]
    await asyncio.wait(list(stage.launcher._calls.values()), timeout=10)
    await stage.launcher.tick()
    assert json.loads(stage.door.handed[-1][1]["files"][1]["content"])["stop"] is True
    await stage.launcher.tick()
    ended = stage.run(run["id"])
    assert (ended["state"], ended["outcome"]) == ("succeeded", "enough"), ended["detail"]
    assert ended["detail"] == (
        "Enough for this run: 3 of 4 files have a summary of what they now say, 1 are still "
        "to be summarised."
    )
    path = [(step["name"], step["outcome"]) for step in ended["steps"]]
    assert path == [
        ("prepare", "cloned"),
        ("pick", "picked"),
        ("summarise", "summarised"),
        ("pick", "picked"),
        ("summarise", "summarised"),
        ("pick", "enough"),
    ]
    first = ended["steps"][2]
    assert first["detail"] == "Summaries of 1 files were written. 1 got none."
    assert first["output"] == {
        "stored": 1,
        "paths": ["src/session.ts"],
        "skipped": ["src/price.ts"],
    }
    assert first["usage"]["cost"] > 0 and first["usage"]["provider"] == "deepseek"

    assert summaries.known(PROJECT) == {
        "src/session.ts": "1" * 40,
        "src/price.ts": "2" * 40,
        "src/cart.ts": "1" * 40,
    }
    assert summaries.of(PROJECT, ["src/session.ts", "src/price.ts"]) == {
        "src/session.ts": {"blob": "1" * 40, "summary": "Keeps a signed-in session in a cookie."}
    }
    state = summaries.state(PROJECT)
    assert (state["files"], state["without"], state["models"]) == (2, 1, ["model-a-0915"])
    await stage.http.aclose()
    await stage.github.aclose()


@pytest.mark.asyncio
async def test_what_is_found_comes_with_what_the_summaries_say(stage: Any) -> None:  # noqa: F811
    summaries = Summaries(stage.sessions)
    file = "packages/billing/src/money.ts"
    summaries.store(
        PROJECT,
        [
            {
                "path": file,
                "blob": "f" * 40,
                "summary": "Rounds money in cents; an expired cookie is not its business.",
            },
            {
                "path": "src/session.ts",
                "blob": "e" * 40,
                "summary": "Keeps the sign-in cookie and says when it has expired.",
            },
        ],
        provider="deepseek",
        model="model-a",
        run_id=None,
    )
    stage.provider.answers = [said(EDITS)]
    run = await _to_the_model(stage, _editing())
    found = {
        "files": [
            {"path": path, "score": 9.5, "lines": 40, "test": False, "why": ["it defines cookie"]}
            for path in (file, "src/session.ts")
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
        "history": 0,
    }
    found["files"][0]["hash"] = "f" * 40
    found["files"][1]["hash"] = "0" * 40
    stage.door.answers = [_tool_log("find", found)]
    await stage.launcher.tick()
    # The tool was told which files' summaries speak of what the run is about.
    asked = json.loads(stage.door.handed[0][1]["files"][1]["content"])
    assert {hint["path"] for hint in asked["hints"]} == {file, "src/session.ts"}
    assert all("cookie" in hint["terms"] or "expired" in hint["terms"] for hint in asked["hints"])
    # What was found is kept with each file's summary, and with whether the file has
    # changed since the summary was written.
    kept = {entry["path"]: entry for entry in _steps(stage, run["id"])["find"]["output"]["files"]}
    assert (
        kept[file]["summary"].startswith("Rounds money in cents") and "summary_of" not in kept[file]
    )
    assert kept["src/session.ts"]["summary_of"] == "an earlier version of the file"

    # A loop's model is shown what the last search found, a line a file.
    shown = _found(stage.runs.happened(UUID(run["id"])))
    assert shown.splitlines() == [
        f"- {file} (40 lines): it defines cookie. What it is for: Rounds money in cents; an expired cookie is not its business.",
        "- src/session.ts (40 lines): it defines cookie. What it is for (said of an earlier version of the file): Keeps the sign-in cookie and says when it has expired.",
    ]
    assert (
        _found([]) == ""
        and _found([{"state": "succeeded", "output": {"files": [{"path": "a", "text": "x"}]}}])
        == ""
    )
    await stage.http.aclose()
    await stage.github.aclose()


# --- the console -----------------------------------------------------------------------------


def test_the_console_says_how_much_is_summarised_and_starts_a_run(stage: Any) -> None:  # noqa: F811
    from fastapi.testclient import TestClient

    from engineering_control.config import Settings
    from engineering_control.dashboard.app import create_app
    from engineering_control.dashboard.repository import DashboardRepository

    password = "correct horse battery staple"
    repository = DashboardRepository(str(stage.sessions.kw["bind"].url))
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
        token = client.get("/login/challenge").json()["csrf_token"]
        client.post("/login", data={"csrf_token": token, "password": password})

        def post(path: str, **fields: Any) -> httpx.Response:
            csrf = client.get("/api/dashboard/session").json()["csrf_token"]
            return client.post(f"/api/dashboard{path}", json={"csrf_token": csrf, **fields})

        state = client.get(f"/api/dashboard/projects/{PROJECT}/summaries").json()
        assert (state["files"], state["without"], state["latest"]) == (0, 0, None)
        assert client.get("/api/dashboard/projects/no-such/summaries").status_code == 404
        for wrong, why in (
            ({"provider": "claude", "model": "x"}, "provider"),
            ({"provider": "deepseek", "model": ""}, "does not name the model it asks"),
            ({"provider": "deepseek", "model": "two words"}, "model"),
            (
                {"provider": "deepseek", "model": "", "agent": str(uuid4())},
                "That agent is not there any more",
            ),
        ):
            refused = post(f"/projects/{PROJECT}/summaries", **wrong)
            assert refused.status_code == 422 and why in refused.json()["detail"], refused.text
        assert (
            post(
                f"/projects/{PROJECT}/summaries", provider="deepseek", model="m", dollars=0
            ).status_code
            == 422
        )
        started = post(
            f"/projects/{PROJECT}/summaries",
            provider="deepseek",
            model="model-a",
            batches=4,
            dollars=2,
        )
        assert started.status_code == 201, started.text
        run = client.get(f"/api/dashboard/runs/{started.json()['id']}").json()
        assert run["title"] == "Summarising the code of Falcone workflow"
        again = post(f"/projects/{PROJECT}/summaries", provider="deepseek", model="model-a")
        assert again.status_code == 409 and "being summarised already" in again.json()["detail"]
        # Four batches: the picking task may be come to five times, the last to say what is left.
        with stage.sessions() as session:
            from engineering_control.db.models import SandboxRun

            carried = session.get(SandboxRun, UUID(started.json()["id"])).data["flow_document"]
        pick = next(task for task in carried["tasks"] if task["id"] == "pick")
        assert pick["with"]["steps"] == 5 and carried["budgets"]["dollars"] == 2

        Summaries(stage.sessions).store(
            PROJECT,
            [{"path": "a.ts", "blob": "1" * 40, "summary": "A."}],
            provider="deepseek",
            model="model-a",
            run_id=None,
        )
        assert client.get(f"/api/dashboard/projects/{PROJECT}/summaries").json()["files"] == 1
        dropped = post(f"/projects/{PROJECT}/summaries/forget")
        assert dropped.status_code == 200 and dropped.json()["dropped"] == 1
        assert client.get(f"/api/dashboard/projects/{PROJECT}/summaries").json()["files"] == 0
    asyncio.run(stage.http.aclose())
    asyncio.run(stage.github.aclose())
