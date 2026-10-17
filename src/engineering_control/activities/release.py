from __future__ import annotations

import asyncio
import hashlib
import stat
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from temporalio.exceptions import ApplicationError

from engineering_control.adapters.command import CommandFailure, SafeCommandRunner
from engineering_control.adapters.falcone.release import (
    FalconeHelmAdapter,
    FalconeRecoveryPlan,
    FalconeReleasePlan,
)
from engineering_control.adapters.musematic.gitops import MusematicGitOpsAdapter, MusematicPromotion
from engineering_control.config import Settings
from engineering_control.engineering.configuration import EngineeringCatalog
from engineering_control.projects import PROJECT_SET
from engineering_control.security.redaction import SecretRedactor
from engineering_control.worktrees.hardened_git import changeset_location, enclosing_worktree


class ReleaseActivityHandler:
    """Deterministic release-only dispatcher; it never accepts prompts or shell strings."""

    supported_steps = frozenset(
        {
            "deployment.render",
            "preview.preflight",
            "preview.deploy",
            "preview.verify",
            "staging.preflight",
            "staging.deploy",
            "recovery.execute",
        }
    )

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
                "kubeconform": settings.kubeconform_executable,
                "argocd": settings.argocd_executable,
            }
        )
        self.runner = runner
        self.helm = helm or FalconeHelmAdapter(
            runner,
            git_location=lambda path: changeset_location(
                enclosing_worktree(path, settings.worktree_root),
                worktree_root=settings.worktree_root,
                metadata_root=settings.worktree_metadata_root,
                mirror_root=settings.mirror_root,
                seal=False,
            ),
        )
        self.gitops = gitops or MusematicGitOpsAdapter(
            runner,
            git_location=lambda worktree: changeset_location(
                worktree,
                worktree_root=settings.worktree_root,
                metadata_root=settings.worktree_metadata_root,
                mirror_root=settings.mirror_root,
                seal=False,
            ),
        )
        self.redactor = SecretRedactor()
        self.catalog = EngineeringCatalog(
            project_root=settings.project_config_root,
            persona_root=settings.persona_config_root,
            flow_root=settings.flow_config_root,
            credential_root=settings.credential_root,
            attestation_root=settings.credential_attestation_root,
            browser_worker_identity=settings.browser_worker_identity,
        )

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        # F-08: every branch does blocking helm/argocd/gitops subprocess work through
        # sync helpers; run each off the event loop in a worker thread rather than
        # converting the whole synchronous call graph to async/await.
        if step == "deployment.render":
            return await asyncio.to_thread(self._render, payload)
        if step == "recovery.execute":
            return await asyncio.to_thread(self._recover, payload)
        if step in {"preview.preflight", "staging.preflight"}:
            return await asyncio.to_thread(self._preflight, payload)
        if step == "preview.verify":
            return await asyncio.to_thread(self._verify_preview, payload)
        if step not in {"preview.deploy", "staging.deploy"}:
            raise ApplicationError("unsupported release activity", non_retryable=True)
        return await asyncio.to_thread(self._deploy, payload)

    def _deploy(self, payload: dict[str, Any]) -> dict[str, Any]:
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
                result = self._helm_release(
                    plan_value,
                    approval,
                    self._approved_preflight(payload),
                )
            elif strategy == "argocd-gitops":
                result = self._gitops_observe(
                    plan_value,
                    approval,
                    self._approved_preflight(payload),
                )
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

    def _render(self, payload: dict[str, Any]) -> dict[str, Any]:
        project = payload.get("project")
        snapshot = payload.get("snapshot")
        if project not in PROJECT_SET or not isinstance(snapshot, dict):
            raise ApplicationError(
                "deployment rendering requires a project and immutable snapshot",
                type="PolicyViolation",
                non_retryable=True,
            )
        manifest = self.catalog.project(project)
        if not manifest.has_runtime():
            # NEW-160: a project without a runtime (gntik-ui) has nothing to render.
            raise ApplicationError(
                "PROJECT_HAS_NO_RUNTIME: the project has no deployment to render",
                type="PolicyViolation",
                non_retryable=True,
            )
        deployment = manifest.runtime_deployment()
        if project == "llmwiki":
            return self._render_llmwiki(payload, manifest, snapshot)
        try:
            chart_worktree_key = f"{deployment.chart_repository}_worktree"
            chart_worktree = Path(str(snapshot.get(chart_worktree_key))).resolve()
            deployment_worktree = Path(str(snapshot.get("deployment_worktree"))).resolve()
            self._bounded_worktree(chart_worktree)
            self._bounded_worktree(deployment_worktree)
            chart = (chart_worktree / deployment.chart_path).resolve()
            if not chart.is_dir() or not chart.is_relative_to(chart_worktree):
                raise ValueError("configured chart path escaped its immutable worktree")
            normal_values = [
                self._bounded_render_file(chart_worktree, relative)
                for relative in deployment.values_paths
            ]
            isolated_values = [
                self._bounded_render_file(deployment_worktree, relative)
                for relative in deployment.secret_bearing_values_paths
            ]
            lint_arguments = ["lint", str(chart)]
            render_arguments = [
                "template",
                "engineering-render",
                str(chart),
                "--namespace",
                deployment.namespaces[0],
            ]
            for values_file in [*normal_values, *isolated_values]:
                lint_arguments.extend(("--values", str(values_file)))
                render_arguments.extend(("--values", str(values_file)))
            lint = self.runner.run("helm", lint_arguments, cwd=chart_worktree, timeout_seconds=900)
            rendered = self.runner.run_yaml_summary(
                "helm", render_arguments, cwd=chart_worktree, timeout_seconds=900
            )
            if not rendered.document_count:
                raise ValueError("configured chart rendered no Kubernetes objects")
            return {
                **payload,
                "status": "DEPLOYMENT_RENDER_PASS",
                "render_evidence": {
                    "chart_repository": deployment.chart_repository,
                    "chart_path": deployment.chart_path,
                    "normal_values_paths": deployment.values_paths,
                    "secret_bearing_values_paths": [
                        "[ISOLATED_SECRET_BEARING_VALUES]"
                        for _ in deployment.secret_bearing_values_paths
                    ],
                    "helm_lint_sha256": hashlib.sha256(lint.stdout.encode()).hexdigest(),
                    "rendered_sha256": rendered.stdout_sha256,
                    "manifest_count": rendered.document_count,
                    "kinds": dict(rendered.kinds),
                    "rendered_content_persisted": False,
                },
            }
        except (CommandFailure, OSError, ValueError, yaml.YAMLError) as error:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "DEPLOYMENT_RENDER_FAILED",
                "error_type": type(error).__name__,
                "rendered_content_persisted": False,
            }

    def _render_llmwiki(
        self, payload: dict[str, Any], manifest: Any, snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        worktree = Path(str(snapshot.get("source_worktree"))).resolve()
        try:
            self._bounded_worktree(worktree)
            paths = [
                self._bounded_render_file(worktree, relative)
                for relative in manifest.deployment.values_paths
            ]
            if not paths:
                raise ValueError("LLMWiki has no deployment manifest")
            checks: list[dict[str, Any]] = []
            kinds: dict[str, int] = {}
            combined = hashlib.sha256()
            for path in paths:
                raw = path.read_bytes()
                combined.update(raw)
                documents = [item for item in yaml.safe_load_all(raw) if isinstance(item, dict)]
                if any(item.get("kind") == "Secret" for item in documents):
                    raise ValueError("LLMWiki manifest contains a persistent Secret object")
                for document in documents:
                    kind = str(document.get("kind", "Unknown"))
                    kinds[kind] = kinds.get(kind, 0) + 1
                result = self.runner.run(
                    "kubeconform",
                    ["-strict", "-summary", str(path)],
                    cwd=worktree,
                    timeout_seconds=300,
                    check=False,
                )
                checks.append(
                    {
                        "path": str(path.relative_to(worktree)),
                        "status": "PASS" if result.return_code == 0 else "FAIL",
                    }
                )
            status = (
                "DEPLOYMENT_RENDER_PASS"
                if all(item["status"] == "PASS" for item in checks)
                else "BLOCKED"
            )
            return {
                **payload,
                "status": status,
                "blocker": (
                    None if status == "DEPLOYMENT_RENDER_PASS" else "KUBERNETES_SCHEMA_INVALID"
                ),
                "render_evidence": {
                    "strategy": "repo-local-manifest",
                    "manifest_sha256": combined.hexdigest(),
                    "checks": checks,
                    "kinds": dict(sorted(kinds.items())),
                    "secret_objects_present": False,
                    "rendered_content_persisted": False,
                },
            }
        except (CommandFailure, OSError, ValueError, yaml.YAMLError) as error:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "DEPLOYMENT_RENDER_FAILED",
                "error_type": type(error).__name__,
                "rendered_content_persisted": False,
            }

    @staticmethod
    def _bounded_render_file(root: Path, relative: str) -> Path:
        path = (root / relative).resolve()
        if not path.is_file() or not path.is_relative_to(root):
            raise ValueError("configured values path escaped its immutable worktree")
        return path

    def _verify_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_plan = payload.get("release_plan")
        approval = payload.get("approval_validation")
        if not isinstance(raw_plan, dict) or not isinstance(approval, dict):
            raise ApplicationError(
                "preview verification requires its approved typed plan",
                type="ApprovalMismatch",
                non_retryable=True,
            )
        if approval.get("current") is not True:
            raise ApplicationError(
                "preview approval is absent, invalidated, or stale",
                type="ApprovalMismatch",
                non_retryable=True,
            )
        strategy = raw_plan.get("strategy")
        value = {key: item for key, item in raw_plan.items() if key != "strategy"}
        try:
            if strategy == "helm":
                helm_plan = FalconeReleasePlan.model_validate(value)
                self._bounded_worktree(helm_plan.chart_directory)
                self._scoped_credential(helm_plan.kubeconfig_path)
                self._matching_binding(helm_plan.approval_binding_sha256, approval)
                result = self.helm.verify(helm_plan)
            elif strategy == "argocd-gitops":
                gitops_plan = MusematicPromotion.model_validate(value)
                self._bounded_worktree(gitops_plan.deployment_repository)
                self._matching_binding(gitops_plan.approval_binding_sha256, approval)
                result = self.gitops.observe_reconciliation(gitops_plan)
                if (
                    result.get("revision_matches") is not True
                    or result.get("sync_status") != "Synced"
                    or result.get("health_status") != "Healthy"
                ):
                    raise ApplicationError(
                        "preview Git revision has not reconciled healthy",
                        type="ReconciliationPending",
                    )
            else:
                raise ApplicationError(
                    "preview strategy is not allowlisted",
                    type="PolicyViolation",
                    non_retryable=True,
                )
        except ValidationError as error:
            raise ApplicationError(
                "preview verification plan failed strict schema validation",
                type="PolicyViolation",
                non_retryable=True,
            ) from error
        safe = self.redactor.redact({**payload, "preview_verification": result}).value
        if not isinstance(safe, dict):
            raise TypeError("redacted preview verification must remain an object")
        safe["status"] = "PREVIEW_VERIFIED"
        return safe

    def _recover(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_plan = payload.get("recovery_plan")
        approval = payload.get("approval_validation")
        if not isinstance(raw_plan, dict) or not isinstance(approval, dict):
            raise ApplicationError(
                "recovery requires a typed plan and immutable recovery approval",
                type="ApprovalMismatch",
                non_retryable=True,
            )
        if approval.get("current") is not True:
            raise ApplicationError(
                "recovery approval is absent, invalidated, or stale",
                type="ApprovalMismatch",
                non_retryable=True,
            )
        strategy = raw_plan.get("strategy")
        value = {key: item for key, item in raw_plan.items() if key != "strategy"}
        try:
            if strategy != "helm":
                raise ApplicationError(
                    "GitOps recovery requires a protected deployment-repository PR",
                    type="ProtectedRecoveryPRRequired",
                    non_retryable=True,
                )
            plan = FalconeRecoveryPlan.model_validate(value)
            self._scoped_credential(plan.kubeconfig_path)
            self._matching_binding(plan.recovery_approval_binding_sha256, approval)
            result = self.helm.rollback(plan, approval_is_current=True)
        except ValidationError as error:
            raise ApplicationError(
                "recovery plan failed strict schema validation",
                type="PolicyViolation",
                non_retryable=True,
            ) from error
        safe = self.redactor.redact({**payload, **result}).value
        if not isinstance(safe, dict):
            raise TypeError("redacted recovery result must remain an object")
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

    def _helm_release(
        self,
        value: dict[str, Any],
        approval: dict[str, Any],
        approved_preflight: dict[str, Any],
    ) -> dict[str, Any]:
        plan = FalconeReleasePlan.model_validate(value)
        self._bounded_worktree(plan.chart_directory)
        self._scoped_credential(plan.kubeconfig_path)
        self._matching_binding(plan.approval_binding_sha256, approval)
        command = self.helm.deploy(
            plan,
            approval_is_current=True,
            approved_preflight=approved_preflight,
        )
        transaction = hashlib.sha256(command.stdout.encode()).hexdigest()
        return {
            "status": "DEPLOYED",
            "strategy": "helm",
            "transaction_id": transaction,
            "return_code": command.return_code,
        }

    def _gitops_observe(
        self,
        value: dict[str, Any],
        approval: dict[str, Any],
        approved_preflight: dict[str, Any],
    ) -> dict[str, Any]:
        plan = MusematicPromotion.model_validate(value)
        self._bounded_worktree(plan.deployment_repository)
        self._matching_binding(plan.approval_binding_sha256, approval)
        if self.gitops.preflight(plan) != approved_preflight:
            raise ApplicationError(
                "GitOps desired state drifted after immutable approval",
                type="ApprovalMismatch",
                non_retryable=True,
            )
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

    @staticmethod
    def _approved_preflight(payload: dict[str, Any]) -> dict[str, Any]:
        value = payload.get("release_preflight")
        if not isinstance(value, dict) or value.get("status") != "PREFLIGHT_PASS":
            raise ApplicationError(
                "release requires the exact approved preflight result",
                type="ApprovalMismatch",
                non_retryable=True,
            )
        return value

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
