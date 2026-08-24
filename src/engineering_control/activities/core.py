from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from temporalio import activity
from temporalio.exceptions import ApplicationError

from engineering_control.config import Settings
from engineering_control.security.redaction import SecretRedactor

from .browser import BrowserActivityHandler
from .delivery import DeliveryActivityHandler
from .metadata import MetadataActivityHandler
from .preview_cleanup import PreviewCleanupActivityHandler
from .provider import ProviderActivityHandler
from .release import ReleaseActivityHandler
from .retention import RetentionActivityHandler
from .scan import ScanActivityHandler


class ControlActivities:
    """Narrow activity dispatcher; mutating handlers are separate deterministic adapters."""

    _allowed_steps = {
        "project.snapshot",
        "campaign.persist_coverage",
        "campaign.persist_snapshot_blockers",
        "campaign.persist_cell_blocker",
        "audit.prepare_agent_cell",
        "audit.deterministic_cell",
        "audit.agent_cell",
        "campaign.persist_cell_result",
        "campaign.finalize",
        "finding.fresh_snapshot",
        "finding.load_candidate",
        "finding.prepare_verifier",
        "finding.reproduce",
        "finding.controls",
        "finding.dedupe",
        "finding.finalize",
        "finding.record_terminal",
        "agent.record_start",
        "agent.persist_result",
        "delivery.load_confirmed_finding",
        "delivery.ensure_issue",
        "delivery.prepare_spec",
        "delivery.invoke_spec",
        "delivery.create_openspec",
        "delivery.acquire_leases",
        "delivery.allocate_worktrees",
        "delivery.persist_worktree_leases",
        "delivery.deterministic_prechecks",
        "delivery.route_providers",
        "delivery.prepare_maker",
        "delivery.invoke_maker",
        "delivery.prepare_deployment_maker",
        "delivery.invoke_deployment_maker",
        "delivery.prepare_checker",
        "delivery.invoke_checker",
        "delivery.validate_changeset",
        "delivery.persist_handoff",
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
        "cleanup.discover",
        "cleanup.protect_active",
        "cleanup.authorize",
        "cleanup.execute",
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
    }
    _mutation_steps = {
        "preview.deploy",
        "staging.deploy",
        "recovery.execute",
        "cleanup.execute",
        "cleanup.execute_evidence_files",
        "cleanup.execute_delivery_files",
        "cleanup.execute_browser_files",
        "cleanup.execute_codex_files",
        "cleanup.execute_preview_namespaces",
        "cleanup.execute_metadata",
        "reconcile.apply_promotion",
        "reconcile.publish_promotion",
    }

    _worker_steps = {
        "controller": _allowed_steps
        - {
            "project.snapshot",
            "finding.fresh_snapshot",
            "delivery.allocate_worktrees",
            "delivery.deterministic_prechecks",
            "delivery.validate_changeset",
            "audit.deterministic_cell",
            "runtime.deterministic_checks",
            "cleanup.protect_active",
            "cleanup.execute",
            "cleanup.inspect_delivery_files",
            "cleanup.execute_delivery_files",
            "cleanup.inspect_browser_files",
            "cleanup.execute_browser_files",
            "cleanup.inspect_codex_files",
            "cleanup.execute_codex_files",
            "cleanup.inspect_preview_namespaces",
            "cleanup.execute_preview_namespaces",
            "preview.preflight",
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
            "quota.refresh",
            "runtime.browser_checks",
            "staging.preflight",
            "preview.deploy",
            "preview.verify",
            "staging.deploy",
            "recovery.execute",
        },
        "scan": {
            "project.snapshot",
            "finding.fresh_snapshot",
            "delivery.allocate_worktrees",
            "delivery.deterministic_prechecks",
            "delivery.validate_changeset",
            "audit.deterministic_cell",
            "runtime.deterministic_checks",
            "cleanup.protect_active",
            "cleanup.execute",
            "reconcile.apply_promotion",
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
        },
        "browser": {
            "runtime.browser_checks",
            "cleanup.inspect_browser_files",
            "cleanup.execute_browser_files",
        },
        "delivery": {
            "delivery.publish",
            "reconcile.inspect_ci",
            "reconcile.publish_promotion",
        },
        "release": {
            "preview.preflight",
            "staging.preflight",
            "preview.deploy",
            "preview.verify",
            "staging.deploy",
            "recovery.execute",
            "cleanup.inspect_preview_namespaces",
            "cleanup.execute_preview_namespaces",
        },
        "synthetic": _allowed_steps,
    }

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
            DeliveryActivityHandler(settings) if self.worker_kind == "delivery" else None
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
        safe_payload = self.redactor.redact(payload).value
        safe_payload = self._attach_orchestration_context(safe_payload)
        activity.heartbeat({"step": step, "phase": "validated"})
        if self.settings.synthetic_mode or self.worker_kind == "synthetic":
            return self._synthetic_step(step, safe_payload)
        if step in self._mutation_steps:
            dry_run = bool(self._find_value(safe_payload, "dry_run", default=True))
            if dry_run:
                return self._envelope(step, safe_payload, "DRY_RUN")
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
            if step == "reconcile.publish_promotion" and self.delivery_handler is not None:
                return await self.delivery_handler.handle(step, safe_payload)
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
        if self.provider_handler is not None:
            return await self.provider_handler.handle(step, safe_payload)
        if self.metadata_handler is not None and step in self.metadata_handler.supported_steps:
            return await self.metadata_handler.handle(step, safe_payload)
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
