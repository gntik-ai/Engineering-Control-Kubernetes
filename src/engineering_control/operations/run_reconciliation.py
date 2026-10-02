"""Reconciliation decision logic for recurring Run/Campaign projections."""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from engineering_control.run_projection import TERMINAL_RUN_OUTCOMES

TERMINAL_OUTCOMES = TERMINAL_RUN_OUTCOMES

_TERMINAL_ORCHESTRATION_STATUSES = ("COMPLETED", "FAILED", "CANCELED", "TERMINATED", "TIMED_OUT")

_SNAPSHOT_FIELDS = (
    "application_run_id",
    "workflow_id",
    "first_execution_run_id",
    "run_count",
    "run_type",
    "run_status",
    "run_error_code",
    "run_finished_at",
    "campaign_id",
    "campaign_status",
    "campaign_run_count",
    "run_updated_at",
    "campaign_updated_at",
    "project",
    "campaign_metadata_sha256",
    "campaign_binding_present",
    "campaign_binding_conflict",
)


@dataclass(frozen=True)
class RunDecision:
    classification: str
    target_run_status: str | None = None
    target_campaign_status: str | None = None
    target_error_code: str | None = None


def _is_valid_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return str(parsed) == value and parsed.int != 0


def _is_valid_workflow_id(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 240 and value.isprintable()


def _exact_int(value: object, expected: int) -> bool:
    return type(value) is int and value == expected


def _normalize_outcome(value: str) -> str:
    if value == "CANCELED":
        return "CANCELLED"
    if value in ("COMPLETE", "COMPLETED"):
        return "SUCCESS"
    return value


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def decide_run_reconciliation(candidate: dict[str, Any], observed: dict[str, Any]) -> RunDecision:
    if not isinstance(candidate, dict) or not isinstance(observed, dict):
        return RunDecision(classification="EXECUTION_BINDING_UNVERIFIED")

    app_run_id = candidate.get("application_run_id")
    workflow_id = candidate.get("workflow_id")
    first_execution_run_id = candidate.get("first_execution_run_id")
    run_type = candidate.get("run_type")

    if (
        not isinstance(run_type, str)
        or not isinstance(candidate.get("run_status"), str)
        or (
            candidate.get("run_error_code") is not None
            and not isinstance(candidate["run_error_code"], str)
        )
    ):
        return RunDecision(classification="EXECUTION_BINDING_UNVERIFIED")

    if (
        not _is_valid_uuid(app_run_id)
        or not _is_valid_workflow_id(workflow_id)
        or not _is_valid_uuid(first_execution_run_id)
    ):
        return RunDecision(classification="EXECUTION_BINDING_UNVERIFIED")

    if not _exact_int(candidate.get("run_count"), 1):
        return RunDecision(classification="AMBIGUOUS_APPLICATION_RUN")

    if (
        observed.get("identity_verified") is not True
        or observed.get("workflow_id") != workflow_id
        or observed.get("first_execution_run_id") != first_execution_run_id
        or not _is_valid_uuid(observed.get("run_id"))
    ):
        return RunDecision(classification="EXECUTION_LINEAGE_MISMATCH")

    is_audit_campaign = run_type == "audit-campaign"
    campaign_id = candidate.get("campaign_id")
    if is_audit_campaign and (
        not _is_valid_uuid(campaign_id) or not _exact_int(candidate.get("campaign_run_count"), 1)
    ):
        return RunDecision(classification="AMBIGUOUS_CAMPAIGN_BINDING")

    observed_status = observed.get("status")
    if observed_status not in _TERMINAL_ORCHESTRATION_STATUSES:
        return RunDecision(classification="ACTIVE_OR_UNKNOWN_ORCHESTRATION")

    if observed_status == "COMPLETED":
        business_status = observed.get("business_status")
        if not isinstance(business_status, str) or business_status not in TERMINAL_OUTCOMES:
            return RunDecision(classification="TERMINAL_OUTCOME_UNVERIFIED")
        if is_audit_campaign and observed.get("result_campaign_id") != campaign_id:
            return RunDecision(classification="TERMINAL_OUTCOME_UNVERIFIED")
        desired_run_status = _normalize_outcome(business_status)
    elif observed_status == "CANCELED":
        desired_run_status = "CANCELLED"
    else:
        desired_run_status = "FAILED"

    existing_status = candidate.get("run_status")
    existing_error = candidate.get("run_error_code")
    existing_finished_at = candidate.get("run_finished_at")
    existing_norm = (
        _normalize_outcome(existing_status) if isinstance(existing_status, str) else None
    )
    existing_terminal = existing_norm in TERMINAL_OUTCOMES if existing_norm else False
    has_error = existing_error is not None

    if desired_run_status == "SUCCESS":
        if existing_norm != "SUCCESS" or not _valid_timestamp(existing_finished_at) or has_error:
            return RunDecision(classification="PERSISTED_OUTCOME_CONFLICT")
    elif (
        existing_terminal or existing_finished_at is not None
    ) and existing_norm != desired_run_status:
        return RunDecision(classification="PERSISTED_OUTCOME_CONFLICT")

    run_matches = existing_norm == desired_run_status
    desired_campaign_status = None
    campaign_matches = True

    if is_audit_campaign:
        desired_campaign_status = (
            "COMPLETE" if desired_run_status == "SUCCESS" else desired_run_status
        )
        campaign_status = candidate.get("campaign_status")
        campaign_norm = (
            _normalize_outcome(campaign_status) if isinstance(campaign_status, str) else None
        )
        campaign_terminal = campaign_norm in TERMINAL_OUTCOMES if campaign_norm else False
        if campaign_terminal and campaign_norm != desired_run_status:
            return RunDecision(classification="PERSISTED_OUTCOME_CONFLICT")
        campaign_matches = campaign_norm == desired_run_status

    if run_matches and campaign_matches:
        return RunDecision(classification="TERMINAL_RECONCILED")

    if has_error:
        target_error_code = existing_error
    elif observed_status != "COMPLETED":
        target_error_code = f"RECONCILED_TEMPORAL_{observed_status}"
    else:
        target_error_code = None

    return RunDecision(
        classification="TERMINAL_REPAIR_AVAILABLE",
        target_run_status=existing_status if run_matches else desired_run_status,
        target_campaign_status=(
            desired_campaign_status if is_audit_campaign and not campaign_matches else None
        ),
        target_error_code=target_error_code,
    )


def run_snapshot_token(candidate: dict[str, Any]) -> str:
    payload = (
        {field: candidate.get(field) for field in _SNAPSHOT_FIELDS}
        if isinstance(candidate, dict)
        else {}
    )
    if isinstance(candidate, dict) and "campaign_metadata_sha256" in candidate:
        payload["campaign_metadata_sha256"] = candidate.get("campaign_metadata_sha256")
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
