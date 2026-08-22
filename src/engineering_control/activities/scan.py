from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from temporalio.exceptions import ApplicationError

from engineering_control.adapters.command import CommandFailure, SafeCommandRunner
from engineering_control.adapters.kubernetes import KubernetesObserver
from engineering_control.adapters.scanning.tooling import DeterministicScanner
from engineering_control.config import Settings
from engineering_control.evidence.transfer import EvidencePublisher
from engineering_control.models.enums import Project, Provider, RedactionStatus
from engineering_control.security.redaction import SecretRedactor
from engineering_control.worktrees.manager import (
    GitWorktreeManager,
    WorktreeAccess,
    WorktreeAllocation,
    WorktreeError,
)


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
    repository: str = Field(pattern=r"^(falcone|falcone-charts|musematic|musematic-deploy)$")
    worktree_path: Path
    branch: str = Field(pattern=r"^agent/(falcone|musematic)/[a-zA-Z0-9._-]+/[a-f0-9-]{36}$")
    base_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    reason: str = Field(pattern=r"^(RELEASED_RETENTION_EXPIRED|ABANDONED_LEASE_EXPIRED)$")


class ScanActivityHandler:
    """Bounded mirror and worktree operations owned only by scan-runner."""

    supported_steps = frozenset(
        {
            "project.snapshot",
            "audit.deterministic_cell",
            "finding.fresh_snapshot",
            "delivery.allocate_worktrees",
            "delivery.deterministic_prechecks",
            "runtime.deterministic_checks",
            "cleanup.protect_active",
            "cleanup.execute",
        }
    )
    _repositories = {
        Project.FALCONE: ("falcone", "falcone-charts"),
        Project.MUSEMATIC: ("musematic", "musematic-deploy"),
    }

    def __init__(
        self,
        settings: Settings,
        manager: GitWorktreeManager | None = None,
        scanner: DeterministicScanner | None = None,
        publisher: EvidencePublisher | None = None,
        observer_runner: SafeCommandRunner | None = None,
    ) -> None:
        self.settings = settings
        self.manager = manager or GitWorktreeManager(
            mirror_root=settings.mirror_root,
            worktree_root=settings.worktree_root,
            lease_root=settings.lease_root / "worktrees",
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

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if step == "project.snapshot":
                return self._snapshot(payload)
            if step == "audit.deterministic_cell":
                return self._deterministic_cell(payload)
            if step == "finding.fresh_snapshot":
                return self._fresh_finding_snapshot(payload)
            if step == "delivery.allocate_worktrees":
                return self._allocate(payload)
            if step == "delivery.deterministic_prechecks":
                return self._delivery_prechecks(payload)
            if step == "runtime.deterministic_checks":
                return self._runtime_deterministic_checks(payload)
            if step == "cleanup.protect_active":
                return self._protect_cleanup_candidates(payload)
            if step == "cleanup.execute":
                return self._execute_cleanup(payload)
            if step == "delivery.validate_changeset":
                return self._validate_changeset(payload)
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
        except (ValueError, ValidationError, WorktreeError) as error:
            message = str(error)
            error_type = "DirtyWorktree" if "dirty" in message.lower() else "PolicyViolation"
            raise ApplicationError(message, type=error_type, non_retryable=True) from error
        raise ApplicationError(
            f"{step} has no configured deterministic scan handler",
            type="NotConfigured",
            non_retryable=True,
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
        authorization_sha = payload.get("cleanup_authorization_sha256")
        orchestration = self._find_mapping(payload, "orchestration")
        if (
            not isinstance(raw_authorized, list)
            or not isinstance(authorization_sha, str)
            or orchestration is None
            or not isinstance(orchestration.get("workflow_id"), str)
        ):
            raise ValueError("cleanup execution requires controller authorization")
        canonical = json.dumps(
            {
                "workflow_id": orchestration["workflow_id"],
                "candidates": raw_authorized,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        if hashlib.sha256(canonical).hexdigest() != authorization_sha:
            raise ValueError("cleanup authorization changed after controller validation")
        removed: list[str] = []
        absent: list[str] = []
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
            self.manager.cleanup(allocation, evidence_captured=False)
            removed.append(str(candidate.lease_id))
        return {
            **payload,
            "status": "CLEANUP_EXECUTED",
            "removed_lease_ids": removed,
            "already_absent_lease_ids": absent,
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
        evidence_root = (
            self.settings.evidence_root
            / "scan"
            / str(plan.change_set_id)
            / "runtime"
        )
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
        resolved = path.resolve()
        if not resolved.is_file() or not resolved.is_relative_to(root):
            raise ValueError("observer kubeconfig escaped its credential root")
        mode = resolved.stat().st_mode
        if not stat.S_ISREG(mode) or mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError("observer kubeconfig permissions are too broad")
        return resolved

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
        source = self.manager.allocate_audit_snapshot(
            repository=source_repository,
            campaign_id=finding_id,
            base_ref="refs/heads/main",
            expected_base_sha=source_sha,
            require_base_ref_match=False,
        )
        deployment = self.manager.allocate_audit_snapshot(
            repository=deployment_repository,
            campaign_id=finding_id,
            base_ref="refs/heads/main",
            expected_base_sha=deployment_sha,
            require_base_ref_match=False,
        )
        evidence = {
            "finding_id": str(finding_id),
            "source_sha": source.base_sha,
            "deployment_sha": deployment.base_sha,
            "source_worktree": str(source.path),
            "deployment_worktree": str(deployment.path),
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
        source_sha = self._sha(payload.get("source_sha"), "source_sha")
        deployment_sha = self._sha(payload.get("deployment_sha"), "deployment_sha")
        source_repository, deployment_repository = self._repositories[project]
        source = self.manager.snapshot(
            source_repository,
            base_ref="refs/heads/main",
            expected_sha=source_sha,
        )
        deployment = self.manager.snapshot(
            deployment_repository,
            base_ref="refs/heads/main",
            expected_sha=deployment_sha,
        )
        source_worktree = self.manager.allocate_audit_snapshot(
            repository=source_repository,
            campaign_id=campaign_id,
            base_ref="refs/heads/main",
            expected_base_sha=source_sha,
        )
        deployment_worktree = self.manager.allocate_audit_snapshot(
            repository=deployment_repository,
            campaign_id=campaign_id,
            base_ref="refs/heads/main",
            expected_base_sha=deployment_sha,
        )
        return {
            **payload,
            "status": "SNAPSHOT_VERIFIED",
            "source_snapshot": source,
            "deployment_snapshot": deployment,
            "source_worktree": str(source_worktree.path),
            "deployment_worktree": str(deployment_worktree.path),
        }

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
        source = self.manager.allocate(
            repository=request.source_repository,
            project=request.project.value,
            issue=request.issue,
            change_set_id=request.change_set_id,
            base_ref=request.source_base_ref,
            expected_base_sha=request.source_base_sha,
            access=access,
        )
        deployment = self.manager.allocate(
            repository=request.deployment_repository,
            project=request.project.value,
            issue=request.issue,
            change_set_id=request.change_set_id,
            base_ref=request.deployment_base_ref,
            expected_base_sha=request.deployment_base_sha,
            access=access,
        )
        return {
            **payload,
            "status": "WORKTREES_ALLOCATED",
            "source_worktree": str(source.path),
            "source_branch": source.branch,
            "deployment_worktree": str(deployment.path),
            "deployment_branch": deployment.branch,
        }

    def _delivery_prechecks(self, payload: dict[str, Any]) -> dict[str, Any]:
        change_set_id = UUID(str(payload["change_set_id"]))
        evidence_root = self.settings.evidence_root / "scan" / str(change_set_id) / "prechecks"
        summaries: dict[str, Any] = {}
        blocked = False
        for name, path_value in (
            ("source", payload["source_worktree"]),
            ("deployment", payload["deployment_worktree"]),
        ):
            worktree = Path(str(path_value)).resolve()
            root = self.settings.worktree_root.resolve()
            if not worktree.is_dir() or not worktree.is_relative_to(root):
                raise ValueError("precheck worktree escaped its bounded root")
            output = evidence_root / name
            secret = self.scanner.secret_scan(worktree, output)
            trivy = self.scanner.vulnerability_scan(worktree, output)
            semgrep = self.scanner.semgrep(worktree, output)
            self.scanner.sbom(worktree, output)
            semgrep_findings = len(semgrep.get("results", []))
            severe_vulnerabilities = self._severe_vulnerability_count(trivy)
            if secret.get("status") == "FINDINGS" or semgrep_findings or severe_vulnerabilities:
                blocked = True
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
        return {
            **payload,
            "status": "BLOCKED" if blocked else "PRECHECKS_PASS",
            "blocker": "DETERMINISTIC_PRECHECK_FINDINGS" if blocked else None,
            "prechecks": summaries,
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
        for key in ("source_maker_result", "deployment_maker_result"):
            maker = payload.get(key)
            if not isinstance(maker, dict) or not isinstance(maker.get("structured_result"), dict):
                raise ValueError(f"ChangeSet validation requires {key}")
            tests = maker["structured_result"].get("tests")
            if not isinstance(tests, list) or any(
                isinstance(test, dict) and test.get("status") == "FAIL" for test in tests
            ):
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "MAKER_TEST_FAILURE",
                }
        change_set_id = UUID(str(payload["change_set_id"]))
        evidence_root = self.settings.evidence_root / "scan" / str(change_set_id) / "changes"
        evidence_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        changes: dict[str, Any] = {}
        blocked = False
        for name, path_key, base_key in (
            ("source", "source_worktree", "source_base_sha"),
            ("deployment", "deployment_worktree", "deployment_base_sha"),
        ):
            worktree = Path(str(payload[path_key]))
            summary, diff = self.manager.change_evidence(worktree, base_sha=str(payload[base_key]))
            if name == "source" and summary["changed_file_count"] == 0:
                blocked = True
            diff_path = evidence_root / f"{name}.patch"
            safe_diff = str(SecretRedactor().redact_text(diff).value)
            output = evidence_root / f"{name}-postchecks"
            secret = self.scanner.secret_scan(worktree, output)
            trivy = self.scanner.vulnerability_scan(worktree, output)
            semgrep = self.scanner.semgrep(worktree, output)
            self.scanner.sbom(worktree, output)
            semgrep_findings = len(semgrep.get("results", []))
            severe = self._severe_vulnerability_count(trivy)
            if secret.get("status") == "FINDINGS" or semgrep_findings or severe:
                blocked = True
            artifacts = self._publish_reports(
                payload,
                sorted(report for report in output.iterdir() if report.is_file()),
                filename_prefix=f"postchecks-{name}",
            )
            if secret.get("status") == "FINDINGS":
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
                "diff_path": (
                    None if secret.get("status") == "FINDINGS" else str(diff_path)
                ),
                "diff_withheld": secret.get("status") == "FINDINGS",
                "diff_redacted_sha256": hashlib.sha256(safe_diff.encode()).hexdigest(),
                "secret_status": secret.get("status"),
                "semgrep_findings": semgrep_findings,
                "high_or_critical_vulnerabilities": severe,
                "artifacts": artifacts,
            }
        return {
            **payload,
            "status": "BLOCKED" if blocked else "LOCAL_VALIDATION_PASS",
            "blocker": "CHANGESET_VALIDATION_FINDINGS" if blocked else None,
            "change_evidence": changes,
        }

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
        for report in reports:
            content_type = "application/json" if report.suffix == ".json" else "text/plain"
            receipt = self.publisher.publish(
                report,
                project=project,
                workflow_id=workflow_id,
                campaign_id=campaign_id,
                change_set_id=change_set_id,
                category="changes",
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
