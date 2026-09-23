from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from engineering_control.activities.core import ControlActivities
from engineering_control.activities.delivery import DeliveryActivityHandler
from engineering_control.activities.github import GitHubActivityHandler
from engineering_control.config import Settings
from engineering_control.workflows import worker_registry
from engineering_control.workflows.definitions import ControlledWorkflow
from engineering_control.workflows.role_worker import QUEUES
from engineering_control.workflows.worker_registry import (
    CONTROL_TASK_QUEUE,
    GITHUB_TASK_QUEUE,
    ROLE_WORKER_REGISTRY,
    WORKER_DEPLOYMENT_NAME,
    provider_task_queue,
    role_worker_spec,
    worker_deployment_kwargs,
)

ROOT = Path(__file__).parents[1]
ROLE_KINDS = {"codex", "claude", "browser", "scan", "delivery", "release", "github"}
GITHUB_STEPS = frozenset(
    {
        "llmwiki.bootstrap.plan",
        "github.refresh_mirrors",
        "backlog.github_import",
        "delivery.refresh_issue_body",
        "finding.dedupe",
        "delivery.publish",
        "reconcile.inspect_ci",
        "reconcile.publish_promotion",
        "final.merge_source",
        "final.inspect_artifact",
        "final.merge_deployment",
        "final.sync_worktrees",
        "final.comment_issue",
        "final.close_issue",
        "enhancement.publish",
        "enhancement.github_dedupe",
        "finding.publish",
        "gitops.pr_checks",
        "gitops.merge",
        "gitops.release_tag",
        "gitops.workflow_run",
        "gitops.image_pin",
        "gitops.environment_issue",
        "gitops.close_issue",
    }
)


def test_role_worker_registry_is_complete_and_github_is_controller_only() -> None:
    assert set(ROLE_WORKER_REGISTRY) == ROLE_KINDS
    assert QUEUES == {kind: spec.task_queue for kind, spec in ROLE_WORKER_REGISTRY.items()}
    assert len(set(QUEUES.values())) == len(QUEUES)
    assert ROLE_WORKER_REGISTRY["github"].host == "controller"
    assert ROLE_WORKER_REGISTRY["github"].unix_user == "hermes"
    assert ROLE_WORKER_REGISTRY["github"].service == "engineering-github-worker.service"
    assert ROLE_WORKER_REGISTRY["github"].task_queue == "engineering-github"
    assert all(
        spec.host == "worker" for kind, spec in ROLE_WORKER_REGISTRY.items() if kind != "github"
    )


def test_worker_deployment_kwargs_is_a_noop_when_build_id_unset() -> None:
    assert worker_deployment_kwargs(None) == {}
    assert worker_deployment_kwargs("") == {}


def test_worker_deployment_kwargs_enables_auto_upgrade_versioning_when_set() -> None:
    from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
    from temporalio.worker import WorkerDeploymentConfig

    kwargs = worker_deployment_kwargs("abc123")
    assert set(kwargs) == {"deployment_config"}
    config = kwargs["deployment_config"]
    assert isinstance(config, WorkerDeploymentConfig)
    assert config.use_worker_versioning is True
    assert config.default_versioning_behavior == VersioningBehavior.AUTO_UPGRADE
    assert config.version == WorkerDeploymentVersion(
        deployment_name=WORKER_DEPLOYMENT_NAME, build_id="abc123"
    )


def test_unknown_worker_kind_and_queue_typo_fail_closed() -> None:
    with pytest.raises(ValueError, match="bounded worker role"):
        role_worker_spec("github-typo")
    with pytest.raises(ValidationError):
        Settings(worker_kind="github-typo")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="canonical queue"):
        Settings(github_task_queue="engineering-github-typo")
    with pytest.raises(ValueError, match="codex or claude"):
        provider_task_queue("github", default="codex")


def test_original_missing_github_registration_failure_is_reproduced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pre_fix_registry = dict(ROLE_WORKER_REGISTRY)
    pre_fix_registry.pop("github")
    monkeypatch.setattr(worker_registry, "ROLE_WORKER_REGISTRY", pre_fix_registry)
    with pytest.raises(ValueError, match="bounded worker role"):
        worker_registry.role_worker_spec("github")


def test_github_activity_set_is_typed_reachable_and_exclusive() -> None:
    registered = ControlActivities.steps_for_worker("github")
    assert registered == GITHUB_STEPS
    assert registered == (
        GitHubActivityHandler.supported_steps | DeliveryActivityHandler.supported_steps
    )
    assert all(
        ControlledWorkflow._activity_queue(step, {}) == GITHUB_TASK_QUEUE for step in registered
    )
    assert not registered & ControlActivities.steps_for_worker("release")
    assert not registered & ControlActivities.steps_for_worker("browser")
    assert not registered & ControlActivities.steps_for_worker("scan")
    assert not registered & ControlActivities.steps_for_worker("codex")
    assert not registered & ControlActivities.steps_for_worker("claude")


def test_every_role_kind_has_a_queue_activity_set_and_service() -> None:
    for kind, spec in ROLE_WORKER_REGISTRY.items():
        assert ControlActivities.steps_for_worker(kind)
        assert spec.task_queue == f"engineering-{kind}"
        if kind in {"codex", "claude", "browser", "scan", "delivery"}:
            assert spec.service == f"engineering-worker@{kind}.service"
        else:
            assert (ROOT / "systemd" / spec.service).is_file()


def test_every_registered_activity_routes_to_a_worker_that_owns_it() -> None:
    worker_kinds = ("controller", *ROLE_WORKER_REGISTRY)
    queue_owners = {
        CONTROL_TASK_QUEUE: "controller",
        **{spec.task_queue: kind for kind, spec in ROLE_WORKER_REGISTRY.items()},
    }
    activity_steps = set().union(
        *(ControlActivities.steps_for_worker(kind) for kind in worker_kinds)
    )

    for step in activity_steps:
        queue = ControlledWorkflow._activity_queue(step, {})
        owner = queue_owners.get(queue)
        owners = {kind for kind in worker_kinds if step in ControlActivities.steps_for_worker(kind)}
        assert owner in owners, f"{step} routes to {queue}, owners={sorted(owners)}"


def _literal_workflow_step_calls() -> set[str]:
    """Every literal step name passed to self._step/_github_write_step/_agent_step
    in workflows/definitions.py -- misses only genuinely dynamic (variable) step
    names, which are the caller's own responsibility to get right at the literal
    call site that supplies them.
    """
    source = (ROOT / "src/engineering_control/workflows/definitions.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    steps: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"_step", "_github_write_step", "_agent_step"}
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            steps.add(node.args[0].value)
    return steps


def test_every_literal_step_a_workflow_calls_is_a_registered_activity() -> None:
    """Regression test for a real production incident (2026-09-22): NEW-16's fix
    added a `backlog.register_run` step call in workflows/definitions.py, but
    never added it to ControlActivities._allowed_steps -- so every fresh
    ExistingGitHubBacklogLoopWorkflow execution failed immediately with
    'unknown or invalid control step', a non-retryable ApplicationError, on its
    second activity call. Existing tests all mocked execute_control_step and
    never caught it. This checks the real dispatcher's registered steps against
    every literal step name workflows/definitions.py actually calls.
    """
    called_steps = _literal_workflow_step_calls()
    assert len(called_steps) > 50  # sanity: the AST walk actually found calls
    worker_kinds = ("controller", *ROLE_WORKER_REGISTRY)
    registered = set().union(
        *(ControlActivities.steps_for_worker(kind) for kind in worker_kinds)
    )
    missing = called_steps - registered
    assert not missing, (
        f"workflows/definitions.py calls step(s) never registered in any "
        f"ControlActivities allowed-steps set: {sorted(missing)}"
    )


def test_observability_canary_routes_each_step_to_one_bounded_worker() -> None:
    expected = {
        "observability.register": "controller",
        "observability.prepare_workspace": "scan",
        "observability.prepare_codex": "controller",
        "observability.invoke_codex": "codex",
        "observability.prepare_claude": "controller",
        "observability.invoke_claude": "claude",
        "observability.finalize": "controller",
        "observability.cleanup_workspace": "scan",
    }
    for step, owner in expected.items():
        owners = {
            kind
            for kind in (
                "controller",
                "scan",
                "codex",
                "claude",
                "browser",
                "delivery",
                "release",
                "github",
            )
            if step in ControlActivities.steps_for_worker(kind)
        }
        assert owners == {owner}


def test_reverse_observability_canary_routes_opposite_native_providers() -> None:
    expected = {
        "observability.prepare_claude_maker": "controller",
        "observability.invoke_claude_maker": "claude",
        "observability.prepare_codex_checker": "controller",
        "observability.invoke_codex_checker": "codex",
    }
    for step, owner in expected.items():
        owners = {
            kind
            for kind in ("controller", "codex", "claude", "browser", "scan", "delivery")
            if step in ControlActivities.steps_for_worker(kind)
        }
        assert owners == {owner}


def test_observability_canary_workspace_parent_is_owned_by_scan_runner() -> None:
    worker = (ROOT / "scripts/bootstrap-worker.sh").read_text(encoding="utf-8")
    assert "install -d -o scan-runner -g scan-runner -m 0755" in worker
    assert '"${PLATFORM_ROOT}/worktrees/observability-canary"' in worker


def test_github_systemd_contract_is_hardened_and_secret_free() -> None:
    unit = (ROOT / "systemd/engineering-github-worker.service").read_text(encoding="utf-8")
    for required in (
        "User=hermes",
        "Group=hermes",
        "SupplementaryGroups=engineering-control",
        "WorkingDirectory=/opt/engineering-control/source",
        "EnvironmentFile=/etc/engineering-control/env/github-worker.env",
        "Environment=HOME=/home/hermes",
        "Environment=PATH=/usr/local/bin:/usr/bin:/bin",
        "Environment=ENGINEERING_WORKER_KIND=github",
        "Environment=ENGINEERING_GITHUB_WRITES_ENABLED=true",
        "Environment=ENGINEERING_GITHUB_ISSUE_WRITE_VALIDATION_FILE=/etc/engineering-control/policy/github-issue-write-validation.yaml",
        "ExecStart=/opt/engineering-control/venv/bin/engineering-role-worker",
        "Restart=on-failure",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectControlGroups=true",
        "RestrictSUIDSGID=true",
        "UMask=0077",
    ):
        assert required in unit
    assert "/home/hermes/.config/gh" in unit
    assert "/etc/engineering-control/policy/github-issue-write-validation.yaml" in unit
    assert "/home/hermes/.codex" in unit
    assert "/home/hermes/.claude" in unit
    assert "/home/hermes/.kube" in unit
    assert "/etc/engineering-control/env/release" in unit
    for forbidden in ("GITHUB_TOKEN", "GH_TOKEN", "auth token", "worker-common.env"):
        assert forbidden not in unit


def test_controller_bootstrap_owns_github_worker_and_worker_bootstrap_does_not() -> None:
    controller = (ROOT / "scripts/bootstrap-controller.sh").read_text(encoding="utf-8")
    worker = (ROOT / "scripts/bootstrap-worker.sh").read_text(encoding="utf-8")
    unit = "engineering-github-worker.service"
    assert controller.count(unit) >= 4
    assert unit not in worker
    assert 'if [[ ! -f "${CONFIG_ROOT}/env/github-worker.env" ]]' in controller
    assert "ENGINEERING_GITHUB_WRITES_ENABLED=true" in controller
    assert "GitHub broker capability" in controller
    assert "systemctl daemon-reload" in controller
    assert "github-issue-write-validation.yaml" in controller
    assert "install -d -o root -g engineering-control -m 0750" in controller
    assert "ENGINEERING_GITHUB_ISSUE_WRITE_VALIDATION_FILE" in controller
    assert "github-runner.token" in controller
    assert "ENGINEERING_EVIDENCE_INGEST_URL=http://10.10.0.2:8750/v1/evidence" in controller
    assert (
        "ENGINEERING_EVIDENCE_INGEST_TOKEN_FILE=%s/env/evidence-uploaders/github-runner.token"
        in controller
    )
    assert "release-runner github-runner" in controller
    assert "auth token" not in controller
    assert ".config/gh" not in controller
    assert "GITHUB_TOKEN" not in controller
    assert "GH_TOKEN" not in controller


def test_registry_verifier_is_mandatory_for_both_bootstraps() -> None:
    verifier = (ROOT / "scripts/verify-worker-registry.py").read_text(encoding="utf-8")
    assert "ROLE_WORKER_REGISTRY" in verifier
    assert "ControlActivities.steps_for_worker" in verifier
    assert "ControlledWorkflow._activity_queue" in verifier
    assert "source GitHub write policy must remain disabled" in verifier
    assert "source GitHub safe repository list must be empty" in verifier
    assert "WRITE_REPOSITORIES" in verifier
    assert "set(repositories) == WRITE_REPOSITORIES" in verifier
    for bootstrap in ("bootstrap-controller.sh", "bootstrap-worker.sh"):
        content = (ROOT / "scripts" / bootstrap).read_text(encoding="utf-8")
        assert "verify-worker-registry.py" in content


def test_controller_github_bootstrap_is_source_idempotent() -> None:
    controller = (ROOT / "scripts/bootstrap-controller.sh").read_text(encoding="utf-8")
    worker = (ROOT / "scripts/bootstrap-worker.sh").read_text(encoding="utf-8")
    assert controller.count('if [[ ! -f "${CONFIG_ROOT}/env/github-worker.env" ]]') == 1
    assert "install -d -o hermes -g hermes -m 0700" in controller
    assert 'chown root:hermes "${CONFIG_ROOT}/env/github-worker.env"' in controller
    assert 'chmod 0640 "${CONFIG_ROOT}/env/github-worker.env"' in controller
    assert "systemctl enable engineering-api.service" in controller
    assert "systemctl restart engineering-api.service" in controller
    assert "systemctl enable --now engineering-github-worker.service" not in controller
    assert "reconcile-github-write-policy.py" in controller
    assert "policy/github-issue-write-validation.yaml" not in worker
    assert 'install -d -o root -g engineering-control -m 0750 "${CONFIG_ROOT}/policy"' in controller
    assert "install -d -o engineering-control -g engineering-control -m 0700" in controller
    assert "engineering-github-policy-reconciler.path" in controller
    assert "engineering-github-policy-reconciler.path" not in worker


def test_backlog_policy_reconciler_is_fixed_typed_and_secret_isolated() -> None:
    script = (ROOT / "scripts/reconcile-backlog-github-policy.py").read_text(encoding="utf-8")
    service = (ROOT / "systemd/engineering-github-policy-reconciler.service").read_text(
        encoding="utf-8"
    )
    path = (ROOT / "systemd/engineering-github-policy-reconciler.path").read_text(encoding="utf-8")
    for repository in (
        "gntik-ai/falcone",
        "gntik-ai/falcone-charts",
        "gntik-ai/musematic",
        "gntik-ai/musematic-deploy",
    ):
        assert repository in script
    assert "GitHubWritePolicyDocument" in script
    assert "APPROVED_POLICY_PENDING" in script
    assert "canonical_sha256" in script
    assert "production" in script
    assert "gh " not in script
    assert "subprocess" not in script
    assert "GITHUB_TOKEN" not in script
    assert "GH_TOKEN" not in script
    assert "User=root" in service
    assert "/home/hermes/.config/gh" in service
    assert "ReadWritePaths=/etc/engineering-control/policy" in service
    assert "PathExists=/var/lib/engineering-control/github-policy-requests/activate.json" in path


def test_github_runtime_validator_uses_only_typed_broker_reads() -> None:
    script = (ROOT / "scripts/validate-gh-cli-broker.py").read_text(encoding="utf-8")
    assert "GhCliBroker" in script
    assert "broker.preflight(REQUIRED_REPOSITORIES)" in script
    assert "writes_enabled=False" in script
    assert "auth token" not in script
    assert "GITHUB_TOKEN" not in script
    assert "GH_TOKEN" not in script
    assert "create_issue" not in script
    assert "create_pull_request" not in script


def test_write_denial_runtime_validator_uses_real_queue_without_write_bypass() -> None:
    script = (ROOT / "scripts/validate-github-write-denial.py").read_text(encoding="utf-8")
    validation = (ROOT / "src/engineering_control/validation/github_write.py").read_text(
        encoding="utf-8"
    )
    controller = (ROOT / "scripts/verify-controller.sh").read_text(encoding="utf-8")
    assert "GitHubWriteDenialAcceptanceWorkflow" in script
    assert '"step": "finding.publish"' in validation
    assert '"acceptance_only": True' in validation
    assert "task_queue=GITHUB_TASK_QUEUE" in validation
    assert "maximum_attempts=1" in validation
    assert "validate-github-write-denial.py" in controller
    for forbidden in ("auth token", "GITHUB_TOKEN", "GH_TOKEN", "create_issue", "git push"):
        assert forbidden not in script
        assert forbidden not in validation
