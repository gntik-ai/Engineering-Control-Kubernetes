import asyncio
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from temporalio.service import RPCError, RPCStatusCode

from engineering_control.db.base import Base
from engineering_control.db.models import Run
from engineering_control.operations.recurring_reconciliation import RecurringRunReconciler
from engineering_control.operations.repository import OperationsRepository
from engineering_control.operations.run_reconciliation import RunDecision


@pytest.fixture
def repository(tmp_path):
    repo = OperationsRepository(f"sqlite:///{tmp_path / 'loop.db'}")
    Base.metadata.create_all(repo.engine)
    try:
        yield repo
    finally:
        repo.engine.dispose()


@pytest.fixture
def control():
    return SimpleNamespace(
        client=SimpleNamespace(namespace="fixture"),
        get_reconciliation_observation=AsyncMock(),
    )


def make_reconciler(control, repository, **kwargs):
    return RecurringRunReconciler(control, repository, "fixture", **kwargs)


def make_candidate(**overrides):
    candidate = {
        "workflow_id": "wf-1",
        "application_run_id": str(uuid4()),
        "first_execution_run_id": str(uuid4()),
        "run_count": 1,
        "run_type": "observability-acceptance",
        "campaign_run_count": 0,
        "campaign_binding_conflict": False,
        "campaign_binding_present": False,
    }
    candidate.update(overrides)
    return candidate


def test_namespace_mismatch_denied(control, repository):
    with pytest.raises(ValueError):
        RecurringRunReconciler(control, repository, "other-namespace")


def test_bool_config_value_denied(control, repository):
    with pytest.raises(ValueError):
        RecurringRunReconciler(control, repository, "fixture", batch_size=True)


@pytest.mark.asyncio
async def test_wrong_run_count_refuses_temporal(control, repository):
    reconciler = make_reconciler(control, repository)
    candidate = make_candidate(run_count=2)
    classification = await reconciler._classify(candidate)
    assert classification == "AMBIGUOUS_APPLICATION_RUN"
    control.get_reconciliation_observation.assert_not_called()


@pytest.mark.asyncio
async def test_audit_campaign_binding_conflict_refuses_temporal(control, repository):
    reconciler = make_reconciler(control, repository)
    candidate = make_candidate(
        run_type="audit-campaign", campaign_run_count=1, campaign_binding_conflict=True
    )
    classification = await reconciler._classify(candidate)
    assert classification == "AMBIGUOUS_CAMPAIGN_BINDING"
    control.get_reconciliation_observation.assert_not_called()


@pytest.mark.asyncio
async def test_missing_row_binding_invokes_recovery(control, repository, monkeypatch):
    reconciler = make_reconciler(control, repository)
    candidate = make_candidate(first_execution_run_id=None)
    app_uuid = UUID(candidate["application_run_id"])
    current_run_id = str(uuid4())
    first_run_id = str(uuid4())
    control.get_reconciliation_observation.return_value = {
        "workflow_id": candidate["workflow_id"],
        "identity_verified": True,
        "run_id": current_run_id,
        "first_execution_run_id": first_run_id,
    }
    recover_mock = AsyncMock()
    monkeypatch.setattr(
        "engineering_control.operations.recurring_reconciliation.recover_recorded_run",
        recover_mock,
    )
    refreshed = make_candidate(application_run_id=candidate["application_run_id"])
    monkeypatch.setattr(reconciler.store, "refresh", lambda run_id: refreshed)
    monkeypatch.setattr(reconciler.store, "apply", lambda c, o: RunDecision("TERMINAL_RECONCILED"))

    classification = await reconciler._classify(candidate)

    assert classification == "TERMINAL_RECONCILED"
    recover_mock.assert_awaited_once()
    args, kwargs = recover_mock.call_args
    assert args[1] is control.client
    assert args[2] == "fixture"
    assert args[3] == candidate["workflow_id"]
    assert args[4] == current_run_id and type(args[4]) is str
    assert args[5] == app_uuid and type(args[5]) is UUID
    assert kwargs["expected_first_run_id"] == first_run_id
    assert type(kwargs["expected_first_run_id"]) is str


@pytest.mark.asyncio
async def test_workflow_mismatch_refuses_recovery(control, repository, monkeypatch):
    reconciler = make_reconciler(control, repository)
    candidate = make_candidate(first_execution_run_id=None)
    control.get_reconciliation_observation.return_value = {
        "workflow_id": "different-workflow",
        "identity_verified": True,
        "run_id": str(uuid4()),
        "first_execution_run_id": str(uuid4()),
    }
    recover_mock = AsyncMock()
    monkeypatch.setattr(
        "engineering_control.operations.recurring_reconciliation.recover_recorded_run",
        recover_mock,
    )
    classification = await reconciler._classify(candidate)
    assert classification == "IDENTITY_UNVERIFIABLE"
    recover_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_running_verified_is_benign_without_repair_count(control, repository, monkeypatch):
    reconciler = make_reconciler(control, repository)
    candidate = make_candidate()
    control.get_reconciliation_observation.return_value = {
        "workflow_id": candidate["workflow_id"],
        "identity_verified": True,
        "status": "RUNNING",
    }
    monkeypatch.setattr(
        reconciler.store, "apply", lambda c, o: RunDecision("ACTIVE_OR_UNKNOWN_ORCHESTRATION")
    )
    classification = await reconciler._classify(candidate)
    assert classification == "ACTIVE_VERIFIED"

    state = {"counts": {}, "repair_count": 0, "unresolved_count": 0, "error_count": 0}
    reconciler._count(state, classification)
    assert state["repair_count"] == 0
    assert state["unresolved_count"] == 0
    assert state["error_count"] == 0


def test_only_terminal_repair_available_increments_repair(control, repository):
    reconciler = make_reconciler(control, repository)
    state = {"counts": {}, "repair_count": 0, "unresolved_count": 0, "error_count": 0}
    reconciler._count(state, "TERMINAL_REPAIR_AVAILABLE")
    assert state["repair_count"] == 1
    reconciler._count(state, "TERMINAL_RECONCILED")
    assert state["repair_count"] == 1
    reconciler._count(state, "AMBIGUOUS_APPLICATION_RUN")
    assert state["repair_count"] == 1
    assert state["unresolved_count"] == 1


@pytest.mark.asyncio
async def test_partial_batch_retains_cursor_then_empty_page_closes_sweep(
    control, repository, monkeypatch
):
    reconciler = make_reconciler(control, repository, batch_size=2)
    rows = [make_candidate() for _ in range(2)]
    monkeypatch.setattr(reconciler.store, "apply", lambda c, o: RunDecision("TERMINAL_RECONCILED"))
    control.get_reconciliation_observation.return_value = {
        "workflow_id": "wf",
        "identity_verified": True,
    }
    calls = {"n": 0}

    def candidates(cursor, batch_size):
        calls["n"] += 1
        return rows if calls["n"] == 1 else []

    monkeypatch.setattr(reconciler.store, "candidates", candidates)

    first = await reconciler.one_cycle()
    assert first["status"] == "PARTIAL"
    assert first["state"]["cursor"] == rows[-1]["application_run_id"]
    assert first["state"]["last_sweep"] is None

    second = await reconciler.one_cycle()
    assert second["status"] == "SWEEP_COMPLETE"
    assert second["state"]["cursor"] is None
    assert second["state"]["last_sweep"] is not None
    assert second["state"]["last_sweep"]["healthy"] is True


@pytest.mark.asyncio
async def test_rpc_not_found_records_unresolved_and_error(control, repository):
    reconciler = make_reconciler(control, repository)
    candidate = make_candidate()
    control.get_reconciliation_observation.side_effect = RPCError(
        "not found", RPCStatusCode.NOT_FOUND, b""
    )
    classification = await reconciler._classify(candidate)
    assert classification == "HISTORY_OR_EXECUTION_UNAVAILABLE"
    state = {"counts": {}, "repair_count": 0, "unresolved_count": 0, "error_count": 0}
    reconciler._count(state, classification)
    assert state["unresolved_count"] == 1
    assert state["error_count"] == 1


@pytest.mark.asyncio
async def test_cancelled_apply_joins_thread_before_releasing_lock(control, repository, monkeypatch):
    reconciler = make_reconciler(control, repository, batch_size=1)
    candidate = make_candidate()
    control.get_reconciliation_observation.return_value = {
        "workflow_id": candidate["workflow_id"],
        "identity_verified": True,
    }
    monkeypatch.setattr(reconciler.store, "candidates", lambda cursor, batch_size: [candidate])

    thread_started = threading.Event()
    release_apply = threading.Event()
    thread_finished = threading.Event()

    def slow_apply(c, o):
        thread_started.set()
        release_apply.wait(timeout=5)
        thread_finished.set()
        return RunDecision("TERMINAL_RECONCILED")

    monkeypatch.setattr(reconciler.store, "apply", slow_apply)

    release_called = threading.Event()
    real_release = reconciler.state_store.release

    def tracking_release(lease):
        result = real_release(lease)
        release_called.set()
        return result

    monkeypatch.setattr(reconciler.state_store, "release", tracking_release)

    task = asyncio.create_task(reconciler.one_cycle())
    assert await asyncio.wait_for(asyncio.to_thread(thread_started.wait, 5), timeout=6)
    task.cancel()
    await asyncio.sleep(0)
    assert not thread_finished.is_set()
    assert not release_called.is_set()

    release_apply.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert thread_finished.is_set()
    assert release_called.is_set()

    lease = await asyncio.wait_for(asyncio.to_thread(reconciler.state_store.acquire), timeout=5)
    assert lease is not None
    await asyncio.to_thread(real_release, lease)


@pytest.mark.asyncio
async def test_real_postgres_cycle_commits_through_leadership_session():
    url = "postgresql+psycopg://engineering_ci@127.0.0.1:5432/engineering_control_ci"
    if os.environ.get("ENGINEERING_TEST_POSTGRES_URL") is None:
        pytest.skip("owned PostgreSQL absent")
    assert os.environ["ENGINEERING_TEST_POSTGRES_URL"] == url
    assert os.environ.get("HERMES_RECONCILIATION_PG_FIXTURE") == "network-none"
    repo = OperationsRepository(url)
    Base.metadata.create_all(repo.engine)
    row_id, first = uuid4(), str(uuid4())
    workflow_id = f"owned-cycle-{row_id}"
    try:
        with repo.sessions.begin() as session:
            session.add(
                Run(
                    id=row_id,
                    workflow_id=workflow_id,
                    run_type="observability-acceptance",
                    project="falcone",
                    status="QUEUED",
                    temporal_first_execution_run_id=first,
                )
            )
        control = SimpleNamespace(
            client=SimpleNamespace(namespace="fixture"),
            get_reconciliation_observation=AsyncMock(
                return_value={
                    "workflow_id": workflow_id,
                    "run_id": str(uuid4()),
                    "first_execution_run_id": first,
                    "status": "FAILED",
                    "identity_verified": True,
                    "business_status": None,
                    "result_campaign_id": None,
                }
            ),
        )
        reconciler = make_reconciler(control, repo)
        pending = reconciler.store.candidates(None, 100)
        # Earlier tests deliberately retain ambiguous rows in this owned database.
        # This control fixture attests ONLY our execution, not those other rows.
        other_rows = sum(row["workflow_id"] != workflow_id for row in pending)
        result = await asyncio.wait_for(reconciler.one_cycle(), 15)
        assert result["status"] == "SWEEP_COMPLETE"
        assert result["state"]["last_sweep"]["repairs"] == 1
        assert result["state"]["last_sweep"]["processed"] == len(pending)
        assert result["state"]["last_sweep"]["unresolved"] == other_rows
        assert result["state"]["last_sweep"]["healthy"] is (other_rows == 0)
        with repo.sessions() as session:
            row = session.get(Run, row_id)
            assert row.status == "FAILED" and row.error_code == "RECONCILED_TEMPORAL_FAILED"
            assert row.finished_at is None  # No fabricated application completion timestamp.
        lease = reconciler.state_store.acquire()
        assert lease is not None
        reconciler.state_store.release(lease)
    finally:
        repo.engine.dispose()
