"""API-owned, bounded projection repair; never starts or mutates Temporal work."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from functools import partial
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.exc import SQLAlchemyError
from temporalio.service import RPCError, RPCStatusCode

from engineering_control.db.run_enrollment import recover_recorded_run
from engineering_control.operations.connection_budget import (
    diagnostic_budget,
    joined_diagnostic_thread,
)
from engineering_control.operations.reconciliation_state import ReconciliationStateStore
from engineering_control.operations.run_reconciliation_store import RunProjectionStore

log = structlog.get_logger(__name__)
_BENIGN = frozenset({"TERMINAL_REPAIR_AVAILABLE", "TERMINAL_RECONCILED", "ACTIVE_VERIFIED"})
_ERRORS = frozenset(
    {
        "HISTORY_OR_EXECUTION_UNAVAILABLE",
        "OBSERVATION_FAILED",
        "OBSERVATION_TIMEOUT",
        "DATABASE_ERROR",
        "CYCLE_DEADLINE",
    }
)


def _uuid_text(value: Any) -> str:
    if type(value) is not str:
        raise ValueError("execution identity is unverified")
    parsed = UUID(value)
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError("execution identity is unverified")
    return value


class RecurringRunReconciler:
    def __init__(
        self,
        control: Any,
        repository: Any,
        namespace: str,
        *,
        interval_seconds: int = 30,
        batch_size: int = 25,
        cycle_seconds: int = 30,
        max_sweep_age_seconds: int = 300,
    ) -> None:
        for value, minimum, maximum in (
            (interval_seconds, 5, 300),
            (batch_size, 1, 100),
            (cycle_seconds, 10, 120),
            (max_sweep_age_seconds, 30, 3600),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("reconciliation configuration outside bounds")
        if not namespace or control.client.namespace != namespace:
            raise ValueError("reconciliation namespace mismatch")
        self.control, self.repository, self.namespace = control, repository, namespace
        self.interval_seconds, self.batch_size = interval_seconds, batch_size
        self.cycle_seconds = cycle_seconds
        self.store = RunProjectionStore(repository.engine, repository.sessions)
        self.state_store = ReconciliationStateStore(
            repository.engine, repository.sessions, max_sweep_age_seconds
        )

    async def _classify(
        self, candidate: dict[str, Any], *, store: Any = None, sessions: Any = None
    ) -> str:
        store = self.store if store is None else store
        sessions = self.repository.sessions if sessions is None else sessions
        if type(candidate["run_count"]) is not int or candidate["run_count"] != 1:
            return "AMBIGUOUS_APPLICATION_RUN"
        audit = candidate["run_type"] == "audit-campaign"
        if audit and (
            type(candidate["campaign_run_count"]) is not int
            or candidate["campaign_run_count"] != 1
            or candidate["campaign_binding_conflict"]
        ):
            return "AMBIGUOUS_CAMPAIGN_BINDING"
        try:
            observation = await asyncio.wait_for(
                self.control.get_reconciliation_observation(candidate["workflow_id"]), 10
            )
            needs_recovery = candidate["first_execution_run_id"] is None or (
                audit and not candidate["campaign_binding_present"]
            )
            if needs_recovery:
                if observation.get("identity_verified") is not True or (
                    observation.get("workflow_id") != candidate["workflow_id"]
                ):
                    return "IDENTITY_UNVERIFIABLE"
                current = _uuid_text(observation.get("run_id"))
                first = _uuid_text(observation.get("first_execution_run_id"))
                await asyncio.wait_for(
                    recover_recorded_run(
                        sessions,
                        self.control.client,
                        self.namespace,
                        candidate["workflow_id"],
                        current,
                        UUID(_uuid_text(candidate["application_run_id"])),
                        expected_first_run_id=first,
                    ),
                    10,
                )
                candidate = await joined_diagnostic_thread(
                    store.refresh, UUID(candidate["application_run_id"])
                )
                if candidate is None:
                    return "SNAPSHOT_CHANGED"
            decision = await joined_diagnostic_thread(store.apply, candidate, observation)
            if decision.classification == "ACTIVE_OR_UNKNOWN_ORCHESTRATION":
                return (
                    "ACTIVE_VERIFIED"
                    if (
                        observation.get("status") == "RUNNING"
                        and observation.get("identity_verified") is True
                    )
                    else "ORCHESTRATION_STATUS_UNVERIFIED"
                )
            return str(decision.classification)
        except RPCError as error:
            return (
                "HISTORY_OR_EXECUTION_UNAVAILABLE"
                if error.status == RPCStatusCode.NOT_FOUND
                else "OBSERVATION_FAILED"
            )
        except TimeoutError:
            return "OBSERVATION_TIMEOUT"
        except ValueError:
            return "IDENTITY_UNVERIFIABLE"
        except SQLAlchemyError:
            return "DATABASE_ERROR"

    @staticmethod
    def _count(state: dict[str, Any], classification: str) -> None:
        counts = state["counts"]
        counts[classification] = counts.get(classification, 0) + 1
        if classification == "TERMINAL_REPAIR_AVAILABLE":
            state["repair_count"] += 1
        if classification not in _BENIGN:
            state["unresolved_count"] += 1
        if classification in _ERRORS:
            state["error_count"] += 1

    async def _release(self, lease: Any) -> None:
        # Fresh budget: expired/cancelled work must not prevent owned lock cleanup.
        async def release() -> None:
            with diagnostic_budget(5):
                await joined_diagnostic_thread(self.state_store.release, lease)

        task = asyncio.create_task(release(), name="projection-leadership-release")
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def one_cycle(self) -> dict[str, Any]:
        started = time.monotonic()
        holder: dict[str, Any] = {}
        state: dict[str, Any] | None = None
        complete = False

        def acquire() -> None:
            # Cancellation joins this thread; a just-acquired lease remains owned
            # even when the await is cancelled before returning its result.
            holder["lease"] = self.state_store.acquire()

        try:
            try:
                with diagnostic_budget(self.cycle_seconds):
                    async with asyncio.timeout(self.cycle_seconds):
                        await joined_diagnostic_thread(acquire)
                        if holder["lease"] is None:
                            return {"status": "BUSY"}
                        sessions = await joined_diagnostic_thread(
                            self.state_store.owned_sessions, holder["lease"]
                        )
                        store = self.store
                        state_store = self.state_store
                        if self.repository.engine.dialect.name == "postgresql":
                            store = RunProjectionStore(self.repository.engine, sessions)
                            state_store = ReconciliationStateStore(
                                self.repository.engine,
                                sessions,
                                self.state_store._max_sweep_age_seconds,
                            )
                        state = await joined_diagnostic_thread(state_store.load, datetime.now(UTC))
                        cursor = UUID(state["cursor"]) if state["cursor"] else None
                        rows = await joined_diagnostic_thread(
                            store.candidates, cursor, self.batch_size
                        )
                        for row in rows:
                            classification = await self._classify(
                                row, store=store, sessions=sessions
                            )
                            self._count(state, classification)
                            state["processed_count"] += 1
                            state["cursor"] = row["application_run_id"]
                        complete = len(rows) < self.batch_size
            except TimeoutError:
                if state is None:
                    raise
                self._count(state, "CYCLE_DEADLINE")
            if state is None:
                raise RuntimeError("reconciliation state unavailable")
            with diagnostic_budget(5):
                saved = await joined_diagnostic_thread(
                    partial(
                        state_store.save,
                        state,
                        complete=complete,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        now=datetime.now(UTC),
                    )
                )
            return {"status": "SWEEP_COMPLETE" if complete else "PARTIAL", "state": saved}
        finally:
            if holder.get("lease") is not None:
                await self._release(holder["lease"])

    async def serve(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.one_cycle()
            except Exception as error:  # noqa: BLE001 - bounded retry, no exception values
                log.error("recurring_reconciliation_cycle_failed", error_type=type(error).__name__)
            try:
                await asyncio.wait_for(stop.wait(), self.interval_seconds)
            except TimeoutError:
                pass
