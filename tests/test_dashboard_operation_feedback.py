"""Real HTTP/SQLite receipt behavior with explicit, non-network admission input."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import func, select
from test_dashboard_action_retry import prepare
from test_dashboard_session_rotation import client_for, repositories

from engineering_control.dashboard.admission import unknown_admission
from engineering_control.db.models import OperationRequest

__all__ = ["repositories"]


class AdmissionFixture:
    def __init__(self, state: str) -> None:
        self.state = state
        self.calls = 0

    async def read(self):
        self.calls += 1
        if self.state == "ERROR":
            raise OSError("inert-admission-error-not-for-response")
        if self.state == "TIMEOUT":
            await asyncio.sleep(30)
        value = unknown_admission()
        value["status"] = self.state
        return value


@pytest.mark.parametrize(
    "state", ["SYNTHETIC_ONLY", "DISPATCHER_STOPPED", "DISPATCHING", "UNKNOWN", "ERROR"]
)
def test_accepted_receipt_survives_dispatch_observation(repositories, state):
    dashboard, operations = repositories
    observation = AdmissionFixture(state)
    with client_for(dashboard, operations) as client:
        client.app.state.dashboard_service.admission_reader = observation
        body = prepare(client)
        accepted = client.post("/api/dashboard/actions/execute", json=body)
        assert accepted.status_code == 202
        value = accepted.json()
        assert value["status"] == "PENDING"
        assert value["dispatch_observation"]["status"] == ("UNKNOWN" if state == "ERROR" else state)
        identifier = value["operation_id"]
        repeated = client.post("/api/dashboard/actions/execute", json=body)
        assert repeated.json()["operation_id"] == identifier
        status = client.get(f"/api/dashboard/operations/{identifier}")
        assert status.status_code == 200
        assert status.json() == repeated.json()
        assert "inert-admission-error" not in status.text
        assert observation.calls == 3
        with operations.sessions() as session:
            assert session.scalar(select(func.count()).select_from(OperationRequest)) == 1
            row = session.get(OperationRequest, UUID(identifier))
            assert row.status == "PENDING" and row.started_at is None


@pytest.mark.parametrize("state", ["DISPATCHING", "COMPLETED", "STARTED", "FAILED", "BLOCKED"])
def test_observation_never_overrides_authoritative_operation_status(repositories, state):
    dashboard, operations = repositories
    observation = AdmissionFixture("SYNTHETIC_ONLY")
    with client_for(dashboard, operations) as client:
        client.app.state.dashboard_service.admission_reader = observation
        accepted = client.post("/api/dashboard/actions/execute", json=prepare(client))
        identifier = accepted.json()["operation_id"]
        with operations.sessions.begin() as session:
            session.get(OperationRequest, UUID(identifier)).status = state
        observed = client.get(f"/api/dashboard/operations/{identifier}")
        assert observed.status_code == 200 and observed.json()["status"] == state
        assert ("dispatch_observation" in observed.json()) is (state == "DISPATCHING")
        assert observation.calls == (2 if state == "DISPATCHING" else 1)


def test_anonymous_status_does_not_observe_admission(repositories):
    dashboard, operations = repositories
    observation = AdmissionFixture("ERROR")
    with client_for(dashboard, operations) as client:
        client.app.state.dashboard_service.admission_reader = observation
        response = client.get("/api/dashboard/operations/00000000-0000-0000-0000-000000000001")
        assert response.status_code == 401
        assert observation.calls == 0 and "dispatch_observation" not in response.text


def test_slow_observation_preserves_receipt_after_bounded_wait(repositories):
    dashboard, operations = repositories
    with client_for(dashboard, operations) as client:
        client.app.state.dashboard_service.admission_reader = AdmissionFixture("TIMEOUT")
        response = client.post("/api/dashboard/actions/execute", json=prepare(client))
        assert response.status_code == 202
        assert response.json()["status"] == "PENDING"
        assert response.json()["dispatch_observation"]["status"] == "UNKNOWN"


def test_missing_observer_has_no_installed_config_fallback(repositories):
    dashboard, operations = repositories
    with client_for(dashboard, operations) as client:
        assert client.app.state.dashboard_service.admission_reader is None
        response = client.post("/api/dashboard/actions/execute", json=prepare(client))
        assert response.status_code == 202
        assert response.json()["dispatch_observation"]["status"] == "UNKNOWN"
