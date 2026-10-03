"""Truthful application-level projection for a workflow, kept separate from raw Temporal status.

Temporal COMPLETED only means the orchestration finished; it says nothing about whether the
underlying business operation succeeded. This module derives a projection strictly from
persisted `runs` rows written by activities, never from Temporal history or workflow results,
and fails closed (AMBIGUOUS / UNAVAILABLE) rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from engineering_control.run_projection import classify_run_projection

_ALLOWED_BUSINESS_STATUS = frozenset(
    {
        "RUNNING",
        "SUCCESS",
        "BLOCKED",
        "FAILED",
        "PLANNED",
        "QUEUED",
        "IN_PROGRESS",
        "CANCELLED",
        "CANCELED",
        "COMPLETE",
        "COMPLETED",
        "SKIPPED",
        "STOPPED",
        "STOPPED_AFTER_CURRENT",
        "TERMINATED",
        "TIMED_OUT",
        "AWAITING_APPROVAL",
        "AWAITING_BUILD",
        "AWAITING_CI",
        "AWAITING_HUMAN",
        "EXTERNAL_WAIT",
        "PAUSED",
        "DRY_RUN",
        "EMPTY",
        "NOT_RUN",
    }
)
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,119}\Z", re.ASCII)


@dataclass(frozen=True)
class RunOutcomeRow:
    """Bounded scalar projection of a single `runs` row — never a full row copy."""

    status: str
    error_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True)
class WorkflowApplicationProjection:
    run_association: str
    persisted_status: str | None
    business_status: str | None
    error_code: str | None
    run_projection: str | None
    observed_at: str
    updated_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    source: str = "APPLICATION_RUN_PROJECTION"
    scope: str = "WORKFLOW_ID_ONLY"


def _safe_status(raw: str) -> str | None:
    return raw if isinstance(raw, str) and raw in _ALLOWED_BUSINESS_STATUS else None


def _safe_error_code(raw: str | None) -> str | None:
    if not isinstance(raw, str):
        return None
    return raw if _ERROR_CODE.fullmatch(raw) else "REDACTED_ERROR_CODE"


def _utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


def derive_workflow_application_projection(
    rows: list[RunOutcomeRow], *, now: datetime | None = None
) -> WorkflowApplicationProjection:
    """Derive an honest application-level projection from persisted `runs` rows.

    - No matching row: unknown business outcome (run_association=NONE).
    - More than one matching row: fail closed as AMBIGUOUS, never "latest wins".
    - Exactly one row: expose its persisted status/error_code (allowlisted/bounded)
      as an explicitly UNVERIFIED workflow-level association, since Run has no
      execution binding to the specific Temporal run.
    """
    observed = now or datetime.now(UTC)
    if len(rows) == 0:
        return WorkflowApplicationProjection(
            run_association="NONE",
            persisted_status=None,
            business_status=None,
            error_code=None,
            run_projection=None,
            observed_at=observed.isoformat(),
        )
    if len(rows) > 1:
        return WorkflowApplicationProjection(
            run_association="AMBIGUOUS",
            persisted_status=None,
            business_status=None,
            error_code=None,
            run_projection="AMBIGUOUS",
            observed_at=observed.isoformat(),
        )
    row = rows[0]
    projection = classify_run_projection(
        status=row.status if isinstance(row.status, str) else "",
        started_at=row.started_at,
        finished_at=row.finished_at,
        updated_at=row.updated_at,
        now=observed,
    )
    return WorkflowApplicationProjection(
        run_association="UNVERIFIED",
        persisted_status=_safe_status(row.status),
        business_status=None,
        error_code=_safe_error_code(row.error_code),
        run_projection=projection,
        observed_at=observed.isoformat(),
        updated_at=_utc(row.updated_at),
        started_at=_utc(row.started_at),
        finished_at=_utc(row.finished_at),
    )
