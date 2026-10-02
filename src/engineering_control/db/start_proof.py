"""Immutable Temporal start-proof reader.

Callers must supply an already-authenticated/approved Client. This module
authenticates the *observed* Temporal workflow lineage (current run, first
run, originating memo) but does not by itself authorize any database
ownership or recovery action -- it produces evidence for later exact Run
enrollment/recovery decisions made elsewhere.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import temporalio.service
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client

_UNVERIFIABLE_MSG = "execution start proof is unverifiable"
_RPC_TIMEOUT = timedelta(seconds=3)
_NIL_UUID = UUID(int=0)
_MEMO_ID_KEY = "engineering_application_run_id"
_MEMO_VERSION_KEY = "engineering_binding_version"
_MAX_MEMO_BYTES = 8192


@dataclass(frozen=True)
class ExecutionStartProof:
    namespace: str
    workflow_id: str
    current_run_id: str
    first_run_id: str
    workflow_type: str
    application_run_id: UUID | None
    parent_workflow_id: str | None
    parent_run_id: str | None


def _fail() -> ValueError:
    return ValueError(_UNVERIFIABLE_MSG)


def _check_str(value: object) -> str:
    if type(value) is not str:
        raise _fail()
    if not (1 <= len(value) <= 240):
        raise _fail()
    if not value.isprintable():
        raise _fail()
    return value


def _check_uuid_str(value: object) -> str:
    if type(value) is not str or len(value) != 36:
        raise _fail()
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise _fail() from None
    if str(parsed) != value:
        raise _fail()
    if parsed == _NIL_UUID:
        raise _fail()
    return value


async def prove_execution_start(
    client: Client,
    namespace: str,
    workflow_id: str,
    current_run_id: str,
) -> ExecutionStartProof:
    _check_str(namespace)
    _check_str(workflow_id)
    _check_uuid_str(current_run_id)
    if namespace != client.namespace:
        raise _fail()

    async with asyncio.timeout(10):
        try:
            current_handle = client.get_workflow_handle(workflow_id, run_id=current_run_id)
            description = await current_handle.describe(rpc_timeout=_RPC_TIMEOUT)

            if description.id != workflow_id or description.run_id != current_run_id:
                raise _fail()

            raw_info = description.raw_description.workflow_execution_info
            execution = raw_info.execution
            if execution.workflow_id != workflow_id or execution.run_id != current_run_id:
                raise _fail()

            workflow_type = _check_str(description.workflow_type)

            first_run_id = _check_uuid_str(raw_info.first_run_id)

            if first_run_id != current_run_id:
                first_handle = client.get_workflow_handle(workflow_id, run_id=first_run_id)
                first_description = await first_handle.describe(rpc_timeout=_RPC_TIMEOUT)
                if (
                    first_description.id != workflow_id
                    or first_description.run_id != first_run_id
                    or first_description.workflow_type != workflow_type
                ):
                    raise _fail()
                first_raw_info = first_description.raw_description.workflow_execution_info
                if first_raw_info.first_run_id != first_run_id:
                    raise _fail()
                first_execution = first_raw_info.execution
                if (
                    first_execution.workflow_id != workflow_id
                    or first_execution.run_id != first_run_id
                ):
                    raise _fail()
            else:
                first_handle = current_handle

            iterator = first_handle.fetch_history_events(
                page_size=1,
                wait_new_event=False,
                skip_archival=True,
                rpc_timeout=_RPC_TIMEOUT,
            )
            try:
                first_event = await anext(iterator)
            except StopAsyncIteration:
                raise _fail() from None

            if first_event.event_id != 1:
                raise _fail()
            if first_event.event_type != EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED:
                raise _fail()
            if not first_event.HasField("workflow_execution_started_event_attributes"):
                raise _fail()
            attrs = first_event.workflow_execution_started_event_attributes

            if attrs.workflow_id != workflow_id:
                raise _fail()
            if attrs.workflow_type.name != workflow_type:
                raise _fail()
            if attrs.first_execution_run_id != first_run_id:
                raise _fail()
            if attrs.continued_execution_run_id != "":
                raise _fail()

            memo_fields = attrs.memo.fields
            has_id = _MEMO_ID_KEY in memo_fields
            has_version = _MEMO_VERSION_KEY in memo_fields
            if has_id != has_version:
                raise _fail()

            application_run_id: UUID | None = None
            if has_id:
                id_payload = memo_fields[_MEMO_ID_KEY]
                version_payload = memo_fields[_MEMO_VERSION_KEY]
                if id_payload.ByteSize() + version_payload.ByteSize() > _MAX_MEMO_BYTES:
                    raise _fail()
                try:
                    decoded = await client.data_converter.decode([id_payload, version_payload])
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    raise
                except Exception:
                    raise _fail() from None
                if len(decoded) != 2:
                    raise _fail()
                decoded_id, decoded_version = decoded
                if type(decoded_version) is not int or decoded_version != 1:
                    raise _fail()
                if type(decoded_id) is not str:
                    raise _fail()
                application_run_id = UUID(_check_uuid_str(decoded_id))

            parent_workflow_id: str | None = None
            parent_run_id: str | None = None
            if attrs.HasField("parent_workflow_execution"):
                if attrs.parent_workflow_namespace != namespace:
                    raise _fail()
                parent_workflow_id = _check_str(attrs.parent_workflow_execution.workflow_id)
                parent_run_id = _check_uuid_str(attrs.parent_workflow_execution.run_id)
            elif attrs.parent_workflow_namespace != "":
                raise _fail()

        except asyncio.CancelledError:
            raise
        except (temporalio.service.RPCError, TimeoutError):
            raise
        except Exception:
            raise _fail() from None

    return ExecutionStartProof(
        namespace=namespace,
        workflow_id=workflow_id,
        current_run_id=current_run_id,
        first_run_id=first_run_id,
        workflow_type=workflow_type,
        application_run_id=application_run_id,
        parent_workflow_id=parent_workflow_id,
        parent_run_id=parent_run_id,
    )
