from __future__ import annotations

from datetime import UTC, datetime

PROVIDER_FRESHNESS_SECONDS = 300

_FRESHNESS_SECONDS = {
    "dashboard-sse": 300,
    "engineering-api": 300,
    "engineering-dashboard": 300,
    "postgresql": 300,
    "temporal": 300,
    "public-dns": 300,
    "public-https": 300,
    "caddy-https": 300,
    "tls-certificate": 300,
    "hermes-gateway": 600,
    "telegram": 600,
    "github-broker": 900,
}


def freshness_limit(component: str) -> int | None:
    if component == "engineering-cli":
        return None
    if component.startswith("worker-"):
        return 180
    if component.startswith("provider-"):
        return PROVIDER_FRESHNESS_SECONDS
    if component.startswith("project-"):
        return 86_400
    return _FRESHNESS_SECONDS.get(component, 300)


def effective_connection_status(
    component: str,
    status: str,
    checked_at: datetime,
    now: datetime,
) -> str:
    limit = freshness_limit(component)
    if limit is None or status not in {"CONNECTED", "DEGRADED"}:
        return status
    observed = checked_at if checked_at.tzinfo else checked_at.replace(tzinfo=UTC)
    if max(0, int((now - observed).total_seconds())) > limit:
        return "STALE"
    return status
