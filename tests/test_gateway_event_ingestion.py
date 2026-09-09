"""Gateway retry identity; fixture databases only, no remote API calls."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from engineering_control.api.main import create_app
from engineering_control.db.base import Base
from engineering_control.db.models import DashboardEvent
from engineering_control.observability.agent import (
    AgentObservabilityRepository,
    GatewayEventConflict,
    GatewayEventInput,
)

CORRELATION = "22ed8a68-93ae-4ce3-b94f-9d178fe91935"


def event(**changes):
    return GatewayEventInput.model_validate(
        {
            "event_type": "telegram.command_recognized",
            "command": "usage",
            "delivery_correlation_id": CORRELATION,
            "platform_update_id": 711,
            **changes,
        }
    )


@pytest.fixture(params=["sqlite", "postgresql"])
def repository(request, tmp_path):
    admin = None
    database = "gateway_events_" + uuid4().hex
    url = f"sqlite:///{tmp_path / 'events.db'}"
    if request.param == "postgresql":
        fixture_url = "postgresql+psycopg://engineering_ci@127.0.0.1:5432/engineering_control_ci"
        if "ENGINEERING_TEST_POSTGRES_URL" not in os.environ:
            pytest.skip("requires explicitly supplied isolated PostgreSQL fixture")
        assert os.environ["ENGINEERING_TEST_POSTGRES_URL"] == fixture_url
        admin = create_engine(
            make_url(fixture_url).set(database="postgres"), isolation_level="AUTOCOMMIT"
        )
        with admin.connect() as connection:
            assert connection.scalar(text("SELECT current_user")) == "engineering_ci"
            connection.execute(text(f'CREATE DATABASE "{database}"'))
        url = make_url(fixture_url).set(database=database).render_as_string(False)
    value = AgentObservabilityRepository(url, maximum_transcript_bytes=1024)
    Base.metadata.create_all(value.engine)
    try:
        yield value
    finally:
        value.engine.dispose()
        if admin is not None:
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{database}"'))
            admin.dispose()


@pytest.mark.asyncio
async def test_duplicate_retry_returns_original_row_without_refreshing_time(repository):
    body = event()
    first = await repository.gateway_event("hermes-gateway", body)
    with repository.sessions() as session:
        original = session.get(DashboardEvent, first["event_id"])
        observed = original.occurred_at
    second = await repository.gateway_event("hermes-gateway", body)
    assert second["event_id"] == first["event_id"]
    with repository.sessions() as session:
        rows = list(session.scalars(select(DashboardEvent)))
        assert len(rows) == 1 and rows[0].occurred_at == observed


@pytest.mark.asyncio
async def test_concurrent_duplicate_retry_has_one_committed_event(repository):
    receipts = await asyncio.gather(
        *(repository.gateway_event("hermes-gateway", event()) for _ in range(4))
    )
    assert len({value["event_id"] for value in receipts}) == 1
    with repository.sessions() as session:
        assert len(list(session.scalars(select(DashboardEvent)))) == 1


@pytest.mark.asyncio
async def test_changed_payload_under_same_identity_conflicts_without_mutation(repository):
    first = await repository.gateway_event("hermes-gateway", event(outcome="ORIGINAL"))
    with pytest.raises(GatewayEventConflict):
        await repository.gateway_event("hermes-gateway", event(outcome="CHANGED"))
    with repository.sessions() as session:
        row = session.get(DashboardEvent, first["event_id"])
        assert row.metadata_json["outcome"] == "ORIGINAL"
        assert len(list(session.scalars(select(DashboardEvent)))) == 1


@pytest.mark.asyncio
async def test_same_command_different_stage_or_update_has_distinct_identity(repository):
    bodies = [
        event(),
        event(event_type="telegram.command_received"),
        event(
            delivery_correlation_id="22ed8a68-93ae-4ce3-b94f-9d178fe91936", platform_update_id=712
        ),
    ]
    results = [await repository.gateway_event("hermes-gateway", value) for value in bodies]
    assert len({value["event_id"] for value in results}) == 3


@pytest.mark.asyncio
async def test_delayed_event_keeps_observation_time_not_retry_time(repository):
    observed = datetime.now(UTC) - timedelta(hours=2)
    body = event(event_observed_at=observed)
    first = await repository.gateway_event("hermes-gateway", body)
    second = await repository.gateway_event("hermes-gateway", body)
    assert second["duplicate"] is True and first["event_id"] == second["event_id"]
    with repository.sessions() as session:
        row = session.get(DashboardEvent, first["event_id"])
        assert row.occurred_at.replace(tzinfo=UTC) == observed
        assert datetime.fromisoformat(row.metadata_json["event_received_at"]) > observed


@pytest.mark.asyncio
async def test_equivalent_timezone_forms_have_same_canonical_binding(repository):
    first = await repository.gateway_event(
        "hermes-gateway", event(event_observed_at="2026-08-01T12:00:00+02:00")
    )
    second = await repository.gateway_event(
        "hermes-gateway", event(event_observed_at="2026-08-01T10:00:00Z")
    )
    assert first["event_id"] == second["event_id"]


@pytest.mark.parametrize(
    "changes",
    [
        {"event_observed_at": "2026-08-01T12:00:00"},
        {"event_observed_at": datetime.now(UTC) + timedelta(minutes=1)},
        {"platform_update_id": None},
        {"delivery_correlation_id": None},
        {
            "platform_update_id": None,
            "delivery_correlation_id": None,
            "event_observed_at": datetime.now(UTC),
        },
    ],
)
def test_incomplete_identity_or_invalid_observation_time_is_rejected(changes):
    with pytest.raises(ValidationError):
        event(**changes)


@pytest.mark.asyncio
async def test_legacy_uncorrelated_event_is_explicit_not_invented_identity(repository):
    value = GatewayEventInput(event_type="telegram.command_received", command="usage")
    result = await repository.gateway_event("hermes-gateway", value)
    with repository.sessions() as session:
        row = session.get(DashboardEvent, result["event_id"])
        assert row.gateway_ingestion_key is None
        assert row.metadata_json["event_contract"] == "legacy-unattested"


@pytest.mark.asyncio
async def test_receipt_survives_repository_process_handle_reopen(repository):
    first = await repository.gateway_event("hermes-gateway", event())
    other = AgentObservabilityRepository(str(repository.engine.url), maximum_transcript_bytes=1024)
    try:
        assert (await other.gateway_event("hermes-gateway", event()))["event_id"] == first[
            "event_id"
        ]
    finally:
        other.engine.dispose()


@pytest.mark.asyncio
async def test_unauthorized_identity_creates_no_events(repository):
    with pytest.raises(ValueError, match="not authorized"):
        await repository.gateway_event("other-service", event())
    with repository.sessions() as session:
        assert list(session.scalars(select(DashboardEvent))) == []


def test_api_authentication_conflict_and_duplicate_receipts(repository):
    token = uuid4().hex
    app = create_app(service=cast(Any, object()), token=token, gateway_repository=repository)
    path = "/v1/observability/gateway-events"
    body = event().model_dump(mode="json")
    headers = {"Authorization": "Bearer " + token, "X-Engineering-Actor": "hermes-gateway"}
    with TestClient(app) as client:
        assert client.post(path, json=body).status_code == 401
        assert (
            client.post(
                path, json=body, headers={**headers, "X-Engineering-Actor": "api-client"}
            ).status_code
            == 403
        )
        first = client.post(path, json=body, headers=headers)
        repeated = client.post(path, json=body, headers=headers)
        assert first.status_code == repeated.status_code == 202
        assert first.json()["event_id"] == repeated.json()["event_id"]
        assert repeated.json()["duplicate"] is True
        conflict = client.post(path, json={**body, "outcome": "CHANGED"}, headers=headers)
        assert conflict.status_code == 409
        assert conflict.json() == {"detail": "gateway event identity conflict"}
