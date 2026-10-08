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
from collections.abc import Callable, Iterable
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
    pull_request_head_repository,
)
from engineering_control.config import Settings
from engineering_control.db import models as db
from engineering_control.delivery_notifications import (
    enqueue_milestone,
    explain_blocker,
    literal,
)
from engineering_control.engineering.models import ProjectManifest
from engineering_control.models.enums import ChangeSetState
from engineering_control.models.reconciliation import StagingFlowPolicy
from engineering_control.policy.github_write import (
    GITOPS_READ_ONLY_GITHUB_STEPS,
    GITOPS_WRITE_CAPABLE_GITHUB_STEPS,
    GitHubWritePolicyEvaluator,
)
from engineering_control.projects import (
    PROJECT_ALTERNATION,
    PROJECT_SET,
    REPOSITORY_SET,
    REPOSITORY_SLUG_ALTERNATION,
    ProjectName,
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
        "gitops.diagnose_sync",
        "gitops.remediate_sync",
        "gitops.notify_sync",
        "gitops.notify_merged",
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
# NEW-145b: the head branches a publication reconciliation may list PRs for.
_LISTING_REPOSITORIES = REPOSITORY_SET
_AGENT_BRANCH = re.compile(rf"agent/(?:{PROJECT_ALTERNATION})/[A-Za-z0-9._-]+/[a-f0-9-]{{36}}")
_PR_URL = re.compile(
    rf"https://github\.com/(gntik-ai/(?:{REPOSITORY_SLUG_ALTERNATION}))"
    r"/pull/([1-9][0-9]*)"
)
_REPOSITORY = rf"^gntik-ai/({REPOSITORY_SLUG_ALTERNATION})$"


class StagingFlowBlocked(ValueError):
    """A fail-closed stop with a stable operator-facing blocker code."""

    def __init__(self, blocker: str) -> None:
        super().__init__(blocker)
        self.blocker = blocker


class StagingGitOpsPlan(BaseModel):
    """Immutable ChangeSet binding produced by gitops.prepare from the database."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    change_set_id: UUID
    project: ProjectName
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
    if project not in PROJECT_SET:
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


# -- NEW-113: Argo CD sync failure diagnosis and bounded remediation ------------------

# NEW-116: SCOPE_INHERITED is a completed Hermes full-sync request that Argo CD ran with a
# previous operation's selective scope (resources, syncOptions, strategy or revision).
# NEW-118: PRUNE_REQUIRED is a resource Argo CD marks requiresPruning (a delivered change
# removed it; Hermes syncs with prune=false). Hermes never prunes: it escalates at once.
SYNC_FAILURE_CLASSES = (
    "PATCH_CONFLICT",
    "JOB_IMMUTABLE",
    "TRANSIENT",
    "SCOPE_INHERITED",
    "PRUNE_REQUIRED",
    "OTHER",
)
# NEW-118: kinds an operator may usually delete once verified unused. Informational only:
# the escalation issue offers the delete command for these, never runs it.
SAFE_PRUNE_KINDS = frozenset(
    {
        ("", "ConfigMap"),
        ("", "Service"),
        ("", "ServiceAccount"),
        ("rbac.authorization.k8s.io", "Role"),
        ("rbac.authorization.k8s.io", "RoleBinding"),
        ("networking.k8s.io", "NetworkPolicy"),
        ("batch", "Job"),
        ("apps", "Deployment"),
    }
)
_KIND = re.compile(r"[A-Z][A-Za-z0-9]{0,62}")
_GROUP = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
MAXIMUM_SYNC_REMEDIATION_ROUNDS = 3
MAXIMUM_TRANSIENT_RESYNCS = 3
MAXIMUM_REMEDIATED_RESOURCES = 10
MAXIMUM_DIAGNOSED_RESOURCES = 25
MAXIMUM_POD_REASONS = 5
SYNC_MESSAGE_LIMIT = 300
SYNC_NOTIFICATION_TOPIC = "telegram.engineering-gitops-sync"
TRANSIENT_POD_REASONS = frozenset({"ImagePullBackOff", "ErrImagePull"})
# Patch-incompatible changes (a field switches between mutually exclusive forms) that a
# full replace applies, e.g. gntik-ai/falcone#980: env[].value -> env[].valueFrom.
_PATCH_CONFLICT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"may not be specified when `?value`? is not empty",
        r"may not have more than one field specified at a time",
        r"may not specify more than 1 handler type",
        r"may not specify more than 1 volume type",
    )
)
# Within one message a permanent error wins over any recoverable match (finding 6).
_PERMANENT_SYNC_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bis forbidden\b",
        r"\bcannot (?:get|list|watch|create|update|patch|delete) resource\b",
        r"\bnot found\b",
        r"\bunknown field\b",
        r"\bcould not find\b",
        r"\bno matches for kind\b",
        r"\bValidationError\b",
        r"\bx509\b",
        r"\bcertificate\b",
        r"\bunknown authority\b",
        r"\bdenied the request\b",
        r"\bexceeded quota\b",
    )
)
# Kubernetes field errors ("<path>: Invalid value: ..."); each must be explained by a
# replaceable conflict or a Job's immutable field, otherwise the message is OTHER.
_FIELD_ERROR = re.compile(
    r"(?:Invalid value|Forbidden|Required value|Unsupported value|Duplicate value|"
    r"Too long|Too many):",
    re.IGNORECASE,
)
_RECOVERABLE_SYNC_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"the object has been modified",
        r"etcdserver: request timed out",
        r"TooManyRequests|too many requests",
    )
)
# Transport failures only; a webhook that answered (e.g. denied, x509) is not transient.
_TRANSPORT_FAILURE = re.compile(
    r"context deadline exceeded|i/o timeout|connection refused|connection reset|"
    r"no endpoints available|\bEOF\b|Client\.Timeout exceeded",
    re.IGNORECASE,
)
_IMMUTABLE_FIELD = re.compile(r"field is immutable", re.IGNORECASE)
_DEGRADED_HEALTH = frozenset({"Degraded", "Missing", "Progressing"})
# NEW-155: Argo CD's Running-phase message names the resource(s) whose health it awaits
# ("waiting for healthy state of apps/Deployment/x"; the core group is empty).
_HEALTH_WAIT = re.compile(
    r"waiting for healthy state of ([a-z0-9.-]{0,253})/([A-Za-z0-9]{1,63})/([a-z0-9.-]{1,253})"
)
ESO_LOG_ARGUMENTS = ("--since=15m", "--tail=3000")
ESO_LOG_LIMIT = 8_000_000
_WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"})
_BENIGN_WAITING = frozenset({"ContainerCreating", "PodInitializing"})
# action -> (failure class it repairs, Argo syncOptions, apply force). Argo reads force
# from operation.sync.syncStrategy.apply.force, not from syncOptions (finding 3).
# "complete" finishes a selective replace with one normal sync when the rest of the
# application is still OutOfSync.
SYNC_REMEDIATION_ACTIONS: dict[str, tuple[str, tuple[str, ...], bool]] = {
    "replace": ("PATCH_CONFLICT", ("Replace=true",), False),
    "replace_force": ("JOB_IMMUTABLE", ("Replace=true",), True),
    "resync": ("TRANSIENT", (), False),
    "complete": ("", (), False),
    # NEW-116: one TRUE full sync after a full-sync request ran with an inherited scope.
    "full_resync": ("SCOPE_INHERITED", (), False),
}
MAXIMUM_SCOPE_RESYNCS = 1
# NEW-116: the explicit hook strategy of a full sync (Argo CD's default strategy, and the
# only one that runs PreSync/Sync/PostSync hooks; an apply strategy or a resource list
# makes the controller skip hooks: controller/sync.go WithOperationSettings).
FULL_SYNC_STRATEGY: dict[str, Any] = {"hook": {}}
REMEDIATION_INFO = "hermes-remediation"
REMEDIATION_PREVIOUS_INFO = "hermes-previous-started-at"
_REMEDIATION_ID = re.compile(r"[A-Za-z0-9._:/-]{1,240}")
# The JSON-patch "test" on metadata.resourceVersion failed, so the write provably did not
# apply. The API server turns the evanphx/json-patch error ("testing value ... failed") into
# a generic 422 ("The request is invalid: the server rejected our request due to an error
# in our request"); a 422 is always a rejected, unapplied request.
_CONDITIONAL_WRITE_CONFLICT = re.compile(
    r"testing value\b.*\bfailed|\btest failed\b"
    r"|the server rejected our request due to an error in our request"
    r"|\(Invalid\)|\b422\b",
    re.IGNORECASE,
)


def classify_sync_failure(kind: str, message: str) -> str:
    """Classify one failed Argo CD resource result by its whole Kubernetes error message."""
    text = message or ""
    if any(pattern.search(text) for pattern in _PERMANENT_SYNC_PATTERNS):
        return "OTHER"
    conflicts = sum(len(pattern.findall(text)) for pattern in _PATCH_CONFLICT_PATTERNS)
    immutable = len(_IMMUTABLE_FIELD.findall(text))
    field_errors = len(_FIELD_ERROR.findall(text))
    if immutable:
        # Jobs are disposable; any other immutable field (e.g. a selector) needs a human.
        explained = kind == "Job" and field_errors <= conflicts + immutable
        return "JOB_IMMUTABLE" if explained else "OTHER"
    if conflicts:
        return "PATCH_CONFLICT" if field_errors <= conflicts else "OTHER"
    if field_errors or re.search(r"\bis invalid\b", text, re.IGNORECASE):
        return "OTHER"
    if _TRANSPORT_FAILURE.search(text) or any(
        pattern.search(text) for pattern in _RECOVERABLE_SYNC_PATTERNS
    ):
        return "TRANSIENT"
    return "OTHER"


def classify_degraded(reasons: Iterable[str]) -> str:
    """Degraded health is only TRANSIENT when every pod reason is an image pull back-off."""
    values = {reason for reason in reasons if reason}
    return "TRANSIENT" if values and values <= TRANSIENT_POD_REASONS else "OTHER"


def sealed_secret_stores(log: str) -> set[tuple[str, str, str]]:
    """NEW-155: (kind, namespace, name) of stores whose backend the ESO log reports sealed.

    Only identities leave this function; no log text is ever kept.
    """
    sealed: set[tuple[str, str, str]] = set()
    for line in log.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or "is sealed" not in str(entry.get("error") or "").lower():
            continue
        for kind, keys in (
            ("ClusterSecretStore", ("clustersecretstore", "ClusterSecretStore")),
            ("SecretStore", ("secretstore", "SecretStore")),
        ):
            for key in keys:
                reference = entry.get(key)
                name = str(_nested(reference, "name") or "")
                if not _KUBERNETES_NAME.fullmatch(name):
                    continue
                namespace = ""
                if kind == "SecretStore":
                    namespace = str(_nested(reference, "namespace") or entry.get("namespace") or "")
                sealed.add((kind, namespace, name))
    return sealed


def overall_failure_class(classes: Iterable[str]) -> str:
    """OTHER dominates: a partial repair never makes the sync succeed."""
    seen = set(classes)
    for value in ("OTHER", "PATCH_CONFLICT", "JOB_IMMUTABLE", "PRUNE_REQUIRED", "TRANSIENT"):
        if value in seen:
            return value
    return "OTHER"


def full_sync_scope_violations(
    application: dict[str, Any], operation_state: dict[str, Any], *, check_result: bool
) -> list[str]:
    """NEW-116: why a completed operation is NOT the true full sync Hermes requested.

    Argo CD v3.4.x merges each new operation state into the previous one (JSON merge
    patch of ``status.operationState`` with ``omitempty`` fields), so an omitted or empty
    resources/syncOptions/syncStrategy/revision silently keeps the previous operation's
    value. Hermes's full sync writes none of them except the hook strategy, so any of them
    here is inherited. ``check_result`` also requires every source pinned to a commit SHA
    in the spec to be the revision the operation synced (only meaningful on success).
    """
    sync = _nested(operation_state, "operation", "sync")
    sync = sync if isinstance(sync, dict) else {}
    violations: list[str] = []
    if sync.get("resources"):
        violations.append("resources")
    if sync.get("syncOptions"):
        violations.append("sync_options")
    if _nested(sync, "syncStrategy", "apply") is not None:
        violations.append("apply_strategy")
    if sync.get("dryRun"):
        violations.append("dry_run")
    if sync.get("revision") or sync.get("revisions"):
        violations.append("explicit_revision")
    if check_result and not _pinned_revisions_synced(application, operation_state):
        violations.append("revision")
    return violations


def _pinned_revisions_synced(application: dict[str, Any], operation_state: dict[str, Any]) -> bool:
    spec = application.get("spec")
    spec = spec if isinstance(spec, dict) else {}
    sources = spec.get("sources")
    if isinstance(sources, list) and sources:
        synced = _nested(operation_state, "syncResult", "revisions")
        synced = synced if isinstance(synced, list) else []
        for index, source in enumerate(sources):
            target = str(source.get("targetRevision") or "") if isinstance(source, dict) else ""
            if _SHA.fullmatch(target) and (index >= len(synced) or synced[index] != target):
                return False
        return True
    target = str(_nested(spec, "source", "targetRevision") or "")
    return not _SHA.fullmatch(target) or (
        _nested(operation_state, "syncResult", "revision") == target
    )


def resource_identity(item: dict[str, Any]) -> dict[str, str]:
    return {
        "group": str(item.get("group") or "")[:253],
        "kind": str(item.get("kind") or "")[:63],
        "namespace": str(item.get("namespace") or "")[:63],
        "name": str(item.get("name") or "")[:253],
    }


def resource_key(item: dict[str, Any]) -> str:
    identity = resource_identity(item)
    return "/".join(identity[field] for field in ("group", "kind", "namespace", "name"))


def _owned_by(pod: dict[str, Any], kind: str, name: str) -> bool:
    owners = _nested(pod, "metadata", "ownerReferences")
    for owner in owners if isinstance(owners, list) else []:
        if not isinstance(owner, dict):
            continue
        owner_kind, owner_name = owner.get("kind"), str(owner.get("name") or "")
        if owner_kind == kind and owner_name == name:
            return True
        if kind == "Deployment" and owner_kind == "ReplicaSet":
            if owner_name.rsplit("-", 1)[0] == name:
                return True
    return False


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
            "gitops.diagnose_sync": self._diagnose_sync,
            "gitops.remediate_sync": self._remediate_sync,
            "gitops.notify_sync": self._notify_sync,
            "gitops.notify_merged": self._notify_merged,
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
            # NEW-113: Argo CD sync failure class, escalation issue and bounded attempts.
            "argo_sync_failure_class": (
                str(payload["argo_sync_failure_class"])
                if payload.get("argo_sync_failure_class") in SYNC_FAILURE_CLASSES
                else None
            ),
            "argo_sync_issue_number": (
                int(payload["argo_sync_issue_number"])
                if isinstance(payload.get("argo_sync_issue_number"), int)
                and payload["argo_sync_issue_number"] > 0
                else None
            ),
            "argo_sync_remediations": _sync_attempts(payload.get("argo_sync_remediations")),
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
            # NEW-118 review 3: lock the row so concurrent persists serialize the
            # read-modify-write of recovery_json and the write-once ``source_merge``.
            change_set = session.get(db.ChangeSet, plan.change_set_id, with_for_update=True)
            if change_set is None or change_set.project != plan.project:
                raise ValueError("staging GitOps ChangeSet binding is missing")
            recovery = dict(change_set.recovery_json or {})
            # A replayed plan for a source PR the ChangeSet no longer binds (rebinding to
            # a new PR) neither creates nor restores a merge record.
            bound_url = change_set.source_pr_url
            plan_bound = bool(bound_url) and plan.source_pr_url == bound_url
            # A plan for a different source PR than the one bound now is stale: it never
            # moves the heads, the state or the current staging record.
            stale = bool(bound_url) and plan.source_pr_url != bound_url
            record["source_pr_url"] = plan.source_pr_url
            # NEW-118 review: a merged source PR stays merged. The first recorded merge is
            # durable (``source_merge``, bound to the source PR) and a later recheck that
            # stops before the merge step (e.g. deployment CI) never nulls it out.
            previous = recovery.get("staging_gitops")
            previous_sha = (
                previous.get("merge_sha")
                if isinstance(previous, dict)
                and previous.get("source_pr_url", plan.source_pr_url) == plan.source_pr_url
                else None
            )
            durable = recovery.get("source_merge")
            # Write-once per source PR: a record for a previously bound PR is replaced
            # when the PR bound now merges.
            if plan_bound and not (
                isinstance(durable, dict)
                and _SHA.fullmatch(str(durable.get("merge_sha")))
                and durable.get("source_pr_url") == bound_url
            ):
                first_sha = merge_sha if merge_sha is not None else previous_sha
                if first_sha is not None and _SHA.fullmatch(str(first_sha)):
                    durable = {
                        "merge_sha": str(first_sha),
                        "source_pr_url": plan.source_pr_url,
                        "source_head_sha": plan.source_head_sha,
                        "recorded_at": record["recorded_at"],
                    }
                    recovery["source_merge"] = durable
            if (
                record["merge_sha"] is None
                and plan_bound
                and isinstance(durable, dict)
                and durable.get("source_pr_url") == bound_url
            ):
                record["merge_sha"] = durable.get("merge_sha")
            if not stale:
                recovery["staging_gitops"] = record
            change_set.recovery_json = recovery
            if merge_sha is not None and not stale:
                # NEW-102: the merged PR heads are what was delivered; a stale write (e.g. a
                # repair's unpublished local head) never survives on a merged ChangeSet.
                change_set.source_head_sha = plan.source_head_sha
                if plan.deployment_pr_number is not None and plan.deployment_head_sha:
                    change_set.deployment_head_sha = plan.deployment_head_sha
            if not stale and outcome == "STAGING_VERIFIED":
                change_set.state = ChangeSetState.RUNTIME_VERIFIED.value
            elif (
                not stale
                and merge_sha is not None
                and change_set.state
                in {
                    ChangeSetState.SOURCE_PR.value,
                    ChangeSetState.CI_VALIDATED.value,
                    # NEW-102: a late repair write may have reset the state after prepare;
                    # the merge is what happened, so the ChangeSet is SOURCE_MERGED.
                    ChangeSetState.PLANNED.value,
                    ChangeSetState.IMPLEMENTING.value,
                    ChangeSetState.REQUEST_CHANGES.value,
                }
            ):
                change_set.state = ChangeSetState.SOURCE_MERGED.value
            change_set.version += 1
            if workflow_id:
                run = session.scalar(select(db.Run).where(db.Run.workflow_id == workflow_id))
                if run is not None and run.run_type == "staging-gitops":
                    run.status = "SUCCEEDED" if outcome == "STAGING_VERIFIED" else outcome
                    run.error_code = None if outcome == "STAGING_VERIFIED" else str(blocker)
                    run.finished_at = datetime.now(UTC)
            state = change_set.state
            if not stale:
                self._notify_result(session, plan, record, outcome, workflow_id)
        return {"status": "PERSISTED", "outcome": outcome, "change_set_state": state}

    def _notify_merged(self, payload: dict[str, Any]) -> dict[str, Any]:
        """NEW-127 review: announce a merge as soon as the merge step succeeded."""
        plan = StagingGitOpsPlan.model_validate(payload.get("plan"))
        target = str(payload.get("target") or "")
        merge_sha = str(payload.get("merge_sha") or "")
        if target not in {"source", "deployment"} or not _SHA.fullmatch(merge_sha):
            raise ValueError("merge notification target or SHA is invalid")
        orchestration = payload.get("orchestration")
        workflow_id = (
            str(orchestration.get("workflow_id", "")) if isinstance(orchestration, dict) else ""
        )
        with self.sessions.begin() as session:
            created = self._enqueue_merged(session, plan, target, merge_sha, workflow_id)
        return {"status": "NOTIFIED" if created else "ALREADY_NOTIFIED", "target": target}

    def _enqueue_merged(
        self,
        session: Any,
        plan: StagingGitOpsPlan,
        target: str,
        merge_sha: str,
        workflow_id: str,
    ) -> bool:
        """One PRS_MERGED per merged PR; persist_result reuses the key (no duplicate)."""
        url = plan.source_pr_url if target == "source" else plan.deployment_pr_url
        if not url:
            return False
        issue = f"{plan.issue_repository}#{plan.issue_number}" if plan.issue_number else None
        return enqueue_milestone(
            session,
            event="PRS_MERGED",
            dedupe_key=f"{plan.change_set_id}:{target}:{merge_sha}",
            project=plan.project,
            issue=issue,
            workflow_id=workflow_id or None,
            detail=f"{target} PR {url} merged at {merge_sha[:12]}",
            extra={"target": target, "merge_sha": merge_sha, "pull_request": url},
            redactor=self.redactor,
        )

    def _notify_result(
        self,
        session: Any,
        plan: StagingGitOpsPlan,
        record: dict[str, Any],
        outcome: str,
        workflow_id: str,
    ) -> None:
        """NEW-127: PRs merged, STAGING_VERIFIED or a staging blocker, once each."""
        issue = f"{plan.issue_repository}#{plan.issue_number}" if plan.issue_number else None
        common: dict[str, Any] = {
            "project": plan.project,
            "issue": issue,
            "workflow_id": workflow_id or None,
            "redactor": self.redactor,
        }
        merge_sha = record.get("merge_sha")
        for target, sha in (
            ("source", merge_sha),
            ("deployment", record.get("deployment_merge_sha")),
        ):
            if sha:
                self._enqueue_merged(session, plan, target, str(sha), workflow_id)
        if outcome == "STAGING_VERIFIED":
            enqueue_milestone(
                session,
                event="STAGING_VERIFIED",
                dedupe_key=f"{plan.change_set_id}:{merge_sha or ''}",
                detail=(
                    "staging runs the merged change"
                    + (f" (image {record['image_tag']})" if record.get("image_tag") else "")
                    + (
                        f", Argo revision {record['argo_revision']}"
                        if record.get("argo_revision")
                        else ""
                    )
                ),
                **common,
            )
        elif outcome == "BLOCKED":
            blocker = str(record.get("blocker") or "STAGING_GITOPS_FAILED")
            checks = [literal(item, 120) for item in list(record.get("ci_regressions") or [])[:8]]
            enqueue_milestone(
                session,
                event="STAGING_BLOCKED",
                dedupe_key=f"{plan.change_set_id}:{workflow_id}:{blocker}",
                detail=(
                    f"{blocker} - {explain_blocker(blocker)}"
                    + (
                        f"; failing checks on the {record.get('ci_regression_target')} PR: "
                        + ", ".join(checks)
                        if checks
                        else ""
                    )
                ),
                extra={"blocker": blocker, "failing_checks": checks},
                **common,
            )

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
        external_secrets: list[dict[str, str]] = []
        external_secrets_total = 0
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
                # NEW-155: ExternalSecrets left in ESO's error backoff after a store recovers.
                ready_condition: dict[str, Any] = {}
                conditions = _nested(item, "status", "conditions")
                for condition in conditions if isinstance(conditions, list) else []:
                    if isinstance(condition, dict) and condition.get("type") == "Ready":
                        ready_condition = condition
                if ready_condition.get("status") != "True":
                    external_secrets_total += 1
                    if len(external_secrets) < MAXIMUM_DIAGNOSED_RESOURCES:
                        external_secrets.append(
                            {
                                "namespace": namespace,
                                "name": _name(item),
                                "reason": str(ready_condition.get("reason") or "Unknown")[:80],
                            }
                        )
        stores: list[dict[str, Any]] = []
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
        # NEW-155: a sealed OpenBao/Vault shows only as InvalidProviderConfig on the store;
        # the ESO controller log names the cause. Only store names leave this block.
        # Review: None means the log could not be read, so seal status is unknown (not
        # False); the pre-NEW-155 ESO restart still applies then.
        sealed = self._sealed_stores(flow) if stores else set()
        for store in stores:
            store["sealed"] = (
                None
                if sealed is None
                else (store["kind"], store["namespace"], store["name"]) in sealed
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
            "sealed_backend_stores": sorted({store["name"] for store in stores if store["sealed"]}),
            "seal_status_known": sealed is not None,
            "eso_objects_not_ready": external_secrets,
            "eso_objects_not_ready_total": external_secrets_total,
            "eso_resources_observed": eso_resources_observed,
            "argo_application": flow.argo_application,
            "argo_health": health[:40],
            "argo_sync": sync[:40],
        }

    def _sealed_stores(self, flow: StagingFlowPolicy) -> set[tuple[str, str, str]] | None:
        """NEW-155: stores the ESO controller log reports sealed; None when unreadable."""
        try:
            result = self._kubectl(
                [
                    "--namespace",
                    flow.external_secrets_namespace,
                    "logs",
                    f"deployment/{flow.external_secrets_deployment}",
                    *ESO_LOG_ARGUMENTS,
                ],
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            # Review: an optional read; its captured output is never exported.
            return None
        if result.return_code or len(result.stdout.encode()) > ESO_LOG_LIMIT:
            return None
        return sealed_secret_stores(result.stdout)

    def _environment_check(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = StagingGitOpsPlan.model_validate(payload.get("plan"))
        _, flow = enabled_staging_flow(self.settings.project_config_root, plan.project)
        diagnosis = self._observe(flow)
        return {
            "status": "HEALTHY" if diagnosis["healthy"] else "UNHEALTHY",
            "diagnosis": diagnosis,
        }

    @staticmethod
    def _operation_running(application: dict[str, Any]) -> bool:
        return application.get("operation") is not None or (
            _nested(application, "status", "operationState", "phase") in {"Running", "Terminating"}
        )

    def _refresh(self, flow: StagingFlowPolicy) -> None:
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

    def _refresh_and_sync(self, flow: StagingFlowPolicy) -> bool:
        """Hard-refresh the Application and request a TRUE full sync unless one is in flight.

        NEW-116: the full-sync form (no resources, syncOptions or revision; the hook
        strategy) only stays full because ``_start_operation`` also clears the previous
        ``status.operationState`` the controller would otherwise merge it into.
        """
        self._refresh(flow)
        operation = {
            "initiatedBy": {"username": ARGO_SYNC_USERNAME},
            "sync": {"prune": False, "syncStrategy": FULL_SYNC_STRATEGY},
        }
        return self._start_operation(flow, lambda _: operation) == "STARTED"

    def _start_operation(
        self,
        flow: StagingFlowPolicy,
        build: Callable[[dict[str, Any]], dict[str, Any] | str],
        landed: Callable[[dict[str, Any]], bool] | None = None,
    ) -> str:
        """Write the COMPLETE operation only if none is in flight, atomically.

        NEW-113: ``build`` sees the very read whose resourceVersion the JSON patch
        ``test``s, so the busy check, any revalidation and the write are one step; and
        ``add /operation`` writes the whole object, so no operator-set field (e.g.
        syncStrategy.apply.force) survives into it. Returns STARTED, BUSY or the refusal
        blocker ``build`` returned.

        NEW-116: ``/operation`` alone is not enough. Argo CD v3.4.x initialises the new
        operation state with a JSON MERGE patch of ``status.operationState`` (controller
        ``setOperationState``), and every SyncOperation field is ``omitempty``, so an
        absent/empty resources, syncOptions, syncStrategy or revision keeps the PREVIOUS
        operation's value: a full sync after a selective one runs selectively, without
        hooks. The same patch therefore removes the completed ``status.operationState``,
        exactly as Argo CD's own API does (util/argo ``SetAppOperation`` sets
        ``Status.OperationState = nil`` with the operation). A full-sync request (no
        resources) always carries the explicit hook strategy.
        """
        for _ in range(3):
            application = self._application(flow)
            # NEW-113 review fallback: a write whose response was lost may already have
            # landed; recognise it by its identity on every read and never write twice.
            if landed is not None and landed(application):
                return "STARTED"
            if self._operation_running(application):
                return "BUSY"
            version = _nested(application, "metadata", "resourceVersion")
            if not isinstance(version, str) or not version:
                raise ValueError("Argo CD Application has no resourceVersion")
            operation = build(application)
            if isinstance(operation, str):
                return operation
            sync = {**operation.get("sync", {}), "prune": False}
            if not sync.get("resources") and "syncStrategy" not in sync:
                sync["syncStrategy"] = FULL_SYNC_STRATEGY
            operation = {**operation, "sync": sync}
            patch: list[dict[str, Any]] = [
                {"op": "test", "path": "/metadata/resourceVersion", "value": version},
                {"op": "add", "path": "/operation", "value": operation},
            ]
            if _nested(application, "status", "operationState") is not None:
                # Not running (checked above) and guarded by the resourceVersion test.
                patch.append({"op": "remove", "path": "/status/operationState"})
            written = self._kubectl(
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
                ],
                check=False,
            )
            if not written.return_code:
                return "STARTED"
            after = self._application(flow)
            if landed is not None and landed(after):
                return "STARTED"
            if _nested(after, "metadata", "resourceVersion") == version:
                # Nothing moved, so the write itself failed (e.g. RBAC): fail closed.
                raise CommandFailure("Argo CD operation patch was rejected")
            if landed is not None and not _CONDITIONAL_WRITE_CONFLICT.search(
                f"{written.stdout}\n{written.stderr}"
            ):
                # NEW-113 review: a remediation write may have landed even though its
                # response was lost, and a later operation may already have replaced its
                # identity. Only a confirmed failed resourceVersion test justifies writing
                # again; any other failure is an unknown outcome for the operator.
                return "OUTCOME_UNKNOWN"
        # The Application kept changing under us: report busy and let the flow poll.
        return "BUSY"

    def _remediate(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = StagingGitOpsPlan.model_validate(payload.get("plan"))
        _, flow = enabled_staging_flow(self.settings.project_config_root, plan.project)
        # Never trust the payload's diagnosis for mutations: observe again.
        diagnosis = self._observe(flow)
        actions: list[str] = []
        synced = self._refresh_and_sync(flow)
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
        if diagnosis["sealed_backend_stores"]:
            # NEW-155: a restart cannot unseal OpenBao/Vault; the issue tells the operator.
            actions.append("secret-backend-sealed")
        elif diagnosis["stores_not_ready"] or diagnosis["eso_objects_not_ready"]:
            # NEW-155: also when every store is Ready again but ExternalSecrets sit in
            # ESO's error backoff (observed >10 min after an unseal).
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
        if not self._refresh_and_sync(flow):
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
        if payload.get("selective") is not True and phase in {"Succeeded", "Failed", "Error"}:
            # NEW-116: a full sync was requested; a selective (inherited) scope never
            # counts as SYNCED, and is reported apart from an ordinary failure.
            violations = full_sync_scope_violations(
                application, operation_state, check_result=phase == "Succeeded"
            )
            if violations:
                return {
                    "status": "SYNC_SCOPE_INHERITED",
                    "blocker": "ARGO_SYNC_SCOPE_INHERITED",
                    "scope_violations": violations,
                    "operation_started_at": started[:80],
                    **observed,
                }
        if (
            payload.get("selective") is not True
            and phase == "Succeeded"
            and sync != "Synced"
            and self._only_prune_drift(application)
        ):
            # NEW-118: the sync applied everything; only resources Argo CD would prune
            # remain OutOfSync, which no amount of waiting fixes (falcone-charts #45).
            return {
                "status": "SYNC_PRUNE_REQUIRED",
                "blocker": "ARGO_PRUNE_REQUIRED",
                "operation_started_at": started[:80],
                **observed,
            }
        if phase in {"Failed", "Error"}:
            return {
                "status": "SYNC_FAILED",
                "blocker": "ARGO_SYNC_FAILED",
                "message": self._bounded_message(operation_state.get("message")),
                # NEW-113: names the failed operation a remediation may act on.
                "operation_started_at": started[:80],
                **observed,
            }
        if phase == "Succeeded" and sync != "Synced" and payload.get("selective") is True:
            # NEW-113: a selective replace applied; the rest still needs one normal sync.
            return {"status": "SYNC_PARTIAL", **observed}
        if phase == "Succeeded" and sync == "Synced" and health == "Healthy":
            revisions = _nested(application, "status", "sync", "revisions")
            revision = (
                ",".join(str(value) for value in revisions)
                if isinstance(revisions, list) and revisions
                else str(_nested(application, "status", "sync", "revision") or "")
            )
            return {"status": "SYNCED", "argo_revision": revision[:400], **observed}
        return {"status": "SYNC_PENDING", **observed}

    @staticmethod
    def _only_prune_drift(application: dict[str, Any]) -> bool:
        """Some managed resource is OutOfSync and every OutOfSync one requires pruning."""
        resources = _nested(application, "status", "resources")
        drifting = [
            item
            for item in (resources if isinstance(resources, list) else [])
            if isinstance(item, dict) and item.get("status") not in {None, "Synced"}
        ]
        return bool(drifting) and all(item.get("requiresPruning") is True for item in drifting)

    # -- NEW-113: sync failure diagnosis, remediation and notification --------------------

    def _bounded_message(self, value: object, limit: int = SYNC_MESSAGE_LIMIT) -> str:
        # Redact the ORIGINAL text first: the line-anchored credential patterns need its
        # line breaks. Only then flatten and truncate (finding 8).
        redacted = str(self.redactor.redact_text(str(value or "")).value)
        return " ".join(redacted.split())[:limit]

    def _pod_reasons(
        self,
        namespace: str,
        kind: str,
        name: str,
        cache: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, str]]:
        """Every non-benign container reason of the workload's pods (unbounded here;
        only the exported evidence is bounded, so classification sees all of them)."""
        if namespace not in cache:
            # _items fails closed (ValueError -> blocked -> OTHER) on an oversized list.
            cache[namespace] = _items(
                self._kubectl_json(["--namespace", namespace, "get", "pods"]), "pods"
            )
        reasons: list[dict[str, str]] = []
        for pod in cache[namespace]:
            if not _owned_by(pod, kind, name):
                continue
            statuses: list[Any] = []
            for key in ("initContainerStatuses", "containerStatuses"):
                values = _nested(pod, "status", key)
                if isinstance(values, list):
                    statuses.extend(values)
            for container in statuses:
                waiting = _nested(container, "state", "waiting")
                terminated = _nested(container, "state", "terminated")
                state: dict[str, Any] = {}
                if isinstance(waiting, dict) and waiting.get("reason") not in _BENIGN_WAITING:
                    state = waiting
                elif isinstance(terminated, dict) and terminated.get("reason") != "Completed":
                    state = terminated
                if not state.get("reason"):
                    continue
                reasons.append(
                    {
                        "pod": _name(pod),
                        "container": str(_nested(container, "name") or "")[:63],
                        "reason": str(state["reason"])[:60],
                        "message": self._bounded_message(state.get("message"), 200),
                    }
                )
        return reasons

    def _sync_diagnosis(
        self, flow: StagingFlowPolicy, application: dict[str, Any]
    ) -> dict[str, Any]:
        """Read-only: failed sync results and degraded managed resources, classified.

        Classification covers EVERY failed result and pod reason; only the exported
        evidence is bounded (finding 5).
        """
        status = application.get("status")
        status = status if isinstance(status, dict) else {}
        operation_state = status.get("operationState")
        operation_state = operation_state if isinstance(operation_state, dict) else {}
        phase = str(operation_state.get("phase") or "")
        sync = str(_nested(status, "sync", "status") or "Unknown")
        health = str(_nested(status, "health", "status") or "Unknown")
        managed_items = status.get("resources")
        managed_items = managed_items if isinstance(managed_items, list) else []
        managed = {resource_key(item): item for item in managed_items if isinstance(item, dict)}
        classes: list[str] = []
        failed: list[dict[str, Any]] = []
        failed_total = 0
        results = _nested(operation_state, "syncResult", "resources")
        for item in results if isinstance(results, list) else []:
            if not isinstance(item, dict) or (
                item.get("status") != "SyncFailed"
                and item.get("hookPhase") not in {"Failed", "Error"}
            ):
                continue
            identity = resource_identity(item)
            failure_class = classify_sync_failure(identity["kind"], str(item.get("message") or ""))
            key = resource_key(identity)
            live = managed.get(key)
            eligible = (
                live is not None
                and identity["namespace"] in flow.namespaces
                and bool(_KUBERNETES_NAME.fullmatch(identity["name"]))
            )
            # A replaceable conflict Hermes may not touch is the operator's to fix.
            classes.append(
                "OTHER"
                if failure_class in {"PATCH_CONFLICT", "JOB_IMMUTABLE"} and not eligible
                else failure_class
            )
            failed_total += 1
            if len(failed) >= MAXIMUM_DIAGNOSED_RESOURCES:
                continue
            failed.append(
                {
                    **identity,
                    "status": str(item.get("status") or item.get("hookPhase") or "")[:40],
                    "message": self._bounded_message(item.get("message")),
                    "failure_class": failure_class,
                    "managed": live is not None,
                    "eligible": eligible,
                    # Still drifting: OutOfSync or not Healthy in the live status.
                    "current": live is not None
                    and (
                        live.get("status") != "Synced"
                        or _nested(live, "health", "status") in _DEGRADED_HEALTH
                    ),
                }
            )
        degraded: list[dict[str, Any]] = []
        degraded_keys: set[str] = set()
        degraded_total = 0
        pods: dict[str, list[dict[str, Any]]] = {}

        def add_degraded(item: dict[str, Any], item_health: object) -> None:
            nonlocal degraded_total
            identity = resource_identity(item)
            reasons: list[dict[str, str]] = []
            if (
                identity["kind"] in _WORKLOAD_KINDS
                and identity["namespace"] in flow.namespaces
                and _KUBERNETES_NAME.fullmatch(identity["name"])
            ):
                reasons = self._pod_reasons(
                    identity["namespace"], identity["kind"], identity["name"], pods
                )
            failure_class = classify_degraded(reason["reason"] for reason in reasons)
            classes.append(failure_class)
            degraded_keys.add(resource_key(identity))
            degraded_total += 1
            if len(degraded) >= MAXIMUM_DIAGNOSED_RESOURCES:
                return
            degraded.append(
                {
                    **identity,
                    "health": str(item_health)[:40],
                    "message": self._bounded_message(_nested(item, "health", "message")),
                    "pod_reasons": reasons[:MAXIMUM_POD_REASONS],
                    "pod_reasons_total": len(reasons),
                    "failure_class": failure_class,
                }
            )

        if sync == "Synced" and health != "Healthy":
            for item in managed_items:
                item_health = _nested(item, "health", "status")
                if not isinstance(item, dict) or item_health not in _DEGRADED_HEALTH:
                    continue
                add_degraded(item, item_health)
        raw_message = str(operation_state.get("message") or "")
        # NEW-155: a sync stuck waiting for a resource's health is caused by THAT resource
        # (even one whose health the scan above skips, e.g. an ExternalSecret), never by
        # unrelated prune drift (production 2026-10-07: ARGO_PRUNE_REQUIRED misreported).
        waits = _HEALTH_WAIT.findall(raw_message[:20_000])[:MAXIMUM_DIAGNOSED_RESOURCES]
        for group, kind, name in waits:
            matches = [
                item
                for item in managed_items
                if isinstance(item, dict)
                and (str(item.get("group") or ""), item.get("kind"), item.get("name"))
                == (group, kind, name)
            ]
            if not matches:
                classes.append("OTHER")
            for item in matches:
                if resource_key(item) not in degraded_keys:
                    add_degraded(item, _nested(item, "health", "status") or "Unknown")
        # NEW-116: a successful sync can still leave the application OutOfSync (e.g. it ran
        # with an inherited selective scope); name those resources instead of "none".
        out_of_sync: list[dict[str, str]] = []
        out_of_sync_total = 0
        for item in managed_items:
            if not isinstance(item, dict) or item.get("status") in {None, "Synced"}:
                continue
            out_of_sync_total += 1
            if len(out_of_sync) < MAXIMUM_DIAGNOSED_RESOURCES:
                out_of_sync.append(
                    {**resource_identity(item), "status": str(item.get("status"))[:40]}
                )
        # NEW-118: resources Argo CD would prune (Hermes syncs with prune=false and never
        # prunes); each is the operator's, with its Argo CD tracking id for verification.
        prune_required: list[dict[str, Any]] = []
        prune_required_total = 0
        for item in managed_items:
            if not isinstance(item, dict) or item.get("requiresPruning") is not True:
                continue
            identity = resource_identity(item)
            classes.append("PRUNE_REQUIRED")
            prune_required_total += 1
            if len(prune_required) < MAXIMUM_DIAGNOSED_RESOURCES:
                prune_required.append(
                    {
                        **identity,
                        "status": str(item.get("status") or "")[:40],
                        "tracking_id": (
                            f"{flow.argo_application}:{identity['group']}/{identity['kind']}:"
                            f"{identity['namespace']}/{identity['name']}"
                        )[:700],
                        "safe_kind": (identity["group"], identity["kind"]) in SAFE_PRUNE_KINDS,
                        "in_flow_namespaces": identity["namespace"] in flow.namespaces,
                    }
                )
        operation_sync = _nested(operation_state, "operation", "sync")
        operation_sync = operation_sync if isinstance(operation_sync, dict) else {}
        scoped = operation_sync.get("resources")
        options = operation_sync.get("syncOptions")
        operation_scope = {
            "resources_total": len(scoped) if isinstance(scoped, list) else 0,
            "sync_options": [
                str(option)[:60] for option in (options if isinstance(options, list) else [])
            ][:5],
            "strategy": (
                "apply" if _nested(operation_sync, "syncStrategy", "apply") is not None else "hook"
            ),
        }
        if not classes and phase in {"Failed", "Error"}:
            operation_class = classify_sync_failure("", raw_message)
            classes.append("TRANSIENT" if operation_class == "TRANSIENT" else "OTHER")
        return {
            "application": flow.argo_application,
            "phase": phase[:40],
            "sync": sync[:40],
            "health": health[:40],
            "operation_started_at": str(operation_state.get("startedAt") or "")[:80],
            "message": self._bounded_message(raw_message),
            "failed_resources": failed,
            "failed_resources_total": failed_total,
            "degraded_resources": degraded,
            "degraded_resources_total": degraded_total,
            "out_of_sync_resources": out_of_sync,
            "out_of_sync_resources_total": out_of_sync_total,
            "prune_required_resources": prune_required,
            "prune_required_resources_total": prune_required_total,
            "operation_scope": operation_scope,
            "classes": sorted(set(classes)),
            # NEW-155: while Argo CD awaits a resource's health, prune drift is reported
            # (above) but never the class: PRUNE_REQUIRED would outrank a TRANSIENT wait.
            "failure_class": overall_failure_class(
                value for value in classes if not waits or value != "PRUNE_REQUIRED"
            ),
        }

    def _diagnose_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = StagingGitOpsPlan.model_validate(payload.get("plan"))
        _, flow = enabled_staging_flow(self.settings.project_config_root, plan.project)
        diagnosis = self._sync_diagnosis(flow, self._application(flow))
        return {
            "status": "DIAGNOSED",
            "failure_class": diagnosis["failure_class"],
            "diagnosis": diagnosis,
        }

    @staticmethod
    def _require_staging_target(manifest: ProjectManifest) -> None:
        deployment = manifest.deployment
        if (
            deployment.staging_policy.production
            or deployment.production_supported
            or deployment.production_installed
        ):
            raise StagingFlowBlocked("ARGO_REMEDIATION_PRODUCTION_REFUSED")

    @staticmethod
    def _selective_sync_completed(application: dict[str, Any]) -> bool:
        """The last operation was Hermes's selective replace and it succeeded."""
        operation_state = _nested(application, "status", "operationState")
        return (
            isinstance(operation_state, dict)
            and operation_state.get("phase") == "Succeeded"
            and _nested(operation_state, "operation", "initiatedBy", "username")
            == ARGO_SYNC_USERNAME
            and bool(_nested(operation_state, "operation", "sync", "resources"))
            and _nested(application, "status", "sync", "status") != "Synced"
        )

    @staticmethod
    def _remediation_operation(
        application: dict[str, Any], remediation_id: str
    ) -> dict[str, Any] | None:
        """The pending or last operation carrying this remediation identity, if any."""
        for operation in (
            application.get("operation"),
            _nested(application, "status", "operationState", "operation"),
        ):
            info = _nested(operation, "info")
            for item in info if isinstance(info, list) else []:
                if (
                    isinstance(item, dict)
                    and item.get("name") == REMEDIATION_INFO
                    and item.get("value") == remediation_id
                ):
                    return operation if isinstance(operation, dict) else None
        return None

    def _remediate_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Bounded staging-only Argo CD sync remediation; never prunes.

        Idempotent per ``remediation_id`` (workflow id, round, action): a retried attempt
        whose write already landed returns that operation instead of syncing again. The
        CURRENT Application is re-diagnosed on the same read the write is conditioned
        on: the failed operation must still be the one the flow diagnosed, the overall
        class must still be the remediable class (no OTHER), and every named resource
        must still be failing, drifting, managed, in the flow's namespaces and unrepaired.
        """
        plan = StagingGitOpsPlan.model_validate(payload.get("plan"))
        manifest, flow = enabled_staging_flow(self.settings.project_config_root, plan.project)
        self._require_staging_target(manifest)
        action = str(payload.get("action") or "")
        if action not in SYNC_REMEDIATION_ACTIONS:
            raise StagingFlowBlocked("ARGO_REMEDIATION_ACTION_INVALID")
        round_number = payload.get("round")
        if not isinstance(round_number, int) or not (
            1 <= round_number <= MAXIMUM_SYNC_REMEDIATION_ROUNDS
        ):
            raise StagingFlowBlocked("ARGO_REMEDIATION_ROUNDS_EXHAUSTED")
        remediation_id = str(payload.get("remediation_id") or "")
        if not _REMEDIATION_ID.fullmatch(remediation_id):
            raise StagingFlowBlocked("ARGO_REMEDIATION_ID_INVALID")
        expected_class, sync_options, force = SYNC_REMEDIATION_ACTIONS[action]

        def refused(blocker: str) -> dict[str, Any]:
            return {"status": "REMEDIATION_REFUSED", "blocker": blocker, "action": action}

        def triggered(
            resources: list[dict[str, str]], previous: str, *, replayed: bool = False
        ) -> dict[str, Any]:
            return {
                "status": "REMEDIATION_TRIGGERED",
                "action": action,
                "remediation_id": remediation_id,
                "replayed": replayed,
                "resources": resources,
                "remediated_keys": [resource_key(item) for item in resources],
                "previous_operation_started_at": previous,
            }

        existing = self._remediation_operation(self._application(flow), remediation_id)
        if existing is not None:
            info = existing.get("info")
            previous = next(
                (
                    str(item.get("value") or "")
                    for item in (info if isinstance(info, list) else [])
                    if isinstance(item, dict) and item.get("name") == REMEDIATION_PREVIOUS_INFO
                ),
                "",
            )
            raw = _nested(existing, "sync", "resources")
            resources = [
                resource_identity(item)
                for item in (raw if isinstance(raw, list) else [])
                if isinstance(item, dict)
            ]
            return triggered(resources, "" if previous == "-" else previous, replayed=True)

        attempt = payload.get("attempt")
        if action == "resync" and (
            not isinstance(attempt, int) or not 1 <= attempt <= MAXIMUM_TRANSIENT_RESYNCS
        ):
            return refused("ARGO_REMEDIATION_TRANSIENT_RETRIES_EXHAUSTED")
        if action == "full_resync" and (
            not isinstance(attempt, int) or not 1 <= attempt <= MAXIMUM_SCOPE_RESYNCS
        ):
            return refused("ARGO_REMEDIATION_SCOPE_RETRIES_EXHAUSTED")
        selected: list[dict[str, str]] = []
        if action in {"replace", "replace_force"}:
            raw_resources = payload.get("resources")
            if not isinstance(raw_resources, list) or not raw_resources:
                return refused("ARGO_REMEDIATION_NO_RESOURCES")
            if len(raw_resources) > MAXIMUM_REMEDIATED_RESOURCES:
                return refused("ARGO_REMEDIATION_TOO_MANY_RESOURCES")
            for raw in raw_resources:
                if not isinstance(raw, dict):
                    return refused("ARGO_REMEDIATION_RESOURCE_INVALID")
                identity = resource_identity(raw)
                if identity["namespace"] not in flow.namespaces:
                    return refused("ARGO_REMEDIATION_OUTSIDE_NAMESPACES")
                if not _KUBERNETES_NAME.fullmatch(identity["name"]) or not identity["kind"]:
                    return refused("ARGO_REMEDIATION_RESOURCE_INVALID")
                if action == "replace_force" and (
                    identity["kind"] != "Job" or identity["group"] != "batch"
                ):
                    return refused("ARGO_REMEDIATION_FORCE_REQUIRES_JOB")
                if identity not in selected:
                    selected.append(identity)
        raw_done = payload.get("already_remediated")
        done = {str(value) for value in raw_done} if isinstance(raw_done, list) else set()
        failed_started = str(payload.get("failed_operation_started_at") or "")
        previous_started = ""

        def build(application: dict[str, Any]) -> dict[str, Any] | str:
            nonlocal previous_started
            operation_state = _nested(application, "status", "operationState")
            operation_state = operation_state if isinstance(operation_state, dict) else {}
            previous_started = str(operation_state.get("startedAt") or "")
            if action == "complete":
                if not self._selective_sync_completed(application):
                    return "ARGO_REMEDIATION_NOTHING_TO_COMPLETE"
            elif action == "full_resync":
                # NEW-116: the diagnosed Hermes full-sync request must still be the latest
                # operation, completed, and still show the inherited scope.
                phase = str(operation_state.get("phase") or "")
                if (
                    not failed_started
                    or previous_started != failed_started
                    or phase not in {"Succeeded", "Failed", "Error"}
                    or _nested(operation_state, "operation", "initiatedBy", "username")
                    != ARGO_SYNC_USERNAME
                    or not full_sync_scope_violations(
                        application, operation_state, check_result=phase == "Succeeded"
                    )
                ):
                    return "ARGO_REMEDIATION_STALE_DIAGNOSIS"
            else:
                diagnosis = self._sync_diagnosis(flow, application)
                if diagnosis["sync"] == "Synced" and diagnosis["health"] == "Healthy":
                    return "ARGO_REMEDIATION_NOT_NEEDED"
                failed_phase = diagnosis["phase"] in {"Failed", "Error"}
                if failed_started or action != "resync":
                    # The failed operation the flow diagnosed must still be the latest.
                    if (
                        not failed_phase
                        or not failed_started
                        or previous_started != failed_started
                        or _nested(operation_state, "operation", "initiatedBy", "username")
                        != ARGO_SYNC_USERNAME
                    ):
                        return "ARGO_REMEDIATION_STALE_DIAGNOSIS"
                elif diagnosis["sync"] != "Synced":
                    # A transient re-sync of degraded health needs a Synced application.
                    return "ARGO_REMEDIATION_STALE_DIAGNOSIS"
                if diagnosis["failure_class"] != expected_class:
                    return "ARGO_REMEDIATION_CLASS_MISMATCH"
                managed_items = _nested(application, "status", "resources")
                managed = {
                    resource_key(item)
                    for item in (managed_items if isinstance(managed_items, list) else [])
                    if isinstance(item, dict)
                }
                failing = {resource_key(entry): entry for entry in diagnosis["failed_resources"]}
                for identity in selected:
                    key = resource_key(identity)
                    if key not in managed:
                        return "ARGO_REMEDIATION_RESOURCE_NOT_MANAGED"
                    if key in done:
                        return "ARGO_REMEDIATION_ALREADY_APPLIED"
                    entry = failing.get(key)
                    if entry is None or entry["failure_class"] != expected_class:
                        return "ARGO_REMEDIATION_CLASS_MISMATCH"
                    if not entry["current"]:
                        return "ARGO_REMEDIATION_STALE_DIAGNOSIS"
            sync: dict[str, Any] = {}
            if selected:
                sync["resources"] = selected
            if sync_options:
                sync["syncOptions"] = list(sync_options)
            if force:
                # Argo CD reads force from the sync strategy, never from syncOptions.
                sync["syncStrategy"] = {"apply": {"force": True}}
            return {
                "initiatedBy": {"username": ARGO_SYNC_USERNAME},
                "info": [
                    {"name": REMEDIATION_INFO, "value": remediation_id},
                    {"name": REMEDIATION_PREVIOUS_INFO, "value": previous_started or "-"},
                ],
                "sync": sync,
            }

        self._refresh(flow)
        started = self._start_operation(
            flow,
            build,
            landed=lambda application: (
                self._remediation_operation(application, remediation_id) is not None
            ),
        )
        if started == "BUSY":
            return {"status": "SYNC_BUSY", "action": action}
        if started == "OUTCOME_UNKNOWN":
            return {
                "status": "REMEDIATION_OUTCOME_UNKNOWN",
                "blocker": "ARGO_REMEDIATION_OUTCOME_UNKNOWN",
                "action": action,
            }
        if started != "STARTED":
            return refused(started)
        return triggered(selected, previous_started)

    def _notify_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Queue one bounded, redacted operator notification (Telegram via the outbox)."""
        plan = StagingGitOpsPlan.model_validate(payload.get("plan"))
        _, flow = enabled_staging_flow(self.settings.project_config_root, plan.project)
        event = str(payload.get("event") or "")
        if event not in {"detected", "remediation", "escalated"}:
            raise ValueError("sync notification event is invalid")
        failure_class = str(payload.get("failure_class") or "OTHER")
        if failure_class not in SYNC_FAILURE_CLASSES:
            failure_class = "OTHER"

        def bounded(key: str, pattern: str) -> str | None:
            value = str(payload.get(key) or "")
            return value if re.fullmatch(pattern, value) else None

        action = bounded("action", r"[a-z_]{1,20}")
        result = bounded("result", r"[A-Z][A-Z0-9_]{0,79}")
        blocker = bounded("blocker", r"[A-Z][A-Z0-9_]{0,79}")
        issue_url = bounded(
            "issue_url", r"https://github\.com/gntik-ai/[a-z-]{1,40}/issues/[0-9]{1,9}"
        )
        round_number = payload.get("round") if isinstance(payload.get("round"), int) else None
        diagnosis = payload.get("diagnosis")
        diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
        resources: list[str] = []
        for key in ("failed_resources", "degraded_resources", "prune_required_resources"):
            entries = diagnosis.get(key)
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict) and len(resources) < 10:
                    identity = resource_identity(entry)
                    entry_class = str(
                        entry.get("failure_class")
                        or ("PRUNE_REQUIRED" if key == "prune_required_resources" else "OTHER")
                    )[:20]
                    resources.append(
                        f"{identity['kind']}/{identity['namespace']}/{identity['name']}"[:200]
                        + f" {entry_class}"
                    )
        raw_targets = payload.get("resources")
        for raw in raw_targets if isinstance(raw_targets, list) else []:
            if isinstance(raw, dict) and len(resources) < 10:
                identity = resource_identity(raw)
                resources.append(
                    f"{identity['kind']}/{identity['namespace']}/{identity['name']}"[:200]
                )
        argo_message = self._bounded_message(diagnosis.get("message"))
        issue = f"{plan.issue_repository}#{plan.issue_number}"
        application = flow.argo_application
        if event == "detected":
            outcome = (
                "did not become Synced and Healthy in time"
                if blocker == "ARGO_SYNC_TIMEOUT"
                else "left resources that require pruning"
                if blocker == "ARGO_PRUNE_REQUIRED"
                else "failed"
            )
            message = (
                f"Argo CD sync of {application} {outcome} for {issue} (class {failure_class}, "
                f"{len(resources)} resource(s)). Hermes is diagnosing and applying bounded "
                "remediation."
            )
        elif event == "remediation":
            message = (
                f"Argo CD remediation {action or 'unknown'} on {application} for {issue} "
                f"(round {round_number or '?'}): {result or 'UNKNOWN'}."
            )
        else:
            message = (
                f"Argo CD sync of {application} for {issue} needs the operator "
                f"({blocker or 'ARGO_SYNC_FAILED'}, class {failure_class}): "
                f"{issue_url or 'the issue could not be written'}."
            )
        notification = self.redactor.redact(
            {
                "message": message,
                "project": plan.project,
                "argo_application": application,
                "change_set_id": str(plan.change_set_id),
                "issue": issue,
                "event": event,
                "failure_class": failure_class,
                "resources": resources,
                "action": action,
                "round": round_number,
                "result": result,
                "blocker": blocker,
                "issue_url": issue_url,
                "argo_message": argo_message,
            }
        ).value
        with self.sessions.begin() as session:
            session.add(
                db.NotificationOutbox(
                    topic=SYNC_NOTIFICATION_TOPIC,
                    status="PENDING",
                    payload_json=notification,
                    attempts=0,
                    next_attempt_at=datetime.now(UTC),
                    delivered_at=None,
                    last_error_code=None,
                )
            )
        return {"status": "NOTIFIED", "topic": SYNC_NOTIFICATION_TOPIC, "event": event}

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
                    # NEW-145: a reconciliation proves the PR still targets these.
                    "head_branch": str(pull_request.get("headRefName") or ""),
                    "base_branch": str(pull_request.get("baseRefName") or ""),
                    # NEW-147 review: and that its head branch is the repository's own.
                    "head_repository": (
                        pull_request_head_repository(pull_request) or ""
                        if pull_request.get("isCrossRepository") is False
                        else ""
                    ),
                }
            )
        result: dict[str, Any] = {"status": "OK", "heads": heads}
        listings = payload.get("branch_listings")
        if listings is not None:
            result["listings"] = self._branch_listings(listings)
        return result

    def _branch_listings(self, raw: Any) -> list[dict[str, Any]]:
        """NEW-145b: every PR (any state, any base) whose head is each named branch."""
        if not isinstance(raw, list) or not 1 <= len(raw) <= 2:
            raise ValueError("pull request branch listing request is invalid")
        listings: list[dict[str, Any]] = []
        for item in raw:
            repository = str(item.get("repository") if isinstance(item, dict) else "")
            branch = str(item.get("branch") if isinstance(item, dict) else "")
            if repository not in _LISTING_REPOSITORIES or not _AGENT_BRANCH.fullmatch(branch):
                raise ValueError("pull request branch listing is outside the allowlist")
            pull_requests = []
            for value in self.broker.pull_requests_by_head(repository, branch):
                state = str(value.get("state") or "").upper()
                if state == "CLOSED" and value.get("merged_at"):
                    state = "MERGED"
                pull_requests.append(
                    {
                        "number": value.get("number"),
                        "url": str(value.get("url") or ""),
                        "state": state,
                        "base_branch": str(value.get("base_ref") or ""),
                        "head_sha": str(value.get("head_sha") or ""),
                        "created_at": str(value.get("created_at") or ""),
                    }
                )
            listings.append(
                {"repository": repository, "branch": branch, "pull_requests": pull_requests}
            )
        return listings

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
        floor = (int(own_match[1]), int(own_match[2]), int(own_match[3])) if own_match else None
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
        if action == "report_sync":
            return self._report_sync_failure(plan, flow, payload, issue_number)
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

    def _report_sync_failure(
        self,
        plan: StagingGitOpsPlan,
        flow: StagingFlowPolicy,
        payload: dict[str, Any],
        issue_number: int | None,
    ) -> dict[str, Any]:
        """NEW-113: open (or comment on) one issue with the Argo CD sync diagnosis."""
        repository = plan.source_repository
        binding = f"changeset:{plan.change_set_id}"
        merge_sha = self._merge_sha(payload)
        failure_class = str(payload.get("failure_class") or "")
        if failure_class not in SYNC_FAILURE_CLASSES:
            return {"status": "BLOCKED", "blocker": "ENVIRONMENT_ISSUE_CLASS_INVALID"}
        blocker = str(payload.get("blocker") or "")
        if blocker not in {"ARGO_SYNC_FAILED", "ARGO_SYNC_TIMEOUT", "ARGO_PRUNE_REQUIRED"}:
            blocker = "ARGO_SYNC_FAILED"
        diagnosis = payload.get("diagnosis")
        attempts = payload.get("attempts")
        title = f"Hermes staging Argo CD sync failed for change {plan.change_set_id}"
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
        suffix = f"{merge_sha[:12]}-{failure_class.lower().replace('_', '-')}"
        marker = (
            f"<!-- engineering-control-staging-gitops:argo-sync:{plan.change_set_id}:{suffix} -->"
        )
        body = render_sync_failure(
            plan,
            flow,
            merge_sha=merge_sha,
            failure_class=failure_class,
            blocker=blocker,
            diagnosis=diagnosis if isinstance(diagnosis, dict) else {},
            attempts=attempts if isinstance(attempts, list) else [],
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
                    raise ValueError("created sync failure issue URL is invalid")
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
            "issue_url": f"https://github.com/{repository}/issues/{issue_number}",
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
        "### ExternalSecrets not Ready",
        *(rows("eso_objects_not_ready", ("namespace", "name", "reason")) or ["- none"]),
        *_render_sealed_backends(diagnosis.get("sealed_backend_stores")),
        "",
        "Bounded remediation: Argo CD hard refresh + sync, deletion of failed pods so their "
        "controllers recreate them, and an external-secrets controller restart when a "
        "secret store or ExternalSecret is not Ready (never while a secret backend is "
        "sealed). No secret values are included in this report.",
    ]
    return "\n".join(lines)


# NEW-155: store name -> (product, namespace, recovery secret, pod, CLI) of the known
# sealed-backend recoveries; any other store gets the generic instruction only.
_UNSEAL_PROCEDURES = {
    "openbao-backend": ("falcone OpenBao", "secret-store", "openbao-recovery", "openbao-0", "bao"),
    "vault-backend": ("musematic Vault", "vault", "vault-recovery", "vault-0", "vault"),
    "vault-musematic": ("musematic Vault", "vault", "vault-recovery", "vault-0", "vault"),
}


def _render_sealed_backends(value: Any) -> list[str]:
    """NEW-155: operator guidance for sealed secret backends; store names only."""
    names = [
        str(name)
        for name in (value if isinstance(value, list) else [])[:MAXIMUM_DIAGNOSED_RESOURCES]
        if isinstance(name, str) and _KUBERNETES_NAME.fullmatch(name)
    ]
    if not names:
        return []
    lines = [
        "",
        "### Secret backends sealed",
        "The backend (OpenBao/Vault) of these secret stores is sealed: "
        + ", ".join(f"`{name}`" for name in names)
        + ". Hermes cannot unseal it and does not restart External Secrets while it is sealed.",
        "",
        "Operator: unseal each sealed backend with 3 unseal keys, then retry the loop; Hermes "
        "restarts External Secrets itself once the stores are Ready.",
    ]
    procedures = sorted({_UNSEAL_PROCEDURES[name] for name in names if name in _UNSEAL_PROCEDURES})
    for product, namespace, secret, pod, cli in procedures:
        lines.extend(
            [
                "",
                f"{product}:",
                "```sh",
                "for i in 1 2 3; do",
                f"  kubectl -n {namespace} get secret {secret} "
                '-o jsonpath="{.data.unseal$i}" | base64 -d \\',
                f"    | kubectl -n {namespace} exec -i {pod} -- "
                f"sh -c 'read -r k; {cli} operator unseal \"$k\"'",
                "done",
                "```",
            ]
        )
    return lines


def _sync_attempts(value: Any) -> list[dict[str, Any]]:
    """NEW-113: bounded record of the Argo CD sync remediations a delivery tried."""
    attempts: list[dict[str, Any]] = []
    for item in value[:MAXIMUM_SYNC_REMEDIATION_ROUNDS] if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "")
        failure_class = str(item.get("failure_class") or "")
        result = str(item.get("result") or "")
        resources = item.get("resources")
        attempts.append(
            {
                "round": item.get("round") if isinstance(item.get("round"), int) else None,
                "action": action if action in SYNC_REMEDIATION_ACTIONS else None,
                "failure_class": failure_class if failure_class in SYNC_FAILURE_CLASSES else None,
                "result": result if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", result) else None,
                "resources": [
                    str(key)[:600] for key in (resources if isinstance(resources, list) else [])
                ][:MAXIMUM_REMEDIATED_RESOURCES],
            }
        )
    return attempts


_RENDER_REDACTOR = SecretRedactor()


def _render_prune_required(items: list[dict[str, Any]]) -> list[str]:
    """NEW-118: per resource, its identity, read-only verification and operator guidance.

    Commands are built only from validated Kubernetes identifiers; anything else gets no
    command at all. Hermes runs none of them.
    """
    lines: list[str] = []
    for item in items:
        identity = resource_identity(item)
        kind, group = identity["kind"], identity["group"]
        namespace, name = identity["namespace"], identity["name"]
        lines.append(
            f"- {_code(kind, 63)} {_code(namespace, 63)}/{_code(name, 120)} "
            f"(Argo CD tracking id {_code(item.get('tracking_id'), 400)}); safe-kind "
            f"allowlist: {'yes' if item.get('safe_kind') is True else 'no'} (informational)"
        )
        valid = (
            _KIND.fullmatch(kind)
            and (group == "" or _GROUP.fullmatch(group))
            and _KUBERNETES_NAME.fullmatch(namespace)
            and _KUBERNETES_NAME.fullmatch(name)
        )
        if not valid:
            lines.append("  - requires manual review; do not delete blindly.")
            continue
        resource = f"{kind.lower()}.{group}" if group else kind.lower()
        lines.append(
            f"  - verify (read-only): `kubectl -n {namespace} get {resource}/{name} -o yaml` "
            f"and `kubectl -n {namespace} get deployments,statefulsets,daemonsets,jobs,"
            f"cronjobs,pods -o yaml | grep -n -F -- '{name}'`"
        )
        if item.get("safe_kind") is True and (group, kind) in SAFE_PRUNE_KINDS:
            lines.append(
                "  - prune (run only after verifying it is unused): "
                f"`kubectl -n {namespace} delete {resource}/{name}`"
            )
        else:
            lines.append("  - requires manual review; do not delete blindly.")
    return lines


def _code(value: Any, limit: int) -> str:
    """Inline-code-safe, single-line, bounded text for an issue body.

    NEW-113: redact the original text before flattening it (line-anchored patterns).
    """
    redacted = str(_RENDER_REDACTOR.redact_text(str(value or "")).value)
    return "`" + " ".join(redacted.split()).replace("`", "'")[:limit] + "`"


def render_sync_failure(
    plan: StagingGitOpsPlan,
    flow: StagingFlowPolicy,
    *,
    merge_sha: str,
    failure_class: str,
    blocker: str,
    diagnosis: dict[str, Any],
    attempts: list[dict[str, Any]],
) -> str:
    """NEW-113: secret-free Argo CD sync diagnosis: resources, classes, what was tried."""

    def entries(key: str) -> list[dict[str, Any]]:
        values = diagnosis.get(key)
        return (
            [item for item in values[:10] if isinstance(item, dict)]
            if isinstance(values, list)
            else []
        )

    failed = [
        f"- {_code(item.get('kind'), 63)} {_code(item.get('namespace'), 63)}/"
        f"{_code(item.get('name'), 120)} class={_code(item.get('failure_class'), 20)} "
        f"status={_code(item.get('status'), 40)}: {_code(item.get('message'), 300)}"
        for item in entries("failed_resources")
    ]
    degraded: list[str] = []
    for item in entries("degraded_resources"):
        reasons = item.get("pod_reasons")
        reason_text = ", ".join(
            f"{str(reason.get('pod'))[:120]}/{str(reason.get('container'))[:63]}: "
            f"{str(reason.get('reason'))[:60]}"
            for reason in (reasons[:5] if isinstance(reasons, list) else [])
            if isinstance(reason, dict)
        )
        degraded.append(
            f"- {_code(item.get('kind'), 63)} {_code(item.get('namespace'), 63)}/"
            f"{_code(item.get('name'), 120)} health={_code(item.get('health'), 40)} "
            f"class={_code(item.get('failure_class'), 20)}"
            + (f" pods: {_code(reason_text, 300)}" if reason_text else "")
        )
    out_of_sync = [
        f"- {_code(item.get('kind'), 63)} {_code(item.get('namespace'), 63)}/"
        f"{_code(item.get('name'), 120)} status={_code(item.get('status'), 40)}"
        for item in entries("out_of_sync_resources")
    ]
    prune_required = _render_prune_required(entries("prune_required_resources"))
    prune_total = diagnosis.get("prune_required_resources_total")
    listed = len(entries("prune_required_resources"))
    if isinstance(prune_total, int) and prune_total > listed:
        prune_required.append(f"- ... and {prune_total - listed} more")
    out_of_sync_total = diagnosis.get("out_of_sync_resources_total")
    if isinstance(out_of_sync_total, int) and out_of_sync_total > len(out_of_sync):
        out_of_sync.append(f"- ... and {out_of_sync_total - len(out_of_sync)} more")
    scope = diagnosis.get("operation_scope")
    scope = scope if isinstance(scope, dict) else {}
    scoped_total = scope.get("resources_total")
    scope_line = (
        "- Last operation scope: "
        + (
            f"selective ({scoped_total} resource(s))"
            if isinstance(scoped_total, int) and scoped_total > 0
            else "full"
        )
        + f", strategy={_code(scope.get('strategy'), 10)}, syncOptions="
        + _code(
            ",".join(str(option) for option in scope.get("sync_options") or [])
            if isinstance(scope.get("sync_options"), list)
            else "",
            200,
        )
    )
    tried = [
        f"- round {item.get('round')}: {_code(item.get('action'), 20)} "
        f"({_code(item.get('failure_class'), 20)}) on "
        + (", ".join(_code(key, 200) for key in item.get("resources") or []) or "the application")
        + f" -> {_code(item.get('result'), 80)}"
        for item in _sync_attempts(attempts)
    ]
    lines = [
        f"Hermes could not complete the Argo CD sync of `{flow.argo_application}` "
        f"(blocker `{blocker}`, failure class `{failure_class}`).",
        "",
        f"- Change set: `{plan.change_set_id}`",
        f"- Source PR: {plan.source_pr_url}",
        f"- Delivered issue: {plan.issue_repository}#{plan.issue_number}",
        f"- Revision: `{merge_sha}`",
        f"- Argo CD: phase={_code(diagnosis.get('phase'), 40)} "
        f"sync={_code(diagnosis.get('sync'), 40)} health={_code(diagnosis.get('health'), 40)}",
        f"- Argo CD message: {_code(diagnosis.get('message'), 300)}",
        *([scope_line] if scope else []),
        "",
        "### Remediation tried",
        *(tried or ["- none (this failure class is not remediated automatically)"]),
        "",
        "Bounded remediation: a patch-incompatible change is re-synced once per resource with "
        "`Replace=true`, an immutable Job once with `Replace=true,Force=true`, and transient "
        "API errors are re-synced at most 3 times, and a full sync that ran with an inherited "
        "selective scope is re-synced once as a true full sync; nothing is ever pruned or "
        "deleted by Hermes. Everything else needs the operator. No secret values are "
        "included in this report.",
        "",
        "### Failed resources",
        *(failed or ["- none"]),
        "",
        "### Degraded resources",
        *(degraded or ["- none"]),
        "",
        "### OutOfSync resources",
        *(out_of_sync or ["- none"]),
        "",
        "### Requires prune",
        *(
            [
                "Argo CD would prune these resources (the delivered change removed them); "
                "Hermes never prunes or deletes. Verify each one is unused before acting.",
                "",
                *prune_required,
            ]
            if prune_required
            else ["- none"]
        ),
    ]
    # gh comments are bounded to 12,000 bytes including the marker.
    return "\n".join(lines).encode()[:11_000].decode(errors="ignore")
