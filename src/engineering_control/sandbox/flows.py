"""What a run of each kind does: the job of each step, and what follows from its result.

The launcher knows pods; a flow knows work. For the step the launcher is about to start, a
flow says what the sandbox is to do and what it needs (``plan``). For a step that ended, it
reads the result and says what it means and which step follows (``conclude``). When no
step is left it says how the run ends (``done``).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from uuid import UUID

from engineering_control.github_app import GitHubApp, GitHubAppError
from engineering_control.sandbox import deliver, steps
from engineering_control.sandbox.pods import Egress
from engineering_control.sandbox.runs import Lease, StepWork
from engineering_control.sandbox.steps import Verdict

MAX_ACCOUNT_RETRIES: Final = 3


@dataclass(frozen=True)
class Plan:
    """What the sandbox of a step is to do, and what it needs."""

    job: dict[str, Any]
    egress: Egress = "internet"
    workspace: bool = False
    cpu: str = "2"
    memory: str = "4Gi"
    deadline_seconds: int = 1800
    # Labels the pod carries beyond its own, for a network rule to select it by.
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Follow:
    name: str
    provider: str | None = None
    given: dict[str, Any] | None = None


@dataclass(frozen=True)
class Result:
    """What a step's result means for the run."""

    verdict: Verdict
    output: dict[str, Any] | None = None
    follow: tuple[Follow, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)
    # Set when the run now waits for the administrator: (outcome, what to tell them).
    wait: tuple[str, str] | None = None


@dataclass(frozen=True)
class End:
    state: str
    outcome: str
    detail: str | None = None
    # Set instead of an end when the run goes back to waiting (publishing failed).
    wait: tuple[str, str] | None = None
    data: dict[str, Any] = field(default_factory=dict)


class Flow(Protocol):
    async def plan(self, item: StepWork, lease: Lease | None) -> Plan: ...

    async def conclude(
        self, item: StepWork, commands: list[dict[str, Any]] | None, log: str
    ) -> Result: ...

    async def done(self, item: StepWork) -> End: ...

    async def ended(self, item: StepWork) -> None:
        """Called once a step's sandbox is gone, however it ended."""


class AccountCheck:
    """One step: ask the account's client a trivial question."""

    async def plan(self, item: StepWork, lease: Lease | None) -> Plan:
        if lease is None:
            raise ValueError("trying an account needs the account")
        return Plan(
            steps.check_job(lease.provider, lease.generation),
            cpu="1",
            memory="2Gi",
            deadline_seconds=steps.CHECK_SECONDS + 120,
        )

    async def conclude(
        self, item: StepWork, commands: list[dict[str, Any]] | None, log: str
    ) -> Result:
        return Result(steps.interpret_check(commands, datetime.now(UTC)))

    async def done(self, item: StepWork) -> End:
        return End("succeeded", "done")

    async def ended(self, item: StepWork) -> None:
        return None


def _failed(outcome: str, detail: str) -> Result:
    return Result(Verdict(False, outcome, detail))


def _tail(command: dict[str, Any], limit: int = 400) -> str:
    said = f"{command.get('stdout') or ''}\n{command.get('stderr') or ''}"
    return " ".join(said.split())[-limit:]


def _short(text: str, limit: int = 240) -> str:
    return " ".join(text.split())[-limit:]


def _last_line(command: dict[str, Any]) -> str:
    lines = str(command.get("stdout") or "").strip().splitlines()
    return lines[-1].strip() if lines else ""


def account_trouble(
    item: StepWork, commands: list[dict[str, Any]], *, retry_provider: str | None
) -> Result | None:
    """A step whose agent was refused by the subscription says so about the account and
    is given to another account, a few times at most. ``retry_provider`` names the
    providers the step may be tried on again."""
    agent = next((c for c in commands if c.get("name") in ("agent", "setup")), None)
    if agent is None or (agent.get("exit_code") == 0 and not agent.get("timed_out")):
        return None
    problem = (
        Verdict(
            False,
            "sign_in_required",
            "The stored sign-in could not be used.",
            account_state="sign_in_required",
            account_reason="The stored sign-in could not be read. Sign the account in again.",
        )
        if agent.get("name") == "setup"
        # Only what the client itself reported: the rest of an agent's output is the
        # repository's own text and says nothing about the account.
        else steps.account_problem(
            {"stdout": deliver.agent_errors(item.provider or "", commands)},
            datetime.now(UTC),
        )
    )
    if problem is None:
        return None
    retries = int(item.input.get("account_retries") or 0)
    if retries >= MAX_ACCOUNT_RETRIES:
        return Result(problem)
    # The same step again, for whichever account is free: this one is now out.
    again = {**item.input, "account_retries": retries + 1}
    return Result(
        Verdict(
            True,
            problem.outcome,
            f"{problem.detail} The step is tried again with another account.",
            account_state=problem.account_state,
            account_reason=problem.account_reason,
            limited_until=problem.limited_until,
        ),
        follow=(Follow(item.name, retry_provider, again),),
    )


def why_no_answer(provider: str, commands: list[dict[str, Any]]) -> str:
    """Why an agent ended without its closing answer, in a few words."""
    agent = next((c for c in commands if c.get("name") == "agent"), commands[0])
    if agent.get("timed_out"):
        return "it ran out of time"
    return _short(deliver.agent_errors(provider, commands)) or "it ended early"


class Checkouts:
    """Checking a repository out for a run: a token for that one clone, revoked when the
    step that used it has ended. A token forgotten here expires by itself within the hour."""

    def __init__(self, github: GitHubApp | None) -> None:
        self.github = github
        self._tokens: dict[UUID, str] = {}

    async def plan(
        self,
        item: StepWork,
        *,
        branch: str,
        toolchain: deliver.Toolchain,
        commit: str | None = None,
    ) -> Plan:
        if self.github is None:
            raise GitHubAppError("The GitHub App is not configured, so nothing can be checked out")
        data = item.run_data
        token = await self.github.repository_token(data["repository"], {"contents": "read"})
        self._tokens[item.step_id] = token
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        return Plan(
            deliver.prepare_job(
                repository=data["repository"],
                base_branch=data["base_branch"],
                branch=branch,
                authorization=f"Basic {basic}",
                toolchain=toolchain,
                commit=commit if commit and len(commit) == 40 else None,
            ),
            workspace=True,
            deadline_seconds=3600,
        )

    async def ended(self, item: StepWork) -> None:
        token = self._tokens.pop(item.step_id, None)
        if token is not None and self.github is not None:
            await self.github.revoke(token)

    @staticmethod
    def read(commands: list[dict[str, Any]]) -> tuple[str | None, Result | None]:
        """The commit the checkout is at, or why there is none."""
        by_name = {str(command.get("name")): command for command in commands}
        checkout = by_name.get("checkout")
        if checkout is None or checkout.get("exit_code") != 0:
            return None, _failed(
                "checkout_failed",
                "The repository could not be checked out: " + _tail(checkout or commands[-1]),
            )
        for command in commands:
            name = str(command.get("name"))
            if name.startswith("install") and command.get("exit_code") != 0:
                return None, _failed(
                    "install_failed", f"Installing what the project needs failed: {_tail(command)}"
                )
        base_sha = _last_line(checkout)
        if len(base_sha) != 40:
            return None, _failed(
                "checkout_failed", "The checkout did not say which commit it is at."
            )
        return base_sha, None
