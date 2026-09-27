from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from temporalio import workflow
from temporalio.converter import DataConverter
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from engineering_control.activities.core import ControlActivities
from engineering_control.activities.delivery import DeliveryActivityHandler
from engineering_control.config import Settings
from engineering_control.policy.github_write import (
    GITHUB_ACTIVITY_STEPS,
    GITOPS_READ_ONLY_GITHUB_STEPS,
    GITOPS_WRITE_CAPABLE_GITHUB_STEPS,
    READ_ONLY_GITHUB_STEPS,
    WRITE_CAPABLE_GITHUB_STEPS,
    GitHubWritePolicyDenial,
    GitHubWritePolicyState,
)
from engineering_control.validation.github_write import (
    DENIAL_ACCEPTANCE_STATUS,
    GitHubWriteDenialAcceptanceWorkflow,
    classify_write_denial,
)
from engineering_control.workflows import definitions
from engineering_control.workflows.definitions import ControlledWorkflow


class NoCommandRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def run(self, executable: str, arguments: list[str], **_: Any) -> Any:
        self.calls.append((executable, tuple(arguments)))
        raise AssertionError("write denial must occur before any external command")


def test_github_activity_classification_is_complete_and_disjoint() -> None:
    assert len(GITHUB_ACTIVITY_STEPS) == 24
    assert len(READ_ONLY_GITHUB_STEPS) == 9
    assert len(WRITE_CAPABLE_GITHUB_STEPS) == 8
    assert len(GITOPS_READ_ONLY_GITHUB_STEPS) == 3
    assert len(GITOPS_WRITE_CAPABLE_GITHUB_STEPS) == 4
    assert READ_ONLY_GITHUB_STEPS.isdisjoint(WRITE_CAPABLE_GITHUB_STEPS)
    assert GITOPS_READ_ONLY_GITHUB_STEPS.isdisjoint(GITOPS_WRITE_CAPABLE_GITHUB_STEPS)
    assert GITHUB_ACTIVITY_STEPS == ControlActivities.steps_for_worker("github")


def write_payloads(root: Path) -> dict[str, dict[str, Any]]:
    change_set_id = uuid4()
    branch = f"agent/falcone/acceptance/{change_set_id}"
    publication = {
        "repository": "gntik-ai/falcone",
        "repository_slug": "falcone",
        "working_directory": str(root / "falcone" / str(change_set_id)),
        "branch": branch,
        "head_sha": "a" * 40,
        "base_branch": "main",
        "title": "bounded acceptance",
        "body": "bounded acceptance",
    }
    chart_publication = {
        **publication,
        "repository": "gntik-ai/falcone-charts",
        "repository_slug": "falcone-charts",
        "working_directory": str(root / "falcone-charts" / str(change_set_id)),
        "head_sha": "b" * 40,
    }
    closure = {
        "change_set_id": str(change_set_id),
        "source": {"repository": "gntik-ai/falcone"},
        "deployment": {"repository": "gntik-ai/falcone-charts"},
    }
    issue_plan = {"closure": {**closure, "issue_repository": "gntik-ai/falcone"}}
    return {
        "delivery.publish": {
            "publication_plan": {
                "change_set_id": str(change_set_id),
                "publications": [publication, chart_publication],
            }
        },
        "finding.publish": {
            "status": "PUBLICATION_READY",
            "finding_publication": {"repository": "gntik-ai/llmwiki"},
        },
        "enhancement.publish": {
            "approval_validated": True,
            "approval_binding": "sha256:" + "a" * 64,
            "affected_repository": "gntik-ai/llmwiki",
        },
        "reconcile.publish_promotion": {
            "promotion_publication": {
                **chart_publication,
                "change_set_id": str(change_set_id),
            }
        },
        "final.merge_source": {"final_closure_plan": closure},
        "final.merge_deployment": {"final_closure_plan": closure},
        "final.comment_issue": {"final_issue_plan": issue_plan},
        "final.close_issue": {"final_issue_plan": issue_plan},
    }


def policy_file(
    root: Path,
    *,
    enabled: bool,
    repositories: tuple[str, ...],
    malformed: bool = False,
    readable: bool = True,
) -> tuple[Path, Path]:
    config_root = root / "config"
    (config_root / "projects.d").mkdir(parents=True, exist_ok=True)
    policy = config_root / "policy/github-issue-write-validation.yaml"
    policy.parent.mkdir(exist_ok=True)
    policy.parent.chmod(0o700)
    content = (
        "not: [valid"
        if malformed
        else f"enabled: {str(enabled).lower()}\n"
        + (
            "safe_repositories:\n" + "".join(f"  - {repository}\n" for repository in repositories)
            if repositories
            else "safe_repositories: []\n"
        )
    )
    policy.write_text(content, encoding="utf-8")
    policy.chmod(0o600 if readable else 0o000)
    return config_root / "projects.d", policy


def outside_payload(step: str, payload: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(payload))
    if step == "delivery.publish":
        value["publication_plan"]["publications"][0]["repository"] = "outside/repository"
    elif step == "finding.publish":
        value["finding_publication"]["repository"] = "outside/repository"
    elif step == "enhancement.publish":
        value["affected_repository"] = "outside/repository"
    elif step == "reconcile.publish_promotion":
        value["promotion_publication"]["repository"] = "outside/repository"
    elif step in {"final.merge_source", "final.merge_deployment"}:
        key = "source" if step == "final.merge_source" else "deployment"
        value["final_closure_plan"][key]["repository"] = "outside/repository"
    else:
        value["final_issue_plan"]["closure"]["issue_repository"] = "outside/repository"
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("step", sorted(WRITE_CAPABLE_GITHUB_STEPS))
@pytest.mark.parametrize(
    ("case", "reason"),
    (
        ("missing", "POLICY_MISSING"),
        ("unreadable", "POLICY_UNREADABLE"),
        ("malformed", "POLICY_INVALID"),
        ("disabled", "POLICY_DISABLED"),
        ("empty", "EMPTY_SAFE_REPOSITORY_LIST"),
        ("target_absent", "REPOSITORY_NOT_ALLOWLISTED"),
        ("different_repository", "REPOSITORY_NOT_ALLOWLISTED"),
        ("outside_global", "REPOSITORY_NOT_ALLOWLISTED"),
    ),
)
async def test_every_write_activity_returns_terminal_pre_spawn_denial(
    tmp_path: Path, step: str, case: str, reason: str
) -> None:
    config_root = tmp_path / "config"
    project_root = config_root / "projects.d"
    project_root.mkdir(parents=True)
    policy = config_root / "policy/github-issue-write-validation.yaml"
    if case != "missing":
        repositories: tuple[str, ...] = ()
        enabled = case != "disabled"
        malformed = case == "malformed"
        readable = case != "unreadable"
        if case in {"target_absent", "different_repository", "outside_global"}:
            repositories = ("gntik-ai/musematic",)
        project_root, policy = policy_file(
            tmp_path,
            enabled=enabled,
            repositories=repositories,
            malformed=malformed,
            readable=readable,
        )
    runner = NoCommandRunner()
    handler = DeliveryActivityHandler(
        Settings(
            project_config_root=project_root,
            github_issue_write_validation_file=policy,
            github_writes_enabled=True,
            run_root=tmp_path / "runs",
            worktree_root=tmp_path / "worktrees",
        ),
        runner,  # type: ignore[arg-type]
    )
    payload = write_payloads(tmp_path / "worktrees")[step]
    if case == "outside_global":
        payload = outside_payload(step, payload)

    result = await handler.handle(step, payload)

    assert result["status"] == "BLOCKED_BY_POLICY"
    assert result["terminal"] is True
    assert result["retryable"] is False
    assert result["reason_code"] == reason
    assert result["external_command_started"] is False
    assert result["external_mutation_attempted"] is False
    assert result["mutating_command_constructed"] is False
    assert result["external_command_count"] == 0
    assert result["external_mutation_attempt_count"] == 0
    assert result["mutating_command_construction_count"] == 0
    assert runner.calls == []


@pytest.mark.parametrize("step", sorted(WRITE_CAPABLE_GITHUB_STEPS))
def test_valid_synthetic_policy_allows_only_exact_typed_targets(tmp_path: Path, step: str) -> None:
    payload = write_payloads(tmp_path / "worktrees")[step]
    target_handler = DeliveryActivityHandler
    targets = target_handler._write_repositories(step, payload)
    project_root, policy = policy_file(tmp_path, enabled=True, repositories=targets)
    runner = NoCommandRunner()
    handler = DeliveryActivityHandler(
        Settings(
            project_config_root=project_root,
            github_issue_write_validation_file=policy,
            github_writes_enabled=True,
            run_root=tmp_path / "runs",
            worktree_root=tmp_path / "worktrees",
        ),
        runner,  # type: ignore[arg-type]
    )

    decision = handler.write_policy.evaluate(lambda: handler._write_repositories(step, payload))

    assert decision is None
    assert runner.calls == []


@pytest.mark.asyncio
async def test_github_worker_routes_enhancement_write_to_policy_before_generic_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root, policy = policy_file(tmp_path, enabled=False, repositories=())
    monkeypatch.setattr("engineering_control.activities.core.activity.heartbeat", lambda _: None)
    activities = ControlActivities(
        Settings(
            project_config_root=project_root,
            github_issue_write_validation_file=policy,
            github_writes_enabled=False,
            run_root=tmp_path / "runs",
        ),
        "github",
    )

    result = await activities.execute_control_step(
        {
            "step": "enhancement.publish",
            "payload": {
                "dry_run": False,
                "affected_repository": "gntik-ai/llmwiki",
            },
        }
    )

    assert result["status"] == "BLOCKED_BY_POLICY"
    assert result["reason_code"] == "POLICY_DISABLED"


@pytest.mark.asyncio
async def test_policy_denial_temporal_payload_is_secret_free_and_serializable() -> None:
    denial = GitHubWritePolicyDenial(
        policy=GitHubWritePolicyState(enabled=False, repository_allowed=False),
        reason_code="POLICY_DISABLED",
    ).model_dump(mode="json")
    converter = DataConverter.default

    encoded = await converter.encode([denial])
    decoded = await converter.decode(encoded)

    assert decoded == [denial]
    assert "token" not in json.dumps(decoded).lower()
    assert "password" not in json.dumps(decoded).lower()


@pytest.mark.asyncio
async def test_policy_denial_does_not_echo_the_write_request(tmp_path: Path) -> None:
    project_root, policy = policy_file(tmp_path, enabled=False, repositories=())
    handler = DeliveryActivityHandler(
        Settings(
            project_config_root=project_root,
            github_issue_write_validation_file=policy,
            run_root=tmp_path / "runs",
        ),
        NoCommandRunner(),  # type: ignore[arg-type]
    )

    result = await handler.handle(
        "finding.publish",
        {
            "status": "PUBLICATION_READY",
            "finding_publication": {
                "repository": "gntik-ai/llmwiki",
                "password": "must-not-survive",
            },
        },
    )

    serialized = json.dumps(result, sort_keys=True)
    assert "must-not-survive" not in serialized
    assert "password" not in serialized.lower()


@pytest.mark.asyncio
async def test_workflow_waits_for_explicit_retry_after_policy_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controlled = ControlledWorkflow()
    calls: list[str] = []

    async def fake_step(
        step: str, payload: dict[str, Any], *, timeout_minutes: int = 30
    ) -> dict[str, Any]:
        del payload, timeout_minutes
        calls.append(step)
        if len(calls) == 1:
            return {
                "status": "BLOCKED_BY_POLICY",
                "reason_code": "POLICY_DISABLED",
            }
        return {"status": "PUBLISHED"}

    async def explicit_retry(predicate: Any, **_kwargs: Any) -> None:
        controlled._retry_step = "finding.publish"
        assert predicate() is True

    monkeypatch.setattr(controlled, "_step", fake_step)
    monkeypatch.setattr(definitions.workflow, "wait_condition", explicit_retry)
    # A history recorded before NEW-84 keeps the plain wait for the retry signal.
    monkeypatch.setattr(definitions.workflow, "patched", lambda _name: False)

    result = await controlled._github_write_step("finding.publish", {})

    assert result["status"] == "PUBLISHED"
    assert calls == ["finding.publish", "finding.publish"]
    assert controlled._phase == "AWAITING_WRITE_POLICY"


@pytest.mark.asyncio
async def test_policy_denial_alerts_once_and_retries_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # NEW-84: one operator alert, then a 5-minute re-check until the approval is back.
    from types import SimpleNamespace

    controlled = ControlledWorkflow()
    calls: list[str] = []

    async def fake_step(
        step: str, payload: dict[str, Any], *, timeout_minutes: int = 30
    ) -> dict[str, Any]:
        del timeout_minutes
        calls.append(step)
        if step == "notify.write_policy_required":
            assert payload["step"] == "finding.publish"
            return {"status": "NOTIFIED"}
        if calls.count("finding.publish") < 3:
            return {"status": "BLOCKED_BY_POLICY", "reason_code": "POLICY_DISABLED"}
        return {"status": "PUBLISHED"}

    waits: list[Any] = []

    async def timed_out(predicate: Any, **kwargs: Any) -> None:
        waits.append(kwargs.get("timeout"))
        raise TimeoutError

    monkeypatch.setattr(controlled, "_step", fake_step)
    monkeypatch.setattr(definitions.workflow, "wait_condition", timed_out)
    monkeypatch.setattr(definitions.workflow, "patched", lambda _name: True)
    monkeypatch.setattr(definitions.workflow, "info", lambda: SimpleNamespace(workflow_id="wf-1"))

    result = await controlled._github_write_step("finding.publish", {"project": "musematic"})

    assert result["status"] == "PUBLISHED"
    assert calls == [
        "finding.publish",
        "notify.write_policy_required",
        "finding.publish",
        "finding.publish",
    ]
    assert all(wait is not None and wait.total_seconds() == 300 for wait in waits)


def test_acceptance_harness_requires_exact_safe_denial_and_one_activity_start() -> None:
    result = {
        "status": "BLOCKED_BY_POLICY",
        "terminal": True,
        "retryable": False,
        "external_command_started": False,
        "external_mutation_attempted": False,
        "mutating_command_constructed": False,
        "external_command_count": 0,
        "external_mutation_attempt_count": 0,
        "mutating_command_construction_count": 0,
        "policy": {"enabled": False, "repository_allowed": False},
        "reason_code": "POLICY_DISABLED",
    }
    assert classify_write_denial(result, activity_starts=1) == DENIAL_ACCEPTANCE_STATUS
    for unsafe in (
        {**result, "external_command_started": True},
        {**result, "external_mutation_attempted": True},
        {**result, "status": "BLOCKED"},
    ):
        with pytest.raises(RuntimeError):
            classify_write_denial(unsafe, activity_starts=1)
    with pytest.raises(RuntimeError, match="unconsumed or retried"):
        classify_write_denial(result, activity_starts=2)


@pytest.mark.asyncio
async def test_write_denial_acceptance_workflow_validates_in_default_temporal_sandbox() -> None:
    runner = SandboxedWorkflowRunner()
    definition = workflow._Definition.must_from_class(GitHubWriteDenialAcceptanceWorkflow)

    runner.prepare_workflow(definition)
