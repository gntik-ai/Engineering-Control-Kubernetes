from pathlib import Path
from uuid import uuid4

import pytest

from engineering_control.db.base import Base
from engineering_control.db.models import Campaign, Run
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
