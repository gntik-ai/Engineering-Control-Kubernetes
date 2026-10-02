"""Temporal-authenticated identity binding for Run.temporal_first_execution_run_id."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from typing import TypeGuard
from uuid import UUID

from temporalio import activity

from engineering_control.db.models import Run

_BINDING_ERROR = "verified activity run binding: unverifiable identity"
_MAX_WORKFLOW_ID_LEN = 240
_DESCRIBE_TIMEOUT = timedelta(seconds=3)
_OUTER_TIMEOUT_SECONDS = 4


@dataclass(frozen=True)
class _Identity:
    workflow_id: str
    current_run_id: str
    first_run_id: str


_current_identity: ContextVar[_Identity | None] = ContextVar("_current_identity", default=None)


def _is_printable_bounded(value: object) -> TypeGuard[str]:
    return type(value) is str and 0 < len(value) <= _MAX_WORKFLOW_ID_LEN and value.isprintable()


def _canonical_nonnil_uuid(value: object) -> str | None:
    if type(value) is not str or len(value) != 36:
        return None
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None
    if parsed.int == 0 or str(parsed) != value:
        return None
    return value


@asynccontextmanager
async def verified_activity_run_binding() -> AsyncIterator[None]:
    """Prove, inside a running Temporal activity, the identity of its run and bind it."""
    try:
        info = activity.info()
        client = activity.client()
    except RuntimeError:
        raise ValueError(_BINDING_ERROR) from None

    workflow_id = getattr(info, "workflow_id", None)
    namespace = getattr(info, "namespace", None)
    canonical_current = _canonical_nonnil_uuid(getattr(info, "workflow_run_id", None))

    if not _is_printable_bounded(workflow_id):
        raise ValueError(_BINDING_ERROR)
    if canonical_current is None:
        raise ValueError(_BINDING_ERROR)
    if not _is_printable_bounded(namespace) or namespace != getattr(client, "namespace", None):
        raise ValueError(_BINDING_ERROR)

    handle = client.get_workflow_handle(workflow_id, run_id=canonical_current)

    async with asyncio.timeout(_OUTER_TIMEOUT_SECONDS):
        description = await handle.describe(rpc_timeout=_DESCRIBE_TIMEOUT)

    raw = getattr(getattr(description, "raw_description", None), "workflow_execution_info", None)
    execution = getattr(raw, "execution", None)
    canonical_first = _canonical_nonnil_uuid(getattr(raw, "first_run_id", None))
    if (
        getattr(description, "id", None) != workflow_id
        or getattr(description, "run_id", None) != canonical_current
        or getattr(execution, "workflow_id", None) != workflow_id
        or getattr(execution, "run_id", None) != canonical_current
        or canonical_first is None
    ):
        raise ValueError(_BINDING_ERROR)

    identity = _Identity(
        workflow_id=workflow_id,
        current_run_id=canonical_current,
        first_run_id=canonical_first,
    )
    token = _current_identity.set(identity)
    try:
        yield
    finally:
        _current_identity.reset(token)


def new_bound_run(**fields: object) -> Run:
    """Construct a brand-new Run, binding its first-execution run id if proven."""
    if "temporal_first_execution_run_id" in fields:
        raise ValueError("new_bound_run does not accept temporal_first_execution_run_id")
    run = Run(**fields)
    if not _is_printable_bounded(getattr(run, "workflow_id", None)):
        raise ValueError("new_bound_run requires a valid workflow_id")

    identity = _current_identity.get()
    if identity is not None and identity.workflow_id == run.workflow_id:
        run.temporal_first_execution_run_id = identity.first_run_id
    return run
