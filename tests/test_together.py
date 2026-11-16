"""Files that change together: the clone reads the names of what the branch's last commits
changed, and Find the files adds what changed with the files it found."""

# ruff: noqa: S603, S607 - git, with arguments this file wrote

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from engineering_control.engine import validate
from engineering_control.engine.builtin import BUILTIN
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.document import parse
from engineering_control.engine.records import FlowRecords
from engineering_control.sandbox import deliver
from tests.test_sandbox_delivery import (  # noqa: F401 - ``world`` is a fixture
    BASE,
    _log,
    _ran,
    world,
)
from tests.test_workbench import _load

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]


def _git(where: Path, *arguments: str) -> str:
    done = subprocess.run([*GIT, *arguments], cwd=where, check=True, capture_output=True)
    return done.stdout.decode().strip()


def _commit(work: Path, message: str, **files: str) -> str:
    for name, text in files.items():
        path = work / name.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", message)
    return _git(work, "rev-parse", "HEAD")


@pytest.fixture
def origin(tmp_path: Path) -> dict[str, Any]:
    """A repository as GitHub would serve it, with a history: the price and the schema
    change together three times, another file once, and one commit changes everything."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    names = {
        "price": "src__price.ts",
        "schema": "db__schema.sql",
        "other": "src__other.ts",
        "readme": "README.md",
    }
    shas = [
        _commit(
            work,
            "first",
            **{
                names["price"]: "export function roundPrice(cents: number) { return cents; }\n",
                names["schema"]: "create table prices (cents integer);\n",
                names["other"]: "export const unrelated = 1;\n",
                names["readme"]: "# The shop\n",
            },
        )
    ]
    for round_number in (2, 3):
        shas.append(
            _commit(
                work,
                f"price {round_number}",
                **{
                    names[
                        "price"
                    ]: f"export function roundPrice(c: number) {{ return c + {round_number}; }}\n",
                    names[
                        "schema"
                    ]: f"create table prices (cents integer, v{round_number} integer);\n",
                },
            )
        )
    shas.append(
        _commit(
            work,
            "price and other",
            **{
                names["price"]: "export function roundPrice(c: number) { return Math.round(c); }\n",
                names["other"]: "export const unrelated = 2;\n",
            },
        )
    )
    shas.append(_commit(work, "readme", **{names["readme"]: "# The shop, again\n"}))
    # A commit that touches very much says nothing of what belongs together.
    bulk = {f"gen__file{n}.ts": f"export const n{n} = {n};\n" for n in range(40)}
    shas.append(
        _commit(
            work,
            "bulk",
            **bulk,
            **{names["price"]: "// touched\n" + (work / "src/price.ts").read_text()},
        )
    )
    served = tmp_path / "served" / "acme" / "shop.git"
    served.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(served))
    _git(served, "config", "uploadpack.allowFilter", "true")
    _git(served, "config", "uploadpack.allowReachableSHA1InWant", "true")
    return {"served": tmp_path / "served", "shas": shas}


def _checkout(tmp_path: Path, origin: dict[str, Any], name: str, commit: str | None = None) -> Path:
    workspace = tmp_path / name
    workspace.mkdir()
    url = f"file://{origin['served']}/acme/shop.git"
    _git(
        workspace, "clone", "-q", "--depth", "1", "--branch", "main", "--single-branch", url, "repo"
    )
    if commit is not None:
        _git(workspace / "repo", "fetch", "-q", "--depth", "1", "origin", commit)
        _git(workspace / "repo", "checkout", "-q", "-B", "work", "FETCH_HEAD")
    return workspace


def _read_history(
    workspace: Path, origin: dict[str, Any], commits: int = 300
) -> subprocess.CompletedProcess[bytes]:
    """Run the clone's own command for the history, with GitHub's address led to the
    repository here."""
    command = deliver._history("acme/shop", "main", "Basic not-used", commits)
    assert command["allow_failure"] is True and command["name"] == "history"
    cache = workspace / ".cache"
    cache.mkdir(exist_ok=True)
    environment = {
        **os.environ,
        **command["env"],
        "CACHE": str(cache),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.file://{origin['served']}/.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://github.com/",
    }
    return subprocess.run(
        command["argv"], cwd=workspace, env=environment, capture_output=True, check=False
    )


def test_the_clone_reads_the_names_of_what_the_last_commits_changed(
    tmp_path: Path, origin: dict[str, Any]
) -> None:
    workspace = _checkout(tmp_path, origin, "one")
    done = _read_history(workspace, origin)
    assert done.returncode == 0, done.stderr.decode()
    assert done.stdout.decode().strip() == "6", "six commits, counted"
    record = (workspace / ".cache" / "history.txt").read_text()
    blocks = [block.split() for block in record.split("\x01") if block.strip()]
    assert [block[0] for block in blocks] == list(reversed(origin["shas"]))
    assert blocks[2][1:] == ["src/other.ts", "src/price.ts"], "names only"
    # What was fetched for it is gone, and the checkout is still one commit deep.
    assert not (workspace / ".cache" / "history.git").exists()
    assert _git(workspace / "repo", "rev-list", "--count", "HEAD") == "1"
    settings = _git(workspace / "repo", "config", "--list")
    assert "promisor" not in settings and "partialclone" not in settings

    # An evaluation works at an earlier commit: nothing later is read, so what was done
    # afterwards cannot say where to look.
    third = origin["shas"][2]
    earlier = _checkout(tmp_path, origin, "two", commit=third)
    done = _read_history(earlier, origin)
    assert done.returncode == 0 and done.stdout.decode().strip() == "3"
    record = (earlier / ".cache" / "history.txt").read_text()
    assert third in record and all(sha not in record for sha in origin["shas"][3:])

    # A history that does not reach the commit is no history: the command fails, which
    # stops nothing, and leaves no record.
    out_of_reach = _checkout(tmp_path, origin, "three", commit=origin["shas"][0])
    done = _read_history(out_of_reach, origin, commits=2)
    assert done.returncode != 0
    assert not (out_of_reach / ".cache" / "history.txt").exists()
    assert not (out_of_reach / ".cache" / "history.git").exists()


def test_find_adds_what_changed_together_with_the_files_it_found(
    tmp_path: Path, origin: dict[str, Any]
) -> None:
    workspace = _checkout(tmp_path, origin, "find")
    assert _read_history(workspace, origin).returncode == 0
    tools = _load(
        "tools",
        {
            "BENCH_REPO": str(workspace / "repo"),
            "BENCH_INDEX": str(workspace / ".cache" / "index.json"),
            "BENCH_HISTORY": str(workspace / ".cache" / "history.txt"),
        },
    )
    files = tools.index()["files"]
    changed, pairs, commits = tools.together(files)
    assert commits == 6
    assert changed["src/price.ts"] == 5 and changed["db/schema.sql"] == 3
    # The commit of forty files counts as a change of the price, and pairs it with nothing.
    # The first commit made all four files; the commit of forty pairs the price with nothing.
    assert pairs["src/price.ts"] == {"db/schema.sql": 3, "src/other.ts": 2, "README.md": 1}
    assert not any(name.startswith("gen/") for name in pairs)

    found = tools.find({"text": "roundPrice gives a wrong price", "limit": 6})
    by_path = {entry["path"]: entry for entry in found["files"]}
    assert found["history"] == 6 and found["files"][0]["path"] == "src/price.ts"
    # The schema names nothing of the question and is imported by nothing: it is there
    # because it changed with the price.
    assert by_path["db/schema.sql"]["why"] == [
        "it changed together with src/price.ts in 3 of 5 commits"
    ]
    # Twice together, and in a quarter of the price's commits: that counts as well.
    assert (
        "it changed together with src/price.ts in 2 of 5 commits" in by_path["src/other.ts"]["why"]
    )
    assert "README.md" not in by_path

    # Without neighbours, and without a record of the history, Find goes by the code alone.
    alone = tools.find({"text": "roundPrice gives a wrong price", "limit": 6, "neighbours": False})
    assert alone["history"] == 0 and "db/schema.sql" not in {e["path"] for e in alone["files"]}
    (workspace / ".cache" / "history.txt").unlink()
    without = tools.find({"text": "roundPrice gives a wrong price", "limit": 6})
    assert without["history"] == 0 and "db/schema.sql" not in {e["path"] for e in without["files"]}


@pytest.mark.asyncio
async def test_the_clone_task_reads_the_history_unless_it_is_told_not_to(world: Any) -> None:  # noqa: F811
    setting = next(p for p in TASKS["workspace.clone"].params if p.name == "history")
    assert (setting.kind, setting.default, setting.low, setting.high) == ("int", 300, 0, 1000)
    world.account("ChatGPT one", "chatgpt")
    world.account("Claude one", "claude")
    run = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    await world.launcher.tick()
    checkout, history = world.job()["commands"]
    assert (checkout["name"], history["name"]) == ("checkout", "history")
    assert history["env"]["COMMITS"] == "300" and history["allow_failure"] is True
    assert history["env"]["AUTHORIZATION"] == checkout["env"]["AUTHORIZATION"]
    assert "--filter=blob:none" in history["argv"][-1] and "--depth 1 " not in history["argv"][-1]
    world.fake.finish(_log([_ran("checkout", f"{BASE}\n"), _ran("history", "212\n")]))
    await world.launcher.tick()
    step = world.run(run["id"])["steps"][0]
    assert (step["state"], step["outcome"]) == ("succeeded", "cloned")
    assert step["detail"].endswith("What its last 212 commits changed was read, by name.")

    # A history that could not be read is said, and stops nothing.
    world.runs.request_cancel(__import__("uuid").UUID(run["id"]))
    await world.launcher.tick()
    world.fake.pods.clear()
    again = world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    await world.launcher.tick()
    world.fake.finish(
        _log([_ran("checkout", f"{BASE}\n"), _ran("history", exit_code=128, stderr="fatal")])
    )
    await world.launcher.tick()
    step = world.run(again["id"])["steps"][0]
    assert (
        step["outcome"] == "cloned"
        and "does not know which files change together" in step["detail"]
    )

    # A flow can say that none is read.
    world.runs.request_cancel(__import__("uuid").UUID(again["id"]))
    await world.launcher.tick()
    world.fake.pods.clear()
    raw = BUILTIN["deliver-issue"].as_dict()
    next(task for task in raw["tasks"] if task["id"] == "prepare")["with"] = {"history": 0}
    assert validate.errors(parse(raw)) == []
    flows = FlowRecords(world.sessions)
    flows.save_draft("deliver-issue", raw)
    flows.publish("deliver-issue")
    world.runs.start_delivery(world.item_id, toolchain=world.toolchain)
    await world.launcher.tick()
    assert [command["name"] for command in world.job()["commands"]] == ["checkout"]
    await world.github.aclose()
