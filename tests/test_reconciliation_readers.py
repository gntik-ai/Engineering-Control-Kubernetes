import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from engineering_control.config import Settings
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.base import Base
from engineering_control.db.models import AgentExecution, ConnectionSnapshot, Run
from engineering_control.operations.repository import OperationsRepository
from engineering_control.watchdog import OperationalWatchdog


def _iso(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


@pytest.fixture
def dashboard_repo(tmp_path):
    repo = DashboardRepository(f"sqlite:///{tmp_path / 'dashboard.db'}")
    Base.metadata.create_all(repo.engine)
    try:
        yield repo
    finally:
        repo.engine.dispose()


@pytest.fixture
def ops_repo(tmp_path):
    repo = OperationsRepository(
        f"sqlite:///{tmp_path / 'ops.db'}",
        projection_reconciliation_enabled=True,
        projection_reconciliation_interval_seconds=5,
        projection_reconciliation_cycle_seconds=10,
        projection_reconciliation_max_sweep_age_seconds=30,
    )
    Base.metadata.create_all(repo.engine)
    try:
        yield repo
    finally:
        repo.engine.dispose()


def test_workflow_duration_failed_no_end(dashboard_repo):
    wfid = str(uuid.uuid4())
    started = datetime.now(UTC) - timedelta(minutes=5)
    with dashboard_repo.sessions() as session:
        run = Run(
            id=uuid.uuid4(),
            workflow_id=wfid,
            run_type="agent",
            project="falcone",
            status="FAILED",
            started_at=started,
            finished_at=None,
        )
        session.add(run)
        session.commit()

    result = dashboard_repo.observability.workflow(wfid)
    assert result["duration_seconds"] is None
    assert result["duration_basis"] == "unknown"
    assert result["finish_time_status"] == "not_recorded"
    assert result["ended_at"] is None
    assert result["raw_status"] == "FAILED"

    with dashboard_repo.sessions() as session:
        persisted = session.query(Run).filter_by(workflow_id=wfid).one()
        assert persisted.finished_at is None
        context = dashboard_repo._backlog_execution_context(
            session, workflow_id=wfid, run=persisted, change_set=None
        )
        assert context["duration_seconds"] is None
        assert context["finish_time_status"] == "not_recorded"


def test_workflow_queued_no_started_at(dashboard_repo):
    wfid = str(uuid.uuid4())
    with dashboard_repo.sessions() as session:
        run = Run(
            id=uuid.uuid4(),
            workflow_id=wfid,
            run_type="agent",
            project="falcone",
            status="QUEUED",
            started_at=None,
            finished_at=None,
        )
        session.add(run)
        session.commit()
        created_iso = _iso(run.created_at)

    result = dashboard_repo.observability.workflow(wfid)
    assert result["started_at"] is None
    assert result["duration_seconds"] is None
    assert result["queued_at"] == created_iso.replace("+00:00", "Z")


@pytest.mark.parametrize(
    "raw",
    [
        "VERIFICATION_REQUIRED",
        "PARTIAL_BLOCKED",
        "EMPTY",
        "NOT_RUN",
        "DRY_RUN",
        "COMPLETED_WITH_FINDINGS",
    ],
)
def test_terminal_business_outcomes_are_not_active_work(dashboard_repo, raw):
    with dashboard_repo.sessions.begin() as session:
        session.add(
            Run(
                workflow_id="fixture-terminal-outcome",
                run_type="synthetic",
                project="falcone",
                status=raw,
                started_at=datetime.now(UTC) - timedelta(minutes=3),
            )
        )
    item = dashboard_repo.observability.workflow("fixture-terminal-outcome")
    assert item["projection_state"] == "TERMINAL"
    assert item["duration_seconds"] is None
    assert dashboard_repo.observability.overview()["summary"]["active_workflows"] == 0


def test_execution_reader_unfinished(dashboard_repo):
    wfid = str(uuid.uuid4())
    exec_id = uuid.uuid4()
    started = datetime.now(UTC) - timedelta(minutes=2)
    with dashboard_repo.sessions() as session:
        run = Run(
            id=uuid.uuid4(),
            workflow_id=wfid,
            run_type="agent",
            project="falcone",
            status="FAILED",
            started_at=started,
            finished_at=None,
        )
        session.add(run)
        execution = AgentExecution(
            id=exec_id,
            workflow_id=wfid,
            project="falcone",
            provider="codex",
            role="maker",
            host="worker",
            worker_kind="codex",
            task_queue="engineering-codex",
            status="FAILED",
            phase="fixture",
            started_at=started,
            ended_at=None,
            selection_reason="fixture",
            cli_path_identifier="codex",
            cli_version="fixture",
            attempt=1,
            max_attempts=1,
            total_log_bytes=0,
            redaction_count=0,
            output_truncated=False,
            transcript_incomplete=False,
        )
        session.add(execution)
        session.commit()

    result = dashboard_repo.observability.execution(exec_id)
    assert result["duration_seconds"] is None
    assert result["finish_time_status"] == "not_recorded"

    with dashboard_repo.sessions() as session:
        persisted = session.query(AgentExecution).filter_by(id=exec_id).one()
        assert persisted.ended_at is None


def test_connections_empty_reconciler_degraded(ops_repo):
    snapshot = ops_repo._operator_snapshot("readiness-core")
    assert snapshot["status"] == "DEGRADED"
    connections = ops_repo._connections()
    reconciler = next(c for c in connections if c.component == "projection-reconciler")
    assert reconciler.status == "DEGRADED"


def _seed_snapshot(ops_repo, checked_at, last_success_at, healthy=True, healthy_raw=None):
    now_iso = _iso(last_success_at)
    detail = {
        "version": 1,
        "cursor": None,
        "sweep_started_at_utc": now_iso,
        "counts": {},
        "processed_count": 0,
        "repair_count": 0,
        "unresolved_count": 0,
        "error_count": 0,
        "last_sweep": {
            "completed_at_utc": now_iso,
            "processed": 1,
            "repairs": 0,
            "unresolved": 0,
            "errors": 0,
            "healthy": healthy_raw if healthy_raw is not None else healthy,
        },
    }
    with ops_repo.sessions() as session:
        snap = ConnectionSnapshot(
            component="projection-reconciler",
            logical_target="engineering_control.runs",
            status="CONNECTED",
            source="fixture",
            checked_at=checked_at,
            last_success_at=last_success_at,
            detail_json=detail,
        )
        session.add(snap)
        session.commit()


def test_connection_healthy_connected(ops_repo):
    now = datetime.now(UTC)
    _seed_snapshot(ops_repo, now, now, healthy=True)
    connections = ops_repo._connections()
    reconciler = next(c for c in connections if c.component == "projection-reconciler")
    assert reconciler.status == "CONNECTED"


def test_connection_stale_old_checked(ops_repo):
    now = datetime.now(UTC)
    old_checked = now - timedelta(seconds=40)
    _seed_snapshot(ops_repo, old_checked, now, healthy=True)
    connections = ops_repo._connections()
    reconciler = next(c for c in connections if c.component == "projection-reconciler")
    assert reconciler.status == "STALE"


def test_connection_stale_old_sweep(ops_repo):
    now = datetime.now(UTC)
    old_success = now - timedelta(seconds=60)
    _seed_snapshot(ops_repo, now, old_success, healthy=True)
    connections = ops_repo._connections()
    reconciler = next(c for c in connections if c.component == "projection-reconciler")
    assert reconciler.status == "STALE"


def test_connection_invalid_healthy_value_degraded(ops_repo):
    now = datetime.now(UTC)
    _seed_snapshot(ops_repo, now, now, healthy_raw=1)
    connections = ops_repo._connections()
    reconciler = next(c for c in connections if c.component == "projection-reconciler")
    assert reconciler.status == "DEGRADED"


@pytest.mark.parametrize("age", [0, 40])
def test_watchdog_projection_reconciliation_healthy(tmp_path, ops_repo, age):
    now = datetime.now(UTC)
    _seed_snapshot(ops_repo, now - timedelta(seconds=age), now, healthy=True)
    connections = ops_repo._connections()
    reader_status = next(c for c in connections if c.component == "projection-reconciler").status

    settings = Settings(
        database_url=str(ops_repo.engine.url),
        projection_reconciliation_enabled=True,
        projection_reconciliation_interval_seconds=5,
        projection_reconciliation_cycle_seconds=10,
        projection_reconciliation_max_sweep_age_seconds=30,
        watchdog_state_file=str(tmp_path / "state.json"),
    )
    watchdog = OperationalWatchdog(settings, sink=Mock())
    with ops_repo.sessions() as session:
        snapshot = (
            session.query(ConnectionSnapshot).filter_by(component="projection-reconciler").one()
        )
        result = watchdog._projection_reconciliation(snapshot, now)

    assert result.healthy == (reader_status == "CONNECTED")
    watchdog.engine.dispose()


def test_disabled_reconciler_not_configured(tmp_path):
    repo = OperationsRepository(
        f"sqlite:///{tmp_path / 'ops_disabled.db'}",
        projection_reconciliation_enabled=False,
        projection_reconciliation_interval_seconds=5,
        projection_reconciliation_cycle_seconds=10,
        projection_reconciliation_max_sweep_age_seconds=30,
    )
    Base.metadata.create_all(repo.engine)
    try:
        now = datetime.now(UTC)
        _seed_snapshot(repo, now, now)
        connections = repo._connections()
        reconciler = next(c for c in connections if c.component == "projection-reconciler")
        assert reconciler.status == "NOT_CONFIGURED"
    finally:
        repo.engine.dispose()
