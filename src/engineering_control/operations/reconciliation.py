"""Execution-bound campaign reconciliation; orchestration completion is not success."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

TERMINAL_ORCHESTRATION = frozenset({"COMPLETED", "FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"})
TERMINAL_OUTCOMES = frozenset(
    {
        "SUCCESS",
        "COMPLETE",
        "COMPLETED",
        "BLOCKED",
        "FAILED",
        "CANCELLED",
        "CANCELED",
        "EMPTY",
        "NOT_RUN",
        "SKIPPED",
        "DRY_RUN",
        "INCOMPLETE",
        "VERIFICATION_REQUIRED",
        "PARTIAL_BLOCKED",
        "COMPLETED_WITH_FINDINGS",
        "STOPPED",
        "STOPPED_AFTER_CURRENT",
    }
)


def utc_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


def safe_execution_binding(metadata: dict[str, Any]) -> dict[str, Any] | None:
    raw = metadata.get("reconciliation_binding")
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
        return None
    if not isinstance(raw.get("workflow_id"), str) or not 1 <= len(raw["workflow_id"]) <= 240:
        return None
    for key in ("first_execution_run_id", "application_run_id"):
        value = raw.get(key)
        if not isinstance(value, str):
            return None
        try:
            if str(UUID(value)) != value:
                return None
        except ValueError:
            return None
    return {
        key: raw[key]
        for key in ("version", "workflow_id", "first_execution_run_id", "application_run_id")
    }


def snapshot_token(campaign: Any, runs: list[Any]) -> str:
    """Internal fingerprint includes all persisted binding data, never a bearer token."""
    values = {
        "id": str(campaign.id),
        "status": campaign.status,
        "updated_at": utc_text(campaign.updated_at),
        "metadata": campaign.metadata_json,
        "runs": sorted(
            [
                {
                    "id": str(run.id),
                    "workflow_id": run.workflow_id,
                    "status": run.status,
                    "error_code": run.error_code,
                    "updated_at": utc_text(run.updated_at),
                    "started_at": utc_text(run.started_at),
                    "finished_at": utc_text(run.finished_at),
                }
                for run in runs
            ],
            key=lambda row: row["id"],
        ),
    }
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def observation_token(observed: dict[str, Any]) -> str:
    values = {
        key: observed.get(key)
        for key in (
            "workflow_id",
            "run_id",
            "first_execution_run_id",
            "status",
            "identity_verified",
            "business_status",
            "result_campaign_id",
        )
    }
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ReconciliationDecision:
    classification: str
    target_campaign_status: str | None = None
    target_run_status: str | None = None


def decide_reconciliation(
    candidate: dict[str, Any], observed: dict[str, Any]
) -> ReconciliationDecision:
    """Only a controller-attested execution binding and terminal outcome permit a repair.

    Unknown/legacy rows remain inspectable. A current persisted blocker never becomes
    success solely because a Temporal run returned. The caller rechecks the same
    snapshot and unique row set transactionally before applying this decision.
    """
    status = observed.get("status")
    if not isinstance(status, str) or status not in TERMINAL_ORCHESTRATION:
        return ReconciliationDecision("ACTIVE_OR_UNKNOWN_ORCHESTRATION")
    binding = candidate.get("execution_binding")
    if not isinstance(binding, dict) or binding.get("version") != 1:
        return ReconciliationDecision("EXECUTION_BINDING_UNVERIFIED")
    if candidate.get("run_count") != 1:
        return ReconciliationDecision("AMBIGUOUS_APPLICATION_RUN")
    if not all(
        isinstance(binding.get(key), str) and binding[key]
        for key in ("workflow_id", "first_execution_run_id", "application_run_id")
    ):
        return ReconciliationDecision("EXECUTION_BINDING_UNVERIFIED")
    if (
        binding["workflow_id"] != candidate.get("workflow_id")
        or binding["workflow_id"] != observed.get("workflow_id")
        or binding["application_run_id"] != candidate.get("application_run_id")
        or binding["first_execution_run_id"] != observed.get("first_execution_run_id")
        or not observed.get("run_id")
        or observed.get("identity_verified") is not True
    ):
        return ReconciliationDecision("EXECUTION_LINEAGE_MISMATCH")
    if status == "COMPLETED":
        outcome = observed.get("business_status")
        if (
            observed.get("result_campaign_id") != candidate.get("campaign_id")
            or not isinstance(outcome, str)
            or outcome not in TERMINAL_OUTCOMES
        ):
            return ReconciliationDecision("TERMINAL_OUTCOME_UNVERIFIED")
    else:
        outcome = "CANCELLED" if status == "CANCELED" else "FAILED"
    campaign_status = {"SUCCESS": "COMPLETE", "COMPLETED": "COMPLETE", "CANCELED": "CANCELLED"}.get(
        outcome, outcome
    )
    run_status = {"COMPLETE": "SUCCESS", "COMPLETED": "SUCCESS", "CANCELED": "CANCELLED"}.get(
        outcome, outcome
    )
    existing = candidate.get("run_status")
    if run_status == "SUCCESS" and (
        candidate.get("run_error_code")
        or existing not in {"SUCCESS", "COMPLETE", "COMPLETED"}
        or candidate.get("run_finished_at") is None
    ):
        return ReconciliationDecision("PERSISTED_OUTCOME_CONFLICT")
    if run_status == "SUCCESS":
        # A workflow's returned COMPLETE is not proof that business evidence was
        # persisted. Success may only reconcile an already-finished successful row.
        run_status = cast(str, existing)
    if candidate.get("status") == campaign_status and existing == run_status:
        return ReconciliationDecision("TERMINAL_RECONCILED")
    # Never overwrite an already-final business record with a different terminal fact.
    # Its history/lineage needs explicit investigation, not a new synthetic timestamp.
    if candidate.get("run_finished_at") is not None and existing != run_status:
        return ReconciliationDecision("PERSISTED_OUTCOME_CONFLICT")
    return ReconciliationDecision("TERMINAL_REPAIR_AVAILABLE", campaign_status, run_status)
