from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from temporalio.exceptions import ApplicationError

from engineering_control.config import Settings
from engineering_control.models.enums import Project, Provider
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
        }
    )
    _repositories = {
        Project.FALCONE: ("falcone", "falcone-charts"),
        Project.MUSEMATIC: ("musematic", "musematic-deploy"),
    }

    def __init__(self, settings: Settings, manager: GitWorktreeManager | None = None) -> None:
        self.settings = settings
        self.manager = manager or GitWorktreeManager(
            mirror_root=settings.mirror_root,
            worktree_root=settings.worktree_root,
            lease_root=settings.lease_root / "worktrees",
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
