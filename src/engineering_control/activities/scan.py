from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from temporalio import activity
from temporalio.exceptions import ApplicationError

from engineering_control.activities.retention import cleanup_authorization_sha256
from engineering_control.activities.ui_plan_scan import UI_PLAN_SCAN_STEPS, UiPlanScanSteps
from engineering_control.adapters.command import CommandFailure, SafeCommandRunner
from engineering_control.adapters.kubernetes import KubernetesObserver
from engineering_control.adapters.scanning.tooling import DeterministicScanner
from engineering_control.config import Settings
from engineering_control.engineering.configuration import EngineeringCatalog
from engineering_control.engineering.models import (
    ProjectPairSnapshot,
    ToolchainPolicy,
    ToolchainTestCommand,
    UiMigrationPolicy,
)
from engineering_control.engineering.node_test_baseline import (
    EVIDENCE_TEST_NAMES,
    MAX_REPORT_BYTES,
    REPORTER_SOURCE,
    REPORTER_VERSION,
    NodeTestCommand,
    bounded_names,
    expand_test_paths,
    parse_node_test_command,
    parse_reporter_output,
    sandbox_argv,
)
from engineering_control.engineering.safe_files import (
    open_directory_no_follow,
    write_text_at,
)
from engineering_control.engineering.toolchain import (
    NodeToolchain,
    ToolchainUnavailableError,
    enabled_toolchain,
    load_toolchain_policy,
    match_toolchain_test_command,
    payload_node_toolchain,
    prepare_executables,
    render_argv,
    resolve_node_toolchain,
)
from engineering_control.engineering.ui_inventory import (
    SYMLINK_ROOT,
    InventoryError,
    build_inventory,
    check_git_scan_roots,
    inventory_content_key,
    render_markdown,
)
from engineering_control.engineering.ui_kit import (
    UI_MCP_UNAVAILABLE,
    GntikUiKitClient,
    KitSession,
    KitUnavailable,
    collect_kit_snapshot,
    snapshot_plan,
)
from engineering_control.evidence.transfer import EvidencePublisher
from engineering_control.identity.attestation import observer_contract_hash
from engineering_control.models.enums import Project, Provider, RedactionStatus
from engineering_control.models.reconciliation import PromotionPlan
from engineering_control.projects import (
    PROJECT_ALTERNATION,
    PROJECT_REPOSITORY_SLUGS,
    PROJECT_SET,
    PROJECTS,
    REPOSITORY_SLUG_ALTERNATION,
    SEPARATE_DEPLOYMENT_REPOSITORY_SLUGS,
)
from engineering_control.security.redaction import SecretRedactor
from engineering_control.worktrees.agent_config import is_agent_config_path
from engineering_control.worktrees.manager import (
    GitWorktreeManager,
    WorktreeAccess,
    WorktreeAllocation,
    WorktreeError,
)

_T = TypeVar("_T")
_TOOLCHAIN_STDERR_TAIL = 2000
_TOOLCHAIN_HEARTBEAT_SECONDS = 30.0
_BASELINE_TEST_TIMEOUT_SECONDS = 1200
_BASELINE_OUTPUT_TAIL = 500
# NEW-161: maker-chosen `node --test` suites (base only, sandboxed). The
# per-run bound matches the contract baseline and is never below the 900s integration
# suites need; the shared budget keeps validate_changeset inside its 120-minute timeout.
_NODE_TEST_MAX_COMMANDS = 3
_NODE_TEST_TIMEOUT_SECONDS = 1200
_NODE_TEST_MIN_SECONDS = 900
_NODE_TEST_BUDGET_SECONDS = 4800
_NODE_TEST_OUTPUT_BYTES = 256 * 1024
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


_MAX_RESULT_NAMES = 64
_MAX_RESULT_NAME_CHARS = 64
_MAX_RESULT_NAMES_BYTES = 4096


def _bounded_names(values: Any) -> list[str]:
    """At most 64 names of at most 64 characters and 4 KiB in total (Temporal payloads)."""
    names: list[str] = []
    total = 0
    for value in values if isinstance(values, list) else []:
        name = str(value)[:_MAX_RESULT_NAME_CHARS]
        total += len(name.encode("utf-8"))
        if len(names) >= _MAX_RESULT_NAMES or total > _MAX_RESULT_NAMES_BYTES:
            break
        names.append(name)
    return names


class WorktreeAllocationRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    change_set_id: UUID
    project: Project
    issue: str = Field(min_length=1, max_length=80)
    source_repository: str
    source_base_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_base_ref: str = "refs/heads/main"
    deployment_repository: str
    deployment_base_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    deployment_base_ref: str = "refs/heads/main"
    maker_provider: Provider
    checker_provider: Provider
    # NEW-117: approved import heads; a base other than them must fast-forward from them.
    source_approved_base_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    deployment_approved_base_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")


class RuntimeObservationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project: Project
    deployment_id: UUID
    change_set_id: UUID
    kubeconfig_path: Path
    kube_context: str = Field(min_length=1, max_length=253)
    namespace: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    resources: tuple[str, ...] = Field(min_length=1, max_length=10)
    source_worktree: Path
    deployment_worktree: Path
    expected_source_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    expected_deployment_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    expected_image_digests: dict[str, str]
    revision_annotation: str | None = None
    argo_application: str | None = None

    @model_validator(mode="after")
    def bounded_observation(self) -> RuntimeObservationPlan:
        allowed = KubernetesObserver._resources - {"services", "ingresses", "cronjobs", "events"}
        if len(set(self.resources)) != len(self.resources) or set(self.resources) - allowed:
            raise ValueError("runtime observation resources are not allowlisted and unique")
        if not self.expected_image_digests or any(
            not re.fullmatch(r"sha256:[a-f0-9]{64}", value)
            for value in self.expected_image_digests.values()
        ):
            raise ValueError("runtime observation requires immutable expected image digests")
        if self.project is Project.FALCONE and not self.revision_annotation:
            raise ValueError("Falcone runtime observation requires a revision annotation")
        if self.project is Project.MUSEMATIC and not self.argo_application:
            raise ValueError("Musematic runtime observation requires an Argo application")
        return self


class CleanupCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lease_id: UUID
    change_set_id: UUID
    repository: str = Field(pattern=rf"^({REPOSITORY_SLUG_ALTERNATION})$")
    worktree_path: Path
    branch: str = Field(pattern=rf"^agent/({PROJECT_ALTERNATION})/[a-zA-Z0-9._-]+/[a-f0-9-]{{36}}$")
    base_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    reason: str = Field(
        pattern=r"^(RELEASED_RETENTION_EXPIRED|ABANDONED_LEASE_EXPIRED|ABANDONED_CHANGESET_STALE)$"
    )


class ScanActivityHandler(UiPlanScanSteps):
    """Bounded mirror and worktree operations owned only by scan-runner."""

    supported_steps = frozenset(
        {
            "project.snapshot",
            "project.snapshot_pair",
            "deployment.compare_live",
            "audit.static_source",
            "audit.static_deployment",
            "audit.kubernetes_observe",
            "audit.correlate_runtime",
            "audit.deterministic_cell",
            "finding.fresh_snapshot",
            "delivery.allocate_worktrees",
            "delivery.deterministic_prechecks",
            "delivery.prepare_toolchain",
            "delivery.resolve_dependencies",
            "delivery.converge_deployment_pin",
            "runtime.deterministic_checks",
            "identity.observer_attest",
            "cleanup.protect_active",
            "cleanup.execute",
            "cleanup.prune_orphan_worktrees",
            "reconcile.apply_promotion",
            "observability.prepare_workspace",
            "observability.cleanup_workspace",
            "ui.inventory",
            "ui.kit_snapshot",
            *UI_PLAN_SCAN_STEPS,
        }
    )
    _repositories = {Project(name): PROJECT_REPOSITORY_SLUGS[name] for name in PROJECTS}

    def __init__(
        self,
        settings: Settings,
        manager: GitWorktreeManager | None = None,
        scanner: DeterministicScanner | None = None,
        publisher: EvidencePublisher | None = None,
        observer_runner: SafeCommandRunner | None = None,
        promotion_runner: SafeCommandRunner | None = None,
        toolchain_runner: SafeCommandRunner | None = None,
        dependency_runner: SafeCommandRunner | None = None,
        git_runner: SafeCommandRunner | None = None,
        baseline_runner: SafeCommandRunner | None = None,
        kit_client_factory: Callable[[UiMigrationPolicy], KitSession] | None = None,
    ) -> None:
        self.settings = settings
        # NEW-121 v2: Hermes itself queries the pinned gntik-ui MCP image (no model).
        self.kit_client_factory = kit_client_factory or GntikUiKitClient.from_policy
        self.manager = manager or GitWorktreeManager(
            mirror_root=settings.mirror_root,
            worktree_root=settings.worktree_root,
            lease_root=settings.lease_root / "worktrees",
            metadata_root=settings.worktree_metadata_root,
            # NEW-61: allocation default ACLs and the post-validation delivery grant add
            # read on the ChangeSet Git metadata for the controller's SSH fetch.
            publication_transport_identity=(
                settings.publication_transport_identity
                if settings.publication_fetch_from_worker
                else None
            ),
            transport_syncs_mirrors=settings.mirror_sync_to_worker,
        )
        self.scanner = scanner or DeterministicScanner(
            SafeCommandRunner(
                {
                    "gitleaks": settings.gitleaks_executable,
                    "trivy": settings.trivy_executable,
                    "semgrep": settings.semgrep_executable,
                    "syft": settings.syft_executable,
                    "kubeconform": settings.kubeconform_executable,
                }
            )
        )
        self.publisher = publisher or EvidencePublisher(settings, settings.evidence_root)
        self.observer_runner = observer_runner or SafeCommandRunner(
            {"kubectl": settings.kubectl_executable}
        )
        self.promotion_runner = promotion_runner or SafeCommandRunner(
            {"git": settings.git_executable, "helm": settings.helm_executable}
        )
        # delivery.prepare_toolchain: only the allowlisted installers, never a shell.
        self.toolchain_runner = toolchain_runner or SafeCommandRunner(prepare_executables(settings))
        # NEW-75: go + pnpm for delivery.resolve_dependencies only.
        self.dependency_runner = dependency_runner or SafeCommandRunner(
            {"go": settings.go_executable, "pnpm": settings.toolchain_pnpm_executable}
        )
        # NEW-159: a project selecting another node/pnpm gets per-call runners whose pnpm
        # is the selected one; injected runners (tests) are always used as given.
        self._runners_injected = toolchain_runner is not None, dependency_runner is not None
        self.git_runner = git_runner or SafeCommandRunner({"git": settings.git_executable})
        # NEW-49: None builds a per-command allowlist (see _baseline_executables).
        self.baseline_runner = baseline_runner
        self.catalog = EngineeringCatalog(
            project_root=settings.project_config_root,
            persona_root=settings.persona_config_root,
            flow_root=settings.flow_config_root,
            credential_root=settings.credential_root,
            attestation_root=settings.credential_attestation_root,
            browser_worker_identity=settings.browser_worker_identity,
        )

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if step == "project.snapshot":
                return await asyncio.to_thread(self._snapshot, payload)
            if step == "project.snapshot_pair":
                return await asyncio.to_thread(self._snapshot_pair, payload)
            if step in {"audit.static_source", "audit.static_deployment"}:
                return await asyncio.to_thread(self._static_surface, step, payload)
            if step == "deployment.compare_live":
                return await asyncio.to_thread(self._compare_live, payload)
            if step == "audit.kubernetes_observe":
                return await asyncio.to_thread(self._kubernetes_observe, payload)
            if step == "audit.correlate_runtime":
                return await asyncio.to_thread(self._correlate_runtime, payload)
            if step == "audit.deterministic_cell":
                return await asyncio.to_thread(self._deterministic_cell, payload)
            if step == "finding.fresh_snapshot":
                return await asyncio.to_thread(self._fresh_finding_snapshot, payload)
            if step == "delivery.allocate_worktrees":
                return await asyncio.to_thread(self._allocate, payload)
            if step == "delivery.deterministic_prechecks":
                return await asyncio.to_thread(self._delivery_prechecks, payload)
            if step == "delivery.prepare_toolchain":
                return await self._with_heartbeat(
                    asyncio.to_thread(self._prepare_toolchain, payload)
                )
            if step == "delivery.resolve_dependencies":
                return await self._with_heartbeat(
                    asyncio.to_thread(self._resolve_dependencies, payload)
                )
            if step == "delivery.converge_deployment_pin":
                return await asyncio.to_thread(self._converge_deployment_pin, payload)
            if step == "runtime.deterministic_checks":
                return await asyncio.to_thread(self._runtime_deterministic_checks, payload)
            if step == "identity.observer_attest":
                return await asyncio.to_thread(self._observer_attestation, payload)
            if step == "cleanup.protect_active":
                return await asyncio.to_thread(self._protect_cleanup_candidates, payload)
            if step == "cleanup.execute":
                return await asyncio.to_thread(self._execute_cleanup, payload)
            if step == "cleanup.prune_orphan_worktrees":
                return await asyncio.to_thread(self._prune_orphan_worktrees, payload)
            if step == "reconcile.apply_promotion":
                return await asyncio.to_thread(self._apply_promotion, payload)
            if step == "delivery.validate_changeset":
                # NEW-49: may run the maker-test baseline (prepare + tests) at the base commit.
                return await self._with_heartbeat(
                    asyncio.to_thread(self._validate_changeset, payload),
                    phase="changeset_validation",
                )
            if step == "observability.prepare_workspace":
                return await asyncio.to_thread(self._prepare_observability_workspace, payload)
            if step == "observability.cleanup_workspace":
                return await asyncio.to_thread(self._cleanup_observability_workspace, payload)
            if step == "ui.inventory":
                return await asyncio.to_thread(self._ui_inventory, payload)
            # NEW-123: model input staging and blueprint evidence (no model runs here).
            if step == "ui_plan.stage_inputs":
                return await asyncio.to_thread(self._ui_plan_stage_inputs, payload)
            if step == "ui_plan.stage_document":
                return await asyncio.to_thread(self._ui_plan_stage_document, payload)
            if step == "ui_plan.stage_parts":
                return await asyncio.to_thread(self._ui_plan_stage_parts, payload)
            if step == "ui.kit_snapshot":
                return await self._with_heartbeat(
                    self._ui_kit_snapshot(payload), phase="ui_kit_snapshot"
                )
        except PermissionError as error:
            raise ApplicationError(
                "runtime observer credential violates least-privilege policy",
                type="SecurityBlocker",
                non_retryable=True,
            ) from error
        except CommandFailure as error:
            raise ApplicationError(
                "runtime observation command failed",
                type="RuntimeObservationUnavailable",
            ) from error
        except (OSError, ValueError, ValidationError, WorktreeError) as error:
            message = str(error)
            error_type = "DirtyWorktree" if "dirty" in message.lower() else "PolicyViolation"
            raise ApplicationError(message, type=error_type, non_retryable=True) from error
        raise ApplicationError(
            f"{step} has no configured deterministic scan handler",
            type="NotConfigured",
            non_retryable=True,
        )

    def _observability_workspace(self, payload: dict[str, Any]) -> tuple[Path, Path]:
        workspace_id = UUID(str(payload["workspace_id"]))
        root = self.settings.worktree_root.resolve(strict=True)
        parent = root / "observability-canary"
        target = parent / str(workspace_id)
        if target.parent != parent or parent.parent != root:
            raise ValueError("observability workspace escaped its fixed boundary")
        return parent, target

    def _prepare_observability_workspace(self, payload: dict[str, Any]) -> dict[str, Any]:
        parent, target = self._observability_workspace(payload)
        parent.mkdir(mode=0o755, exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("observability workspace parent is unsafe")
        if target.exists():
            if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
                raise ValueError("observability workspace is not an empty real directory")
        else:
            target.mkdir(mode=0o755)
        # Provider workers have distinct Unix identities and require traverse-only
        # access to this empty, read-only canary boundary.
        os.chmod(parent, 0o755)  # noqa: S103
        os.chmod(target, 0o755)  # noqa: S103
        return {
            **payload,
            "workspace": str(target),
            "workspace_status": "READY",
            "status": "PREPARING",
        }

    def _cleanup_observability_workspace(self, payload: dict[str, Any]) -> dict[str, Any]:
        parent, target = self._observability_workspace(payload)
        if target.exists():
            if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
                raise ValueError("observability workspace cleanup requires an empty real directory")
            target.rmdir()
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("observability workspace parent is unsafe")
        return {**payload, "workspace_cleanup_status": "COMPLETE"}

    def _apply_promotion(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = PromotionPlan.model_validate(payload.get("promotion_plan"))
        try:
            worktree = self._promotion_worktree(plan)
            git_arguments = ["-c", f"safe.directory={worktree}"]
            branch = self.promotion_runner.run(
                "git", [*git_arguments, "branch", "--show-current"], cwd=worktree
            ).stdout.strip()
            head = self.promotion_runner.run(
                "git", [*git_arguments, "rev-parse", "HEAD"], cwd=worktree
            ).stdout.strip()
            status = self.promotion_runner.run(
                "git", [*git_arguments, "status", "--porcelain=v1"], cwd=worktree
            ).stdout
            if branch != plan.branch or head != plan.expected_head_sha or status:
                raise ValueError("promotion worktree identity, head, or cleanliness changed")
            deployment_file = self._promotion_file(worktree, plan.deployment_file)
            original = deployment_file.read_text(encoding="utf-8")
            updated = self._replace_yaml_digests(original, plan.digest_paths, plan.image_digests)
            self._atomic_text(deployment_file, updated)
            loaded = yaml.safe_load(deployment_file.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("promoted values file is not a YAML object")
            for component, dotted in plan.digest_paths.items():
                value: Any = loaded
                for key in dotted.split("."):
                    if not isinstance(value, dict) or key not in value:
                        raise ValueError("promoted digest path disappeared after the edit")
                    value = value[key]
                if value != plan.image_digests[component]:
                    raise ValueError("promoted digest does not match the candidate manifest")
            changed = self.promotion_runner.run(
                "git", [*git_arguments, "diff", "--name-only"], cwd=worktree
            ).stdout.splitlines()
            if changed != [plan.deployment_file]:
                raise ValueError("promotion changed files outside the allowlisted values file")
            self.promotion_runner.run("git", [*git_arguments, "diff", "--check"], cwd=worktree)
            lint_sha, rendered_sha, manifest_count, chart_version = self._validate_promoted_chart(
                plan, worktree
            )
            diff = self.promotion_runner.run(
                "git",
                [*git_arguments, "diff", "--unified=0", "--", plan.deployment_file],
                cwd=worktree,
            ).stdout
            self.promotion_runner.run(
                "git", [*git_arguments, "add", "--", plan.deployment_file], cwd=worktree
            )
            self.promotion_runner.run(
                "git",
                [
                    *git_arguments,
                    "-c",
                    "user.name=Engineering Control",
                    "-c",
                    "user.email=engineering-control@local.invalid",
                    "commit",
                    "-m",
                    f"promote immutable candidate for {plan.change_set_id}",
                ],
                cwd=worktree,
            )
            promoted_head = self.promotion_runner.run(
                "git", [*git_arguments, "rev-parse", "HEAD"], cwd=worktree
            ).stdout.strip()
            final_status = self.promotion_runner.run(
                "git", [*git_arguments, "status", "--porcelain=v1"], cwd=worktree
            ).stdout
            if not re.fullmatch(r"[a-f0-9]{40}", promoted_head) or final_status:
                raise ValueError("promotion commit is not an exact clean Git head")
            evidence = {
                "change_set_id": str(plan.change_set_id),
                "base_sha": plan.expected_head_sha,
                "promoted_sha": promoted_head,
                "deployment_file": plan.deployment_file,
                "image_digests": plan.image_digests,
                "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
                "helm_lint_sha256": lint_sha,
                "rendered_manifest_sha256": rendered_sha,
                "rendered_manifest_count": manifest_count,
                "chart_version": chart_version,
            }
            evidence_root = (
                self.settings.evidence_root / "scan" / str(plan.change_set_id) / "promotion"
            )
            evidence_root.mkdir(parents=True, exist_ok=True, mode=0o750)
            evidence_path = evidence_root / "immutable-promotion.json"
            self._atomic_text(
                evidence_path, json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n"
            )
            receipts = self._publish_reports(
                payload, [evidence_path], filename_prefix="immutable-promotion"
            )
            return {
                **payload,
                "status": "PROMOTION_COMMITTED",
                "promoted_head_sha": promoted_head,
                "promotion_evidence": evidence,
                "promotion_artifacts": receipts,
            }
        except (CommandFailure, OSError, ValueError, WorktreeError) as error:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "IMMUTABLE_PROMOTION_VALIDATION_FAILED",
                "error_type": type(error).__name__,
                "worktree_preserved": True,
            }

    def _promotion_worktree(self, plan: PromotionPlan) -> Path:
        root = self.settings.worktree_root.resolve()
        resolved = plan.working_directory.resolve()
        expected = root / plan.repository_slug / str(plan.change_set_id)
        if not resolved.is_dir() or resolved != expected:
            raise ValueError("promotion escaped its exact ChangeSet worktree")
        return resolved

    @staticmethod
    def _promotion_file(worktree: Path, relative: str) -> Path:
        path = (worktree / relative).resolve()
        if not path.is_file() or not path.is_relative_to(worktree):
            raise ValueError("promotion file escaped its bounded worktree")
        return path

    @staticmethod
    def _replace_yaml_digests(
        original: str, digest_paths: dict[str, str], image_digests: dict[str, str]
    ) -> str:
        targets = {path: image_digests[component] for component, path in digest_paths.items()}
        replaced: set[str] = set()
        stack: list[tuple[int, str]] = []
        output: list[str] = []
        key_line = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z0-9_-]+):(?P<tail>.*)$")
        for line in original.splitlines(keepends=True):
            match = key_line.match(line.rstrip("\r\n"))
            if match is None:
                output.append(line)
                continue
            indent = len(match.group("indent"))
            while stack and indent <= stack[-1][0]:
                stack.pop()
            key = match.group("key")
            dotted = ".".join([entry[1] for entry in stack] + [key])
            tail = match.group("tail")
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            if dotted in targets:
                if dotted in replaced or not tail.strip() or tail.lstrip().startswith("#"):
                    raise ValueError("promotion digest path is ambiguous or not a scalar")
                comment = ""
                if " #" in tail:
                    comment = " #" + tail.split(" #", 1)[1]
                output.append(f"{match.group('indent')}{key}: {targets[dotted]}{comment}{ending}")
                replaced.add(dotted)
                continue
            if not tail.strip() or tail.lstrip().startswith("#"):
                stack.append((indent, key))
            output.append(line)
        if replaced != set(targets):
            raise ValueError("promotion values file is missing an allowlisted digest path")
        return "".join(output)

    def _validate_promoted_chart(
        self, plan: PromotionPlan, worktree: Path
    ) -> tuple[str, str, int, str]:
        chart = (worktree / plan.chart_directory).resolve()
        if not chart.is_dir() or not chart.is_relative_to(worktree):
            raise ValueError("promotion chart directory escaped the worktree")
        values: list[Path] = []
        for relative in plan.values_files:
            path = (worktree / relative).resolve()
            if not path.is_file() or not path.is_relative_to(worktree):
                raise ValueError("promotion values file escaped the worktree")
            values.append(path)
        chart_metadata = yaml.safe_load((chart / "Chart.yaml").read_text(encoding="utf-8"))
        if not isinstance(chart_metadata, dict):
            raise ValueError("promotion chart metadata is invalid")
        chart_version = str(chart_metadata.get("version") or "")
        if not chart_version or (
            plan.chart_version is not None and chart_version != plan.chart_version
        ):
            raise ValueError("promotion chart version changed after planning")
        lint_arguments = ["lint", str(chart)]
        template_arguments = [
            "template",
            "engineering-candidate",
            str(chart),
            "--namespace",
            "falcone-preview",
        ]
        for value in values:
            lint_arguments.extend(("--values", str(value)))
            template_arguments.extend(("--values", str(value)))
        lint = self.promotion_runner.run("helm", lint_arguments, cwd=worktree, timeout_seconds=900)
        rendered = self.promotion_runner.run(
            "helm", template_arguments, cwd=worktree, timeout_seconds=900
        )
        missing = [
            digest for digest in plan.image_digests.values() if digest not in rendered.stdout
        ]
        if missing:
            raise ValueError("rendered chart omits candidate image digests")
        documents = [document for document in yaml.safe_load_all(rendered.stdout) if document]
        if not documents:
            raise ValueError("promotion chart rendered no Kubernetes manifests")
        return (
            hashlib.sha256(lint.stdout.encode()).hexdigest(),
            hashlib.sha256(rendered.stdout.encode()).hexdigest(),
            len(documents),
            chart_version,
        )

    def _protect_cleanup_candidates(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_candidates = payload.get("cleanup_candidates")
        if not isinstance(raw_candidates, list):
            raise ValueError("cleanup protection requires candidate records")
        candidates = [CleanupCandidate.model_validate(value) for value in raw_candidates]
        eligible: list[dict[str, Any]] = []
        protected: list[dict[str, Any]] = []
        for candidate in candidates:
            expected = (
                self.settings.worktree_root.resolve()
                / candidate.repository
                / str(candidate.change_set_id)
            )
            path = candidate.worktree_path.resolve()
            record = candidate.model_dump(mode="json")
            if path != expected:
                protected.append({**record, "protection": "PATH_IDENTITY_MISMATCH"})
                continue
            if not path.exists():
                eligible.append({**record, "inspection": "ABSENT"})
                continue
            if not path.is_dir():
                protected.append({**record, "protection": "NOT_A_DIRECTORY"})
                continue
            try:
                branch = self.manager._run(["git", "branch", "--show-current"], path)
                head = self.manager._run(["git", "rev-parse", "HEAD"], path)
                dirty = self.manager.is_dirty(path)
            except WorktreeError:
                protected.append({**record, "protection": "GIT_IDENTITY_UNPROVEN"})
                continue
            if branch != candidate.branch:
                protected.append({**record, "protection": "BRANCH_IDENTITY_MISMATCH"})
                continue
            if dirty:
                protected.append({**record, "protection": "DIRTY_WORKTREE"})
                continue
            eligible.append(
                {
                    **record,
                    "inspection": "CLEAN",
                    "observed_branch": branch,
                    "observed_head_sha": head,
                }
            )
        return {
            **payload,
            "status": "CLEANUP_PROTECTED",
            "eligible_candidates": eligible,
            "protected_candidates": protected,
        }

    def _execute_cleanup(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.cleanup_execution_enabled:
            raise ApplicationError(
                "worktree cleanup execution is disabled by policy",
                type="SecurityBlocker",
                non_retryable=True,
            )
        raw_authorized = payload.get("authorized_candidates")
        raw_files = payload.get("authorized_file_candidates")
        raw_components = payload.get("authorized_component_lease_candidates")
        raw_previews = payload.get("authorized_preview_namespace_candidates", [])
        authorization_sha = payload.get("cleanup_authorization_sha256")
        orchestration = self._find_mapping(payload, "orchestration")
        if (
            not isinstance(raw_authorized, list)
            or not isinstance(raw_files, list)
            or not isinstance(raw_components, list)
            or not isinstance(raw_previews, list)
            or not isinstance(authorization_sha, str)
            or orchestration is None
            or not isinstance(orchestration.get("workflow_id"), str)
        ):
            raise ValueError("cleanup execution requires controller authorization")
        expected_authorization = cleanup_authorization_sha256(
            workflow_id=orchestration["workflow_id"],
            worktrees=raw_authorized,
            files=raw_files,
            component_leases=raw_components,
            preview_namespaces=raw_previews,
        )
        if expected_authorization != authorization_sha:
            raise ValueError("cleanup authorization changed after controller validation")
        removed: list[str] = []
        absent: list[str] = []
        protected: list[dict[str, str]] = []
        for raw in raw_authorized:
            candidate = CleanupCandidate.model_validate(raw)
            allocation = WorktreeAllocation(
                repository=candidate.repository,
                path=candidate.worktree_path,
                branch=candidate.branch,
                base_sha=candidate.base_sha,
                change_set_id=candidate.change_set_id,
            )
            if not allocation.path.exists():
                absent.append(str(candidate.lease_id))
                continue
            try:
                # NEW-85: abandoned attempts are archived (refs/archive/...) then removed;
                # one protected worktree no longer fails the whole cleanup run.
                self.manager.cleanup(
                    allocation,
                    evidence_captured=False,
                    archive_dirty=candidate.reason == "ABANDONED_CHANGESET_STALE",
                )
            except WorktreeError as error:
                protected.append({"lease_id": str(candidate.lease_id), "reason": str(error)[:200]})
                continue
            removed.append(str(candidate.lease_id))
        return {
            **payload,
            "status": "CLEANUP_EXECUTED",
            "removed_lease_ids": removed,
            "already_absent_lease_ids": absent,
            "protected_cleanup_candidates": protected,
        }

    def _prune_orphan_worktrees(self, payload: dict[str, Any]) -> dict[str, Any]:
        """NEW-85: snapshots/acceptance leftovers no lease tracks (dry-run reports only)."""
        dry_run = payload.get("dry_run") is not False or not self.settings.cleanup_execution_enabled
        cutoff = datetime.now(UTC) - timedelta(days=self.settings.cleanup_retention_days)
        # NEW-119: an identity-blocked project's repositories are left untouched.
        excluded = payload.get("cleanup_identity_blocked_repositories", [])
        if not isinstance(excluded, list) or not all(isinstance(item, str) for item in excluded):
            raise ValueError("identity-blocked cleanup repositories are invalid")
        # A leased or inspection-protected path is excluded explicitly, so an
        # unattributable protected checkout is never taken for an orphan snapshot.
        raw_leased = payload.get("leased_worktree_paths", [])
        if not isinstance(raw_leased, list) or not all(
            isinstance(item, str) for item in raw_leased
        ):
            raise ValueError("leased worktree paths are invalid")
        excluded_paths = set(raw_leased)
        for key in ("cleanup_candidates", "eligible_candidates", "protected_candidates"):
            records = payload.get(key, [])
            if not isinstance(records, list):
                raise ValueError("cleanup worktree records are invalid")
            for record in records:
                path = record.get("worktree_path") if isinstance(record, dict) else None
                if not isinstance(path, str) or not path:
                    raise ValueError("cleanup worktree record lacks its path")
                excluded_paths.add(path)
        result = self.manager.prune_orphan_worktrees(
            older_than=cutoff,
            dry_run=dry_run,
            excluded_repositories=frozenset(excluded),
            excluded_paths=frozenset(excluded_paths),
        )
        return {
            **payload,
            "status": "ORPHAN_WORKTREES_INSPECTED" if dry_run else "ORPHAN_WORKTREES_PRUNED",
            "orphan_worktrees": result["removed"][:200],
            "orphan_worktrees_kept": result["kept"][:50],
        }

    def _runtime_deterministic_checks(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_plan = self._find_mapping(payload, "runtime_observation_plan")
        if raw_plan is None:
            raise ValueError("runtime observation requires a typed plan")
        plan = RuntimeObservationPlan.model_validate(raw_plan)
        source_snapshot = self.manager.inspect_snapshot(
            plan.source_worktree,
            expected_sha=plan.expected_source_sha,
        )
        deployment_snapshot = self.manager.inspect_snapshot(
            plan.deployment_worktree,
            expected_sha=plan.expected_deployment_sha,
        )
        kubeconfig = self._observer_credential(plan.kubeconfig_path)
        observer = KubernetesObserver(
            self.observer_runner,
            kubeconfig=kubeconfig,
            context=plan.kube_context,
            working_directory=self.settings.run_root / "scan",
        )
        observer.verify_permissions((plan.namespace,), plan.resources)
        summaries: dict[str, Any] = {}
        revisions: set[str] = set()
        observed_digests: set[str] = set()
        failures: list[str] = []
        for resource in plan.resources:
            name = plan.argo_application if resource == "applications.argoproj.io" else None
            result = observer.get(resource, namespace=plan.namespace, name=name)
            summary = self._summarize_runtime_resource(
                resource,
                json.loads(result.stdout),
                revision_annotation=plan.revision_annotation,
            )
            summaries[resource] = summary
            revisions.update(summary["revisions"])
            observed_digests.update(summary["image_digests"])
            failures.extend(summary["failures"])
        revision_matches = revisions == {plan.expected_deployment_sha}
        expected_digests = set(plan.expected_image_digests.values())
        digests_match = expected_digests.issubset(observed_digests)
        if not revision_matches:
            failures.append("approved deployment revision was not observed exactly")
        if not digests_match:
            failures.append("one or more approved immutable image digests were not observed")
        observation = {
            "status": "RUNTIME_OBSERVATION_PASS" if not failures else "FAIL",
            "expected_revision": plan.expected_deployment_sha,
            "observed_revisions": sorted(revisions),
            "expected_image_digests": dict(sorted(plan.expected_image_digests.items())),
            "observed_image_digests": sorted(observed_digests),
            "revision_matches": revision_matches,
            "digests_match": digests_match,
            "resources": summaries,
            "source_snapshot": source_snapshot,
            "deployment_snapshot": deployment_snapshot,
            "failures": failures,
        }
        evidence_root = self.settings.evidence_root / "scan" / str(plan.change_set_id) / "runtime"
        evidence_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        report = evidence_root / f"{plan.deployment_id}-observation.json"
        safe = SecretRedactor().redact(observation).value
        if not isinstance(safe, dict):
            raise TypeError("redacted runtime observation must remain an object")
        self._atomic_text(report, json.dumps(safe, indent=2, sort_keys=True) + "\n")
        receipts = self._publish_reports(
            payload,
            [report],
            filename_prefix=f"runtime-{plan.deployment_id}",
        )
        return {
            **payload,
            "status": safe["status"],
            "runtime_observation": {**safe, "artifacts": receipts},
        }

    def _observer_credential(self, path: Path) -> Path:
        root = self.settings.observer_credential_root.resolve()
        if path.is_symlink():
            raise ValueError("observer kubeconfig must not be a symlink")
        resolved = path.resolve()
        if not resolved.is_file() or not resolved.is_relative_to(root):
            raise ValueError("observer kubeconfig escaped its credential root")
        metadata = resolved.stat()
        effective_groups = {os.getegid(), *os.getgroups()}
        if not self._observer_mode_valid(
            metadata,
            effective_uid=os.geteuid(),
            effective_groups=effective_groups,
        ):
            raise ValueError("observer kubeconfig permissions are too broad")
        return resolved

    @staticmethod
    def _observer_mode_valid(
        metadata: os.stat_result, *, effective_uid: int, effective_groups: set[int]
    ) -> bool:
        mode = stat.S_IMODE(metadata.st_mode)
        if not stat.S_ISREG(metadata.st_mode):
            return False
        owner_private = metadata.st_uid == effective_uid and mode == 0o600
        root_group_read = (
            metadata.st_uid == 0 and metadata.st_gid in effective_groups and mode == 0o640
        )
        return owner_private or root_group_read

    def _observer_attestation(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_value = str(payload.get("project"))
        if project_value not in PROJECT_SET:
            raise ValueError("observer attestation project is invalid")
        manifest = self.catalog.project(project_value)
        # NEW-160: fails closed (PROJECT_HAS_NO_RUNTIME) for a project without a runtime.
        deployment = manifest.runtime_deployment()
        raw_path = Path(deployment.observer_credential_ref.removeprefix("file:"))
        checked = datetime.now(UTC).isoformat()
        base = {
            "project": project_value,
            "contract_id": f"{project_value}-observer",
            "worker_identity": f"scan-runner@{os.uname().nodename}",
            "checked_timestamp": checked,
            "attestation_ttl_seconds": 300,
            "contract_hash": observer_contract_hash(project_value),
        }
        file_present = raw_path.is_file() and not raw_path.is_symlink()
        try:
            kubeconfig = self._observer_credential(raw_path)
        except (OSError, ValueError):
            return {
                **base,
                "file_present": file_present,
                "parser_valid": False,
                "target_valid": False,
                "permissions_valid": False,
                "forbidden_resource_read_denied": False,
                "mutation_access_denied": False,
                "blocker_code": (
                    "OBSERVER_FILE_PERMISSIONS" if file_present else "OBSERVER_CREDENTIAL_MISSING"
                ),
            }

        observer = KubernetesObserver(
            self.observer_runner,
            kubeconfig=kubeconfig,
            context=deployment.kubernetes_context_ref,
            working_directory=self.settings.run_root / "scan",
        )
        try:
            primary_namespace = deployment.namespaces[0]
            observer.verify_permissions(
                (primary_namespace,), ("deployments", "statefulsets", "pods", "events")
            )
            for namespace in deployment.namespaces[1:]:
                observer.verify_permissions((namespace,), ("pods", "events"))
            if deployment.argocd_application:
                observer.verify_permissions(("argocd",), ("applications.argoproj.io",))
            identity = observer.identity()
        except (CommandFailure, PermissionError, ValueError, json.JSONDecodeError):
            return {
                **base,
                "file_present": True,
                "parser_valid": False,
                "target_valid": False,
                "permissions_valid": False,
                "forbidden_resource_read_denied": False,
                "mutation_access_denied": False,
                "blocker_code": "OBSERVER_AUTHORIZATION_INVALID",
            }
        target_valid = (
            identity.get("context") == deployment.kubernetes_context_ref
            and identity.get("api_server") == deployment.api_server_identity
        )
        return {
            **base,
            "file_present": True,
            "parser_valid": True,
            "target_valid": target_valid,
            "permissions_valid": True,
            "forbidden_resource_read_denied": True,
            "mutation_access_denied": True,
            "blocker_code": "NONE" if target_valid else "OBSERVER_TARGET_INVALID",
        }

    @staticmethod
    def _summarize_runtime_resource(
        resource: str,
        value: Any,
        *,
        revision_annotation: str | None,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("Kubernetes observation returned a non-object")
        items_value = value.get("items")
        raw_items = items_value if isinstance(items_value, list) else [value]
        items: list[dict[str, Any]] = [item for item in raw_items if isinstance(item, dict)]
        revisions: set[str] = set()
        digests: set[str] = set()
        names: list[str] = []
        failures: list[str] = []
        digest_pattern = re.compile(r"sha256:[a-f0-9]{64}")
        for item in items:
            metadata = ScanActivityHandler._mapping_value(item, "metadata")
            spec = ScanActivityHandler._mapping_value(item, "spec")
            status = ScanActivityHandler._mapping_value(item, "status")
            name = str(metadata.get("name", "unknown"))
            names.append(name)
            annotations = ScanActivityHandler._mapping_value(metadata, "annotations")
            if revision_annotation and isinstance(annotations.get(revision_annotation), str):
                revisions.add(str(annotations[revision_annotation]))
            if resource == "applications.argoproj.io":
                sync = ScanActivityHandler._mapping_value(status, "sync")
                health = ScanActivityHandler._mapping_value(status, "health")
                if isinstance(sync.get("revision"), str):
                    revisions.add(str(sync["revision"]))
                if sync.get("status") != "Synced" or health.get("status") != "Healthy":
                    failures.append(f"{resource}/{name} is not Synced and Healthy")
            template = ScanActivityHandler._mapping_value(spec, "template")
            template_spec = ScanActivityHandler._mapping_value(template, "spec")
            pod_spec = template_spec or spec
            containers = pod_spec.get("containers") if isinstance(pod_spec, dict) else []
            if isinstance(containers, list):
                for container in containers:
                    if isinstance(container, dict) and isinstance(container.get("image"), str):
                        digests.update(digest_pattern.findall(str(container["image"])))
            container_statuses = status.get("containerStatuses")
            if isinstance(container_statuses, list):
                for container in container_statuses:
                    if isinstance(container, dict) and isinstance(container.get("imageID"), str):
                        digests.update(digest_pattern.findall(str(container["imageID"])))
            if resource == "deployments":
                desired = int(spec.get("replicas", 1))
                if int(status.get("availableReplicas", 0)) < desired:
                    failures.append(f"deployment/{name} is not fully available")
            elif resource == "statefulsets":
                desired = int(spec.get("replicas", 1))
                if int(status.get("readyReplicas", 0)) < desired:
                    failures.append(f"statefulset/{name} is not fully ready")
            elif resource == "daemonsets":
                desired = int(status.get("desiredNumberScheduled", 0))
                if desired < 1 or int(status.get("numberReady", 0)) < desired:
                    failures.append(f"daemonset/{name} is not fully ready")
            elif resource == "pods" and status.get("phase") not in {"Running", "Succeeded"}:
                failures.append(f"pod/{name} is not running or succeeded")
            elif resource == "jobs":
                desired = int(spec.get("completions", 1))
                if int(status.get("succeeded", 0)) < desired:
                    failures.append(f"job/{name} is incomplete")
        if not items:
            failures.append(f"{resource} returned no objects")
        return {
            "count": len(items),
            "names": sorted(names),
            "revisions": sorted(revisions),
            "image_digests": sorted(digests),
            "failures": failures,
        }

    @staticmethod
    def _mapping_value(value: dict[str, Any], key: str) -> dict[str, Any]:
        candidate = value.get(key)
        return candidate if isinstance(candidate, dict) else {}

    def _deterministic_cell(self, payload: dict[str, Any]) -> dict[str, Any]:
        snapshot = self._find_mapping(payload, "snapshot")
        cell = self._find_mapping(payload, "cell")
        if snapshot is None or cell is None:
            raise ValueError("deterministic audit requires snapshot and cell")
        source = self.manager.inspect_snapshot(
            Path(str(snapshot["source_worktree"])), expected_sha=str(snapshot["source_sha"])
        )
        deployment = self.manager.inspect_snapshot(
            Path(str(snapshot["deployment_worktree"])),
            expected_sha=str(snapshot["deployment_sha"]),
        )
        evidence = {
            "cell": cell,
            "source": source,
            "deployment": deployment,
        }
        canonical = json.dumps(evidence, separators=(",", ":"), sort_keys=True).encode()
        return {
            **payload,
            "status": "DETERMINISTIC_PASS",
            "evidence": evidence,
            "evidence_sha256": hashlib.sha256(canonical).hexdigest(),
        }

    def _fresh_finding_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        finding_id = UUID(str(payload["finding_id"]))
        project = Project(str(payload["project"]))
        source_sha = self._sha(payload.get("affected_source_sha"), "affected_source_sha")
        deployment_sha = self._sha(
            payload.get("affected_deployment_sha"), "affected_deployment_sha"
        )
        source_repository, deployment_repository = self._repositories[project]
        base_ref = self._base_ref(project)
        if source_repository == deployment_repository and source_sha != deployment_sha:
            raise ValueError("self-contained project snapshot SHAs must match")
        import_heads: dict[str, str] = {}
        snapshot_identity = finding_id
        if payload.get("flow") == "current-state-revalidation":
            # NEW-117: an existing backlog issue is revalidated at the CURRENT heads of
            # the same repositories and branches, which must fast-forward from the heads
            # the approved campaign was imported at; the delivery then starts there.
            import_heads = {
                "import_source_sha": source_sha,
                "import_deployment_sha": deployment_sha,
            }
            current_source = self.manager.resolve_head(source_repository, base_ref=base_ref)
            current_deployment = (
                current_source
                if source_repository == deployment_repository
                else self.manager.resolve_head(deployment_repository, base_ref=base_ref)
            )
            for repository, ancestor, head in (
                (source_repository, source_sha, current_source),
                (deployment_repository, deployment_sha, current_deployment),
            ):
                if not self.manager.is_fast_forward(repository, ancestor, head):
                    raise ValueError(
                        f"current head of {repository} does not descend from the approved "
                        f"import head {ancestor}"
                    )
            if (current_source, current_deployment) != (source_sha, deployment_sha):
                # One immutable snapshot per head pair (an earlier round's snapshot of
                # other heads is never reused or moved).
                snapshot_identity = uuid5(
                    NAMESPACE_URL,
                    f"revalidation-snapshot:{finding_id}:{current_source}:{current_deployment}",
                )
            source_sha, deployment_sha = current_source, current_deployment
        source = self.manager.allocate_audit_snapshot(
            repository=source_repository,
            campaign_id=snapshot_identity,
            base_ref=base_ref,
            expected_base_sha=source_sha,
            require_base_ref_match=False,
        )
        deployment = source
        if source_repository != deployment_repository:
            deployment = self.manager.allocate_audit_snapshot(
                repository=deployment_repository,
                campaign_id=snapshot_identity,
                base_ref=base_ref,
                expected_base_sha=deployment_sha,
                require_base_ref_match=False,
            )
        evidence = {
            "finding_id": str(finding_id),
            "source_sha": source.base_sha,
            "deployment_sha": deployment.base_sha,
            "source_worktree": str(source.path),
            "deployment_worktree": str(deployment.path),
            **import_heads,
        }
        canonical = json.dumps(evidence, separators=(",", ":"), sort_keys=True).encode()
        return {
            **payload,
            **evidence,
            "status": "FRESH_SNAPSHOT",
            "snapshot_evidence_sha256": hashlib.sha256(canonical).hexdigest(),
        }

    def _snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        project = Project(str(payload.get("project")))
        campaign_id = UUID(str(payload.get("campaign_id")))
        source_repository, deployment_repository = self._repositories[project]
        base_ref = self._base_ref(project)
        source_sha = self._optional_or_resolved_sha(
            payload.get("source_sha"), source_repository, "source_sha", base_ref
        )
        deployment_sha = self._optional_or_resolved_sha(
            payload.get("deployment_sha"), deployment_repository, "deployment_sha", base_ref
        )
        if source_repository == deployment_repository and source_sha != deployment_sha:
            raise ValueError("self-contained project snapshot SHAs must match")
        source = self.manager.snapshot(
            source_repository,
            base_ref=base_ref,
            expected_sha=source_sha,
        )
        deployment = source
        if source_repository != deployment_repository:
            deployment = self.manager.snapshot(
                deployment_repository,
                base_ref=base_ref,
                expected_sha=deployment_sha,
            )
        source_worktree = self.manager.allocate_audit_snapshot(
            repository=source_repository,
            campaign_id=campaign_id,
            base_ref=base_ref,
            expected_base_sha=source_sha,
        )
        deployment_worktree = source_worktree
        if source_repository != deployment_repository:
            deployment_worktree = self.manager.allocate_audit_snapshot(
                repository=deployment_repository,
                campaign_id=campaign_id,
                base_ref=base_ref,
                expected_base_sha=deployment_sha,
            )
        self.manager.grant_release_read(
            deployment_repository,
            deployment_worktree.path,
        )
        return {
            **payload,
            "status": "SNAPSHOT_VERIFIED",
            "campaign_id": str(campaign_id),
            "source_sha": source_sha,
            "deployment_sha": deployment_sha,
            "source_snapshot": source,
            "deployment_snapshot": deployment,
            "source_worktree": str(source_worktree.path),
            "deployment_worktree": str(deployment_worktree.path),
        }

    def _optional_or_resolved_sha(
        self, value: Any, repository: str, field: str, base_ref: str
    ) -> str:
        if value is None:
            return self.manager.resolve_head(repository, base_ref=base_ref)
        return self._sha(value, field)

    @staticmethod
    def _base_ref(project: Project) -> str:
        return "refs/heads/master" if project is Project.LLMWIKI else "refs/heads/main"

    def _static_surface(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        snapshot = self._find_mapping(payload, "snapshot")
        if snapshot is None:
            raise ValueError("static audit requires the immutable paired repository snapshot")
        source = step == "audit.static_source"
        name = "source" if source else "deployment"
        worktree = Path(str(snapshot[f"{name}_worktree"]))
        sha = str(snapshot[f"{name}_sha"])
        inspection = self.manager.inspect_snapshot(worktree, expected_sha=sha)
        canonical = json.dumps(inspection, separators=(",", ":"), sort_keys=True).encode()
        return {
            **payload,
            "status": "STATIC_SOURCE_PASS" if source else "STATIC_DEPLOYMENT_PASS",
            f"{name}_inspection": inspection,
            f"{name}_inspection_sha256": hashlib.sha256(canonical).hexdigest(),
        }

    def _ui_inventory(self, payload: dict[str, Any]) -> dict[str, Any]:
        """NEW-120: the deterministic legacy-console inventory of an immutable snapshot.

        Model-free and read-only on the worktree: the snapshot must be at the expected SHA
        and clean. The full JSON and Markdown go to evidence; the result carries only the
        summary, the hashes and the evidence receipts (bounded Temporal payload).
        """
        project = Project(str(payload["project"]))
        snapshot = self._find_mapping(payload, "snapshot")
        if snapshot is None:
            raise ValueError("ui inventory requires the immutable source snapshot")
        worktree = Path(str(snapshot["source_worktree"]))
        source_sha = self._sha(snapshot.get("source_sha"), "source_sha")
        inspection = self.manager.inspect_snapshot(worktree, expected_sha=source_sha)
        policy = self.catalog.project(project.value).ui_migration
        if policy is None:
            raise ValueError(f"UI_MIGRATION_NOT_CONFIGURED: {project.value} has no ui_migration")
        try:
            # NEW-136 review: every console scan root must be a real directory, in Git and
            # on disk, or the tree id would cover only a link and miss console changes.
            check_git_scan_roots(
                lambda relative: self.manager.snapshot_object_type(
                    worktree, expected_sha=source_sha, relative=relative
                ),
                policy.legacy_app,
            )
            # NEW-136: the legacy app's tree id makes the reuse key change with console files.
            tree = self.manager.snapshot_tree(
                worktree, expected_sha=source_sha, relative=policy.legacy_app
            )
            document = build_inventory(
                worktree, policy, source_sha=source_sha, project=project.value, legacy_app_tree=tree
            )
        except InventoryError as error:
            if not str(error).startswith(SYMLINK_ROOT):
                raise
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": SYMLINK_ROOT,
                "reason": str(error)[:300],
                "source_sha": source_sha,
            }
        digest = str(document["inventory_sha256"])
        directory = self.settings.evidence_root / "scan" / "ui-inventory" / digest[:32]
        json_path = directory / "ui-inventory.json"
        markdown_path = directory / "ui-inventory.md"
        verified = open_directory_no_follow(self.settings.evidence_root / "scan", directory)
        try:
            self._store_redacted_json(verified, json_path.name, document)
            write_text_at(verified, markdown_path.name, render_markdown(document))
        finally:
            os.close(verified)
        receipts = self._publish_reports(
            payload,
            [json_path, markdown_path],
            filename_prefix=f"ui-inventory-{digest[:12]}",
            category="snapshot",
        )
        return {
            **payload,
            "status": "UI_INVENTORY_PASS",
            "schema_version": document["schema_version"],
            "source_sha": source_sha,
            "source_inspection": inspection,
            "inventory_sha256": digest,
            "legacy_app_tree": tree,
            "inventory_content_key": inventory_content_key(document),
            "summary": document["summary"],
            "route_tables": document["route_tables"],
            "evidence": receipts,
        }

    async def _ui_kit_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        """NEW-121 v2: a versioned gntik-ui kit snapshot from the digest-pinned image.

        Deterministic and model-free: the scan worker calls only read-only allowlisted
        tools. NEW-129: with `mcp.socket` set it connects to the root-owned gntik-ui MCP
        service socket (the service runs the pinned, isolated container; the worker never
        gets Docker access); otherwise it runs the container itself. A missing or untrusted
        socket, docker or image is a BLOCKED result (UI_MCP_UNAVAILABLE), not a retry.
        """
        project = Project(str(payload["project"]))
        policy = (await asyncio.to_thread(self.catalog.project, project.value)).ui_migration
        if policy is None:
            raise ValueError(f"UI_MIGRATION_NOT_CONFIGURED: {project.value} has no ui_migration")
        selection: dict[str, list[str]] = {}
        for kind in ("templates", "blocks", "components"):
            value = payload.get(kind, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError(f"ui kit snapshot {kind} must be a list of ids")
            if len(value) > 100:
                raise ValueError(f"ui kit snapshot {kind} exceeds 100 ids")
            selection[kind] = value
        plan = snapshot_plan(**selection)
        try:
            async with self.kit_client_factory(policy) as client:
                document = await collect_kit_snapshot(
                    client,
                    image=policy.mcp.image,
                    digest=policy.mcp.digest,
                    gntik_ui_version=policy.gntik_ui.version,
                    plan=plan,
                )
        except KitUnavailable as error:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": UI_MCP_UNAVAILABLE,
                "reason": str(error)[:300],
                "image": f"{policy.mcp.image}@{policy.mcp.digest}",
                "transport": "socket" if policy.mcp.socket else "docker",
            }
        except TimeoutError as error:
            raise ApplicationError(
                "gntik-ui kit snapshot exceeded its deadline", type="UiKitTimeout"
            ) from error
        return await asyncio.to_thread(self._store_kit_snapshot, payload, document)

    def _store_kit_snapshot(
        self, payload: dict[str, Any], document: dict[str, Any]
    ) -> dict[str, Any]:
        digest = str(document["snapshot_sha256"])
        directory = self.settings.evidence_root / "scan" / "ui-kit" / digest[:32]
        path = directory / "ui-kit-snapshot.json"
        verified = open_directory_no_follow(self.settings.evidence_root / "scan", directory)
        try:
            self._store_redacted_json(verified, path.name, document)
        finally:
            os.close(verified)
        receipts = self._publish_reports(
            payload, [path], filename_prefix=f"ui-kit-{digest[:12]}", category="snapshot"
        )
        return {
            **payload,
            "status": "UI_KIT_SNAPSHOT_PASS",
            "schema_version": document["schema_version"],
            "snapshot_sha256": digest,
            "image": document["image"],
            "transport": str(document["transport"]["kind"]),
            "gntik_ui_version": document["gntik_ui_version"],
            # Server-controlled data is bounded here; the full lists live only in evidence.
            "server_info": {
                str(key)[:32]: str(value)[:64]
                for key, value in list(document["server_info"].items())[:4]
            },
            "tools_available_count": len(document["tools_available"]),
            "tools_available": _bounded_names(document["tools_available"]),
            "summary": {
                key: int(document["summary"][key])
                for key in ("calls", "errors", "truncated", "text_bytes")
            },
            "evidence": receipts,
        }

    @staticmethod
    def _store_redacted_json(directory: int, name: str, document: dict[str, Any]) -> None:
        """Store exactly the hashed document: it must already be redaction-stable, so the
        evidence upload's own redaction cannot change what the reported digest covers."""
        if SecretRedactor().redact(document).value != document:
            raise ValueError("ui evidence was not redacted before it was hashed")
        write_text_at(directory, name, json.dumps(document, indent=2, sort_keys=True) + "\n")

    def _snapshot_pair(self, payload: dict[str, Any]) -> dict[str, Any]:
        project = Project(str(payload.get("project")))
        snapshot = self._find_mapping(payload, "snapshot")
        if snapshot is None:
            raise ValueError("paired snapshot requires both repository snapshots")
        manifest = self.catalog.project(project.value)
        # NEW-160: fails closed (PROJECT_HAS_NO_RUNTIME) for a project without a runtime.
        deployment = manifest.runtime_deployment()
        raw_credential = Path(deployment.observer_credential_ref.removeprefix("file:"))
        try:
            kubeconfig = self._observer_credential(raw_credential)
        except (OSError, ValueError):
            return {
                **payload,
                "status": "BLOCKED",
                "readiness": "BLOCKED_BY_CREDENTIAL",
                "blocker": (
                    "KUBERNETES_OBSERVER_CREDENTIAL_MISSING"
                    if not raw_credential.is_file()
                    else "KUBERNETES_OBSERVER_CREDENTIAL_PERMISSIONS"
                ),
                "missing_credential_ref": deployment.observer_credential_ref,
            }
        observer = KubernetesObserver(
            self.observer_runner,
            kubeconfig=kubeconfig,
            context=deployment.kubernetes_context_ref,
            working_directory=self.settings.run_root / "scan",
        )
        namespace = deployment.namespaces[0]
        resources = ["deployments", "statefulsets", "pods", "events"]
        observer.verify_permissions((namespace,), tuple(resources))
        # The Argo CD Application lives in the Argo CD namespace, not the workload one
        # (gntik-ai/falcone#1027 review): read it there.
        argo_namespace = "argocd"
        if deployment.strategy == "argocd":
            flow = manifest.staging_flow()
            argo_namespace = flow.argo_namespace if flow is not None else "argocd"
            resources.append("applications.argoproj.io")
            observer.verify_permissions((argo_namespace,), ("applications.argoproj.io",))
        identity = observer.identity()
        if identity["context"] != deployment.kubernetes_context_ref:
            raise PermissionError("observer Kubernetes context changed")
        if identity["api_server"] != deployment.api_server_identity:
            raise PermissionError("observer API server identity changed")
        summaries: dict[str, Any] = {}
        revisions: set[str] = set()
        image_digests: set[str] = set()
        failures: list[str] = []
        pod_names: list[str] = []
        helm_revisions: set[int] = set()
        for resource in resources:
            name = deployment.argocd_application if resource == "applications.argoproj.io" else None
            resource_namespace = (
                argo_namespace if resource == "applications.argoproj.io" else namespace
            )
            observed = json.loads(
                observer.get(resource, namespace=resource_namespace, name=name).stdout
            )
            summary = self._summarize_runtime_resource(
                resource,
                observed,
                revision_annotation=deployment.deployment_revision_annotation,
            )
            summaries[resource] = summary
            revisions.update(summary["revisions"])
            image_digests.update(summary["image_digests"])
            failures.extend(summary["failures"])
            if resource == "pods":
                pod_names.extend(summary["names"][:3])
            if deployment.helm_release_revision_annotation:
                helm_revisions.update(
                    self._integer_annotations(observed, deployment.helm_release_revision_annotation)
                )
        logs = self._bounded_redacted_logs(observer, namespace, pod_names)
        intended_revision = str(snapshot["deployment_sha"])
        deployed_revision = intended_revision if revisions == {intended_revision} else None
        if deployed_revision is None:
            return {
                **payload,
                "status": "LIVE_REVISION_DRIFT" if revisions else "BLOCKED",
                "blocker": (
                    "LIVE_REVISION_DRIFT" if revisions else "RUNTIME_DEPLOYMENT_REVISION_UNPROVEN"
                ),
                "intended_deployment_sha": intended_revision,
                "observed_deployment_revisions": sorted(revisions),
                "kubernetes_identity": identity,
                "workload_observation": summaries,
                "bounded_redacted_logs": logs,
            }
        if not image_digests:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "RUNNING_IMAGE_DIGESTS_UNPROVEN",
                "kubernetes_identity": identity,
                "workload_observation": summaries,
            }
        if deployment.strategy == "helm" and len(helm_revisions) != 1:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "HELM_RELEASE_REVISION_UNPROVEN_WITHOUT_SECRET_ACCESS",
                "kubernetes_identity": identity,
                "workload_observation": summaries,
            }
        runtime = {
            "running_image_digests": {
                f"image-{index}": digest for index, digest in enumerate(sorted(image_digests), 1)
            },
            "helm_release_revision": next(iter(helm_revisions), None),
            "argocd_revision": (deployed_revision if deployment.strategy == "argocd" else None),
            "kubernetes_context_identity": str(identity["username"]),
            "api_server_identity": str(identity["api_server"]),
            "namespace": namespace,
            "runtime_url": str(deployment.runtime_url),
            "deployed_revision": deployed_revision,
        }
        pair = ProjectPairSnapshot.model_validate(
            {
                "schema_version": 1,
                "project": project.value,
                "source": {
                    "url": str(manifest.repositories.source.url),
                    "branch": manifest.repositories.source.default_branch,
                    "sha": snapshot["source_sha"],
                },
                "deployment": {
                    "url": str(manifest.repositories.deployment.url),
                    "branch": manifest.repositories.deployment.default_branch,
                    "sha": snapshot["deployment_sha"],
                },
                "runtime": runtime,
                "intended_deployment_sha": intended_revision,
                "deployment_revision_matches": True,
                "capture_time": datetime.now(UTC),
                "status": "PASS",
            }
        )
        return {
            **payload,
            "status": "PAIR_SNAPSHOT_PASS" if not failures else "BLOCKED",
            "blocker": "KUBERNETES_WORKLOAD_NOT_READY" if failures else None,
            "pair_snapshot": pair.model_dump(mode="json"),
            "workload_observation": summaries,
            "bounded_redacted_logs": logs,
        }

    @staticmethod
    def _integer_annotations(value: Any, annotation: str) -> set[int]:
        if not isinstance(value, dict):
            return set()
        items = value.get("items")
        values = items if isinstance(items, list) else [value]
        result: set[int] = set()
        for item in values:
            if not isinstance(item, dict):
                continue
            annotations = item.get("metadata", {}).get("annotations", {})
            candidate = annotations.get(annotation) if isinstance(annotations, dict) else None
            if isinstance(candidate, str) and candidate.isdigit():
                result.add(int(candidate))
        return result

    @staticmethod
    def _bounded_redacted_logs(
        observer: KubernetesObserver, namespace: str, pod_names: list[str]
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        redactor = SecretRedactor()
        for pod in sorted(set(pod_names))[:3]:
            result = observer.logs(pod, namespace=namespace, tail=100)
            safe = str(redactor.redact_text(result.stdout[-16_384:]).value)
            records.append(
                {
                    "pod": pod,
                    "tail_lines": 100,
                    "content": safe,
                    "sha256": hashlib.sha256(safe.encode()).hexdigest(),
                }
            )
        return records

    @staticmethod
    def _pair_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
        snapshot = payload.get("snapshot")
        if not isinstance(snapshot, dict):
            return None
        pair = snapshot.get("pair_snapshot")
        return pair if isinstance(pair, dict) else None

    def _compare_live(self, payload: dict[str, Any]) -> dict[str, Any]:
        pair = self._pair_from_payload(payload)
        if pair is None or pair.get("status") != "PAIR_SNAPSHOT_PASS":
            return {**payload, "status": "BLOCKED", "blocker": "PAIR_SNAPSHOT_NOT_PASSING"}
        return {**payload, "status": "LIVE_DEPLOYMENT_MATCH"}

    def _kubernetes_observe(self, payload: dict[str, Any]) -> dict[str, Any]:
        pair = self._pair_from_payload(payload)
        if pair is None or not isinstance(pair.get("workload_observation"), dict):
            return {**payload, "status": "BLOCKED", "blocker": "KUBERNETES_EVIDENCE_MISSING"}
        return {**payload, "status": "KUBERNETES_OBSERVATION_PASS"}

    def _correlate_runtime(self, payload: dict[str, Any]) -> dict[str, Any]:
        pair = self._pair_from_payload(payload)
        if pair is None or not isinstance(pair.get("pair_snapshot"), dict):
            return {**payload, "status": "BLOCKED", "blocker": "RUNTIME_CORRELATION_MISSING"}
        return {**payload, "status": "RUNTIME_CORRELATION_PASS"}

    def _allocate(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = self._find_mapping(payload, "allocation_request")
        if raw is None:
            raise ValueError("worktree allocation requires allocation_request")
        request = WorktreeAllocationRequest.model_validate(raw)
        if request.maker_provider is request.checker_provider:
            raise ValueError("maker and checker providers must differ")
        access = WorktreeAccess(
            f"{request.maker_provider.value}-runner",
            f"{request.checker_provider.value}-runner",
        )
        for repository, approved, base in (
            (request.source_repository, request.source_approved_base_sha, request.source_base_sha),
            (
                request.deployment_repository,
                request.deployment_approved_base_sha,
                request.deployment_base_sha,
            ),
        ):
            if approved is not None and not self.manager.is_fast_forward(
                repository, approved, base
            ):
                raise ValueError(
                    f"delivery base {base} of {repository} does not descend from the "
                    f"approved import head {approved}"
                )
        source = self.manager.allocate(
            repository=request.source_repository,
            project=request.project.value,
            issue=request.issue,
            change_set_id=request.change_set_id,
            base_ref=request.source_base_ref,
            expected_base_sha=request.source_base_sha,
            access=access,
            resume_prior_commits=self.settings.delivery_resume_prior_commits,
        )
        deployment = source
        if request.source_repository == request.deployment_repository:
            if (
                request.source_base_sha != request.deployment_base_sha
                or request.source_base_ref != request.deployment_base_ref
            ):
                raise ValueError("self-contained ChangeSet source/deployment bases must match")
        else:
            deployment = self.manager.allocate(
                repository=request.deployment_repository,
                project=request.project.value,
                issue=request.issue,
                change_set_id=request.change_set_id,
                base_ref=request.deployment_base_ref,
                expected_base_sha=request.deployment_base_sha,
                access=access,
                resume_prior_commits=self.settings.delivery_resume_prior_commits,
            )
        return {
            **payload,
            "status": "WORKTREES_ALLOCATED",
            "source_worktree": str(source.path),
            "source_branch": source.branch,
            "deployment_worktree": str(deployment.path),
            "deployment_branch": deployment.branch,
            # NEW-52 (additive): the previous attempt's HEAD a worktree kept, else None.
            "source_resumed_from_sha": source.resumed_from_sha,
            "deployment_resumed_from_sha": deployment.resumed_from_sha,
        }

    def _delivery_prechecks(self, payload: dict[str, Any]) -> dict[str, Any]:
        change_set_id = UUID(str(payload["change_set_id"]))
        evidence_root = self.settings.evidence_root / "scan" / str(change_set_id) / "prechecks"
        summaries: dict[str, Any] = {}
        baseline: dict[str, dict[str, dict[str, int]]] = {}
        for name, path_value in (
            ("source", payload["source_worktree"]),
            ("deployment", payload["deployment_worktree"]),
        ):
            worktree = Path(str(path_value)).resolve()
            root = self.settings.worktree_root.resolve()
            if not worktree.is_dir() or not worktree.is_relative_to(root):
                raise ValueError("precheck worktree escaped its bounded root")
            output = evidence_root / name
            resumed = payload.get(f"{name}_resumed_from_sha")
            if isinstance(resumed, str) and resumed:
                # NEW-52: a resumed worktree already contains the previous attempt's
                # commits; scanning it would absorb the change's own findings into the
                # baseline. Scan a scratch checkout of the base commit instead.
                base_sha = str(payload[f"{name}_base_sha"])
                output = evidence_root / f"{name}-base-{base_sha}"
                if output.exists():
                    shutil.rmtree(output)  # reports name the scratch checkout's path
                with self._precheck_base_checkout(
                    change_set_id, name, str(payload[f"{name}_repository"]), base_sha
                ) as checkout:
                    secret, trivy, semgrep = self._precheck_scans(checkout, output)
                    counts = self._finding_counts(checkout, secret, trivy, semgrep)
            else:
                secret, trivy, semgrep = self._precheck_scans(worktree, output)
                counts = self._finding_counts(worktree, secret, trivy, semgrep)
            semgrep_findings = len(semgrep.get("results", []))
            severe_vulnerabilities = self._severe_vulnerability_count(trivy)
            # NEW-28: pre-existing findings are the baseline, not a blocker -- otherwise
            # no real repository can ever receive a change. validate_changeset blocks on
            # any finding the change introduces relative to this baseline.
            baseline[name] = counts
            artifacts = self._publish_reports(
                payload,
                sorted(report for report in output.iterdir() if report.is_file()),
                filename_prefix=f"prechecks-{name}",
            )
            summaries[name] = {
                "secret_status": secret.get("status"),
                "semgrep_findings": semgrep_findings,
                "high_or_critical_vulnerabilities": severe_vulnerabilities,
                "artifacts": artifacts,
            }
        self._atomic_text(
            self._baseline_path(change_set_id), json.dumps(baseline, indent=2, sort_keys=True)
        )
        return {
            **payload,
            "status": "PRECHECKS_PASS",
            "blocker": None,
            "prechecks": summaries,
        }

    def _precheck_scans(
        self, worktree: Path, output: Path
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        secret = self.scanner.secret_scan(worktree, output)
        trivy = self.scanner.vulnerability_scan(worktree, output)
        semgrep = self.scanner.semgrep(worktree, output)
        self.scanner.sbom(worktree, output)
        return secret, trivy, semgrep

    @contextmanager
    def _precheck_base_checkout(
        self, change_set_id: UUID, name: str, repository: str, base_sha: str
    ) -> Iterator[Path]:
        """NEW-52: a scratch, detached checkout of base_sha for a resumed worktree's
        prechecks baseline, owned by the scan worker and removed after the scan."""
        if not re.fullmatch(r"[a-f0-9]{40}", base_sha):
            raise ValueError("the prechecks baseline requires the exact base commit")
        root = self.settings.toolchain_cache_root / "baselines" / "prechecks" / str(change_set_id)
        root.mkdir(parents=True, exist_ok=True, mode=0o750)
        checkout = root / f"{name}-base"
        if checkout.is_symlink():
            raise ValueError("prechecks baseline checkout must not be a symlink")
        if checkout.exists():
            shutil.rmtree(checkout)  # left behind by an interrupted attempt
        try:
            self._clone_detached(repository, base_sha, checkout, cwd=root)
            # Scan only the tree, like a worktree whose .git is a pointer file.
            shutil.rmtree(checkout / ".git")
            yield checkout
        finally:
            if checkout.exists():
                shutil.rmtree(checkout)

    def _clone_detached(self, repository: str, base_sha: str, target: Path, *, cwd: Path) -> None:
        """Check out base_sha, detached, from the canonical mirror (never written)."""
        mirror = self.manager.verify_mirror(repository)
        # --shared: objects are read from the canonical mirror, which is never written.
        self.git_runner.run(
            "git",
            [
                "-c",
                f"safe.directory={mirror}",
                "clone",
                "--shared",
                "--no-checkout",
                "--quiet",
                str(mirror),
                str(target),
            ],
            cwd=cwd,
            timeout_seconds=900,
            environment={"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"},
        )
        self._git(target, ["checkout", "--detach", "--quiet", base_sha], timeout_seconds=900)
        if self._git(target, ["rev-parse", "HEAD"]).strip() != base_sha:
            raise ValueError("baseline checkout does not match the source base commit")

    @staticmethod
    async def _with_heartbeat(work: Awaitable[_T], phase: str = "toolchain_prepare") -> _T:
        """Keep a long blocking step alive against the workflow's 2-minute heartbeat timeout."""
        task = asyncio.ensure_future(work)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=_TOOLCHAIN_HEARTBEAT_SECONDS)
                if done:
                    return task.result()
                try:
                    activity.heartbeat({"phase": phase})
                except RuntimeError:
                    pass  # not running inside an activity (unit tests)
        except asyncio.CancelledError:
            task.cancel()
            raise

    def _bounded_worktree(self, value: object) -> Path:
        worktree = Path(str(value)).resolve()
        root = self.settings.worktree_root.resolve()
        if worktree == root or not worktree.is_dir() or not worktree.is_relative_to(root):
            raise ValueError("toolchain worktree escaped its bounded root")
        return worktree

    def _git(self, worktree: Path, arguments: list[str], *, timeout_seconds: int = 300) -> str:
        return self.git_runner.run(
            "git",
            ["-c", f"safe.directory={worktree}", *arguments],
            cwd=worktree,
            timeout_seconds=timeout_seconds,
            environment={
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
            },
        ).stdout

    def _git_status(self, worktree: Path, *, ignored: bool) -> dict[str, str]:
        """Map each porcelain path to its XY code ('??' untracked, '!!' ignored)."""
        arguments = ["status", "--porcelain=v1", "-z", "--untracked-files=normal"]
        if ignored:
            arguments.append("--ignored=traditional")
        tokens = self._git(worktree, arguments).split("\0")
        entries: dict[str, str] = {}
        index = 0
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if len(token) < 4:
                continue
            code, path = token[:2], token[3:]
            entries[path] = code
            if code[0] in {"R", "C"}:
                index += 1  # -z rename/copy records carry the original path next
        return entries

    def _toolchain_environment(self) -> dict[str, str]:
        """Network-enabled installer environment: private caches, no credentials."""
        root = self.settings.toolchain_cache_root
        directories = {
            name: root / name
            for name in (
                "uv",
                "pnpm-store",
                "pnpm-cache",
                "home",
                "xdg-cache",
                # NEW-75/79a: Go modules and the Trivy DB, read-only for makers/checkers.
                "go-mod",
                "go-build",
                "go-path",
                "trivy",
            )
        }
        for directory in (root, *directories.values()):
            directory.mkdir(parents=True, exist_ok=True, mode=0o750)
            if directory.stat().st_uid == os.getuid():
                directory.chmod(0o750)
        store = str(directories["pnpm-store"])
        cache = str(directories["pnpm-cache"])
        return {
            "CI": "true",
            # HOME is a private empty directory so no ~/.npmrc, ~/.netrc or uv/pnpm user
            # configuration (and no credential in them) is ever read.
            "HOME": str(directories["home"]),
            "XDG_CACHE_HOME": str(directories["xdg-cache"]),
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_CACHE_DIR": str(directories["uv"]),
            "UV_NO_PROGRESS": "1",
            # Copy, never hardlink: makers can write to the worktree, and a hardlinked
            # file would let them modify the shared cache for every later ChangeSet.
            "UV_LINK_MODE": "copy",
            "npm_config_store_dir": store,
            "pnpm_config_store_dir": store,
            "npm_config_cache_dir": cache,
            "pnpm_config_cache_dir": cache,
            "npm_config_package_import_method": "copy",
            "pnpm_config_package_import_method": "copy",
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
            "GOMODCACHE": str(directories["go-mod"]),
            "GOCACHE": str(directories["go-build"]),
            "GOPATH": str(directories["go-path"]),
            "GOTOOLCHAIN": "local",
            "GOTELEMETRY": "off",
            "GOFLAGS": "-modcacherw",
            "TRIVY_CACHE_DIR": str(directories["trivy"]),
            "TRIVY_NO_PROGRESS": "true",
        }

    def _node_toolchain_environment(
        self, environment: dict[str, str], toolchain: NodeToolchain, base_path: str
    ) -> dict[str, str]:
        """Add the selected node/pnpm to an environment; the default returns it unchanged."""
        if toolchain.default:
            return environment
        return {**environment, **toolchain.variables, "PATH": toolchain.path(base_path)}

    def _toolchain_runner_for(self, toolchain: NodeToolchain) -> SafeCommandRunner:
        if toolchain.default or self._runners_injected[0]:
            return self.toolchain_runner
        return SafeCommandRunner(prepare_executables(self.settings, toolchain))

    def _dependency_runner_for(self, toolchain: NodeToolchain) -> SafeCommandRunner:
        if toolchain.default or self._runners_injected[1]:
            return self.dependency_runner
        return SafeCommandRunner(
            {"go": self.settings.go_executable, "pnpm": toolchain.pnpm_executable}
        )

    # NEW-75: the only commands the platform runs to resolve maker-edited manifests
    # (network; no install scripts), and the only files they may change.
    _DEPENDENCY_COMMANDS: dict[str, tuple[tuple[str, ...], ...]] = {
        "go": (("go", "mod", "tidy"),),
        "pnpm": (("pnpm", "install", "--lockfile-only", "--ignore-scripts"),),
    }
    _DEPENDENCY_FILES = {"go": ("go.mod", "go.sum"), "pnpm": ("pnpm-lock.yaml",)}
    _DEPENDENCY_MANIFEST = {"go": "go.mod", "pnpm": "package.json"}

    def _resolve_dependencies(self, payload: dict[str, Any]) -> dict[str, Any]:
        """NEW-75: regenerate lockfiles for manifests the offline maker edited, commit them,
        then reinstall the toolchain so the maker's next round can build and test."""
        agent = payload.get("agent_result")
        structured = agent.get("structured_result") if isinstance(agent, dict) else None
        raw = structured.get("dependency_resolution") if isinstance(structured, dict) else None
        if not isinstance(raw, list) or not 1 <= len(raw) <= 10:
            return self._dependency_blocked(payload, "DEPENDENCY_REQUEST_INVALID", "no requests")
        worktree = self._bounded_worktree(payload["source_worktree"])
        if self._git_status(worktree, ignored=False):
            return self._dependency_blocked(
                payload, "DEPENDENCY_WORKTREE_DIRTY", "the maker left uncommitted changes"
            )
        try:
            # NEW-159 review 2: the delivery's carried selection, as for the maker.
            toolchain = payload_node_toolchain(self.settings, payload)
        except ToolchainUnavailableError as error:
            return self._dependency_blocked(payload, error.blocker, str(error))
        except (OSError, ValueError) as error:
            return self._dependency_blocked(payload, "TOOLCHAIN_CONTRACT_INVALID", str(error))
        environment = self._node_toolchain_environment(
            self._dependency_environment(), toolchain, _DEFAULT_PATH
        )
        dependency_runner = self._dependency_runner_for(toolchain)
        allowed: set[str] = set()
        commands: list[dict[str, Any]] = []
        for item in raw:
            ecosystem = str(item.get("ecosystem", "")) if isinstance(item, dict) else ""
            directory = str(item.get("directory", "")) if isinstance(item, dict) else ""
            if ecosystem not in self._DEPENDENCY_COMMANDS or not re.fullmatch(
                r"\.|[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*", directory
            ):
                return self._dependency_blocked(
                    payload, "DEPENDENCY_REQUEST_INVALID", f"{ecosystem!r} {directory!r}"
                )
            cwd = (worktree / directory).resolve()
            manifest = cwd / self._DEPENDENCY_MANIFEST[ecosystem]
            if not cwd.is_relative_to(worktree) or not manifest.is_file():
                return self._dependency_blocked(
                    payload,
                    "DEPENDENCY_REQUEST_INVALID",
                    f"{directory!r} has no {self._DEPENDENCY_MANIFEST[ecosystem]}",
                )
            prefix = "" if cwd == worktree else f"{cwd.relative_to(worktree).as_posix()}/"
            allowed.update(prefix + name for name in self._DEPENDENCY_FILES[ecosystem])
            for argv in self._DEPENDENCY_COMMANDS[ecosystem]:
                record: dict[str, Any] = {"ecosystem": ecosystem, "cwd": directory, "argv": argv}
                try:
                    result = dependency_runner.run(
                        argv[0],
                        list(argv[1:]),
                        cwd=cwd,
                        timeout_seconds=1200,
                        environment=environment,
                        check=False,
                    )
                except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                    commands.append({**record, "status": "NOT_COMPLETED"})
                    return self._dependency_blocked(
                        payload, "DEPENDENCY_RESOLUTION_FAILED", f"{argv[0]}: {error}", commands
                    )
                if result.return_code:
                    commands.append({**record, "status": "FAILED"})
                    return self._dependency_blocked(
                        payload,
                        "DEPENDENCY_RESOLUTION_FAILED",
                        result.stderr.strip() or result.stdout,
                        commands,
                    )
                commands.append({**record, "status": "PASS"})
        changed = sorted(self._git_status(worktree, ignored=False))
        unexpected = [path for path in changed if path not in allowed]
        if unexpected:
            return self._dependency_blocked(
                payload,
                "DEPENDENCY_RESOLUTION_TOUCHED_OTHER_FILES",
                ", ".join(unexpected[:50]),
                commands,
            )
        commit = None
        if changed:
            self._git(worktree, ["add", "--", *changed])
            self._git(
                worktree,
                [
                    "commit",
                    "--quiet",
                    "--no-verify",
                    "-m",
                    "chore(deps): regenerate lockfiles (Hermes dependency resolution)",
                ],
            )
            commit = self._git(worktree, ["rev-parse", "HEAD"]).strip()
        prepared = self._prepare_toolchain(payload)
        if prepared.get("status") == "BLOCKED":
            return prepared
        return {
            **prepared,
            "status": "DEPENDENCIES_RESOLVED",
            "blocker": None,
            "dependency_resolution_result": {
                "files": changed,
                "commit": commit,
                "commands": commands,
            },
        }

    def _dependency_environment(self) -> dict[str, str]:
        """The toolchain installer environment plus private Go caches (never GOPROXY auth)."""
        return {**self._toolchain_environment(), "GIT_TERMINAL_PROMPT": "0"}

    @staticmethod
    def _dependency_blocked(
        payload: dict[str, Any],
        blocker: str,
        detail: str,
        commands: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        safe = str(SecretRedactor().redact_text(detail).value)[-_TOOLCHAIN_STDERR_TAIL:]
        return {
            **payload,
            "status": "BLOCKED",
            "blocker": blocker,
            "dependency_resolution_result": {
                "status": "BLOCKED",
                "blocker": blocker,
                "detail": safe,
                "commands": commands or [],
            },
        }

    # -- NEW-118: deterministic CI-pin convergence -------------------------------------

    _PIN_MAX_FILE_BYTES = 1024 * 1024
    _PIN_IDENTITY = ("Engineering Control", "engineering-control@local.invalid")

    def _converge_deployment_pin(self, payload: dict[str, Any]) -> dict[str, Any]:
        """NEW-118: set the source CI pin of the deployment repository to its worktree HEAD.

        Runs after the deployment maker and before the checker (falcone #980/#45: the
        source maker runs first, so every deployment commit left FALCONE_CHARTS_REF one
        commit behind). It converges whenever the delivery has a paired deployment worktree,
        also without new deployment commits (the worktree HEAD is then the deployment main
        the ChangeSet started from, and a pin left at an earlier PR's authored commit is
        stale). Only the exact ``VARIABLE: '<40-hex>'`` definition lines change, byte for
        byte (newlines kept), in a clean source worktree, committed as the platform
        identity the worktree manager configures for maker commits; anything ambiguous
        blocks instead. A merged source PR is never moved (NEW-103 repairs): its pin stays
        at the merged value, recorded with whether that commit is an ancestor of the
        deployment HEAD.
        """
        project = str(payload.get("project", ""))
        source_value = payload.get("source_worktree")
        deployment_value = payload.get("deployment_worktree")
        if not source_value or not deployment_value or source_value == deployment_value:
            # No paired deployment worktree (llmwiki): consistently exempt, and the
            # checker prompt carries no pin rule for it either.
            return self._pin_result(payload, "DEPLOYMENT_PIN_NOT_APPLICABLE", "NO_PAIRED_WORKTREES")
        try:
            manifest = self.catalog.project(Project(project).value)
        except (OSError, ValueError, ValidationError):
            return self._pin_blocked(payload, "DEPLOYMENT_PIN_CONTRACT_UNREADABLE", {})
        policy = manifest.deployment_pin
        if policy is None:
            return self._pin_result(payload, "DEPLOYMENT_PIN_NOT_APPLICABLE", "NOT_CONFIGURED")
        agent = payload.get("agent_result")
        structured = agent.get("structured_result") if isinstance(agent, dict) else None
        if not isinstance(structured, dict) or structured.get("decision") != "COMPLETE":
            # delivery.prepare_checker blocks an incomplete deployment maker itself.
            return self._pin_result(
                payload, "DEPLOYMENT_PIN_NOT_APPLICABLE", "DEPLOYMENT_MAKER_NOT_COMPLETE"
            )
        source = self._bounded_worktree(source_value)
        deployment = self._bounded_worktree(deployment_value)
        deployment_head = self._git(deployment, ["rev-parse", "HEAD"]).strip()
        if not re.fullmatch(r"[a-f0-9]{40}", deployment_head):
            return self._pin_blocked(payload, "DEPLOYMENT_PIN_HEAD_INVALID", {})
        parsed = self._parse_pins(source, policy, deployment_head)
        if payload.get("source_pr_merged") is True:
            pins = (
                sorted({sha for item in parsed["files"] for sha in item["old"]})
                if "files" in parsed
                else []
            )
            return self._pin_result(
                payload,
                "DEPLOYMENT_PIN_NOT_APPLICABLE",
                "SOURCE_PR_MERGED",
                deployment_head=deployment_head,
                merged_pins=[
                    {"pin": pin, "ancestor_of_deployment_head": self._is_ancestor(deployment, pin)}
                    for pin in pins
                ],
            )
        if "blocker" in parsed:
            return self._pin_blocked(payload, parsed["blocker"], parsed["details"])
        files: list[dict[str, Any]] = parsed["files"]
        rewrites: dict[str, tuple[Path, bytes, bytes]] = parsed["rewrites"]
        evidence: dict[str, Any] = {
            "variable": policy.variable,
            "deployment_repository": str(manifest.repositories.deployment.full_name),
            "deployment_head": deployment_head,
            "files": files,
            "commit": None,
        }
        if not rewrites:
            self._write_pin_evidence(payload, evidence)
            return {
                **payload,
                "status": "DEPLOYMENT_PIN_UNCHANGED",
                "deployment_pin_result": evidence,
            }
        if self._git_status(source, ignored=False):
            return self._pin_blocked(
                payload, "DEPLOYMENT_PIN_SOURCE_DIRTY", {"deployment_head": deployment_head}
            )
        before = self._git(source, ["rev-parse", "HEAD"]).strip()
        changed = sorted(rewrites)
        try:
            for path, _, updated in rewrites.values():
                path.write_bytes(updated)
            if sorted(self._git_status(source, ignored=False)) != changed:
                raise ValueError("pin convergence changed files outside its configured files")
            name, email = self._PIN_IDENTITY
            self._git(source, ["add", "--", *changed])
            self._git(
                source,
                [
                    "-c",
                    f"user.name={name}",
                    "-c",
                    f"user.email={email}",
                    "commit",
                    "--quiet",
                    "--no-verify",
                    "-m",
                    f"chore(ci): pin {evidence['deployment_repository']} to "
                    f"{deployment_head[:12]} (Hermes pin convergence)",
                ],
            )
        except (OSError, ValueError, CommandFailure):
            for path, original, _ in rewrites.values():
                path.write_bytes(original)
            self._git(source, ["reset", "--quiet", "--", *changed])
            raise
        commit = self._git(source, ["rev-parse", "HEAD"]).strip()
        parent = self._git(source, ["rev-parse", "HEAD^"]).strip()
        # Exactly the pin lines, one removed and one added each, nothing else committed.
        numstat = sorted(
            tuple(line.split("\t"))
            for line in self._git(
                source, ["diff", "--numstat", "--no-renames", "HEAD^", "HEAD"]
            ).splitlines()
            if line
        )
        expected = sorted(
            (str(item["changed_lines"]), str(item["changed_lines"]), item["file"])
            for item in files
            if item["file"] in rewrites
        )
        if (
            parent != before
            or numstat != expected
            or self._git_status(source, ignored=False)
            or any(path.read_bytes() != updated for path, _, updated in rewrites.values())
        ):
            raise ValueError("pin convergence commit is not exactly the configured pin lines")
        evidence["commit"] = commit
        self._write_pin_evidence(payload, evidence)
        return {**payload, "status": "DEPLOYMENT_PIN_CONVERGED", "deployment_pin_result": evidence}

    def _parse_pins(self, source: Path, policy: Any, deployment_head: str) -> dict[str, Any]:
        """Validate every configured pin line and compute the byte-exact rewrites."""
        definition = re.compile(rf"^[ \t]*[\"']?{re.escape(policy.variable)}[\"']?[ \t]*:")
        exact = re.compile(
            rf"^(?P<prefix>[ \t]*{re.escape(policy.variable)}:[ \t]*)(?P<quote>['\"]?)"
            r"(?P<sha>[0-9a-f]{40})(?P=quote)(?P<suffix>[ \t]*(?:#[^\r\n]*)?)$"
        )
        files: list[dict[str, Any]] = []
        rewrites: dict[str, tuple[Path, bytes, bytes]] = {}
        for relative in policy.files:
            unresolved = source / relative
            path = unresolved.resolve()
            if (
                unresolved.is_symlink()
                or not path.is_relative_to(source)
                or not path.is_file()
                or path.stat().st_size > self._PIN_MAX_FILE_BYTES
            ):
                return {"blocker": "DEPLOYMENT_PIN_FILE_MISSING", "details": {"file": relative}}
            original = path.read_bytes()
            try:
                text = original.decode("utf-8")
            except UnicodeDecodeError:
                return {"blocker": "DEPLOYMENT_PIN_MALFORMED", "details": {"file": relative}}
            # Split on "\n" only, so "\r\n" and every other byte is kept exactly.
            lines = text.split("\n")
            old: list[str] = []
            changed_lines = 0
            for index, line in enumerate(lines):
                carriage = "\r" if line.endswith("\r") else ""
                body = line[: len(line) - len(carriage)]
                if not definition.match(body):
                    continue
                match = exact.fullmatch(body)
                if match is None:
                    return {
                        "blocker": "DEPLOYMENT_PIN_MALFORMED",
                        "details": {"file": relative, "line": index + 1},
                    }
                old.append(match.group("sha"))
                replaced = (
                    f"{match.group('prefix')}{match.group('quote')}{deployment_head}"
                    f"{match.group('quote')}{match.group('suffix')}{carriage}"
                )
                if replaced != line:
                    changed_lines += 1
                lines[index] = replaced
            if len(old) != policy.occurrences:
                return {
                    "blocker": "DEPLOYMENT_PIN_AMBIGUOUS",
                    "details": {
                        "file": relative,
                        "expected": policy.occurrences,
                        "found": len(old),
                    },
                }
            files.append(
                {
                    "file": relative,
                    "old": old,
                    "new": deployment_head,
                    "changed_lines": changed_lines,
                }
            )
            updated = "\n".join(lines).encode("utf-8")
            if updated != original:
                rewrites[relative] = (path, original, updated)
        return {"files": files, "rewrites": rewrites}

    def _is_ancestor(self, worktree: Path, sha: str) -> bool:
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            return False
        result = self.git_runner.run(
            "git",
            ["-c", f"safe.directory={worktree}", "merge-base", "--is-ancestor", sha, "HEAD"],
            cwd=worktree,
            timeout_seconds=300,
            environment={"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"},
            check=False,
        )
        return result.return_code == 0

    def _write_pin_evidence(self, payload: dict[str, Any], evidence: dict[str, Any]) -> None:
        try:
            change_set_id = UUID(str(payload.get("change_set_id")))
        except ValueError:
            return
        directory = self.settings.evidence_root / "scan" / str(change_set_id) / "deployment-pin"
        directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        target = directory / f"{evidence['deployment_head']}.json"
        target.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _pin_result(
        payload: dict[str, Any], status: str, reason: str, **details: Any
    ) -> dict[str, Any]:
        return {
            **payload,
            "status": status,
            "deployment_pin_result": {"reason": reason, "commit": None, **details},
        }

    @staticmethod
    def _pin_blocked(
        payload: dict[str, Any], blocker: str, details: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            **payload,
            "status": "BLOCKED",
            "blocker": blocker,
            "deployment_pin_result": {"blocker": blocker, "commit": None, **details},
        }

    @staticmethod
    def _toolchain_blocked(
        payload: dict[str, Any], blocker: str, detail: str, commands: list[dict[str, Any]]
    ) -> dict[str, Any]:
        safe = str(SecretRedactor().redact_text(detail).value)[-_TOOLCHAIN_STDERR_TAIL:]
        return {
            **payload,
            "status": "BLOCKED",
            "blocker": blocker,
            "toolchain_preparation": {
                "status": "BLOCKED",
                "blocker": blocker,
                "detail": safe,
                "commands": commands,
            },
        }

    def _prepare_toolchain(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Install the project's dependencies in the source worktree before the maker.

        Runs after delivery.deterministic_prechecks so the NEW-28 baseline never contains
        toolchain directories, and fails closed unless everything created is git-ignored.
        """
        try:
            policy = load_toolchain_policy(
                self.settings.project_config_root, str(payload["project"])
            )
        except (OSError, ValueError) as error:
            return self._toolchain_blocked(payload, "TOOLCHAIN_CONTRACT_INVALID", str(error), [])
        try:
            # NEW-159 review 3: a re-preparation in the same delivery (e.g. after dependency
            # resolution) keeps the selection it carries; only the first one reads the manifest.
            if isinstance(payload.get("toolchain_preparation"), Mapping):
                toolchain = payload_node_toolchain(self.settings, payload)
            else:
                toolchain = resolve_node_toolchain(self.settings, policy)
        except ToolchainUnavailableError as error:
            return self._toolchain_blocked(payload, error.blocker, str(error), [])
        except ValueError as error:
            return self._toolchain_blocked(payload, "TOOLCHAIN_CONTRACT_INVALID", str(error), [])
        # NEW-159 review 1: a non-default selection travels in toolchain_preparation to
        # the maker/checker invocations (the provider worker never reads the manifest).
        selection = toolchain.selection()
        if policy is None or not policy.enabled:
            return {
                **payload,
                "status": "TOOLCHAIN_SKIPPED",
                "blocker": None,
                "toolchain_preparation": {
                    "status": "TOOLCHAIN_SKIPPED",
                    **({"node_toolchain": selection} if selection is not None else {}),
                },
            }
        worktree = self._bounded_worktree(payload["source_worktree"])
        environment = self._node_toolchain_environment(
            self._toolchain_environment(), toolchain, _DEFAULT_PATH
        )
        toolchain_runner = self._toolchain_runner_for(toolchain)
        before = self._git_status(worktree, ignored=True)
        commands: list[dict[str, Any]] = []
        for command in policy.prepare:
            cwd = (worktree / command.cwd).resolve()
            if not cwd.is_relative_to(worktree) or not cwd.is_dir():
                return self._toolchain_blocked(
                    payload,
                    "TOOLCHAIN_PREPARE_FAILED",
                    f"prepare cwd {command.cwd!r} is not a directory inside the worktree",
                    commands,
                )
            argv = render_argv(command.argv, self.settings)
            record: dict[str, Any] = {"cwd": command.cwd, "argv": argv}
            started = time.monotonic()
            try:
                result = toolchain_runner.run(
                    argv[0],
                    argv[1:],
                    cwd=cwd,
                    timeout_seconds=command.timeout_seconds,
                    environment=environment,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                commands.append({**record, "status": "TIMED_OUT"})
                return self._toolchain_blocked(
                    payload,
                    "TOOLCHAIN_PREPARE_FAILED",
                    f"{argv[0]} timed out after {command.timeout_seconds}s",
                    commands,
                )
            except (OSError, ValueError) as error:
                commands.append({**record, "status": "NOT_STARTED"})
                return self._toolchain_blocked(
                    payload, "TOOLCHAIN_PREPARE_FAILED", f"{argv[0]}: {error}", commands
                )
            record["duration_seconds"] = round(time.monotonic() - started, 1)
            record["return_code"] = result.return_code
            if result.return_code:
                commands.append({**record, "status": "FAILED"})
                # pnpm reports errors on stdout; uv on stderr. Both are already redacted.
                return self._toolchain_blocked(
                    payload,
                    "TOOLCHAIN_PREPARE_FAILED",
                    result.stderr.strip() or result.stdout,
                    commands,
                )
            commands.append({**record, "status": "PASS"})
        after = self._git_status(worktree, ignored=True)
        created = {path: code for path, code in after.items() if before.get(path) != code}
        not_ignored = sorted(path for path, code in created.items() if code == "??")
        tracked = sorted(path for path, code in created.items() if code not in {"??", "!!"})
        if not_ignored:
            return self._toolchain_blocked(
                payload,
                "TOOLCHAIN_ARTIFACTS_NOT_IGNORED",
                "untracked paths that are not git-ignored: " + ", ".join(not_ignored[:50]),
                commands,
            )
        if tracked:
            return self._toolchain_blocked(
                payload,
                "TOOLCHAIN_MODIFIED_TRACKED_FILES",
                "prepare commands modified tracked files: " + ", ".join(tracked[:50]),
                commands,
            )
        preparation: dict[str, Any] = {
            "status": "TOOLCHAIN_READY",
            "commands": commands,
            "ignored_paths_created": sorted(path for path, code in created.items() if code == "!!")[
                :100
            ],
            "test_commands": [item.model_dump() for item in policy.test_commands],
        }
        if selection is not None:
            preparation["node_toolchain"] = selection
        return {
            **payload,
            "status": "TOOLCHAIN_READY",
            "blocker": None,
            "toolchain_preparation": preparation,
        }

    def _toolchain_cleanup_policy(self, project: str) -> ToolchainPolicy | None:
        try:
            policy = enabled_toolchain(self.settings.project_config_root, project)
        except (OSError, ValueError):
            # Without a readable contract nothing is removed; the NEW-28 delta gate then
            # sees any toolchain directories and blocks, which is the fail-closed outcome.
            return None
        return policy if policy is not None and policy.cleanup_paths_ignored_only else None

    def _clean_ignored_toolchain_artifacts(self, value: object) -> None:
        """Remove only git-ignored files (.venv, node_modules) so scanners never see them."""
        worktree = self._bounded_worktree(value)
        before = self._git_status(worktree, ignored=False)
        self._git(worktree, ["clean", "-ffdX", "--quiet"], timeout_seconds=900)
        if self._git_status(worktree, ignored=False) != before:
            raise ValueError("toolchain cleanup changed tracked or non-ignored worktree files")

    def _preexisting_test_failures(
        self, payload: dict[str, Any], failures: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        """NEW-49: evidence that every failing maker command also fails at the base commit.

        ``failures`` are the maker FAILs with their origin ("source" or "deployment").
        A contract test command is rerun at the source base (NEW-49, unchanged). NEW-161
        (review 1, base-only): any other command must be a strictly parsed `node --test`
        invocation; it runs, sandboxed, only at its own repository's BASE commit and is
        pre-existing if that run fails with at least one test-level failure in the
        targeted files. Maker-authored head code is never executed here.

        Returns (None, None) when a FAIL is not eligible and (None, error) when the
        baseline could not be established; both keep MAKER_TEST_FAILURE.
        """
        project = str(payload.get("project", ""))
        try:
            policy = enabled_toolchain(self.settings.project_config_root, project)
        except (OSError, ValueError):
            return None, None
        source = Path(str(payload.get("source_worktree", "")))
        commands: list[ToolchainTestCommand] = []
        node_commands: list[tuple[str, NodeTestCommand, dict[str, Any]]] = []
        for failure in failures:
            reported = str(failure["command"])
            command = (
                match_toolchain_test_command(policy, reported, self.settings, worktree=source)
                if policy is not None
                else None
            )
            if command is not None:
                if command not in commands:
                    commands.append(command)
                continue
            origin = str(failure["origin"])
            side = self._node_test_side(payload, origin)
            if side is None:
                return None, None  # no checkout of that maker's repository is known
            node_command = parse_node_test_command(
                reported, prefixes=self.settings.node_test_path_prefixes, worktree=side[2]
            )
            if node_command is None:
                return None, None
            if all((origin, node_command) != item[:2] for item in node_commands):
                node_commands.append((origin, node_command, failure))
        if len(node_commands) > _NODE_TEST_MAX_COMMANDS:
            return None, None
        base_sha = str(payload.get("source_base_sha", ""))
        try:
            # NEW-159 review 2: the baseline runs under the delivery's carried selection,
            # never a later manifest edit, exactly as the maker and checker did.
            toolchain = payload_node_toolchain(self.settings, payload)
            repository = str(payload["source_repository"])
            results = (
                self._baseline_test_results(
                    project, repository, base_sha, policy, commands, toolchain
                )
                if policy is not None and commands
                else []
            )
            deadline = time.monotonic() + _NODE_TEST_BUDGET_SECONDS
            node_results = []
            for origin, node_command, _ in node_commands:
                side = self._node_test_side(payload, origin)
                assert side is not None  # checked above
                node_results.append(
                    self._node_test_base_result(
                        project,
                        origin,
                        side[0],
                        side[1],
                        policy if origin == "source" else None,
                        node_command,
                        toolchain,
                        deadline,
                    )
                )
        except (
            CommandFailure,
            KeyError,
            OSError,
            ValueError,
            WorktreeError,
            subprocess.TimeoutExpired,
        ) as error:
            detail = f"{type(error).__name__}: {error}"
            return None, str(SecretRedactor().redact_text(detail).value)[-_TOOLCHAIN_STDERR_TAIL:]
        evidence: list[dict[str, Any]] = []
        for command, result in zip(commands, results, strict=True):
            if result["exit_code"] == 0:
                return None, None  # the change introduced this failure
            evidence.append(
                {
                    "command": f"cd {command.cwd} && {shlex.join(result['argv'])}",
                    "base_sha": base_sha,
                    "base_exit_code": result["exit_code"],
                    "base_output_tail": result["output_tail"],
                }
            )
        for (origin, node_command, failure), base in zip(node_commands, node_results, strict=True):
            side = self._node_test_side(payload, origin)
            assert side is not None
            item = self._node_test_evidence(origin, node_command, side[1], base, failure)
            if item is None:
                return None, None
            evidence.append(item)
        return evidence, None

    @staticmethod
    def _node_test_side(payload: dict[str, Any], origin: str) -> tuple[str, str, Path] | None:
        """NEW-161 review 1: (repository, base sha, worktree) of the maker that reported."""
        if origin not in {"source", "deployment"}:
            return None
        repository = payload.get(f"{origin}_repository")
        base_sha = payload.get(f"{origin}_base_sha")
        worktree = payload.get(f"{origin}_worktree")
        if not all(isinstance(value, str) and value for value in (repository, base_sha, worktree)):
            return None
        if not re.fullmatch(r"[a-f0-9]{40}", str(base_sha)):
            return None
        return str(repository), str(base_sha), Path(str(worktree))

    @staticmethod
    def _maker_reported(failure: dict[str, Any]) -> dict[str, Any]:
        reported = failure.get("maker")
        if not isinstance(reported, dict):
            return {}
        bounded: dict[str, Any] = {}
        for key, value in sorted(reported.items())[:12]:
            if isinstance(value, str):
                bounded[str(key)[:40]] = str(SecretRedactor().redact_text(value).value)[
                    -_BASELINE_OUTPUT_TAIL:
                ]
            elif isinstance(value, bool | int | float) or value is None:
                bounded[str(key)[:40]] = value
        return bounded

    @classmethod
    def _node_test_evidence(
        cls,
        origin: str,
        command: NodeTestCommand,
        base_sha: str,
        base: dict[str, Any],
        failure: dict[str, Any],
    ) -> dict[str, Any] | None:
        """NEW-161 review 1: pre-existing iff the base fails with a test-level failure."""
        test_failures = base.get("test_failures")
        if not isinstance(test_failures, dict) or base["exit_code"] == 0:
            return None  # paths matched nothing, malformed report, or passes at the base
        failing: Counter[str] = Counter(test_failures)
        if not failing:
            return None  # only file-level (import, syntax, process, hook) failures
        other: Counter[str] = Counter(base.get("other_failures") or {})
        return {
            "command": command.display,
            "kind": "node_test",
            "origin": origin,
            "base_sha": base_sha,
            "base_exit_code": base["exit_code"],
            "base_output_tail": base["output_tail"],
            "base_test_files": base["files"][:EVIDENCE_TEST_NAMES],
            "base_test_file_count": len(base["files"]),
            "base_failing_count": sum(failing.values()),
            "base_failing_tests": bounded_names(failing),
            "base_other_failure_count": sum(other.values()),
            "base_other_failures": bounded_names(other),
            "maker_reported": cls._maker_reported(failure),
            "head_reexecuted": False,
            "note": (
                "Base-only check: the head was not re-executed, so a regression inside "
                "this already-failing non-contract suite is not detected."
            ),
        }

    @staticmethod
    def _node_test_key(
        command: NodeTestCommand, policy: ToolchainPolicy | None, toolchain: NodeToolchain
    ) -> str:
        identity: dict[str, Any] = {
            "kind": "node_test",
            "reporter": REPORTER_VERSION,
            "cwd": command.cwd,
            "argv": command.argv,
            # The checkout is prepared by the contract (if any): part of the identity.
            "prepare": (
                [item.model_dump(mode="json") for item in policy.prepare]
                if policy is not None
                else None
            ),
        }
        selection = toolchain.selection()
        if selection is not None:
            identity["node_toolchain"] = selection
        return json.dumps(identity, sort_keys=True)

    @staticmethod
    def _cached_node_test_result(value: object) -> bool:
        if not isinstance(value, dict) or value.get("kind") != "node_test":
            return False

        def counts(item: object) -> bool:
            return isinstance(item, dict) and all(
                isinstance(name, str) and isinstance(count, int) and count > 0
                for name, count in item.items()
            )

        files = value.get("files")
        return (
            isinstance(value.get("exit_code"), int)
            and isinstance(files, list)
            and all(isinstance(item, str) for item in files)
            and isinstance(value.get("output_tail"), str)
            # NEW-186: an entry without a report (cached before NEW-186 when the sandbox
            # never started) is rerun, never trusted.
            and counts(value.get("test_failures"))
            and counts(value.get("other_failures"))
        )

    def _node_test_base_result(
        self,
        project: str,
        origin: str,
        repository: str,
        base_sha: str,
        policy: ToolchainPolicy | None,
        command: NodeTestCommand,
        toolchain: NodeToolchain,
        deadline: float,
    ) -> dict[str, Any]:
        """NEW-161: the command's result at the base, cached like NEW-49 (per base)."""
        root = self.settings.toolchain_cache_root / "baselines" / Project(project).value
        if origin == "deployment":
            # The deployment repository has its own, never prepared, checkout.
            root = root / "deployment"
        root = root / base_sha
        root.mkdir(parents=True, exist_ok=True, mode=0o750)
        results_path = root / "results.json"
        reporter = root / f"node-test-reporter-v{REPORTER_VERSION}.mjs"
        key = self._node_test_key(command, policy, toolchain)
        with (root / ".lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            cached = self._cached_baseline_results(results_path)
            if self._cached_node_test_result(cached.get(key)):
                return cached[key]
            if not reporter.is_file() or reporter.read_text(encoding="utf-8") != REPORTER_SOURCE:
                self._atomic_text(reporter, REPORTER_SOURCE)
            worktree = self._baseline_worktree(root, repository, base_sha, policy, toolchain)
            cached[key] = self._run_node_test(worktree, command, toolchain, reporter, deadline)
            self._atomic_text(results_path, json.dumps(cached, indent=2, sort_keys=True))
            return cached[key]

    def _run_node_test(
        self,
        checkout: Path,
        command: NodeTestCommand,
        toolchain: NodeToolchain,
        reporter: Path,
        deadline: float,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": "node_test",
            "cwd": command.cwd,
            "argv": command.argv,
            "files": [],
            "exit_code": -1,
            "output_tail": "",
            "test_failures": None,
            "other_failures": None,
            "passed": 0,
        }
        files = expand_test_paths(checkout, command)
        if files is None:
            result["output_tail"] = "a test path matched no file, a symlink or too many entries"
            return result
        result["files"] = files
        timeout_seconds = int(min(_NODE_TEST_TIMEOUT_SECONDS, deadline - time.monotonic()))
        if timeout_seconds < _NODE_TEST_MIN_SECONDS:
            raise ValueError("the node test baseline budget is exhausted")
        node = (toolchain.node_bin / "node").resolve()
        if not node.is_file():
            raise ValueError(f"node executable {node} is missing")
        nonce = secrets.token_hex(16)
        # NEW-161 review 3: the trusted base writes to a per-run copy-on-write layer
        # (removed below) instead of failing on a read-only checkout.
        overlay_root = self.settings.toolchain_cache_root / "node-test-overlay"
        overlay_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        layer = Path(tempfile.mkdtemp(prefix="run-", dir=overlay_root))
        upper, work = layer / "upper", layer / "work"
        upper.mkdir(mode=0o700)
        work.mkdir(mode=0o700)
        try:
            arguments = sandbox_argv(
                checkout=checkout.resolve(),
                command=command,
                files=files,
                node=node,
                reporter=reporter.resolve(),
                nonce=nonce,
                variables=toolchain.variables,
                overlay=(upper.resolve(), work.resolve()),
            )
            runner = self.baseline_runner or SafeCommandRunner(
                {"bwrap": self.settings.bwrap_executable}
            )
            # Output is held in memory as a bounded tail only.
            run = runner.run(
                "bwrap",
                arguments,
                cwd=checkout,
                timeout_seconds=timeout_seconds,
                check=False,
                max_output_bytes=MAX_REPORT_BYTES,
            )
        finally:
            _remove_overlay_layer(layer)
        report = parse_reporter_output(run.stdout, nonce, files, command.cwd)
        visible = "\n".join(line for line in run.stdout.splitlines() if nonce not in line)
        text = str(SecretRedactor().redact_text(f"{visible}\n{run.stderr}").value)
        result["exit_code"] = run.return_code
        result["output_tail"] = text.strip()[-_BASELINE_OUTPUT_TAIL:]
        if report is None:
            # NEW-186: no report block means the sandbox or node never ran the suite
            # (falcone #1099: bwrap could not mount /proc). Fail visibly as a baseline
            # error and never cache it, instead of a silent "not pre-existing".
            raise ValueError(
                f"node test sandbox produced no report (exit {run.return_code}): "
                f"{result['output_tail'][-300:]}"
            )
        result["test_failures"] = dict(sorted(report.test_failures.items()))
        result["other_failures"] = dict(sorted(report.other_failures.items()))
        result["passed"] = report.passed
        return result

    @staticmethod
    def _baseline_key(cwd: str, argv: list[str], toolchain: NodeToolchain | None = None) -> str:
        identity: dict[str, Any] = {"argv": argv, "cwd": cwd}
        selection = toolchain.selection() if toolchain is not None else None
        if selection is not None:
            # NEW-159 review 1: a non-default node/pnpm is part of the cache identity; the
            # default key is unchanged, so existing baseline caches stay valid.
            identity["node_toolchain"] = selection
        return json.dumps(identity, sort_keys=True)

    @staticmethod
    def _baseline_prepared_marker(
        base_sha: str, toolchain: NodeToolchain | None, *, contract: bool = True
    ) -> str:
        selection = toolchain.selection() if toolchain is not None else None
        # NEW-161: a project without a contract has an unprepared checkout; a later
        # contract never reuses it (and a contract checkout keeps its NEW-159 marker).
        unprepared = "" if contract else " unprepared"
        if selection is None:
            return f"{base_sha}{unprepared}\n"  # byte-identical to the pre-NEW-159 marker
        return f"{base_sha}{unprepared} {json.dumps(selection, sort_keys=True)}\n"

    @staticmethod
    def _cached_baseline_results(path: Path) -> dict[str, dict[str, Any]]:
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}  # a torn or foreign file is recomputed, never trusted
        if not isinstance(raw, dict):
            return {}
        return {
            key: value
            for key, value in raw.items()
            if isinstance(value, dict)
            and isinstance(value.get("exit_code"), int)
            and isinstance(value.get("argv"), list)
        }

    def _baseline_test_results(
        self,
        project: str,
        repository: str,
        base_sha: str,
        policy: ToolchainPolicy,
        commands: list[ToolchainTestCommand],
        toolchain: NodeToolchain,
    ) -> list[dict[str, Any]]:
        """Run the contract commands at the base once per (project, base, argv), then cache."""
        if not re.fullmatch(r"[a-f0-9]{40}", base_sha):
            raise ValueError("the maker test baseline requires the source base commit")
        root = self.settings.toolchain_cache_root / "baselines" / Project(project).value / base_sha
        root.mkdir(parents=True, exist_ok=True, mode=0o750)
        results_path = root / "results.json"
        keys = [
            self._baseline_key(command.cwd, render_argv(command.argv, self.settings), toolchain)
            for command in commands
        ]
        with (root / ".lock").open("a") as lock:
            # Concurrent ChangeSets on the same base wait for one checkout and one run.
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            cached = self._cached_baseline_results(results_path)
            if any(key not in cached for key in keys):
                worktree = self._baseline_worktree(root, repository, base_sha, policy, toolchain)
                default_path = f"{self.settings.toolchain_pnpm_executable.parent}:{_DEFAULT_PATH}"
                environment = self._node_toolchain_environment(
                    {
                        **self._toolchain_environment(),
                        "PATH": default_path,
                        # As for the agents (NEW-48), so mypy runs as it did for the maker.
                        "MYPY_CACHE_DIR": "/dev/null",
                    },
                    toolchain,
                    default_path,
                )
                for command, key in zip(commands, keys, strict=True):
                    if key not in cached:
                        cached[key] = self._run_baseline_test(
                            worktree, command, environment, toolchain
                        )
                        self._atomic_text(
                            results_path, json.dumps(cached, indent=2, sort_keys=True)
                        )
            return [cached[key] for key in keys]

    def _baseline_worktree(
        self,
        root: Path,
        repository: str,
        base_sha: str,
        policy: ToolchainPolicy | None,
        toolchain: NodeToolchain | None = None,
    ) -> Path:
        """A detached, toolchain-prepared checkout of base_sha owned by the scan worker.

        NEW-161: without a contract (policy None) nothing is prepared, as for the maker.
        """
        worktree = root / "worktree"
        prepared = root / "prepared"
        if toolchain is None:
            toolchain = resolve_node_toolchain(self.settings, policy)
        # NEW-159 review 1: the checkout is reused only if it was prepared with the same
        # node/pnpm selection (the default marker is the pre-NEW-159 "<sha>\n").
        marker = self._baseline_prepared_marker(base_sha, toolchain, contract=policy is not None)
        if (
            prepared.is_file()
            and prepared.read_text(encoding="utf-8") == marker
            and worktree.is_dir()
            and not worktree.is_symlink()
        ):
            if self._git(worktree, ["rev-parse", "HEAD"]).strip() == base_sha:
                return worktree
        prepared.unlink(missing_ok=True)
        if worktree.is_symlink():
            raise ValueError("baseline worktree must not be a symlink")
        if worktree.exists():
            shutil.rmtree(worktree)  # a partial checkout or prepare from an earlier attempt
        self._clone_detached(repository, base_sha, worktree, cwd=root)
        environment = self._node_toolchain_environment(
            self._toolchain_environment(), toolchain, _DEFAULT_PATH
        )
        toolchain_runner = self._toolchain_runner_for(toolchain)
        resolved_root = worktree.resolve()
        for command in policy.prepare if policy is not None else []:
            cwd = (worktree / command.cwd).resolve()
            if not cwd.is_relative_to(resolved_root) or not cwd.is_dir():
                raise ValueError(f"baseline prepare cwd {command.cwd!r} is not in the checkout")
            argv = render_argv(command.argv, self.settings)
            result = toolchain_runner.run(
                argv[0],
                argv[1:],
                cwd=cwd,
                timeout_seconds=command.timeout_seconds,
                environment=environment,
                check=False,
            )
            if result.return_code:
                raise ValueError(
                    f"baseline prepare {argv[0]} failed with {result.return_code}: "
                    + (result.stderr.strip() or result.stdout)[-_TOOLCHAIN_STDERR_TAIL:]
                )
        prepared.write_text(marker, encoding="utf-8")
        return worktree

    def _baseline_executables(
        self, root: Path, cwd: Path, executable: str, toolchain: NodeToolchain | None = None
    ) -> dict[str, Path]:
        """Contract test commands may run the installers, timeout, or a checkout-local tool."""
        executables = {
            **prepare_executables(self.settings, toolchain),
            "timeout": self.settings.timeout_executable,
        }
        if "/" in executable:
            # normpath, not resolve: .venv/bin entries may be symlinks to the interpreter.
            local = Path(os.path.normpath(cwd / executable))
            if not local.is_relative_to(root) or not local.is_file():
                raise ValueError(f"baseline test executable {executable!r} is not in the checkout")
            executables[executable] = local
        return executables

    def _run_baseline_test(
        self,
        worktree: Path,
        command: ToolchainTestCommand,
        environment: dict[str, str],
        toolchain: NodeToolchain | None = None,
    ) -> dict[str, Any]:
        root = worktree.resolve()
        cwd = (worktree / command.cwd).resolve()
        if not cwd.is_relative_to(root) or not cwd.is_dir():
            raise ValueError(f"baseline test cwd {command.cwd!r} is not in the checkout")
        argv = render_argv(command.argv, self.settings)
        runner = self.baseline_runner or SafeCommandRunner(
            self._baseline_executables(root, cwd, argv[0], toolchain)
        )
        # argv is normally timeout-wrapped (NEW-46); this bound also covers unwrapped ones.
        result = runner.run(
            argv[0],
            argv[1:],
            cwd=cwd,
            timeout_seconds=_BASELINE_TEST_TIMEOUT_SECONDS,
            environment=environment,
            check=False,
        )
        # Only the exit code and a short tail of the (already redacted) output are kept.
        output = str(SecretRedactor().redact_text(f"{result.stdout}\n{result.stderr}").value)
        return {
            "cwd": command.cwd,
            "argv": argv,
            "exit_code": result.return_code,
            "output_tail": output.strip()[-_BASELINE_OUTPUT_TAIL:],
        }

    def _validate_changeset(self, payload: dict[str, Any]) -> dict[str, Any]:
        checker = payload.get("agent_result")
        if not isinstance(checker, dict) or not isinstance(checker.get("structured_result"), dict):
            raise ValueError("ChangeSet validation requires the independent checker result")
        if checker["structured_result"].get("decision") != "PASS":
            return {
                **payload,
                "status": "REQUEST_CHANGES",
                "blocker": "INDEPENDENT_CHECKER_DID_NOT_PASS",
            }
        # NEW-161 review 1: each FAIL keeps the maker (repository) it came from.
        failed_commands: list[dict[str, Any]] = []
        for key in ("source_maker_result", "deployment_maker_result"):
            maker = payload.get(key)
            if not isinstance(maker, dict) or not isinstance(maker.get("structured_result"), dict):
                if failed_commands:
                    return {**payload, "status": "BLOCKED", "blocker": "MAKER_TEST_FAILURE"}
                raise ValueError(f"ChangeSet validation requires {key}")
            tests = maker["structured_result"].get("tests")
            if not isinstance(tests, list):
                return {**payload, "status": "BLOCKED", "blocker": "MAKER_TEST_FAILURE"}
            failed_commands.extend(
                {
                    "origin": key.removesuffix("_maker_result"),
                    "command": str(test.get("command", "")),
                    "maker": test,
                }
                for test in tests
                if isinstance(test, dict) and test.get("status") == "FAIL"
            )
        if failed_commands:
            # NEW-49: the independent checker PASSed (above). A maker FAIL still blocks
            # unless every failing command is a contract test command that the scan
            # worker also sees fail at the source BASE commit (a pre-existing failure,
            # e.g. #993's mypy error in an untouched file). Anything else fails closed.
            preexisting, baseline_error = self._preexisting_test_failures(payload, failed_commands)
            if preexisting is None:
                maker_blocked: dict[str, Any] = {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "MAKER_TEST_FAILURE",
                }
                if baseline_error is not None:
                    maker_blocked["baseline_error"] = baseline_error
                return maker_blocked
            payload = {**payload, "preexisting_test_failures": preexisting}
        change_set_id = UUID(str(payload["change_set_id"]))
        evidence_root = self.settings.evidence_root / "scan" / str(change_set_id) / "changes"
        evidence_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        baseline = self._load_baseline(change_set_id)
        changes: dict[str, Any] = {}
        blocked = False
        empty_change = False
        validation_findings: list[str] = []
        toolchain = self._toolchain_cleanup_policy(str(payload.get("project", "")))
        for name, path_key, base_key in (
            ("source", "source_worktree", "source_base_sha"),
            ("deployment", "deployment_worktree", "deployment_base_sha"),
        ):
            worktree = Path(str(payload[path_key]))
            if toolchain is not None:
                # The prepared .venv/node_modules are git-ignored and absent from the
                # prechecks baseline; scanning them would flag thousands of "introduced"
                # findings. git clean -X never touches tracked or non-ignored files.
                self._clean_ignored_toolchain_artifacts(worktree)
            base_sha = str(payload[base_key])
            if self.manager.whitespace_check_failed(worktree, base_sha=base_sha):
                # NEW-180: the next correction round's maker is told to fix it instead of
                # the delivery failing as a policy violation inside change_evidence's own
                # `git diff --check`. Git's output (names, offending lines) is not relayed.
                return {
                    **payload,
                    "status": "REQUEST_CHANGES",
                    "blocker": "CHANGESET_VALIDATION_FINDINGS",
                    "validation_findings": [
                        f"{name}: `git diff --check {base_sha}` reports whitespace errors or "
                        "conflict markers in the change; run it in the worktree and fix "
                        "every reported line, generated files such as test snapshots included"
                    ],
                }
            summary, diff = self.manager.change_evidence(worktree, base_sha=str(payload[base_key]))
            reported = summary.get("agent_config_paths")
            refused = sorted(
                set(self._agent_config_paths(summary.get("changed_paths")))
                | {str(item) for item in (reported if isinstance(reported, list) else [])}
            )
            if refused:
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "AGENT_CONFIG_CHANGE_REFUSED",
                    "agent_config_paths": [f"{name}: {path}" for path in refused[:40]],
                    "operator_message": (
                        f"the {name} ChangeSet adds or modifies agent configuration "
                        f"({', '.join(refused[:5])}); .codex/, .claude/ and .mcp.json are "
                        "loaded by agent CLIs and are never delivered automatically. "
                        "Review the change and deliver it manually if the issue targets it."
                    ),
                }
            if name == "source" and summary["changed_file_count"] == 0:
                blocked = True
                empty_change = True
            diff_path = evidence_root / f"{name}.patch"
            safe_diff = str(SecretRedactor().redact_text(diff).value)
            # NEW-80: the scanners reuse an existing report in their output directory
            # (activity retries). Keyed only by the ChangeSet, a later attempt re-read
            # the first attempt's reports: #1125 kept "introducing" the grpc CVE its
            # new head no longer had. Scan each head into its own directory.
            head = str(summary.get("head_sha") or "")[:12]
            output = evidence_root / (f"{name}-postchecks-{head}" if head else f"{name}-postchecks")
            secret = self.scanner.secret_scan(worktree, output)
            trivy = self.scanner.vulnerability_scan(worktree, output)
            semgrep = self.scanner.semgrep(worktree, output)
            self.scanner.sbom(worktree, output)
            semgrep_findings = len(semgrep.get("results", []))
            severe = self._severe_vulnerability_count(trivy)
            counts = self._finding_counts(worktree, secret, trivy, semgrep)
            side_baseline = baseline.get(name) if baseline is not None else None
            introduced = self._introduced_findings(side_baseline, counts)
            validation_findings.extend(
                f"{name}: {item}" for item in self._introduced_identities(side_baseline, counts)
            )
            if any(introduced.values()):
                blocked = True
            secrets_introduced = bool(introduced["secrets"])
            artifacts = self._publish_reports(
                payload,
                sorted(report for report in output.iterdir() if report.is_file()),
                filename_prefix=f"postchecks-{name}",
            )
            if secrets_introduced:
                artifacts.append(
                    {
                        "status": "WITHHELD_SECRET_FINDINGS",
                        "filename": diff_path.name,
                        "sha256": hashlib.sha256(safe_diff.encode()).hexdigest(),
                    }
                )
            else:
                self._atomic_text(diff_path, safe_diff)
                artifacts.extend(
                    self._publish_reports(
                        payload,
                        [diff_path],
                        filename_prefix=f"postchecks-{name}",
                    )
                )
            changes[name] = {
                **summary,
                "release_impact": self._classify_release_impact(
                    repository=str(payload[f"{name}_repository"]),
                    changed_paths=summary["changed_paths"],
                    diff=diff,
                ),
                "diff_path": (None if secrets_introduced else str(diff_path)),
                "diff_withheld": secrets_introduced,
                "introduced_findings": introduced,
                "diff_redacted_sha256": hashlib.sha256(safe_diff.encode()).hexdigest(),
                "secret_status": secret.get("status"),
                "semgrep_findings": semgrep_findings,
                "high_or_critical_vulnerabilities": severe,
                "artifacts": artifacts,
            }
        if not blocked:
            for repository_key, worktree_key in (
                ("source_repository", "source_worktree"),
                ("deployment_repository", "deployment_worktree"),
            ):
                self.manager.grant_delivery_read(
                    str(payload[repository_key]),
                    Path(str(payload[worktree_key])),
                )
            self.manager.grant_release_read(
                str(payload["deployment_repository"]),
                Path(str(payload["deployment_worktree"])),
            )
        if blocked and not empty_change and validation_findings and baseline is not None:
            # NEW-76: introduced findings are the maker's to fix; name them (identities
            # only, never secret values) so the next correction attempt can.
            return {
                **payload,
                "status": "REQUEST_CHANGES",
                "blocker": "CHANGESET_VALIDATION_FINDINGS",
                "change_evidence": changes,
                "validation_findings": validation_findings[:40],
            }
        return {
            **payload,
            "status": "BLOCKED" if blocked else "LOCAL_VALIDATION_PASS",
            "blocker": "CHANGESET_VALIDATION_FINDINGS" if blocked else None,
            "change_evidence": changes,
        }

    @staticmethod
    def _agent_config_paths(changed_paths: object) -> list[str]:
        """NEW-130: changed paths that agent CLIs load as configuration.

        Codex loads every `.codex/` folder between its cwd and the project root, Claude
        Code loads `.claude/` and `.mcp.json`; a maker that writes them could start an
        MCP server or hook in a later run. Any component, including the last, matches.
        The worktree manager adds what a path list cannot show (NUL-delimited raw
        names, renames split, symlink targets and aliases) as `agent_config_paths`.
        """
        if not isinstance(changed_paths, list):
            return []
        return sorted(str(path) for path in changed_paths if is_agent_config_path(str(path)))

    @staticmethod
    def _classify_release_impact(
        *, repository: str, changed_paths: object, diff: str
    ) -> dict[str, Any]:
        """Classify release gates without persisting source or secret-bearing snippets."""
        if not isinstance(changed_paths, list) or any(
            not isinstance(path, str) for path in changed_paths
        ):
            raise ValueError("release impact requires bounded changed paths")
        normalized_paths = "\n".join(str(path).lower() for path in changed_paths)
        searchable = f"{normalized_paths}\n{diff.lower()}"
        patterns = {
            "migration": (
                r"(?:^|/)(?:migrations?|alembic|flyway|liquibase)(?:/|$)",
                r"\b(?:create|alter|drop|truncate)\s+(?:table|index|schema|database)\b",
                r"\b(?:alembic|flyway|liquibase|schema migration)\b",
            ),
            "secret_lifecycle": (
                r"\b(?:externalsecret|sealedsecret|secretstore|clustersecretstore)\b",
                r"\b(?:openbao|vault|sops|secret lifecycle|encryption key)\b",
                r"(?:^|/)(?:secrets?|external-secrets|openbao|vault)(?:/|$)",
            ),
            "cluster_scoped": (
                r"\bkind:\s*(?:customresourcedefinition|clusterrole|clusterrolebinding|namespace|storageclass|validatingwebhookconfiguration|mutatingwebhookconfiguration)\b",
                r"(?:^|/)(?:crds?|clusterroles?|webhooks?)(?:/|$)",
            ),
            "auth_or_tenant_boundary": (
                r"(?:^|/)(?:auth|iam|rbac|keycloak|tenan(?:t|cy)|authorization)(?:/|$)",
                (
                    r"\b(?:tenant isolation|authorization|authentication|keycloak|"
                    r"clusterrolebinding)\b"
                ),
            ),
            "destructive_lifecycle": (
                r"\b(?:drop\s+(?:table|schema|database)|truncate\s+table|delete\s+from)\b",
                r"\b(?:purge|destructive lifecycle|backup restore)\b",
            ),
        }
        matched = sorted(
            category
            for category, expressions in patterns.items()
            if any(re.search(expression, searchable, re.MULTILINE) for expression in expressions)
        )
        deployment_repository = repository in frozenset(SEPARATE_DEPLOYMENT_REPOSITORY_SLUGS)
        risk_floor = "R3" if matched else "R2" if deployment_repository and changed_paths else "R1"
        classifier = {
            "version": 1,
            "repository": repository,
            "changed_paths_sha256": hashlib.sha256(normalized_paths.encode()).hexdigest(),
            "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
            "categories": matched,
            "migration": "migration" in matched,
            "risk_floor": risk_floor,
        }
        classifier["sha256"] = hashlib.sha256(
            json.dumps(classifier, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return classifier

    def _baseline_path(self, change_set_id: UUID) -> Path:
        prechecks = self.settings.evidence_root / "scan" / str(change_set_id) / "prechecks"
        return prechecks / "baseline.json"

    def _load_baseline(self, change_set_id: UUID) -> dict[str, Any] | None:
        path = self._baseline_path(change_set_id)
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None

    @classmethod
    def _finding_counts(
        cls,
        worktree: Path,
        secret: dict[str, Any],
        trivy: dict[str, Any],
        semgrep: dict[str, Any],
    ) -> dict[str, dict[str, int]]:
        """Line-independent finding identities with multiplicity, so edits that only shift
        lines match the baseline while a second occurrence of the same rule still counts."""
        root = f"{worktree.resolve()}/"

        def relative(value: Any) -> str:
            text = str(value or "")
            return text[len(root) :] if text.startswith(root) else text

        def key(*parts: Any) -> str:
            return json.dumps([str(part or "") for part in parts])

        counts: dict[str, Counter[str]] = {
            "secrets": Counter(),
            "semgrep": Counter(),
            "vulnerabilities": Counter(),
        }
        for finding in secret.get("findings") or []:
            if isinstance(finding, dict):
                counts["secrets"][key(finding.get("RuleID"), relative(finding.get("File")))] += 1
        for finding in semgrep.get("results") or []:
            if isinstance(finding, dict):
                counts["semgrep"][key(finding.get("check_id"), relative(finding.get("path")))] += 1
        for result in trivy.get("Results") or []:
            if not isinstance(result, dict):
                continue
            target = relative(result.get("Target"))
            for category, fields in (
                ("Vulnerabilities", ("VulnerabilityID", "PkgName", "InstalledVersion")),
                ("Misconfigurations", ("ID", "AVDID")),
                ("Secrets", ("RuleID",)),
            ):
                for finding in result.get(category) or []:
                    if isinstance(finding, dict) and str(finding.get("Severity", "")).upper() in {
                        "HIGH",
                        "CRITICAL",
                    }:
                        identity = key(category, target, *(finding.get(field) for field in fields))
                        counts["vulnerabilities"][identity] += 1
        return {name: dict(counter) for name, counter in counts.items()}

    @staticmethod
    def _introduced_findings(
        baseline: dict[str, Any] | None, current: dict[str, dict[str, int]]
    ) -> dict[str, int]:
        # No recorded baseline (e.g. prechecks from an older release): fail closed and
        # treat every current finding as introduced, i.e. the previous absolute gate.
        introduced: dict[str, int] = {}
        for category, findings in current.items():
            before = baseline.get(category) if isinstance(baseline, dict) else None
            before = before if isinstance(before, dict) else {}
            introduced[category] = sum(
                max(0, count - int(before.get(identity, 0))) for identity, count in findings.items()
            )
        return introduced

    @staticmethod
    def _introduced_identities(
        baseline: dict[str, Any] | None, current: dict[str, dict[str, int]]
    ) -> list[str]:
        """NEW-76: readable identities of introduced findings (no secret values)."""
        labels = {"secrets": "secret", "semgrep": "semgrep", "vulnerabilities": "trivy"}
        identities: list[str] = []
        for category, findings in current.items():
            before = baseline.get(category) if isinstance(baseline, dict) else None
            before = before if isinstance(before, dict) else {}
            for identity, count in sorted(findings.items()):
                if count <= int(before.get(identity, 0)):
                    continue
                try:
                    parts = [str(part) for part in json.loads(identity) if part]
                except (TypeError, ValueError):
                    parts = [identity]
                identities.append(f"{labels.get(category, category)} " + " | ".join(parts))
                if len(identities) >= 20:
                    return identities
        return identities

    @staticmethod
    def _severe_vulnerability_count(report: dict[str, Any]) -> int:
        count = 0
        results = report.get("Results")
        if not isinstance(results, list):
            return 0
        for result in results:
            if not isinstance(result, dict):
                continue
            for key in ("Vulnerabilities", "Misconfigurations", "Secrets"):
                findings = result.get(key)
                if not isinstance(findings, list):
                    continue
                count += sum(
                    1
                    for finding in findings
                    if isinstance(finding, dict)
                    and str(finding.get("Severity", "")).upper() in {"HIGH", "CRITICAL"}
                )
        return count

    @staticmethod
    def _atomic_text(path: Path, value: str) -> None:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(value.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)

    def _publish_reports(
        self,
        payload: dict[str, Any],
        reports: list[Path],
        *,
        filename_prefix: str,
        category: str = "changes",
    ) -> list[dict[str, Any]]:
        orchestration = payload.get("orchestration")
        if not isinstance(orchestration, dict) or not isinstance(
            orchestration.get("workflow_id"), str
        ):
            if self.settings.environment == "production":
                raise ValueError("production evidence requires its Temporal workflow identity")
            workflow_id = "development-local"
        else:
            workflow_id = str(orchestration["workflow_id"])
        project = Project(str(payload["project"]))
        campaign_id = UUID(str(payload["campaign_id"])) if payload.get("campaign_id") else None
        change_set_id = (
            UUID(str(payload["change_set_id"])) if payload.get("change_set_id") else None
        )
        receipts: list[dict[str, Any]] = []
        redactor = SecretRedactor()
        for report in reports:
            # NEW-54: an empty file (e.g. the deployment patch of a source-only ChangeSet,
            # allowed since NEW-41/42) is refused by the evidence transfer ("empty or
            # exceeds the configured size bound") and failed validate_changeset. Record it
            # instead of uploading it.
            if report.stat().st_size == 0:
                receipts.append({"status": "EMPTY_NOT_UPLOADED", "filename": report.name})
                continue
            # Scanner output (trivy "Secrets", semgrep matched lines, SBOM fields) is not
            # pre-redacted; the ingest service rejects any evidence it would still redact.
            text = report.read_text(encoding="utf-8", errors="replace")
            content_type = "text/plain"
            if report.suffix == ".json":
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    safe = str(redactor.redact_text(text).value)
                else:
                    content_type = "application/json"
                    safe = json.dumps(redactor.redact(parsed).value, indent=2, sort_keys=True)
            else:
                safe = str(redactor.redact_text(text).value)
            if safe != text:
                self._atomic_text(report, safe)
            receipt = self.publisher.publish(
                report,
                project=project,
                workflow_id=workflow_id,
                campaign_id=campaign_id,
                change_set_id=change_set_id,
                category=category,
                filename=f"{filename_prefix}-{report.name}",
                producer="scan-runner",
                content_type=content_type,
                redaction_status=RedactionStatus.REDACTED,
            )
            receipts.append(receipt)
        return receipts

    @staticmethod
    def _sha(value: Any, name: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 40
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"{name} must contain 40 lowercase hexadecimal characters")
        return value

    @classmethod
    def _find_mapping(cls, value: Any, key: str) -> dict[str, Any] | None:
        if isinstance(value, dict):
            candidate = value.get(key)
            if isinstance(candidate, dict):
                return candidate
            for nested in value.values():
                found = cls._find_mapping(nested, key)
                if found is not None:
                    return found
        return None


def _remove_overlay_layer(layer: Path) -> None:
    """NEW-161 review 3: overlayfs leaves its work/work directory mode 000; make every
    directory traversable before removing the per-run layer."""
    for directory, names, _files in os.walk(layer, topdown=True, followlinks=False):
        for name in names:
            path = Path(directory) / name
            if not path.is_symlink():
                with contextlib.suppress(OSError):
                    path.chmod(0o700)
    shutil.rmtree(layer, ignore_errors=True)
