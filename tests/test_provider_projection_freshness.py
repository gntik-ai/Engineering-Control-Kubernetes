"""New durable snapshots supersede stale cache rows without erasing history."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from engineering_control.api.main import create_app
from engineering_control.config import Settings
from engineering_control.dashboard.observability import DashboardObservabilityProjection
from engineering_control.db.base import Base
from engineering_control.db.models import ConnectionSnapshot, QuotaSnapshot
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.operations.connection_status import provider_connection_status
from engineering_control.operations.repository import OperationsRepository
from engineering_control.security.redaction import SecretRedactor


@pytest.mark.parametrize(
    "seconds,status", [(0, "CONNECTED"), (299, "CONNECTED"), (301, "STALE"), (-60, "STALE")]
)
def test_provider_status_has_one_freshness_contract(seconds, status):
    now = datetime.now(UTC)
    assert (
        provider_connection_status(
            captured_at=now - timedelta(seconds=seconds),
            authenticated=True,
            cli_present=True,
            rate_limited=False,
            now=now,
        )
        == status
    )


def test_new_quota_wins_over_stale_projection_and_failure_preserves_last_success(tmp_path):
    repo = OperationsRepository(f"sqlite+pysqlite:///{tmp_path / 'provider.db'}")
    Base.metadata.create_all(repo.engine)
    now = datetime.now(UTC)
    old = now - timedelta(days=7)
    try:
        with repo.sessions.begin() as session:
            session.add(
                ConnectionSnapshot(
                    component="provider-codex",
                    logical_target="native-codex-cli",
                    status="STALE",
                    checked_at=old,
                    last_success_at=old,
                    last_failure_at=None,
                    source="quota-snapshots",
                    safe_error="old projection",
                    detail_json={},
                )
            )
            session.add(
                QuotaSnapshot(
                    provider="codex",
                    captured_at=now - timedelta(seconds=10),
                    authenticated=True,
                    cli_present=True,
                    rate_limited=False,
                    running_jobs=0,
                    status="AVAILABLE",
                    source="native_cli_auth_status;capacity_not_exposed",
                    remaining_capacity=None,
                    total_capacity=None,
                )
            )
        first = next(row for row in repo._connections() if row.component == "provider-codex")
        assert first.status == "CONNECTED" and first.freshness_seconds <= 11
        assert first.details["capacity_status"] == "UNKNOWN"
        assert (
            first.details["remaining_capacity"] is None and first.details["total_capacity"] is None
        )
        with repo.sessions.begin() as session:
            session.add(
                QuotaSnapshot(
                    provider="codex",
                    captured_at=now,
                    authenticated=False,
                    cli_present=True,
                    rate_limited=False,
                    running_jobs=0,
                    status="MANUAL_AUTH_REQUIRED_CODEX",
                    source="native_cli_auth_status;capacity_not_exposed",
                    remaining_capacity=None,
                    total_capacity=None,
                )
            )
        second = next(row for row in repo._connections() if row.component == "provider-codex")
        assert second.status == "DEGRADED"
        assert second.last_success_at == first.last_success_at
        assert second.last_failure_at > second.last_success_at
        with repo.sessions() as session:
            assert session.query(ConnectionSnapshot).one().checked_at.replace(tzinfo=UTC) == old
        assert len(repo._connections()) == 2
    finally:
        repo.engine.dispose()


@pytest.mark.parametrize("seconds,current", [(10, True), (301, False), (-60, False)])
def test_api_usage_provider_connections_and_overview_agree(tmp_path, seconds, current):
    url = f"sqlite+pysqlite:///{tmp_path / 'cross-interface.db'}"
    repo = OperationsRepository(url)
    metadata = SQLMetadataRepository(url)
    Base.metadata.create_all(repo.engine)
    captured = datetime.now(UTC) - timedelta(seconds=seconds)
    try:
        with repo.sessions.begin() as session:
            session.add(
                QuotaSnapshot(
                    provider="codex",
                    captured_at=captured,
                    authenticated=True,
                    cli_present=True,
                    rate_limited=False,
                    running_jobs=0,
                    status="AVAILABLE",
                    source="synthetic-native-auth-observation",
                    remaining_capacity=None,
                    total_capacity=None,
                )
            )
        app = create_app(
            settings=Settings(api_admission_mode="synthetic-only"),
            token="inert-cross-interface-token",  # noqa: S106 - synthetic fixture only
            service=AsyncMock(),
            repository=metadata,
            operations_repository=repo,
            command_bus=Mock(),
            gateway_repository=Mock(),
        )
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer inert-cross-interface-token"}
            providers = client.get("/v1/providers", headers=headers).json()["providers"]["codex"]
            usage = client.get("/v1/usage", headers=headers).json()["codex"]
            connections = client.get("/v1/connections", headers=headers).json()["items"]
            connection = next(row for row in connections if row["component"] == "provider-codex")
        overview = DashboardObservabilityProjection(repo.sessions, SecretRedactor()).overview()
        provider = next(row for row in overview["providers"] if row["provider"] == "codex")
        for value in (providers, usage):
            assert value["status"] == ("AVAILABLE" if current else "STALE")
            assert value["raw_status"] == "AVAILABLE"
            assert value["capacity_status"] == "UNKNOWN"
        assert connection["status"] == ("CONNECTED" if current else "STALE")
        assert provider["status"] == connection["status"]
        assert provider["available"] is current
        assert provider["capacity_status"] == "UNKNOWN"
    finally:
        repo.engine.dispose()
        metadata.engine.dispose()
