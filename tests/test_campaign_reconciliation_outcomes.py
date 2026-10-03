import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from temporalio.client import WorkflowExecutionStatus

from engineering_control.api.main import create_app
from engineering_control.api.service import TemporalControlService
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import Campaign, Run
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.operations.reconciliation import decide_reconciliation, observation_token
from engineering_control.operations.repository import OperationsRepository


@pytest.fixture
def scenario(tmp_path):
    postgres = os.environ.get("HERMES_RECONCILIATION_PG_FIXTURE") == "network-none"
    url = (
        "postgresql+psycopg://engineering_ci@127.0.0.1:5432/engineering_control_ci"
        if postgres
        else "sqlite:///" + str(tmp_path / "reconciliation.sqlite")
    )
    repository = OperationsRepository(url)
    if postgres:
        # The outer fixture owns this disposable, loopback-only database. Never
        # accept a caller-selected URL or load service configuration here.
        with repository.engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT current_database(), current_user").one() == (
                "engineering_control_ci",
                "engineering_ci",
            )
        Base.metadata.drop_all(repository.engine)
        Base.metadata.create_all(repository.engine)
    else:
        Campaign.__table__.create(repository.engine)
        Run.__table__.create(repository.engine)
    campaign_id, run_id, first = uuid4(), uuid4(), str(uuid4())
    workflow_id = "audit-fixture-" + str(campaign_id)
    at = datetime(2026, 9, 15, tzinfo=UTC)
    with repository.sessions.begin() as session:
        session.add(
            Campaign(
                id=campaign_id,
                project="falcone",
                name="isolated",
                status="RUNNING",
                metadata_json={
                    "reconciliation_binding": {
                        "version": 1,
                        "workflow_id": workflow_id,
                        "first_execution_run_id": first,
                        "application_run_id": str(run_id),
                    }
                },
                created_at=at,
                updated_at=at,
            )
        )
        session.add(
            Run(
                id=run_id,
                campaign_id=campaign_id,
                workflow_id=workflow_id,
                run_type="audit-campaign",
                project="falcone",
                status="RUNNING",
                created_at=at,
                updated_at=at,
            )
        )
    observation = {
        "workflow_id": workflow_id,
        "run_id": first,
        "first_execution_run_id": first,
        "status": "COMPLETED",
        "business_status": "COMPLETE",
        "identity_verified": True,
        "result_campaign_id": str(campaign_id),
    }
    value = SimpleNamespace(
        repository=repository,
        campaign_id=campaign_id,
        run_id=run_id,
        workflow_id=workflow_id,
        observation=observation,
        at=at,
    )
    yield value
    repository.engine.dispose()


def candidate(s):
    return s.repository._campaign_reconciliation_candidates(2, 0)[0]


def rows(s):
    with s.repository.sessions() as session:
        campaign, run = session.get(Campaign, s.campaign_id), session.get(Run, s.run_id)
        return campaign.status, run.status, run.error_code, run.finished_at


def apply(s, snapshot=None):
    return s.repository._reconcile_terminal_campaign(
        s.campaign_id,
        s.workflow_id,
        s.observation["status"],
        snapshot or candidate(s)["snapshot_token"],
        s.observation,
    )


def client(s):
    service = Mock()
    service.get_reconciliation_observation = AsyncMock(side_effect=lambda wid: s.observation)
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    return TestClient(
        create_app(
            settings=Settings(),
            service=service,
            token="x" * 32,
            operations_repository=s.repository,
            command_bus=bus,
        )
    )


def confirmation(s, token):
    return {
        "confirmation": "APPLY_EXACT_RECONCILIATION_ROWS",
        "rows": [
            {
                "campaign_id": str(s.campaign_id),
                "snapshot_token": token,
                "observation_token": observation_token(s.observation),
            }
        ],
    }


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("COMPLETE", "SUCCESS"),
        ("BLOCKED", "BLOCKED"),
        ("EMPTY", "EMPTY"),
        ("NOT_RUN", "NOT_RUN"),
        ("FAILED", "FAILED"),
        ("DRY_RUN", "DRY_RUN"),
    ],
)
def test_exact_terminal_outcome_not_raw_temporal_success(scenario, outcome, expected):
    s = scenario
    s.observation["business_status"] = outcome
    if outcome == "COMPLETE":
        with s.repository.sessions.begin() as session:
            run = session.get(Run, s.run_id)
            run.status, run.finished_at = "SUCCESS", s.at
    assert apply(s)
    assert rows(s)[1] == expected
    if outcome != "COMPLETE":
        assert rows(s)[3] is None  # Never fabricate a business completion timestamp.


@pytest.mark.parametrize(
    "status", ["BLOCKED", "FAILED", "EMPTY", "NOT_RUN", "CANCELLED", "RUNNING"]
)
def test_persisted_adverse_outcome_cannot_become_success(scenario, status):
    s = scenario
    with s.repository.sessions.begin() as session:
        run = session.get(Run, s.run_id)
        run.status = status
        run.error_code = "CLEANUP_ACTIVITY_FAILED" if status in ("BLOCKED", "FAILED") else None
    before = rows(s)
    assert not apply(s)
    assert rows(s) == before


def test_legacy_raw_status_call_and_unbound_rows_never_write(scenario):
    s = scenario
    assert not s.repository._reconcile_terminal_campaign(s.campaign_id, s.workflow_id, "COMPLETED")
    with s.repository.sessions.begin() as session:
        session.get(Campaign, s.campaign_id).metadata_json = {}
    assert (
        decide_reconciliation(candidate(s), s.observation).classification
        == "EXECUTION_BINDING_UNVERIFIED"
    )
    assert not apply(s)


def test_same_chain_continue_as_new_is_not_same_as_reused_workflow_id(scenario):
    s = scenario
    s.observation["business_status"] = "BLOCKED"
    s.observation["run_id"] = str(uuid4())
    assert (
        decide_reconciliation(candidate(s), s.observation).classification
        == "TERMINAL_REPAIR_AVAILABLE"
    )
    s.observation["first_execution_run_id"] = str(uuid4())
    assert not apply(s)


@pytest.mark.parametrize(
    "field,value",
    [
        ("identity_verified", False),
        ("result_campaign_id", "wrong"),
        ("workflow_id", "wrong"),
        ("business_status", "UNKNOWN"),
        ("first_execution_run_id", ""),
    ],
)
def test_incomplete_or_conflicting_evidence_denied(scenario, field, value):
    s = scenario
    s.observation[field] = value
    assert not apply(s)
    assert rows(s)[:2] == ("RUNNING", "RUNNING")


def test_multiple_runs_never_select_latest(scenario):
    s = scenario
    with s.repository.sessions.begin() as session:
        session.add(
            Run(
                campaign_id=s.campaign_id,
                workflow_id=s.workflow_id,
                run_type="audit-campaign",
                status="RUNNING",
                project="falcone",
            )
        )
    assert candidate(s)["run_count"] == 2 and candidate(s)["application_run_id"] is None
    with pytest.raises(ValueError, match="snapshot changed"):
        apply(s)


def test_changed_row_version_rolls_back_both_rows(scenario):
    s = scenario
    token = candidate(s)["snapshot_token"]
    with s.repository.sessions.begin() as session:
        run = session.get(Run, s.run_id)
        run.status = "BLOCKED"
        run.updated_at = s.at + timedelta(seconds=1)
    before = rows(s)
    with pytest.raises(ValueError, match="snapshot changed"):
        apply(s, token)
    assert rows(s) == before


def test_retry_is_idempotent_and_preserves_real_timestamp(scenario):
    s = scenario
    with s.repository.sessions.begin() as session:
        run = session.get(Run, s.run_id)
        run.status = "SUCCESS"
        run.finished_at = s.at
    token = candidate(s)["snapshot_token"]
    assert apply(s, token)
    after = rows(s)
    with pytest.raises(ValueError, match="snapshot changed"):
        apply(s, token)
    assert not apply(s)
    assert rows(s) == after


def test_http_preview_then_exact_row_apply_and_stale_retry(scenario):
    s = scenario
    with s.repository.sessions.begin() as session:
        run = session.get(Run, s.run_id)
        run.status, run.finished_at = "SUCCESS", s.at
    headers = {"Authorization": "Bearer " + "x" * 32}
    with client(s) as http:
        assert http.get("/v1/campaigns/reconcile").status_code == 401
        preview = http.get("/v1/campaigns/reconcile", headers=headers)
        assert preview.status_code == 200 and preview.json()["status"] == "PREVIEW"
        assert rows(s)[:2] == ("RUNNING", "SUCCESS")
        token = preview.json()["items"][0]["snapshot_token"]
        assert http.post("/v1/campaigns/reconcile", headers=headers).status_code == 400
        response = http.post(
            "/v1/campaigns/reconcile", headers=headers, json=confirmation(s, token)
        )
        assert response.status_code == 200 and response.json()["items"][0]["projection_repaired"]
        repeat = http.post("/v1/campaigns/reconcile", headers=headers, json=confirmation(s, token))
        assert repeat.json()["items"][0]["classification"] == "PREVIEW_STALE"
        assert rows(s)[:2] == ("COMPLETE", "SUCCESS")


def test_changed_temporal_outcome_invalidates_exact_confirmation(scenario):
    s = scenario
    request = confirmation(s, candidate(s)["snapshot_token"])
    s.observation["business_status"] = "BLOCKED"
    with client(s) as http:
        response = http.post(
            "/v1/campaigns/reconcile", headers={"Authorization": "Bearer " + "x" * 32}, json=request
        )
    assert response.json()["items"][0]["classification"] == "PREVIEW_STALE"
    assert rows(s)[:2] == ("RUNNING", "RUNNING")


def test_postgresql_concurrent_update_cannot_commit_stale_campaign(scenario):
    if os.environ.get("HERMES_RECONCILIATION_PG_FIXTURE") != "network-none":
        pytest.skip("requires owned PostgreSQL fixture for real concurrent locking")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import event, select
    from sqlalchemy.exc import OperationalError

    s = scenario
    s.observation["business_status"] = "BLOCKED"
    token = candidate(s)["snapshot_token"]
    before_lock = Event()

    def observed_query(connection, cursor, statement, parameters, context, many):
        if "FROM runs" in statement and "FOR UPDATE" in statement:
            before_lock.set()

    holder = s.repository.sessions()
    held = holder.scalar(select(Run).where(Run.id == s.run_id).with_for_update())
    event.listen(s.repository.engine, "before_cursor_execute", observed_query)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            attempt = executor.submit(apply, s, token)
            assert before_lock.wait(3)
            held.status = "BLOCKED"
            holder.commit()
            with pytest.raises(OperationalError) as failure:
                attempt.result(timeout=6)
            assert failure.value.orig.sqlstate == "40001"
    finally:
        holder.rollback()
        holder.close()
        event.remove(s.repository.engine, "before_cursor_execute", observed_query)
    assert rows(s)[:2] == ("RUNNING", "BLOCKED")


def test_initial_binding_is_immutable_and_acknowledgement_scoped(scenario):
    s = scenario
    metadata = SQLMetadataRepository(str(s.repository.engine.url))
    try:
        metadata._bind_campaign_execution(
            s.campaign_id, s.workflow_id, s.observation["first_execution_run_id"]
        )
        with pytest.raises(ValueError, match="immutable"):
            metadata._bind_campaign_execution(s.campaign_id, s.workflow_id, str(uuid4()))
    finally:
        metadata.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", [False, True])
async def test_temporal_result_is_pinned_and_latest_identity_rechecked(drift):
    first, current, campaign_id = str(uuid4()), str(uuid4()), str(uuid4())

    def description(run_id):
        return SimpleNamespace(
            run_id=run_id,
            status=WorkflowExecutionStatus.COMPLETED,
            raw_description=SimpleNamespace(
                workflow_execution_info=SimpleNamespace(first_run_id=first)
            ),
        )

    latest = Mock()
    latest.describe = AsyncMock(
        side_effect=[description(current), description(str(uuid4()) if drift else current)]
    )
    pinned = Mock()
    pinned.result = AsyncMock(
        return_value={"status": "BLOCKED", "campaign_id": campaign_id, "private": "not exported"}
    )
    temporal = Mock()
    temporal.get_workflow_handle = Mock(
        side_effect=lambda workflow_id, **kw: pinned if kw else latest
    )
    service = TemporalControlService(temporal, "fixture-queue", Mock())
    observed = await service.get_reconciliation_observation("audit-fixture")
    assert observed["identity_verified"] is (not drift)
    assert observed["first_execution_run_id"] == first and observed["run_id"] == current
    assert (
        observed["business_status"] == "BLOCKED" and observed["result_campaign_id"] == campaign_id
    )
    assert "private" not in observed
    temporal.get_workflow_handle.assert_any_call("audit-fixture", run_id=current)
    pinned.result.assert_awaited_once_with(follow_runs=False, rpc_timeout=timedelta(seconds=5))


@pytest.mark.asyncio
async def test_temporal_failure_does_not_become_a_success_result():
    first = str(uuid4())
    description = SimpleNamespace(
        run_id=first,
        status=WorkflowExecutionStatus.FAILED,
        raw_description=SimpleNamespace(
            workflow_execution_info=SimpleNamespace(first_run_id=first)
        ),
    )
    handle = Mock()
    handle.describe = AsyncMock(return_value=description)
    handle.result = AsyncMock()
    temporal = Mock()
    temporal.get_workflow_handle.return_value = handle
    observed = await TemporalControlService(
        temporal, "fixture", Mock()
    ).get_reconciliation_observation("fixture")
    assert observed["status"] == "FAILED" and observed["business_status"] is None
    handle.result.assert_not_awaited()
