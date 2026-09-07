from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from engineering_control.db.models import DashboardEvent, NotificationOutbox, OperationRequest
from engineering_control.retention_limits import NOTIFICATION_OUTBOX_PENDING_STATUS
from engineering_control.security.redaction import SecretRedactor


def record_dispatch_receipt(
    session: Session,
    operation_id: UUID,
    *,
    status: str,
    workflow_id: str | None = None,
    run_id: str | None = None,
    safe_error: str | None = None,
    linked: bool = False,
) -> bool:
    """Atomically stage one dispatch receipt, event and notification intent.

    The caller owns commit/rollback. This never sends a notification or reports
    eventual workflow completion. A superseding receipt/state is not rewritten.
    """
    if status not in {"STARTED", "COMPLETED", "FAILED", "BLOCKED"}:
        raise ValueError("operation terminal dispatch status is invalid")
    if status == "STARTED" and (
        not isinstance(workflow_id, str) or not workflow_id.strip() or len(workflow_id) > 240
    ):
        raise ValueError("operation dispatch workflow identity is invalid")
    if run_id is not None and (not isinstance(run_id, str) or len(run_id) > 240):
        raise ValueError("operation dispatch run identity is invalid")
    if status != "STARTED" and (workflow_id is not None or run_id is not None or linked):
        raise ValueError("non-workflow dispatch outcome cannot replace a workflow receipt")
    if not session.in_transaction():
        raise RuntimeError("dispatch receipt requires a caller-owned transaction")
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(text("SET LOCAL lock_timeout = '5s'"))
        session.execute(text("SET LOCAL statement_timeout = '10s'"))
    operation = session.get(
        OperationRequest, operation_id, with_for_update=True, populate_existing=True
    )
    if operation is None:
        raise ValueError("operation disappeared during dispatch")
    if (
        operation.status not in {"PENDING", "DISPATCHING"}
        or operation.workflow_id is not None
        or operation.temporal_run_id is not None
        or operation.completed_at is not None
    ):
        return False

    now = datetime.now(UTC)
    operation.status = status
    operation.workflow_id = workflow_id
    operation.temporal_run_id = run_id
    operation.safe_error = (
        str(SecretRedactor().redact(safe_error[:2000]).value)[:500] if safe_error else None
    )
    if status == "STARTED":
        if operation.started_at is None:
            operation.started_at = now
    else:
        operation.completed_at = now

    event_type = "operation.linked" if linked else f"operation.{status.lower()}"
    level = "WARNING" if status == "BLOCKED" else "ERROR" if status == "FAILED" else "INFO"
    message = {
        "operation.blocked": "Typed operation was blocked by current policy or state",
        "operation.failed": "Typed application operation failed",
        "operation.completed": "Typed operation completed without starting a workflow",
        "operation.started": "Typed operation started one durable workflow",
        "operation.linked": "Typed operation linked an existing durable workflow",
    }[event_type]
    session.add(
        DashboardEvent(
            occurred_at=now,
            level=level,
            event_type=event_type,
            source="controller",
            host=None,
            worker_kind="controller",
            project=operation.project,
            workflow_id=(
                workflow_id
                or (operation.target_id if operation.target_type == "workflow" else None)
            ),
            temporal_run_id=run_id,
            issue_number=(
                int(operation.target_id.rsplit("#", 1)[1])
                if operation.target_type == "github_issue"
                and operation.target_id.rsplit("#", 1)[-1].isdigit()
                else None
            ),
            campaign_id=(
                operation.target_id
                if operation.target_type in {"backlog_campaign", "audit", "audit_campaign"}
                else None
            ),
            entity_type="operation",
            entity_id=str(operation.id),
            current_phase=status,
            message=message,
            metadata_json={
                "action": operation.action,
                "status": status,
                "operation": str(operation.id),
            },
        )
    )
    if status == "STARTED":
        # A first receipt recovered from Temporal is still worth notifying.
        # Its LINKED phase says it adopted an existing execution, not a new one.
        session.add(
            NotificationOutbox(
                topic="telegram.engineering-operation-started",
                status=NOTIFICATION_OUTBOX_PENDING_STATUS,
                payload_json={
                    "operation_id": str(operation.id),
                    "project": operation.project,
                    "target": operation.target_id,
                    "workflow_id": workflow_id,
                    "phase": "LINKED" if linked else "QUEUED",
                    "event_type": event_type,
                },
                attempts=0,
                next_attempt_at=now,
                delivered_at=None,
                last_error_code=None,
            )
        )
    return True
