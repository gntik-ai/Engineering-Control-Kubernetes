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
from engineering_control.engine import catalogue, conditions, routing
from engineering_control.engine.builtin import BUILTIN, FLOW_OF_KIND
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.document import Document, FlowInvalid, Node, parse
from engineering_control.engine.tasks import (
    PLATFORM,
    SANDBOX,
    Again,
    Approval,
    Context,
    Done,
    Failed,
    Services,
    merges_after,
)
from engineering_control.github_app import GitHubApp
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
MAX_HOPS: Final = routing.MAX_HOPS
Documents = Callable[[str, int], Document | None]


def _failed(outcome: str, detail: str, output: dict[str, Any] | None = None) -> Result:
    return Result(Verdict(False, outcome, detail), output=output)


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
        carried = item.run_data.get("flow_document")
        if carried:
            # A run of the evaluation lab carries the flow it tries.
            try:
                return parse(carried)
            except FlowInvalid as error:
                raise ValueError(f"the flow the run carries cannot be read: {error}") from error
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
        if ctx.model_used is not None:
            noted["model"] = ctx.model_used
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
        if isinstance(concluded, Done):
            # How the task ended, in the flow's own words: what a simulation of a changed
            # flow goes by when it is asked how this run would have gone on.
            await asyncio.to_thread(self.runs.note_step, item.step_id, {"went": concluded.outcome})
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
            if ctx.dry and (ctx.task.publishes or ctx.task.approval):
                # Nothing leaves a run of the evaluation lab, whatever its flow says.
                last = End(
                    "succeeded",
                    "evaluated",
                    "The run came to where the administrator would decide. Nothing is "
                    "published in an evaluation.",
                )
                break
            ctx.item.run_data.update(gathered)
            performed = await PLATFORM[node.task].perform(ctx)
            gathered.update(performed.data)
            last = performed.end
            if last is not None:
                # What a task leaves is there for the ones after it: the pull request
                # for the checks, the checks for the merge.
                gathered.update(last.data)
            if performed.outcome is None:
                break
            target = node.next.get(performed.outcome)
            if not target:
                break
            if ctx.task.publishes:
                # What reached GitHub is not done twice: from here the run goes on at the
                # task after it, also when it is asked again later.
                gathered["at"] = target
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
                again=last.again,
            )
        return last

    async def ended(self, item: StepWork) -> None:
        await self.services.checkouts.ended(item)

    # --- where a task's outcome leads ----------------------------------------------------

    @staticmethod
    def _provider(node: Node, ran: dict[str, Any], *, again: bool) -> str | None:
        return routing.provider_for(node, ran, again=again)

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
            data["ran"] = routing.ran_on(item.run_data.get("ran") or {}, node, item.provider)
        round_number = int(item.input.get("round") or 1)
        to = routing.follow(
            document,
            node,
            done.outcome,
            seen=[*(item.input.get("seen") or [])],
            each=dict(item.input.get("each") or {}),
            round_number=round_number,
            known={**item.run_data, **data},
            rounds=routing.rounds_allowed(document, item.run_data),
        )
        # How each task ended so far: what an If further on may ask about.
        data["went"] = to.went(item.run_data.get("went"), node.id, done.outcome)
        if to.passed:
            # Which way each If went, and what it asked: shown with the step it followed.
            chose = [
                {
                    "task": passed,
                    "way": way,
                    "asked": conditions.words(chooser.params.get("condition")),
                }
                for passed, way in to.passed
                if (chooser := document.node(passed)) is not None and chooser.task == "control.when"
            ]
            if chose:
                await asyncio.to_thread(self.runs.note_step, item.step_id, {"chose": chose})
        if to.kind == "loop":
            return _failed("loop", "The flow went round without reaching a task.", done.output)
        if to.kind == "end":
            # The flow ends here.
            data["at"] = None
            data["ended"] = {"outcome": done.label, "detail": done.detail}
            return Result(verdict, output=done.output, data=data)
        following = document.node(str(to.target))
        if following is None:  # ``follow`` names only tasks the document has
            return _failed("unknown", f"The flow has no task {to.target}.", done.output)
        if to.kind == "platform":
            data["at"] = following.id
            # Whether deciding to publish is deciding to merge as well: said with the
            # decision, wherever it is asked for.
            data["merges"] = merges_after(document, following)
            if TASKS[following.task].approval and not ctx.dry:
                return Result(
                    verdict, output=done.output, data=data, wait=Approval.wait(document, following)
                )
            return Result(verdict, output=done.output, data=data)
        if to.kind == "last_round":
            return _failed(
                done.label,
                f"{done.detail} That was round {round_number}, the last one.",
                done.output,
            )
        # A task in a sandbox follows.
        spent = await asyncio.to_thread(self._spent, item, document)
        if spent is not None:
            return _failed("over_budget", spent, done.output)
        given = {**done.given, "round": to.round, "seen": list(to.seen)}
        if to.each:
            given["each"] = to.each
        ran = {**(item.run_data.get("ran") or {}), **(data.get("ran") or {})}
        provider = self._provider(following, ran, again=to.again)
        return Result(
            verdict,
            output=done.output,
            data=data,
            follow=(Follow(following.id, provider, given),),
        )
