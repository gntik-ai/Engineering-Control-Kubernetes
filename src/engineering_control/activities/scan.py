from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from temporalio.exceptions import ApplicationError

from engineering_control.adapters.command import SafeCommandRunner
from engineering_control.adapters.scanning.tooling import DeterministicScanner
from engineering_control.config import Settings
from engineering_control.evidence.transfer import EvidencePublisher
from engineering_control.models.enums import Project, Provider, RedactionStatus
from engineering_control.security.redaction import SecretRedactor
from engineering_control.worktrees.manager import (
    GitWorktreeManager,
    WorktreeAccess,
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


class ScanActivityHandler:
    """Bounded mirror and worktree operations owned only by scan-runner."""

    supported_steps = frozenset(
        {
            "project.snapshot",
            "audit.deterministic_cell",
            "finding.fresh_snapshot",
            "delivery.allocate_worktrees",
            "delivery.deterministic_prechecks",
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
            if step == "delivery.validate_changeset":
                return self._validate_changeset(payload)
        except (ValueError, ValidationError, WorktreeError) as error:
            message = str(error)
            error_type = "DirtyWorktree" if "dirty" in message.lower() else "PolicyViolation"
            raise ApplicationError(message, type=error_type, non_retryable=True) from error
        raise ApplicationError(
            f"{step} has no configured deterministic scan handler",
            type="NotConfigured",
            non_retryable=True,
        )

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
