from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import asdict, replace
from datetime import timedelta
from typing import Any, Literal
from uuid import UUID, uuid5

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError
from temporalio.exceptions import TimeoutError as ActivityTimeoutError

from engineering_control.workflows.cleanup_scope import (
    rescope_after_recovery,
    scope_identity_blockers,
)
from engineering_control.workflows.inputs import (
    AcceptanceLifecycleInput,
    AuditCampaignInput,
    AutonomousBacklogCampaignInput,
    BacklogDecompositionPlanInput,
    ChangeRequestInput,
    CleanupInput,
    ConnectionsRefreshInput,
    DeliveryReconciliationInput,
    EnhancementDeliveryInput,
    EnhancementDiscoveryInput,
    EnhancementSolutionLoopInput,
    ExistingGitHubBacklogLoopInput,
    FinalClosureInput,
    FindingVerificationInput,
    GitHubBacklogImportInput,
    IssueAmendObservationInput,
    IssueDeliveryInput,
    IssueSolutionLoopInput,
    LLMWikiStagingBootstrapInput,
    MultiProjectAuditInput,
    ObservabilityAcceptanceInput,
    PreviewDeploymentInput,
    PublicationHeadsObservationInput,
    QuotaRefreshInput,
    RecoveryInput,
    RuntimeVerificationInput,
    StagingDeploymentInput,
    StagingGitOpsDeliveryInput,
    UiBlueprintFanOutInput,
    UiGapConsolidationPlanInput,
    UiMigrationPlanInput,
)
from engineering_control.workflows.provider_stops import (
    PROVIDER_UNAVAILABLE,
    provider_unavailable_stop,
)
from engineering_control.workflows.worker_registry import (
    CONTROL_TASK_QUEUE,
    GITHUB_TASK_QUEUE,
    provider_task_queue,
    task_queue_for,
)

# NEW-113 review fallback: an Argo remediation write is never retried automatically. A
# failed or lost attempt may already have written its operation, and a later operator sync
# can erase the evidence of it, so an unknown outcome is escalated to the operator.
SINGLE_ATTEMPT = RetryPolicy(maximum_attempts=1)

ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=5,
    non_retryable_error_types=[
        "PolicyViolation",
        "SecurityBlocker",
        "ApprovalMismatch",
        "DirtyWorktree",
        "NotConfigured",
        "SchemaViolation",
        "MANUAL_AUTH_REQUIRED_CODEX",
        "MANUAL_AUTH_REQUIRED_CLAUDE",
        "PROVIDER_CLI_MISSING_CODEX",
        "PROVIDER_CLI_MISSING_CLAUDE",
    ],
)


# NEW-214b: the issue a pipelined BACK slot works on. Its loop-state notices name that
# issue, not the FRONT slot's. Set only inside a BACK task's own context.
_BACK_DRAIN_POINT: Any = object()


def _live_loop_items(
    raw_items: list[Any], back_in_flight: Callable[[], bool]
) -> Iterator[tuple[int, Any]]:
    """enumerate(raw_items, start=1) over a growing list (refills append to it).

    NEW-214b: once the issues ran out with a BACK slot in flight, yields a drain point
    instead of stopping, so a slot that BACK outcome refills is still worked on.
    """
    ordinal = 0
    while True:
        if ordinal < len(raw_items):
            ordinal += 1
            yield ordinal, raw_items[ordinal - 1]
        elif back_in_flight():
            yield ordinal, _BACK_DRAIN_POINT
        else:
            return


# NEW-214 Stage 2: which slot a task is ("back", "front-<n>"); None is the loop itself.
_SLOT_ID: ContextVar[str | None] = ContextVar("hermes_slot_id", default=None)


def _in_front_lane() -> bool:
    return (_SLOT_ID.get() or "").startswith("front-")


class _LaneStopped(Exception):  # noqa: N818 - control flow, not an error
    """NEW-214 Stage 2: the loop ends (another slot recorded its end); stop this lane."""


_SLOT_ISSUE: ContextVar[str | None] = ContextVar("hermes_slot_issue", default=None)


class ControlledWorkflow:
    _reported_deployment_errors = frozenset(
        {"ApprovalMismatch", "PolicyViolation", "SecurityBlocker", "ReconciliationPending"}
    )

    def __init__(self) -> None:
        self._child_run_ids: dict[str, str] = {}
        self._paused = False
        self._cancelled = False
        self._pause_after_current = False
        self._stop_after_current = False
        self._approval: dict[str, Any] | None = None
        self._phase = "INITIALIZING"
        self._skip_reason: str | None = None
        self._retry_step: str | None = None
        self._write_policy_reason: str | None = None
        # NEW-84: ids a write-policy alert names (set by workflows that know them).
        self._write_notice: dict[str, Any] = {}
        # NEW-114: retry/skip signals a backlog loop received for an issue it was not
        # waiting on; kept (bounded) so the status query shows they were ignored.
        self._ignored_signals: list[dict[str, str]] = []
        # NEW-114: why the last free-slot refill selected nothing (selection refused).
        self._refill_blocker: str | None = None
        # NEW-127: the issue a backlog loop works on, named in its loop-state notices.
        self._loop_issue: str | None = None
        # NEW-127: whether this backlog loop registered its own Run (state only).
        self._loop_register_run = False
        # NEW-162: the backlog loop's results and delivered count (state only), so a skip
        # that ends the loop from a failed bookkeeping step completes it truthfully.
        self._loop_results: list[dict[str, Any]] = []
        self._loop_delivered = 0
        # NEW-214b: pipelined (concurrency 2) loop slots, workflow state only.
        self._pipelined = False
        self._front_busy = False
        self._implementation_holder: str | None = None
        self._back_task: asyncio.Task[tuple[str, str]] | None = None
        self._back_cancelled = False
        # NEW-214 Stage 2: FRONT lanes of a concurrency-3 loop (workflow state only).
        self._front_lanes = 1
        self._front_capacity_payload: dict[str, Any] = {}
        self._lanes_busy: set[str] = set()
        self._front_refs: set[str] = set()
        self._handoff_queue: list[tuple[str, str]] = []
        self._back_outcomes: dict[str, str] = {}
        self._back_ref = ""
        self._back_resume: dict[int, dict[str, Any]] = {}
        self._repair_queue: list[dict[str, Any]] = []
        self._handoff_tickets: dict[str, tuple[Any, int]] = {}
        self._lane_ordinals: dict[str, int] = {}
        self._held_paused = False
        self._loop_request: ExistingGitHubBacklogLoopInput | None = None
        self._makers_running: set[str] = set()
        self._operator_waiters: set[str] = set()
        self._operator_holder: str | None = None
        self._loop_ending = False
        self._refilling = False

    @workflow.signal(name="pause")
    async def pause(self) -> None:
        self._paused = True

    @workflow.signal(name="resume")
    async def resume(self) -> None:
        self._paused = False
        self._pause_after_current = False

    @workflow.signal(name="cancel")
    async def cancel(self) -> None:
        self._cancelled = True
        self._paused = False
        self._pause_after_current = False
        self._stop_after_current = False

    @workflow.signal(name="pause_after_current")
    async def pause_after_current(self) -> None:
        if not self._cancelled and not self._stop_after_current:
            self._pause_after_current = True

    @workflow.signal(name="stop_after_current")
    async def stop_after_current(self) -> None:
        if not self._cancelled:
            self._stop_after_current = True
            self._pause_after_current = False

    @workflow.signal(name="approval")
    async def approval(self, payload: dict[str, Any]) -> None:
        self._approval = payload

    @workflow.signal(name="skip")
    async def skip(self, reason: str) -> None:
        self._skip_reason = reason[:500]

    @workflow.signal(name="retry")
    async def retry(self, step: str) -> None:
        self._retry_step = step[:120]

    @workflow.query(name="status")
    def status(self) -> dict[str, Any]:
        return {
            "phase": self._phase,
            "paused": self._paused,
            "cancelled": self._cancelled,
            "pause_after_current": self._pause_after_current,
            "stop_after_current": self._stop_after_current,
            "approval_received": self._approval is not None,
            "skip_reason": self._skip_reason,
            "retry_step": self._retry_step,
            "write_policy_reason": self._write_policy_reason,
            "ignored_signals": list(self._ignored_signals),
        }

    def _loop_state_issue_patched(self) -> bool:
        # NEW-127: histories recorded before this marker keep their activity input.
        return workflow.patched("backlog-loop-state-issue-v1")

    def _note_ignored_signal(self, signal: str, value: str | None, awaited: str) -> None:
        """NEW-114: record (never act on) a signal naming an issue the loop does not await.

        Only workflow state and a replay-safe log line: no command, so no patch marker."""
        entry = {"signal": signal, "value": str(value or "")[:200], "awaited": awaited[:200]}
        self._ignored_signals = [*self._ignored_signals, entry][-20:]
        workflow.logger.warning("backlog loop ignored a signal for another issue: %s", entry)

    async def _slot_pause_gate(self) -> None:
        """NEW-214b: only one pipelined slot ever awaits the operator.

        A BACK slot pauses only once the FRONT slot is idle. A FRONT slot first lets the
        BACK slot finish, idle meanwhile. The existing retry/skip signals then always
        address the one paused issue. Without pipelining this does nothing.
        """
        if not getattr(self, "_pipelined", False):
            return
        if self._front_lanes >= 2:
            await self._operator_turn()
            return
        if _SLOT_ID.get() == "back":
            await workflow.wait_condition(lambda: not self._front_busy)
            return
        task = self._back_task
        if task is None or task.done():
            return
        self._front_busy = False
        try:
            await workflow.wait_condition(task.done)
        finally:
            self._front_busy = True

    async def _operator_turn(self) -> None:
        """NEW-214 Stage 2: the operator turn among the BACK slot and the FRONT lanes.

        A slot leaves RUNNING (pause or end) only once every FRONT lane is idle and,
        for a FRONT lane or the loop itself, the BACK slot ended. Waiting slots keep the
        lanes from starting new work, and one slot at a time holds the turn, so the
        retry/skip signals always address the one paused issue.
        """
        me = _SLOT_ID.get() or "loop"
        self._operator_waiters.add(me)
        self._lanes_busy.discard(me)
        try:
            while True:
                await workflow.wait_condition(
                    lambda: (
                        self._operator_holder in (None, me)
                        and not (self._lanes_busy - {me})
                        and (me == "back" or self._back_task is None or self._back_task.done())
                    )
                )
                if self._operator_holder in (None, me):
                    self._operator_holder = me
                    return
        finally:
            self._operator_waiters.discard(me)
            if me.startswith("front-"):
                self._lanes_busy.add(me)

    def _release_operator(self, slot: str | None) -> None:
        if self._operator_holder == (slot or "loop"):
            self._operator_holder = None
            self._held_paused = False

    async def _end_operator_turn(self, slot: str) -> None:
        """NEW-214 Stage 2: a slot's issue ended; it gives its operator turn up.

        A turn that last recorded PAUSED (a skip records no RUNNING) records RUNNING
        first, so no other slot works while the campaign reads PAUSED.
        """
        if self._front_lanes < 2 or self._operator_holder != slot:
            return
        if self._held_paused and not self._loop_ending and self._loop_request is not None:
            state = {**_backlog_execution_payload(self._loop_request), "loop_status": "RUNNING"}
            if self._loop_register_run:
                state["workflow_id"] = workflow.info().workflow_id
            await self._step("backlog.record_loop_state", state)
        self._release_operator(slot)

    def _operator_quiet(self, lane: str) -> bool:
        return not (self._operator_waiters - {lane}) and self._operator_holder in (None, lane)

    async def _lane_wait(self, ready: Callable[[], bool]) -> None:
        """NEW-214 Stage 2: a FRONT lane waits idle for ``ready``.

        It resumes only while no other slot awaits or holds the operator turn, and stops
        (``_LaneStopped``) once the loop ends.
        """
        lane = _SLOT_ID.get() or "loop"
        self._lanes_busy.discard(lane)
        try:
            await workflow.wait_condition(
                lambda: self._loop_ending or (ready() and self._operator_quiet(lane))
            )
        finally:
            self._lanes_busy.add(lane)
        if self._loop_ending:
            raise _LaneStopped()

    def _waits_for_dependency(self, item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        dependencies = {str(value) for value in item.get("dependencies") or []}
        return bool(dependencies & self._in_flight_refs())

    def _in_flight_refs(self) -> set[str]:
        """Issues another slot still works on: FRONT lanes, hand-over and repair queues,
        BACK (also while a lane drains it)."""
        refs = set(self._front_refs) | {ref for _lane, ref in self._handoff_queue}
        refs |= {str(entry["ref"]) for entry in self._repair_queue}
        if self._back_task is not None and not self._back_task.done():
            refs.add(self._back_ref)
        return refs

    async def _handoff_turn(self, ref: str) -> None:
        """NEW-214 Stage 2: lanes hand over to BACK in the order their checkers passed.

        The ticket (checker-pass time, plan order) is taken when the delivery returns its
        PR (_issue_delivery), so lookups between that and the hand-over never reorder.
        """
        lane = _SLOT_ID.get() or "loop"
        self._handoff_tickets.setdefault(lane, (workflow.now(), self._lane_ordinals.get(lane, 0)))
        self._handoff_queue.append((lane, ref))
        try:
            await self._lane_wait(
                lambda: min(self._handoff_tickets.values()) == self._handoff_tickets[lane]
            )
        except BaseException:
            self._handoff_done()
            raise

    def _handoff_done(self) -> None:
        lane = _SLOT_ID.get() or "loop"
        self._handoff_queue = [entry for entry in self._handoff_queue if entry[0] != lane]
        self._handoff_tickets.pop(lane, None)

    async def _checkpoint(self, phase: str) -> bool:
        self._phase = phase
        if self._paused:
            await workflow.wait_condition(lambda: not self._paused or self._cancelled)
        return not self._cancelled

    async def _step(
        self,
        step: str,
        payload: dict[str, Any],
        *,
        timeout_minutes: int = 30,
        retry_policy: RetryPolicy = ACTIVITY_RETRY,
    ) -> dict[str, Any]:
        if self._loop_ending and _in_front_lane():
            # NEW-214 Stage 2: another slot recorded the loop's end; this lane stops.
            raise _LaneStopped()
        if (
            step == "backlog.record_loop_state"
            and self._pipelined
            and payload.get("loop_status") != "RUNNING"
        ):
            # NEW-214b: no slot leaves RUNNING while the other could still be working, so
            # a campaign is never PAUSED, STOPPED or ended with a BACK slot in flight.
            await self._slot_pause_gate()
            if self._back_cancelled and _SLOT_ID.get() != "back":
                return {"status": "RECORDED", "reason": "BACK_SLOT_RECORDED_CANCELLED"}
        loop_issue = _SLOT_ISSUE.get() or self._loop_issue
        if (
            step == "backlog.record_loop_state"
            and loop_issue is not None
            and "issue" not in payload
            and self._loop_state_issue_patched()
        ):
            # NEW-127: the loop-state notification names the issue (and its commands).
            payload = {**payload, "issue": loop_issue}
        result = await workflow.execute_activity(
            "execute_control_step",
            {"step": step, "payload": payload},
            task_queue=self._activity_queue(step, payload),
            start_to_close_timeout=timedelta(minutes=timeout_minutes),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=retry_policy,
        )
        if not isinstance(result, dict):
            raise TypeError(f"activity {step} returned a non-object result")
        if step == "backlog.record_loop_state" and self._front_lanes >= 2:
            # NEW-214 Stage 2: the turn holder's state is now durable. RUNNING ends its
            # operator wait (only now may other slots work again); PAUSED is remembered,
            # so a turn given up without RUNNING records it first (_end_operator_turn).
            holder = _SLOT_ID.get() or "loop"
            if self._operator_holder == holder:
                self._held_paused = payload.get("loop_status") == "PAUSED"
                if payload.get("loop_status") == "RUNNING":
                    self._release_operator(holder)
            if payload.get("loop_status") in {"CANCELLED", "FAILED", "STOPPED", "COMPLETE"}:
                # The end is durable: no lane starts anything from now on.
                self._loop_ending = True
        if (
            step == "backlog.record_loop_state"
            and self._pipelined
            and payload.get("loop_status") == "CANCELLED"
            and _SLOT_ID.get() == "back"
        ):
            # NEW-214b: the BACK slot recorded the loop CANCELLED (also on a path that
            # then raises, e.g. a cancelled failed bookkeeping step).
            self._back_cancelled = True
        if step == "backlog.register_child_run" and workflow.patched("child-run-start-memo-v1"):
            status = result.get("status")
            result_child_id = result.get("child_workflow_id")
            run_id = result.get("run_id")
            payload_child_id = payload.get("child_workflow_id")
            if (
                status != "REGISTERED"
                or result_child_id != payload_child_id
                or type(payload_child_id) is not str
            ):
                raise ApplicationError(
                    "child Run registration is invalid",
                    type="PolicyViolation",
                    non_retryable=True,
                )
            self._remember_child_run(payload_child_id, run_id)
        return result

    def _remember_child_run(self, workflow_id: str, run_id: Any) -> None:
        if type(workflow_id) is not str or not (1 <= len(workflow_id) <= 240):
            raise ApplicationError(
                "child Run registration is invalid",
                type="PolicyViolation",
                non_retryable=True,
            )
        if not workflow_id.isprintable():
            raise ApplicationError(
                "child Run registration is invalid",
                type="PolicyViolation",
                non_retryable=True,
            )
        if type(run_id) is not str or len(run_id) != 36:
            raise ApplicationError(
                "child Run registration is invalid",
                type="PolicyViolation",
                non_retryable=True,
            )
        try:
            parsed = UUID(run_id)
        except ValueError:
            raise ApplicationError(
                "child Run registration is invalid",
                type="PolicyViolation",
                non_retryable=True,
            ) from None
        if str(parsed) != run_id or parsed.int == 0:
            raise ApplicationError(
                "child Run registration is invalid",
                type="PolicyViolation",
                non_retryable=True,
            )
        existing = self._child_run_ids.get(workflow_id)
        if existing == run_id:
            return
        if existing is not None:
            raise ApplicationError(
                "child Run registration is invalid",
                type="PolicyViolation",
                non_retryable=True,
            )
        if len(self._child_run_ids) >= 1024:
            raise ApplicationError(
                "child Run registration is invalid",
                type="PolicyViolation",
                non_retryable=True,
            )
        self._child_run_ids[workflow_id] = run_id

    def _child_run_options(self, workflow_id: str) -> dict[str, Any]:
        if self._loop_ending and _in_front_lane():
            # NEW-214 Stage 2: another slot recorded the loop's end; start no child.
            raise _LaneStopped()
        run_id = self._child_run_ids.get(workflow_id)
        if run_id is None:
            return {}
        return {
            "memo": {
                "engineering_application_run_id": run_id,
                "engineering_binding_version": 1,
            }
        }

    async def _observability_terminal_run(
        self, context: dict[str, Any], *, reverse: bool = False
    ) -> dict[str, Any]:
        """Finalize a registered canary after cleanup; retain real activity failures."""
        context = await self._step("observability.register", context)
        primary_error: ActivityError | None = None
        cleanup_error: ActivityError | None = None
        terminal = "FAILED"
        blocker: str | None = None
        try:
            context = await self._step("observability.prepare_workspace", context)
            maker_prepare, maker_invoke, checker_prepare, checker_invoke = (
                (
                    "observability.prepare_claude_maker",
                    "observability.invoke_claude_maker",
                    "observability.prepare_codex_checker",
                    "observability.invoke_codex_checker",
                )
                if reverse
                else (
                    "observability.prepare_codex",
                    "observability.invoke_codex",
                    "observability.prepare_claude",
                    "observability.invoke_claude",
                )
            )
            context = await self._step(maker_prepare, context)
            context = await self._agent_step(maker_invoke, context, timeout_minutes=10)
            if context.get("status") == "SUCCESS":
                context = await self._step(checker_prepare, context)
                context = await self._agent_step(checker_invoke, context, timeout_minutes=10)
                terminal = "COMPLETED" if context.get("status") == "SUCCESS" else "FAILED"
            if terminal == "FAILED":
                safe_blockers = {
                    "MANUAL_AUTH_REQUIRED_CODEX",
                    "MANUAL_AUTH_REQUIRED_CLAUDE",
                    "PROVIDER_CLI_MISSING_CODEX",
                    "PROVIDER_CLI_MISSING_CLAUDE",
                    "RATE_LIMITED",
                    "SchemaViolation",
                    "PolicyViolation",
                    "PROVIDER_ACTIVITY_FAILED",
                }
                raw_blocker = context.get("blocker")
                blocker = (
                    raw_blocker
                    if isinstance(raw_blocker, str) and raw_blocker in safe_blockers
                    else "OBSERVABILITY_PROVIDER_NON_SUCCESS"
                )
        except ActivityError as error:
            primary_error = error
            blocker = "OBSERVABILITY_ACTIVITY_FAILED"
        finally:
            # A failed prepare may have created part of the owned workspace.
            # The existing cleanup activity derives its confined path from workspace_id.
            # Cancellation is not swallowed or converted into a successful result.
            try:
                await self._step("observability.cleanup_workspace", context)
            except ActivityError as error:
                cleanup_error = error
        if cleanup_error is not None:
            terminal = "FAILED"
            blocker = (
                "OBSERVABILITY_ACTIVITY_AND_CLEANUP_FAILED"
                if primary_error is not None
                else "OBSERVABILITY_CLEANUP_FAILED"
            )
        finalized = await self._step(
            "observability.finalize",
            {**context, "terminal_status": terminal, "blocker": blocker},
        )
        if primary_error is not None:
            raise primary_error
        if cleanup_error is not None:
            raise cleanup_error
        return finalized

    async def _github_write_step(
        self, step: str, payload: dict[str, Any], *, timeout_minutes: int = 30
    ) -> dict[str, Any]:
        """Wait durably for an explicit retry signal after a terminal policy denial.

        NEW-84: the first denial queues one Telegram alert with the approve commands, and
        the step is then re-tried every 5 minutes so an approval alone resumes the flow.
        """
        self._retry_step = None
        auto_resume: bool | None = None
        while True:
            result = await self._step(step, payload, timeout_minutes=timeout_minutes)
            if result.get("status") != "BLOCKED_BY_POLICY":
                self._write_policy_reason = None
                return result
            self._phase = "AWAITING_WRITE_POLICY"
            self._write_policy_reason = str(result.get("reason_code", "POLICY_INVALID"))[:120]
            if auto_resume is None:
                auto_resume = workflow.patched("write-policy-alert-auto-resume-v1")
                if auto_resume:
                    await self._step(
                        "notify.write_policy_required",
                        {
                            **self._write_notice,
                            "project": self._write_notice.get("project") or payload.get("project"),
                            "workflow_id": workflow.info().workflow_id,
                            "step": step,
                            "reason_code": self._write_policy_reason,
                        },
                    )
            if auto_resume:
                try:
                    await workflow.wait_condition(
                        lambda: self._cancelled or self._retry_step == step,
                        timeout=timedelta(minutes=5),
                    )
                except TimeoutError:
                    continue
            else:
                await workflow.wait_condition(lambda: self._cancelled or self._retry_step == step)
            if self._cancelled:
                return {**result, "status": "CANCELLED", "workflow_state": "CANCELLED"}
            self._retry_step = None

    async def _agent_step(
        self,
        step: str,
        payload: dict[str, Any],
        *,
        timeout_minutes: int,
        retry_policy: RetryPolicy = ACTIVITY_RETRY,
    ) -> dict[str, Any]:
        """Persist an invocation boundary before and after a native provider activity.

        NEW-234 PR5 review: `retry_policy` (default ACTIVITY_RETRY, unchanged for every
        existing caller) applies to the provider activity only."""
        started = await self._step(
            "agent.record_start",
            {**payload, "provider_step": step},
        )
        try:
            result = await self._step(
                step, started, timeout_minutes=timeout_minutes, retry_policy=retry_policy
            )
        except ActivityError as error:
            failed = _agent_failure_payload(started, error.cause)
            return await self._step("agent.persist_result", failed)
        return await self._step("agent.persist_result", result)

    async def _deployment_failure(
        self, context: dict[str, Any], error: ActivityError
    ) -> dict[str, Any]:
        cause = error.cause
        blocker = (
            cause.type
            if isinstance(cause, ApplicationError)
            and cause.type in self._reported_deployment_errors
            else "DEPLOYMENT_ACTIVITY_FAILED"
        )
        return await self._step(
            "deployment.record_terminal",
            {**context, "terminal_status": "DEPLOYMENT_FAILED", "blocker": blocker},
        )

    @classmethod
    def _activity_queue(cls, step: str, payload: dict[str, Any]) -> str:
        if step in {"cleanup.inspect_evidence_files", "cleanup.execute_evidence_files"}:
            return CONTROL_TASK_QUEUE
        if step in {"cleanup.inspect_delivery_files", "cleanup.execute_delivery_files"}:
            return task_queue_for("delivery")
        if step in {"cleanup.inspect_browser_files", "cleanup.execute_browser_files"}:
            return task_queue_for("browser")
        if step in {"cleanup.inspect_codex_files", "cleanup.execute_codex_files"}:
            return task_queue_for("codex")
        if step in {"cleanup.inspect_preview_namespaces", "cleanup.execute_preview_namespaces"}:
            return task_queue_for("release")
        if step in {
            "project.snapshot",
            "project.snapshot_pair",
            "deployment.compare_live",
            "audit.static_source",
            "audit.static_deployment",
            "audit.kubernetes_observe",
            "audit.correlate_runtime",
            "finding.fresh_snapshot",
            "delivery.allocate_worktrees",
            "delivery.deterministic_prechecks",
            "delivery.prepare_toolchain",
            "delivery.prepare_ui_page",
            "delivery.resolve_dependencies",
            "delivery.converge_deployment_pin",
            "delivery.validate_changeset",
            "audit.deterministic_cell",
            "runtime.deterministic_checks",
            "identity.observer_attest",
            "cleanup.protect_active",
            "cleanup.execute",
            "cleanup.prune_orphan_worktrees",
            "observability.prepare_workspace",
            "observability.cleanup_workspace",
            "ui.inventory",
            "ui.kit_snapshot",
        }:
            return task_queue_for("scan")
        if step == "finding.reproduce":
            return provider_task_queue(
                cls._nested_value(payload, "provider", "claude"), default="claude"
            )
        if step == "audit.agent_cell":
            return provider_task_queue(
                cls._nested_value(payload, "provider", "codex"), default="codex"
            )
        if step in {
            "audit.api_flow",
            "audit.browser_flow",
            "audit.llmwiki_consumer_suite",
            "identity.superadmin_attest",
            "identity.inspect_stale",
            "identity.recover_stale",
        }:
            return task_queue_for("browser")
        if step in {"deployment.render"}:
            return task_queue_for("release")
        if step in {
            "finding.dedupe",
            "finding.publish",
            "enhancement.publish",
            "enhancement.github_dedupe",
        }:
            return GITHUB_TASK_QUEUE
        if step == "llmwiki.bootstrap.plan":
            return GITHUB_TASK_QUEUE
        if step in {
            "github.refresh_mirrors",
            "backlog.github_import",
            "delivery.refresh_issue_body",
        }:
            return GITHUB_TASK_QUEUE
        if step == "llmwiki.bootstrap.execute":
            return task_queue_for("release")
        if step == "change_request.github_dedupe":
            return GITHUB_TASK_QUEUE
        # NEW-123: fixed opposite providers -- Claude drafts the blueprint and the gap change
        # requests; Codex independently reviews (change_request.review routes below).
        if step in {"ui_plan.draft", "ui_fanout.gap_draft", "ui_consolidate.plan"}:
            return provider_task_queue("claude", default="claude")
        if step == "ui_plan.review":
            return provider_task_queue("codex", default="codex")
        if step in {"ui_plan.stage_inputs", "ui_plan.stage_document", "ui_plan.stage_parts"}:
            return task_queue_for("scan")
        # NEW-234 PR5: the backlog decomposition -- Claude drafts each part, Codex
        # independently reviews it; the scan worker builds the inventory and stages the
        # model inputs; the GitHub worker takes the read-only issue snapshot.
        if step == "decomposition.draft":
            return provider_task_queue("claude", default="claude")
        if step == "decomposition.review":
            return provider_task_queue("codex", default="codex")
        if step in {
            "decomposition.inventory",
            "decomposition.stage_inputs",
            "decomposition.stage_document",
        }:
            return task_queue_for("scan")
        if step == "decomposition.read_issues":
            return GITHUB_TASK_QUEUE
        # NEW-108: fixed opposite providers -- Claude drafts, Codex independently reviews.
        if step == "change_request.draft":
            return provider_task_queue("claude", default="claude")
        if step == "change_request.review":
            return provider_task_queue("codex", default="codex")
        if step in {"enhancement.discover", "enhancement.product_review"}:
            return provider_task_queue(
                cls._nested_value(payload, "provider", "codex"), default="codex"
            )
        if step == "delivery.invoke_spec":
            return provider_task_queue(
                cls._nested_value(payload, "spec_provider", "claude"), default="claude"
            )
        if step == "delivery.invoke_maker":
            return provider_task_queue(
                cls._nested_value(payload, "maker_provider", "codex"), default="codex"
            )
        if step == "delivery.invoke_deployment_maker":
            return provider_task_queue(
                cls._nested_value(payload, "maker_provider", "codex"), default="codex"
            )
        if step == "delivery.invoke_checker":
            return provider_task_queue(
                cls._nested_value(payload, "checker_provider", "claude"), default="claude"
            )
        if step in {"observability.invoke_codex", "observability.invoke_codex_checker"}:
            return provider_task_queue("codex", default="codex")
        if step in {"observability.invoke_claude", "observability.invoke_claude_maker"}:
            return provider_task_queue("claude", default="claude")
        if step == "reconcile.invoke_release_reviewer":
            return provider_task_queue(
                cls._nested_value(payload, "release_review_provider", "claude"),
                default="claude",
            )
        if step == "quota.refresh":
            return provider_task_queue(
                cls._nested_value(payload, "provider", "codex"), default="codex"
            )
        if step == "runtime.independent_review":
            return provider_task_queue(
                cls._nested_value(payload, "provider", "codex"), default="codex"
            )
        if step == "delivery.publish":
            return GITHUB_TASK_QUEUE
        if step in {
            "gitops.pr_checks",
            "gitops.pr_heads",
            "gitops.branch_protection",
            "gitops.deployment_pr_checks",
            "gitops.merge",
            "gitops.release_tag",
            "gitops.workflow_run",
            "gitops.image_pin",
            "gitops.environment_issue",
            "gitops.close_issue",
        }:
            return GITHUB_TASK_QUEUE
        if step in {
            "reconcile.inspect_ci",
            "reconcile.publish_promotion",
            "final.merge_source",
            "final.inspect_artifact",
            "final.merge_deployment",
            "final.sync_worktrees",
            "final.comment_issue",
            "final.close_issue",
        }:
            return GITHUB_TASK_QUEUE
        if step == "reconcile.apply_promotion":
            return task_queue_for("scan")
        if step in {"runtime.browser_checks"}:
            return task_queue_for("browser")
        if step in {
            "preview.preflight",
            "preview.verify",
            "staging.preflight",
            "preview.deploy",
            "staging.deploy",
            "recovery.execute",
        }:
            return task_queue_for("release")
        return CONTROL_TASK_QUEUE

    @staticmethod
    def _nested_value(value: Any, key: str, default: str) -> str:
        if isinstance(value, dict):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
            for nested in value.values():
                found = ControlledWorkflow._nested_value(nested, key, "")
                if found:
                    return found
        return default


@workflow.defn(name="AuditCampaignWorkflow")
class AuditCampaignWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: AuditCampaignInput) -> dict[str, Any]:
        plan_payload = asdict(request)
        if request.preset in {"e2e-smoke", "full-e2e", "security", "falcone-intensive"}:
            authority = "falcone" if request.project == "llmwiki" else request.project
            plan_payload["worker_attestation"] = await self._step(
                "identity.superadmin_attest", {"project": authority}
            )
        if request.preset != "static-full":
            plan_payload["observer_attestation"] = await self._step(
                "identity.observer_attest", {"project": request.project}
            )
        plan = await self._step("campaign.plan", plan_payload)
        if request.plan_only:
            return plan
        repository_surfaces = request.repository_surfaces or list(
            plan.get("repository_surfaces", [])
        )
        domains = request.domains or list(plan.get("domains", ["product"]))
        personas = request.personas or list(plan.get("personas", ["anonymous"]))
        flows = request.flows or list(plan.get("flows", ["repository-static"]))
        lenses = request.lenses or list(plan.get("lenses", ["correctness"]))
        environments = request.environments or [request.environment]
        if not flows:
            flows = ["repository-static"]
        if not repository_surfaces:
            repository_surfaces = ["source", "deployment", "pair"]
        if not await self._checkpoint("SNAPSHOT"):
            return {"status": "CANCELLED"}
        cells = [
            {
                "repository_surface": repository_surface,
                "domain": domain,
                "persona": persona,
                "flow": flow,
                "lens": lens,
                "environment": environment,
            }
            for repository_surface in repository_surfaces
            for domain in domains
            for persona in personas
            for flow in flows
            for lens in lenses
            for environment in environments
        ]
        if len(cells) > 100_000:
            return {"status": "BLOCKED", "blocker": "COVERAGE_MATRIX_LIMIT"}
        batch = cells[request.cell_offset : request.cell_offset + 50]
        snapshot: dict[str, Any] | None = None
        snapshot_blocker = request.snapshot_blocker
        root_blocker_classification = request.root_blocker_classification
        if snapshot_blocker is None:
            try:
                await self._step("github.refresh_mirrors", {"project": request.project})
                snapshot = await self._step("project.snapshot", asdict(request))
            except ActivityError:
                snapshot_blocker = "PROJECT_SNAPSHOT_UNAVAILABLE"
                root_blocker_classification = "EXTERNAL_DEPENDENCY_FAILURE"
        campaign_id = request.campaign_id or (snapshot or {}).get("campaign_id")
        if not campaign_id:
            return {"status": "BLOCKED", "blocker": "PROJECT_SNAPSHOT_UNAVAILABLE"}
        if snapshot_blocker is not None:
            await self._step(
                "campaign.persist_snapshot_blockers",
                {
                    "campaign_id": campaign_id,
                    "cells": batch,
                    "total": len(cells),
                    "blocker": snapshot_blocker,
                    "root_blocker_classification": root_blocker_classification,
                },
            )
            next_offset = request.cell_offset + len(batch)
            if next_offset < len(cells):
                workflow.continue_as_new(
                    replace(
                        request,
                        campaign_id=campaign_id,
                        cell_offset=next_offset,
                        snapshot_blocker=snapshot_blocker,
                        root_blocker_classification=root_blocker_classification,
                    )
                )
            summary = await self._step(
                "campaign.finalize",
                {
                    "campaign_id": campaign_id,
                    "expected_cells": len(cells),
                    "terminal_blocker": (
                        snapshot_blocker
                        if snapshot_blocker == "BOOTSTRAP_VALIDATION_SUPERSEDED"
                        else None
                    ),
                },
            )
            return {
                "status": summary.get("status", "INCOMPLETE"),
                "campaign_id": campaign_id,
                "blocker": snapshot_blocker,
                "summary": summary,
            }
        if snapshot is None:
            raise RuntimeError("project snapshot result is missing")
        await self._step(
            "project.persist_snapshot",
            {
                "campaign_id": campaign_id,
                "project": request.project,
                "snapshot": snapshot,
            },
        )
        if plan.get("blockers"):
            return {
                "status": "BLOCKED_BY_CREDENTIAL",
                "campaign_id": campaign_id,
                "blockers": plan["blockers"],
                "plan": plan,
                "snapshot": snapshot,
            }
        e2e = "runtime" in repository_surfaces
        if e2e:
            pair_snapshot = await self._step(
                "project.snapshot_pair", {**asdict(request), "snapshot": snapshot}
            )
            await self._step(
                "project.persist_snapshot",
                {
                    "campaign_id": campaign_id,
                    "project": request.project,
                    "snapshot": snapshot,
                    "pair_snapshot": pair_snapshot,
                },
            )
            if pair_snapshot.get("status") in {"BLOCKED", "LIVE_REVISION_DRIFT"}:
                return {
                    "status": "BLOCKED",
                    "campaign_id": campaign_id,
                    "blocker": pair_snapshot.get("blocker", pair_snapshot.get("status")),
                    "snapshot": pair_snapshot,
                }
            snapshot = {**snapshot, "pair_snapshot": pair_snapshot}
        stage_context: dict[str, Any] = {
            **asdict(request),
            "campaign_id": campaign_id,
            "snapshot": snapshot,
            "plan": plan,
        }
        if request.preset == "falcone-intensive":
            await self._step("github.refresh_mirrors", {"project": "llmwiki"})
            stage_context["llmwiki_consumer_snapshot"] = await self._step(
                "project.snapshot",
                {
                    "project": "llmwiki",
                    "campaign_id": campaign_id,
                    "environment": "staging",
                },
            )
        stages = self._audit_stages(e2e=e2e, preset=request.preset)
        for stage in stages:
            if not await self._checkpoint(stage.upper().replace(".", "_")):
                return {"status": "CANCELLED", "campaign_id": campaign_id}
            stage_context = await self._step(stage, stage_context, timeout_minutes=120)
            if stage_context.get("status") in {
                "BLOCKED",
                "BLOCKED_BY_CREDENTIAL",
                "FAILED",
            }:
                return {
                    "status": "BLOCKED",
                    "campaign_id": campaign_id,
                    "blocker": stage_context.get("blocker", stage_context["status"]),
                    "context": stage_context,
                }
        if not cells:
            # NEW-14: a leaner project's manifest can legitimately produce zero domains/
            # personas/lenses for this preset (e.g. llmwiki under static-full, which was
            # built with falcone's richer set in mind) -- report it as a clean, diagnosed
            # outcome instead of calling persist_coverage with total=0, which it correctly
            # rejects as inconsistent input and crashes the workflow.
            return {
                "status": "BLOCKED",
                "campaign_id": campaign_id,
                "blocker": "NO_APPLICABLE_COVERAGE_CELLS",
            }
        await self._step(
            "campaign.persist_coverage",
            {"campaign_id": campaign_id, "cells": batch, "total": len(cells)},
        )
        for index, cell in enumerate(batch, start=request.cell_offset):
            if not await self._checkpoint(f"AUDIT_CELL_{index}"):
                return {"status": "CANCELLED", "campaign_id": campaign_id}
            deterministic = await self._step(
                "audit.deterministic_cell",
                {"campaign_id": campaign_id, "cell": cell, "snapshot": snapshot},
            )
            prepared = await self._step(
                "audit.prepare_agent_cell",
                {
                    "campaign_id": campaign_id,
                    "cell": cell,
                    "snapshot": snapshot,
                    "deterministic": deterministic,
                },
            )
            if prepared.get("status") in {"RATE_LIMITED", "BLOCKED"}:
                await self._step(
                    "campaign.persist_cell_blocker",
                    {
                        **prepared,
                        "deterministic_evidence_sha256": deterministic.get("evidence_sha256"),
                        "blocker": prepared.get("blocker", prepared["status"]),
                    },
                )
                continue
            agent_result = await self._agent_step(
                "audit.agent_cell",
                prepared,
                timeout_minutes=120,
            )
            if agent_result.get("status") in {
                "RATE_LIMITED",
                "BLOCKED",
                "FAILED",
                "TIMED_OUT",
                "CANCELLED",
            }:
                await self._step(
                    "campaign.persist_cell_blocker",
                    {
                        **agent_result,
                        "deterministic_evidence_sha256": deterministic.get("evidence_sha256"),
                        "blocker": agent_result.get("blocker", agent_result["status"]),
                    },
                )
                continue
            persisted = await self._step(
                "campaign.persist_cell_result",
                {
                    "campaign_id": campaign_id,
                    "cell": cell,
                    "agent_result": agent_result.get("agent_result"),
                    "deterministic_evidence_sha256": deterministic.get("evidence_sha256"),
                },
            )
            if persisted.get("status") == "CANDIDATE_FOUND":
                finding_id = str(persisted.get("finding_id", ""))
                if not finding_id:
                    raise ValueError("candidate coverage result did not return a finding ID")
                verification = await workflow.execute_child_workflow(
                    "FindingVerificationWorkflow",
                    FindingVerificationInput(
                        finding_id=finding_id,
                        project=request.project,
                        fresh_snapshot=True,
                    ),
                    id=f"finding-verify-{finding_id}",
                    task_queue=workflow.info().task_queue,
                    execution_timeout=timedelta(hours=4),
                    **self._child_run_options(f"finding-verify-{finding_id}"),
                )
                if verification.get("status") in {"RATE_LIMITED", "BLOCKED", "FAILED"}:
                    continue
                if verification.get("status") == "CONFIRMED" and request.publish_findings:
                    publication = await self._step(
                        "finding.prepare_publication",
                        {"finding_id": finding_id, "campaign_id": campaign_id},
                    )
                    if publication.get("status") == "PUBLICATION_READY":
                        publication = await self._github_write_step(
                            "finding.publish", {**publication, "dry_run": False}
                        )
                    if publication.get("status") == "PUBLISHED":
                        publication = await self._step("finding.persist_publication", publication)
                    if publication.get("status") not in {"PUBLISHED", "ALREADY_PUBLISHED"}:
                        return {
                            "status": "BLOCKED",
                            "campaign_id": campaign_id,
                            "blocker": publication.get("blocker", "FINDING_PUBLICATION_FAILED"),
                            "publication": publication,
                        }
        next_offset = request.cell_offset + len(batch)
        if next_offset < len(cells):
            workflow.continue_as_new(
                replace(
                    request,
                    campaign_id=campaign_id,
                    cell_offset=next_offset,
                    snapshot_blocker=None,
                )
            )
        summary = await self._step(
            "campaign.finalize", {"campaign_id": campaign_id, "expected_cells": len(cells)}
        )
        return {
            "status": summary.get("status", "INCOMPLETE"),
            "campaign_id": campaign_id,
            "summary": summary,
        }

    @staticmethod
    def _audit_stages(*, e2e: bool, preset: str) -> list[str]:
        stages = [
            "audit.static_source",
            "audit.static_deployment",
            "deployment.render",
        ]
        if e2e:
            stages.extend(
                [
                    "deployment.compare_live",
                    "audit.api_flow",
                    "audit.browser_flow",
                    "audit.kubernetes_observe",
                    "audit.correlate_runtime",
                ]
            )
        if preset == "falcone-intensive":
            stages.append("audit.llmwiki_consumer_suite")
        stages.extend(
            [
                "audit.classify_candidate",
                "audit.discover_enhancements",
                "audit.verify_cleanup",
            ]
        )
        return stages


@workflow.defn(name="MultiProjectAuditWorkflow")
class MultiProjectAuditWorkflow(ControlledWorkflow):
    """Execute the fixed project catalog sequentially under one durable parent."""

    _projects = ("falcone", "musematic", "llmwiki")
    _presets = frozenset(
        {
            "snapshot",
            "static-full",
            "e2e-smoke",
            "full-e2e",
            "security",
            "ux",
            "deployment",
        }
    )
    _child_statuses = frozenset(
        {
            "BLOCKED",
            "BLOCKED_BY_CREDENTIAL",
            "CANCELLED",
            "COMPLETE",
            "FAILED",
            "INCOMPLETE",
            "SUCCESS",
            "TIMED_OUT",
            "VERIFICATION_REQUIRED",
        }
    )

    @workflow.run
    async def run(self, request: MultiProjectAuditInput) -> dict[str, Any]:
        if (
            request.environment != "staging"
            or request.preset not in self._presets
            or set(request.campaign_ids) != set(self._projects)
            or set(request.child_workflow_ids) != set(self._projects)
        ):
            raise ApplicationError(
                "all-project audit binding is invalid",
                type="PolicyViolation",
                non_retryable=True,
            )
        if request.application_run_id is not None:
            try:
                if type(request.application_run_id) is not str:
                    raise ValueError
                parent_run_id = UUID(request.application_run_id)
                if str(parent_run_id) != request.application_run_id or parent_run_id.int == 0:
                    raise ValueError
            except (TypeError, ValueError, AttributeError):
                raise ApplicationError(
                    "all-project Run binding is invalid", type="PolicyViolation", non_retryable=True
                ) from None
            for project in self._projects:
                self._remember_child_run(
                    request.child_workflow_ids[project], str(uuid5(parent_run_id, project))
                )
        parent_workflow_id = workflow.info().workflow_id
        await self._step(
            "campaign.group_status",
            {
                "workflow_id": parent_workflow_id,
                "status": "RUNNING",
                "preset": request.preset,
                "environment": request.environment,
                "project_results": {},
            },
        )
        results: dict[str, dict[str, str | None]] = {}
        for project in self._projects:
            if not await self._checkpoint(f"AUDIT_{project.upper()}"):
                await self._step(
                    "campaign.group_status",
                    {
                        "workflow_id": parent_workflow_id,
                        "status": "CANCELLED",
                        "preset": request.preset,
                        "environment": request.environment,
                        "project_results": results,
                    },
                )
                return {"status": "CANCELLED", "project_results": results}
            try:
                child = await workflow.execute_child_workflow(
                    "AuditCampaignWorkflow",
                    AuditCampaignInput(
                        project=project,
                        name=f"{request.preset}-staging-all-projects",
                        preset=request.preset,
                        environment="staging",
                        dry_run=False,
                        publish_findings=request.publish_findings,
                        campaign_id=request.campaign_ids[project],
                    ),
                    id=request.child_workflow_ids[project],
                    task_queue=workflow.info().task_queue,
                    execution_timeout=timedelta(days=7),
                    **self._child_run_options(request.child_workflow_ids[project]),
                )
            except ChildWorkflowError:
                child = {"status": "FAILED", "blocker": "AUDIT_CHILD_WORKFLOW_FAILED"}
            if not isinstance(child, dict):
                child = {"status": "FAILED", "blocker": "AUDIT_CHILD_RESULT_INVALID"}
            child_status = str(child.get("status", "FAILED")).upper()
            raw_blocker = child.get("blocker")
            blocker = str(raw_blocker).upper() if raw_blocker is not None else None
            if child_status not in self._child_statuses:
                child_status = "FAILED"
                blocker = "UNKNOWN_AUDIT_CHILD_STATUS"
            if blocker is not None and (
                not 1 <= len(blocker) <= 120
                or blocker[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                or any(
                    character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
                    for character in blocker
                )
            ):
                blocker = "AUDIT_CHILD_BLOCKED"
            result = {
                "status": child_status,
                "campaign_id": request.campaign_ids[project],
                "workflow_id": request.child_workflow_ids[project],
                "blocker": blocker,
            }
            results[project] = result
            await self._step(
                "campaign.group_status",
                {
                    "workflow_id": parent_workflow_id,
                    "status": "RUNNING",
                    "preset": request.preset,
                    "environment": request.environment,
                    "current_project": project,
                    "project_results": results,
                },
            )
        child_statuses = {str(result["status"]).upper() for result in results.values()}
        if child_statuses & {"FAILED", "TIMED_OUT"}:
            terminal = "FAILED"
        elif child_statuses - {"COMPLETE", "VERIFICATION_REQUIRED", "SUCCESS"}:
            terminal = "BLOCKED"
        else:
            terminal = "SUCCESS"
        await self._step(
            "campaign.group_status",
            {
                "workflow_id": parent_workflow_id,
                "status": terminal,
                "preset": request.preset,
                "environment": request.environment,
                "project_results": results,
            },
        )
        return {"status": terminal, "project_results": results}


@workflow.defn(name="FindingVerificationWorkflow")
class FindingVerificationWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: FindingVerificationInput) -> dict[str, Any]:
        candidate = await self._step("finding.load_candidate", asdict(request))
        # NEW-217: a revalidated backlog issue carries its GitHub binding and approved body
        # hash. Candidates loaded before NEW-217 have neither, so their histories never
        # reach the marker and replay unchanged.
        if candidate.get("issue_body_sha256") and workflow.patched(
            "verification-refresh-issue-body-v1"
        ):
            candidate = await self._refresh_candidate_body(candidate)
            if candidate.get("status") == "BLOCKED":
                blocked = await self._step(
                    "finding.record_terminal",
                    {
                        "finding": candidate,
                        "status": "BLOCKED",
                        "blocker": ISSUE_BODY_CHANGED_SINCE_IMPORT,
                    },
                )
                return {"status": "BLOCKED", "context": blocked}
        fresh = await self._step("finding.fresh_snapshot", candidate)
        attempts: list[dict[str, Any]] = []
        for ordinal in (1, 2):
            if not await self._checkpoint(f"REPRODUCTION_{ordinal}"):
                return {"status": "CANCELLED"}
            prepared = await self._step(
                "finding.prepare_verifier",
                {"finding": candidate, "snapshot": fresh, "ordinal": ordinal},
            )
            if prepared.get("status") in {"RATE_LIMITED", "BLOCKED"}:
                prepared = await self._step("finding.record_terminal", prepared)
                return {"status": prepared["status"], "context": prepared}
            reproduced = await self._agent_step("finding.reproduce", prepared, timeout_minutes=60)
            if reproduced.get("status") != "SUCCESS":
                reproduced = await self._step("finding.record_terminal", reproduced)
                return {"status": reproduced.get("status", "FAILED"), "context": reproduced}
            attempts.append(reproduced)
        try:
            controls = await self._step(
                "finding.controls",
                {"finding": candidate, "snapshot": fresh, "attempts": attempts},
            )
        except Exception:
            # NEW-21, 2026-09-22: _evaluate_controls treats every other "can't
            # confirm" case (environment invalid, not reproduced, flaky) as a
            # graceful disposition -- but a verification attempt whose evidence
            # isn't anchored to the fresh snapshot's own hash raises a bare
            # ValueError instead, and this workflow (like IssueDeliveryWorkflow
            # before NEW-20) had no try/except at all, so that one case crashed
            # the entire parent campaign instead of just blocking this one
            # issue. Deliberately NOT weakening _evaluate_controls' own
            # anti-staleness check (it's a legitimate integrity gate); this
            # just makes its failure read like every other blocked verification
            # -- continue_after_blocked_issue already knows how to move on from
            # exactly this outcome.
            blocked = await self._step(
                "finding.record_terminal",
                {"finding": candidate, "status": "BLOCKED", "blocker": "VERIFICATION_INTEGRITY"},
            )
            return {"status": "BLOCKED", "context": blocked}
        duplicate = await self._step(
            "finding.dedupe",
            {"finding": candidate, "attempts": attempts, "controls": controls},
        )
        return await self._step(
            "finding.finalize",
            {
                "finding": candidate,
                "attempts": attempts,
                "controls": controls,
                "duplicate": duplicate,
            },
        )

    async def _refresh_candidate_body(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """NEW-217: revalidate against the issue's bounded full body, not the import's
        first 4000 characters.

        The body is read only under the hash it was imported or amended with, the same
        refresh delivery uses (NEW-44/NEW-112): any other text stops this revalidation,
        and the operator re-imports or amends. A read failure keeps the imported copy.
        """
        refreshed = await self._step(
            "delivery.refresh_issue_body",
            {
                "issue_repository": candidate.get("issue_repository"),
                "issue_number": candidate.get("issue_number"),
                "approved_issue_body_sha256": candidate["issue_body_sha256"],
            },
        )
        if refreshed.get("status") == "ISSUE_BODY_CHANGED_SINCE_APPROVAL":
            return {
                **candidate,
                "status": "BLOCKED",
                "blocker_detail": refreshed.get("blocker_detail"),
                "approved_body_sha256": refreshed.get("approved_body_sha256"),
                "current_body_sha256": refreshed.get("current_body_sha256"),
            }
        body = refreshed.get("body_excerpt")
        if refreshed.get("status") != "ISSUE_BODY_REFRESHED" or not isinstance(body, str):
            return candidate
        if not body:
            return candidate
        return {**candidate, "actual_behavior": body}


@workflow.defn(name="IssueDeliveryWorkflow")
class IssueDeliveryWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: IssueDeliveryInput) -> dict[str, Any]:
        self._risk_class = request.risk_class
        delivery_request = request.payload()
        context = await self._step("delivery.load_confirmed_finding", delivery_request)
        if context.get("status") == "BLOCKED":
            # 2b678d9: a durable approval/binding refusal ends the delivery before any
            # lease or worktree exists. Main's loader never returned BLOCKED (it raised),
            # so main histories never reach this return; it precedes the toolchain
            # marker so neither line's command sequence changes.
            return context
        # Makers and the checker have no network, so the scan worker installs the project
        # toolchain in the source worktree first -- after the prechecks, so the NEW-28
        # finding baseline never contains toolchain directories. Histories recorded
        # before this step existed carry no marker and replay without it.
        toolchain_steps: tuple[str, ...] = (
            ("delivery.prepare_toolchain",) if self._prepare_toolchain_patched() else ()
        )
        ordered_steps = (
            "delivery.ensure_issue",
            "delivery.acquire_leases",
            "delivery.route_providers",
            "delivery.allocate_worktrees",
            "delivery.persist_worktree_leases",
            "delivery.deterministic_prechecks",
            *toolchain_steps,
            "delivery.prepare_spec",
            "delivery.invoke_spec",
            "delivery.create_openspec",
            "delivery.prepare_maker",
            "delivery.invoke_maker",
            "delivery.prepare_deployment_maker",
            "delivery.invoke_deployment_maker",
            "delivery.prepare_checker",
            "delivery.invoke_checker",
            "delivery.validate_changeset",
            "delivery.prepare_publication",
            "delivery.publish",
            "delivery.persist_publication",
        )
        try:
            for step in ordered_steps:
                if not await self._checkpoint(step.upper().replace(".", "_")):
                    await self._release_delivery_leases_best_effort(context)
                    return {"status": "CANCELLED", "context": context}
                step_payload = {**context, "delivery_request": delivery_request}
                if step in {
                    "delivery.invoke_spec",
                    "delivery.invoke_maker",
                    "delivery.invoke_deployment_maker",
                    "delivery.invoke_checker",
                }:
                    context = await self._agent_step(step, step_payload, timeout_minutes=120)
                else:
                    if step == "delivery.publish":
                        context = await self._github_write_step(
                            step, step_payload, timeout_minutes=30
                        )
                    elif step == "delivery.prepare_toolchain":
                        # Up to 10 bounded installs of <= 3600 s each; the handler
                        # heartbeats while they run.
                        context = await self._step(step, step_payload, timeout_minutes=90)
                    elif step == "delivery.validate_changeset":
                        # NEW-49: may prepare a base-commit checkout (<= 80 min contract
                        # budget) and rerun failing test commands there; heartbeats. An
                        # activity timeout is not part of replay, so no patch marker.
                        context = await self._step(step, step_payload, timeout_minutes=120)
                    else:
                        context = await self._step(step, step_payload, timeout_minutes=30)
                if step == "delivery.ensure_issue" and context.get("status") == "ISSUE_READY":
                    context = await self._refresh_issue_body(context)
                if step == "delivery.invoke_maker":
                    context = await self._resolve_maker_dependencies(context, request)
                if (
                    step == "delivery.validate_changeset"
                    and context.get("status") == "REQUEST_CHANGES"
                    and context.get("validation_findings")
                    and workflow.patched("delivery-record-validation-findings-v1")
                ):
                    # NEW-76: the next correction attempt's maker is told what to fix.
                    await self._step("delivery.record_validation_findings", context)
                if (
                    context.get("status")
                    in {
                        "RATE_LIMITED",
                        "BLOCKED",
                        "FAILED",
                        "TIMED_OUT",
                        "CANCELLED",
                        "REQUEST_CHANGES",
                        "DRY_RUN",
                    }
                    and step != "delivery.publish"
                ):
                    if step == "delivery.prepare_publication":
                        context = await self._step("delivery.persist_handoff", context)
                    # NEW-20, 2026-09-22: every OTHER stop here (anything before
                    # delivery.persist_publication, whose own two branches already
                    # release) previously left this run's component-implementation
                    # lease held for its full 4h TTL, blocking every other delivery
                    # attempt at the same component -- including this same issue's
                    # own next correction-retry attempt, which mints a fresh run_id
                    # per attempt. Idempotent no-op if nothing was ever acquired.
                    await self._release_delivery_leases_best_effort(context)
                    return {"status": context["status"], "context": context}
                if (
                    step == "delivery.deterministic_prechecks"
                    and context.get("ui_page")
                    and self._ui_page_inputs_patched()
                ):
                    # NEW-227: a blueprint page's maker and checker get its legacy changes
                    # since the blueprint and a kit snapshot of the pinned gntik-ui (scan
                    # worker). Only for such a delivery: every other one never reaches the
                    # marker, so its history is unchanged.
                    if not await self._checkpoint("DELIVERY_PREPARE_UI_PAGE"):
                        await self._release_delivery_leases_best_effort(context)
                        return {"status": "CANCELLED", "context": context}
                    context = await self._step(
                        "delivery.prepare_ui_page",
                        {**context, "delivery_request": delivery_request},
                        timeout_minutes=30,
                    )
                    if context.get("status") == "BLOCKED":
                        await self._release_delivery_leases_best_effort(context)
                        return {"status": "BLOCKED", "context": context}
                if step == "delivery.invoke_deployment_maker" and self._pin_convergence_patched():
                    # NEW-118: the source maker runs first, so a deployment commit left the
                    # source CI pin of the deployment repository one commit behind
                    # (falcone #980/#45: ~6 checker rounds). The scan worker sets it to the
                    # deployment worktree HEAD before the checker sees the ChangeSet.
                    if not await self._checkpoint("DELIVERY_CONVERGE_DEPLOYMENT_PIN"):
                        await self._release_delivery_leases_best_effort(context)
                        return {"status": "CANCELLED", "context": context}
                    context = await self._step(
                        "delivery.converge_deployment_pin",
                        {**context, "delivery_request": delivery_request},
                        timeout_minutes=30,
                    )
                    if context.get("status") == "BLOCKED":
                        await self._release_delivery_leases_best_effort(context)
                        return {"status": "BLOCKED", "context": context}
            return {"status": context.get("status", "SOURCE_PR"), "context": context}
        except Exception:
            # NEW-20, 2026-09-22: an uncaught exception anywhere in this pipeline
            # (e.g. NEW-19's semgrep crash) skipped delivery.persist_publication
            # entirely, so nothing ever released this run's lease -- reproduced
            # live when exactly that crash blocked the very next retry's attempt
            # at the same issue for the rest of the lease's 4h TTL.
            await self._release_delivery_leases_best_effort(context)
            raise

    # NEW-75: at most this many platform dependency resolutions per delivery attempt.
    _DEPENDENCY_ROUNDS = 2

    async def _resolve_maker_dependencies(
        self, context: dict[str, Any], request: IssueDeliveryInput
    ) -> dict[str, Any]:
        """NEW-75: the offline maker edited manifests and asked for lockfile resolution.

        The scan worker (network) regenerates and commits the lockfiles and reinstalls
        the toolchain; the maker is then invoked again on top of that commit.
        """
        rounds = 0
        while rounds < self._DEPENDENCY_ROUNDS and self._maker_decision(context) == (
            "DEPENDENCIES_REQUIRED"
        ):
            if not workflow.patched("delivery-resolve-dependencies-v1"):
                break
            rounds += 1
            delivery_request = request.payload()
            resolved = await self._step(
                "delivery.resolve_dependencies",
                {**context, "delivery_request": delivery_request},
                timeout_minutes=120,
            )
            if resolved.get("status") != "DEPENDENCIES_RESOLVED":
                return resolved
            # NEW-77: the previous round's provider result must not travel into the next
            # invocation; on a provider failure agent.persist_result read it back and
            # rejected its identity (round 1 id vs the round 2 request).
            fresh = {
                key: value
                for key, value in resolved.items()
                if key not in {"agent_result", "agent_invocation_id", "agent_invocation_status"}
            }
            prepared = await self._step(
                "delivery.prepare_maker",
                {**fresh, "dependency_round": rounds, "delivery_request": delivery_request},
                timeout_minutes=30,
            )
            if prepared.get("status") != "SOURCE_MAKER_READY":
                return prepared
            context = await self._agent_step(
                "delivery.invoke_maker",
                {**prepared, "delivery_request": delivery_request},
                timeout_minutes=120,
            )
        return context

    @staticmethod
    def _maker_decision(context: dict[str, Any]) -> str:
        agent = context.get("agent_result")
        structured = agent.get("structured_result") if isinstance(agent, dict) else None
        return str(structured.get("decision", "")) if isinstance(structured, dict) else ""

    async def _refresh_issue_body(self, context: dict[str, Any]) -> dict[str, Any]:
        # NEW-44: the Finding copied an existing backlog issue's body once at import,
        # so decisions the operator later adds to the issue never reached the
        # spec/maker. Re-read it here; only actual_behavior is taken from the result,
        # and any failure keeps the imported copy. Histories recorded before this
        # step existed carry no marker and replay without it.
        if context.get("persona") != "existing-github-issue" or not context.get("issue_number"):
            return context
        if getattr(self, "_risk_class", None) == "R3":
            if workflow.patched("delivery-r3-approved-full-body-v1"):
                # NEW-210: the imported copy keeps only the issue's first 4000 characters,
                # so amendments appended to a long R3 issue never reached its spec/maker.
                # An R3 delivery now re-reads the body only under its approved hash: the
                # refresh refuses any other text (ISSUE_BODY_CHANGED_SINCE_APPROVAL), so the
                # maker still works only from the exact text the operator approved.
                if context.get("approved_issue_body_sha256") is None:
                    return context
            elif workflow.patched("delivery-r3-approved-issue-body-v1"):
                # NEW-95 review: an R3 delivery works only from the issue text the operator
                # approved (the imported copy bound by its R3 candidate); a later edit must
                # be re-imported and re-approved rather than reaching the maker unapproved.
                return context
        if not workflow.patched("delivery-refresh-issue-body-v1"):
            return context
        refresh_request: dict[str, Any] = {
            "issue_repository": context.get("issue_repository"),
            "issue_number": context.get("issue_number"),
        }
        if context.get("approved_issue_body_sha256") is not None:
            refresh_request["approved_issue_body_sha256"] = context["approved_issue_body_sha256"]
        refreshed = await self._step("delivery.refresh_issue_body", refresh_request)
        if refreshed.get("status") == "ISSUE_BODY_CHANGED_SINCE_APPROVAL" and workflow.patched(
            "delivery-refresh-body-approval-scope-v1"
        ):
            # NEW-112 review: an issue edited after approval stops this delivery (and
            # releases its leases) with the operator's re-approval guidance; only the
            # approved text may reach the spec/maker. Older histories never see this status.
            return {
                **context,
                "status": "BLOCKED",
                "blocker": "ISSUE_BODY_CHANGED_SINCE_APPROVAL",
                "blocker_detail": refreshed.get("blocker_detail"),
                "approved_body_sha256": refreshed.get("approved_body_sha256"),
                "current_body_sha256": refreshed.get("current_body_sha256"),
            }
        body = refreshed.get("body_excerpt")
        if refreshed.get("status") != "ISSUE_BODY_REFRESHED" or not isinstance(body, str):
            return context
        if not body:
            return context
        return {**context, "actual_behavior": body}

    def _prepare_toolchain_patched(self) -> bool:
        return workflow.patched("delivery-prepare-toolchain-v1")

    def _ui_page_inputs_patched(self) -> bool:
        # Checked once, after the prechecks of a UI page delivery (NEW-227).
        return workflow.patched("delivery-ui-page-inputs-v1")

    def _pin_convergence_patched(self) -> bool:
        # Checked once, right after the deployment maker; histories recorded before
        # NEW-118 carry no marker there and replay without the step.
        return workflow.patched("delivery-deployment-pin-convergence-v1")

    async def _release_delivery_leases_best_effort(self, context: dict[str, Any]) -> None:
        # Discards the activity's own response payload deliberately: it returns
        # {**context, "status": "LEASES_RELEASED"|"NO_RUN_TO_RELEASE_LEASES_FOR"},
        # and merging that back would silently clobber context["status"] (e.g.
        # "BLOCKED"/"REQUEST_CHANGES") that callers and result records depend on.
        await self._step("delivery.release_leases", context)


@workflow.defn(name="DeliveryReconciliationWorkflow")
class DeliveryReconciliationWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: DeliveryReconciliationInput) -> dict[str, Any]:
        context: dict[str, Any] = asdict(request)
        ordered = (
            "reconcile.prepare",
            "reconcile.inspect_ci",
            "reconcile.persist_ci",
        )
        for step in ordered:
            if not await self._checkpoint(step.upper().replace(".", "_")):
                return {"status": "CANCELLED", "context": context}
            context = await self._step(step, context, timeout_minutes=30)
        if context.get("status") != "CANDIDATE_BUILT":
            return {"status": context.get("status", "BLOCKED"), "context": context}

        if not await self._checkpoint("RECONCILE_PREPARE_PROMOTION"):
            return {"status": "CANCELLED", "context": context}
        context = await self._step("reconcile.prepare_promotion", context)
        if context.get("status") == "PROMOTION_PUBLICATION_READY":
            pass
        elif context.get("status") != "PROMOTION_READY":
            return {"status": context.get("status", "BLOCKED"), "context": context}
        else:
            if not await self._checkpoint("RECONCILE_APPLY_PROMOTION"):
                return {"status": "CANCELLED", "context": context}
            context = await self._step("reconcile.apply_promotion", context, timeout_minutes=60)
            context = await self._step("reconcile.persist_promotion", context)
            if context.get("status") != "PROMOTION_PUBLICATION_READY":
                return {"status": context.get("status", "BLOCKED"), "context": context}

        if not await self._checkpoint("RECONCILE_PUBLISH_PROMOTION"):
            return {"status": "CANCELLED", "context": context}
        context = await self._github_write_step(
            "reconcile.publish_promotion", context, timeout_minutes=30
        )
        context = await self._step("reconcile.persist_promotion_publication", context)
        if context.get("status") != "RELEASE_REVIEW_REQUIRED":
            return {"status": context.get("status", "BLOCKED"), "context": context}
        if not await self._checkpoint("RECONCILE_RELEASE_REVIEW"):
            return {"status": "CANCELLED", "context": context}
        context = await self._step("reconcile.prepare_release_review", context)
        if context.get("status") != "RELEASE_REVIEW_READY":
            return {"status": context.get("status", "BLOCKED"), "context": context}
        context = await self._agent_step(
            "reconcile.invoke_release_reviewer", context, timeout_minutes=90
        )
        if context.get("agent_invocation_status") != "SUCCESS":
            return {"status": context.get("status", "BLOCKED"), "context": context}
        context = await self._step("reconcile.persist_release_review", context)
        return {"status": context.get("status", "DEPLOY_PR"), "context": context}


@workflow.defn(name="FinalClosureWorkflow")
class FinalClosureWorkflow(ControlledWorkflow):
    """Close one imported issue only after exact protected-merge verification."""

    @workflow.run
    async def run(self, request: FinalClosureInput) -> dict[str, Any]:
        hardened_recovery = workflow.patched("final-closure-recovery-v2")
        context: dict[str, Any] = asdict(request)
        if not await self._checkpoint("FINAL_PREPARE"):
            return {"status": "CANCELLED", "context": context}
        context = await self._step("final.prepare", context, timeout_minutes=30)
        if context.get("status") == "FIXED":
            return {"status": "FIXED", "context": context}
        if context.get("status") == "FINAL_ISSUE_CLOSE_READY":
            return await self._resume_final_issue_close(
                context, hardened_recovery=hardened_recovery
            )
        if context.get("status") == "FINAL_DEPLOYMENT_MERGE_PERSISTED":
            return await self._resume_after_final_deployment_merge(
                request,
                context,
                hardened_recovery=hardened_recovery,
            )
        source_merge_persisted = context.get("status") == "FINAL_SOURCE_MERGE_PERSISTED"
        if not source_merge_persisted and context.get("status") != "FINAL_CLOSURE_READY":
            return {"status": context.get("status", "BLOCKED"), "context": context}

        if not source_merge_persisted:
            if not await self._checkpoint("FINAL_MERGE_SOURCE"):
                return {"status": "CANCELLED", "context": context}
            context = await self._github_write_step(
                "final.merge_source", context, timeout_minutes=30
            )
            if context.get("status") in {"BLOCKED", "FAILED", "CANCELLED"}:
                return {"status": context["status"], "context": context}
            # A protected merge is irreversible. New histories must persist its exact result
            # before honoring cancellation; the patch-false branch preserves old histories.
            if not hardened_recovery and not await self._checkpoint("FINAL_PERSIST_SOURCE_MERGE"):
                return {"status": "CANCELLED", "context": context}
            context = await self._step("final.persist_source_merge", context, timeout_minutes=30)
            if context.get("status") in {"BLOCKED", "FAILED", "CANCELLED"}:
                return {"status": context["status"], "context": context}

        for attempt in range(120):
            if hardened_recovery and not await self._checkpoint("FINAL_WAIT_FOR_BUILD"):
                return {"status": "CANCELLED", "context": context}
            context = await self._step("final.inspect_artifact", context, timeout_minutes=30)
            if context.get("status") == "FINAL_ARTIFACT_VERIFIED":
                break
            if not hardened_recovery:
                return {
                    "status": "BLOCKED",
                    "blocker": context.get("blocker", "FINAL_BUILD_NOT_YET_AVAILABLE"),
                    "context": context,
                }
            if (
                context.get("status") != "AWAITING_FINAL_BUILD"
                or context.get("blocker") != "FINAL_BUILD_NOT_YET_AVAILABLE"
            ):
                return {
                    "status": "BLOCKED",
                    "blocker": context.get("blocker", "FINAL_BUILD_RECONCILIATION_FAILED"),
                    "context": context,
                }
            if attempt < 119:
                await workflow.sleep(timedelta(minutes=1))
        else:
            return {
                "status": "BLOCKED",
                "blocker": "FINAL_BUILD_WAIT_EXHAUSTED",
                "context": context,
            }
        if context.get("status") != "FINAL_ARTIFACT_VERIFIED":
            return {
                "status": "BLOCKED",
                "blocker": context.get("blocker", "FINAL_BUILD_NOT_YET_AVAILABLE"),
                "context": context,
            }
        context = await self._step("final.persist_artifact", context)
        if context.get("status") != "FINAL_ARTIFACT_BUILT":
            return {"status": context.get("status", "BLOCKED"), "context": context}

        context = await self._step("reconcile.prepare_promotion", context)
        if context.get("status") != "DEPLOY_PR":
            if context.get("status") == "PROMOTION_READY":
                context = await self._step("reconcile.apply_promotion", context, timeout_minutes=60)
                context = await self._step("reconcile.persist_promotion", context)
            if context.get("status") != "PROMOTION_PUBLICATION_READY":
                return {"status": context.get("status", "BLOCKED"), "context": context}
            context = await self._github_write_step(
                "reconcile.publish_promotion", context, timeout_minutes=30
            )
            context = await self._step("reconcile.persist_promotion_publication", context)
            if context.get("status") != "RELEASE_REVIEW_REQUIRED":
                return {"status": context.get("status", "BLOCKED"), "context": context}
            context = await self._step("reconcile.prepare_release_review", context)
            if context.get("status") != "RELEASE_REVIEW_READY":
                return {"status": context.get("status", "BLOCKED"), "context": context}
            context = await self._agent_step(
                "reconcile.invoke_release_reviewer", context, timeout_minutes=90
            )
            if context.get("agent_invocation_status") != "SUCCESS":
                return {"status": context.get("status", "BLOCKED"), "context": context}
            context = await self._step("reconcile.persist_release_review", context)
            if context.get("status") != "DEPLOY_PR":
                return {"status": context.get("status", "BLOCKED"), "context": context}

        if hardened_recovery and not await self._checkpoint("FINAL_MERGE_DEPLOYMENT"):
            return {"status": "CANCELLED", "context": context}
        context = await self._step("final.authorize_deployment_merge", context)
        if context.get("status") != "FINAL_DEPLOYMENT_MERGE_AUTHORIZED":
            return {"status": context.get("status", "BLOCKED"), "context": context}
        context = await self._github_write_step(
            "final.merge_deployment", context, timeout_minutes=30
        )
        if context.get("status") != "DEPLOYMENT_MERGED":
            return {"status": context.get("status", "BLOCKED"), "context": context}
        context = await self._step("final.persist_deployment_merge", context)
        if context.get("status") != "FINAL_DEPLOYMENT_MERGE_PERSISTED":
            return {"status": context.get("status", "BLOCKED"), "context": context}
        return await self._resume_after_final_deployment_merge(
            request, context, hardened_recovery=hardened_recovery
        )

    async def _resume_after_final_deployment_merge(
        self,
        request: FinalClosureInput,
        context: dict[str, Any],
        *,
        hardened_recovery: bool,
    ) -> dict[str, Any]:
        if hardened_recovery and not await self._checkpoint("FINAL_SYNC_WORKTREES"):
            return {"status": "CANCELLED", "context": context}
        context = await self._step("final.sync_worktrees", context, timeout_minutes=30)
        if context.get("status") != "FINAL_WORKTREES_SYNCHRONIZED":
            return {"status": context.get("status", "BLOCKED"), "context": context}

        task_queue = workflow.info().task_queue
        staging_id = f"{workflow.info().workflow_id}-final-staging"
        if hardened_recovery and not await self._checkpoint("FINAL_STAGING_DEPLOYMENT"):
            return {"status": "CANCELLED", "context": context}
        await self._step(
            "backlog.register_child_run",
            {
                **asdict(request),
                "child_kind": "staging-deployment",
                "child_workflow_id": staging_id,
            },
        )
        staging = await workflow.execute_child_workflow(
            "StagingDeploymentWorkflow",
            StagingDeploymentInput(
                change_set_id=request.change_set_id,
                environment="staging",
                dry_run=False,
            ),
            id=staging_id,
            task_queue=task_queue,
            execution_timeout=timedelta(hours=12),
            **self._child_run_options(staging_id),
        )
        if staging.get("status") != "STAGING_DEPLOYED":
            return {
                "status": "BLOCKED",
                "blocker": staging.get("blocker") or staging.get("status"),
                "context": context,
            }
        staging_context = staging.get("context")
        deployment_id = (
            str(staging_context.get("deployment_id", ""))
            if isinstance(staging_context, dict)
            else ""
        )
        if not deployment_id:
            return {"status": "BLOCKED", "blocker": "FINAL_DEPLOYMENT_BINDING_MISSING"}
        verification_id = f"{workflow.info().workflow_id}-final-runtime"
        if hardened_recovery and not await self._checkpoint("FINAL_RUNTIME_VERIFICATION"):
            return {"status": "CANCELLED", "context": context}
        await self._step(
            "backlog.register_child_run",
            {
                **asdict(request),
                "child_kind": "runtime-verification",
                "child_workflow_id": verification_id,
                "deployment_id": deployment_id,
            },
        )
        verified = await workflow.execute_child_workflow(
            "RuntimeVerificationWorkflow",
            RuntimeVerificationInput(deployment_id=deployment_id, project=request.project),
            id=verification_id,
            task_queue=task_queue,
            execution_timeout=timedelta(hours=12),
            **self._child_run_options(verification_id),
        )
        if verified.get("status") != "RUNTIME_VERIFIED":
            return {
                "status": "BLOCKED",
                "blocker": verified.get("blocker") or verified.get("status"),
                "context": context,
            }
        if hardened_recovery and not await self._checkpoint("FINAL_PREPARE_ISSUE_CLOSE"):
            return {"status": "CANCELLED", "context": context}
        context = await self._step(
            "final.prepare_issue_close", {**context, "deployment_id": deployment_id}
        )
        if context.get("status") != "FINAL_ISSUE_CLOSE_READY":
            return {"status": context.get("status", "BLOCKED"), "context": context}
        return await self._resume_final_issue_close(context, hardened_recovery=hardened_recovery)

    async def _resume_final_issue_close(
        self, context: dict[str, Any], *, hardened_recovery: bool
    ) -> dict[str, Any]:
        if hardened_recovery and not await self._checkpoint("FINAL_COMMENT_ISSUE"):
            return {"status": "CANCELLED", "context": context}
        context = await self._github_write_step("final.comment_issue", context)
        if context.get("status") != "FINAL_EVIDENCE_COMMENTED":
            return {"status": context.get("status", "BLOCKED"), "context": context}
        context = await self._step("final.persist_comment", context)
        if context.get("status") != "FINAL_EVIDENCE_PERSISTED":
            return {"status": context.get("status", "BLOCKED"), "context": context}
        if hardened_recovery and not await self._checkpoint("FINAL_CLOSE_ISSUE"):
            return {"status": "CANCELLED", "context": context}
        context = await self._github_write_step("final.close_issue", context)
        if context.get("status") != "ISSUE_CLOSED":
            return {"status": context.get("status", "BLOCKED"), "context": context}
        context = await self._step("final.persist_issue_close", context)
        return {"status": context.get("status", "FIXED"), "context": context}


@workflow.defn(name="PreviewDeploymentWorkflow")
class PreviewDeploymentWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: PreviewDeploymentInput) -> dict[str, Any]:
        if request.project == "llmwiki":
            raise ApplicationError(
                "LLMWiki rejects preview and production targets",
                type="PolicyViolation",
                non_retryable=True,
            )
        context: dict[str, Any] = asdict(request)
        try:
            for step in ("preview.prepare", "preview.preflight"):
                if not await self._checkpoint(step.upper().replace(".", "_")):
                    context = await self._step(
                        "deployment.record_terminal",
                        {**context, "terminal_status": "CANCELLED"},
                    )
                    return {"status": "CANCELLED", "context": context}
                context = await self._step(step, context, timeout_minutes=60)
                if context.get("status") in {"BLOCKED", "FAILED", "CANCELLED"}:
                    return {"status": context["status"], "context": context}
            if request.dry_run:
                context = await self._step(
                    "deployment.record_terminal",
                    {**context, "terminal_status": "PREVIEW_PREFLIGHT_VALIDATED"},
                )
                return {"status": "PREVIEW_PREFLIGHT_VALIDATED", "context": context}
            context = await self._step("preview.record_candidate", context)
            if context.get("status") == "BLOCKED":
                return {"status": "BLOCKED", "context": context}
            automatic_approval = context.get("automatic_approval")
            if isinstance(automatic_approval, dict):
                self._approval = automatic_approval
            else:
                self._phase = "AWAITING_PREVIEW_APPROVAL"
                await workflow.wait_condition(lambda: self._approval is not None or self._cancelled)
                if self._cancelled:
                    context = await self._step(
                        "deployment.record_terminal",
                        {**context, "terminal_status": "CANCELLED"},
                    )
                    return {"status": "CANCELLED", "context": context}
            context = await self._step(
                "preview.validate_approval",
                {"context": context, "approval": self._approval},
            )
            if context.get("status") != "PREVIEW_APPROVED":
                return {"status": context.get("status", "BLOCKED"), "context": context}
            context = await self._step("preview.deploy", context, timeout_minutes=60)
            context = await self._step("preview.verify", context, timeout_minutes=60)
            context = await self._step("preview.finalize", context)
            return {"status": context.get("status", "PREVIEW_VERIFIED"), "context": context}
        except ActivityError as error:
            context = await self._deployment_failure(context, error)
            return {"status": "DEPLOYMENT_FAILED", "context": context}


@workflow.defn(name="StagingDeploymentWorkflow")
class StagingDeploymentWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: StagingDeploymentInput) -> dict[str, Any]:
        context: dict[str, Any] = asdict(request)
        try:
            if not await self._checkpoint("STAGING_PREPARE"):
                context = await self._step(
                    "deployment.record_terminal", {**context, "terminal_status": "CANCELLED"}
                )
                return {"status": "CANCELLED", "context": context}
            context = await self._step("staging.prepare", context)
            if context.get("status") == "BLOCKED":
                return {"status": "BLOCKED", "context": context}
            if not await self._checkpoint("STAGING_PREFLIGHT"):
                context = await self._step(
                    "deployment.record_terminal", {**context, "terminal_status": "CANCELLED"}
                )
                return {"status": "CANCELLED", "context": context}
            context = await self._step("staging.preflight", context, timeout_minutes=60)
            if context.get("status") in {"BLOCKED", "FAILED"}:
                return {"status": context["status"], "context": context}
            if request.dry_run:
                context = await self._step(
                    "deployment.record_terminal",
                    {**context, "terminal_status": "STAGING_PREFLIGHT_VALIDATED"},
                )
                return {"status": "STAGING_PREFLIGHT_VALIDATED", "context": context}
            context = await self._step("staging.record_candidate", context)
            if context.get("status") == "BLOCKED":
                context = await self._step(
                    "deployment.record_terminal",
                    {
                        **context,
                        "terminal_status": "BLOCKED",
                        "blocker": context.get("blocker", "STAGING_DEPLOYMENT_BLOCKED"),
                    },
                )
                return {"status": "BLOCKED", "context": context}
            automatic_approval = context.get("automatic_approval")
            if isinstance(automatic_approval, dict):
                self._approval = automatic_approval
            else:
                self._phase = "AWAITING_STAGING_APPROVAL"
                await workflow.wait_condition(lambda: self._approval is not None or self._cancelled)
                if self._cancelled:
                    context = await self._step(
                        "deployment.record_terminal",
                        {**context, "terminal_status": "CANCELLED"},
                    )
                    return {"status": "CANCELLED", "context": context}
            context = await self._step(
                "staging.validate_approval", {"context": context, "approval": self._approval}
            )
            if context.get("status") != "STAGING_APPROVED":
                return {"status": context.get("status", "BLOCKED"), "context": context}
            context = await self._step("staging.deploy", context, timeout_minutes=60)
            context = await self._step("staging.finalize", context)
            return {"status": context.get("status", "STAGING_DEPLOYED"), "context": context}
        except ActivityError as error:
            context = await self._deployment_failure(context, error)
            return {"status": "DEPLOYMENT_FAILED", "context": context}


@workflow.defn(name="RuntimeVerificationWorkflow")
class RuntimeVerificationWorkflow(ControlledWorkflow):
    _reported_runtime_errors = frozenset(
        {
            "MANUAL_AUTH_REQUIRED_CODEX",
            "MANUAL_AUTH_REQUIRED_CLAUDE",
            "PROVIDER_CLI_MISSING_CODEX",
            "PROVIDER_CLI_MISSING_CLAUDE",
            "RATE_LIMITED",
            "RuntimeObservationUnavailable",
            "BrowserRunnerFailure",
            "SecurityBlocker",
            "PolicyViolation",
            "SchemaViolation",
        }
    )

    @workflow.run
    async def run(self, request: RuntimeVerificationInput) -> dict[str, Any]:
        context: dict[str, Any] = asdict(request)
        try:
            for step in (
                "runtime.snapshot",
                "runtime.deterministic_checks",
                "runtime.browser_checks",
            ):
                if not await self._checkpoint(step.upper().replace(".", "_")):
                    context = await self._step(
                        "runtime.record_failure",
                        {**context, "blocker": "CANCELLED"},
                    )
                    return {"status": "CANCELLED", "context": context}
                context = await self._step(step, context, timeout_minutes=60)
                if step == "runtime.snapshot" and context.get("status") in {
                    "BLOCKED",
                    "RATE_LIMITED",
                }:
                    return {"status": context["status"], "context": context}
            context = await self._step("runtime.prepare_review", context)
            requests = context.get("runtime_review_requests")
            if not isinstance(requests, list) or not requests:
                raise ValueError("runtime review preparation returned no requests")
            review_results: list[dict[str, Any]] = []
            for request_value in requests:
                if not isinstance(request_value, dict):
                    raise TypeError("runtime review request must be an object")
                if not await self._checkpoint("RUNTIME_INDEPENDENT_REVIEW"):
                    context = await self._step(
                        "runtime.record_failure",
                        {**context, "blocker": "CANCELLED"},
                    )
                    return {"status": "CANCELLED", "context": context}
                reviewed = await self._agent_step(
                    "runtime.independent_review",
                    {
                        **context,
                        "provider": request_value.get("provider"),
                        "runtime_review_request": request_value,
                    },
                    timeout_minutes=60,
                )
                review_status = reviewed.get("status")
                if review_status != "SUCCESS":
                    blocker = (
                        "RATE_LIMITED"
                        if review_status == "RATE_LIMITED"
                        else "RUNTIME_REVIEW_FAILED"
                    )
                    context = await self._step(
                        "runtime.record_failure",
                        {**context, "blocker": blocker},
                    )
                    return {"status": context.get("status", "BLOCKED"), "context": context}
                review_results.append(reviewed)
            context = await self._step(
                "runtime.finalize",
                {**context, "runtime_review_results": review_results},
            )
            return {"status": context.get("status", "FAILED"), "context": context}
        except ActivityError as error:
            cause = error.cause
            blocker = (
                cause.type
                if isinstance(cause, ApplicationError)
                and cause.type in self._reported_runtime_errors
                else "RUNTIME_ACTIVITY_FAILED"
            )
            context = await self._step(
                "runtime.record_failure",
                {**context, "blocker": blocker},
            )
            return {"status": context.get("status", "BLOCKED"), "context": context}


@workflow.defn(name="RecoveryWorkflow")
class RecoveryWorkflow(ControlledWorkflow):
    _reported_recovery_errors = frozenset(
        {
            "ApprovalMismatch",
            "PolicyViolation",
            "ProtectedRecoveryPRRequired",
        }
    )

    @workflow.run
    async def run(self, request: RecoveryInput) -> dict[str, Any]:
        context: dict[str, Any] = asdict(request)
        try:
            context = await self._step("recovery.inspect", context)
            if context.get("status") == "BLOCKED":
                return {"status": "BLOCKED", "context": context}
            context = await self._step("recovery.validate_binding", context)
            if request.dry_run or context.get("status") == "FORWARD_RECOVERY_REQUIRED":
                return {
                    "status": context.get("status", "RECOVERY_PLAN_VALIDATED"),
                    "context": context,
                }
            context = await self._step("recovery.prepare_candidate", context)
            if context.get("status") == "BLOCKED":
                return {"status": "BLOCKED", "context": context}
            self._phase = "AWAITING_RECOVERY_APPROVAL"
            await workflow.wait_condition(lambda: self._approval is not None or self._cancelled)
            if self._cancelled:
                context = await self._step(
                    "recovery.record_failure", {**context, "blocker": "CANCELLED"}
                )
                return {"status": "CANCELLED", "context": context}
            context = await self._step(
                "recovery.validate_approval",
                {"context": context, "approval": self._approval or {}},
            )
            if context.get("status") != "RECOVERY_APPROVED":
                return {"status": context.get("status", "BLOCKED"), "context": context}
            context = await self._step("recovery.execute", context, timeout_minutes=60)
            verified = await self._step("recovery.verify", context, timeout_minutes=60)
            return {"status": verified.get("status", "RECOVERED"), "context": verified}
        except ActivityError as error:
            cause = error.cause
            blocker = (
                cause.type
                if isinstance(cause, ApplicationError)
                and cause.type in self._reported_recovery_errors
                else "RECOVERY_ACTIVITY_FAILED"
            )
            context = await self._step("recovery.record_failure", {**context, "blocker": blocker})
            return {"status": context.get("status", "BLOCKED"), "context": context}


@workflow.defn(name="QuotaRefreshWorkflow")
class QuotaRefreshWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: QuotaRefreshInput) -> dict[str, Any]:
        snapshots = []
        for provider in request.providers:
            snapshots.append(await self._step("quota.refresh", {"provider": provider}))
        await self._step("quota.persist", {"snapshots": snapshots})
        return {"status": "COMPLETE", "snapshots": snapshots}


@workflow.defn(name="ConnectionsRefreshWorkflow")
class ConnectionsRefreshWorkflow(ControlledWorkflow):
    """NEW-05: keep the connection-health snapshot from decaying to DEGRADED between

    missions by periodically re-running the same bounded read-only probes
    `recheck-connections` already performs on demand.
    """

    @workflow.run
    async def run(self, request: ConnectionsRefreshInput) -> dict[str, Any]:
        result = await self._step("connections.refresh", {"scope": request.scope})
        return {"status": "COMPLETE", "scope": request.scope, "result": result}


@workflow.defn(name="CleanupWorkflow")
class CleanupWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: CleanupInput) -> dict[str, Any]:
        context: dict[str, Any] = asdict(request)
        try:
            context = await self._step("cleanup.discover", context)
            context = await self._step("cleanup.protect_active", context)
            context = await self._step("cleanup.inspect_evidence_files", context)
            context = await self._step("cleanup.inspect_delivery_files", context)
            context = await self._step("cleanup.inspect_browser_files", context)
            context = await self._step("identity.inspect_stale", context)
            context = await self._step("cleanup.inspect_codex_files", context)
            context = await self._step("cleanup.inspect_preview_namespaces", context)
            blocked_projects: dict[str, str] = {}
            if workflow.patched("cleanup-inspection-outcome-v1") and context.get(
                "identity_recovery_blockers"
            ):
                scope = (
                    scope_identity_blockers(context)
                    if self._identity_project_scope_patched()
                    else None
                )
                if scope is None or scope.blocked_all:
                    blocked_finalization = {
                        **context,
                        "terminal_status": "BLOCKED",
                        "blocker": "CLEANUP_IDENTITY_INSPECTION_BLOCKED",
                    }
                    if scope is not None:
                        blocked_finalization["cleanup_blocked_projects"] = scope.blocked_projects
                    context = await self._step("cleanup.finalize", blocked_finalization)
                    return {"status": "BLOCKED", "context": context}
                # NEW-119: only the blocked projects' candidates are withheld (recorded
                # as protected); every other project's cleanup continues.
                context = scope.context
                blocked_projects = scope.blocked_projects
            if request.dry_run:
                if workflow.patched("cleanup-orphan-worktrees-v1"):
                    # NEW-85: report snapshot/acceptance leftovers no lease tracks.
                    context = await self._step(
                        "cleanup.prune_orphan_worktrees", {**context, "dry_run": True}
                    )
                context = await self._step(
                    "cleanup.finalize", {**context, "terminal_status": "DRY_RUN"}
                )
                if blocked_projects:
                    return {
                        "status": "DRY_RUN",
                        "blocked_projects": blocked_projects,
                        "context": context,
                    }
                return {"status": "DRY_RUN", "context": context}
            context = await self._step("cleanup.authorize", context)
            if context.get("status") == "BLOCKED":
                context = await self._step(
                    "cleanup.finalize", {**context, "terminal_status": "BLOCKED"}
                )
                return {"status": "BLOCKED", "context": context}
            if blocked_projects:
                # NEW-119: on the scoped path identity recovery runs before any removal,
                # so a project that fails recovery is withheld before anything is removed.
                context = await self._step("identity.recover_stale", {**context, "dry_run": False})
                if context.get("identity_recovery_blockers") or context.get("status") != "PASS":
                    rescope = rescope_after_recovery(context, blocked_projects)
                    if rescope.blocked_all:
                        context = await self._step(
                            "cleanup.finalize",
                            {
                                **context,
                                "terminal_status": "BLOCKED",
                                "blocker": "CLEANUP_IDENTITY_RECOVERY_BLOCKED",
                            },
                        )
                        return {"status": "BLOCKED", "context": context}
                    blocked_projects = rescope.blocked_projects
                    # The withheld candidates leave the authorization: authorize again.
                    context = await self._step("cleanup.authorize", rescope.context)
                    if context.get("status") == "BLOCKED":
                        context = await self._step(
                            "cleanup.finalize", {**context, "terminal_status": "BLOCKED"}
                        )
                        return {"status": "BLOCKED", "context": context}
            context = await self._step(
                "cleanup.execute_evidence_files", {**context, "dry_run": False}
            )
            context = await self._step(
                "cleanup.execute_delivery_files", {**context, "dry_run": False}
            )
            context = await self._step(
                "cleanup.execute_browser_files", {**context, "dry_run": False}
            )
            if not blocked_projects:
                context = await self._step("identity.recover_stale", {**context, "dry_run": False})
            context = await self._step("cleanup.execute_codex_files", {**context, "dry_run": False})
            context = await self._step(
                "cleanup.execute_preview_namespaces", {**context, "dry_run": False}
            )
            context = await self._step("cleanup.execute", {**context, "dry_run": False})
            if workflow.patched("cleanup-orphan-worktrees-v1"):
                context = await self._step(
                    "cleanup.prune_orphan_worktrees", {**context, "dry_run": False}
                )
            context = await self._step("cleanup.execute_metadata", {**context, "dry_run": False})
            if blocked_projects:
                context = await self._step(
                    "cleanup.finalize", {**context, "terminal_status": "PARTIAL"}
                )
                return {
                    "status": "PARTIAL",
                    "blocked_projects": blocked_projects,
                    "context": context,
                }
            context = await self._step(
                "cleanup.finalize", {**context, "terminal_status": "COMPLETE"}
            )
            return {"status": "COMPLETE", "context": context}
        except ActivityError:
            context = await self._step(
                "cleanup.finalize",
                {
                    **context,
                    "terminal_status": "BLOCKED",
                    "blocker": "CLEANUP_ACTIVITY_FAILED",
                },
            )
            return {"status": "BLOCKED", "context": context}

    def _identity_project_scope_patched(self) -> bool:
        # NEW-119: confine identity-recovery blockers to the projects they name.
        return workflow.patched("cleanup-identity-blocker-project-scope-v1")


@workflow.defn(name="AcceptanceLifecycleWorkflow")
class AcceptanceLifecycleWorkflow(ControlledWorkflow):
    """Synthetic end-to-end lifecycle used only by the explicit validation worker."""

    @workflow.run
    async def run(self, request: AcceptanceLifecycleInput) -> dict[str, Any]:
        if request.activity_task_queue != "engineering-acceptance" and not (
            request.activity_task_queue.startswith("engineering-acceptance-validation-")
            and len(request.activity_task_queue) <= 128
        ):
            raise ValueError("acceptance activity task queue is outside its bounded namespace")
        context: dict[str, Any] = {
            **asdict(request),
            "run_id": workflow.info().run_id,
            "created_at": workflow.now().isoformat(),
        }
        for step, timeout in (
            ("acceptance.project_snapshot", 2),
            ("acceptance.worktree", 2),
            ("acceptance.maker", 2),
            ("acceptance.checker", 2),
            ("acceptance.evidence", 2),
            ("acceptance.transition", 2),
        ):
            if not await self._checkpoint(step.upper().replace(".", "_")):
                return {"status": "CANCELLED", **context}
            context = await workflow.execute_activity(
                step,
                context,
                task_queue=request.activity_task_queue,
                start_to_close_timeout=timedelta(minutes=timeout),
                heartbeat_timeout=timedelta(seconds=10),
                retry_policy=ACTIVITY_RETRY,
            )
        return context


@workflow.defn(name="ObservabilityAcceptanceWorkflow")
class ObservabilityAcceptanceWorkflow(ControlledWorkflow):
    """Read-only native Codex then Claude canary with no product or repository mutation."""

    @workflow.run
    async def run(self, request: ObservabilityAcceptanceInput) -> dict[str, Any]:
        context: dict[str, Any] = asdict(request)
        if workflow.patched("observability-terminal-finalization-v1"):
            return await self._observability_terminal_run(context)
        context = await self._step("observability.register", context)
        context = await self._step("observability.prepare_workspace", context)
        try:
            context = await self._step("observability.prepare_codex", context)
            context = await self._agent_step(
                "observability.invoke_codex", context, timeout_minutes=10
            )
            if context.get("status") != "SUCCESS":
                return await self._step(
                    "observability.finalize",
                    {**context, "terminal_status": "FAILED", "blocker": context.get("blocker")},
                )
            context = await self._step("observability.prepare_claude", context)
            context = await self._agent_step(
                "observability.invoke_claude", context, timeout_minutes=10
            )
            terminal = "COMPLETED" if context.get("status") == "SUCCESS" else "FAILED"
            return await self._step(
                "observability.finalize", {**context, "terminal_status": terminal}
            )
        finally:
            await self._step("observability.cleanup_workspace", context)


@workflow.defn(name="ReverseObservabilityAcceptanceWorkflow")
class ReverseObservabilityAcceptanceWorkflow(ControlledWorkflow):
    """Read-only native Claude maker then Codex checker canary."""

    @workflow.run
    async def run(self, request: ObservabilityAcceptanceInput) -> dict[str, Any]:
        context: dict[str, Any] = {**asdict(request), "provider_direction": "claude-to-codex"}
        if workflow.patched("observability-terminal-finalization-v1"):
            return await self._observability_terminal_run(context, reverse=True)
        context = await self._step("observability.register", context)
        context = await self._step("observability.prepare_workspace", context)
        try:
            context = await self._step("observability.prepare_claude_maker", context)
            context = await self._agent_step(
                "observability.invoke_claude_maker", context, timeout_minutes=10
            )
            if context.get("status") != "SUCCESS":
                return await self._step(
                    "observability.finalize",
                    {**context, "terminal_status": "FAILED", "blocker": context.get("blocker")},
                )
            context = await self._step("observability.prepare_codex_checker", context)
            context = await self._agent_step(
                "observability.invoke_codex_checker", context, timeout_minutes=10
            )
            terminal = "COMPLETED" if context.get("status") == "SUCCESS" else "FAILED"
            return await self._step(
                "observability.finalize", {**context, "terminal_status": terminal}
            )
        finally:
            await self._step("observability.cleanup_workspace", context)


@workflow.defn(name="IssueSolutionLoopWorkflow")
class IssueSolutionLoopWorkflow(ControlledWorkflow):
    """Durable one-issue-at-a-time solution loop; production mode is intentionally absent."""

    @workflow.run
    async def run(self, request: IssueSolutionLoopInput) -> dict[str, Any]:
        if request.execution_mode not in {"plan-only", "source-pr", "preview", "staging"}:
            raise ValueError("unsupported issue-loop execution mode")
        if not 1 <= request.maximum_items <= 100 or not 1 <= request.component_concurrency <= 4:
            raise ValueError("issue-loop bounds are invalid")
        context = await self._step("loop.load_selection", asdict(request))
        if workflow.patched("issue-loop-selection-outcomes-v1"):
            selection_status = context.get("status")
            if isinstance(selection_status, str) and selection_status in {"BLOCKED", "FAILED"}:
                blocker = context.get("blocker")
                if not (
                    isinstance(blocker, str)
                    and 1 <= len(blocker) <= 120
                    and blocker.isascii()
                    and blocker.isupper()
                    and blocker.replace("_", "").isalnum()
                ):
                    blocker = f"LOOP_SELECTION_{selection_status}"
                self._phase = f"SELECTION_{selection_status}"
                return {"status": selection_status, "blocker": blocker, "results": []}
            if not isinstance(selection_status, str) or selection_status not in {
                "SELECTED",
                "PASS",
            }:
                self._phase = "SELECTION_FAILED"
                return {
                    "status": "FAILED",
                    "blocker": "LOOP_SELECTION_STATUS_INVALID",
                    "results": [],
                }
            selection_items = context.get("items")
            if not isinstance(selection_items, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("finding_id"), str)
                or not item.get("finding_id")
                or not isinstance(item.get("risk_class"), str)
                or item.get("risk_class") not in {"R0", "R1", "R2", "R3"}
                for item in selection_items[: request.maximum_items]
            ):
                self._phase = "SELECTION_FAILED"
                return {
                    "status": "FAILED",
                    "blocker": "LOOP_SELECTION_SCHEMA_INVALID",
                    "results": [],
                }
            if not selection_items:
                self._phase = "EMPTY"
                return {"status": "EMPTY", "reason": "NO_ELIGIBLE_FINDINGS", "results": []}
        raw_items = context.get("items", [])
        if not isinstance(raw_items, list):
            raise TypeError("issue-loop selection must be a list")
        items = raw_items[: request.maximum_items]
        results: list[dict[str, Any]] = []
        for ordinal, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise TypeError("issue-loop item must be an object")
            if not await self._checkpoint(f"ISSUE_{ordinal}_ELIGIBILITY"):
                return {"status": "CANCELLED", "results": results}
            if self._skip_reason:
                skipped = await self._step(
                    "loop.record_skip",
                    {**context, "item": item, "reason": self._skip_reason},
                )
                results.append(skipped)
                self._skip_reason = None
                continue
            leased = await self._step("loop.acquire_component", {**context, "item": item})
            if leased.get("status") == "BLOCKED":
                return {"status": "PAUSED", "blocker": leased.get("blocker"), "results": results}
            try:
                delivery: dict[str, Any] = {}
                for review_attempt in range(1, 4):
                    delivery = await workflow.execute_child_workflow(
                        "IssueDeliveryWorkflow",
                        IssueDeliveryInput(
                            finding_id=str(item["finding_id"]),
                            risk_class=str(item["risk_class"]),
                            dry_run=request.dry_run or request.execution_mode == "plan-only",
                        ).payload(),
                        id=f"{workflow.info().workflow_id}-issue-{ordinal}-review-{review_attempt}",
                        task_queue=workflow.info().task_queue,
                        execution_timeout=timedelta(hours=12),
                        **self._child_run_options(
                            f"{workflow.info().workflow_id}-issue-{ordinal}-review-{review_attempt}"
                        ),
                    )
                    if delivery.get("status") != "REQUEST_CHANGES":
                        break
                if delivery.get("status") in {
                    "BLOCKED",
                    "FAILED",
                    "RATE_LIMITED",
                    "TIMED_OUT",
                    "REQUEST_CHANGES",
                }:
                    blocked = await self._step(
                        "loop.record_blocker",
                        {**leased, "item": item, "delivery": delivery},
                    )
                    return {
                        "status": "PAUSED",
                        "blocker": blocked.get("blocker", delivery.get("status")),
                        "results": results,
                    }
                if request.execution_mode in {"preview", "staging"} and not request.dry_run:
                    change_set_id = str(delivery.get("context", {}).get("change_set_id", ""))
                    if not change_set_id:
                        return {
                            "status": "PAUSED",
                            "blocker": "CHANGE_SET_BINDING_MISSING",
                            "results": results,
                        }
                    reconciled = await workflow.execute_child_workflow(
                        "DeliveryReconciliationWorkflow",
                        DeliveryReconciliationInput(
                            change_set_id=change_set_id,
                            dry_run=False,
                        ),
                        id=f"{workflow.info().workflow_id}-issue-{ordinal}-reconcile",
                        task_queue=workflow.info().task_queue,
                        execution_timeout=timedelta(hours=12),
                        **self._child_run_options(
                            f"{workflow.info().workflow_id}-issue-{ordinal}-reconcile"
                        ),
                    )
                    if reconciled.get("status") not in {"READY", "COMPLETE", "PASS"}:
                        return {
                            "status": "PAUSED",
                            "blocker": reconciled.get("status", "RECONCILIATION_FAILED"),
                            "results": results,
                        }
                    deployment = await workflow.execute_child_workflow(
                        "PreviewDeploymentWorkflow"
                        if request.execution_mode == "preview"
                        else "StagingDeploymentWorkflow",
                        PreviewDeploymentInput(
                            change_set_id=change_set_id,
                            project=request.project,
                            dry_run=False,
                        )
                        if request.execution_mode == "preview"
                        else StagingDeploymentInput(
                            change_set_id=change_set_id,
                            environment="staging",
                            dry_run=False,
                        ),
                        id=f"{workflow.info().workflow_id}-issue-{ordinal}-deploy",
                        task_queue=workflow.info().task_queue,
                        execution_timeout=timedelta(hours=12),
                        **self._child_run_options(
                            f"{workflow.info().workflow_id}-issue-{ordinal}-deploy"
                        ),
                    )
                    if deployment.get("status") not in {"COMPLETE", "PASS", "VERIFIED"}:
                        return {
                            "status": "PAUSED",
                            "blocker": deployment.get("status", "DEPLOYMENT_FAILED"),
                            "results": results,
                        }
                completed = await self._step(
                    "loop.record_item", {**leased, "item": item, "delivery": delivery}
                )
                results.append(completed)
            finally:
                await self._step("loop.release_component", {**leased, "item": item})
        finalized = await self._step(
            "loop.finalize", {**context, "processed": results, "requested_items": len(items)}
        )
        return {"status": finalized.get("status", "COMPLETE"), "results": results}


# NEW-215: the label scope of a backlog import (same pattern as
# engineering.backlog.ISSUE_LABEL_PATTERN; a test keeps them equal).
_BACKLOG_IMPORT_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,63}$")


@workflow.defn(name="GitHubBacklogImportWorkflow")
class GitHubBacklogImportWorkflow(ControlledWorkflow):
    """Read and persist one paired existing-issue backlog without any GitHub write."""

    @workflow.run
    async def run(self, request: GitHubBacklogImportInput) -> dict[str, Any]:
        if request.project not in {
            "falcone",
            "musematic",
            "llmwiki",
            "gntik-ui",
            "falcone-serverless",
        }:
            raise ApplicationError(
                "existing GitHub backlog project is not allowlisted",
                type="PolicyViolation",
                non_retryable=True,
            )
        if request.source != "github-open" or request.import_filter not in {
            "bugs",
            "bugs-and-docs",
            "security",
            "enhancements",
            "all",
        }:
            raise ApplicationError(
                "backlog source or filter is not allowlisted",
                type="PolicyViolation",
                non_retryable=True,
            )
        if not 1 <= request.maximum_items <= 500:
            raise ValueError("backlog maximum-items bound is invalid")
        if request.issue_label is not None and (
            not isinstance(request.issue_label, str)
            or _BACKLOG_IMPORT_LABEL.fullmatch(request.issue_label) is None
        ):
            raise ApplicationError(
                "backlog import label is not allowlisted",
                type="PolicyViolation",
                non_retryable=True,
            )
        context = asdict(request)
        for step in ("backlog.github_import", "backlog.persist_import", "backlog.plan"):
            if not await self._checkpoint(step.upper().replace(".", "_")):
                return {"status": "CANCELLED", "backlog_campaign_id": request.backlog_campaign_id}
            context = await self._step(step, context, timeout_minutes=30)
            if context.get("status") in {"BLOCKED", "FAILED"}:
                return context
        return context


# NEW-145: the pull requests a publication reconciliation may read (same allowlist as
# gitops.pr_heads).
_PUBLICATION_PR_URL = re.compile(
    r"https://github\.com/gntik-ai/"
    r"(?:falcone|falcone-charts|musematic|musematic-deploy|llmwiki|gntik-ui|falcone-serverless)"
    r"/pull/[1-9][0-9]{0,9}"
)
_PUBLICATION_REPOSITORY = re.compile(
    r"gntik-ai/"
    r"(?:falcone|falcone-charts|musematic|musematic-deploy|llmwiki|gntik-ui|falcone-serverless)"
)
_PUBLICATION_BRANCH = re.compile(
    r"agent/(?:falcone|musematic|llmwiki|gntik-ui|falcone-serverless)"
    r"/[A-Za-z0-9._-]+/[a-f0-9-]{36}"
)
_AMEND_ISSUE_REPOSITORIES = frozenset(
    {
        "gntik-ai/falcone",
        "gntik-ai/falcone-charts",
        "gntik-ai/musematic",
        "gntik-ai/musematic-deploy",
        "gntik-ai/llmwiki",
        "gntik-ai/gntik-ui",
        "gntik-ai/falcone-serverless",
    }
)


@workflow.defn(name="IssueAmendObservationWorkflow")
class IssueAmendObservationWorkflow(ControlledWorkflow):
    """NEW-115: read one backlog issue's current GitHub body for an operator amendment.

    Read-only: it runs the delivery's own issue-body read (same broker, same hash), so the
    hash an operator confirms is exactly the one a later delivery refresh compares."""

    @workflow.run
    async def run(self, request: IssueAmendObservationInput) -> dict[str, Any]:
        if (
            request.issue_repository not in _AMEND_ISSUE_REPOSITORIES
            or type(request.issue_number) is not int
            or request.issue_number < 1
        ):
            raise ApplicationError(
                "issue amendment target is not allowlisted",
                type="PolicyViolation",
                non_retryable=True,
            )
        self._phase = "ISSUE_AMEND_OBSERVE"
        return await self._step(
            "delivery.refresh_issue_body",
            {
                "issue_repository": request.issue_repository,
                "issue_number": request.issue_number,
            },
            timeout_minutes=1,
        )


@workflow.defn(name="PublicationHeadsObservationWorkflow")
class PublicationHeadsObservationWorkflow(ControlledWorkflow):
    """NEW-145: read the live head and state of a ChangeSet's bound pull requests for an
    operator publication reconciliation.

    Read-only: it runs NEW-102's own PR-head read (gitops.pr_heads) on the GitHub worker,
    the same read the CI-repair restore trusts."""

    @workflow.run
    async def run(self, request: PublicationHeadsObservationInput) -> dict[str, Any]:
        urls = request.pull_request_urls
        if (
            not isinstance(urls, list)
            or not 1 <= len(urls) <= 2
            or not all(isinstance(url, str) and _PUBLICATION_PR_URL.fullmatch(url) for url in urls)
        ):
            raise ApplicationError(
                "publication reconciliation pull requests are not allowlisted",
                type="PolicyViolation",
                non_retryable=True,
            )
        listings = request.branch_listings
        if listings is not None and (
            not isinstance(listings, list)
            or not 1 <= len(listings) <= 2
            or not all(
                isinstance(item, dict)
                and set(item) == {"repository", "branch"}
                and isinstance(item["branch"], str)
                and _PUBLICATION_BRANCH.fullmatch(item["branch"])
                and isinstance(item["repository"], str)
                and _PUBLICATION_REPOSITORY.fullmatch(item["repository"])
                for item in listings
            )
        ):
            raise ApplicationError(
                "publication reconciliation branch listings are not allowlisted",
                type="PolicyViolation",
                non_retryable=True,
            )
        self._phase = "PUBLICATION_HEADS_OBSERVE"
        payload: dict[str, Any] = {"pull_requests": [{"url": url} for url in urls]}
        if listings is not None:
            # NEW-145b: only a listing request changes the payload; a heads-only read (and
            # every history recorded before this) schedules exactly the same activity input.
            payload["branch_listings"] = listings
        return await self._step("gitops.pr_heads", payload, timeout_minutes=1)


@workflow.defn(name="StagingGitOpsDeliveryWorkflow")
class StagingGitOpsDeliveryWorkflow(ControlledWorkflow):
    """Post-merge staging GitOps delivery: checks, merge, build, gate, pin, sync, verify.

    Every activity is short; waiting happens here with durable timers so a 90-minute
    CI run never holds an activity slot. Fails closed with one precise blocker.
    """

    _MERGE_ROUNDS = 20
    _REMEDIATION_ROUNDS = 3

    def _later_release_pin_patched(self) -> bool:
        # NEW-174: asked only when the pin step reports a later release, so a history
        # recorded before this change (no release_sha in its pin result) replays unchanged.
        return workflow.patched("gitops-later-release-pin-v1")

    def _notify_merged_patched(self) -> bool:
        # NEW-127: histories recorded before this marker have no notify_merged step.
        return workflow.patched("gitops-notify-merged-v1")

    # NEW-113: Argo CD sync remediation bounds (mirrored by activities/gitops.py).
    _SYNC_REMEDIATION_ROUNDS = 3
    _TRANSIENT_RESYNCS = 3
    _SYNC_PATCH = "gitops-argo-sync-remediation-v1"
    # NEW-116: a full sync Argo CD ran with an inherited selective scope (one full resync).
    _SCOPE_PATCH = "gitops-argo-sync-scope-inherited-v1"
    _SCOPE_RESYNCS = 1
    # NEW-118: resources Argo CD would prune are escalated at once (Hermes never prunes).
    _PRUNE_PATCH = "gitops-argo-prune-escalation-v1"
    # NEW-160: a project without a runtime (gntik-ui) ends at the protected merge. Checked
    # only when gitops.prepare reports merge_only, which no earlier history recorded.
    _MERGE_ONLY_PATCH = "gitops-merge-only-delivery-v1"
    # Tests run the real flow with short timers. Production keeps 1.0, and _timer then
    # returns the very same timedelta, so every timer command stays byte-identical.
    _TIMER_SCALE = 1.0

    @workflow.run
    async def run(self, request: StagingGitOpsDeliveryInput) -> dict[str, Any]:
        prepared = await self._step("gitops.prepare", asdict(request))
        if prepared.get("status") == "ALREADY_VERIFIED":
            return {"status": "STAGING_VERIFIED", "already_verified": True}
        if prepared.get("status") != "READY" or not isinstance(prepared.get("plan"), dict):
            return {
                "status": "BLOCKED",
                "blocker": str(prepared.get("blocker") or "STAGING_GITOPS_PREPARE_FAILED"),
            }
        base: dict[str, Any] = {"plan": prepared["plan"], "project": request.project}
        self._write_notice = {
            "project": request.project,
            "backlog_campaign_id": request.backlog_campaign_id,
            "issue": f"{request.issue_repository}#{request.issue_number}",
        }
        flow = prepared.get("staging_flow")
        flow = flow if isinstance(flow, dict) else {}
        evidence: dict[str, Any] = {}
        outcome = await self._deliver(base, flow, evidence)
        await self._step(
            "gitops.persist_result",
            {
                **base,
                **evidence,
                "outcome": outcome["status"],
                "blocker": outcome.get("blocker"),
            },
        )
        return {**outcome, "evidence": evidence}

    async def _poll(
        self,
        step: str,
        payload: dict[str, Any],
        *,
        pending: frozenset[str],
        attempts: int,
        interval: timedelta,
        write: bool = False,
        retry_policy: RetryPolicy = ACTIVITY_RETRY,
    ) -> dict[str, Any]:
        """Call a short activity until it leaves ``pending``; TIMED_OUT when bounded out."""
        result: dict[str, Any] = {}
        for attempt in range(attempts):
            if attempt:
                await workflow.sleep(self._timer(interval))
            if not await self._checkpoint(step.upper().replace(".", "_")):
                return {"status": "CANCELLED", "blocker": "CANCELLED"}
            if write:
                result = await self._github_write_step(step, payload)
            else:
                result = await self._step(step, payload, retry_policy=retry_policy)
            if result.get("status") not in pending:
                return result
        return {**result, "status": "TIMED_OUT"}

    @staticmethod
    def _blocked(result: dict[str, Any], default: str) -> dict[str, Any]:
        if result.get("status") == "CANCELLED":
            return {"status": "CANCELLED", "blocker": "CANCELLED"}
        return {"status": "BLOCKED", "blocker": str(result.get("blocker") or default)}

    async def _deliver(
        self, base: dict[str, Any], flow: dict[str, Any], evidence: dict[str, Any]
    ) -> dict[str, Any]:
        plan = base["plan"]
        minute = timedelta(minutes=1)
        half_minute = timedelta(seconds=30)

        # 1. Source PR checks (~90 minutes).
        checks = await self._poll(
            "gitops.pr_checks",
            base,
            pending=frozenset({"CHECKS_PENDING"}),
            attempts=90,
            interval=minute,
        )
        if checks.get("status") == "TIMED_OUT":
            return {"status": "BLOCKED", "blocker": "SOURCE_PR_CI_TIMEOUT"}
        # NEW-63: keep the baseline gate's verdict (job names only) for the ChangeSet record;
        # a CI-repair re-delivery hands the regressions to the maker (NEW-64).
        baseline = checks.get("ci_baseline")
        if isinstance(baseline, dict):
            evidence["ci_baseline_pr"] = baseline.get("pr")
            evidence["ci_preexisting_failures"] = baseline.get("preexisting_failures") or []
        if checks.get("status") != "CHECKS_PASSED":
            if checks.get("blocker") == "SOURCE_PR_CI_REGRESSION":
                evidence["ci_regressions"] = checks.get("failed_checks") or []
                if checks.get("reason"):
                    # NEW-99: the failures were not compared with unchanged main.
                    evidence["ci_regression_reason"] = checks.get("reason")
                if checks.get("failure_excerpts"):
                    # NEW-100: the repair maker needs the failing lines, not only job names.
                    evidence["ci_failure_excerpts"] = checks.get("failure_excerpts")
            return self._blocked(checks, "SOURCE_PR_CI_FAILED")

        # 1b. NEW-103: the deployment PR's CI must also pass before either PR merges,
        # so a deployment failure can no longer leave the source merged on its own.
        if plan.get("deployment_pr_number") and workflow.patched("gitops-deployment-ci-gate-v1"):
            deployment_checks = await self._poll(
                "gitops.deployment_pr_checks",
                base,
                pending=frozenset({"CHECKS_PENDING"}),
                attempts=90,
                interval=minute,
            )
            if deployment_checks.get("status") == "TIMED_OUT":
                return {"status": "BLOCKED", "blocker": "DEPLOYMENT_PR_CI_TIMEOUT"}
            if deployment_checks.get("status") != "CHECKS_PASSED":
                if deployment_checks.get("blocker") == "DEPLOYMENT_PR_CI_REGRESSION":
                    evidence["ci_regressions"] = deployment_checks.get("failed_checks") or []
                    evidence["ci_regression_reason"] = deployment_checks.get("reason")
                    evidence["ci_regression_target"] = "deployment"
                    if deployment_checks.get("failure_excerpts"):
                        evidence["ci_failure_excerpts"] = deployment_checks.get("failure_excerpts")
                return self._blocked(deployment_checks, "DEPLOYMENT_PR_CI_FAILED")

        # 2. Ready + merge (source first, then the deployment PR when one exists).
        targets = ["source"]
        if plan.get("deployment_pr_number"):
            targets.append("deployment")
        for target in targets:
            merged = await self._poll(
                "gitops.merge",
                {**base, "target": target},
                pending=frozenset({"MERGE_PENDING"}),
                attempts=self._MERGE_ROUNDS,
                interval=half_minute,
                write=True,
            )
            if merged.get("status") == "TIMED_OUT":
                return {"status": "BLOCKED", "blocker": f"{target.upper()}_PR_MERGE_BLOCKED"}
            if merged.get("status") != "MERGED":
                return self._blocked(merged, "PROTECTED_MERGE_FAILED")
            key = "merge_sha" if target == "source" else "deployment_merge_sha"
            evidence[key] = str(merged.get("merge_sha", ""))
            if self._notify_merged_patched():
                # NEW-127: announce each merge now, not after hours of CI/image/Argo waits.
                await self._step(
                    "gitops.notify_merged",
                    {**base, "target": target, "merge_sha": evidence[key]},
                )
        merge_sha = evidence["merge_sha"]
        merged_base = {**base, "merge_sha": merge_sha}
        if flow.get("merge_only") is True and workflow.patched(self._MERGE_ONLY_PATCH):
            # NEW-160: no release tag, image build, environment gate, Argo CD sync or
            # runtime verification exists; close the delivered issue with the merge.
            if not await self._checkpoint("GITOPS_CLOSE_ISSUE"):
                return {"status": "CANCELLED", "blocker": "CANCELLED"}
            closed = await self._github_write_step("gitops.close_issue", merged_base)
            if closed.get("status") != "ISSUE_CLOSED":
                return self._blocked(closed, "DELIVERED_ISSUE_CLOSE_FAILED")
            return {"status": "MERGE_DELIVERED"}

        # 3. Release tag (optional) and the image-producing builds.
        if flow.get("release_tag_enabled"):
            if not await self._checkpoint("GITOPS_RELEASE_TAG"):
                return {"status": "CANCELLED", "blocker": "CANCELLED"}
            tagged = await self._github_write_step("gitops.release_tag", merged_base)
            if tagged.get("status") != "TAG_READY":
                return self._blocked(tagged, "RELEASE_TAG_FAILED")
            evidence["release_tag"] = tagged.get("release_tag")
        for role, configured, timeout_blocker in (
            ("main_ci", flow.get("main_ci_workflow"), "MAIN_CI_TIMEOUT"),
            ("image", flow.get("image_workflow"), "IMAGE_BUILD_TIMEOUT"),
        ):
            if not configured:
                continue
            run = await self._poll(
                "gitops.workflow_run",
                {**merged_base, "workflow_role": role},
                pending=frozenset({"RUN_PENDING"}),
                attempts=90,
                interval=minute,
            )
            if run.get("status") == "TIMED_OUT":
                return {"status": "BLOCKED", "blocker": timeout_blocker}
            if run.get("status") != "RUN_SUCCEEDED":
                return self._blocked(
                    run, "MAIN_CI_FAILED" if role == "main_ci" else "IMAGE_BUILD_FAILED"
                )
            if role == "image" and run.get("release_sha"):
                # NEW-68: a later release containing the merge ships this change; every
                # following step pins, syncs and verifies that release's commit.
                evidence["release_tag"] = run.get("release_tag")
                evidence["release_sha"] = str(run["release_sha"])
        deployed_base = {
            **merged_base,
            "merge_sha": str(evidence.get("release_sha") or merge_sha),
            "delivered_merge_sha": merge_sha,
        }

        # 4. Environment gate with bounded remediation.
        gate = await self._environment_gate(base, evidence)
        if gate is not None:
            return gate

        # 5. Image pin written by argocd-image-updater (~30 minutes).
        pin = await self._poll(
            "gitops.image_pin",
            deployed_base,
            pending=frozenset({"PIN_PENDING"}),
            attempts=30,
            interval=minute,
        )
        if pin.get("status") == "TIMED_OUT":
            return {"status": "BLOCKED", "blocker": "IMAGE_NOT_PINNED"}
        if pin.get("status") != "PINNED":
            return self._blocked(pin, "IMAGE_NOT_PINNED")
        if pin.get("release_sha") and self._later_release_pin_patched():
            # NEW-174: a later release containing the merge was pinned (an image hotfix
            # after the delivery); sync and verification follow its commit, as NEW-68 does
            # for a later release found while waiting for the image build.
            deployed_base = {**deployed_base, "merge_sha": str(pin["release_sha"])}
            evidence["release_tag"] = pin.get("release_tag")
            evidence["release_sha"] = str(pin["release_sha"])
        evidence["image_tag"] = pin.get("image_tag")
        evidence["pinned_repositories"] = list(pin.get("pinned_repositories") or [])

        # 6. Hermes-triggered Argo sync (~20 minutes).
        triggered = await self._poll(
            "gitops.trigger_sync",
            deployed_base,
            pending=frozenset({"SYNC_BUSY"}),
            attempts=10,
            interval=half_minute,
        )
        if triggered.get("status") == "TIMED_OUT":
            return {"status": "BLOCKED", "blocker": "ARGO_OPERATION_BUSY"}
        if triggered.get("status") != "SYNC_TRIGGERED":
            return self._blocked(triggered, "ARGO_SYNC_FAILED")
        evidence["chart_pin"] = triggered.get("chart_pin")
        synced = await self._poll(
            "gitops.sync_status",
            {
                **deployed_base,
                "previous_operation_started_at": triggered.get("previous_operation_started_at"),
            },
            pending=frozenset({"SYNC_PENDING"}),
            attempts=40,
            interval=half_minute,
        )
        if (
            synced.get("status") in {"SYNC_FAILED", "TIMED_OUT"}
            or self._scope_inherited(synced)
            or self._prune_required(synced)
        ) and self._sync_recovery_patched():
            # NEW-113: diagnose, remediate within bounds, notify, and escalate the rest
            # (gntik-ai/falcone#980: a patch-incompatible env change failed the sync).
            synced = await self._recover_sync(deployed_base, synced, evidence)
            if synced.get("status") != "SYNCED":
                return synced
        if synced.get("status") == "TIMED_OUT":
            return {"status": "BLOCKED", "blocker": "ARGO_SYNC_TIMEOUT"}
        if synced.get("status") != "SYNCED":
            return self._blocked(synced, "ARGO_SYNC_FAILED")
        evidence["argo_revision"] = synced.get("argo_revision")

        # 7. Verify every pinned workload runs the new tag and is fully ready.
        verified = await self._poll(
            "gitops.verify_workloads",
            {**deployed_base, "pinned_repositories": evidence["pinned_repositories"]},
            pending=frozenset({"VERIFICATION_PENDING"}),
            attempts=10,
            interval=half_minute,
        )
        if verified.get("status") != "VERIFIED":
            if verified.get("status") == "CANCELLED":
                return self._blocked(verified, "CANCELLED")
            return {"status": "BLOCKED", "blocker": "STAGING_VERIFICATION_FAILED"}

        # 8. Close the delivered issue with the delivery evidence.
        if not await self._checkpoint("GITOPS_CLOSE_ISSUE"):
            return {"status": "CANCELLED", "blocker": "CANCELLED"}
        closed = await self._github_write_step(
            "gitops.close_issue",
            {
                **deployed_base,
                "release_tag": evidence.get("release_tag"),
                "pinned_repositories": evidence["pinned_repositories"],
                "argo_revision": evidence.get("argo_revision"),
            },
        )
        if closed.get("status") != "ISSUE_CLOSED":
            return self._blocked(closed, "DELIVERED_ISSUE_CLOSE_FAILED")
        return {"status": "STAGING_VERIFIED"}

    def _timer(self, interval: timedelta) -> timedelta:
        return interval if self._TIMER_SCALE == 1.0 else interval * self._TIMER_SCALE

    def _sync_recovery_patched(self) -> bool:
        # Checked only where a sync failed or timed out, so a history recorded before
        # NEW-113 (no marker there) replays its old BLOCKED return unchanged.
        return workflow.patched(self._SYNC_PATCH)

    def _scope_inherited(self, synced: dict[str, Any]) -> bool:
        # NEW-116: checked only for the new status, which no earlier history recorded, so
        # every pre-NEW-116 history replays unchanged.
        return synced.get("status") == "SYNC_SCOPE_INHERITED" and workflow.patched(
            self._SCOPE_PATCH
        )

    def _prune_required(self, synced: dict[str, Any]) -> bool:
        # NEW-118: only the new SYNC_PRUNE_REQUIRED status (never recorded before) reaches
        # the marker, so every earlier history replays unchanged.
        return synced.get("status") == "SYNC_PRUNE_REQUIRED" and workflow.patched(self._PRUNE_PATCH)

    @staticmethod
    def _sync_blocker(synced: dict[str, Any], failure_class: str) -> str:
        # NEW-118: resources Argo CD would prune are never remediated; the operator
        # verifies and deletes them (the issue lists each one with commands).
        if synced.get("status") == "SYNC_PRUNE_REQUIRED" or failure_class == "PRUNE_REQUIRED":
            return "ARGO_PRUNE_REQUIRED"
        return "ARGO_SYNC_TIMEOUT" if synced.get("status") == "TIMED_OUT" else "ARGO_SYNC_FAILED"

    @staticmethod
    def _remediation_id(rounds: int, action: str) -> str:
        workflow_id = re.sub(r"[^A-Za-z0-9._:/-]", "-", workflow.info().workflow_id)[:200]
        return f"{workflow_id}:round-{rounds}:{action}"

    @staticmethod
    def _sync_resource_key(entry: dict[str, Any]) -> str:
        return "/".join(
            str(entry.get(field) or "") for field in ("group", "kind", "namespace", "name")
        )

    @classmethod
    def _sync_remediation_plan(
        cls,
        failure_class: str,
        diagnosis: dict[str, Any],
        remediated: list[str],
        *,
        timed_out: bool,
        transient: int,
        scope_resyncs: int = 0,
    ) -> tuple[str | None, list[dict[str, str]]]:
        """Pick the next bounded remediation, or None to escalate to the operator."""
        if failure_class == "SCOPE_INHERITED":
            # NEW-116: one TRUE full sync (the activity clears the inherited state).
            return ("full_resync", []) if scope_resyncs < cls._SCOPE_RESYNCS else (None, [])
        if failure_class == "TRANSIENT":
            return ("resync", []) if transient < cls._TRANSIENT_RESYNCS else (None, [])
        if timed_out or failure_class not in {"PATCH_CONFLICT", "JOB_IMMUTABLE"}:
            # Degraded health and everything unclassified go to the operator.
            return None, []
        raw = diagnosis.get("failed_resources")
        targets = [
            {field: str(entry.get(field) or "") for field in ("group", "kind", "namespace", "name")}
            for entry in (raw if isinstance(raw, list) else [])
            if isinstance(entry, dict)
            and entry.get("failure_class") == failure_class
            and entry.get("eligible") is True
            and cls._sync_resource_key(entry) not in remediated
        ][:10]
        if not targets:
            return None, []
        return ("replace" if failure_class == "PATCH_CONFLICT" else "replace_force"), targets

    async def _notify_sync(self, deployed_base: dict[str, Any], event: str, **details: Any) -> None:
        try:
            await self._step("gitops.notify_sync", {**deployed_base, "event": event, **details})
        except ActivityError:
            # A lost operator notification never changes the delivery outcome.
            pass

    async def _recover_sync(
        self,
        deployed_base: dict[str, Any],
        synced: dict[str, Any],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """NEW-113: bounded Argo CD sync remediation; returns SYNCED or a final outcome."""
        half_minute = timedelta(seconds=30)
        remediated: list[str] = []
        attempts: list[dict[str, Any]] = []
        transient = 0
        scope_resyncs = 0
        rounds = 0
        detected = False
        while True:
            timed_out = synced.get("status") == "TIMED_OUT"
            diagnosed = await self._step("gitops.diagnose_sync", deployed_base)
            raw = diagnosed.get("diagnosis")
            diagnosis: dict[str, Any] = (
                raw
                if isinstance(raw, dict)
                else {"message": str(synced.get("message") or "")[:300]}
            )
            failure_class = (
                str(diagnosed.get("failure_class") or "OTHER")
                if diagnosed.get("status") == "DIAGNOSED"
                else "OTHER"
            )
            scope_inherited = self._scope_inherited(synced)
            if scope_inherited:
                failure_class = "SCOPE_INHERITED"
            if not detected:
                detected = True
                await self._notify_sync(
                    deployed_base,
                    "detected",
                    failure_class=failure_class,
                    blocker=self._sync_blocker(synced, failure_class),
                    diagnosis=diagnosis,
                )
            action, resources = self._sync_remediation_plan(
                failure_class,
                diagnosis,
                remediated,
                timed_out=timed_out,
                transient=transient,
                scope_resyncs=scope_resyncs,
            )
            if action is None or rounds >= self._SYNC_REMEDIATION_ROUNDS:
                return await self._escalate_sync(
                    deployed_base, synced, failure_class, diagnosis, attempts, evidence
                )
            rounds += 1
            if not await self._checkpoint(f"GITOPS_SYNC_REMEDIATION_{rounds}"):
                return {"status": "CANCELLED", "blocker": "CANCELLED"}
            if action == "resync":
                transient += 1
                await workflow.sleep(self._timer(timedelta(seconds=30 * 2 ** (transient - 1))))
            if action == "full_resync":
                scope_resyncs += 1
            record: dict[str, Any] = {
                "round": rounds,
                "action": action,
                "failure_class": failure_class,
                "resources": [self._sync_resource_key(item) for item in resources],
            }
            triggered = await self._remediation_call(
                "gitops.remediate_sync",
                {
                    **deployed_base,
                    "action": action,
                    "resources": resources,
                    "round": rounds,
                    "attempt": scope_resyncs if action == "full_resync" else transient,
                    "already_remediated": remediated,
                    # Stable per remediation, so a retried activity cannot sync twice.
                    "remediation_id": self._remediation_id(rounds, action),
                    # The failed operation this diagnosis describes (None for health).
                    "failed_operation_started_at": (
                        synced.get("operation_started_at")
                        if synced.get("status") == "SYNC_FAILED" or scope_inherited
                        else None
                    ),
                },
                pending=frozenset({"SYNC_BUSY"}),
                attempts=10,
                interval=half_minute,
            )
            if triggered.get("status") == "CANCELLED":
                return {"status": "CANCELLED", "blocker": "CANCELLED"}
            if triggered.get("status") == "REMEDIATION_TRIGGERED":
                remediated.extend(str(key) for key in triggered.get("remediated_keys") or [])
                synced = await self._poll(
                    "gitops.sync_status",
                    {
                        **deployed_base,
                        "previous_operation_started_at": triggered.get(
                            "previous_operation_started_at"
                        ),
                        "selective": bool(resources),
                    },
                    pending=frozenset({"SYNC_PENDING"}),
                    attempts=40,
                    interval=half_minute,
                )
                if synced.get("status") == "SYNC_PARTIAL":
                    # The replaced resources applied; one normal sync applies the rest.
                    completed = await self._remediation_call(
                        "gitops.remediate_sync",
                        {
                            **deployed_base,
                            "action": "complete",
                            "round": rounds,
                            "remediation_id": self._remediation_id(rounds, "complete"),
                        },
                        pending=frozenset({"SYNC_BUSY"}),
                        attempts=10,
                        interval=half_minute,
                    )
                    if completed.get("status") == "REMEDIATION_TRIGGERED":
                        synced = await self._poll(
                            "gitops.sync_status",
                            {
                                **deployed_base,
                                "previous_operation_started_at": completed.get(
                                    "previous_operation_started_at"
                                ),
                            },
                            pending=frozenset({"SYNC_PENDING"}),
                            attempts=40,
                            interval=half_minute,
                        )
                    elif completed.get("status") == "REMEDIATION_OUTCOME_UNKNOWN":
                        record["result"] = "ARGO_REMEDIATION_OUTCOME_UNKNOWN"
                        attempts.append(record)
                        return await self._escalate_sync(
                            deployed_base, completed, failure_class, diagnosis, attempts, evidence
                        )
                    else:
                        synced = {**completed, "status": "SYNC_FAILED"}
                result = str(synced.get("status") or "UNKNOWN")
            else:
                result = str(triggered.get("blocker") or triggered.get("status") or "UNKNOWN")
            record["result"] = result[:80]
            attempts.append(record)
            evidence["argo_sync_remediations"] = attempts
            await self._notify_sync(
                deployed_base,
                "remediation",
                failure_class=failure_class,
                action=action,
                round=rounds,
                result=record["result"],
                resources=resources,
            )
            if triggered.get("status") != "REMEDIATION_TRIGGERED":
                return await self._escalate_sync(
                    deployed_base, synced, failure_class, diagnosis, attempts, evidence
                )
            if synced.get("status") == "SYNCED":
                evidence["argo_sync_failure_class"] = failure_class
                return synced
            if (
                synced.get("status") not in {"SYNC_FAILED", "TIMED_OUT"}
                and not self._scope_inherited(synced)
                and not self._prune_required(synced)
            ):
                return self._blocked(synced, "ARGO_SYNC_FAILED")

    async def _remediation_call(
        self,
        step: str,
        payload: dict[str, Any],
        *,
        pending: frozenset[str],
        attempts: int,
        interval: timedelta,
    ) -> dict[str, Any]:
        """Poll the mutating remediation step with single-attempt activities.

        A failed activity (including a timed-out one whose write may have landed) is not
        retried: its outcome is unknown, so the flow escalates instead of writing again.
        """
        try:
            return await self._poll(
                step,
                payload,
                pending=pending,
                attempts=attempts,
                interval=interval,
                retry_policy=SINGLE_ATTEMPT,
            )
        except ActivityError:
            return {
                "status": "REMEDIATION_OUTCOME_UNKNOWN",
                "blocker": "ARGO_REMEDIATION_OUTCOME_UNKNOWN",
            }

    async def _escalate_sync(
        self,
        deployed_base: dict[str, Any],
        synced: dict[str, Any],
        failure_class: str,
        diagnosis: dict[str, Any],
        attempts: list[dict[str, Any]],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """NEW-113: one issue + one notification with the diagnosis, then BLOCKED."""
        blocker = self._sync_blocker(synced, failure_class)
        evidence["argo_sync_failure_class"] = failure_class
        evidence["argo_sync_remediations"] = attempts
        if not await self._checkpoint("GITOPS_SYNC_ESCALATION"):
            return {"status": "CANCELLED", "blocker": "CANCELLED"}
        issue = await self._github_write_step(
            "gitops.environment_issue",
            {
                **deployed_base,
                "action": "report_sync",
                "failure_class": failure_class,
                "blocker": blocker,
                "diagnosis": diagnosis,
                "attempts": attempts,
            },
        )
        if issue.get("status") == "CANCELLED":
            return {"status": "CANCELLED", "blocker": "CANCELLED"}
        issue_url: str | None = None
        if issue.get("status") in {"ISSUE_OPENED", "ISSUE_COMMENTED"} and isinstance(
            issue.get("issue_number"), int
        ):
            evidence["argo_sync_issue_number"] = issue["issue_number"]
            issue_url = str(issue.get("issue_url") or "") or None
        else:
            evidence["argo_sync_issue_blocker"] = str(
                issue.get("blocker") or "ENVIRONMENT_ISSUE_FAILED"
            )[:120]
        await self._notify_sync(
            deployed_base,
            "escalated",
            failure_class=failure_class,
            blocker=blocker,
            issue_url=issue_url,
            diagnosis=diagnosis,
        )
        return {"status": "BLOCKED", "blocker": blocker, "failure_class": failure_class}

    async def _environment_gate(
        self, base: dict[str, Any], evidence: dict[str, Any]
    ) -> dict[str, Any] | None:
        check = await self._step("gitops.environment_check", base)
        issue_number: int | None = None
        rounds = 0
        while check.get("status") != "HEALTHY":
            if check.get("status") != "UNHEALTHY":
                return self._blocked(check, "ENVIRONMENT_CHECK_FAILED")
            if rounds >= self._REMEDIATION_ROUNDS:
                evidence["remediation_rounds"] = rounds
                return {"status": "BLOCKED", "blocker": "ENVIRONMENT_UNHEALTHY"}
            rounds += 1
            if not await self._checkpoint(f"GITOPS_REMEDIATION_{rounds}"):
                return {"status": "CANCELLED", "blocker": "CANCELLED"}
            issue = await self._github_write_step(
                "gitops.environment_issue",
                {
                    **base,
                    "action": "report",
                    "round": rounds,
                    "diagnosis": check.get("diagnosis"),
                    "issue_number": issue_number,
                },
            )
            if issue.get("status") not in {"ISSUE_OPENED", "ISSUE_COMMENTED"}:
                return self._blocked(issue, "ENVIRONMENT_ISSUE_FAILED")
            issue_number = int(issue["issue_number"])
            evidence["environment_issue_number"] = issue_number
            await self._step("gitops.remediate", {**base, "round": rounds})
            await workflow.sleep(timedelta(minutes=2))
            check = await self._step("gitops.environment_check", base)
        evidence["remediation_rounds"] = rounds
        if issue_number is not None:
            resolved = await self._github_write_step(
                "gitops.environment_issue",
                {**base, "action": "resolve", "issue_number": issue_number},
            )
            if resolved.get("status") != "ISSUE_RESOLVED":
                return self._blocked(resolved, "ENVIRONMENT_ISSUE_FAILED")
        return None


_LOOP_RESULT_KEYS = (
    "status",
    "blocker",
    "failure_code",
    "reason_code",
    "attempt",
    "stage",
    "issue_number",
    "issue_url",
    "source_pr_url",
    "deployment_pr_url",
    "merge_sha",
    "release_tag",
    "image_tag",
)
_LOOP_RESULT_LIMIT = 200


def _compact_loop_outcome(outcome: dict[str, Any]) -> dict[str, Any]:
    """NEW-59: keep a bounded, scalar summary of each child result in the loop outcome."""
    results = outcome.get("results")
    if not isinstance(results, list):
        return outcome
    compact: list[dict[str, Any]] = []
    for item in results[-_LOOP_RESULT_LIMIT:]:
        if not isinstance(item, dict):
            continue
        summary = {
            key: item[key]
            for key in _LOOP_RESULT_KEYS
            if key in item and (item[key] is None or isinstance(item[key], (str, int, float, bool)))
        }
        context = item.get("context")
        if isinstance(context, dict):
            for key in _LOOP_RESULT_KEYS:
                value = context.get(key)
                if key not in summary and isinstance(value, (str, int, float, bool)):
                    summary[key] = value
        compact.append(summary)
    return {**outcome, "results": compact, "results_total": len(results)}


def _backlog_execution_payload(request: ExistingGitHubBacklogLoopInput) -> dict[str, Any]:
    value = asdict(request)
    # Preserve the exact legacy activity payload when replaying old inputs.
    for key in ("issue_operation_id", "issue_approval_binding"):
        if value[key] is None:
            value.pop(key)
    if value.get("maximum_concurrency") == 1:
        # NEW-214b review: concurrency-1 activity payloads stay byte-identical.
        value.pop("maximum_concurrency")
    return value


# NEW-114: outcomes decided on the issue itself that say nothing was wrong with the loop
# and nothing was attempted: they never consume one of the approved maximum_items slots.
_SLOT_FREEING_BLOCKERS = frozenset(
    {"CURRENT_REVALIDATION_REJECTED", "CURRENT_REVALIDATION_DUPLICATE"}
)
_SLOT_FREEING_MARKERS = ("ALREADY_FIXED", "ISSUE_CLOSED", "NOT_REPRODUCIBLE")
# Bounds the refill: one loop never walks more than this many issues in total.
_LOOP_REFILL_LIMIT = 100
# Child statuses that mean the child itself failed, not that the issue is blocked.
_REVALIDATION_FAILURE_STATUSES = frozenset({"CHILD_FAILED", "FAILED"})
# NEW-217: a revalidation that stopped because its issue changed after import.
ISSUE_BODY_CHANGED_SINCE_IMPORT = "ISSUE_BODY_CHANGED_SINCE_IMPORT"
# NEW-229: issue_delivery_approval.APPROVAL_LIFETIME_BLOCKERS (not importable in the
# workflow sandbox); a child registration refused for one of them pauses on it.
_APPROVAL_LIFETIME_BLOCKERS = frozenset({"CAMPAIGN_APPROVAL_EXPIRING", "CAMPAIGN_APPROVAL_EXPIRED"})


def _approval_lifetime_blocker(delivery: dict[str, Any]) -> str | None:
    """NEW-229: a BLOCKED delivery child whose approval expired while it ran (its context
    names CAMPAIGN_APPROVAL_EXPIRED), so the loop pauses on that reason. Deterministic on
    replay: children recorded before NEW-229 never report it."""
    context = delivery.get("context")
    blocker = context.get("blocker") if isinstance(context, dict) else None
    if delivery.get("status") == "BLOCKED" and blocker in _APPROVAL_LIFETIME_BLOCKERS:
        return str(blocker)
    return None


def _revalidation_body_changed(verification: dict[str, Any]) -> bool:
    context = verification.get("context")
    return (
        verification.get("status") == "BLOCKED"
        and isinstance(context, dict)
        and context.get("blocker") == ISSUE_BODY_CHANGED_SINCE_IMPORT
    )


def _frees_slot(result: dict[str, Any]) -> bool:
    """NEW-114: a REJECTED/duplicate/already-fixed/closed or skipped item frees its slot."""
    if result.get("status") == "SKIPPED":
        return True
    blocker = str(result.get("blocker") or "")
    return blocker in _SLOT_FREEING_BLOCKERS or any(
        marker in blocker for marker in _SLOT_FREEING_MARKERS
    )


def _issue_ref(item: dict[str, Any]) -> str:
    return f"{item.get('repository')}#{item.get('issue_number')}"


def _failure_code(value: str) -> str:
    """Upper-snake, bounded failure code (PolicyViolation -> POLICY_VIOLATION)."""
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return (re.sub(r"[^A-Z0-9]+", "_", snake.upper()).strip("_") or "UNKNOWN")[:60]


def _child_failure_code(error: BaseException) -> str:
    """NEW-114: the most specific code of a failed child (e.g. BASE_SHA_MISMATCH)."""
    current: BaseException | None = error
    for _ in range(8):
        if current is None:
            break
        if isinstance(current, ApplicationError):
            if "base sha mismatch" in str(current).lower():
                return "BASE_SHA_MISMATCH"
            if current.type:
                return _failure_code(current.type)
        current = current.__cause__
    return "CHILD_WORKFLOW_FAILED"


_PROVIDER_ACCESS_PREFIXES = ("MANUAL_AUTH_REQUIRED_", "PROVIDER_CLI_MISSING_")


def _provider_access_code(blocker: Any, child: Any) -> str | None:
    """NEW-232: a provider login/CLI code in a child's blocker or its final context.

    Revalidation reports a generic CURRENT_REVALIDATION_BLOCKED, so the child's own
    context blocker is read too."""
    context = child.get("context") if isinstance(child, dict) else None
    for value in (
        blocker,
        child.get("blocker") if isinstance(child, dict) else None,
        context.get("blocker") if isinstance(context, dict) else None,
    ):
        if isinstance(value, str) and value.startswith(_PROVIDER_ACCESS_PREFIXES):
            return value[:120]
    return None


_AGENT_STEP_BLOCKERS = frozenset(
    {
        "MANUAL_AUTH_REQUIRED_CODEX",
        "MANUAL_AUTH_REQUIRED_CLAUDE",
        "PROVIDER_CLI_MISSING_CODEX",
        "PROVIDER_CLI_MISSING_CLAUDE",
        "RATE_LIMITED",
        "SchemaViolation",
        "PolicyViolation",
    }
)
# A previous provider call's result in the context (e.g. the spec's, when the maker
# raised): agent.persist_result would read it as this call's result and reject its
# identity, so the attempt crashed before its stop was recorded (NEW-137 review).
_PREVIOUS_PROVIDER_RESULT_KEYS = frozenset(
    {"agent_result", "agent_invocation_id", "agent_invocation_status"}
)


def _agent_failure_payload(started: dict[str, Any], cause: BaseException | None) -> dict[str, Any]:
    """agent.persist_result's input after a provider activity raised (no result)."""
    blocker = (
        cause.type
        if isinstance(cause, ApplicationError) and cause.type in _AGENT_STEP_BLOCKERS
        else "PROVIDER_ACTIVITY_FAILED"
    )
    failed: dict[str, Any] = {
        key: value for key, value in started.items() if key not in _PREVIOUS_PROVIDER_RESULT_KEYS
    }
    failed.update({"status": "FAILED", "blocker": blocker})
    if blocker == "PROVIDER_ACTIVITY_FAILED":
        # NEW-137: the bounded failure type (e.g. TIMEOUT_HEARTBEAT) tells a provider
        # worker that stopped answering from a non-transient activity error. Data only
        # (no command), so replay is unchanged.
        failed["provider_failure_type"] = _provider_failure_type(cause)
    details = cause.details if isinstance(cause, ApplicationError) else ()
    if blocker == "SchemaViolation" and details and isinstance(details[0], dict):
        # Usage of a call whose output failed its schema (numbers only).
        failed["failed_agent_usage"] = {
            key: details[0].get(key) for key in ("usage", "provider", "run_id")
        }
        findings = details[0].get("findings")
        if isinstance(findings, list) and findings:
            # NEW-132: the bounded, redacted schema errors (input only, no command).
            failed["failed_schema_findings"] = [str(item)[:400] for item in findings[:20]]
    return failed


# NEW-166: sdk-core's worker_shutdown_failure (activities.rs): this exact message, no stack
# trace, retryable. A Python exception always carries its traceback as the stack trace.
_SDK_WORKER_SHUTDOWN_MESSAGE = "Worker is shutting down and this activity did not complete in time"


def _sdk_worker_shutdown(cause: ApplicationError) -> bool:
    failure = cause.failure
    return (
        failure is not None
        and not cause.non_retryable
        and failure.message == _SDK_WORKER_SHUTDOWN_MESSAGE
        and failure.stack_trace == ""
        and failure.source == ""
        and not failure.HasField("cause")
    )


def _provider_failure_type(cause: BaseException | None) -> str:
    """NEW-137: bounded type of a provider activity failure (TIMEOUT_HEARTBEAT, ...)."""
    if isinstance(cause, ActivityTimeoutError) and cause.type is not None:
        return f"TIMEOUT_{cause.type.name}"
    if isinstance(cause, ApplicationError) and cause.type:
        code = _failure_code(cause.type)
        if code == "WORKER_SHUTDOWN" and not (
            cause.type == "WorkerShutdown" and _sdk_worker_shutdown(cause)
        ):
            # NEW-166 review: application code reusing the SDK's type, or any alias that
            # normalizes to it, is not a worker shutdown.
            return "APPLICATION_WORKER_SHUTDOWN"
        return code
    return _failure_code(type(cause).__name__) if cause is not None else "UNKNOWN"


def _delivery_failure_blocker(delivery: dict[str, Any]) -> str:
    code = delivery.get("failure_code") or delivery.get("blocker") or "FAILED"
    return f"DELIVERY_FAILED:{_failure_code(str(code))}"


def _loop_outcomes(results: list[dict[str, Any]]) -> list[str]:
    """Bounded per-issue outcome lines for the nothing-delivered notification."""
    outcomes: list[str] = []
    for result in results:
        if not isinstance(result, dict) or "issue_number" not in result:
            continue
        line = f"{result.get('repository')}#{result.get('issue_number')}:{result.get('status')}"
        reason = result.get("blocker") or result.get("reason_code")
        outcomes.append(f"{line}:{reason}"[:160] if reason else line[:160])
    return outcomes[-20:]


# NEW-214a: staging blockers a merge-forward repair round answers (gitops._behind_base).
_MERGE_FORWARD_BLOCKERS = frozenset({"SOURCE_PR_BEHIND_BASE", "DEPLOYMENT_PR_BEHIND_BASE"})


class _LoopIssueSkipped(Exception):  # noqa: N818 - control flow, not an error
    """NEW-162: the operator skipped the issue a failed loop step paused on."""

    def __init__(self, skipped: dict[str, Any]) -> None:
        super().__init__("backlog issue skipped after a failed loop step")
        self.skipped = skipped


@workflow.defn(name="ExistingGitHubBacklogLoopWorkflow")
class ExistingGitHubBacklogLoopWorkflow(ControlledWorkflow):
    """Durably control imported issues; fail closed until current reproduction is proven."""

    @workflow.run
    async def run(self, request: ExistingGitHubBacklogLoopInput) -> dict[str, Any]:
        # NEW-59: every child delivery's full context was accumulated in "results"; after
        # 19 retry rounds on musematic #993 the loop's completion payload exceeded
        # Temporal's 2 MB limit (TMPRL1103) and the loop could never finish. Only the
        # returned value is compacted, so no command changes and replay is unaffected.
        outcome = await self._run_loop(request)
        return _compact_loop_outcome(outcome)

    def _blocked_start_closes_run_patched(self) -> bool:
        # NEW-97: a start blocked at selection records a closed Run so its operation ends.
        return workflow.patched("existing-backlog-blocked-start-closes-run-v1")

    async def _run_loop(self, request: ExistingGitHubBacklogLoopInput) -> dict[str, Any]:
        if request.project not in {
            "falcone",
            "musematic",
            "llmwiki",
            "gntik-ui",
            "falcone-serverless",
        }:
            raise ApplicationError(
                "existing backlog loop project is not allowlisted",
                type="PolicyViolation",
                non_retryable=True,
            )
        if request.delivery_mode not in {"source-pr", "full-delivery"}:
            raise ApplicationError(
                "existing backlog delivery mode is invalid",
                type="PolicyViolation",
                non_retryable=True,
            )
        register_child_runs = workflow.patched("existing-backlog-register-child-runs-v1")
        sequential_source_pr = workflow.patched("existing-backlog-sequential-source-pr-v1")
        after_current_controls = workflow.patched("existing-backlog-after-current-controls-v1")
        final_closure_enabled = workflow.patched("existing-backlog-final-closure-v1")
        global_child_blocker_return = workflow.patched("global-backlog-child-blocker-return-v1")
        register_run = workflow.patched("existing-backlog-register-run-v1")
        self._loop_register_run = register_run
        context = await self._step("backlog.load_execution", _backlog_execution_payload(request))
        if context.get("status") == "BLOCKED":
            if register_run and self._blocked_start_closes_run_patched():
                # NEW-97: without a Run the operation stayed STARTED forever and every later
                # start of the issue was refused as ISSUE_WORKFLOW_ALREADY_ACTIVE.
                await self._step(
                    "backlog.register_run",
                    {
                        **_backlog_execution_payload(request),
                        "workflow_id": workflow.info().workflow_id,
                        "closed_blocker": str(context.get("blocker") or "BLOCKED"),
                    },
                )
            return context
        try:
            if register_run:
                await self._step(
                    "backlog.register_run",
                    {
                        **_backlog_execution_payload(request),
                        "workflow_id": workflow.info().workflow_id,
                    },
                )
            outcome = await self._run_backlog_loop(
                request,
                context,
                register_child_runs=register_child_runs,
                sequential_source_pr=sequential_source_pr,
                after_current_controls=after_current_controls,
                final_closure_enabled=final_closure_enabled,
                global_child_blocker_return=global_child_blocker_return,
                register_run=register_run,
            )
            back_failure = await self._finish_back_slot()
            if back_failure is not None:
                raise back_failure
            return outcome
        except _LoopIssueSkipped as skipped:
            # NEW-162: a skip always closes its own loop (it used to re-raise the step's
            # error, failing the loop and the whole campaign).
            back_failure = await self._finish_back_slot()
            if back_failure is not None:
                await self._record_loop_failed(request, register_run)
                raise back_failure from None
            return await self._complete_after_skip(request, skipped.skipped, register_run)
        except Exception:
            # Without this, an uncaught exception (e.g. a non-retryable
            # ApplicationError from an activity or child workflow) leaves the
            # campaign stuck at status RUNNING forever with no Run row to
            # reconcile against -- see NEW-16. Deliberately covers
            # backlog.register_run's own call too (NEW-16 follow-up,
            # 2026-09-22): backlog.load_execution already flips campaign.status
            # to RUNNING before this point, so if register_run itself fails
            # (as it did in production the first time this whole path was ever
            # exercised for real -- a missing dispatch-allowlist entry),
            # nothing else will ever reset that status, and the campaign
            # blocks GitHub write-policy ownership for every other campaign
            # until its ~24h approval TTL expires, with no exposed way to
            # release it early. record_loop_state handles a still-missing Run
            # row gracefully (skips updating it) and unconditionally resets
            # campaign.status regardless, so this is safe even though
            # register_run hasn't necessarily succeeded.
            await self._record_loop_failed(request, register_run)
            raise

    async def _record_loop_failed(
        self, request: ExistingGitHubBacklogLoopInput, register_run: bool
    ) -> None:
        if register_run:
            await self._step(
                "backlog.record_loop_state",
                {
                    **_backlog_execution_payload(request),
                    "workflow_id": workflow.info().workflow_id,
                    "loop_status": "FAILED",
                },
            )

    async def _run_backlog_loop(
        self,
        request: ExistingGitHubBacklogLoopInput,
        context: dict[str, Any],
        *,
        register_child_runs: bool,
        sequential_source_pr: bool,
        after_current_controls: bool,
        final_closure_enabled: bool,
        global_child_blocker_return: bool,
        register_run: bool,
    ) -> dict[str, Any]:
        raw_items = context.get("items")
        if not isinstance(raw_items, list):
            raise TypeError("existing backlog selection must be a list")
        results: list[dict[str, Any]] = []
        self._loop_results = results
        # NEW-114: issues actually delivered; a loop ending with none says so.
        delivered = 0
        # NEW-214b: approved concurrency 2 pipelines issues: one FRONT slot (revalidation,
        # maker/checker) beside one BACK slot (CI, merge, staging). The marker is asked only
        # for concurrency 2, which no earlier history recorded.
        pipelined = (
            request.maximum_concurrency in (2, 3)
            and request.global_campaign_id is None
            and request.target_repository is None
            and request.delivery_mode == "full-delivery"
            and sequential_source_pr
            and after_current_controls
            and workflow.patched("existing-backlog-pipelined-v1")
        )
        back: dict[str, Any] | None = None
        # NEW-214b: issues this loop delivered; a pipelined issue needs its dependencies
        # among the earlier issues delivered (SKIPPED does not count), as the design says.
        delivered_refs: set[str] = set()
        # NEW-214 Stage 2: FRONT lanes; 1 is the NEW-214b pipeline (concurrency 2).
        lanes = 1
        if pipelined:
            refused = await self._pipeline_prerequisites(request, register_run=register_run)
            if refused is not None:
                return {**refused, "results": results}
            self._pipelined = True
            self._front_busy = True
            if request.maximum_concurrency == 3 and workflow.patched(
                "existing-backlog-front-lanes-v1"
            ):
                # Asked only for concurrency 3, which no earlier history recorded.
                self._front_capacity_payload = {
                    "project": request.project,
                    "maximum_concurrency": request.maximum_concurrency,
                }
                capacity = await self._step("delivery.front_capacity", self._front_capacity_payload)
                lanes = 2 if capacity.get("front_slots") == 2 else 1
                self._front_lanes = lanes

        async def drain_back() -> dict[str, Any] | None:
            """NEW-214b: let the BACK slot finish, then apply its outcome to the loop."""
            nonlocal back, delivered
            if back is None:
                return None
            current, back = back, None
            if _in_front_lane():
                # NEW-214 Stage 2: idle meanwhile; resumes once no slot awaits the operator.
                try:
                    await self._lane_wait(current["task"].done)
                except _LaneStopped:
                    back = current  # the coordinator applies its outcome (e.g. CANCELLED)
                    raise
                staged, staged_blocker = current["task"].result()
                self._back_task = None
            else:
                self._front_busy = False
                try:
                    staged, staged_blocker = await current["task"]
                finally:
                    self._front_busy = True
                    self._back_task = None
            if staged == "REPAIR":
                # NEW-214 Stage 2: BACK gave the repair up and queued it (_back_slot) for a
                # FRONT lane; the PR then rejoins the hand-over queue.
                return None
            return await apply_back_outcome(current, staged, staged_blocker)

        async def apply_back_outcome(
            current: dict[str, Any], staged: str, staged_blocker: str
        ) -> dict[str, Any] | None:
            nonlocal delivered
            if staged == "CANCELLED":
                return {"status": "CANCELLED", "results": results}
            if staged == "BLOCKED":
                return {"status": "BLOCKED", "blocker": staged_blocker, "results": results}
            if staged == "DONE":
                delivered += 1
                self._loop_delivered = delivered
                delivered_refs.add(current["ref"])
            elif staged == "SKIPPED" and self._free_slots_patched(current["ordinal"]):
                await self._refill_free_slot(request, raw_items, register_run=register_run)
            return None

        async def hand_over(
            ref: str,
            ordinal: int,
            arguments: dict[str, Any],
            resume: dict[str, Any] | None = None,
        ) -> dict[str, Any] | None:
            """NEW-214b: start this issue's BACK once the previous one ended. NEW-214 Stage
            2: FRONT lanes hand over in the order their checkers passed (FIFO), and their
            BACK gives repairs up to a lane."""
            nonlocal back
            if lanes >= 2:
                await self._handoff_turn(ref)
            try:
                terminal = await drain_back()
                if terminal is not None:
                    return terminal
                launch = (
                    {**arguments, "repair_mode": "yield", "resume": resume}
                    if lanes >= 2
                    else arguments
                )
                task = asyncio.create_task(self._back_slot(ref, **launch))
                self._back_task = task
                self._back_ref = ref
                back = {"task": task, "ordinal": ordinal, "ref": ref, "arguments": arguments}
            finally:
                if lanes >= 2:
                    self._handoff_done()
            return None

        # raw_items may grow while iterating: an issue that frees its slot (REJECTED,
        # duplicate, skipped...) is replaced by the next eligible one (_refill_free_slot).
        def back_in_flight() -> bool:
            return back is not None

        async def front_item(ordinal: int, item: Any) -> dict[str, Any] | None:
            """One issue's FRONT (the loop body). NEW-214 Stage 2: FRONT lanes run it too."""
            nonlocal back, delivered
            if item is _BACK_DRAIN_POINT:
                # NEW-214b: the selected issues ran out with a BACK slot in flight; its
                # outcome may refill a slot, whose issue this loop then still works on.
                terminal = await drain_back()
                if terminal is not None:
                    return terminal
                return None
            if not isinstance(item, dict):
                raise TypeError("existing backlog issue must be an object")
            # NEW-127: workflow state only (no command); read by _step for loop-state notices.
            self._loop_issue = f"{item.get('repository')}#{item.get('issue_number')}"
            if pipelined:
                dependencies = {str(value) for value in item.get("dependencies") or []}
                delivered_now = delivered_refs
                if lanes >= 2:
                    # NEW-214 Stage 2: wait (idle) until no dependency is in another slot.
                    await self._lane_wait(lambda: not dependencies & self._in_flight_refs())
                    delivered_now = delivered_refs | {
                        ref for ref, staged in self._back_outcomes.items() if staged == "DONE"
                    }
                elif back is not None and back["ref"] in dependencies:
                    # NEW-214b: an issue never starts while one it depends on is in flight.
                    terminal = await drain_back()
                    if terminal is not None:
                        return terminal
                earlier = {
                    f"{other.get('repository')}#{other.get('issue_number')}"
                    for other in raw_items[: ordinal - 1]
                    if isinstance(other, dict)
                }
                undelivered = sorted((dependencies & earlier) - delivered_now)
                if undelivered:
                    results.append(
                        {
                            "repository": item.get("repository"),
                            "issue_number": item.get("issue_number"),
                            "status": "NOT_STARTED",
                            "blocker": "DEPENDENCY_NOT_DELIVERED",
                            "dependencies": undelivered,
                        }
                    )
                    return None
            continue_to_next_issue = False
            gate_skipped = False
            revalidation_round = 0
            # NEW-91: a reload that returns no items (for example the campaign approval no
            # longer current) pauses on its blocker instead of reusing the stale item.
            reload_blocker: str | None = None
            while True:
                if not await self._checkpoint(f"BACKLOG_ISSUE_{ordinal}"):
                    cancel_state = {
                        **_backlog_execution_payload(request),
                        "loop_status": "CANCELLED",
                    }
                    if register_run:
                        cancel_state["workflow_id"] = workflow.info().workflow_id
                    await self._step("backlog.record_loop_state", cancel_state)
                    return {"status": "CANCELLED", "results": results}
                # NEW-71: nothing refreshed the canonical mirrors for backlog delivery, so
                # revalidation and allocation ran against a weeks-old main (and failed once
                # main moved). Refresh them (controller fetch + push to the worker) first.
                mirror_blocker: str | None = reload_blocker
                reload_blocker = None
                if mirror_blocker is None and workflow.patched(
                    "existing-backlog-refresh-mirrors-v1"
                ):
                    try:
                        await self._step("github.refresh_mirrors", {"project": request.project})
                    except ActivityError:
                        mirror_blocker = "CANONICAL_MIRROR_REFRESH_FAILED"
                if mirror_blocker is not None:
                    gate = {"status": "BLOCKED", "blocker": mirror_blocker}
                else:
                    gate = await self._step(
                        "backlog.revalidate_gate",
                        {**_backlog_execution_payload(request), "item": item},
                    )
                if gate.get("status") == "READY":
                    break
                blocker = str(gate.get("blocker") or gate.get("status") or "BLOCKED")
                # NEW-114: a failed revalidation child pauses the loop (never moves on).
                revalidation_failed = False
                verification: dict[str, Any] = {}
                if blocker in {
                    "CURRENT_SOURCE_REVALIDATION_REQUIRED",
                    "REVALIDATED_ISSUE_FINDING_MATERIALIZATION_REQUIRED",
                }:
                    candidate = await self._loop_step(
                        request,
                        item,
                        "backlog.materialize_candidate",
                        {**_backlog_execution_payload(request), "item": item},
                        register_run=register_run,
                    )
                    verification_workflow_id = (
                        f"{workflow.info().workflow_id}-issue-{ordinal}-"
                        f"revalidate-{item['issue_number']}"
                    )
                    if revalidation_round:
                        # NEW-74: a retried revalidation reused the child id and its run
                        # registration refused the new binding, failing the loop.
                        verification_workflow_id = (
                            f"{verification_workflow_id}-r{revalidation_round}"
                        )
                    revalidation_round += 1
                    if register_child_runs:
                        await self._loop_step(
                            request,
                            item,
                            "backlog.register_child_run",
                            {
                                **_backlog_execution_payload(request),
                                "child_kind": "finding-verification",
                                "child_workflow_id": verification_workflow_id,
                                "finding_id": str(candidate["finding_id"]),
                            },
                            register_run=register_run,
                        )
                    try:
                        verification = await workflow.execute_child_workflow(
                            "FindingVerificationWorkflow",
                            FindingVerificationInput(
                                finding_id=str(candidate["finding_id"]),
                                project=request.project,
                                fresh_snapshot=True,
                            ),
                            id=verification_workflow_id,
                            task_queue=workflow.info().task_queue,
                            execution_timeout=timedelta(hours=4),
                            **self._child_run_options(verification_workflow_id),
                        )
                    except ChildWorkflowError:
                        # NEW-72: a failed revalidation child (e.g. a stale mirror) crashed
                        # the whole loop; it now pauses for the operator like any blocker.
                        verification = {"status": "CHILD_FAILED"}
                    else:
                        await self._loop_step(
                            request,
                            item,
                            "backlog.record_revalidation",
                            {
                                **_backlog_execution_payload(request),
                                "item": item,
                                "verification": verification,
                            },
                            register_run=register_run,
                        )
                    refreshed = await self._step(
                        "backlog.load_execution", _backlog_execution_payload(request)
                    )
                    candidates = refreshed.get("items")
                    stale_reload: str | None = None
                    if isinstance(candidates, list):
                        item = next(
                            (
                                value
                                for value in candidates
                                if isinstance(value, dict)
                                and value.get("repository") == item.get("repository")
                                and value.get("issue_number") == item.get("issue_number")
                            ),
                            item,
                        )
                    elif workflow.patched("existing-backlog-reload-fail-closed-v1"):
                        stale_reload = str(
                            refreshed.get("blocker")
                            or refreshed.get("status")
                            or "BACKLOG_RELOAD_FAILED"
                        )
                    if stale_reload is None and verification.get("status") == "CONFIRMED":
                        continue
                    blocker = stale_reload or (
                        f"CURRENT_REVALIDATION_{verification.get('status', 'FAILED')}"
                    )
                    if stale_reload is None and _revalidation_body_changed(verification):
                        # NEW-217: the issue text changed after import, so it was not
                        # revalidated. Like delivery's approval-bound refresh, this waits for
                        # the operator (amend, or re-import) and never advances silently.
                        # No earlier verification returned this blocker, so older histories
                        # never reach this branch.
                        blocker = ISSUE_BODY_CHANGED_SINCE_IMPORT
                    if (
                        stale_reload is None
                        and verification.get("status", "FAILED") in _REVALIDATION_FAILURE_STATUSES
                        and self._child_failure_pauses_patched(ordinal)
                    ):
                        # NEW-114: a provider error is not a verdict on the issue; the
                        # loop pauses and the operator's retry revalidates it again.
                        revalidation_failed = True
                        blocker = "REVALIDATION_FAILED"
                issue_ref = f"{item['repository']}#{item['issue_number']}"
                # A FAILED revalidation already pauses with its own code. The marker names
                # the retry round too: an operator retry is a new decision point.
                access_blocker = (
                    None
                    if revalidation_failed
                    else self._provider_access_pause(
                        blocker, verification, f"revalidation-{ordinal}-{revalidation_round}"
                    )
                )
                if access_blocker is not None:
                    blocker = access_blocker
                if global_child_blocker_return and request.global_campaign_id is not None:
                    results.append(
                        {
                            "status": "BLOCKED",
                            "blocker": blocker,
                            "repository": item["repository"],
                            "issue_number": item["issue_number"],
                        }
                    )
                    await self._step(
                        "backlog.record_loop_state",
                        {
                            **_backlog_execution_payload(request),
                            "loop_status": "PAUSED",
                            "blocker": blocker,
                        },
                    )
                    return {"status": "BLOCKED", "blocker": blocker, "results": results}
                if (
                    sequential_source_pr
                    and request.continue_after_blocked_issue
                    and not self._requires_operator_pause(blocker)
                    and not revalidation_failed
                    and access_blocker is None
                ):
                    results.append(
                        {
                            "status": "BLOCKED",
                            "blocker": blocker,
                            "repository": item["repository"],
                            "issue_number": item["issue_number"],
                        }
                    )
                    continue_to_next_issue = True
                    break
                await self._slot_pause_gate()
                self._phase = f"AWAITING_OPERATOR:{issue_ref}:{blocker}"
                self._skip_reason = None
                self._retry_step = None
                gate_paused: dict[str, Any] = {
                    **_backlog_execution_payload(request),
                    "loop_status": "PAUSED",
                    "blocker": blocker,
                }
                if register_run:
                    # NEW-70: without the workflow id the loop Run stayed RUNNING and the
                    # retry preflight refused the operator's signal (NEW-33b, gate path).
                    gate_paused["workflow_id"] = workflow.info().workflow_id
                await self._step("backlog.record_loop_state", gate_paused)
                await workflow.wait_condition(
                    lambda: (
                        self._cancelled
                        or self._skip_reason is not None
                        or self._retry_step is not None
                    )
                )
                if self._cancelled:
                    cancel_state = {
                        **_backlog_execution_payload(request),
                        "loop_status": "CANCELLED",
                    }
                    if register_run:
                        cancel_state["workflow_id"] = workflow.info().workflow_id
                    await self._step("backlog.record_loop_state", cancel_state)
                    return {"status": "CANCELLED", "results": results}
                if self._skip_reason is not None:
                    skip_value = self._skip_reason
                    reference, separator, reason = skip_value.rpartition("|")
                    if not separator or reference != issue_ref:
                        self._note_ignored_signal("skip", skip_value, issue_ref)
                        self._skip_reason = None
                        continue
                    skipped = await self._step(
                        "backlog.record_skip",
                        {
                            **_backlog_execution_payload(request),
                            "repository": item["repository"],
                            "issue_number": item["issue_number"],
                            "reason_code": reason,
                        },
                    )
                    results.append(skipped)
                    self._skip_reason = None
                    gate_skipped = True
                    break
                if self._retry_step != issue_ref:
                    self._note_ignored_signal("retry", self._retry_step, issue_ref)
                    self._retry_step = None
                    continue
                self._retry_step = None
                if workflow.patched("existing-backlog-gate-retry-resume-v1"):
                    # NEW-91: the gate pause set the campaign PAUSED, which voids its
                    # approval; without RUNNING the reload below returned no items and the
                    # loop re-ran revalidation on its stale copy of the item.
                    resumed: dict[str, Any] = {
                        **_backlog_execution_payload(request),
                        "loop_status": "RUNNING",
                    }
                    if register_run:
                        resumed["workflow_id"] = workflow.info().workflow_id
                    await self._step("backlog.record_loop_state", resumed)
                refreshed = await self._step(
                    "backlog.load_execution", _backlog_execution_payload(request)
                )
                candidates = refreshed.get("items")
                if isinstance(candidates, list):
                    item = next(
                        (
                            candidate
                            for candidate in candidates
                            if isinstance(candidate, dict)
                            and candidate.get("repository") == item.get("repository")
                            and candidate.get("issue_number") == item.get("issue_number")
                        ),
                        item,
                    )
                elif workflow.patched("existing-backlog-reload-fail-closed-v1"):
                    reload_blocker = str(
                        refreshed.get("blocker")
                        or refreshed.get("status")
                        or "BACKLOG_RELOAD_FAILED"
                    )
            # NEW-114: the "skipped" check used to read whichever result came last, so an
            # issue following a skipped one was itself skipped without any delivery.
            stale_skip = (
                not continue_to_next_issue
                and not gate_skipped
                and bool(results)
                and results[-1].get("status") == "SKIPPED"
            )
            if (
                continue_to_next_issue
                or gate_skipped
                or (stale_skip and not self._free_slots_patched(ordinal))
            ):
                if _frees_slot(results[-1]) and self._free_slots_patched(ordinal):
                    await self._refill_free_slot(request, raw_items, register_run=register_run)
                boundary = await self._after_issue_boundary(
                    request,
                    results,
                    enabled=after_current_controls,
                    has_more=ordinal < len(raw_items),
                )
                if boundary is not None:
                    return boundary
                return None
            delivery: dict[str, Any] = {"status": "FAILED", "blocker": "DELIVERY_NOT_STARTED"}
            # NEW-33: a blocked delivery used to record PAUSED and then end the workflow,
            # leaving a PAUSED campaign no signal could reach (resume/retry/skip target
            # this workflow), still owning the GitHub write policy. Now it really waits.
            operator_pause = workflow.patched("existing-backlog-delivery-operator-pause-v1")
            delivery_round = 0
            advance_to_next_issue = False
            delivery_skipped = False
            while True:
                issue_prefix = f"{workflow.info().workflow_id}-issue-{ordinal}"
                if delivery_round:
                    issue_prefix = f"{issue_prefix}-retry-{delivery_round}"
                budget_blocker: str | None = None
                if sequential_source_pr:
                    delivery = {"status": "FAILED", "blocker": "DELIVERY_NOT_STARTED"}
                    for attempt in range(1, request.correction_limit + 2):
                        delivery_workflow_id = f"{issue_prefix}-attempt-{attempt}"
                        child_approval_id: str | None = None
                        if register_child_runs:
                            child_registration = await self._loop_step(
                                request,
                                item,
                                "backlog.register_child_run",
                                {
                                    **_backlog_execution_payload(request),
                                    "child_kind": "issue-delivery",
                                    "child_workflow_id": delivery_workflow_id,
                                    "finding_id": str(gate["finding_id"]),
                                },
                                register_run=register_run,
                            )
                            child_approval_id = child_registration.get("issue_approval_id")
                        delivery = await self._issue_delivery(
                            gate, delivery_workflow_id, child_approval_id
                        )
                        results.append({**delivery, "attempt": attempt})
                        if delivery.get("status") != "REQUEST_CHANGES":
                            break
                        budget_blocker = await self._retry_round_budget_blocker(
                            request,
                            item,
                            delivery,
                            ordinal,
                            delivery_round,
                            register_run=register_run,
                        )
                        if budget_blocker is not None:
                            break
                else:
                    delivery_workflow_id = issue_prefix
                    child_approval_id = None
                    if register_child_runs:
                        child_registration = await self._loop_step(
                            request,
                            item,
                            "backlog.register_child_run",
                            {
                                **_backlog_execution_payload(request),
                                "child_kind": "issue-delivery",
                                "child_workflow_id": delivery_workflow_id,
                                "finding_id": str(gate["finding_id"]),
                            },
                            register_run=register_run,
                        )
                        child_approval_id = child_registration.get("issue_approval_id")
                    delivery = await self._issue_delivery(
                        gate, delivery_workflow_id, child_approval_id
                    )
                    results.append(delivery)
                pause_blocker: str | None = None
                if delivery.get("status") in {"BLOCKED", "FAILED", "RATE_LIMITED", "TIMED_OUT"}:
                    blocker = str(delivery.get("blocker") or delivery.get("status"))
                    blocker = _approval_lifetime_blocker(delivery) or blocker
                    # NEW-114: a FAILED child (e.g. a non-retryable PolicyViolation) is not
                    # a verdict on the issue: pause with its code instead of moving on.
                    delivery_failed = delivery.get(
                        "status"
                    ) == "FAILED" and self._child_failure_pauses_patched(ordinal)
                    if delivery_failed:
                        blocker = _delivery_failure_blocker(delivery)
                        if self._provider_unavailable_pause(delivery, ordinal, delivery_round):
                            # NEW-137: the release recorded REQUEST_CHANGES; a retry binds.
                            blocker = f"DELIVERY_FAILED:{PROVIDER_UNAVAILABLE}"
                    # A FAILED child already pauses with its own code.
                    access_blocker = (
                        None
                        if delivery_failed
                        else self._provider_access_pause(
                            blocker, delivery, f"delivery-{ordinal}-{delivery_round}"
                        )
                    )
                    if access_blocker is not None:
                        blocker = access_blocker
                    if global_child_blocker_return and request.global_campaign_id is not None:
                        await self._step(
                            "backlog.record_loop_state",
                            {
                                **_backlog_execution_payload(request),
                                "loop_status": "PAUSED",
                                "blocker": blocker,
                            },
                        )
                        return {"status": "BLOCKED", "blocker": blocker, "results": results}
                    if (
                        sequential_source_pr
                        and request.continue_after_blocked_issue
                        and not self._requires_operator_pause(blocker)
                        and not delivery_failed
                        and access_blocker is None
                    ):
                        advance_to_next_issue = True
                        break
                    pause_blocker = blocker
                elif sequential_source_pr and delivery.get("status") == "REQUEST_CHANGES":
                    # NEW-133: a retry round stops once its ChangeSet's lineage is used up.
                    exhausted = budget_blocker or "CORRECTION_LIMIT_EXHAUSTED"
                    if global_child_blocker_return and request.global_campaign_id is not None:
                        await self._step(
                            "backlog.record_loop_state",
                            {
                                **_backlog_execution_payload(request),
                                "loop_status": "PAUSED",
                                "blocker": exhausted,
                            },
                        )
                        return {
                            "status": "BLOCKED",
                            "blocker": exhausted,
                            "results": results,
                        }
                    pause_blocker = exhausted
                if pause_blocker is None:
                    break
                paused_state = {
                    **_backlog_execution_payload(request),
                    "loop_status": "PAUSED",
                    "blocker": pause_blocker,
                }
                if operator_pause and register_run:
                    # NEW-33b: the loop Run must read PAUSED, or the retry/resume
                    # preflight (which reads Run.status) refuses the operator's signal.
                    paused_state["workflow_id"] = workflow.info().workflow_id
                await self._slot_pause_gate()
                await self._step("backlog.record_loop_state", paused_state)
                if not operator_pause:
                    return {"status": "PAUSED", "results": results}
                resumed_state = {**_backlog_execution_payload(request), "loop_status": "RUNNING"}
                if register_run:
                    resumed_state["workflow_id"] = workflow.info().workflow_id
                outcome, skipped = await self._await_delivery_operator(
                    request, item, pause_blocker, register_run=register_run
                )
                if outcome == "CANCELLED":
                    return {"status": "CANCELLED", "results": results}
                if outcome == "RETRY":
                    delivery_round += 1
                    await self._step("backlog.record_loop_state", resumed_state)
                    continue
                results.append(skipped)
                await self._step("backlog.record_loop_state", resumed_state)
                advance_to_next_issue = True
                delivery_skipped = True
                break
            if advance_to_next_issue:
                if delivery_skipped and self._free_slots_patched(ordinal):
                    await self._refill_free_slot(request, raw_items, register_run=register_run)
                boundary = await self._after_issue_boundary(
                    request,
                    results,
                    enabled=after_current_controls,
                    has_more=ordinal < len(raw_items),
                )
                if boundary is not None:
                    return boundary
                return None
            if request.delivery_mode == "source-pr":
                if sequential_source_pr:
                    delivered += 1
                    self._loop_delivered = delivered
                    boundary = await self._after_issue_boundary(
                        request,
                        results,
                        enabled=after_current_controls,
                        has_more=ordinal < len(raw_items),
                    )
                    if boundary is not None:
                        return boundary
                    return None
                return {"status": "AWAITING_STAGING_DELIVERY", "results": results}
            if not workflow.patched("existing-backlog-full-delivery-orchestration-v1"):
                return {
                    "status": "BLOCKED_BY_DELIVERY",
                    "blocker": "FULL_DELIVERY_REQUIRES_PROJECT_BUILD_AND_STAGING_GATES",
                    "results": results,
                }
            if workflow.patched("existing-backlog-staging-gitops-v1"):
                flow_state = await self._step(
                    "gitops.flow_for_project", {"project": request.project}
                )
                if flow_state.get("enabled") is True and pipelined:
                    # NEW-214b: BACK stays serialized (merges and deploys one at a time);
                    # this issue's BACK starts once the previous one ended, and the FRONT
                    # moves on to the next issue meanwhile. NEW-214 Stage 2: FRONT lanes
                    # hand over in the order their checkers passed (FIFO).
                    item_ref = _issue_ref(item)
                    terminal = await hand_over(
                        item_ref,
                        ordinal,
                        {
                            "request": request,
                            "item": item,
                            "finding_id": str(gate["finding_id"]),
                            "ordinal": ordinal,
                            "delivery": delivery,
                            "results": results,
                            "register_child_runs": register_child_runs,
                            "register_run": register_run,
                            "global_child_blocker_return": global_child_blocker_return,
                            "gate": gate,
                        },
                    )
                    if terminal is not None:
                        return terminal
                    if lanes < 2 and (self._stop_after_current or self._pause_after_current):
                        # Stop and pause-after-current carry the started BACK through first
                        # (with lanes, the coordinator does once every lane is between
                        # issues).
                        terminal = await drain_back()
                        if terminal is not None:
                            return terminal
                    boundary = await self._after_issue_boundary(
                        request,
                        results,
                        enabled=after_current_controls,
                        has_more=ordinal < len(raw_items),
                    )
                    if boundary is not None:
                        return boundary
                    return None
                if flow_state.get("enabled") is True:
                    # Post-merge staging GitOps flow replaces the pre-merge candidate
                    # pipeline entirely; _execute_full_delivery is never reached.
                    staged, staged_blocker = await self._staging_gitops_delivery(
                        request=request,
                        item=item,
                        finding_id=str(gate["finding_id"]),
                        ordinal=ordinal,
                        delivery=delivery,
                        results=results,
                        register_child_runs=register_child_runs,
                        register_run=register_run,
                        global_child_blocker_return=global_child_blocker_return,
                        gate=gate,
                    )
                    if staged == "CANCELLED":
                        return {"status": "CANCELLED", "results": results}
                    if staged == "BLOCKED":
                        return {"status": "BLOCKED", "blocker": staged_blocker, "results": results}
                    if staged == "DONE":
                        delivered += 1
                        self._loop_delivered = delivered
                    elif staged == "SKIPPED" and self._free_slots_patched(ordinal):
                        # NEW-114 review: an operator skip of the GitOps stage frees the
                        # issue's slot like any other skip.
                        await self._refill_free_slot(request, raw_items, register_run=register_run)
                    boundary = await self._after_issue_boundary(
                        request,
                        results,
                        enabled=after_current_controls,
                        has_more=ordinal < len(raw_items),
                    )
                    if boundary is not None:
                        return boundary
                    return None
            full_delivery = await self._execute_full_delivery(
                request=request,
                finding_id=str(gate["finding_id"]),
                ordinal=ordinal,
                delivery=delivery,
                results=results,
                register_child_runs=register_child_runs,
            )
            if full_delivery["status"] != "RUNTIME_VERIFIED":
                await self._step(
                    "backlog.record_loop_state",
                    {
                        **_backlog_execution_payload(request),
                        "loop_status": "PAUSED",
                        "blocker": full_delivery["blocker"],
                    },
                )
                return {
                    "status": "BLOCKED",
                    "blocker": full_delivery["blocker"],
                    "results": results,
                }
            if not final_closure_enabled:
                blocker = "FINAL_MERGED_REVISION_DELIVERY_SERVICE_REQUIRED"
                await self._step(
                    "backlog.record_loop_state",
                    {
                        **_backlog_execution_payload(request),
                        "loop_status": "PAUSED",
                        "blocker": blocker,
                    },
                )
                return {"status": "BLOCKED", "blocker": blocker, "results": results}
            closure_id = f"{workflow.info().workflow_id}-issue-{ordinal}-final-closure"
            closure = await workflow.execute_child_workflow(
                "FinalClosureWorkflow",
                FinalClosureInput(
                    backlog_campaign_id=request.backlog_campaign_id,
                    change_set_id=full_delivery["change_set_id"],
                    finding_id=str(gate["finding_id"]),
                    project=request.project,
                    issue_repository=str(item["repository"]),
                    issue_number=int(item["issue_number"]),
                    r3_approval_binding=(
                        request.r3_approval_binding if str(gate["risk_class"]) == "R3" else None
                    ),
                ),
                id=closure_id,
                task_queue=workflow.info().task_queue,
                execution_timeout=timedelta(days=7),
                **self._child_run_options(closure_id),
            )
            results.append({**closure, "stage": "final-closure"})
            if closure.get("status") != "FIXED":
                blocker = str(
                    closure.get("blocker") or closure.get("status") or "FINAL_CLOSURE_FAILED"
                )
                await self._step(
                    "backlog.record_loop_state",
                    {
                        **_backlog_execution_payload(request),
                        "loop_status": "PAUSED",
                        "blocker": blocker,
                    },
                )
                return {"status": "BLOCKED", "blocker": blocker, "results": results}
            delivered += 1
            self._loop_delivered = delivered
            delivered_refs.add(_issue_ref(item))
            boundary = await self._after_issue_boundary(
                request,
                results,
                enabled=after_current_controls,
                has_more=ordinal < len(raw_items),
            )
            if boundary is not None:
                return boundary
            return None

        cursor = 0  # NEW-214 Stage 2: the next issue a FRONT lane takes, in plan order

        async def front_repair(entry: dict[str, Any]) -> dict[str, Any] | None:
            """NEW-214 Stage 2: repair a PR its BACK gave up, then hand it back to BACK."""
            ordinal, ref = int(entry["ordinal"]), str(entry["ref"])
            staged, staged_blocker = await self._staging_gitops_delivery(
                **entry["arguments"], repair_mode="only", resume=self._back_resume.get(ordinal)
            )
            if staged == "REPAIRED":
                return await hand_over(
                    ref, ordinal, entry["arguments"], resume=self._back_resume.get(ordinal)
                )
            self._back_outcomes[ref] = staged
            return await apply_back_outcome(entry, staged, staged_blocker)

        def lane_may_proceed() -> bool:
            return (
                bool(self._repair_queue)
                or self._stop_after_current
                or self._pause_after_current
                or cursor >= len(raw_items)
                or not self._waits_for_dependency(raw_items[cursor])
            )

        async def front_lane(lane: str) -> dict[str, Any] | None:
            """One FRONT lane: repairs first, then issues in plan order, until none is left.

            After a stop or pause after the current issue it takes only repairs, so every
            started issue is carried through BACK before the boundary applies.
            """
            nonlocal cursor
            _SLOT_ID.set(lane)
            self._lanes_busy.add(lane)
            try:
                while True:
                    # Between issues: wait while another slot awaits the operator, and
                    # while the next issue in plan order depends on one still in a slot
                    # (repairs, which may be what it waits for, are taken meanwhile).
                    await self._lane_wait(lane_may_proceed)
                    entry: dict[str, Any] | None = None
                    if self._repair_queue:
                        entry = self._repair_queue.pop(0)
                        ordinal, item = int(entry["ordinal"]), entry["arguments"]["item"]
                    elif (
                        self._stop_after_current
                        or self._pause_after_current
                        or cursor >= len(raw_items)
                    ):
                        return None
                    else:
                        cursor += 1
                        ordinal, item = cursor, raw_items[cursor - 1]
                    ref = _issue_ref(item) if isinstance(item, dict) else ""
                    _SLOT_ISSUE.set(ref)  # this lane's loop-state notices name its issue
                    self._front_refs.add(ref)
                    self._lane_ordinals[lane] = ordinal
                    try:
                        outcome = await (
                            front_repair(entry) if entry is not None else front_item(ordinal, item)
                        )
                    except _LoopIssueSkipped as skipped:
                        # A skipped bookkeeping step ends only this lane's issue.
                        results.append(skipped.skipped)
                        if self._free_slots_patched(ordinal):
                            await self._refill_free_slot(
                                request, raw_items, register_run=register_run
                            )
                        outcome = None
                    finally:
                        self._front_refs.discard(ref)
                        self._handoff_tickets.pop(lane, None)
                    await self._end_operator_turn(lane)
                    if outcome is not None:
                        self._loop_ending = True
                        return outcome
            except _LaneStopped:
                return None
            except BaseException:
                self._loop_ending = True
                raise
            finally:
                self._lanes_busy.discard(lane)
                if self._operator_holder == lane and self._loop_ending:
                    self._release_operator(lane)

        async def run_front_lanes() -> dict[str, Any] | None:
            """NEW-214 Stage 2: run the FRONT lanes; apply boundaries between issues."""
            self._loop_request = request
            while True:
                outcomes = await asyncio.gather(
                    *(asyncio.create_task(front_lane(f"front-{index}")) for index in range(lanes)),
                    return_exceptions=True,
                )
                failures = [value for value in outcomes if isinstance(value, BaseException)]
                if failures:
                    raise failures[0]
                for outcome in outcomes:
                    if isinstance(outcome, dict):
                        return outcome
                # Every lane is between issues: carry the last BACK through. It may free and
                # refill a slot, or give a repair up to the lanes.
                terminal = await drain_back()
                if terminal is not None:
                    return terminal
                if self._repair_queue:
                    continue
                has_more = cursor < len(raw_items)
                if has_more and (self._stop_after_current or self._pause_after_current):
                    # Stop or pause as a sequential loop does after its current issue.
                    boundary = await self._after_issue_boundary(
                        request, results, enabled=after_current_controls, has_more=True
                    )
                    if boundary is not None:
                        return boundary
                    continue
                if not has_more:
                    return None

        if lanes < 2:
            for ordinal, item in _live_loop_items(raw_items, back_in_flight):
                outcome = await front_item(ordinal, item)
                if outcome is not None:
                    return outcome
        else:
            outcome = await run_front_lanes()
            if outcome is not None:
                return outcome
        terminal = await drain_back()
        if terminal is not None:
            return terminal
        complete_state = {**_backlog_execution_payload(request), "loop_status": "COMPLETE"}
        if register_run:
            complete_state["workflow_id"] = workflow.info().workflow_id
        nothing_delivered = (
            delivered == 0
            and request.global_campaign_id is None
            and self._nothing_delivered_patched()
        )
        if nothing_delivered:
            # NEW-114: a loop whose every issue was rejected, skipped or blocked used to
            # read COMPLETE like a successful one; it now says so and alerts the operator.
            self._phase = "COMPLETE_NOTHING_DELIVERED"
            complete_state["completion"] = "COMPLETE_NOTHING_DELIVERED"
            complete_state["outcomes"] = _loop_outcomes(results)
            if self._refill_blocker is not None:
                complete_state["refill_blocker"] = self._refill_blocker
        await self._step("backlog.record_loop_state", complete_state)
        if nothing_delivered:
            return {
                "status": "COMPLETE",
                "completion": "COMPLETE_NOTHING_DELIVERED",
                "delivered": 0,
                "results": results,
            }
        return {"status": "COMPLETE", "results": results}

    async def _execute_full_delivery(
        self,
        *,
        request: ExistingGitHubBacklogLoopInput,
        finding_id: str,
        ordinal: int,
        delivery: dict[str, Any],
        results: list[dict[str, Any]],
        register_child_runs: bool,
    ) -> dict[str, str]:
        """Drive the existing immutable candidate through staging verification.

        This method owns only the already-reviewed candidate pipeline.  It never
        merges a PR or closes an issue, and returns a precise blocker whenever a
        project build, approval, release identity, observer, or verification gate
        is unavailable.
        """
        delivery_context = delivery.get("context")
        change_set_id = (
            str(delivery_context.get("change_set_id", ""))
            if isinstance(delivery_context, dict)
            else ""
        )
        if not change_set_id:
            return {"status": "BLOCKED", "blocker": "CHANGE_SET_BINDING_MISSING"}

        workflow_root = workflow.info().workflow_id
        task_queue = workflow.info().task_queue
        stages: tuple[tuple[str, str, Any], ...] = (
            (
                "delivery-reconciliation",
                "DeliveryReconciliationWorkflow",
                DeliveryReconciliationInput(change_set_id=change_set_id, dry_run=False),
            ),
        )
        current: dict[str, Any] = delivery
        for child_kind, workflow_type, child_input in stages:
            child_id = f"{workflow_root}-issue-{ordinal}-{child_kind}"
            if register_child_runs:
                await self._step(
                    "backlog.register_child_run",
                    {
                        **_backlog_execution_payload(request),
                        "child_kind": child_kind,
                        "child_workflow_id": child_id,
                        "finding_id": finding_id,
                        "change_set_id": change_set_id,
                    },
                )
            current = await workflow.execute_child_workflow(
                workflow_type,
                child_input,
                id=child_id,
                task_queue=task_queue,
                execution_timeout=timedelta(hours=12),
                **self._child_run_options(child_id),
            )
            results.append({**current, "stage": child_kind})
            if current.get("status") != "DEPLOY_PR":
                return {
                    "status": "BLOCKED",
                    "blocker": str(
                        current.get("blocker")
                        or current.get("status")
                        or "DELIVERY_RECONCILIATION_FAILED"
                    ),
                }

        staging_id = f"{workflow_root}-issue-{ordinal}-staging-deployment"
        if register_child_runs:
            await self._step(
                "backlog.register_child_run",
                {
                    **_backlog_execution_payload(request),
                    "child_kind": "staging-deployment",
                    "child_workflow_id": staging_id,
                    "finding_id": finding_id,
                    "change_set_id": change_set_id,
                },
            )
        staging = await workflow.execute_child_workflow(
            "StagingDeploymentWorkflow",
            StagingDeploymentInput(
                change_set_id=change_set_id, environment="staging", dry_run=False
            ),
            id=staging_id,
            task_queue=task_queue,
            execution_timeout=timedelta(hours=12),
            **self._child_run_options(staging_id),
        )
        results.append({**staging, "stage": "staging-deployment"})
        if staging.get("status") != "STAGING_DEPLOYED":
            return {
                "status": "BLOCKED",
                "blocker": str(
                    staging.get("blocker") or staging.get("status") or "STAGING_DEPLOYMENT_FAILED"
                ),
            }
        staging_context = staging.get("context")
        deployment_id = (
            str(staging_context.get("deployment_id", ""))
            if isinstance(staging_context, dict)
            else ""
        )
        if not deployment_id:
            return {"status": "BLOCKED", "blocker": "DEPLOYMENT_BINDING_MISSING"}

        verification_id = f"{workflow_root}-issue-{ordinal}-runtime-verification"
        if register_child_runs:
            await self._step(
                "backlog.register_child_run",
                {
                    **_backlog_execution_payload(request),
                    "child_kind": "runtime-verification",
                    "child_workflow_id": verification_id,
                    "finding_id": finding_id,
                    "change_set_id": change_set_id,
                    "deployment_id": deployment_id,
                },
            )
        verified = await workflow.execute_child_workflow(
            "RuntimeVerificationWorkflow",
            RuntimeVerificationInput(deployment_id=deployment_id, project=request.project),
            id=verification_id,
            task_queue=task_queue,
            execution_timeout=timedelta(hours=12),
            **self._child_run_options(verification_id),
        )
        results.append({**verified, "stage": "runtime-verification"})
        if verified.get("status") != "RUNTIME_VERIFIED":
            return {
                "status": "BLOCKED",
                "blocker": str(
                    verified.get("blocker")
                    or verified.get("status")
                    or "RUNTIME_VERIFICATION_FAILED"
                ),
            }
        return {
            "status": "RUNTIME_VERIFIED",
            "blocker": "",
            "change_set_id": change_set_id,
            "deployment_id": deployment_id,
        }

    async def _staging_gitops_delivery(
        self,
        *,
        request: ExistingGitHubBacklogLoopInput,
        item: dict[str, Any],
        finding_id: str,
        ordinal: int,
        delivery: dict[str, Any],
        results: list[dict[str, Any]],
        register_child_runs: bool,
        register_run: bool,
        global_child_blocker_return: bool,
        gate: dict[str, Any] | None = None,
        repair_mode: Literal["inline", "yield", "only"] = "inline",
        resume: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        """Run the post-merge staging GitOps child; hold for the operator when it blocks.

        Returns ("DONE"|"SKIPPED"|"CANCELLED"|"BLOCKED", blocker). A retry re-runs
        only the GitOps child (under a fresh child id); skip moves to the next issue.
        NEW-64: when the CI baseline gate attributes PR CI failures to the ChangeSet
        (SOURCE_PR_CI_REGRESSION), the issue delivery re-runs on the same ChangeSet
        (resuming its commits, updating the same PR) before a fresh GitOps child.

        NEW-214 Stage 2: a BACK slot beside FRONT lanes gives the repair up ("yield":
        returns "REPAIR"), so the next published PR can take BACK. A FRONT lane then runs
        only the repair ("only": returns "REPAIRED"), and BACK resumes from the state
        saved in ``_back_resume``. "inline" is the sequential behaviour.
        """
        delivery_context = delivery.get("context")
        change_set_id = (
            str(delivery_context.get("change_set_id", ""))
            if isinstance(delivery_context, dict)
            else ""
        )
        workflow_root = workflow.info().workflow_id
        gitops_round = 0
        repair_round = 0
        repair_pending = False
        restore_pending = False
        # NEW-195: an operator retry of a CI regression rechecks first; this grants the
        # one repair that retry used to start directly, even at the automatic limit.
        operator_repair_grant = False
        # NEW-214a: the pending repair merges a moved base forward (refresh mirrors first).
        merge_forward_pending = False
        if resume is not None:
            gitops_round = int(resume["gitops_round"])
            repair_round = int(resume["repair_round"])
            repair_pending = bool(resume["repair_pending"])
            restore_pending = bool(resume["restore_pending"])
            operator_repair_grant = bool(resume["operator_repair_grant"])
            merge_forward_pending = bool(resume["merge_forward_pending"])

        def save_progress() -> None:
            self._back_resume[ordinal] = {
                "gitops_round": gitops_round,
                "repair_round": repair_round,
                "repair_pending": repair_pending,
                "restore_pending": restore_pending,
                "operator_repair_grant": operator_repair_grant,
                "merge_forward_pending": merge_forward_pending,
            }

        while True:
            staged: dict[str, Any] | None = None
            if restore_pending and change_set_id:
                # NEW-102: a stopped repair left the ChangeSet mid-implementation; return it
                # to its published PRs before the recheck, or pause with the reason.
                restore_blocker = await self._restore_published_change_set(
                    change_set_id, workflow_root
                )
                if restore_blocker is None:
                    restore_pending = False
                else:
                    staged = {"status": "BLOCKED", "blocker": restore_blocker}
            if repair_mode == "yield" and staged is None and repair_pending and gate is not None:
                save_progress()
                return "REPAIR", ""  # a FRONT lane repairs; BACK takes the next PR
            if staged is None and repair_pending and gate is not None:
                repair_round += 1
                authorization_blocker = await self._authorize_ci_repair(
                    request=request,
                    item=item,
                    register_run=register_run,
                    ordinal=ordinal,
                    repair_round=repair_round,
                    change_set_id=change_set_id,
                    workflow_root=workflow_root,
                )
                if authorization_blocker is not None:
                    staged = {"status": "BLOCKED", "blocker": authorization_blocker}
                elif merge_forward_pending:
                    # NEW-214a review: the worker's canonical mirror must hold the base the
                    # PR fell behind; the scan worker never fetches from GitHub itself.
                    try:
                        await self._step("github.refresh_mirrors", {"project": request.project})
                    except ActivityError:
                        staged = {"status": "BLOCKED", "blocker": "CANONICAL_MIRROR_REFRESH_FAILED"}
                if staged is None and authorization_blocker is None:
                    repaired = await self._ci_repair_delivery(
                        request=request,
                        item=item,
                        register_run=register_run,
                        gate=gate,
                        ordinal=ordinal,
                        repair_round=repair_round,
                        results=results,
                        register_child_runs=register_child_runs,
                    )
                    if repaired.get("status") == "SOURCE_PR":
                        repair_pending = False
                        merge_forward_pending = False
                        gitops_round += 1
                    else:
                        reason = str(repaired.get("blocker") or repaired.get("status") or "FAILED")
                        repaired_context = repaired.get("context")
                        context_blocker = (
                            repaired_context.get("blocker")
                            if isinstance(repaired_context, dict)
                            else None
                        )
                        if context_blocker in {
                            "SOURCE_PR_MERGE_FORWARD_NOT_MERGED",
                            "DEPLOYMENT_PR_MERGE_FORWARD_NOT_MERGED",
                        }:
                            # NEW-214a: name the refusal (only this new path reports it).
                            reason = str(context_blocker)
                        # NEW-229: an approval that expired in the repair child is named
                        # (CI_REPAIR_CAMPAIGN_APPROVAL_EXPIRED); the CI_REPAIR_ prefix keeps
                        # the retry's recheck and published-state restore.
                        reason = _approval_lifetime_blocker(repaired) or reason
                        staged = {"status": "BLOCKED", "blocker": f"CI_REPAIR_{reason}"[:120]}
            if repair_mode == "only" and staged is None:
                save_progress()
                return "REPAIRED", ""  # back to BACK for the CI, merge and staging
            if staged is None and change_set_id:
                child_id = f"{workflow_root}-issue-{ordinal}-staging-gitops"
                if gitops_round:
                    child_id = f"{child_id}-retry-{gitops_round}"
                if register_child_runs:
                    await self._loop_step(
                        request,
                        item,
                        "backlog.register_child_run",
                        {
                            **_backlog_execution_payload(request),
                            "child_kind": "staging-gitops",
                            "child_workflow_id": child_id,
                            "finding_id": finding_id,
                            "change_set_id": change_set_id,
                        },
                        register_run=register_run,
                    )
                try:
                    staged = await workflow.execute_child_workflow(
                        "StagingGitOpsDeliveryWorkflow",
                        StagingGitOpsDeliveryInput(
                            change_set_id=change_set_id,
                            project=request.project,
                            issue_repository=str(item["repository"]),
                            issue_number=int(item["issue_number"]),
                            finding_id=finding_id,
                            backlog_campaign_id=request.backlog_campaign_id,
                        ),
                        id=child_id,
                        task_queue=workflow.info().task_queue,
                        execution_timeout=timedelta(hours=12),
                        **self._child_run_options(child_id),
                    )
                except ChildWorkflowError:
                    staged = {"status": "FAILED", "blocker": "STAGING_GITOPS_CHILD_WORKFLOW_FAILED"}
            elif staged is None:
                staged = {"status": "BLOCKED", "blocker": "CHANGE_SET_BINDING_MISSING"}
            results.append({**staged, "stage": "staging-gitops"})
            # NEW-160: MERGE_DELIVERED is a merge-only child's success (no prior history
            # recorded it, so this widening replays every earlier result unchanged).
            if staged.get("status") in {"STAGING_VERIFIED", "MERGE_DELIVERED"}:
                return "DONE", ""
            blocker = str(staged.get("blocker") or staged.get("status") or "STAGING_GITOPS_FAILED")
            if (
                (
                    blocker == "SOURCE_PR_CI_REGRESSION"
                    or (
                        blocker == "DEPLOYMENT_PR_CI_REGRESSION"
                        and self._deployment_ci_repair_patched(ordinal)
                    )
                    or self._merge_forward_repair(blocker, ordinal)
                )
                and gate is not None
                and (
                    # The operator's grant never depends on the automatic-repair marker,
                    # which a pre-NEW-64 history replays as False (NEW-195 review).
                    operator_repair_grant
                    or (repair_round < self._CI_REPAIR_ROUNDS and self._gitops_ci_repair_patched())
                )
            ):
                operator_repair_grant = False
                repair_pending = True
                merge_forward_pending = blocker in _MERGE_FORWARD_BLOCKERS
                continue
            # A grant covers only the recheck its retry started; any pause ends it.
            operator_repair_grant = False
            paused_state = {
                **_backlog_execution_payload(request),
                "loop_status": "PAUSED",
                "blocker": blocker,
            }
            if register_run:
                paused_state["workflow_id"] = workflow_root
            await self._slot_pause_gate()
            await self._step("backlog.record_loop_state", paused_state)
            if global_child_blocker_return and request.global_campaign_id is not None:
                return "BLOCKED", blocker
            outcome, skipped = await self._await_delivery_operator(
                request, item, blocker, register_run=register_run
            )
            if outcome == "CANCELLED":
                return "CANCELLED", blocker
            resumed_state = {**_backlog_execution_payload(request), "loop_status": "RUNNING"}
            if register_run:
                resumed_state["workflow_id"] = workflow_root
            await self._step("backlog.record_loop_state", resumed_state)
            if outcome == "RETRY":
                if (
                    gate is not None
                    and blocker.startswith("CI_REPAIR_")
                    and self._repair_retry_rechecks_patched(ordinal)
                ):
                    # NEW-101: re-check the PR's CI first, so the next repair gets the
                    # current failures and their log excerpts (NEW-100), not stale evidence.
                    # At the automatic limit the recheck pauses on the regression, and the
                    # operator's next retry repairs with that fresh evidence.
                    repair_pending = False
                    gitops_round += 1
                    if self._repair_restores_published_patched(ordinal):
                        restore_pending = True
                elif (
                    gate is not None
                    and (
                        blocker == "SOURCE_PR_CI_REGRESSION"
                        or (
                            blocker == "DEPLOYMENT_PR_CI_REGRESSION"
                            and self._deployment_ci_repair_patched(ordinal)
                        )
                    )
                    and self._regression_retry_rechecks_patched(ordinal)
                ):
                    # NEW-195: the regression's CI evidence may predate a fix on main
                    # (falcone #1087 repaired a test that main had already fixed). Re-check
                    # first: a stale failure reruns on current main (NEW-67 for source PRs,
                    # NEW-195 for deployment PRs). A still-failing check repairs once with
                    # fresh evidence, even at the automatic limit.
                    operator_repair_grant = True
                    gitops_round += 1
                elif gate is not None and (
                    blocker == "SOURCE_PR_CI_REGRESSION"
                    or blocker.startswith("CI_REPAIR_")
                    or (
                        blocker == "DEPLOYMENT_PR_CI_REGRESSION"
                        and self._deployment_ci_repair_patched(ordinal)
                    )
                    or self._merge_forward_repair(blocker, ordinal)
                ):
                    # NEW-64: the operator's retry repeats the CI repair, not only GitOps.
                    repair_pending = True
                    merge_forward_pending = blocker in _MERGE_FORWARD_BLOCKERS
                else:
                    gitops_round += 1
                    if (
                        blocker == "CHANGE_SET_STATE_NOT_DELIVERABLE"
                        and repair_round > 0
                        and self._late_reset_restores_patched(ordinal)
                    ):
                        # NEW-102: a late repair write reset the ChangeSet after its restore;
                        # restore it again before the recheck.
                        restore_pending = True
                continue
            results.append(skipped)
            return "SKIPPED", blocker

    # NEW-64: automatic CI-repair re-deliveries before the loop waits for the operator.
    _CI_REPAIR_ROUNDS = 2

    async def _restore_published_change_set(
        self, change_set_id: str, workflow_root: str
    ) -> str | None:
        """NEW-102: plan, read live PR heads, apply; None when restored, else a blocker.

        Failures become a CI_REPAIR_RESTORE_* pause (retry repeats the restore) instead of
        an activity error that an operator skip or cancel would turn into a failed loop.
        """
        step = "backlog.restore_published_change_set"
        base = {"change_set_id": change_set_id, "workflow_id": workflow_root}
        try:
            plan = await self._step(step, {**base, "phase": "plan"})
            if plan.get("status") != "READY":
                return f"CI_REPAIR_RESTORE_{plan.get('reason') or 'REFUSED'}"[:120]
            heads = await self._step(
                "gitops.pr_heads", {"pull_requests": plan.get("pull_requests") or []}
            )
            if heads.get("status") != "OK":
                return "CI_REPAIR_RESTORE_PR_HEADS_UNAVAILABLE"
            applied = await self._step(
                step,
                {
                    **base,
                    "phase": "apply",
                    "version": plan.get("version"),
                    "live_heads": heads.get("heads") or [],
                },
            )
        except ActivityError:
            return "CI_REPAIR_RESTORE_STEP_FAILED"
        if applied.get("status") != "RESTORED":
            return f"CI_REPAIR_RESTORE_{applied.get('reason') or 'REFUSED'}"[:120]
        return None

    def _deployment_ci_repair_patched(self, ordinal: int) -> bool:
        # NEW-103: deployment-PR CI regressions go through the same bounded CI repair.
        return workflow.patched(f"existing-backlog-deployment-ci-repair-v1-{ordinal}")

    def _merge_forward_repair(self, blocker: str, ordinal: int) -> bool:
        """NEW-214a: a PR behind its base is repaired by merging the base forward.

        gitops only reports these blockers for a project with merge_forward enabled, which no
        earlier history did, so the marker is asked only for them and old histories replay
        unchanged.
        """
        return blocker in _MERGE_FORWARD_BLOCKERS and workflow.patched(
            f"existing-backlog-merge-forward-repair-v1-{ordinal}"
        )

    def _late_reset_restores_patched(self, ordinal: int) -> bool:
        return workflow.patched(f"existing-backlog-late-reset-restores-v1-{ordinal}")

    def _repair_restores_published_patched(self, ordinal: int) -> bool:
        return workflow.patched(f"existing-backlog-ci-repair-restores-published-v1-{ordinal}")

    def _gitops_ci_repair_patched(self) -> bool:
        return workflow.patched("existing-backlog-gitops-ci-repair-v1")

    def _regression_retry_rechecks_patched(self, ordinal: int) -> bool:
        return workflow.patched(f"existing-backlog-regression-retry-rechecks-v1-{ordinal}")

    def _repair_retry_rechecks_patched(self, ordinal: int) -> bool:
        # Temporal memoizes a patch per workflow, so the decision is scoped to the issue:
        # an earlier issue replayed from before the deploy does not pin later ones to it.
        return workflow.patched(f"existing-backlog-ci-repair-retry-rechecks-v1-{ordinal}")

    async def _authorize_ci_repair(
        self,
        *,
        request: ExistingGitHubBacklogLoopInput,
        item: dict[str, Any],
        register_run: bool,
        ordinal: int,
        repair_round: int,
        change_set_id: str,
        workflow_root: str,
    ) -> str | None:
        """NEW-112 review: durably record the repair round before any repair child starts.

        The delivery-child approval accepts a CI-repair child only against this record.
        Histories recorded before it carry no marker and keep their command sequence.
        """
        if not workflow.patched(f"existing-backlog-ci-repair-authority-v1-{ordinal}"):
            return None
        if not register_run or not change_set_id:
            return "CI_REPAIR_PARENT_RUN_UNVERIFIED"
        recorded = await self._loop_step(
            request,
            item,
            "backlog.record_ci_repair",
            {
                **_backlog_execution_payload(request),
                "workflow_id": workflow_root,
                "ordinal": ordinal,
                "repair_round": repair_round,
                "change_set_id": change_set_id,
            },
            register_run=register_run,
        )
        if recorded.get("status") == "RECORDED":
            return None
        return str(recorded.get("blocker") or "CI_REPAIR_NOT_AUTHORIZED")[:120]

    async def _ci_repair_delivery(
        self,
        *,
        request: ExistingGitHubBacklogLoopInput,
        item: dict[str, Any],
        register_run: bool,
        gate: dict[str, Any],
        ordinal: int,
        repair_round: int,
        results: list[dict[str, Any]],
        register_child_runs: bool,
    ) -> dict[str, Any]:
        """Re-run the issue delivery for the published ChangeSet (maker resumes its commits)."""
        prefix = f"{workflow.info().workflow_id}-issue-{ordinal}-ci-repair-{repair_round}"
        delivery: dict[str, Any] = {"status": "FAILED", "blocker": "DELIVERY_NOT_STARTED"}
        for attempt in range(1, request.correction_limit + 2):
            child_id = f"{prefix}-attempt-{attempt}"
            child_approval_id: str | None = None
            if register_child_runs:
                child_registration = await self._loop_step(
                    request,
                    item,
                    "backlog.register_child_run",
                    {
                        **_backlog_execution_payload(request),
                        "child_kind": "issue-delivery",
                        "child_workflow_id": child_id,
                        "finding_id": str(gate["finding_id"]),
                    },
                    register_run=register_run,
                )
                child_approval_id = child_registration.get("issue_approval_id")
            delivery = await self._issue_delivery(gate, child_id, child_approval_id)
            results.append({**delivery, "attempt": attempt, "stage": "ci-repair"})
            if delivery.get("status") != "REQUEST_CHANGES":
                break
        return delivery

    async def _after_issue_boundary(
        self,
        request: ExistingGitHubBacklogLoopInput,
        results: list[dict[str, Any]],
        *,
        enabled: bool,
        has_more: bool,
    ) -> dict[str, Any] | None:
        """Apply campaign controls only between issues, never during provider work."""
        if _in_front_lane():
            return None  # NEW-214 Stage 2: the lane coordinator applies them for all lanes
        if not enabled or not has_more:
            return None
        if self._stop_after_current:
            self._stop_after_current = False
            self._phase = "STOPPED_AFTER_CURRENT"
            await self._step(
                "backlog.record_loop_state",
                self._boundary_state(
                    request,
                    {"loop_status": "STOPPED", "control_state": "STOPPED_AFTER_CURRENT"},
                ),
            )
            return {"status": "STOPPED", "results": results}
        if not self._pause_after_current:
            return None
        self._pause_after_current = False
        self._paused = True
        self._phase = "PAUSED_AFTER_CURRENT"
        await self._step(
            "backlog.record_loop_state",
            self._boundary_state(
                request, {"loop_status": "PAUSED", "control_state": "PAUSED_AFTER_CURRENT"}
            ),
        )
        await workflow.wait_condition(lambda: not self._paused or self._cancelled)
        if not self._cancelled:
            if self._boundary_records_run_patched():
                # NEW-127: the resume is a transition the operator and watchdog must see.
                await self._step(
                    "backlog.record_loop_state",
                    self._boundary_state(request, {"loop_status": "RUNNING"}),
                )
            return None
        await self._step(
            "backlog.record_loop_state",
            self._boundary_state(request, {"loop_status": "CANCELLED"}),
        )
        return {"status": "CANCELLED", "results": results}

    def _boundary_records_run_patched(self) -> bool:
        # NEW-127: boundary states name the loop Run (finalizing it) and record resumes.
        return workflow.patched("existing-backlog-boundary-records-run-v1")

    def _boundary_state(
        self, request: ExistingGitHubBacklogLoopInput, state: dict[str, Any]
    ) -> dict[str, Any]:
        payload = {**_backlog_execution_payload(request), **state}
        if self._loop_register_run and self._boundary_records_run_patched():
            payload["workflow_id"] = workflow.info().workflow_id
        return payload

    async def _pipeline_prerequisites(
        self, request: ExistingGitHubBacklogLoopInput, *, register_run: bool
    ) -> dict[str, Any] | None:
        """NEW-214b: concurrency 2 needs merge-forward and strict up-to-date protection.

        A PR built while another merges must be refused (atomically, by GitHub) once its
        base moved, and merge-forward must answer that. Otherwise the loop ends before any
        work, and the operator restarts it with concurrency 1.
        """
        readiness = await self._step("gitops.branch_protection", {"project": request.project})
        # NEW-214 Stage 2: the project caps the approved concurrency (falcone 2, gntik-ui
        # 3). A result recorded before the cap existed carries none.
        cap = readiness.get("maximum_concurrency")
        above_cap = type(cap) is int and request.maximum_concurrency > cap
        if readiness.get("ready") is True and not above_cap:
            return None
        blocker = (
            "CONCURRENCY_ABOVE_PROJECT_CAP"
            if readiness.get("ready") is True
            else "CONCURRENCY_2_REQUIRES_MERGE_FORWARD"
            if readiness.get("merge_forward") is not True
            else "CONCURRENCY_2_REQUIRES_STRICT_PROTECTION"
        )
        state = {**_backlog_execution_payload(request), "loop_status": "FAILED", "blocker": blocker}
        if register_run:
            state["workflow_id"] = workflow.info().workflow_id
        await self._step("backlog.record_loop_state", state)
        return {"status": "BLOCKED", "blocker": blocker}

    async def _back_slot(self, issue_ref: str, **arguments: Any) -> tuple[str, str]:
        """NEW-214b: one issue's CI, merge and staging, as a task beside the FRONT slot."""
        _SLOT_ISSUE.set(issue_ref)  # this task's own context only
        _SLOT_ID.set("back")
        staged: tuple[str, str] = ("FAILED", "")
        try:
            staged = await self._staging_gitops_delivery(**arguments)
            if staged[0] == "REPAIR":
                # NEW-214 Stage 2: queued before this task ends, so the issue never leaves
                # custody (a dependent keeps waiting for it).
                self._repair_queue.append(
                    {
                        "ref": issue_ref,
                        "ordinal": arguments["ordinal"],
                        "arguments": {
                            key: value
                            for key, value in arguments.items()
                            if key not in ("repair_mode", "resume")
                        },
                    }
                )
            return staged
        except _LoopIssueSkipped as skipped:
            # A skipped bookkeeping step ends only this slot's issue (NEW-162 closes a
            # sequential loop, which has no other issue in flight).
            arguments["results"].append(skipped.skipped)
            staged = ("SKIPPED", "")
            return staged
        finally:
            # NEW-214 Stage 2: a dependent issue reads the outcome once BACK ended.
            self._back_outcomes[issue_ref] = staged[0]
            await self._end_operator_turn("back")

    async def _finish_back_slot(self) -> BaseException | None:
        """NEW-214b: a loop never ends while its BACK slot still runs.

        A BACK outcome the loop did not apply yet still counts as delivered (for
        NEW-162's skip close). Returns the BACK slot's failure, which fails the loop.
        """
        task = self._back_task
        if task is None:
            return None
        if not task.done():
            self._front_busy = False
            await workflow.wait_condition(task.done)
        self._back_task = None
        failure = task.exception()
        if failure is None and task.result()[0] == "DONE":
            self._loop_delivered += 1
        return failure

    async def _loop_step(
        self,
        request: ExistingGitHubBacklogLoopInput,
        item: dict[str, Any],
        step: str,
        payload: dict[str, Any],
        *,
        register_run: bool,
    ) -> dict[str, Any]:
        """NEW-82: a failed loop bookkeeping activity (e.g. #1125's register_child_run
        binding conflict) pauses the issue for the operator instead of failing the whole
        loop; the operator's retry re-runs the step, skip/cancel end as before."""
        while True:
            try:
                return await self._step(step, payload)
            except ActivityError as error:
                if not workflow.patched("existing-backlog-resilient-loop-steps-v1"):
                    raise
                blocker = "LOOP_STEP_FAILED_" + step.upper().replace(".", "_")
                cause = error.cause
                if (
                    isinstance(cause, ApplicationError)
                    and cause.type in _APPROVAL_LIFETIME_BLOCKERS
                ):
                    # NEW-229: the child was refused before it started because its campaign
                    # approval expires too soon (or expired): pause on that reason. Replay
                    # safe without a patch marker: only the payload differs, and histories
                    # recorded before NEW-229 never carry these failure types.
                    blocker = str(cause.type)
                paused = {
                    **_backlog_execution_payload(request),
                    "loop_status": "PAUSED",
                    "blocker": blocker,
                }
                if register_run:
                    paused["workflow_id"] = workflow.info().workflow_id
                await self._slot_pause_gate()
                await self._step("backlog.record_loop_state", paused)
                outcome, skipped = await self._await_delivery_operator(
                    request, item, blocker, register_run=register_run
                )
                if outcome == "SKIPPED" and self._loop_step_skip_closes_patched():
                    raise _LoopIssueSkipped(skipped) from error
                if outcome != "RETRY":
                    raise error
                resumed = {**_backlog_execution_payload(request), "loop_status": "RUNNING"}
                if register_run:
                    resumed["workflow_id"] = workflow.info().workflow_id
                await self._step("backlog.record_loop_state", resumed)

    def _loop_step_skip_closes_patched(self) -> bool:
        # NEW-162: evaluated only when the skip arrives, so a loop paused on a failed step
        # before this change also closes cleanly when it is skipped after the deploy.
        return workflow.patched("existing-backlog-loop-step-skip-closes-v1")

    async def _complete_after_skip(
        self,
        request: ExistingGitHubBacklogLoopInput,
        skipped: dict[str, Any],
        register_run: bool,
    ) -> dict[str, Any]:
        """NEW-162: end the loop after the operator skipped the issue a failed loop step
        paused on; the skip is already recorded. Completes like the loop's own end (the
        remaining selected issues, if any, are left for a new start)."""
        results = [*self._loop_results, skipped]
        state: dict[str, Any] = {**_backlog_execution_payload(request), "loop_status": "COMPLETE"}
        if register_run:
            state["workflow_id"] = workflow.info().workflow_id
        nothing_delivered = self._loop_delivered == 0 and request.global_campaign_id is None
        if nothing_delivered:
            self._phase = "COMPLETE_NOTHING_DELIVERED"
            state["completion"] = "COMPLETE_NOTHING_DELIVERED"
            state["outcomes"] = _loop_outcomes(results)
        await self._step("backlog.record_loop_state", state)
        outcome: dict[str, Any] = {"status": "COMPLETE", "results": results}
        if nothing_delivered:
            outcome.update(completion="COMPLETE_NOTHING_DELIVERED", delivered=0)
        return outcome

    async def _await_delivery_operator(
        self,
        request: ExistingGitHubBacklogLoopInput,
        item: dict[str, Any],
        blocker: str,
        *,
        register_run: bool,
    ) -> tuple[str, dict[str, Any]]:
        """Hold a blocked delivery until the operator cancels, skips or retries it."""
        issue_ref = f"{item['repository']}#{item['issue_number']}"
        self._phase = f"AWAITING_OPERATOR:{issue_ref}:{blocker}"
        self._skip_reason = None
        self._retry_step = None
        while True:
            await workflow.wait_condition(
                lambda: (
                    self._cancelled or self._skip_reason is not None or self._retry_step is not None
                )
            )
            if self._cancelled:
                cancel_state = {**_backlog_execution_payload(request), "loop_status": "CANCELLED"}
                if register_run:
                    cancel_state["workflow_id"] = workflow.info().workflow_id
                await self._step("backlog.record_loop_state", cancel_state)
                return "CANCELLED", {}
            if self._skip_reason is not None:
                skip_value = self._skip_reason
                reference, separator, reason = skip_value.rpartition("|")
                self._skip_reason = None
                if not separator or reference != issue_ref:
                    self._note_ignored_signal("skip", skip_value, issue_ref)
                    continue
                self._phase = f"RESUMED:{issue_ref}"
                skipped = await self._step(
                    "backlog.record_skip",
                    {
                        **_backlog_execution_payload(request),
                        "repository": item["repository"],
                        "issue_number": item["issue_number"],
                        "reason_code": reason,
                    },
                )
                return "SKIPPED", skipped
            retry_ref, self._retry_step = self._retry_step, None
            if retry_ref == issue_ref:
                self._phase = f"RESUMED:{issue_ref}"
                return "RETRY", {}
            self._note_ignored_signal("retry", retry_ref, issue_ref)

    async def _issue_delivery(
        self,
        gate: dict[str, Any],
        delivery_workflow_id: str,
        issue_approval_id: str | None = None,
    ) -> dict[str, Any]:
        if self._front_lanes >= 2:
            # NEW-214 Stage 2: the loop's makers share the component lease (one writer
            # slot each). An additional FRONT maker also needs provider quota above the
            # 25% verification reserve; without it the lane waits for the other maker.
            # Below the reserve the loop keeps one FRONT maker at a time, as concurrency 2
            # does; it never stalls. The check repeats after every wait.
            while _in_front_lane() and self._makers_running:
                capacity = await self._step("delivery.front_capacity", self._front_capacity_payload)
                if capacity.get("quota_ok") is True:
                    break
                await self._lane_wait(lambda: not self._makers_running)
            self._makers_running.add(delivery_workflow_id)
            try:
                result = await self._issue_delivery_child(
                    gate,
                    delivery_workflow_id,
                    issue_approval_id,
                    lease_share_group=workflow.info().workflow_id,
                )
            finally:
                self._makers_running.discard(delivery_workflow_id)
            lane = _SLOT_ID.get() or ""
            if lane.startswith("front-") and result.get("status") == "SOURCE_PR":
                # The checker passed: this fixes the lane's place in the hand-over queue.
                self._handoff_tickets[lane] = (workflow.now(), self._lane_ordinals.get(lane, 0))
            return result
        if getattr(self, "_pipelined", False):
            # NEW-214b: one maker/checker child at a time per loop (FRONT delivery or BACK
            # CI repair), so the component's implementation lease never conflicts.
            await workflow.wait_condition(lambda: self._implementation_holder is None)
            self._implementation_holder = delivery_workflow_id
            try:
                return await self._issue_delivery_child(
                    gate, delivery_workflow_id, issue_approval_id
                )
            finally:
                self._implementation_holder = None
        return await self._issue_delivery_child(gate, delivery_workflow_id, issue_approval_id)

    async def _issue_delivery_child(
        self,
        gate: dict[str, Any],
        delivery_workflow_id: str,
        issue_approval_id: str | None,
        lease_share_group: str | None = None,
    ) -> dict[str, Any]:
        # A failed delivery child (e.g. NEW-23's evidence 422) must block its one issue
        # through the normal FAILED path below, not crash the whole backlog loop.
        try:
            result: dict[str, Any] = await workflow.execute_child_workflow(
                "IssueDeliveryWorkflow",
                IssueDeliveryInput(
                    finding_id=str(gate["finding_id"]),
                    risk_class=str(gate["risk_class"]),
                    dry_run=False,
                    issue_approval_id=issue_approval_id,
                    lease_share_group=lease_share_group,
                ).payload(),
                id=delivery_workflow_id,
                task_queue=workflow.info().task_queue,
                execution_timeout=timedelta(hours=12),
                **self._child_run_options(delivery_workflow_id),
            )
        except ChildWorkflowError as error:
            # NEW-114: keep the child's own failure code; the loop pauses on it.
            return {
                "status": "FAILED",
                "blocker": "DELIVERY_CHILD_WORKFLOW_FAILED",
                "failure_code": _child_failure_code(error),
            }
        return result

    def _correction_budget_patched(self, ordinal: int, delivery_round: int) -> bool:
        # NEW-133: a retry round checks the ChangeSet's correction budget after each
        # REQUEST_CHANGES attempt. Temporal memoizes a patch decision for the whole
        # execution, so the marker is per issue and retry round: a replayed pre-deploy
        # round stays unpatched while a later round of the same loop is checked.
        return workflow.patched(
            f"existing-backlog-retry-correction-budget-v1-{ordinal}-{delivery_round}"
        )

    async def _retry_round_budget_blocker(
        self,
        request: ExistingGitHubBacklogLoopInput,
        item: dict[str, Any],
        delivery: dict[str, Any],
        ordinal: int,
        delivery_round: int,
        *,
        register_run: bool,
    ) -> str | None:
        """NEW-133: the blocker that ends an operator retry round, or None to continue.

        Only a retry round asks: the first round is bounded by its own attempt range.
        After an operator correction extension the round's first attempt is the one
        extra attempt; instead of starting a second child that the bounded approval
        lineage refuses (a POLICY_VIOLATION child failure), the loop pauses with
        CORRECTION_EXTENSION_USED (or CORRECTION_LIMIT_EXHAUSTED without an extension).
        """
        if not delivery_round:
            return None
        context = delivery.get("context")
        change_set_id = context.get("change_set_id") if isinstance(context, dict) else None
        if not isinstance(change_set_id, str) or not change_set_id:
            return None
        if not self._correction_budget_patched(ordinal, delivery_round):
            return None
        budget = await self._loop_step(
            request,
            item,
            "backlog.correction_budget",
            {**_backlog_execution_payload(request), "change_set_id": change_set_id},
            register_run=register_run,
        )
        if budget.get("status") != "EXHAUSTED":
            return None
        return str(budget.get("blocker") or "CORRECTION_LIMIT_EXHAUSTED")

    @staticmethod
    def _provider_unavailable_pause(
        delivery: dict[str, Any], ordinal: int, delivery_round: int
    ) -> bool:
        """NEW-137: the FAILED child stopped because a provider call was unavailable.

        The marker is asked only for such a child, and per issue and retry round (Temporal
        memoizes a patch for the whole execution): a replayed pre-deploy pause keeps its
        DELIVERY_FAILED:<code> blocker, while a later round of the same loop is named."""
        if provider_unavailable_stop(delivery.get("context")) is None:
            return False
        return workflow.patched(
            f"existing-backlog-provider-unavailable-pause-v1-{ordinal}-{delivery_round}"
        )

    @staticmethod
    def _provider_access_pause(blocker: str, child: dict[str, Any], point: str) -> str | None:
        """NEW-232: the expired-login or missing-CLI code that stopped a child, or None.

        Such a stop is not a verdict on the issue: every later issue would stop the same
        way, so the loop pauses on this one (backlog-retry resumes it after the login)
        instead of using up its remaining items. The marker is per decision point (issue,
        and round for a delivery), like NEW-137's, so a replayed older history advances."""
        code = _provider_access_code(blocker, child)
        if code is None:
            return None
        if not workflow.patched(f"existing-backlog-provider-access-pause-v1-{point}"):
            return None
        return code

    @staticmethod
    def _child_failure_pauses_patched(ordinal: int) -> bool:
        # NEW-114: a FAILED revalidation or delivery child pauses the loop (per issue
        # ordinal, so an execution in flight upgrades at its next issue).
        return workflow.patched(f"existing-backlog-child-failure-pauses-v1-{ordinal}")

    @staticmethod
    def _free_slots_patched(ordinal: int) -> bool:
        # NEW-114: a rejected/duplicate/skipped issue frees its maximum_items slot, and
        # a skip no longer leaks into the next issue.
        return workflow.patched(f"existing-backlog-free-slots-v1-{ordinal}")

    @staticmethod
    def _nothing_delivered_patched() -> bool:
        # NEW-114: a loop that delivered nothing completes as COMPLETE_NOTHING_DELIVERED.
        return workflow.patched("existing-backlog-nothing-delivered-v1")

    async def _refill_free_slot(
        self,
        request: ExistingGitHubBacklogLoopInput,
        raw_items: list[Any],
        *,
        register_run: bool,
    ) -> None:
        if self._front_lanes < 2:
            await self._refill_free_slot_once(request, raw_items, register_run=register_run)
            return
        # NEW-214 Stage 2: one refill at a time, so two lanes never select the same issue.
        # Waiters wake together, so each rechecks before it takes the refill.
        while self._refilling:
            await workflow.wait_condition(lambda: not self._refilling)
        self._refilling = True
        try:
            await self._refill_free_slot_once(request, raw_items, register_run=register_run)
        finally:
            self._refilling = False

    async def _refill_free_slot_once(
        self,
        request: ExistingGitHubBacklogLoopInput,
        raw_items: list[Any],
        *,
        register_run: bool,
    ) -> None:
        """NEW-114: replace an issue that freed its slot with the next eligible one.

        backlog.load_execution re-checks the campaign approval and the item bounds and
        excludes every issue this loop already holds, so the loop never works on more
        than maximum_items issues that consume a slot, and stays within its approval."""
        if request.target_repository is not None or request.global_campaign_id is not None:
            return
        queued = [_issue_ref(value) for value in raw_items if isinstance(value, dict)]
        if len(queued) >= _LOOP_REFILL_LIMIT:
            return
        # An operator pause (e.g. before a gate skip) left the campaign PAUSED, which
        # voids its approval for selection; the loop is running again before it selects.
        resumed: dict[str, Any] = {**_backlog_execution_payload(request), "loop_status": "RUNNING"}
        if register_run:
            resumed["workflow_id"] = workflow.info().workflow_id
        await self._step("backlog.record_loop_state", resumed)
        refreshed = await self._step(
            "backlog.load_execution",
            {**_backlog_execution_payload(request), "exclude_issue_refs": queued},
        )
        candidates = refreshed.get("items")
        if not isinstance(candidates, list):
            # Kept for the completion record: no refill because selection was refused.
            self._refill_blocker = str(
                refreshed.get("blocker") or refreshed.get("status") or "BACKLOG_RELOAD_FAILED"
            )[:120]
            return
        for candidate in candidates:
            if isinstance(candidate, dict) and _issue_ref(candidate) not in queued:
                raw_items.append(candidate)
                return

    @staticmethod
    def _requires_operator_pause(blocker: str) -> bool:
        normalized = blocker.upper()
        return any(
            marker in normalized
            for marker in (
                "R3",
                "MIGRATION",
                "SECRET",
                "CREDENTIAL",
                "CROSS_TENANT",
                "CROSS-TENANT",
                "PRODUCTION",
                "EXTERNAL_DRIFT",
                "REVISION_DRIFT",
                "APPROVAL",
                # NEW-217: only the operator can amend or re-import a changed issue.
                ISSUE_BODY_CHANGED_SINCE_IMPORT,
            )
        )


@workflow.defn(name="AutonomousBacklogCampaignWorkflow")
class AutonomousBacklogCampaignWorkflow(ControlledWorkflow):
    """One global durable selector; at most one child issue workflow is active."""

    @workflow.run
    async def run(self, request: AutonomousBacklogCampaignInput) -> dict[str, Any]:
        if (
            set(request.backlog_campaign_ids) != {"falcone", "musematic", "llmwiki"}
            or request.environment != "staging"
            or request.maximum_risk != "R2"
            or request.global_active_issues != 1
            or request.global_deployments != 1
            or request.production
            or request.implementation_correction_limit != 5
            or request.deployment_correction_limit != 3
        ):
            raise ApplicationError(
                "global backlog policy is outside the staging R2 mission boundary",
                type="PolicyViolation",
                non_retryable=True,
            )
        context = await self._step("global_backlog.register", asdict(request))
        iterations_this_run = 0
        while not self._cancelled:
            if not await self._checkpoint("GLOBAL_ISSUE_SELECTION"):
                break
            selection = await self._step("global_backlog.select_next", context)
            item = selection.get("item")
            if selection.get("status") == "WAITING":
                wait_seconds = max(
                    30,
                    min(int(selection.get("retry_after_seconds", 900)), 900),
                )
                self._phase = "WAITING_FOR_BLOCKED_ISSUE_RETRY"
                await workflow.sleep(timedelta(seconds=wait_seconds))
                iterations_this_run += 1
                if iterations_this_run >= 25:
                    workflow.continue_as_new(request)
                continue
            if item is None:
                return await self._step(
                    "global_backlog.finalize", {**context, "terminal_status": "COMPLETE"}
                )
            if not isinstance(item, dict):
                raise TypeError("global backlog selection must be an object")
            self._phase = (
                f"ISSUE:{item['repository']}#{item['issue_number']}:CURRENT_STATE_REVALIDATION"
            )
            project = str(item["project"])
            child_id = (
                f"{workflow.info().workflow_id}-{project}-"
                f"{str(item['repository']).removeprefix('gntik-ai/').replace('-', '_')}-"
                f"{item['issue_number']}"
            )
            result = await workflow.execute_child_workflow(
                "ExistingGitHubBacklogLoopWorkflow",
                ExistingGitHubBacklogLoopInput(
                    project=project,
                    backlog_campaign_id=str(item["backlog_campaign_id"]),
                    maximum_risk=str(item.get("execution_maximum_risk", "R2")),
                    delivery_mode=str(item["delivery_mode"]),
                    maximum_items=1,
                    target_repository=str(item["repository"]),
                    target_issue_number=int(item["issue_number"]),
                    continue_after_blocked_issue=False,
                    correction_limit=5,
                    global_campaign_id=request.campaign_id,
                    r3_approval_binding=(
                        str(item["r3_approval_binding"])
                        if item.get("r3_approval_binding")
                        else None
                    ),
                ),
                id=child_id,
                task_queue=workflow.info().task_queue,
                execution_timeout=timedelta(days=7),
                **self._child_run_options(child_id),
            )
            context = await self._step(
                "global_backlog.record_result", {**context, "item": item, "result": result}
            )
            iterations_this_run += 1
            if self._pause_after_current:
                self._pause_after_current = False
                self._paused = True
            if self._stop_after_current:
                return await self._step(
                    "global_backlog.finalize", {**context, "terminal_status": "STOPPED"}
                )
            if self._paused:
                await workflow.wait_condition(lambda: not self._paused or self._cancelled)
            if iterations_this_run >= 25:
                workflow.continue_as_new(request)
        return await self._step(
            "global_backlog.finalize", {**context, "terminal_status": "CANCELLED"}
        )


@workflow.defn(name="EnhancementDiscoveryWorkflow")
class EnhancementDiscoveryWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: EnhancementDiscoveryInput) -> dict[str, Any]:
        context: dict[str, Any] = asdict(request)
        for step in ("enhancement.load_evidence", "enhancement.prepare_discovery"):
            if not await self._checkpoint(step.upper().replace(".", "_")):
                return {"status": "CANCELLED", "context": context}
            context = await self._step(step, context)
            if context.get("status") in {"BLOCKED", "FAILED"}:
                return {"status": context["status"], "context": context}
        if request.dry_run:
            return {"status": "DRY_RUN", "context": context}
        for step in (
            "enhancement.discover",
            "enhancement.validate_evidence",
            "enhancement.dedupe",
            "enhancement.github_dedupe",
            "enhancement.product_review",
            "enhancement.persist",
        ):
            if not await self._checkpoint(step.upper().replace(".", "_")):
                return {"status": "CANCELLED", "context": context}
            if step in {"enhancement.discover", "enhancement.product_review"}:
                context = await self._agent_step(step, context, timeout_minutes=120)
            else:
                context = await self._step(step, context)
            if (
                step in {"enhancement.dedupe", "enhancement.github_dedupe"}
                and context.get("status") == "DUPLICATE"
            ):
                return {"status": "DUPLICATE", "context": context}
            if context.get("status") in {"BLOCKED", "FAILED", "RATE_LIMITED"}:
                return {"status": context["status"], "context": context}
        return {"status": "CANDIDATE_ENHANCEMENT", "context": context}


@workflow.defn(name="EnhancementDeliveryWorkflow")
class EnhancementDeliveryWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: EnhancementDeliveryInput) -> dict[str, Any]:
        context = await self._step("enhancement.load_approved", asdict(request))
        if context.get("status") != "APPROVED":
            return {"status": "BLOCKED", "blocker": "ENHANCEMENT_APPROVAL_REQUIRED"}
        context = await self._step("enhancement.create_openspec", context)
        if not request.publish:
            context = await self._step("enhancement.publication_dry_run", context)
            return {"status": "DRY_RUN", "context": context}
        context = await self._github_write_step(
            "enhancement.publish", {**context, "dry_run": False}
        )
        if context.get("status") != "PUBLISHED":
            return {"status": context.get("status", "BLOCKED"), "context": context}
        context = await self._step("enhancement.persist_publication", context)
        return {"status": context.get("status", "PUBLISHED"), "context": context}


@workflow.defn(name="EnhancementSolutionLoopWorkflow")
class EnhancementSolutionLoopWorkflow(ControlledWorkflow):
    @workflow.run
    async def run(self, request: EnhancementSolutionLoopInput) -> dict[str, Any]:
        approved = await self._step("enhancement.load_approved", asdict(request))
        if approved.get("status") != "APPROVED":
            return {"status": "BLOCKED", "blocker": "ENHANCEMENT_APPROVAL_REQUIRED"}
        prepared = await self._step("enhancement.prepare_solution", approved)
        if request.dry_run or request.execution_mode == "plan-only":
            return {"status": "DRY_RUN", "context": prepared}
        result = await workflow.execute_child_workflow(
            "IssueSolutionLoopWorkflow",
            IssueSolutionLoopInput(
                project=str(prepared["project"]),
                campaign_id=str(prepared["campaign_id"]),
                maximum_risk=request.risk,
                maximum_items=1,
                execution_mode=request.execution_mode,
                component_concurrency=1,
                stop_policy="pause-on-blocker",
                dry_run=False,
            ),
            id=f"{workflow.info().workflow_id}-solution",
            task_queue=workflow.info().task_queue,
            execution_timeout=timedelta(days=7),
            **self._child_run_options(f"{workflow.info().workflow_id}-solution"),
        )
        if not isinstance(result, dict):
            raise RuntimeError("enhancement solution child returned an invalid result")
        return result


@workflow.defn(name="LLMWikiStagingBootstrapWorkflow")
class LLMWikiStagingBootstrapWorkflow(ControlledWorkflow):
    """Plan and, only after an exact binding, reconcile first-stage LLMWiki staging."""

    @workflow.run
    async def run(self, request: LLMWikiStagingBootstrapInput) -> dict[str, Any]:
        if request.project != "llmwiki" or request.environment != "staging":
            raise ApplicationError(
                "LLMWiki bootstrap rejects non-staging targets",
                type="PolicyViolation",
                non_retryable=True,
            )
        plan = await self._step("llmwiki.bootstrap.plan", asdict(request))
        if not request.execute:
            return plan
        if plan.get("blockers"):
            return {**plan, "status": "BLOCKED"}
        if plan.get("approval_binding") != request.approval_binding:
            raise ApplicationError(
                "LLMWiki bootstrap binding changed",
                type="ApprovalMismatch",
                non_retryable=True,
            )
        return await self._step(
            "llmwiki.bootstrap.execute",
            {**plan, "dry_run": False, "approval_binding_valid": True},
            timeout_minutes=120,
        )


@workflow.defn(name="ChangeRequestWorkflow")
class ChangeRequestWorkflow(ControlledWorkflow):
    """NEW-108: draft, independently review and persist one change request as a candidate.

    It never approves: a REVISE verdict gets one automatic redraft and re-review, and an
    unresolved verdict is still persisted as a CANDIDATE with its findings visible.
    """

    @workflow.run
    async def run(self, request: ChangeRequestInput) -> dict[str, Any]:
        try:
            result = await self._draft_and_review(request)
        except ActivityError:
            await self._finalize_change_request_run(request, "FAILED", "CHANGE_REQUEST_FAILED")
            raise
        await self._finalize_change_request_run(
            request, str(result.get("status", "FAILED")), result.get("error_code")
        )
        return result

    async def _finalize_change_request_run(
        self, request: ChangeRequestInput, status: str, error_code: Any
    ) -> None:
        """Close the control Run that the provider invocations opened (if any).

        Best effort: a finalization failure (e.g. the same database outage) must never
        replace the request's own outcome or the original exception, so it is logged and
        the Run is left for the operator to see as still running.
        """
        try:
            await self._step(
                "change_request.finalize_run",
                {
                    "change_request_id": request.change_request_id,
                    "revision": request.revision,
                    "terminal_status": status,
                    "error_code": error_code,
                },
            )
        except ActivityError:
            workflow.logger.warning(
                "change request %s revision %s: control Run finalization failed",
                request.change_request_id,
                request.revision,
            )

    async def _draft_and_review(self, request: ChangeRequestInput) -> dict[str, Any]:
        context = await self._step("change_request.load", asdict(request))
        if context.get("status") != "DRAFTING":
            return {
                "status": "SUPERSEDED",
                "change_request_id": request.change_request_id,
                "revision": request.revision,
            }
        try:
            if not await self._checkpoint("CHANGE_REQUEST_SNAPSHOT"):
                return await self._change_request_failure(context, "internal", "CANCELLED")
            try:
                context = await self._step("project.snapshot", context, timeout_minutes=60)
            except ActivityError:
                return await self._change_request_failure(context, "snapshot", "UNAVAILABLE")
            for attempt in (1, 2):
                if not await self._checkpoint("CHANGE_REQUEST_DRAFT"):
                    return await self._change_request_failure(context, "internal", "CANCELLED")
                context = await self._step(
                    "change_request.prepare_draft", {**context, "draft_attempt": attempt}
                )
                if context.get("status") != "DRAFT_READY":
                    return await self._change_request_failure(
                        context, "draft", str(context.get("blocker", "DRAFT_INPUT_INVALID"))
                    )
                context = await self._agent_step(
                    "change_request.draft", context, timeout_minutes=120
                )
                if context.get("status") != "SUCCESS":
                    return await self._change_request_failure(
                        context, "draft", str(context.get("blocker") or context.get("status"))
                    )
                context = await self._step("change_request.prepare_review", context)
                if context.get("status") != "REVIEW_READY":
                    return await self._change_request_failure(
                        context, "draft", str(context.get("blocker", "DRAFT_SCHEMA_INVALID"))
                    )
                if not await self._checkpoint("CHANGE_REQUEST_REVIEW"):
                    return await self._change_request_failure(context, "internal", "CANCELLED")
                context = await self._agent_step(
                    "change_request.review", context, timeout_minutes=120
                )
                if context.get("status") != "SUCCESS":
                    return await self._change_request_failure(
                        context, "review", str(context.get("blocker") or context.get("status"))
                    )
                context = await self._step("change_request.record_review", context)
                if context.get("status") == "REVIEW_APPROVED":
                    break
                if context.get("status") != "REVIEW_REVISE":
                    return await self._change_request_failure(
                        context, "review", str(context.get("blocker", "REVIEW_SCHEMA_INVALID"))
                    )
        except ActivityError:
            return await self._change_request_failure(context, "internal", "STEP_FAILED")
        try:
            context = await self._step("change_request.github_dedupe", context)
        except ActivityError:
            # Deduplication only warns; an unavailable search never blocks the candidate.
            context = {**context, "dedupe": {"status": "UNAVAILABLE", "matches": []}}
        persisted = await self._step("change_request.persist", context)
        return {
            "status": persisted.get("status", "FAILED"),
            "change_request_id": request.change_request_id,
            "revision": request.revision,
            "error_code": persisted.get("error_code"),
            "approval_binding": persisted.get("approval_binding"),
            "review_decision": persisted.get("review_decision"),
        }

    async def _change_request_failure(
        self, context: dict[str, Any], phase: str, blocker: str
    ) -> dict[str, Any]:
        recorded = await self._step(
            "change_request.record_failure",
            {
                "change_request_id": context.get("change_request_id"),
                "revision": context.get("revision"),
                "failure_phase": phase,
                "blocker": blocker,
                # NEW-146: the step's bounded, redacted message, when it reported one (an
                # activity input only: replay never compares it, and it is absent otherwise).
                **_blocker_detail(context),
            },
        )
        return {
            "status": recorded.get("status", "FAILED"),
            "change_request_id": context.get("change_request_id"),
            "revision": context.get("revision"),
            "error_code": recorded.get("error_code"),
        }


_UI_FANOUT_NO_DEDUPE = {"status": "UNAVAILABLE", "matches": []}


def _blocker_detail(context: dict[str, Any]) -> dict[str, Any]:
    """NEW-146: carry a failed step's blocker_detail into change_request.record_failure."""
    detail = context.get("blocker_detail")
    return {"blocker_detail": str(detail)[:400]} if detail else {}


@workflow.defn(name="UiMigrationPlanWorkflow")
class UiMigrationPlanWorkflow(ControlledWorkflow):
    """NEW-123 flow A: the UI migration blueprint, persisted as an approvable candidate.

    snapshot -> deterministic inventory -> kit snapshot (BLOCKED without kit data) ->
    Claude draft (plan-ui-migration) -> Codex review; at most one redraft and a second
    review, otherwise BLOCKED with the findings. It never approves anything.

    NEW-131 (patch ui-plan-batched-blueprint-v1): Hermes fills the mechanical fields from the
    inventory; Claude drafts a frame, then page batches; Codex reviews each part (one redraft
    per part; NEW-138, patch new-138-two-redrafts-<part>: two); Hermes assembles the reviewed
    parts into the blueprint candidate.
    """

    @workflow.run
    async def run(self, request: UiMigrationPlanInput) -> dict[str, Any]:
        try:
            result = await self._plan(request)
        except ActivityError:
            result = await self._ui_plan_terminal(
                {"blueprint_id": request.blueprint_id, "project": request.project},
                "FAILED",
                "UI_PLAN_STEP_FAILED",
            )
        try:
            await self._step(
                "ui_plan.finalize_run",
                {
                    "blueprint_id": request.blueprint_id,
                    "terminal_status": result.get("status"),
                    "error_code": result.get("error_code"),
                },
            )
        except ActivityError:
            workflow.logger.warning("ui plan %s: Run finalization failed", request.blueprint_id)
        return result

    async def _ui_plan_terminal(
        self,
        context: dict[str, Any],
        status: str,
        blocker: str,
        blockers: list[Any] | None = None,
        *,
        usage: dict[str, Any] | None = None,
        rejection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        recorded = await self._step(
            "ui_plan.record_terminal",
            {
                "blueprint_id": context.get("blueprint_id"),
                "project": context.get("project"),
                "terminal_status": status,
                "blocker": blocker,
                "blockers": list(blockers or [])[:50],
                # NEW-131 review: input only (no new command), so replay is unaffected.
                **({"part_usage": usage} if usage else {}),
                # NEW-142 review: a final-attempt provider schema refusal, kept durably
                # (input only, like part_usage).
                **({"part_rejection": rejection} if rejection else {}),
            },
        )
        return {
            "status": recorded.get("status", status),
            "blueprint_id": context.get("blueprint_id"),
            "error_code": recorded.get("error_code", blocker),
            "blockers": list(blockers or [])[:50],
        }

    async def _plan(self, request: UiMigrationPlanInput) -> dict[str, Any]:
        context = await self._step("ui_plan.register", asdict(request))
        if context.get("status") == "BLOCKED":
            return {
                "status": "BLOCKED",
                "blueprint_id": request.blueprint_id,
                "error_code": context.get("blocker"),
                "blockers": context.get("blockers", []),
            }
        if context.get("status") != "REGISTERED":
            return {"status": "SUPERSEDED", "blueprint_id": request.blueprint_id}
        if not await self._checkpoint("UI_PLAN_SNAPSHOT"):
            return await self._ui_plan_terminal(context, "FAILED", "UI_PLAN_CANCELLED")
        try:
            snapshot = await self._step(
                "project.snapshot",
                {
                    "project": request.project,
                    "campaign_id": context["campaign_id"],
                    "source_sha": context.get("source_sha"),
                },
                timeout_minutes=60,
            )
        except ActivityError:
            return await self._ui_plan_terminal(context, "BLOCKED", "UI_PLAN_SNAPSHOT_UNAVAILABLE")
        source = {
            "source_worktree": snapshot.get("source_worktree"),
            "source_sha": snapshot.get("source_sha"),
        }
        if not await self._checkpoint("UI_PLAN_INVENTORY"):
            return await self._ui_plan_terminal(context, "FAILED", "UI_PLAN_CANCELLED")
        inventory = await self._step(
            "ui.inventory", {"project": request.project, "snapshot": source}
        )
        if inventory.get("status") != "UI_INVENTORY_PASS":
            if inventory.get("status") == "BLOCKED" and workflow.patched(
                self._INVENTORY_BLOCKED_PATCH
            ):
                # NEW-136 review: e.g. a symlinked console scan root (fail closed).
                return await self._ui_plan_terminal(
                    context,
                    "BLOCKED",
                    str(inventory.get("blocker") or "UI_INVENTORY_BLOCKED"),
                    [str(inventory.get("reason") or "")[:300]],
                )
            return await self._ui_plan_terminal(context, "FAILED", "UI_INVENTORY_FAILED")
        kit = await self._step("ui.kit_snapshot", {"project": request.project})
        if kit.get("status") == "BLOCKED":
            # No draft without kit data (design section 11: models get no MCP).
            return await self._ui_plan_terminal(
                context,
                "BLOCKED",
                str(kit.get("blocker") or "UI_MCP_UNAVAILABLE"),
                [str(kit.get("reason") or "")[:300]],
            )
        if kit.get("status") != "UI_KIT_SNAPSHOT_PASS":
            return await self._ui_plan_terminal(context, "FAILED", "UI_KIT_SNAPSHOT_FAILED")
        context = await self._step(
            "ui_plan.record_inputs",
            {
                **context,
                **source,
                "inventory_sha256": inventory.get("inventory_sha256"),
                "inventory_summary": inventory.get("summary"),
                "kit_snapshot_sha256": kit.get("snapshot_sha256"),
                "gntik_ui_version": kit.get("gntik_ui_version"),
                "kit_image": kit.get("image"),
                "evidence": {
                    "inventory": inventory.get("evidence", []),
                    "kit": kit.get("evidence", []),
                },
            },
        )
        if context.get("status") != "INPUTS_RECORDED":
            return {"status": "SUPERSEDED", "blueprint_id": request.blueprint_id}
        staged = await self._step(
            "ui_plan.stage_inputs",
            {
                "project": request.project,
                "blueprint_id": request.blueprint_id,
                "inventory_sha256": context.get("inventory_sha256"),
                "kit_snapshot_sha256": context.get("kit_snapshot_sha256"),
            },
        )
        if staged.get("status") != "STAGED":
            return await self._ui_plan_terminal(
                context,
                "BLOCKED",
                str(staged.get("blocker") or "UI_PLAN_INPUTS_UNAVAILABLE"),
                [str(staged.get("reason") or "")[:300]],
            )
        context = {
            **context,
            "inputs": {
                "input_directory": staged.get("input_directory"),
                "files": staged.get("files"),
            },
            "review_rounds": [],
        }
        if workflow.patched(self._BATCHED_PATCH):
            # NEW-131: Hermes fills the mechanical fields; Claude drafts the frame and the
            # page batches, Codex reviews each part; Hermes assembles the reviewed parts.
            return await self._plan_batched(request, context)
        approved: dict[str, Any] | None = None
        for attempt in (1, 2):
            if not await self._checkpoint("UI_PLAN_DRAFT"):
                return await self._ui_plan_terminal(context, "FAILED", "UI_PLAN_CANCELLED")
            context = await self._step(
                "ui_plan.prepare_draft", {**context, "draft_attempt": attempt}
            )
            if context.get("status") != "DRAFT_READY":
                return await self._ui_plan_terminal(
                    context, "FAILED", str(context.get("blocker") or "DRAFT_INPUT_INVALID")
                )
            context = await self._agent_step("ui_plan.draft", context, timeout_minutes=200)
            if context.get("status") != "SUCCESS":
                return await self._ui_plan_terminal(
                    context, "BLOCKED", self._ui_provider_blocker("DRAFT", context)
                )
            recorded = await self._step("ui_plan.record_draft", context)
            if recorded.get("status") != "DRAFT_RECORDED":
                return await self._ui_plan_terminal(
                    recorded,
                    "BLOCKED",
                    str(recorded.get("blocker") or "DRAFT_SCHEMA_INVALID"),
                    recorded.get("blockers"),
                )
            context = {key: value for key, value in recorded.items() if key != "document"}
            draft = await self._step(
                "ui_plan.stage_document",
                {
                    "project": request.project,
                    "blueprint_id": request.blueprint_id,
                    "name": f"draft-{attempt}.json",
                    "document": recorded.get("document"),
                },
            )
            if draft.get("status") != "STAGED":
                return await self._ui_plan_terminal(
                    context, "BLOCKED", str(draft.get("blocker") or "UI_PLAN_INPUTS_UNAVAILABLE")
                )
            context = {**context, "draft_file": draft.get("document")}
            if not await self._checkpoint("UI_PLAN_REVIEW"):
                return await self._ui_plan_terminal(context, "FAILED", "UI_PLAN_CANCELLED")
            context = await self._step("ui_plan.prepare_review", context)
            if context.get("status") != "REVIEW_READY":
                return await self._ui_plan_terminal(
                    context, "FAILED", str(context.get("blocker") or "REVIEW_INPUT_INVALID")
                )
            context = await self._agent_step("ui_plan.review", context, timeout_minutes=200)
            if context.get("status") != "SUCCESS":
                return await self._ui_plan_terminal(
                    context, "BLOCKED", self._ui_provider_blocker("REVIEW", context)
                )
            reviewed = await self._step("ui_plan.record_review", context)
            context = {
                key: value for key, value in reviewed.items() if key not in {"document", "markdown"}
            }
            if reviewed.get("status") == "REVIEW_APPROVED":
                approved = reviewed
                break
            if reviewed.get("status") != "REVIEW_REVISE":
                return await self._ui_plan_terminal(
                    context,
                    "BLOCKED",
                    str(reviewed.get("blocker") or "REVIEW_SCHEMA_INVALID"),
                    reviewed.get("blockers"),
                )
            review = await self._step(
                "ui_plan.stage_document",
                {
                    "project": request.project,
                    "blueprint_id": request.blueprint_id,
                    "name": f"review-{attempt}.json",
                    "document": reviewed.get("document"),
                },
            )
            if review.get("status") != "STAGED":
                return await self._ui_plan_terminal(
                    context, "BLOCKED", str(review.get("blocker") or "UI_PLAN_INPUTS_UNAVAILABLE")
                )
            context = {**context, "review_file": review.get("document")}
        if approved is None:
            return await self._ui_plan_terminal(context, "BLOCKED", "BLUEPRINT_REVIEW_UNRESOLVED")
        if not await self._checkpoint("UI_PLAN_PERSIST"):
            return await self._ui_plan_terminal(context, "FAILED", "UI_PLAN_CANCELLED")
        published = await self._step(
            "ui_plan.stage_document",
            {
                "project": request.project,
                "blueprint_id": request.blueprint_id,
                "publish": True,
                "document": approved.get("document"),
                "markdown": approved.get("markdown"),
            },
        )
        persisted = await self._step(
            "ui_plan.persist",
            {
                "blueprint_id": request.blueprint_id,
                "project": request.project,
                "draft_attempt": approved.get("draft_attempt"),
                "reviewed_sha256": approved.get("reviewed_sha256"),
                "review_rounds": context.get("review_rounds", []),
                "evidence": published.get("evidence", []),
            },
        )
        return {
            "status": persisted.get("status", "FAILED"),
            "blueprint_id": request.blueprint_id,
            "error_code": persisted.get("error_code"),
            "blueprint_sha256": persisted.get("blueprint_sha256"),
            "approval_binding": persisted.get("approval_binding"),
            "review_rounds": len(context.get("review_rounds", [])),
        }

    _BATCHED_PATCH = "ui-plan-batched-blueprint-v1"
    # NEW-136 review: a BLOCKED inventory ends the plan BLOCKED with the inventory's blocker
    # (before: FAILED UI_INVENTORY_FAILED; the inventory step never returned BLOCKED then).
    _INVENTORY_BLOCKED_PATCH = "ui-plan-inventory-blocked-v1"
    # NEW-132: a first draft Hermes rejects (schema or structure) gets the part's redraft.
    _STRUCTURAL_REDRAFT_PATCH = "new-132-structural-redraft"
    _STRUCTURAL_REJECTIONS = frozenset({"DRAFT_SCHEMA_INVALID", "DRAFT_STRUCTURE_INVALID"})
    # NEW-138 (operator, 2026-10-05): two redrafts per part; the id is suffixed per part.
    _TWO_REDRAFTS_PATCH = "new-138-two-redrafts"
    # NEW-142: a part continues an earlier run's unresolved draft; suffixed per part.
    _CONTINUATION_PATCH = "new-142-continuation"
    # Per part provider call: the request's own timeout is PART_TIMEOUT_SECONDS (1800 s);
    # the activity bound leaves room for the CLI's start-up and result handling.
    _PART_TIMEOUT_MINUTES = 40

    async def _plan_batched(
        self, request: UiMigrationPlanInput, context: dict[str, Any]
    ) -> dict[str, Any]:
        staged = await self._step(
            "ui_plan.stage_parts",
            {
                "project": request.project,
                "blueprint_id": request.blueprint_id,
                "inventory_sha256": context.get("inventory_sha256"),
                "kit_snapshot_sha256": context.get("kit_snapshot_sha256"),
                "batch_pages": context.get("batch_pages"),
            },
            timeout_minutes=30,
        )
        if staged.get("status") != "STAGED":
            return await self._ui_plan_terminal(
                context,
                "BLOCKED",
                str(staged.get("blocker") or "UI_PLAN_INPUTS_UNAVAILABLE"),
                [str(staged.get("reason") or "")[:300]],
            )
        recorded = await self._step(
            "ui_plan.record_parts",
            {
                **context,
                "mechanical": staged.get("mechanical"),
                "mechanical_sha256": staged.get("mechanical_sha256"),
                "parts": staged.get("parts"),
                "kit_slice": staged.get("kit_slice"),
            },
        )
        if recorded.get("status") != "PARTS_RECORDED":
            return await self._ui_plan_terminal(
                recorded, "FAILED", str(recorded.get("blocker") or "UI_PLAN_PARTS_INVALID")
            )
        context = {key: value for key, value in recorded.items() if key != "status"}
        approved: list[dict[str, Any]] = []
        for part in list(context.get("parts") or []):
            outcome = await self._ui_plan_part(
                request, {**context, "approved_parts": approved}, part
            )
            if outcome.get("status") != "PART_APPROVED":
                return await self._ui_plan_terminal(
                    outcome,
                    str(outcome.get("terminal") or "BLOCKED"),
                    str(outcome.get("blocker") or "UI_PLAN_PART_FAILED"),
                    outcome.get("blockers"),
                    usage=self._part_usage(outcome),
                    rejection=self._part_rejection(outcome),
                )
            if part.get("kind") == "frame":
                # The complete reviewed frame, as a file every batch reads (never truncated).
                brief = await self._step(
                    "ui_plan.stage_document",
                    {
                        "project": request.project,
                        "blueprint_id": request.blueprint_id,
                        "name": f"part-{outcome.get('part_id')}-brief.json",
                        "document": outcome.get("frame_brief"),
                    },
                )
                if brief.get("status") != "STAGED":
                    return await self._ui_plan_terminal(
                        context,
                        "BLOCKED",
                        str(brief.get("blocker") or "UI_PLAN_INPUTS_UNAVAILABLE"),
                    )
                context = {**context, "frame_brief_file": brief.get("document")}
            approved.append(
                {
                    "part_id": outcome.get("part_id"),
                    "kind": part.get("kind"),
                    "content_sha256": outcome.get("content_sha256"),
                }
            )
        if not await self._checkpoint("UI_PLAN_ASSEMBLE"):
            return await self._ui_plan_terminal(context, "FAILED", "UI_PLAN_CANCELLED")
        assembled = await self._step("ui_plan.assemble", {**context, "approved_parts": approved})
        if assembled.get("status") != "ASSEMBLED":
            return await self._ui_plan_terminal(
                context,
                "BLOCKED",
                str(assembled.get("blocker") or "ASSEMBLY_INVALID"),
                assembled.get("blockers"),
            )
        if not await self._checkpoint("UI_PLAN_PERSIST"):
            return await self._ui_plan_terminal(context, "FAILED", "UI_PLAN_CANCELLED")
        published = await self._step(
            "ui_plan.stage_document",
            {
                "project": request.project,
                "blueprint_id": request.blueprint_id,
                "publish": True,
                "document": assembled.get("document"),
                "markdown": assembled.get("markdown"),
            },
        )
        rounds = list(assembled.get("review_rounds") or [])
        persisted = await self._step(
            "ui_plan.persist",
            {
                "blueprint_id": request.blueprint_id,
                "project": request.project,
                "draft_attempt": assembled.get("draft_attempt"),
                "reviewed_sha256": assembled.get("reviewed_sha256"),
                "review_rounds": rounds,
                "evidence": published.get("evidence", []),
            },
        )
        return {
            "status": persisted.get("status", "FAILED"),
            "blueprint_id": request.blueprint_id,
            "error_code": persisted.get("error_code"),
            "blueprint_sha256": persisted.get("blueprint_sha256"),
            "approval_binding": persisted.get("approval_binding"),
            "review_rounds": len(rounds),
            "parts": len(approved),
        }

    @staticmethod
    def _structural_redraft(
        current: dict[str, Any], code: str, details: list[Any], usage: Any
    ) -> dict[str, Any]:
        """The context for attempt 2 after Hermes refused attempt 1 (no review ran)."""
        dropped = {
            "status",
            "document",
            "blocker",
            "failed_agent_usage",
            "failed_schema_findings",
            "agent_result",
        }
        redraft = {key: value for key, value in current.items() if key not in dropped}
        redraft["rejection"] = {"code": code, "details": [str(item) for item in details[:50]]}
        if isinstance(usage, dict):
            # Recorded by ui_plan.prepare_part (the refused call consumed tokens).
            redraft["rejected_usage"] = {
                "agent_result": usage,
                "part_id": current.get("part_id"),
                "phase": "draft",
                "attempt": current.get("draft_attempt"),
            }
        return redraft

    @staticmethod
    def _part_usage(outcome: dict[str, Any]) -> dict[str, Any] | None:
        """A failed part's last provider result, so its token usage is still recorded."""
        result = outcome.get("agent_result")
        if not isinstance(result, dict):
            result = outcome.get("failed_agent_usage")
        if not isinstance(result, dict) or outcome.get("status") == "SUCCESS":
            return None
        step = str(outcome.get("provider_step") or "")
        return {
            "agent_result": result,
            "part_id": outcome.get("part_id"),
            "phase": "review" if step == "ui_plan.review" else "draft",
            "attempt": outcome.get("draft_attempt"),
        }

    @staticmethod
    def _part_rejection(outcome: dict[str, Any]) -> dict[str, Any] | None:
        """NEW-142 review: the provider schema refusal that ended a part (its findings), so
        a later plan continuing the part sees it; Hermes's own rejections are recorded by
        ui_plan.record_part_draft."""
        findings = outcome.get("failed_schema_findings")
        if outcome.get("blocker") != "DRAFT_SCHEMA_INVALID" or not isinstance(findings, list):
            return None
        return {
            "part_id": outcome.get("part_id"),
            "attempt": outcome.get("draft_attempt"),
            "code": "DRAFT_SCHEMA_INVALID",
            "details": [str(item) for item in findings][:50],
        }

    async def _ui_plan_part(
        self, request: UiMigrationPlanInput, context: dict[str, Any], part: dict[str, Any]
    ) -> dict[str, Any]:
        """Draft (Claude) and review (Codex) one part; at most two redrafts of that part
        (NEW-138; one before the per-part patch)."""
        current: dict[str, Any] = {**context, "part": part, "part_rounds": []}
        max_attempts = 2
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            if attempt == 2 and workflow.patched(
                f"{self._TWO_REDRAFTS_PATCH}-{part.get('kind')}-{part.get('index')}"
            ):
                # NEW-138: scoped per part, so a part replayed from before the deploy (no
                # marker, memoized false) never limits the parts that follow it.
                max_attempts = 3
            if not await self._checkpoint("UI_PLAN_PART_DRAFT"):
                return {**current, "terminal": "FAILED", "blocker": "UI_PLAN_CANCELLED"}
            # NEW-142 review: prepare_part offers a continuation only to a workflow that
            # stages it (data only; the marker is per part, so replays stay unchanged).
            capable = attempt == 1 and workflow.patched(
                f"{self._CONTINUATION_PATCH}-{part.get('kind')}-{part.get('index')}"
            )
            current = await self._step(
                "ui_plan.prepare_part",
                {
                    **current,
                    "draft_attempt": attempt,
                    "max_draft_attempts": max_attempts,
                    "continuation_capable": capable,
                },
            )
            if current.get("status") == "PART_REUSED":
                return {**current, "status": "PART_APPROVED"}
            if current.get("status") != "DRAFT_READY":
                return {
                    **current,
                    "terminal": "FAILED",
                    "blocker": str(current.get("blocker") or "DRAFT_INPUT_INVALID"),
                }
            continued = current.get("continuation")
            if (
                capable
                and isinstance(current.get("continuation_document"), dict)
                and isinstance(continued, dict)
            ):
                # NEW-142: stage the earlier run's last reviewed draft the prompt continues
                # (hash-verified against what ui_plan.prepare_part bound into the prompt).
                previous = await self._step(
                    "ui_plan.stage_document",
                    {
                        "project": request.project,
                        "blueprint_id": request.blueprint_id,
                        "name": str((continued.get("file") or {}).get("name")),
                        "document": current.get("continuation_document"),
                    },
                )
                if (previous.get("document") or {}).get("sha256") != (
                    continued.get("file") or {}
                ).get("sha256"):
                    return {
                        **current,
                        "blocker": str(previous.get("blocker") or "UI_PLAN_INPUTS_UNAVAILABLE"),
                    }
            current = await self._agent_step(
                "ui_plan.draft", current, timeout_minutes=self._PART_TIMEOUT_MINUTES
            )
            if current.get("status") != "SUCCESS":
                if (
                    attempt < max_attempts
                    and current.get("blocker") == "SchemaViolation"
                    and workflow.patched(self._STRUCTURAL_REDRAFT_PATCH)
                ):
                    # NEW-132: the provider boundary refused the output against the same
                    # schema; its findings use one of the part's redrafts.
                    current = self._structural_redraft(
                        current,
                        "DRAFT_SCHEMA_INVALID",
                        list(current.get("failed_schema_findings") or []),
                        current.get("failed_agent_usage"),
                    )
                    continue
                if (
                    attempt == max_attempts
                    and current.get("blocker") == "SchemaViolation"
                    and (max_attempts == 3 or "rejection" in current)
                ):
                    # NEW-132: the last redraft was refused again; report its schema findings.
                    # NEW-138: on a part's third attempt, whatever came before it (an
                    # intervening REVISE clears the earlier rejection). Before the patch,
                    # unchanged. Only the returned blocker differs: no workflow command.
                    return {
                        **current,
                        "blocker": "DRAFT_SCHEMA_INVALID",
                        "blockers": [
                            f"{current.get('part_id')}: {item}"
                            for item in current.get("failed_schema_findings") or []
                        ][:50],
                    }
                return {**current, "blocker": self._ui_provider_blocker("DRAFT", current)}
            recorded = await self._step("ui_plan.record_part_draft", current)
            if recorded.get("status") != "DRAFT_RECORDED":
                if (
                    attempt < max_attempts
                    and recorded.get("blocker") in self._STRUCTURAL_REJECTIONS
                    and workflow.patched(self._STRUCTURAL_REDRAFT_PATCH)
                ):
                    # NEW-132: Hermes's own deterministic findings use one of the part's
                    # redrafts, like a REVISE (no review of the rejected draft).
                    current = self._structural_redraft(
                        recorded,
                        str(recorded.get("blocker")),
                        list(recorded.get("blockers") or []),
                        None,
                    )
                    continue
                return {
                    **recorded,
                    "blocker": str(recorded.get("blocker") or "DRAFT_SCHEMA_INVALID"),
                }
            current = {key: value for key, value in recorded.items() if key != "document"}
            name = f"part-{current.get('part_id')}-draft-{attempt}.json"
            draft = await self._step(
                "ui_plan.stage_document",
                {
                    "project": request.project,
                    "blueprint_id": request.blueprint_id,
                    "name": name,
                    "document": recorded.get("document"),
                },
            )
            if draft.get("status") != "STAGED":
                return {
                    **current,
                    "blocker": str(draft.get("blocker") or "UI_PLAN_INPUTS_UNAVAILABLE"),
                }
            current = {**current, "draft_file": draft.get("document")}
            if not await self._checkpoint("UI_PLAN_PART_REVIEW"):
                return {**current, "terminal": "FAILED", "blocker": "UI_PLAN_CANCELLED"}
            current = await self._step("ui_plan.prepare_part_review", current)
            if current.get("status") != "REVIEW_READY":
                return {
                    **current,
                    "terminal": "FAILED",
                    "blocker": str(current.get("blocker") or "REVIEW_INPUT_INVALID"),
                }
            current = await self._agent_step(
                "ui_plan.review", current, timeout_minutes=self._PART_TIMEOUT_MINUTES
            )
            if current.get("status") != "SUCCESS":
                return {**current, "blocker": self._ui_provider_blocker("REVIEW", current)}
            reviewed = await self._step("ui_plan.record_part_review", current)
            current = {key: value for key, value in reviewed.items() if key != "document"}
            if reviewed.get("status") == "PART_APPROVED":
                return current
            if reviewed.get("status") != "PART_REVISE":
                return {
                    **current,
                    "blocker": str(reviewed.get("blocker") or "REVIEW_SCHEMA_INVALID"),
                }
            review = await self._step(
                "ui_plan.stage_document",
                {
                    "project": request.project,
                    "blueprint_id": request.blueprint_id,
                    "name": f"part-{current.get('part_id')}-review-{attempt}.json",
                    "document": reviewed.get("document"),
                },
            )
            if review.get("status") != "STAGED":
                return {
                    **current,
                    "blocker": str(review.get("blocker") or "UI_PLAN_INPUTS_UNAVAILABLE"),
                }
            # A REVISE redraft answers this review, not an earlier structural rejection.
            current = {
                **{key: value for key, value in current.items() if key != "rejection"},
                "review_file": review.get("document"),
            }
        return {**current, "blocker": "BLUEPRINT_PART_REVIEW_UNRESOLVED"}

    @staticmethod
    def _ui_provider_blocker(phase: str, context: dict[str, Any]) -> str:
        blocker = str(context.get("blocker") or context.get("status") or "")
        if blocker in {"SchemaViolation", "PolicyViolation"} or not blocker:
            return f"{phase}_PROVIDER_FAILED"
        return blocker if re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", blocker) else f"{phase}_FAILED"


@workflow.defn(name="UiBlueprintFanOutWorkflow")
class UiBlueprintFanOutWorkflow(ControlledWorkflow):
    """NEW-123: after operator approval, one change-request DRAFT per blueprint item.

    Each epic, page, cutover and gap becomes a NEW-108 change request under a deterministic
    id (blueprint hash + item key): gaps are drafted by Claude (triage-ui-gap), the rest is
    rendered deterministically, and Codex reviews every draft. Idempotent and resumable,
    capped at max_items per run; batches continue as new. Hermes never publishes.
    """

    @workflow.run
    async def run(self, request: UiBlueprintFanOutInput) -> dict[str, Any]:
        plan = await self._step("ui_fanout.plan", asdict(request))
        counts = {
            "drafted": request.drafted,
            "failed": request.failed,
            "skipped": request.skipped,
        }
        processed = request.processed
        if plan.get("status") == "BLOCKED":
            return await self._fanout_finalize(
                request, plan, counts, processed, str(plan.get("blocker"))
            )
        items = [str(key) for key in plan.get("items", [])]
        source_worktree = request.source_worktree
        if items and not source_worktree:
            try:
                snapshot = await self._step(
                    "project.snapshot",
                    {"project": plan.get("project"), "campaign_id": plan.get("campaign_id")},
                    timeout_minutes=60,
                )
            except ActivityError:
                return await self._fanout_finalize(
                    request, plan, counts, processed, "UI_FANOUT_SNAPSHOT_UNAVAILABLE"
                )
            source_worktree = str(snapshot.get("source_worktree") or "")
        for key in items:
            if not await self._checkpoint("UI_FANOUT_ITEM"):
                return await self._fanout_finalize(
                    request, plan, counts, processed, "UI_FANOUT_CANCELLED"
                )
            outcome = await self._fanout_item(request.blueprint_id, key, source_worktree or "")
            counts[outcome] += 1
            processed += 1
        pending_after = int(plan.get("pending_total") or 0) - len(items)
        if items and pending_after > 0 and processed < request.max_items:
            workflow.continue_as_new(
                replace(
                    request,
                    processed=processed,
                    source_worktree=source_worktree,
                    drafted=counts["drafted"],
                    failed=counts["failed"],
                    skipped=counts["skipped"],
                )
            )
        return await self._fanout_finalize(request, plan, counts, processed, None)

    async def _fanout_finalize(
        self,
        request: UiBlueprintFanOutInput,
        plan: dict[str, Any],
        counts: dict[str, int],
        processed: int,
        blocker: str | None,
    ) -> dict[str, Any]:
        pending = max(0, int(plan.get("pending_total") or 0) - len(plan.get("items", [])))
        finalized = await self._step(
            "ui_fanout.finalize",
            {
                "blueprint_id": request.blueprint_id,
                "project": plan.get("project"),
                "max_items": request.max_items,
                "processed": processed,
                "pending_total": pending if blocker is None else plan.get("pending_total", 0),
                "blocker": blocker,
                **counts,
            },
        )
        return {
            "status": "BLOCKED" if blocker else "COMPLETED",
            "blueprint_id": request.blueprint_id,
            "error_code": blocker,
            "processed": processed,
            "pending": finalized.get("pending_total", pending),
            **counts,
            # NEW-146: the blueprint's failed items and why (bounded; finalize reads them).
            **(
                {"failed_items": finalized["failed_items"]} if finalized.get("failed_items") else {}
            ),
        }

    async def _fanout_item(self, blueprint_id: str, key: str, source_worktree: str) -> str:
        context: dict[str, Any] = {"blueprint_id": blueprint_id, "item_key": key}
        try:
            context = await self._step(
                "ui_fanout.prepare_item",
                {"blueprint_id": blueprint_id, "item_key": key, "source_worktree": source_worktree},
            )
            if context.get("status") == "SKIPPED":
                return "skipped"
            if context.get("status") == "FAILED_RECORDED":
                return "failed"
            if context.get("status") not in {"DRAFT_READY", "PROPOSAL_READY"}:
                return await self._fanout_item_failure(context, "draft")
            gap = context.get("status") == "DRAFT_READY"
            for attempt in (1, 2):
                if context.get("status") == "DRAFT_READY":
                    context = await self._agent_step(
                        "ui_fanout.gap_draft", context, timeout_minutes=70
                    )
                    if context.get("status") != "SUCCESS":
                        return await self._fanout_item_failure(context, "draft")
                    context = await self._step("ui_fanout.record_gap_draft", context)
                    if context.get("status") != "PROPOSAL_READY":
                        return await self._fanout_item_failure(context, "draft")
                context = await self._step(
                    "ui_fanout.prepare_review", {**context, "draft_attempt": attempt}
                )
                if context.get("status") != "REVIEW_READY":
                    return await self._fanout_item_failure(context, "draft")
                context = await self._agent_step(
                    "change_request.review", context, timeout_minutes=70
                )
                if context.get("status") != "SUCCESS":
                    return await self._fanout_item_failure(context, "review")
                context = await self._step("change_request.record_review", context)
                if context.get("status") == "REVIEW_APPROVED":
                    break
                if context.get("status") != "REVIEW_REVISE":
                    return await self._fanout_item_failure(context, "review")
                if not gap or attempt == 2:
                    # An unresolved review is persisted as a CANDIDATE with its findings.
                    break
                context = await self._step("ui_fanout.prepare_gap_draft", context)
                if context.get("status") != "DRAFT_READY":
                    return await self._fanout_item_failure(context, "draft")
            if context.get("dedupe_supported"):
                try:
                    context = await self._step("change_request.github_dedupe", context)
                except ActivityError:
                    context = {**context, "dedupe": dict(_UI_FANOUT_NO_DEDUPE)}
            else:
                context = {**context, "dedupe": dict(_UI_FANOUT_NO_DEDUPE)}
            persisted = await self._step("change_request.persist", context)
        except ActivityError:
            return await self._fanout_item_failure(
                {**context, "blocker": "STEP_FAILED"}, "internal"
            )
        return "drafted" if persisted.get("status") == "CANDIDATE" else "failed"

    async def _fanout_item_failure(self, context: dict[str, Any], phase: str) -> str:
        if not context.get("change_request_id"):
            return "failed"
        try:
            await self._step(
                "change_request.record_failure",
                {
                    "change_request_id": context.get("change_request_id"),
                    "revision": context.get("revision"),
                    "failure_phase": phase,
                    "blocker": str(context.get("blocker") or context.get("status") or "FAILED"),
                    **_blocker_detail(context),
                },
            )
        except ActivityError:
            workflow.logger.warning("ui fan-out item %s: failure was not recorded", context)
        return "failed"


@workflow.defn(name="UiGapConsolidationPlanWorkflow")
class UiGapConsolidationPlanWorkflow(ControlledWorkflow):
    """NEW-154: one bounded Claude step proposes clusters of duplicate gap requests.

    Hermes checks the plan deterministically and stores it with its sha256 (PROPOSED), or
    records why it was refused (INVALID) or failed. Nothing is merged: the operator applies
    a stored plan by its sha (one audited transaction, outside this workflow)."""

    @workflow.run
    async def run(self, request: UiGapConsolidationPlanInput) -> dict[str, Any]:
        context = await self._step("ui_consolidate.load", asdict(request))
        if context.get("status") != "READY":
            return await self._consolidation_end(
                request, str(context.get("status") or "FAILED"), context.get("blocker")
            )
        try:
            snapshot = await self._step(
                "project.snapshot",
                {"project": context.get("project"), "campaign_id": context.get("campaign_id")},
                timeout_minutes=60,
            )
        except ActivityError:
            return await self._consolidation_failure(request, "UI_GAP_SNAPSHOT_UNAVAILABLE", {})
        try:
            context = await self._step(
                "ui_consolidate.prepare_plan",
                {**context, "source_worktree": snapshot.get("source_worktree")},
            )
            if context.get("status") == "SUPERSEDED":
                return await self._consolidation_end(request, "SUPERSEDED", None)
            if context.get("status") != "PLAN_READY":
                return await self._consolidation_failure(
                    request, str(context.get("blocker") or "UI_GAP_PLAN_FAILED"), context
                )
            context = await self._agent_step("ui_consolidate.plan", context, timeout_minutes=40)
            if context.get("status") != "SUCCESS":
                return await self._consolidation_failure(
                    request, str(context.get("blocker") or context.get("status")), context
                )
            recorded = await self._step("ui_consolidate.record_plan", context)
        except ActivityError:
            return await self._consolidation_failure(request, "STEP_FAILED", {})
        return await self._consolidation_end(
            request, str(recorded.get("status") or "FAILED"), recorded.get("blocker"), recorded
        )

    async def _consolidation_failure(
        self, request: UiGapConsolidationPlanInput, blocker: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        recorded = await self._step(
            "ui_consolidate.record_failure",
            {
                "consolidation_id": request.consolidation_id,
                "blocker": blocker,
                **_blocker_detail(context),
            },
        )
        return await self._consolidation_end(
            request, str(recorded.get("status") or "FAILED"), blocker
        )

    async def _consolidation_end(
        self,
        request: UiGapConsolidationPlanInput,
        status: str,
        blocker: Any,
        recorded: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            await self._step(
                "ui_consolidate.finalize",
                {
                    "consolidation_id": request.consolidation_id,
                    "terminal_status": status,
                    "error_code": blocker,
                },
            )
        except ActivityError:
            workflow.logger.warning(
                "ui gap consolidation %s: control Run finalization failed",
                request.consolidation_id,
            )
        return {
            "status": status,
            "consolidation_id": request.consolidation_id,
            "error_code": blocker,
            **{
                key: value
                for key, value in (recorded or {}).items()
                if key in {"plan_sha256", "clusters", "merged", "problems"}
            },
        }


@workflow.defn(name="BacklogDecompositionPlanWorkflow")
class BacklogDecompositionPlanWorkflow(ControlledWorkflow):
    """NEW-234 PR5 flow D: the backlog decomposition plan, persisted as an approvable
    candidate. Modelled on UiMigrationPlanWorkflow's batched plan (a new workflow type: the
    UI workflows and their patch markers are untouched).

    snapshot at the pinned source SHA -> read-only GitHub issue snapshot -> deterministic
    inventory -> the frame, then one batch per epic (PR 4's epic_order): Claude drafts
    (decompose-backlog), Codex reviews (review-decomposition), at most three drafts and
    three reviews per part -> assembly, where a whole-plan problem a part owns goes back to
    that part as a redraft -> CANDIDATE with its approval binding. It never approves,
    publishes or edits anything.
    """

    # Design section 3 (engineering.decomposition.MAX_PART_DRAFTS / MAX_PART_REVIEWS).
    _MAX_PART_DRAFTS = 3
    _ASSEMBLY_ROUNDS = 3
    # Per part provider call: the request's own timeout is PART_TIMEOUT_SECONDS (1800 s).
    _PART_TIMEOUT_MINUTES = 40
    _STRUCTURAL_REJECTIONS = frozenset(
        {"DRAFT_SCHEMA_INVALID", "DRAFT_STRUCTURE_INVALID", "DRAFT_BINDING_MISMATCH"}
    )
    # A provider call runs once per budget reservation (review: Temporal retries and
    # capacity retries would execute it again uncharged). A failed call ends the plan; a
    # new plan reuses every part already approved.

    @workflow.run
    async def run(self, request: BacklogDecompositionPlanInput) -> dict[str, Any]:
        try:
            result = await self._plan(request)
        except ActivityError:
            result = await self._decomposition_terminal(
                {"decomposition_id": request.decomposition_id, "project": request.project},
                "FAILED",
                "DECOMPOSITION_STEP_FAILED",
            )
        try:
            await self._step(
                "decomposition.finalize_run",
                {
                    "decomposition_id": request.decomposition_id,
                    "terminal_status": result.get("status"),
                    "error_code": result.get("error_code"),
                },
            )
        except ActivityError:
            workflow.logger.warning(
                "decomposition %s: Run finalization failed", request.decomposition_id
            )
        return result

    async def _decomposition_terminal(
        self,
        context: dict[str, Any],
        status: str,
        blocker: str,
        blockers: list[Any] | None = None,
        *,
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        recorded = await self._step(
            "decomposition.record_terminal",
            {
                "decomposition_id": context.get("decomposition_id"),
                "project": context.get("project"),
                "terminal_status": status,
                "blocker": blocker,
                "blockers": list(blockers or [])[:50],
                **({"part_usage": usage} if usage else {}),
            },
        )
        return {
            "status": recorded.get("status", status),
            "decomposition_id": context.get("decomposition_id"),
            "error_code": recorded.get("error_code", blocker),
            "blockers": list(blockers or [])[:50],
        }

    async def _plan(self, request: BacklogDecompositionPlanInput) -> dict[str, Any]:
        context = await self._step("decomposition.register", asdict(request))
        if context.get("status") == "BLOCKED":
            return {
                "status": "BLOCKED",
                "decomposition_id": request.decomposition_id,
                "error_code": context.get("blocker"),
                "blockers": context.get("blockers", []),
            }
        if context.get("status") != "REGISTERED":
            return {"status": "SUPERSEDED", "decomposition_id": request.decomposition_id}
        if not await self._checkpoint("DECOMPOSITION_SNAPSHOT"):
            return await self._decomposition_terminal(context, "FAILED", "DECOMPOSITION_CANCELLED")
        try:
            snapshot = await self._step(
                "project.snapshot",
                {
                    "project": request.project,
                    "campaign_id": context["campaign_id"],
                    "source_sha": context.get("source_sha"),
                },
                timeout_minutes=60,
            )
        except ActivityError:
            return await self._decomposition_terminal(
                context, "BLOCKED", "DECOMPOSITION_SNAPSHOT_UNAVAILABLE"
            )
        source = {
            "source_worktree": snapshot.get("source_worktree"),
            "source_sha": snapshot.get("source_sha"),
        }
        if not await self._checkpoint("DECOMPOSITION_INVENTORY"):
            return await self._decomposition_terminal(context, "FAILED", "DECOMPOSITION_CANCELLED")
        issues = await self._step(
            "decomposition.read_issues",
            {"project": request.project, "decomposition_id": request.decomposition_id},
        )
        if issues.get("status") != "ISSUES_READ":
            return await self._decomposition_terminal(
                context,
                "BLOCKED",
                str(issues.get("blocker") or "DECOMPOSITION_ISSUES_UNAVAILABLE"),
                [str(issues.get("reason") or "")[:300]],
            )
        inventory = await self._step(
            "decomposition.inventory",
            {
                "project": request.project,
                "decomposition_id": request.decomposition_id,
                "snapshot": source,
                "issues": issues.get("issues"),
            },
        )
        if inventory.get("status") == "BLOCKED":
            return await self._decomposition_terminal(
                context,
                "BLOCKED",
                str(inventory.get("blocker") or "DECOMPOSITION_SOURCE_INVALID"),
                inventory.get("blockers"),
            )
        if inventory.get("status") != "INVENTORY_PASS":
            return await self._decomposition_terminal(
                context, "FAILED", "DECOMPOSITION_INVENTORY_FAILED"
            )
        recorded = await self._step(
            "decomposition.record_inputs",
            {**context, **source, "inventory": inventory.get("inventory")},
        )
        if recorded.get("status") in {"BLOCKED", "FAILED"}:
            return await self._decomposition_terminal(
                context,
                str(recorded.get("status")),
                str(recorded.get("blocker") or "DECOMPOSITION_INVENTORY_MISMATCH"),
                recorded.get("blockers"),
            )
        if recorded.get("status") != "INPUTS_RECORDED":
            return {"status": "SUPERSEDED", "decomposition_id": request.decomposition_id}
        context = {key: value for key, value in recorded.items() if key != "status"}
        approved: list[dict[str, Any]] = []
        for part in list(context.get("parts") or []):
            outcome = await self._decomposition_part(
                request, {**context, "approved_parts": list(approved)}, part
            )
            if outcome.get("status") != "PART_APPROVED":
                return await self._part_terminal(context, outcome)
            approved.append(self._approved_entry(part, outcome))
        assembled: dict[str, Any] = {}
        for assembly_round in range(self._ASSEMBLY_ROUNDS + 1):
            if not await self._checkpoint("DECOMPOSITION_ASSEMBLE"):
                return await self._decomposition_terminal(
                    context, "FAILED", "DECOMPOSITION_CANCELLED"
                )
            assembled = await self._step(
                "decomposition.assemble", {**context, "approved_parts": approved}
            )
            if assembled.get("status") == "ASSEMBLED":
                break
            redrafts = list(assembled.get("redrafts") or [])
            if (
                assembled.get("status") != "ASSEMBLY_REDRAFT"
                or assembly_round == self._ASSEMBLY_ROUNDS
            ):
                return await self._decomposition_terminal(
                    context,
                    "BLOCKED",
                    str(assembled.get("blocker") or "ASSEMBLY_INVALID"),
                    assembled.get("blockers") or self._redraft_blockers(redrafts),
                )
            for redraft in redrafts:
                # A whole-plan problem goes back to the part that owns it, as its next
                # draft (the part's draft budget is shared with its review rounds).
                index = next(
                    (
                        position
                        for position, item in enumerate(approved)
                        if item["part_id"] == redraft.get("part_id")
                    ),
                    None,
                )
                if index is None:
                    return await self._decomposition_terminal(
                        context, "FAILED", "DECOMPOSITION_PART_UNKNOWN"
                    )
                entry = approved[index]
                if int(entry["attempt"]) >= self._MAX_PART_DRAFTS:
                    return await self._decomposition_terminal(
                        context, "BLOCKED", "ASSEMBLY_INVALID", self._redraft_blockers([redraft])
                    )
                outcome = await self._decomposition_part(
                    request,
                    {**context, "approved_parts": approved[:index]},
                    {"kind": entry["kind"], "number": entry["number"]},
                    redraft={
                        "part_id": entry["part_id"],
                        "part_input": entry["part_input"],
                        "first_attempt": int(entry["attempt"]) + 1,
                        "rejection": {
                            "code": "ASSEMBLY_INVALID",
                            "details": list(redraft.get("problems") or [])[:50],
                        },
                    },
                )
                if outcome.get("status") != "PART_APPROVED":
                    return await self._part_terminal(context, outcome)
                approved[index] = self._approved_entry(
                    {"kind": entry["kind"], "number": entry["number"]}, outcome
                )
        if not await self._checkpoint("DECOMPOSITION_PERSIST"):
            return await self._decomposition_terminal(context, "FAILED", "DECOMPOSITION_CANCELLED")
        published = await self._step(
            "decomposition.stage_document",
            {
                "project": request.project,
                "decomposition_id": request.decomposition_id,
                "publish": True,
                "document": assembled.get("document"),
                "markdown": assembled.get("markdown"),
            },
        )
        persisted = await self._step(
            "decomposition.persist",
            {
                "decomposition_id": request.decomposition_id,
                "project": request.project,
                "plan_sha256": assembled.get("plan_sha256"),
                "evidence": published.get("evidence", []),
            },
        )
        return {
            "status": persisted.get("status", "FAILED"),
            "decomposition_id": request.decomposition_id,
            "error_code": persisted.get("error_code"),
            "plan_sha256": persisted.get("plan_sha256"),
            "approval_binding": persisted.get("approval_binding"),
            "parts": len(approved),
            "stories": persisted.get("stories"),
            "tasks": persisted.get("tasks"),
        }

    @staticmethod
    def _approved_entry(part: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
        return {
            "part_id": outcome.get("part_id"),
            "kind": part.get("kind"),
            "number": part.get("number"),
            "content_sha256": outcome.get("content_sha256"),
            "attempt": int(outcome.get("draft_attempt") or 1),
            "part_input": outcome.get("part_input"),
        }

    @staticmethod
    def _redraft_blockers(redrafts: list[Any]) -> list[str]:
        lines = []
        for redraft in redrafts:
            for problem in redraft.get("problems") or []:
                lines.append(
                    f"{redraft.get('part_id')}: [{problem.get('code')}] "
                    f"{problem.get('subject')}: {problem.get('detail')}"
                )
        return lines[:50]

    async def _part_terminal(
        self, context: dict[str, Any], outcome: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._decomposition_terminal(
            context,
            str(outcome.get("terminal") or "BLOCKED"),
            str(outcome.get("blocker") or "DECOMPOSITION_PART_FAILED"),
            outcome.get("blockers"),
            usage=self._part_usage(outcome),
        )

    @staticmethod
    def _part_usage(outcome: dict[str, Any]) -> dict[str, Any] | None:
        """A failed part's last provider result, so its token usage is still recorded."""
        result = outcome.get("agent_result")
        if not isinstance(result, dict):
            result = outcome.get("failed_agent_usage")
        if not isinstance(result, dict) or outcome.get("status") == "SUCCESS":
            return None
        step = str(outcome.get("provider_step") or "")
        return {
            "agent_result": result,
            "part_id": outcome.get("part_id"),
            "phase": "review" if step == "decomposition.review" else "draft",
            "attempt": outcome.get("draft_attempt"),
        }

    @staticmethod
    def _structural_redraft(
        current: dict[str, Any], code: str, details: list[Any], usage: Any
    ) -> dict[str, Any]:
        """The context for the next draft after Hermes or the provider boundary refused
        this one (no review ran)."""
        dropped = {
            "status",
            "document",
            "blocker",
            "blockers",
            "failed_agent_usage",
            "failed_schema_findings",
            "agent_result",
        }
        redraft = {key: value for key, value in current.items() if key not in dropped}
        redraft["rejection"] = {"code": code, "details": list(details)[:50]}
        if isinstance(usage, dict):
            # Recorded by decomposition.prepare_part (the refused call consumed tokens).
            redraft["rejected_usage"] = {
                "agent_result": usage,
                "part_id": current.get("part_id"),
                "attempt": current.get("draft_attempt"),
            }
        return redraft

    @staticmethod
    def _provider_blocker(phase: str, context: dict[str, Any]) -> str:
        blocker = str(context.get("blocker") or context.get("status") or "")
        if blocker in {"SchemaViolation", "PolicyViolation"} or not blocker:
            return f"{phase}_PROVIDER_FAILED"
        return blocker if re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", blocker) else f"{phase}_FAILED"

    async def _stage_part_input(
        self, request: BacklogDecompositionPlanInput, context: dict[str, Any], part: dict[str, Any]
    ) -> dict[str, Any]:
        """The part's id and staged input (hash-verified against what
        decomposition.prepare_part_input bound into the part), or a terminal outcome."""
        prepared = await self._step("decomposition.prepare_part_input", {**context, "part": part})
        if prepared.get("status") == "BLOCKED":
            return {**prepared, "terminal": "BLOCKED"}
        if prepared.get("status") not in {"INPUT_READY", "PART_REUSED"}:
            return {**prepared, "terminal": "FAILED", "blocker": "DRAFT_INPUT_INVALID"}
        staged = await self._step(
            "decomposition.stage_inputs",
            {
                "project": request.project,
                "decomposition_id": request.decomposition_id,
                "files": prepared.get("files"),
            },
        )
        receipts = staged.get("files") or {}
        hashes = prepared.get("file_hashes") or {}
        if staged.get("status") != "STAGED" or any(
            (receipts.get(name) or {}).get("sha256") != digest for name, digest in hashes.items()
        ):
            return {
                "part_id": prepared.get("part_id"),
                "status": "BLOCKED",
                "terminal": "BLOCKED",
                "blocker": str(staged.get("blocker") or "DECOMPOSITION_INPUTS_UNAVAILABLE"),
                "blockers": staged.get("blockers"),
            }
        name = str(prepared.get("input_name"))
        return {
            "status": prepared.get("status"),
            "part_id": prepared.get("part_id"),
            "content_sha256": prepared.get("content_sha256"),
            "draft_attempt": prepared.get("draft_attempt"),
            "part_input": receipts.get(name),
            "reference_files": {key: value for key, value in receipts.items() if key != name},
        }

    async def _decomposition_part(
        self,
        request: BacklogDecompositionPlanInput,
        context: dict[str, Any],
        part: dict[str, Any],
        *,
        redraft: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Draft (Claude) and review (Codex) one part: at most three drafts, each reviewed
        unless Hermes refused it; an assembly `redraft` continues the part's attempts."""
        if redraft is None:
            staged = await self._stage_part_input(request, context, part)
            if staged.get("status") == "PART_REUSED":
                return {**staged, "status": "PART_APPROVED"}
            if staged.get("status") != "INPUT_READY":
                return staged
            current: dict[str, Any] = {
                **context,
                "part": part,
                "part_id": staged.get("part_id"),
                "part_input": staged.get("part_input"),
                "reference_files": staged.get("reference_files"),
                "part_rounds": [],
            }
            attempt = 0
        else:
            current = {
                **context,
                "part": part,
                "part_id": redraft["part_id"],
                "part_input": redraft["part_input"],
                "part_rounds": [],
                "rejection": redraft["rejection"],
            }
            attempt = int(redraft["first_attempt"]) - 1
        while attempt < self._MAX_PART_DRAFTS:
            attempt += 1
            if not await self._checkpoint("DECOMPOSITION_PART_DRAFT"):
                return {**current, "terminal": "FAILED", "blocker": "DECOMPOSITION_CANCELLED"}
            current = await self._step(
                "decomposition.prepare_part", {**current, "draft_attempt": attempt}
            )
            if current.get("status") == "BLOCKED":
                return {**current, "terminal": "BLOCKED"}
            if current.get("status") != "DRAFT_READY":
                return {
                    **current,
                    "terminal": "FAILED",
                    "blocker": str(current.get("blocker") or "DRAFT_INPUT_INVALID"),
                }
            current = await self._agent_step(
                "decomposition.draft",
                current,
                timeout_minutes=self._PART_TIMEOUT_MINUTES,
                retry_policy=SINGLE_ATTEMPT,
            )
            if current.get("status") != "SUCCESS":
                if attempt < self._MAX_PART_DRAFTS and current.get("blocker") == "SchemaViolation":
                    # The provider boundary refused the output against the part schema; its
                    # findings use one of the part's redrafts.
                    current = self._structural_redraft(
                        current,
                        "DRAFT_SCHEMA_INVALID",
                        list(current.get("failed_schema_findings") or []),
                        current.get("failed_agent_usage"),
                    )
                    continue
                return {**current, "blocker": self._provider_blocker("DRAFT", current)}
            recorded = await self._step("decomposition.record_part_draft", current)
            if recorded.get("status") != "DRAFT_RECORDED":
                if (
                    attempt < self._MAX_PART_DRAFTS
                    and recorded.get("blocker") in self._STRUCTURAL_REJECTIONS
                ):
                    # Hermes's own deterministic findings use one of the part's redrafts,
                    # like a REVISE (no review of the rejected draft).
                    current = self._structural_redraft(
                        recorded,
                        str(recorded.get("blocker")),
                        list(recorded.get("blockers") or []),
                        None,
                    )
                    continue
                return {
                    **recorded,
                    "blocker": str(recorded.get("blocker") or "DRAFT_SCHEMA_INVALID"),
                }
            current = {key: value for key, value in recorded.items() if key != "document"}
            draft = await self._step(
                "decomposition.stage_document",
                {
                    "project": request.project,
                    "decomposition_id": request.decomposition_id,
                    "name": f"part-{current.get('part_id')}-draft-{attempt}.json",
                    "document": recorded.get("document"),
                },
            )
            if draft.get("status") != "STAGED":
                return {
                    **current,
                    "blocker": str(draft.get("blocker") or "DECOMPOSITION_INPUTS_UNAVAILABLE"),
                    "blockers": draft.get("blockers"),
                }
            current = {**current, "draft_file": draft.get("document")}
            if not await self._checkpoint("DECOMPOSITION_PART_REVIEW"):
                return {**current, "terminal": "FAILED", "blocker": "DECOMPOSITION_CANCELLED"}
            current = await self._step("decomposition.prepare_part_review", current)
            if current.get("status") == "BLOCKED":
                return {**current, "terminal": "BLOCKED"}
            if current.get("status") != "REVIEW_READY":
                return {
                    **current,
                    "terminal": "FAILED",
                    "blocker": str(current.get("blocker") or "REVIEW_INPUT_INVALID"),
                }
            current = await self._agent_step(
                "decomposition.review",
                current,
                timeout_minutes=self._PART_TIMEOUT_MINUTES,
                retry_policy=SINGLE_ATTEMPT,
            )
            if current.get("status") != "SUCCESS":
                return {**current, "blocker": self._provider_blocker("REVIEW", current)}
            reviewed = await self._step("decomposition.record_part_review", current)
            current = {key: value for key, value in reviewed.items() if key != "document"}
            if reviewed.get("status") == "PART_APPROVED":
                return current
            if reviewed.get("status") != "PART_REVISE":
                return {
                    **current,
                    "blocker": str(reviewed.get("blocker") or "REVIEW_SCHEMA_INVALID"),
                }
            review = await self._step(
                "decomposition.stage_document",
                {
                    "project": request.project,
                    "decomposition_id": request.decomposition_id,
                    "name": f"part-{current.get('part_id')}-review-{attempt}.json",
                    "document": reviewed.get("document"),
                },
            )
            if review.get("status") != "STAGED":
                return {
                    **current,
                    "blocker": str(review.get("blocker") or "DECOMPOSITION_INPUTS_UNAVAILABLE"),
                    "blockers": review.get("blockers"),
                }
            # A REVISE redraft answers this review, not an earlier Hermes rejection.
            current = {
                **{key: value for key, value in current.items() if key != "rejection"},
                "review_file": review.get("document"),
            }
        return {**current, "blocker": "DECOMPOSITION_PART_REVIEW_UNRESOLVED"}


ALL_WORKFLOWS = [
    AuditCampaignWorkflow,
    MultiProjectAuditWorkflow,
    FindingVerificationWorkflow,
    IssueDeliveryWorkflow,
    DeliveryReconciliationWorkflow,
    FinalClosureWorkflow,
    PreviewDeploymentWorkflow,
    StagingDeploymentWorkflow,
    RuntimeVerificationWorkflow,
    RecoveryWorkflow,
    QuotaRefreshWorkflow,
    ConnectionsRefreshWorkflow,
    CleanupWorkflow,
    AcceptanceLifecycleWorkflow,
    ObservabilityAcceptanceWorkflow,
    ReverseObservabilityAcceptanceWorkflow,
    IssueSolutionLoopWorkflow,
    GitHubBacklogImportWorkflow,
    IssueAmendObservationWorkflow,
    PublicationHeadsObservationWorkflow,
    ExistingGitHubBacklogLoopWorkflow,
    AutonomousBacklogCampaignWorkflow,
    EnhancementDiscoveryWorkflow,
    EnhancementDeliveryWorkflow,
    EnhancementSolutionLoopWorkflow,
    LLMWikiStagingBootstrapWorkflow,
    StagingGitOpsDeliveryWorkflow,
    ChangeRequestWorkflow,
    UiMigrationPlanWorkflow,
    UiBlueprintFanOutWorkflow,
    UiGapConsolidationPlanWorkflow,
    BacklogDecompositionPlanWorkflow,
]
