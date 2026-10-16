"""Contract tests for engineering_control.db.runtime_binding."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from temporalio.api.workflowservice.v1 import DescribeWorkflowExecutionResponse
from temporalio.testing import ActivityEnvironment

from engineering_control.db import runtime_binding
from engineering_control.db.models import Base, Run

# Fixed run ids: FIRST.upper() parametrizes a test, and pytest-xdist workers must collect
# identical test ids (NEW-212).
CURRENT = "1b50086a-d899-44d3-8d30-d393cacc9abb"
FIRST = "4ea50575-fdc9-4dbf-b031-94c15e9f0957"


def _Info(workflow_id="wf-1", workflow_run_id=CURRENT, namespace="ns-a"):
    return replace(
        ActivityEnvironment.default_info(),
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        namespace=namespace,
    )


class _Description:
    def __init__(self, id_: str, run_id: str, first=FIRST) -> None:
        self.id = id_
        self.run_id = run_id
        self.raw_description = DescribeWorkflowExecutionResponse()
        raw = self.raw_description.workflow_execution_info
        raw.execution.workflow_id, raw.execution.run_id = id_, run_id
        raw.first_run_id = first


class _Handle:
    def __init__(self, workflow_id: str, run_id: str, result: _Description) -> None:
        self.describe_calls: list[timedelta] = []
        self._result = result

    async def describe(self, rpc_timeout: timedelta) -> _Description:
        self.describe_calls.append(rpc_timeout)
        return self._result


class _Client:
    def __init__(self, namespace: str, result: _Description) -> None:
        self.namespace = namespace
        self._result = result
        self.handle_calls: list[tuple[str, str]] = []
        self.last_handle: _Handle | None = None

    def get_workflow_handle(self, workflow_id: str, run_id: str) -> _Handle:
        self.handle_calls.append((workflow_id, run_id))
        self.last_handle = _Handle(workflow_id, run_id, self._result)
        return self.last_handle


def _patch(monkeypatch, info: _Info, client: _Client) -> None:
    monkeypatch.setattr(runtime_binding.activity, "info", lambda: info)
    monkeypatch.setattr(runtime_binding.activity, "client", lambda: client)


def _fields(**overrides) -> dict:
    base = dict(workflow_id="wf-1", run_type="ingest", status="pending", project="proj")
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_happy_path_binds_pinned_run_and_first_id(monkeypatch):
    info = _Info()
    client = _Client("ns-a", _Description("wf-1", CURRENT))
    _patch(monkeypatch, info, client)

    async with runtime_binding.verified_activity_run_binding():
        run = runtime_binding.new_bound_run(**_fields())

    assert run.temporal_first_execution_run_id == FIRST
    assert client.handle_calls == [("wf-1", CURRENT)]
    assert client.last_handle.describe_calls == [timedelta(seconds=3)]


@pytest.mark.asyncio
async def test_namespace_mismatch_denied(monkeypatch):
    _patch(monkeypatch, _Info(namespace="ns-a"), _Client("ns-b", _Description("wf-1", CURRENT)))
    with pytest.raises(ValueError, match=runtime_binding._BINDING_ERROR):
        async with runtime_binding.verified_activity_run_binding():
            pass


@pytest.mark.asyncio
async def test_description_drift_denied(monkeypatch):
    _patch(monkeypatch, _Info(), _Client("ns-a", _Description("wf-1", str(uuid4()))))
    with pytest.raises(ValueError, match=runtime_binding._BINDING_ERROR):
        async with runtime_binding.verified_activity_run_binding():
            pass


@pytest.mark.asyncio
async def test_absent_first_run_id_denied(monkeypatch):
    info = _Info()
    _patch(monkeypatch, info, _Client("ns-a", _Description("wf-1", CURRENT, "")))
    with pytest.raises(ValueError, match=runtime_binding._BINDING_ERROR):
        async with runtime_binding.verified_activity_run_binding():
            pass


def test_payload_binding_injection_denied():
    with pytest.raises(ValueError):
        runtime_binding.new_bound_run(**_fields(temporal_first_execution_run_id=None))
    with pytest.raises(ValueError):
        runtime_binding.new_bound_run(**_fields(temporal_first_execution_run_id=FIRST))


def test_outside_context_leaves_field_null():
    run = runtime_binding.new_bound_run(**_fields())
    assert run.temporal_first_execution_run_id is None


@pytest.mark.asyncio
async def test_unrelated_child_workflow_stays_null(monkeypatch):
    _patch(monkeypatch, _Info(workflow_id="wf-1"), _Client("ns-a", _Description("wf-1", CURRENT)))
    async with runtime_binding.verified_activity_run_binding():
        run = runtime_binding.new_bound_run(**_fields(workflow_id="wf-different"))
    assert run.temporal_first_execution_run_id is None


@pytest.mark.asyncio
async def test_exception_resets_contextvar(monkeypatch):
    _patch(
        monkeypatch, _Info(namespace="ns-mismatch"), _Client("ns-a", _Description("wf-1", CURRENT))
    )
    with pytest.raises(ValueError):
        async with runtime_binding.verified_activity_run_binding():
            pass
    assert runtime_binding._current_identity.get() is None


@pytest.mark.asyncio
async def test_cancellation_resets_contextvar(monkeypatch):
    _patch(monkeypatch, _Info(), _Client("ns-a", _Description("wf-1", CURRENT)))

    async def _inner() -> None:
        try:
            async with runtime_binding.verified_activity_run_binding():
                await asyncio.sleep(10)
        finally:
            assert runtime_binding._current_identity.get() is None

    task = asyncio.create_task(_inner())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime_binding._current_identity.get() is None


_fake_state: ContextVar[tuple[_Info, _Client] | None] = ContextVar("_fake_state", default=None)


@pytest.mark.asyncio
async def test_concurrent_task_isolation(monkeypatch):
    monkeypatch.setattr(runtime_binding.activity, "info", lambda: _fake_state.get()[0])
    monkeypatch.setattr(runtime_binding.activity, "client", lambda: _fake_state.get()[1])

    async def _bound(workflow_id: str, first: str, results: dict, key: str) -> None:
        current = str(uuid4())
        info = _Info(workflow_id=workflow_id, workflow_run_id=current)
        client = _Client("ns-a", _Description(workflow_id, current, first))
        _fake_state.set((info, client))
        async with runtime_binding.verified_activity_run_binding():
            await asyncio.sleep(0.01)
            run = runtime_binding.new_bound_run(**_fields(workflow_id=workflow_id))
            results[key] = run.temporal_first_execution_run_id

    first_a, first_b = str(uuid4()), str(uuid4())
    results: dict = {}
    await asyncio.gather(
        _bound("wf-a", first_a, results, "a"),
        _bound("wf-b", first_b, results, "b"),
    )
    assert results == {"a": first_a, "b": first_b}


@pytest.mark.asyncio
async def test_to_thread_sees_context(monkeypatch):
    _patch(monkeypatch, _Info(), _Client("ns-a", _Description("wf-1", CURRENT)))
    async with runtime_binding.verified_activity_run_binding():
        identity = await asyncio.to_thread(runtime_binding._current_identity.get)
    assert identity is not None
    assert identity.first_run_id == FIRST


@pytest.mark.asyncio
async def test_actual_sdk_context_and_existing_row_preserved():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            session.add(Run(**_fields()))
            session.commit()
            before = dict(session.execute(select(Run.__table__)).mappings().one())
            environment = ActivityEnvironment(client=_Client("ns-a", _Description("wf-1", CURRENT)))
            environment.info = _Info()

            async def run_inside_real_sdk():
                async with runtime_binding.verified_activity_run_binding():
                    return await asyncio.to_thread(runtime_binding.new_bound_run, **_fields())

            created = await environment.run(run_inside_real_sdk)
            assert created.temporal_first_execution_run_id == FIRST and CURRENT != FIRST
            assert dict(session.execute(select(Run.__table__)).mappings().one()) == before
            assert runtime_binding._current_identity.get() is None
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", ["", "0" * 36, "00000000-0000-0000-0000-000000000000", FIRST.upper()]
)
@pytest.mark.parametrize("field", ["current", "first"])
async def test_invalid_uuid_rejected(monkeypatch, value, field):
    current, first = (value, FIRST) if field == "current" else (CURRENT, value)
    _patch(
        monkeypatch,
        _Info(workflow_run_id=current),
        _Client("ns-a", _Description("wf-1", current, first)),
    )
    with pytest.raises(ValueError, match="unverifiable identity"):
        async with runtime_binding.verified_activity_run_binding():
            pytest.fail("unverified binding entered")


def test_workflow_controls_denied_unicode_accepted():
    assert (
        runtime_binding.new_bound_run(**_fields(workflow_id="prueba-ñ-東京")).workflow_id
        == "prueba-ñ-東京"
    )
    for invalid in ("bad\n", "bad\t", "bad\x85", "", "x" * 241):
        with pytest.raises(ValueError, match="valid workflow_id"):
            runtime_binding.new_bound_run(**_fields(workflow_id=invalid))


@pytest.mark.asyncio
async def test_nested_exception_restores_outer_identity(monkeypatch):
    _patch(monkeypatch, _Info(), _Client("ns-a", _Description("wf-1", CURRENT)))
    async with runtime_binding.verified_activity_run_binding():
        outer = runtime_binding._current_identity.get()
        with pytest.raises(RuntimeError, match="synthetic"):
            async with runtime_binding.verified_activity_run_binding():
                raise RuntimeError("synthetic")
        assert runtime_binding._current_identity.get() is outer
    assert runtime_binding._current_identity.get() is None


@pytest.mark.asyncio
async def test_raw_execution_drift_and_rpc_failure(monkeypatch):
    description = _Description("wf-1", CURRENT)
    description.raw_description.workflow_execution_info.execution.run_id = str(uuid4())
    _patch(monkeypatch, _Info(), _Client("ns-a", description))
    with pytest.raises(ValueError, match="unverifiable identity"):
        async with runtime_binding.verified_activity_run_binding():
            pytest.fail("drift entered")

    async def timeout(_self, rpc_timeout):
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(_Handle, "describe", timeout)
    with pytest.raises(TimeoutError, match="synthetic timeout"):
        async with runtime_binding.verified_activity_run_binding():
            pytest.fail("timeout entered")
    assert runtime_binding._current_identity.get() is None
