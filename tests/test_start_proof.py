import asyncio
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.api.workflowservice.v1 import DescribeWorkflowExecutionResponse
from temporalio.converter import DataConverter
from temporalio.service import RPCError, RPCStatusCode

from engineering_control.db.start_proof import prove_execution_start

NAMESPACE = "fixture-only"
WORKFLOW_ID = "wf-fixture"


async def make_fixture(
    *,
    continued=False,
    app_id=None,
    parent=None,
    memo_version=1,
    corrupt_memo=None,
    workflow_id=WORKFLOW_ID,
    workflow_type="AuditCampaignWorkflow",
):
    current_run = str(uuid.uuid4())
    first_run = str(uuid.uuid4()) if continued else current_run

    def _raw(wf_id, run_id, first_id):
        raw = DescribeWorkflowExecutionResponse()
        raw.workflow_execution_info.execution.workflow_id = wf_id
        raw.workflow_execution_info.execution.run_id = run_id
        raw.workflow_execution_info.first_run_id = first_id
        return raw

    current_desc = SimpleNamespace(
        id=workflow_id,
        run_id=current_run,
        workflow_type=workflow_type,
        raw_description=_raw(workflow_id, current_run, first_run),
    )
    first_desc = SimpleNamespace(
        id=workflow_id,
        run_id=first_run,
        workflow_type=workflow_type,
        raw_description=_raw(workflow_id, first_run, first_run),
    )

    event = HistoryEvent()
    event.event_id = 1
    event.event_type = EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED
    attrs = event.workflow_execution_started_event_attributes
    attrs.workflow_id = workflow_id
    attrs.workflow_type.name = workflow_type
    attrs.first_execution_run_id = first_run

    if app_id is not None:
        payloads = await DataConverter.default.encode([app_id, memo_version])
        if corrupt_memo == "missing_id":
            attrs.memo.fields["engineering_binding_version"].CopyFrom(payloads[1])
        elif corrupt_memo == "missing_version":
            attrs.memo.fields["engineering_application_run_id"].CopyFrom(payloads[0])
        elif corrupt_memo == "oversized":
            payloads[0].data = b"x" * 8300
            attrs.memo.fields["engineering_application_run_id"].CopyFrom(payloads[0])
            attrs.memo.fields["engineering_binding_version"].CopyFrom(payloads[1])
        else:
            attrs.memo.fields["engineering_application_run_id"].CopyFrom(payloads[0])
            attrs.memo.fields["engineering_binding_version"].CopyFrom(payloads[1])

    if parent is not None:
        attrs.parent_workflow_namespace = parent.get("namespace", NAMESPACE)
        if "workflow_id" in parent:
            attrs.parent_workflow_execution.workflow_id = parent["workflow_id"]
        if "run_id" in parent:
            attrs.parent_workflow_execution.run_id = parent["run_id"]

    class FakeIterator:
        def __init__(self, ev):
            self._ev = ev
            self._used = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._used:
                raise AssertionError("history iterator consumed more than once")
            self._used = True
            return self._ev

    class FakeHandle:
        def __init__(self, run_id, desc, rpc_error=None):
            self.run_id = run_id
            self._desc = desc
            self._rpc_error = rpc_error

        async def describe(self, rpc_timeout=None):
            assert rpc_timeout.total_seconds() == 3
            if self._rpc_error:
                raise self._rpc_error
            return self._desc

        def fetch_history_events(self, **kwargs):
            assert self.run_id == first_run
            assert kwargs == {
                "page_size": 1,
                "wait_new_event": False,
                "skip_archival": True,
                "rpc_timeout": timedelta(seconds=3),
            }
            return FakeIterator(event)

    handles = {
        current_run: FakeHandle(current_run, current_desc),
        first_run: FakeHandle(first_run, first_desc),
    }

    class FakeClient:
        namespace = NAMESPACE
        data_converter = DataConverter.default

        def get_workflow_handle(self, wf_id, run_id=None):
            assert wf_id == workflow_id
            assert run_id in handles
            return handles[run_id]

    return SimpleNamespace(
        client=FakeClient(),
        current=current_run,
        first=first_run,
        app_id=app_id,
        event=event,
        handles=handles,
        workflow_id=workflow_id,
    )


@pytest.mark.asyncio
async def test_single_run_valid():
    fx = await make_fixture()
    proof = await prove_execution_start(fx.client, NAMESPACE, WORKFLOW_ID, fx.current)
    assert proof.first_run_id == fx.first
    assert proof.current_run_id == fx.current


@pytest.mark.asyncio
async def test_continued_run_uses_first_history():
    app_id = str(uuid.uuid4())
    fx = await make_fixture(continued=True, app_id=app_id)
    proof = await prove_execution_start(fx.client, NAMESPACE, WORKFLOW_ID, fx.current)
    assert proof.application_run_id == uuid.UUID(app_id)
    assert proof.first_run_id != proof.current_run_id


@pytest.mark.asyncio
async def test_absent_memo_returns_none():
    fx = await make_fixture()
    proof = await prove_execution_start(fx.client, NAMESPACE, WORKFLOW_ID, fx.current)
    assert proof.application_run_id is None


@pytest.mark.asyncio
async def test_unicode_workflow_id_accepted():
    workflow_id = "wf-\u00e9\u65e5\u672c"
    fx = await make_fixture(workflow_id=workflow_id)
    proof = await prove_execution_start(fx.client, NAMESPACE, workflow_id, fx.current)
    assert proof.workflow_id == workflow_id


@pytest.mark.asyncio
async def test_parent_ids_retained():
    parent = {"workflow_id": "parent-wf", "run_id": str(uuid.uuid4())}
    fx = await make_fixture(parent=parent)
    proof = await prove_execution_start(fx.client, NAMESPACE, WORKFLOW_ID, fx.current)
    assert proof.parent_workflow_id == parent["workflow_id"]
    assert proof.parent_run_id == parent["run_id"]


@pytest.mark.asyncio
async def test_parent_foreign_namespace_rejected():
    parent = {"namespace": "other-ns", "workflow_id": "parent-wf", "run_id": str(uuid.uuid4())}
    fx = await make_fixture(parent=parent)
    with pytest.raises(ValueError, match="execution start proof is unverifiable"):
        await prove_execution_start(fx.client, NAMESPACE, WORKFLOW_ID, fx.current)


@pytest.mark.asyncio
async def test_parent_partial_metadata_rejected():
    parent = {"workflow_id": "parent-wf"}
    fx = await make_fixture(parent=parent)
    with pytest.raises(ValueError, match="execution start proof is unverifiable"):
        await prove_execution_start(fx.client, NAMESPACE, WORKFLOW_ID, fx.current)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(memo_version=0),
        dict(memo_version=2),
        dict(memo_version=True),
        dict(corrupt_memo="missing_id"),
        dict(corrupt_memo="missing_version"),
        dict(corrupt_memo="oversized"),
    ],
)
@pytest.mark.asyncio
async def test_memo_corruptions_rejected(kwargs):
    app_id = str(uuid.uuid4())
    fx = await make_fixture(app_id=app_id, **kwargs)
    with pytest.raises(ValueError, match="execution start proof is unverifiable"):
        await prove_execution_start(fx.client, NAMESPACE, WORKFLOW_ID, fx.current)


@pytest.mark.parametrize(
    "bad_id", ["not-a-uuid", str(uuid.UUID(int=0)), "12345678-1234-1234-1234-12345678901Z"]
)
@pytest.mark.asyncio
async def test_memo_invalid_uuid_rejected(bad_id):
    fx = await make_fixture(app_id=bad_id)
    with pytest.raises(ValueError, match="execution start proof is unverifiable"):
        await prove_execution_start(fx.client, NAMESPACE, WORKFLOW_ID, fx.current)


@pytest.mark.asyncio
async def test_attrs_workflow_id_mismatch_rejected():
    fx = await make_fixture()
    fx.event.workflow_execution_started_event_attributes.workflow_id = "other-wf"
    with pytest.raises(ValueError, match="execution start proof is unverifiable"):
        await prove_execution_start(fx.client, NAMESPACE, WORKFLOW_ID, fx.current)


@pytest.mark.asyncio
async def test_attrs_type_mismatch_rejected():
    fx = await make_fixture()
    fx.event.workflow_execution_started_event_attributes.workflow_type.name = "OtherWorkflow"
    with pytest.raises(ValueError, match="execution start proof is unverifiable"):
        await prove_execution_start(fx.client, NAMESPACE, WORKFLOW_ID, fx.current)


@pytest.mark.asyncio
async def test_event_id_not_one_rejected():
    fx = await make_fixture()
    fx.event.event_id = 2
    with pytest.raises(ValueError, match="execution start proof is unverifiable"):
        await prove_execution_start(fx.client, NAMESPACE, WORKFLOW_ID, fx.current)


@pytest.mark.asyncio
async def test_event_type_wrong_rejected():
    fx = await make_fixture()
    fx.event.event_type = EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED
    with pytest.raises(ValueError, match="execution start proof is unverifiable"):
        await prove_execution_start(fx.client, NAMESPACE, WORKFLOW_ID, fx.current)


@pytest.mark.asyncio
async def test_first_continued_run_id_set_rejected():
    fx = await make_fixture()
    fx.event.workflow_execution_started_event_attributes.continued_execution_run_id = str(
        uuid.uuid4()
    )
    with pytest.raises(ValueError, match="execution start proof is unverifiable"):
        await prove_execution_start(fx.client, NAMESPACE, WORKFLOW_ID, fx.current)


@pytest.mark.asyncio
async def test_raw_current_mismatch_rejected():
    fx = await make_fixture()
    fx.handles[fx.current]._desc.raw_description.workflow_execution_info.execution.run_id = str(
        uuid.uuid4()
    )
    with pytest.raises(ValueError, match="execution start proof is unverifiable"):
        await prove_execution_start(fx.client, NAMESPACE, WORKFLOW_ID, fx.current)


@pytest.mark.asyncio
async def test_rpc_not_found_propagates():
    fx = await make_fixture()
    fx.handles[fx.current] = SimpleNamespace(
        describe=_raise_rpc(RPCError("not found", RPCStatusCode.NOT_FOUND, b"")),
    )
    with pytest.raises(RPCError):
        await prove_execution_start(fx.client, NAMESPACE, WORKFLOW_ID, fx.current)


def _raise_rpc(exc):
    async def _inner(rpc_timeout=None):
        raise exc

    return _inner


@pytest.mark.asyncio
async def test_timeout_propagates():
    fx = await make_fixture()
    fx.handles[fx.current] = SimpleNamespace(describe=_raise_rpc(TimeoutError()))
    with pytest.raises(TimeoutError):
        await prove_execution_start(fx.client, NAMESPACE, WORKFLOW_ID, fx.current)


@pytest.mark.asyncio
async def test_cancelled_error_propagates():
    fx = await make_fixture()
    fx.handles[fx.current] = SimpleNamespace(describe=_raise_rpc(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await prove_execution_start(fx.client, NAMESPACE, WORKFLOW_ID, fx.current)
