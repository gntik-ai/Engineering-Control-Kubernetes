from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from engineering_control.adapters.command import CommandResult
from engineering_control.adapters.github.client import GhCliBroker, GhCliPolicyError
from engineering_control.engineering.backlog import DocumentationImpact
from engineering_control.models.final_closure import FinalClosurePlan
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
    assert steps == [
        "final.prepare",
        "final.merge_source",
        "final.persist_source_merge",
        "final.inspect_artifact",
    ]
    assert "final.merge_deployment" not in steps
    assert "final.close_issue" not in steps


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
