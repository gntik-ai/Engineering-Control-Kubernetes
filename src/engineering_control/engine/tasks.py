"""What each task does: the job its sandbox runs, and what its result means.

A sandbox task has two halves. ``plan`` says what the pod is to do. ``conclude`` reads
what came back and says which of the task's outcomes it is; where that outcome leads is
the flow's to say, not the task's. A platform task has one: ``perform``, which the
platform carries out itself, with its own credentials, once no sandbox is left to run.

Nothing here knows which flow it is part of. The checks that hold whatever the flow (the
other subscription reviews, rounds are bounded, nothing is published without the gate and
the administrator) are the engine's (``interpreter``, ``validate``).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import dataclasses
import functools
import time
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from engineering_control.agent_models import flags
from engineering_control.dashboard.backlog_items import BacklogItems
from engineering_control.dashboard.project_records import ProjectNotFound, application_host
from engineering_control.engine.catalogue import (
    AUDITING,
    CHECKS_MINUTES,
    CHECKS_MINUTES_CEILING,
    HISTORY_COMMITS,
    ITEM_KINDS,
    TASKS,
    VERIFYING,
    TaskType,
)
from engineering_control.engine.document import Document, Node
from engineering_control.engine.prompts import CONTRACTS
from engineering_control.github_app import ChangedFile, GitHubApp, GitHubAppError
from engineering_control.sandbox import audit, deliver, discover, steps
from engineering_control.sandbox.flows import Checkouts, End, Plan, why_no_answer
from engineering_control.sandbox.runs import Lease, Runs, StepWork

# What the branch of a run that publishes nothing is called after.
_BRANCH_WORDS: Final = {"audit-code": "audit", "discover-enhancements": "discovery"}


@dataclass
class Services:
    """What tasks work with: the platform's records and its way to GitHub."""

    runs: Runs
    github: GitHubApp | None
    backlog: BacklogItems | None
    checkouts: Checkouts


@dataclass
class Context:
    """A step as its task sees it."""

    item: StepWork
    document: Document
    node: Node
    task: TaskType
    services: Services
    # Set for a run of the evaluation lab: nothing leaves the run.
    dry: bool = False
    # Which prompt the step was given, with the hash of its wording.
    prompt_used: dict[str, str] | None = None
    # Which model the step's agent was started with, when the flow chose one.
    model_used: dict[str, str] | None = None
    # The agent the task names, as the administrator defined it: what it is told and how
    # it works. None when the task names none.
    agent: dict[str, Any] | None = None

    @property
    def data(self) -> dict[str, Any]:
        return self.item.run_data

    @property
    def given(self) -> dict[str, Any]:
        return self.item.input

    @property
    def params(self) -> dict[str, Any]:
        return {**self.task.defaults(), **self.node.params}

    def model(self, provider: str) -> dict[str, str]:
        """The model and the reasoning level the flow chose for this task on that
        subscription: only what was chosen."""
        chosen = (self.node.params.get("models") or {}).get(provider) or {}
        if not chosen and self.agent is not None:
            # What the task leaves open comes from its agent.
            chosen = ((self.agent["settings"].get("models")) or {}).get(provider) or {}
        return {
            key: str(chosen[key]).strip()
            for key in ("model", "effort")
            if isinstance(chosen, dict) and str(chosen.get(key) or "").strip()
        }

    def each(self, over: str) -> int | None:
        """Where the run is in the list it goes through, when the step is inside one."""
        state = self.given.get("each") or {}
        for node in self.document.nodes:
            if node.task == "control.each" and (node.params.get("over") or "areas") == over:
                index = state.get(node.id)
                return int(index) if isinstance(index, int) else None
        return None

    def prompt(self, values: dict[str, Any]) -> str:
        contract = CONTRACTS[str(self.task.prompt)]
        overrides = self.document.prompts.get(contract.name) or {}
        self.prompt_used = {"name": contract.name, "sha256": contract.sha256(overrides)}
        return self.as_agent(contract.render(overrides, values))

    def as_agent(self, prompt: str) -> str:
        """The prompt with what the task's agent is told put before it, when the task
        names an agent. Which agent, and a hash of what it was told, is kept with the
        step."""
        if self.agent is None:
            return prompt
        self.prompt_used = {
            **(self.prompt_used or {}),
            "agent": str(self.agent["name"]),
            "agent_sha256": str(self.agent["sha256"]),
        }
        return f"{self.agent['told']}\n\n{prompt}"

    async def outputs(self, task: str | tuple[str, ...]) -> list[dict[str, Any]]:
        """What the run's steps of that kind of task produced, in the order they ran."""
        kinds = (task,) if isinstance(task, str) else task
        found: list[dict[str, Any]] = []
        for node in self.document.nodes:
            if node.task in kinds:
                found += await asyncio.to_thread(
                    self.services.runs.step_outputs, self.item.run_id, node.id
                )
        return found

    def setting(self, task: str | tuple[str, ...], name: str, default: int) -> int:
        """A numeric setting of the flow's task of that kind."""
        kinds = (task,) if isinstance(task, str) else task
        for node in self.document.nodes:
            if node.task in kinds:
                try:
                    return int(node.params.get(name, default))
                except (TypeError, ValueError):
                    return default
        return default

    def toolchain(self) -> deliver.Toolchain:
        return deliver.Toolchain.from_dict(self.data.get("toolchain"))

    def minutes(self) -> int:
        wanted = self.node.params.get("minutes")
        if wanted is None and self.agent is not None:
            wanted = self.agent["settings"].get("minutes")
        try:
            return max(5, min(int(wanted or self.params.get("minutes") or 50), 55))
        except (TypeError, ValueError):
            return 50


@dataclass(frozen=True)
class Done:
    """The task ended with one of its outcomes."""

    outcome: str  # what the flow routes on
    label: str  # what the step is recorded as
    detail: str | None = None
    output: dict[str, Any] | None = None
    data: dict[str, Any] = field(default_factory=dict)
    given: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Failed:
    """The task failed, and the run with it."""

    label: str
    detail: str
    output: dict[str, Any] | None = None


@dataclass(frozen=True)
class Again:
    """The task is run once more, with this input."""

    label: str
    detail: str
    given: dict[str, Any]
    output: dict[str, Any] | None = None


Concluded = Done | Failed | Again


@dataclass(frozen=True)
class Performed:
    """What a task of the platform's own came to: an outcome to go on with, or the end
    of the run."""

    outcome: str | None = None
    end: End | None = None
    data: dict[str, Any] = field(default_factory=dict)


class SandboxTask(Protocol):
    async def plan(self, ctx: Context, lease: Lease | None) -> Plan: ...

    async def conclude(
        self, ctx: Context, commands: list[dict[str, Any]], log: str
    ) -> Concluded: ...


class PlatformTask(Protocol):
    async def perform(self, ctx: Context) -> Performed: ...


def _tail(command: dict[str, Any], limit: int = 400) -> str:
    said = f"{command.get('stdout') or ''}\n{command.get('stderr') or ''}"
    return " ".join(said.split())[-limit:]


def _last_line(command: dict[str, Any]) -> str:
    lines = str(command.get("stdout") or "").strip().splitlines()
    return lines[-1].strip() if lines else ""


def _words(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


def _agent(ctx: Context, lease: Lease | None, job: dict[str, Any]) -> Plan:
    """A sandbox for an agent: the account's client, the checkout, and time to work."""
    seconds = ctx.minutes() * 60
    chosen = ctx.model(lease.provider) if lease is not None else {}
    said = flags(lease.provider, chosen.get("model"), chosen.get("effort")) if lease else []
    for command in job["commands"]:
        if command.get("name") == "agent":
            command["timeout"] = seconds
            argv = command["argv"]
            # Codex reads its prompt from standard input, which its last word says.
            at = len(argv) - 1 if argv and argv[-1] == "-" else len(argv)
            argv[at:at] = said
    if lease is not None and (chosen.get("model") or chosen.get("effort")):
        # Which model the agent was started with: traceable like the prompt's wording.
        ctx.model_used = {"provider": lease.provider, **chosen}
    return Plan(job, workspace=True, memory="6Gi", deadline_seconds=seconds + 600)


def _reader(ctx: Context, lease: Lease | None, prompt: str, schema: dict[str, Any]) -> Plan:
    """An agent that reads and judges: whatever it changes is thrown away."""
    if lease is None:
        raise ValueError("the step needs an account")
    return _agent(ctx, lease, deliver.review_job(lease.provider, lease.generation, prompt, schema))


def _answer(
    ctx: Context, commands: list[dict[str, Any]], *, who: str
) -> tuple[dict[str, Any] | None, Concluded | None]:
    """The agent's answer, or what to do without one: ask once more, or give up."""
    provider = ctx.item.provider or ""
    answer = deliver.agent_answer(provider, commands)
    if answer is not None:
        return answer, None
    why = why_no_answer(provider, commands)
    asked = int(ctx.given.get("asked_again") or 0)
    if asked >= 1:
        return None, Failed("no_answer", f"{who} gave no answer twice: {why}")
    return None, Again(
        "no_answer",
        f"{who} gave no answer ({why}). It is asked again.",
        {**ctx.given, "asked_again": asked + 1},
    )


async def _known(ctx: Context) -> list[str]:
    """What the project's backlog already holds, to recognise what is known."""
    if ctx.services.backlog is None:
        return []
    try:
        return await asyncio.to_thread(ctx.services.backlog.open_titles, ctx.data["project"])
    except ProjectNotFound:
        return []


# --- the workspace ----------------------------------------------------------------------


class Checkout:
    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        data = ctx.data
        github = ctx.services.github
        if github is None:
            raise GitHubAppError("The GitHub App is not configured, so nothing can be checked out")
        issue = data.get("issue") or {}
        if issue.get("number") and not ctx.dry:
            # The issue as it reads now, for the agents. An item of the platform's own
            # carries its text already, and so does past work that is gone through again.
            fresh = await github.issue(data["repository"], int(issue["number"]))
            await asyncio.to_thread(
                ctx.services.runs.merge_data,
                ctx.item.run_id,
                {
                    "issue": {
                        "number": fresh.number,
                        "title": fresh.title or issue["title"],
                        "url": fresh.url or issue.get("url"),
                        "body": fresh.body,
                    }
                },
            )
        toolchain = ctx.toolchain()
        wanted = deliver.Toolchain(
            prepare=toolchain.prepare if ctx.params.get("install", True) else (),
            tests=toolchain.tests if ctx.params.get("baseline") else (),
        )
        word = _BRANCH_WORDS.get(ctx.document.key, ctx.document.key)
        branch = data.get("branch") or f"engineering-control/{word}-{ctx.item.run_id.hex[:8]}"
        return await ctx.services.checkouts.plan(
            ctx.item, branch=branch, toolchain=wanted, commit=data.get("at_commit")
        )

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        base_sha, failure = Checkouts.read(commands)
        if failure is not None or base_sha is None:
            verdict = failure.verdict if failure is not None else None
            return Failed(
                verdict.outcome if verdict else "checkout_failed",
                (verdict.detail if verdict else None) or "The checkout gave no commit.",
            )
        output: dict[str, Any] = {"base_sha": base_sha}
        if ctx.params.get("baseline"):
            by_name = {str(command.get("name")): command for command in commands}
            output["baseline"] = {
                name.removeprefix("baseline "): command.get("exit_code")
                for name, command in by_name.items()
                if name.startswith("baseline ")
            }
        return Done(
            "ok", "prepared", "Checked out and installed.", output=output, data=dict(output)
        )


async def _item_as_it_reads_now(ctx: Context) -> None:
    """The issue as it reads on GitHub now, for the agents. An item of the platform's own
    carries its text already, and so does past work that is gone through again."""
    data = ctx.data
    issue = data.get("issue") or {}
    github = ctx.services.github
    if not issue.get("number") or ctx.dry or github is None:
        return
    fresh = await github.issue(data["repository"], int(issue["number"]))
    await asyncio.to_thread(
        ctx.services.runs.merge_data,
        ctx.item.run_id,
        {
            "issue": {
                "number": fresh.number,
                "title": fresh.title or issue["title"],
                "url": fresh.url or issue.get("url"),
                "body": fresh.body,
            }
        },
    )


def _own_commands(ctx: Context, part: str) -> tuple[tuple[deliver.Command, ...], bool]:
    """The commands a task runs: the ones it names, or the project's. With whether they
    are the task's own."""
    named = ctx.params.get("commands")
    if isinstance(named, list) and named:
        return deliver.commands_of(named), True
    toolchain = ctx.toolchain()
    return (toolchain.prepare if part == "prepare" else toolchain.tests), False


def _failed_command(commands: list[dict[str, Any]], prefix: str) -> dict[str, Any] | None:
    return next(
        (
            command
            for command in commands
            if str(command.get("name")).startswith(prefix) and command.get("exit_code") != 0
        ),
        None,
    )


class Clone:
    """Clone the repository at a branch and start the run's own branch there."""

    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        data = ctx.data
        if ctx.services.github is None:
            raise GitHubAppError("The GitHub App is not configured, so nothing can be checked out")
        await _item_as_it_reads_now(ctx)
        word = _BRANCH_WORDS.get(ctx.document.key, ctx.document.key)
        branch = data.get("branch") or f"engineering-control/{word}-{ctx.item.run_id.hex[:8]}"
        return await ctx.services.checkouts.plan(
            ctx.item,
            branch=branch,
            toolchain=deliver.Toolchain(),
            commit=data.get("at_commit"),
            base_branch=self._from(ctx),
            history=self._history(ctx),
        )

    @staticmethod
    def _history(ctx: Context) -> int:
        """How many of the branch's last commits are read for what changes together."""
        try:
            return max(0, min(int(ctx.params.get("history", HISTORY_COMMITS)), 1000))
        except (TypeError, ValueError):
            return HISTORY_COMMITS

    @staticmethod
    def _from(ctx: Context) -> str:
        """The branch that is cloned: the one the task names, or the project's own."""
        return str(ctx.params.get("branch") or "").strip() or str(ctx.data["base_branch"])

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        base_sha, failure = Checkouts.read(commands)
        if failure is not None or base_sha is None:
            verdict = failure.verdict if failure is not None else None
            return Failed(
                verdict.outcome if verdict else "checkout_failed",
                (verdict.detail if verdict else None) or "The checkout gave no commit.",
            )
        base = self._from(ctx)
        said = f"Cloned {ctx.data['repository']} at {base} ({base_sha[:12]})."
        history = next((c for c in commands if c.get("name") == "history"), None)
        read: int | None = None
        if history is not None:
            if history.get("exit_code") == 0:
                counted = str(history.get("stdout") or "").strip().splitlines()[-1:]
                read = int(counted[0]) if counted and counted[0].isdigit() else 0
                said += f" What its last {read:,} commits changed was read, by name."
            else:
                said += (
                    " Its history could not be read, so this run does not know which "
                    "files change together."
                )
        return Done(
            "ok",
            "cloned",
            said,
            output={"base_sha": base_sha, "branch": base, "history": read},
            # What a pull request of the run is opened against is what was cloned.
            data={"base_sha": base_sha, "base_branch": base},
        )


class Install:
    """Run the commands that install what the project needs."""

    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        commands, _own = _own_commands(ctx, "prepare")
        return Plan(deliver.install_job(commands), workspace=True, deadline_seconds=3600)

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        ran, own = _own_commands(ctx, "prepare")
        failed = _failed_command(commands, "install ")
        if failed is not None:
            return Failed("install_failed", f"Installing the project failed: {_tail(failed)}")
        lines = [deliver.line_of(command) for command in ran]
        if not ran:
            note = ctx.toolchain().note
            return Done("ok", "nothing_to_install", note or "The project names nothing to install.")
        data: dict[str, Any] = {}
        if own:
            # What was installed with is what the run's agents are told of.
            data["toolchain"] = {
                **(ctx.data.get("toolchain") or {}),
                "prepare": [command.as_dict() for command in ran],
            }
        return Done(
            "ok",
            "installed",
            f"Installed with {len(ran)} command{'s' if len(ran) != 1 else ''}"
            + (" of the flow's own." if own else " of the project's."),
            output={"commands": lines, "own": own},
            data=data,
        )


def _written(data: dict[str, Any]) -> bool:
    """Whether an agent of the run has changed the code since the run started."""
    head = data.get("head")
    return bool(head) and head != data.get("base_sha")


class RunTests:
    """Run the project's tests, without network. Before anything is changed: what
    already fails. After a change: the same commands under the same conditions, compared
    with before, which is what a change has to pass to be published."""

    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        commands, _own = _own_commands(ctx, "tests")
        return Plan(
            deliver.tests_job(commands, after=_written(ctx.data)),
            egress="none",
            workspace=True,
            memory="6Gi",
            deadline_seconds=3600,
        )

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        ran, own = _own_commands(ctx, "tests")
        lines = [deliver.line_of(command) for command in ran]
        results: dict[str, Any] = {}
        said: dict[str, dict[str, Any]] = {}
        for command in commands:
            name = str(command.get("name"))
            for word in ("baseline ", "test "):
                if name.startswith(word):
                    results[name.removeprefix(word)] = command.get("exit_code")
                    said[name.removeprefix(word)] = command

        def line(index: str) -> str:
            return lines[int(index) - 1] if index.isdigit() and int(index) <= len(lines) else index

        failing = [index for index, code in results.items() if code != 0]
        data: dict[str, Any] = {}
        if own:
            # Whatever runs the tests later in the run runs what was run here, and the
            # agents are told of the same commands.
            data["toolchain"] = {
                **(ctx.data.get("toolchain") or {}),
                "tests": [command.as_dict() for command in ran],
            }
        output: dict[str, Any] = {
            "commands": lines,
            "own": own,
            "baseline": results,
            "failing": [line(index) for index in failing],
        }
        if not _written(ctx.data):
            # Before any change: what is found now is what a change is compared with.
            data["baseline"] = results
            data["baseline_by_command"] = {line(index): code for index, code in results.items()}
            if not ran:
                return Done(
                    "passed",
                    "no_tests",
                    "The project names no test commands, so nothing was run.",
                    output=output,
                    data=data,
                )
            if failing:
                return Done(
                    "failed",
                    "tests_failing",
                    f"{len(failing)} of {len(ran)} test commands fail before anything is "
                    "changed: " + "; ".join(output["failing"])[:300],
                    output=output,
                    data=data,
                )
            return Done(
                "passed",
                "tests_passing",
                f"All {len(ran)} test command{'s' if len(ran) != 1 else ''} pass before "
                "anything is changed.",
                output=output,
                data=data,
            )

        # After a change: compared with before, command by command.
        before = ctx.data.get("baseline_by_command")
        if not isinstance(before, dict):
            # The task that checked out and ran the tests in one step kept them by number.
            numbered = ctx.data.get("baseline")
            before = (
                {line(str(index)): code for index, code in numbered.items()}
                if isinstance(numbered, dict)
                else None
            )
        regressions = [index for index in failing if before and before.get(line(index)) == 0]
        already = [line(index) for index in failing if index not in regressions]
        output = {
            **output,
            "regressions": [line(index) for index in regressions],
            "already_failing": already,
            "compared": before is not None,
        }
        if regressions:
            return Done(
                "regression",
                "tests_failed",
                f"{len(regressions)} test command(s) that passed before the change fail "
                "after it: " + "; ".join(output["regressions"])[:300],
                output=output,
                data=data,
                given={
                    "failures": [
                        f"{line(index)}\n{_tail(said[index], 3000)}" for index in regressions
                    ]
                },
            )
        if not ran:
            return Done(
                "passed",
                "no_tests",
                "The project names no test commands, so nothing was run.",
                output=output,
                data=data,
            )
        if already:
            why = (
                "as before the change"
                if before is not None
                else "and they were not run before the change, so there is nothing to compare with"
            )
            return Done(
                "failed",
                "tests_no_worse",
                f"{len(already)} of {len(ran)} test commands fail after the change, {why}: "
                + "; ".join(already)[:300],
                output=output,
                data=data,
            )
        return Done(
            "passed",
            "tests_passed",
            f"All {len(ran)} test command{'s' if len(ran) != 1 else ''} pass after the change.",
            output=output,
            data=data,
        )


class ScanSecrets:
    """Scan what the change added for anything that looks like a credential. One found
    ends the run: there is no way on from it."""

    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        return Plan(
            deliver.secrets_job(ctx.data["base_sha"]),
            egress="none",
            workspace=True,
            deadline_seconds=900,
        )

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        secrets = next((c for c in commands if c.get("name") == "secrets"), None)
        if secrets is None or secrets.get("exit_code") == 23:
            return Failed(
                "secret_found", "The change holds something that looks like a credential."
            )
        if secrets.get("exit_code") != 0:
            return Failed("scan_failed", f"The scan for secrets did not run: {_tail(secrets)}")
        return Done("ok", "scanned", "No secret was found in the change.")


# --- delivering a change ----------------------------------------------------------------


class Implement:
    # How the agent is called in what is said of its work.
    who = "The implementer"
    what = "resolve the issue"

    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        if lease is None:
            raise ValueError("implementing needs an account")
        data = ctx.data
        prompt = ctx.prompt(
            {
                "subject": deliver.subject(data["issue"], data["repository"]),
                "repository": data["repository"],
                "issue": data["issue"],
                "toolchain": ctx.toolchain(),
                "findings": ctx.given.get("findings"),
                "failures": ctx.given.get("failures"),
                "notes": data.get("notes"),
            }
        )
        job = deliver.implement_job(
            lease.provider,
            lease.generation,
            prompt,
            deliver.commit_message(data["issue"]),
            base_sha=data.get("base_sha"),
        )
        return _agent(ctx, lease, job)

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        provider = ctx.item.provider or ""
        answer = deliver.agent_answer(provider, commands)
        agent = next((c for c in commands if c.get("name") == "agent"), commands[0])
        commit = next((c for c in commands if c.get("name") == "commit"), None)
        head = _last_line(commit) if commit and commit.get("exit_code") == 0 else ""
        changed = len(head) == 40 and head != ctx.data.get("base_sha")
        if answer is None:
            # The agent ended without its closing answer: it ran out of time, or the
            # provider cut its last message off. Work it did is not thrown away for that:
            # it is committed, and the reviewer and the tests judge it as it is.
            why = why_no_answer(provider, commands)
            if not changed:
                return Failed("no_answer", f"{self.who} changed nothing and gave no answer: {why}")
            answer = {
                "decision": "COMPLETE",
                "summary": f"{self.who} ended without its closing answer ({why}). What "
                "it changed is committed and is reviewed as it stands.",
                "tests": [],
                "blocked_reason": "",
            }
        if answer.get("decision") != "COMPLETE":
            reason = str(answer.get("blocked_reason") or answer.get("summary") or "no reason given")
            return Failed("blocked", f"{self.who} could not {self.what}: {reason}", answer)
        if len(head) != 40:
            return Failed(
                "commit_failed", f"The change could not be committed: {_tail(commit or agent)}"
            )
        if not changed:
            return Failed("no_change", f"{self.who} says it is done but changed nothing.", answer)
        summary = str(answer.get("summary") or "")
        touched = deliver.touched_files(commands)
        return Done(
            "done",
            "implemented",
            summary[:500],
            output=answer,
            data={
                "maker": provider,
                "head": head,
                "round": int(ctx.given.get("round") or 1),
                # Which files differ from where the run started, for an If to ask about.
                **({"touched": touched} if touched is not None else {}),
            },
            given={"summary": summary},
        )


class Review:
    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        data = ctx.data
        prompt = ctx.prompt(
            {
                "subject": deliver.subject(data["issue"], data["repository"]),
                "repository": data["repository"],
                "base_sha": data["base_sha"],
                "issue": data["issue"],
                "toolchain": ctx.toolchain(),
                "summary": str(ctx.given.get("summary") or ""),
                "notes": data.get("notes"),
            }
        )
        return _reader(ctx, lease, prompt, deliver.CHECKER_SCHEMA)

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        answer, instead = _answer(ctx, commands, who="The reviewer")
        if answer is None:
            return instead or Failed("no_answer", "The reviewer gave no answer.")
        decision = answer.get("decision")
        summary = str(answer.get("summary") or "")[:500]
        if decision == "PASS":
            return Done("pass", "passed", summary, output=answer)
        if decision == "REQUEST_CHANGES":
            findings = [f for f in answer.get("findings") or [] if isinstance(f, dict)]
            return Done(
                "changes",
                "changes_requested",
                f"The reviewer asks for changes: {summary}",
                output=answer,
                given={"findings": findings},
            )
        return Failed("rejected", f"The reviewer stopped the change: {summary}", answer)


# --- the tasks whose instruction the administrator writes ---------------------------------


def _about(data: dict[str, Any]) -> dict[str, Any]:
    """What a run is about, as the tasks with an instruction of their own are told: the
    item it works on, or what it was told to look at."""
    issue = data.get("issue") or {}
    if issue.get("title"):
        return dict(issue)
    scope = data.get("scope") or {}
    return {
        "title": str(scope.get("title") or "").strip() or "(none: the run is about the repository)",
        "body": str(scope.get("text") or ""),
    }


def _instructed(ctx: Context) -> dict[str, Any]:
    """What the prompt of a task with an instruction of its own is put together from."""
    data = ctx.data
    about = _about(data)
    repository = str(data.get("repository") or "")
    if (data.get("issue") or {}).get("title"):
        subject = deliver.subject(about, repository)
    else:
        subject = f"the repository {repository}"
    return {
        "instruction": str(ctx.params.get("instruction") or "").strip(),
        "subject": subject,
        "repository": repository,
        "base_sha": data.get("base_sha"),
        # Whether an agent of this run has changed the code so far.
        "changed": bool(data.get("head")) and data.get("head") != data.get("base_sha"),
        "issue": about,
        "toolchain": ctx.toolchain(),
        "notes": data.get("notes"),
        "summary": str(ctx.given.get("summary") or ""),
        "findings": ctx.given.get("findings"),
        "failures": ctx.given.get("failures"),
    }


class Change(Implement):
    """An agent that changes the code as an instruction of the flow says. What holds
    for the implementer holds for it: the platform commits, and nothing it wrote is
    published without the review, the tests, the collection and the decision."""

    who = "The agent"
    what = "do its task"

    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        if lease is None:
            raise ValueError("changing the code needs an account")
        values = _instructed(ctx)
        about = values["issue"]
        message = (
            deliver.commit_message(about)
            if (ctx.data.get("issue") or {}).get("title")
            else f"Change made by the task {ctx.node.id}"
        )
        job = deliver.implement_job(
            lease.provider,
            lease.generation,
            ctx.prompt(values),
            message,
            base_sha=ctx.data.get("base_sha"),
        )
        return _agent(ctx, lease, job)


class Check:
    """An agent that checks what an instruction of the flow says, and passes it or asks
    for changes. It is not the review a change needs before it is published."""

    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        return _reader(ctx, lease, ctx.prompt(_instructed(ctx)), deliver.CHECKER_SCHEMA)

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        answer, instead = _answer(ctx, commands, who="The check")
        if answer is None:
            return instead or Failed("no_answer", "The check gave no answer.")
        decision = answer.get("decision")
        summary = str(answer.get("summary") or "")[:500]
        if decision == "PASS":
            return Done("pass", "passed", summary, output=answer)
        if decision == "REQUEST_CHANGES":
            findings = [f for f in answer.get("findings") or [] if isinstance(f, dict)]
            return Done(
                "changes",
                "changes_requested",
                f"The check asks for changes: {summary}",
                output=answer,
                given={"findings": findings},
            )
        return Failed("rejected", f"The check stopped the work: {summary}", answer)


class Report:
    """An agent that looks into what an instruction of the flow says and reports. The
    report stays with the run, for the administrator and for the agents after it."""

    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        return _reader(ctx, lease, ctx.prompt(_instructed(ctx)), deliver.REPORT_SCHEMA)

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        answer, instead = _answer(ctx, commands, who="The agent")
        if answer is None:
            return instead or Failed("no_answer", "The agent gave no report.")
        report = str(answer.get("report") or "").strip()
        summary = _words(answer.get("summary") or report, 500)
        if not report:
            return Failed("no_report", "The agent answered without a report.", answer)
        # A task that is gone back to replaces what it reported before.
        earlier = [
            note
            for note in ctx.data.get("notes") or []
            if isinstance(note, dict) and note.get("task") != ctx.node.id
        ]
        notes = [*earlier, {"task": ctx.node.id, "text": report[: deliver.NOTE_CHARACTERS]}]
        return Done(
            "done",
            "reported",
            summary,
            output={"summary": summary, "report": report[: 4 * deliver.NOTE_CHARACTERS]},
            data={"notes": notes[-deliver.NOTES_KEPT :]},
        )


class CheckChange:
    """The validation every change passes before it can be published."""

    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        return Plan(
            deliver.verify_job(ctx.data["base_sha"], ctx.toolchain()),
            egress="none",
            workspace=True,
            memory="6Gi",
            deadline_seconds=3600,
        )

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        baseline = ctx.data.get("baseline") or {}
        toolchain = ctx.toolchain()
        regressions: list[str] = []
        already: list[str] = []
        for command in commands:
            name = str(command.get("name"))
            if not name.startswith("test ") or command.get("exit_code") == 0:
                continue
            index = name.removeprefix("test ")
            text = toolchain.tests[int(index) - 1].text if index.isdigit() else name
            if baseline.get(index) == 0:
                regressions.append(f"{text}\n{_tail(command, 3000)}")
            else:
                already.append(text)
        output = {
            "regressions": [entry.split("\n", 1)[0] for entry in regressions],
            "already_failing": already,
        }
        secrets = next((c for c in commands if c.get("name") == "secrets"), None)
        if secrets is None or secrets.get("exit_code") == 23:
            return Failed(
                "secret_found", "The change holds something that looks like a credential.", output
            )
        if secrets.get("exit_code") != 0:
            return Failed(
                "scan_failed", f"The scan for secrets did not run: {_tail(secrets)}", output
            )
        if regressions:
            return Done(
                "regression",
                "tests_failed",
                f"{len(regressions)} test command(s) that passed before the change fail after it.",
                output=output,
                given={"failures": regressions},
            )
        note = f" {len(already)} command(s) failed before the change as well." if already else ""
        return Done(
            "ok", "verified", f"The tests pass and no secret was found.{note}", output=output
        )


class CollectChange:
    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        return Plan(
            deliver.bundle_job(ctx.data["base_sha"]),
            egress="none",
            workspace=True,
            cpu="1",
            memory="2Gi",
            deadline_seconds=600,
        )

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        bundle = steps.parse_artifact(log, "bundle")
        if bundle is None or not isinstance(bundle.get("files"), list):
            return Failed(
                "bundle_failed", f"The change could not be collected: {_tail(commands[-1])}"
            )
        paths = [str(file.get("path")) for file in bundle["files"]]
        refused = deliver.protected_paths(paths)
        if refused:
            return Failed(
                "protected_files",
                "The change touches files an agent may not change: " + ", ".join(refused[:10]),
                {"files": [{"path": p, "status": "?"} for p in paths]},
            )
        change = {
            "summary": str(bundle.get("summary") or ""),
            "files": [{"path": f.get("path"), "status": f.get("status")} for f in bundle["files"]],
            "diff": str(bundle.get("diff") or ""),
            "diff_complete": bool(bundle.get("diff_complete")),
            "head": bundle.get("head"),
        }
        return Done(
            "ok",
            "bundled",
            str(bundle.get("summary") or ""),
            output=bundle,
            data={"change": change},
        )


class Approval:
    """The administrator's decision. The run waits here; nothing is decided by waiting."""

    WAIT: Final = ("ready", "A change is ready. Publish it as a pull request, or discard it.")
    # What the administrator is asked when the flow goes on to merge what it publishes.
    WAIT_MERGES: Final = (
        "ready",
        "A change is ready. Publish it as a pull request, which is merged once GitHub's "
        "checks pass, or discard it.",
    )

    @classmethod
    def wait(cls, document: Document, node: Node) -> tuple[str, str]:
        """What the run waits with at this decision: whether deciding to publish is
        deciding to merge as well."""
        return cls.WAIT_MERGES if merges_after(document, node) else cls.WAIT

    async def perform(self, ctx: Context) -> Performed:
        if ctx.dry:
            return Performed(
                end=End(
                    "succeeded",
                    "evaluated",
                    "The change was collected. Nothing is published in an evaluation.",
                )
            )
        decision = ctx.data.get("decision")
        if decision == "discard":
            return Performed(end=End("cancelled", "discarded", "The change was discarded."))
        if decision != "publish":
            return Performed(
                end=End("failed", "unfinished", "The run ended without a change to decide on.")
            )
        return Performed(outcome="publish")


class PullRequest:
    async def perform(self, ctx: Context) -> Performed:
        data = ctx.data
        services = ctx.services
        bundles = await ctx.outputs("change.collect")
        bundle = bundles[-1] if bundles else None
        if services.github is None or bundle is None:
            return Performed(end=End("failed", "publish_failed", "There is no change to publish."))
        try:
            files = [
                ChangedFile(
                    path=str(file["path"]),
                    mode=str(file["mode"]),
                    content=None
                    if file.get("content") is None
                    else base64.b64decode(file["content"], validate=True),
                )
                for file in bundle["files"]
            ]
        except (KeyError, TypeError, ValueError, binascii.Error):
            return Performed(
                end=End("failed", "publish_failed", "The collected change could not be read.")
            )
        issue = data["issue"]
        body = "\n\n".join(
            [
                f"Resolves #{issue['number']}."
                if issue.get("number")
                # An item of the platform's backlog has no issue to close: the pull
                # request says itself what was asked for.
                else "What was asked for:\n\n" + str(issue.get("body") or issue["title"])[:20_000],
                str((data.get("change") or {}).get("summary") or ""),
                "Made by Engineering Control: implemented by an agent on one subscription, "
                "reviewed by an agent on another, tests run in a sandbox. Published after "
                "the administrator's approval.",
            ]
        )
        try:
            pull = await services.github.publish(
                data["repository"],
                base_branch=data["base_branch"],
                base_sha=data["base_sha"],
                branch=data["branch"],
                message=deliver.commit_message(issue),
                title=str(issue["title"]),
                body=body,
                files=files,
            )
        except GitHubAppError as error:
            # Not the end: the administrator can try again once the cause is removed.
            return Performed(
                end=End(
                    "waiting",
                    "publish_failed",
                    wait=("publish_failed", f"Publishing failed: {error}"),
                )
            )
        opened = {"number": pull.number, "url": pull.url, "sha": pull.sha}
        item_id = await asyncio.to_thread(services.runs.item_of, ctx.item.run_id)
        if services.backlog is not None and item_id is not None:
            # An issue of the repository is closed by GitHub when the pull request is
            # merged, and the next import says so. An item of the platform's own has
            # nothing else to close it: what it asked for now exists as a pull request.
            closes = None if issue.get("number") else "done"
            await asyncio.to_thread(
                functools.partial(
                    services.backlog.note_facts, item_id, {"pull_request": opened}, state=closes
                )
            )
        return Performed(
            outcome="ok",
            end=End(
                "succeeded",
                "published",
                f"Pull request #{pull.number} is open.",
                data={"pull_request": opened},
            ),
        )


def _pull_request(data: dict[str, Any]) -> tuple[int, str] | None:
    """The pull request the run opened, as its number and the commit it is for."""
    pull = data.get("pull_request") or {}
    number, sha = pull.get("number"), str(pull.get("sha") or "")
    if isinstance(number, int) and number > 0 and len(sha) == 40:
        return number, sha
    return None


class Checks:
    """Wait for what GitHub runs on the pull request the run opened. The platform asks
    GitHub from time to time; no sandbox runs meanwhile."""

    # How often GitHub is asked, and how long checks that have not appeared are waited for
    # before it is said that there are none.
    POLL_SECONDS: Final = 30
    APPEAR_SECONDS: Final = 120

    async def perform(self, ctx: Context) -> Performed:
        data = ctx.data
        found = _pull_request(data)
        github = ctx.services.github
        if found is None or github is None:
            return Performed(
                end=End("failed", "no_pull_request", "The run opened no pull request to check.")
            )
        number, sha = found
        if data.get("decision") == "discard":
            # Asked after the checks could not be read: the administrator stops here.
            return Performed(
                end=End("cancelled", "stopped", f"Stopped. Pull request #{number} stays open.")
            )
        kept = dict(data.get("checks") or {})
        if kept.get("sha") != sha:
            kept = {"sha": sha}
        now = time.time()
        since = float(kept.get("since") or now)
        if kept.get("result") in ("passed", "failed"):
            # Asked again after the checks ended (the launcher started anew): as it was.
            return self._ended(ctx, number, kept)
        if kept.get("asked") and now - float(kept["asked"]) < self.POLL_SECONDS:
            return Performed(end=End("running", "checking", kept.get("said"), again=True))
        try:
            checks = await github.checks(str(data["repository"]), sha)
        except GitHubAppError as error:
            if "not allowed" in str(error):
                # Nothing a later try cures: the app has to be given the permission.
                return Performed(
                    end=End(
                        "waiting",
                        "checks_unreadable",
                        wait=(
                            "checks_unreadable",
                            f"Pull request #{number} is open, but GitHub's checks cannot be "
                            f"read: {error} Then decide to publish again, which only looks at "
                            "the checks again; discarding stops here and leaves the pull "
                            "request open.",
                        ),
                    )
                )
            said = f"GitHub's checks on pull request #{number} could not be read just now: {error}"
            return Performed(
                end=End(
                    "running",
                    "checking",
                    said,
                    again=True,
                    data={"checks": {**kept, "since": since, "asked": now, "said": said}},
                )
            )
        minutes = max(
            5, min(int(ctx.params.get("minutes") or CHECKS_MINUTES), CHECKS_MINUTES_CEILING)
        )
        waited = now - since
        seen = {"sha": sha, "since": since, "asked": now, "total": checks.total}
        if checks.state == "failed":
            names = ", ".join(checks.failed)
            return self._ended(
                ctx,
                number,
                {
                    **seen,
                    "result": "failed",
                    "said": f"{len(checks.failed)} of {checks.total} checks of GitHub's failed "
                    f"on pull request #{number}: {names}.",
                    "failed": list(checks.failed),
                },
            )
        if checks.state == "passed":
            return self._ended(
                ctx,
                number,
                {
                    **seen,
                    "result": "passed",
                    "said": f"All {checks.total} checks of GitHub's passed on pull request "
                    f"#{number}.",
                },
            )
        if checks.state == "none" and waited >= self.APPEAR_SECONDS:
            allowed = bool(ctx.params.get("allow_none"))
            return self._ended(
                ctx,
                number,
                {
                    **seen,
                    "result": "passed" if allowed else "failed",
                    "said": f"GitHub runs no checks on pull request #{number}."
                    + ("" if allowed else " Nothing checked it, so it does not count as passed."),
                },
            )
        if waited >= minutes * 60:
            names = ", ".join(checks.pending) or "none has appeared"
            return self._ended(
                ctx,
                number,
                {
                    **seen,
                    "result": "failed",
                    "said": f"GitHub's checks on pull request #{number} did not finish within "
                    f"{minutes} minutes: {names}.",
                    "failed": list(checks.pending),
                },
            )
        said = (
            f"Waiting for GitHub's checks on pull request #{number}: {checks.done} of "
            f"{checks.total} done."
            if checks.total
            else f"Waiting for GitHub's checks on pull request #{number} to appear."
        )
        return Performed(
            end=End(
                "running", "checking", said, again=True, data={"checks": {**seen, "said": said}}
            )
        )

    @staticmethod
    def _ended(ctx: Context, number: int, kept: dict[str, Any]) -> Performed:
        passed = kept.get("result") == "passed"
        return Performed(
            outcome="passed" if passed else "failed",
            end=End(
                "succeeded" if passed else "failed",
                "checks_passed" if passed else "checks_failed",
                str(kept.get("said") or "")
                + ("" if passed else f" Pull request #{number} stays open; nothing is merged."),
                data={"checks": kept},
            ),
        )


class Merge:
    """Merge the pull request the run opened, as it stood when GitHub's checks passed it."""

    async def perform(self, ctx: Context) -> Performed:
        data = ctx.data
        found = _pull_request(data)
        github = ctx.services.github
        if found is None or github is None:
            return Performed(
                end=End("failed", "no_pull_request", "The run opened no pull request to merge.")
            )
        number, sha = found
        checks = data.get("checks") or {}
        if checks.get("result") != "passed" or checks.get("sha") != sha:
            # The rules of a flow see to it; this holds whatever a flow says.
            return Performed(
                end=End(
                    "failed",
                    "not_checked",
                    f"GitHub's checks have not passed pull request #{number}: it is not merged.",
                )
            )
        method = str(ctx.params.get("method") or "squash")
        try:
            made = await github.merge(str(data["repository"]), number, sha=sha, method=method)
        except GitHubAppError as error:
            return Performed(
                end=End(
                    "failed",
                    "merge_refused",
                    f"Pull request #{number} was not merged: {error}. It stays open.",
                )
            )
        merged = {**(data.get("pull_request") or {}), "merged": made or True}
        item_id = await asyncio.to_thread(ctx.services.runs.item_of, ctx.item.run_id)
        if ctx.services.backlog is not None and item_id is not None:
            await asyncio.to_thread(
                functools.partial(
                    ctx.services.backlog.note_facts, item_id, {"pull_request": merged}, state="done"
                )
            )
        return Performed(
            outcome="ok",
            end=End(
                "succeeded",
                "merged",
                f"Pull request #{number} is merged into {data.get('base_branch')}.",
                data={"pull_request": merged},
            ),
        )


def merges_after(document: Document, node: Node) -> bool:
    """Whether the flow, going on from this task by the platform's own tasks, comes to
    one that merges."""
    seen: set[str] = set()
    todo = [target for target in node.next.values() if target]
    while todo:
        current = document.node(todo.pop())
        if current is None or current.id in seen or current.task not in TASKS:
            continue
        seen.add(current.id)
        if TASKS[current.task].merges:
            return True
        todo += [target for target in current.next.values() if target]
    return False


# --- auditing ---------------------------------------------------------------------------


class PlanAreas:
    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        prompt = ctx.prompt(
            {
                "repository": ctx.data["repository"],
                "base_sha": ctx.data["base_sha"],
                "max_areas": audit.MAX_AREAS,
            }
        )
        return _reader(ctx, lease, prompt, audit.PLAN_SCHEMA)

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        answer, instead = _answer(ctx, commands, who="The planner")
        if answer is None:
            return instead or Failed("no_answer", "The planner gave no answer.")
        planned = audit.areas(answer)
        summary = _words(answer.get("summary"))
        output = {
            "summary": str(answer.get("summary") or "")[:20_000],
            "areas": planned,
            "left_out": [str(entry)[:300] for entry in (answer.get("left_out") or [])[:60]],
        }
        if not planned:
            # Nothing to split: the repository is audited as one scope after all.
            return Done("none", "one_area", f"No areas were named: {summary}", output=output)
        return Done(
            "areas",
            "planned",
            f"{len(planned)} area(s): {summary}",
            output=output,
            data={"areas": planned},
        )


def _browsing(ctx: Context, plan: Plan) -> Plan:
    """A step that looks at the project's application: it is told where it runs, may
    reach its hosts, and finds the test identity when one is stored."""
    application = ctx.data.get("application") or {}
    url = str(application.get("url") or "")
    if not url:
        return plan
    job = {
        **plan.job,
        "commands": [
            {**command, "env": {**command.get("env", {}), "APPLICATION_URL": url}}
            if command.get("name") == "agent"
            else command
            for command in plan.job.get("commands", [])
        ],
    }
    return dataclasses.replace(
        plan,
        job=job,
        application_hosts=tuple(
            str(host) for host in (application.get("hosts") or [application_host(url)]) if host
        ),
        identity_secret=str(application["secret"])
        if application.get("identity") and application.get("secret")
        else None,
    )


class Audit:
    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        data = ctx.data
        area = ctx.each("areas")
        planned = data.get("areas") or []
        prompt = ctx.prompt(
            {
                "application": data.get("application"),
                "repository": data["repository"],
                "base_sha": data["base_sha"],
                "max_findings": audit.MAX_FINDINGS,
                "scope": data.get("scope") or {},
                "area": (planned[area], area, len(planned))
                if area is not None and area < len(planned)
                else None,
            }
        )
        plan = _reader(ctx, lease, prompt, audit.AUDIT_SCHEMA)
        return _browsing(ctx, plan) if ctx.task.application else plan

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        answer, instead = _answer(ctx, commands, who="The auditor")
        if answer is None:
            return instead or Failed("no_answer", "The auditor gave no answer.")
        area = ctx.each("areas")
        summary = _words(answer.get("summary"))
        blocked = answer.get("decision") == "BLOCKED"
        if blocked and area is None:
            return Failed("blocked", f"The auditor could not audit the scope: {summary}", answer)
        candidates = [] if blocked else audit.candidates(answer)
        audited: dict[str, Any] = {
            "area": area,
            "blocked": blocked,
            "summary": str(answer.get("summary") or "")[:20_000],
            "covered": [str(entry)[:300] for entry in (answer.get("covered") or [])[:60]],
            "not_covered": [str(entry)[:300] for entry in (answer.get("not_covered") or [])[:60]],
            "open_questions": [
                str(entry)[:1_000] for entry in (answer.get("open_questions") or [])[:30]
            ],
            "candidates": candidates,
        }
        if blocked:
            # One area that could not be audited does not end an audit of the whole.
            return Done(
                "nothing",
                "area_blocked",
                f"This area could not be audited: {summary}",
                output=audited,
            )
        if not candidates:
            return Done("nothing", "no_findings", summary, output=audited)
        return Done(
            "candidates",
            "candidates",
            f"{len(candidates)} candidate finding(s): {summary}",
            output=audited,
            data={"auditor": ctx.item.provider},
            given={"ordinal": 1},
        )


class Verify:
    """Independent reproductions, one after the other; no round is shown another."""

    def _rounds(self, ctx: Context) -> int:
        try:
            return max(1, min(int(ctx.params.get("rounds") or 2), 3))
        except (TypeError, ValueError):
            return 2

    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        data = ctx.data
        area = ctx.each("areas")
        found = await ctx.outputs(AUDITING)
        mine = [output for output in found if output.get("area") == area]
        prompt = ctx.prompt(
            {
                "application": data.get("application"),
                "repository": data["repository"],
                "base_sha": data["base_sha"],
                "findings": (mine[-1] if mine else {}).get("candidates") or [],
                "known": await _known(ctx),
                "ordinal": int(ctx.given.get("ordinal") or 1),
                "tests": [command.text for command in ctx.toolchain().tests],
            }
        )
        plan = _reader(ctx, lease, prompt, audit.VERIFY_SCHEMA)
        return _browsing(ctx, plan) if ctx.task.application else plan

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        answer, instead = _answer(ctx, commands, who="The verifier")
        if answer is None:
            return instead or Failed("no_answer", "The verifier gave no answer.")
        ordinal = int(ctx.given.get("ordinal") or 1)
        summary = _words(answer.get("summary"))
        detail = f"Round {ordinal}: {summary}"
        if ordinal < self._rounds(ctx):
            # The next reproduction, by the same subscription, told nothing of this one.
            again = {key: value for key, value in ctx.given.items() if key != "asked_again"}
            return Again(
                "verified",
                detail,
                {**again, "ordinal": ordinal + 1},
                output={"round": ordinal, "area": ctx.each("areas"), **answer},
            )
        return Done(
            "done",
            "verified",
            detail,
            output={"round": ordinal, "area": ctx.each("areas"), **answer},
        )


def _counted(counts: dict[str, int], words: dict[str, str]) -> str:
    return ", ".join(f"{counts[key]} {word}" for key, word in words.items() if counts[key])


async def _close_request(ctx: Context, facts: dict[str, Any]) -> None:
    """A run that an item asked for has done what the item asked."""
    services = ctx.services
    if ctx.dry or services.backlog is None:
        return
    item_id = await asyncio.to_thread(services.runs.item_of, ctx.item.run_id)
    if item_id is not None:
        await asyncio.to_thread(
            functools.partial(services.backlog.note_facts, item_id, facts, state="done")
        )


class ProposeFindings:
    """Decide from the rounds of verification, by the original's rule, and add what is
    confirmed to the backlog."""

    async def perform(self, ctx: Context) -> Performed:
        data = ctx.data
        services = ctx.services
        run_id = ctx.item.run_id
        audits = await ctx.outputs(AUDITING)
        if not audits:
            return Performed(end=End("failed", "unfinished", "The run ended without an audit."))
        plans = await ctx.outputs("agent.plan_areas")
        planned = data.get("areas") or []
        rounds = await ctx.outputs(VERIFYING)
        # How many independent reproductions decide a finding: what the flow says.
        wanted = max(1, min(ctx.setting(VERIFYING, "rounds", len(audit.ROUNDS)), 3))
        kind = str(ctx.params.get("kind") or "finding")
        if kind not in ITEM_KINDS:
            kind = "finding"
        # The latest audit of each area: a step given again to another account answers
        # once more for the same area.
        by_area: dict[int | None, dict[str, Any]] = {}
        for output in audits:
            by_area[output.get("area")] = output
        findings: list[dict[str, Any]] = []
        areas: list[dict[str, Any]] = []
        counts = {"CONFIRMED": 0, "REJECTED": 0, "FLAKY": 0, "BLOCKED": 0, "DUPLICATE": 0}
        number = 0
        for area, audited in sorted(by_area.items(), key=lambda entry: entry[0] or 0):
            by_round = {
                int(answer.get("round") or 0): answer
                for answer in rounds
                if answer.get("area") == area
            }
            answers = [by_round[ordinal] for ordinal in sorted(by_round)]
            name = planned[area]["name"] if area is not None and area < len(planned) else None
            confirmed = 0
            for local, finding in enumerate(audited.get("candidates") or [], start=1):
                number += 1
                tried = audit.attempts(answers, local)
                verdict = audit.disposition(tried, wanted)
                entry: dict[str, Any] = {
                    "number": number,
                    "title": finding["title"],
                    "severity": finding["severity"],
                    "lens": finding["lens"],
                    "area": name,
                    "disposition": verdict,
                    "notes": [
                        f"Round {index}: " + " ".join(str(t.get("note") or "").split())[:600]
                        for index, t in enumerate(tried, start=1)
                    ],
                    "duplicate_of": next(
                        (str(t["duplicate_of"])[:300] for t in tried if t.get("duplicate_of")),
                        None,
                    ),
                    "item_id": None,
                }
                if verdict == "CONFIRMED" and not ctx.dry and services.backlog is not None:
                    made = await asyncio.to_thread(
                        functools.partial(
                            services.backlog.produce,
                            data["project"],
                            title=finding["title"],
                            section=data.get("section"),
                            description=audit.description(finding, tried, data["base_sha"]),
                            facts={
                                "kind": kind,
                                "severity": finding["severity"],
                                "lens": finding["lens"],
                                "confidence": finding["confidence"],
                                "component": finding["affected_component"],
                                "files": finding["files"][:20],
                                "commit": data["base_sha"],
                            },
                            proposed_by="audit",
                            origin_ref=f"run:{run_id}:finding:{number}",
                            reason=f"Found and verified by the audit of {data['base_sha'][:12]}.",
                            run_id=run_id,
                        )
                    )
                    if made is None:
                        # The backlog already has it, from this run or under the same title.
                        verdict = entry["disposition"] = "DUPLICATE"
                    else:
                        entry["item_id"] = made["id"]
                if verdict == "CONFIRMED":
                    confirmed += 1
                counts[verdict] += 1
                findings.append(entry)
            if area is not None:
                areas.append(
                    {
                        "name": name,
                        "paths": (planned[area]["paths"] if area < len(planned) else [])[:12],
                        "summary": _words(audited.get("summary"), 600),
                        "blocked": bool(audited.get("blocked")),
                        "candidates": len(audited.get("candidates") or []),
                        "confirmed": confirmed,
                    }
                )

        def gathered(key: str, limit: int) -> list[str]:
            seen: list[str] = []
            for output in by_area.values():
                for value in output.get(key) or []:
                    if value not in seen:
                        seen.append(value)
            return seen[:limit]

        reached = {entry["name"] for entry in areas}
        missed = [str(e.get("name")) for e in planned if str(e.get("name")) not in reached]
        plan = plans[-1] if plans else {}
        result = {
            "summary": plan.get("summary") if planned else next(iter(by_area.values()))["summary"],
            "covered": gathered("covered", 80),
            "not_covered": (
                gathered("not_covered", 60)
                + [str(entry) for entry in plan.get("left_out") or []]
                + [f"The area {name}: not reached" for name in missed]
            )[:100],
            "open_questions": gathered("open_questions", 40),
            "findings": findings,
            "counts": counts,
            "areas": areas,
        }
        said = _counted(
            counts,
            {
                "CONFIRMED": "confirmed",
                "REJECTED": "not reproduced",
                "FLAKY": "reproduced once only",
                "BLOCKED": "could not be verified",
                "DUPLICATE": "already known",
            },
        )
        await _close_request(ctx, {"audit_run": str(run_id), "confirmed": counts["CONFIRMED"]})
        over = f" in {len(areas)} area(s)" if areas else ""
        if not number:
            end = End(
                "succeeded",
                "no_findings",
                f"The audit found nothing{over}.",
                data={"result": result},
            )
        else:
            end = End(
                "succeeded",
                "findings" if counts["CONFIRMED"] else "nothing_confirmed",
                f"{number} candidate finding(s){over}: {said}.",
                data={"result": result},
            )
        return Performed(outcome="ok", end=end)


# --- looking for enhancements -----------------------------------------------------------


class Discover:
    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        data = ctx.data
        prompt = ctx.prompt(
            {
                "repository": data["repository"],
                "base_sha": data["base_sha"],
                "max_proposals": discover.MAX_PROPOSALS,
                "scope": data.get("scope") or {},
                "known": await _known(ctx),
            }
        )
        return _reader(ctx, lease, prompt, discover.DISCOVER_SCHEMA)

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        answer, instead = _answer(ctx, commands, who="The discoverer")
        if answer is None:
            return instead or Failed("no_answer", "The discoverer gave no answer.")
        proposals = discover.proposals(answer)
        summary = _words(answer.get("summary"))
        output = {"summary": str(answer.get("summary") or "")[:20_000], "proposals": proposals}
        if not proposals:
            return Done("nothing", "nothing_proposed", summary, output=output)
        return Done(
            "proposals",
            "proposed",
            f"{len(proposals)} proposal(s): {summary}",
            output=output,
            data={"discoverer": ctx.item.provider},
        )


class ReviewProposals:
    async def plan(self, ctx: Context, lease: Lease | None) -> Plan:
        data = ctx.data
        found = await ctx.outputs("agent.discover")
        prompt = ctx.prompt(
            {
                "repository": data["repository"],
                "base_sha": data["base_sha"],
                "proposals": (found[-1] if found else {}).get("proposals") or [],
                "known": await _known(ctx),
            }
        )
        return _reader(ctx, lease, prompt, discover.REVIEW_SCHEMA)

    async def conclude(self, ctx: Context, commands: list[dict[str, Any]], log: str) -> Concluded:
        answer, instead = _answer(ctx, commands, who="The reviewer")
        if answer is None:
            return instead or Failed("no_answer", "The reviewer gave no answer.")
        return Done("done", "reviewed", _words(answer.get("summary")), output=answer)


class ProposeEnhancements:
    async def perform(self, ctx: Context) -> Performed:
        data = ctx.data
        services = ctx.services
        run_id = ctx.item.run_id
        found = await ctx.outputs("agent.discover")
        if not found:
            return Performed(end=End("failed", "unfinished", "The run ended without a search."))
        proposals = found[-1].get("proposals") or []
        reviews = await ctx.outputs("agent.review_proposals")
        review = reviews[-1] if reviews else {}
        listed: list[dict[str, Any]] = []
        counts = {
            "APPROVE": 0,
            "REQUEST_CHANGES": 0,
            "BLOCKED": 0,
            "DUPLICATE": 0,
            "NOT_REVIEWED": 0,
        }
        for number, proposal in enumerate(proposals, start=1):
            judged = discover.review_of(review, number)
            verdict = discover.verdict(judged)
            entry: dict[str, Any] = {
                "number": number,
                "title": proposal["title"],
                "risk": proposal["risk"],
                "persona": proposal["persona"],
                "disposition": verdict,
                "notes": [" ".join(str((judged or {}).get("summary") or "").split())[:600]],
                "duplicate_of": str((judged or {}).get("duplicate_of") or "")[:300] or None,
                "item_id": None,
            }
            writes = not ctx.dry and services.backlog is not None
            if verdict == "APPROVE" and judged is not None and writes and services.backlog:
                made = await asyncio.to_thread(
                    functools.partial(
                        services.backlog.produce,
                        data["project"],
                        title=proposal["title"],
                        section=data.get("section"),
                        description=discover.description(proposal, judged, data["base_sha"]),
                        facts={
                            "kind": "enhancement",
                            "risk": proposal["risk"],
                            "persona": proposal["persona"],
                            "commit": data["base_sha"],
                        },
                        proposed_by="discovery",
                        origin_ref=f"run:{run_id}:enhancement:{number}",
                        reason="Proposed by a search for enhancements and approved in review.",
                        run_id=run_id,
                    )
                )
                if made is None:
                    verdict = entry["disposition"] = "DUPLICATE"
                else:
                    entry["item_id"] = made["id"]
            counts[verdict] += 1
            listed.append(entry)
        result = {"summary": found[-1].get("summary"), "proposals": listed, "counts": counts}
        if not proposals:
            end = End(
                "succeeded",
                "nothing_proposed",
                "The search proposed nothing.",
                data={"result": result},
            )
        else:
            said = _counted(
                counts,
                {
                    "APPROVE": "wait for your approval",
                    "REQUEST_CHANGES": "would need changes",
                    "BLOCKED": "stopped in review",
                    "DUPLICATE": "already known",
                    "NOT_REVIEWED": "not reviewed",
                },
            )
            end = End(
                "succeeded",
                "proposed" if counts["APPROVE"] else "nothing_approved",
                f"{len(proposals)} proposal(s): {said}.",
                data={"result": result},
            )
        return Performed(outcome="ok", end=end)


SANDBOX: Final[dict[str, SandboxTask]] = {
    "workspace.clone": Clone(),
    "workspace.install": Install(),
    "check.tests": RunTests(),
    "check.secrets": ScanSecrets(),
    "workspace.checkout": Checkout(),
    "agent.implement": Implement(),
    "agent.review": Review(),
    "agent.change": Change(),
    "agent.check": Check(),
    "agent.report": Report(),
    "check.change": CheckChange(),
    "change.collect": CollectChange(),
    "agent.plan_areas": PlanAreas(),
    "agent.audit": Audit(),
    "agent.verify": Verify(),
    # The same two, looking at the running application: the catalogue says so, and the
    # prompt and what the step may reach follow from that.
    "agent.audit_application": Audit(),
    "agent.verify_application": Verify(),
    "agent.discover": Discover(),
    "agent.review_proposals": ReviewProposals(),
}
PLATFORM: Final[dict[str, PlatformTask]] = {
    "people.approval": Approval(),
    "github.pull_request": PullRequest(),
    "github.checks": Checks(),
    "github.merge": Merge(),
    "findings.propose": ProposeFindings(),
    "proposals.propose": ProposeEnhancements(),
}
assert set(SANDBOX) | set(PLATFORM) == {  # noqa: S101 - the catalogue and the code agree
    # The tasks that ask a model over an API are in ``asking``, the ones the run's
    # workbench carries out in ``bench_tasks``.
    key
    for key, task in TASKS.items()
    if task.where not in ("control", "model", "bench")
}
