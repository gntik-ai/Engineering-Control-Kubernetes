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
    classify_degraded,
    classify_sync_failure,
    evaluate_pull_request_checks,
    image_tags_in_values,
    next_release_tag,
    overall_failure_class,
    pod_failure_reason,
    render_sync_failure,
    sealed_secret_stores,
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
        "metadata": {"name": "musematic-platform", "namespace": "argocd", "resourceVersion": "100"},
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
        eso_log: str | None = None,
    ) -> None:
        self.pods = pods or {}
        # NEW-155: the external-secrets controller log; None = unreadable.
        self.eso_log = eso_log
        self.deployments = deployments or {}
        self.statefulsets = statefulsets or {}
        self.externalsecrets = externalsecrets
        self.stores = stores or {}
        self.application = application or _application()
        self.calls: list[list[str]] = []
        self.cwds: list[Path] = []
        # Called before a JSON patch applies: lets a test move the Application under us.
        self.before_patch: Any = None
        self.reject_patches = False

    def _bump(self) -> None:
        metadata = self.application.setdefault("metadata", {})
        metadata["resourceVersion"] = str(int(metadata.get("resourceVersion") or "0") + 1)

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
            if self.before_patch is not None:
                self.before_patch(self)
            if self.reject_patches:
                return CommandResult(1, "", "forbidden")
            if patch_type == "json":
                for operation in patch:
                    if operation["op"] != "test":
                        continue
                    parts = operation["path"].strip("/").split("/")
                    target = self.application
                    for part in parts:
                        if isinstance(target, list) and part.isdigit():
                            target = target[int(part)] if int(part) < len(target) else None
                        else:
                            target = target.get(part) if isinstance(target, dict) else None
                    if target != operation["value"]:
                        return CommandResult(1, "", "test failed")
                for operation in patch:
                    parts = operation["path"].strip("/").split("/")
                    target = self.application
                    for part in parts[:-1]:
                        target = target[int(part)] if isinstance(target, list) else target[part]
                    if operation["op"] == "remove":
                        # RFC 6902: removing an absent member fails the whole patch.
                        if parts[-1] not in target:
                            return CommandResult(1, "", "Unprocessable Entity")
                        del target[parts[-1]]
                    elif operation["op"] != "test":
                        target[parts[-1]] = operation["value"]
                self._bump()
            else:
                self.application.update(patch)
            return CommandResult(0, "patched", "")
        if verb == "logs":
            if self.eso_log is None:
                return CommandResult(1, "", "logs unavailable")
            return CommandResult(0, self.eso_log, "")
        if verb in {"annotate", "delete", "rollout"}:
            if verb == "annotate":
                self._bump()
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
        "sync": {"prune": False, "syncStrategy": {"hook": {}}},
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
        ],
        # NEW-113: the complete operation, conditioned on the resourceVersion read after
        # the chart pin and the hard-refresh annotation.
        [
            {"op": "test", "path": "/metadata/resourceVersion", "value": "102"},
            {
                "op": "add",
                "path": "/operation",
                "value": {
                    "initiatedBy": {"username": "hermes-engineering"},
                    "sync": {"prune": False, "syncStrategy": {"hook": {}}},
                },
            },
            # NEW-116: the previous operation state is cleared with the write, so the
            # controller cannot merge the new operation into it.
            {"op": "remove", "path": "/status/operationState"},
        ],
    ]
    assert kubectl.application["operation"]["initiatedBy"]["username"] == "hermes-engineering"

    # Idempotent: an already-pinned source is not patched again.
    kubectl.application.pop("operation")
    kubectl.calls.clear()
    again = await handler.handle("gitops.trigger_sync", {"plan": _plan(), "merge_sha": MERGE_SHA})
    assert again["chart_pin"]["changed"] is False
    assert not [call for call in kubectl.calls if "--patch" in call and "/spec/" in call[-1]]


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
    # NEW-116: SYNCED needs the operation to have synced the pinned chart revision.
    kubectl.application["status"]["operationState"]["syncResult"] = {
        "revisions": [OLD_CHART_REVISION, "e" * 40]
    }
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
    # NEW-102: a merged ChangeSet records the plan's delivered head and SOURCE_MERGED, even
    # after a late repair write reset it to PLANNED with an unpublished head.
    with handler.sessions.begin() as session:
        stale = session.get(db.ChangeSet, change_set_id)
        assert stale is not None
        stale.source_head_sha = "9" * 40
        stale.state = "PLANNED"
    await handler.handle(
        "gitops.persist_result",
        {"plan": plan, "outcome": "BLOCKED", "blocker": "IMAGE_NOT_PINNED", "merge_sha": MERGE_SHA},
    )
    with handler.sessions.begin() as session:
        merged = session.get(db.ChangeSet, change_set_id)
        assert merged is not None and merged.source_head_sha == plan["source_head_sha"]
        assert merged.state == "SOURCE_MERGED"
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
        max_output_bytes: int | None = None,
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binding_activity_environment: Any
) -> None:
    monkeypatch.setattr("engineering_control.activities.core.activity.heartbeat", lambda _: None)
    from engineering_control.db.base import Base

    # Merged with 2b678d9: controller production steps run inside a verified Run binding.
    activities = ControlActivities(_settings(tmp_path), "controller")
    assert activities.metadata_handler is not None
    Base.metadata.create_all(activities.metadata_handler.engine)
    result = await binding_activity_environment.run(
        activities.execute_control_step,
        {"step": "gitops.flow_for_project", "payload": {"project": "musematic"}},
    )
    assert result["status"] == "ENABLED" and result["enabled"] is True
    activities.metadata_handler.engine.dispose()


@pytest.mark.asyncio
async def test_a_registered_gitops_child_enrolls_its_run_and_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # NEW-112 review: the backlog loop pre-registers the staging-gitops child Run without
    # an execution binding; its first controller step must enroll it from the start memo.
    from dataclasses import replace

    from temporalio.exceptions import ApplicationError
    from temporalio.testing import ActivityEnvironment
    from test_start_proof import NAMESPACE, WORKFLOW_ID, make_fixture

    monkeypatch.setattr("engineering_control.activities.core.activity.heartbeat", lambda _: None)
    activities = ControlActivities(_settings(tmp_path), "controller")
    assert activities.metadata_handler is not None
    Base.metadata.create_all(activities.metadata_handler.engine)
    run_id = uuid4()
    with activities.metadata_handler.sessions.begin() as session:
        session.add(
            db.Run(
                id=run_id,
                workflow_id=WORKFLOW_ID,
                run_type="staging-gitops",
                status="QUEUED",
                project="musematic",
            )
        )

    async def run_step(workflow_type: str) -> dict[str, Any]:
        fixture = await make_fixture(app_id=str(run_id), workflow_type=workflow_type)
        environment = ActivityEnvironment(client=fixture.client)
        environment.info = replace(
            environment.info,
            namespace=NAMESPACE,
            workflow_id=WORKFLOW_ID,
            workflow_run_id=fixture.current,
        )
        return await environment.run(
            activities.execute_control_step,
            {"step": "gitops.flow_for_project", "payload": {"project": "musematic"}},
        )

    # The memo must come from the exact child workflow type the Run kind names.
    with pytest.raises(ApplicationError, match="Existing Run identity is unverifiable"):
        await run_step("AuditCampaignWorkflow")
    result = await run_step("StagingGitOpsDeliveryWorkflow")
    assert result["status"] == "ENABLED" and result["enabled"] is True
    with activities.metadata_handler.sessions() as session:
        run = session.get(db.Run, run_id)
        assert run is not None and run.temporal_first_execution_run_id is not None
    activities.metadata_handler.engine.dispose()


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
    # Go test output: "--- FAIL" and "got ...; want ..." far from the final error line.
    go_log = "--- FAIL: TestDelete (0.01s)\n    store_test.go:42: got nil; want flow\n"
    go = failure_excerpt(go_log + "=== RUN TestOther\n" * 50 + "##[error]exit 1\n", context=1)
    assert "--- FAIL: TestDelete" in go and "got nil; want flow" in go
    # NEW-104: a failed dependency audit names each high/critical advisory and its fix,
    # keeping exactly that advisory's table (no neighbouring advisory bleeds in).
    ruling = "├─────────────────────┼──────────────────────────────────────────┤\n"
    audit_log = (
        "Progress: resolved 838, reused 833\n" * 40
        + "┌─────────────────────┬──────────────────────────────────────────┐\n"
        + "│ moderate            │ esbuild dev server CORS Error handling                 │\n"
        + ruling
        + "│ Package             │ esbuild                                                │\n"
        + ruling
        + "│ Patched versions    │ >=0.25.0                                               │\n"
        + "└─────────────────────┴──────────────────────────────────────────┘\n"
        + "┌─────────────────────┬──────────────────────────────────────────┐\n"
        + "│ high                │ @grpc/grpc-js: In certain configurations,              │\n"
        + ruling
        + "│ Package             │ @grpc/grpc-js                                          │\n"
        + ruling
        + "│ Vulnerable versions │ <1.14.5                                                │\n"
        + ruling
        + "│ Patched versions    │ >=1.14.5                                               │\n"
        + ruling
        + "│ Paths               │ apps/control-plane-executor > @grpc/grpc-js@1.14.4     │\n"
        + ruling
        + "│ More info           │ https://github.com/advisories/GHSA-m9gg-hp2v-232j      │\n"
        + "└─────────────────────┴──────────────────────────────────────────┘\n"
        + "Severity: 4 low | 17 moderate | 4 high\n"
        + " ELIFECYCLE  Command failed with exit code 1.\n"
        + "##[error]Process completed with exit code 1.\n"
    )
    audit = failure_excerpt(audit_log)
    assert "@grpc/grpc-js: In certain configurations" in audit
    assert "Patched versions    │ >=1.14.5" in audit and "GHSA-m9gg-hp2v-232j" in audit
    assert "Paths               │ apps/control-plane-executor" in audit
    assert "esbuild" not in audit and ">=0.25.0" not in audit  # no moderate bleed-through
    assert "├" not in audit and "Progress: resolved" not in audit
    npm_audit = failure_excerpt(
        "undici  <6.24.0\nSeverity: high\nDenial of Service\nfix available\n"
    )
    assert "undici  <6.24.0" in npm_audit and "Severity: high" in npm_audit
    # Box drawings outside an audit table (e.g. a snapshot diff) are kept verbatim.
    snapshot = failure_excerpt(
        "not ok 3 - renders the panel\n  expected:\n  ┌──────┐\n  │ OK   │\n"
        "  ├──────┤\n  │ Save │\n  └──────┘\n"
    )
    assert all(part in snapshot for part in ("┌──────┐", "├──────┤", "│ Save │", "└──────┘"))
    # Many moderate advisories (even ones mentioning "Error") never crowd out a high one.
    moderate = (
        "┌──┐\n│ moderate │ parser Error recovery │\n├──┤\n│ Package │ other │\n└──┘\n"
    ) * 30
    crowded = failure_excerpt(moderate + audit_log[audit_log.index("┌", 1000) :])
    assert "@grpc/grpc-js" in crowded and "other" not in crowded
    # A truncated advisory (no closing ruling) never swallows the job's error lines.
    truncated = failure_excerpt(
        "┌──┐\n│ moderate │ parser │\n├──┤\n│ Package │ other │\n"
        "Process exited\n##[error]Process completed with exit code 1.\n"
    )
    assert "##[error]Process completed" in truncated
    # A snapshot box with a severity-looking row but no Package row is not an audit table.
    boxed = failure_excerpt(
        "not ok 4 - risk badge\n  ┌──────┐\n  │ high │ badge │\n  ├──────┤\n  └──────┘\n"
    )
    assert "├──────┤" in boxed
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
    # Log reads run no extra preflight (repo view) beyond the gate's own.
    first_log = next(
        index for index, call in enumerate(fake.calls) if "/actions/jobs/" in " ".join(call)
    )
    assert not any(call[:2] == ["repo", "view"] for call in fake.calls[first_log:])


@pytest.mark.asyncio
async def test_unreadable_job_logs_do_not_use_up_the_excerpt_budget(tmp_path: Path) -> None:
    job_url = "https://github.com/gntik-ai/musematic/actions/runs/5/job/{}"
    checks = [
        {**_check(f"job-{index}", "FAILURE"), "detailsUrl": job_url.format(100 + index)}
        for index in range(8)
    ]
    handler, fake = _baseline_handler(tmp_path, [".github/workflows/ci.yml"], checks)
    fake.job_logs = {106: "not ok 1 - six", 107: "not ok 1 - seven"}  # the rest are 404

    blocked = await handler.handle("gitops.pr_checks", {"plan": _plan()})

    assert set(blocked["failure_excerpts"]) == {"job-6", "job-7"}


def test_bounded_command_output_keeps_only_the_tail(tmp_path: Path) -> None:
    # NEW-100 review: a huge CI log must not be read into memory whole.
    import subprocess
    import sys

    from engineering_control.adapters.command import SafeCommandRunner

    runner = SafeCommandRunner({"python": Path(sys.executable)})
    script = "import sys; sys.stdout.write('a' * 5_000_000 + 'END')"
    result = runner.run("python", ["-c", script], cwd=tmp_path, max_output_bytes=1000)
    assert len(result.stdout) == 1000 and result.stdout.endswith("END")
    failed = runner.run(
        "python",
        ["-c", "import sys; sys.exit(3)"],
        cwd=tmp_path,
        check=False,
        max_output_bytes=1000,
    )
    assert failed.return_code == 3
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run(
            "python",
            ["-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            timeout_seconds=1,
            max_output_bytes=1000,
        )


@pytest.mark.asyncio
async def test_a_stalled_job_log_read_is_skipped(tmp_path: Path) -> None:
    # NEW-100 review: a log request timing out must not abort the CI verdict.
    import subprocess

    job_url = "https://github.com/gntik-ai/musematic/actions/runs/5/job/{}"
    checks = [
        {**_check("slow", "FAILURE"), "detailsUrl": job_url.format(21)},
        {**_check("quality", "FAILURE"), "detailsUrl": job_url.format(22)},
    ]
    handler, fake = _baseline_handler(tmp_path, [".github/workflows/ci.yml"], checks)
    fake.job_logs = {22: "not ok 1 - quality"}
    answer = fake._answer

    def stalled(args: list[str]) -> str:
        if "/actions/jobs/21/" in " ".join(args):
            raise subprocess.TimeoutExpired("gh", 15)
        return answer(args)

    fake._answer = stalled  # type: ignore[method-assign]

    blocked = await handler.handle("gitops.pr_checks", {"plan": _plan()})

    assert blocked["blocker"] == "SOURCE_PR_CI_REGRESSION"
    assert set(blocked["failure_excerpts"]) == {"quality"}


@pytest.mark.asyncio
async def test_pr_heads_reads_each_bound_pull_request(tmp_path: Path) -> None:
    # NEW-102: the restore compares recorded published heads with the live PRs.
    handler, fake = _baseline_handler(tmp_path, [], [])
    url = "https://github.com/gntik-ai/musematic/pull/77"
    result = await handler.handle("gitops.pr_heads", {"pull_requests": [{"url": url}]})
    assert result["status"] == "OK"
    [head] = result["heads"]
    assert head["url"] == url and head["state"] == "OPEN"
    assert head["head_sha"] == fake.pull_requests[("gntik-ai/musematic", 77)]["headRefOid"]
    outside = await handler.handle(
        "gitops.pr_heads", {"pull_requests": [{"url": "https://github.com/evil/x/pull/1"}]}
    )
    assert outside["status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_deployment_pr_checks_gate_the_merge_of_both_prs(tmp_path: Path) -> None:
    # NEW-103: falcone #1001 merged its source while falcone-charts CI was still running
    # (and then failed); the deployment PR's CI must pass before either PR merges.
    deployment_head = "e" * 40
    job_url = "https://github.com/gntik-ai/musematic-deploy/actions/runs/5/job/{}"
    handler, fake = _baseline_handler(tmp_path, [], [])
    plan = _plan(
        deployment_pr_number=9,
        deployment_pr_url="https://github.com/gntik-ai/musematic-deploy/pull/9",
        deployment_head_sha=deployment_head,
    )

    def deployment_pr(**changes: Any) -> None:
        fake.pull_requests[("gntik-ai/musematic-deploy", 9)] = _pull_request(
            **{
                "number": 9,
                "url": "https://github.com/gntik-ai/musematic-deploy/pull/9",
                "headRefOid": deployment_head,
                **changes,
            }
        )

    no_deployment = await handler.handle("gitops.deployment_pr_checks", {"plan": _plan()})
    assert no_deployment["status"] == "CHECKS_PASSED"

    deployment_pr(statusCheckRollup=[_check("validate", None)])
    pending = await handler.handle("gitops.deployment_pr_checks", {"plan": plan})
    assert pending["status"] == "CHECKS_PENDING"

    deployment_pr(
        statusCheckRollup=[
            {**_check("validate", "FAILURE"), "detailsUrl": job_url.format(31)},
            _check("lint", "SUCCESS"),
            _check("slow", None),  # a still-running check never hides the failure
        ]
    )
    fake.job_logs = {31: "not ok 51 - default umbrella stays at its approved baseline"}
    failed = await handler.handle("gitops.deployment_pr_checks", {"plan": plan})
    assert failed["status"] == "BLOCKED"
    assert failed["blocker"] == "DEPLOYMENT_PR_CI_REGRESSION"
    assert failed["failed_checks"] == ["validate"] and failed["preexisting_checks"] == []
    assert failed["reason"] == "DEPLOYMENT_CI_NOT_BASELINED"
    assert "approved baseline" in failed["failure_excerpts"]["validate"]

    # All green is not enough: the configured deployment CI workflow must have succeeded.
    deployment_pr(statusCheckRollup=[_check("lint", "SUCCESS")])
    unrelated = await handler.handle("gitops.deployment_pr_checks", {"plan": plan})
    assert unrelated == {"status": "CHECKS_PENDING", "reason": "REQUIRED_DEPLOYMENT_CI_MISSING"}
    deployment_pr(
        statusCheckRollup=[
            {**_check("guard", "SUCCESS"), "workflowName": "gitops-release-guard"},
            {**_check("publish", "SKIPPED"), "workflowName": "gitops-release-guard"},
        ]
    )
    passed = await handler.handle("gitops.deployment_pr_checks", {"plan": plan})
    assert passed["status"] == "CHECKS_PASSED"

    deployment_pr(headRefOid="f" * 40)
    moved = await handler.handle("gitops.deployment_pr_checks", {"plan": plan})
    assert moved["blocker"] == "DEPLOYMENT_PR_BINDING_CHANGED"

    deployment_pr(state="MERGED")
    merged = await handler.handle("gitops.deployment_pr_checks", {"plan": plan})
    assert merged["status"] == "CHECKS_PASSED" and merged["already_merged"] is True


# -- NEW-113: Argo CD sync failure diagnosis, remediation and escalation --------------

# The operator's report for gntik-ai/falcone#980, verbatim (Kubernetes adds backticks).
MESSAGE_980 = (
    'Deployment.apps "falcone-control-plane-executor" is invalid: '
    'spec.template.spec.containers[0].env[1].valueFrom: Invalid value: "": '
    "may not be specified when value is not empty"
)
SELECTOR_IMMUTABLE = (
    'Deployment.apps "web" is invalid: spec.selector: Invalid value: '
    'v1.LabelSelector{MatchLabels:map[string]string{"app":"web"}}: field is immutable'
)
JOB_TEMPLATE_IMMUTABLE = (
    'Job.batch "db-migrate" is invalid: spec.template: Invalid value: '
    "core.PodTemplateSpec{...}: field is immutable"
)
WEBHOOK_TIMEOUT = (
    "Internal error occurred: failed calling webhook "
    '"validate.nginx.ingress.kubernetes.io": failed to call webhook: Post '
    '"https://ingress-nginx-controller-admission.ingress-nginx.svc:443/networking/v1/'
    'ingresses?timeout=10s": context deadline exceeded'
)


@pytest.mark.parametrize(
    ("kind", "message", "expected"),
    [
        ("Deployment", MESSAGE_980, "PATCH_CONFLICT"),
        ("Deployment", MESSAGE_980.replace("when value", "when `value`"), "PATCH_CONFLICT"),
        (
            "StatefulSet",
            'StatefulSet.apps "db" is invalid: spec.template.spec.containers[0].env[2].'
            'valueFrom: Invalid value: "": may not have more than one field specified at a '
            "time",
            "PATCH_CONFLICT",
        ),
        ("Deployment", SELECTOR_IMMUTABLE, "OTHER"),
        ("StatefulSet", SELECTOR_IMMUTABLE.replace("Deployment", "StatefulSet"), "OTHER"),
        ("Job", JOB_TEMPLATE_IMMUTABLE, "JOB_IMMUTABLE"),
        ("Ingress", WEBHOOK_TIMEOUT, "TRANSIENT"),
        (
            "ConfigMap",
            'admission webhook "validate.kyverno.svc" failed: context deadline exceeded',
            "TRANSIENT",
        ),
        (
            "Deployment",
            'Operation cannot be fulfilled on deployments.apps "web": the object has been '
            "modified; please apply your changes to the latest version and try again",
            "TRANSIENT",
        ),
        ("Service", "etcdserver: request timed out", "TRANSIENT"),
        ("Service", "Get https://10.43.0.1:443/api: dial tcp: i/o timeout", "TRANSIENT"),
        ("Service", "dial tcp 10.43.0.1:443: connect: connection refused", "TRANSIENT"),
        (
            "Service",
            "the server has received too many requests and has asked us to try again "
            "later (TooManyRequests)",
            "TRANSIENT",
        ),
        (
            "Deployment",
            "error validating data: ValidationError(Deployment.spec.template.spec): unknown "
            'field "containerz"',
            "OTHER",
        ),
        (
            "ServiceMonitor",
            "The Kubernetes API could not find monitoring.coreos.com/ServiceMonitor for "
            'requested resource ns/x. Make sure the "ServiceMonitor" CRD is installed.',
            "OTHER",
        ),
        ("ConfigMap", 'namespaces "in-falcone-staging" not found', "OTHER"),
        (
            "Deployment",
            'deployments.apps "web" is forbidden: User "system:serviceaccount:argocd:'
            'argocd-application-controller" cannot patch resource "deployments"',
            "OTHER",
        ),
        ("Deployment", "", "OTHER"),
        # Finding 6: a permanent error in the same message wins over a recoverable match.
        ("Deployment", MESSAGE_980 + ", " + SELECTOR_IMMUTABLE.split(": ", 1)[1], "OTHER"),
        (
            "Deployment",
            MESSAGE_980 + ", spec.replicas: Invalid value: -1: must be greater than or equal to 0",
            "OTHER",
        ),
        ("Deployment", MESSAGE_980 + '; unknown field "containerz"', "OTHER"),
        ("Job", JOB_TEMPLATE_IMMUTABLE + '; namespaces "x" not found', "OTHER"),
        (
            "Deployment",
            'the object has been modified; deployments.apps "web" is forbidden: User "x" '
            'cannot patch resource "deployments"',
            "OTHER",
        ),
        ("ConfigMap", 'ConfigMap "x" is invalid: data: connection refused', "OTHER"),
        (
            "Deployment",
            "may not specify more than 1 handler type; context deadline exceeded",
            "PATCH_CONFLICT",
        ),
        # Finding 7: a webhook failure is TRANSIENT only for a transport failure.
        (
            "Ingress",
            'Internal error occurred: failed calling webhook "validate.nginx.ingress.io": '
            'failed to call webhook: Post "https://admission.svc:443/x": tls: failed to verify '
            "certificate: x509: certificate signed by unknown authority",
            "OTHER",
        ),
        (
            "Ingress",
            'admission webhook "validate.nginx.ingress.kubernetes.io" denied the request: '
            "host already defined",
            "OTHER",
        ),
        ("Ingress", 'Internal error occurred: failed calling webhook "x.io"', "OTHER"),
        (
            "Ingress",
            'Internal error occurred: failed calling webhook "x.io": failed to call webhook: '
            'Post "https://x.svc:443/v": no endpoints available for service "x"',
            "TRANSIENT",
        ),
        (
            "Ingress",
            'Internal error occurred: failed calling webhook "x.io": failed to call webhook: '
            'Post "https://x.svc:443/v": EOF',
            "TRANSIENT",
        ),
        (
            "Ingress",
            'Internal error occurred: failed calling webhook "x.io": failed to call webhook: '
            'Post "https://x.svc:443/v": read tcp 10.0.0.1:443: connection reset by peer',
            "TRANSIENT",
        ),
    ],
)
def test_sync_failures_are_classified_from_argo_messages(
    kind: str, message: str, expected: str
) -> None:
    assert classify_sync_failure(kind, message) == expected


def test_degraded_health_and_overall_class_rules() -> None:
    assert classify_degraded(["ImagePullBackOff", "ErrImagePull"]) == "TRANSIENT"
    assert classify_degraded(["ImagePullBackOff", "CrashLoopBackOff"]) == "OTHER"
    assert classify_degraded([]) == "OTHER"
    assert overall_failure_class(["PATCH_CONFLICT", "OTHER"]) == "OTHER"
    assert overall_failure_class(["TRANSIENT", "JOB_IMMUTABLE"]) == "JOB_IMMUTABLE"
    assert overall_failure_class(["TRANSIENT", "PATCH_CONFLICT"]) == "PATCH_CONFLICT"
    assert overall_failure_class([]) == "OTHER"


def _managed(kind: str, name: str, *, group: str = "apps", health: str = "Healthy") -> dict:
    return {
        "group": group,
        "version": "v1",
        "kind": kind,
        "namespace": "musematic-platform",
        "name": name,
        "status": "OutOfSync",
        "health": {"status": health},
    }


def _sync_result(*entries: tuple[str, str, str, str]) -> dict[str, Any]:
    return {
        "phase": "Failed",
        "startedAt": "2026-10-02T09:00:00Z",
        "message": "one or more objects failed to apply, reason: " + entries[0][3],
        "operation": {"initiatedBy": {"username": "hermes-engineering"}, "sync": {}},
        "syncResult": {
            "resources": [
                {
                    "group": group,
                    "version": "v1",
                    "kind": kind,
                    "namespace": "musematic-platform",
                    "name": name,
                    "status": "SyncFailed",
                    "message": message,
                    "syncPhase": "Sync",
                }
                for group, kind, name, message in entries
            ]
            + [
                {
                    "group": "",
                    "kind": "Service",
                    "namespace": "musematic-platform",
                    "name": "web",
                    "status": "Synced",
                    "message": "service/web unchanged",
                }
            ]
        },
    }


def _failed_application(*entries: tuple[str, str, str, str], **extra: Any) -> dict[str, Any]:
    managed = [_managed(kind, name, group=group) for group, kind, name, _ in entries]
    managed.append(_managed("Service", "web", group=""))
    status: dict[str, Any] = {
        "operationState": _sync_result(*entries),
        "sync": {"status": "OutOfSync"},
        "health": {"status": "Healthy"},
        "resources": managed,
    }
    status.update(extra)
    return _application(**status)


EXECUTOR = ("apps", "Deployment", "falcone-control-plane-executor", MESSAGE_980)


@pytest.mark.asyncio
async def test_diagnose_sync_parses_failed_results_bounded_and_redacted(tmp_path: Path) -> None:
    secret = "ghp_" + "A" * 36
    noisy = ("apps", "Deployment", "web", SELECTOR_IMMUTABLE + f" token {secret} " + "x" * 600)
    kubectl = FakeKubectl(application=_failed_application(EXECUTOR, noisy))
    handler = _controller(tmp_path, kubectl)
    result = await handler.handle("gitops.diagnose_sync", {"plan": _plan()})
    assert result["status"] == "DIAGNOSED"
    diagnosis = result["diagnosis"]
    failed = diagnosis["failed_resources"]
    assert [(item["name"], item["failure_class"]) for item in failed] == [
        ("falcone-control-plane-executor", "PATCH_CONFLICT"),
        ("web", "OTHER"),
    ]
    assert failed[0] | {"message": ""} == {
        "group": "apps",
        "kind": "Deployment",
        "namespace": "musematic-platform",
        "name": "falcone-control-plane-executor",
        "status": "SyncFailed",
        "message": "",
        "failure_class": "PATCH_CONFLICT",
        "managed": True,
        "eligible": True,
        "current": True,
    }
    assert "may not be specified when value is not empty" in failed[0]["message"]
    assert len(failed[1]["message"]) <= 300 and secret not in json.dumps(result)
    assert diagnosis["classes"] == ["OTHER", "PATCH_CONFLICT"]
    # One patch conflict next to an immutable Deployment selector is the operator's.
    assert result["failure_class"] == "OTHER"
    assert diagnosis["degraded_resources"] == []
    assert not [call for call in kubectl.calls if call[0] != "get" and call[2] != "get"]

    only_980 = FakeKubectl(application=_failed_application(EXECUTOR))
    single = await _controller(tmp_path / "b", only_980).handle(
        "gitops.diagnose_sync", {"plan": _plan()}
    )
    assert single["failure_class"] == "PATCH_CONFLICT"


def _owned_pod(name: str, owner: str, reason: str, *, owner_kind: str = "ReplicaSet") -> dict:
    pod = _pod(name, reason)
    pod["metadata"]["ownerReferences"] = [{"kind": owner_kind, "name": owner}]
    return pod


@pytest.mark.asyncio
async def test_diagnose_sync_reports_degraded_workloads_with_pod_reasons(tmp_path: Path) -> None:
    resources = [
        _managed("Deployment", "web", health="Degraded"),
        _managed("Deployment", "api", health="Healthy"),
    ]
    application = _application(
        operationState={"phase": "Succeeded", "startedAt": "2026-10-02T09:00:00Z"},
        sync={"status": "Synced"},
        health={"status": "Degraded"},
        resources=resources,
    )
    pods = {
        "musematic-platform": [
            _owned_pod("web-5d9c7-abcde", "web-5d9c7", "ImagePullBackOff"),
            _owned_pod("api-7f8-xyz", "api-7f8", "CrashLoopBackOff"),
        ]
    }
    kubectl = FakeKubectl(application=application, pods=pods)
    result = await _controller(tmp_path, kubectl).handle("gitops.diagnose_sync", {"plan": _plan()})
    degraded = result["diagnosis"]["degraded_resources"]
    assert [(item["name"], item["health"], item["failure_class"]) for item in degraded] == [
        ("web", "Degraded", "TRANSIENT")
    ]
    assert degraded[0]["pod_reasons"] == [
        {"pod": "web-5d9c7-abcde", "container": "main", "reason": "ImagePullBackOff", "message": ""}
    ]
    assert result["failure_class"] == "TRANSIENT"

    pods["musematic-platform"][0] = _owned_pod("web-5d9c7-abcde", "web-5d9c7", "CrashLoopBackOff")
    again = await _controller(
        tmp_path / "b", FakeKubectl(application=application, pods=pods)
    ).handle("gitops.diagnose_sync", {"plan": _plan()})
    assert again["failure_class"] == "OTHER"


FAILED_AT = "2026-10-02T09:00:00Z"
EXECUTOR_TARGET = {
    "group": "apps",
    "kind": "Deployment",
    "namespace": "musematic-platform",
    "name": "falcone-control-plane-executor",
}
EXECUTOR_KEY = "apps/Deployment/musematic-platform/falcone-control-plane-executor"


def _remediate(**changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "plan": _plan(),
        "merge_sha": MERGE_SHA,
        "action": "replace",
        "round": 1,
        "attempt": 0,
        "already_remediated": [],
        "remediation_id": "wf-1:round-1:replace",
        "failed_operation_started_at": FAILED_AT,
        "resources": [EXECUTOR_TARGET],
    }
    payload.update(changes)
    return payload


def _operation_patches(kubectl: FakeKubectl) -> list[list[dict[str, Any]]]:
    return [
        patch
        for patch in (
            json.loads(call[call.index("--patch") + 1])
            for call in kubectl.calls
            if "--type" in call and call[call.index("--type") + 1] == "json"
        )
        if any(operation["path"] == "/operation" for operation in patch)
    ]


def _written_operation(kubectl: FakeKubectl) -> dict[str, Any]:
    patch = _operation_patches(kubectl)[-1]
    assert patch[0]["op"] == "test" and patch[0]["path"] == "/metadata/resourceVersion"
    assert patch[1]["op"] == "add" and patch[1]["path"] == "/operation"
    value: dict[str, Any] = patch[1]["value"]
    return value


@pytest.mark.asyncio
async def test_replace_remediation_writes_one_complete_operation(tmp_path: Path) -> None:
    kubectl = FakeKubectl(application=_failed_application(EXECUTOR))
    handler = _controller(tmp_path, kubectl)
    result = await handler.handle("gitops.remediate_sync", _remediate())
    assert result == {
        "status": "REMEDIATION_TRIGGERED",
        "action": "replace",
        "remediation_id": "wf-1:round-1:replace",
        "replayed": False,
        "resources": [EXECUTOR_TARGET],
        "remediated_keys": [EXECUTOR_KEY],
        "previous_operation_started_at": FAILED_AT,
    }
    assert _written_operation(kubectl) == {
        "initiatedBy": {"username": "hermes-engineering"},
        "info": [
            {"name": "hermes-remediation", "value": "wf-1:round-1:replace"},
            {"name": "hermes-previous-started-at", "value": FAILED_AT},
        ],
        "sync": {"resources": [EXECUTOR_TARGET], "syncOptions": ["Replace=true"], "prune": False},
    }
    assert kubectl.application["operation"] == _written_operation(kubectl)
    verbs = {call[2] for call in kubectl.calls if call[0] == "--namespace"}
    assert verbs <= {"get", "annotate", "patch"}


@pytest.mark.asyncio
async def test_finding_1_busy_check_and_write_are_atomic_and_complete(tmp_path: Path) -> None:
    # An operator-set operation (here with a forced apply) is never merged into: busy.
    application = _failed_application(EXECUTOR)
    application["operation"] = {"sync": {"syncStrategy": {"apply": {"force": True}}}}
    kubectl = FakeKubectl(application=application)
    handler = _controller(tmp_path, kubectl)
    assert await handler.handle("gitops.remediate_sync", _remediate()) == {
        "status": "SYNC_BUSY",
        "action": "replace",
    }
    assert _operation_patches(kubectl) == []
    assert kubectl.application["operation"] == {
        "sync": {"syncStrategy": {"apply": {"force": True}}}
    }

    # Someone writes an operation between the read and the patch: the resourceVersion
    # test fails, the re-read sees the operation, and Hermes reports busy.
    racing = FakeKubectl(application=_failed_application(EXECUTOR))

    def operator_sync(fake: FakeKubectl) -> None:
        fake.before_patch = None
        fake.application["operation"] = {"sync": {"syncStrategy": {"apply": {"force": True}}}}
        fake._bump()

    racing.before_patch = operator_sync
    raced = await _controller(tmp_path / "b", racing).handle("gitops.remediate_sync", _remediate())
    assert raced["status"] == "SYNC_BUSY"
    assert racing.application["operation"] == {"sync": {"syncStrategy": {"apply": {"force": True}}}}

    # A plain status update moves the version: the write is retried on a fresh read.
    moving = FakeKubectl(application=_failed_application(EXECUTOR))

    def status_update(fake: FakeKubectl) -> None:
        fake.before_patch = None
        fake._bump()

    moving.before_patch = status_update
    moved = await _controller(tmp_path / "c", moving).handle("gitops.remediate_sync", _remediate())
    assert moved["status"] == "REMEDIATION_TRIGGERED"
    first, second = _operation_patches(moving)
    assert int(second[0]["value"]) == int(first[0]["value"]) + 1

    # A rejected write with nothing moving (e.g. RBAC) fails closed, never "busy".
    rejected = FakeKubectl(application=_failed_application(EXECUTOR))
    rejected.reject_patches = True
    blocked = await _controller(tmp_path / "d", rejected).handle(
        "gitops.remediate_sync", _remediate()
    )
    assert blocked["status"] == "BLOCKED"
    assert blocked["blocker"] == "STAGING_GITOPS_REMEDIATE_SYNC_FAILED"


@pytest.mark.asyncio
async def test_finding_2_a_retried_remediation_does_not_sync_again(tmp_path: Path) -> None:
    kubectl = FakeKubectl(application=_failed_application(EXECUTOR))
    handler = _controller(tmp_path, kubectl)
    first = await handler.handle("gitops.remediate_sync", _remediate())
    assert first["status"] == "REMEDIATION_TRIGGERED" and first["replayed"] is False
    # The activity completion is lost; Argo has meanwhile started the operation.
    operation = kubectl.application.pop("operation")
    kubectl.application["status"]["operationState"] = {
        "phase": "Running",
        "startedAt": "2026-10-02T09:01:00Z",
        "operation": operation,
    }
    kubectl.calls.clear()
    retried = await handler.handle("gitops.remediate_sync", _remediate())
    assert retried == {**first, "replayed": True}
    assert retried["previous_operation_started_at"] == FAILED_AT
    assert _operation_patches(kubectl) == []
    assert not [call for call in kubectl.calls if "annotate" in call]

    # Still pending (not yet picked up by Argo): also recognised.
    pending = FakeKubectl(application=_failed_application(EXECUTOR))
    pending_handler = _controller(tmp_path / "b", pending)
    await pending_handler.handle("gitops.remediate_sync", _remediate())
    pending.calls.clear()
    again = await pending_handler.handle("gitops.remediate_sync", _remediate())
    assert again["replayed"] is True and _operation_patches(pending) == []

    # Another round is another identity.
    next_round = await pending_handler.handle(
        "gitops.remediate_sync", _remediate(remediation_id="wf-1:round-2:replace", round=2)
    )
    assert next_round["status"] == "SYNC_BUSY"


@pytest.mark.asyncio
async def test_finding_3_immutable_job_force_uses_the_apply_sync_strategy(tmp_path: Path) -> None:
    job = ("batch", "Job", "db-migrate", JOB_TEMPLATE_IMMUTABLE)
    kubectl = FakeKubectl(application=_failed_application(job))
    handler = _controller(tmp_path, kubectl)
    target = {
        "group": "batch",
        "kind": "Job",
        "namespace": "musematic-platform",
        "name": "db-migrate",
    }
    result = await handler.handle(
        "gitops.remediate_sync",
        _remediate(action="replace_force", resources=[target], remediation_id="wf:r1:force"),
    )
    assert result["status"] == "REMEDIATION_TRIGGERED"
    assert _written_operation(kubectl)["sync"] == {
        "resources": [target],
        "syncOptions": ["Replace=true"],
        "syncStrategy": {"apply": {"force": True}},
        "prune": False,
    }
    # Never Force for anything but a batch/Job, and never via syncOptions.
    deployment = await _controller(
        tmp_path / "b", FakeKubectl(application=_failed_application(EXECUTOR))
    ).handle("gitops.remediate_sync", _remediate(action="replace_force"))
    assert deployment["blocker"] == "ARGO_REMEDIATION_FORCE_REQUIRES_JOB"


def _drift(application: dict[str, Any], **live: Any) -> dict[str, Any]:
    for item in application["status"]["resources"]:
        if item["name"] == "falcone-control-plane-executor":
            item.update(live)
    return application


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("application", "changes", "blocker"),
    [
        # a newer operation replaced the failure the flow diagnosed
        (
            _failed_application(EXECUTOR),
            {"failed_operation_started_at": "2026-10-02T08:00:00Z"},
            "ARGO_REMEDIATION_STALE_DIAGNOSIS",
        ),
        (
            (_failed_application(EXECUTOR)),
            {"failed_operation_started_at": None},
            "ARGO_REMEDIATION_STALE_DIAGNOSIS",
        ),
        # the application recovered on its own
        (
            _failed_application(EXECUTOR, sync={"status": "Synced"}, health={"status": "Healthy"}),
            {},
            "ARGO_REMEDIATION_NOT_NEEDED",
        ),
        # an OTHER failure appeared next to the conflict
        (
            _failed_application(EXECUTOR, ("apps", "Deployment", "web", SELECTOR_IMMUTABLE)),
            {},
            "ARGO_REMEDIATION_CLASS_MISMATCH",
        ),
        # the selected resource is no longer drifting
        (
            _drift(_failed_application(EXECUTOR), status="Synced"),
            {},
            "ARGO_REMEDIATION_STALE_DIAGNOSIS",
        ),
        # the latest failed operation is not Hermes's
        (
            _failed_application(EXECUTOR)
            | {
                "status": _failed_application(EXECUTOR)["status"]
                | {
                    "operationState": _sync_result(EXECUTOR)
                    | {"operation": {"initiatedBy": {"username": "admin"}}}
                }
            },
            {},
            "ARGO_REMEDIATION_STALE_DIAGNOSIS",
        ),
    ],
)
async def test_finding_4_remediation_revalidates_the_current_application(
    tmp_path: Path, application: dict[str, Any], changes: dict[str, Any], blocker: str
) -> None:
    kubectl = FakeKubectl(application=json.loads(json.dumps(application)))
    result = await _controller(tmp_path, kubectl).handle(
        "gitops.remediate_sync", _remediate(**changes)
    )
    assert result == {"status": "REMEDIATION_REFUSED", "blocker": blocker, "action": "replace"}
    assert _operation_patches(kubectl) == [] and "operation" not in kubectl.application


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entries", "changes", "blocker"),
    [
        (
            (EXECUTOR,),
            {
                "resources": [
                    {"group": "apps", "kind": "Deployment", "namespace": "argocd", "name": "x"}
                ]
            },
            "ARGO_REMEDIATION_OUTSIDE_NAMESPACES",
        ),
        (
            (EXECUTOR,),
            {"resources": [{**EXECUTOR_TARGET, "namespace": "platform-data"}]},
            "ARGO_REMEDIATION_RESOURCE_NOT_MANAGED",
        ),
        (
            (("apps", "Deployment", "falcone-control-plane-executor", SELECTOR_IMMUTABLE),),
            {},
            "ARGO_REMEDIATION_CLASS_MISMATCH",
        ),
        ((EXECUTOR,), {"already_remediated": [EXECUTOR_KEY]}, "ARGO_REMEDIATION_ALREADY_APPLIED"),
        ((EXECUTOR,), {"resources": []}, "ARGO_REMEDIATION_NO_RESOURCES"),
        (
            (EXECUTOR,),
            {"resources": [EXECUTOR_TARGET] * 11},
            "ARGO_REMEDIATION_TOO_MANY_RESOURCES",
        ),
        ((EXECUTOR,), {"action": "resync", "attempt": 1}, "ARGO_REMEDIATION_CLASS_MISMATCH"),
        (
            (("apps", "Deployment", "web", WEBHOOK_TIMEOUT),),
            {"action": "resync", "attempt": 4},
            "ARGO_REMEDIATION_TRANSIENT_RETRIES_EXHAUSTED",
        ),
        ((EXECUTOR,), {"action": "complete"}, "ARGO_REMEDIATION_NOTHING_TO_COMPLETE"),
    ],
)
async def test_sync_remediation_refuses_unsafe_requests(
    tmp_path: Path,
    entries: tuple[tuple[str, str, str, str], ...],
    changes: dict[str, Any],
    blocker: str,
) -> None:
    kubectl = FakeKubectl(application=_failed_application(*entries))
    result = await _controller(tmp_path, kubectl).handle(
        "gitops.remediate_sync", _remediate(**changes)
    )
    assert result["status"] == "REMEDIATION_REFUSED" and result["blocker"] == blocker
    assert _operation_patches(kubectl) == [] and "operation" not in kubectl.application


@pytest.mark.asyncio
async def test_sync_remediation_bounds_rounds_and_refuses_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kubectl = FakeKubectl(application=_failed_application(EXECUTOR))
    handler = _controller(tmp_path, kubectl)
    exhausted = await handler.handle("gitops.remediate_sync", _remediate(round=4))
    assert exhausted == {"status": "BLOCKED", "blocker": "ARGO_REMEDIATION_ROUNDS_EXHAUSTED"}
    invalid = await handler.handle("gitops.remediate_sync", _remediate(action="prune"))
    assert invalid == {"status": "BLOCKED", "blocker": "ARGO_REMEDIATION_ACTION_INVALID"}
    no_id = await handler.handle("gitops.remediate_sync", _remediate(remediation_id="a b"))
    assert no_id == {"status": "BLOCKED", "blocker": "ARGO_REMEDIATION_ID_INVALID"}

    from engineering_control.activities import gitops

    real = gitops.enabled_staging_flow

    def production(root: Path, project: str) -> tuple[ProjectManifest, StagingFlowPolicy]:
        manifest, flow = real(root, project)
        policy = manifest.deployment.staging_policy.model_copy(update={"production": True})
        deployment = manifest.deployment.model_copy(update={"staging_policy": policy})
        return manifest.model_copy(update={"deployment": deployment}), flow

    monkeypatch.setattr(gitops, "enabled_staging_flow", production)
    refused = await handler.handle("gitops.remediate_sync", _remediate())
    assert refused == {"status": "BLOCKED", "blocker": "ARGO_REMEDIATION_PRODUCTION_REFUSED"}
    assert _operation_patches(kubectl) == []


@pytest.mark.asyncio
async def test_sync_remediation_waits_for_a_running_operation(tmp_path: Path) -> None:
    application = _failed_application(EXECUTOR)
    application["status"]["operationState"]["phase"] = "Running"
    kubectl = FakeKubectl(application=application)
    result = await _controller(tmp_path, kubectl).handle("gitops.remediate_sync", _remediate())
    assert result == {"status": "SYNC_BUSY", "action": "replace"}
    assert _operation_patches(kubectl) == []


@pytest.mark.asyncio
async def test_transient_resync_and_completion_use_a_normal_sync(tmp_path: Path) -> None:
    transient = ("apps", "Deployment", "web", WEBHOOK_TIMEOUT)
    kubectl = FakeKubectl(application=_failed_application(transient))
    handler = _controller(tmp_path, kubectl)
    resynced = await handler.handle(
        "gitops.remediate_sync",
        _remediate(action="resync", attempt=3, resources=[], remediation_id="wf:r1:resync"),
    )
    assert resynced["status"] == "REMEDIATION_TRIGGERED" and resynced["resources"] == []
    assert _written_operation(kubectl)["sync"] == {"prune": False, "syncStrategy": {"hook": {}}}

    # Degraded health (no failed operation): a transient re-sync needs Synced + unhealthy.
    degraded = _application(
        operationState={"phase": "Succeeded", "startedAt": "2026-10-02T09:00:00Z"},
        sync={"status": "Synced"},
        health={"status": "Degraded"},
        resources=[_managed("Deployment", "web", health="Degraded")],
    )
    pods = {"musematic-platform": [_owned_pod("web-5d9c7-a", "web-5d9c7", "ImagePullBackOff")]}
    health_kubectl = FakeKubectl(application=degraded, pods=pods)
    health = await _controller(tmp_path / "h", health_kubectl).handle(
        "gitops.remediate_sync",
        _remediate(
            action="resync",
            attempt=1,
            resources=[],
            failed_operation_started_at=None,
            remediation_id="wf:r1:resync",
        ),
    )
    assert health["status"] == "REMEDIATION_TRIGGERED"

    # A succeeded selective replace with the rest still OutOfSync is completed once.
    selective = _failed_application(EXECUTOR)
    selective["status"]["operationState"] = {
        "phase": "Succeeded",
        "startedAt": "2026-10-02T09:05:00Z",
        "operation": {
            "initiatedBy": {"username": "hermes-engineering"},
            "sync": {"resources": [EXECUTOR_TARGET], "syncOptions": ["Replace=true"]},
        },
    }
    completing = FakeKubectl(application=json.loads(json.dumps(selective)))
    completed = await _controller(tmp_path / "b", completing).handle(
        "gitops.remediate_sync",
        _remediate(action="complete", resources=None, remediation_id="wf:r1:complete"),
    )
    assert completed["status"] == "REMEDIATION_TRIGGERED"
    assert completed["previous_operation_started_at"] == "2026-10-02T09:05:00Z"
    # NEW-116: the follow-up is a TRUE full sync, never the replace's inherited scope.
    assert _written_operation(completing)["sync"] == {
        "prune": False,
        "syncStrategy": {"hook": {}},
    }
    assert _operation_patches(completing)[-1][-1] == {
        "op": "remove",
        "path": "/status/operationState",
    }
    assert "operationState" not in completing.application["status"]

    payload = {
        "plan": _plan(),
        "previous_operation_started_at": "2026-10-02T09:00:00Z",
        "selective": True,
    }
    partial = await _controller(tmp_path / "c", FakeKubectl(application=selective)).handle(
        "gitops.sync_status", payload
    )
    assert partial["status"] == "SYNC_PARTIAL"
    # NEW-116: without the flag a full sync was requested, so a completed selective
    # operation is an inherited scope, never a pending or synced full sync.
    inherited = await _controller(tmp_path / "d", FakeKubectl(application=selective)).handle(
        "gitops.sync_status", {**payload, "selective": None}
    )
    assert inherited["status"] == "SYNC_SCOPE_INHERITED"
    assert inherited["scope_violations"][:2] == ["resources", "sync_options"]


@pytest.mark.asyncio
async def test_finding_5_classification_sees_every_failure_and_pod_reason(
    tmp_path: Path,
) -> None:
    conflicts = [("apps", "Deployment", f"svc-{index}", MESSAGE_980) for index in range(30)] + [
        ("apps", "Deployment", "web", SELECTOR_IMMUTABLE)
    ]
    kubectl = FakeKubectl(application=_failed_application(*conflicts))
    result = await _controller(tmp_path, kubectl).handle("gitops.diagnose_sync", {"plan": _plan()})
    diagnosis = result["diagnosis"]
    assert len(diagnosis["failed_resources"]) == 25
    assert diagnosis["failed_resources_total"] == 31
    assert all(item["failure_class"] == "PATCH_CONFLICT" for item in diagnosis["failed_resources"])
    assert result["failure_class"] == "OTHER"  # the 31st failure is past the export bound

    pods = [_owned_pod(f"web-5d9c7-{index}", "web-5d9c7", "ImagePullBackOff") for index in range(6)]
    pods.append(_owned_pod("web-5d9c7-z", "web-5d9c7", "CrashLoopBackOff"))
    degraded = _application(
        operationState={"phase": "Succeeded", "startedAt": "2026-10-02T09:00:00Z"},
        sync={"status": "Synced"},
        health={"status": "Degraded"},
        resources=[_managed("Deployment", "web", health="Degraded")],
    )
    health = await _controller(
        tmp_path / "b", FakeKubectl(application=degraded, pods={"musematic-platform": pods})
    ).handle("gitops.diagnose_sync", {"plan": _plan()})
    (entry,) = health["diagnosis"]["degraded_resources"]
    assert len(entry["pod_reasons"]) == 5 and entry["pod_reasons_total"] == 7
    assert entry["failure_class"] == "OTHER" and health["failure_class"] == "OTHER"

    # A pod listing beyond the observation bound fails closed (blocked -> escalated).
    many = {
        "musematic-platform": [
            _owned_pod(f"p-{i}", "web-5d9c7", "ImagePullBackOff") for i in range(2001)
        ]
    }
    capped = await _controller(tmp_path / "c", FakeKubectl(application=degraded, pods=many)).handle(
        "gitops.diagnose_sync", {"plan": _plan()}
    )
    assert capped["status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_finding_8_multiline_credentials_are_redacted_before_flattening(
    tmp_path: Path,
) -> None:
    leaked = "rejected the object\npassword: hunter2-s3cret-value"
    entry = ("apps", "Deployment", "falcone-control-plane-executor", leaked)
    application = _failed_application(entry)
    application["status"]["operationState"]["message"] = leaked
    handler = _controller(tmp_path, FakeKubectl(application=application))
    result = await handler.handle("gitops.diagnose_sync", {"plan": _plan()})
    assert "hunter2" not in json.dumps(result)
    assert "rejected the object password: [REDACTED]" in json.dumps(result)

    status = await handler.handle(
        "gitops.sync_status", {"plan": _plan(), "previous_operation_started_at": ""}
    )
    assert status["status"] == "SYNC_FAILED" and "hunter2" not in status["message"]

    await handler.handle(
        "gitops.notify_sync",
        {
            "plan": _plan(),
            "event": "detected",
            "failure_class": "OTHER",
            "diagnosis": {"message": leaked, "failed_resources": []},
        },
    )
    with handler.sessions() as session:
        (row,) = session.query(db.NotificationOutbox).all()
    assert "hunter2" not in json.dumps(row.payload_json)

    body = render_sync_failure(
        StagingGitOpsPlan.model_validate(_plan()),
        StagingFlowPolicy.model_validate(_musematic_manifest()["delivery_control"]["staging_flow"]),
        merge_sha=MERGE_SHA,
        failure_class="OTHER",
        blocker="ARGO_SYNC_FAILED",
        diagnosis={
            "message": leaked,
            "failed_resources": [{"kind": "Deployment", "name": "x", "message": leaked}],
        },
        attempts=[],
    )
    assert "hunter2" not in body and "[REDACTED]" in body


@pytest.mark.asyncio
async def test_sync_notifications_are_queued_bounded_and_redacted(tmp_path: Path) -> None:
    handler = _controller(tmp_path, FakeKubectl())
    secret = "ghp_" + "B" * 36
    diagnosis = {
        "message": f"failed with {secret} " + "y" * 900,
        "failed_resources": [{**_remediate()["resources"][0], "failure_class": "PATCH_CONFLICT"}]
        * 30,
    }
    for event, extra in (
        ("detected", {"diagnosis": diagnosis, "blocker": "ARGO_SYNC_FAILED"}),
        ("remediation", {"action": "replace", "round": 1, "result": "SYNCED"}),
        (
            "escalated",
            {"issue_url": "https://github.com/gntik-ai/musematic/issues/501", "diagnosis": {}},
        ),
    ):
        result = await handler.handle(
            "gitops.notify_sync",
            {"plan": _plan(), "event": event, "failure_class": "PATCH_CONFLICT", **extra},
        )
        assert result["status"] == "NOTIFIED"
    with handler.sessions() as session:
        rows = session.query(db.NotificationOutbox).order_by(db.NotificationOutbox.created_at).all()
    assert [row.topic for row in rows] == ["telegram.engineering-gitops-sync"] * 3
    detected, remediation, escalated = (row.payload_json for row in rows)
    assert len(detected["resources"]) == 10 and len(detected["argo_message"]) <= 300
    assert secret not in json.dumps(detected) and len(json.dumps(detected)) < 4000
    assert "musematic-platform" in detected["message"] and "failed" in detected["message"]
    assert remediation["result"] == "SYNCED" and remediation["action"] == "replace"
    assert escalated["issue_url"].endswith("/issues/501")
    assert escalated["issue_url"] in escalated["message"]
    invalid = await handler.handle("gitops.notify_sync", {"plan": _plan(), "event": "spam"})
    assert invalid["status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_sync_failure_issue_is_opened_then_commented(tmp_path: Path) -> None:
    handler, fake = _github(tmp_path)
    payload = {
        "plan": _plan(),
        "merge_sha": MERGE_SHA,
        "action": "report_sync",
        "failure_class": "OTHER",
        "blocker": "ARGO_SYNC_FAILED",
        "diagnosis": {
            "phase": "Failed",
            "sync": "OutOfSync",
            "health": "Healthy",
            "message": "one or more objects failed to apply",
            "failed_resources": [
                {
                    "kind": "Deployment",
                    "namespace": "musematic-platform",
                    "name": "web",
                    "status": "SyncFailed",
                    "failure_class": "OTHER",
                    "message": SELECTOR_IMMUTABLE,
                }
            ],
        },
        "attempts": [
            {
                "round": 1,
                "action": "replace",
                "failure_class": "PATCH_CONFLICT",
                "resources": ["apps/Deployment/musematic-platform/x"],
                "result": "SYNC_FAILED",
            }
        ],
    }
    opened = await handler.handle("gitops.environment_issue", payload)
    assert opened["status"] == "ISSUE_OPENED" and opened["issue_number"] == 500
    issue = fake.issues[("gntik-ai/musematic", 500)]
    assert issue["title"] == f"Hermes staging Argo CD sync failed for change {UUID(int=7)}"
    for text in ("field is immutable", "`OTHER`", "ARGO_SYNC_FAILED", "round 1", "SYNC_FAILED"):
        assert text in issue["body"]
    assert len(issue["body"].encode()) < 12_000

    again = await handler.handle(
        "gitops.environment_issue", {**payload, "failure_class": "TRANSIENT"}
    )
    assert again["status"] == "ISSUE_COMMENTED" and again["issue_number"] == 500
    assert again["issue_url"] == "https://github.com/gntik-ai/musematic/issues/500"
    invalid = await handler.handle("gitops.environment_issue", {**payload, "failure_class": "NOPE"})
    assert invalid["blocker"] == "ENVIRONMENT_ISSUE_CLASS_INVALID"


class _LostResponseKubectl(FakeKubectl):
    """Applies the JSON patch but reports failure: the response was lost in transit."""

    def _answer(self, namespace: str | None, args: list[str]) -> CommandResult:
        result = super()._answer(namespace, args)
        if args[0] == "patch" and not result.return_code:
            return CommandResult(1, "", "connection reset by peer")
        return result


@pytest.mark.asyncio
async def test_a_landed_write_with_a_lost_response_is_never_written_twice(
    tmp_path: Path,
) -> None:
    # NEW-113 review fallback: after a failed patch the re-read finds this remediation's
    # identity on the Application, so the attempt reports STARTED without a second write.
    kubectl = _LostResponseKubectl(application=_failed_application(EXECUTOR))
    result = await _controller(tmp_path, kubectl).handle("gitops.remediate_sync", _remediate())
    assert result["status"] == "REMEDIATION_TRIGGERED"
    assert len(_operation_patches(kubectl)) == 1


class _LostResponseThenOperatorSyncKubectl(FakeKubectl):
    """The patch lands, its response is lost, and an operator sync then replaces the
    operation (so the remediation identity is gone) and moves the resourceVersion."""

    def _answer(self, namespace: str | None, args: list[str]) -> CommandResult:
        result = super()._answer(namespace, args)
        if args[0] == "patch" and not result.return_code:
            self.application.pop("operation", None)
            self.application["status"]["operationState"] = {
                "phase": "Running",
                "startedAt": "2026-10-02T09:05:00Z",
                "operation": {"initiatedBy": {"username": "admin"}, "sync": {}},
            }
            self._bump()
            return CommandResult(1, "", "connection reset by peer")
        return result


@pytest.mark.asyncio
async def test_an_ambiguous_remediation_write_is_not_repeated(tmp_path: Path) -> None:
    # NEW-113 review: only a confirmed failed resourceVersion test justifies another
    # write; a lost response whose evidence was overwritten is an unknown outcome.
    kubectl = _LostResponseThenOperatorSyncKubectl(application=_failed_application(EXECUTOR))
    result = await _controller(tmp_path, kubectl).handle("gitops.remediate_sync", _remediate())
    assert result["status"] == "REMEDIATION_OUTCOME_UNKNOWN"
    assert result["blocker"] == "ARGO_REMEDIATION_OUTCOME_UNKNOWN"
    assert len(_operation_patches(kubectl)) == 1


@pytest.mark.parametrize(
    "output",
    [
        "testing value /metadata/resourceVersion failed: test failed",
        "The request is invalid: the server rejected our request due to an error in our request",
        "Error from server (Invalid): the server rejected our request due to an error"
        " in our request",
    ],
)
def test_a_rejected_conditional_write_is_recognised(output: str) -> None:
    from engineering_control.activities.gitops import _CONDITIONAL_WRITE_CONFLICT

    assert _CONDITIONAL_WRITE_CONFLICT.search(output)


@pytest.mark.parametrize(
    "output", ["connection reset by peer", "i/o timeout", "error: unexpected EOF", ""]
)
def test_an_ambiguous_write_failure_is_not_a_conflict(output: str) -> None:
    from engineering_control.activities.gitops import _CONDITIONAL_WRITE_CONFLICT

    assert not _CONDITIONAL_WRITE_CONFLICT.search(output)


# -- NEW-116: a full sync must never inherit a previous selective scope -----------------

OPERATOR_SELECTIVE = {
    "phase": "Succeeded",
    "startedAt": "2026-10-02T08:00:00Z",
    "finishedAt": "2026-10-02T08:01:00Z",
    "message": "successfully synced (all tasks run)",
    "operation": {
        "initiatedBy": {"username": "operator"},
        "sync": {
            "revision": "e5a9e49",
            "resources": [EXECUTOR_TARGET],
            "syncOptions": ["Replace=true"],
        },
    },
    "syncResult": {"revisions": [OLD_CHART_REVISION, "e" * 40]},
}


def _merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 JSON merge patch, as the API server applies it."""
    if not isinstance(patch, dict):
        return patch
    merged = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = _merge_patch(merged.get(key), value)
    return merged


def _omitempty(value: Any) -> Any:
    """Go encoding/json ``omitempty``: drops "", false, nil and empty lists (not {})."""
    if isinstance(value, dict):
        return {
            key: _omitempty(item)
            for key, item in value.items()
            if item not in (None, "", False, [])
        }
    if isinstance(value, list):
        return [_omitempty(item) for item in value]
    return value


def _argo_v342_initialises(application: dict[str, Any], started_at: str) -> None:
    """Argo CD v3.4.2 processRequestedAppOperation -> NewOperationState -> setOperationState:
    a JSON MERGE patch of status.operationState with the omitempty-encoded new state."""
    state = {
        "operation": _omitempty(application["operation"]),
        "phase": "Running",
        "startedAt": started_at,
    }
    status = application["status"]
    status["operationState"] = _merge_patch(status.get("operationState"), state)


@pytest.mark.asyncio
async def test_new_116_a_plain_trigger_after_a_selective_sync_writes_a_true_full_sync(
    tmp_path: Path,
) -> None:
    # The live falcone-staging case: an operator's selective Replace=true sync of the
    # executor at e5a9e49 is the last operation; Hermes then triggers its normal sync.
    application = _application(
        operationState=json.loads(json.dumps(OPERATOR_SELECTIVE)),
        sync={"status": "OutOfSync", "revisions": [OLD_CHART_REVISION, "e" * 40]},
    )
    kubectl = FakeKubectl(application=application)
    handler = _controller(tmp_path, kubectl)
    result = await handler.handle("gitops.trigger_sync", {"plan": _plan(), "merge_sha": MERGE_SHA})
    assert result["status"] == "SYNC_TRIGGERED"
    assert result["previous_operation_started_at"] == "2026-10-02T08:00:00Z"
    (patch,) = _operation_patches(kubectl)
    assert patch[1:] == [
        {
            "op": "add",
            "path": "/operation",
            "value": {
                "initiatedBy": {"username": "hermes-engineering"},
                "sync": {"prune": False, "syncStrategy": {"hook": {}}},
            },
        },
        {"op": "remove", "path": "/status/operationState"},
    ]
    assert patch[0]["op"] == "test" and patch[0]["path"] == "/metadata/resourceVersion"

    # The controller's merge now starts from nothing: no resources, syncOptions or
    # revision of the operator's selective sync survive into the new operation.
    _argo_v342_initialises(kubectl.application, "2026-10-03T10:00:00Z")
    sync = kubectl.application["status"]["operationState"]["operation"]["sync"]
    assert sync == {"syncStrategy": {"hook": {}}}

    # Control: the pre-NEW-116 write (operation only) inherits the selective scope.
    inherited = _application(operationState=json.loads(json.dumps(OPERATOR_SELECTIVE)))
    inherited["operation"] = {
        "initiatedBy": {"username": "hermes-engineering"},
        "sync": {"prune": False},
    }
    _argo_v342_initialises(inherited, "2026-10-03T10:00:00Z")
    assert inherited["status"]["operationState"]["operation"]["sync"] == {
        "revision": "e5a9e49",
        "resources": [EXECUTOR_TARGET],
        "syncOptions": ["Replace=true"],
    }

    # The completed TRUE full sync of the pinned revision is SYNCED.
    kubectl.application.pop("operation")
    state = kubectl.application["status"]["operationState"]
    state.update(phase="Succeeded", syncResult={"revisions": [MERGE_SHA, "e" * 40]})
    kubectl.application["status"]["sync"] = {
        "status": "Synced",
        "revisions": [MERGE_SHA, "e" * 40],
    }
    synced = await handler.handle(
        "gitops.sync_status",
        {**_plan_payload(), "previous_operation_started_at": "2026-10-02T08:00:00Z"},
    )
    assert synced["status"] == "SYNCED"


def _plan_payload() -> dict[str, Any]:
    return {"plan": _plan(), "merge_sha": MERGE_SHA}


@pytest.mark.asyncio
async def test_new_116_an_inherited_selective_operation_is_never_synced(tmp_path: Path) -> None:
    # What falcone-staging showed after every Hermes trigger since 2026-10-02: Hermes's
    # operation, merged into the operator's selective one. Even Synced+Healthy is refused.
    merged = json.loads(json.dumps(OPERATOR_SELECTIVE))
    merged["operation"]["initiatedBy"] = {"username": "hermes-engineering"}
    merged["startedAt"] = "2026-10-03T10:00:00Z"
    kubectl = FakeKubectl(application=_application(operationState=merged))
    handler = _controller(tmp_path, kubectl)
    payload = {**_plan_payload(), "previous_operation_started_at": "2026-10-02T08:00:00Z"}
    result = await handler.handle("gitops.sync_status", payload)
    assert result["status"] == "SYNC_SCOPE_INHERITED"
    assert result["blocker"] == "ARGO_SYNC_SCOPE_INHERITED"
    assert result["scope_violations"] == ["resources", "sync_options", "explicit_revision"]
    assert result["operation_started_at"] == "2026-10-03T10:00:00Z"
    assert (result["sync"], result["health"]) == ("Synced", "Healthy")

    # Inherited even when the selective sync itself failed: reported apart from SYNC_FAILED.
    kubectl.application["status"]["operationState"]["phase"] = "Failed"
    failed = await handler.handle("gitops.sync_status", payload)
    assert failed["status"] == "SYNC_SCOPE_INHERITED"

    # A full sync that did not sync the pinned chart revision is not SYNCED either.
    kubectl.application["spec"]["sources"][0]["targetRevision"] = MERGE_SHA
    kubectl.application["status"]["operationState"] = {
        "phase": "Succeeded",
        "startedAt": "2026-10-03T10:00:00Z",
        "operation": {
            "initiatedBy": {"username": "hermes-engineering"},
            "sync": {"syncStrategy": {"hook": {}}},
        },
        "syncResult": {"revisions": [OLD_CHART_REVISION, "e" * 40]},
    }
    stale = await handler.handle("gitops.sync_status", payload)
    assert stale["status"] == "SYNC_SCOPE_INHERITED"
    assert stale["scope_violations"] == ["revision"]
    # An apply strategy (skips hooks) is not a full sync.
    state = kubectl.application["status"]["operationState"]
    state["syncResult"] = {"revisions": [MERGE_SHA, "e" * 40]}
    state["operation"]["sync"] = {"syncStrategy": {"apply": {"force": True}}}
    applied = await handler.handle("gitops.sync_status", payload)
    assert applied["scope_violations"] == ["apply_strategy"]
    # A selective replace remediation keeps its selective semantics.
    state["operation"]["sync"] = {"resources": [EXECUTOR_TARGET]}
    kubectl.application["status"]["sync"]["status"] = "OutOfSync"
    partial = await handler.handle("gitops.sync_status", {**payload, "selective": True})
    assert partial["status"] == "SYNC_PARTIAL"


@pytest.mark.asyncio
async def test_new_116_full_resync_remediation_is_one_true_full_sync(tmp_path: Path) -> None:
    merged = json.loads(json.dumps(OPERATOR_SELECTIVE))
    merged["operation"]["initiatedBy"] = {"username": "hermes-engineering"}
    merged["startedAt"] = FAILED_AT
    application = _application(operationState=merged)
    application["status"]["sync"]["status"] = "OutOfSync"
    original = json.dumps(application)
    kubectl = FakeKubectl(application=json.loads(original))
    handler = _controller(tmp_path, kubectl)
    payload = _remediate(
        action="full_resync", attempt=1, resources=[], remediation_id="wf:r1:full_resync"
    )
    triggered = await handler.handle("gitops.remediate_sync", payload)
    assert triggered["status"] == "REMEDIATION_TRIGGERED"
    assert triggered["previous_operation_started_at"] == FAILED_AT
    (patch,) = _operation_patches(kubectl)
    assert patch[1]["value"]["sync"] == {"prune": False, "syncStrategy": {"hook": {}}}
    assert patch[2] == {"op": "remove", "path": "/status/operationState"}

    # At most once, and only for the diagnosed inherited operation.
    again = await _controller(tmp_path / "b", FakeKubectl(application=json.loads(original))).handle(
        "gitops.remediate_sync", {**payload, "attempt": 2}
    )
    assert again["blocker"] == "ARGO_REMEDIATION_SCOPE_RETRIES_EXHAUSTED"
    stale = await _controller(tmp_path / "c", FakeKubectl(application=json.loads(original))).handle(
        "gitops.remediate_sync", {**payload, "failed_operation_started_at": "2026-10-01T00:00Z"}
    )
    assert stale["blocker"] == "ARGO_REMEDIATION_STALE_DIAGNOSIS"
    full = json.loads(original)
    full["status"]["operationState"]["operation"]["sync"] = {}
    clean = await _controller(tmp_path / "d", FakeKubectl(application=full)).handle(
        "gitops.remediate_sync", payload
    )
    assert clean["blocker"] == "ARGO_REMEDIATION_STALE_DIAGNOSIS"


@pytest.mark.asyncio
async def test_new_116_escalation_lists_out_of_sync_resources(tmp_path: Path) -> None:
    # The sync "succeeded" (selectively) but the application is OutOfSync: the issue must
    # name what is OutOfSync instead of "Failed resources: none / Degraded: none".
    merged = json.loads(json.dumps(OPERATOR_SELECTIVE))
    merged["operation"]["initiatedBy"] = {"username": "hermes-engineering"}
    application = _application(
        operationState=merged,
        sync={"status": "OutOfSync"},
        resources=[
            {**_managed("Deployment", "falcone-control-plane-executor"), "status": "Synced"},
            _managed("Job", "falcone-migrate", group="batch"),
            _managed("Deployment", "falcone-web"),
        ],
    )
    kubectl = FakeKubectl(application=application)
    diagnosed = await _controller(tmp_path, kubectl).handle(
        "gitops.diagnose_sync", {"plan": _plan()}
    )
    diagnosis = diagnosed["diagnosis"]
    assert diagnosis["failed_resources"] == [] and diagnosis["degraded_resources"] == []
    assert [item["name"] for item in diagnosis["out_of_sync_resources"]] == [
        "falcone-migrate",
        "falcone-web",
    ]
    assert diagnosis["out_of_sync_resources_total"] == 2
    assert diagnosis["operation_scope"] == {
        "resources_total": 1,
        "sync_options": ["Replace=true"],
        "strategy": "hook",
    }

    handler, fake = _github(tmp_path / "gh")
    opened = await handler.handle(
        "gitops.environment_issue",
        {
            "plan": _plan(),
            "merge_sha": MERGE_SHA,
            "action": "report_sync",
            "failure_class": "SCOPE_INHERITED",
            "blocker": "ARGO_SYNC_FAILED",
            "diagnosis": diagnosis,
            "attempts": [
                {
                    "round": 1,
                    "action": "full_resync",
                    "failure_class": "SCOPE_INHERITED",
                    "resources": [],
                    "result": "SYNC_SCOPE_INHERITED",
                }
            ],
        },
    )
    assert opened["status"] == "ISSUE_OPENED"
    body = fake.issues[("gntik-ai/musematic", opened["issue_number"])]["body"]
    section = body.split("### OutOfSync resources", 1)[1]
    assert "`falcone-migrate` status=`OutOfSync`" in section
    assert "`falcone-web` status=`OutOfSync`" in section
    assert "executor" not in section
    assert "Last operation scope: selective (1 resource(s))" in body
    assert "`SCOPE_INHERITED`" in body and "`full_resync`" in body


# -- NEW-118: resources Argo CD would prune are detected and escalated, never pruned ----

PRUNE_AT = "2026-10-03T12:00:00Z"
KEYCLOAK_CONFIG = {
    "group": "",
    "kind": "ConfigMap",
    "namespace": "musematic-platform",
    "name": "falcone-keycloak-config",
}


def _prunable(kind: str, name: str, *, group: str = "", namespace: str = "musematic-platform"):
    return {
        "group": group,
        "version": "v1",
        "kind": kind,
        "namespace": namespace,
        "name": name,
        "status": "OutOfSync",
        "requiresPruning": True,
    }


def _prune_application(*extra: dict[str, Any]) -> dict[str, Any]:
    """falcone-charts #45: the full sync applied, only the removed ConfigMap is left."""
    return _application(
        operationState={
            "phase": "Succeeded",
            "startedAt": PRUNE_AT,
            "operation": {
                "initiatedBy": {"username": "hermes-engineering"},
                "sync": {"prune": False, "syncStrategy": {"hook": {}}},
            },
            "syncResult": {"resources": [], "revisions": [OLD_CHART_REVISION, "e" * 40]},
        },
        sync={"status": "OutOfSync"},
        resources=[
            {**_managed("Deployment", "web"), "status": "Synced"},
            _prunable("ConfigMap", "falcone-keycloak-config"),
            *extra,
        ],
    )


@pytest.mark.asyncio
async def test_new_118_sync_status_reports_drift_that_only_pruning_fixes(tmp_path: Path) -> None:
    kubectl = FakeKubectl(application=_prune_application())
    handler = _controller(tmp_path, kubectl)
    payload = {**_plan_payload(), "previous_operation_started_at": "2026-10-03T11:00:00Z"}
    result = await handler.handle("gitops.sync_status", payload)
    assert result["status"] == "SYNC_PRUNE_REQUIRED"
    assert (result["blocker"], result["operation_started_at"]) == ("ARGO_PRUNE_REQUIRED", PRUNE_AT)
    # Other drift still waits for the sync to settle (or time out) as before.
    kubectl.application = _prune_application(_managed("Deployment", "api"))
    assert (await handler.handle("gitops.sync_status", payload))["status"] == "SYNC_PENDING"
    # Detection is read-only: no write of any kind reached the cluster.
    assert {call[2] for call in kubectl.calls if call[0] == "--namespace"} == {"get"}


@pytest.mark.asyncio
async def test_new_118_diagnosis_lists_every_prune_resource_with_its_tracking_id(
    tmp_path: Path,
) -> None:
    kubectl = FakeKubectl(application=_prune_application(_prunable("Secret", "old-credentials")))
    diagnosed = await _controller(tmp_path, kubectl).handle(
        "gitops.diagnose_sync", {"plan": _plan()}
    )
    assert diagnosed["failure_class"] == "PRUNE_REQUIRED"
    diagnosis = diagnosed["diagnosis"]
    assert diagnosis["prune_required_resources_total"] == 2
    assert diagnosis["prune_required_resources"] == [
        {
            **KEYCLOAK_CONFIG,
            "status": "OutOfSync",
            "tracking_id": "musematic-platform:/ConfigMap:musematic-platform/"
            "falcone-keycloak-config",
            "safe_kind": True,
            "in_flow_namespaces": True,
        },
        {
            **KEYCLOAK_CONFIG,
            "kind": "Secret",
            "name": "old-credentials",
            "status": "OutOfSync",
            "tracking_id": "musematic-platform:/Secret:musematic-platform/old-credentials",
            "safe_kind": False,
            "in_flow_namespaces": True,
        },
    ]


@pytest.mark.asyncio
async def test_new_118_hermes_has_no_prune_remediation(tmp_path: Path) -> None:
    from engineering_control.activities.gitops import SYNC_REMEDIATION_ACTIONS

    assert not any("prune" in action for action in SYNC_REMEDIATION_ACTIONS)
    kubectl = FakeKubectl(application=_prune_application())
    handler = _controller(tmp_path, kubectl)
    for action in ("prune_removed", "prune_full_sync", "prune"):
        refused = await handler.handle(
            "gitops.remediate_sync",
            {
                "plan": _plan(),
                "merge_sha": MERGE_SHA,
                "action": action,
                "round": 1,
                "remediation_id": f"wf-1:round-1:{action}",
                "resources": [KEYCLOAK_CONFIG],
            },
        )
        assert refused == {"status": "BLOCKED", "blocker": "ARGO_REMEDIATION_ACTION_INVALID"}
    # Even a full sync Hermes writes keeps prune=false.
    await handler.handle("gitops.trigger_sync", _plan_payload())
    assert all(
        operation["value"]["sync"]["prune"] is False
        for patch in _operation_patches(kubectl)
        for operation in patch
        if operation["path"] == "/operation"
    )
    assert not any(call[2] == "delete" for call in kubectl.calls if call[0] == "--namespace")


@pytest.mark.asyncio
async def test_new_118_escalation_issue_gives_verification_and_operator_commands(
    tmp_path: Path,
) -> None:
    kubectl = FakeKubectl(
        application=_prune_application(
            _prunable("Secret", "old-credentials"),
            _prunable("StatefulSet", "old-db", group="apps"),
            _prunable("Weird", "Bad_Name"),
        )
    )
    diagnosed = await _controller(tmp_path, kubectl).handle(
        "gitops.diagnose_sync", {"plan": _plan()}
    )
    handler, fake = _github(tmp_path / "gh")
    opened = await handler.handle(
        "gitops.environment_issue",
        {
            "plan": _plan(),
            "merge_sha": MERGE_SHA,
            "action": "report_sync",
            "failure_class": "PRUNE_REQUIRED",
            "blocker": "ARGO_PRUNE_REQUIRED",
            "diagnosis": diagnosed["diagnosis"],
            "attempts": [],
        },
    )
    body = fake.issues[("gntik-ai/musematic", opened["issue_number"])]["body"]
    assert "(blocker `ARGO_PRUNE_REQUIRED`, failure class `PRUNE_REQUIRED`)" in body
    section = body.split("### Requires prune", 1)[1]
    assert "Hermes never prunes or deletes" in section
    keycloak, secret, statefulset, weird = section.split("\n- ")[1:]
    assert "(Argo CD tracking id `musematic-platform:/ConfigMap:musematic-platform/" in keycloak
    assert "safe-kind allowlist: yes (informational)" in keycloak
    assert (
        "verify (read-only): `kubectl -n musematic-platform get configmap/falcone-keycloak-config"
        " -o yaml` and `kubectl -n musematic-platform get deployments,statefulsets,daemonsets,"
        "jobs,cronjobs,pods -o yaml | grep -n -F -- 'falcone-keycloak-config'`" in keycloak
    )
    assert (
        "prune (run only after verifying it is unused): "
        "`kubectl -n musematic-platform delete configmap/falcone-keycloak-config`" in keycloak
    )
    for entry, resource in (
        (secret, "secret/old-credentials"),
        (statefulset, "statefulset.apps/old-db"),
    ):
        assert "safe-kind allowlist: no (informational)" in entry
        assert f"get {resource} -o yaml" in entry
        assert "requires manual review; do not delete blindly." in entry
        assert "delete" not in entry.replace("do not delete blindly", "")
    # An identifier that is not a valid Kubernetes name gets no command at all.
    assert "kubectl" not in weird and "requires manual review; do not delete blindly." in weird


@pytest.mark.asyncio
async def test_new_118_a_recorded_source_merge_survives_rechecks_and_skips_pin_convergence(
    tmp_path: Path,
) -> None:
    # Codex round 2: merge the source, then the deployment CI fails and a recheck persists
    # merge_sha=None, then OpenSpec preparation of the repair resets the state to PLANNED.
    # The source PR is still merged: convergence must not touch the source branch.
    from engineering_control.activities.metadata import MetadataActivityHandler

    handler = _controller(tmp_path, FakeKubectl())
    change_set_id = _seed_change_set(handler)
    plan = _plan(change_set_id=str(change_set_id))
    await handler.handle(
        "gitops.persist_result",
        {
            "plan": plan,
            "outcome": "BLOCKED",
            "blocker": "DEPLOYMENT_PR_MERGE_BLOCKED",
            "merge_sha": MERGE_SHA,
        },
    )
    recheck = await handler.handle(
        "gitops.persist_result",
        {
            "plan": plan,
            "outcome": "BLOCKED",
            "blocker": "DEPLOYMENT_PR_CI_REGRESSION",
            "ci_regressions": ["Chart release"],
            "ci_regression_target": "deployment",
        },
    )
    assert recheck["status"] == "PERSISTED"
    with handler.sessions.begin() as session:
        change_set = session.get(db.ChangeSet, change_set_id)
        assert change_set is not None
        recovery = change_set.recovery_json
        assert recovery["staging_gitops"]["blocker"] == "DEPLOYMENT_PR_CI_REGRESSION"
        assert recovery["staging_gitops"]["merge_sha"] == MERGE_SHA
        assert recovery["source_merge"]["merge_sha"] == MERGE_SHA
        assert recovery["source_merge"]["source_pr_url"] == plan["source_pr_url"]
        change_set.state = "PLANNED"  # the repair's OpenSpec preparation
    metadata = MetadataActivityHandler(handler.settings)
    try:
        assert metadata._source_pr_merged(change_set_id) is True
    finally:
        metadata.engine.dispose()
    # A recheck with a different merge SHA never rewrites the first recorded merge.
    await handler.handle(
        "gitops.persist_result",
        {"plan": plan, "outcome": "BLOCKED", "blocker": "X_FAILED", "merge_sha": "f" * 40},
    )
    with handler.sessions.begin() as session:
        change_set = session.get(db.ChangeSet, change_set_id)
        assert change_set is not None
        assert change_set.recovery_json["source_merge"]["merge_sha"] == MERGE_SHA


async def test_new_118_a_stale_source_pr_binding_never_counts_as_merged(tmp_path: Path) -> None:
    # Codex rounds 3-4: after the ChangeSet is rebound to a new source PR, a replayed
    # persist for the old PR (even one carrying a merge SHA or STAGING_VERIFIED) moves
    # neither the heads, the state nor the staging record, and never makes the new PR
    # merged.
    from engineering_control.activities.metadata import MetadataActivityHandler

    handler = _controller(tmp_path, FakeKubectl())
    change_set_id = _seed_change_set(handler)
    plan = _plan(change_set_id=str(change_set_id))
    await handler.handle(
        "gitops.persist_result",
        {"plan": plan, "outcome": "BLOCKED", "blocker": "X_FAILED", "merge_sha": MERGE_SHA},
    )
    new_url = plan["source_pr_url"].rsplit("/", 1)[0] + "/78"
    with handler.sessions.begin() as session:
        change_set = session.get(db.ChangeSet, change_set_id)
        assert change_set is not None
        change_set.source_pr_url = new_url
        change_set.source_head_sha = "c" * 40
        change_set.state = "PLANNED"
        before = dict(change_set.recovery_json["staging_gitops"])
    for payload in (
        {"outcome": "BLOCKED", "blocker": "DEPLOYMENT_PR_CI_REGRESSION"},
        {"outcome": "BLOCKED", "blocker": "X_FAILED", "merge_sha": "f" * 40},
        {"outcome": "STAGING_VERIFIED", "merge_sha": "f" * 40},
    ):
        stale = await handler.handle("gitops.persist_result", {"plan": plan, **payload})
        assert stale["status"] == "PERSISTED"
        assert stale["change_set_state"] == "PLANNED"
    with handler.sessions.begin() as session:
        change_set = session.get(db.ChangeSet, change_set_id)
        assert change_set is not None
        assert change_set.state == "PLANNED"
        assert change_set.source_head_sha == "c" * 40
        assert change_set.recovery_json["staging_gitops"] == before
    metadata = MetadataActivityHandler(handler.settings)
    try:
        assert metadata._source_pr_merged(change_set_id) is False
    finally:
        metadata.engine.dispose()


async def test_new_118_the_merge_record_is_write_once_per_bound_source_pr(tmp_path: Path) -> None:
    # Codex round 4: #77's merge record must not block recording #78's merge once the
    # ChangeSet is rebound to #78 and #78 merges.
    from engineering_control.activities.metadata import MetadataActivityHandler

    handler = _controller(tmp_path, FakeKubectl())
    change_set_id = _seed_change_set(handler)
    old_plan = _plan(change_set_id=str(change_set_id))
    await handler.handle(
        "gitops.persist_result",
        {"plan": old_plan, "outcome": "BLOCKED", "blocker": "X_FAILED", "merge_sha": MERGE_SHA},
    )
    new_url = old_plan["source_pr_url"].rsplit("/", 1)[0] + "/78"
    with handler.sessions.begin() as session:
        change_set = session.get(db.ChangeSet, change_set_id)
        assert change_set is not None
        change_set.source_pr_url = new_url
        change_set.state = "SOURCE_PR"
    new_plan = {**old_plan, "source_pr_url": new_url, "source_pr_number": 78}
    await handler.handle(
        "gitops.persist_result",
        {"plan": new_plan, "outcome": "BLOCKED", "blocker": "X_FAILED", "merge_sha": "f" * 40},
    )
    await handler.handle(
        "gitops.persist_result",
        {"plan": new_plan, "outcome": "BLOCKED", "blocker": "DEPLOYMENT_PR_CI_REGRESSION"},
    )
    with handler.sessions.begin() as session:
        change_set = session.get(db.ChangeSet, change_set_id)
        assert change_set is not None
        durable = change_set.recovery_json["source_merge"]
        assert durable["source_pr_url"] == new_url
        assert durable["merge_sha"] == "f" * 40
        assert change_set.recovery_json["staging_gitops"]["merge_sha"] == "f" * 40
        change_set.state = "PLANNED"
    metadata = MetadataActivityHandler(handler.settings)
    try:
        assert metadata._source_pr_merged(change_set_id) is True
    finally:
        metadata.engine.dispose()


def test_failure_excerpt_names_every_failing_test_first() -> None:
    """NEW-153: falcone-charts #61 (2026-10-07) failed 10 TAP tests, but noise lines (curl
    --fail, "0 chart(s) failed") filled the context budget and the excerpt named only one."""
    from engineering_control.adapters.github.client import failure_excerpt

    noise = "curl --fail --silent --location x\n1 chart(s) linted, 0 chart(s) failed\n" * 40
    tap = "".join(f"not ok {n} - contract {n} holds\n  error: |-\n" for n in range(150, 160))
    excerpt = failure_excerpt(noise + tap + "# fail 10\n")
    head = excerpt.splitlines()
    assert head[0] == "Failing tests (10):"
    assert [line for line in head[1:11]] == [
        f"not ok {n} - contract {n} holds" for n in range(150, 160)
    ]
    assert len(head) <= 60 and len(excerpt) <= 4000
    many = failure_excerpt("".join(f"not ok {n} - t{n}\n" for n in range(1, 101)))
    assert "Failing tests (100):" in many and "... and 75 more" in many
    assert len(many.splitlines()) <= 60


# -- NEW-155: staging environment recovery ----------------------------------------------

ESO_FUNCTION_INVOCATION = {
    "group": "external-secrets.io",
    "version": "v1",
    "kind": "ExternalSecret",
    "namespace": "musematic-platform",
    "name": "platform-function-invocation",
    "status": "Synced",
}


def _waiting_application(message: str, *resources: dict[str, Any]) -> dict[str, Any]:
    """Production 2026-10-07: Argo CD stuck waiting on one resource's health."""
    return _application(
        operationState={
            "phase": "Running",
            "startedAt": "2026-10-07T10:00:00Z",
            "message": message,
            "operation": {"initiatedBy": {"username": "hermes-engineering"}, "sync": {}},
        },
        sync={"status": "Synced"},
        health={"status": "Progressing"},
        resources=[
            {**_managed("Deployment", "web"), "status": "Synced"},
            _prunable("ConfigMap", "falcone-keycloak-config"),
            *resources,
        ],
    )


@pytest.mark.asyncio
async def test_new_155_a_health_wait_is_never_reported_as_prune_required(tmp_path: Path) -> None:
    from engineering_control.workflows.definitions import StagingGitOpsDeliveryWorkflow

    message = (
        "waiting for healthy state of "
        "external-secrets.io/ExternalSecret/platform-function-invocation"
    )
    kubectl = FakeKubectl(application=_waiting_application(message, ESO_FUNCTION_INVOCATION))
    result = await _controller(tmp_path, kubectl).handle("gitops.diagnose_sync", {"plan": _plan()})
    diagnosis = result["diagnosis"]
    assert result["failure_class"] == "OTHER"
    assert diagnosis["failed_resources"] == []
    assert diagnosis["degraded_resources"] == [
        {
            "group": "external-secrets.io",
            "kind": "ExternalSecret",
            "namespace": "musematic-platform",
            "name": "platform-function-invocation",
            "health": "Unknown",
            "message": "",
            "pod_reasons": [],
            "pod_reasons_total": 0,
            "failure_class": "OTHER",
        }
    ]
    # The prune drift is still reported, just not as the cause.
    assert diagnosis["prune_required_resources_total"] == 1
    assert diagnosis["classes"] == ["OTHER", "PRUNE_REQUIRED"]
    # The (unchanged) workflow mapping then reports the timeout, not ARGO_PRUNE_REQUIRED.
    blocker = StagingGitOpsDeliveryWorkflow._sync_blocker(
        {"status": "TIMED_OUT"}, result["failure_class"]
    )
    assert blocker == "ARGO_SYNC_TIMEOUT"
    assert not [call for call in kubectl.calls if call[0] != "get" and call[2] != "get"]


@pytest.mark.asyncio
async def test_new_155_pure_prune_drift_is_still_prune_required(tmp_path: Path) -> None:
    kubectl = FakeKubectl(application=_prune_application())
    result = await _controller(tmp_path, kubectl).handle("gitops.diagnose_sync", {"plan": _plan()})
    assert result["failure_class"] == "PRUNE_REQUIRED"
    assert result["diagnosis"]["degraded_resources"] == []


@pytest.mark.asyncio
async def test_new_155_waited_on_workloads_and_unknown_resources(tmp_path: Path) -> None:
    web = {**_managed("Deployment", "web", health="Healthy"), "status": "Synced"}
    pods = {"musematic-platform": [_owned_pod("web-5d9c7-abcde", "web-5d9c7", "ErrImagePull")]}
    message = "waiting for healthy state of apps/Deployment/web"
    application = _waiting_application(message)
    application["status"]["resources"] = [web]
    kubectl = FakeKubectl(application=application, pods=pods)
    result = await _controller(tmp_path, kubectl).handle("gitops.diagnose_sync", {"plan": _plan()})
    degraded = result["diagnosis"]["degraded_resources"]
    assert [(item["name"], item["health"], item["failure_class"]) for item in degraded] == [
        ("web", "Healthy", "TRANSIENT")
    ]
    assert result["failure_class"] == "TRANSIENT"
    # Even next to prune drift, a transient wait keeps its own class.
    application["status"]["resources"] = [web, _prunable("ConfigMap", "old")]
    again = await _controller(
        tmp_path / "b", FakeKubectl(application=application, pods=pods)
    ).handle("gitops.diagnose_sync", {"plan": _plan()})
    assert again["failure_class"] == "TRANSIENT"
    assert again["diagnosis"]["prune_required_resources_total"] == 1

    # Several phrases (core group included); one names a resource Argo does not list.
    message = (
        "waiting for healthy state of /Service/missing-svc, "
        "waiting for healthy state of apps/Deployment/web"
    )
    unknown = FakeKubectl(application=_waiting_application(message), pods=pods)
    result = await _controller(tmp_path / "c", unknown).handle(
        "gitops.diagnose_sync", {"plan": _plan()}
    )
    assert result["failure_class"] == "OTHER"
    assert [item["name"] for item in result["diagnosis"]["degraded_resources"]] == ["web"]


SEALED_ERROR = (
    "could not get provider client: unable to log in to auth method: unable to log in with "
    "Kubernetes auth: Error making API request.\n\nURL: PUT https://openbao.secret-store:8200"
    "/v1/auth/kubernetes/login\nCode: 503. Errors:\n\n* Vault is sealed"
)


def _eso_log(*entries: dict[str, Any]) -> str:
    lines = ["plain text line", '{"level":"info","msg":"reconciled"}']
    lines.extend(
        json.dumps({"level": "error", "error": SEALED_ERROR, **entry}) for entry in entries
    )
    return "\n".join(lines) + "\n"


def test_new_155_sealed_stores_are_parsed_from_eso_json_logs() -> None:
    log = _eso_log(
        {
            "logger": "controllers.ClusterSecretStore",
            "clustersecretstore": {"name": "openbao-backend"},
        },
        {"controller": "clustersecretstore", "ClusterSecretStore": {"name": "vault-backend"}},
        {"controller": "secretstore", "SecretStore": {"name": "local", "namespace": "apps"}},
        {"clustersecretstore": {"name": "Not_A_Name"}},
    ) + json.dumps({"error": "permission denied", "clustersecretstore": {"name": "other"}})
    assert sealed_secret_stores(log) == {
        ("ClusterSecretStore", "", "openbao-backend"),
        ("ClusterSecretStore", "", "vault-backend"),
        ("SecretStore", "apps", "local"),
    }


def _sealed_kubectl(**changes: Any) -> FakeKubectl:
    values: dict[str, Any] = {
        "externalsecrets": {
            "musematic-platform": [
                {
                    "metadata": {"name": "platform-db"},
                    "spec": {
                        "secretStoreRef": {"name": "openbao-backend", "kind": "ClusterSecretStore"}
                    },
                    "status": {
                        "conditions": [
                            {"type": "Ready", "status": "False", "reason": "SecretSyncedError"}
                        ]
                    },
                },
                {
                    "metadata": {"name": "platform-cache"},
                    "spec": {"secretStoreRef": {"name": "cache", "kind": "ClusterSecretStore"}},
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                },
            ]
        },
        "stores": {
            "openbao-backend": {
                "status": {
                    "conditions": [
                        {"type": "Ready", "status": "False", "reason": "InvalidProviderConfig"}
                    ]
                }
            },
            "cache": {"status": {"conditions": [{"type": "Ready", "status": "True"}]}},
        },
        "eso_log": _eso_log({"clustersecretstore": {"name": "openbao-backend"}}),
        "application": _application(health={"status": "Degraded"}),
    }
    values.update(changes)
    return FakeKubectl(**values)


def _rollouts(kubectl: FakeKubectl) -> list[list[str]]:
    return [call for call in kubectl.calls if "rollout" in call]


@pytest.mark.asyncio
async def test_new_155_a_sealed_backend_is_named_and_never_restarted(tmp_path: Path) -> None:
    kubectl = _sealed_kubectl()
    handler = _controller(tmp_path, kubectl)
    check = await handler.handle("gitops.environment_check", {"plan": _plan()})
    diagnosis = check["diagnosis"]
    assert check["status"] == "UNHEALTHY"
    assert diagnosis["sealed_backend_stores"] == ["openbao-backend"]
    assert diagnosis["stores_not_ready"] == [
        {
            "kind": "ClusterSecretStore",
            "name": "openbao-backend",
            "namespace": "",
            "reason": "InvalidProviderConfig",
            "sealed": True,
        }
    ]
    assert diagnosis["eso_objects_not_ready"] == [
        {"namespace": "musematic-platform", "name": "platform-db", "reason": "SecretSyncedError"}
    ]
    assert diagnosis["eso_objects_not_ready_total"] == 1
    # No log text reaches the diagnosis: only store names and the boolean.
    dumped = json.dumps(check)
    assert "is sealed" not in dumped and "auth/kubernetes" not in dumped
    logs = [call for call in kubectl.calls if "logs" in call]
    assert logs == [
        [
            "--namespace",
            "external-secrets",
            "logs",
            "deployment/external-secrets",
            "--since=15m",
            "--tail=3000",
        ]
    ]

    result = await handler.handle("gitops.remediate", {"plan": _plan(), "round": 1})
    assert result["actions"] == ["argo-hard-refresh", "argo-sync", "secret-backend-sealed"]
    assert _rollouts(kubectl) == []


@pytest.mark.asyncio
async def test_new_155_unreadable_or_oversized_logs_are_unknown(tmp_path: Path) -> None:
    for index, log in enumerate((None, "x" * 8_000_001)):
        kubectl = _sealed_kubectl(eso_log=log)
        handler = _controller(tmp_path / str(index), kubectl)
        result = await handler.handle("gitops.remediate", {"plan": _plan(), "round": 1})
        assert result["diagnosis"]["sealed_backend_stores"] == []
        assert result["diagnosis"]["stores_not_ready"][0]["sealed"] is False
        assert "restart-external-secrets" in result["actions"]
        assert len(_rollouts(kubectl)) == 1


@pytest.mark.asyncio
async def test_new_155_external_secrets_in_backoff_restart_eso_once_stores_recover(
    tmp_path: Path,
) -> None:
    recovered = {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
    kubectl = _sealed_kubectl(stores={"openbao-backend": recovered, "cache": recovered})
    handler = _controller(tmp_path, kubectl)
    result = await handler.handle("gitops.remediate", {"plan": _plan(), "round": 1})
    assert result["diagnosis"]["stores_not_ready"] == []
    assert result["diagnosis"]["sealed_backend_stores"] == []
    assert result["actions"] == ["argo-hard-refresh", "argo-sync", "restart-external-secrets"]
    assert _rollouts(kubectl) == [
        ["--namespace", "external-secrets", "rollout", "restart", "deployment/external-secrets"]
    ]
    # Every store Ready: the controller log is never read.
    assert not [call for call in kubectl.calls if "logs" in call]

    # Nothing not Ready: no restart.
    synced = _sealed_kubectl(stores={"openbao-backend": recovered, "cache": recovered})
    for item in synced.externalsecrets["musematic-platform"]:
        item["status"] = recovered["status"]
    quiet = await _controller(tmp_path / "b", synced).handle(
        "gitops.remediate", {"plan": _plan(), "round": 1}
    )
    assert quiet["actions"] == ["argo-hard-refresh", "argo-sync"]


@pytest.mark.asyncio
async def test_new_155_environment_issue_explains_a_sealed_backend(tmp_path: Path) -> None:
    handler, fake = _github(tmp_path)
    diagnosis = {
        "argo_health": "Degraded",
        "stores_not_ready": [
            {
                "kind": "ClusterSecretStore",
                "name": "openbao-backend",
                "namespace": "",
                "reason": "InvalidProviderConfig",
                "sealed": True,
            }
        ],
        "sealed_backend_stores": ["openbao-backend", "vault-musematic", "team-store"],
        "eso_objects_not_ready": [
            {
                "namespace": "musematic-platform",
                "name": "platform-db",
                "reason": "SecretSyncedError",
            }
        ],
    }
    opened = await handler.handle(
        "gitops.environment_issue",
        {"plan": _plan(), "action": "report", "round": 1, "diagnosis": diagnosis},
    )
    assert opened["status"] == "ISSUE_OPENED"
    body = fake.issues[("gntik-ai/musematic", opened["issue_number"])]["body"]
    assert "### Secret backends sealed" in body
    assert "`openbao-backend`, `vault-musematic`, `team-store`" in body
    assert "Hermes cannot unseal" in body and "3 unseal keys" in body
    assert "kubectl -n secret-store get secret openbao-recovery" in body
    assert "kubectl -n secret-store exec -i openbao-0" in body
    assert "bao operator unseal" in body
    assert "kubectl -n vault exec -i vault-0" in body and "vault operator unseal" in body
    assert "platform-db" in body and "SecretSyncedError" in body

    # Unknown store names get only the generic instruction.
    generic = {**diagnosis, "sealed_backend_stores": ["team-store"]}
    commented = await handler.handle(
        "gitops.environment_issue",
        {"plan": _plan(), "action": "report", "round": 2, "diagnosis": generic},
    )
    comment = fake.issues[("gntik-ai/musematic", commented["issue_number"])]["comments"][-1]
    assert "3 unseal keys" in comment["body"] and "kubectl -n" not in comment["body"]

    healthy = {**diagnosis, "sealed_backend_stores": []}
    again = await handler.handle(
        "gitops.environment_issue",
        {"plan": _plan(), "action": "report", "round": 3, "diagnosis": healthy},
    )
    comment = fake.issues[("gntik-ai/musematic", again["issue_number"])]["comments"][-1]
    assert "Secret backends sealed" not in comment["body"]
