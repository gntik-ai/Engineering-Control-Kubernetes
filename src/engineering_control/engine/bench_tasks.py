"""The tasks of the workbench: finding the files a run is about, reading them, and
running a command of the project's.

They are tasks of a sandbox like the others (``plan`` and ``conclude``), carried out in
the sandbox the run keeps instead of one of their own, which is why each takes well
under a second where a sandbox of its own takes seconds to start. No agent and no model
takes part in them: each is a program with the same answer for the same input.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Final

from engineering_control.engine.catalogue import TASKS, loop_steps
from engineering_control.engine.tasks import (
    Concluded,
    Context,
    Done,
    Failed,
    SandboxTask,
    _about,
    _last_line,
    _own_commands,
    _tail,
    _words,
)
from engineering_control.models.providers import family_of
from engineering_control.sandbox import deliver, steps
from engineering_control.sandbox.flows import Plan
from engineering_control.sandbox.runs import Lease
from engineering_control.sandbox.workbench import tools_source

_FOLDER: Final = "/tmp/bench"  # noqa: S108 - the pod's own empty volume
_ANSWERS: Final = "/workspace/.run"
QUESTION_CHARACTERS: Final = 20_000
RELATED_MINUTES: Final = 10
# How much text of files the picker hands back at a time, in all its groups.
DIGEST_CHARACTERS: Final = 1_200_000
FOUND_KEPT: Final = 80


def tool_job(
    name: str,
    question: dict[str, Any],
    *,
    max_bytes: int,
    seconds: int = 600,
    env: dict[str, str] | None = None,
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
                **({"env": env} if env else {}),
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
    asked = str((ctx.given.get("asked") or {}).get("words") or "").strip()
    if asked:
        # In a loop, the model says what to look for: that, and nothing it did not ask.
        return f"{ctx.params.get('words') or ''}\n{asked}".strip()[:QUESTION_CHARACTERS]
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
        question: dict[str, Any] = {
            "text": question_of(ctx),
            "limit": int(ctx.params.get("limit") or 20),
            "neighbours": bool(ctx.params.get("neighbours", True)),
        }
        summaries = ctx.services.summaries
        if summaries is not None and ctx.data.get("project"):
            # What the summaries of the project's files say of what is asked.
            question["hints"] = await asyncio.to_thread(
                summaries.hints, str(ctx.data["project"]), question["text"]
            )
        return _bench(tool_job("find", question, max_bytes=2_000_000))

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        found = _answer(commands, log, "find")
        if isinstance(found, str):
            return Failed("find_failed", f"The files could not be looked for: {found}")
        files = [entry for entry in found.get("files") or [] if isinstance(entry, dict)]
        paths = [str(entry.get("path")) for entry in files][:FOUND_KEPT]
        summaries = ctx.services.summaries
        if summaries is not None and ctx.data.get("project") and paths:
            # Each file found is shown with what its summary says, so that what to read
            # can be chosen without reading it.
            known = await asyncio.to_thread(summaries.of, str(ctx.data["project"]), paths)
            for entry in files:
                has = known.get(str(entry.get("path")))
                if has:
                    entry["summary"] = has["summary"]
                    if has["blob"] != entry.get("hash"):
                        entry["summary_of"] = "an earlier version of the file"
        indexed = found.get("indexed") or {}
        mapped = (
            f" The map has {indexed.get('code', 0):,} files of code and "
            f"{indexed.get('imports', 0):,} imports between them."
        )
        history = found.get("history")
        if isinstance(history, int) and history > 0:
            mapped += f" What {history:,} commits changed says what changes together."
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
        asked = [str(path) for path in (ctx.given.get("asked") or {}).get("paths") or []]
        named = paths_of(ctx.params.get("paths"))
        # What the model of a loop asks for, or else what the task names, or what was found.
        paths = asked or named or [str(path) for path in ctx.data.get("found") or []]
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

    @staticmethod
    def _chosen(ctx: Context) -> tuple[deliver.Command, ...]:
        """The commands to run: all the task has, or the one a loop's model asked for
        by its number. A number the task has no command for means all of them."""
        commands, _own = _own_commands(ctx, "tests")
        try:
            number = int((ctx.given.get("asked") or {}).get("command") or 0)
        except (TypeError, ValueError):
            number = 0
        return (commands[number - 1],) if 1 <= number <= len(commands) else commands

    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        commands = self._chosen(ctx)
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
        lines = [deliver.line_of(command) for command in self._chosen(ctx)]

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


class Digest:
    """Pick the next files to summarise: the code that has no summary of what it now
    says. A loop goes round it: every time it is come to, it picks the next batch."""

    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        summaries = ctx.services.summaries
        if summaries is None or not ctx.data.get("project"):
            raise ValueError(f"{ctx.node.id} has no project to keep summaries for")
        count = int((ctx.given.get("loops") or {}).get(ctx.node.id) or 1)
        characters = int(ctx.params.get("characters") or 60_000)
        question = {
            "known": await asyncio.to_thread(summaries.known, str(ctx.data["project"])),
            "limit": int(ctx.params.get("limit") or 12),
            "lines": int(ctx.params.get("lines") or 300),
            "characters": characters,
            # As many groups as the task says, and no more than can be handed back.
            "groups": max(
                1, min(int(ctx.params.get("groups") or 1), DIGEST_CHARACTERS // characters)
            ),
            "tests": bool(ctx.params.get("tests", False)),
            # The last time it may be come to, it picks nothing and says what is left.
            "stop": count >= loop_steps(ctx.params),
        }
        return _bench(tool_job("digest", question, max_bytes=1_500_000))

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        found = _answer(commands, log, "digest")
        if isinstance(found, str):
            return Failed("digest_failed", f"The files could not be picked: {found}")
        summaries = ctx.services.summaries
        gone = [str(path) for path in found.get("gone") or []]
        if summaries is not None and gone:
            await asyncio.to_thread(summaries.forget, str(ctx.data["project"]), gone)
        files = [entry for entry in found.get("files") or [] if isinstance(entry, dict)]
        left, total = int(found.get("left") or 0), int(found.get("total") or 0)
        fresh = int(found.get("fresh") or 0)
        counted = {"left": left, "total": total, "fresh": fresh, "gone": len(gone)}
        if not files:
            if left == 0:
                return Done(
                    "none",
                    "all_summarised",
                    f"Every one of the {total:,} files of code has a summary of what it now says.",
                    output=counted,
                )
            return Done(
                "none",
                "enough",
                f"Enough for this run: {fresh:,} of {total:,} files have a summary of what "
                f"they now say, {left:,} are still to be summarised.",
                output=counted,
            )
        paths = [str(entry.get("path")) for entry in files]
        first = ", ".join(paths[:3]) + (" and others" if len(paths) > 3 else "")
        groups = len({int(entry.get("group") or 1) for entry in files})
        return Done(
            "files",
            "picked",
            _words(
                f"{len(files)} files to summarise"
                + (f", in {groups} groups" if groups > 1 else "")
                + f": {first}. {left:,} more wait.",
                500,
            ),
            output={**counted, "files": files},
        )


class RelatedTests:
    """Run the tests that cover what was changed, by the repository's own runner. It is a
    quick look, not the tests a change has to pass before it is published."""

    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        asked = [str(path) for path in (ctx.given.get("asked") or {}).get("paths") or []]
        minutes = max(1, min(int(ctx.params.get("minutes") or RELATED_MINUTES), 30))
        question = {
            # What a loop's model names, or what the task names, or what the run changed.
            "paths": asked or paths_of(ctx.params.get("paths")),
            "base": str(ctx.data.get("base_sha") or ""),
            "limit": int(ctx.params.get("limit") or 20),
            "seconds": minutes * 60,
        }
        named = ctx.params.get("commands")
        if isinstance(named, list) and named:
            # The flow says how the related tests are run: a build first, a runner the
            # repository does not name.
            question["commands"] = [
                {"argv": list(command.argv), "cwd": command.cwd, "timeout": command.timeout}
                for command in deliver.commands_of(named)
            ]
        job = tool_job(
            "tests", question, max_bytes=600_000, seconds=minutes * 60 + 60, env=deliver.TOOLS
        )
        return _bench(job, seconds=minutes * 60 + 360)

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        found = _answer(commands, log, "tests")
        if isinstance(found, str):
            return Failed("tests_failed", f"The related tests could not be run: {found}")
        related = [entry for entry in found.get("tests") or [] if isinstance(entry, dict)]
        ran = [entry for entry in found.get("ran") or [] if isinstance(entry, dict)]
        left = [entry for entry in found.get("unrunnable") or [] if isinstance(entry, dict)]
        changed = found.get("changed") or []
        said_left = (
            f" {len(left)} could not be run: "
            + "; ".join(f"{e.get('path')} ({e.get('why')})" for e in left[:3])
            if left
            else ""
        )
        if not ran:
            why = (
                "Nothing was changed, so there are no tests to run."
                if not changed
                else f"No test covers the {len(changed)} files that were changed."
                if not related
                else f"None of the {len(related)} related tests can be run here."
            )
            return Done("none", "no_tests", _words(why + said_left, 600), output=found)
        files = len(related) - len(left)
        failed = [entry for entry in ran if entry.get("exit_code") != 0]
        if not failed:
            return Done(
                "passed",
                "passed",
                _words(f"{files} related test files passed." + said_left, 600),
                output=found,
            )
        return Done(
            "failed",
            "failed",
            _words(
                f"{len(failed)} of {len(ran)} groups of related tests failed: "
                + "; ".join(str(entry.get("line")) for entry in failed)
                + said_left,
                600,
            ),
            output=found,
            given={
                "failures": [
                    f"{entry.get('line')}"
                    + (" (stopped: it took too long)" if entry.get("timed_out") else "")
                    + f"\n{str(entry.get('said') or '')[-3000:]}"
                    for entry in failed
                ]
            },
        )


class BugTest:
    """Run the tests the run wrote for its bug. Before the fix they have to fail: that is
    what shows the bug. After it they have to pass. Which files they are is found the
    first time, from what the run changed, and kept for the times after."""

    @staticmethod
    def _kept(ctx: Context) -> list[str]:
        return [str(path) for path in ((ctx.data.get("bug") or {}).get("tests") or [])]

    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        kept = self._kept(ctx)
        minutes = max(1, min(int(ctx.params.get("minutes") or RELATED_MINUTES), 30))
        project = ctx.toolchain().tests
        whole = sum(command.timeout for command in project)
        question: dict[str, Any] = {
            "paths": kept,
            "base": str(ctx.data.get("base_sha") or ""),
            # The first time: the tests the run wrote, and none that only cover them.
            "only_changed": not kept,
            "limit": 20,
            "seconds": minutes * 60,
            # Where none of them can be run alone, the project's own test commands.
            "otherwise": [
                {
                    "argv": list(command.argv),
                    "cwd": command.cwd,
                    "timeout": command.timeout,
                    "line": deliver.line_of(command),
                }
                for command in project
            ],
            "otherwise_seconds": whole,
        }
        named = ctx.params.get("commands")
        if isinstance(named, list) and named:
            question["commands"] = [
                {"argv": list(command.argv), "cwd": command.cwd, "timeout": command.timeout}
                for command in deliver.commands_of(named)
            ]
        seconds = minutes * 60 + whole
        job = tool_job(
            "tests", question, max_bytes=600_000, seconds=seconds + 60, env=deliver.TOOLS
        )
        return _bench(job, seconds=seconds + 360)

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        found = _answer(commands, log, "tests")
        if isinstance(found, str):
            return Failed("tests_failed", f"The bug's tests could not be run: {found}")
        fixing = ctx.params.get("expect") == "pass"
        ran = [entry for entry in found.get("ran") or [] if isinstance(entry, dict)]
        whole = bool(found.get("whole"))
        files = self._kept(ctx) or [
            str(entry.get("path"))
            for entry in found.get("tests") or []
            if isinstance(entry, dict) and entry.get("path")
        ]
        known = dict(ctx.data.get("bug") or {})
        if not ran:
            why = (
                "The run wrote no test for the bug."
                if not files
                else f"None of the {len(files)} test files written for the bug can be run "
                "here, and the project names no test commands."
            )
            return Done(
                "none",
                "no_test",
                why,
                output=found,
                data={"bug": {**known, "tests": files, **({} if fixing else {"shown": False})}},
            )
        failed = [entry for entry in ran if entry.get("exit_code") != 0]
        if whole:
            # The project's own commands: one that failed before anything was changed
            # says nothing of the bug.
            before = ctx.data.get("baseline_by_command")
            if isinstance(before, dict):
                failed = [entry for entry in failed if before.get(str(entry.get("line"))) == 0]
        how = (
            "the project's test commands, compared with before anything was changed"
            if whole
            else f"{len(files)} test file{'s' if len(files) != 1 else ''} of its own"
        )
        lines = "; ".join(str(entry.get("line")) for entry in failed)
        failures = [
            f"{entry.get('line')}"
            + (" (stopped: it took too long)" if entry.get("timed_out") else "")
            + f"\n{str(entry.get('said') or '')[-3000:]}"
            for entry in failed
        ]
        bug = {**known, "tests": files, "by": "commands" if whole else "tests"}
        if fixing:
            if failed:
                return Done(
                    "fails",
                    "still_failing",
                    _words(f"The bug's test still fails after the fix ({how}): {lines}", 600),
                    output=found,
                    data={"bug": {**bug, "fixed": False}},
                    given={"failures": failures},
                )
            return Done(
                "passes",
                "fixed",
                f"The bug's test passes after the fix ({how}).",
                output=found,
                data={"bug": {**bug, "fixed": True}},
            )
        if failed:
            return Done(
                "fails",
                "reproduced",
                _words(f"The bug is shown: its test fails before the fix ({how}): {lines}", 600),
                output=found,
                data={"bug": {**bug, "shown": True}},
                # What the test says is what the fix has to make go away.
                given={"failures": failures},
            )
        return Done(
            "passes",
            "not_reproduced",
            "The bug is not shown: what was written for it passes before anything is "
            f"fixed ({how}).",
            output=found,
            data={"bug": {**bug, "shown": False}},
        )


class Apply:
    """Apply the edits a model wrote, and commit. What is changed this way is a change
    like an agent's, and is held to the same rules before it is published."""

    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        written = await ctx.outputs("model.edit")
        edits = written[-1].get("edits") if written else None
        if not edits:
            raise ValueError(f"{ctx.node.id} has no edits to apply: no task before it wrote any")
        issue = ctx.data.get("issue") or {}
        message = (
            deliver.commit_message(issue)
            if issue.get("title")
            else f"Change made by the task {ctx.node.id}"
        )
        job = tool_job(
            "apply", {"edits": edits, "protected": list(deliver.PROTECTED)}, max_bytes=1_000_000
        )
        job["commands"] += deliver.commit_commands(message, ctx.data.get("base_sha"))
        return _bench(job)

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        tool = next((c for c in commands if c.get("name") == "apply"), commands[0])
        answer = _answer([tool], log, "apply")
        if isinstance(answer, str):
            return Failed("apply_failed", f"The edits could not be applied: {answer}")
        refused = [entry for entry in answer.get("refused") or [] if isinstance(entry, dict)]
        if refused:
            reasons = [
                f"{entry.get('path')}"
                + (
                    f" (edit {entry.get('edit')}, {entry.get('action')})"
                    if entry.get("edit")
                    else ""
                )
                + f": {entry.get('why')}"
                for entry in refused
            ]
            return Done(
                "refused",
                "refused",
                _words(
                    f"{len(refused)} of {answer.get('edits')} edits do not fit, so nothing "
                    "was changed: " + "; ".join(reasons),
                    600,
                ),
                output=answer,
                given={"refused": reasons},
            )
        commit = next((c for c in commands if c.get("name") == "commit"), None)
        head = _last_line(commit) if commit and commit.get("exit_code") == 0 else ""
        if len(head) != 40:
            return Failed(
                "commit_failed", f"The change could not be committed: {_tail(commit or tool)}"
            )
        applied = [entry for entry in answer.get("applied") or [] if isinstance(entry, dict)]
        if not applied or head == ctx.data.get("base_sha"):
            return Failed("no_change", "The edits were applied and changed nothing.", answer)
        written = await ctx.outputs("model.edit")
        last = written[-1] if written else {}
        provider = str(last.get("provider") or "")
        touched = deliver.touched_files(commands)
        paths = [str(entry.get("path")) for entry in applied]
        return Done(
            "applied",
            "applied",
            _words(f"{len(applied)} files changed: " + ", ".join(paths), 500),
            output=answer,
            data={
                "maker": provider,
                "head": head,
                "round": int(ctx.given.get("round") or 1),
                # Whose model wrote the change: the review is by another family's. A
                # model asked through OpenRouter is of its maker's family, not
                # OpenRouter's, so that what Claude wrote there is not reviewed by Claude.
                "ran": {
                    **(ctx.data.get("ran") or {}),
                    ctx.node.id: family_of(provider, str(last.get("model") or ""))
                    if provider == "openrouter"
                    else provider,
                },
                **({"touched": touched} if touched is not None else {}),
            },
            given={"summary": str(last.get("summary") or "")},
        )


BENCH: Final[dict[str, SandboxTask]] = {
    "code.find": Find(),
    "code.read": Read(),
    "code.run": Run(),
    "code.tests": RelatedTests(),
    "check.reproduction": BugTest(),
    "code.digest": Digest(),
    "code.apply": Apply(),
}
assert set(BENCH) == {  # noqa: S101 - the catalogue and the code agree
    key for key, task in TASKS.items() if task.where == "bench"
}
