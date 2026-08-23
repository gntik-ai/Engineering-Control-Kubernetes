from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import Campaign, CoverageCell, Finding, Run
from engineering_control.db.repository import SQLMetadataRepository


@pytest.mark.asyncio
async def test_finding_verification_start_is_bounded_and_retryable(tmp_path: Path) -> None:
    database = tmp_path / "verification-start.sqlite"
    repository = SQLMetadataRepository(f"sqlite:///{database}")
    Base.metadata.create_all(repository.engine)
    campaign_id = uuid4()
    coverage_id = uuid4()
    finding_id = uuid4()
    with repository.sessions.begin() as session:
        session.add(
            Campaign(
                id=campaign_id,
                project="falcone",
                name="verification fixture",
                status="VERIFICATION_REQUIRED",
                source_sha="a" * 40,
                deployment_sha="b" * 40,
                required_cells=1,
                metadata_json={},
            )
        )
        session.add(
            CoverageCell(
                id=coverage_id,
                campaign_id=campaign_id,
                domain="gateway",
                persona="tenant-admin",
                lens="correctness",
                environment="snapshot",
                status="CANDIDATE_FOUND",
                blocker=None,
                exclusion_approval_id=None,
                evidence_json=[{"sha256": "c" * 64}],
                result_json={"provider": "claude"},
            )
        )
        session.add(
            Finding(
                id=finding_id,
                campaign_id=campaign_id,
                coverage_cell_id=coverage_id,
                fingerprint="d" * 64,
                project="falcone",
                domain="gateway",
                persona="tenant-admin",
                severity="medium",
                confidence=0.9,
                risk_class="R2",
                disposition="CANDIDATE",
                expected_behavior="expected",
                actual_behavior="actual",
                evidence_json=[{"sha256": "c" * 64}],
                minimal_reproduction=["bounded reproduction"],
                affected_source_sha="a" * 40,
                affected_deployment_sha="b" * 40,
                affected_image_digest=None,
                affected_component="gateway",
                impact_json={},
                recommended_action="verify",
            )
        )

    first_workflow = f"finding-verify-{finding_id}-{uuid4()}"
    project = await repository.create_finding_verification_run(first_workflow, finding_id)
    assert project.value == "falcone"
    with pytest.raises(ValueError, match="active verification"):
        await repository.create_finding_verification_run(
            f"finding-verify-{finding_id}-{uuid4()}", finding_id
        )

    with repository.sessions.begin() as session:
        first = session.scalar(select(Run).where(Run.workflow_id == first_workflow))
        assert first is not None
        assert first.campaign_id == campaign_id
        assert first.summary == str(finding_id)
        first.status = "BLOCKED"
        finding = session.get(Finding, finding_id)
        assert finding is not None
        finding.disposition = "BLOCKED"

    retry_workflow = f"finding-verify-{finding_id}-{uuid4()}"
    await repository.create_finding_verification_run(retry_workflow, finding_id)
    handler = MetadataActivityHandler(Settings(database_url=f"sqlite:///{database}"))
    loaded = await handler.handle(
        "finding.load_candidate",
        {
            "finding_id": str(finding_id),
            "project": "falcone",
            "orchestration": {"workflow_id": retry_workflow},
        },
    )
    terminal = await handler.handle(
        "finding.record_terminal",
        {
            "finding": loaded,
            "status": "RATE_LIMITED",
            "routing_reason": "codex:provider_rate_limited;claude:provider_rate_limited",
            "orchestration": {"workflow_id": retry_workflow},
        },
    )
    assert terminal["disposition"] == "BLOCKED"
    assert terminal["status"] == "RATE_LIMITED"
    with repository.sessions() as session:
        retries = session.scalars(select(Run).where(Run.run_type == "finding-verification")).all()
    assert len(retries) == 2
    retry = next(run for run in retries if run.workflow_id == retry_workflow)
    assert retry.status == "RATE_LIMITED"
    assert retry.finished_at is None
