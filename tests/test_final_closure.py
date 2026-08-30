from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from engineering_control.activities.delivery import DeliveryActivityHandler
from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.adapters.command import CommandResult
from engineering_control.adapters.github.client import GhCliBroker, GhCliPolicyError
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import (
    BacklogIssueRecord,
    ChangeSet,
    Deployment,
    GitHubBacklogCampaign,
    VerificationResult,
    WorktreeLease,
)
from engineering_control.engineering.backlog import (
    BACKLOG_APPROVAL_PHRASE,
    DocumentationImpact,
    approval_binding,
)
from engineering_control.models.final_closure import FinalClosurePlan, FinalDeploymentMergePlan
from engineering_control.models.reconciliation import DeliveryControlPolicy
from engineering_control.workflows import definitions as workflow_definitions
from engineering_control.workflows.definitions import FinalClosureWorkflow
from engineering_control.workflows.inputs import FinalClosureInput


def closure_plan() -> dict[str, Any]:
    change_set_id = uuid4()
    branch = f"agent/falcone/issue/{change_set_id}"
    policy = DeliveryControlPolicy.model_validate(
        {
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
        }
    )
    return FinalClosurePlan(
        change_set_id=change_set_id,
        run_id=uuid4(),
        workflow_id=f"final-{change_set_id}",
        backlog_campaign_id=uuid4(),
        approval_binding_sha256="a" * 64,
        project="falcone",
        issue_repository="gntik-ai/falcone",
        issue_number=42,
        source={
            "repository": "gntik-ai/falcone",
            "number": 10,
            "url": "https://github.com/gntik-ai/falcone/pull/10",
            "head_branch": branch,
            "head_sha": "b" * 40,
        },
        deployment={
            "repository": "gntik-ai/falcone-charts",
            "number": 11,
            "url": "https://github.com/gntik-ai/falcone-charts/pull/11",
            "head_branch": branch,
            "head_sha": "c" * 40,
        },
        maker_provider="codex",
        checker_provider="claude",
        documentation_impact=DocumentationImpact.DOCUMENTATION_NOT_REQUIRED_WITH_EVIDENCE,
        policy=policy,
        production=False,
    ).model_dump(mode="json")


def test_final_closure_plan_rejects_provider_or_repository_drift() -> None:
    value = closure_plan()
    value["checker_provider"] = "codex"
    with pytest.raises(ValueError, match="opposite native providers"):
        FinalClosurePlan.model_validate(value)


def test_final_deployment_merge_plan_binds_the_reviewed_pr_identity() -> None:
    closure = closure_plan()
    promoted = {**closure["deployment"], "head_sha": "d" * 40}
    plan = FinalDeploymentMergePlan(
        closure=closure,
        deployment=promoted,
        independent_review_sha256="e" * 64,
    )
    assert plan.deployment.head_sha == "d" * 40

    changed_pr = plan.model_dump(mode="json")
    changed_pr["deployment"]["number"] = 12
    changed_pr["deployment"]["url"] = "https://github.com/gntik-ai/falcone-charts/pull/12"
    with pytest.raises(ValueError, match="protected binding"):
        FinalDeploymentMergePlan.model_validate(changed_pr)

    value = closure_plan()
    value["deployment"]["repository"] = "gntik-ai/musematic-deploy"
    value["deployment"]["url"] = "https://github.com/gntik-ai/musematic-deploy/pull/11"
    with pytest.raises(ValueError, match="project pair"):
        FinalClosurePlan.model_validate(value)


def test_broker_merges_only_the_exact_bound_head(tmp_path: Any, monkeypatch: Any) -> None:
    broker = GhCliBroker(object(), tmp_path, writes_enabled=True)  # type: ignore[arg-type]
    current = [
        {
            "number": 10,
            "url": "https://github.com/gntik-ai/falcone/pull/10",
            "state": "OPEN",
            "isDraft": False,
            "headRefOid": "b" * 40,
            "baseRefName": "main",
            "mergeStateStatus": "CLEAN",
        },
        {
            "number": 10,
            "url": "https://github.com/gntik-ai/falcone/pull/10",
            "state": "MERGED",
            "isDraft": False,
            "headRefOid": "b" * 40,
            "baseRefName": "main",
            "mergeStateStatus": "UNKNOWN",
            "mergeCommit": {"oid": "d" * 40},
        },
    ]
    calls: list[list[str]] = []
    monkeypatch.setattr(broker, "_write", lambda repository, _binding: repository)
    monkeypatch.setattr(broker, "view_pull_request", lambda _repository, _number: current.pop(0))
    monkeypatch.setattr(broker, "branch_commit", lambda _repository, _sha: {"tree": "e" * 40})
    monkeypatch.setattr(
        broker,
        "_run",
        lambda arguments: calls.append(arguments) or CommandResult(0, "", ""),
    )

    result = broker.merge_pull_request(
        repository="gntik-ai/falcone",
        number=10,
        expected_head_sha="b" * 40,
        expected_base_branch="main",
        change_binding=f"changeset:{uuid4()}",
    )

    assert result["merge_sha"] == "d" * 40
    assert calls[0][-2:] == ["--match-head-commit", "b" * 40]


def test_broker_rejects_changed_head_before_merge(tmp_path: Any, monkeypatch: Any) -> None:
    broker = GhCliBroker(object(), tmp_path, writes_enabled=True)  # type: ignore[arg-type]
    monkeypatch.setattr(broker, "_write", lambda repository, _binding: repository)
    monkeypatch.setattr(
        broker,
        "view_pull_request",
        lambda _repository, _number: {"headRefOid": "f" * 40},
    )
    with pytest.raises(GhCliPolicyError, match="head or base changed"):
        broker.merge_pull_request(
            repository="gntik-ai/falcone",
            number=10,
            expected_head_sha="b" * 40,
            expected_base_branch="main",
            change_binding=f"changeset:{uuid4()}",
        )


def test_broker_rejects_changed_base_before_merge(tmp_path: Any, monkeypatch: Any) -> None:
    broker = GhCliBroker(object(), tmp_path, writes_enabled=True)  # type: ignore[arg-type]
    monkeypatch.setattr(broker, "_write", lambda repository, _binding: repository)
    monkeypatch.setattr(
        broker,
        "view_pull_request",
        lambda _repository, _number: {
            "headRefOid": "b" * 40,
            "baseRefName": "release",
        },
    )
    with pytest.raises(GhCliPolicyError, match="head or base changed"):
        broker.merge_pull_request(
            repository="gntik-ai/falcone",
            number=10,
            expected_head_sha="b" * 40,
            expected_base_branch="main",
            change_binding=f"changeset:{uuid4()}",
        )


def test_broker_rejects_leading_option_in_typed_value() -> None:
    with pytest.raises(GhCliPolicyError, match="forbidden"):
        GhCliBroker._value("--repo", "workflow name", maximum=160)


def test_broker_refuses_issue_close_without_persisted_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = GhCliBroker(object(), tmp_path, writes_enabled=True)  # type: ignore[arg-type]
    calls: list[list[str]] = []
    monkeypatch.setattr(broker, "_write", lambda repository, _binding: repository)
    monkeypatch.setattr(
        broker,
        "view_issue",
        lambda _repository, _number: {"state": "OPEN", "comments": []},
    )
    monkeypatch.setattr(broker, "_run", lambda arguments: calls.append(arguments))

    with pytest.raises(GhCliPolicyError, match="persisted evidence comment"):
        broker.close_issue(
            repository="gntik-ai/falcone",
            number=42,
            marker=f"<!-- engineering-control-final-closure:{uuid4()} -->",
            change_binding=f"changeset:{uuid4()}",
        )
    assert calls == []


def test_final_worktree_sync_rejects_symlink_before_git(
    tmp_path: Path,
) -> None:
    plan = closure_plan()
    worktree_root = tmp_path / "worktrees"
    source = worktree_root / "falcone" / plan["change_set_id"]
    source.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    source.symlink_to(outside, target_is_directory=True)
    handler = DeliveryActivityHandler(
        Settings(state_root=tmp_path / "state", worktree_root=worktree_root)
    )

    def merged(binding: dict[str, Any], merge_sha: str, tree_sha: str) -> dict[str, Any]:
        return {
            "repository": binding["repository"],
            "pull_request_number": binding["number"],
            "pull_request_url": binding["url"],
            "head_sha": binding["head_sha"],
            "merge_sha": merge_sha,
            "merge_tree_sha": tree_sha,
            "state": "MERGED",
        }

    result = handler._sync_final_worktrees(
        {
            "final_closure_plan": plan,
            "source_merge": merged(plan["source"], "d" * 40, "e" * 40),
            "deployment_merge": merged(plan["deployment"], "f" * 40, "1" * 40),
        }
    )

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "FINAL_WORKTREE_SYNCHRONIZATION_FAILED"


def test_final_closure_database_gates_require_runtime_and_persisted_comment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'closure.sqlite'}", state_root=tmp_path)
    )
    Base.metadata.create_all(handler.engine)
    campaign_id = uuid4()
    change_set_id = uuid4()
    issue_record_id = uuid4()
    finding_id = uuid4()
    branch = f"agent/falcone/issue/{change_set_id}"
    source_head = "b" * 40
    deployment_head = "c" * 40
    source_merge_sha = "d" * 40
    deployment_merge_sha = "e" * 40
    binding = approval_binding(
        campaign_id=str(campaign_id),
        project="falcone",
        import_filter="all",
        source_head="1" * 40,
        deployment_head="2" * 40,
        dependency_plan_hash="3" * 64,
        maximum_risk="R2",
        maximum_items=1,
        delivery_mode="full-delivery",
        include_enhancements=False,
    )
    policy = DeliveryControlPolicy.model_validate(closure_plan()["policy"])
    monkeypatch.setattr(handler, "_delivery_control_policy", lambda _project: policy)
    with handler.sessions.begin() as session:
        session.add(
            GitHubBacklogCampaign(
                id=campaign_id,
                project="falcone",
                source_repository="gntik-ai/falcone",
                deployment_repository="gntik-ai/falcone-charts",
                imported_at=datetime.now(UTC),
                issue_query="is:issue is:open",
                import_filter="all",
                source_default_branch="main",
                deployment_default_branch="main",
                source_head_at_import="1" * 40,
                deployment_head_at_import="2" * 40,
                issue_count=1,
                eligible_count=1,
                deferred_count=0,
                duplicate_count=0,
                stale_count=0,
                already_fixed_count=0,
                existing_pr_count=0,
                blocked_count=0,
                dependency_graph_hash="3" * 64,
                plan_hash="3" * 64,
                approval_binding=binding.sha256(),
                approval_json={
                    "binding": binding.model_dump(mode="json"),
                    "policy_role": "owner",
                    "approval": {
                        "approved_by": "telegram:42",
                        "approved_at": datetime.now(UTC).isoformat(),
                        "typed_confirmation": BACKLOG_APPROVAL_PHRASE,
                    },
                },
                maximum_risk="R2",
                maximum_items=1,
                maximum_concurrency=1,
                delivery_mode="full-delivery",
                include_enhancements=False,
                status="APPROVED",
                workflow_id=f"backlog-import-{campaign_id}",
            )
        )
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=finding_id,
                issue_id=None,
                project="falcone",
                risk_class="R2",
                state="RUNTIME_VERIFIED",
                version=1,
                source_repository="falcone",
                source_branch=branch,
                source_base_sha="4" * 40,
                source_head_sha=source_head,
                source_pr_url="https://github.com/gntik-ai/falcone/pull/10",
                deployment_repository="falcone-charts",
                deployment_branch=branch,
                deployment_base_sha="5" * 40,
                deployment_head_sha=deployment_head,
                deployment_pr_url="https://github.com/gntik-ai/falcone-charts/pull/11",
                image_digests={"api": "sha256:" + "6" * 64},
                chart_version=None,
                recovery_json={
                    "provider_chain": {"maker": "codex", "checker": "claude"},
                    "release_evidence": {"independent_review_sha256": "7" * 64},
                },
            )
        )
        session.add(
            BacklogIssueRecord(
                id=issue_record_id,
                backlog_campaign_id=campaign_id,
                repository="gntik-ai/falcone",
                issue_number=42,
                issue_url_reference="https://github.com/gntik-ai/falcone/issues/42",
                title="bounded final closure",
                state="OPEN",
                labels_json=["bug"],
                milestone=None,
                issue_type="PRODUCT_BUG",
                openspec_kind=None,
                severity="high",
                calculated_risk="R2",
                component="runtime",
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
                finding_id=finding_id,
                change_set_id=change_set_id,
                delivery_status="RUNTIME_VERIFIED",
                final_closure_status="OPEN",
                documentation_impact=(
                    DocumentationImpact.DOCUMENTATION_NOT_REQUIRED_WITH_EVIDENCE.value
                ),
                body_sha256="8" * 64,
                github_updated_at=datetime.now(UTC),
                github_snapshot_json={"state": "OPEN"},
            )
        )
    workflow_id = f"final-{change_set_id}"
    prepared = handler._prepare_final_closure(
        {
            "backlog_campaign_id": str(campaign_id),
            "change_set_id": str(change_set_id),
            "project": "falcone",
            "issue_repository": "gntik-ai/falcone",
            "issue_number": 42,
            "orchestration": {"workflow_id": workflow_id},
        }
    )
    assert prepared["status"] == "FINAL_CLOSURE_READY"
    plan = FinalClosurePlan.model_validate(prepared["final_closure_plan"])
    deployment_id = uuid4()
    now = datetime.now(UTC)
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, change_set_id)
        assert change_set is not None
        change_set.source_head_sha = source_merge_sha
        change_set.deployment_head_sha = deployment_merge_sha
        recovery = dict(change_set.recovery_json)
        closure = dict(recovery["final_closure"])
        closure["final_artifact"] = {"manifest_sha256": "9" * 64}
        recovery["final_closure"] = closure
        change_set.recovery_json = recovery
        session.add(
            Deployment(
                id=deployment_id,
                change_set_id=change_set_id,
                project="falcone",
                environment="staging",
                strategy="helm",
                target_context="staging",
                target_namespace="in-falcone-staging",
                desired_sha=deployment_merge_sha,
                status="RUNTIME_VERIFIED",
                transaction_id="final-test",
                started_at=now,
                finished_at=now,
                recovery_json={},
            )
        )
        for repository in ("falcone", "falcone-charts"):
            session.add(
                WorktreeLease(
                    change_set_id=change_set_id,
                    repository=repository,
                    worktree_path=str(tmp_path / "worktrees" / repository / str(change_set_id)),
                    branch=branch,
                    owner_run_id=plan.run_id,
                    base_sha="a" * 40,
                    expires_at=now + timedelta(hours=1),
                    released_at=None,
                )
            )

    def merge(
        repository: str,
        number: int,
        head_sha: str,
        merge_sha: str,
        tree_sha: str,
    ) -> dict[str, Any]:
        return {
            "repository": repository,
            "pull_request_number": number,
            "pull_request_url": f"https://github.com/{repository}/pull/{number}",
            "head_sha": head_sha,
            "merge_sha": merge_sha,
            "merge_tree_sha": tree_sha,
            "state": "MERGED",
        }

    source_merge = merge("gntik-ai/falcone", 10, source_head, source_merge_sha, "a" * 40)
    deployment_merge = merge(
        "gntik-ai/falcone-charts", 11, deployment_head, deployment_merge_sha, "b" * 40
    )
    close_input = {
        **prepared,
        "status": "FINAL_WORKTREES_SYNCHRONIZED",
        "deployment_id": str(deployment_id),
        "source_merge": source_merge,
        "deployment_merge": deployment_merge,
        "final_worktree_sync": [
            {
                "repository": source_merge["repository"],
                "merge_sha": source_merge_sha,
                "merge_tree_sha": "a" * 40,
                "clean": True,
            },
            {
                "repository": deployment_merge["repository"],
                "merge_sha": deployment_merge_sha,
                "merge_tree_sha": "b" * 40,
                "clean": True,
            },
        ],
    }
    with pytest.raises(ValueError, match="runtime, cleanup, or immutable revision"):
        handler._prepare_final_issue_close(close_input)
    with handler.sessions.begin() as session:
        session.add(
            VerificationResult(
                change_set_id=change_set_id,
                deployment_id=deployment_id,
                run_id=plan.run_id,
                verification_type="post-staging-runtime",
                verifier_provider="claude",
                status="RUNTIME_VERIFIED",
                evidence_json={"bounded": True},
            )
        )
    ready = handler._prepare_final_issue_close(close_input)
    assert ready["status"] == "FINAL_ISSUE_CLOSE_READY"
    fake_close = {
        **ready,
        "status": "ISSUE_CLOSED",
        "issue_close_result": {
            "status": "CLOSED",
            "issue_url": "https://github.com/gntik-ai/falcone/issues/42",
            "closed_at": datetime.now(UTC).isoformat(),
        },
    }
    with pytest.raises(ValueError, match="persisted comment evidence"):
        handler._persist_final_issue_close(fake_close)
    commented = handler._persist_final_comment(
        {
            **ready,
            "status": "FINAL_EVIDENCE_COMMENTED",
            "comment_result": {
                "status": "COMMENTED",
                "comment_url": "https://github.com/gntik-ai/falcone/issues/42#issuecomment-1",
            },
        }
    )
    assert commented["status"] == "FINAL_EVIDENCE_PERSISTED"
    adopted = handler._persist_final_comment(
        {
            **ready,
            "status": "FINAL_EVIDENCE_COMMENTED",
            "comment_result": {
                "status": "COMMENT_PRESENT",
                "comment_url": "https://github.com/gntik-ai/falcone/issues/42#issuecomment-1",
            },
        }
    )
    assert adopted["status"] == "FINAL_EVIDENCE_PERSISTED"
    resumed = handler._prepare_final_closure(
        {
            "backlog_campaign_id": str(campaign_id),
            "change_set_id": str(change_set_id),
            "project": "falcone",
            "issue_repository": "gntik-ai/falcone",
            "issue_number": 42,
            "orchestration": {"workflow_id": workflow_id},
        }
    )
    assert resumed["status"] == "FINAL_ISSUE_CLOSE_READY"
    with handler.sessions() as session:
        issue = session.get(BacklogIssueRecord, issue_record_id)
        assert issue is not None
        assert issue.final_closure_status == "FINAL_EVIDENCE_PERSISTED"
    fixed = handler._persist_final_issue_close(
        {**fake_close, "final_issue_plan": resumed["final_issue_plan"]}
    )
    assert fixed["status"] == "FIXED"


@pytest.mark.asyncio
async def test_final_closure_workflow_requires_final_build_before_other_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = FinalClosureWorkflow()
    instance._checkpoint = lambda _phase: _true()  # type: ignore[method-assign]
    steps: list[str] = []

    async def step(name: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        steps.append(name)
        statuses = {
            "final.prepare": "FINAL_CLOSURE_READY",
            "final.merge_source": "SOURCE_MERGED",
            "final.persist_source_merge": "AWAITING_FINAL_BUILD",
            "final.inspect_artifact": "AWAITING_FINAL_BUILD",
        }
        return {**payload, "status": statuses[name], "blocker": "FINAL_BUILD_NOT_YET_AVAILABLE"}

    instance._step = step  # type: ignore[method-assign]
    instance._github_write_step = step  # type: ignore[method-assign]
    monkeypatch.setattr(workflow_definitions.workflow, "sleep", lambda _delay: _true())
    result = await instance.run(
        FinalClosureInput(
            backlog_campaign_id=str(uuid4()),
            change_set_id=str(uuid4()),
            finding_id=str(uuid4()),
            project="falcone",
            issue_repository="gntik-ai/falcone",
            issue_number=42,
        )
    )
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "FINAL_BUILD_WAIT_EXHAUSTED"
    assert steps[:3] == [
        "final.prepare",
        "final.merge_source",
        "final.persist_source_merge",
    ]
    assert steps.count("final.inspect_artifact") == 120
    assert "final.merge_deployment" not in steps
    assert "final.close_issue" not in steps


@pytest.mark.asyncio
async def test_final_closure_resumes_only_bounded_issue_comment_and_close() -> None:
    instance = FinalClosureWorkflow()
    instance._checkpoint = lambda _phase: _true()  # type: ignore[method-assign]
    steps: list[str] = []

    async def step(name: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        steps.append(name)
        statuses = {
            "final.prepare": "FINAL_ISSUE_CLOSE_READY",
            "final.comment_issue": "FINAL_EVIDENCE_COMMENTED",
            "final.persist_comment": "FINAL_EVIDENCE_PERSISTED",
            "final.close_issue": "ISSUE_CLOSED",
            "final.persist_issue_close": "FIXED",
        }
        return {**payload, "status": statuses[name]}

    instance._step = step  # type: ignore[method-assign]
    instance._github_write_step = step  # type: ignore[method-assign]

    result = await instance.run(
        FinalClosureInput(
            backlog_campaign_id=str(uuid4()),
            change_set_id=str(uuid4()),
            finding_id=str(uuid4()),
            project="falcone",
            issue_repository="gntik-ai/falcone",
            issue_number=42,
        )
    )

    assert result["status"] == "FIXED"
    assert steps == [
        "final.prepare",
        "final.comment_issue",
        "final.persist_comment",
        "final.close_issue",
        "final.persist_issue_close",
    ]


@pytest.mark.asyncio
async def test_final_closure_orders_verification_before_comment_and_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = FinalClosureWorkflow()
    instance._checkpoint = lambda _phase: _true()  # type: ignore[method-assign]
    steps: list[str] = []

    async def step(name: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        steps.append(name)
        statuses = {
            "final.prepare": "FINAL_CLOSURE_READY",
            "final.merge_source": "SOURCE_MERGED",
            "final.persist_source_merge": "AWAITING_FINAL_BUILD",
            "final.inspect_artifact": "FINAL_ARTIFACT_VERIFIED",
            "final.persist_artifact": "FINAL_ARTIFACT_BUILT",
            "reconcile.prepare_promotion": "PROMOTION_PUBLICATION_READY",
            "reconcile.publish_promotion": "PROMOTION_PUBLISHED",
            "reconcile.persist_promotion_publication": "RELEASE_REVIEW_REQUIRED",
            "reconcile.prepare_release_review": "RELEASE_REVIEW_READY",
            "reconcile.invoke_release_reviewer": "RELEASE_REVIEWED",
            "reconcile.persist_release_review": "DEPLOY_PR",
            "final.merge_deployment": "DEPLOYMENT_MERGED",
            "final.persist_deployment_merge": "FINAL_DEPLOYMENT_MERGE_PERSISTED",
            "final.sync_worktrees": "FINAL_WORKTREES_SYNCHRONIZED",
            "backlog.register_child_run": "REGISTERED",
            "final.prepare_issue_close": "FINAL_ISSUE_CLOSE_READY",
            "final.comment_issue": "FINAL_EVIDENCE_COMMENTED",
            "final.persist_comment": "FINAL_EVIDENCE_PERSISTED",
            "final.close_issue": "ISSUE_CLOSED",
            "final.persist_issue_close": "FIXED",
        }
        result = {**payload, "status": statuses[name]}
        if name == "reconcile.invoke_release_reviewer":
            result["agent_invocation_status"] = "SUCCESS"
        return result

    async def child(name: str, _input: object, **_kwargs: Any) -> dict[str, Any]:
        steps.append(name)
        if name == "StagingDeploymentWorkflow":
            return {
                "status": "STAGING_DEPLOYED",
                "context": {"deployment_id": str(uuid4())},
            }
        assert name == "RuntimeVerificationWorkflow"
        return {"status": "RUNTIME_VERIFIED", "context": {}}

    instance._step = step  # type: ignore[method-assign]
    instance._github_write_step = step  # type: ignore[method-assign]
    instance._agent_step = step  # type: ignore[method-assign]
    monkeypatch.setattr(
        workflow_definitions.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="final-test", task_queue="engineering"),
    )
    monkeypatch.setattr(workflow_definitions.workflow, "execute_child_workflow", child)

    result = await instance.run(
        FinalClosureInput(
            backlog_campaign_id=str(uuid4()),
            change_set_id=str(uuid4()),
            finding_id=str(uuid4()),
            project="falcone",
            issue_repository="gntik-ai/falcone",
            issue_number=42,
        )
    )

    assert result["status"] == "FIXED"
    assert steps.index("RuntimeVerificationWorkflow") < steps.index("final.comment_issue")
    assert steps.index("final.persist_comment") < steps.index("final.close_issue")


async def _true() -> bool:
    return True
