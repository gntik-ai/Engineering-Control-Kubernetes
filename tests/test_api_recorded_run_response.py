import asyncio
import os
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from engineering_control.api.main import create_app
from engineering_control.api.schemas import CampaignStart
from engineering_control.api.service import TemporalControlService
from engineering_control.config import Settings
from engineering_control.db.models import Base, Campaign, Run
from engineering_control.db.repository import EmptyMetadataRepository, SQLMetadataRepository
from engineering_control.operations.run_start import RecordedRunStartUnconfirmedError


@pytest.fixture
def repository(tmp_path):
    value = SQLMetadataRepository(f"sqlite:///{tmp_path / 'run.db'}")
    Base.metadata.create_all(value.engine)
    try:
        yield value
    finally:
        value.engine.dispose()


def request() -> CampaignStart:
    return CampaignStart(project="falcone", preset="static-full", environment="staging")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "start_timeout",
        "run_bind",
        "campaign_bind",
        "cancel",
        "missing",
        None,
        "",
        4,
        "00000000-0000-0000-0000-000000000000",
        "DEADBEEF-1111-4111-8111-111111111111",
    ],
)
async def test_start_failure_preserves_exact_row_and_never_restarts(repository, fault):
    first_id = str(uuid4())
    client = SimpleNamespace(
        start_workflow=AsyncMock(return_value=SimpleNamespace(first_execution_run_id=first_id))
    )
    if fault == "start_timeout":
        client.start_workflow.side_effect = TimeoutError("private transport diagnostic")
    elif fault == "run_bind":
        repository.bind_recorded_run = AsyncMock(side_effect=ValueError("private DB diagnostic"))
    elif fault == "campaign_bind":
        repository.bind_campaign_execution = AsyncMock(
            side_effect=ValueError("private DB diagnostic")
        )
    elif fault == "cancel":
        client.start_workflow.side_effect = asyncio.CancelledError()
    elif fault == "missing":
        client.start_workflow.return_value = SimpleNamespace()
    else:
        client.start_workflow.return_value.first_execution_run_id = fault
    service = TemporalControlService(client, "fixture-only", repository)
    expected = asyncio.CancelledError if fault == "cancel" else RecordedRunStartUnconfirmedError
    with pytest.raises(expected) as caught:
        await service.start_campaign(request())
    client.start_workflow.assert_awaited_once()
    call = client.start_workflow.await_args
    app_id = UUID(call.kwargs["memo"]["engineering_application_run_id"])
    with repository.sessions() as session:
        row = session.scalars(select(Run)).one()
        assert row.id == app_id and row.workflow_id == call.kwargs["id"]
        assert row.status == "QUEUED"
        assert row.temporal_first_execution_run_id == (
            first_id if fault == "campaign_bind" else None
        )
        campaign = session.get(Campaign, row.campaign_id)
        assert "reconciliation_binding" not in campaign.metadata_json
    if fault != "cancel":
        assert caught.value.application_run_id == app_id
        assert caught.value.workflow_id == call.kwargs["id"]
        assert "private" not in str(caught.value)


@pytest.mark.asyncio
async def test_empty_repository_denies_before_temporal_start():
    client = SimpleNamespace(start_workflow=AsyncMock())
    with pytest.raises(ValueError, match="durable run metadata store is unavailable"):
        await TemporalControlService(
            client, "fixture-only", EmptyMetadataRepository()
        ).start_campaign(request())
    client.start_workflow.assert_not_awaited()


@pytest.mark.asyncio
async def test_binding_is_idempotent_and_does_not_mutate_progress(repository):
    first_id = str(uuid4())
    before = {}

    async def started(*args, **kwargs):
        with repository.sessions() as session:
            row = session.get(Run, UUID(kwargs["memo"]["engineering_application_run_id"]))
            before.update({c.name: getattr(row, c.name) for c in Run.__table__.columns})
        return SimpleNamespace(first_execution_run_id=first_id)

    client = SimpleNamespace(start_workflow=AsyncMock(side_effect=started))
    accepted = await TemporalControlService(client, "fixture-only", repository).start_campaign(
        request()
    )
    await repository.bind_recorded_run(before["id"], accepted.workflow_id, first_id)
    with pytest.raises(ValueError, match="different execution"):
        await repository.bind_recorded_run(before["id"], accepted.workflow_id, str(uuid4()))
    with repository.sessions() as session:
        row = session.get(Run, before["id"])
        after = {c.name: getattr(row, c.name) for c in Run.__table__.columns}
    assert after == before | {"temporal_first_execution_run_id": first_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", [UUID(int=0), "11111111-1111-4111-8111-111111111111"])
async def test_invalid_explicit_row_id_rolls_back_campaign(repository, bad_id):
    with pytest.raises(ValueError, match="non-nil UUID"):
        await repository.create_campaign(
            uuid4(), "fixture-only", request(), application_run_id=bad_id
        )
    with repository.sessions() as session:
        assert session.scalars(select(Run)).all() == []


@pytest.mark.asyncio
async def test_real_postgresql_api_ack_persists_exact_binding():
    fixture_url = "postgresql+psycopg://engineering_ci@127.0.0.1:5432/engineering_control_ci"
    if os.environ.get("ENGINEERING_TEST_POSTGRES_URL") is None:
        pytest.skip("isolated PostgreSQL fixture required")
    assert os.environ["ENGINEERING_TEST_POSTGRES_URL"] == fixture_url
    assert os.environ.get("HERMES_RECONCILIATION_PG_FIXTURE") == "network-none"
    repo = SQLMetadataRepository(fixture_url)
    Base.metadata.create_all(repo.engine)
    first_id = str(uuid4())
    client = SimpleNamespace(
        start_workflow=AsyncMock(return_value=SimpleNamespace(first_execution_run_id=first_id))
    )
    try:
        accepted = await TemporalControlService(client, "fixture-only", repo).start_campaign(
            request()
        )
        app_id = UUID(
            client.start_workflow.await_args.kwargs["memo"]["engineering_application_run_id"]
        )
        with repo.sessions() as session:
            row = session.get(Run, app_id)
            assert row.workflow_id == accepted.workflow_id
            assert row.temporal_first_execution_run_id == first_id and row.status == "QUEUED"
            original_timestamp = row.updated_at
        await repo.bind_recorded_run(app_id, accepted.workflow_id, first_id)
        with pytest.raises(ValueError, match="different execution"):
            await repo.bind_recorded_run(app_id, accepted.workflow_id, str(uuid4()))
        with repo.sessions() as session:
            row = session.get(Run, app_id)
            assert row.temporal_first_execution_run_id == first_id
            assert row.updated_at == original_timestamp
    finally:
        repo.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [
        ("create_campaign", (uuid4(), "fixture", None)),
        ("create_multi_project_audit", ("fixture", {}, None)),
        *[
            (name, ("fixture", None))
            for name in (
                "create_delivery_run",
                "create_finding_verification_run",
                "create_delivery_reconciliation_run",
                "create_deployment_run",
                "create_runtime_verification_run",
                "create_recovery_run",
            )
        ],
    ],
)
async def test_all_empty_create_paths_reject_explicit_id(name, args):
    with pytest.raises(ValueError, match="durable run metadata store is unavailable"):
        await getattr(EmptyMetadataRepository(), name)(*args, application_run_id=uuid4())


@pytest.mark.asyncio
async def test_duplicate_row_id_rolls_back_new_campaign_and_keeps_original(repository):
    app_id, original_campaign, rejected_campaign = uuid4(), uuid4(), uuid4()
    await repository.create_campaign(
        original_campaign, "first", request(), application_run_id=app_id
    )
    with pytest.raises(IntegrityError):
        await repository.create_campaign(
            rejected_campaign, "second", request(), application_run_id=app_id
        )
    with repository.sessions() as session:
        assert session.get(Campaign, rejected_campaign) is None
        assert session.get(Run, app_id).workflow_id == "first"


@pytest.mark.asyncio
async def test_unsupported_binding_dialect_fails_closed(repository, monkeypatch):
    monkeypatch.setattr(repository.engine.dialect, "name", "unsupported")
    with pytest.raises(ValueError, match="unsupported run binding database"):
        await repository.bind_recorded_run(uuid4(), "fixture", str(uuid4()))


@pytest.mark.asyncio
async def test_inflight_binding_deadline_preserves_unconfirmed_row(repository, monkeypatch):
    # Exercise the real timeout context with a scaled deadline and a genuinely suspended bind.
    original_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda _seconds: original_timeout(0.03))
    entered = asyncio.Event()

    async def wait_for_binding(*args):
        entered.set()
        await asyncio.Event().wait()

    repository.bind_recorded_run = wait_for_binding
    first_id = str(uuid4())
    client = SimpleNamespace(
        start_workflow=AsyncMock(return_value=SimpleNamespace(first_execution_run_id=first_id))
    )
    with pytest.raises(RecordedRunStartUnconfirmedError) as caught:
        await TemporalControlService(client, "fixture-only", repository).start_campaign(request())
    assert entered.is_set()
    client.start_workflow.assert_awaited_once()
    with repository.sessions() as session:
        row = session.get(Run, caught.value.application_run_id)
        assert row.temporal_first_execution_run_id is None
        assert "reconciliation_binding" not in session.get(Campaign, row.campaign_id).metadata_json


@pytest.mark.asyncio
async def test_repository_postgresql_binding_lock_timeout_preserves_row():
    fixture_url = "postgresql+psycopg://engineering_ci@127.0.0.1:5432/engineering_control_ci"
    if os.environ.get("ENGINEERING_TEST_POSTGRES_URL") is None:
        pytest.skip("isolated PostgreSQL fixture required")
    assert os.environ["ENGINEERING_TEST_POSTGRES_URL"] == fixture_url
    assert os.environ.get("HERMES_RECONCILIATION_PG_FIXTURE") == "network-none"
    repo = SQLMetadataRepository(fixture_url)
    Base.metadata.create_all(repo.engine)
    app_id, workflow_id = uuid4(), f"fixture-{uuid4()}"
    try:
        await repo.create_campaign(uuid4(), workflow_id, request(), application_run_id=app_id)
        with repo.sessions.begin() as locked:
            locked.scalar(select(Run).where(Run.id == app_id).with_for_update())
            started = time.monotonic()
            with pytest.raises(OperationalError) as caught:
                await repo.bind_recorded_run(app_id, workflow_id, str(uuid4()))
            assert getattr(caught.value.orig, "sqlstate", None) == "55P03"
            assert 2.5 <= time.monotonic() - started < 8
        with repo.sessions() as session:
            assert session.get(Run, app_id).temporal_first_execution_run_id is None
        await repo.bind_recorded_run(app_id, workflow_id, str(uuid4()))
    finally:
        repo.engine.dispose()


def test_unconfirmed_run_response_keeps_identifiers_and_auth_boundary(tmp_path) -> None:
    app_id = uuid4()
    workflow_id = f"fixture-audit-{uuid4()}"

    class Service:
        calls = 0

        async def start_campaign(self, request: Any) -> None:
            self.calls += 1
            raise RecordedRunStartUnconfirmedError(workflow_id, app_id)

    service = Service()
    app = create_app(
        settings=Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'http.db'}"),
        service=service,  # type: ignore[arg-type]
        token="x" * 32,
        gateway_repository=object(),  # type: ignore[arg-type]
    )
    payload = {"project": "falcone", "preset": "static-full", "environment": "staging"}
    with TestClient(app) as client:
        denied = client.post("/v1/campaigns", json=payload)
        assert denied.status_code in {401, 403}
        assert service.calls == 0
        assert str(app_id) not in denied.text
        response = client.post(
            "/v1/campaigns", json=payload, headers={"Authorization": "Bearer " + "x" * 32}
        )
    assert service.calls == 1
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "status": "START_UNCONFIRMED",
        "application_run_id": str(app_id),
        "expected_workflow_id": workflow_id,
        "detail": (
            "Start acknowledgement is unconfirmed; preserve this Run and inspect "
            "its status. Do not start a replacement workflow."
        ),
    }
