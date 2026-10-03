from __future__ import annotations

from dataclasses import dataclass

NORMALIZED_STATUSES = (
    "backlog",
    "eligible",
    "queued",
    "preparing",
    "implementing",
    "reviewing",
    "reworking",
    "testing",
    "awaiting_ci",
    "awaiting_approval",
    "building",
    "deploying",
    "verifying",
    "blocked",
    "paused",
    "completed",
    "failed",
    "cancelled",
    "skipped",
)

TERMINAL_STATUSES = frozenset({"blocked", "completed", "failed", "cancelled", "skipped"})
_EXACT: dict[str, str] = {}


def _states(normalized: str, *values: str) -> None:
    for value in values:
        _EXACT[value] = normalized


_states(
    "backlog",
    "BACKLOG",
    "IMPORTED",
    "CANDIDATE",
    "CANDIDATE_FOUND",
    "CANDIDATE_ENHANCEMENT",
    "CANDIDATE_MATERIALIZED",
    "OPEN",
    "NOT_STARTED",
)
_states(
    "eligible",
    "ELIGIBLE",
    "ELIGIBLE_FOR_REVALIDATION",
    "ELIGIBLE_FOR_RUNTIME_REVALIDATION",
    "APPROVED",
    "READY",
    "STAGING_APPROVED",
    "CONFIRMED",
    "REPRODUCED_CURRENT_DEPLOYMENT",
    "REPRODUCED_CURRENT_SOURCE",
)
_states("queued", "ACCEPTED", "QUEUED", "PENDING", "RETRY_QUEUED", "NOT_STARTED_QUEUED")
_states(
    "preparing",
    "PREPARING",
    "PLANNED",
    "BACKLOG_REVALIDATION",
    "NEEDS_CURRENT_STATE_REVALIDATION",
    "REPOSITORIES_CAPTURED",
    "CANDIDATE_LOADED",
    "CONFIRMED_LOADED",
    "EVIDENCE_LOADED",
    "EVIDENCE_VALIDATED",
    "LEASES_ACQUIRED",
    "OPENSPEC_PLANNED",
    "OPENSPEC_READY",
    "PAIR_SNAPSHOT_PASS",
    "PREFLIGHT_PASS",
    "PREFLIGHT_VALIDATED",
    "PRECHECKS_PASS",
    "RECONCILIATION_READY",
    "REVALIDATION_RECORDED",
    "ROUTED",
    "SNAPSHOT_PERSISTED",
    "SNAPSHOT_VERIFIED",
    "SPEC_READY",
    "TOOLCHAIN_READY",
    "TOOLCHAIN_SKIPPED",
    "WORKTREES_ALLOCATED",
    "WORKTREE_LEASES_PERSISTED",
)
_states("implementing", "DELIVERY_ACTIVE", "IMPLEMENTING")
_states(
    "reviewing",
    "ADOPTABLE_ACTIVE_PR",
    "CHECKER_READY",
    "RELEASE_REVIEW_REQUIRED",
    "REVIEWING",
    "REVIEW_REQUIRED",
)
_states("reworking", "RETEST_REQUIRED", "REWORKING", "RUNTIME_RETEST_REQUIRED")
_states(
    "testing",
    "CANDIDATE_VERIFIED",
    "CI_RUNNING",
    "DETERMINISTIC_PASS",
    "LOCAL_VALIDATION_PASS",
    "TESTING",
    "TEST_STARTED",
)
_states(
    "awaiting_ci",
    "AWAITING_CI",
    "CI_PENDING",
    "DEPLOY_PR",
    "SOURCE_PR_CHECKS_NOT_REPORTED",
    "SOURCE_PR_CHECKS_PENDING",
)
_states(
    "awaiting_approval",
    "AWAITING_APPROVAL",
    "AWAITING_STAGING_APPROVAL",
    "APPROVAL_REQUIRED",
    "APPROVED_POLICY_PENDING",
    "AWAITING_PREVIEW_APPROVAL",
    "AWAITING_RECOVERY_APPROVAL",
    "AWAITING_RECOVERY_DECISION",
    "AWAITING_WRITE_POLICY",
    "PREVIEW_APPROVED",
    "PRODUCT_REVIEWED",
    "RECOVERY_APPROVED",
)
_states(
    "building",
    "BUILDING",
    "CI_VALIDATED",
    "AWAITING_IMMUTABLE_ARTIFACT",
    "CANDIDATE_BUILT",
    "FINAL_ARTIFACT_BUILT",
)
_states("deploying", "DEPLOYED", "DEPLOYING", "PROMOTION_COMMITTED")
_states(
    "verifying",
    "CURRENT_SOURCE_REVALIDATION_PASS",
    "PREVIEW_DEPLOYED",
    "VERIFYING",
    "PREVIEW_VERIFIED",
    "RUNTIME_OBSERVATION_PASS",
    "STAGING_DEPLOYED",
    "VERIFICATION_REQUIRED",
    "RUNTIME_VERIFIED",
    "VERIFIED_FIXED",
)
_states(
    "blocked",
    "BLOCKED",
    "RATE_LIMITED",
    "BLOCKED_BY_POLICY",
    "BLOCKED_BY_CREDENTIAL",
    "APPROVAL_INVALIDATED",
    "FORWARD_RECOVERY_REQUIRED",
    "INCIDENT_OPEN",
    "INCOMPLETE",
    "MANUAL_GITHUB_REGISTRY_AUTH_REQUIRED",
    "REVALIDATION_BLOCKED",
    "BLOCKED_BY_DELIVERY",
    "CLEANUP_BLOCKED",
    "CONFLICTING_PR",
    "FLAKY",
    "STALE_PR",
)
_states("paused", "PAUSED", "PAUSED_AFTER_CURRENT")
_states(
    "completed",
    "SUCCESS",
    "PASS",
    "COMPLETE",
    "COMPLETED",
    "NO_FINDING",
    "CONFIRMED_FINDING",
    "ISSUE_CLOSED",
    "PUBLISHED",
    "SOURCE_MERGED",
    "ROLLED_BACK",
    "PREVIEW_EXPIRED",
    "DELIVERED",
    "ALREADY_MERGED",
    "ALREADY_PUBLISHED",
    "CLEANUP_EXECUTED",
    "MERGED",
    "PASSED",
    "PERSISTED",
    "REDACTED",
    "ROLLBACK_APPLIED",
    "SCANNED_CLEAN",
    "VERIFIED",
    "VERIFIED_PUBLISHED",
)
_states(
    "failed",
    "FAILED",
    "FAIL",
    "ERROR",
    "TIMED_OUT",
    "DEPLOYMENT_FAILED",
    "VERIFICATION_FAILED",
    "RECOVERY_FAILED",
    "SOURCE_PR_CHECKS_FAILED",
)
_states(
    "cancelled",
    "CANCELLED",
    "CANCELED",
    "CLOSED_WITHOUT_MERGE",
    "REJECTED",
    "SUPERSEDED",
    "STOPPED",
    "STOPPED_AFTER_CURRENT",
)
_states(
    "skipped",
    "ALREADY_FIXED",
    "DEFERRED",
    "DRY_RUN",
    "DUPLICATE",
    "EXCLUDED",
    "NOT_REPRODUCIBLE",
    "SKIPPED",
    "UNRELATED_PR",
)


@dataclass(frozen=True)
class StatusMapping:
    normalized: str
    blocker_code: str | None = None


def map_status(
    raw_status: str | None, *, phase: str | None = None, entity_type: str | None = None
) -> StatusMapping:
    raw = (raw_status or "").strip().upper().replace("-", "_").replace(" ", "_")
    context = f"{phase or ''} {entity_type or ''}".upper()
    if raw == "NOT_STARTED" and any(value in context for value in ("CELL", "COVERAGE")):
        return StatusMapping("queued")
    if raw in {"CANDIDATE_FOUND", "CONFIRMED_FINDING"} and any(
        value in context for value in ("AUDIT", "CELL", "COVERAGE")
    ):
        return StatusMapping("completed")
    if raw in {"RUNNING", "IN_PROGRESS"}:
        contextual = (
            ("reviewing", ("CHECK", "REVIEW", "AUDITOR")),
            ("reworking", ("REWORK", "CORRECTION")),
            ("testing", ("TEST", "CI")),
            ("building", ("BUILD", "ARTIFACT")),
            ("deploying", ("DEPLOY", "PROMOTION")),
            ("verifying", ("VERIFY", "RUNTIME")),
            ("preparing", ("PREPAR", "WORKTREE")),
        )
        for normalized, markers in contextual:
            if any(marker in context for marker in markers):
                return StatusMapping(normalized)
        return StatusMapping("implementing")
    if raw == "SOURCE_PR":
        return StatusMapping("awaiting_ci" if "CI" in context else "reviewing")
    if raw == "REQUEST_CHANGES":
        normalized = "reworking" if any(x in context for x in ("MAKER", "REWORK")) else "reviewing"
        return StatusMapping(normalized)
    if raw == "RETEST_REQUIRED":
        return StatusMapping("reworking" if "CORRECTION" in context else "testing")
    mapped = _EXACT.get(raw)
    if mapped is None:
        return StatusMapping("blocked", "UNKNOWN_STATUS_MAPPING")
    return StatusMapping(mapped)


def normalize_status(
    raw_status: str | None, *, phase: str | None = None, entity_type: str | None = None
) -> str:
    return map_status(raw_status, phase=phase, entity_type=entity_type).normalized


_NEXT_STEP = {
    "backlog": "Evaluate eligibility",
    "eligible": "Queue workflow",
    "queued": "Prepare bounded worktree",
    "preparing": "Start assigned provider work",
    "implementing": "Run deterministic tests",
    "reviewing": "Record independent review verdict",
    "reworking": "Re-run tests after corrections",
    "testing": "Publish CI result",
    "awaiting_ci": "Receive CI result",
    "awaiting_approval": "Receive operator approval decision",
    "building": "Publish immutable build result",
    "deploying": "Run runtime verification",
    "verifying": "Finalize evidence and cleanup",
    "blocked": "Resolve blocker and emit the expected retry event",
    "paused": "Receive an authorized resume signal",
}


def expected_next_step(normalized_status: str, blocker_code: str | None = None) -> str | None:
    if normalized_status == "blocked":
        return (
            f"Resolve {blocker_code} and emit the expected retry event"
            if blocker_code
            else _NEXT_STEP.get("blocked", "Resolve unknown status mapping")
        )
    if normalized_status in TERMINAL_STATUSES:
        return None
    value = _NEXT_STEP.get(normalized_status, "Resolve unknown status mapping")
    return value


def active_first_key(status: str, last_activity: str | None) -> tuple[int, str]:
    return (1 if status in TERMINAL_STATUSES else 0, "" if last_activity is None else last_activity)
