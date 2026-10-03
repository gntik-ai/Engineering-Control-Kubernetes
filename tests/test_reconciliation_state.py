"""Tests for ReconciliationStateStore (SQLite + optional PostgreSQL)."""

import copy
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.db.base import Base
from engineering_control.db.models import ConnectionSnapshot
from engineering_control.operations.connection_budget import (
    diagnostic_budget,
    install_diagnostic_sql_budget,
)
from engineering_control.operations.reconciliation_state import ReconciliationStateStore
from engineering_control.operations.repository import OperationsRepository

PG_URL = "postgresql+psycopg://engineering_ci@127.0.0.1:5432/engineering_control_ci"
COMPONENT = "projection-reconciler"


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


@pytest.fixture
def repo(tmp_path):
    r = OperationsRepository(f"sqlite:///{tmp_path / 'fixture.db'}")
    Base.metadata.create_all(r.engine)
    try:
        yield r
    finally:
        r.engine.dispose()


@pytest.fixture
def store(repo):
    return ReconciliationStateStore(repo.engine, repo.sessions, max_sweep_age_seconds=30)


def _row(repo):
    with repo.sessions() as session:
        return session.execute(
            select(ConnectionSnapshot).where(ConnectionSnapshot.component == COMPONENT)
        ).scalar_one()


def test_first_partial_is_degraded_with_no_last_success(repo, store):
    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    state = store.initial(now)
    store.save(state, complete=False, elapsed_ms=5, now=now)
    row = _row(repo)
    assert row.status == "DEGRADED"
    assert row.last_success_at is None
    assert "no completed reconciliation sweep" in row.safe_error


def test_healthy_complete_sweep_is_connected(repo, store):
    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    state = store.initial(now)
    result = store.save(state, complete=True, elapsed_ms=5, now=now)
    row = _row(repo)
    assert row.status == "CONNECTED"
    assert row.safe_error is None
    assert row.next_action is None
    assert _utc(row.last_success_at) == now
    assert result["cursor"] is None
    assert result["last_sweep"]["healthy"] is True


def test_later_partial_with_errors_preserves_last_success(repo, store):
    t0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    state = store.save(store.initial(t0), complete=True, elapsed_ms=5, now=t0)
    t1 = t0 + timedelta(seconds=2)
    partial = copy.deepcopy(state)
    partial["unresolved_count"] = 1
    store.save(partial, complete=False, elapsed_ms=5, now=t1)
    row = _row(repo)
    assert row.status == "DEGRADED"
    assert _utc(row.last_success_at) == t0


def test_error_partial_degrades_without_refreshing_success(repo, store):
    t0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    state = store.save(store.initial(t0), complete=True, elapsed_ms=5, now=t0)
    t1 = t0 + timedelta(seconds=2)
    partial = copy.deepcopy(state)
    partial["error_count"] = 1
    store.save(partial, complete=False, elapsed_ms=5, now=t1)
    row = _row(repo)
    assert row.status == "DEGRADED"
    assert _utc(row.last_success_at) == t0
    assert row.last_failure_at is not None


def test_full_unhealthy_sweep_resets_counters_but_keeps_failed_last_sweep(repo, store):
    t0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    state = store.initial(t0)
    state["cursor"] = str(uuid.uuid4())
    state["unresolved_count"] = 3
    state["processed_count"] = 7
    state["counts"] = {"FOO": 3}
    result = store.save(state, complete=True, elapsed_ms=5, now=t0)
    row = _row(repo)
    assert row.status == "DEGRADED"
    assert result["cursor"] is None
    assert result["counts"] == {}
    assert result["processed_count"] == 0
    assert result["unresolved_count"] == 0
    assert result["last_sweep"]["healthy"] is False
    assert result["last_sweep"]["unresolved"] == 3
    assert result["last_sweep"]["processed"] == 7


def test_stale_full_sweep_never_green(repo, store):
    t0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    state = store.save(store.initial(t0), complete=True, elapsed_ms=5, now=t0)
    t1 = t0 + timedelta(seconds=store._max_sweep_age_seconds + 10)
    store.save(state, complete=False, elapsed_ms=5, now=t1)
    row = _row(repo)
    assert row.status == "DEGRADED"
    assert "stale" in row.safe_error


def _valid_state(now):
    return {
        "version": 1,
        "cursor": None,
        "sweep_started_at_utc": now.isoformat(),
        "counts": {"FOO": 1},
        "processed_count": 1,
        "repair_count": 0,
        "unresolved_count": 0,
        "error_count": 0,
        "last_sweep": None,
    }


def _bad_states(now):
    base = _valid_state(now)

    def mut(**kw):
        s = copy.deepcopy(base)
        s.update(kw)
        return s

    extra = copy.deepcopy(base)
    extra["unexpected"] = 1

    naive_date = copy.deepcopy(base)
    naive_date["sweep_started_at_utc"] = "2026-09-16T00:00:00"

    bad_counts_key = mut(counts={"lowercase": 1})
    bad_counter_bool = mut(processed_count=True)
    nil_cursor = mut(cursor=str(uuid.UUID(int=0)))
    bad_cursor = mut(cursor="not-a-uuid")
    bad_version = mut(version=2)
    bad_last_sweep = mut(
        last_sweep={
            "completed_at_utc": now.isoformat(),
            "processed": 1,
            "repairs": 0,
            "unresolved": 1,
            "errors": 0,
            "healthy": True,
        }
    )
    return {
        "extra_key": extra,
        "naive_date": naive_date,
        "bad_counts_key": bad_counts_key,
        "bool_counter": bad_counter_bool,
        "nil_cursor": nil_cursor,
        "malformed_cursor": bad_cursor,
        "bad_version": bad_version,
        "inconsistent_last_sweep": bad_last_sweep,
    }


@pytest.mark.parametrize("name", list(_bad_states(datetime(2026, 9, 16, tzinfo=UTC)).keys()))
def test_validate_rejects_malformed_states(store, name):
    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    bad = _bad_states(now)[name]
    with pytest.raises(ValueError):
        store.validate(bad)


def test_no_other_component_touched(repo, store):
    other_now = datetime(2026, 9, 1, tzinfo=UTC)
    with repo.sessions.begin() as session:
        session.add(
            ConnectionSnapshot(
                component="unrelated-component",
                logical_target="other.target",
                source="isolated-fixture",
                status="CONNECTED",
                checked_at=other_now,
                detail_json={"marker": True},
            )
        )
    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    store.save(store.initial(now), complete=True, elapsed_ms=5, now=now)
    with repo.sessions() as session:
        other = session.execute(
            select(ConnectionSnapshot).where(ConnectionSnapshot.component == "unrelated-component")
        ).scalar_one()
    assert other.status == "CONNECTED"
    assert other.detail_json == {"marker": True}


def test_sqlite_lock_exclusion_and_release(store):
    token1 = store.acquire()
    assert token1 is not None
    token2 = store.acquire()
    assert token2 is None
    store.release(token1)
    token3 = store.acquire()
    assert token3 is not None
    store.release(token3)


@pytest.mark.skipif(
    os.environ.get("ENGINEERING_TEST_POSTGRES_URL") is None,
    reason="postgres fixture not configured",
)
def test_postgres_advisory_lock_two_independent_stores():
    url = os.environ["ENGINEERING_TEST_POSTGRES_URL"]
    assert url == PG_URL
    assert os.environ.get("HERMES_RECONCILIATION_PG_FIXTURE") == "network-none"
    engine_a = create_engine(url)
    engine_b = create_engine(url)
    install_diagnostic_sql_budget(engine_a)
    install_diagnostic_sql_budget(engine_b)
    sessions_a = sessionmaker(bind=engine_a, class_=Session)
    sessions_b = sessionmaker(bind=engine_b, class_=Session)
    store_a = ReconciliationStateStore(engine_a, sessions_a)
    store_b = ReconciliationStateStore(engine_b, sessions_b)
    try:
        with diagnostic_budget(15):
            token_a = store_a.acquire()
            assert token_a is not None
            token_b = store_b.acquire()
            assert token_b is None
            store_a.release(token_a)
            token_b2 = store_b.acquire()
            assert token_b2 is not None
            store_b.release(token_b2)
    finally:
        engine_a.dispose()
        engine_b.dispose()


@pytest.mark.skipif(
    os.environ.get("ENGINEERING_TEST_POSTGRES_URL") is None, reason="owned PG absent"
)
def test_lost_postgres_leader_cannot_reconnect_and_write():
    assert os.environ["ENGINEERING_TEST_POSTGRES_URL"] == PG_URL
    assert os.environ.get("HERMES_RECONCILIATION_PG_FIXTURE") == "network-none"
    engine = create_engine(PG_URL, pool_size=3)
    install_diagnostic_sql_budget(engine)
    store = ReconciliationStateStore(engine, sessionmaker(bind=engine))
    lease = second = None
    try:
        with diagnostic_budget(15):
            lease = store.acquire()
            assert lease is not None
            owned = store.owned_sessions(lease)
            with owned.begin() as session:
                pid = session.scalar(text("SELECT pg_backend_pid()"))
                assert session.scalar(text("SELECT 1")) == 1
            # Terminate ONLY this fixture-owned, captured backend in network-none PG.
            with engine.begin() as killer:
                assert (
                    killer.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}) is True
                )
            with pytest.raises(DBAPIError):
                with owned.begin() as session:
                    session.scalar(text("SELECT 1"))
            second = store.acquire()
            assert second is not None
            with pytest.raises(ValueError, match="leadership lost"):
                with owned.begin() as session:
                    session.execute(text("CREATE TEMP TABLE must_not_be_created(value int)"))
            with pytest.raises(ValueError, match="leadership lost"):
                store.release(lease)
            lease = None
            assert second.scalar(text("SELECT to_regclass('pg_temp.must_not_be_created')")) is None
            second.commit()
            store.release(second)
            second = None
    finally:
        for connection in (lease, second):
            if connection is not None:
                connection.invalidate()
                connection.close()
        engine.dispose()
