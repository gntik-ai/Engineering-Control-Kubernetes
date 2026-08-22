from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import Campaign, Finding, QuotaSnapshot


@pytest.mark.asyncio
async def test_quota_persistence_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "metadata.sqlite"
    handler = MetadataActivityHandler(Settings(database_url=f"sqlite:///{database}"))
    Base.metadata.create_all(handler.engine)
    captured_at = datetime.now(UTC).isoformat()
    payload = {
        "snapshots": [
            {
                "provider": "codex",
                "captured_at": captured_at,
                "remaining_capacity": None,
                "total_capacity": None,
                "reset_at": None,
                "running_jobs": 0,
                "rate_limited": False,
                "source": "native_cli_auth_status;capacity_not_exposed",
                "authenticated": False,
            }
        ]
    }

    first = await handler.handle("quota.persist", payload)
    second = await handler.handle("quota.persist", payload)

    assert first["status"] == "PERSISTED"
    assert second["persisted_snapshots"] == 1
    with handler.sessions() as session:
        count = session.scalar(select(func.count()).select_from(QuotaSnapshot))
    assert count == 1


def add_campaign(handler: MetadataActivityHandler, project: str = "falcone") -> Campaign:
    campaign = Campaign(
        id=uuid4(),
        project=project,
        name="bounded audit",
        status="QUEUED",
        source_sha="a" * 40,
        deployment_sha="b" * 40,
        required_cells=1,
        metadata_json={"dry_run": True},
    )
    with handler.sessions.begin() as session:
        session.add(campaign)
    return campaign


def audit_cell() -> dict[str, str]:
    return {
        "domain": "gateway",
        "persona": "tenant-admin",
        "lens": "correctness",
        "environment": "snapshot",
    }


@pytest.mark.asyncio
async def test_campaign_coverage_and_no_finding_require_evidence(tmp_path: Path) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'campaign.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    campaign = add_campaign(handler)
    cell = audit_cell()
    await handler.handle(
        "campaign.persist_coverage",
        {"campaign_id": str(campaign.id), "cells": [cell], "total": 1},
    )
    result = await handler.handle(
        "campaign.persist_cell_result",
        {
            "campaign_id": str(campaign.id),
            "cell": cell,
            "deterministic_evidence_sha256": "c" * 64,
            "agent_result": {
                "provider": "codex",
                "run_id": str(uuid4()),
                "structured_result": {
                    "summary": "contract matches the immutable snapshot",
                    "decision": "PASS",
                    "evidence": [
                        {
                            "kind": "git-snapshot",
                            "reference": "source:gateway",
                            "sha256": "c" * 64,
                        }
                    ],
                    "open_questions": [],
                    "finding": None,
                },
            },
        },
    )
    final = await handler.handle(
        "campaign.finalize",
        {"campaign_id": str(campaign.id), "expected_cells": 1},
    )

    assert result["status"] == "NO_FINDING"
    assert final["status"] == "COMPLETE"


@pytest.mark.asyncio
async def test_audit_cell_preparation_is_bounded_and_schema_driven(tmp_path: Path) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'prepare.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    campaign = add_campaign(handler)
    prepared = await handler.handle(
        "audit.prepare_agent_cell",
        {
            "campaign_id": str(campaign.id),
            "cell": audit_cell(),
            "snapshot": {
                "project": "falcone",
                "source_sha": "a" * 40,
                "deployment_sha": "b" * 40,
                "source_worktree": str(tmp_path / "source"),
                "deployment_worktree": str(tmp_path / "deployment"),
            },
            "deterministic": {"evidence_sha256": "c" * 64},
        },
    )

    request = prepared["agent_request"]
    assert prepared["status"] == "READY"
    assert prepared["provider"] == "codex"
    assert request["writable"] is False
    assert request["output_schema"]["properties"]["evidence"]["minItems"] == 1


@pytest.mark.asyncio
async def test_failed_audit_cell_creates_candidate_not_confirmed_finding(tmp_path: Path) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'finding.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    campaign = add_campaign(handler)
    cell = audit_cell()
    await handler.handle(
        "campaign.persist_coverage",
        {"campaign_id": str(campaign.id), "cells": [cell], "total": 1},
    )
    result = await handler.handle(
        "campaign.persist_cell_result",
        {
            "campaign_id": str(campaign.id),
            "cell": cell,
            "deterministic_evidence_sha256": "d" * 64,
            "agent_result": {
                "provider": "claude",
                "run_id": str(uuid4()),
                "structured_result": {
                    "summary": "bounded mismatch",
                    "decision": "FAIL",
                    "evidence": [
                        {
                            "kind": "source",
                            "reference": "gateway/contract",
                            "sha256": "d" * 64,
                        }
                    ],
                    "open_questions": [],
                    "finding": {
                        "severity": "medium",
                        "confidence": 0.9,
                        "expected_behavior": "request follows the declared contract",
                        "actual_behavior": "request diverges from the declared contract",
                        "minimal_reproduction": ["inspect the bounded fixture"],
                        "affected_component": "gateway",
                        "security_impact": None,
                        "data_impact": None,
                        "recommended_action": "independently reproduce before filing",
                    },
                },
            },
        },
    )
    final = await handler.handle(
        "campaign.finalize",
        {"campaign_id": str(campaign.id), "expected_cells": 1},
    )

    with handler.sessions() as session:
        finding = session.scalar(select(Finding))
    assert finding is not None and finding.disposition == "CANDIDATE"
    assert result["finding_id"] == str(finding.id)
    assert final["status"] == "VERIFICATION_REQUIRED"
