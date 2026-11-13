"""What a run of each kind does: the job of each step, and what follows from its result.

The launcher knows pods; a flow knows work. For the step the launcher is about to start, a
flow says what the sandbox is to do and what it needs (``plan``). For a step that ended, it
reads the result and says what it means and which step follows (``conclude``). When no
step is left it says how the run ends (``done``).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from uuid import UUID

from engineering_control.github_app import ChangedFile, GitHubApp, GitHubAppError
from engineering_control.sandbox import deliver, steps
from engineering_control.sandbox.pods import Egress
from engineering_control.sandbox.runs import Lease, Runs, StepWork
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


class DeliverIssue:
    """Check out, implement, review, verify, bundle; then wait for the administrator and
    publish the change as a pull request, or discard it."""

    def __init__(self, runs: Runs, github: GitHubApp | None) -> None:
        self.runs = runs
        self.github = github
        # Tokens issued for a checkout, by step, so that each is revoked when its step
        # has ended. A token forgotten here expires by itself within the hour.
        self._tokens: dict[UUID, str] = {}

    # --- planning ------------------------------------------------------------------------

    async def plan(self, item: StepWork, lease: Lease | None) -> Plan:
        data = item.run_data
        toolchain = deliver.Toolchain.from_dict(data.get("toolchain"))
        if item.name == "prepare":
            if self.github is None:
                raise GitHubAppError(
                    "The GitHub App is not configured, so nothing can be checked out"
                )
            # The issue as it reads now, for the agents; then a token for this checkout.
            issue = await self.github.issue(data["repository"], int(data["issue"]["number"]))
            await asyncio.to_thread(
                self.runs.merge_data,
                item.run_id,
                {
                    "issue": {
                        "number": issue.number,
                        "title": issue.title or data["issue"]["title"],
                        "url": issue.url or data["issue"].get("url"),
                        "body": issue.body,
                    }
                },
            )
            token = await self.github.repository_token(data["repository"], {"contents": "read"})
            self._tokens[item.step_id] = token
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
            return Plan(
                deliver.prepare_job(
                    repository=data["repository"],
                    base_branch=data["base_branch"],
                    branch=data["branch"],
                    authorization=f"Basic {basic}",
                    toolchain=toolchain,
                ),
                workspace=True,
                deadline_seconds=3600,
            )
        if item.name == "implement":
            if lease is None:
                raise ValueError("implementing needs an account")
            prompt = deliver.maker_prompt(
                repository=data["repository"],
                issue=data["issue"],
                toolchain=toolchain,
                findings=item.input.get("findings"),
                failures=item.input.get("failures"),
            )
            message = f"{data['issue']['title']}\n\nResolves #{data['issue']['number']}."
            return Plan(
                deliver.implement_job(lease.provider, lease.generation, prompt, message),
                workspace=True,
                memory="6Gi",
                deadline_seconds=3600,
            )
        if item.name == "review":
            if lease is None:
                raise ValueError("reviewing needs an account")
            prompt = deliver.checker_prompt(
                repository=data["repository"],
                issue=data["issue"],
                base_sha=data["base_sha"],
                toolchain=toolchain,
                summary=str(item.input.get("summary") or ""),
            )
            return Plan(
                deliver.review_job(lease.provider, lease.generation, prompt),
                workspace=True,
                memory="6Gi",
                deadline_seconds=3600,
            )
        if item.name == "verify":
            return Plan(
                deliver.verify_job(data["base_sha"], toolchain),
                egress="none",
                workspace=True,
                memory="6Gi",
                deadline_seconds=3600,
            )
        if item.name == "bundle":
            return Plan(
                deliver.bundle_job(data["base_sha"]),
                egress="none",
                workspace=True,
                cpu="1",
                memory="2Gi",
                deadline_seconds=600,
            )
        raise ValueError(f"a delivery has no step {item.name}")

    async def ended(self, item: StepWork) -> None:
        token = self._tokens.pop(item.step_id, None)
        if token is not None and self.github is not None:
            await self.github.revoke(token)

    # --- reading results -----------------------------------------------------------------

    async def conclude(
        self, item: StepWork, commands: list[dict[str, Any]] | None, log: str
    ) -> Result:
        if not commands:
            return _failed("no_result", "The sandbox ended without a result.")
        reader = getattr(self, f"_after_{item.name}", None)
        if reader is None:
            return _failed("unknown", f"A delivery has no step {item.name}.")
        result: Result = reader(item, commands, log)
        return result

    def _after_prepare(self, item: StepWork, commands: list[dict[str, Any]], log: str) -> Result:
        by_name = {str(command.get("name")): command for command in commands}
        checkout = by_name.get("checkout")
        if checkout is None or checkout.get("exit_code") != 0:
            return _failed(
                "checkout_failed",
                "The repository could not be checked out: " + _tail(checkout or commands[-1]),
            )
        for command in commands:
            name = str(command.get("name"))
            if name.startswith("install") and command.get("exit_code") != 0:
                return _failed(
                    "install_failed", f"Installing what the project needs failed: {_tail(command)}"
                )
        base_sha = _last_line(checkout)
        if len(base_sha) != 40:
            return _failed("checkout_failed", "The checkout did not say which commit it is at.")
        baseline = {
            name.removeprefix("baseline "): command.get("exit_code")
            for name, command in by_name.items()
            if name.startswith("baseline ")
        }
        return Result(
            Verdict(True, "prepared", "Checked out and installed."),
            output={"base_sha": base_sha, "baseline": baseline},
            follow=(Follow("implement", ",".join(deliver.PROVIDERS), {"round": 1}),),
            data={"base_sha": base_sha, "baseline": baseline},
        )

    def _account_trouble(self, item: StepWork, commands: list[dict[str, Any]]) -> Result | None:
        """A step whose agent was refused by the subscription says so about the account and
        is given to another account, a few times at most."""
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
        provider = ",".join(deliver.PROVIDERS) if item.name == "implement" else item.provider
        if (
            item.name == "implement"
            and item.run_data.get("maker")
            and item.input.get("round", 1) > 1
        ):
            provider = item.run_data["maker"]
        return Result(
            Verdict(
                True,
                problem.outcome,
                f"{problem.detail} The step is tried again with another account.",
                account_state=problem.account_state,
                account_reason=problem.account_reason,
                limited_until=problem.limited_until,
            ),
            follow=(Follow(item.name, provider, again),),
        )

    def _after_implement(self, item: StepWork, commands: list[dict[str, Any]], log: str) -> Result:
        trouble = self._account_trouble(item, commands)
        if trouble is not None:
            return trouble
        provider = item.provider or ""
        answer = deliver.agent_answer(provider, commands)
        agent = next((c for c in commands if c.get("name") == "agent"), commands[0])
        commit = next((c for c in commands if c.get("name") == "commit"), None)
        head = _last_line(commit) if commit and commit.get("exit_code") == 0 else ""
        changed = len(head) == 40 and head != item.run_data.get("base_sha")
        if answer is None:
            # The agent ended without its closing answer: it ran out of time, or the
            # provider cut its last message off. Work it did is not thrown away for that:
            # it is committed, and the reviewer and the tests judge it as it is.
            why = (
                "it ran out of time"
                if agent.get("timed_out")
                else _short(deliver.agent_errors(provider, commands)) or "it ended early"
            )
            if not changed:
                return _failed(
                    "no_answer", f"The implementer changed nothing and gave no answer: {why}"
                )
            answer = {
                "decision": "COMPLETE",
                "summary": f"The implementer ended without its closing answer ({why}). What "
                "it changed is committed and is reviewed as it stands.",
                "tests": [],
                "blocked_reason": "",
            }
        if answer.get("decision") != "COMPLETE":
            reason = str(answer.get("blocked_reason") or answer.get("summary") or "no reason given")
            return Result(
                Verdict(False, "blocked", f"The implementer could not resolve the issue: {reason}"),
                output=answer,
            )
        if len(head) != 40:
            return _failed(
                "commit_failed", f"The change could not be committed: {_tail(commit or agent)}"
            )
        if not changed:
            return Result(
                Verdict(False, "no_change", "The implementer says it is done but changed nothing."),
                output=answer,
            )
        round_number = int(item.input.get("round") or 1)
        other = next(name for name in deliver.PROVIDERS if name != provider)
        return Result(
            Verdict(True, "implemented", str(answer.get("summary") or "")[:500]),
            output=answer,
            follow=(
                Follow(
                    "review",
                    other,
                    {"round": round_number, "summary": str(answer.get("summary") or "")},
                ),
            ),
            data={"maker": provider, "head": head, "round": round_number},
        )

    def _rework(
        self,
        item: StepWork,
        outcome: str,
        detail: str,
        given: dict[str, Any],
        output: dict[str, Any],
    ) -> Result:
        round_number = int(item.input.get("round") or 1)
        if round_number >= deliver.MAX_ROUNDS:
            return Result(
                Verdict(False, outcome, f"{detail} That was round {round_number}, the last one."),
                output=output,
            )
        return Result(
            Verdict(True, outcome, detail),
            output=output,
            follow=(
                Follow(
                    "implement", item.run_data.get("maker"), {"round": round_number + 1, **given}
                ),
            ),
        )

    def _after_review(self, item: StepWork, commands: list[dict[str, Any]], log: str) -> Result:
        trouble = self._account_trouble(item, commands)
        if trouble is not None:
            return trouble
        answer = deliver.agent_answer(item.provider or "", commands)
        agent = next((c for c in commands if c.get("name") == "agent"), commands[0])
        if answer is None:
            why = (
                "it ran out of time"
                if agent.get("timed_out")
                else _short(deliver.agent_errors(item.provider or "", commands)) or "it ended early"
            )
            asked = int(item.input.get("asked_again") or 0)
            if asked >= 1:
                return _failed("no_answer", f"The reviewer gave no answer twice: {why}")
            # A review without a verdict is no review: ask once more.
            return Result(
                Verdict(
                    True, "no_answer", f"The reviewer gave no answer ({why}). It is asked again."
                ),
                follow=(Follow("review", item.provider, {**item.input, "asked_again": asked + 1}),),
            )
        decision = answer.get("decision")
        summary = str(answer.get("summary") or "")[:500]
        if decision == "PASS":
            return Result(
                Verdict(True, "passed", summary),
                output=answer,
                follow=(Follow("verify", None, {"round": item.input.get("round") or 1}),),
            )
        if decision == "REQUEST_CHANGES":
            findings = [f for f in answer.get("findings") or [] if isinstance(f, dict)]
            return self._rework(
                item,
                "changes_requested",
                f"The reviewer asks for changes: {summary}",
                {"findings": findings},
                answer,
            )
        return Result(
            Verdict(False, "rejected", f"The reviewer stopped the change: {summary}"), output=answer
        )

    def _after_verify(self, item: StepWork, commands: list[dict[str, Any]], log: str) -> Result:
        baseline = item.run_data.get("baseline") or {}
        toolchain = deliver.Toolchain.from_dict(item.run_data.get("toolchain"))
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
            return Result(
                Verdict(
                    False,
                    "secret_found",
                    "The change holds something that looks like a credential.",
                ),
                output=output,
            )
        if secrets.get("exit_code") != 0:
            return Result(
                Verdict(
                    False, "scan_failed", f"The scan for secrets did not run: {_tail(secrets)}"
                ),
                output=output,
            )
        if regressions:
            return self._rework(
                item,
                "tests_failed",
                f"{len(regressions)} test command(s) that passed before the change fail after it.",
                {"failures": regressions},
                output,
            )
        note = f" {len(already)} command(s) failed before the change as well." if already else ""
        return Result(
            Verdict(True, "verified", f"The tests pass and no secret was found.{note}"),
            output=output,
            follow=(Follow("bundle", None, None),),
        )

    def _after_bundle(self, item: StepWork, commands: list[dict[str, Any]], log: str) -> Result:
        bundle = steps.parse_artifact(log, "bundle")
        if bundle is None or not isinstance(bundle.get("files"), list):
            return _failed(
                "bundle_failed", f"The change could not be collected: {_tail(commands[-1])}"
            )
        paths = [str(file.get("path")) for file in bundle["files"]]
        refused = deliver.protected_paths(paths)
        if refused:
            return Result(
                Verdict(
                    False,
                    "protected_files",
                    "The change touches files an agent may not change: " + ", ".join(refused[:10]),
                ),
                output={"files": [{"path": p, "status": "?"} for p in paths]},
            )
        change = {
            "summary": str(bundle.get("summary") or ""),
            "files": [{"path": f.get("path"), "status": f.get("status")} for f in bundle["files"]],
            "diff": str(bundle.get("diff") or ""),
            "diff_complete": bool(bundle.get("diff_complete")),
            "head": bundle.get("head"),
        }
        return Result(
            Verdict(True, "bundled", str(bundle.get("summary") or "")),
            output=bundle,
            data={"change": change},
            wait=("ready", "A change is ready. Publish it as a pull request, or discard it."),
        )

    # --- the end -------------------------------------------------------------------------

    async def done(self, item: StepWork) -> End:
        data = item.run_data
        decision = data.get("decision")
        if decision == "discard":
            return End("cancelled", "discarded", "The change was discarded.")
        if decision != "publish":
            return End("failed", "unfinished", "The run ended without a change to decide on.")
        bundle = await asyncio.to_thread(self.runs.step_output, item.run_id, "bundle")
        if self.github is None or bundle is None:
            return End("failed", "publish_failed", "There is no change to publish.")
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
            return End("failed", "publish_failed", "The collected change could not be read.")
        issue = data["issue"]
        body = "\n\n".join(
            [
                f"Resolves #{issue['number']}.",
                str((data.get("change") or {}).get("summary") or ""),
                "Made by Engineering Control: implemented by an agent on one subscription, "
                "reviewed by an agent on another, tests run in a sandbox. Published after "
                "the administrator's approval.",
            ]
        )
        try:
            pull = await self.github.publish(
                data["repository"],
                base_branch=data["base_branch"],
                base_sha=data["base_sha"],
                branch=data["branch"],
                message=f"{issue['title']}\n\nResolves #{issue['number']}.",
                title=str(issue["title"]),
                body=body,
                files=files,
            )
        except GitHubAppError as error:
            # Not the end: the administrator can try again once the cause is removed.
            return End(
                "waiting", "publish_failed", wait=("publish_failed", f"Publishing failed: {error}")
            )
        return End(
            "succeeded",
            "published",
            f"Pull request #{pull.number} is open.",
            data={"pull_request": {"number": pull.number, "url": pull.url}},
        )
