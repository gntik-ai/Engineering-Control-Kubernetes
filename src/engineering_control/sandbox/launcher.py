"""The launcher: moves every unfinished run forward, one sandbox per step.

It is a loop over the runs that are not finished. For each it looks at the run's current
step and does the one thing that step needs next: lease an account and start the pod, look
at how the pod is doing, or read its result and decide what follows. Everything it knows is
in the database and in the cluster, so stopping and starting it loses nothing: the next
pass finds the same pods under the same names.

The launcher is the only part of the platform that may start a pod, and only in the
namespace of the sandboxes.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import ipaddress
import signal
import socket
from datetime import UTC, datetime
from typing import Any, Final, cast
from uuid import UUID

import httpx
import structlog
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from engineering_control.cluster import Cluster, ClusterError
from engineering_control.config import Settings, get_settings
from engineering_control.dashboard.backlog_items import BacklogItems
from engineering_control.engine.agents import Agents
from engineering_control.engine.evaluations import Evaluations
from engineering_control.engine.interpreter import Engine
from engineering_control.engine.records import FlowRecords
from engineering_control.engine.summaries import Summaries
from engineering_control.flow_library import EVALUATION, WORK_KINDS
from engineering_control.gateway.grants import issue as issue_grant
from engineering_control.github_app import GitHubApp, GitHubAppError
from engineering_control.health import Health, listen
from engineering_control.logging import configure_logging
from engineering_control.models import shape
from engineering_control.models.batches import Batches, Followed, plus
from engineering_control.models.sealing import read_sealing_key
from engineering_control.models.service import ModelError, Models, Question, Reply
from engineering_control.platform_metrics import platform_metrics, unavailable
from engineering_control.sandbox import deliver, pods, steps
from engineering_control.sandbox.egress import grant, read_grant_key
from engineering_control.sandbox.flows import AccountCheck, Flow, Follow, Result
from engineering_control.sandbox.queues import Budgets, Queues
from engineering_control.sandbox.runs import CONVERSATION, Lease, Runs, StepWork
from engineering_control.sandbox.triggers import PUSH_LOOK_EVERY, Triggers
from engineering_control.sandbox.workbench import Workbench, WorkbenchError, WorkbenchLost
from engineering_control.supervisor.conversation import Conversation
from engineering_control.supervisor.definition import Definitions
from engineering_control.supervisor.operations import Operations
from engineering_control.supervisor.turn import SupervisorTurn

LOG = structlog.get_logger(__name__)
PASS_SECONDS: Final = 3.0
# A pass looks at every step under way and asks the cluster about each: minutes at most.
# One that has not come back after this long is taken to stand, and the process restarted.
STALLED_SECONDS: Final = 600.0
WORKSPACE_GIBIBYTES: Final = 10
PROXY_LOOKUP_SECONDS: Final = 60.0
WATCH_SECONDS: Final = 20.0
TRIGGERS_EVERY: Final = 60.0
# How much of a running step's output is shown, and how far back the result is looked for.
# A long line reaches the cluster's log in pieces, each counted as a line.
SHOWN_LINES: Final = 600
RESULT_LINES: Final = 20_000
SHOWN_CHARACTERS: Final = 300_000
# How long a pod may wait for its node or its image before the step is given up.
START_SECONDS: Final = 600
# How often a step that asks a model puts its question again when the providers were
# busy or the launcher started anew, before the run gives up. Waiting for an account
# with a key is not counted.
MODEL_TRIES: Final = 5
# The name a step's one question has in its batch, and how often batches that are
# left over are looked for.
ONE: Final = "question"
SWEEP_SECONDS: Final = 300.0
# How often a step is handed to a new workbench when the one it ran in was lost.
BENCH_TRIES: Final = 5
# How many steps that need no sandbox of their own a run may get through in one pass, and
# how long a job of the workbench is waited for before the pass goes on to other runs.
QUICK_STEPS: Final = 12
QUICK_SECONDS: Final = 2.0
# Reasons a container cannot start that waiting does not cure.
_STUCK: Final = {
    "ErrImagePull": "The sandbox image could not be pulled.",
    "ImagePullBackOff": "The sandbox image could not be pulled.",
    "InvalidImageName": "The sandbox image's name is not valid.",
    "CreateContainerConfigError": (
        "The sandbox could not be set up: something it mounts is missing."
    ),
    "CreateContainerError": "The sandbox's container could not be created.",
}


def pod_progress(pod: dict[str, Any]) -> tuple[str, str | None]:
    """Where a pod is: ``starting``, ``running``, ``succeeded`` or ``failed``, with what
    the cluster says about it when that is worth showing."""
    status = pod.get("status") or {}
    phase = status.get("phase")
    if phase == "Succeeded":
        return "succeeded", None
    if phase == "Failed":
        if status.get("reason") == "DeadlineExceeded":
            return "failed", "The step ran longer than it is allowed to."
        return "failed", status.get("message") or status.get("reason")
    if phase == "Running":
        return "running", None
    for container in status.get("containerStatuses") or []:
        waiting = (container.get("state") or {}).get("waiting") or {}
        if waiting.get("reason") in _STUCK:
            return "starting", _STUCK[waiting["reason"]]
    for condition in status.get("conditions") or []:
        if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
            # What the cluster's scheduler says is the only way to tell a full node from
            # one that is away, cordoned or missing its label.
            why = " ".join(str(condition.get("message") or "").split())[:400]
            waiting = "Waiting for a place on the sandbox node."
            if not why:
                return "starting", waiting
            said = f"{waiting} The cluster says: {why}"
            # No node is short of anything and none is cordoned: every node was passed
            # over for its label or a taint, so the sandbox node is not there to be used.
            passed_over = "didn't match Pod's node" in why or "untolerated taint" in why
            if passed_over and not any(
                sign in why for sign in ("Insufficient", "unschedulable", "Too many pods")
            ):
                said += (
                    " That is the sandbox node being away, or having joined the cluster "
                    f"again without its label and taint ({pods.NODE_LABEL}=true): the "
                    'guide\'s section "The sandbox node" has the two commands that set them.'
                )
            return "starting", said
    return "starting", None


class Launcher:
    def __init__(
        self,
        runs: Runs,
        cluster: Cluster,
        sandboxes: pods.Sandboxes,
        github: GitHubApp | None = None,
        *,
        parallel_steps: int = 4,
        conversation: Conversation | None = None,
        tools_url: str | None = None,
        gateway_url: str | None = None,
        queues: Queues | None = None,
        backlog: BacklogItems | None = None,
        grant_key: bytes | None = None,
        models: Models | None = None,
        bench_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # The sandbox a run keeps for its small jobs.
        self.workbench: Workbench | None = Workbench(
            cluster, self._sandboxes, grant_key, transport=bench_transport
        )
        # Who asks a model over a provider's API, and the questions that are out: a step
        # that asks one is not a sandbox, so it is followed here and not in the cluster.
        self.models = models
        # Questions asked in a batch: handed in, followed and fetched from their records.
        self.batches: Batches | None = Batches(models) if models is not None else None
        self._swept = 0.0
        self._calls: dict[UUID, asyncio.Task[Reply]] = {}
        self._questions: dict[UUID, Question] = {}
        # What a step's grant to reach a project's own application is signed with.
        self.grant_key = grant_key
        self.gateway_url = gateway_url
        self.runs = runs
        self.cluster = cluster
        self.sandboxes = sandboxes
        self.parallel_steps = parallel_steps
        self._running = 0
        self._addresses: dict[str, tuple[str, float]] = {}
        self.conversation = conversation
        self.queues = queues
        # One engine runs every flow: the kind of a run only says what it is recorded as.
        sessions = getattr(runs, "sessions", None)
        self.evaluations = Evaluations(sessions, runs) if sessions else None
        # Triggers start what queues' budgets allow; without queues there are none.
        self.triggers = Triggers(sessions, runs, queues.budgets) if sessions and queues else None
        self.github = github
        self._triggered = self._looked = float("-inf")
        engine = Engine(
            runs,
            github,
            backlog,
            FlowRecords(sessions).document if sessions else None,
            Agents(sessions) if sessions else None,
            Summaries(sessions) if sessions else None,
        )
        self.flows: dict[str, Flow] = {
            "account_check": AccountCheck(),
            **{kind: engine for kind in (*WORK_KINDS, EVALUATION)},
        }
        # The operations role: what went wrong is noticed here, on every few passes.
        self.operations = Operations(runs.sessions, conversation) if conversation else None
        self._watched = 0.0
        if conversation is not None:
            self.flows[CONVERSATION] = SupervisorTurn(
                runs,
                conversation,
                conversation.definitions,
                lambda: self._address(tools_url),
                self.operations,
            )

    async def tick(self) -> int:
        """One pass over every unfinished run. Returns how many it looked at. A run whose
        step fails here is retried on the next pass; it never stops the others."""
        if self.conversation is not None:
            # Turns that ended without a reply are said so; messages that waited get theirs.
            await asyncio.to_thread(self.conversation.settle)
        if self.queues is not None:
            # Queues follow the runs of their items and start the next ones.
            await asyncio.to_thread(self.queues.advance)
        if self.operations is not None:
            await self._operate()
        if self.evaluations is not None:
            # Evaluations go through their cases one after the other.
            await asyncio.to_thread(self.evaluations.advance)
        if self.triggers is not None:
            await self._trigger()
        if self.batches is not None:
            # Batches nobody waits for any more are brought to an end, now and then.
            now = asyncio.get_running_loop().time()
            if now - self._swept > SWEEP_SECONDS:
                self._swept = now
                try:
                    await self.batches.sweep()
                except Exception as error:  # noqa: BLE001 - never holds the runs up
                    LOG.warning("batches_not_swept", error=type(error).__name__)
        work = await asyncio.to_thread(self.runs.work)
        # Steps already in a sandbox count against the limit; a step that has to wait for
        # a free one says so and is looked at again on the next pass, oldest run first.
        self._running = sum(
            1
            for item in work
            if item.state == "running"
            and item.pod_name
            and not item.pod_name.startswith(pods.BENCH_PREFIX)
        )
        for first in work:
            item: StepWork | None = first
            # A step that needs no sandbox of its own (the workbench's, a model's) ends
            # within the pass, and the run's next step is taken up at once: several such
            # steps go by in one pass instead of one every few seconds.
            for _quick in range(QUICK_STEPS):
                if item is None:
                    break
                try:
                    quick = await self._advance(item)
                except (ClusterError, GitHubAppError, OSError) as error:
                    # Something outside refused or did not answer: say so and try again.
                    LOG.warning("launcher_outside_error", run=str(item.run_id), error=str(error))
                    await asyncio.to_thread(self.runs.step_waiting, item.step_id, str(error))
                    break
                except Exception as error:  # noqa: BLE001 - one run must not stop the loop
                    LOG.error("launcher_step_error", run=str(item.run_id), error=repr(error)[:300])
                    break
                if not quick:
                    break
                item = await asyncio.to_thread(self.runs.work_of, item.run_id)
        if self.workbench is not None:
            with contextlib.suppress(ClusterError):
                await self.workbench.tidy()
        return len(work)

    async def _trigger(self) -> None:
        """Start what schedules and arriving items start, once a minute, and look for
        moved branches every few minutes. Neither needs looking at every three seconds."""
        if self.triggers is None:
            return
        now = asyncio.get_running_loop().time()
        if now - self._triggered >= TRIGGERS_EVERY:
            self._triggered = now
            await asyncio.to_thread(self.triggers.advance)
        if self.github is not None and now - self._looked >= PUSH_LOOK_EVERY.total_seconds():
            self._looked = now
            try:
                await self.triggers.look(self.github)
            except GitHubAppError as error:
                LOG.warning("triggers_look_failed", error=str(error)[:200])

    async def _operate(self, *, force: bool = False) -> None:
        """Notice trouble, close what the supervisor has dealt with, and ask it about what
        is new. Trouble does not need looking for every three seconds."""
        if self.operations is None:
            return
        now = asyncio.get_running_loop().time()
        if not force and now - self._watched < WATCH_SECONDS:
            return
        self._watched = now
        operations = self.operations

        def look() -> None:
            operations.watch()
            operations.settle()
            operations.dispatch()

        await asyncio.to_thread(look)

    async def _advance(self, item: StepWork) -> bool:
        """Move a run's step forward. True when the step ended in this pass without a
        sandbox of its own: the run's next step can be taken up at once."""
        flow = self.flows.get(item.run_kind)
        if flow is None:
            await self._finish(
                item, state="failed", outcome="unknown", detail=f"No flow for {item.run_kind}."
            )
        elif item.cancel_requested:
            await self._cancel(item)
        elif item.state == "done":
            end = await flow.done(item)
            if end.data:
                await asyncio.to_thread(self.runs.merge_data, item.run_id, end.data)
            if end.again:
                # The platform waits for something outside; it is asked again next pass.
                if end.data:
                    await asyncio.to_thread(
                        lambda: self.runs.run_progress(
                            item.run_id, outcome=end.outcome, detail=end.detail
                        )
                    )
            elif end.wait is not None:
                outcome, detail = end.wait
                await asyncio.to_thread(
                    lambda: self.runs.run_waiting(item.run_id, outcome=outcome, detail=detail)
                )
            else:
                await self._finish(item, state=end.state, outcome=end.outcome, detail=end.detail)
        elif item.state == "pending":
            where = self._where(flow, item)
            if where == "model":
                await self._ask(item)
            elif where == "bench":
                return await self._bench(item)
            else:
                await self._start(item)
        elif item.pod_name is None and self._where(flow, item) == "model":
            return await self._asked(item)
        elif item.pod_name is not None and item.pod_name.startswith(pods.BENCH_PREFIX):
            return await self._bench_watch(item)
        else:
            await self._watch(item)
        return False

    @staticmethod
    def _where(flow: Flow, item: StepWork) -> str:
        where = getattr(flow, "where", None)
        return str(where(item)) if where is not None else "sandbox"

    # --- the workbench -------------------------------------------------------------------

    async def _bench(self, item: StepWork) -> bool:
        """Hand a step's job to the run's workbench, starting the workbench when the
        run has none. A job that ends at once is concluded in the same pass."""
        if self.workbench is None:
            await self._fail_step(item, "no_workbench", "This installation has no workbench.")
            return False
        if item.attempt >= BENCH_TRIES:
            await self._fail_step(
                item,
                "workbench_lost",
                f"The workbench was lost {BENCH_TRIES} times over this step.",
            )
            return False
        try:
            address, said = await self.workbench.ready(item.run_id)
        except WorkbenchError as error:
            await self._fail_step(item, "no_workbench", str(error))
            return False
        if address is None:
            await asyncio.to_thread(self.runs.step_waiting, item.step_id, said)
            return False
        try:
            plan = await self.flows[item.run_kind].plan(item, None)
        except ValueError as error:
            await self._fail_step(item, "not_started", str(error))
            return False
        try:
            taken = await self.workbench.hand(address, item.run_id, str(item.step_id), plan.job)
        except WorkbenchLost as error:
            await asyncio.to_thread(self.runs.step_waiting, item.step_id, f"Waiting: {error}.")
            return False
        except WorkbenchError as error:
            await self._fail_step(item, "no_workbench", str(error))
            return False
        if not taken:
            await asyncio.to_thread(
                self.runs.step_waiting, item.step_id, "The workbench is busy with another job."
            )
            return False
        await asyncio.to_thread(self.runs.step_started, item.step_id, pods.bench_name(item.run_id))
        return await self._bench_result(item, address, wait=QUICK_SECONDS)

    async def _bench_watch(self, item: StepWork) -> bool:
        if self.workbench is None:
            await self._fail_step(item, "no_workbench", "This installation has no workbench.")
            return False
        try:
            address, _said = await self.workbench.ready(item.run_id)
        except WorkbenchError as error:
            await self._fail_step(item, "no_workbench", str(error))
            return False
        if address is None:
            # The workbench is gone, and what it was doing with it: the step is run again
            # in the one that is started now.
            await asyncio.to_thread(
                lambda: self.runs.step_again(
                    item.step_id, "The workbench was lost; the step is run again.", forgiven=False
                )
            )
            return False
        return await self._bench_result(item, address, wait=1.0)

    async def _bench_result(self, item: StepWork, address: str, *, wait: float) -> bool:
        try:
            log = await self.workbench.result(  # type: ignore[union-attr]
                address, item.run_id, str(item.step_id), wait=wait
            )
        except WorkbenchLost:
            await asyncio.to_thread(
                lambda: self.runs.step_again(
                    item.step_id, "The workbench was lost; the step is run again.", forgiven=False
                )
            )
            return False
        except WorkbenchError as error:
            await self._fail_step(item, "no_workbench", str(error))
            return False
        if log is None:
            return False
        await self._conclude(item, log, None)
        return True

    # --- asking a model ------------------------------------------------------------------

    async def _ask(self, item: StepWork) -> None:
        """Put the step's question to a model. The answer is waited for in the
        background: the other runs go on meanwhile."""
        if self.models is None:
            await self._fail_step(item, "no_models", "This installation cannot ask a model.")
            return
        if item.attempt >= MODEL_TRIES:
            await self._fail_step(
                item,
                "model_unavailable",
                f"The model could not be asked in {MODEL_TRIES} tries: its providers "
                "were busy or did not answer each time.",
            )
            return
        engine = cast(Engine, self.flows[item.run_kind])
        try:
            question = await engine.question(item)
        except ValueError as error:
            await self._fail_step(item, "not_asked", str(error))
            return
        if question.batch and self.batches is not None:
            await asyncio.to_thread(self.runs.step_started, item.step_id, None)
            try:
                await self.batches.submit(
                    item.step_id, item.run_id, [(ONE, question)], hours=question.hours
                )
            except ModelError as error:
                await self._not_asked(item, error)
                return
            self._questions[item.step_id] = question
            LOG.info("model_asked_in_a_batch", run=str(item.run_id), step=item.name)
            return
        self._at_once(item, question)
        await asyncio.to_thread(self.runs.step_started, item.step_id, None)
        LOG.info("model_asked", run=str(item.run_id), step=item.name)

    def _at_once(self, item: StepWork, question: Question, earlier: str | None = None) -> None:
        """Put the question to a model now. Whatever happens to the provider, the step
        does not wait for ever."""
        assert self.models is not None  # noqa: S101 - a step that asks has them
        limit = question.seconds * 4 + 300
        self._calls[item.step_id] = asyncio.create_task(
            asyncio.wait_for(self.models.ask(question, earlier), timeout=limit)
        )
        self._questions[item.step_id] = question

    async def _not_asked(self, item: StepWork, error: ModelError) -> None:
        if error.later:
            detail, forgiven = error.detail, error.label == "no_account"
            await asyncio.to_thread(
                lambda: self.runs.step_again(item.step_id, detail, forgiven=forgiven)
            )
        else:
            await self._fail_step(item, error.label, error.detail)

    async def _batched(self, item: StepWork, followed: Followed) -> bool:
        """Go on from where the step's batch is: wait, or take what it answered."""
        if followed.state == "waiting":
            await asyncio.to_thread(self.runs.step_waiting, item.step_id, followed.detail)
            return False
        if followed.state == "failed" or not followed.answers:
            await self._fail_step(item, "batch_failed", followed.detail or "The batch failed.")
            return False
        answer = followed.answers[0]
        engine = cast(Engine, self.flows[item.run_kind])
        try:
            # The question as it was put: what the step's log shows, and what is asked
            # at once when the batch brought nothing that can be used.
            question = await engine.question(item)
        except ValueError as error:
            await self._fail_step(item, "not_asked", str(error))
            return False
        if answer.state == "answered":
            data = shape.read_object(answer.text)
            why = (
                "it was not a JSON object"
                if data is None
                else shape.problem(question.schema, data) or ""
            )
            if data is not None and not why:
                reply = Reply(
                    data=data,
                    text=answer.text,
                    provider=followed.provider,
                    model=answer.model,
                    account_id=followed.account_id or item.step_id,
                    account=followed.account,
                    usage=answer.usage or {},
                    seconds=followed.seconds,
                    asked=1,
                )
                await self._answered(item, reply, question, batch=True)
                return True
            # An answer of the wrong shape is asked for again at once, not in another
            # batch: one more question, saying what was wrong.
            self._at_once(item, question, why)
            return False
        if answer.state == "late" or answer.kind == "busy":
            if question.late == "ask":
                self._at_once(item, question)
                return False
            await self._fail_step(
                item,
                "batch_late" if answer.state == "late" else "batch_failed",
                (
                    f"The batch was not answered within {question.hours} hours. What was "
                    "not answered is not charged. Start the run again, or set the task "
                    "to be asked at once when its batch is late."
                    if answer.state == "late"
                    else f"The provider could not answer in the batch: {answer.error or ''}"
                ),
            )
            return False
        await self._fail_step(
            item, answer.kind or "batch_failed", answer.error or "The batch gave no answer."
        )
        return False

    async def _asked(self, item: StepWork) -> bool:
        call = self._calls.get(item.step_id)
        if call is None and self.batches is not None:
            followed = await self.batches.follow(item.step_id)
            if followed is not None:
                return await self._batched(item, followed)
        if call is None:
            # The launcher started again while the model was being asked: ask again.
            await asyncio.to_thread(
                lambda: self.runs.step_again(
                    item.step_id, "The question is put again.", forgiven=False
                )
            )
            return False
        if not call.done():
            waited = (datetime.now(UTC) - _aware(item.started_at or datetime.now(UTC))).seconds
            await asyncio.to_thread(
                self.runs.step_waiting,
                item.step_id,
                f"The model is being asked ({waited // 15 * 15} seconds so far).",
            )
            return False
        del self._calls[item.step_id]
        question = self._questions.pop(item.step_id, None)
        try:
            reply = call.result()
        except ModelError as error:
            await self._not_asked(item, error)
            return False
        except TimeoutError:
            await self._fail_step(item, "model_timeout", "The model did not answer in time.")
            return False
        except asyncio.CancelledError:
            await self._fail_step(item, "cancelled", "The question was withdrawn.")
            return False
        await self._answered(item, reply, question)
        return True

    async def _answered(
        self, item: StepWork, reply: Reply, question: Question | None, *, batch: bool = False
    ) -> None:
        """Keep what the question used and go on as the answer says."""
        used = reply.usage
        if self.batches is not None and not batch:
            # A question that was first asked in a batch, and then at once: both count.
            earlier = await asyncio.to_thread(self.batches.used, item.step_id)
            used = plus(earlier, reply.usage) or reply.usage
        await asyncio.to_thread(self.runs.note_usage, item.step_id, used)
        result = await cast(Engine, self.flows[item.run_kind]).answered(item, reply)
        how = (
            f"in a batch; answered after {_span(reply.seconds)}"
            if batch
            else f"{'twice; ' if reply.asked > 1 else ''}answered in {reply.seconds} seconds"
        )
        said = (
            f"--- asked {reply.provider} ({reply.model}) with the account {reply.account}, "
            f"{how} ---\n"
            f"{question.system if question else ''}\n\n{question.prompt if question else ''}"
            f"\n\n--- answered ---\n{reply.text}"
        )
        self._questions.pop(item.step_id, None)
        await self._settle(item, result, result.verdict, log=said, exit_code=None)
        LOG.info("model_answered", run=str(item.run_id), step=item.name, model=reply.model)

    # --- starting ------------------------------------------------------------------------

    async def _address(self, url: str | None) -> str | None:
        """A service of the platform as http://address:port. A sandbox has no name server,
        so the launcher looks the name up for it; the address of a service stays the same
        for as long as the service exists."""
        if url is None:
            return None
        now = asyncio.get_running_loop().time()
        known = self._addresses.get(url)
        if known is None or now - known[1] > PROXY_LOOKUP_SECONDS:
            host, _, port = url.removeprefix("http://").rpartition(":")
            try:
                ipaddress.ip_address(host)
                address = host
            except ValueError:
                found = await asyncio.get_running_loop().getaddrinfo(
                    host, int(port), family=socket.AF_INET, type=socket.SOCK_STREAM
                )
                address = str(found[0][4][0])
            known = (f"http://{address}:{port}", now)
            self._addresses[url] = known
        return known[0]

    async def _sandboxes(self) -> pods.Sandboxes:
        """The sandboxes' settings with the proxy as an address."""
        if self.sandboxes.proxy is None:
            return self.sandboxes
        return dataclasses.replace(self.sandboxes, proxy=await self._address(self.sandboxes.proxy))

    async def _start(self, item: StepWork) -> None:
        if self._running >= self.parallel_steps:
            await asyncio.to_thread(
                self.runs.step_waiting,
                item.step_id,
                f"Waiting for a free sandbox: {self.parallel_steps} steps run at the same time.",
            )
            return
        lease: Lease | None = None
        if item.provider is not None:
            lease = await asyncio.to_thread(self.runs.lease, item.step_id)
            if lease is None:
                by_key = (item.input or {}).get("paid") == "api"
                await asyncio.to_thread(
                    self.runs.step_waiting,
                    item.step_id,
                    "Waiting for an account with an API key of the client's own provider "
                    "(Anthropic's for Claude, OpenAI's for ChatGPT) that is ready and free."
                    if by_key
                    else "Waiting for an account that is signed in and free.",
                )
                return
        account = None
        if lease is not None and lease.api is not None:
            # An account with an API key has no sign-in to mount. The client is told
            # where the gateway is and given a grant for this step; the key stays here.
            address = await self._address(self.gateway_url)
            if address is None or self.grant_key is None:
                await self._fail_step(
                    item,
                    "not_started",
                    "This installation has no gateway for clients that are paid by an API "
                    "key: set the task to be paid by the subscription.",
                )
                return
            lease = dataclasses.replace(
                lease,
                gateway={
                    "url": address,
                    "grant": issue_grant(
                        self.grant_key, account_id=lease.account_id, step_id=item.step_id
                    ),
                },
            )
        elif lease is not None:
            account = pods.AccountMount(lease.account_id, lease.keeps_home)
            if account.keeps_home:
                await self.cluster.create_claim(
                    pods.claim(
                        account.home_claim,
                        namespace=self.sandboxes.namespace,
                        gibibytes=1,
                        labels={"engineering-control/account": str(lease.account_id)},
                    )
                )
        try:
            plan = await self.flows[item.run_kind].plan(item, lease)
        except ValueError as error:
            # What the step needs is not there (the agent it names, say): the run says
            # so and ends, instead of being tried again for ever.
            await self._fail_step(item, "not_started", str(error))
            return
        workspace = None
        if plan.workspace:
            # The run's own volume, made with its first step and kept until the run ends.
            workspace = pods.workspace_claim(item.run_id)
            await self.cluster.create_claim(
                pods.claim(
                    workspace,
                    namespace=self.sandboxes.namespace,
                    gibibytes=WORKSPACE_GIBIBYTES,
                    labels={"engineering-control/run": str(item.run_id)},
                )
            )
        manifest = pods.step_pod(
            await self._sandboxes(),
            run_id=item.run_id,
            step=item.name,
            position=item.position,
            attempt=item.attempt + 1,
            job=plan.job,
            egress=plan.egress,
            account=account,
            workspace=workspace,
            cpu=plan.cpu,
            memory=plan.memory,
            deadline_seconds=plan.deadline_seconds,
            labels=plan.labels,
            # A step that looks at the project's application may reach the hosts the
            # project names for it, for as long as the step may last, and not a host more.
            browser_grant=grant(
                self.grant_key, plan.application_hosts, seconds=plan.deadline_seconds + 300
            )
            if plan.application_hosts and self.grant_key
            else None,
            identity_secret=plan.identity_secret,
        )
        await self.cluster.create_pod(manifest)
        await asyncio.to_thread(self.runs.step_started, item.step_id, manifest["metadata"]["name"])
        self._running += 1
        LOG.info("sandbox_started", run=str(item.run_id), step=item.name)

    # --- watching ------------------------------------------------------------------------

    async def _watch(self, item: StepWork) -> None:
        if item.pod_name is None:
            await self._fail_step(item, "lost", "The step has no sandbox.")
            return
        pod = await self.cluster.pod(item.pod_name)
        if pod is None:
            await self._fail_step(item, "lost", "The sandbox disappeared before it finished.")
            return
        progress, said = pod_progress(pod)
        if progress == "starting":
            waited = (datetime.now(UTC) - _aware(item.started_at or datetime.now(UTC))).seconds
            if waited > START_SECONDS:
                await self.cluster.delete_pod(item.pod_name)
                await self._fail_step(
                    item, "not_started", said or "The sandbox did not start in time."
                )
            elif said:
                await asyncio.to_thread(self.runs.step_waiting, item.step_id, said)
            return
        if progress == "running":
            log = await self.cluster.pod_log(item.pod_name, tail_lines=SHOWN_LINES)
            await asyncio.to_thread(self.runs.step_log, item.step_id, log)
            return
        log = await self.cluster.pod_log(item.pod_name, tail_lines=RESULT_LINES)
        await self._conclude(item, log, said)
        await self.cluster.delete_pod(item.pod_name)
        await self.flows[item.run_kind].ended(item)

    async def _conclude(self, item: StepWork, log: str, said: str | None) -> None:
        commands = steps.parse_result(log)
        if item.provider and commands:
            used = deliver.agent_usage(item.provider, commands)
            if used is not None:
                # What the client worked its conversation out to cost is a price only
                # for an account that is paid by the token.
                client_cost = used.pop("client_cost", None)
                used = await asyncio.to_thread(
                    self.runs.paid_by_key, item.step_id, used, client_cost
                )
                await asyncio.to_thread(self.runs.note_usage, item.step_id, used)
        result = await self.flows[item.run_kind].conclude(item, commands, log)
        verdict = result.verdict
        if commands is None and said:
            verdict = steps.Verdict(False, "failed", said)
        last = commands[-1] if commands else {}
        await self._settle(
            item,
            result,
            verdict,
            log=log,
            exit_code=last.get("exit_code") if isinstance(last.get("exit_code"), int) else None,
        )
        LOG.info("sandbox_finished", run=str(item.run_id), step=item.name, outcome=verdict.outcome)

    async def _settle(
        self,
        item: StepWork,
        result: Result,
        verdict: steps.Verdict,
        *,
        log: str,
        exit_code: int | None,
    ) -> None:
        """Record how a step ended and go on as its result says: the next step, the
        administrator's decision, or the end of the run."""
        await asyncio.to_thread(
            lambda: self.runs.step_finished(
                item.step_id,
                state="succeeded" if verdict.succeeded else "failed",
                outcome=verdict.outcome,
                detail=verdict.detail,
                exit_code=exit_code,
                output=result.output,
                log=log[-SHOWN_CHARACTERS:],
            )
        )
        if verdict.account_state and item.account_id is not None:
            await asyncio.to_thread(
                lambda: self.runs.account_learned(
                    item.account_id,  # type: ignore[arg-type]
                    state=verdict.account_state,  # type: ignore[arg-type]
                    reason=verdict.account_reason,
                    limited_until=verdict.limited_until,
                )
            )
        if result.data:
            await asyncio.to_thread(self.runs.merge_data, item.run_id, result.data)
        if not verdict.succeeded:
            await self._finish(item, state="failed", outcome=verdict.outcome, detail=verdict.detail)
        elif result.wait is not None:
            outcome, detail = result.wait
            await asyncio.to_thread(
                lambda: self.runs.run_waiting(item.run_id, outcome=outcome, detail=detail)
            )
            # What the run made is in the database now; its volume is no longer needed.
            await self._release_workspace(item)
        else:
            for follow in result.follow:
                await asyncio.to_thread(self._add, item, follow)

    def _add(self, item: StepWork, follow: Follow) -> None:
        self.runs.add_step(
            item.run_id, name=follow.name, provider=follow.provider, given=follow.given
        )

    async def _fail_step(self, item: StepWork, outcome: str, detail: str) -> None:
        await asyncio.to_thread(
            lambda: self.runs.step_finished(
                item.step_id, state="failed", outcome=outcome, detail=detail
            )
        )
        await self._finish(item, state="failed", outcome=outcome, detail=detail)

    async def _cancel(self, item: StepWork) -> None:
        call = self._calls.pop(item.step_id, None)
        self._questions.pop(item.step_id, None)
        if call is not None:
            # A question that is out is withdrawn: its answer is not waited for.
            call.cancel()
        elif self.batches is not None and item.state == "running":
            # A batch the step waits for is stopped at the provider.
            await self.batches.stop(item.step_id)
        if item.state == "running" and item.pod_name is not None:
            await self.cluster.delete_pod(item.pod_name, grace_seconds=0)
            await self.flows[item.run_kind].ended(item)
        await self._finish(
            item, state="cancelled", outcome="cancelled", detail="Stopped by the administrator."
        )

    async def _finish(
        self, item: StepWork, *, state: str, outcome: str, detail: str | None
    ) -> None:
        await asyncio.to_thread(
            lambda: self.runs.run_finished(item.run_id, state=state, outcome=outcome, detail=detail)
        )
        await self._release_workspace(item)

    async def _release_workspace(self, item: StepWork) -> None:
        """A run that has ended, or waits with its change collected, keeps nothing in the
        cluster. One that never had a volume has none to delete, which is fine; one whose
        volume cannot be deleted right now is not held up by it."""
        if self.workbench is not None:
            # The workbench first: a volume a pod still has is not let go.
            with contextlib.suppress(ClusterError):
                await self.workbench.release(item.run_id)
        try:
            await self.cluster.delete_claim(pods.workspace_claim(item.run_id))
        except ClusterError as error:
            LOG.warning("workspace_not_released", run=str(item.run_id), error=str(error))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _span(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 2:
        return f"{int(seconds)} seconds"
    if minutes < 120:
        return f"{minutes} minutes"
    return f"{minutes // 60} hours and {minutes % 60} minutes"


def sandboxes_from(settings: Settings) -> pods.Sandboxes:
    if settings.sandbox_namespace is None or settings.sandbox_image is None:
        raise ValueError("the launcher needs the sandbox namespace and the sandbox image")
    return pods.Sandboxes(
        namespace=settings.sandbox_namespace,
        image=settings.sandbox_image,
        pull_secret=settings.sandbox_pull_secret,
        proxy=settings.sandbox_egress_proxy,
    )


async def serve(settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    configure_logging(resolved.log_level)
    sandboxes = sandboxes_from(resolved)
    cluster = Cluster.from_settings(resolved)
    if cluster is None:
        raise ValueError("the launcher needs the sandbox namespace")
    database_url = resolved.database_url.get_secret_value()
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=3,
        max_overflow=2,
        connect_args={"connect_timeout": 5} if database_url.startswith("postgresql") else {},
    )
    github = GitHubApp.from_settings(resolved)
    sessions = sessionmaker(engine, expire_on_commit=False)
    runs = Runs(sessions)
    launcher = Launcher(
        runs,
        cluster,
        sandboxes,
        github,
        parallel_steps=resolved.sandbox_parallel_steps,
        conversation=Conversation(sessions, Definitions(sessions)),
        tools_url=resolved.supervisor_tools_url,
        gateway_url=resolved.gateway_url,
        queues=Queues(sessions, runs, Budgets(sessions)),
        backlog=BacklogItems(sessions),
        grant_key=read_grant_key(resolved.sandbox_egress_grant_key_file),
        models=Models(sessions, read_sealing_key(resolved.model_keys_key_file)),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stop.set)
    health = Health("launcher")

    async def state_of_the_work() -> list[str]:
        try:
            return await asyncio.to_thread(platform_metrics, sessions)
        except Exception:  # noqa: BLE001 - said as a number: the database does not answer
            return unavailable()

    told = await listen(health, resolved.health_host, resolved.health_port, extra=state_of_the_work)
    # A pass that fails (the database away for a moment) still turns the loop: a restart
    # would not bring the database back. A pass that never returns does not.
    health.expect("pass", within=STALLED_SECONDS)
    LOG.info("launcher_started", namespace=sandboxes.namespace, image=sandboxes.image)
    try:
        while not stop.is_set():
            try:
                await launcher.tick()
            except Exception as error:  # noqa: BLE001 - the database may be away for a moment
                LOG.error("launcher_pass_failed", error=repr(error)[:300])
                health.count("launcher_pass_failed")
            health.beat("pass")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=PASS_SECONDS)
    finally:
        told.close()
        await cluster.aclose()
        if launcher.models is not None:
            await launcher.models.aclose()
        if launcher.workbench is not None:
            await launcher.workbench.aclose()
        if github is not None:
            await github.aclose()
        engine.dispose()


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
