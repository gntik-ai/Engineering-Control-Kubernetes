from __future__ import annotations

from datetime import UTC, datetime

PROVIDER_FRESHNESS_SECONDS = 300
PROVIDER_REFRESH_INTERVAL_SECONDS = 240
PROVIDER_REFRESH_EXECUTION_TIMEOUT_SECONDS = 45


def provider_observation_current(captured_at: datetime, now: datetime) -> bool:
    captured = captured_at if captured_at.tzinfo else captured_at.replace(tzinfo=UTC)
    return -5 <= (now - captured).total_seconds() <= PROVIDER_FRESHNESS_SECONDS


def provider_connection_status(
    *,
    captured_at: datetime,
    authenticated: bool | None,
    cli_present: bool | None,
    rate_limited: bool,
    now: datetime,
) -> str:
    """Current authentication evidence, never a claim of known subscription capacity."""
    if not provider_observation_current(captured_at, now):
        return "STALE"
    if authenticated is True and cli_present is True and not rate_limited:
        return "CONNECTED"
    return "DEGRADED"


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
    if not -5 <= (now - observed).total_seconds() <= limit:
        return "STALE"
    return status
