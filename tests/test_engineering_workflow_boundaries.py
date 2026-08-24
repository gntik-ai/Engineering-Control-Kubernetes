from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db import models as db
from engineering_control.db.base import Base

ROOT = Path(__file__).parents[1]


def control_settings(tmp_path: Path) -> Settings:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'engineering.sqlite'}",
        project_config_root=ROOT / "config/projects",
        persona_config_root=ROOT / "config/personas",
        flow_config_root=ROOT / "config/flows",
        credential_root=credentials,
        worktree_root=tmp_path / "worktrees",
    )


@pytest.mark.asyncio
async def test_snapshot_persistence_feeds_bounded_enhancement_requests(
    tmp_path: Path,
) -> None:
    settings = control_settings(tmp_path)
    handler = EngineeringActivityHandler(settings)
    Base.metadata.create_all(handler.engine)
    campaign_id = uuid4()
    run_id = uuid4()
    artifact_id = uuid4()
    source_worktree = settings.worktree_root / "falcone" / str(campaign_id)
    source_worktree.mkdir(parents=True)
    with handler.sessions.begin() as session:
        session.add(
            db.Campaign(
                id=campaign_id,
                project="falcone",
                name="enhancement-source",
                status="IN_PROGRESS",
                source_sha=None,
                deployment_sha=None,
                required_cells=0,
                metadata_json={},
            )
        )
        session.add(
            db.Run(
                id=run_id,
                workflow_id=f"audit-{campaign_id}",
                run_type="audit-campaign",
                status="SUCCESS",
                project="falcone",
                campaign_id=campaign_id,
            )
        )
        session.add(
            db.Artifact(
                id=artifact_id,
                project="falcone",
                campaign_id=campaign_id,
                change_set_id=None,
                run_id=run_id,
                relative_path=f"audit/{campaign_id}/summary.json",
                sha256="e" * 64,
                content_type="application/json",
                producer="audit",
                redaction_status="REDACTED",
                size_bytes=128,
                retention_class="standard",
                purged_at=None,
                purge_reason=None,
            )
        )

    snapshot = await handler.handle(
        "project.persist_snapshot",
        {
            "campaign_id": str(campaign_id),
            "project": "falcone",
            "snapshot": {
                "source_sha": "a" * 40,
                "deployment_sha": "b" * 40,
                "source_worktree": str(source_worktree),
                "deployment_worktree": str(settings.worktree_root / "falcone-charts"),
                "source_snapshot": {},
                "deployment_snapshot": {},
            },
        },
    )
    evidence = await handler.handle(
        "enhancement.load_evidence",
        {"campaign_id": str(campaign_id), "project": "falcone"},
    )
    prepared = await handler.handle("enhancement.prepare_discovery", evidence)
    proposal = {
        "persona": "standard-user",
        "observed_problem": "The audited workflow requires repeated navigation.",
        "current_behavior": "The user repeats the same bounded navigation.",
        "evidence_handles": evidence["evidence_handles"],
        "proposed_outcome": "Offer one bounded shortcut.",
        "user_value": "Reduce repeat work.",
        "success_metric": "Reduce median steps by one.",
        "affected_repository": "gntik-ai/falcone",
        "dependencies": [],
        "security_data_implications": "No new data access.",
        "risk": "R1",
        "acceptance_criteria": ["The shortcut preserves authorization."],
        "rollout_strategy": "Feature flag for one safe tenant.",
        "kill_switch": "Disable the feature flag.",
    }
    validated = await handler.handle(
        "enhancement.validate_evidence",
        {
            **prepared,
            "agent_result": {"structured_result": {"enhancement": proposal}},
        },
    )
    deduped = await handler.handle("enhancement.dedupe", validated)

    assert snapshot["status"] == "SNAPSHOT_PERSISTED"
    assert evidence["source_sha"] == "a" * 40
    assert prepared["discovery_provider"] == "codex"
    assert prepared["discovery_request"]["writable"] is False
    assert deduped["status"] == "UNIQUE"
    assert deduped["product_review_provider"] == "claude"
    assert deduped["product_review_request"]["writable"] is False


@pytest.mark.asyncio
async def test_confirmed_deployment_finding_routes_one_issue_to_deployment_repo(
    tmp_path: Path,
) -> None:
    settings = control_settings(tmp_path)
    handler = MetadataActivityHandler(settings)
    Base.metadata.create_all(handler.engine)
    campaign_id = uuid4()
    coverage_id = uuid4()
    finding_id = uuid4()
    with handler.sessions.begin() as session:
        session.add(
            db.Campaign(
                id=campaign_id,
                project="musematic",
                name="routing",
                status="COMPLETE",
                source_sha="a" * 40,
                deployment_sha="b" * 40,
                required_cells=1,
                metadata_json={},
            )
        )
        session.add(
            db.CoverageCell(
                id=coverage_id,
                campaign_id=campaign_id,
                repository_surface="deployment",
                domain="deployment",
                persona="developer-operator",
                flow="repository-static",
                lens="correctness",
                environment="snapshot",
                status="CONFIRMED_FINDING",
                blocker=None,
                exclusion_approval_id=None,
                evidence_json=[{"reference": "render", "sha256": "c" * 64}],
                result_json={},
            )
        )
        session.add(
            db.Finding(
                id=finding_id,
                campaign_id=campaign_id,
                coverage_cell_id=coverage_id,
                fingerprint="f" * 64,
                project="musematic",
                domain="deployment",
                persona="developer-operator",
                severity="low",
                confidence=0.95,
                risk_class="R1",
                disposition="CONFIRMED",
                expected_behavior="Rendered state remains consistent.",
                actual_behavior="Rendered state differs.",
                evidence_json=[{"reference": "render", "sha256": "c" * 64}],
                minimal_reproduction=["Render the configured chart."],
                affected_source_sha="a" * 40,
                affected_deployment_sha="b" * 40,
                affected_image_digest=None,
                affected_component="helm-platform",
                impact_json={
                    "repository_surface": "deployment",
                    "flow": "repository-static",
                    "cleanup_result": "NOT_APPLICABLE_NON_MUTATING_REPOSITORY_AUDIT",
                },
                recommended_action="Align the deployment repository rendering contract.",
            )
        )

    prepared = await handler.handle("finding.prepare_publication", {"finding_id": str(finding_id)})

    assert prepared["status"] == "PUBLICATION_READY"
    assert prepared["finding_publication"]["repository"] == "gntik-ai/musematic-deploy"
    assert "gntik-ai/musematic-deploy" in prepared["finding_publication"]["body"]
