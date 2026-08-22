from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import (
    Approval,
    Campaign,
    ChangeSet,
    CoverageCell,
    Finding,
    QuotaSnapshot,
    Run,
    WorktreeLease,
)


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

    loaded = await handler.handle(
        "finding.load_candidate",
        {"finding_id": str(finding.id), "project": "falcone"},
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
        {"finding": loaded, "snapshot": snapshot, "ordinal": 1},
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
    duplicate = await handler.handle(
        "finding.dedupe",
        {"finding": loaded, "attempts": [attempt, attempt], "controls": controls},
    )
    verified = await handler.handle(
        "finding.finalize",
        {
            "finding": loaded,
            "attempts": [attempt, attempt],
            "controls": controls,
            "duplicate": duplicate,
        },
    )

    assert controls["disposition"] == "CONFIRMED"
    assert duplicate["blocker"] == "GITHUB_DUPLICATE_SEARCH_NOT_CONFIGURED"
    assert verified["disposition"] == "BLOCKED"


@pytest.mark.asyncio
async def test_delivery_transaction_routes_and_leases_both_worktrees(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'delivery.sqlite'}",
            worktree_root=worktree_root,
            state_root=tmp_path / "state",
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
    assert live_issue_gate["blocker"] == "GITHUB_ISSUE_CREATE_NOT_CONFIGURED"
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
    assert candidate["release_plan"]["approval_binding_sha256"] == candidate[
        "artifact_binding_sha256"
    ]
    assert candidate["approval_candidate"]["source_sha"] == "b" * 40
    assert candidate["approval_candidate"]["deployment_sha"] == "d" * 40
    assert len(candidate["approval_candidate"]["release_preflight_sha256"]) == 64

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
