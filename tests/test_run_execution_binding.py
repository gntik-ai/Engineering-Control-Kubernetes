"""Tests for the H001 run execution binding primitive (source-only delta)."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from engineering_control.db.models import Base, Run
from engineering_control.db.run_binding import bind_run_execution

NIL = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as s:
            yield s
    finally:
        engine.dispose()


def _make_run(session, workflow_id="wf-1"):
    run = Run(
        id=uuid4(),
        workflow_id=workflow_id,
        run_type="reconcile",
        status="RUNNING",
        project="platform",
        updated_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    session.add(run)
    session.flush()
    return run


def test_first_binding_succeeds(session):
    run = _make_run(session)
    before = dict(session.execute(sa.select(Run.__table__)).mappings().one())
    exec_id = str(uuid4())
    assert bind_run_execution(session, run.id, "wf-1", exec_id) is True
    assert run.temporal_first_execution_run_id == exec_id
    after = dict(session.execute(sa.select(Run.__table__)).mappings().one())
    assert after.pop("temporal_first_execution_run_id") == exec_id
    assert before.pop("temporal_first_execution_run_id") is None
    assert after == before
    assert run.status == "RUNNING"


def test_identical_binding_is_noop(session):
    run = _make_run(session)
    exec_id = str(uuid4())
    bind_run_execution(session, run.id, "wf-1", exec_id)
    updates = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("UPDATE"):
            updates.append(True)

    sa.event.listen(session.bind, "before_cursor_execute", capture)
    try:
        assert bind_run_execution(session, run.id, "wf-1", exec_id) is False
        assert not updates
    finally:
        sa.event.remove(session.bind, "before_cursor_execute", capture)


def test_conflicting_binding_rejected(session):
    run = _make_run(session)
    bind_run_execution(session, run.id, "wf-1", str(uuid4()))
    with pytest.raises(ValueError):
        bind_run_execution(session, run.id, "wf-1", str(uuid4()))


def test_wrong_workflow_id_rejected(session):
    run = _make_run(session)
    with pytest.raises(ValueError):
        bind_run_execution(session, run.id, "wf-other", str(uuid4()))


def test_missing_run_rejected(session):
    with pytest.raises(ValueError):
        bind_run_execution(session, uuid4(), "wf-1", str(uuid4()))


@pytest.mark.parametrize(
    "value",
    [
        NIL,
        "not-a-uuid",
        "01234567-89AB-4CDE-8FAB-0123456789ABC",
        " 3c64ab90-e1d0-4916-aa98-a46d03d4b1de",  # fixed: xdist workers must collect equal ids
        None,
        1,
        "x" * 36,
    ],
)
def test_malformed_execution_id_rejected(session, value):
    run = _make_run(session)
    with pytest.raises(ValueError):
        bind_run_execution(session, run.id, "wf-1", value)


def test_empty_overlength_control_workflow_id_rejected(session):
    run = _make_run(session)
    for wf in ["", "x" * 241, "bad\x00id", "bad\x85id", "bad\u2028id", None, 1]:
        with pytest.raises(ValueError):
            bind_run_execution(session, run.id, wf, str(uuid4()))


def test_duplicate_workflow_id_distinct_rows_not_crossbound(session):
    run_a = _make_run(session, workflow_id="wf-dup")
    run_b = _make_run(session, workflow_id="wf-dup")
    exec_a, exec_b = str(uuid4()), str(uuid4())
    bind_run_execution(session, run_a.id, "wf-dup", exec_a)
    bind_run_execution(session, run_b.id, "wf-dup", exec_b)
    assert run_a.temporal_first_execution_run_id == exec_a
    assert run_b.temporal_first_execution_run_id == exec_b


def test_rollback_undoes_binding(session):
    run = _make_run(session)
    session.commit()
    run_id = run.id
    bind_run_execution(session, run.id, "wf-1", str(uuid4()))
    session.rollback()
    preserved = session.get(Run, run_id)
    assert preserved is not None and preserved.temporal_first_execution_run_id is None


@pytest.mark.parametrize("value", [None, NIL, UUID(NIL), 1])
def test_invalid_application_id_rejected(session, value):
    with pytest.raises(ValueError, match="application_run_id is invalid"):
        bind_run_execution(session, value, "wf-1", str(uuid4()))


@pytest.mark.parametrize("pending", ["new", "dirty", "deleted"])
def test_pending_session_changes_are_not_flushed(session, pending):
    run = _make_run(session)
    run_id = run.id
    if pending == "new":
        session.add(
            Run(workflow_id="other", run_type="cleanup", status="QUEUED", project="platform")
        )
    elif pending == "dirty":
        run.status = "FAILED"
    else:
        session.delete(run)
    writes = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(True)

    sa.event.listen(session.bind, "before_cursor_execute", capture)
    try:
        with pytest.raises(ValueError, match="clean session"):
            bind_run_execution(session, run_id, "wf-1", str(uuid4()))
        assert not writes
    finally:
        sa.event.remove(session.bind, "before_cursor_execute", capture)
        session.rollback()


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0018_run_execution_binding.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0018", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration_engine():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE runs (id TEXT PRIMARY KEY, workflow_id TEXT)"))
    try:
        yield engine
    finally:
        engine.dispose()


def _run_with_op(engine, func):
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        module = _load_migration()
        module.op = Operations(ctx)
        func(module)


def test_migration_upgrade_adds_column_and_is_idempotent(migration_engine):
    with migration_engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO runs VALUES ('historical', 'old-workflow')"))
    _run_with_op(migration_engine, lambda m: m.upgrade())
    _run_with_op(migration_engine, lambda m: m.upgrade())
    inspector = sa.inspect(migration_engine)
    cols = {c["name"]: c for c in inspector.get_columns("runs")}
    assert "temporal_first_execution_run_id" in cols
    assert cols["temporal_first_execution_run_id"]["nullable"] is True
    with migration_engine.connect() as conn:
        assert conn.execute(sa.text("SELECT temporal_first_execution_run_id FROM runs")).one() == (
            None,
        )


def test_migration_incompatible_column_fails(migration_engine):
    with migration_engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE runs ADD COLUMN temporal_first_execution_run_id INTEGER"))
    with pytest.raises(RuntimeError):
        _run_with_op(migration_engine, lambda m: m.upgrade())


def test_migration_unexpected_default_fails(migration_engine):
    with migration_engine.begin() as conn:
        conn.execute(
            sa.text(
                "ALTER TABLE runs ADD COLUMN temporal_first_execution_run_id "
                "VARCHAR(240) DEFAULT 'unexpected'"
            )
        )
    with pytest.raises(RuntimeError, match="incompatible"):
        _run_with_op(migration_engine, lambda m: m.upgrade())


def test_migration_downgrade_empty_ok(migration_engine):
    _run_with_op(migration_engine, lambda m: m.upgrade())
    _run_with_op(migration_engine, lambda m: m.downgrade())
    inspector = sa.inspect(migration_engine)
    cols = {c["name"] for c in inspector.get_columns("runs")}
    assert "temporal_first_execution_run_id" not in cols


def test_migration_downgrade_populated_refuses(migration_engine):
    _run_with_op(migration_engine, lambda m: m.upgrade())
    with migration_engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO runs (id, workflow_id) VALUES ('r1', 'wf')"))
        conn.execute(
            sa.text("UPDATE runs SET temporal_first_execution_run_id = :v WHERE id = 'r1'"),
            {"v": str(uuid4())},
        )
    with pytest.raises(RuntimeError):
        _run_with_op(migration_engine, lambda m: m.downgrade())
    with migration_engine.connect() as conn:
        assert (
            conn.scalar(sa.text("SELECT temporal_first_execution_run_id FROM runs WHERE id = 'r1'"))
            is not None
        )
