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
import signal
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from engineering_control.cluster import Cluster, ClusterError
from engineering_control.config import Settings, get_settings
from engineering_control.github_app import GitHubApp, GitHubAppError
from engineering_control.logging import configure_logging
from engineering_control.sandbox import pods, steps
from engineering_control.sandbox.flows import AccountCheck, DeliverIssue, Flow, Follow
from engineering_control.sandbox.runs import Lease, Runs, StepWork

LOG = structlog.get_logger(__name__)
PASS_SECONDS: Final = 3.0
WORKSPACE_GIBIBYTES: Final = 10
# How much of a running step's output is shown, and how far back the result is looked for.
# A long line reaches the cluster's log in pieces, each counted as a line.
SHOWN_LINES: Final = 600
RESULT_LINES: Final = 20_000
SHOWN_CHARACTERS: Final = 300_000
# How long a pod may wait for its node or its image before the step is given up.
START_SECONDS: Final = 600
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
            return "starting", "Waiting for room on the sandbox node."
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
    ) -> None:
        self.runs = runs
        self.cluster = cluster
        self.sandboxes = sandboxes
        self.parallel_steps = parallel_steps
        self._running = 0
        self.flows: dict[str, Flow] = {
            "account_check": AccountCheck(),
            "deliver_issue": DeliverIssue(runs, github),
        }

    async def tick(self) -> int:
        """One pass over every unfinished run. Returns how many it looked at. A run whose
        step fails here is retried on the next pass; it never stops the others."""
        work = await asyncio.to_thread(self.runs.work)
        # Steps already in a sandbox count against the limit; a step that has to wait for
        # a free one says so and is looked at again on the next pass, oldest run first.
        self._running = sum(1 for item in work if item.state == "running")
        for item in work:
            try:
                await self._advance(item)
            except (ClusterError, GitHubAppError) as error:
                # Something outside refused or did not answer: say so and try again.
                LOG.warning("launcher_outside_error", run=str(item.run_id), error=str(error))
                await asyncio.to_thread(self.runs.step_waiting, item.step_id, str(error))
            except Exception as error:  # noqa: BLE001 - one run must not stop the loop
                LOG.error("launcher_step_error", run=str(item.run_id), error=repr(error)[:300])
        return len(work)

    async def _advance(self, item: StepWork) -> None:
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
            if end.wait is not None:
                outcome, detail = end.wait
                await asyncio.to_thread(
                    lambda: self.runs.run_waiting(item.run_id, outcome=outcome, detail=detail)
                )
            else:
                await self._finish(item, state=end.state, outcome=end.outcome, detail=end.detail)
        elif item.state == "pending":
            await self._start(item)
        else:
            await self._watch(item)

    # --- starting ------------------------------------------------------------------------

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
                await asyncio.to_thread(
                    self.runs.step_waiting,
                    item.step_id,
                    "Waiting for an account that is signed in and free.",
                )
                return
        account = None
        if lease is not None:
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
        plan = await self.flows[item.run_kind].plan(item, lease)
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
            self.sandboxes,
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
        result = await self.flows[item.run_kind].conclude(item, commands, log)
        verdict = result.verdict
        if commands is None and said:
            verdict = steps.Verdict(False, "failed", said)
        last = commands[-1] if commands else {}
        await asyncio.to_thread(
            lambda: self.runs.step_finished(
                item.step_id,
                state="succeeded" if verdict.succeeded else "failed",
                outcome=verdict.outcome,
                detail=verdict.detail,
                exit_code=last.get("exit_code") if isinstance(last.get("exit_code"), int) else None,
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
        LOG.info("sandbox_finished", run=str(item.run_id), step=item.name, outcome=verdict.outcome)

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
        try:
            await self.cluster.delete_claim(pods.workspace_claim(item.run_id))
        except ClusterError as error:
            LOG.warning("workspace_not_released", run=str(item.run_id), error=str(error))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def sandboxes_from(settings: Settings) -> pods.Sandboxes:
    if settings.sandbox_namespace is None or settings.sandbox_image is None:
        raise ValueError("the launcher needs the sandbox namespace and the sandbox image")
    return pods.Sandboxes(
        namespace=settings.sandbox_namespace,
        image=settings.sandbox_image,
        pull_secret=settings.sandbox_pull_secret,
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
    launcher = Launcher(
        Runs(sessionmaker(engine, expire_on_commit=False)),
        cluster,
        sandboxes,
        github,
        parallel_steps=resolved.sandbox_parallel_steps,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stop.set)
    LOG.info("launcher_started", namespace=sandboxes.namespace, image=sandboxes.image)
    try:
        while not stop.is_set():
            try:
                await launcher.tick()
            except Exception as error:  # noqa: BLE001 - the database may be away for a moment
                LOG.error("launcher_pass_failed", error=repr(error)[:300])
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=PASS_SECONDS)
    finally:
        await cluster.aclose()
        if github is not None:
            await github.aclose()
        engine.dispose()


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
