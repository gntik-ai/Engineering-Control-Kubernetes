from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from jsonschema.validators import Draft202012Validator
from sqlalchemy import func, select
from temporalio.exceptions import ApplicationError

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import (
    Approval,
    ApprovalCandidate,
    Campaign,
    ChangeSet,
    ComponentLease,
    CoverageCell,
    DashboardEvent,
    Deployment,
    Finding,
    FindingVerification,
    Incident,
    Issue,
    NotificationOutbox,
    QuotaSnapshot,
    Run,
    WorktreeLease,
)
from engineering_control.models.enums import Project
from engineering_control.policy.approvals import ApprovalService


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
                "cli_present": True,
                "status": "MANUAL_AUTH_REQUIRED_CODEX",
            }
        ]
    }

    first = await handler.handle("quota.persist", payload)
    second = await handler.handle("quota.persist", payload)

    assert first["status"] == "PERSISTED"
    assert second["persisted_snapshots"] == 1
    with handler.sessions() as session:
        count = session.scalar(select(func.count()).select_from(QuotaSnapshot))
        snapshot = session.scalar(select(QuotaSnapshot))
    assert count == 1
    assert snapshot is not None
    assert snapshot.authenticated is False
    assert snapshot.cli_present is True
    assert snapshot.status == "MANUAL_AUTH_REQUIRED_CODEX"


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


def add_changeset_finding(handler: MetadataActivityHandler, change_set_id: UUID) -> None:
    """Give standalone staging fixtures the real finding their ChangeSet references."""
    with handler.sessions() as session:
        change_set = session.get(ChangeSet, change_set_id)
        assert change_set is not None
        project = change_set.project
    campaign = add_campaign(handler, project)
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, change_set_id)
        assert change_set is not None
        coverage = CoverageCell(
            id=uuid4(),
            campaign_id=campaign.id,
            domain="api",
            persona="operator",
            lens="correctness",
            environment="staging",
            status="CONFIRMED_FINDING",
            evidence_json=[],
            result_json={},
        )
        session.add(coverage)
        session.flush()
        session.add(
            Finding(
                id=change_set.finding_id,
                campaign_id=campaign.id,
                coverage_cell_id=coverage.id,
                fingerprint=uuid4().hex * 2,
                project=project,
                domain="api",
                persona="operator",
                severity="medium",
                confidence=0.9,
                risk_class=change_set.risk_class,
                disposition="CONFIRMED",
                expected_behavior="fixture contract",
                actual_behavior="fixture mismatch",
                evidence_json=[],
                minimal_reproduction=["fixture only"],
                affected_source_sha=change_set.source_base_sha,
                affected_deployment_sha=change_set.deployment_base_sha,
                affected_component="api",
                impact_json={},
                recommended_action="fixture repair",
            )
        )


@pytest.mark.asyncio
async def test_multi_project_audit_status_is_bounded_idempotent_and_observable(
    tmp_path: Path,
) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'multi-project.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    workflow_id = "audit-all-11111111-1111-4111-8111-111111111111"
    with handler.sessions.begin() as session:
        session.add(
            Run(
                workflow_id=workflow_id,
                run_type="multi-project-audit",
                status="QUEUED",
                project="platform",
                worker="controller",
            )
        )
    running = {
        "workflow_id": workflow_id,
        "orchestration": {
            "workflow_id": workflow_id,
            "workflow_run_id": "synthetic-run",
            "activity_run_id": "synthetic-activity-run",
            "activity_id": "synthetic-activity",
            "attempt": 1,
            "task_queue": "engineering-control",
        },
        "status": "RUNNING",
        "preset": "snapshot",
        "environment": "staging",
        "current_project": "falcone",
        "project_results": {
            "falcone": {
                "status": "COMPLETE",
                "campaign_id": "11111111-1111-4111-8111-111111111111",
                "workflow_id": f"{workflow_id}-falcone",
                "blocker": None,
            }
        },
    }
    await handler.handle("campaign.group_status", running)
    await handler.handle("campaign.group_status", running)
    terminal = await handler.handle(
        "campaign.group_status",
        {**running, "status": "BLOCKED"},
    )

    with handler.sessions() as session:
        run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
        events = session.scalars(
            select(DashboardEvent).where(DashboardEvent.workflow_id == workflow_id)
        ).all()
    assert terminal["status"] == "BLOCKED"
    assert run is not None and run.status == "BLOCKED"
    assert run.started_at is not None and run.finished_at is not None
    assert run.error_code == "BLOCKED"
    assert len(events) == 2
    assert {event.event_type for event in events} == {"workflow.progress", "workflow.failed"}


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
async def test_campaign_provider_blocker_is_bound_to_deterministic_evidence(
    tmp_path: Path,
) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'blocked-campaign.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    campaign = add_campaign(handler)
    cell = audit_cell()
    await handler.handle(
        "campaign.persist_coverage",
        {"campaign_id": str(campaign.id), "cells": [cell], "total": 1},
    )

    result = await handler.handle(
        "campaign.persist_cell_blocker",
        {
            "campaign_id": str(campaign.id),
            "cell": cell,
            "provider": "codex",
            "status": "BLOCKED",
            "blocker": "MANUAL_AUTH_REQUIRED_CODEX",
            "deterministic_evidence_sha256": "e" * 64,
        },
    )
    final = await handler.handle(
        "campaign.finalize",
        {"campaign_id": str(campaign.id), "expected_cells": 1},
    )

    with handler.sessions() as session:
        coverage = session.scalar(select(CoverageCell))
    assert coverage is not None
    assert coverage.status == "BLOCKED"
    assert coverage.blocker == "MANUAL_AUTH_REQUIRED_CODEX"
    assert coverage.evidence_json[0]["sha256"] == "e" * 64
    assert result["coverage_status"] == "BLOCKED"
    assert final["status"] == "COMPLETE"


@pytest.mark.asyncio
async def test_campaign_snapshot_failure_persists_every_blocked_cell_and_finishes_run(
    tmp_path: Path,
) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'snapshot-blocked.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    campaign = add_campaign(handler, project="musematic")
    campaign.required_cells = 2
    workflow_id = f"audit-musematic-{campaign.id}"
    with handler.sessions.begin() as session:
        stored = session.get(Campaign, campaign.id)
        assert stored is not None
        stored.required_cells = 2
        session.add(
            Run(
                id=uuid4(),
                campaign_id=campaign.id,
                workflow_id=workflow_id,
                run_type="audit-campaign",
                status="QUEUED",
                project="musematic",
            )
        )
    cells = [
        audit_cell(),
        {**audit_cell(), "lens": "security"},
    ]
    for cell in cells:
        persisted = await handler.handle(
            "campaign.persist_snapshot_blockers",
            {
                "campaign_id": str(campaign.id),
                "cells": [cell],
                "total": 2,
                "blocker": "PROJECT_SNAPSHOT_UNAVAILABLE",
            },
        )
        assert persisted["status"] == "BLOCKED_COVERAGE_PERSISTED"
    final = await handler.handle(
        "campaign.finalize",
        {"campaign_id": str(campaign.id), "expected_cells": 2},
    )

    with handler.sessions() as session:
        coverage = session.scalars(select(CoverageCell).order_by(CoverageCell.lens)).all()
        stored_campaign = session.get(Campaign, campaign.id)
        run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
    assert len(coverage) == 2
    assert all(cell.status == "BLOCKED" for cell in coverage)
    assert all(cell.blocker == "PROJECT_SNAPSHOT_UNAVAILABLE" for cell in coverage)
    assert all(cell.evidence_json[0]["kind"] == "snapshot-blocker" for cell in coverage)
    assert all(len(cell.evidence_json[0]["sha256"]) == 64 for cell in coverage)
    assert len({cell.evidence_json[0]["sha256"] for cell in coverage}) == 2
    root_references = [cell.result_json["root_blocker"] for cell in coverage]
    assert len({item["root_blocker_id"] for item in root_references}) == 1
    assert all(item["code"] == "PROJECT_SNAPSHOT_UNAVAILABLE" for item in root_references)
    assert all(item["classification"] == "EXTERNAL_DEPENDENCY_FAILURE" for item in root_references)
    assert all(item["dependent_cell_count"] == 2 for item in root_references)
    roots = stored_campaign.metadata_json["root_blockers"]  # type: ignore[union-attr]
    assert len(roots) == 1
    assert next(iter(roots.values()))["dependent_cell_count"] == 2
    assert stored_campaign is not None and stored_campaign.status == "COMPLETE"
    assert run is not None and run.status == "SUCCESS" and run.finished_at is not None
    assert final["status"] == "COMPLETE"


@pytest.mark.asyncio
async def test_root_blocker_classification_cannot_be_overridden_by_a_payload(
    tmp_path: Path,
) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'root-classification.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    campaign = add_campaign(handler)
    cell = audit_cell()
    await handler.handle(
        "campaign.persist_coverage",
        {"campaign_id": str(campaign.id), "cells": [cell], "total": 1},
    )

    with pytest.raises(ApplicationError) as captured:
        await handler.handle(
            "campaign.persist_cell_blocker",
            {
                "campaign_id": str(campaign.id),
                "cell": cell,
                "status": "BLOCKED",
                "blocker": "PERSONA_CREDENTIAL_UNAVAILABLE",
                "root_blocker_classification": "PRODUCT_DEFECT",
                "deterministic_evidence_sha256": "a" * 64,
            },
        )
    assert captured.value.type == "PolicyViolation"
    assert isinstance(captured.value.__cause__, ValueError)
    assert "authoritative code" in str(captured.value.__cause__)


@pytest.mark.asyncio
async def test_root_blocker_reconciliation_replaces_stale_roots_and_exactly_counts_cells(
    tmp_path: Path,
) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'root-reconciliation.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    campaign = add_campaign(handler)
    cell = audit_cell()
    await handler.handle(
        "campaign.persist_coverage",
        {"campaign_id": str(campaign.id), "cells": [cell], "total": 1},
    )
    base = {
        "campaign_id": str(campaign.id),
        "cell": cell,
        "status": "BLOCKED",
        "deterministic_evidence_sha256": "b" * 64,
    }

    first = await handler.handle(
        "campaign.persist_cell_blocker",
        {**base, "blocker": "PERSONA_CREDENTIAL_UNAVAILABLE"},
    )
    repeated = await handler.handle(
        "campaign.persist_cell_blocker",
        {**base, "blocker": "PERSONA_CREDENTIAL_UNAVAILABLE"},
    )
    replacement = await handler.handle(
        "campaign.persist_cell_blocker",
        {**base, "blocker": "BLOCKED_BY_RUNTIME_HEALTH"},
    )

    assert first["root_blocker"]["dependent_cell_count"] == 1
    assert repeated["root_blocker"]["dependent_cell_count"] == 1
    assert replacement["root_blocker"]["dependent_cell_count"] == 1
    assert replacement["root_blocker"]["classification"] == "DEPLOYMENT_DEFECT"
    with handler.sessions() as session:
        stored = session.get(Campaign, campaign.id)
        coverage = session.scalar(select(CoverageCell))
    assert stored is not None
    roots = stored.metadata_json["root_blockers"]
    assert len(roots) == 1
    root = next(iter(roots.values()))
    assert root["code"] == "BLOCKED_BY_RUNTIME_HEALTH"
    assert root["dependent_cell_count"] == 1
    assert coverage is not None
    assert coverage.result_json["root_blocker"] == root


@pytest.mark.asyncio
async def test_nonterminal_dry_run_campaign_can_be_superseded_with_recovery_evidence(
    tmp_path: Path,
) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'superseded-campaign.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    campaign = add_campaign(handler)
    workflow_id = f"audit-falcone-{campaign.id}"
    with handler.sessions.begin() as session:
        session.add(
            Run(
                id=uuid4(),
                campaign_id=campaign.id,
                workflow_id=workflow_id,
                run_type="audit-campaign",
                status="RUNNING",
                project="falcone",
            )
        )

    persisted = await handler.handle(
        "campaign.persist_snapshot_blockers",
        {
            "campaign_id": str(campaign.id),
            "cells": [audit_cell()],
            "total": 1,
            "blocker": "BOOTSTRAP_VALIDATION_SUPERSEDED",
        },
    )
    final = await handler.handle(
        "campaign.finalize",
        {
            "campaign_id": str(campaign.id),
            "expected_cells": 1,
            "terminal_blocker": "BOOTSTRAP_VALIDATION_SUPERSEDED",
        },
    )

    with handler.sessions() as session:
        coverage = session.scalar(select(CoverageCell))
        run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
        stored_campaign = session.get(Campaign, campaign.id)
    assert persisted["status"] == "BLOCKED_COVERAGE_PERSISTED"
    assert coverage is not None
    assert coverage.status == "BLOCKED"
    assert coverage.blocker == "BOOTSTRAP_VALIDATION_SUPERSEDED"
    assert coverage.evidence_json[0]["kind"] == "recovery-blocker"
    assert len(coverage.evidence_json[0]["sha256"]) == 64
    assert stored_campaign is not None and stored_campaign.status == "COMPLETE"
    assert run is not None and run.status == "CANCELLED"
    assert run.error_code == "BOOTSTRAP_VALIDATION_SUPERSEDED"
    assert final["status"] == "COMPLETE"


@pytest.mark.asyncio
async def test_live_campaign_cannot_use_bootstrap_supersession_path(tmp_path: Path) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'live-campaign.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    campaign = add_campaign(handler)
    with handler.sessions.begin() as session:
        stored = session.get(Campaign, campaign.id)
        assert stored is not None
        stored.metadata_json = {"dry_run": False}

    with pytest.raises(ApplicationError, match="invalid metadata activity input"):
        await handler.handle(
            "campaign.persist_snapshot_blockers",
            {
                "campaign_id": str(campaign.id),
                "cells": [audit_cell()],
                "total": 1,
                "blocker": "BOOTSTRAP_VALIDATION_SUPERSEDED",
            },
        )


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
            "orchestration": {"workflow_id": f"audit-falcone-{campaign.id}"},
        },
    )

    request = prepared["agent_request"]
    assert prepared["status"] == "READY"
    assert prepared["provider"] == "codex"
    assert request["writable"] is False
    assert request["output_schema"]["properties"]["evidence"]["minItems"] == 1
    schema = request["output_schema"]
    Draft202012Validator.check_schema(schema)
    unsupported = {"allOf", "if", "then"}

    def keywords(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set(value).intersection(unsupported) | set().union(
                *(keywords(item) for item in value.values())
            )
        if isinstance(value, list):
            return set().union(*(keywords(item) for item in value))
        return set()

    assert keywords(schema) == set()
    finding = schema["properties"]["finding"]
    assert finding["type"] == ["object", "null"]
    assert set(finding["properties"]) == set(finding["required"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "finding"),
    [
        ("PASS", {"unexpected": "finding"}),
        ("FAIL", None),
        ("BLOCKED", {"unexpected": "finding"}),
        ("REQUEST_CHANGES", {"unexpected": "finding"}),
    ],
)
async def test_audit_result_decision_and_finding_must_be_consistent(
    tmp_path: Path, decision: str, finding: object
) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / f'audit-{decision}.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    campaign = add_campaign(handler)
    cell = audit_cell()
    await handler.handle(
        "campaign.persist_coverage",
        {"campaign_id": str(campaign.id), "cells": [cell], "total": 1},
    )

    with pytest.raises(ApplicationError, match="invalid metadata activity input") as caught:
        await handler.handle(
            "campaign.persist_cell_result",
            {
                "campaign_id": str(campaign.id),
                "cell": cell,
                "deterministic_evidence_sha256": "c" * 64,
                "agent_result": {
                    "provider": "codex",
                    "run_id": str(uuid4()),
                    "structured_result": {
                        "summary": "bounded synthetic result",
                        "decision": decision,
                        "evidence": [
                            {
                                "kind": "git-snapshot",
                                "reference": "source:gateway",
                                "sha256": "c" * 64,
                            }
                        ],
                        "open_questions": [],
                        "finding": finding,
                    },
                },
            },
        )
    assert isinstance(caught.value.__cause__, ValueError)
    assert "finding" in str(caught.value.__cause__)


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

    verification_workflow_id = f"finding-verify-{finding.id}-{uuid4()}"
    loaded = await handler.handle(
        "finding.load_candidate",
        {
            "finding_id": str(finding.id),
            "project": "falcone",
            "orchestration": {"workflow_id": verification_workflow_id},
        },
    )
    snapshot = {
        **loaded,
        "source_sha": "a" * 40,
        "deployment_sha": "b" * 40,
        "source_worktree": str(tmp_path / "fresh-source"),
        "deployment_worktree": str(tmp_path / "fresh-deployment"),
        "snapshot_evidence_sha256": "e" * 64,
    }
    prepared = await handler.handle(
        "finding.prepare_verifier",
        {
            "finding": loaded,
            "snapshot": snapshot,
            "ordinal": 1,
            "orchestration": {"workflow_id": verification_workflow_id},
        },
    )
    assert prepared["provider"] == "codex"
    attempt = {
        "agent_result": {
            "provider": "codex",
            "structured_result": {
                "summary": "independently reproduced",
                "reproduced": True,
                "positive_control": True,
                "negative_control": True,
                "environment_valid": True,
                "evidence": [
                    {
                        "kind": "fresh-snapshot",
                        "reference": "verification",
                        "sha256": "e" * 64,
                    }
                ],
                "open_questions": [],
            },
        }
    }
    controls = await handler.handle(
        "finding.controls",
        {"finding": loaded, "snapshot": snapshot, "attempts": [attempt, attempt]},
    )
    # GitHub duplicate search is a delivery concern.  Metadata consumes its
    # typed result and never executes a GitHub client itself.
    duplicate = {
        "status": "BLOCKED",
        "blocker": "GITHUB_DUPLICATE_SEARCH_NOT_CONFIGURED",
        "matches": [],
    }
    verification_payload = {
        "finding": loaded,
        "attempts": [attempt, attempt],
        "controls": controls,
        "duplicate": duplicate,
        "orchestration": {"workflow_id": verification_workflow_id},
    }
    verified = await handler.handle("finding.finalize", verification_payload)
    retried = await handler.handle("finding.finalize", verification_payload)

    assert controls["disposition"] == "CONFIRMED"
    assert duplicate["blocker"] == "GITHUB_DUPLICATE_SEARCH_NOT_CONFIGURED"
    assert verified["disposition"] == "BLOCKED"
    assert retried["disposition"] == "BLOCKED"
    with handler.sessions() as session:
        persisted_verification = session.scalar(select(FindingVerification))
        persisted_run = session.scalar(
            select(Run).where(Run.workflow_id == verification_workflow_id)
        )
        persisted_finding = session.get(Finding, finding.id)
    assert persisted_verification is not None
    assert persisted_run is not None
    assert persisted_verification.run_id == persisted_run.id
    assert persisted_run is not None and persisted_run.status == "BLOCKED"
    assert persisted_finding is not None
    assert len(persisted_finding.evidence_json) == 3


@pytest.mark.asyncio
async def test_delivery_transaction_routes_and_leases_both_worktrees(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    # NEW-243: falcone rotates the maker since 2026-11-01 (a hash of the random finding id),
    # so this test pins today's non-rotated routing with a copy of the configs without it.
    project_config_root = tmp_path / "projects"
    shutil.copytree(Path(__file__).parents[1] / "config/projects", project_config_root)
    falcone_config = project_config_root / "falcone.yaml"
    manifest = yaml.safe_load(falcone_config.read_text())
    manifest.pop("provider_rotation", None)
    falcone_config.write_text(yaml.safe_dump(manifest, sort_keys=False))
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'delivery.sqlite'}",
            worktree_root=worktree_root,
            state_root=tmp_path / "state",
            project_config_root=project_config_root,
        )
    )
    Base.metadata.create_all(handler.engine)
    campaign = add_campaign(handler)
    coverage = CoverageCell(
        id=uuid4(),
        campaign_id=campaign.id,
        domain="gateway",
        persona="tenant-admin",
        lens="correctness",
        environment="snapshot",
        status="CONFIRMED_FINDING",
        blocker=None,
        exclusion_approval_id=None,
        evidence_json=[{"sha256": "c" * 64}],
        result_json={"provider": "claude"},
    )
    finding = Finding(
        id=uuid4(),
        campaign_id=campaign.id,
        coverage_cell_id=coverage.id,
        fingerprint="f" * 64,
        project="falcone",
        domain="gateway",
        persona="tenant-admin",
        severity="medium",
        confidence=0.9,
        risk_class="R2",
        disposition="CONFIRMED",
        expected_behavior="bounded contract",
        actual_behavior="bounded mismatch",
        evidence_json=[{"sha256": "c" * 64}],
        minimal_reproduction=["inspect the bounded contract"],
        affected_source_sha="a" * 40,
        affected_deployment_sha="b" * 40,
        affected_image_digest=None,
        affected_component="gateway",
        impact_json={},
        recommended_action="implement the bounded fix",
    )
    workflow_id = f"delivery-{finding.id}"
    run = Run(
        id=uuid4(),
        campaign_id=None,
        change_set_id=None,
        workflow_id=workflow_id,
        run_type="issue-delivery",
        status="QUEUED",
        project="falcone",
    )
    with handler.sessions.begin() as session:
        session.add_all([coverage, finding, run])

    loaded = await handler.handle(
        "delivery.load_confirmed_finding",
        {
            "finding_id": str(finding.id),
            "risk_class": "R2",
            "dry_run": True,
            "orchestration": {"workflow_id": workflow_id},
        },
    )
    issue_gate = await handler.handle("delivery.ensure_issue", loaded)
    live_issue_gate = await handler.handle("delivery.ensure_issue", {**loaded, "dry_run": False})
    leased = await handler.handle("delivery.acquire_leases", loaded)
    routed = await handler.handle("delivery.route_providers", leased)
    change_set_id = routed["change_set_id"]
    allocation = {
        **routed,
        "source_worktree": str(worktree_root / "falcone" / change_set_id),
        "deployment_worktree": str(worktree_root / "falcone-charts" / change_set_id),
        "source_branch": f"agent/falcone/123/{change_set_id}",
        "deployment_branch": f"agent/falcone/123/{change_set_id}",
    }
    persisted = await handler.handle("delivery.persist_worktree_leases", allocation)

    assert issue_gate["status"] == "DRY_RUN"
    assert "issue creation" in issue_gate["dry_run_summary"]
    assert live_issue_gate["blocker"] == "EXPLICIT_FINDING_PUBLICATION_REQUIRED"
    assert routed["maker_provider"] == "codex"
    assert routed["checker_provider"] == "claude"
    assert len(persisted["worktree_lease_ids"]) == 2
    with handler.sessions() as session:
        lease_count = session.scalar(select(func.count()).select_from(WorktreeLease))
    assert lease_count == 2


@pytest.mark.asyncio
async def test_delivery_component_lease_conflict_is_structured(tmp_path: Path) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'lease-conflict.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    first_run = Run(
        id=uuid4(),
        campaign_id=None,
        change_set_id=None,
        workflow_id="delivery-first",
        run_type="issue-delivery",
        status="RUNNING",
        project="falcone",
    )
    second_run = Run(
        id=uuid4(),
        campaign_id=None,
        change_set_id=None,
        workflow_id="delivery-second",
        run_type="issue-delivery",
        status="RUNNING",
        project="falcone",
    )
    with handler.sessions.begin() as session:
        session.add_all([first_run, second_run])

    first = await handler.handle(
        "delivery.acquire_leases",
        {
            "project": "falcone",
            "affected_component": "gateway",
            "run_id": str(first_run.id),
        },
    )
    conflict = await handler.handle(
        "delivery.acquire_leases",
        {
            "project": "falcone",
            "affected_component": "gateway",
            "run_id": str(second_run.id),
        },
    )

    assert first["status"] == "LEASES_ACQUIRED"
    assert conflict == {
        "project": "falcone",
        "affected_component": "gateway",
        "run_id": str(second_run.id),
        "status": "BLOCKED",
        "blocker": "COMPONENT_LEASE_CONFLICT",
    }


@pytest.mark.asyncio
async def test_disabled_deployment_target_is_a_structured_blocker(tmp_path: Path) -> None:
    target_root = tmp_path / "targets"
    target_root.mkdir()
    (target_root / "falcone-staging.yaml").write_text(
        """version: 1
enabled: false
project: falcone
environment: staging
strategy: helm
target_context: UNCONFIGURED
target_namespace: UNCONFIGURED
blocker: SCOPED_FALCONE_STAGING_TARGET_REQUIRED
configuration: {}
""",
        encoding="utf-8",
    )
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'deployment-blocked.sqlite'}",
            deployment_target_root=target_root,
        )
    )
    Base.metadata.create_all(handler.engine)
    change_set_id = uuid4()
    workflow_id = "deploy-staging-blocked"
    change_set = ChangeSet(
        id=change_set_id,
        finding_id=uuid4(),
        issue_id=None,
        project="falcone",
        risk_class="R2",
        state="DEPLOY_PR",
        version=1,
        source_repository="falcone",
        source_branch="agent/falcone/1/test",
        source_base_sha="a" * 40,
        source_head_sha="b" * 40,
        source_pr_url="https://github.com/gntik-ai/falcone/pull/1",
        deployment_repository="falcone-charts",
        deployment_branch="agent/falcone/1/test",
        deployment_base_sha="c" * 40,
        deployment_head_sha="d" * 40,
        deployment_pr_url="https://github.com/gntik-ai/falcone-charts/pull/1",
        image_digests={"api": f"sha256:{'e' * 64}"},
        chart_version="1.2.3",
        recovery_json={},
    )
    run = Run(
        id=uuid4(),
        campaign_id=None,
        change_set_id=change_set_id,
        workflow_id=workflow_id,
        run_type="staging-deployment",
        status="QUEUED",
        project="falcone",
    )
    with handler.sessions.begin() as session:
        session.add_all([change_set, run])

    add_changeset_finding(handler, change_set_id)
    result = await handler.handle(
        "staging.prepare",
        {
            "change_set_id": str(change_set_id),
            "environment": "staging",
            "dry_run": True,
            "orchestration": {"workflow_id": workflow_id},
        },
    )

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "SCOPED_FALCONE_STAGING_TARGET_REQUIRED"


@pytest.mark.asyncio
async def test_cancelled_deployment_run_is_persisted_as_terminal(tmp_path: Path) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'deployment-cancelled.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    change_set_id = uuid4()
    run_id = uuid4()
    workflow_id = "deploy-preview-cancelled"
    with handler.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="falcone",
                risk_class="R1",
                state="CANDIDATE_BUILT",
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
                image_digests={},
                chart_version=None,
                recovery_json={},
            )
        )
        session.add(
            Run(
                id=run_id,
                campaign_id=None,
                change_set_id=change_set_id,
                workflow_id=workflow_id,
                run_type="preview-deployment",
                status="QUEUED",
                project="falcone",
            )
        )

    result = await handler.handle(
        "deployment.record_terminal",
        {
            "change_set_id": str(change_set_id),
            "terminal_status": "CANCELLED",
            "orchestration": {"workflow_id": workflow_id},
        },
    )

    assert result["status"] == "CANCELLED"
    with handler.sessions() as session:
        run = session.get(Run, run_id)
        assert run is not None
        assert run.status == "CANCELLED"
        assert run.finished_at is not None


@pytest.mark.asyncio
async def test_failed_approved_staging_mutation_releases_lease_and_opens_incident(
    tmp_path: Path,
) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'deployment-failed.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    change_set_id = uuid4()
    deployment_id = uuid4()
    run_id = uuid4()
    workflow_id = "deploy-staging-failed"
    binding = "f" * 64
    with handler.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="falcone",
                risk_class="R2",
                state="STAGING_APPROVED",
                version=3,
                source_repository="falcone",
                source_branch="agent/falcone/1/test",
                source_base_sha="a" * 40,
                source_head_sha="b" * 40,
                source_pr_url="https://github.com/gntik-ai/falcone/pull/1",
                deployment_repository="falcone-charts",
                deployment_branch="agent/falcone/1/test",
                deployment_base_sha="c" * 40,
                deployment_head_sha="d" * 40,
                deployment_pr_url="https://github.com/gntik-ai/falcone-charts/pull/1",
                image_digests={"api": f"sha256:{'e' * 64}"},
                chart_version="1.2.3",
                recovery_json={},
            )
        )
        session.add(
            Run(
                id=run_id,
                change_set_id=change_set_id,
                workflow_id=workflow_id,
                run_type="staging-deployment",
                status="RUNNING",
                project="falcone",
            )
        )
        session.add(
            Deployment(
                id=deployment_id,
                change_set_id=change_set_id,
                project="falcone",
                environment="staging",
                strategy="helm",
                target_context="falcone-staging-releaser",
                target_namespace="falcone-staging",
                desired_sha="d" * 40,
                status="STAGING_APPROVED",
                transaction_id=None,
                started_at=datetime.now(UTC),
                finished_at=None,
                recovery_json={},
            )
        )
        session.add(
            ComponentLease(
                component_key="falcone:deploy:staging",
                owner_run_id=run_id,
                mode="deployment",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                released_at=None,
            )
        )
        session.add(
            ApprovalCandidate(
                change_set_id=change_set_id,
                workflow_id=workflow_id,
                risk_class="R2",
                artifact_binding_sha256=binding,
                payload_json={"artifact_binding_sha256": binding},
                invalidated_at=None,
            )
        )

    result = await handler.handle(
        "deployment.record_terminal",
        {
            "project": "falcone",
            "environment": "staging",
            "change_set_id": str(change_set_id),
            "deployment_id": str(deployment_id),
            "artifact_binding_sha256": binding,
            "terminal_status": "DEPLOYMENT_FAILED",
            "blocker": "DEPLOYMENT_ACTIVITY_FAILED",
            "approval_validation": {"current": True},
            "release_plan": {"strategy": "helm"},
            "release_preflight": {
                "previous_revision": 7,
                "previous_manifest_sha256": "9" * 64,
            },
            "release_evidence": {
                "migration": False,
                "rollback_plan_sha256": "1" * 64,
                "ci_result_sha256": "2" * 64,
                "independent_review_sha256": "3" * 64,
            },
            "orchestration": {"workflow_id": workflow_id},
        },
    )

    assert result["status"] == "DEPLOYMENT_FAILED"
    with handler.sessions() as session:
        run = session.get(Run, run_id)
        deployment = session.get(Deployment, deployment_id)
        change_set = session.get(ChangeSet, change_set_id)
        incident = session.scalar(select(Incident).where(Incident.deployment_id == deployment_id))
        active_lease_count = session.scalar(
            select(func.count())
            .select_from(ComponentLease)
            .where(ComponentLease.released_at.is_(None))
        )
        candidate = session.scalar(
            select(ApprovalCandidate).where(ApprovalCandidate.workflow_id == workflow_id)
        )
        assert run is not None and run.status == "FAILED"
        assert deployment is not None and deployment.status == "DEPLOYMENT_FAILED"
        assert deployment.recovery_json["deployed_artifact_binding_sha256"] == binding
        assert change_set is not None and change_set.state == "DEPLOYMENT_FAILED"
        assert incident is not None and incident.recovery_state == "DEPLOYMENT_FAILED"
        assert active_lease_count == 0
        assert candidate is not None and candidate.invalidated_at is not None


@pytest.mark.asyncio
async def test_staging_candidate_binds_preflight_and_both_repository_heads(
    tmp_path: Path,
) -> None:
    target_root = tmp_path / "targets"
    target_root.mkdir()
    (target_root / "musematic-staging.yaml").write_text(
        """version: 1
enabled: true
project: musematic
environment: staging
strategy: argocd-gitops
target_context: musematic-staging-observer
target_namespace: musematic-staging
configuration:
  argo_application: musematic-staging
""",
        encoding="utf-8",
    )
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'deployment-ready.sqlite'}",
            deployment_target_root=target_root,
        )
    )
    Base.metadata.create_all(handler.engine)
    change_set_id = uuid4()
    run_id = uuid4()
    workflow_id = "deploy-staging-ready"
    deployment_worktree = tmp_path / "worktrees" / "musematic-deploy" / str(change_set_id)
    recovery = {
        "release_evidence": {
            "rollback_plan_sha256": "1" * 64,
            "ci_result_sha256": "2" * 64,
            "independent_review_sha256": "3" * 64,
            "backup_requirement": "not-required",
            "migration": False,
        }
    }
    change_set = ChangeSet(
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
        recovery_json=recovery,
    )
    run = Run(
        id=run_id,
        campaign_id=None,
        change_set_id=change_set_id,
        workflow_id=workflow_id,
        run_type="staging-deployment",
        status="QUEUED",
        project="musematic",
    )
    lease = WorktreeLease(
        id=uuid4(),
        change_set_id=change_set_id,
        repository="musematic-deploy",
        worktree_path=str(deployment_worktree),
        branch=f"agent/musematic/1/{change_set_id}",
        owner_run_id=run_id,
        base_sha="c" * 40,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        released_at=None,
    )
    with handler.sessions.begin() as session:
        session.add_all([change_set, run, lease])

    add_changeset_finding(handler, change_set_id)
    prepared = await handler.handle(
        "staging.prepare",
        {
            "change_set_id": str(change_set_id),
            "environment": "staging",
            "dry_run": False,
            "orchestration": {"workflow_id": workflow_id},
        },
    )
    candidate = await handler.handle(
        "staging.record_candidate",
        {
            **prepared,
            "release_preflight": {"status": "PREFLIGHT_PASS", "deployment_sha": "d" * 40},
        },
    )

    assert prepared["release_plan"]["target_revision"] == "d" * 40
    assert candidate["status"] == "AWAITING_STAGING_APPROVAL"
    assert (
        candidate["release_plan"]["approval_binding_sha256"] == candidate["artifact_binding_sha256"]
    )
    assert candidate["approval_candidate"]["source_sha"] == "b" * 40
    assert candidate["approval_candidate"]["deployment_sha"] == "d" * 40
    assert len(candidate["approval_candidate"]["release_preflight_sha256"]) == 64
    with handler.sessions() as session:
        deployment_lease = session.scalar(
            select(ComponentLease).where(ComponentLease.released_at.is_(None))
        )
        assert deployment_lease is not None
        assert deployment_lease.component_key == "musematic:deploy:staging"
    with handler.sessions.begin() as session:
        session.add(
            Approval(
                change_set_id=change_set_id,
                approval_type="staging",
                decision="approve",
                artifact_binding_sha256=candidate["artifact_binding_sha256"],
                approver_subject="operator:test",
                approved_at=datetime.now(UTC),
                invalidated_at=None,
                metadata_json={},
            )
        )

    approved = await handler.handle(
        "staging.validate_approval",
        {
            "context": candidate,
            "approval": {
                "decision": "approve",
                "artifact_binding_sha256": candidate["artifact_binding_sha256"],
            },
        },
    )

    assert approved["status"] == "STAGING_APPROVED"
    assert approved["approval_validation"]["current"] is True

    with handler.sessions.begin() as session:
        stored = session.get(ChangeSet, change_set_id)
        assert stored is not None
        stored.deployment_head_sha = "f" * 40

    invalidated = await handler.handle(
        "staging.validate_approval",
        {
            "context": candidate,
            "approval": {
                "decision": "approve",
                "artifact_binding_sha256": candidate["artifact_binding_sha256"],
            },
        },
    )

    assert invalidated["status"] == "BLOCKED"
    assert invalidated["blocker"] == "APPROVAL_ARTIFACT_DRIFT"
    with handler.sessions() as session:
        stored_run = session.get(Run, run_id)
        assert stored_run is not None
        assert stored_run.status == "BLOCKED"
        assert stored_run.error_code == "APPROVAL_ARTIFACT_DRIFT"
        active_lease_count = session.scalar(
            select(func.count())
            .select_from(ComponentLease)
            .where(ComponentLease.released_at.is_(None))
        )
        assert active_lease_count == 0


@pytest.mark.asyncio
async def test_migration_deployment_requires_real_backup_and_parity_evidence(
    tmp_path: Path,
) -> None:
    target_root = tmp_path / "targets"
    target_root.mkdir()
    (target_root / "musematic-staging.yaml").write_text(
        """version: 1
enabled: true
project: musematic
environment: staging
strategy: argocd-gitops
target_context: musematic-staging-observer
target_namespace: musematic-staging
configuration:
  argo_application: musematic-staging
""",
        encoding="utf-8",
    )
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'migration-evidence.sqlite'}",
            deployment_target_root=target_root,
        )
    )
    Base.metadata.create_all(handler.engine)
    change_set_id = uuid4()
    run_id = uuid4()
    workflow_id = "migration-evidence-required"
    recovery = {
        "release_evidence": {
            "rollback_plan_sha256": "1" * 64,
            "ci_result_sha256": "2" * 64,
            "independent_review_sha256": "3" * 64,
            "backup_requirement": "real-backup-and-parity-required",
            "migration": True,
            "backup_evidence_sha256": None,
            "parity_evidence_sha256": None,
        }
    }
    with handler.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="musematic",
                risk_class="R3",
                state="DEPLOY_PR",
                version=1,
                source_repository="musematic",
                source_branch=f"agent/musematic/1/{change_set_id}",
                source_base_sha="a" * 40,
                source_head_sha="b" * 40,
                source_pr_url="https://github.com/gntik-ai/musematic/pull/1",
                deployment_repository="musematic-deploy",
                deployment_branch=f"agent/musematic/1/{change_set_id}",
                deployment_base_sha="c" * 40,
                deployment_head_sha="d" * 40,
                deployment_pr_url="https://github.com/gntik-ai/musematic-deploy/pull/1",
                image_digests={"api": f"sha256:{'e' * 64}"},
                chart_version=None,
                recovery_json=recovery,
            )
        )
        session.add(
            Run(
                id=run_id,
                change_set_id=change_set_id,
                workflow_id=workflow_id,
                run_type="staging-deployment",
                status="QUEUED",
                project="musematic",
            )
        )

    add_changeset_finding(handler, change_set_id)
    result = await handler.handle(
        "staging.prepare",
        {
            "change_set_id": str(change_set_id),
            "orchestration": {"workflow_id": workflow_id},
        },
    )

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "RELEASE_EVIDENCE_INCOMPLETE"
    assert result["missing_requirements"] == [
        "backup_evidence_sha256",
        "parity_evidence_sha256",
    ]


@pytest.mark.asyncio
async def test_low_risk_staging_approval_is_persisted_and_revalidated(
    tmp_path: Path,
) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'low-risk-approval.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    change_set_id = uuid4()
    run_id = uuid4()
    workflow_id = "deploy-low-risk"
    deployment_id = uuid4()
    with handler.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="falcone",
                risk_class="R1",
                state="DEPLOY_PR",
                version=1,
                source_repository="falcone",
                source_branch=f"agent/falcone/1/{change_set_id}",
                source_base_sha="a" * 40,
                source_head_sha="b" * 40,
                source_pr_url="https://github.com/gntik-ai/falcone/pull/1",
                deployment_repository="falcone-charts",
                deployment_branch=f"agent/falcone/1/{change_set_id}",
                deployment_base_sha="c" * 40,
                deployment_head_sha="d" * 40,
                deployment_pr_url="https://github.com/gntik-ai/falcone-charts/pull/1",
                image_digests={"api": f"sha256:{'e' * 64}"},
                chart_version="1.2.3",
                recovery_json={},
            )
        )
        session.add(
            Run(
                id=run_id,
                change_set_id=change_set_id,
                workflow_id=workflow_id,
                run_type="staging-deployment",
                status="RUNNING",
                project="falcone",
            )
        )

    candidate = await handler.handle(
        "staging.record_candidate",
        {
            "change_set_id": str(change_set_id),
            "deployment_id": str(deployment_id),
            "orchestration": {"workflow_id": workflow_id},
            "target_context": "scoped-staging",
            "target_namespace": "falcone-staging",
            "release_preflight": {"status": "PREFLIGHT_PASS", "manifest_sha256": "f" * 64},
            "release_evidence": {
                "rollback_plan_sha256": "1" * 64,
                "ci_result_sha256": "2" * 64,
                "independent_review_sha256": "3" * 64,
                "backup_requirement": "not-required",
                "migration": False,
            },
            "release_plan": {"approval_binding_sha256": "0" * 64},
        },
    )

    assert candidate["requires_human_approval"] is False
    assert candidate["automatic_approval"]["approver_subject"] == ("policy:low-risk-staging")
    approved = await handler.handle(
        "staging.validate_approval",
        {"context": candidate, "approval": candidate["automatic_approval"]},
    )
    assert approved["status"] == "STAGING_APPROVED"
    assert approved["approval_validation"]["current"] is True
    with handler.sessions() as session:
        approval = session.scalar(select(Approval).where(Approval.change_set_id == change_set_id))
        assert approval is not None
        assert approval.approver_subject == "policy:low-risk-staging"
        assert approval.metadata_json["automatic"] is True


@pytest.mark.asyncio
async def test_runtime_verification_routes_opposite_maker_and_finalizes(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    (runtime_root / "falcone-staging.yaml").write_text(
        """version: 1
enabled: true
project: falcone
environment: staging
target_context: falcone-staging-observer
target_namespace: falcone-staging
configuration:
  observer_kubeconfig_path: /etc/engineering-control/env/observer/falcone.kubeconfig
  observed_resources: [deployments, pods]
  revision_annotation: engineering.gntik.ai/deployment-sha
  journey_plan_path: /etc/engineering-control/journeys/falcone-staging.json
""",
        encoding="utf-8",
    )
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'runtime.sqlite'}",
            runtime_target_root=runtime_root,
            browser_evidence_root=tmp_path / "browser-evidence",
        )
    )
    Base.metadata.create_all(handler.engine)
    change_set_id = uuid4()
    deployment_id = uuid4()
    run_id = uuid4()
    workflow_id = f"runtime-verify-{deployment_id}"
    source_worktree = tmp_path / "worktrees" / "falcone" / str(change_set_id)
    deployment_worktree = tmp_path / "worktrees" / "falcone-charts" / str(change_set_id)
    source_worktree.mkdir(parents=True)
    deployment_worktree.mkdir(parents=True)
    change_set = ChangeSet(
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
        source_pr_url="https://github.com/gntik-ai/falcone/pull/1",
        deployment_repository="falcone-charts",
        deployment_branch="agent/falcone/1/test",
        deployment_base_sha="c" * 40,
        deployment_head_sha="d" * 40,
        deployment_pr_url="https://github.com/gntik-ai/falcone-charts/pull/1",
        image_digests={"api": f"sha256:{'e' * 64}"},
        chart_version="1.2.3",
        recovery_json={"provider_chain": {"maker": "codex", "checker": "claude"}},
    )
    approval_payload = {
        "change_set_id": str(change_set_id),
        "project": "falcone",
        "risk_class": "R2",
        "source_pr": "https://github.com/gntik-ai/falcone/pull/1",
        "source_sha": "b" * 40,
        "deployment_pr": "https://github.com/gntik-ai/falcone-charts/pull/1",
        "deployment_sha": "d" * 40,
        "image_digests": {"api": f"sha256:{'e' * 64}"},
        "target_context": "falcone-staging-releaser",
        "target_namespace": "falcone-staging",
        "migration": False,
        "backup_requirement": "not-required",
        "rollback_plan_sha256": "1" * 64,
        "ci_result_sha256": "2" * 64,
        "independent_review_sha256": "3" * 64,
        "release_preflight_sha256": "4" * 64,
    }
    binding = ApprovalService.digest(approval_payload)
    deployment = Deployment(
        id=deployment_id,
        change_set_id=change_set_id,
        project="falcone",
        environment="staging",
        strategy="helm",
        target_context="falcone-staging-releaser",
        target_namespace="falcone-staging",
        desired_sha="d" * 40,
        status="STAGING_DEPLOYED",
        transaction_id="transaction",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        recovery_json={},
    )
    run = Run(
        id=run_id,
        campaign_id=None,
        change_set_id=change_set_id,
        workflow_id=workflow_id,
        run_type="runtime-verification",
        status="QUEUED",
        project="falcone",
        summary=str(deployment_id),
    )
    candidate = ApprovalCandidate(
        id=uuid4(),
        change_set_id=change_set_id,
        workflow_id="deploy-staging-source",
        risk_class="R2",
        artifact_binding_sha256=binding,
        payload_json={**approval_payload, "artifact_binding_sha256": binding},
        invalidated_at=None,
    )
    approval = Approval(
        id=uuid4(),
        change_set_id=change_set_id,
        approval_type="staging",
        decision="approve",
        artifact_binding_sha256=binding,
        approver_subject="operator:test",
        approved_at=datetime.now(UTC),
        invalidated_at=None,
        metadata_json={},
    )
    leases = [
        WorktreeLease(
            id=uuid4(),
            change_set_id=change_set_id,
            repository=repository,
            worktree_path=str(path),
            branch=f"agent/falcone/1/{change_set_id}",
            owner_run_id=run_id,
            base_sha=base_sha,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            released_at=None,
        )
        for repository, path, base_sha in (
            ("falcone", source_worktree, "a" * 40),
            ("falcone-charts", deployment_worktree, "c" * 40),
        )
    ]
    with handler.sessions.begin() as session:
        session.add_all([change_set, deployment, run, candidate, approval, *leases])

    snapshot = await handler.handle(
        "runtime.snapshot",
        {
            "deployment_id": str(deployment_id),
            "project": "falcone",
            "orchestration": {"workflow_id": workflow_id},
        },
    )

    assert snapshot["status"] == "RUNTIME_SNAPSHOT_READY"
    assert snapshot["review_providers"] == ["claude"]
    prepared = await handler.handle(
        "runtime.prepare_review",
        {
            **snapshot,
            "runtime_observation": {
                "status": "RUNTIME_OBSERVATION_PASS",
                "observed_revisions": ["d" * 40],
                "observed_image_digests": [f"sha256:{'e' * 64}"],
            },
            "result": {"status": "PASS", "error": None},
        },
    )
    assert prepared["runtime_review_requests"][0]["provider"] == "claude"
    finalized = await handler.handle(
        "runtime.finalize",
        {
            **prepared,
            "runtime_review_results": [
                {
                    "status": "SUCCESS",
                    "agent_result": {
                        "provider": "claude",
                        "structured_result": {
                            "decision": "RUNTIME_VERIFIED",
                            "observed_revision": "d" * 40,
                            "observed_digests": {"api": f"sha256:{'e' * 64}"},
                            "checks": [
                                {
                                    "name": "approved-artifacts",
                                    "status": "PASS",
                                    "summary": "revision and digests match",
                                }
                            ],
                            "evidence": [binding],
                            "incident_required": False,
                        },
                    },
                }
            ],
            "orchestration": {"workflow_id": workflow_id},
        },
    )

    assert finalized["status"] == "RUNTIME_VERIFIED"
    with handler.sessions() as session:
        stored_change_set = session.get(ChangeSet, change_set_id)
        stored_deployment = session.get(Deployment, deployment_id)
        stored_run = session.get(Run, run_id)
        assert stored_change_set is not None and stored_change_set.state == "RUNTIME_VERIFIED"
        assert stored_deployment is not None and stored_deployment.status == "RUNTIME_VERIFIED"
        assert stored_run is not None and stored_run.status == "SUCCESS"


@pytest.mark.asyncio
async def test_recovery_requires_new_bound_approval_and_preserves_open_incident(
    tmp_path: Path,
) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'recovery.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    change_set_id = uuid4()
    deployment_id = uuid4()
    run_id = uuid4()
    workflow_id = f"recovery-{deployment_id}"
    preflight = {
        "status": "PREFLIGHT_PASS",
        "previous_revision": 7,
        "previous_status": "deployed",
        "previous_manifest_sha256": "9" * 64,
    }
    release_evidence = {
        "migration": False,
        "backup_requirement": "not-required",
        "rollback_plan_sha256": "1" * 64,
        "ci_result_sha256": "2" * 64,
        "independent_review_sha256": "3" * 64,
    }
    approval_payload = {
        "change_set_id": str(change_set_id),
        "project": "falcone",
        "risk_class": "R2",
        "source_pr": "https://github.com/gntik-ai/falcone/pull/1",
        "source_sha": "b" * 40,
        "deployment_pr": "https://github.com/gntik-ai/falcone-charts/pull/1",
        "deployment_sha": "d" * 40,
        "image_digests": {"api": f"sha256:{'e' * 64}"},
        "target_context": "falcone-staging-releaser",
        "target_namespace": "falcone-staging",
        "migration": False,
        "backup_requirement": "not-required",
        "rollback_plan_sha256": "1" * 64,
        "ci_result_sha256": "2" * 64,
        "independent_review_sha256": "3" * 64,
        "release_preflight_sha256": hashlib.sha256(
            json.dumps(preflight, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest(),
    }
    original_binding = ApprovalService.digest(approval_payload)
    change_set = ChangeSet(
        id=change_set_id,
        finding_id=uuid4(),
        issue_id=None,
        project="falcone",
        risk_class="R2",
        state="INCIDENT_OPEN",
        version=2,
        source_repository="falcone",
        source_branch="agent/falcone/1/test",
        source_base_sha="a" * 40,
        source_head_sha="b" * 40,
        source_pr_url="https://github.com/gntik-ai/falcone/pull/1",
        deployment_repository="falcone-charts",
        deployment_branch="agent/falcone/1/test",
        deployment_base_sha="c" * 40,
        deployment_head_sha="d" * 40,
        deployment_pr_url="https://github.com/gntik-ai/falcone-charts/pull/1",
        image_digests={"api": f"sha256:{'e' * 64}"},
        chart_version="1.2.3",
        recovery_json={},
    )
    deployment = Deployment(
        id=deployment_id,
        change_set_id=change_set_id,
        project="falcone",
        environment="staging",
        strategy="helm",
        target_context="falcone-staging-releaser",
        target_namespace="falcone-staging",
        desired_sha="d" * 40,
        status="VERIFICATION_FAILED",
        transaction_id="failed-transaction",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        recovery_json={
            "deployed_artifact_binding_sha256": original_binding,
            "release_plan": {
                "strategy": "helm",
                "release_name": "falcone",
                "kube_context": "falcone-staging-releaser",
                "kubeconfig_path": "/etc/engineering-control/env/release/falcone.kubeconfig",
                "timeout_minutes": 30,
            },
            "release_preflight": preflight,
            "release_evidence": release_evidence,
            "previous_deployment_sha": "c" * 40,
        },
    )
    run = Run(
        id=run_id,
        change_set_id=change_set_id,
        workflow_id=workflow_id,
        run_type="recovery",
        status="QUEUED",
        project="falcone",
        summary=str(deployment_id),
    )
    with handler.sessions.begin() as session:
        session.add_all(
            [
                change_set,
                deployment,
                run,
                Approval(
                    change_set_id=change_set_id,
                    approval_type="staging",
                    decision="approve",
                    artifact_binding_sha256=original_binding,
                    approver_subject="operator:staging",
                    approved_at=datetime.now(UTC),
                    invalidated_at=None,
                    metadata_json={},
                ),
            ]
        )

    inspected = await handler.handle(
        "recovery.inspect",
        {
            "deployment_id": str(deployment_id),
            "expected_artifact_binding_sha256": original_binding,
            "dry_run": False,
            "orchestration": {"workflow_id": workflow_id},
        },
    )
    validated = await handler.handle("recovery.validate_binding", inspected)
    candidate = await handler.handle("recovery.prepare_candidate", validated)

    assert candidate["status"] == "AWAITING_RECOVERY_APPROVAL"
    assert candidate["recovery_plan"]["previous_revision"] == 7
    recovery_binding = candidate["artifact_binding_sha256"]
    with handler.sessions.begin() as session:
        session.add(
            Approval(
                change_set_id=change_set_id,
                approval_type="recovery",
                decision="approve",
                artifact_binding_sha256=recovery_binding,
                approver_subject="operator:recovery",
                approved_at=datetime.now(UTC),
                invalidated_at=None,
                metadata_json={},
            )
        )
    approved = await handler.handle(
        "recovery.validate_approval",
        {
            "context": candidate,
            "approval": {
                "decision": "approve",
                "artifact_binding_sha256": recovery_binding,
            },
        },
    )
    assert approved["status"] == "RECOVERY_APPROVED"
    recovered = await handler.handle(
        "recovery.verify",
        {
            **approved,
            "status": "ROLLBACK_APPLIED",
            "manifest_sha256": "9" * 64,
            "transaction_sha256": "8" * 64,
        },
    )

    assert recovered["status"] == "ROLLED_BACK"
    assert recovered["runtime_retest_required"] is True
    with handler.sessions() as session:
        stored_change_set = session.get(ChangeSet, change_set_id)
        stored_deployment = session.get(Deployment, deployment_id)
        stored_run = session.get(Run, run_id)
        incident = session.scalar(select(Incident).where(Incident.deployment_id == deployment_id))
        active_leases = session.scalar(
            select(func.count())
            .select_from(ComponentLease)
            .where(ComponentLease.released_at.is_(None))
        )
        assert stored_change_set is not None and stored_change_set.state == "ROLLED_BACK"
        assert stored_deployment is not None and stored_deployment.status == "ROLLBACK_APPLIED"
        assert stored_run is not None and stored_run.status == "SUCCESS"
        assert incident is not None and incident.recovery_state == "RUNTIME_RETEST_REQUIRED"
        assert active_leases == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["absent", "protected", "both"])
async def test_cleanup_authorizes_only_terminal_retained_worktree_lease(
    tmp_path: Path, outcome: str
) -> None:
    worktree_root = tmp_path / "worktrees"
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'cleanup.sqlite'}",
            worktree_root=worktree_root,
            cleanup_execution_enabled=True,
            cleanup_retention_days=1,
        )
    )
    Base.metadata.create_all(handler.engine)
    change_set_id = uuid4()
    owner_run_id = uuid4()
    lease_id = uuid4()
    workflow_id = "cleanup-test"
    branch = f"agent/falcone/123/{change_set_id}"
    path = worktree_root / "falcone" / str(change_set_id)
    old = datetime.now(UTC) - timedelta(days=2)
    with handler.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="falcone",
                risk_class="R1",
                state="ISSUE_CLOSED",
                version=1,
                source_repository="falcone",
                source_branch=branch,
                source_base_sha="a" * 40,
                source_head_sha="b" * 40,
                source_pr_url=None,
                deployment_repository="falcone-charts",
                deployment_branch=f"agent/falcone-charts/123/{change_set_id}",
                deployment_base_sha="c" * 40,
                deployment_head_sha="d" * 40,
                deployment_pr_url=None,
                image_digests={},
                chart_version=None,
                recovery_json={},
            )
        )
        session.add(
            Run(
                id=owner_run_id,
                change_set_id=change_set_id,
                workflow_id="delivery-terminal",
                run_type="issue-delivery",
                status="SUCCESS",
                project="falcone",
                started_at=old,
                finished_at=old,
            )
        )
        session.add(
            WorktreeLease(
                id=lease_id,
                change_set_id=change_set_id,
                repository="falcone",
                worktree_path=str(path),
                branch=branch,
                owner_run_id=owner_run_id,
                base_sha="a" * 40,
                expires_at=old,
                released_at=old,
            )
        )

    discovered = await handler.handle(
        "cleanup.discover",
        {
            "dry_run": False,
            "retention_policy": "standard",
            "orchestration": {"workflow_id": workflow_id},
        },
    )
    assert [value["lease_id"] for value in discovered["cleanup_candidates"]] == [str(lease_id)]
    inspected = {
        **discovered,
        "eligible_candidates": [{**discovered["cleanup_candidates"][0], "inspection": "ABSENT"}],
        "protected_candidates": [],
        "inspected_file_candidates": [],
        "inspected_preview_namespace_candidates": [],
    }
    authorized = await handler.handle("cleanup.authorize", inspected)
    assert authorized["status"] == "CLEANUP_AUTHORIZED"
    metadata_executed = await handler.handle(
        "cleanup.execute_metadata",
        {
            **authorized,
            "removed_file_candidate_ids": [],
            "absent_file_candidate_ids": [],
            "removed_preview_namespace_candidate_ids": [],
            "absent_preview_namespace_candidate_ids": [],
        },
    )
    # NEW-105: a worktree cleanup.execute could not remove (protected) is retained and
    # reported; it no longer fails the run. A lease cannot be both completed and protected.
    protected = [{"lease_id": str(lease_id), "reason": "Permission denied"}]
    request = {
        **metadata_executed,
        "removed_lease_ids": [],
        "already_absent_lease_ids": [] if outcome == "protected" else [str(lease_id)],
        "protected_cleanup_candidates": [] if outcome == "absent" else protected,
        "terminal_status": "COMPLETE",
    }
    if outcome == "both":
        with pytest.raises(Exception, match="invalid metadata activity input"):
            await handler.handle("cleanup.finalize", request)
        return
    finalized = await handler.handle("cleanup.finalize", request)
    assert finalized["status"] == "COMPLETE"
    with handler.sessions() as session:
        cleanup_run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
        assert cleanup_run is not None and cleanup_run.status == "SUCCESS"
        if outcome == "protected":
            assert cleanup_run.error_code == "CLEANUP_PROTECTED_WORKTREES_RETAINED"
            assert "1 protected worktrees retained" in (cleanup_run.summary or "")
            assert f"{lease_id}: Permission denied" in (cleanup_run.summary or "")
        else:
            assert cleanup_run.error_code is None


@pytest.mark.asyncio
async def test_cleanup_blocked_enqueues_one_idempotent_failure_notification(tmp_path: Path) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'cleanup-blocked.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    workflow_id = "cleanup-blocked-notification-test"
    with handler.sessions.begin() as session:
        session.add(
            Run(
                id=uuid4(),
                change_set_id=None,
                workflow_id=workflow_id,
                run_type="cleanup",
                status="RUNNING",
                project="platform",
                started_at=datetime.now(UTC),
            )
        )

    payload = {
        "orchestration": {"workflow_id": workflow_id},
        "terminal_status": "BLOCKED",
        "blocker": "CLEANUP_ACTIVITY_FAILED",
    }
    first = await handler.handle("cleanup.finalize", payload)
    second = await handler.handle("cleanup.finalize", payload)
    assert first["status"] == second["status"] == "BLOCKED"
    with handler.sessions() as session:
        rows = list(session.scalars(select(NotificationOutbox)).all())
        run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
    assert len(rows) == 1
    assert rows[0].topic == "telegram.engineering-cleanup-failed"
    assert rows[0].payload_json["error_code"] == "CLEANUP_ACTIVITY_FAILED"
    assert run is not None and run.status == "BLOCKED"


@pytest.mark.asyncio
async def test_cleanup_protects_all_resources_owned_by_rate_limited_run(tmp_path: Path) -> None:
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'cleanup-rate-limit.sqlite'}",
            worktree_root=tmp_path / "worktrees",
            cleanup_retention_days=1,
            cleanup_expiry_grace_hours=1,
        )
    )
    Base.metadata.create_all(handler.engine)
    change_set_id = uuid4()
    owner_run_id = uuid4()
    worktree_lease_id = uuid4()
    component_lease_id = uuid4()
    old = datetime.now(UTC) - timedelta(days=2)
    branch = f"agent/falcone/123/{change_set_id}"
    with handler.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="falcone",
                risk_class="R1",
                state="ISSUE_CLOSED",
                version=1,
                source_repository="falcone",
                source_branch=branch,
                source_base_sha="a" * 40,
                source_head_sha="b" * 40,
                source_pr_url=None,
                deployment_repository="falcone-charts",
                deployment_branch=f"agent/falcone-charts/123/{change_set_id}",
                deployment_base_sha="c" * 40,
                deployment_head_sha="d" * 40,
                deployment_pr_url=None,
                image_digests={},
                chart_version=None,
                recovery_json={},
            )
        )
        session.add(
            Run(
                id=owner_run_id,
                change_set_id=change_set_id,
                workflow_id="delivery-rate-limited",
                run_type="issue-delivery",
                status="RATE_LIMITED",
                project="falcone",
                started_at=old,
                finished_at=old,
                error_code="PROVIDER_RATE_LIMIT",
            )
        )
        session.add(
            WorktreeLease(
                id=worktree_lease_id,
                change_set_id=change_set_id,
                repository="falcone",
                worktree_path=str(tmp_path / "worktrees" / "falcone" / str(change_set_id)),
                branch=branch,
                owner_run_id=owner_run_id,
                base_sha="a" * 40,
                expires_at=old,
                released_at=old,
            )
        )
        session.add(
            ComponentLease(
                id=component_lease_id,
                component_key="falcone:gateway",
                owner_run_id=owner_run_id,
                mode="write",
                expires_at=old,
                released_at=None,
            )
        )

    discovered = await handler.handle(
        "cleanup.discover",
        {
            "dry_run": True,
            "retention_policy": "standard",
            "orchestration": {"workflow_id": "cleanup-rate-limit-test"},
        },
    )

    assert discovered["cleanup_candidates"] == []
    assert discovered["component_lease_cleanup_candidates"] == []
    assert discovered["file_cleanup_candidates"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["repository-static", "current-state-revalidation"])
async def test_verifier_prompt_states_the_evidence_binding_controls_enforce(
    tmp_path: Path, flow: str
) -> None:
    # NEW-22: live verifiers reproduced findings correctly but omitted the snapshot
    # hash (or padded a git SHA to 64 chars), because the prompt never said
    # _evaluate_controls requires it; one attempt also failed closed looking for a
    # GitHub issue index the control plane never supplies.
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
    await handler.handle(
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
                    "evidence": [{"kind": "source", "reference": "gw", "sha256": "d" * 64}],
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
    with handler.sessions() as session:
        finding = session.scalar(select(Finding))
    assert finding is not None
    workflow_id = f"finding-verify-{finding.id}-{uuid4()}"
    loaded = await handler.handle(
        "finding.load_candidate",
        {
            "finding_id": str(finding.id),
            "project": "falcone",
            "orchestration": {"workflow_id": workflow_id},
        },
    )
    loaded = {**loaded, "flow": flow}
    anchor = "a5f81bf64f588d77c051d1213c7fe814c339eb58bb36be3c94f7570ad9643558"
    snapshot = {
        **loaded,
        "source_sha": "a" * 40,
        "deployment_sha": "b" * 40,
        "source_worktree": str(tmp_path / "fresh-source"),
        "deployment_worktree": str(tmp_path / "fresh-deployment"),
        "snapshot_evidence_sha256": anchor,
    }
    prepared = await handler.handle(
        "finding.prepare_verifier",
        {
            "finding": loaded,
            "snapshot": snapshot,
            "ordinal": 1,
            "orchestration": {"workflow_id": workflow_id},
        },
    )
    prompt = prepared["verifier_request"]["prompt"]

    assert f'kind "fresh_snapshot" and sha256 exactly "{anchor}"' in prompt
    assert "No GitHub issue index is supplied" in prompt
    assert "must not set environment_valid to false" in prompt
    assert ("its own issue is expected to exist" in prompt) is (
        flow == "current-state-revalidation"
    )
    # NEW-73: revalidation verifiers judge from source when the steps need network/CI.
    assert ("This is a current-SOURCE revalidation" in prompt) is (
        flow == "current-state-revalidation"
    )


@pytest.mark.asyncio
async def test_existing_backlog_issue_is_bound_to_the_changeset_for_openspec(
    tmp_path: Path,
) -> None:
    # NEW-29: the first delivery past prechecks (musematic #1025) failed at
    # delivery.create_openspec with "OpenSpec requires a ChangeSet with a GitHub issue":
    # the revalidated backlog issue was linked to the finding but never to the ChangeSet,
    # which only the finding-publication path did.
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'delivery.sqlite'}",
            worktree_root=tmp_path / "worktrees",
            state_root=tmp_path / "state",
            project_config_root=Path(__file__).parents[1] / "config/projects",
        )
    )
    Base.metadata.create_all(handler.engine)
    campaign = add_campaign(handler)
    coverage = CoverageCell(
        id=uuid4(),
        campaign_id=campaign.id,
        domain="gateway",
        persona="tenant-admin",
        lens="correctness",
        environment="snapshot",
        status="CONFIRMED_FINDING",
        blocker=None,
        exclusion_approval_id=None,
        evidence_json=[{"sha256": "c" * 64}],
        result_json={"provider": "claude"},
    )
    finding = Finding(
        id=uuid4(),
        campaign_id=campaign.id,
        coverage_cell_id=coverage.id,
        fingerprint="f" * 64,
        project="falcone",
        domain="gateway",
        persona="tenant-admin",
        severity="medium",
        confidence=0.9,
        risk_class="R1",
        disposition="CONFIRMED",
        expected_behavior="bounded contract",
        actual_behavior="bounded mismatch",
        evidence_json=[{"sha256": "c" * 64}],
        minimal_reproduction=["inspect the bounded contract"],
        affected_source_sha="a" * 40,
        affected_deployment_sha="b" * 40,
        affected_image_digest=None,
        affected_component="gateway",
        impact_json={},
        recommended_action="implement the bounded fix",
    )
    source_issue_repository, _ = handler._project_issue_repositories(Project.FALCONE)
    issue = Issue(
        id=uuid4(),
        finding_id=finding.id,
        repository=source_issue_repository,
        number=1025,
        url=f"https://github.com/{source_issue_repository}/issues/1025",
        state="OPEN",
        openspec_change=None,
    )
    workflow_id = f"backlog-loop-falcone-issue-1-attempt-1-{finding.id}"
    run = Run(
        id=uuid4(),
        campaign_id=None,
        change_set_id=None,
        workflow_id=workflow_id,
        run_type="issue-delivery",
        status="QUEUED",
        project="falcone",
    )
    with handler.sessions.begin() as session:
        session.add_all([coverage, finding, issue, run])

    loaded = await handler.handle(
        "delivery.load_confirmed_finding",
        {
            "finding_id": str(finding.id),
            "risk_class": "R1",
            "dry_run": False,
            "orchestration": {"workflow_id": workflow_id},
        },
    )
    spec = await handler.handle(
        "delivery.create_openspec",
        {
            **loaded,
            "agent_result": {"structured_result": {"decision": "READY", "title": "fix"}},
        },
    )

    assert loaded["issue_number"] == 1025
    assert spec["status"] == "OPENSPEC_READY"
    with handler.sessions() as session:
        change_set = session.get(ChangeSet, UUID(loaded["change_set_id"]))
        assert change_set is not None and change_set.issue_id == issue.id


@pytest.mark.parametrize(
    ("state", "run_status", "finished", "lease_released", "activity_days", "expected"),
    [
        # NEW-85: delivered ChangeSets end RUNTIME_VERIFIED, never ISSUE_CLOSED.
        ("RUNTIME_VERIFIED", "SUCCESS", True, False, 20, "ABANDONED_LEASE_EXPIRED"),
        ("RUNTIME_VERIFIED", "SUCCESS", True, True, 20, "RELEASED_RETENTION_EXPIRED"),
        # A published ChangeSet may still be merged/released: never auto-cleaned.
        ("SOURCE_PR", "SUCCESS", True, True, 20, None),
        # Killed pre-GO attempts: Run never finished, lease never released.
        ("CONFIRMED", "RUNNING", False, False, 20, "ABANDONED_CHANGESET_STALE"),
        ("PLANNED", "RUNNING", False, False, 20, "ABANDONED_CHANGESET_STALE"),
        # ...but not while anything touched the ChangeSet within the retention period.
        ("CONFIRMED", "RUNNING", False, False, 3, None),
    ],
)
def test_worktree_cleanup_eligibility_covers_delivered_and_abandoned_changesets(
    tmp_path: Path,
    state: str,
    run_status: str,
    finished: bool,
    lease_released: bool,
    activity_days: int,
    expected: str | None,
) -> None:
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'eligibility.sqlite'}",
            worktree_root=tmp_path / "worktrees",
            cleanup_retention_days=14,
        )
    )
    Base.metadata.create_all(handler.engine)
    now = datetime.now(UTC)
    old = now - timedelta(days=activity_days)
    change_set_id, run_id, lease_id = uuid4(), uuid4(), uuid4()
    with handler.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="musematic",
                risk_class="R1",
                state=state,
                version=1,
                source_repository="musematic",
                source_branch=f"agent/musematic/1/{change_set_id}",
                source_base_sha="a" * 40,
                source_head_sha="b" * 40,
                source_pr_url=None,
                deployment_repository="musematic-deploy",
                deployment_branch=f"agent/musematic-deploy/1/{change_set_id}",
                deployment_base_sha="c" * 40,
                deployment_head_sha="d" * 40,
                deployment_pr_url=None,
                image_digests={},
                chart_version=None,
                recovery_json={},
                created_at=old,
                updated_at=old,
            )
        )
        session.add(
            Run(
                id=run_id,
                change_set_id=change_set_id,
                workflow_id=f"delivery-{run_id}",
                run_type="issue-delivery",
                status=run_status,
                project="musematic",
                started_at=old,
                finished_at=old if finished else None,
                created_at=old,
                updated_at=old,
            )
        )
        session.add(
            WorktreeLease(
                id=lease_id,
                change_set_id=change_set_id,
                repository="musematic",
                worktree_path=str(tmp_path / "worktrees" / "musematic" / str(change_set_id)),
                branch=f"agent/musematic/1/{change_set_id}",
                owner_run_id=run_id,
                base_sha="a" * 40,
                expires_at=old,
                released_at=old if lease_released else None,
            )
        )

    with handler.sessions() as session:
        lease = session.get(WorktreeLease, lease_id)
        assert lease is not None
        assert handler._cleanup_lease_eligibility(session, lease, now) == expected
