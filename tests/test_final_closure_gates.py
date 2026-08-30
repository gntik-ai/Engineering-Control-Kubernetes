from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from engineering_control.activities.delivery import DeliveryActivityHandler
from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import (
    ActionCandidate,
    AgentExecution,
    BacklogIssueRecord,
    ChangeSet,
    Deployment,
    GitHubBacklogCampaign,
    Run,
    VerificationResult,
    WorktreeLease,
)
from engineering_control.engineering.backlog import (
    BACKLOG_APPROVAL_PHRASE,
    DocumentationImpact,
    approval_binding,
    canonical_sha256,
)
from engineering_control.models.enums import ChangeSetState
from engineering_control.models.final_closure import (
    FinalArtifactPlan,
    FinalClosurePlan,
    FinalDeploymentMergePlan,
    FinalIssueMutationPlan,
    ProtectedMergeResult,
)
from engineering_control.models.reconciliation import CandidateArtifactManifest


def _runtime_evidence() -> dict[str, Any]:
    return {
        "observation": {"status": "RUNTIME_OBSERVATION_PASS"},
        "browser": {"status": "PASS"},
        "reviews": [
            {
                "decision": "RUNTIME_VERIFIED",
                "checks": [{"name": "issue-acceptance-and-regression", "status": "PASS"}],
            }
        ],
    }


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'final-closure.sqlite'}",
        project_config_root=Path("config/projects").resolve(),
        worktree_root=tmp_path / "worktrees",
        run_root=tmp_path / "runs",
    )


def _campaign(campaign_id: UUID, approval: Any) -> GitHubBacklogCampaign:
    return GitHubBacklogCampaign(
        id=campaign_id,
        project="falcone",
        source_repository="gntik-ai/falcone",
        deployment_repository="gntik-ai/falcone-charts",
        imported_at=datetime.now(UTC),
        issue_query="is:issue is:open",
        import_filter="all",
        source_default_branch="main",
        deployment_default_branch="main",
        source_head_at_import="a" * 40,
        deployment_head_at_import="c" * 40,
        issue_count=1,
        eligible_count=1,
        deferred_count=0,
        duplicate_count=0,
        stale_count=0,
        already_fixed_count=0,
        existing_pr_count=0,
        blocked_count=0,
        dependency_graph_hash="1" * 64,
        plan_hash="2" * 64,
        approval_binding=approval.sha256(),
        approval_json={
            "binding": approval.model_dump(mode="json"),
            "policy_role": "owner",
            "approval": {
                "approved_by": "telegram:42",
                "approved_at": datetime.now(UTC).isoformat(),
                "typed_confirmation": BACKLOG_APPROVAL_PHRASE,
            },
        },
        maximum_risk=approval.maximum_risk,
        maximum_items=1,
        maximum_concurrency=1,
        delivery_mode="full-delivery",
        include_enhancements=False,
        status="APPROVED",
        workflow_id=f"backlog-import-{campaign_id}",
    )


def _issue(campaign_id: UUID, change_set_id: UUID, *, risk: str = "R2") -> BacklogIssueRecord:
    return BacklogIssueRecord(
        backlog_campaign_id=campaign_id,
        repository="gntik-ai/falcone",
        issue_number=42,
        issue_url_reference="https://github.com/gntik-ai/falcone/issues/42",
        title="Bounded final closure",
        state="OPEN",
        labels_json=["bug"],
        milestone=None,
        issue_type="PRODUCT_BUG",
        openspec_kind=None,
        severity="high",
        calculated_risk=risk,
        component="gateway",
        source_or_deployment_owner="source",
        documentation_levels_json=[],
        dependencies_json=[],
        blocking_issues_json=[],
        issues_blocked_json=[],
        linked_prs_json=[],
        linked_branches_json=[],
        linked_commits_json=[],
        duplicate_candidates_json=[],
        current_source_reproduction="REPRODUCED_CURRENT_SOURCE",
        current_runtime_reproduction="REPRODUCED_CURRENT_RUNTIME",
        current_runtime_required=True,
        reproduction_mode="RUNTIME_REQUIRED",
        candidate_status="READY",
        eligibility_status="READY",
        eligibility_reason="CURRENT_REVALIDATION_PASS",
        planned_wave="W0",
        planned_order=1,
        finding_id=None,
        change_set_id=change_set_id,
        delivery_status="RUNTIME_VERIFIED",
        final_closure_status="OPEN",
        documentation_impact=(DocumentationImpact.DOCUMENTATION_NOT_REQUIRED_WITH_EVIDENCE.value),
        body_sha256="3" * 64,
        github_updated_at=datetime.now(UTC),
        github_snapshot_json={"state": "OPEN"},
    )


def _change_set(
    change_set_id: UUID,
    *,
    state: str = ChangeSetState.RUNTIME_VERIFIED.value,
    source_head: str = "b" * 40,
    deployment_head: str = "c" * 40,
    recovery: dict[str, Any] | None = None,
) -> ChangeSet:
    branch = f"agent/falcone/issue/{change_set_id}"
    return ChangeSet(
        id=change_set_id,
        finding_id=uuid4(),
        issue_id=None,
        project="falcone",
        risk_class="R2",
        state=state,
        version=1,
        source_repository="falcone",
        source_branch=branch,
        source_base_sha="a" * 40,
        source_head_sha=source_head,
        source_pr_url="https://github.com/gntik-ai/falcone/pull/10",
        deployment_repository="falcone-charts",
        deployment_branch=branch,
        deployment_base_sha="c" * 40,
        deployment_head_sha=deployment_head,
        deployment_pr_url="https://github.com/gntik-ai/falcone-charts/pull/11",
        image_digests={},
        chart_version=None,
        recovery_json=recovery
        or {
            "provider_chain": {"maker": "codex", "checker": "claude"},
            "release_evidence": {"independent_review_sha256": "4" * 64},
        },
    )


def _seed_prepare(
    tmp_path: Path,
    *,
    state: str = ChangeSetState.RUNTIME_VERIFIED.value,
    risk: str = "R2",
    approved_at: datetime | None = None,
) -> tuple[MetadataActivityHandler, dict[str, Any], GitHubBacklogCampaign]:
    handler = MetadataActivityHandler(_settings(tmp_path))
    Base.metadata.create_all(handler.engine)
    campaign_id = uuid4()
    change_set_id = uuid4()
    approval = approval_binding(
        campaign_id=str(campaign_id),
        project="falcone",
        import_filter="all",
        source_head="a" * 40,
        deployment_head="c" * 40,
        dependency_plan_hash="2" * 64,
        maximum_risk="R3" if risk == "R3" else "R2",
        maximum_items=1,
        delivery_mode="full-delivery",
        include_enhancements=False,
        now=approved_at,
    )
    campaign = _campaign(campaign_id, approval)
    with handler.sessions.begin() as session:
        session.add(campaign)
        session.add(_issue(campaign_id, change_set_id, risk=risk))
        change_set = _change_set(change_set_id, state=state)
        change_set.risk_class = risk
        session.add(change_set)
    return (
        handler,
        {
            "backlog_campaign_id": str(campaign_id),
            "change_set_id": str(change_set_id),
            "project": "falcone",
            "issue_repository": "gntik-ai/falcone",
            "issue_number": 42,
            "orchestration": {"workflow_id": f"final-closure-{change_set_id}"},
        },
        campaign,
    )


def _plan() -> FinalClosurePlan:
    value = _seed_plan_values()
    return FinalClosurePlan.model_validate(value)


def _seed_plan_values() -> dict[str, Any]:
    change_set_id = uuid4()
    branch = f"agent/falcone/issue/{change_set_id}"
    return {
        "change_set_id": str(change_set_id),
        "run_id": str(uuid4()),
        "workflow_id": f"final-{change_set_id}",
        "backlog_campaign_id": str(uuid4()),
        "approval_binding_sha256": "1" * 64,
        "project": "falcone",
        "issue_repository": "gntik-ai/falcone",
        "issue_number": 42,
        "source": {
            "repository": "gntik-ai/falcone",
            "number": 10,
            "url": "https://github.com/gntik-ai/falcone/pull/10",
            "head_branch": branch,
            "head_sha": "b" * 40,
            "base_branch": "main",
        },
        "deployment": {
            "repository": "gntik-ai/falcone-charts",
            "number": 11,
            "url": "https://github.com/gntik-ai/falcone-charts/pull/11",
            "head_branch": branch,
            "head_sha": "c" * 40,
            "base_branch": "main",
        },
        "maker_provider": "codex",
        "checker_provider": "claude",
        "documentation_impact": (
            DocumentationImpact.DOCUMENTATION_NOT_REQUIRED_WITH_EVIDENCE.value
        ),
        "policy": {
            "source_ci_workflow": "CI",
            "candidate_build": {
                "enabled": True,
                "workflow_name": "Build",
                "artifact_name": "manifest",
                "manifest_file": "manifest.json",
                "signer_workflow": "gntik-ai/falcone/.github/workflows/build.yml",
            },
            "immutable_promotion": {
                "enabled": True,
                "deployment_file": "values.yaml",
                "chart_directory": "charts/falcone",
                "values_files": ["values.yaml"],
                "component_lease": "falcone:deployment",
                "image_repositories": {"api": "ghcr.io/gntik-ai/falcone"},
                "digest_paths": {"api": "image.digest"},
            },
        },
        "production": False,
    }


def _merge(plan: FinalClosurePlan, *, source: bool) -> ProtectedMergeResult:
    binding = plan.source if source else plan.deployment
    return ProtectedMergeResult(
        repository=binding.repository,
        pull_request_number=binding.number,
        pull_request_url=binding.url,
        head_sha=binding.head_sha,
        merge_sha=("d" if source else "e") * 40,
        merge_tree_sha=("f" if source else "9") * 40,
        state="MERGED",
    )


def test_prepare_final_closure_rejects_expired_approval(tmp_path: Path) -> None:
    handler, payload, _campaign_row = _seed_prepare(
        tmp_path, approved_at=datetime.now(UTC) - timedelta(days=2)
    )
    with pytest.raises(ValueError, match="approval is absent, expired, or changed"):
        handler._prepare_final_closure(payload)


def test_prepare_final_closure_rejects_changed_approval(tmp_path: Path) -> None:
    handler, payload, campaign = _seed_prepare(tmp_path)
    with handler.sessions.begin() as session:
        row = session.get(GitHubBacklogCampaign, campaign.id)
        assert row is not None
        changed = dict(row.approval_json)
        binding = dict(changed["binding"])
        binding["source_head"] = "f" * 40
        changed["binding"] = binding
        row.approval_json = changed
    with pytest.raises(ValueError, match="approval is absent, expired, or changed"):
        handler._prepare_final_closure(payload)


def test_prepare_final_closure_rejects_non_resumable_state(tmp_path: Path) -> None:
    handler, payload, _campaign_row = _seed_prepare(
        tmp_path, state=ChangeSetState.IMPLEMENTING.value
    )
    with pytest.raises(ValueError, match="resumable verified state"):
        handler._prepare_final_closure(payload)


def test_prepare_final_closure_rechecks_current_exact_r3_approval(tmp_path: Path) -> None:
    handler, payload, _campaign_row = _seed_prepare(tmp_path, risk="R3")
    with pytest.raises(ValueError, match="current exact R3 issue approval"):
        handler._prepare_final_closure(payload)


def test_prepare_final_closure_accepts_current_exact_r3_approval(tmp_path: Path) -> None:
    handler, payload, campaign = _seed_prepare(tmp_path, risk="R3")
    now = datetime.now(UTC)
    expires_at = now + timedelta(hours=1)
    with handler.sessions.begin() as session:
        issue = session.scalar(
            select(BacklogIssueRecord).where(BacklogIssueRecord.issue_number == 42)
        )
        assert issue is not None
        candidate_payload = {
            "project": campaign.project,
            "repository": issue.repository,
            "issue_number": issue.issue_number,
            "issue_body_sha256": issue.body_sha256,
            "issue_updated_at": issue.github_updated_at.isoformat(),
            "source_repository": campaign.source_repository,
            "deployment_repository": campaign.deployment_repository,
            "source_branch": campaign.source_default_branch,
            "deployment_branch": campaign.deployment_default_branch,
            "source_head": campaign.source_head_at_import,
            "deployment_head": campaign.deployment_head_at_import,
            "environment": "staging",
            "production": False,
            "delivery_adapter": "synthetic:test",
            "expires_at": expires_at.isoformat(),
        }
        candidate_binding = canonical_sha256(candidate_payload)
        session.add(
            ActionCandidate(
                action="r3_issue_approval",
                project="falcone",
                target_type="github_issue",
                target_id=f"{issue.repository}#{issue.issue_number}",
                payload_json={
                    **candidate_payload,
                    "approved_by": "telegram:42",
                    "approved_at": now.isoformat(),
                },
                capability_status="AVAILABLE",
                blockers_json=[],
                binding_hash=candidate_binding,
                confirmation_phrase=f"APPROVE_R3_FALCONE_42_{candidate_binding[:16].upper()}",
                session_binding_hash="6" * 64,
                created_at=now,
                expires_at=expires_at,
                used_at=now,
            )
        )
    payload["r3_approval_binding"] = candidate_binding
    assert handler._prepare_final_closure(payload)["status"] == "FINAL_CLOSURE_READY"
    with handler.sessions.begin() as session:
        candidate = session.scalar(
            select(ActionCandidate).where(ActionCandidate.binding_hash == candidate_binding)
        )
        assert candidate is not None
        candidate.payload_json = {**candidate.payload_json, "delivery_adapter": "tampered"}
    with pytest.raises(ValueError, match="current exact R3 issue approval"):
        handler._prepare_final_closure(payload)


def test_prepare_final_closure_rejects_a_different_current_r3_binding(tmp_path: Path) -> None:
    handler, payload, campaign = _seed_prepare(tmp_path, risk="R3")
    now = datetime.now(UTC)
    with handler.sessions.begin() as session:
        issue = session.scalar(
            select(BacklogIssueRecord).where(BacklogIssueRecord.issue_number == 42)
        )
        assert issue is not None
        session.add(
            ActionCandidate(
                action="r3_issue_approval",
                project="falcone",
                target_type="github_issue",
                target_id=f"{issue.repository}#{issue.issue_number}",
                payload_json={
                    "project": campaign.project,
                    "repository": issue.repository,
                    "issue_number": issue.issue_number,
                    "issue_body_sha256": issue.body_sha256,
                    "issue_updated_at": issue.github_updated_at.isoformat(),
                    "source_repository": campaign.source_repository,
                    "deployment_repository": campaign.deployment_repository,
                    "source_branch": campaign.source_default_branch,
                    "deployment_branch": campaign.deployment_default_branch,
                    "source_head": campaign.source_head_at_import,
                    "deployment_head": campaign.deployment_head_at_import,
                    "environment": "staging",
                    "production": False,
                    "approved_by": "telegram:42",
                },
                capability_status="AVAILABLE",
                blockers_json=[],
                binding_hash="5" * 64,
                confirmation_phrase="APPROVE_R3_FALCONE_42_5555555555555555",
                session_binding_hash="6" * 64,
                created_at=now,
                expires_at=now + timedelta(hours=1),
                used_at=now,
            )
        )
    payload["r3_approval_binding"] = "7" * 64
    with pytest.raises(ValueError, match="current exact R3 issue approval"):
        handler._prepare_final_closure(payload)


def test_deployment_merge_authorization_reloads_the_exact_persisted_plan(
    tmp_path: Path,
) -> None:
    handler, payload, _campaign = _seed_prepare(tmp_path)
    prepared = handler._prepare_final_closure(payload)
    plan = FinalClosurePlan.model_validate(prepared["final_closure_plan"])
    deployment_plan = FinalDeploymentMergePlan(
        closure=plan,
        deployment=plan.deployment,
        independent_review_sha256="5" * 64,
    )
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, plan.change_set_id)
        assert change_set is not None
        recovery = dict(change_set.recovery_json)
        closure = dict(recovery["final_closure"])
        closure["final_deployment_merge_plan"] = deployment_plan.model_dump(mode="json")
        recovery["final_closure"] = closure
        change_set.recovery_json = recovery
        change_set.state = ChangeSetState.DEPLOY_PR.value
    authorized = handler._authorize_final_deployment_merge(
        {
            **prepared,
            "status": "DEPLOY_PR",
            "final_deployment_merge_plan": deployment_plan.model_dump(mode="json"),
        }
    )
    assert authorized["status"] == "FINAL_DEPLOYMENT_MERGE_AUTHORIZED"
    assert len(authorized["deployment_merge_authorization_sha256"]) == 64
    changed = deployment_plan.model_copy(update={"independent_review_sha256": "6" * 64})
    with pytest.raises(ValueError, match="persisted final deployment merge authorization changed"):
        handler._authorize_final_deployment_merge(
            {
                **prepared,
                "status": "DEPLOY_PR",
                "final_deployment_merge_plan": changed.model_dump(mode="json"),
            }
        )


def test_prepare_final_closure_resumes_after_persisted_deployment_merge(
    tmp_path: Path,
) -> None:
    handler, payload, _campaign = _seed_prepare(tmp_path)
    prepared = handler._prepare_final_closure(payload)
    plan = FinalClosurePlan.model_validate(prepared["final_closure_plan"])
    source_merge = _merge(plan, source=True)
    deployment_merge = _merge(plan, source=False)
    deployment_plan = FinalDeploymentMergePlan(
        closure=plan,
        deployment=plan.deployment,
        independent_review_sha256="5" * 64,
    )
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, plan.change_set_id)
        assert change_set is not None
        recovery = dict(change_set.recovery_json)
        closure = dict(recovery["final_closure"])
        closure.update(
            {
                "source_merge": source_merge.model_dump(mode="json"),
                "final_deployment_merge_plan": deployment_plan.model_dump(mode="json"),
                "deployment_merge": deployment_merge.model_dump(mode="json"),
            }
        )
        recovery["final_closure"] = closure
        change_set.recovery_json = recovery
        change_set.source_head_sha = source_merge.merge_sha
        change_set.deployment_head_sha = deployment_merge.merge_sha
        change_set.state = ChangeSetState.DEPLOY_PR.value

    resumed = handler._prepare_final_closure(payload)
    assert resumed["status"] == "FINAL_DEPLOYMENT_MERGE_PERSISTED"
    assert resumed["source_merge"]["merge_sha"] == source_merge.merge_sha
    assert resumed["deployment_merge"]["merge_sha"] == deployment_merge.merge_sha


@pytest.mark.parametrize(
    ("variant", "error"),
    [
        ("missing-runtime", "runtime, cleanup, or immutable revision"),
        ("wrong-revision", "runtime, cleanup, or immutable revision"),
        ("active-agent", "runtime, cleanup, or immutable revision"),
        ("failed-evidence", "final closure evidence gate"),
    ],
)
def test_prepare_final_issue_close_rejects_missing_or_active_evidence(
    tmp_path: Path, variant: str, error: str
) -> None:
    handler = MetadataActivityHandler(_settings(tmp_path))
    Base.metadata.create_all(handler.engine)
    plan = _plan()
    source = _merge(plan, source=True)
    deployment_merge = _merge(plan, source=False)
    deployment_id = uuid4()
    recovery = {
        "final_closure": {
            "plan": plan.model_dump(mode="json"),
            "final_artifact": {"manifest_sha256": "7" * 64},
        }
    }
    with handler.sessions.begin() as session:
        change_set = _change_set(
            plan.change_set_id,
            source_head=source.merge_sha,
            deployment_head=deployment_merge.merge_sha,
            recovery=recovery,
        )
        change_set.image_digests = {"api": "sha256:" + "8" * 64}
        session.add(change_set)
        session.add(
            Run(
                id=plan.run_id,
                campaign_id=None,
                change_set_id=plan.change_set_id,
                workflow_id=plan.workflow_id,
                run_type="final-closure",
                status="RUNNING",
                project="falcone",
            )
        )
        session.add(_issue(plan.backlog_campaign_id, plan.change_set_id))
        session.add(
            Deployment(
                id=deployment_id,
                change_set_id=plan.change_set_id,
                project="falcone",
                environment="staging",
                strategy="helm",
                target_context="staging",
                target_namespace="falcone",
                desired_sha=(
                    "0" * 40 if variant == "wrong-revision" else deployment_merge.merge_sha
                ),
                status="RUNTIME_VERIFIED",
            )
        )
        if variant != "missing-runtime":
            evidence = _runtime_evidence()
            if variant == "failed-evidence":
                evidence["browser"] = {"status": "FAIL"}
            session.add(
                VerificationResult(
                    change_set_id=plan.change_set_id,
                    deployment_id=deployment_id,
                    run_id=plan.run_id,
                    verification_type="post-staging-runtime",
                    verifier_provider="claude",
                    status="RUNTIME_VERIFIED",
                    evidence_json=evidence,
                )
            )
        for repository in ("falcone", "falcone-charts"):
            session.add(
                WorktreeLease(
                    change_set_id=plan.change_set_id,
                    repository=repository,
                    worktree_path=str(tmp_path / repository),
                    branch=plan.source.head_branch,
                    owner_run_id=plan.run_id,
                    base_sha="a" * 40,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                    released_at=None,
                )
            )
        if variant == "active-agent":
            session.add(
                AgentExecution(
                    workflow_id=plan.workflow_id,
                    temporal_run_id=None,
                    activity_id=None,
                    project="falcone",
                    repository_full_name=plan.source.repository,
                    issue_number=42,
                    campaign_id=None,
                    change_set_id=plan.change_set_id,
                    provider="codex",
                    role="maker",
                    host="worker",
                    worker_kind="codex",
                    task_queue="codex",
                    status="RUNNING",
                    phase="final",
                    selection_reason="test",
                    cli_path_identifier="codex",
                    cli_version="test",
                    attempt=1,
                    max_attempts=1,
                    started_at=datetime.now(UTC),
                    last_heartbeat_at=datetime.now(UTC),
                    ended_at=None,
                    exit_code=None,
                    retryable=None,
                    blocker_code=None,
                    error_summary=None,
                    output_truncated=False,
                    transcript_incomplete=False,
                    total_log_bytes=0,
                    redaction_count=0,
                )
            )
    payload = {
        "status": "FINAL_WORKTREES_SYNCHRONIZED",
        "final_closure_plan": plan.model_dump(mode="json"),
        "source_merge": source.model_dump(mode="json"),
        "deployment_merge": deployment_merge.model_dump(mode="json"),
        "deployment_id": str(deployment_id),
        "final_worktree_sync": [
            {
                "repository": source.repository,
                "merge_sha": source.merge_sha,
                "merge_tree_sha": source.merge_tree_sha,
                "clean": True,
            },
            {
                "repository": deployment_merge.repository,
                "merge_sha": deployment_merge.merge_sha,
                "merge_tree_sha": deployment_merge.merge_tree_sha,
                "clean": True,
            },
        ],
    }
    with pytest.raises(ValueError, match=error):
        handler._prepare_final_issue_close(payload)


def test_final_persistence_rejects_invalid_source_artifact_and_deployment_states(
    tmp_path: Path,
) -> None:
    handler = MetadataActivityHandler(_settings(tmp_path))
    Base.metadata.create_all(handler.engine)
    plan = _plan()
    source = _merge(plan, source=True)
    with handler.sessions.begin() as session:
        session.add(
            _change_set(
                plan.change_set_id,
                state=ChangeSetState.IMPLEMENTING.value,
                source_head=plan.source.head_sha,
                deployment_head=plan.deployment.head_sha,
            )
        )
        session.add(
            Run(
                id=plan.run_id,
                campaign_id=None,
                change_set_id=plan.change_set_id,
                workflow_id=plan.workflow_id,
                run_type="final-closure",
                status="RUNNING",
                project="falcone",
            )
        )
    with pytest.raises(ValueError, match="state changed before persistence"):
        handler._persist_final_source_merge(
            {
                "status": "SOURCE_MERGED",
                "final_closure_plan": plan.model_dump(mode="json"),
                "source_merge": source.model_dump(mode="json"),
            }
        )

    artifact_plan = FinalArtifactPlan(
        closure=plan,
        final_source_sha=source.merge_sha,
        policy=plan.policy,
    )
    manifest = CandidateArtifactManifest(
        schema_version=1,
        project="falcone",
        source_repository="gntik-ai/falcone",
        source_sha=source.merge_sha,
        workflow_run_id=99,
        images=(
            {
                "component": "api",
                "repository": "ghcr.io/gntik-ai/falcone",
                "digest": "sha256:" + "8" * 64,
            },
        ),
    )
    with pytest.raises(ValueError, match="artifact persistence binding"):
        handler._persist_final_artifact(
            {
                "status": "FINAL_ARTIFACT_VERIFIED",
                "final_artifact_plan": artifact_plan.model_dump(mode="json"),
                "final_artifact_manifest": manifest.model_dump(mode="json"),
                "final_build_run_id": 99,
                "final_artifact_evidence": {
                    "manifest_sha256": "7" * 64,
                    "attestation_sha256": {"api": "6" * 64},
                },
                "independent_review_sha256": "4" * 64,
            }
        )

    reviewed = plan.deployment.model_copy(update={"head_sha": "4" * 40})
    deployment_plan = FinalDeploymentMergePlan(
        closure=plan,
        deployment=reviewed,
        independent_review_sha256="5" * 64,
    )
    deployment_merge = ProtectedMergeResult(
        repository=reviewed.repository,
        pull_request_number=reviewed.number,
        pull_request_url=reviewed.url,
        head_sha=reviewed.head_sha,
        merge_sha="e" * 40,
        merge_tree_sha="9" * 40,
        state="MERGED",
    )
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, plan.change_set_id)
        assert change_set is not None
        change_set.deployment_head_sha = reviewed.head_sha
    with pytest.raises(ValueError, match="deployment merge persistence binding"):
        handler._persist_final_deployment_merge(
            {
                "status": "DEPLOYMENT_MERGED",
                "final_closure_plan": plan.model_dump(mode="json"),
                "final_deployment_merge_plan": deployment_plan.model_dump(mode="json"),
                "deployment_merge": deployment_merge.model_dump(mode="json"),
            }
        )

    changed_review = deployment_plan.model_copy(update={"independent_review_sha256": "6" * 64})
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, plan.change_set_id)
        assert change_set is not None
        change_set.state = ChangeSetState.DEPLOY_PR.value
        change_set.recovery_json = {
            **change_set.recovery_json,
            "final_closure": {
                "final_deployment_merge_plan": changed_review.model_dump(mode="json")
            },
        }
    with pytest.raises(ValueError, match="persisted final deployment merge plan changed"):
        handler._persist_final_deployment_merge(
            {
                "status": "DEPLOYMENT_MERGED",
                "final_closure_plan": plan.model_dump(mode="json"),
                "final_deployment_merge_plan": deployment_plan.model_dump(mode="json"),
                "deployment_merge": deployment_merge.model_dump(mode="json"),
            }
        )


class _DirtyRunner:
    def run(self, *_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(stdout=" M tracked.txt\n")


class _NoCloseBroker:
    authenticated_home = Path("/nonexistent")

    def __init__(self) -> None:
        self.calls = 0

    def close_issue(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {}

    def merge_pull_request(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {}


def test_deployment_merge_rejects_missing_metadata_authorization_without_broker_call(
    tmp_path: Path,
) -> None:
    plan = _plan()
    deployment_plan = FinalDeploymentMergePlan(
        closure=plan,
        deployment=plan.deployment,
        independent_review_sha256="5" * 64,
    )
    broker = _NoCloseBroker()
    handler = DeliveryActivityHandler(
        _settings(tmp_path),
        runner=_DirtyRunner(),  # type: ignore[arg-type]
        broker=broker,  # type: ignore[arg-type]
    )
    result = handler._merge_bound_pull_request(
        {
            "status": "DEPLOY_PR",
            "final_closure_plan": plan.model_dump(mode="json"),
            "final_deployment_merge_plan": deployment_plan.model_dump(mode="json"),
        },
        source=False,
    )
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "PROTECTED_DEPLOYMENT_MERGE_FAILED"
    assert broker.calls == 0


def test_final_worktree_sync_rejects_dirty_worktree(tmp_path: Path) -> None:
    plan = _plan()
    for slug in ("falcone", "falcone-charts"):
        (tmp_path / "worktrees" / slug / str(plan.change_set_id)).mkdir(parents=True)
    handler = DeliveryActivityHandler(
        _settings(tmp_path),
        runner=_DirtyRunner(),  # type: ignore[arg-type]
        broker=_NoCloseBroker(),  # type: ignore[arg-type]
    )
    result = handler._sync_final_worktrees(
        {
            "final_closure_plan": plan.model_dump(mode="json"),
            "source_merge": _merge(plan, source=True).model_dump(mode="json"),
            "deployment_merge": _merge(plan, source=False).model_dump(mode="json"),
        }
    )
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "FINAL_WORKTREE_SYNCHRONIZATION_FAILED"


def test_final_worktree_sync_rejects_symlink_escape(tmp_path: Path) -> None:
    plan = _plan()
    outside = tmp_path / "outside"
    outside.mkdir()
    path = tmp_path / "worktrees" / "falcone" / str(plan.change_set_id)
    path.parent.mkdir(parents=True)
    path.symlink_to(outside, target_is_directory=True)
    handler = DeliveryActivityHandler(
        _settings(tmp_path),
        runner=_DirtyRunner(),  # type: ignore[arg-type]
        broker=_NoCloseBroker(),  # type: ignore[arg-type]
    )
    result = handler._sync_final_worktrees(
        {
            "final_closure_plan": plan.model_dump(mode="json"),
            "source_merge": _merge(plan, source=True).model_dump(mode="json"),
            "deployment_merge": _merge(plan, source=False).model_dump(mode="json"),
        }
    )
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "FINAL_WORKTREE_SYNCHRONIZATION_FAILED"


def test_close_final_issue_requires_persisted_evidence_without_broker_call(
    tmp_path: Path,
) -> None:
    plan = _plan()
    issue_plan = FinalIssueMutationPlan(
        closure=plan,
        final_source_sha="d" * 40,
        final_deployment_sha="e" * 40,
        deployment_id=uuid4(),
        evidence_sha256="7" * 64,
        comment_body=(f"<!-- engineering-control-final-closure:{plan.change_set_id} -->\nverified"),
    )
    broker = _NoCloseBroker()
    handler = DeliveryActivityHandler(
        _settings(tmp_path),
        runner=_DirtyRunner(),  # type: ignore[arg-type]
        broker=broker,  # type: ignore[arg-type]
    )
    result = handler._close_final_issue(
        {"status": "FINAL_EVIDENCE_READY", "final_issue_plan": issue_plan.model_dump(mode="json")}
    )
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "FINAL_ISSUE_CLOSE_FAILED"
    assert broker.calls == 0
