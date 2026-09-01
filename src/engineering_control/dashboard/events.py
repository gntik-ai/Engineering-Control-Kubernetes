from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import UUID

from engineering_control.dashboard.observability import DashboardObservabilityProjection
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.dashboard.schemas import ActivityEventInput

_STEP_EVENTS = {
    "agent.record_start": "provider.selected",
    "agent.persist_result": "provider.completed",
    "audit.prepare_agent_cell": "audit_cell.started",
    "campaign.persist_cell_result": "audit_cell.completed",
    "campaign.persist_cell_blocker": "audit_cell.blocked",
    "campaign.finalize": "workflow.completed",
    "finding.finalize": "finding.verified",
    "finding.persist_publication": "finding.published",
    "delivery.ensure_issue": "github.operation_attempted",
    "delivery.persist_publication": "github.operation_completed",
    "preview.deploy": "deployment.started",
    "preview.finalize": "deployment.completed",
    "staging.deploy": "deployment.started",
    "staging.finalize": "deployment.completed",
    "runtime.snapshot": "runtime_verification.started",
    "runtime.finalize": "runtime_verification.completed",
    "runtime.record_failure": "runtime_verification.failed",
    "backlog.record_loop_state": "issue_status.changed",
    "cleanup.finalize": "workflow.completed",
}

AsyncPredicate = Callable[[], Awaitable[bool]]


def event_for_step(
    step: str,
    payload: dict[str, Any],
    *,
    source: str,
    worker_kind: str,
    level: str = "INFO",
) -> ActivityEventInput:
    orchestration = payload.get("orchestration")
    context = orchestration if isinstance(orchestration, dict) else {}
    status = _find(payload, "status")
    blocker = _find(payload, "blocker") or _find(payload, "error_code")
    if blocker:
        level = "ERROR" if "FAIL" in str(status).upper() else "WARNING"
    event_type = _STEP_EVENTS.get(step, "workflow.phase_changed")
    if blocker and event_type.endswith("completed"):
        event_type = event_type.removesuffix("completed") + "failed"
    return ActivityEventInput(
        level=level,
        event_type=event_type,
        source=source,
        host=socket.gethostname(),
        worker_kind=worker_kind,
        project=_bounded_project(_find(payload, "project")),
        workflow_id=_bounded(_find(context, "workflow_id"), 240),
        temporal_run_id=_bounded(_find(context, "workflow_run_id"), 240),
        issue_number=_positive_int(_find(payload, "issue_number")),
        campaign_id=_bounded(_find(payload, "campaign_id"), 240),
        entity_type="activity",
        entity_id=_bounded(_find(context, "activity_id"), 240),
        current_phase=step[:120],
        message=f"{worker_kind} activity {step} {str(status or 'completed').lower()}",
        metadata={
            "step": step,
            "status": str(status)[:240] if status is not None else "completed",
            "blocker_code": str(blocker)[:240] if blocker is not None else None,
            "attempt": _find(context, "attempt"),
            "task_queue": _bounded(_find(context, "task_queue"), 240),
        },
    )


class BestEffortEventRecorder:
    """Persist redacted operational events without ever changing activity results."""

    def __init__(self, repository: DashboardRepository) -> None:
        self.repository = repository

    async def record(self, event: ActivityEventInput) -> None:
        try:
            await self.repository.record_event(event)
        except Exception:  # noqa: BLE001 - projection failure must not affect workflow behavior
            return


async def sse_events(
    repository: DashboardRepository,
    *,
    after_id: int,
    disconnected: AsyncPredicate,
    authorized: AsyncPredicate,
    poll_seconds: float = 1.0,
    heartbeat_seconds: float = 15.0,
    authorization_seconds: float = 5.0,
    maximum_iterations: int | None = None,
) -> AsyncIterator[str]:
    cursor = max(0, after_id)
    since_heartbeat = 0.0
    loop = asyncio.get_running_loop()
    next_authorization = loop.time()
    iterations = 0
    while maximum_iterations is None or iterations < maximum_iterations:
        if await disconnected():
            return
        if loop.time() >= next_authorization:
            if not await authorized():
                return
            next_authorization = loop.time() + max(0.0, authorization_seconds)
        events = await repository.recent_events(limit=100, after_id=cursor)
        if events:
            for event in events:
                cursor = int(event["id"])
                payload = json.dumps(event, separators=(",", ":"), sort_keys=True)
                yield f"id: {cursor}\nevent: activity\ndata: {payload}\n\n"
            since_heartbeat = 0.0
        elif since_heartbeat >= heartbeat_seconds:
            yield ": heartbeat\n\n"
            since_heartbeat = 0.0
        iterations += 1
        await asyncio.sleep(poll_seconds)
        since_heartbeat += poll_seconds


async def transcript_sse(
    projection: DashboardObservabilityProjection,
    execution_id: UUID,
    *,
    after_sequence: int,
    disconnected: AsyncPredicate,
    authorized: AsyncPredicate,
    poll_seconds: float = 1.0,
    heartbeat_seconds: float = 15.0,
    authorization_seconds: float = 5.0,
    maximum_iterations: int | None = None,
) -> AsyncIterator[str]:
    cursor = max(0, after_sequence)
    since_heartbeat = 0.0
    loop = asyncio.get_running_loop()
    next_authorization = loop.time()
    iterations = 0
    while maximum_iterations is None or iterations < maximum_iterations:
        if await disconnected():
            return
        if loop.time() >= next_authorization:
            if not await authorized():
                return
            next_authorization = loop.time() + max(0.0, authorization_seconds)
        result = await asyncio.to_thread(
            projection.execution_logs,
            execution_id,
            limit=100,
            after_sequence=cursor,
        )
        if result is None:
            return
        items = result["items"]
        if isinstance(items, list) and items:
            for item in items:
                if not isinstance(item, dict):
                    continue
                cursor = int(item["sequence"])
                payload = json.dumps(item, separators=(",", ":"), sort_keys=True)
                yield f"id: {cursor}\nevent: transcript\ndata: {payload}\n\n"
            since_heartbeat = 0.0
        elif since_heartbeat >= heartbeat_seconds:
            yield ": heartbeat\n\n"
            since_heartbeat = 0.0
        iterations += 1
        await asyncio.sleep(poll_seconds)
        since_heartbeat += poll_seconds


def _find(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for nested in value.values():
            found = _find(nested, key)
            if found is not None:
                return found
    return None


def _bounded(value: Any, limit: int) -> str | None:
    return str(value)[:limit] if value not in (None, "") else None


def _bounded_project(value: Any) -> str | None:
    text = _bounded(value, 32)
    return text if text in {"falcone", "musematic", "llmwiki", "platform"} else None


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
