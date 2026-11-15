"""The tasks of the workbench: finding the files a run is about, reading them, and
running a command of the project's.

They are tasks of a sandbox like the others (``plan`` and ``conclude``), carried out in
the sandbox the run keeps instead of one of their own, which is why each takes well
under a second where a sandbox of its own takes seconds to start. No agent and no model
takes part in them: each is a program with the same answer for the same input.
"""

from __future__ import annotations

import json
from typing import Any, Final

from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.tasks import (
    Concluded,
    Context,
    Done,
    Failed,
    SandboxTask,
    _about,
    _own_commands,
    _tail,
    _words,
)
from engineering_control.sandbox import deliver, steps
from engineering_control.sandbox.flows import Plan
from engineering_control.sandbox.runs import Lease
from engineering_control.sandbox.workbench import tools_source

_FOLDER: Final = "/tmp/bench"  # noqa: S108 - the pod's own empty volume
_ANSWERS: Final = "/workspace/.run"
QUESTION_CHARACTERS: Final = 20_000
FOUND_KEPT: Final = 80


def tool_job(
    name: str, question: dict[str, Any], *, max_bytes: int, seconds: int = 600
) -> dict[str, Any]:
    """A job that runs one of the workbench's tools on a question and hands its answer
    back whole."""
    asked, answered = f"{_FOLDER}/{name}.json", f"{_ANSWERS}/{name}.json"
    return {
        "files": [
            {"path": f"{_FOLDER}/tools.py", "content": tools_source()},
            {"path": asked, "content": json.dumps(question, separators=(",", ":"))},
        ],
        "commands": [
            {
                "name": name,
                "argv": ["python3", f"{_FOLDER}/tools.py", name, asked, answered],
                "cwd": deliver.REPO,
                "timeout": seconds,
                "echo": 20_000,
            }
        ],
        "artifacts": [{"name": name, "path": answered, "max_bytes": max_bytes}],
    }


def _bench(job: dict[str, Any], *, seconds: int = 900) -> Plan:
    return Plan(job, egress="none", workspace=True, bench=True, deadline_seconds=seconds)


def _answer(commands: list[dict[str, Any]], log: str, name: str) -> dict[str, Any] | str:
    """What the tool handed back, or why it handed nothing back."""
    last = commands[-1]
    if last.get("exit_code") != 0 or last.get("timed_out"):
        return _tail(last) or "the tool ended without saying why"
    found = steps.parse_artifact(log, name)
    return found if found is not None else "the tool handed nothing back"


def question_of(ctx: Context) -> str:
    """What a run is about, as words to look for: its item, what the task adds, and what
    an earlier task found or what failed."""
    about = _about(ctx.data)
    parts = [
        str(ctx.params.get("words") or ""),
        str(about.get("title") or ""),
        str(about.get("body") or ""),
    ]
    for finding in ctx.given.get("findings") or []:
        if isinstance(finding, dict):
            parts.append(f"{finding.get('file') or ''} {finding.get('description') or ''}")
    parts += [str(failure) for failure in ctx.given.get("failures") or []]
    return "\n".join(part for part in parts if part.strip())[:QUESTION_CHARACTERS]


class Find:
    """Find the files the run is about, on the map of the code."""

    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        question = {
            "text": question_of(ctx),
            "limit": int(ctx.params.get("limit") or 20),
            "neighbours": bool(ctx.params.get("neighbours", True)),
        }
        return _bench(tool_job("find", question, max_bytes=2_000_000))

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        found = _answer(commands, log, "find")
        if isinstance(found, str):
            return Failed("find_failed", f"The files could not be looked for: {found}")
        files = [entry for entry in found.get("files") or [] if isinstance(entry, dict)]
        paths = [str(entry.get("path")) for entry in files][:FOUND_KEPT]
        indexed = found.get("indexed") or {}
        mapped = (
            f" The map has {indexed.get('code', 0):,} files of code and "
            f"{indexed.get('imports', 0):,} imports between them."
        )
        if not paths:
            return Done(
                "none",
                "nothing_found",
                "No file of the repository matches what the run is about." + mapped,
                output=found,
                data={"found": []},
            )
        first = ", ".join(paths[:3]) + (" and others" if len(paths) > 3 else "")
        return Done(
            "found",
            "found",
            f"{len(paths)} files: {first}." + mapped,
            output=found,
            data={"found": paths},
        )


def paths_of(value: Any) -> list[str]:
    """The paths a task names, as written on one line."""
    return [part for part in str(value or "").replace(",", " ").split() if part]


class Read:
    """Read files for the tasks that follow: the ones the task names, or the ones found."""

    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        named = paths_of(ctx.params.get("paths"))
        paths = named or [str(path) for path in ctx.data.get("found") or []]
        if not paths:
            raise ValueError(
                f"{ctx.node.id} has no files to read: it names none, and no task before it "
                "found any"
            )
        question = {
            "paths": paths,
            "lines": int(ctx.params.get("lines") or 600),
            "characters": int(ctx.params.get("characters") or 200_000),
        }
        return _bench(tool_job("read", question, max_bytes=3_000_000))

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        found = _answer(commands, log, "read")
        if isinstance(found, str):
            return Failed("read_failed", f"The files could not be read: {found}")
        files = [entry for entry in found.get("files") or [] if isinstance(entry, dict)]
        left = [entry for entry in found.get("left_out") or [] if isinstance(entry, dict)]
        if not files:
            why = "; ".join(f"{e.get('path')}: {e.get('why')}" for e in left[:5])
            return Failed("nothing_read", f"None of the files could be read. {why}"[:600], found)
        cut = sum(1 for entry in files if entry.get("cut"))
        characters = sum(len(str(entry.get("text") or "")) for entry in files)
        said = f"{len(files)} files, {characters:,} characters."
        if cut:
            said += f" {cut} are not whole: only their first lines were read."
        if left:
            said += f" {len(left)} were left out: " + "; ".join(
                f"{e.get('path')} ({e.get('why')})" for e in left[:3]
            )
        return Done(
            "ok",
            "read",
            said[:600],
            output=found,
            data={
                "sources": [
                    {key: entry.get(key) for key in ("path", "lines", "shown", "cut")}
                    for entry in files
                ]
            },
        )


class Run:
    """Run commands of the project's in the workbench and say whether they passed. It is
    a quick look, not the tests a change has to pass before it is published."""

    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        commands, _own = _own_commands(ctx, "tests")
        if not commands:
            raise ValueError(f"{ctx.node.id} has no command to run, and the project names no tests")
        job = {
            "commands": [
                deliver._in_repo(command, f"check {index}", allow_failure=True)
                for index, command in enumerate(commands, start=1)
            ]
        }
        seconds = sum(command.timeout for command in commands) + 300
        return _bench(job, seconds=seconds)

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        named, _own = _own_commands(ctx, "tests")
        lines = [deliver.line_of(command) for command in named]

        def line(index: int) -> str:
            return lines[index] if index < len(lines) else f"command {index + 1}"

        failed = [
            index
            for index, command in enumerate(commands)
            if command.get("exit_code") != 0 or command.get("timed_out")
        ]
        output = {
            "commands": [
                {"line": line(index), "passed": index not in failed, "seconds": c.get("seconds")}
                for index, c in enumerate(commands)
            ]
        }
        if not failed:
            return Done("passed", "passed", f"{len(commands)} command(s) passed.", output=output)
        return Done(
            "failed",
            "failed",
            f"{len(failed)} of {len(commands)} command(s) failed: "
            + _words("; ".join(line(index) for index in failed), 300),
            output=output,
            given={
                "failures": [f"{line(index)}\n{_tail(commands[index], 3000)}" for index in failed]
            },
        )


BENCH: Final[dict[str, SandboxTask]] = {
    "code.find": Find(),
    "code.read": Read(),
    "code.run": Run(),
}
assert set(BENCH) == {  # noqa: S101 - the catalogue and the code agree
    key for key, task in TASKS.items() if task.where == "bench"
}
