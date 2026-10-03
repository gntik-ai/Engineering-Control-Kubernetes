import asyncio
import os
import uuid
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    import pytest
    from temporalio.client import Client, WorkflowHistory
    from temporalio.worker import Replayer, Worker
    from test_run_binding_real_temporal import FixtureAudit

    from engineering_control.db.start_proof import prove_execution_start

from engineering_control.workflows.definitions import ACTIVITY_RETRY, ControlledWorkflow
from engineering_control.workflows.inputs import AuditCampaignInput

TASK_QUEUE = "engineering-control"


@activity.defn(name="execute_control_step")
async def register(payload: dict[str, Any]) -> dict[str, Any]:
    assert payload["step"] == "backlog.register_child_run"
    child_id = payload["payload"]["child_workflow_id"]
    return {
        "status": "REGISTERED",
        "child_workflow_id": child_id,
        "run_id": payload["payload"]["run_id"],
    }


@workflow.defn(name="FixtureRegisteredChild")
class Legacy(ControlledWorkflow):
    async def _step(
        self, step: str, payload: dict[str, Any], *, timeout_minutes: int = 30
    ) -> dict[str, Any]:
        result = await workflow.execute_activity(
            "execute_control_step",
            {"step": step, "payload": payload},
            task_queue=self._activity_queue(step, payload),
            start_to_close_timeout=timedelta(minutes=timeout_minutes),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=ACTIVITY_RETRY,
        )
        if not isinstance(result, dict):
            raise TypeError(f"activity {step} returned a non-object result")
        return result

    @workflow.run
    async def run(self, request: dict) -> dict:
        await self._step("backlog.register_child_run", request)
        child = await workflow.execute_child_workflow(
            "AuditCampaignWorkflow",
            AuditCampaignInput(project="falcone", campaign_id=request["campaign_id"]),
            id=request["child_workflow_id"],
            task_queue=TASK_QUEUE,
        )
        return child


@workflow.defn(name="FixtureRegisteredChild")
class Current(ControlledWorkflow):
    @workflow.run
    async def run(self, request: dict) -> dict:
        await self._step("backlog.register_child_run", request)
        child = await workflow.execute_child_workflow(
            "AuditCampaignWorkflow",
            AuditCampaignInput(project="falcone", campaign_id=request["campaign_id"]),
            id=request["child_workflow_id"],
            task_queue=TASK_QUEUE,
            **self._child_run_options(request["child_workflow_id"]),
        )
        return child


def _require_fixture_env() -> str:
    gate = os.environ.get("HERMES_TEMPORAL_FIXTURE")
    target = os.environ.get("ENGINEERING_TEST_TEMPORAL_TARGET")
    if gate is None or target is None:
        pytest.skip("isolated Temporal fixture required")
    assert gate == "network-none" and target == "127.0.0.1:7233"
    return target


async def _fetch_history(client: Client, workflow_id: str, run_id: str) -> WorkflowHistory:
    handle = client.get_workflow_handle(workflow_id, run_id=run_id)
    events = []
    async for event in handle.fetch_history_events(page_size=100, rpc_timeout=timedelta(seconds=3)):
        events.append(event)
        if len(events) > 256:
            pytest.fail("workflow history exceeded 256-event cap")
    return WorkflowHistory(workflow_id=workflow_id, events=events)


@pytest.mark.asyncio
async def test_child_run_registration_memo_replay_gate():
    target = _require_fixture_env()
    client = await asyncio.wait_for(Client.connect(target, namespace="fixture-only"), 10)

    legacy_id = f"legacy-{uuid.uuid4()}"
    legacy_child_id = f"legacy-child-{uuid.uuid4()}"
    legacy_request = {
        "campaign_id": str(uuid.uuid4()),
        "child_workflow_id": legacy_child_id,
        "run_id": str(uuid.uuid4()),
    }
    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[Legacy, FixtureAudit],
        activities=[register],
    ):
        handle = await client.start_workflow(
            Legacy.run,
            legacy_request,
            id=legacy_id,
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
            rpc_timeout=timedelta(seconds=3),
        )
        result = await asyncio.wait_for(handle.result(), 40)
        assert result["status"] == "COMPLETE"
        assert result["campaign_id"] == legacy_request["campaign_id"]
        old_history = await _fetch_history(client, handle.id, handle.first_execution_run_id)

    assert not any(
        e.HasField("marker_recorded_event_attributes")
        and e.marker_recorded_event_attributes.marker_name == "core_patch"
        for e in old_history.events
    )

    replayer_current = Replayer(workflows=[Current])
    old_child = [
        e.start_child_workflow_execution_initiated_event_attributes
        for e in old_history.events
        if e.HasField("start_child_workflow_execution_initiated_event_attributes")
    ]
    assert len(old_child) == 1 and dict(old_child[0].memo.fields) == {}
    replay = await asyncio.wait_for(
        replayer_current.replay_workflow(old_history, raise_on_replay_failure=False), 20
    )
    assert replay.replay_failure is None

    new_id = f"current-{uuid.uuid4()}"
    new_child_id = f"current-child-{uuid.uuid4()}"
    new_request = {
        "campaign_id": str(uuid.uuid4()),
        "child_workflow_id": new_child_id,
        "run_id": str(uuid.uuid4()),
    }
    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[Current, FixtureAudit],
        activities=[register],
    ):
        handle = await client.start_workflow(
            Current.run,
            new_request,
            id=new_id,
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
            rpc_timeout=timedelta(seconds=3),
        )
        result = await asyncio.wait_for(handle.result(), 40)
        assert result["status"] == "COMPLETE"
        assert result["campaign_id"] == new_request["campaign_id"]
        new_history = await _fetch_history(client, handle.id, handle.first_execution_run_id)

    marker_events = [
        e for e in new_history.events if e.HasField("marker_recorded_event_attributes")
    ]
    assert len(marker_events) >= 1

    child_start_events = [
        e
        for e in new_history.events
        if e.HasField("start_child_workflow_execution_initiated_event_attributes")
    ]
    assert len(child_start_events) == 1
    memo = child_start_events[0].start_child_workflow_execution_initiated_event_attributes.memo
    assert set(memo.fields) == {"engineering_application_run_id", "engineering_binding_version"}
    actual = {
        key: (await client.data_converter.decode([payload]))[0]
        for key, payload in memo.fields.items()
    }
    assert actual == {
        "engineering_application_run_id": new_request["run_id"],
        "engineering_binding_version": 1,
    }
    desc = await client.get_workflow_handle(new_child_id).describe(rpc_timeout=timedelta(seconds=3))
    proof = await prove_execution_start(client, "fixture-only", new_child_id, desc.run_id)
    assert proof.application_run_id == uuid.UUID(new_request["run_id"])
    assert proof.parent_workflow_id == new_id
    assert proof.parent_run_id == handle.first_execution_run_id

    replayer_new = Replayer(workflows=[Current])
    replay = await asyncio.wait_for(
        replayer_new.replay_workflow(new_history, raise_on_replay_failure=False), 20
    )
    assert replay.replay_failure is None
