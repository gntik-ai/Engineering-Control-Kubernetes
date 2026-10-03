"""Tests for recurring Run/Campaign reconciliation: pure policy + SQLAlchemy store."""

from __future__ import annotations

import copy
import datetime
import os
import uuid

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker

from engineering_control.db.models import Base, Campaign, Run
from engineering_control.operations.connection_budget import diagnostic_budget
from engineering_control.operations.repository import OperationsRepository
from engineering_control.operations.run_reconciliation import decide_run_reconciliation
from engineering_control.operations.run_reconciliation_store import RunProjectionStore

NOW = datetime.datetime.now(datetime.UTC).isoformat()


def _base_candidate(**overrides):
    c = {
        "application_run_id": str(uuid.uuid4()),
        "workflow_id": "wf-1",
        "first_execution_run_id": str(uuid.uuid4()),
        "run_count": 1,
        "run_type": "audit-campaign",
        "run_status": "SUCCESS",
        "run_error_code": None,
        "run_finished_at": NOW,
        "campaign_id": str(uuid.uuid4()),
        "campaign_status": "QUEUED",
        "campaign_run_count": 1,
        "project": "falcone",
    }
    c.update(overrides)
    return c


def _base_observed(candidate, **overrides):
    o = {
        "workflow_id": candidate["workflow_id"],
        "run_id": str(uuid.uuid4()),
        "first_execution_run_id": candidate["first_execution_run_id"],
        "status": "COMPLETED",
        "identity_verified": True,
        "business_status": "COMPLETE",
        "result_campaign_id": candidate["campaign_id"],
    }
    o.update(overrides)
    return o


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("first_execution_run_id", None, "EXECUTION_BINDING_UNVERIFIED"),
        (
            "first_execution_run_id",
            "00000000-0000-0000-0000-000000000000",
            "EXECUTION_BINDING_UNVERIFIED",
        ),
        (
            "first_execution_run_id",
            "ABCDEF00-0000-0000-0000-000000000001",
            "EXECUTION_BINDING_UNVERIFIED",
        ),
        ("run_count", True, "AMBIGUOUS_APPLICATION_RUN"),
        ("run_count", 2, "AMBIGUOUS_APPLICATION_RUN"),
    ],
)
def test_policy_invalid_candidate_binding(field, value, expected):
    candidate = _base_candidate(**{field: value})
    observed = _base_observed(candidate)
    decision = decide_run_reconciliation(candidate, observed)
    assert decision.classification == expected
    assert decision.target_run_status is None
    assert decision.target_campaign_status is None


def test_policy_identity_not_verified():
    candidate = _base_candidate()
    observed = _base_observed(candidate, identity_verified=False)
    decision = decide_run_reconciliation(candidate, observed)
    assert decision.classification == "EXECUTION_LINEAGE_MISMATCH"


def test_repair_does_not_rewrite_an_already_correct_campaign_alias():
    candidate = _base_candidate(
        run_status="QUEUED", run_finished_at=None, campaign_status="CANCELED"
    )
    observed = _base_observed(candidate, status="CANCELED", business_status=None)
    decision = decide_run_reconciliation(candidate, observed)
    assert decision.classification == "TERMINAL_REPAIR_AVAILABLE"
    assert decision.target_run_status == "CANCELLED"
    assert decision.target_campaign_status is None


def test_policy_wrong_first_execution_run_id_in_observed():
    candidate = _base_candidate()
    observed = _base_observed(candidate, first_execution_run_id=str(uuid.uuid4()))
    decision = decide_run_reconciliation(candidate, observed)
    assert decision.classification == "EXECUTION_LINEAGE_MISMATCH"


def test_policy_wrong_workflow_id_in_observed():
    candidate = _base_candidate()
    observed = _base_observed(candidate, workflow_id="wf-other")
    decision = decide_run_reconciliation(candidate, observed)
    assert decision.classification == "EXECUTION_LINEAGE_MISMATCH"


def test_policy_malformed_observed_run_id():
    candidate = _base_candidate()
    observed = _base_observed(candidate, run_id="not-a-uuid")
    decision = decide_run_reconciliation(candidate, observed)
    assert decision.classification == "EXECUTION_LINEAGE_MISMATCH"


def test_policy_wrong_result_campaign_id():
    candidate = _base_candidate()
    observed = _base_observed(candidate, result_campaign_id=str(uuid.uuid4()))
    decision = decide_run_reconciliation(candidate, observed)
    assert decision.classification == "TERMINAL_OUTCOME_UNVERIFIED"


def test_policy_unsupported_business_status():
    candidate = _base_candidate()
    observed = _base_observed(candidate, business_status="NOT_A_REAL_STATUS")
    decision = decide_run_reconciliation(candidate, observed)
    assert decision.classification == "TERMINAL_OUTCOME_UNVERIFIED"


@pytest.mark.parametrize("bad_status", [123, None, [], {}])
def test_policy_invalid_business_status_types(bad_status):
    candidate = _base_candidate()
    observed = _base_observed(candidate, business_status=bad_status)
    decision = decide_run_reconciliation(candidate, observed)
    assert decision.classification == "TERMINAL_OUTCOME_UNVERIFIED"


def test_policy_active_orchestration_leaves_queued():
    candidate = _base_candidate(run_status="QUEUED", campaign_status="QUEUED", run_finished_at=None)
    observed = _base_observed(candidate, status="RUNNING")
    decision = decide_run_reconciliation(candidate, observed)
    assert decision.classification == "ACTIVE_OR_UNKNOWN_ORCHESTRATION"


def test_policy_canceled_orchestration_maps_cancelled():
    candidate = _base_candidate(run_status="QUEUED", campaign_status="QUEUED", run_finished_at=None)
    observed = _base_observed(candidate, status="CANCELED")
    decision = decide_run_reconciliation(candidate, observed)
    assert decision.classification == "TERMINAL_REPAIR_AVAILABLE"
    assert decision.target_run_status == "CANCELLED"
    assert decision.target_campaign_status == "CANCELLED"


def test_policy_success_guard_rejects_fake_success_from_queued():
    candidate = _base_candidate(run_status="QUEUED", run_finished_at=None, campaign_status="QUEUED")
    observed = _base_observed(candidate, status="COMPLETED", business_status="COMPLETE")
    decision = decide_run_reconciliation(candidate, observed)
    assert decision.classification == "PERSISTED_OUTCOME_CONFLICT"


def test_policy_success_guard_allows_campaign_queued_to_complete():
    candidate = _base_candidate(run_status="SUCCESS", run_finished_at=NOW, campaign_status="QUEUED")
    observed = _base_observed(candidate, status="COMPLETED", business_status="COMPLETE")
    decision = decide_run_reconciliation(candidate, observed)
    assert decision.classification == "TERMINAL_REPAIR_AVAILABLE"
    assert decision.target_campaign_status == "COMPLETE"
    assert decision.target_run_status == "SUCCESS"


def test_policy_existing_campaign_complete_alias_is_terminal_reconciled():
    candidate = _base_candidate(
        run_status="SUCCESS", run_finished_at=NOW, campaign_status="COMPLETE"
    )
    observed = _base_observed(candidate, status="COMPLETED", business_status="COMPLETE")
    decision = decide_run_reconciliation(candidate, observed)
    assert decision.classification == "TERMINAL_RECONCILED"


def test_policy_existing_terminal_campaign_conflict():
    candidate = _base_candidate(run_status="SUCCESS", run_finished_at=NOW, campaign_status="FAILED")
    observed = _base_observed(candidate, status="COMPLETED", business_status="COMPLETE")
    decision = decide_run_reconciliation(candidate, observed)
    assert decision.classification == "PERSISTED_OUTCOME_CONFLICT"


def test_policy_blocked_preserves_error_even_without_finished_at():
    candidate = _base_candidate(
        run_status="BLOCKED",
        run_finished_at=None,
        run_error_code="CLEANUP_ACTIVITY_FAILED",
        campaign_status="BLOCKED",
    )
    observed = _base_observed(candidate, status="COMPLETED", business_status="BLOCKED")
    decision = decide_run_reconciliation(candidate, observed)
    assert decision.classification == "TERMINAL_RECONCILED"


def test_snapshot_token_changes_with_campaign_metadata_or_updated_at():
    from engineering_control.operations.run_reconciliation import run_snapshot_token

    base = _base_candidate(campaign_metadata_sha256="a" * 64, campaign_updated_at=NOW)
    tok1 = run_snapshot_token(base)
    changed_meta = copy.deepcopy(base)
    changed_meta["campaign_metadata_sha256"] = "b" * 64
    assert run_snapshot_token(changed_meta) != tok1
    changed_updated = copy.deepcopy(base)
    changed_updated["run_updated_at"] = "2099-01-01T00:00:00+00:00"
    assert run_snapshot_token(changed_updated) != tok1


def _row_snapshot(session, instance):
    mapper = inspect(type(instance)).mapper
    return {col.key: copy.deepcopy(getattr(instance, col.key)) for col in mapper.column_attrs}


@pytest.fixture
def db_session(tmp_path):
    database_url = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    repo = OperationsRepository(database_url)
    engine = repo.engine
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield engine, sessions
    finally:
        engine.dispose()


def _seed(sessions, run_status="QUEUED", campaign_status="QUEUED"):
    run_id = uuid.uuid4()
    campaign_id = uuid.uuid4()
    first_id = str(uuid.uuid4())
    wf_id = f"wf-{uuid.uuid4().hex}"
    with sessions() as session:
        campaign = Campaign(
            id=campaign_id,
            name="fixture-audit",
            status=campaign_status,
            project="falcone",
            metadata_json={
                "reconciliation_binding": {
                    "version": 1,
                    "workflow_id": wf_id,
                    "first_execution_run_id": first_id,
                    "application_run_id": str(run_id),
                }
            },
        )
        session.add(campaign)
        session.flush()
        run = Run(
            id=run_id,
            campaign_id=campaign_id,
            status=run_status,
            run_type="audit-campaign",
            project="falcone",
            workflow_id=wf_id,
            temporal_first_execution_run_id=first_id,
            finished_at=None,
        )
        session.add(run)
        session.commit()
    return run_id, campaign_id, first_id, wf_id


def test_store_refresh_returns_candidate_with_snapshot_token(db_session):
    engine, sessions = db_session
    run_id, campaign_id, first_id, wf_id = _seed(sessions)
    store = RunProjectionStore(engine, sessions)
    candidate = store.refresh(run_id)
    assert candidate["application_run_id"] == str(run_id)
    assert candidate["workflow_id"] == wf_id
    assert "snapshot_token" in candidate and candidate["snapshot_token"]


def test_store_apply_positive_failed_path_and_reapply_idempotent(db_session):
    engine, sessions = db_session
    run_id, campaign_id, first_id, wf_id = _seed(sessions)
    store = RunProjectionStore(engine, sessions)
    candidate = store.refresh(run_id)
    with sessions() as session:
        before_run = _row_snapshot(session, session.get(Run, run_id))
    observed = {
        "workflow_id": wf_id,
        "run_id": str(uuid.uuid4()),
        "first_execution_run_id": first_id,
        "status": "FAILED",
        "identity_verified": True,
        "business_status": None,
        "result_campaign_id": None,
    }
    decision = store.apply(candidate, observed)
    assert decision.classification == "TERMINAL_REPAIR_AVAILABLE"
    with sessions() as session:
        run = session.get(Run, run_id)
        campaign = session.get(Campaign, campaign_id)
        assert run.status == "FAILED"
        assert run.error_code == "RECONCILED_TEMPORAL_FAILED"
        assert campaign.status == "FAILED"
        assert campaign.metadata_json["reconciliation_binding"]["workflow_id"] == wf_id
        assert run.finished_at is None
        after_run = _row_snapshot(session, run)
    for key in before_run:
        if key in ("status", "error_code", "updated_at"):
            continue
        assert after_run[key] == before_run[key]

    fresh_candidate = store.refresh(run_id)
    decision2 = store.apply(fresh_candidate, observed)
    assert decision2.classification == "TERMINAL_RECONCILED"
    with sessions() as session:
        run = session.get(Run, run_id)
        assert run.status == "FAILED"
        assert run.error_code == "RECONCILED_TEMPORAL_FAILED"


def test_store_apply_stale_snapshot_rejected(db_session):
    engine, sessions = db_session
    run_id, campaign_id, first_id, wf_id = _seed(sessions)
    store = RunProjectionStore(engine, sessions)
    candidate = store.refresh(run_id)
    with sessions() as session:
        run = session.get(Run, run_id)
        run.summary = "changed-out-of-band"
        run.updated_at = datetime.datetime.now(datetime.UTC)
        session.commit()
    observed = {
        "workflow_id": wf_id,
        "run_id": str(uuid.uuid4()),
        "first_execution_run_id": first_id,
        "status": "FAILED",
        "identity_verified": True,
        "business_status": None,
        "result_campaign_id": None,
    }
    decision = store.apply(candidate, observed)
    assert decision.classification == "SNAPSHOT_CHANGED"
    with sessions() as session:
        run = session.get(Run, run_id)
        assert run.status == "QUEUED"
        assert run.error_code is None


def test_store_candidates_ignores_non_audit_sharing_campaign_id(db_session):
    engine, sessions = db_session
    run_id, campaign_id, first_id, wf_id = _seed(sessions)
    with sessions() as session:
        finding_run = Run(
            id=uuid.uuid4(),
            campaign_id=campaign_id,
            status="QUEUED",
            run_type="finding-verification",
            project="falcone",
            workflow_id=f"wf-{uuid.uuid4().hex}",
            temporal_first_execution_run_id=str(uuid.uuid4()),
            finished_at=None,
        )
        session.add(finding_run)
        session.commit()
    store = RunProjectionStore(engine, sessions)
    candidate = store.refresh(run_id)
    assert candidate["campaign_run_count"] == 1


def test_store_candidates_flags_duplicate_audit_parent_as_ambiguous(db_session):
    engine, sessions = db_session
    run_id, campaign_id, first_id, wf_id = _seed(sessions)
    with sessions() as session:
        dup = Run(
            id=uuid.uuid4(),
            campaign_id=campaign_id,
            status="QUEUED",
            run_type="audit-campaign",
            project="falcone",
            workflow_id=f"wf-{uuid.uuid4().hex}",
            temporal_first_execution_run_id=str(uuid.uuid4()),
            finished_at=None,
        )
        session.add(dup)
        session.commit()
    store = RunProjectionStore(engine, sessions)
    candidate = store.refresh(run_id)
    assert candidate["campaign_run_count"] == 2
    observed = {
        "workflow_id": wf_id,
        "run_id": str(uuid.uuid4()),
        "first_execution_run_id": first_id,
        "status": "FAILED",
        "identity_verified": True,
        "business_status": None,
        "result_campaign_id": None,
    }
    decision = store.apply(candidate, observed)
    assert decision.classification == "AMBIGUOUS_CAMPAIGN_BINDING"


def test_store_candidates_limit_bounds(db_session):
    engine, sessions = db_session
    _seed(sessions)
    store = RunProjectionStore(engine, sessions)
    with pytest.raises(ValueError):
        store.candidates(after=None, limit=0)
    with pytest.raises(ValueError):
        store.candidates(after=None, limit=101)
    results = store.candidates(after=None, limit=100)
    assert results is not None


@pytest.mark.postgresql
def test_store_apply_under_diagnostic_budget_on_owned_postgresql():
    if os.environ.get("ENGINEERING_TEST_POSTGRES_URL") is None:
        pytest.skip("owned PostgreSQL fixture required")
    database_url = "postgresql+psycopg://engineering_ci@127.0.0.1:5432/engineering_control_ci"
    assert os.environ["ENGINEERING_TEST_POSTGRES_URL"] == database_url
    assert os.environ.get("HERMES_RECONCILIATION_PG_FIXTURE") == "network-none"
    repo = OperationsRepository(database_url)
    engine = repo.engine
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        run_id, campaign_id, first_id, wf_id = _seed(sessions)
        store = RunProjectionStore(engine, sessions)
        observed = {
            "workflow_id": wf_id,
            "run_id": str(uuid.uuid4()),
            "first_execution_run_id": first_id,
            "status": "FAILED",
            "identity_verified": True,
            "business_status": None,
            "result_campaign_id": None,
        }
        with diagnostic_budget(15):
            candidate = store.refresh(run_id)
            decision = store.apply(candidate, observed)
        assert decision.classification == "TERMINAL_REPAIR_AVAILABLE"
        with sessions() as session:
            run = session.get(Run, run_id)
            assert run.status == "FAILED"
            assert run.error_code == "RECONCILED_TEMPORAL_FAILED"
        with diagnostic_budget(15):
            fresh = store.refresh(run_id)
            decision2 = store.apply(fresh, observed)
        assert decision2.classification == "TERMINAL_RECONCILED"
    finally:
        engine.dispose()
