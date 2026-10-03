from __future__ import annotations

import os
import queue
import re
import threading
import time
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from engineering_control.db.models import Base, Run
from engineering_control.db.run_binding import bind_run_execution

EXPECTED_URL = "postgresql+psycopg://engineering_ci@127.0.0.1:5432/engineering_control_ci"
CONFLICT_MESSAGE = "run is already bound to a different execution"


def _require_url() -> str:
    url = os.environ.get("ENGINEERING_TEST_POSTGRES_URL")
    if url is None:
        pytest.skip("ENGINEERING_TEST_POSTGRES_URL not set")
    if url != EXPECTED_URL:
        pytest.fail("unexpected fixture target; refusing to run")
    return url


@pytest.fixture(scope="module")
def engine():
    url = _require_url()
    eng = create_engine(url, future=True, connect_args={"options": "-c statement_timeout=10000"})
    try:
        Base.metadata.create_all(eng)
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, future=True)


def _new_run_id() -> UUID:
    return uuid4()


def _new_workflow_id() -> str:
    return f"wf-{uuid4()}"


def _make_run(factory: sessionmaker, run_id: UUID, workflow_id: str) -> None:
    with factory() as session:
        session.add(
            Run(
                id=run_id,
                workflow_id=workflow_id,
                run_type="synthetic-test",
                status="RUNNING",
                project="platform",
            )
        )
        session.commit()


def _snapshot(factory: sessionmaker, run_id: UUID) -> dict:
    with factory() as session:
        row = session.execute(select(Run).where(Run.id == run_id)).scalar_one()
        return {c.name: getattr(row, c.name) for c in Run.__table__.columns}


def _backend_pid(session) -> int:
    return session.execute(text("SELECT pg_backend_pid()")).scalar_one()


def _wait_for_lock_wait(factory: sessionmaker, pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with factory() as session:
            waiting = session.execute(
                text(
                    "SELECT 1 FROM pg_stat_activity WHERE pid = :pid AND wait_event_type = 'Lock'"
                ),
                {"pid": pid},
            ).first()
        if waiting is not None:
            return True
        time.sleep(0.05)
    return False


def test_persisted_columns_and_rollback_preserve_baseline(session_factory):
    committed_run_id = _new_run_id()
    workflow_id = _new_workflow_id()
    _make_run(session_factory, committed_run_id, workflow_id)
    before_commit = _snapshot(session_factory, committed_run_id)

    with session_factory() as session:
        assert bind_run_execution(session, committed_run_id, workflow_id, str(uuid4())) is True
        session.commit()
    after_commit = _snapshot(session_factory, committed_run_id)

    changed = {k for k in before_commit if before_commit[k] != after_commit[k]}
    assert changed == {"temporal_first_execution_run_id"}
    for k in before_commit:
        if k != "temporal_first_execution_run_id":
            assert before_commit[k] == after_commit[k]

    rollback_run_id = _new_run_id()
    rollback_workflow_id = _new_workflow_id()
    _make_run(session_factory, rollback_run_id, rollback_workflow_id)
    baseline = _snapshot(session_factory, rollback_run_id)
    assert baseline["temporal_first_execution_run_id"] is None

    with session_factory() as session:
        assert (
            bind_run_execution(session, rollback_run_id, rollback_workflow_id, str(uuid4())) is True
        )
        session.rollback()

    assert _snapshot(session_factory, rollback_run_id) == baseline


def test_stale_identity_map_rejects_conflicting_binding(session_factory):
    run_id = _new_run_id()
    workflow_id = _new_workflow_id()
    _make_run(session_factory, run_id, workflow_id)
    winner_execution_id = str(uuid4())

    stale_session = session_factory()
    try:
        stale_row = stale_session.execute(select(Run).where(Run.id == run_id)).scalar_one()
        assert stale_row.temporal_first_execution_run_id is None

        with session_factory() as other_session:
            assert (
                bind_run_execution(other_session, run_id, workflow_id, winner_execution_id) is True
            )
            other_session.commit()

        winner_snapshot = _snapshot(session_factory, run_id)
        assert stale_row.temporal_first_execution_run_id is None
        with pytest.raises(ValueError, match=re.escape(CONFLICT_MESSAGE)):
            bind_run_execution(stale_session, run_id, workflow_id, str(uuid4()))
    finally:
        stale_session.rollback()
        stale_session.close()

    assert _snapshot(session_factory, run_id) == winner_snapshot
    assert winner_snapshot["temporal_first_execution_run_id"] == winner_execution_id
    assert winner_snapshot["updated_at"] is not None


@pytest.mark.parametrize("same", [False, True])
def test_concurrent_for_update_first_writer_wins(session_factory, engine, same):
    run_id = _new_run_id()
    workflow_id = _new_workflow_id()
    _make_run(session_factory, run_id, workflow_id)
    winner_execution_id = str(uuid4())

    pid_queue: queue.Queue = queue.Queue()
    result_queue: queue.Queue = queue.Queue()
    contender_execution_id = winner_execution_id if same else str(uuid4())

    def contender() -> None:
        engine_c = create_engine(engine.url, future=True)
        try:
            with sessionmaker(bind=engine_c, future=True)() as session:
                session.execute(text("SET LOCAL lock_timeout = '10000ms'"))
                session.execute(text("SET LOCAL statement_timeout = '10000ms'"))
                pid_queue.put(_backend_pid(session))
                try:
                    outcome = bind_run_execution(
                        session, run_id, workflow_id, contender_execution_id
                    )
                except Exception as exc:
                    result_queue.put(("contender", "error", exc))
                else:
                    result_queue.put(("contender", "ok", outcome))
                finally:
                    session.rollback()
        except Exception as exc:
            result_queue.put(("contender", "fatal", exc))
        finally:
            engine_c.dispose()

    holder_session = session_factory()
    contender_thread = threading.Thread(target=contender, daemon=False)
    started = False
    try:
        holder_session.execute(text("SET LOCAL lock_timeout = '10000ms'"))
        holder_session.execute(text("SET LOCAL statement_timeout = '10000ms'"))
        assert bind_run_execution(holder_session, run_id, workflow_id, winner_execution_id) is True

        contender_thread.start()
        started = True
        contender_pid = pid_queue.get(timeout=5)
        assert _wait_for_lock_wait(session_factory, contender_pid, timeout=5.0)
        holder_session.commit()
    finally:
        try:
            holder_session.rollback()
        finally:
            holder_session.close()
        if started:
            contender_thread.join(timeout=12)
            assert not contender_thread.is_alive()

    assert not contender_thread.is_alive()

    kind, payload = None, None
    while not result_queue.empty():
        _, kind, payload = result_queue.get()

    if same:
        assert kind == "ok"
        assert payload is False
    else:
        assert kind == "error"
        assert isinstance(payload, ValueError)
        assert CONFLICT_MESSAGE in str(payload)

    final = _snapshot(session_factory, run_id)
    assert final["temporal_first_execution_run_id"] == winner_execution_id


def test_identical_committed_binding_issues_no_update(session_factory, engine):
    run_id = _new_run_id()
    workflow_id = _new_workflow_id()
    _make_run(session_factory, run_id, workflow_id)
    execution_id = str(uuid4())

    with session_factory() as session:
        assert bind_run_execution(session, run_id, workflow_id, execution_id) is True
        session.commit()

    statements: list = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        with session_factory() as session:
            assert bind_run_execution(session, run_id, workflow_id, execution_id) is False
            session.commit()
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert not any(s.strip().upper().startswith("UPDATE") for s in statements)
