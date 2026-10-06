"""Shared bounded-retention constants used by cleanup and observability."""

DASHBOARD_SESSION_REVOCATION_CLEANUP_LIMIT = 10_000
NOTIFICATION_OUTBOX_PENDING_STATUS = "PENDING"
NOTIFICATION_OUTBOX_DELIVERED_STATUS = "DELIVERED"
NOTIFICATION_OUTBOX_UNKNOWN_STATUS = "UNKNOWN"
NOTIFICATION_OUTBOX_WRITE_STATUSES = (
    NOTIFICATION_OUTBOX_PENDING_STATUS,
    NOTIFICATION_OUTBOX_DELIVERED_STATUS,
)
NOTIFICATION_OUTBOX_METRIC_STATUSES = (
    *NOTIFICATION_OUTBOX_WRITE_STATUSES,
    NOTIFICATION_OUTBOX_UNKNOWN_STATUS,
)
# NEW-144: audit events that ChangeSet custody re-proves on every read (a NEW-143 issue
# approval re-issue). Retention keeps one of these only while a ChangeSet's custody chain
# references it (operations.issue_delivery_approval.custody_referenced_event_ids);
# unreferenced or superseded ones age out normally (NEW-144 review 2).
RETAINED_DASHBOARD_EVENT_TYPES = ("issue_approval_reissued",)
