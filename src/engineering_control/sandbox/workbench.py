"""The launcher's side of the workbench: the one sandbox a run keeps for its small jobs.

A step in a sandbox of its own takes seconds to start. The tasks that find, read and
change code are many and quick, so a run that has them gets one sandbox that stays: it
is started when the first such task comes up, carries out each job it is handed, and is
deleted when the run ends or waits for the administrator, or when it has had nothing to
do for a while.

The workbench has no network of its own. One door is open, to the launcher alone (the
namespace's rules see to that), and it asks for a token that is made from the run and a
key only the platform holds. What runs in it is the code of the repository, so nothing
it answers is trusted as more than a job's output, and no account is ever mounted in it.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import httpx

from engineering_control.cluster import Cluster
from engineering_control.sandbox import pods

_HERE: Final = Path(__file__).parent / "bench"
START_SECONDS: Final = 600
IDLE_SECONDS: Final = 600
DEADLINE_SECONDS: Final = 6 * 3600
WORKSPACE_GIBIBYTES: Final = 10


def server_source() -> str:
    return (_HERE / "server.py").read_text(encoding="utf-8")


def tools_source() -> str:
    return (_HERE / "tools.py").read_text(encoding="utf-8")


class WorkbenchError(Exception):
    """The workbench cannot be had: the step that needs it fails."""


class WorkbenchLost(Exception):  # noqa: N818 - a state, not a fault
    """The workbench is there but does not know the job: it was started anew."""


class Workbench:
    def __init__(
        self,
        cluster: Cluster,
        sandboxes: Callable[[], Awaitable[pods.Sandboxes]],
        key: bytes | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cluster = cluster
        self.sandboxes = sandboxes
        self.key = key or b"engineering-control workbench"
        self.http = httpx.AsyncClient(transport=transport, trust_env=False)
        self._since: dict[UUID, float] = {}
        self._used: dict[UUID, float] = {}

    async def aclose(self) -> None:
        await self.http.aclose()

    def token(self, run_id: UUID) -> str:
        return hmac.new(self.key, f"workbench:{run_id}".encode(), hashlib.sha256).hexdigest()

    def _headers(self, run_id: UUID) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(run_id)}"}

    def used(self, run_id: UUID) -> None:
        self._used[run_id] = time.monotonic()

    async def ready(self, run_id: UUID) -> tuple[str | None, str]:
        """Where the run's workbench answers, or None and what to say while it is not
        there yet. Starts it when the run has none."""
        name = pods.bench_name(run_id)
        pod = await self.cluster.pod(name)
        waited = time.monotonic() - self._since.setdefault(run_id, time.monotonic())
        if pod is None:
            sandboxes = await self.sandboxes()
            workspace = pods.workspace_claim(run_id)
            await self.cluster.create_claim(
                pods.claim(
                    workspace,
                    namespace=sandboxes.namespace,
                    gibibytes=WORKSPACE_GIBIBYTES,
                    labels={"engineering-control/run": str(run_id)},
                )
            )
            await self.cluster.create_pod(
                pods.bench_pod(
                    sandboxes,
                    run_id=run_id,
                    workspace=workspace,
                    server=server_source(),
                    token=self.token(run_id),
                    deadline_seconds=DEADLINE_SECONDS,
                )
            )
            return None, "The workbench is starting."
        status = pod.get("status") or {}
        phase = status.get("phase")
        if phase in ("Succeeded", "Failed"):
            # It ended (its time was up, or the node went away): the next pass starts one.
            await self.cluster.delete_pod(name, grace_seconds=0)
            return None, "The workbench ended and is started again."
        address = status.get("podIP")
        if phase == "Running" and address:
            try:
                answer = await self.http.get(
                    f"http://{address}:{pods.BENCH_PORT}/health", timeout=3
                )
                if answer.status_code == 200:
                    self._since.pop(run_id, None)
                    self.used(run_id)
                    return f"http://{address}:{pods.BENCH_PORT}", ""
            except httpx.HTTPError:
                pass
        if waited > START_SECONDS:
            await self.cluster.delete_pod(name, grace_seconds=0)
            self._since.pop(run_id, None)
            raise WorkbenchError("The workbench did not start in time.")
        return None, "The workbench is starting."

    async def hand(self, address: str, run_id: UUID, key: str, job: dict[str, Any]) -> bool:
        """Hand the workbench a job. False when it is busy with another."""
        try:
            answer = await self.http.post(
                f"{address}/job",
                headers=self._headers(run_id),
                json={"key": key, "job": job},
                timeout=20,
            )
        except httpx.HTTPError as error:
            raise WorkbenchLost(
                f"the workbench did not take the job: {type(error).__name__}"
            ) from error
        if answer.status_code == 409:
            return False
        if answer.status_code not in (200, 202):
            raise WorkbenchError(f"The workbench refused the job ({answer.status_code}).")
        self.used(run_id)
        return True

    async def result(self, address: str, run_id: UUID, key: str, *, wait: float) -> str | None:
        """What the job printed, once it has ended; None while it runs."""
        try:
            answer = await self.http.get(
                f"{address}/job/{key}",
                headers=self._headers(run_id),
                params={"wait": f"{wait:g}"},
                timeout=wait + 10,
            )
        except httpx.HTTPError as error:
            raise WorkbenchLost(f"the workbench did not answer: {type(error).__name__}") from error
        if answer.status_code == 404:
            raise WorkbenchLost("the workbench does not know the job")
        if answer.status_code != 200:
            raise WorkbenchError(f"The workbench answered {answer.status_code}.")
        self.used(run_id)
        body = answer.json()
        return str(body.get("log") or "") if body.get("done") else None

    async def release(self, run_id: UUID) -> None:
        """The run has no more use for its workbench."""
        known = run_id in self._used or run_id in self._since
        self._used.pop(run_id, None)
        self._since.pop(run_id, None)
        if known:
            await self.cluster.delete_pod(pods.bench_name(run_id), grace_seconds=0)

    async def tidy(self) -> None:
        """Delete the workbenches that have had nothing to do for a while. A run that
        comes to need one again gets a new one: what counts is on its volume."""
        now = time.monotonic()
        for run_id in [run for run, at in self._used.items() if now - at > IDLE_SECONDS]:
            await self.release(run_id)
