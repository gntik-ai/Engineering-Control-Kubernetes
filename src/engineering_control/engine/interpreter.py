"""The interpreter: one engine that runs every flow.

The launcher knows pods and asks a flow three things about a run: what the next step's
sandbox is to do, what a finished step's result means, and how the run ends. This engine
answers them for any flow document. It finds the task the step stands for, lets the task
say what happened, and follows the document to what comes next.

What holds for every flow is kept here and not in the tasks or the documents:

- A task that judges another's work is given to the other subscription than the one
  that did the work. A task that is gone back to stays with the subscription it had.
- Going back to a task is a round. A run has as many rounds as its flow says, never more
  than the platform's ceiling; the round that would be one too many ends the run.
- A run has a budget of time and, when its flow says so, of tokens; once it is used up
  no further task is started.
- The platform's own tasks (the administrator's decision, publishing, adding to the
  backlog) come after the sandboxes' tasks and are carried out by the platform, with
  credentials no sandbox ever holds.
- A run of the evaluation lab goes through the same tasks and leaves nothing behind: it
  ends where the administrator's decision would be, and adds nothing to the backlog.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Final

import structlog

from engineering_control.dashboard.backlog_items import BacklogItems
from engineering_control.engine import catalogue
from engineering_control.engine.builtin import BUILTIN, FLOW_OF_KIND
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.document import Document, Node
from engineering_control.engine.tasks import (
    PLATFORM,
    SANDBOX,
    Again,
    Approval,
    Context,
    Done,
    Failed,
    Services,
)
from engineering_control.github_app import GitHubApp
from engineering_control.sandbox import deliver
from engineering_control.sandbox.flows import (
    Checkouts,
    End,
    Follow,
    Plan,
    Result,
    account_trouble,
)
from engineering_control.sandbox.runs import Lease, Runs, StepWork
from engineering_control.sandbox.steps import Verdict

LOG = structlog.get_logger(__name__)
# How many tasks that only choose a way may follow one another before the engine stops
# following: a flow that passes validation never comes near it.
MAX_HOPS: Final = 30
Documents = Callable[[str, int], Document | None]


def _failed(outcome: str, detail: str, output: dict[str, Any] | None = None) -> Result:
    return Result(Verdict(False, outcome, detail), output=output)


def scope_given(data: dict[str, Any]) -> bool:
    scope = data.get("scope") or {}
    return bool(str(scope.get("title") or "").strip() or str(scope.get("text") or "").strip())


class Engine:
    def __init__(
        self,
        runs: Runs,
        github: GitHubApp | None,
        backlog: BacklogItems | None,
        documents: Documents | None = None,
    ) -> None:
        self.runs = runs
        self.services = Services(runs, github, backlog, Checkouts(github))
        # Where a version of a flow other than the platform's own is read from.
        self.documents = documents

    # --- which flow, which task ----------------------------------------------------------

    def document(self, item: StepWork) -> Document:
        """The flow the run follows: the version it started with."""
        flow = item.run_data.get("flow") or {}
        key = str(flow.get("key") or FLOW_OF_KIND.get(item.run_kind) or "")
        version = int(flow.get("version") or 0)
        if version and self.documents is not None:
            found = self.documents(key, version)
            if found is not None:
                return found
        if key in BUILTIN:
            return BUILTIN[key]
        raise ValueError(f"the run follows the flow {key}, which is not there")

    def _context(self, item: StepWork, node: Node | None = None) -> Context:
        document = self.document(item)
        found = node or document.node(item.name)
        if found is None or found.task not in TASKS:
            raise ValueError(f"the flow {document.key} has no task {item.name}")
        return Context(
            item=item,
            document=document,
            node=found,
            task=TASKS[found.task],
            services=self.services,
            dry=bool(item.run_data.get("evaluation")),
        )

    # --- the launcher's three questions --------------------------------------------------

    async def plan(self, item: StepWork, lease: Lease | None) -> Plan:
        ctx = self._context(item)
        plan = await SANDBOX[ctx.node.task].plan(ctx, lease)
        noted: dict[str, Any] = {}
        if ctx.prompt_used is not None:
            # Which wording the agent was given: traceable to a version of the flow.
            noted["prompt"] = ctx.prompt_used
        if noted:
            await asyncio.to_thread(self.runs.note_step, item.step_id, noted)
        return plan

    async def conclude(
        self, item: StepWork, commands: list[dict[str, Any]] | None, log: str
    ) -> Result:
        if not commands:
            return _failed("no_result", "The sandbox ended without a result.")
        try:
            ctx = self._context(item)
        except ValueError as error:
            return _failed("unknown", str(error))
        if ctx.task.account:
            trouble = account_trouble(
                item,
                commands,
                retry_provider=self._provider(
                    ctx.node,
                    item.run_data.get("ran") or {},
                    again=int(item.input.get("round") or 1) > 1,
                ),
            )
            if trouble is not None:
                return trouble
        concluded = await SANDBOX[ctx.node.task].conclude(ctx, commands, log)
        if isinstance(concluded, Failed):
            return _failed(concluded.label, concluded.detail, concluded.output)
        if isinstance(concluded, Again):
            return Result(
                Verdict(True, concluded.label, concluded.detail),
                output=concluded.output,
                follow=(Follow(item.name, item.provider, concluded.given),),
            )
        return await self._route(ctx, concluded)

    async def done(self, item: StepWork) -> End:
        """No sandbox is left to run: carry out the platform's own tasks, or say how the
        run ended."""
        data = item.run_data
        stands = data.get("at")
        document = self.document(item)
        if not stands:
            ended = data.get("ended") or {}
            return End(
                "succeeded", str(ended.get("outcome") or "done"), ended.get("detail") or None
            )
        gathered: dict[str, Any] = {}
        node = document.node(str(stands))
        last: End | None = None
        for _hop in range(MAX_HOPS):
            if node is None or TASKS[node.task].where != "platform":
                break
            ctx = self._context(item, node)
            ctx.item.run_data.update(gathered)
            performed = await PLATFORM[node.task].perform(ctx)
            gathered.update(performed.data)
            last = performed.end
            if performed.outcome is None:
                break
            target = node.next.get(performed.outcome)
            if not target:
                break
            node = document.node(target)
        if last is None:
            return End("succeeded", "done", None, data=gathered)
        if gathered:
            return End(
                last.state,
                last.outcome,
                last.detail,
                wait=last.wait,
                data={**gathered, **last.data},
            )
        return last

    async def ended(self, item: StepWork) -> None:
        await self.services.checkouts.ended(item)

    # --- where a task's outcome leads ----------------------------------------------------

    @staticmethod
    def _provider(node: Node, ran: dict[str, Any], *, again: bool) -> str | None:
        """The subscriptions a task may be given to. ``ran`` says which subscription each
        task of the run last worked on; ``again`` that the task is gone back to."""
        task = TASKS[node.task]
        if not task.account:
            return None
        other = node.params.get("other_than")
        if other and ran.get(str(other)) in deliver.PROVIDERS:
            # The other subscription than the one whose work is judged.
            return next(name for name in deliver.PROVIDERS if name != ran[str(other)])
        if again and ran.get(node.id) in deliver.PROVIDERS:
            # A task that is gone back to stays with the subscription it had.
            return str(ran[node.id])
        providers = node.params.get("providers") or task.defaults().get("providers")
        if isinstance(providers, list) and providers:
            return ",".join(str(name) for name in providers if name in deliver.PROVIDERS)
        return ",".join(deliver.PROVIDERS)

    def _spent(self, item: StepWork, document: Document) -> str | None:
        """Why no further task may start for the run, when its budget is used up."""
        minutes = catalogue.budget("minutes", document.budgets)
        took = self.runs.minutes_running(item.run_id)
        if minutes is not None and took > minutes:
            return (
                f"The run has taken {took} minutes; its flow allows {minutes}. "
                "No further task is started."
            )
        tokens = catalogue.budget("tokens", document.budgets)
        if tokens is not None:
            used = self.runs.tokens_used(item.run_id)
            if used > tokens:
                return (
                    f"The run's agents have used {used:,} tokens; its flow allows "
                    f"{tokens:,}. No further task is started."
                )
        return None

    async def _route(self, ctx: Context, done: Done) -> Result:
        item, document, node = ctx.item, ctx.document, ctx.node
        verdict = Verdict(True, done.label, done.detail)
        data = dict(done.data)
        if ctx.task.account and item.provider:
            data["ran"] = {**(item.run_data.get("ran") or {}), node.id: item.provider}
        seen = [*(item.input.get("seen") or [])]
        if node.id not in seen:
            seen.append(node.id)
        each = dict(item.input.get("each") or {})
        round_number = int(item.input.get("round") or 1)
        known = {**item.run_data, **data}

        # Follow the document, passing the tasks that only choose a way.
        target = node.next.get(done.outcome)
        for _hop in range(MAX_HOPS):
            following = document.node(target) if target else None
            if following is None or TASKS[following.task].where != "control":
                break
            if following.task == "control.when":
                condition = following.params.get("condition") or "scope_given"
                holds = scope_given(known) if condition == "scope_given" else False
                target = following.next.get("yes" if holds else "no")
                continue
            # For each entry of a list: the next entry, or on when there is none left.
            entries = known.get(str(following.params.get("over") or "areas")) or []
            limit = min(len(entries), int(following.params.get("max") or catalogue.AREAS_CEILING))
            index = int(each.get(following.id, -1)) + 1
            if index < limit:
                each[following.id] = index
                # An entry is gone through from the start: its rounds are its own.
                seen = [following.id]
                round_number = 1
                target = following.next.get("item")
            else:
                each.pop(following.id, None)
                target = following.next.get("done")
        else:
            return _failed("loop", "The flow went round without reaching a task.", done.output)

        following = document.node(target) if target else None
        if following is None:
            # The flow ends here.
            data["at"] = None
            data["ended"] = {"outcome": done.label, "detail": done.detail}
            return Result(verdict, output=done.output, data=data)
        kind = TASKS[following.task]
        if kind.where == "platform":
            data["at"] = following.id
            if kind.approval and not ctx.dry:
                return Result(verdict, output=done.output, data=data, wait=Approval.WAIT)
            return Result(verdict, output=done.output, data=data)

        # A task in a sandbox follows.
        rounds = max(
            catalogue.budget("rounds", document.budgets) or deliver.MAX_ROUNDS,
            deliver.rounds_of(item.run_data) if item.run_data.get("max_rounds") else 0,
        )
        rounds = min(rounds, deliver.ROUNDS_CEILING)
        again = following.id in seen
        if again:
            # Going back to a task is a round, and rounds are counted.
            if round_number >= rounds:
                return _failed(
                    done.label,
                    f"{done.detail} That was round {round_number}, the last one.",
                    done.output,
                )
            round_number += 1
            # The tasks after it are gone through afresh in the new round.
            seen = seen[: seen.index(following.id)]
        spent = await asyncio.to_thread(self._spent, item, document)
        if spent is not None:
            return _failed("over_budget", spent, done.output)
        given = {**done.given, "round": round_number, "seen": seen}
        if each:
            given["each"] = each
        ran = {**(item.run_data.get("ran") or {}), **(data.get("ran") or {})}
        provider = self._provider(following, ran, again=again)
        return Result(
            verdict,
            output=done.output,
            data=data,
            follow=(Follow(following.id, provider, given),),
        )
