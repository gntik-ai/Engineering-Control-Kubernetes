from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from temporalio import activity
from temporalio.exceptions import ApplicationError

from engineering_control.config import Settings
from engineering_control.db.run_enrollment import enroll_activity_run
from engineering_control.db.runtime_binding import verified_activity_run_binding
from engineering_control.security.redaction import SecretRedactor

from .browser import BrowserActivityHandler
from .change_requests import ChangeRequestActivityHandler, ChangeRequestGitHubHandler
from .connections import ConnectionsActivityHandler
from .delivery import DeliveryActivityHandler
from .engineering import EngineeringActivityHandler
from .github import GitHubActivityHandler
from .gitops import GITOPS_GITHUB_STEPS, GitOpsControllerHandler
from .metadata import MetadataActivityHandler
from .preview_cleanup import PreviewCleanupActivityHandler
from .provider import ProviderActivityHandler
from .release import ReleaseActivityHandler
from .retention import RetentionActivityHandler
from .scan import ScanActivityHandler
from .ui_plan import UiPlanActivityHandler


class ControlActivities:
    """Narrow activity dispatcher; mutating handlers are separate deterministic adapters."""

    _allowed_steps = {
        "project.snapshot",
        "project.snapshot_pair",
        "project.persist_snapshot",
        "campaign.plan",
        "campaign.persist_coverage",
        "campaign.persist_snapshot_blockers",
        "campaign.persist_cell_blocker",
        "audit.prepare_agent_cell",
        "audit.deterministic_cell",
        "audit.agent_cell",
        "campaign.persist_cell_result",
        "campaign.finalize",
        "campaign.group_status",
        "deployment.render",
        "deployment.compare_live",
        "audit.static_source",
        "audit.static_deployment",
        "audit.api_flow",
        "audit.browser_flow",
        "audit.llmwiki_consumer_suite",
        "identity.superadmin_attest",
        "identity.observer_attest",
        "identity.inspect_stale",
        "identity.recover_stale",
        "audit.kubernetes_observe",
        "audit.correlate_runtime",
        "audit.classify_candidate",
        "audit.discover_enhancements",
        "audit.verify_cleanup",
        "finding.fresh_snapshot",
        "finding.load_candidate",
        "finding.prepare_verifier",
        "finding.reproduce",
        "finding.controls",
        "finding.dedupe",
        "finding.finalize",
        "finding.prepare_publication",
        "finding.publish",
        "finding.persist_publication",
        "finding.record_terminal",
        "agent.record_start",
        "agent.persist_result",
        "delivery.load_confirmed_finding",
        "delivery.ensure_issue",
        "delivery.refresh_issue_body",
        "delivery.prepare_spec",
        "delivery.invoke_spec",
        "delivery.create_openspec",
        "delivery.acquire_leases",
        "delivery.release_leases",
        "delivery.allocate_worktrees",
        "delivery.persist_worktree_leases",
        "delivery.deterministic_prechecks",
        "delivery.prepare_toolchain",
        "delivery.resolve_dependencies",
        "delivery.converge_deployment_pin",
        "delivery.route_providers",
        "delivery.prepare_maker",
        "delivery.invoke_maker",
        "delivery.prepare_deployment_maker",
        "delivery.invoke_deployment_maker",
        "delivery.prepare_checker",
        "delivery.invoke_checker",
        "delivery.validate_changeset",
        "delivery.persist_handoff",
        "delivery.record_validation_findings",
        "delivery.prepare_publication",
        "delivery.publish",
        "delivery.persist_publication",
        "reconcile.prepare",
        "reconcile.inspect_ci",
        "reconcile.persist_ci",
        "reconcile.prepare_promotion",
        "reconcile.apply_promotion",
        "reconcile.persist_promotion",
        "reconcile.publish_promotion",
        "reconcile.persist_promotion_publication",
        "reconcile.prepare_release_review",
        "reconcile.invoke_release_reviewer",
        "reconcile.persist_release_review",
        "final.prepare",
        "final.merge_source",
        "final.persist_source_merge",
        "final.inspect_artifact",
        "final.persist_artifact",
        "final.authorize_deployment_merge",
        "final.merge_deployment",
        "final.persist_deployment_merge",
        "final.sync_worktrees",
        "final.prepare_issue_close",
        "final.comment_issue",
        "final.persist_comment",
        "final.close_issue",
        "final.persist_issue_close",
        "preview.preflight",
        "preview.prepare",
        "preview.record_candidate",
        "preview.validate_approval",
        "preview.deploy",
        "preview.verify",
        "preview.finalize",
        "staging.prepare",
        "staging.preflight",
        "staging.record_candidate",
        "staging.validate_approval",
        "staging.deploy",
        "staging.finalize",
        "deployment.record_terminal",
        "runtime.snapshot",
        "runtime.deterministic_checks",
        "runtime.browser_checks",
        "runtime.prepare_review",
        "runtime.independent_review",
        "runtime.finalize",
        "runtime.record_failure",
        "recovery.inspect",
        "recovery.validate_binding",
        "recovery.prepare_candidate",
        "recovery.validate_approval",
        "recovery.execute",
        "recovery.verify",
        "recovery.record_failure",
        "quota.refresh",
        "quota.persist",
        "connections.refresh",
        "cleanup.discover",
        "cleanup.protect_active",
        "cleanup.authorize",
        "cleanup.execute",
        "cleanup.prune_orphan_worktrees",
        "cleanup.inspect_evidence_files",
        "cleanup.inspect_delivery_files",
        "cleanup.inspect_browser_files",
        "cleanup.inspect_codex_files",
        "cleanup.execute_evidence_files",
        "cleanup.execute_delivery_files",
        "cleanup.execute_browser_files",
        "cleanup.execute_codex_files",
        "cleanup.inspect_preview_namespaces",
        "cleanup.execute_preview_namespaces",
        "cleanup.execute_metadata",
        "cleanup.finalize",
        "loop.load_selection",
        "loop.acquire_component",
        "loop.record_skip",
        "loop.record_blocker",
        "loop.record_item",
        "loop.release_component",
        "loop.finalize",
        "enhancement.load_evidence",
        "enhancement.prepare_discovery",
        "enhancement.discover",
        "enhancement.validate_evidence",
        "enhancement.dedupe",
        "enhancement.github_dedupe",
        "enhancement.product_review",
        "enhancement.persist",
        "enhancement.load_approved",
        "enhancement.create_openspec",
        "enhancement.publication_dry_run",
        "enhancement.publish",
        "enhancement.persist_publication",
        "enhancement.prepare_solution",
        # NEW-108 operator change requests.
        "change_request.load",
        "change_request.prepare_draft",
        "change_request.draft",
        "change_request.prepare_review",
        "change_request.review",
        "change_request.record_review",
        "change_request.github_dedupe",
        "change_request.persist",
        "change_request.record_failure",
        "change_request.finalize_run",
        # NEW-123: UI migration flow A (blueprint) and its approved change-request fan-out.
        "ui_plan.register",
        "ui_plan.record_inputs",
        "ui_plan.stage_inputs",
        "ui_plan.prepare_draft",
        "ui_plan.draft",
        "ui_plan.record_draft",
        "ui_plan.stage_document",
        "ui_plan.prepare_review",
        "ui_plan.review",
        "ui_plan.record_review",
        "ui_plan.persist",
        "ui_plan.record_terminal",
        "ui_plan.finalize_run",
        # NEW-131: batched drafting (mechanical fields by Hermes, frame and page batches).
        "ui_plan.stage_parts",
        "ui_plan.record_parts",
        "ui_plan.prepare_part",
        "ui_plan.record_part_draft",
        "ui_plan.prepare_part_review",
        "ui_plan.record_part_review",
        "ui_plan.assemble",
        "ui_fanout.plan",
        "ui_fanout.prepare_item",
        "ui_fanout.gap_draft",
        "ui_fanout.record_gap_draft",
        "ui_fanout.prepare_gap_draft",
        "ui_fanout.prepare_review",
        "ui_fanout.finalize",
        "llmwiki.bootstrap.plan",
        "llmwiki.bootstrap.execute",
        "github.refresh_mirrors",
        "backlog.github_import",
        "backlog.persist_import",
        "backlog.plan",
        "backlog.approval_candidate",
        "backlog.record_approval",
        "backlog.record_skip",
        "backlog.record_retry",
        "backlog.load_execution",
        "backlog.materialize_candidate",
        "backlog.record_revalidation",
        "backlog.revalidate_gate",
        "backlog.record_loop_state",
        "backlog.restore_published_change_set",
        "notify.write_policy_required",
        "backlog.register_child_run",
        "backlog.register_run",
        "backlog.record_ci_repair",
        "backlog.correction_budget",
        "global_backlog.register",
        "global_backlog.select_next",
        "global_backlog.record_result",
        "global_backlog.finalize",
        "observability.register",
        "observability.prepare_workspace",
        "observability.prepare_codex",
        "observability.invoke_codex",
        "observability.prepare_claude",
        "observability.invoke_claude",
        "observability.prepare_claude_maker",
        "observability.invoke_claude_maker",
        "observability.prepare_codex_checker",
        "observability.invoke_codex_checker",
        "observability.finalize",
        "observability.cleanup_workspace",
        # NEW-120: deterministic legacy-console inventory (scan worker, read-only).
        "ui.inventory",
        "ui.kit_snapshot",
        # Post-merge staging GitOps flow: controller (DB + kubectl) steps ...
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
        # ... and GitHub-worker steps (GitOpsGitHubHandler via GitHubActivityHandler).
        "gitops.pr_checks",
        "gitops.pr_heads",
        "gitops.deployment_pr_checks",
        "gitops.merge",
        "gitops.release_tag",
        "gitops.workflow_run",
        "gitops.image_pin",
        "gitops.environment_issue",
        "gitops.close_issue",
    }
    _mutation_steps = {
        "preview.deploy",
        "staging.deploy",
        "recovery.execute",
        "cleanup.execute",
        # NEW-85: cleanup.prune_orphan_worktrees is not listed here: it must run in dry
        # runs to report candidates; its handler deletes only when the payload says
        # dry_run=False AND cleanup execution is enabled.
        "cleanup.execute_evidence_files",
        "cleanup.execute_delivery_files",
        "cleanup.execute_browser_files",
        "cleanup.execute_codex_files",
        "cleanup.execute_preview_namespaces",
        "cleanup.execute_metadata",
        "reconcile.apply_promotion",
        "reconcile.publish_promotion",
        "final.merge_source",
        "final.merge_deployment",
        "final.comment_issue",
        "final.close_issue",
        "enhancement.publish",
        "llmwiki.bootstrap.execute",
        "identity.recover_stale",
    }

    _worker_steps = {
        "controller": _allowed_steps
        - {
            "project.snapshot",
            "project.snapshot_pair",
            "deployment.compare_live",
            "audit.static_source",
            "audit.static_deployment",
            "audit.kubernetes_observe",
            "audit.correlate_runtime",
            "finding.fresh_snapshot",
            "delivery.allocate_worktrees",
            "delivery.deterministic_prechecks",
            "delivery.prepare_toolchain",
            "delivery.resolve_dependencies",
            "delivery.validate_changeset",
            "audit.deterministic_cell",
            "runtime.deterministic_checks",
            "identity.observer_attest",
            "cleanup.protect_active",
            "cleanup.execute",
            "cleanup.prune_orphan_worktrees",
            "cleanup.inspect_delivery_files",
            "cleanup.execute_delivery_files",
            "cleanup.inspect_browser_files",
            "cleanup.execute_browser_files",
            "cleanup.inspect_codex_files",
            "cleanup.execute_codex_files",
            "cleanup.inspect_preview_namespaces",
            "cleanup.execute_preview_namespaces",
            "preview.preflight",
            "deployment.render",
            "audit.agent_cell",
            "delivery.invoke_spec",
            "delivery.invoke_maker",
            "delivery.invoke_deployment_maker",
            "delivery.invoke_checker",
            "reconcile.invoke_release_reviewer",
            "delivery.publish",
            "reconcile.inspect_ci",
            "reconcile.apply_promotion",
            "reconcile.publish_promotion",
            "final.merge_source",
            "final.inspect_artifact",
            "final.merge_deployment",
            "final.sync_worktrees",
            "final.comment_issue",
            "final.close_issue",
            "quota.refresh",
            "runtime.browser_checks",
            "audit.api_flow",
            "audit.browser_flow",
            "audit.llmwiki_consumer_suite",
            "identity.superadmin_attest",
            "identity.inspect_stale",
            "identity.recover_stale",
            "staging.preflight",
            "preview.deploy",
            "preview.verify",
            "staging.deploy",
            "recovery.execute",
            "llmwiki.bootstrap.plan",
            "llmwiki.bootstrap.execute",
            "github.refresh_mirrors",
            "backlog.github_import",
            "observability.prepare_workspace",
            "observability.invoke_codex",
            "observability.invoke_claude",
            "observability.invoke_claude_maker",
            "observability.invoke_codex_checker",
            "observability.cleanup_workspace",
            "ui.inventory",
            "ui.kit_snapshot",
            "change_request.draft",
            "change_request.review",
            "change_request.github_dedupe",
            "ui_plan.stage_inputs",
            "ui_plan.stage_document",
            "ui_plan.stage_parts",
            "ui_plan.draft",
            "ui_plan.review",
            "ui_fanout.gap_draft",
        }
        - GITOPS_GITHUB_STEPS,
        "scan": {
            "project.snapshot",
            "project.snapshot_pair",
            "deployment.compare_live",
            "audit.static_source",
            "audit.static_deployment",
            "audit.kubernetes_observe",
            "audit.correlate_runtime",
            "finding.fresh_snapshot",
            "delivery.allocate_worktrees",
            "delivery.deterministic_prechecks",
            "delivery.prepare_toolchain",
            "delivery.resolve_dependencies",
            "delivery.converge_deployment_pin",
            "delivery.validate_changeset",
            "audit.deterministic_cell",
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
            "ui_plan.stage_inputs",
            "ui_plan.stage_document",
            "ui_plan.stage_parts",
        },
        "codex": {
            "audit.agent_cell",
            "finding.reproduce",
            "delivery.invoke_spec",
            "delivery.invoke_maker",
            "delivery.invoke_deployment_maker",
            "delivery.invoke_checker",
            "reconcile.invoke_release_reviewer",
            "quota.refresh",
            "runtime.independent_review",
            "cleanup.inspect_codex_files",
            "cleanup.execute_codex_files",
            "enhancement.discover",
            "enhancement.product_review",
            "observability.invoke_codex",
            "observability.invoke_codex_checker",
            "change_request.review",
            "ui_plan.review",
        },
        "claude": {
            "audit.agent_cell",
            "finding.reproduce",
            "delivery.invoke_spec",
            "delivery.invoke_maker",
            "delivery.invoke_deployment_maker",
            "delivery.invoke_checker",
            "reconcile.invoke_release_reviewer",
            "quota.refresh",
            "runtime.independent_review",
            "enhancement.discover",
            "enhancement.product_review",
            "observability.invoke_claude",
            "observability.invoke_claude_maker",
            "change_request.draft",
            "ui_plan.draft",
            "ui_fanout.gap_draft",
        },
        "browser": {
            "runtime.browser_checks",
            "audit.api_flow",
            "audit.browser_flow",
            "audit.llmwiki_consumer_suite",
            "identity.superadmin_attest",
            "identity.inspect_stale",
            "identity.recover_stale",
            "cleanup.inspect_browser_files",
            "cleanup.execute_browser_files",
        },
        "delivery": {
            "cleanup.inspect_delivery_files",
            "cleanup.execute_delivery_files",
        },
        "github": {
            "llmwiki.bootstrap.plan",
            "github.refresh_mirrors",
            "backlog.github_import",
            "delivery.refresh_issue_body",
            "finding.dedupe",
            "delivery.publish",
            "reconcile.inspect_ci",
            "reconcile.publish_promotion",
            "final.merge_source",
            "final.inspect_artifact",
            "final.merge_deployment",
            "final.sync_worktrees",
            "final.comment_issue",
            "final.close_issue",
            "enhancement.publish",
            "enhancement.github_dedupe",
            "finding.publish",
            "change_request.github_dedupe",
            *GITOPS_GITHUB_STEPS,
        },
        "release": {
            "preview.preflight",
            "deployment.render",
            "staging.preflight",
            "preview.deploy",
            "preview.verify",
            "staging.deploy",
            "recovery.execute",
            "cleanup.inspect_preview_namespaces",
            "cleanup.execute_preview_namespaces",
            "llmwiki.bootstrap.execute",
        },
        "synthetic": _allowed_steps,
    }

    @classmethod
    def steps_for_worker(cls, worker_kind: str) -> frozenset[str]:
        try:
            return frozenset(cls._worker_steps[worker_kind])
        except KeyError as error:
            raise ValueError(f"unknown worker kind: {worker_kind}") from error

    def __init__(self, settings: Settings, worker_kind: str | None = None) -> None:
        self.settings = settings
        self.worker_kind = worker_kind or settings.worker_kind
        if self.worker_kind not in self._worker_steps:
            raise ValueError(f"unknown worker kind: {self.worker_kind}")
        self.redactor = SecretRedactor()
        self.provider_handler = (
            ProviderActivityHandler(settings, self.worker_kind)
            if self.worker_kind in {"codex", "claude"}
            else None
        )
        self.release_handler = (
            ReleaseActivityHandler(settings) if self.worker_kind == "release" else None
        )
        self.metadata_handler = (
            MetadataActivityHandler(settings) if self.worker_kind == "controller" else None
        )
        self.browser_handler = (
            BrowserActivityHandler(settings) if self.worker_kind == "browser" else None
        )
        self.delivery_handler = (
            DeliveryActivityHandler(settings)
            if self.worker_kind in {"delivery", "github"}
            else None
        )
        self.scan_handler = ScanActivityHandler(settings) if self.worker_kind == "scan" else None
        self.retention_handler = (
            RetentionActivityHandler(settings, self.worker_kind)
            if self.worker_kind in {"controller", "delivery", "browser", "codex"}
            else None
        )
        self.preview_cleanup_handler = (
            PreviewCleanupActivityHandler(settings) if self.worker_kind == "release" else None
        )
        self.engineering_handler = (
            EngineeringActivityHandler(settings) if self.worker_kind == "controller" else None
        )
        self.github_handler = (
            GitHubActivityHandler(settings) if self.worker_kind == "github" else None
        )
        self.connections_handler = (
            ConnectionsActivityHandler(settings) if self.worker_kind == "controller" else None
        )
        self.gitops_handler = (
            GitOpsControllerHandler(settings) if self.worker_kind == "controller" else None
        )
        self.change_request_handler = (
            ChangeRequestActivityHandler(settings) if self.worker_kind == "controller" else None
        )
        self.change_request_github_handler = (
            ChangeRequestGitHubHandler(settings) if self.worker_kind == "github" else None
        )
        self.ui_plan_handler = (
            UiPlanActivityHandler(settings) if self.worker_kind == "controller" else None
        )

    @activity.defn(name="execute_control_step")
    async def execute_control_step(self, request: dict[str, Any]) -> dict[str, Any]:
        step = request.get("step")
        payload = request.get("payload")
        if step not in self._allowed_steps or not isinstance(payload, dict):
            raise ApplicationError("unknown or invalid control step", non_retryable=True)
        if step not in self._worker_steps[self.worker_kind]:
            raise ApplicationError(
                f"step {step} is not allowed on {self.worker_kind} worker",
                type="PolicyViolation",
                non_retryable=True,
            )
        if self.worker_kind == "controller" and not self.settings.synthetic_mode:
            async with verified_activity_run_binding():
                assert self.metadata_handler is not None
                try:
                    await enroll_activity_run(self.metadata_handler.sessions)
                except ValueError:
                    raise ApplicationError(
                        "Existing Run identity is unverifiable",
                        type="RunIdentityUnverified",
                        non_retryable=True,
                    ) from None
                return await self._dispatch_control_step(step, payload)
        return await self._dispatch_control_step(step, payload)

    async def _dispatch_control_step(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        safe_payload = self.redactor.redact(payload).value
        safe_payload = self._attach_orchestration_context(safe_payload)
        activity.heartbeat({"step": step, "phase": "validated"})
        if self.settings.synthetic_mode or self.worker_kind == "synthetic":
            return self._synthetic_step(step, safe_payload)
        if (
            step in DeliveryActivityHandler.write_capable_steps
            and self.delivery_handler is not None
        ):
            return await self.delivery_handler.handle(step, safe_payload)
        if (
            self.change_request_github_handler is not None
            and step in self.change_request_github_handler.supported_steps
        ):
            return await self.change_request_github_handler.handle(step, safe_payload)
        if (
            self.change_request_handler is not None
            and step in self.change_request_handler.supported_steps
        ):
            return await self.change_request_handler.handle(step, safe_payload)
        if self.ui_plan_handler is not None and step in self.ui_plan_handler.supported_steps:
            return await self.ui_plan_handler.handle(step, safe_payload)
        if step in self._mutation_steps:
            dry_run = bool(self._find_value(safe_payload, "dry_run", default=True))
            if dry_run:
                return self._envelope(step, safe_payload, "DRY_RUN")
            if (
                step == "llmwiki.bootstrap.execute"
                and not self.settings.llmwiki_staging_bootstrap_enabled
            ):
                raise ApplicationError(
                    "LLMWiki staging bootstrap execution is disabled",
                    type="SecurityBlocker",
                    non_retryable=True,
                )
            if step == "identity.recover_stale" and self.browser_handler is not None:
                return await self.browser_handler.handle(step, safe_payload)
            if (
                self.retention_handler is not None
                and step in self.retention_handler.supported_steps
            ):
                return await self.retention_handler.handle(step, safe_payload)
            if (
                self.preview_cleanup_handler is not None
                and step in self.preview_cleanup_handler.supported_steps
            ):
                return await self.preview_cleanup_handler.handle(step, safe_payload)
            if step == "cleanup.execute_metadata" and self.metadata_handler is not None:
                return await self.metadata_handler.handle(step, safe_payload)
            if step == "cleanup.execute" and self.scan_handler is not None:
                return await self.scan_handler.handle(step, safe_payload)
            if step == "reconcile.apply_promotion" and self.scan_handler is not None:
                return await self.scan_handler.handle(step, safe_payload)
            if self.release_handler is not None:
                return await self.release_handler.handle(step, safe_payload)
            raise ApplicationError(
                f"{step} requires a configured deterministic release adapter",
                type="SecurityBlocker",
                non_retryable=True,
            )
        if self.retention_handler is not None and step in self.retention_handler.supported_steps:
            return await self.retention_handler.handle(step, safe_payload)
        if (
            self.preview_cleanup_handler is not None
            and step in self.preview_cleanup_handler.supported_steps
        ):
            return await self.preview_cleanup_handler.handle(step, safe_payload)
        if self.release_handler is not None and step in self.release_handler.supported_steps:
            return await self.release_handler.handle(step, safe_payload)
        if (
            self.connections_handler is not None
            and step in self.connections_handler.supported_steps
        ):
            return await self.connections_handler.handle(step, safe_payload)
        if self.provider_handler is not None:
            return await self.provider_handler.handle(step, safe_payload)
        if self.gitops_handler is not None and step in self.gitops_handler.supported_steps:
            return await self.gitops_handler.handle(step, safe_payload)
        if self.metadata_handler is not None and step in self.metadata_handler.supported_steps:
            return await self.metadata_handler.handle(step, safe_payload)
        if (
            self.engineering_handler is not None
            and step in self.engineering_handler.supported_steps
        ):
            return await self.engineering_handler.handle(step, safe_payload)
        if self.github_handler is not None and step in self.github_handler.supported_steps:
            return await self.github_handler.handle(step, safe_payload)
        if self.browser_handler is not None:
            return await self.browser_handler.handle(step, safe_payload)
        if self.delivery_handler is not None:
            return await self.delivery_handler.handle(step, safe_payload)
        if self.scan_handler is not None:
            return await self.scan_handler.handle(step, safe_payload)
        raise ApplicationError(
            f"{step} has no configured production activity handler",
            type="NotConfigured",
            non_retryable=True,
        )

    @staticmethod
    def _attach_orchestration_context(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            info = activity.info()
        except RuntimeError:
            return payload
        return {
            **payload,
            "orchestration": {
                "workflow_id": info.workflow_id,
                "workflow_run_id": info.workflow_run_id,
                "activity_run_id": info.activity_run_id,
                "activity_id": info.activity_id,
                "attempt": info.attempt,
                "task_queue": info.task_queue,
            },
        }

    @staticmethod
    def _find_value(value: Any, key: str, *, default: Any = None) -> Any:
        if isinstance(value, dict):
            if key in value:
                return value[key]
            for nested in value.values():
                found = ControlActivities._find_value(nested, key, default=None)
                if found is not None:
                    return found
        return default

    @staticmethod
    def _envelope(step: str, payload: dict[str, Any], status: str) -> dict[str, Any]:
        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        return {
            **payload,
            "last_step": step,
            "status": status,
            "evidence_sha256": hashlib.sha256(canonical).hexdigest(),
            "recorded_at": datetime.now(UTC).isoformat(),
        }

    def _synthetic_step(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._envelope(step, payload, "PASS")
        if step == "project.snapshot":
            result["campaign_id"] = payload.get("campaign_id") or str(uuid4())
        if step == "delivery.load_confirmed_finding":
            result["change_set_id"] = str(uuid4())
        if step == "loop.load_selection":
            result["items"] = [
                {
                    "finding_id": str(uuid4()),
                    "risk_class": payload.get("maximum_risk", "R1"),
                    "component": f"{payload.get('project', 'falcone')}:synthetic",
                }
            ]
        if step == "enhancement.load_approved":
            result["status"] = "APPROVED"
            result.setdefault("project", "falcone")
            result.setdefault("campaign_id", "synthetic-campaign")
        if step == "delivery.route_providers":
            result["maker_provider"] = "codex"
            result["checker_provider"] = "claude"
        if step == "delivery.persist_handoff":
            result["status"] = "AWAITING_STAGING_APPROVAL"
        if step == "finding.finalize":
            result["disposition"] = "CONFIRMED"
        if step == "quota.refresh":
            result.update(
                {
                    "remaining_capacity": None,
                    "reset_at": None,
                    "source": "not_exposed_by_native_cli",
                }
            )
        return result
