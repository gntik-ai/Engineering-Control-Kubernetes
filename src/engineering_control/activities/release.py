from __future__ import annotations

import hashlib
import stat
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from temporalio.exceptions import ApplicationError

from engineering_control.adapters.command import SafeCommandRunner
from engineering_control.adapters.falcone.release import FalconeHelmAdapter, FalconeReleasePlan
from engineering_control.adapters.musematic.gitops import MusematicGitOpsAdapter, MusematicPromotion
from engineering_control.config import Settings
from engineering_control.security.redaction import SecretRedactor


class ReleaseActivityHandler:
    """Deterministic release-only dispatcher; it never accepts prompts or shell strings."""

    def __init__(
        self,
        settings: Settings,
        *,
        helm: FalconeHelmAdapter | None = None,
        gitops: MusematicGitOpsAdapter | None = None,
    ) -> None:
        self.settings = settings
        runner = SafeCommandRunner(
            {
                "git": Path("/usr/bin/git"),
                "helm": settings.helm_executable,
                "argocd": settings.argocd_executable,
            }
        )
        self.helm = helm or FalconeHelmAdapter(runner)
        self.gitops = gitops or MusematicGitOpsAdapter(runner)
        self.redactor = SecretRedactor()

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        if step == "recovery.execute":
            raise ApplicationError(
                "recovery execution requires a typed project-specific recovery adapter",
                type="NotConfigured",
                non_retryable=True,
            )
        if step in {"preview.preflight", "staging.preflight"}:
            return self._preflight(payload)
        if step not in {"preview.deploy", "staging.deploy"}:
            raise ApplicationError("unsupported release activity", non_retryable=True)
        raw_plan = payload.get("release_plan")
        approval = payload.get("approval_validation")
        if not isinstance(raw_plan, dict) or not isinstance(approval, dict):
            raise ApplicationError(
                "release requires a typed plan and immutable approval validation",
                type="ApprovalMismatch",
                non_retryable=True,
            )
        if approval.get("current") is not True:
            raise ApplicationError(
                "release approval is absent, invalidated, or stale",
                type="ApprovalMismatch",
                non_retryable=True,
            )
        strategy = raw_plan.get("strategy")
        plan_value = {key: value for key, value in raw_plan.items() if key != "strategy"}
        try:
            if strategy == "helm":
                result = self._helm_release(plan_value, approval)
            elif strategy == "argocd-gitops":
                result = self._gitops_observe(plan_value, approval)
            else:
                raise ApplicationError(
                    "release strategy is not allowlisted",
                    type="PolicyViolation",
                    non_retryable=True,
                )
        except ValidationError as error:
            raise ApplicationError(
                "release plan failed strict schema validation",
                type="PolicyViolation",
                non_retryable=True,
            ) from error
        safe = self.redactor.redact({**payload, **result}).value
        if not isinstance(safe, dict):
            raise TypeError("redacted release result must remain an object")
        return safe

    def _preflight(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_plan = payload.get("release_plan")
        if not isinstance(raw_plan, dict):
            raise ApplicationError(
                "release preflight requires a typed plan",
                type="PolicyViolation",
                non_retryable=True,
            )
        strategy = raw_plan.get("strategy")
        value = {key: item for key, item in raw_plan.items() if key != "strategy"}
        try:
            if strategy == "helm":
                helm_plan = FalconeReleasePlan.model_validate(value)
                self._bounded_worktree(helm_plan.chart_directory)
                self._scoped_credential(helm_plan.kubeconfig_path)
                result = self.helm.preflight(helm_plan)
            elif strategy == "argocd-gitops":
                gitops_plan = MusematicPromotion.model_validate(value)
                self._bounded_worktree(gitops_plan.deployment_repository)
                result = self.gitops.preflight(gitops_plan)
            else:
                raise ApplicationError(
                    "release strategy is not allowlisted",
                    type="PolicyViolation",
                    non_retryable=True,
                )
        except ValidationError as error:
            raise ApplicationError(
                "release preflight plan failed strict schema validation",
                type="PolicyViolation",
                non_retryable=True,
            ) from error
        safe = self.redactor.redact(
            {**payload, "status": "PREFLIGHT_PASS", "release_preflight": result}
        ).value
        if not isinstance(safe, dict):
            raise TypeError("redacted release preflight must remain an object")
        return safe

    def _helm_release(self, value: dict[str, Any], approval: dict[str, Any]) -> dict[str, Any]:
        plan = FalconeReleasePlan.model_validate(value)
        self._bounded_worktree(plan.chart_directory)
        self._scoped_credential(plan.kubeconfig_path)
        self._matching_binding(plan.approval_binding_sha256, approval)
        command = self.helm.deploy(plan, approval_is_current=True)
        transaction = hashlib.sha256(command.stdout.encode()).hexdigest()
        return {
            "status": "DEPLOYED",
            "strategy": "helm",
            "transaction_id": transaction,
            "return_code": command.return_code,
        }

    def _gitops_observe(self, value: dict[str, Any], approval: dict[str, Any]) -> dict[str, Any]:
        plan = MusematicPromotion.model_validate(value)
        self._bounded_worktree(plan.deployment_repository)
        self._matching_binding(plan.approval_binding_sha256, approval)
        observed = self.gitops.observe_reconciliation(plan)
        if (
            observed.get("revision_matches") is not True
            or observed.get("sync_status") != "Synced"
            or observed.get("health_status") != "Healthy"
        ):
            raise ApplicationError(
                "Argo CD has not reconciled the exact approved Git revision",
                type="ReconciliationPending",
            )
        return {"status": "DEPLOYED", "strategy": "argocd-gitops", **observed}

    def _bounded_worktree(self, path: Path) -> None:
        root = self.settings.worktree_root.resolve()
        resolved = path.resolve()
        if not resolved.is_dir() or resolved == root or not resolved.is_relative_to(root):
            raise ApplicationError(
                "release repository is outside its bounded worktree root",
                type="PolicyViolation",
                non_retryable=True,
            )

    def _scoped_credential(self, path: Path) -> None:
        root = self.settings.release_credential_root.resolve()
        resolved = path.resolve()
        if not resolved.is_file() or not resolved.is_relative_to(root):
            raise ApplicationError(
                "release kubeconfig is outside the scoped credential root",
                type="PolicyViolation",
                non_retryable=True,
            )
        mode = resolved.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ApplicationError(
                "release kubeconfig grants group or other access",
                type="PolicyViolation",
                non_retryable=True,
            )

    @staticmethod
    def _matching_binding(expected: str, approval: dict[str, Any]) -> None:
        if approval.get("artifact_binding_sha256") != expected:
            raise ApplicationError(
                "release plan no longer matches its immutable approval",
                type="ApprovalMismatch",
                non_retryable=True,
            )
