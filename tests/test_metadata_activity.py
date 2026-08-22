from __future__ import annotations

import hashlib
import json
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
    ApprovalCandidate,
    Campaign,
    ChangeSet,
    ComponentLease,
    CoverageCell,
    Deployment,
    Finding,
    Incident,
    QuotaSnapshot,
    Run,
    WorktreeLease,
)
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
  browser_credential_file: null
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
async def test_cleanup_authorizes_only_terminal_retained_worktree_lease(tmp_path: Path) -> None:
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
    }
    authorized = await handler.handle("cleanup.authorize", inspected)
    assert authorized["status"] == "CLEANUP_AUTHORIZED"
    finalized = await handler.handle(
        "cleanup.finalize",
        {
            **authorized,
            "removed_lease_ids": [],
            "already_absent_lease_ids": [str(lease_id)],
            "terminal_status": "COMPLETE",
        },
    )
    assert finalized["status"] == "COMPLETE"
    with handler.sessions() as session:
        cleanup_run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
        assert cleanup_run is not None and cleanup_run.status == "SUCCESS"
