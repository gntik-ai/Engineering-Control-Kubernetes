from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest


class ControlPlaneMetrics:
    """Render a bounded point-in-time view from authoritative metadata."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.active_campaigns = Gauge(
            "engineering_active_campaigns",
            "Active durable audit campaigns.",
            ("project",),
            registry=self.registry,
        )
        self.queued_findings = Gauge(
            "engineering_queued_findings",
            "Candidate or confirmed findings awaiting delivery.",
            ("project", "disposition"),
            registry=self.registry,
        )
        self.active_implementations = Gauge(
            "engineering_active_implementations",
            "ChangeSets in an active delivery state.",
            ("project", "state"),
            registry=self.registry,
        )
        self.runs = Gauge(
            "engineering_runs",
            "Persisted workflow runs by terminal or active status.",
            ("project", "status"),
            registry=self.registry,
        )
        self.active_runs = Gauge(
            "engineering_active_runs",
            "Queued, running, approval-waiting, or rate-limited runs.",
            ("project",),
            registry=self.registry,
        )
        self.provider_rate_limits = Gauge(
            "engineering_provider_rate_limit_events",
            "Persisted provider invocations that reached a rate limit.",
            ("provider",),
            registry=self.registry,
        )
        self.worker_health = Gauge(
            "engineering_worker_health",
            "One when an identity-bound worker heartbeat is current.",
            ("worker", "host"),
            registry=self.registry,
        )
        self.worker_last_seen = Gauge(
            "engineering_worker_last_heartbeat_unixtime",
            "Unix timestamp of the last identity-bound worker heartbeat.",
            ("worker", "host"),
            registry=self.registry,
        )
        self.deployment_duration = Gauge(
            "engineering_deployment_duration_seconds",
            "Mean completed deployment duration in seconds.",
            ("project", "environment"),
            registry=self.registry,
        )
        self.deployment_failures = Gauge(
            "engineering_deployment_failures",
            "Persisted failed deployments.",
            ("project", "environment"),
            registry=self.registry,
        )
        self.verification_failures = Gauge(
            "engineering_verification_failures",
            "Persisted independent verification failures.",
            ("project", "verification_type"),
            registry=self.registry,
        )
        self.evidence_storage = Gauge(
            "engineering_evidence_storage_bytes",
            "Indexed evidence bytes by retention class.",
            ("project", "retention_class"),
            registry=self.registry,
        )
        self.dashboard_session_revocations = Gauge(
            "engineering_dashboard_session_revocations",
            "Persisted dashboard session revocations by active or expired state.",
            ("state",),
            registry=self.registry,
        )
        self.dashboard_session_revocation_cleanup_truncated = Gauge(
            "engineering_dashboard_session_revocation_cleanup_truncated",
            "One when expired dashboard session revocations exceed one cleanup batch.",
            registry=self.registry,
        )
        self.notification_outbox = Gauge(
            "engineering_notification_outbox",
            "Durable notification outbox records by delivery status.",
            ("status",),
            registry=self.registry,
        )
        self.notification_oldest_pending_age = Gauge(
            "engineering_notification_oldest_pending_age_seconds",
            "Age in seconds of the oldest pending durable notification.",
            registry=self.registry,
        )

    def render(self, snapshot: dict[str, Any], *, heartbeat_ttl_seconds: int) -> tuple[bytes, str]:
        gauges = (
            self.active_campaigns,
            self.queued_findings,
            self.active_implementations,
            self.runs,
            self.active_runs,
            self.provider_rate_limits,
            self.worker_health,
            self.worker_last_seen,
            self.deployment_duration,
            self.deployment_failures,
            self.verification_failures,
            self.evidence_storage,
            self.dashboard_session_revocations,
            self.dashboard_session_revocation_cleanup_truncated,
            self.notification_outbox,
            self.notification_oldest_pending_age,
        )
        for gauge in gauges:
            gauge.clear()
        # Gauge.clear() only removes labelled children. Establish an explicit
        # baseline for unlabelled gauges before applying authoritative rows.
        self.dashboard_session_revocation_cleanup_truncated.set(0)
        self.notification_oldest_pending_age.set(0)
        self._rows(self.active_campaigns, snapshot.get("active_campaigns"), 2)
        self._rows(self.queued_findings, snapshot.get("queued_findings"), 3)
        self._rows(self.active_implementations, snapshot.get("active_implementations"), 3)
        self._rows(self.runs, snapshot.get("runs"), 3)
        self._rows(self.active_runs, snapshot.get("active_runs"), 2)
        self._rows(self.provider_rate_limits, snapshot.get("provider_rate_limits"), 2)
        self._rows(self.deployment_duration, snapshot.get("deployment_durations"), 3)
        self._rows(self.deployment_failures, snapshot.get("deployment_failures"), 3)
        self._rows(self.verification_failures, snapshot.get("verification_failures"), 3)
        self._rows(self.evidence_storage, snapshot.get("evidence_storage"), 3)
        self._rows(
            self.dashboard_session_revocations,
            snapshot.get("dashboard_session_revocations"),
            2,
        )
        self._rows(
            self.dashboard_session_revocation_cleanup_truncated,
            snapshot.get("dashboard_session_revocation_cleanup_truncated"),
            1,
        )
        self._rows(self.notification_outbox, snapshot.get("notification_outbox"), 2)
        self._rows(
            self.notification_oldest_pending_age,
            snapshot.get("notification_oldest_pending_age_seconds"),
            1,
        )
        now = datetime.now(UTC).timestamp()
        heartbeats = snapshot.get("worker_heartbeats")
        if isinstance(heartbeats, list):
            for value in heartbeats:
                if not isinstance(value, dict):
                    continue
                identity = value.get("identity")
                host = value.get("hostname")
                observed = value.get("observed_at")
                if not all(isinstance(item, str) for item in (identity, host, observed)):
                    continue
                try:
                    timestamp = datetime.fromisoformat(str(observed)).timestamp()
                except ValueError:
                    continue
                labels = (str(identity), str(host))
                self.worker_last_seen.labels(*labels).set(timestamp)
                self.worker_health.labels(*labels).set(
                    1 if now - timestamp <= heartbeat_ttl_seconds else 0
                )
        return generate_latest(self.registry), CONTENT_TYPE_LATEST

    @staticmethod
    def _rows(gauge: Gauge, values: Any, width: int) -> None:
        if not isinstance(values, list):
            return
        for row in values:
            if not isinstance(row, list) or len(row) != width:
                continue
            *labels, number = row
            if not all(isinstance(label, str) for label in labels):
                continue
            if not isinstance(number, int | float):
                continue
            if labels:
                gauge.labels(*(str(label) for label in labels)).set(float(number))
            else:
                gauge.set(float(number))
