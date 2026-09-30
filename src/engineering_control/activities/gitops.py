"""Post-merge staging GitOps delivery (Argo CD + argocd-image-updater).

Two handlers share this module:

* ``GitOpsControllerHandler`` runs on the controller worker. It owns the database
  (ChangeSet binding and result persistence) and the bounded kubectl surface used to
  observe/remediate the staging environment and to trigger the Argo CD sync through
  the Application resource itself (no Argo API credentials).
* ``GitOpsGitHubHandler`` runs on the GitHub worker (reached through
  ``GitHubActivityHandler``). It owns every gh call: PR checks, ready+merge, release
  tags, workflow runs, the deploy-repo image pin and issue comments/closures. Every
  write is gated by the same ``GitHubWritePolicyEvaluator`` as the delivery writes.

Every activity is short, idempotent and fails closed: the workflow polls with
``workflow.sleep`` between calls, and the project contract is re-read from disk on
every call instead of trusting the workflow payload.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from temporalio.exceptions import ApplicationError

from engineering_control.adapters.command import CommandFailure, CommandResult, SafeCommandRunner
from engineering_control.adapters.github.client import (
    GhCliAuthError,
    GhCliBroker,
    GhCliPolicyError,
)
from engineering_control.config import Settings
from engineering_control.db import models as db
from engineering_control.engineering.models import ProjectManifest
from engineering_control.models.enums import ChangeSetState
from engineering_control.models.reconciliation import StagingFlowPolicy
from engineering_control.policy.github_write import (
    GITOPS_READ_ONLY_GITHUB_STEPS,
    GITOPS_WRITE_CAPABLE_GITHUB_STEPS,
    GitHubWritePolicyEvaluator,
)
from engineering_control.security.redaction import SecretRedactor

GITOPS_CONTROLLER_STEPS = frozenset(
    {
        "gitops.flow_for_project",
        "gitops.prepare",
        "gitops.environment_check",
        "gitops.remediate",
        "gitops.trigger_sync",
        "gitops.sync_status",
        "gitops.verify_workloads",
        "gitops.persist_result",
    }
)
GITOPS_GITHUB_STEPS = GITOPS_READ_ONLY_GITHUB_STEPS | GITOPS_WRITE_CAPABLE_GITHUB_STEPS
ARGO_SYNC_USERNAME = "hermes-engineering"
UNHEALTHY_POD_REASONS = frozenset({"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "Error"})
MAXIMUM_POD_DELETIONS = 25
_SHA = re.compile(r"[a-f0-9]{40}")
_KUBERNETES_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
_PR_URL = re.compile(
    r"https://github\.com/(gntik-ai/(?:falcone|falcone-charts|musematic|musematic-deploy|llmwiki))"
    r"/pull/([1-9][0-9]*)"
)
_REPOSITORY = r"^gntik-ai/(falcone|falcone-charts|musematic|musematic-deploy|llmwiki)$"


class StagingFlowBlocked(ValueError):
    """A fail-closed stop with a stable operator-facing blocker code."""

    def __init__(self, blocker: str) -> None:
        super().__init__(blocker)
        self.blocker = blocker


class StagingGitOpsPlan(BaseModel):
    """Immutable ChangeSet binding produced by gitops.prepare from the database."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    change_set_id: UUID
    project: Literal["falcone", "musematic", "llmwiki"]
    risk_class: Literal["R0", "R1", "R2", "R3"]
    source_repository: str = Field(pattern=_REPOSITORY)
    source_pr_number: int = Field(ge=1)
    source_pr_url: str = Field(max_length=300)
    source_head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_base_branch: str = Field(default="main", pattern=r"^[A-Za-z0-9._/-]{1,120}$")
    deployment_repository: str = Field(pattern=_REPOSITORY)
    deployment_pr_number: int | None = Field(default=None, ge=1)
    deployment_pr_url: str | None = Field(default=None, max_length=300)
    deployment_head_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    deployment_base_branch: str = Field(default="main", pattern=r"^[A-Za-z0-9._/-]{1,120}$")
    issue_repository: str = Field(pattern=_REPOSITORY)
    issue_number: int = Field(ge=1)

    @model_validator(mode="after")
    def bound_pull_requests(self) -> StagingGitOpsPlan:
        match = _PR_URL.fullmatch(self.source_pr_url)
        if match is None or match.group(1) != self.source_repository:
            raise ValueError("source PR URL does not match the source repository")
        if int(match.group(2)) != self.source_pr_number:
            raise ValueError("source PR number does not match its URL")
        if self.deployment_pr_number is not None:
            deployment = _PR_URL.fullmatch(self.deployment_pr_url or "")
            if (
                deployment is None
                or deployment.group(1) != self.deployment_repository
                or int(deployment.group(2)) != self.deployment_pr_number
                or self.deployment_head_sha is None
            ):
                raise ValueError("deployment PR binding is incomplete")
        return self


def load_project_contract(
    project_config_root: Path, project: str
) -> tuple[ProjectManifest, StagingFlowPolicy | None]:
    if project not in {"falcone", "musematic", "llmwiki"}:
        raise StagingFlowBlocked("STAGING_FLOW_PROJECT_INVALID")
    root = project_config_root.resolve()
    unresolved = root / f"{project}.yaml"
    if unresolved.is_symlink():
        raise StagingFlowBlocked("STAGING_FLOW_CONTRACT_INVALID")
    path = unresolved.resolve()
    if not path.is_file() or path.parent != root or path.stat().st_size > 512 * 1024:
        raise StagingFlowBlocked("STAGING_FLOW_CONTRACT_MISSING")
    try:
        manifest = ProjectManifest.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (ValidationError, yaml.YAMLError, UnicodeError) as error:
        raise StagingFlowBlocked("STAGING_FLOW_CONTRACT_INVALID") from error
    if manifest.project != project:
        raise StagingFlowBlocked("STAGING_FLOW_CONTRACT_INVALID")
    return manifest, manifest.staging_flow()


def enabled_staging_flow(
    project_config_root: Path, project: str
) -> tuple[ProjectManifest, StagingFlowPolicy]:
    manifest, flow = load_project_contract(project_config_root, project)
    blockers = manifest.staging_flow_blockers()
    if flow is None or not flow.enabled or blockers:
        raise StagingFlowBlocked(blockers[0] if blockers else "STAGING_FLOW_DISABLED")
    return manifest, flow


def expected_image_tag(flow: StagingFlowPolicy, merge_sha: str) -> str:
    if not _SHA.fullmatch(merge_sha):
        raise StagingFlowBlocked("MERGE_SHA_INVALID")
    return f"{flow.tag_prefix}{merge_sha[: flow.tag_length]}"


def next_release_tag(tag_names: Iterable[str], prefix: str) -> str:
    """Bump the patch of the highest ``<prefix>MAJOR.MINOR.PATCH`` tag (semver order)."""
    pattern = re.compile(rf"{re.escape(prefix)}(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
    versions = [
        (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        for name in tag_names
        if (match := pattern.fullmatch(name)) is not None
    ]
    major, minor, patch = max(versions, default=(0, 0, 0))
    return f"{prefix}{major}.{minor}.{patch + 1}"


def split_image(image: str) -> tuple[str, str | None]:
    """Return (repository, tag) for ``repo[:tag][@digest]`` references."""
    reference = image.split("@", 1)[0]
    last_slash = reference.rfind("/")
    colon = reference.rfind(":")
    if colon > last_slash:
        return reference[:colon], reference[colon + 1 :]
    return reference, None


def image_tags_in_values(text: str, repositories: Iterable[str]) -> dict[str, list[str]]:
    """Find ``tag:`` values bound to the configured image repositories in a values file.

    The file may be secret-bearing: nothing but the image tags of the configured
    repositories ever leaves this function.
    """
    wanted = set(repositories)
    found: dict[str, list[str]] = {}

    def record(repository: str, tag: object) -> None:
        if repository in wanted and isinstance(tag, str | int) and str(tag):
            found.setdefault(repository, []).append(str(tag)[:128])

    def walk(node: object) -> None:
        if isinstance(node, dict):
            repository = node.get("repository")
            if isinstance(repository, str) and "tag" in node:
                registry = node.get("registry")
                candidate = (
                    f"{registry}/{repository}"
                    if isinstance(registry, str) and repository not in wanted
                    else repository
                )
                record(candidate, node.get("tag"))
            image = node.get("image")
            if isinstance(image, str):
                name, tag = split_image(image)
                if tag is not None:
                    record(name, tag)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    try:
        documents = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        documents = []
        # Fallback line scan: a `repository:` line followed by `tag:` at the same indent.
        pending: tuple[int, str] | None = None
        for line in text.splitlines():
            stripped = line.strip()
            indent = len(line) - len(line.lstrip(" "))
            key, _, raw = stripped.partition(":")
            value = raw.strip().strip("\"'")
            if key == "repository" and value:
                pending = (indent, value)
            elif key == "tag" and pending is not None and pending[0] == indent:
                record(pending[1], value)
                pending = None
    for document in documents:
        walk(document)
    return found


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _items(value: object, label: str) -> list[dict[str, Any]]:
    items = _object(value, label).get("items")
    if not isinstance(items, list) or len(items) > 2000:
        raise ValueError(f"{label} is not a bounded list")
    return [item for item in items if isinstance(item, dict)]


def _name(item: dict[str, Any]) -> str:
    metadata = item.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    return str(name or "")[:253]


def _nested(value: object, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def pod_failure_reason(pod: dict[str, Any]) -> str | None:
    status = pod.get("status")
    if not isinstance(status, dict) or status.get("phase") == "Succeeded":
        return None
    statuses: list[Any] = []
    for key in ("initContainerStatuses", "containerStatuses"):
        values = status.get(key)
        if isinstance(values, list):
            statuses.extend(values)
    for container in statuses:
        waiting = _nested(container, "state", "waiting", "reason")
        if waiting in UNHEALTHY_POD_REASONS:
            return str(waiting)
        if _nested(container, "state", "terminated", "reason") == "Error":
            return "Error"
    if status.get("phase") == "Failed":
        return "Error"
    return None


def _desired(item: dict[str, Any]) -> int:
    replicas = _nested(item, "spec", "replicas")
    return int(replicas) if isinstance(replicas, int) else 1


def _status_count(item: dict[str, Any], key: str) -> int:
    value = _nested(item, "status", key)
    return int(value) if isinstance(value, int) else 0


def _normalize_repo_url(value: str) -> str:
    return value.strip().lower().rstrip("/").removesuffix(".git")


class GitOpsControllerHandler:
    """Controller-side DB + bounded kubectl activities for the staging GitOps flow."""

    supported_steps = GITOPS_CONTROLLER_STEPS

    def __init__(
        self,
        settings: Settings,
        *,
        runner: SafeCommandRunner | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner or SafeCommandRunner({"kubectl": settings.kubectl_executable})
        self.redactor = SecretRedactor()
        database_url = settings.database_url.get_secret_value()
        engine_options: dict[str, Any] = {"pool_pre_ping": True}
        if database_url.startswith("postgresql"):
            engine_options.update(pool_size=2, max_overflow=1, connect_args={"connect_timeout": 5})
        self.engine = create_engine(database_url, **engine_options)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "gitops.flow_for_project": self._flow_for_project,
            "gitops.prepare": self._prepare,
            "gitops.environment_check": self._environment_check,
            "gitops.remediate": self._remediate,
            "gitops.trigger_sync": self._trigger_sync,
            "gitops.sync_status": self._sync_status,
            "gitops.verify_workloads": self._verify_workloads,
            "gitops.persist_result": self._persist_result,
        }
        operation = handlers.get(step)
        if operation is None:
            raise ApplicationError("unsupported staging GitOps activity", non_retryable=True)
        try:
            result = await asyncio.to_thread(operation, payload)
        except StagingFlowBlocked as error:
            return {"status": "BLOCKED", "blocker": error.blocker}
        except (CommandFailure, OSError, ValueError, ValidationError) as error:
            if step == "gitops.persist_result":
                raise ApplicationError(
                    "staging GitOps result could not be persisted",
                    type="SchemaViolation",
                    non_retryable=True,
                ) from error
            return {
                "status": "BLOCKED",
                "blocker": f"STAGING_GITOPS_{step.split('.', 1)[1].upper()}_FAILED",
                "error_type": type(error).__name__,
            }
        safe = self.redactor.redact(result).value
        if not isinstance(safe, dict):
            raise TypeError("redacted staging GitOps result must remain an object")
        return safe

    # -- contract / database -------------------------------------------------------

    def _flow_for_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        project = str(payload.get("project", ""))
        try:
            manifest, flow = load_project_contract(self.settings.project_config_root, project)
        except StagingFlowBlocked as error:
            # An unreadable contract must never silently select the flow.
            return {"status": "DISABLED", "enabled": False, "blocker": error.blocker}
        if flow is None or not flow.enabled:
            return {"status": "DISABLED", "enabled": False}
        return {
            "status": "ENABLED",
            "enabled": True,
            "blockers": manifest.staging_flow_blockers(),
        }

    def _prepare(self, payload: dict[str, Any]) -> dict[str, Any]:
        project = str(payload.get("project", ""))
        manifest, flow = enabled_staging_flow(self.settings.project_config_root, project)
        change_set_id = UUID(str(payload.get("change_set_id", "")))
        issue_repository = str(payload.get("issue_repository", ""))
        if issue_repository not in {
            manifest.repositories.source.issue_repository,
            manifest.repositories.deployment.issue_repository,
        }:
            raise StagingFlowBlocked("DELIVERED_ISSUE_REPOSITORY_OUTSIDE_PROJECT")
        with self.sessions.begin() as session:
            change_set = session.get(db.ChangeSet, change_set_id)
            if change_set is None or change_set.project != project:
                raise StagingFlowBlocked("CHANGE_SET_BINDING_MISSING")
            record = dict(change_set.recovery_json or {}).get("staging_gitops")
            if (
                change_set.state == ChangeSetState.RUNTIME_VERIFIED.value
                and isinstance(record, dict)
                and record.get("status") == "STAGING_VERIFIED"
            ):
                return {"status": "ALREADY_VERIFIED", "staging_gitops": record}
            if change_set.state not in {
                ChangeSetState.SOURCE_PR.value,
                ChangeSetState.CI_VALIDATED.value,
                ChangeSetState.SOURCE_MERGED.value,
            }:
                raise StagingFlowBlocked("CHANGE_SET_STATE_NOT_DELIVERABLE")
            if change_set.risk_class == "R3" and not flow.allow_r3_merge:
                raise StagingFlowBlocked("R3_MERGE_REQUIRES_APPROVAL")
            source_url = str(change_set.source_pr_url or "")
            source = _PR_URL.fullmatch(source_url)
            if source is None or not change_set.source_head_sha:
                raise StagingFlowBlocked("SOURCE_PR_BINDING_MISSING")
            deployment_url = change_set.deployment_pr_url
            deployment = _PR_URL.fullmatch(deployment_url) if deployment_url else None
            plan = StagingGitOpsPlan(
                change_set_id=change_set.id,
                project=project,  # type: ignore[arg-type]
                risk_class=change_set.risk_class,  # type: ignore[arg-type]
                source_repository=str(manifest.repositories.source.full_name),
                source_pr_number=int(source.group(2)),
                source_pr_url=source_url,
                source_head_sha=str(change_set.source_head_sha),
                source_base_branch=manifest.repositories.source.default_branch,
                deployment_repository=str(manifest.repositories.deployment.full_name),
                deployment_pr_number=int(deployment.group(2)) if deployment else None,
                deployment_pr_url=deployment_url if deployment else None,
                deployment_head_sha=(change_set.deployment_head_sha if deployment else None),
                deployment_base_branch=manifest.repositories.deployment.default_branch,
                issue_repository=issue_repository,
                issue_number=int(payload.get("issue_number", 0)),
            )
            if change_set.source_repository != plan.source_repository.rsplit("/", 1)[-1]:
                raise StagingFlowBlocked("CHANGE_SET_SOURCE_REPOSITORY_MISMATCH")
        return {
            "status": "READY",
            "plan": plan.model_dump(mode="json"),
            "staging_flow": {
                "argo_application": flow.argo_application,
                "main_ci_workflow": flow.main_ci_workflow,
                "image_workflow": flow.image_workflow,
                "release_tag_enabled": flow.release_tag.enabled,
                "pin_chart_to_merge": flow.pin_chart_to_merge,
            },
        }

    def _persist_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = StagingGitOpsPlan.model_validate(payload.get("plan"))
        outcome = str(payload.get("outcome", ""))
        if outcome not in {"STAGING_VERIFIED", "BLOCKED", "CANCELLED"}:
            raise ValueError("staging GitOps outcome is invalid")
        merge_sha = payload.get("merge_sha")
        if merge_sha is not None and not _SHA.fullmatch(str(merge_sha)):
            raise ValueError("merge SHA is invalid")
        blocker = payload.get("blocker")
        if blocker is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", str(blocker)):
            blocker = "STAGING_GITOPS_FAILED"
        record: dict[str, Any] = {
            "status": outcome,
            "blocker": blocker,
            "merge_sha": merge_sha,
            "deployment_merge_sha": payload.get("deployment_merge_sha"),
            "release_tag": payload.get("release_tag"),
            # NEW-68: set when a later release containing the merge shipped the change.
            "release_sha": (
                str(payload["release_sha"])
                if _SHA.fullmatch(str(payload.get("release_sha") or ""))
                else None
            ),
            "image_tag": payload.get("image_tag"),
            "pinned_repositories": payload.get("pinned_repositories") or [],
            "argo_revision": payload.get("argo_revision"),
            "chart_pin": payload.get("chart_pin"),
            "environment_issue_number": payload.get("environment_issue_number"),
            "remediation_rounds": payload.get("remediation_rounds", 0),
            # NEW-63/64: CI job names only (the baseline gate's verdict).
            "ci_regressions": _check_names(payload.get("ci_regressions")),
            "ci_preexisting_failures": _check_names(payload.get("ci_preexisting_failures")),
            # NEW-100: bounded, redacted failing-log lines per regressed job.
            "ci_failure_excerpts": _excerpts(payload.get("ci_failure_excerpts")),
            # NEW-103: which PR's CI regressed (the deployment PR has its own gate).
            "ci_regression_target": (
                "deployment" if payload.get("ci_regression_target") == "deployment" else "source"
            ),
            # NEW-99: why the baseline could not excuse anything (None when it ran).
            "ci_regression_reason": (
                str(payload["ci_regression_reason"])
                if payload.get("ci_regression_reason") in UNVERIFIED_CI_REGRESSION_REASONS
                else None
            ),
            "ci_baseline_pr": (
                str(payload["ci_baseline_pr"])
                if isinstance(payload.get("ci_baseline_pr"), str)
                and _PR_URL.fullmatch(str(payload["ci_baseline_pr"]))
                else None
            ),
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        orchestration = payload.get("orchestration")
        workflow_id = (
            str(orchestration.get("workflow_id", "")) if isinstance(orchestration, dict) else ""
        )
        with self.sessions.begin() as session:
            change_set = session.get(db.ChangeSet, plan.change_set_id)
            if change_set is None or change_set.project != plan.project:
                raise ValueError("staging GitOps ChangeSet binding is missing")
            recovery = dict(change_set.recovery_json or {})
            recovery["staging_gitops"] = record
            change_set.recovery_json = recovery
            if merge_sha is not None:
                # NEW-102: the merged PR heads are what was delivered; a stale write (e.g. a
                # repair's unpublished local head) never survives on a merged ChangeSet.
                change_set.source_head_sha = plan.source_head_sha
                if plan.deployment_pr_number is not None and plan.deployment_head_sha:
                    change_set.deployment_head_sha = plan.deployment_head_sha
            if outcome == "STAGING_VERIFIED":
                change_set.state = ChangeSetState.RUNTIME_VERIFIED.value
            elif merge_sha is not None and change_set.state in {
                ChangeSetState.SOURCE_PR.value,
                ChangeSetState.CI_VALIDATED.value,
                # NEW-102: a late repair write may have reset the state after prepare;
                # the merge is what happened, so the ChangeSet is SOURCE_MERGED.
                ChangeSetState.PLANNED.value,
                ChangeSetState.IMPLEMENTING.value,
                ChangeSetState.REQUEST_CHANGES.value,
            }:
                change_set.state = ChangeSetState.SOURCE_MERGED.value
            change_set.version += 1
            if workflow_id:
                run = session.scalar(select(db.Run).where(db.Run.workflow_id == workflow_id))
                if run is not None and run.run_type == "staging-gitops":
                    run.status = "SUCCEEDED" if outcome == "STAGING_VERIFIED" else outcome
                    run.error_code = None if outcome == "STAGING_VERIFIED" else str(blocker)
                    run.finished_at = datetime.now(UTC)
            state = change_set.state
        return {"status": "PERSISTED", "outcome": outcome, "change_set_state": state}

    # -- kubectl -------------------------------------------------------------------

    def _kubeconfig(self) -> Path:
        path = self.settings.staging_kubeconfig_path
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise StagingFlowBlocked("STAGING_KUBECONFIG_UNAVAILABLE")
        if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise StagingFlowBlocked("STAGING_KUBECONFIG_PERMISSIONS_TOO_BROAD")
        return path

    def _work_root(self) -> Path:
        # NEW-81: engineering-temporal-worker (engineering-control) may write only its
        # state root and the evidence root; run_root/gitops raised a read-only OSError
        # on the first production environment check (musematic #1125).
        root = (self.settings.state_root / "gitops").resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root

    def _kubectl(self, arguments: list[str], *, check: bool = True) -> CommandResult:
        return self.runner.run(
            "kubectl",
            ["--kubeconfig", str(self._kubeconfig()), *arguments],
            cwd=self._work_root(),
            timeout_seconds=120,
            check=check,
        )

    def _kubectl_json(self, arguments: list[str]) -> dict[str, Any]:
        raw = self._kubectl([*arguments, "--output", "json"]).stdout
        if len(raw.encode()) > 32_000_000:
            raise ValueError("kubectl response exceeded its bounded size")
        return _object(json.loads(raw), "kubectl response")

    def _application(self, flow: StagingFlowPolicy) -> dict[str, Any]:
        return self._kubectl_json(
            [
                "--namespace",
                flow.argo_namespace,
                "get",
                "applications.argoproj.io",
                flow.argo_application,
            ]
        )

    def _observe(self, flow: StagingFlowPolicy) -> dict[str, Any]:
        pods: list[dict[str, str]] = []
        workloads: list[dict[str, Any]] = []
        store_refs: set[tuple[str, str, str]] = set()
        eso_resources_observed = True
        for namespace in flow.namespaces:
            for pod in _items(
                self._kubectl_json(["--namespace", namespace, "get", "pods"]), "pods"
            ):
                reason = pod_failure_reason(pod)
                if reason is not None:
                    pods.append({"namespace": namespace, "name": _name(pod), "reason": reason})
            for kind in ("deployments", "statefulsets"):
                for item in _items(
                    self._kubectl_json(["--namespace", namespace, "get", kind]), kind
                ):
                    desired = _desired(item)
                    ready = _status_count(item, "readyReplicas")
                    if ready < desired:
                        workloads.append(
                            {
                                "namespace": namespace,
                                "kind": kind,
                                "name": _name(item),
                                "ready": ready,
                                "desired": desired,
                            }
                        )
            listed = self._kubectl(
                [
                    "--namespace",
                    namespace,
                    "get",
                    "externalsecrets.external-secrets.io",
                    "--output",
                    "json",
                ],
                check=False,
            )
            if listed.return_code:
                eso_resources_observed = False
                continue
            for item in _items(json.loads(listed.stdout), "externalsecrets"):
                reference = _nested(item, "spec", "secretStoreRef")
                if isinstance(reference, dict) and isinstance(reference.get("name"), str):
                    kind = str(reference.get("kind") or "SecretStore")
                    store_refs.add((kind, str(reference["name"]), namespace))
        stores: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for kind, name, namespace in sorted(store_refs):
            cluster = kind == "ClusterSecretStore"
            key = (kind, name if cluster else f"{namespace}/{name}")
            if key in seen or not _KUBERNETES_NAME.fullmatch(name):
                continue
            seen.add(key)
            arguments = (
                ["get", "clustersecretstores.external-secrets.io", name]
                if cluster
                else ["--namespace", namespace, "get", "secretstores.external-secrets.io", name]
            )
            result = self._kubectl([*arguments, "--output", "json"], check=False)
            reason = "NotFound"
            ready = False
            if not result.return_code:
                conditions = _nested(json.loads(result.stdout), "status", "conditions")
                for condition in conditions if isinstance(conditions, list) else []:
                    if isinstance(condition, dict) and condition.get("type") == "Ready":
                        ready = condition.get("status") == "True"
                        reason = str(condition.get("reason") or "Unknown")[:80]
            if not ready:
                stores.append(
                    {
                        "kind": kind,
                        "name": name,
                        "namespace": "" if cluster else namespace,
                        "reason": reason,
                    }
                )
        application = self._application(flow)
        health = str(_nested(application, "status", "health", "status") or "Unknown")
        sync = str(_nested(application, "status", "sync", "status") or "Unknown")
        healthy = not pods and not workloads and health == "Healthy"
        return {
            "healthy": healthy,
            "namespaces": list(flow.namespaces),
            "unhealthy_pods": pods,
            "unready_workloads": workloads,
            "stores_not_ready": stores,
            "eso_resources_observed": eso_resources_observed,
            "argo_application": flow.argo_application,
            "argo_health": health[:40],
            "argo_sync": sync[:40],
        }

    def _environment_check(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = StagingGitOpsPlan.model_validate(payload.get("plan"))
        _, flow = enabled_staging_flow(self.settings.project_config_root, plan.project)
        diagnosis = self._observe(flow)
        return {
            "status": "HEALTHY" if diagnosis["healthy"] else "UNHEALTHY",
            "diagnosis": diagnosis,
        }

    def _refresh_and_sync(self, flow: StagingFlowPolicy, application: dict[str, Any]) -> bool:
        """Hard-refresh the Application and request a sync unless one is in flight."""
        self._kubectl(
            [
                "--namespace",
                flow.argo_namespace,
                "annotate",
                "applications.argoproj.io",
                flow.argo_application,
                "argocd.argoproj.io/refresh=hard",
                "--overwrite",
            ]
        )
        running = application.get("operation") is not None or (
            _nested(application, "status", "operationState", "phase") in {"Running", "Terminating"}
        )
        if running:
            return False
        operation = {
            "operation": {
                "initiatedBy": {"username": ARGO_SYNC_USERNAME},
                "sync": {"prune": False},
            }
        }
        self._kubectl(
            [
                "--namespace",
                flow.argo_namespace,
                "patch",
                "applications.argoproj.io",
                flow.argo_application,
                "--type",
                "merge",
                "--patch",
                json.dumps(operation, separators=(",", ":")),
            ]
        )
        return True

    def _remediate(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = StagingGitOpsPlan.model_validate(payload.get("plan"))
        _, flow = enabled_staging_flow(self.settings.project_config_root, plan.project)
        # Never trust the payload's diagnosis for mutations: observe again.
        diagnosis = self._observe(flow)
        actions: list[str] = []
        synced = self._refresh_and_sync(flow, self._application(flow))
        actions.append("argo-hard-refresh")
        if synced:
            actions.append("argo-sync")
        deleted: list[str] = []
        for pod in diagnosis["unhealthy_pods"][:MAXIMUM_POD_DELETIONS]:
            namespace, name = pod["namespace"], pod["name"]
            if namespace not in flow.namespaces or not _KUBERNETES_NAME.fullmatch(name):
                continue
            self._kubectl(
                [
                    "--namespace",
                    namespace,
                    "delete",
                    "pod",
                    name,
                    "--wait=false",
                    "--ignore-not-found",
                ]
            )
            deleted.append(f"{namespace}/{name}")
        if deleted:
            actions.append("delete-failed-pods")
        if diagnosis["stores_not_ready"]:
            self._kubectl(
                [
                    "--namespace",
                    flow.external_secrets_namespace,
                    "rollout",
                    "restart",
                    f"deployment/{flow.external_secrets_deployment}",
                ]
            )
            actions.append("restart-external-secrets")
        return {
            "status": "REMEDIATION_APPLIED",
            "actions": actions,
            "deleted_pods": deleted,
            "diagnosis": diagnosis,
        }

    def _pin_chart_source(
        self, flow: StagingFlowPolicy, application: dict[str, Any], merge_sha: str
    ) -> dict[str, Any]:
        if flow.chart_source_repository is None:
            raise StagingFlowBlocked("ARGO_CHART_SOURCE_NOT_CONFIGURED")
        expected = _normalize_repo_url(flow.chart_source_repository)
        sources = _nested(application, "spec", "sources")
        if not isinstance(sources, list):
            raise StagingFlowBlocked("ARGO_CHART_SOURCE_NOT_MULTI_SOURCE")
        matches = [
            index
            for index, source in enumerate(sources)
            if isinstance(source, dict)
            and _normalize_repo_url(str(source.get("repoURL") or "")) == expected
        ]
        if len(matches) != 1:
            raise StagingFlowBlocked("ARGO_CHART_SOURCE_AMBIGUOUS")
        index = matches[0]
        source = sources[index]
        previous = source.get("targetRevision")
        result = {
            "source_index": index,
            "repo_url": str(source.get("repoURL")),
            "previous_target_revision": previous,
            "target_revision": merge_sha,
            "changed": previous != merge_sha,
        }
        if previous == merge_sha:
            return result
        patch = [
            {"op": "test", "path": f"/spec/sources/{index}/repoURL", "value": source["repoURL"]},
            {
                "op": "replace" if "targetRevision" in source else "add",
                "path": f"/spec/sources/{index}/targetRevision",
                "value": merge_sha,
            },
        ]
        self._kubectl(
            [
                "--namespace",
                flow.argo_namespace,
                "patch",
                "applications.argoproj.io",
                flow.argo_application,
                "--type",
                "json",
                "--patch",
                json.dumps(patch, separators=(",", ":")),
            ]
        )
        return result

    def _trigger_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = StagingGitOpsPlan.model_validate(payload.get("plan"))
        _, flow = enabled_staging_flow(self.settings.project_config_root, plan.project)
        merge_sha = str(payload.get("merge_sha", ""))
        if not _SHA.fullmatch(merge_sha):
            raise StagingFlowBlocked("MERGE_SHA_INVALID")
        application = self._application(flow)
        previous_started = str(_nested(application, "status", "operationState", "startedAt") or "")
        chart_pin = (
            self._pin_chart_source(flow, application, merge_sha)
            if flow.pin_chart_to_merge
            else None
        )
        if not self._refresh_and_sync(flow, application):
            return {"status": "SYNC_BUSY", "chart_pin": chart_pin}
        return {
            "status": "SYNC_TRIGGERED",
            "previous_operation_started_at": previous_started,
            "chart_pin": chart_pin,
        }

    def _sync_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = StagingGitOpsPlan.model_validate(payload.get("plan"))
        _, flow = enabled_staging_flow(self.settings.project_config_root, plan.project)
        previous = str(payload.get("previous_operation_started_at") or "")
        application = self._application(flow)
        operation_state = _nested(application, "status", "operationState")
        operation_state = operation_state if isinstance(operation_state, dict) else {}
        phase = str(operation_state.get("phase") or "")
        started = str(operation_state.get("startedAt") or "")
        initiated = _nested(operation_state, "operation", "initiatedBy", "username")
        sync = str(_nested(application, "status", "sync", "status") or "Unknown")
        health = str(_nested(application, "status", "health", "status") or "Unknown")
        observed = {"phase": phase[:40], "sync": sync[:40], "health": health[:40]}
        if (
            application.get("operation") is not None
            or not started
            or started == previous
            or initiated != ARGO_SYNC_USERNAME
        ):
            return {"status": "SYNC_PENDING", **observed}
        if phase in {"Failed", "Error"}:
            message = str(operation_state.get("message") or "")[:300]
            return {
                "status": "SYNC_FAILED",
                "blocker": "ARGO_SYNC_FAILED",
                "message": message,
                **observed,
            }
        if phase == "Succeeded" and sync == "Synced" and health == "Healthy":
            revisions = _nested(application, "status", "sync", "revisions")
            revision = (
                ",".join(str(value) for value in revisions)
                if isinstance(revisions, list) and revisions
                else str(_nested(application, "status", "sync", "revision") or "")
            )
            return {"status": "SYNCED", "argo_revision": revision[:400], **observed}
        return {"status": "SYNC_PENDING", **observed}

    def _verify_workloads(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = StagingGitOpsPlan.model_validate(payload.get("plan"))
        _, flow = enabled_staging_flow(self.settings.project_config_root, plan.project)
        expected = expected_image_tag(flow, str(payload.get("merge_sha", "")))
        raw_pinned = payload.get("pinned_repositories")
        pinned = (
            {str(value) for value in raw_pinned if str(value) in flow.image_repositories}
            if isinstance(raw_pinned, list)
            else set()
        )
        if not pinned:
            raise StagingFlowBlocked("STAGING_VERIFICATION_FAILED")
        checked: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for namespace in flow.namespaces:
            for kind in ("deployments", "statefulsets"):
                for item in _items(
                    self._kubectl_json(["--namespace", namespace, "get", kind]), kind
                ):
                    containers = _nested(item, "spec", "template", "spec", "containers")
                    images = [
                        split_image(str(container.get("image") or ""))
                        for container in (containers if isinstance(containers, list) else [])
                        if isinstance(container, dict)
                    ]
                    matched = [(name, tag) for name, tag in images if name in pinned]
                    if not matched:
                        continue
                    desired = _desired(item)
                    generation = _nested(item, "metadata", "generation")
                    observed_generation = _nested(item, "status", "observedGeneration")
                    ready = (
                        _status_count(item, "readyReplicas") == desired
                        and _status_count(item, "updatedReplicas") == desired
                        and (
                            not isinstance(generation, int)
                            or (
                                isinstance(observed_generation, int)
                                and observed_generation >= generation
                            )
                        )
                    )
                    tags_ok = all(tag == expected for _, tag in matched)
                    record = {
                        "namespace": namespace,
                        "kind": kind,
                        "name": _name(item),
                        "images": sorted({name for name, _ in matched}),
                        "tags": sorted({str(tag) for _, tag in matched}),
                        "ready": ready,
                    }
                    checked.append(record)
                    if not (ready and tags_ok):
                        failures.append(record)
        if checked and not failures:
            return {"status": "VERIFIED", "image_tag": expected, "workloads": checked}
        return {
            "status": "VERIFICATION_PENDING",
            "blocker": "STAGING_VERIFICATION_FAILED",
            "image_tag": expected,
            "workloads": checked,
            "failures": failures,
        }


class GitOpsGitHubHandler:
    """GitHub-worker activities for the staging GitOps flow (gh broker only)."""

    supported_steps = GITOPS_GITHUB_STEPS
    write_capable_steps = GITOPS_WRITE_CAPABLE_GITHUB_STEPS
    _failure_blockers = {
        "gitops.pr_checks": "SOURCE_PR_CHECKS_UNAVAILABLE",
        "gitops.pr_heads": "PR_HEADS_UNAVAILABLE",
        "gitops.deployment_pr_checks": "DEPLOYMENT_PR_CHECKS_UNAVAILABLE",
        "gitops.merge": "PROTECTED_MERGE_FAILED",
        "gitops.release_tag": "RELEASE_TAG_FAILED",
        "gitops.workflow_run": "WORKFLOW_RUN_UNAVAILABLE",
        "gitops.image_pin": "IMAGE_PIN_UNAVAILABLE",
        "gitops.environment_issue": "ENVIRONMENT_ISSUE_FAILED",
        "gitops.close_issue": "DELIVERED_ISSUE_CLOSE_FAILED",
    }

    def __init__(
        self,
        settings: Settings,
        runner: SafeCommandRunner | None = None,
        broker: GhCliBroker | None = None,
    ) -> None:
        self.settings = settings
        self.redactor = SecretRedactor()
        # NEW-65: gh needs an existing working directory; the controller steps own
        # run_root/gitops (engineering-control, 0700), so the GitHub worker uses its own.
        self.root = (settings.run_root / "gitops-github").resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.broker = broker or GhCliBroker(
            runner or SafeCommandRunner({"gh": settings.github_executable}),
            self.root,
            writes_enabled=settings.github_writes_enabled,
            authenticated_home=settings.github_authenticated_home,
        )
        self.write_policy = GitHubWritePolicyEvaluator(
            policy_path=settings.github_issue_write_validation_file,
            project_config_root=settings.project_config_root,
            production=settings.environment == "production",
            writes_enabled=settings.github_writes_enabled and self.broker.writes_enabled,
        )

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "gitops.pr_checks": self._pr_checks,
            "gitops.pr_heads": self._pr_heads,
            "gitops.deployment_pr_checks": self._deployment_pr_checks,
            "gitops.merge": self._merge,
            "gitops.release_tag": self._release_tag,
            "gitops.workflow_run": self._workflow_run,
            "gitops.image_pin": self._image_pin,
            "gitops.environment_issue": self._environment_issue,
            "gitops.close_issue": self._close_issue,
        }
        operation = handlers.get(step)
        if operation is None:
            raise ApplicationError("unsupported staging GitOps GitHub activity", non_retryable=True)
        if step in self.write_capable_steps:
            try:
                targets = self._write_repositories(step, payload)
            except (ValueError, ValidationError):
                return {"status": "BLOCKED", "blocker": "STAGING_GITOPS_PLAN_INVALID"}
            denial = self.write_policy.evaluate(lambda: targets)
            if denial is not None:
                return denial.model_dump(mode="json")
        try:
            result = await asyncio.to_thread(operation, payload)
        except StagingFlowBlocked as error:
            return {"status": "BLOCKED", "blocker": error.blocker}
        except GhCliAuthError:
            return {"status": "BLOCKED", "blocker": "BLOCKED_BY_GH_AUTH"}
        except (CommandFailure, OSError, ValueError, ValidationError) as error:
            return {
                "status": "BLOCKED",
                "blocker": self._failure_blockers[step],
                "error_type": type(error).__name__,
            }
        safe = self.redactor.redact(result).value
        if not isinstance(safe, dict):
            raise TypeError("redacted staging GitOps result must remain an object")
        return safe

    @staticmethod
    def _write_repositories(step: str, payload: dict[str, Any]) -> tuple[str, ...]:
        plan = StagingGitOpsPlan.model_validate(payload.get("plan"))
        if step == "gitops.merge":
            target = payload.get("target")
            if target == "source":
                return (plan.source_repository,)
            if target == "deployment":
                return (plan.deployment_repository,)
            raise ValueError("merge target is invalid")
        if step in {"gitops.release_tag", "gitops.environment_issue"}:
            return (plan.source_repository,)
        if step == "gitops.close_issue":
            return (plan.issue_repository,)
        raise ValueError("unknown staging GitOps write activity")

    def _context(self, payload: dict[str, Any]) -> tuple[StagingGitOpsPlan, StagingFlowPolicy]:
        plan = StagingGitOpsPlan.model_validate(payload.get("plan"))
        _, flow = enabled_staging_flow(self.settings.project_config_root, plan.project)
        return plan, flow

    @staticmethod
    def _merge_sha(payload: dict[str, Any]) -> str:
        merge_sha = str(payload.get("merge_sha", ""))
        if not _SHA.fullmatch(merge_sha):
            raise StagingFlowBlocked("MERGE_SHA_INVALID")
        return merge_sha

    def _body_file(self, body: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, raw = tempfile.mkstemp(prefix="staging-gitops-", suffix=".md", dir=self.root)
        path = Path(raw)
        os.chmod(path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(str(self.redactor.redact_text(body).value))
        return path

    # -- 1. PR checks ------------------------------------------------------------------

    def _pr_heads(self, payload: dict[str, Any]) -> dict[str, Any]:
        """NEW-102: live head and state of each named pull request (read-only)."""
        raw = payload.get("pull_requests")
        if not isinstance(raw, list) or not 1 <= len(raw) <= 2:
            raise ValueError("pull request head request is invalid")
        heads: list[dict[str, Any]] = []
        for item in raw:
            url = str(item.get("url") if isinstance(item, dict) else "")
            match = _PR_URL.fullmatch(url)
            if match is None:
                raise ValueError("pull request URL is outside the allowlist")
            pull_request = self.broker.view_pull_request(match.group(1), int(match.group(2)))
            heads.append(
                {
                    "url": url,
                    "head_sha": str(pull_request.get("headRefOid") or ""),
                    "state": str(pull_request.get("state") or ""),
                }
            )
        return {"status": "OK", "heads": heads}

    def _deployment_pr_checks(self, payload: dict[str, Any]) -> dict[str, Any]:
        """NEW-103: the deployment PR's CI must pass before either PR merges.

        The deployment merge used to wait only ~10 minutes for a CLEAN merge state, so a
        slower deployment CI (falcone-charts) or a real failure left the source merged and
        the deployment PR unmerged (falcone #1001). There is no CI baseline for deployment
        repositories, so every failure is a regression for the bounded CI repair.
        """
        excerpt_deadline = time.monotonic() + _EXCERPT_DEADLINE_SECONDS
        plan, flow = self._context(payload)
        if plan.deployment_pr_number is None or plan.deployment_head_sha is None:
            return {"status": "CHECKS_PASSED", "deployment_pr": False}
        if flow.deployment_ci_workflow is None:
            # Without the workflow's identity an unrelated green check could pass the gate.
            return {"status": "BLOCKED", "blocker": "DEPLOYMENT_CI_WORKFLOW_NOT_CONFIGURED"}
        repository = plan.deployment_repository
        pull_request = self.broker.view_pull_request(repository, plan.deployment_pr_number)
        if (
            pull_request.get("headRefOid") != plan.deployment_head_sha
            or pull_request.get("baseRefName") != plan.deployment_base_branch
        ):
            return {"status": "BLOCKED", "blocker": "DEPLOYMENT_PR_BINDING_CHANGED"}
        if pull_request.get("state") == "MERGED":
            return {"status": "CHECKS_PASSED", "already_merged": True}
        if pull_request.get("state") != "OPEN":
            return {"status": "BLOCKED", "blocker": "DEPLOYMENT_PR_NOT_OPEN"}
        states = _check_states(pull_request)
        failed = sorted(name for name, value in states.items() if value == "FAILED")
        if not failed:
            # A failure is reported at once (even while other checks run) so the bounded
            # repair can start; otherwise wait for every check and the configured workflow.
            if not states or any(value == "PENDING" for value in states.values()):
                return {"status": "CHECKS_PENDING", "reason": "DEPLOYMENT_PR_CHECKS_PENDING"}
            if not _workflow_succeeded(pull_request, flow.deployment_ci_workflow):
                # The configured CI never ran (or only skipped jobs): not a pass.
                return {"status": "CHECKS_PENDING", "reason": "REQUIRED_DEPLOYMENT_CI_MISSING"}
            return {"status": "CHECKS_PASSED", "check_count": len(states)}
        return {
            "status": "BLOCKED",
            "blocker": "DEPLOYMENT_PR_CI_REGRESSION",
            "failed_checks": failed[:20],
            "preexisting_checks": [],
            "reason": "DEPLOYMENT_CI_NOT_BASELINED",
            "failure_excerpts": self._failure_excerpts(
                repository, pull_request, failed, excerpt_deadline
            ),
        }

    def _pr_checks(self, payload: dict[str, Any]) -> dict[str, Any]:
        # NEW-100: the activity heartbeats only on entry, so the optional CI log reads must
        # end within a fixed time of it, whatever the GitHub calls before them cost.
        excerpt_deadline = time.monotonic() + _EXCERPT_DEADLINE_SECONDS
        plan, flow = self._context(payload)
        pull_request = self.broker.view_pull_request(plan.source_repository, plan.source_pr_number)
        if (
            pull_request.get("headRefOid") != plan.source_head_sha
            or pull_request.get("baseRefName") != plan.source_base_branch
        ):
            return {"status": "BLOCKED", "blocker": "SOURCE_PR_BINDING_CHANGED"}
        if pull_request.get("state") == "MERGED":
            return {"status": "CHECKS_PASSED", "already_merged": True}
        if pull_request.get("state") != "OPEN":
            return {"status": "BLOCKED", "blocker": "SOURCE_PR_NOT_OPEN"}
        result = evaluate_pull_request_checks(pull_request, flow.source_ci_workflow)
        if result.get("blocker") == "SOURCE_PR_CI_FAILED" and flow.ci_baseline.enabled:
            return self._baseline_gate(
                plan, flow, pull_request, create=True, excerpt_deadline=excerpt_deadline
            )
        return result

    # -- NEW-63: baseline-aware CI gate -----------------------------------------------------

    def _baseline_gate(
        self,
        plan: StagingGitOpsPlan,
        flow: StagingFlowPolicy,
        pull_request: dict[str, Any],
        *,
        create: bool,
        excerpt_deadline: float | None,
    ) -> dict[str, Any]:
        """Block only on PR CI jobs that do not also fail on unchanged main.

        The baseline is a draft PR (hermes/ci-baseline/<main12>-<markers8>) from main plus
        marker files that trigger the same path-filtered jobs. It is created once per main
        commit and marker set, read once complete, then closed and its branch deleted.
        """
        states = _check_states(pull_request)
        if not states or any(value == "PENDING" for value in states.values()):
            return {"status": "CHECKS_PENDING", "reason": "SOURCE_PR_CHECKS_PENDING"}
        failed = sorted(name for name, value in states.items() if value == "FAILED")
        repository = plan.source_repository
        # NEW-67: the baseline always runs on current main; PR results from before main
        # moved would turn fixes that landed on main into false regressions (#993's
        # govulncheck). Re-run the PR's CI on the current base first.
        main_date = self.broker.commit_date(repository, plan.source_base_branch)
        if _latest_check_start(pull_request) < main_date:
            if not create:
                return {"status": "BLOCKED", "blocker": "SOURCE_PR_CI_STALE"}
            if self.broker.last_reopened_at(repository, plan.source_pr_number) >= main_date:
                return {"status": "CHECKS_PENDING", "reason": "SOURCE_PR_CI_RERUN_PENDING"}
            self._require_baseline_write(repository)
            self.broker.rerun_pull_request_ci(
                repository=repository,
                number=plan.source_pr_number,
                expected_head_sha=plan.source_head_sha,
                change_binding=f"changeset:{plan.change_set_id}",
            )
            return {"status": "CHECKS_PENDING", "reason": "SOURCE_PR_CI_RERUN_STARTED"}
        try:
            changed = self.broker.pull_request_files(repository, plan.source_pr_number)
        except GhCliPolicyError:
            # A listing at GitHub's file cap may hide .github/ changes: no exemption.
            return _unexempted_regressions(
                failed,
                "CI_BASELINE_FILE_LISTING_TRUNCATED",
                self._failure_excerpts(repository, pull_request, failed, excerpt_deadline),
            )
        if any(path.startswith(".github/") for path in changed):
            # falcone CI-baseline review: a PR that edits CI configuration could drop the
            # jobs exposing its own regression while a pre-existing failure is excused,
            # so it never gets the baseline exemption.
            return _unexempted_regressions(
                failed,
                "CI_BASELINE_CI_CONFIG_CHANGED",
                self._failure_excerpts(repository, pull_request, failed, excerpt_deadline),
            )
        markers = sorted(
            {
                marker.path
                for marker in flow.ci_baseline.markers
                if any(path.startswith(marker.scope) for path in changed)
            }
        )
        if not markers:
            return {
                "status": "BLOCKED",
                "blocker": "SOURCE_PR_CI_FAILED",
                "failed_checks": failed[:20],
                "reason": "CI_BASELINE_NOT_APPLICABLE",
            }
        main = self.broker.branch_commit(repository, plan.source_base_branch)["sha"]
        digest = hashlib.sha256("\n".join(markers).encode()).hexdigest()[:8]
        branch = f"hermes/ci-baseline/{main[:12]}-{digest}"
        binding = f"changeset:{plan.change_set_id}"
        existing = self.broker.list_pull_requests(repository, head=branch, state="all")
        if not existing:
            if not create:
                return {"status": "BLOCKED", "blocker": "CI_BASELINE_MISSING"}
            self._require_baseline_write(repository)
            self._open_baseline(plan, branch, main, markers, binding)
            return {"status": "CHECKS_PENDING", "reason": "CI_BASELINE_STARTED"}
        baseline = self.broker.view_pull_request(repository, int(existing[0]["number"]))
        baseline_states = _check_states(baseline)
        if not baseline_states or any(value == "PENDING" for value in baseline_states.values()):
            if baseline.get("state") != "OPEN":
                # Closed before its checks finished: a stale record, never a verdict.
                return {"status": "BLOCKED", "blocker": "CI_BASELINE_INCOMPLETE"}
            return {"status": "CHECKS_PENDING", "reason": "CI_BASELINE_PENDING"}
        if baseline.get("state") == "OPEN" and create:
            self._require_baseline_write(repository)
            self.broker.close_baseline_pull_request(
                repository=repository, number=int(baseline["number"]), change_binding=binding
            )
        regressions = [name for name in failed if baseline_states.get(name) != "FAILED"]
        preexisting = [name for name in failed if name not in regressions]
        baseline_url = str(baseline.get("url") or "")
        if regressions:
            return {
                "status": "BLOCKED",
                "blocker": "SOURCE_PR_CI_REGRESSION",
                "failed_checks": regressions[:20],
                "preexisting_checks": preexisting[:40],
                "baseline_pr": baseline_url,
                "failure_excerpts": self._failure_excerpts(
                    repository, pull_request, regressions, excerpt_deadline
                ),
            }
        return {
            "status": "CHECKS_PASSED",
            "check_count": len(states),
            "ci_baseline": {"pr": baseline_url, "preexisting_failures": preexisting[:40]},
        }

    def _failure_excerpts(
        self,
        repository: str,
        pull_request: dict[str, Any],
        names: list[str],
        deadline: float | None,
    ) -> dict[str, str]:
        """NEW-100: bounded failing-log lines per regressed job, for the CI-repair maker.

        Best effort: a job whose log cannot be read is simply left out.
        """
        if deadline is None:
            return {}
        jobs = _failed_job_ids(pull_request)
        excerpts: dict[str, str] = {}
        attempts = 0
        for name in names:
            remaining = int(deadline - time.monotonic())
            if (
                len(excerpts) >= _MAX_EXCERPT_JOBS
                or attempts >= _MAX_EXCERPT_ATTEMPTS
                or remaining < 1
            ):
                break
            job_id = jobs.get(name)
            if job_id is None:
                continue
            attempts += 1
            try:
                excerpt = self.broker.job_log_excerpt(
                    repository, job_id, timeout_seconds=min(15, remaining)
                )
            except (CommandFailure, GhCliPolicyError, OSError, subprocess.TimeoutExpired):
                continue
            if excerpt:
                excerpts[name] = excerpt
        return excerpts

    def _require_baseline_write(self, repository: str) -> None:
        """gitops.pr_checks is a read step; its baseline writes pass the write policy."""
        if self.write_policy.evaluate(lambda: (repository,)) is not None:
            raise StagingFlowBlocked("CI_BASELINE_WRITE_DENIED")

    def _open_baseline(
        self,
        plan: StagingGitOpsPlan,
        branch: str,
        main: str,
        markers: list[str],
        binding: str,
    ) -> None:
        repository = plan.source_repository
        try:
            current = self.broker.branch_commit(repository, branch)["sha"]
        except (CommandFailure, GhCliPolicyError):
            current = None
        if current is not None:
            # A previous attempt stopped between the branch and the PR: start clean.
            self.broker.delete_baseline_branch(
                repository=repository, branch=branch, change_binding=binding
            )
        self.broker.create_baseline_branch(
            repository=repository, branch=branch, sha=main, change_binding=binding
        )
        for path in markers:
            self.broker.put_baseline_marker(
                repository=repository, branch=branch, path=path, change_binding=binding
            )
        body = (
            "Hermes CI baseline: unchanged `main` at "
            f"`{main}` plus marker files, so the same path-filtered CI jobs run on main's "
            f"code as on {plan.source_pr_url}. Jobs failing here too are pre-existing and "
            "do not block that PR. **Never merge**; Hermes closes this PR and deletes its "
            "branch once the checks finish.\n"
        )
        self.broker.create_pull_request(
            repository=repository,
            head=branch,
            base=plan.source_base_branch,
            title=f"Hermes CI baseline for main@{main[:12]} - do not merge",
            body_file=self._body_file(body),
            change_binding=binding,
            draft=True,
        )

    # -- 2. ready + merge --------------------------------------------------------------

    def _merge(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan, flow = self._context(payload)
        if plan.risk_class == "R3" and not flow.allow_r3_merge:
            return {"status": "BLOCKED", "blocker": "R3_MERGE_REQUIRES_APPROVAL"}
        target = str(payload.get("target"))
        if target == "source":
            repository, number, head, base = (
                plan.source_repository,
                plan.source_pr_number,
                plan.source_head_sha,
                plan.source_base_branch,
            )
        elif (
            target == "deployment"
            and plan.deployment_pr_number is not None
            and plan.deployment_head_sha is not None
        ):
            repository, number, head, base = (
                plan.deployment_repository,
                plan.deployment_pr_number,
                plan.deployment_head_sha,
                plan.deployment_base_branch,
            )
        else:
            return {"status": "BLOCKED", "blocker": "MERGE_TARGET_INVALID"}
        binding = f"changeset:{plan.change_set_id}"
        current = self.broker.mark_pull_request_ready(
            repository=repository,
            number=number,
            expected_head_sha=head,
            change_binding=binding,
        )
        allow_unstable = False
        if current.get("state") != "MERGED":
            merge_state = str(current.get("mergeStateStatus") or "UNKNOWN")
            if merge_state == "DIRTY":
                return {"status": "BLOCKED", "blocker": f"{target.upper()}_PR_MERGE_CONFLICT"}
            if merge_state == "UNSTABLE" and target == "source" and flow.ci_baseline.enabled:
                # NEW-63: non-required checks failed; merge only when the baseline gate
                # attributes every failure to unchanged main.
                # The merge gate uses only the verdict, so it reads no CI logs.
                verdict = self._baseline_gate(
                    plan, flow, current, create=False, excerpt_deadline=None
                )
                if verdict.get("status") != "CHECKS_PASSED":
                    return {
                        "status": "BLOCKED",
                        "blocker": str(verdict.get("blocker") or "SOURCE_PR_CI_FAILED"),
                    }
                allow_unstable = True
            elif merge_state != "CLEAN":
                return {"status": "MERGE_PENDING", "target": target, "merge_state": merge_state}
        result = self.broker.merge_pull_request(
            repository=repository,
            number=number,
            expected_head_sha=head,
            expected_base_branch=base,
            change_binding=binding,
            allow_unstable=allow_unstable,
        )
        return {
            "status": "MERGED",
            "target": target,
            "merge_sha": result["merge_sha"],
            "merge": result,
        }

    # -- 3. release tag + workflow runs ------------------------------------------------

    def _release_tag(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan, flow = self._context(payload)
        if not flow.release_tag.enabled:
            return {"status": "BLOCKED", "blocker": "RELEASE_TAG_DISABLED"}
        merge_sha = self._merge_sha(payload)
        prefix = flow.release_tag.prefix
        existing = self._tag_at(plan.source_repository, prefix, merge_sha)
        if existing is not None:
            return {"status": "TAG_READY", "release_tag": existing, "reused": True}
        refs = self.broker.list_tag_refs(plan.source_repository, prefix)
        tag = next_release_tag((ref["ref"].removeprefix("refs/tags/") for ref in refs), prefix)
        try:
            self.broker.create_tag_ref(
                repository=plan.source_repository,
                tag=tag,
                sha=merge_sha,
                change_binding=f"changeset:{plan.change_set_id}",
            )
        except CommandFailure:
            # A concurrent/lost create: reuse only a tag that now points at the merge.
            raced = self._tag_at(plan.source_repository, prefix, merge_sha)
            if raced is None:
                raise
            return {"status": "TAG_READY", "release_tag": raced, "reused": True}
        return {"status": "TAG_READY", "release_tag": tag, "reused": False}

    def _tag_at(self, repository: str, prefix: str, merge_sha: str) -> str | None:
        pattern = re.compile(rf"{re.escape(prefix)}([0-9]+)\.([0-9]+)\.([0-9]+)")
        matches: list[tuple[tuple[int, int, int], str]] = []
        for ref in self.broker.list_tag_refs(repository, prefix):
            name = ref["ref"].removeprefix("refs/tags/")
            match = pattern.fullmatch(name)
            if match and ref["sha"] == merge_sha and ref["type"] == "commit":
                matches.append(((int(match[1]), int(match[2]), int(match[3])), name))
        return max(matches)[1] if matches else None

    def _workflow_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan, flow = self._context(payload)
        merge_sha = self._merge_sha(payload)
        role = payload.get("workflow_role")
        if role == "main_ci":
            workflow_name, failure = flow.main_ci_workflow, "MAIN_CI_FAILED"
        elif role == "image":
            workflow_name, failure = flow.image_workflow, "IMAGE_BUILD_FAILED"
        else:
            return {"status": "BLOCKED", "blocker": "WORKFLOW_ROLE_INVALID"}
        if workflow_name is None:
            return {"status": "BLOCKED", "blocker": "WORKFLOW_NOT_CONFIGURED"}
        runs = self.broker.list_workflow_runs(
            plan.source_repository, workflow_name=workflow_name, commit_sha=merge_sha
        )
        exact = [run for run in runs if run.get("headSha") == merge_sha]
        summary = [
            {
                "run_id": run.get("databaseId"),
                "status": str(run.get("status") or "")[:40],
                "conclusion": str(run.get("conclusion") or "")[:40],
                "event": str(run.get("event") or "")[:40],
                "url": str(run.get("url") or "")[:300],
            }
            for run in exact
        ]
        successful = [
            item
            for item in summary
            if str(item["status"]).lower() == "completed"
            and str(item["conclusion"]).lower() == "success"
        ]
        if successful:
            return {"status": "RUN_SUCCEEDED", "workflow_role": role, "run": successful[0]}
        if not exact or any(str(item["status"]).lower() != "completed" for item in summary):
            return {"status": "RUN_PENDING", "workflow_role": role, "runs": summary}
        if role == "image" and flow.release_tag.enabled:
            later = self._later_release(plan, flow, workflow_name, merge_sha)
            if later is not None:
                return {**later, "workflow_role": role, "superseded_runs": summary}
        return {"status": "BLOCKED", "blocker": failure, "workflow_role": role, "runs": summary}

    def _later_release(
        self,
        plan: StagingGitOpsPlan,
        flow: StagingFlowPolicy,
        workflow_name: str,
        merge_sha: str,
    ) -> dict[str, Any] | None:
        """NEW-68: the merge's own release build failed (musematic v0.0.108: the release
        supply-chain gate blocked on advisories published after v0.0.107). The change is
        delivered by the first later release whose commit contains the merge and whose
        image build succeeded; a later build still running is waited for."""
        prefix = flow.release_tag.prefix
        pattern = re.compile(rf"{re.escape(prefix)}([0-9]+)\.([0-9]+)\.([0-9]+)")
        own = self._tag_at(plan.source_repository, prefix, merge_sha)
        own_match = pattern.fullmatch(own) if own else None
        floor = (
            (int(own_match[1]), int(own_match[2]), int(own_match[3])) if own_match else None
        )
        candidates: list[tuple[tuple[int, int, int], str, str]] = []
        for ref in self.broker.list_tag_refs(plan.source_repository, prefix):
            name = ref["ref"].removeprefix("refs/tags/")
            match = pattern.fullmatch(name)
            if not match or ref["type"] != "commit" or ref["sha"] == merge_sha:
                continue
            version = (int(match[1]), int(match[2]), int(match[3]))
            if floor is not None and version <= floor:
                continue
            candidates.append((version, name, ref["sha"]))
        for _, name, sha in sorted(candidates)[:10]:
            if not self.broker.commit_contains(
                plan.source_repository, ancestor=merge_sha, descendant=sha
            ):
                continue
            runs = [
                run
                for run in self.broker.list_workflow_runs(
                    plan.source_repository, workflow_name=workflow_name, commit_sha=sha
                )
                if run.get("headSha") == sha
            ]
            if any(str(run.get("status") or "").lower() != "completed" for run in runs):
                return {"status": "RUN_PENDING", "release_tag": name, "release_sha": sha}
            if any(str(run.get("conclusion") or "").lower() == "success" for run in runs):
                return {"status": "RUN_SUCCEEDED", "release_tag": name, "release_sha": sha}
        return None

    # -- 5. image pin ------------------------------------------------------------------

    def _image_pin(self, payload: dict[str, Any]) -> dict[str, Any]:
        _, flow = self._context(payload)
        expected = expected_image_tag(flow, self._merge_sha(payload))
        text = self.broker.repository_file_text(
            flow.deploy_repository, flow.deploy_values_file, flow.deploy_branch
        )
        observed = image_tags_in_values(text, flow.image_repositories)
        pinned = sorted(repository for repository, tags in observed.items() if expected in tags)
        # NEW-93: argocd-image-updater commits images as their builds appear; falcone #956
        # synced after 5 of 6 pins, leaving web-console (the changed image) on the old tag.
        missing = sorted(set(flow.image_repositories) - set(pinned))
        return {
            "status": "PIN_PENDING" if missing else "PINNED",
            "image_tag": expected,
            "pinned_repositories": pinned,
            "missing_repositories": missing,
            "observed_tags": {key: sorted(set(value)) for key, value in observed.items()},
        }

    # -- 4. environment issue ----------------------------------------------------------

    def _environment_issue(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan, flow = self._context(payload)
        action = payload.get("action")
        repository = plan.source_repository
        binding = f"changeset:{plan.change_set_id}"
        raw_number = payload.get("issue_number")
        issue_number = int(raw_number) if isinstance(raw_number, int) and raw_number > 0 else None
        if action == "resolve":
            if issue_number is None:
                return {"status": "BLOCKED", "blocker": "ENVIRONMENT_ISSUE_MISSING"}
            marker = (
                f"<!-- engineering-control-staging-gitops:environment:{plan.change_set_id}"
                ":resolved -->"
            )
            body = (
                "The staging environment is healthy again after bounded Hermes "
                f"remediation (application `{flow.argo_application}`). Closing.\n\n{marker}\n"
            )
            path = self._body_file(body)
            try:
                self.broker.comment_marked_issue(
                    repository=repository,
                    number=issue_number,
                    body_file=path,
                    marker=marker,
                    change_binding=binding,
                    require_open=False,
                )
            finally:
                path.unlink(missing_ok=True)
            closed = self.broker.close_marked_issue(
                repository=repository, number=issue_number, marker=marker, change_binding=binding
            )
            return {
                "status": "ISSUE_RESOLVED",
                "issue_number": issue_number,
                "issue_url": closed.get("issue_url"),
            }
        if action != "report":
            return {"status": "BLOCKED", "blocker": "ENVIRONMENT_ISSUE_ACTION_INVALID"}
        round_number = payload.get("round")
        if not isinstance(round_number, int) or not 1 <= round_number <= 10:
            return {"status": "BLOCKED", "blocker": "ENVIRONMENT_ISSUE_ROUND_INVALID"}
        diagnosis = payload.get("diagnosis")
        title = f"Hermes staging environment unhealthy for change {plan.change_set_id}"
        if issue_number is None:
            matches = self.broker.search_issues(repository, f'"{plan.change_set_id}" in:title')
            open_issues = sorted(
                int(item["number"])
                for item in matches
                if item.get("state") == "OPEN"
                and item.get("title") == title
                and isinstance(item.get("number"), int)
            )
            issue_number = open_issues[0] if open_issues else None
        marker = (
            f"<!-- engineering-control-staging-gitops:environment:{plan.change_set_id}"
            f":round-{round_number} -->"
        )
        body = render_environment_diagnosis(
            plan, flow, diagnosis if isinstance(diagnosis, dict) else {}, round_number
        )
        path = self._body_file(f"{body}\n{marker}\n")
        try:
            if issue_number is None:
                url = self.broker.create_issue(
                    repository=repository, title=title, body_file=path, change_binding=binding
                )
                match = re.fullmatch(
                    rf"https://github\.com/{re.escape(repository)}/issues/([1-9][0-9]*)", url
                )
                if match is None:
                    raise ValueError("created environment issue URL is invalid")
                return {
                    "status": "ISSUE_OPENED",
                    "issue_number": int(match.group(1)),
                    "issue_url": url,
                }
            comment = self.broker.comment_marked_issue(
                repository=repository,
                number=issue_number,
                body_file=path,
                marker=marker,
                change_binding=binding,
            )
        finally:
            path.unlink(missing_ok=True)
        return {
            "status": "ISSUE_COMMENTED",
            "issue_number": issue_number,
            "comment_url": comment.get("comment_url"),
        }

    # -- 8. close the delivered issue --------------------------------------------------

    def _close_issue(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan, flow = self._context(payload)
        merge_sha = self._merge_sha(payload)
        image_tag = expected_image_tag(flow, merge_sha)
        marker = f"<!-- engineering-control-staging-gitops:delivered:{plan.change_set_id} -->"
        pinned = payload.get("pinned_repositories")
        lines = [
            "Delivered to staging by Hermes (post-merge GitOps flow).",
            "",
            f"- Source PR: {plan.source_pr_url}",
        ]
        if plan.deployment_pr_url:
            lines.append(f"- Deployment PR: {plan.deployment_pr_url}")
        delivered = payload.get("delivered_merge_sha")
        if isinstance(delivered, str) and _SHA.fullmatch(delivered) and delivered != merge_sha:
            # NEW-68: shipped by a later release that contains the merge.
            lines.append(f"- Merge SHA: `{delivered}` (released in `{merge_sha}`)")
        else:
            lines.append(f"- Merge SHA: `{merge_sha}`")
        if isinstance(payload.get("release_tag"), str):
            lines.append(f"- Release tag: `{payload['release_tag']}`")
        lines.append(f"- Image tag: `{image_tag}`")
        if isinstance(pinned, list) and pinned:
            lines.append("- Pinned images: " + ", ".join(f"`{value}`" for value in pinned[:20]))
        lines.append(
            f"- Argo CD application: `{flow.argo_application}` at revision "
            f"`{str(payload.get('argo_revision') or 'unknown')[:400]}`"
        )
        lines.extend(["", marker, ""])
        binding = f"changeset:{plan.change_set_id}"
        path = self._body_file("\n".join(lines))
        try:
            comment = self.broker.comment_marked_issue(
                repository=plan.issue_repository,
                number=plan.issue_number,
                body_file=path,
                marker=marker,
                change_binding=binding,
                require_open=False,
            )
        finally:
            path.unlink(missing_ok=True)
        closed = self.broker.close_marked_issue(
            repository=plan.issue_repository,
            number=plan.issue_number,
            marker=marker,
            change_binding=binding,
        )
        return {
            "status": "ISSUE_CLOSED",
            "comment_url": comment.get("comment_url"),
            "issue_url": closed.get("issue_url"),
        }


_MAX_EXCERPT_JOBS = 5
_MAX_EXCERPT_ATTEMPTS = 10
# Seconds after the pr_checks step starts by which log reads end; its heartbeat timeout is
# 2 minutes and each read is capped at 15 s, so they finish within about 90 s of entry.
_EXCERPT_DEADLINE_SECONDS = 75
_JOB_URL = re.compile(r"https://github\.com/[^/]+/[^/]+/actions/runs/[0-9]+/job/([1-9][0-9]{0,19})")


def _excerpts(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(name)[:200]: str(text)[:4000]
        for name, text in list(value.items())[:_MAX_EXCERPT_JOBS]
        if isinstance(name, str) and isinstance(text, str) and text
    }


def _failed_job_ids(pull_request: dict[str, Any]) -> dict[str, int]:
    """NEW-100: failed check name -> Actions job ID, from the rollup's details URLs."""
    jobs: dict[str, int] = {}
    raw_checks = pull_request.get("statusCheckRollup")
    for raw in raw_checks if isinstance(raw_checks, list) else []:
        if not isinstance(raw, dict):
            continue
        conclusion = str(raw.get("conclusion") or "").upper()
        if conclusion in {"", "SUCCESS", "NEUTRAL", "SKIPPED"}:
            continue
        match = _JOB_URL.match(str(raw.get("detailsUrl") or ""))
        name = str(raw.get("name") or "")[:200]
        if match and name:
            jobs.setdefault(name, int(match.group(1)))
    return jobs


UNVERIFIED_CI_REGRESSION_REASONS = frozenset(
    {
        "CI_BASELINE_CI_CONFIG_CHANGED",
        "CI_BASELINE_FILE_LISTING_TRUNCATED",
        "DEPLOYMENT_CI_NOT_BASELINED",
    }
)


def _unexempted_regressions(
    failed: list[str], reason: str, excerpts: dict[str, str]
) -> dict[str, Any]:
    """NEW-99: when the baseline cannot excuse anything, every failure is a regression.

    Nothing is exempted, but the loop can still send the failures to the maker for CI
    repair instead of pausing on a PR whose own changes broke its checks (falcone #1001).
    """
    return {
        "status": "BLOCKED",
        "blocker": "SOURCE_PR_CI_REGRESSION",
        "failed_checks": failed[:20],
        "preexisting_checks": [],
        "reason": reason,
        "failure_excerpts": excerpts,
    }


def _workflow_succeeded(pull_request: dict[str, Any], workflow: str) -> bool:
    """NEW-103: at least one check of the named workflow concluded SUCCESS."""
    raw_checks = pull_request.get("statusCheckRollup")
    return any(
        isinstance(raw, dict)
        and str(raw.get("workflowName") or raw.get("workflow") or "") == workflow
        and str(raw.get("conclusion") or "").upper() == "SUCCESS"
        for raw in (raw_checks if isinstance(raw_checks, list) else [])
    )


def _check_states(pull_request: dict[str, Any]) -> dict[str, str]:
    """NEW-63: check name -> PENDING | PASSED | FAILED (the worst state wins per name)."""
    raw_checks = pull_request.get("statusCheckRollup")
    if not isinstance(raw_checks, list):
        return {}
    rank = {"PASSED": 0, "PENDING": 1, "FAILED": 2}
    states: dict[str, str] = {}
    for raw in raw_checks:
        if not isinstance(raw, dict):
            raise ValueError("GitHub PR check result is invalid")
        name = str(raw.get("name") or raw.get("context") or "")[:200]
        state = str(raw.get("status") or raw.get("state") or "").upper()
        conclusion = str(raw.get("conclusion") or raw.get("state") or "").upper()
        if state in {"QUEUED", "PENDING", "IN_PROGRESS", "REQUESTED", "WAITING"} or not conclusion:
            value = "PENDING"
        elif conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            value = "PASSED"
        else:
            value = "FAILED"
        if rank[value] >= rank.get(states.get(name, "PASSED"), 0):
            states[name] = value
    return states


def _latest_check_start(pull_request: dict[str, Any]) -> str:
    """NEW-67: ISO start time of the newest PR check ('' when none is reported)."""
    raw_checks = pull_request.get("statusCheckRollup")
    starts = [
        str(raw.get("startedAt") or raw.get("createdAt") or "")
        for raw in (raw_checks if isinstance(raw_checks, list) else [])
        if isinstance(raw, dict)
    ]
    return max((value for value in starts if value), default="")


def _check_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(name)[:200] for name in value if isinstance(name, str)][:40]


def evaluate_pull_request_checks(
    pull_request: dict[str, Any], required_workflow: str
) -> dict[str, Any]:
    raw_checks = pull_request.get("statusCheckRollup")
    if not isinstance(raw_checks, list) or not raw_checks:
        return {"status": "CHECKS_PENDING", "reason": "SOURCE_PR_CHECKS_NOT_REPORTED"}
    workflow_seen = False
    pending = False
    failed: list[str] = []
    for raw in raw_checks:
        if not isinstance(raw, dict):
            raise ValueError("GitHub PR check result is invalid")
        workflow_name = str(raw.get("workflowName") or raw.get("workflow") or "")
        name = str(raw.get("name") or raw.get("context") or "")[:200]
        state = str(raw.get("status") or raw.get("state") or "").upper()
        conclusion = str(raw.get("conclusion") or raw.get("state") or "").upper()
        if workflow_name == required_workflow:
            workflow_seen = True
        if state in {"QUEUED", "PENDING", "IN_PROGRESS", "REQUESTED", "WAITING"} or not conclusion:
            pending = True
        elif conclusion not in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            failed.append(name)
    if failed:
        return {"status": "BLOCKED", "blocker": "SOURCE_PR_CI_FAILED", "failed_checks": failed[:20]}
    if pending or not workflow_seen:
        return {
            "status": "CHECKS_PENDING",
            "reason": "SOURCE_PR_CHECKS_PENDING" if pending else "REQUIRED_CI_WORKFLOW_MISSING",
        }
    return {"status": "CHECKS_PASSED", "check_count": len(raw_checks)}


def render_environment_diagnosis(
    plan: StagingGitOpsPlan,
    flow: StagingFlowPolicy,
    diagnosis: dict[str, Any],
    round_number: int,
) -> str:
    """Render a secret-free diagnosis: names, reason codes and counts only."""

    def rows(key: str, fields: tuple[str, ...]) -> list[str]:
        values = diagnosis.get(key)
        if not isinstance(values, list):
            return []
        return [
            "- " + " ".join(f"{field}=`{str(item.get(field, ''))[:120]}`" for field in fields)
            for item in values[:40]
            if isinstance(item, dict)
        ]

    lines = [
        f"Hermes staging environment gate found the environment unhealthy "
        f"(remediation round {round_number}).",
        "",
        f"- Change set: `{plan.change_set_id}`",
        f"- Source PR: {plan.source_pr_url}",
        f"- Argo CD application: `{flow.argo_application}` "
        f"health=`{str(diagnosis.get('argo_health', 'Unknown'))[:40]}` "
        f"sync=`{str(diagnosis.get('argo_sync', 'Unknown'))[:40]}`",
        f"- Namespaces: {', '.join(f'`{value}`' for value in flow.namespaces)}",
        "",
        "### Unhealthy pods",
        *(rows("unhealthy_pods", ("namespace", "name", "reason")) or ["- none"]),
        "",
        "### Workloads below desired readiness",
        *(
            rows("unready_workloads", ("namespace", "kind", "name", "ready", "desired"))
            or ["- none"]
        ),
        "",
        "### Secret stores not Ready",
        *(rows("stores_not_ready", ("kind", "namespace", "name", "reason")) or ["- none"]),
        "",
        "Bounded remediation: Argo CD hard refresh + sync, deletion of failed pods so their "
        "controllers recreate them, and an external-secrets controller restart when a "
        "secret store is not Ready. No secret values are included in this report.",
    ]
    return "\n".join(lines)
