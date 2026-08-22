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

from .provider import ProviderActivityHandler


class ControlActivities:
    """Narrow activity dispatcher; mutating handlers are separate deterministic adapters."""

    _allowed_steps = {
        "project.snapshot",
        "campaign.persist_coverage",
        "audit.deterministic_cell",
        "audit.agent_cell",
        "campaign.finalize",
        "finding.fresh_snapshot",
        "finding.reproduce",
        "finding.controls",
        "finding.dedupe",
        "finding.finalize",
        "delivery.load_confirmed_finding",
        "delivery.create_openspec",
        "delivery.acquire_leases",
        "delivery.allocate_worktrees",
        "delivery.deterministic_prechecks",
        "delivery.route_providers",
        "delivery.invoke_maker",
        "delivery.invoke_checker",
        "delivery.validate_changeset",
        "delivery.persist_handoff",
        "preview.preflight",
        "preview.prepare",
        "preview.deploy",
        "preview.verify",
        "staging.prepare",
        "staging.preflight",
        "staging.validate_approval",
        "staging.deploy",
        "runtime.snapshot",
        "runtime.deterministic_checks",
        "runtime.browser_checks",
        "runtime.independent_review",
        "runtime.finalize",
        "recovery.inspect",
        "recovery.validate_binding",
        "recovery.execute",
        "recovery.verify",
        "quota.refresh",
        "quota.persist",
        "cleanup.discover",
        "cleanup.protect_active",
        "cleanup.execute",
    }
    _mutation_steps = {
        "preview.deploy",
        "staging.deploy",
        "recovery.execute",
        "cleanup.execute",
    }

    _worker_steps = {
        "controller": _allowed_steps
        - {
            "project.snapshot",
            "finding.fresh_snapshot",
            "finding.reproduce",
            "finding.controls",
            "delivery.allocate_worktrees",
            "delivery.deterministic_prechecks",
            "audit.deterministic_cell",
            "preview.preflight",
            "audit.agent_cell",
            "delivery.invoke_maker",
            "delivery.invoke_checker",
            "quota.refresh",
            "runtime.browser_checks",
            "preview.deploy",
            "staging.deploy",
            "recovery.execute",
        },
        "scan": {
            "project.snapshot",
            "finding.fresh_snapshot",
            "finding.reproduce",
            "finding.controls",
            "delivery.allocate_worktrees",
            "delivery.deterministic_prechecks",
            "audit.deterministic_cell",
            "preview.preflight",
        },
        "codex": {
            "audit.agent_cell",
            "delivery.invoke_maker",
            "delivery.invoke_checker",
            "quota.refresh",
        },
        "claude": {
            "audit.agent_cell",
            "delivery.invoke_maker",
            "delivery.invoke_checker",
            "quota.refresh",
        },
        "browser": {"runtime.browser_checks"},
        "release": {"preview.deploy", "staging.deploy", "recovery.execute"},
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
        activity.heartbeat({"step": step, "phase": "validated"})
        if step in self._mutation_steps:
            dry_run = bool(self._find_value(safe_payload, "dry_run", default=True))
            if dry_run:
                return self._envelope(step, safe_payload, "DRY_RUN")
            raise ApplicationError(
                f"{step} requires a configured deterministic release adapter",
                type="SecurityBlocker",
                non_retryable=True,
            )
        if self.provider_handler is not None:
            return await self.provider_handler.handle(step, safe_payload)
        if self.settings.synthetic_mode or self.worker_kind == "synthetic":
            return self._synthetic_step(step, safe_payload)
        raise ApplicationError(
            f"{step} has no configured production activity handler",
            type="NotConfigured",
            non_retryable=True,
        )

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
