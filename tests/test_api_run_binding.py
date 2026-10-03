from uuid import UUID, uuid4

import pytest

from engineering_control.api.schemas import (
    CampaignStart,
    DeliveryReconcile,
    DeliveryStart,
    DeploymentPrepare,
    MultiProjectAuditStart,
    RollbackRequest,
    RuntimeVerificationStart,
)
from engineering_control.api.service import TemporalControlService
from engineering_control.db.models import (
    Base,
    Campaign,
    ChangeSet,
    CoverageCell,
    Deployment,
    Finding,
)
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.models.enums import Project, RiskClass

CANONICAL_RUN_ID = "11111111-1111-1111-1111-111111111111"


class FakeHandle:
    def __init__(self):
        self.first_execution_run_id = CANONICAL_RUN_ID


class FakeClient:
    def __init__(self):
        self.calls = []

    async def start_workflow(
        self, workflow_name, workflow_input, *, id, task_queue, rpc_timeout, id_reuse_policy, memo
    ):
        self.calls.append(
            dict(
                workflow_name=workflow_name,
                id=id,
                task_queue=task_queue,
                id_reuse_policy=id_reuse_policy,
                memo=memo,
                rpc_timeout=rpc_timeout,
            )
        )
        return FakeHandle()


@pytest.fixture
def repository(tmp_path):
    value = SQLMetadataRepository(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(value.engine)
    try:
        yield value
    finally:
        value.engine.dispose()


@pytest.fixture
def sessions(repository):
    return repository.sessions


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def service(client, repository):
    return TemporalControlService(client, "test-queue", repository)


def _seed(
    sessions, *, cc_status="CANDIDATE", cs_state="CANDIDATE_BUILT", dep_status="STAGING_DEPLOYED"
):
    campaign_id, cell_id, finding_id, change_set_id, deployment_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    with sessions.begin() as session:
        session.add(
            Campaign(
                id=campaign_id,
                project=Project.FALCONE.value,
                name="c",
                status="RUNNING",
            )
        )
        session.add(
            CoverageCell(
                id=cell_id,
                campaign_id=campaign_id,
                domain="d",
                persona="p",
                lens="l",
                environment="staging",
                status="DONE",
            )
        )
        session.add(
            Finding(
                id=finding_id,
                campaign_id=campaign_id,
                coverage_cell_id=cell_id,
                fingerprint=str(uuid4()),
                project=Project.FALCONE.value,
                domain="d",
                persona="p",
                severity="HIGH",
                confidence=0.9,
                risk_class=RiskClass.R1.value,
                disposition=cc_status,
                expected_behavior="e",
                actual_behavior="a",
                minimal_reproduction=[],
                affected_source_sha="a" * 40,
                affected_component="comp",
                recommended_action="fix",
            )
        )
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=finding_id,
                project=Project.FALCONE.value,
                risk_class=RiskClass.R1.value,
                state=cs_state,
                source_repository="r",
                source_base_sha="a" * 40,
                deployment_repository="r",
                deployment_base_sha="a" * 40,
            )
        )
        session.add(
            Deployment(
                id=deployment_id,
                change_set_id=change_set_id,
                project=Project.FALCONE.value,
                environment="staging",
                strategy="blue-green",
                target_context="ctx",
                target_namespace="ns",
                desired_sha="a" * 40,
                status=dep_status,
            )
        )
    return campaign_id, cell_id, finding_id, change_set_id, deployment_id


async def _run_multi(service, ids):
    return await service.start_all_project_audits(
        MultiProjectAuditStart(preset="static-full", environment="staging")
    )


async def _run_campaign(service, ids):
    return await service.start_campaign(
        CampaignStart(project=Project.FALCONE, preset="static-full", environment="staging")
    )


async def _run_finding_verification(service, ids):
    _, _, finding_id, _, _ = ids
    return await service.start_finding_verification(finding_id)


async def _run_delivery(service, ids):
    _, _, finding_id, _, _ = ids
    return await service.start_delivery(
        DeliveryStart(finding_id=finding_id, risk_class=RiskClass.R1, dry_run=True)
    )


async def _run_reconcile(service, ids):
    _, _, _, change_set_id, _ = ids
    return await service.reconcile_delivery(
        DeliveryReconcile(change_set_id=change_set_id, dry_run=True)
    )


async def _run_preview(service, ids):
    _, _, _, change_set_id, _ = ids
    return await service.prepare_deployment(
        DeploymentPrepare(change_set_id=change_set_id, environment="preview", dry_run=True)
    )


async def _run_staging(service, ids):
    _, _, _, change_set_id, _ = ids
    return await service.prepare_deployment(
        DeploymentPrepare(change_set_id=change_set_id, environment="staging", dry_run=True)
    )


async def _run_rollback(service, ids):
    _, _, _, _, deployment_id = ids
    return await service.rollback(
        RollbackRequest(
            deployment_id=deployment_id, expected_artifact_binding_sha256="a" * 64, dry_run=True
        )
    )


async def _run_runtime_verification(service, ids):
    _, _, _, _, deployment_id = ids
    return await service.start_runtime_verification(
        RuntimeVerificationStart(deployment_id=deployment_id)
    )


@pytest.mark.parametrize(
    "runner",
    [
        _run_campaign,
        _run_multi,
        _run_finding_verification,
        _run_delivery,
        _run_reconcile,
        _run_preview,
        _run_staging,
        _run_rollback,
        _run_runtime_verification,
    ],
)
@pytest.mark.asyncio
async def test_start_recorded_workflow_binds_run(service, client, sessions, runner):
    ids = _seed(
        sessions,
        cc_status="CONFIRMED" if runner is _run_delivery else "CANDIDATE",
        cs_state="DEPLOY_PR" if runner is _run_staging else "CANDIDATE_BUILT",
    )

    result = await runner(service, ids)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["task_queue"] == "test-queue"
    assert call["id_reuse_policy"].name == "REJECT_DUPLICATE"
    assert call["rpc_timeout"].total_seconds() == 5
    assert call["memo"]["engineering_binding_version"] == 1
    assert result.run_id == CANONICAL_RUN_ID and result.workflow_id == call["id"]

    application_run_id = UUID(call["memo"]["engineering_application_run_id"])

    with sessions() as session:
        from engineering_control.db.models import Run

        run = session.get(Run, application_run_id)
        assert run is not None
        assert run.id == application_run_id
        assert run.workflow_id == call["id"]
        assert run.temporal_first_execution_run_id == CANONICAL_RUN_ID
        assert run.status == "QUEUED"

        if runner is _run_multi:
            children = session.query(Run).filter(Run.run_type == "audit-campaign").all()
            assert len(children) == 3
            for child in children:
                assert child.temporal_first_execution_run_id is None
