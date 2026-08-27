from __future__ import annotations

NORMALIZED_STATUSES = (
    "backlog",
    "queued",
    "preparing",
    "running",
    "paused",
    "awaiting_approval",
    "blocked",
    "verifying",
    "deploying",
    "completed",
    "failed",
    "cancelled",
    "skipped",
)

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "skipped"})


def normalize_status(raw_status: str | None) -> str:
    raw = (raw_status or "").strip().upper().replace("-", "_")
    if not raw:
        return "backlog"
    exact = {
        "NOT_STARTED": "queued",
        "QUEUED": "queued",
        "PENDING": "queued",
        "PREPARING": "preparing",
        "PLANNED": "preparing",
        "RUNNING": "running",
        "IN_PROGRESS": "running",
        "PAUSED": "paused",
        "AWAITING_APPROVAL": "awaiting_approval",
        "AWAITING_STAGING_APPROVAL": "awaiting_approval",
        "APPROVAL_REQUIRED": "awaiting_approval",
        "BLOCKED": "blocked",
        "RATE_LIMITED": "blocked",
        "VERIFYING": "verifying",
        "RETEST_REQUIRED": "verifying",
        "DEPLOYING": "deploying",
        "PREVIEW_DEPLOYED": "deploying",
        "STAGING_DEPLOYED": "deploying",
        "SUCCESS": "completed",
        "COMPLETE": "completed",
        "COMPLETED": "completed",
        "NO_FINDING": "completed",
        "CONFIRMED_FINDING": "completed",
        "VERIFIED_FIXED": "completed",
        "FAILED": "failed",
        "TIMED_OUT": "failed",
        "CANCELLED": "cancelled",
        "CANCELED": "cancelled",
        "SKIPPED": "skipped",
    }
    if raw in exact:
        return exact[raw]
    fragments = (
        (("AWAIT", "APPROVAL"), "awaiting_approval"),
        (("PAUS",), "paused"),
        (("BLOCK",), "blocked"),
        (("FAIL",), "failed"),
        (("ERROR",), "failed"),
        (("CANCEL",), "cancelled"),
        (("SKIP",), "skipped"),
        (("VERIFY",), "verifying"),
        (("RETEST",), "verifying"),
        (("DEPLOY",), "deploying"),
        (("RUN",), "running"),
        (("IMPLEMENT",), "running"),
        (("PREPAR",), "preparing"),
        (("PLAN",), "preparing"),
        (("QUEUE",), "queued"),
        (("PENDING",), "queued"),
        (("COMPLETE",), "completed"),
        (("SUCCESS",), "completed"),
        (("CLOSED",), "completed"),
        (("FIXED",), "completed"),
    )
    for needles, normalized in fragments:
        if all(needle in raw for needle in needles):
            return normalized
    return "backlog"


def active_first_key(status: str, last_activity: str | None) -> tuple[int, str]:
    return (1 if status in TERMINAL_STATUSES else 0, "" if last_activity is None else last_activity)
