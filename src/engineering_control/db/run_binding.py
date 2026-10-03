"""Runtime binding between a Run row and its first confirmed Temporal execution.

Trusted-caller contract: `workflow_id` and `first_execution_run_id` MUST come
from a successful, already-authenticated Temporal start/registration call made
by the caller. This module performs no authentication and never queries
Temporal; it only validates shape and persists a first-write-wins binding
within the caller's own transaction (no commit here). Historical rows are
never backfilled or guessed at: any row with
`temporal_first_execution_run_id IS NULL` remains explicitly UNVERIFIED until
a trusted caller binds it via this function.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from engineering_control.db.models import Run

_NIL_UUID = UUID(int=0)


def _canonical_uuid(value: str) -> UUID:
    if type(value) is not str or len(value) != 36:
        raise ValueError("first_execution_run_id is invalid")
    try:
        parsed = UUID(value)
    except ValueError:
        raise ValueError("first_execution_run_id is invalid") from None
    if parsed == _NIL_UUID:
        raise ValueError("first_execution_run_id must not be the nil UUID")
    if str(parsed) != value:
        raise ValueError("first_execution_run_id must be a canonical UUID string")
    return parsed


def _validate_workflow_id(workflow_id: str) -> None:
    if (
        type(workflow_id) is not str
        or not 1 <= len(workflow_id) <= 240
        or not workflow_id.isprintable()
    ):
        raise ValueError("workflow_id is invalid")


def bind_run_execution(
    session: Session,
    application_run_id: UUID,
    workflow_id: str,
    first_execution_run_id: str,
) -> bool:
    """First-write-wins binding of a run to its first Temporal execution run ID.

    Returns True if this call performed the binding, False if an identical
    binding already existed. Raises ValueError for a missing row, a
    workflow_id mismatch, a conflicting existing binding, or malformed input.
    Never echoes caller-supplied values in error messages. Does not commit;
    the caller owns the transaction and must roll it back on any exception.
    The session must have no pending new/dirty/deleted objects: this primitive
    must not incidentally autoflush unrelated changes. Only the exact row identified by
    `application_run_id` is ever touched, even when other rows share the same
    `workflow_id`.
    """
    _validate_workflow_id(workflow_id)
    canonical_execution_id = str(_canonical_uuid(first_execution_run_id))
    if type(application_run_id) is not UUID or application_run_id == _NIL_UUID:
        raise ValueError("application_run_id is invalid")
    if session.new or session.dirty or session.deleted:
        raise ValueError("run binding requires a clean session")

    row = session.execute(
        select(Run)
        .where(Run.id == application_run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        raise ValueError("application_run_id does not match an existing run")
    if row.workflow_id != workflow_id:
        raise ValueError("workflow_id does not match the bound run")

    if row.temporal_first_execution_run_id == canonical_execution_id:
        return False
    if row.temporal_first_execution_run_id is not None:
        raise ValueError("run is already bound to a different execution")

    result = session.execute(
        update(Run)
        .where(
            Run.id == application_run_id,
            Run.workflow_id == workflow_id,
            Run.temporal_first_execution_run_id.is_(None),
        )
        .values(
            temporal_first_execution_run_id=canonical_execution_id,
            updated_at=row.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    if cast("CursorResult[Any]", result).rowcount != 1:
        raise ValueError("run binding could not be applied")

    session.expire(row, ["temporal_first_execution_run_id", "updated_at"])
    return True
