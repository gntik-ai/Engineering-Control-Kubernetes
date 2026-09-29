"""Post-merge staging GitOps flow: contract, controller (kubectl/DB) and GitHub steps."""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import ValidationError

from engineering_control.activities.core import ControlActivities
from engineering_control.activities.gitops import (
    GitOpsControllerHandler,
    GitOpsGitHubHandler,
    StagingGitOpsPlan,
    evaluate_pull_request_checks,
    image_tags_in_values,
    next_release_tag,
    pod_failure_reason,
    split_image,
)
from engineering_control.adapters.command import CommandFailure, CommandResult
from engineering_control.adapters.github.client import GhCliPolicyError
from engineering_control.config import Settings
from engineering_control.db import models as db
from engineering_control.db.base import Base
from engineering_control.engineering.models import ProjectManifest
from engineering_control.models.reconciliation import DeliveryControlPolicy, StagingFlowPolicy

ROOT = Path(__file__).parents[1]
MERGE_SHA = "d" * 40
HEAD_SHA = "a" * 40
OLD_CHART_REVISION = "663c0611" + "0" * 32
EXPECTED_TAG = "sha-ddddddd"
IMAGES = (
    "ghcr.io/gntik-ai/musematic/control-plane",
    "ghcr.io/gntik-ai/musematic/runtime-controller",
    "ghcr.io/gntik-ai/musematic/reasoning-engine",
    "ghcr.io/gntik-ai/musematic/simulation-controller",
    "ghcr.io/gntik-ai/musematic/sandbox-manager",
    "ghcr.io/gntik-ai/musematic/web",
)


# -- fixtures -------------------------------------------------------------------------


def _config_root(tmp_path: Path, *, safe_repositories: tuple[str, ...] = ()) -> Path:
    root = tmp_path / "config"
    projects = root / "projects.d"
    projects.mkdir(parents=True)
    for source in (ROOT / "config" / "projects").glob("*.yaml"):
        shutil.copyfile(source, projects / source.name)
    policy = root / "policy" / "github-issue-write-validation.yaml"
    policy.parent.mkdir()
    policy.parent.chmod(0o700)
    listed = "".join(f"  - {repository}\n" for repository in safe_repositories)
    policy.write_text(
        "enabled: true\nsafe_repositories:\n" + listed
        if safe_repositories
        else "enabled: false\nsafe_repositories: []\n",
        encoding="utf-8",
    )
    policy.chmod(0o600)
    return projects


def _disallow_r3(projects: Path) -> Path:
    path = projects / "musematic.yaml"
    text = path.read_text(encoding="utf-8")
    assert "    allow_r3_merge: true\n" in text
    path.write_text(text.replace("    allow_r3_merge: true\n", ""), encoding="utf-8")
    return projects


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    projects = overrides.pop("project_config_root", None) or _config_root(tmp_path)
    kubeconfig = tmp_path / "staging.kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    kubeconfig.chmod(0o600)
    values: dict[str, Any] = {
        "database_url": f"sqlite:///{tmp_path / 'gitops.sqlite'}",
        "project_config_root": projects,
        "github_issue_write_validation_file": projects.parent
        / "policy"
        / "github-issue-write-validation.yaml",
        "run_root": tmp_path / "runs",
        "state_root": tmp_path / "state",
        "staging_kubeconfig_path": kubeconfig,
    }
    values.update(overrides)
    return Settings(**values)


def _plan(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "change_set_id": str(UUID(int=7)),
        "project": "musematic",
        "risk_class": "R1",
        "source_repository": "gntik-ai/musematic",
        "source_pr_number": 77,
        "source_pr_url": "https://github.com/gntik-ai/musematic/pull/77",
        "source_head_sha": HEAD_SHA,
        "source_base_branch": "main",
        "deployment_repository": "gntik-ai/musematic-deploy",
        "deployment_pr_number": None,
        "deployment_pr_url": None,
        "deployment_head_sha": None,
        "deployment_base_branch": "main",
        "issue_repository": "gntik-ai/musematic",
        "issue_number": 1026,
    }
    value.update(changes)
    return StagingGitOpsPlan.model_validate(value).model_dump(mode="json")


def _pod(name: str, reason: str | None = None, *, phase: str = "Running") -> dict[str, Any]:
    state: dict[str, Any] = {"running": {}}
    if reason == "Error":
        state = {"terminated": {"reason": "Error"}}
    elif reason:
        state = {"waiting": {"reason": reason}}
    return {
        "metadata": {"name": name},
        "status": {"phase": phase, "containerStatuses": [{"name": "main", "state": state}]},
    }


def _workload(
    name: str,
    *,
    image: str = "ghcr.io/gntik-ai/musematic/web:" + EXPECTED_TAG,
    desired: int = 1,
    ready: int = 1,
) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "generation": 3},
        "spec": {
            "replicas": desired,
            "template": {"spec": {"containers": [{"name": "main", "image": image}]}},
        },
        "status": {
            "readyReplicas": ready,
            "updatedReplicas": ready,
            "observedGeneration": 3,
        },
    }


def _application(**status: Any) -> dict[str, Any]:
    return {
        "metadata": {"name": "musematic-platform", "namespace": "argocd"},
        "spec": {
            "sources": [
                {
                    "repoURL": "https://github.com/gntik-ai/musematic.git",
                    "path": "deploy/helm/platform",
                    "targetRevision": OLD_CHART_REVISION,
                    "helm": {
                        "valueFiles": [
                            "values.prod.yaml",
                            "$values/apps/musematic-platform/values.override.yaml",
                        ]
                    },
                },
                {
                    "repoURL": "https://github.com/gntik-ai/musematic-deploy.git",
                    "targetRevision": "main",
                    "ref": "values",
                },
            ]
        },
        "status": {
            "health": {"status": "Healthy"},
            "sync": {"status": "Synced", "revisions": [OLD_CHART_REVISION, "e" * 40]},
            **status,
        },
    }


class FakeKubectl:
    """Answers the bounded kubectl surface from in-memory JSON and records every call."""

    def __init__(
        self,
        *,
        pods: dict[str, list[dict[str, Any]]] | None = None,
        deployments: dict[str, list[dict[str, Any]]] | None = None,
        statefulsets: dict[str, list[dict[str, Any]]] | None = None,
        externalsecrets: dict[str, list[dict[str, Any]]] | None = None,
        stores: dict[str, dict[str, Any]] | None = None,
        application: dict[str, Any] | None = None,
    ) -> None:
        self.pods = pods or {}
        self.deployments = deployments or {}
        self.statefulsets = statefulsets or {}
        self.externalsecrets = externalsecrets
        self.stores = stores or {}
        self.application = application or _application()
        self.calls: list[list[str]] = []
        self.cwds: list[Path] = []

    def run(
        self,
        executable: str,
        arguments: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int = 900,
        environment: dict[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        assert executable == "kubectl"
        self.cwds.append(cwd)
        args = list(arguments)
        assert args[0] == "--kubeconfig"
        args = args[2:]
        self.calls.append(args)
        namespace = None
        if args[0] == "--namespace":
            namespace, args = args[1], args[2:]
        result = self._answer(namespace, args)
        if check and result.return_code:
            raise CommandFailure("kubectl failed")
        return result

    def _answer(self, namespace: str | None, args: list[str]) -> CommandResult:
        verb = args[0]
        if verb == "get":
            resource = args[1]
            if resource == "applications.argoproj.io":
                return CommandResult(0, json.dumps(self.application), "")
            if resource == "externalsecrets.external-secrets.io":
                if self.externalsecrets is None:
                    return CommandResult(1, "", "no such resource type")
                items = self.externalsecrets.get(str(namespace), [])
                return CommandResult(0, json.dumps({"items": items}), "")
            if resource in {
                "clustersecretstores.external-secrets.io",
                "secretstores.external-secrets.io",
            }:
                store = self.stores.get(args[2])
                if store is None:
                    return CommandResult(1, "", "not found")
                return CommandResult(0, json.dumps(store), "")
            source = {
                "pods": self.pods,
                "deployments": self.deployments,
                "statefulsets": self.statefulsets,
            }[resource]
            return CommandResult(0, json.dumps({"items": source.get(str(namespace), [])}), "")
        if verb == "patch":
            patch_type = args[args.index("--type") + 1]
            patch = json.loads(args[args.index("--patch") + 1])
            if patch_type == "json":
                for operation in patch:
                    parts = operation["path"].strip("/").split("/")
                    target = self.application
                    for part in parts[:-1]:
                        target = target[int(part)] if isinstance(target, list) else target[part]
                    if operation["op"] == "test":
                        assert target[parts[-1]] == operation["value"]
                    else:
                        target[parts[-1]] = operation["value"]
            else:
                self.application.update(patch)
            return CommandResult(0, "patched", "")
        if verb in {"annotate", "delete", "rollout"}:
            return CommandResult(0, "ok", "")
        raise AssertionError(f"unexpected kubectl call {args}")


def _controller(tmp_path: Path, kubectl: FakeKubectl, **overrides: Any) -> GitOpsControllerHandler:
    handler = GitOpsControllerHandler(
        _settings(tmp_path, **overrides),
        runner=kubectl,  # type: ignore[arg-type]
    )
    Base.metadata.create_all(handler.engine)
    return handler


def _seed_change_set(
    handler: GitOpsControllerHandler, *, state: str = "SOURCE_PR", risk: str = "R1"
) -> UUID:
    change_set_id = uuid4()
    with handler.sessions.begin() as session:
        session.add(
            db.ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                project="musematic",
                risk_class=risk,
                state=state,
                version=1,
                source_repository="musematic",
                source_base_sha="b" * 40,
                source_head_sha=HEAD_SHA,
                source_pr_url="https://github.com/gntik-ai/musematic/pull/77",
                deployment_repository="musematic-deploy",
                deployment_base_sha="c" * 40,
                image_digests={},
                recovery_json={},
            )
        )
    return change_set_id


# -- contract -------------------------------------------------------------------------


def _musematic_manifest() -> dict[str, Any]:
    value: dict[str, Any] = yaml.safe_load(
        (ROOT / "config" / "projects" / "musematic.yaml").read_text(encoding="utf-8")
    )
    return value


def test_musematic_contract_enables_a_consistent_staging_flow() -> None:
    raw = _musematic_manifest()
    manifest = ProjectManifest.model_validate(raw)
    flow = manifest.staging_flow()
    assert flow is not None and flow.enabled
    assert manifest.staging_flow_blockers() == []
    assert flow.argo_application == "musematic-platform"
    assert flow.namespaces == ("musematic-platform", "platform-data")
    assert flow.main_ci_workflow is None
    assert flow.image_workflow == "deploy.yml"
    assert flow.release_tag.enabled and flow.release_tag.prefix == "v"
    assert flow.deploy_repository == "gntik-ai/musematic-deploy"
    assert set(flow.image_repositories) == set(raw["deployment"]["image_repositories"].values())
    assert flow.pin_chart_to_merge
    assert flow.chart_source_repository == "https://github.com/gntik-ai/musematic.git"
    policy = DeliveryControlPolicy.model_validate(raw["delivery_control"])
    assert policy.staging_flow == flow
    llmwiki = ProjectManifest.model_validate(
        yaml.safe_load((ROOT / "config" / "projects" / "llmwiki.yaml").read_text())
    )
    assert llmwiki.staging_flow() is None


def test_falcone_contract_enables_a_consistent_staging_flow() -> None:
    # gntik-ai/falcone#1027: falcone staging is the falcone-staging Argo CD Application;
    # argocd-image-updater writes values/staging-argocd.yaml in falcone-charts.
    raw = yaml.safe_load((ROOT / "config" / "projects" / "falcone.yaml").read_text())
    manifest = ProjectManifest.model_validate(raw)
    flow = manifest.staging_flow()
    assert flow is not None and flow.enabled
    assert manifest.staging_flow_blockers() == []
    assert manifest.deployment.strategy == "argocd"
    assert manifest.deployment.argocd_application == flow.argo_application == "falcone-staging"
    assert flow.deploy_repository == "gntik-ai/falcone-charts"
    assert flow.deploy_values_file == "charts/in-falcone/values/staging-argocd.yaml"
    assert flow.image_workflow == "release-images.yml"
    assert set(flow.image_repositories) == set(raw["deployment"]["image_repositories"].values())
    assert not flow.pin_chart_to_merge
    # NEW-63 for falcone: ci.yml has no path filters, so one root marker runs every job;
    # every top-level entry of gntik-ai/falcone (2026-09-28) must fall under a scope.
    assert flow.ci_baseline.enabled
    assert {marker.path for marker in flow.ci_baseline.markers} == {".hermes-ci-baseline"}
    top_level = (
        ".dockerignore .env.example .gitignore .idea/x .markdownlint-cli2.jsonc "
        "LICENSE README.de.md README.md apps/web-console/x delivery-adapter/x deploy/x "
        "docs/x docs-site/x falcone-loop-state/x internal-contracts/x logo-original.png "
        "logo.svg openspec/x package.json packages/x pnpm-lock.yaml pnpm-workspace.yaml "
        "scripts/x service-catalog.json spikes/x tests/x tools/x turbo.json"
    ).split()
    scopes = [marker.scope for marker in flow.ci_baseline.markers]
    assert all(any(path.startswith(scope) for scope in scopes) for path in top_level)
    assert not any(".github/ci.yml".startswith(scope) for scope in scopes)


@pytest.mark.parametrize(
    "change",
    (
        {"unexpected": True},
        {"kind": "flux"},
        {"namespaces": ["kube-system"]},
        {"namespaces": []},
        {"image_repositories": ["docker.io/library/nginx"]},
        {"deploy_values_file": "../escape.yaml"},
        {"pin_chart_to_merge": True, "chart_source_repository": None},
        {"image_workflow": None},  # release_tag.enabled requires the triggered workflow
        {"tag_length": 3},
        {"release_tag": {"enabled": True, "bump": "minor"}},
    ),
)
def test_staging_flow_contract_is_strict(change: dict[str, Any]) -> None:
    raw = _musematic_manifest()["delivery_control"]["staging_flow"]
    with pytest.raises(ValidationError):
        StagingFlowPolicy.model_validate({**raw, **change})
    manifest = _musematic_manifest()
    manifest["delivery_control"]["staging_flow"].update(change)
    with pytest.raises(ValidationError):
        ProjectManifest.model_validate(manifest)


def test_staging_flow_defaults_are_conservative() -> None:
    flow = StagingFlowPolicy.model_validate(
        {
            "argo_application": "app",
            "namespaces": ["app"],
            "source_ci_workflow": "CI",
            "deploy_repository": "gntik-ai/musematic-deploy",
            "deploy_values_file": "values.yaml",
            "image_repositories": ["ghcr.io/gntik-ai/musematic/web"],
        }
    )
    assert flow.enabled is False
    assert flow.argo_namespace == "argocd"
    assert flow.deploy_branch == "main"
    assert (flow.tag_prefix, flow.tag_length) == ("sha-", 7)
    assert flow.release_tag.enabled is False
    assert flow.pin_chart_to_merge is False


# -- pure helpers ---------------------------------------------------------------------


def test_next_release_tag_uses_semver_order_and_ignores_other_tags() -> None:
    assert next_release_tag(["v0.0.9", "v0.0.10", "v0.0.2"], "v") == "v0.0.11"
    assert next_release_tag(["v0.0.9"], "v") == "v0.0.10"
    assert next_release_tag(["v1.2.3", "v0.9.99", "v1.10.0"], "v") == "v1.10.1"
    assert (
        next_release_tag(["v2.0.0-rc1", "release-9", "v01.0.0", "latest", "v0.0.3"], "v")
        == "v0.0.4"
    )
    assert next_release_tag([], "v") == "v0.0.1"


def test_image_helpers_extract_only_configured_repository_tags() -> None:
    assert split_image("ghcr.io/gntik-ai/musematic/web:sha-1234567") == (
        "ghcr.io/gntik-ai/musematic/web",
        "sha-1234567",
    )
    assert split_image("registry:5000/team/app@sha256:" + "f" * 64) == (
        "registry:5000/team/app",
        None,
    )
    values = """
controlPlane:
  image:
    repository: ghcr.io/gntik-ai/musematic/control-plane
    tag: sha-ddddddd
ui:
  image:
    repository: ghcr.io/gntik-ai/musematic/web
    tag: sha-1111111
clamav:
  image:
    repository: bitnamilegacy/clamav
    tag: 1.4.4
sidecar:
  image: ghcr.io/gntik-ai/musematic/sandbox-manager:sha-ddddddd
database:
  password: hunter2-not-returned
"""
    found = image_tags_in_values(values, IMAGES)
    assert found == {
        "ghcr.io/gntik-ai/musematic/control-plane": ["sha-ddddddd"],
        "ghcr.io/gntik-ai/musematic/web": ["sha-1111111"],
        "ghcr.io/gntik-ai/musematic/sandbox-manager": ["sha-ddddddd"],
    }
    assert "hunter2" not in json.dumps(found)
    broken = (
        "a: [unclosed\nweb:\n  repository: ghcr.io/gntik-ai/musematic/web\n  tag: sha-ddddddd\n"
    )
    assert image_tags_in_values(broken, IMAGES) == {
        "ghcr.io/gntik-ai/musematic/web": ["sha-ddddddd"]
    }


def test_pod_failure_reasons_cover_the_operator_rule() -> None:
    assert pod_failure_reason(_pod("a", "CrashLoopBackOff")) == "CrashLoopBackOff"
    assert pod_failure_reason(_pod("b", "ImagePullBackOff")) == "ImagePullBackOff"
    assert pod_failure_reason(_pod("c", "ErrImagePull")) == "ErrImagePull"
    assert pod_failure_reason(_pod("d", "Error")) == "Error"
    assert pod_failure_reason(_pod("e", "ContainerCreating")) is None
    assert pod_failure_reason(_pod("f")) is None
    assert pod_failure_reason(_pod("g", "Error", phase="Succeeded")) is None


def test_pull_request_check_evaluation() -> None:
    passing = {
        "statusCheckRollup": [
            {"workflowName": "CI", "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"workflowName": "CI", "name": "lint", "status": "COMPLETED", "conclusion": "SKIPPED"},
        ]
    }
    assert evaluate_pull_request_checks(passing, "CI")["status"] == "CHECKS_PASSED"
    assert evaluate_pull_request_checks(passing, "Other")["status"] == "CHECKS_PENDING"
    pending = {"statusCheckRollup": [{"workflowName": "CI", "status": "IN_PROGRESS"}]}
    assert evaluate_pull_request_checks(pending, "CI")["status"] == "CHECKS_PENDING"
    assert evaluate_pull_request_checks({}, "CI")["status"] == "CHECKS_PENDING"
    failing = {
        "statusCheckRollup": [
            {"workflowName": "CI", "name": "test", "status": "COMPLETED", "conclusion": "FAILURE"}
        ]
    }
    failed = evaluate_pull_request_checks(failing, "CI")
    assert failed == {
        "status": "BLOCKED",
        "blocker": "SOURCE_PR_CI_FAILED",
        "failed_checks": ["test"],
    }


# -- controller steps -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_flow_for_project_reports_contract_state(tmp_path: Path) -> None:
    handler = _controller(tmp_path, FakeKubectl())
    enabled = await handler.handle("gitops.flow_for_project", {"project": "musematic"})
    assert enabled["enabled"] is True and enabled["blockers"] == []
    disabled = await handler.handle("gitops.flow_for_project", {"project": "llmwiki"})
    assert disabled == {"status": "DISABLED", "enabled": False}
    invalid = await handler.handle("gitops.flow_for_project", {"project": "other"})
    assert invalid["enabled"] is False


@pytest.mark.asyncio
async def test_prepare_binds_the_published_change_set(tmp_path: Path) -> None:
    handler = _controller(tmp_path, FakeKubectl())
    change_set_id = _seed_change_set(handler)
    request = {
        "change_set_id": str(change_set_id),
        "project": "musematic",
        "issue_repository": "gntik-ai/musematic",
        "issue_number": 1026,
    }
    prepared = await handler.handle("gitops.prepare", request)
    assert prepared["status"] == "READY"
    plan = prepared["plan"]
    assert plan["source_pr_number"] == 77
    assert plan["source_repository"] == "gntik-ai/musematic"
    assert plan["deployment_pr_number"] is None
    assert prepared["staging_flow"]["release_tag_enabled"] is True
    assert prepared["staging_flow"]["main_ci_workflow"] is None

    # NEW-62: musematic opts in to R3 merges (staging-only cluster).
    r3 = _seed_change_set(handler, risk="R3")
    allowed = await handler.handle("gitops.prepare", {**request, "change_set_id": str(r3)})
    assert allowed["status"] == "READY" and allowed["plan"]["risk_class"] == "R3"
    outside = await handler.handle(
        "gitops.prepare", {**request, "issue_repository": "gntik-ai/falcone"}
    )
    assert outside["blocker"] == "DELIVERED_ISSUE_REPOSITORY_OUTSIDE_PROJECT"
    wrong_state = _seed_change_set(handler, state="CONFIRMED")
    early = await handler.handle("gitops.prepare", {**request, "change_set_id": str(wrong_state)})
    assert early["blocker"] == "CHANGE_SET_STATE_NOT_DELIVERABLE"


@pytest.mark.asyncio
async def test_environment_check_healthy_and_unhealthy(tmp_path: Path) -> None:
    healthy = FakeKubectl(
        pods={"musematic-platform": [_pod("web-1")]},
        deployments={"musematic-platform": [_workload("web")]},
        externalsecrets={},
    )
    handler = _controller(tmp_path, healthy)
    result = await handler.handle("gitops.environment_check", {"plan": _plan()})
    assert result["status"] == "HEALTHY"
    assert result["diagnosis"]["argo_health"] == "Healthy"

    unhealthy = FakeKubectl(
        pods={"musematic-platform": [_pod("web-1", "CrashLoopBackOff")]},
        statefulsets={"platform-data": [_workload("postgres", desired=3, ready=1)]},
        externalsecrets={
            "musematic-platform": [
                {"spec": {"secretStoreRef": {"name": "vault", "kind": "ClusterSecretStore"}}}
            ]
        },
        stores={"vault": {"status": {"conditions": [{"type": "Ready", "status": "False"}]}}},
        application=_application(health={"status": "Degraded"}),
    )
    handler = _controller(tmp_path / "second", unhealthy)
    result = await handler.handle("gitops.environment_check", {"plan": _plan()})
    diagnosis = result["diagnosis"]
    assert result["status"] == "UNHEALTHY"
    assert diagnosis["unhealthy_pods"] == [
        {"namespace": "musematic-platform", "name": "web-1", "reason": "CrashLoopBackOff"}
    ]
    assert diagnosis["unready_workloads"][0]["name"] == "postgres"
    assert diagnosis["stores_not_ready"][0]["name"] == "vault"
    assert diagnosis["argo_health"] == "Degraded"


@pytest.mark.asyncio
async def test_kubeconfig_must_be_private(tmp_path: Path) -> None:
    handler = _controller(tmp_path, FakeKubectl())
    handler.settings.staging_kubeconfig_path.chmod(0o640)
    result = await handler.handle("gitops.environment_check", {"plan": _plan()})
    assert result == {"status": "BLOCKED", "blocker": "STAGING_KUBECONFIG_PERMISSIONS_TOO_BROAD"}


@pytest.mark.asyncio
async def test_remediation_is_bounded_to_project_resources(tmp_path: Path) -> None:
    kubectl = FakeKubectl(
        pods={
            "musematic-platform": [_pod("web-1", "CrashLoopBackOff"), _pod("ok")],
            "platform-data": [_pod("job-1", "Error", phase="Failed")],
        },
        externalsecrets={
            "musematic-platform": [
                {"spec": {"secretStoreRef": {"name": "vault", "kind": "ClusterSecretStore"}}}
            ]
        },
        stores={"vault": {"status": {"conditions": [{"type": "Ready", "status": "False"}]}}},
    )
    handler = _controller(tmp_path, kubectl)
    result = await handler.handle("gitops.remediate", {"plan": _plan(), "round": 1})
    assert result["status"] == "REMEDIATION_APPLIED"
    assert result["actions"] == [
        "argo-hard-refresh",
        "argo-sync",
        "delete-failed-pods",
        "restart-external-secrets",
    ]
    mutations = [call for call in kubectl.calls if call[0] != "get" and call[2] != "get"]
    assert [
        "argocd",
        "annotate",
        "applications.argoproj.io",
        "musematic-platform",
        "argocd.argoproj.io/refresh=hard",
        "--overwrite",
    ] in [call[1:] for call in mutations]
    deletes = [call for call in mutations if "delete" in call]
    assert [call[1] for call in deletes] == ["musematic-platform", "platform-data"]
    assert all("--wait=false" in call for call in deletes)
    restart = [call for call in mutations if "rollout" in call]
    assert restart == [
        [
            "--namespace",
            "external-secrets",
            "rollout",
            "restart",
            "deployment/external-secrets",
        ]
    ]
    assert kubectl.application["operation"] == {
        "initiatedBy": {"username": "hermes-engineering"},
        "sync": {"prune": False},
    }


@pytest.mark.asyncio
async def test_trigger_sync_pins_the_chart_source_and_requests_a_sync(tmp_path: Path) -> None:
    kubectl = FakeKubectl(
        application=_application(operationState={"startedAt": "2026-09-01T00:00:00Z"})
    )
    handler = _controller(tmp_path, kubectl)
    result = await handler.handle("gitops.trigger_sync", {"plan": _plan(), "merge_sha": MERGE_SHA})
    assert result["status"] == "SYNC_TRIGGERED"
    assert result["previous_operation_started_at"] == "2026-09-01T00:00:00Z"
    assert result["chart_pin"] == {
        "source_index": 0,
        "repo_url": "https://github.com/gntik-ai/musematic.git",
        "previous_target_revision": OLD_CHART_REVISION,
        "target_revision": MERGE_SHA,
        "changed": True,
    }
    sources = kubectl.application["spec"]["sources"]
    assert sources[0]["targetRevision"] == MERGE_SHA
    assert sources[1]["targetRevision"] == "main"
    json_patches = [
        json.loads(call[call.index("--patch") + 1])
        for call in kubectl.calls
        if "--type" in call and call[call.index("--type") + 1] == "json"
    ]
    assert json_patches == [
        [
            {
                "op": "test",
                "path": "/spec/sources/0/repoURL",
                "value": "https://github.com/gntik-ai/musematic.git",
            },
            {"op": "replace", "path": "/spec/sources/0/targetRevision", "value": MERGE_SHA},
        ]
    ]
    assert kubectl.application["operation"]["initiatedBy"]["username"] == "hermes-engineering"

    # Idempotent: an already-pinned source is not patched again.
    kubectl.application.pop("operation")
    kubectl.calls.clear()
    again = await handler.handle("gitops.trigger_sync", {"plan": _plan(), "merge_sha": MERGE_SHA})
    assert again["chart_pin"]["changed"] is False
    assert not [call for call in kubectl.calls if "--type" in call and "json" in call]


@pytest.mark.asyncio
async def test_trigger_sync_fails_closed_on_ambiguous_chart_sources(tmp_path: Path) -> None:
    application = _application()
    application["spec"]["sources"].append(dict(application["spec"]["sources"][0]))
    kubectl = FakeKubectl(application=application)
    handler = _controller(tmp_path, kubectl)
    result = await handler.handle("gitops.trigger_sync", {"plan": _plan(), "merge_sha": MERGE_SHA})
    assert result == {"status": "BLOCKED", "blocker": "ARGO_CHART_SOURCE_AMBIGUOUS"}
    assert not [call for call in kubectl.calls if "patch" in call]


@pytest.mark.asyncio
async def test_sync_status_requires_a_new_hermes_operation(tmp_path: Path) -> None:
    kubectl = FakeKubectl()
    handler = _controller(tmp_path, kubectl)
    payload = {
        "plan": _plan(),
        "merge_sha": MERGE_SHA,
        "previous_operation_started_at": "2026-09-01T00:00:00Z",
    }
    operation = {"initiatedBy": {"username": "hermes-engineering"}}
    kubectl.application["status"]["operationState"] = {
        "phase": "Succeeded",
        "startedAt": "2026-09-01T00:00:00Z",
        "operation": operation,
    }
    assert (await handler.handle("gitops.sync_status", payload))["status"] == "SYNC_PENDING"
    kubectl.application["status"]["operationState"] = {
        "phase": "Failed",
        "startedAt": "2026-09-23T10:00:00Z",
        "operation": operation,
        "message": "one or more objects failed to apply",
    }
    failed = await handler.handle("gitops.sync_status", payload)
    assert failed["status"] == "SYNC_FAILED" and failed["blocker"] == "ARGO_SYNC_FAILED"
    kubectl.application["status"]["operationState"]["phase"] = "Succeeded"
    synced = await handler.handle("gitops.sync_status", payload)
    assert synced["status"] == "SYNCED"
    assert synced["argo_revision"] == f"{OLD_CHART_REVISION},{'e' * 40}"


@pytest.mark.asyncio
async def test_verify_workloads_requires_the_pinned_tag_and_readiness(tmp_path: Path) -> None:
    web = "ghcr.io/gntik-ai/musematic/web"
    kubectl = FakeKubectl(
        deployments={
            "musematic-platform": [
                _workload("web"),
                _workload("other", image="docker.io/library/redis:7"),
            ]
        }
    )
    handler = _controller(tmp_path, kubectl)
    payload = {"plan": _plan(), "merge_sha": MERGE_SHA, "pinned_repositories": [web]}
    verified = await handler.handle("gitops.verify_workloads", payload)
    assert verified["status"] == "VERIFIED"
    assert [item["name"] for item in verified["workloads"]] == ["web"]

    kubectl.deployments["musematic-platform"][0] = _workload("web", image=f"{web}:sha-1111111")
    stale = await handler.handle("gitops.verify_workloads", payload)
    assert stale["status"] == "VERIFICATION_PENDING"
    kubectl.deployments["musematic-platform"][0] = _workload("web", desired=2, ready=1)
    unready = await handler.handle("gitops.verify_workloads", payload)
    assert unready["status"] == "VERIFICATION_PENDING"
    nothing = await handler.handle(
        "gitops.verify_workloads", {**payload, "pinned_repositories": []}
    )
    assert nothing == {"status": "BLOCKED", "blocker": "STAGING_VERIFICATION_FAILED"}


@pytest.mark.asyncio
async def test_persist_result_records_merge_and_runtime_verified_state(tmp_path: Path) -> None:
    handler = _controller(tmp_path, FakeKubectl())
    change_set_id = _seed_change_set(handler)
    plan = _plan(change_set_id=str(change_set_id))
    with handler.sessions.begin() as session:
        session.add(
            db.Run(
                workflow_id="loop-issue-1-staging-gitops",
                run_type="staging-gitops",
                status="QUEUED",
                project="musematic",
                change_set_id=change_set_id,
            )
        )
    blocked = await handler.handle(
        "gitops.persist_result",
        {"plan": plan, "outcome": "BLOCKED", "blocker": "IMAGE_NOT_PINNED", "merge_sha": MERGE_SHA},
    )
    assert blocked["change_set_state"] == "SOURCE_MERGED"
    # NEW-99: only a known unverified-regression reason is recorded.
    for reason, recorded in (
        ("CI_BASELINE_CI_CONFIG_CHANGED", "CI_BASELINE_CI_CONFIG_CHANGED"),
        ("ANYTHING_ELSE", None),
    ):
        await handler.handle(
            "gitops.persist_result",
            {
                "plan": plan,
                "outcome": "BLOCKED",
                "blocker": "SOURCE_PR_CI_REGRESSION",
                "ci_regressions": ["quality"],
                "ci_regression_reason": reason,
                "ci_failure_excerpts": {
                    **{f"job-{index}": "not ok" for index in range(7)},
                    "quality": "x" * 9000,
                    "bad": 5,
                },
            },
        )
        with handler.sessions.begin() as session:
            change_set = session.get(db.ChangeSet, change_set_id)
            assert change_set is not None
            record = change_set.recovery_json["staging_gitops"]
            assert record["ci_regression_reason"] == recorded
            # NEW-100: at most 5 string excerpts of at most 4000 characters each.
            excerpts = record["ci_failure_excerpts"]
            assert len(excerpts) == 5 and all(len(text) <= 4000 for text in excerpts.values())
    result = await handler.handle(
        "gitops.persist_result",
        {
            "plan": plan,
            "outcome": "STAGING_VERIFIED",
            "merge_sha": MERGE_SHA,
            "image_tag": EXPECTED_TAG,
            "pinned_repositories": ["ghcr.io/gntik-ai/musematic/web"],
            "argo_revision": MERGE_SHA,
            "orchestration": {"workflow_id": "loop-issue-1-staging-gitops"},
        },
    )
    assert result["change_set_state"] == "RUNTIME_VERIFIED"
    with handler.sessions.begin() as session:
        change_set = session.get(db.ChangeSet, change_set_id)
        assert change_set is not None
        record = change_set.recovery_json["staging_gitops"]
        assert record["merge_sha"] == MERGE_SHA and record["status"] == "STAGING_VERIFIED"
        run = session.query(db.Run).one()
        assert run.status == "SUCCEEDED" and run.finished_at is not None
    again = await handler.handle(
        "gitops.prepare",
        {
            "change_set_id": str(change_set_id),
            "project": "musematic",
            "issue_repository": "gntik-ai/musematic",
            "issue_number": 1026,
        },
    )
    assert again["status"] == "ALREADY_VERIFIED"


# -- GitHub steps ---------------------------------------------------------------------


class FakeGh:
    """A tiny in-memory GitHub answering the fixed gh argv the broker constructs."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.pull_requests: dict[tuple[str, int], dict[str, Any]] = {}
        self.issues: dict[tuple[str, int], dict[str, Any]] = {}
        self.tags: list[dict[str, str]] = []
        self.runs: list[dict[str, Any]] = []
        self.files: dict[tuple[str, str], str] = {}
        self.next_issue = 500

    def run(
        self,
        executable: str,
        arguments: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int = 900,
        environment: dict[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        assert executable == "gh"
        args = list(arguments)
        self.calls.append(args)
        return CommandResult(0, self._answer(args), "")

    @property
    def mutations(self) -> list[list[str]]:
        return [
            call
            for call in self.calls
            if call[:2]
            in (
                ["pr", "ready"],
                ["pr", "merge"],
                ["issue", "create"],
                ["issue", "comment"],
                ["issue", "close"],
            )
            or "POST" in call
        ]

    def _answer(self, args: list[str]) -> str:
        if args[:2] == ["auth", "status"]:
            return ""
        if args[:2] == ["api", "user"]:
            return json.dumps({"login": "hermes", "type": "User"})
        if args[:2] == ["repo", "view"]:
            return json.dumps({"nameWithOwner": args[2]})
        if args[:2] == ["pr", "view"]:
            return json.dumps(self.pull_requests[(args[4], int(args[2]))])
        if args[:2] == ["pr", "ready"]:
            pull = self.pull_requests[(args[4], int(args[2]))]
            pull.update(isDraft=False, mergeStateStatus="CLEAN")
            return ""
        if args[:2] == ["pr", "merge"]:
            pull = self.pull_requests[(args[4], int(args[2]))]
            assert args[args.index("--match-head-commit") + 1] == pull["headRefOid"]
            pull.update(state="MERGED", mergeCommit={"oid": MERGE_SHA})
            return ""
        if args[0] == "api" and "/commits/" in args[1]:
            return json.dumps({"sha": MERGE_SHA, "tree": "f" * 40})
        if args[0] == "api" and "/git/matching-refs/tags/" in args[1]:
            return "\n".join(json.dumps(tag) for tag in self.tags)
        if args[:3] == ["api", "-X", "POST"]:
            ref = args[args.index("-f") + 1].removeprefix("ref=")
            sha = args[args.index("-f", args.index("-f") + 1) + 1].removeprefix("sha=")
            self.tags.append({"ref": ref, "sha": sha, "type": "commit"})
            return json.dumps({"ref": ref, "sha": sha})
        if args[:3] == ["api", "-X", "GET"]:
            repository, _, path = args[3].removeprefix("repos/").partition("/contents/")
            return self.files[(repository, path)]
        if args[:2] == ["run", "list"]:
            return json.dumps(self.runs)
        if args[:2] == ["issue", "list"]:
            repository = args[3]
            return json.dumps(
                [
                    {"number": number, "title": issue["title"], "url": "", "state": issue["state"]}
                    for (repo, number), issue in self.issues.items()
                    if repo == repository
                ]
            )
        if args[:2] == ["issue", "create"]:
            repository = args[3]
            number = self.next_issue
            self.next_issue += 1
            body = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
            self.issues[(repository, number)] = {
                "number": number,
                "title": args[args.index("--title") + 1],
                "state": "OPEN",
                "body": body,
                "comments": [{"body": body, "url": "created"}],
                "url": f"https://github.com/{repository}/issues/{number}",
                "closedAt": None,
            }
            return f"https://github.com/{repository}/issues/{number}\n"
        if args[:2] == ["issue", "view"]:
            return json.dumps(self.issues[(args[4], int(args[2]))])
        if args[:2] == ["issue", "comment"]:
            issue = self.issues[(args[4], int(args[2]))]
            body = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
            url = f"{issue['url']}#issuecomment-{len(issue['comments']) + 1}"
            issue["comments"].append({"body": body, "url": url})
            return url
        if args[:2] == ["issue", "close"]:
            issue = self.issues[(args[4], int(args[2]))]
            issue.update(state="CLOSED", closedAt="2026-09-23T12:00:00Z")
            return ""
        raise AssertionError(f"unexpected gh call {args}")


SAFE = ("gntik-ai/musematic", "gntik-ai/musematic-deploy")


def _disable_baseline(projects: Path) -> Path:
    path = projects / "musematic.yaml"
    text = path.read_text(encoding="utf-8")
    enabled = "    ci_baseline:\n      enabled: true\n"
    assert enabled in text
    path.write_text(text.replace(enabled, "    ci_baseline:\n      enabled: false\n"), "utf-8")
    return projects


def _github(
    tmp_path: Path, *, safe: tuple[str, ...] = SAFE, baseline: bool = True
) -> tuple[GitOpsGitHubHandler, FakeGh]:
    fake = FakeGh()
    projects = _config_root(tmp_path, safe_repositories=safe)
    settings = _settings(
        tmp_path,
        project_config_root=projects if baseline else _disable_baseline(projects),
        github_writes_enabled=True,
    )
    return GitOpsGitHubHandler(settings, runner=fake), fake  # type: ignore[arg-type]


def _pull_request(**changes: Any) -> dict[str, Any]:
    value = {
        "number": 77,
        "url": "https://github.com/gntik-ai/musematic/pull/77",
        "state": "OPEN",
        "isDraft": True,
        "headRefName": "agent/musematic/x/" + str(UUID(int=7)),
        "headRefOid": HEAD_SHA,
        "baseRefName": "main",
        "mergeStateStatus": "BLOCKED",
        "statusCheckRollup": [
            {"workflowName": "CI", "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ],
        "mergeCommit": None,
    }
    value.update(changes)
    return value


@pytest.mark.asyncio
async def test_pr_checks_poll_states(tmp_path: Path) -> None:
    handler, fake = _github(tmp_path, baseline=False)
    fake.pull_requests[("gntik-ai/musematic", 77)] = _pull_request()
    assert (await handler.handle("gitops.pr_checks", {"plan": _plan()}))["status"] == (
        "CHECKS_PASSED"
    )
    fake.pull_requests[("gntik-ai/musematic", 77)]["statusCheckRollup"][0].update(
        conclusion="FAILURE"
    )
    failed = await handler.handle("gitops.pr_checks", {"plan": _plan()})
    assert failed["blocker"] == "SOURCE_PR_CI_FAILED"
    fake.pull_requests[("gntik-ai/musematic", 77)]["headRefOid"] = "9" * 40
    moved = await handler.handle("gitops.pr_checks", {"plan": _plan()})
    assert moved["blocker"] == "SOURCE_PR_BINDING_CHANGED"
    assert fake.mutations == []


@pytest.mark.asyncio
async def test_merge_marks_ready_then_merges_the_exact_head(tmp_path: Path) -> None:
    handler, fake = _github(tmp_path)
    fake.pull_requests[("gntik-ai/musematic", 77)] = _pull_request()
    merged = await handler.handle("gitops.merge", {"plan": _plan(), "target": "source"})
    assert merged["status"] == "MERGED" and merged["merge_sha"] == MERGE_SHA
    assert [call[:2] for call in fake.mutations] == [["pr", "ready"], ["pr", "merge"]]
    merge = fake.mutations[1]
    assert ["--merge", "--match-head-commit", HEAD_SHA] == merge[-3:]
    fake.calls.clear()
    again = await handler.handle("gitops.merge", {"plan": _plan(), "target": "source"})
    assert again["status"] == "MERGED" and fake.mutations == []


@pytest.mark.asyncio
async def test_r3_merge_is_blocked_unless_the_project_opts_in(tmp_path: Path) -> None:
    handler, fake = _github(tmp_path)
    fake.pull_requests[("gntik-ai/musematic", 77)] = _pull_request()
    r3 = await handler.handle("gitops.merge", {"plan": _plan(risk_class="R3"), "target": "source"})
    assert r3["status"] == "MERGED"

    strict_root = tmp_path / "strict"
    strict_root.mkdir()
    strict = GitOpsGitHubHandler(
        _settings(
            strict_root,
            project_config_root=_disallow_r3(_config_root(strict_root, safe_repositories=SAFE)),
            github_writes_enabled=True,
        ),
        runner=FakeGh(),  # type: ignore[arg-type]
    )
    blocked = await strict.handle(
        "gitops.merge", {"plan": _plan(risk_class="R3"), "target": "source"}
    )
    assert blocked == {"status": "BLOCKED", "blocker": "R3_MERGE_REQUIRES_APPROVAL"}

    controller = _controller(
        strict_root / "controller",
        FakeKubectl(),
        project_config_root=_disallow_r3(_config_root(strict_root / "controller")),
    )
    change_set_id = _seed_change_set(controller, risk="R3")
    prepared = await controller.handle(
        "gitops.prepare",
        {
            "change_set_id": str(change_set_id),
            "project": "musematic",
            "issue_repository": "gntik-ai/musematic",
            "issue_number": 1026,
        },
    )
    assert prepared == {"status": "BLOCKED", "blocker": "R3_MERGE_REQUIRES_APPROVAL"}


@pytest.mark.asyncio
async def test_merge_waits_for_a_clean_merge_state_and_blocks_conflicts(tmp_path: Path) -> None:
    handler, fake = _github(tmp_path)
    fake.pull_requests[("gntik-ai/musematic", 77)] = _pull_request(isDraft=False)
    pending = await handler.handle("gitops.merge", {"plan": _plan(), "target": "source"})
    assert pending == {"status": "MERGE_PENDING", "target": "source", "merge_state": "BLOCKED"}
    fake.pull_requests[("gntik-ai/musematic", 77)]["mergeStateStatus"] = "DIRTY"
    dirty = await handler.handle("gitops.merge", {"plan": _plan(), "target": "source"})
    assert dirty["blocker"] == "SOURCE_PR_MERGE_CONFLICT"
    assert fake.mutations == []


@pytest.mark.asyncio
async def test_gitops_writes_are_denied_by_the_github_write_policy(tmp_path: Path) -> None:
    handler, fake = _github(tmp_path, safe=("gntik-ai/llmwiki",))
    for step, payload in (
        ("gitops.merge", {"plan": _plan(), "target": "source"}),
        ("gitops.release_tag", {"plan": _plan(), "merge_sha": MERGE_SHA}),
        ("gitops.environment_issue", {"plan": _plan(), "action": "report", "round": 1}),
        ("gitops.close_issue", {"plan": _plan(), "merge_sha": MERGE_SHA}),
    ):
        denied = await handler.handle(step, payload)
        assert denied["status"] == "BLOCKED_BY_POLICY"
        assert denied["reason_code"] == "REPOSITORY_NOT_ALLOWLISTED"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_release_tag_bumps_semver_and_reuses_a_tag_at_the_merge(tmp_path: Path) -> None:
    handler, fake = _github(tmp_path)
    fake.tags = [
        {"ref": "refs/tags/v0.0.9", "sha": "1" * 40, "type": "commit"},
        {"ref": "refs/tags/v0.0.10", "sha": "2" * 40, "type": "commit"},
        {"ref": "refs/tags/v0.0.2", "sha": "3" * 40, "type": "commit"},
        {"ref": "refs/tags/vnext", "sha": "4" * 40, "type": "commit"},
    ]
    created = await handler.handle("gitops.release_tag", {"plan": _plan(), "merge_sha": MERGE_SHA})
    assert created == {"status": "TAG_READY", "release_tag": "v0.0.11", "reused": False}
    posts = [call for call in fake.calls if "POST" in call]
    assert posts == [
        [
            "api",
            "-X",
            "POST",
            "repos/gntik-ai/musematic/git/refs",
            "-f",
            "ref=refs/tags/v0.0.11",
            "-f",
            f"sha={MERGE_SHA}",
            "--jq",
            "{ref: .ref, sha: .object.sha}",
        ]
    ]
    fake.calls.clear()
    reused = await handler.handle("gitops.release_tag", {"plan": _plan(), "merge_sha": MERGE_SHA})
    assert reused == {"status": "TAG_READY", "release_tag": "v0.0.11", "reused": True}
    assert not [call for call in fake.calls if "POST" in call]


@pytest.mark.asyncio
async def test_workflow_run_states(tmp_path: Path) -> None:
    handler, fake = _github(tmp_path)
    payload = {"plan": _plan(), "merge_sha": MERGE_SHA, "workflow_role": "image"}
    assert (await handler.handle("gitops.workflow_run", payload))["status"] == "RUN_PENDING"
    run = {"databaseId": 9, "headSha": MERGE_SHA, "status": "in_progress", "conclusion": ""}
    fake.runs = [run]
    assert (await handler.handle("gitops.workflow_run", payload))["status"] == "RUN_PENDING"
    run.update(status="completed", conclusion="failure")
    failed = await handler.handle("gitops.workflow_run", payload)
    assert failed["blocker"] == "IMAGE_BUILD_FAILED"
    run.update(conclusion="success")
    assert (await handler.handle("gitops.workflow_run", payload))["status"] == "RUN_SUCCEEDED"
    run_list = [call for call in fake.calls if call[:2] == ["run", "list"]][-1]
    assert run_list[run_list.index("--workflow") + 1] == "deploy.yml"
    # musematic leaves main CI unset: asking for it fails closed.
    main = await handler.handle("gitops.workflow_run", {**payload, "workflow_role": "main_ci"})
    assert main == {"status": "BLOCKED", "blocker": "WORKFLOW_NOT_CONFIGURED"}


@pytest.mark.asyncio
async def test_image_pin_detection_reads_the_deploy_values_file(tmp_path: Path) -> None:
    handler, fake = _github(tmp_path)
    key = ("gntik-ai/musematic-deploy", "apps/musematic-platform/values.override.yaml")
    repositories = [
        "ghcr.io/gntik-ai/musematic/control-plane",
        "ghcr.io/gntik-ai/musematic/runtime-controller",
        "ghcr.io/gntik-ai/musematic/reasoning-engine",
        "ghcr.io/gntik-ai/musematic/simulation-controller",
        "ghcr.io/gntik-ai/musematic/sandbox-manager",
        "ghcr.io/gntik-ai/musematic/web",
    ]

    def values(pinned: int) -> str:
        return (
            "".join(
                f"c{index}:\n  image:\n    repository: {repository}\n"
                f"    tag: {EXPECTED_TAG if index < pinned else 'sha-1111111'}\n"
                for index, repository in enumerate(repositories)
            )
            + "secretBlock:\n  password: do-not-leak\n"
        )

    payload = {"plan": _plan(), "merge_sha": MERGE_SHA}
    fake.files[key] = values(0)
    assert (await handler.handle("gitops.image_pin", payload))["status"] == "PIN_PENDING"
    # NEW-93: argocd-image-updater pins images as their builds appear; falcone #956 synced
    # after 5 of 6 pins. Every flow image must carry the merge tag before PINNED.
    fake.files[key] = values(5)
    partial = await handler.handle("gitops.image_pin", payload)
    assert partial["status"] == "PIN_PENDING"
    assert partial["missing_repositories"] == ["ghcr.io/gntik-ai/musematic/web"]
    fake.files[key] = values(6)
    pinned = await handler.handle("gitops.image_pin", payload)
    assert pinned["status"] == "PINNED"
    assert pinned["pinned_repositories"] == sorted(repositories)
    assert pinned["missing_repositories"] == []
    assert pinned["image_tag"] == EXPECTED_TAG
    assert "do-not-leak" not in json.dumps(pinned)
    read = [call for call in fake.calls if call[:3] == ["api", "-X", "GET"]][-1]
    assert "ref=main" in read and "Accept: application/vnd.github.raw" in read


@pytest.mark.asyncio
async def test_environment_issue_is_opened_commented_then_resolved(tmp_path: Path) -> None:
    handler, fake = _github(tmp_path)
    plan = _plan()
    diagnosis = {
        "argo_health": "Degraded",
        "unhealthy_pods": [
            {"namespace": "musematic-platform", "name": "web-1", "reason": "CrashLoopBackOff"}
        ],
        "unready_workloads": [],
        "stores_not_ready": [],
    }
    opened = await handler.handle(
        "gitops.environment_issue",
        {"plan": plan, "action": "report", "round": 1, "diagnosis": diagnosis},
    )
    assert opened["status"] == "ISSUE_OPENED" and opened["issue_number"] == 500
    issue = fake.issues[("gntik-ai/musematic", 500)]
    assert "web-1" in issue["body"] and "CrashLoopBackOff" in issue["body"]
    assert issue["title"].endswith(plan["change_set_id"])

    # A new round finds the open issue opened by this flow and comments on it.
    commented = await handler.handle(
        "gitops.environment_issue",
        {"plan": plan, "action": "report", "round": 2, "diagnosis": diagnosis},
    )
    assert commented["status"] == "ISSUE_COMMENTED" and commented["issue_number"] == 500
    assert len(issue["comments"]) == 2

    resolved = await handler.handle(
        "gitops.environment_issue", {"plan": plan, "action": "resolve", "issue_number": 500}
    )
    assert resolved["status"] == "ISSUE_RESOLVED"
    assert issue["state"] == "CLOSED"
    assert "healthy again" in issue["comments"][-1]["body"]


@pytest.mark.asyncio
async def test_close_issue_comments_delivery_evidence_and_closes(tmp_path: Path) -> None:
    handler, fake = _github(tmp_path)
    fake.issues[("gntik-ai/musematic", 1026)] = {
        "number": 1026,
        "title": "bug",
        "state": "OPEN",
        "comments": [],
        "url": "https://github.com/gntik-ai/musematic/issues/1026",
        "closedAt": None,
    }
    result = await handler.handle(
        "gitops.close_issue",
        {
            "plan": _plan(),
            "merge_sha": MERGE_SHA,
            "release_tag": "v0.0.11",
            "pinned_repositories": ["ghcr.io/gntik-ai/musematic/web"],
            "argo_revision": MERGE_SHA,
        },
    )
    assert result["status"] == "ISSUE_CLOSED"
    issue = fake.issues[("gntik-ai/musematic", 1026)]
    body = issue["comments"][0]["body"]
    for expected in (
        "https://github.com/gntik-ai/musematic/pull/77",
        MERGE_SHA,
        EXPECTED_TAG,
        "v0.0.11",
        "musematic-platform",
    ):
        assert expected in body
    assert issue["state"] == "CLOSED"
    # Idempotent after the issue closed (e.g. auto-closed by the merge).
    fake.calls.clear()
    again = await handler.handle("gitops.close_issue", {"plan": _plan(), "merge_sha": MERGE_SHA})
    assert again["status"] == "ISSUE_CLOSED"
    assert fake.mutations == []


# -- dispatch through the real ControlActivities path ---------------------------------


@pytest.mark.asyncio
async def test_gitops_controller_step_is_wired_to_a_production_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("engineering_control.activities.core.activity.heartbeat", lambda _: None)
    activities = ControlActivities(_settings(tmp_path), "controller")
    result = await activities.execute_control_step(
        {"step": "gitops.flow_for_project", "payload": {"project": "musematic"}}
    )
    assert result["status"] == "ENABLED" and result["enabled"] is True


@pytest.mark.asyncio
async def test_gitops_github_step_is_wired_to_a_production_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("engineering_control.activities.core.activity.heartbeat", lambda _: None)
    activities = ControlActivities(_settings(tmp_path, github_writes_enabled=False), "github")
    result = await activities.execute_control_step(
        {"step": "gitops.merge", "payload": {"plan": _plan(), "target": "source"}}
    )
    # Reached GitOpsGitHubHandler (not "no configured handler"), denied before any gh call.
    assert result["status"] == "BLOCKED_BY_POLICY"
    assert result["external_command_started"] is False


def test_gitops_steps_are_owned_by_exactly_one_worker() -> None:
    controller = ControlActivities.steps_for_worker("controller")
    github = ControlActivities.steps_for_worker("github")
    for step in GitOpsControllerHandler.supported_steps:
        assert step in controller and step not in github
    for step in GitOpsGitHubHandler.supported_steps:
        assert step in github and step not in controller


# -- NEW-63: baseline-aware CI gate ----------------------------------------------------

MAIN_SHA = "b" * 40


MAIN_DATE = "2026-09-26T11:00:00Z"
FRESH = "2026-09-26T12:00:00Z"


def _check(name: str, conclusion: str | None, started: str = FRESH) -> dict[str, Any]:
    if conclusion is None:
        return {
            "workflowName": "CI",
            "name": name,
            "status": "IN_PROGRESS",
            "conclusion": "",
            "startedAt": started,
        }
    return {
        "workflowName": "CI",
        "name": name,
        "status": "COMPLETED",
        "conclusion": conclusion,
        "startedAt": started,
    }


class BaselineGh(FakeGh):
    """FakeGh plus the branch, contents and PR calls of the CI baseline."""

    def __init__(self, changed: list[str]) -> None:
        super().__init__()
        self.changed = changed
        self.branches: dict[str, str] = {}
        self.markers: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.baseline_checks: list[dict[str, Any]] = []
        self.reopened: list[str] = []

    def run(self, executable: str, arguments: Sequence[str], **kwargs: Any) -> CommandResult:
        args = list(arguments)
        if args[0] == "api" and "/commits/hermes/ci-baseline/" in args[1]:
            self.calls.append(args)
            branch = args[1].split("/commits/", 1)[1]
            if branch not in self.branches:
                raise CommandFailure("gh api failed: 404")
            return CommandResult(
                0, json.dumps({"sha": self.branches[branch], "tree": "f" * 40}), ""
            )
        return super().run(executable, arguments, **kwargs)

    job_logs: dict[int, str] = {}

    def _answer(self, args: list[str]) -> str:
        if args[0] == "api" and "/actions/jobs/" in args[1] and args[1].endswith("/logs"):
            job = int(args[1].split("/actions/jobs/", 1)[1].split("/", 1)[0])
            if job not in self.job_logs:
                raise CommandFailure("gh api failed: 404")
            return self.job_logs[job]
        if args[0] == "api" and args[1].endswith("/pulls/77/files"):
            return "\n".join(self.changed)
        if args[0] == "api" and args[1].endswith("/commits/main"):
            if ".commit.committer.date" in args:
                return MAIN_DATE + "\n"
            return json.dumps({"sha": MAIN_SHA, "tree": "f" * 40})
        if args[0] == "api" and args[1].endswith("/issues/77/events"):
            return "".join(f"{value}\n" for value in self.reopened)
        if args[:2] == ["pr", "reopen"]:
            self.pull_requests[(args[4], int(args[2]))]["state"] = "OPEN"
            self.reopened.append(FRESH)
            return ""
        if args[:3] == ["api", "-X", "POST"] and "refs/heads/" in " ".join(args):
            ref = args[args.index("-f") + 1].removeprefix("ref=refs/heads/")
            sha = args[args.index("-f", args.index("-f") + 1) + 1].removeprefix("sha=")
            self.branches[ref] = sha
            return json.dumps({"ref": f"refs/heads/{ref}", "sha": sha})
        if args[:3] == ["api", "-X", "PUT"]:
            path = args[3].split("/contents/", 1)[1]
            branch = next(v for v in args if v.startswith("branch=")).removeprefix("branch=")
            self.markers.append((branch, path))
            return "c" * 40
        if args[:3] == ["api", "-X", "DELETE"]:
            branch = args[3].split("/git/refs/heads/", 1)[1]
            self.branches.pop(branch, None)
            self.deleted.append(branch)
            return ""
        if args[:2] == ["pr", "list"]:
            head = args[args.index("--head") + 1]
            return json.dumps(
                [pull for pull in self.pull_requests.values() if pull["headRefName"] == head]
            )
        if args[:2] == ["pr", "create"]:
            head = args[args.index("--head") + 1]
            self.pull_requests[("gntik-ai/musematic", 900)] = {
                "number": 900,
                "url": "https://github.com/gntik-ai/musematic/pull/900",
                "state": "OPEN",
                "isDraft": True,
                "headRefName": head,
                "headRefOid": self.branches[head],
                "baseRefName": "main",
                "mergeStateStatus": "UNSTABLE",
                "statusCheckRollup": [],
            }
            return "https://github.com/gntik-ai/musematic/pull/900\n"
        if args[:2] == ["pr", "close"]:
            self.pull_requests[(args[4], int(args[2]))]["state"] = "CLOSED"
            return ""
        if args[:2] == ["pr", "ready"]:
            # Failing non-required checks keep the PR UNSTABLE after ready-for-review.
            self.pull_requests[(args[4], int(args[2]))]["isDraft"] = False
            return ""
        return super()._answer(args)

    def finish_baseline(self, checks: list[dict[str, Any]]) -> None:
        self.pull_requests[("gntik-ai/musematic", 900)]["statusCheckRollup"] = checks


def _baseline_handler(
    tmp_path: Path, changed: list[str], pr_checks: list[dict[str, Any]]
) -> tuple[GitOpsGitHubHandler, BaselineGh]:
    fake = BaselineGh(changed)
    settings = _settings(
        tmp_path,
        project_config_root=_config_root(tmp_path, safe_repositories=SAFE),
        github_writes_enabled=True,
    )
    fake.pull_requests[("gntik-ai/musematic", 77)] = _pull_request(
        statusCheckRollup=pr_checks, mergeStateStatus="UNSTABLE"
    )
    return GitOpsGitHubHandler(settings, runner=fake), fake  # type: ignore[arg-type]


PR_FAILURES = [
    _check("Lint Frontend", "FAILURE"),
    _check("Type-check Python", "FAILURE"),
    _check("Test Python", "SUCCESS"),
    _check("CI gate", "FAILURE"),
]


@pytest.mark.asyncio
async def test_baseline_gate_passes_when_every_failure_also_fails_on_main(tmp_path: Path) -> None:
    changed = ["apps/web/components/Panel.tsx", "apps/control-plane/src/platform/x.py"]
    handler, fake = _baseline_handler(tmp_path, changed, PR_FAILURES)

    started = await handler.handle("gitops.pr_checks", {"plan": _plan()})
    assert started == {"status": "CHECKS_PENDING", "reason": "CI_BASELINE_STARTED"}
    [branch] = fake.branches
    assert branch.startswith(f"hermes/ci-baseline/{MAIN_SHA[:12]}-")
    assert fake.branches[branch] == MAIN_SHA
    assert sorted(path for _, path in fake.markers) == [
        "apps/control-plane/tests/hermes_ci_baseline_marker.py",
        "apps/web/.hermes-ci-baseline",
    ]

    fake.finish_baseline([_check("Lint Frontend", None)])
    pending = await handler.handle("gitops.pr_checks", {"plan": _plan()})
    assert pending == {"status": "CHECKS_PENDING", "reason": "CI_BASELINE_PENDING"}

    fake.finish_baseline(
        [
            _check("Lint Frontend", "FAILURE"),
            _check("Type-check Python", "FAILURE"),
            _check("Test Python", "SUCCESS"),
            _check("CI gate", "FAILURE"),
        ]
    )
    passed = await handler.handle("gitops.pr_checks", {"plan": _plan()})
    assert passed["status"] == "CHECKS_PASSED"
    assert passed["ci_baseline"]["preexisting_failures"] == [
        "CI gate",
        "Lint Frontend",
        "Type-check Python",
    ]
    assert fake.pull_requests[("gntik-ai/musematic", 900)]["state"] == "CLOSED"
    assert fake.deleted == [branch]

    # The UNSTABLE source PR merges only because the recorded baseline still covers it.
    merged = await handler.handle("gitops.merge", {"plan": _plan(), "target": "source"})
    assert merged["status"] == "MERGED"


@pytest.mark.asyncio
async def test_baseline_gate_blocks_failures_that_pass_on_main(tmp_path: Path) -> None:
    handler, fake = _baseline_handler(tmp_path, ["apps/web/app/page.tsx"], PR_FAILURES)
    await handler.handle("gitops.pr_checks", {"plan": _plan()})
    fake.finish_baseline(
        [
            _check("Lint Frontend", "SUCCESS"),
            _check("Type-check Python", "FAILURE"),
            _check("CI gate", "FAILURE"),
        ]
    )

    blocked = await handler.handle("gitops.pr_checks", {"plan": _plan()})

    assert blocked["status"] == "BLOCKED"
    assert blocked["blocker"] == "SOURCE_PR_CI_REGRESSION"
    assert blocked["failed_checks"] == ["Lint Frontend"]
    assert blocked["preexisting_checks"] == ["CI gate", "Type-check Python"]
    merge = await handler.handle("gitops.merge", {"plan": _plan(), "target": "source"})
    assert merge == {"status": "BLOCKED", "blocker": "SOURCE_PR_CI_REGRESSION"}
    assert not any(call[:2] == ["pr", "merge"] for call in fake.calls)


@pytest.mark.asyncio
async def test_baseline_gate_waits_for_pending_pr_checks_and_skips_unmapped_paths(
    tmp_path: Path,
) -> None:
    handler, fake = _baseline_handler(
        tmp_path, ["apps/web/x.tsx"], [*PR_FAILURES, _check("Build documentation", None)]
    )
    pending = await handler.handle("gitops.pr_checks", {"plan": _plan()})
    assert pending == {"status": "CHECKS_PENDING", "reason": "SOURCE_PR_CHECKS_PENDING"}
    assert fake.branches == {}

    other, other_fake = _baseline_handler(
        tmp_path / "other", [".github/workflows/ci.yml"], PR_FAILURES
    )
    blocked = await other.handle("gitops.pr_checks", {"plan": _plan()})
    # NEW-99: nothing is excused, but every failure goes to CI repair as a regression.
    assert blocked["blocker"] == "SOURCE_PR_CI_REGRESSION"
    assert blocked["reason"] == "CI_BASELINE_CI_CONFIG_CHANGED"
    assert blocked["failed_checks"] == sorted(
        str(check["name"]) for check in PR_FAILURES if check.get("conclusion") == "FAILURE"
    )
    assert blocked["preexisting_checks"] == []
    assert other_fake.branches == {}
    merge = await other.handle("gitops.merge", {"plan": _plan(), "target": "source"})
    assert merge == {"status": "BLOCKED", "blocker": "SOURCE_PR_CI_REGRESSION"}
    assert not any(call[:2] == ["pr", "merge"] for call in other_fake.calls)

    # A mixed PR (covered app path plus CI configuration) gets no exemption either.
    mixed, mixed_fake = _baseline_handler(
        tmp_path / "mixed", ["apps/web/x.tsx", ".github/workflows/ci.yml"], PR_FAILURES
    )
    blocked = await mixed.handle("gitops.pr_checks", {"plan": _plan()})
    assert blocked["blocker"] == "SOURCE_PR_CI_REGRESSION"
    assert blocked["reason"] == "CI_BASELINE_CI_CONFIG_CHANGED"
    assert mixed_fake.branches == {}

    # A rename out of .github/ still counts as touching .github/ (previous_filename).
    renamed, renamed_fake = _baseline_handler(
        tmp_path / "renamed", ["apps/web/ci.yml\t.github/workflows/ci.yml"], PR_FAILURES
    )
    blocked = await renamed.handle("gitops.pr_checks", {"plan": _plan()})
    assert blocked["reason"] == "CI_BASELINE_CI_CONFIG_CHANGED"
    assert renamed_fake.branches == {}

    # A listing at GitHub's 3000-file cap may be truncated: no exemption.
    capped, capped_fake = _baseline_handler(
        tmp_path / "capped", [f"apps/web/f{index}.tsx" for index in range(3000)], PR_FAILURES
    )
    blocked = await capped.handle("gitops.pr_checks", {"plan": _plan()})
    assert blocked["reason"] == "CI_BASELINE_FILE_LISTING_TRUNCATED"
    assert blocked["blocker"] == "SOURCE_PR_CI_REGRESSION"
    assert blocked["preexisting_checks"] == []
    assert capped_fake.branches == {}

    uncovered, uncovered_fake = _baseline_handler(
        tmp_path / "uncovered", ["unmapped/x.txt"], PR_FAILURES
    )
    blocked = await uncovered.handle("gitops.pr_checks", {"plan": _plan()})
    assert blocked["reason"] == "CI_BASELINE_NOT_APPLICABLE"
    assert uncovered_fake.branches == {}


@pytest.mark.asyncio
async def test_baseline_gate_restarts_a_half_created_baseline_branch(tmp_path: Path) -> None:
    handler, fake = _baseline_handler(tmp_path, ["apps/web/x.tsx"], PR_FAILURES)
    await handler.handle("gitops.pr_checks", {"plan": _plan()})
    [branch] = fake.branches
    del fake.pull_requests[("gntik-ai/musematic", 900)]  # the PR step never happened
    fake.markers.clear()

    again = await handler.handle("gitops.pr_checks", {"plan": _plan()})

    assert again["reason"] == "CI_BASELINE_STARTED"
    assert fake.deleted == [branch] and branch in fake.branches
    assert fake.markers == [(branch, "apps/web/.hermes-ci-baseline")]


def test_broker_refuses_baseline_writes_outside_the_baseline_prefix(tmp_path: Path) -> None:
    handler, _ = _baseline_handler(tmp_path, [], PR_FAILURES)
    broker = handler.broker
    binding = f"changeset:{UUID(int=7)}"
    with pytest.raises(GhCliPolicyError):
        broker.delete_baseline_branch(
            repository="gntik-ai/musematic", branch="main", change_binding=binding
        )
    with pytest.raises(GhCliPolicyError):
        broker.put_baseline_marker(
            repository="gntik-ai/musematic",
            branch="hermes/ci-baseline/bbbbbbbbbbbb-12345678",
            path=".github/workflows/ci.yml",
            change_binding=binding,
        )


def test_github_handler_creates_its_own_gh_working_directory(tmp_path: Path) -> None:
    # NEW-65: the first gitops.pr_checks in production failed with "working directory
    # does not exist: /srv/engineering/runs/gitops" (ValueError -> CHECKS_UNAVAILABLE).
    handler, _ = _github(tmp_path)
    assert handler.root == (tmp_path / "runs" / "gitops-github").resolve()
    assert handler.root.is_dir()


@pytest.mark.asyncio
async def test_stale_pr_ci_is_rerun_on_current_main_before_the_baseline(tmp_path: Path) -> None:
    # NEW-67: #1121's CI ran before main gained the govulncheck pin; comparing it with a
    # baseline on the new main made govulncheck a false regression.
    stale = [_check("govulncheck", "FAILURE", started="2026-09-26T07:20:20Z")]
    handler, fake = _baseline_handler(tmp_path, ["apps/web/x.tsx"], stale)

    rerun = await handler.handle("gitops.pr_checks", {"plan": _plan()})
    assert rerun == {"status": "CHECKS_PENDING", "reason": "SOURCE_PR_CI_RERUN_STARTED"}
    assert [call[:2] for call in fake.calls if call[:2] in (["pr", "close"], ["pr", "reopen"])] == [
        ["pr", "close"],
        ["pr", "reopen"],
    ]
    assert fake.branches == {}  # no baseline is opened against stale PR results

    waiting = await handler.handle("gitops.pr_checks", {"plan": _plan()})
    assert waiting == {"status": "CHECKS_PENDING", "reason": "SOURCE_PR_CI_RERUN_PENDING"}
    assert len(fake.reopened) == 1

    merge = await handler.handle("gitops.merge", {"plan": _plan(), "target": "source"})
    assert merge == {"status": "BLOCKED", "blocker": "SOURCE_PR_CI_STALE"}


RELEASE_SHA = "e" * 40


class ReleaseGh(FakeGh):
    """FakeGh plus the compare API used to find a later release containing a merge."""

    def __init__(self, contains: bool = True) -> None:
        super().__init__()
        self.contains = contains

    def _answer(self, args: list[str]) -> str:
        if args[0] == "api" and "/compare/" in args[1]:
            return "ahead\n" if self.contains else "diverged\n"
        return super()._answer(args)


@pytest.mark.asyncio
async def test_a_later_release_containing_the_merge_delivers_a_failed_release(
    tmp_path: Path,
) -> None:
    # NEW-68: musematic v0.0.108 (the #993 merge) was blocked by the release supply-chain
    # gate; the change ships in the next release whose commit contains the merge.
    fake = ReleaseGh()
    settings = _settings(
        tmp_path,
        project_config_root=_config_root(tmp_path, safe_repositories=SAFE),
        github_writes_enabled=True,
    )
    handler = GitOpsGitHubHandler(settings, runner=fake)  # type: ignore[arg-type]
    fake.tags = [
        {"ref": "refs/tags/v0.0.107", "sha": "1" * 40, "type": "commit"},
        {"ref": "refs/tags/v0.0.108", "sha": MERGE_SHA, "type": "commit"},
    ]
    fake.runs = [
        {"databaseId": 1, "headSha": MERGE_SHA, "status": "completed", "conclusion": "failure"}
    ]
    payload = {"plan": _plan(), "merge_sha": MERGE_SHA, "workflow_role": "image"}

    blocked = await handler.handle("gitops.workflow_run", payload)
    assert blocked["blocker"] == "IMAGE_BUILD_FAILED"  # no later release yet

    fake.tags.append({"ref": "refs/tags/v0.0.109", "sha": RELEASE_SHA, "type": "commit"})
    later = {"databaseId": 2, "headSha": RELEASE_SHA, "status": "in_progress", "conclusion": ""}
    fake.runs.append(later)
    pending = await handler.handle("gitops.workflow_run", payload)
    assert pending["status"] == "RUN_PENDING" and pending["release_tag"] == "v0.0.109"

    later.update(status="completed", conclusion="success")
    shipped = await handler.handle("gitops.workflow_run", payload)
    assert shipped["status"] == "RUN_SUCCEEDED"
    assert (shipped["release_tag"], shipped["release_sha"]) == ("v0.0.109", RELEASE_SHA)

    fake.contains = False  # a later tag that does not contain the merge never counts
    assert (await handler.handle("gitops.workflow_run", payload))["blocker"] == "IMAGE_BUILD_FAILED"


@pytest.mark.asyncio
async def test_controller_kubectl_runs_in_the_writable_state_root(tmp_path: Path) -> None:
    # NEW-81: the first production environment check failed with a read-only OSError:
    # engineering-temporal-worker may write only its state root and the evidence root.
    kubectl = FakeKubectl()
    handler = _controller(tmp_path, kubectl)
    change_set_id = _seed_change_set(handler)
    prepared = await handler.handle(
        "gitops.prepare",
        {
            "change_set_id": str(change_set_id),
            "project": "musematic",
            "issue_repository": "gntik-ai/musematic",
            "issue_number": 1026,
        },
    )
    await handler.handle("gitops.environment_check", {"plan": prepared["plan"]})

    assert kubectl.cwds and all(
        cwd == (tmp_path / "state" / "gitops").resolve() for cwd in kubectl.cwds
    )
    assert not (tmp_path / "runs" / "gitops").exists()


_REPAIR_LOG = """\
2026-09-29T16:53:56.1Z ok 277 - ut-flowrole: create works
2026-09-29T16:53:56.2Z # Subtest: ut-flowrole: tenant_owner is authorized on delete_definition
2026-09-29T16:53:56.3Z not ok 278 - ut-flowrole: tenant_owner is authorized on delete_definition
2026-09-29T16:53:56.4Z   error: 'Flow not found'
2026-09-29T16:53:56.5Z   code: 'FLOW_NOT_FOUND'
2026-09-29T16:53:56.6Z ok 279 - ut-flowrole: failed operation creates a pending retry attempt
2026-09-29T16:53:56.7Z ##[error]Process completed with exit code 1.
"""


def test_failure_excerpt_keeps_failing_lines_and_drops_passing_ones() -> None:
    # NEW-100: the #1001 CI-repair maker saw only job names and could not act.
    from engineering_control.adapters.github.client import failure_excerpt

    excerpt = failure_excerpt(_REPAIR_LOG, context=2)
    assert "not ok 278" in excerpt and "FLOW_NOT_FOUND" in excerpt
    assert "##[error]Process completed" in excerpt
    assert "2026-09-29T" not in excerpt  # timestamps stripped
    assert "ok 277" not in excerpt  # passing lines are never markers
    long = failure_excerpt("not ok 1 - x\n" * 10_000)
    assert len(long) <= 4000 and long.count("\n") < 60


@pytest.mark.asyncio
async def test_unexempted_regressions_carry_bounded_failure_excerpts(tmp_path: Path) -> None:
    job_url = "https://github.com/gntik-ai/musematic/actions/runs/5/job/{}"
    checks = [
        {**_check("quality", "FAILURE"), "detailsUrl": job_url.format(11)},
        {**_check("relay", "FAILURE"), "detailsUrl": job_url.format(12)},
        {**_check("docs", "FAILURE"), "detailsUrl": "https://example.invalid/x"},
        {**_check("lint", "SUCCESS"), "detailsUrl": job_url.format(13)},
    ]
    handler, fake = _baseline_handler(tmp_path, [".github/workflows/ci.yml"], checks)
    fake.job_logs = {11: _REPAIR_LOG}  # job 12's log is unavailable (404)

    blocked = await handler.handle("gitops.pr_checks", {"plan": _plan()})

    assert blocked["blocker"] == "SOURCE_PR_CI_REGRESSION"
    assert set(blocked["failure_excerpts"]) == {"quality"}
    assert "FLOW_NOT_FOUND" in blocked["failure_excerpts"]["quality"]
    assert not any("/actions/jobs/13/" in " ".join(call) for call in fake.calls)
