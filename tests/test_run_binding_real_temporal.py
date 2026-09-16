import asyncio
from datetime import timedelta
from uuid import UUID, uuid4, uuid5

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    import os

    import pytest
    from temporalio.client import Client, WorkflowHistory
    from temporalio.worker import Replayer, Worker

    from engineering_control.db.start_proof import prove_execution_start


from engineering_control.workflows.definitions import MultiProjectAuditWorkflow
from engineering_control.workflows.inputs import AuditCampaignInput, MultiProjectAuditInput

TASK_QUEUE = "engineering-control"
PROJECTS = ["falcone", "musematic", "llmwiki"]
NAMESPACE = "fixture-only"


@workflow.defn(name="AuditCampaignWorkflow")
class FixtureAudit:
    @workflow.run
    async def run(self, request: AuditCampaignInput) -> dict:
        return {"status": "COMPLETE", "campaign_id": request.campaign_id}


@workflow.defn(name="FixtureContinue")
class FixtureContinue:
    @workflow.run
    async def run(self, remaining: int) -> str:
        if remaining > 0:
            workflow.continue_as_new(remaining - 1)
        return "COMPLETED"


@activity.defn(name="execute_control_step")
async def stub_activity(payload: dict) -> dict:
    assert payload["step"] == "campaign.group_status"
    return {}


def _build_input(application_run_id):
    return MultiProjectAuditInput(
        preset="snapshot",
        campaign_ids={p: str(uuid4()) for p in PROJECTS},
        child_workflow_ids={p: f"fixture-{p}-{uuid4()}" for p in PROJECTS},
        application_run_id=application_run_id,
    )


@pytest.mark.asyncio
async def test_multi_project_audit_binding_and_replay():
    gate = os.environ.get("HERMES_TEMPORAL_FIXTURE")
    target = os.environ.get("ENGINEERING_TEST_TEMPORAL_TARGET")
    if gate is None or target is None:
        pytest.skip("isolated Temporal fixture required")
    assert gate == "network-none" and target == "127.0.0.1:7233"

    client = await asyncio.wait_for(Client.connect(target, namespace=NAMESPACE), 10)

    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[MultiProjectAuditWorkflow, FixtureAudit, FixtureContinue],
        activities=[stub_activity],
    ):
        for legacy in (True, False):
            parent_id = f"fixture-parent-{uuid4()}"
            parent_uuid = uuid4()
            app_run_id = None if legacy else str(parent_uuid)
            request = _build_input(app_run_id)
            memo = (
                {}
                if legacy
                else {
                    "engineering_application_run_id": str(parent_uuid),
                    "engineering_binding_version": 1,
                }
            )

            handle = await client.start_workflow(
                MultiProjectAuditWorkflow.run,
                request,
                id=parent_id,
                task_queue=TASK_QUEUE,
                execution_timeout=timedelta(seconds=30),
                rpc_timeout=timedelta(seconds=3),
                memo=memo,
            )
            first_run_id = handle.first_execution_run_id
            assert str(UUID(first_run_id)) == first_run_id
            result = await asyncio.wait_for(handle.result(), 40)
            assert result["status"] == "SUCCESS"

            events = []
            async for event in handle.fetch_history_events(
                page_size=100, rpc_timeout=timedelta(seconds=3)
            ):
                events.append(event)
                assert len(events) <= 256, "history exceeded fixture bound"
            history = WorkflowHistory(parent_id, events)

            initiations = [
                e
                for e in events
                if e.HasField("start_child_workflow_execution_initiated_event_attributes")
            ]
            assert len(initiations) == 3

            for event in initiations:
                attrs = event.start_child_workflow_execution_initiated_event_attributes
                project = next(
                    p for p, cid in request.child_workflow_ids.items() if cid == attrs.workflow_id
                )
                if legacy:
                    assert dict(attrs.memo.fields) == {}
                else:
                    assert set(attrs.memo.fields) == {
                        "engineering_application_run_id",
                        "engineering_binding_version",
                    }
                    expected_app = str(uuid5(parent_uuid, project))
                    dec_app = (
                        await client.data_converter.decode(
                            [attrs.memo.fields["engineering_application_run_id"]]
                        )
                    )[0]
                    dec_ver = (
                        await client.data_converter.decode(
                            [attrs.memo.fields["engineering_binding_version"]]
                        )
                    )[0]
                    assert dec_app == expected_app
                    assert type(dec_ver) is int and dec_ver == 1

                desc = await client.get_workflow_handle(attrs.workflow_id).describe(
                    rpc_timeout=timedelta(seconds=3)
                )
                proof = await prove_execution_start(
                    client, NAMESPACE, attrs.workflow_id, desc.run_id
                )
                assert proof.workflow_id == attrs.workflow_id
                assert proof.current_run_id == desc.run_id
                assert proof.first_run_id == desc.run_id
                assert proof.workflow_type == "AuditCampaignWorkflow"
                assert proof.parent_workflow_id == parent_id
                assert proof.parent_run_id == first_run_id
                assert proof.application_run_id == (None if legacy else UUID(expected_app))

            replayer = Replayer(workflows=[MultiProjectAuditWorkflow])
            replay_result = await asyncio.wait_for(
                replayer.replay_workflow(history, raise_on_replay_failure=False), 20
            )
            assert replay_result.replay_failure is None

        continue_id = f"fixture-continue-{uuid4()}"
        continue_memo = {
            "engineering_application_run_id": str(uuid4()),
            "engineering_binding_version": 1,
        }
        continue_handle = await client.start_workflow(
            FixtureContinue.run,
            2,
            id=continue_id,
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=30),
            rpc_timeout=timedelta(seconds=3),
            memo=continue_memo,
        )
        continue_result = await asyncio.wait_for(continue_handle.result(), 40)
        assert continue_result == "COMPLETED"

        latest_desc = await client.get_workflow_handle(continue_id).describe(
            rpc_timeout=timedelta(seconds=3)
        )
        assert latest_desc.run_id != continue_handle.first_execution_run_id

        final_proof = await prove_execution_start(
            client, NAMESPACE, continue_id, latest_desc.run_id
        )
        assert final_proof.first_run_id == continue_handle.first_execution_run_id
        assert final_proof.application_run_id == UUID(
            continue_memo["engineering_application_run_id"]
        )
