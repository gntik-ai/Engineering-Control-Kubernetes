from __future__ import annotations

import asyncio
import socket
import ssl
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx

from engineering_control.operations.repository import OperationsRepository
from engineering_control.workflows.worker_registry import ROLE_TASK_QUEUES


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class ConnectionHealthProjector:
    """Runs only fixed read-only probes and consumes safe persisted component snapshots."""

    def __init__(self, repository: OperationsRepository) -> None:
        self.repository = repository

    async def refresh(self, scope: str = "all") -> None:
        now = datetime.now(UTC)
        await self._fixed_http("engineering-api", "http://127.0.0.1:8740/health/ready", now)
        await self._fixed_http("engineering-dashboard", "http://127.0.0.1:8750/health/ready", now)
        await self._fixed_http(
            "dashboard-sse",
            "http://127.0.0.1:8750/api/dashboard/activity/stream",
            now,
            expected=frozenset({401}),
        )
        await self._fixed_tcp("temporal", "127.0.0.1", 7233, now)
        if scope in {"all", "public"}:
            await self._dns(now)
            await self._public_https(now)
            await asyncio.to_thread(self._tls, now)
        await asyncio.to_thread(self._persisted, now)

    async def _fixed_http(
        self,
        component: str,
        url: str,
        now: datetime,
        *,
        expected: frozenset[int] = frozenset({200}),
    ) -> None:
        started = time.monotonic()
        status = "DISCONNECTED"
        safe_error: str | None = None
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                response = await client.get(url, follow_redirects=False)
            status = "CONNECTED" if response.status_code in expected else "DEGRADED"
            if response.status_code not in expected:
                safe_error = f"unexpected HTTP status {response.status_code}"
        except (httpx.HTTPError, OSError):
            safe_error = "bounded HTTP probe failed"
        latency = max(0, int((time.monotonic() - started) * 1000))
        await self.repository.upsert_connection(
            {
                "component": component,
                "logical_target": url.split("?", 1)[0],
                "status": status,
                "checked_at": now,
                "last_success_at": now if status == "CONNECTED" else None,
                "last_failure_at": now if status != "CONNECTED" else None,
                "latency_ms": latency,
                "safe_error": safe_error,
                "source": "bounded-fixed-http-probe",
                "next_action": None if status == "CONNECTED" else "Inspect the owning service.",
                "details": {},
            }
        )

    async def _fixed_tcp(self, component: str, host: str, port: int, now: datetime) -> None:
        started = time.monotonic()
        status = "DISCONNECTED"
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=3.0
            )
            writer.close()
            await writer.wait_closed()
            status = "CONNECTED"
        except (TimeoutError, OSError):
            pass
        await self.repository.upsert_connection(
            {
                "component": component,
                "logical_target": f"{host}:{port}",
                "status": status,
                "checked_at": now,
                "last_success_at": now if status == "CONNECTED" else None,
                "last_failure_at": now if status != "CONNECTED" else None,
                "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
                "safe_error": None if status == "CONNECTED" else "bounded TCP probe failed",
                "source": "bounded-fixed-tcp-probe",
                "next_action": None
                if status == "CONNECTED"
                else "Inspect the Temporal frontend service.",
                "details": {"namespace": "engineering-control"},
            }
        )

    async def _public_https(self, now: datetime) -> None:
        started = time.monotonic()
        http_status: int | None = None
        server = ""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                response = await client.get(
                    "https://ade.musematic.ai/login", follow_redirects=False
                )
            http_status = response.status_code
            server = response.headers.get("server", "")[:40]
        except (httpx.HTTPError, OSError):
            pass
        connected = http_status in {200, 303}
        latency = max(0, int((time.monotonic() - started) * 1000))
        await self.repository.upsert_connection(
            {
                "component": "public-https",
                "logical_target": "https://ade.musematic.ai/login",
                "status": "CONNECTED" if connected else "DISCONNECTED",
                "checked_at": now,
                "last_success_at": now if connected else None,
                "last_failure_at": None if connected else now,
                "latency_ms": latency,
                "safe_error": None if connected else "bounded HTTPS probe failed",
                "source": "bounded-public-https-probe",
                "next_action": None if connected else "Inspect DNS, Caddy, and certificate state.",
                "details": {"http_status": http_status},
            }
        )
        caddy = connected and server.lower() == "caddy"
        await self.repository.upsert_connection(
            {
                "component": "caddy-https",
                "logical_target": "ade.musematic.ai virtual host",
                "status": "CONNECTED" if caddy else "DEGRADED" if connected else "DISCONNECTED",
                "checked_at": now,
                "last_success_at": now if caddy else None,
                "last_failure_at": None if caddy else now,
                "latency_ms": latency,
                "safe_error": None if caddy else "public proxy identity was not confirmed",
                "source": "public-response-header",
                "next_action": None if caddy else "Validate the existing Caddy configuration.",
                "details": {"proxy_identity": "caddy" if caddy else "unconfirmed"},
            }
        )

    def _tls(self, now: datetime) -> None:
        started = time.monotonic()
        status = "DISCONNECTED"
        safe_error: str | None = "TLS certificate probe failed"
        details: dict[str, Any] = {}
        try:
            context = ssl.create_default_context()
            with socket.create_connection(("ade.musematic.ai", 443), timeout=5.0) as raw:
                with context.wrap_socket(raw, server_hostname="ade.musematic.ai") as tls:
                    certificate = cast(dict[str, Any], tls.getpeercert() or {})
            names = {
                value for kind, value in certificate.get("subjectAltName", ()) if kind == "DNS"
            }
            expires_text = str(certificate.get("notAfter", ""))
            expires_at = datetime.fromtimestamp(ssl.cert_time_to_seconds(expires_text), UTC)
            valid = "ade.musematic.ai" in names and expires_at > now
            status = "CONNECTED" if valid else "DEGRADED"
            safe_error = None if valid else "certificate identity or validity differs from policy"
            details = {
                "hostname_valid": "ade.musematic.ai" in names,
                "expires_at": expires_at.isoformat(),
            }
        except (OSError, ssl.SSLError, ValueError):
            pass
        self.repository._upsert_connection(
            {
                "component": "tls-certificate",
                "logical_target": "ade.musematic.ai:443",
                "status": status,
                "checked_at": now,
                "last_success_at": now if status == "CONNECTED" else None,
                "last_failure_at": None if status == "CONNECTED" else now,
                "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
                "safe_error": safe_error,
                "source": "bounded-tls-handshake",
                "next_action": None
                if status == "CONNECTED"
                else "Inspect Caddy certificate state.",
                "details": details,
            }
        )

    async def _dns(self, now: datetime) -> None:
        started = time.monotonic()
        answers: set[str] = set()
        try:
            values = await asyncio.get_running_loop().getaddrinfo(
                "ade.musematic.ai", 443, family=socket.AF_INET, type=socket.SOCK_STREAM
            )
            answers = {str(item[4][0]) for item in values}
        except OSError:
            answers = set()
        connected = answers == {"138.199.173.27"}
        await self.repository.upsert_connection(
            {
                "component": "public-dns",
                "logical_target": "ade.musematic.ai A",
                "status": "CONNECTED" if connected else "DEGRADED",
                "checked_at": now,
                "last_success_at": now if connected else None,
                "last_failure_at": None if connected else now,
                "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
                "safe_error": None if connected else "public A record differs from policy",
                "source": "system-resolver",
                "next_action": None if connected else "Verify public DNS outside the dashboard.",
                "details": {"expected": "138.199.173.27", "answers": sorted(answers)},
            }
        )

    def _persisted(self, now: datetime) -> None:
        sources = self.repository.health_sources()
        self.repository._upsert_connection(
            {
                "component": "postgresql",
                "logical_target": "existing Engineering Control database",
                "status": "CONNECTED",
                "checked_at": now,
                "last_success_at": now,
                "last_failure_at": None,
                "latency_ms": None,
                "safe_error": None,
                "source": "successful-projection-query",
                "next_action": None,
                "details": {},
            }
        )
        heartbeat_by_kind = {row.worker_kind: row for row in sources["heartbeats"]}
        heartbeat_age_by_kind: dict[str, int] = {}
        self.repository._upsert_connection(
            {
                "component": "worker-controller",
                "logical_target": "controller task queue",
                "status": "NOT_INSTALLED",
                "checked_at": now,
                "last_success_at": None,
                "last_failure_at": None,
                "latency_ms": None,
                "safe_error": "no controller role-worker task queue exists",
                "source": "fixed-component-registry",
                "next_action": "Use the engineering-api and Temporal connection records.",
                "details": {"replacement_component": "engineering-api"},
            }
        )
        for registered_kind, task_queue in ROLE_TASK_QUEUES.items():
            kind = str(registered_kind)
            row = heartbeat_by_kind.get(kind)
            age = (
                int(
                    (
                        now
                        - (
                            row.observed_at
                            if row.observed_at.tzinfo
                            else row.observed_at.replace(tzinfo=UTC)
                        )
                    ).total_seconds()
                )
                if row is not None
                else None
            )
            status = (
                "CONNECTED"
                if age is not None and age <= 120
                else "STALE"
                if row
                else "NOT_CONFIGURED"
            )
            if age is not None:
                heartbeat_age_by_kind[kind] = age
            self.repository._upsert_connection(  # fixed internal projection; no external probe
                {
                    "component": f"worker-{kind}",
                    "logical_target": task_queue,
                    "status": status,
                    "checked_at": now,
                    "last_success_at": row.observed_at if row is not None else None,
                    "last_failure_at": None,
                    "latency_ms": None,
                    "safe_error": None
                    if status == "CONNECTED"
                    else "worker heartbeat is unavailable or stale",
                    "source": "worker-heartbeats",
                    "next_action": None
                    if status == "CONNECTED"
                    else "Inspect the owning worker service.",
                    "details": {
                        "host": row.hostname if row is not None else None,
                        "age_seconds": age,
                    },
                }
            )
        for provider, snapshot in sources["quotas"].items():
            if snapshot is None:
                status = "NOT_CONFIGURED"
                checked = now
                details: dict[str, Any] = {}
            else:
                checked = snapshot.captured_at
                age = int(
                    (
                        now - (checked if checked.tzinfo else checked.replace(tzinfo=UTC))
                    ).total_seconds()
                )
                ready = snapshot.cli_present is True and snapshot.authenticated is True
                status = (
                    "STALE"
                    if age > 300
                    else "CONNECTED"
                    if ready and not snapshot.rate_limited
                    else "DEGRADED"
                )
                details = {
                    "cli_present": snapshot.cli_present,
                    "authentication_status": "READY" if snapshot.authenticated else "NOT_READY",
                    "rate_limited": snapshot.rate_limited,
                    "running_jobs": snapshot.running_jobs,
                    "capacity_status": snapshot.status,
                }
            self.repository._upsert_connection(
                {
                    "component": f"provider-{provider}",
                    "logical_target": f"native-{provider}-cli",
                    "status": status,
                    "checked_at": checked,
                    "last_success_at": checked if status == "CONNECTED" else None,
                    "last_failure_at": checked if status == "DEGRADED" else None,
                    "latency_ms": None,
                    "safe_error": None
                    if status == "CONNECTED"
                    else "provider readiness is not current",
                    "source": "quota-snapshots",
                    "next_action": None
                    if status == "CONNECTED"
                    else "Inspect the native provider worker over SSH.",
                    "details": details,
                }
            )
        events = sources["events"]
        gateway_success = next(
            (event for event in events if event.event_type == "telegram.response_delivered"), None
        )
        gateway_failure = next(
            (event for event in events if event.event_type == "telegram.response_delivery_failed"),
            None,
        )
        gateway_inbound = next(
            (event for event in events if event.event_type == "telegram.command_received"), None
        )
        gateway = next(
            (
                event
                for event in events
                if event.event_type
                in {
                    "telegram.response_delivered",
                    "telegram.response_delivery_failed",
                    "telegram.command_received",
                }
            ),
            None,
        )
        gateway_age = (
            int(
                (
                    now
                    - (
                        gateway.occurred_at
                        if gateway and gateway.occurred_at.tzinfo
                        else gateway.occurred_at.replace(tzinfo=UTC)
                    )
                ).total_seconds()
            )
            if gateway is not None
            else None
        )
        gateway_recent = gateway_age is not None and gateway_age <= 600
        failure_is_latest = gateway_failure is not None and (
            gateway_success is None
            or _utc(gateway_failure.occurred_at) >= _utc(gateway_success.occurred_at)
        )
        reply_follows_inbound = gateway_success is not None and (
            gateway_inbound is None
            or _utc(gateway_success.occurred_at) >= _utc(gateway_inbound.occurred_at)
        )
        gateway_status = (
            "STALE"
            if gateway is not None and not gateway_recent
            else "DEGRADED"
            if gateway is not None and (failure_is_latest or not reply_follows_inbound)
            else "CONNECTED"
            if gateway_success is not None
            else "DEGRADED"
            if gateway is not None
            else "NOT_CONFIGURED"
        )
        for component, target in (
            ("hermes-gateway", "hermes user gateway"),
            ("telegram", "Telegram transport health snapshot"),
        ):
            self.repository._upsert_connection(
                {
                    "component": component,
                    "logical_target": target,
                    "status": gateway_status,
                    "checked_at": gateway.occurred_at if gateway else now,
                    "last_success_at": (
                        gateway_success.occurred_at if gateway_success is not None else None
                    ),
                    "last_failure_at": (
                        gateway_failure.occurred_at if gateway_failure is not None else None
                    ),
                    "latency_ms": None,
                    "safe_error": None
                    if gateway_status == "CONNECTED"
                    else "latest Telegram delivery failed"
                    if failure_is_latest and gateway_recent
                    else "safe gateway event is stale or incomplete",
                    "source": "redacted-gateway-events",
                    "next_action": None
                    if gateway_status == "CONNECTED"
                    else "Inspect hermes-gateway.service as hermes.",
                    "details": {"last_event_type": gateway.event_type if gateway else None},
                }
            )
        github = next(
            (
                event
                for event in events
                if event.event_type.startswith("github.")
                or event.event_type.startswith("operation.github")
            ),
            None,
        )
        github_age = (
            int(
                (
                    now
                    - (
                        github.occurred_at
                        if github.occurred_at.tzinfo
                        else github.occurred_at.replace(tzinfo=UTC)
                    )
                ).total_seconds()
            )
            if github is not None
            else None
        )
        github_worker = heartbeat_by_kind.get("github")
        github_worker_current = heartbeat_age_by_kind.get("github", 121) <= 120
        github_status = (
            "CONNECTED"
            if github_age is not None and github_age <= 86400
            else "STALE"
            if github
            else "DEGRADED"
            if github_worker_current
            else "NOT_CONFIGURED"
        )
        github_checked = (
            github.occurred_at
            if github is not None
            else github_worker.observed_at
            if github_worker is not None
            else now
        )
        self.repository._upsert_connection(
            {
                "component": "github-broker",
                "logical_target": "allowlisted GitHub application service",
                "status": github_status,
                "checked_at": github_checked,
                "last_success_at": github.occurred_at if github else None,
                "last_failure_at": None,
                "latency_ms": None,
                "safe_error": None
                if github_status == "CONNECTED"
                else "GitHub worker is current but no recent broker operation is projected"
                if github_status == "DEGRADED"
                else "safe GitHub broker event is stale or absent",
                "source": "redacted-dashboard-events-and-worker-heartbeats",
                "next_action": None
                if github_status == "CONNECTED"
                else "Refresh the backlog through an approved channel.",
                "details": {
                    "last_event_type": github.event_type if github else None,
                    "worker_heartbeat_current": github_worker_current,
                },
            }
        )
        for project, snapshot in sources["projects"].items():
            checked = snapshot.updated_at if snapshot is not None else now
            age = (
                int(
                    (
                        now - (checked if checked.tzinfo else checked.replace(tzinfo=UTC))
                    ).total_seconds()
                )
                if snapshot is not None
                else None
            )
            project_status = (
                "CONNECTED"
                if age is not None and age <= 86400
                else "STALE"
                if snapshot
                else "NOT_CONFIGURED"
            )
            self.repository._upsert_connection(
                {
                    "component": f"project-{project}",
                    "logical_target": f"{project} source/deployment/runtime projection",
                    "status": project_status,
                    "checked_at": checked,
                    "last_success_at": checked if snapshot is not None else None,
                    "last_failure_at": None,
                    "latency_ms": None,
                    "safe_error": None
                    if project_status == "CONNECTED"
                    else "project snapshot is stale or absent",
                    "source": "project-snapshots",
                    "next_action": None
                    if project_status == "CONNECTED"
                    else "Run a bounded project snapshot refresh.",
                    "details": {
                        "status": snapshot.status if snapshot is not None else None,
                        "live_revision": snapshot.live_revision if snapshot is not None else None,
                    },
                }
            )
        cli_path = Path("/opt/engineering-control/venv/bin/engineering-ctl")
        self.repository._upsert_connection(
            {
                "component": "engineering-cli",
                "logical_target": str(cli_path),
                "status": "PRESENT" if cli_path.is_file() else "NOT_INSTALLED",
                "checked_at": now,
                "last_success_at": None,
                "last_failure_at": None,
                "latency_ms": None,
                "safe_error": None if cli_path.is_file() else "engineering-ctl is not installed",
                "source": "fixed-path-metadata",
                "next_action": None
                if cli_path.is_file()
                else "Install the committed Engineering Control release.",
                "details": {"api_compatibility": "v1"},
            }
        )
