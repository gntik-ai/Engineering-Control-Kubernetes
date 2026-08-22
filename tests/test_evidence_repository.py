from pathlib import Path
from uuid import uuid4

import pytest

from engineering_control.api.schemas import (
    DeploymentPrepare,
    RollbackRequest,
    RuntimeVerificationStart,
)
from engineering_control.db.base import Base
from engineering_control.db.models import Campaign, ChangeSet, Deployment, Run
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.models.core import ArtifactMetadata
from engineering_control.models.enums import Project, RedactionStatus


@pytest.mark.asyncio
async def test_sql_repository_authorizes_and_idempotently_indexes_evidence(
    tmp_path: Path,
) -> None:
    repository = SQLMetadataRepository(f"sqlite:///{tmp_path / 'metadata.sqlite'}")
    Base.metadata.create_all(repository.engine)
    campaign_id = uuid4()
    run_id = uuid4()
    workflow_id = "audit-falcone-repository"
    with repository.sessions.begin() as session:
        session.add(
            Campaign(
                id=campaign_id,
                project="falcone",
                name="evidence test",
                status="IN_PROGRESS",
                source_sha="a" * 40,
                deployment_sha="b" * 40,
                required_cells=1,
                metadata_json={},
            )
        )
        session.add(
            Run(
                id=run_id,
                campaign_id=campaign_id,
                change_set_id=None,
                workflow_id=workflow_id,
                run_type="audit-campaign",
                status="RUNNING",
                project="falcone",
            )
        )

    binding = await repository.resolve_evidence_binding(
        workflow_id,
        Project.FALCONE,
        campaign_id,
        None,
    )
    artifact_id = uuid4()
    metadata = ArtifactMetadata(
        artifact_id=artifact_id,
        project=Project.FALCONE,
        campaign_id=campaign_id,
        change_set_id=None,
        run_id=run_id,
        relative_path=f"falcone/{campaign_id}/findings/{artifact_id}-report.json",
        sha256="c" * 64,
        content_type="application/json",
        producer="scan-runner",
        redaction_status=RedactionStatus.REDACTED,
        size_bytes=12,
    )
    first = await repository.persist_artifact(metadata)
    second = await repository.persist_artifact(metadata)

    assert binding == {"run_id": run_id, "campaign_id": campaign_id, "change_set_id": None}
    assert first["id"] == str(artifact_id)
    assert second["id"] == str(artifact_id)


@pytest.mark.asyncio
async def test_deployment_run_requires_a_deployable_changeset(tmp_path: Path) -> None:
    repository = SQLMetadataRepository(f"sqlite:///{tmp_path / 'deployment.sqlite'}")
    Base.metadata.create_all(repository.engine)
    change_set_id = uuid4()
    with repository.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="musematic",
                risk_class="R2",
                state="DEPLOY_PR",
                version=1,
                source_repository="musematic",
                source_branch="agent/musematic/1/test",
                source_base_sha="a" * 40,
                source_head_sha="b" * 40,
                source_pr_url="https://github.com/gntik-ai/musematic/pull/1",
                deployment_repository="musematic-deploy",
                deployment_branch="agent/musematic/1/test",
                deployment_base_sha="c" * 40,
                deployment_head_sha="d" * 40,
                deployment_pr_url="https://github.com/gntik-ai/musematic-deploy/pull/1",
                image_digests={"api": f"sha256:{'e' * 64}"},
                chart_version=None,
                recovery_json={},
            )
        )

    project = await repository.create_deployment_run(
        "deploy-preview-test",
        DeploymentPrepare(change_set_id=change_set_id, environment="preview", dry_run=True),
    )

    assert project is Project.MUSEMATIC
    with repository.sessions() as session:
        run = session.query(Run).filter_by(workflow_id="deploy-preview-test").one()
    assert run.change_set_id == change_set_id
    assert run.run_type == "preview-deployment"

    with pytest.raises(ValueError, match="active deployment workflow"):
        await repository.create_deployment_run(
            "deploy-preview-conflict",
            DeploymentPrepare(change_set_id=change_set_id, environment="preview", dry_run=True),
        )


@pytest.mark.asyncio
async def test_runtime_verification_run_is_bound_to_deployed_artifact(tmp_path: Path) -> None:
    repository = SQLMetadataRepository(f"sqlite:///{tmp_path / 'runtime.sqlite'}")
    Base.metadata.create_all(repository.engine)
    change_set_id = uuid4()
    deployment_id = uuid4()
    with repository.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="falcone",
                risk_class="R2",
                state="STAGING_DEPLOYED",
                version=1,
                source_repository="falcone",
                source_branch="agent/falcone/1/test",
                source_base_sha="a" * 40,
                source_head_sha="b" * 40,
                source_pr_url=None,
                deployment_repository="falcone-charts",
                deployment_branch="agent/falcone/1/test",
                deployment_base_sha="c" * 40,
                deployment_head_sha="d" * 40,
                deployment_pr_url=None,
                image_digests={"api": f"sha256:{'e' * 64}"},
                chart_version="1.2.3",
                recovery_json={},
            )
        )
        session.add(
            Deployment(
                id=deployment_id,
                change_set_id=change_set_id,
                project="falcone",
                environment="staging",
                strategy="helm",
                target_context="staging",
                target_namespace="falcone-staging",
                desired_sha="d" * 40,
                status="STAGING_DEPLOYED",
                transaction_id="test",
                started_at=None,
                finished_at=None,
                recovery_json={},
            )
        )

    project = await repository.create_runtime_verification_run(
        "runtime-verify-test",
        RuntimeVerificationStart(deployment_id=deployment_id),
    )

    assert project is Project.FALCONE
    with repository.sessions() as session:
        run = session.query(Run).filter_by(workflow_id="runtime-verify-test").one()
        assert run.change_set_id == change_set_id
        assert run.summary == str(deployment_id)


@pytest.mark.asyncio
async def test_recovery_run_is_authoritatively_bound_before_workflow_start(
    tmp_path: Path,
) -> None:
    repository = SQLMetadataRepository(f"sqlite:///{tmp_path / 'recovery.sqlite'}")
    Base.metadata.create_all(repository.engine)
    change_set_id = uuid4()
    deployment_id = uuid4()
    with repository.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="musematic",
                risk_class="R3",
                state="INCIDENT_OPEN",
                version=1,
                source_repository="musematic",
                source_branch="agent/musematic/1/test",
                source_base_sha="a" * 40,
                source_head_sha="b" * 40,
                source_pr_url=None,
                deployment_repository="musematic-deploy",
                deployment_branch="agent/musematic/1/test",
                deployment_base_sha="c" * 40,
                deployment_head_sha="d" * 40,
                deployment_pr_url=None,
                image_digests={"api": f"sha256:{'e' * 64}"},
                chart_version=None,
                recovery_json={},
            )
        )
        session.add(
            Deployment(
                id=deployment_id,
                change_set_id=change_set_id,
                project="musematic",
                environment="staging",
                strategy="argocd-gitops",
                target_context="musematic-staging",
                target_namespace="musematic-staging",
                desired_sha="d" * 40,
                status="VERIFICATION_FAILED",
                transaction_id="test",
                started_at=None,
                finished_at=None,
                recovery_json={},
            )
        )

    project = await repository.create_recovery_run(
        "recovery-test",
        RollbackRequest(
            deployment_id=deployment_id,
            expected_artifact_binding_sha256="f" * 64,
            dry_run=True,
        ),
    )

    assert project is Project.MUSEMATIC
    with repository.sessions() as session:
        run = session.query(Run).filter_by(workflow_id="recovery-test").one()
        assert run.run_type == "recovery"
        assert run.change_set_id == change_set_id
        assert run.summary == str(deployment_id)
