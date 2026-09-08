from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, inspect, text
from sqlalchemy.engine import make_url

from engineering_control.db.models import ActionCandidate, OperationRequest

URL = "postgresql+psycopg://engineering_ci@127.0.0.1:5432/engineering_control_ci"


@pytest.fixture(params=["sqlite", "postgresql"])
def migration_database(request, tmp_path, monkeypatch):
    admin = None
    database = "receipt_migration_" + uuid4().hex
    if request.param == "sqlite":
        url = f"sqlite+pysqlite:///{tmp_path / 'receipt.db'}"
    else:
        if not os.environ.get("ENGINEERING_TEST_POSTGRES_URL"):
            pytest.skip("requires explicitly supplied isolated PostgreSQL fixture")
        assert os.environ["ENGINEERING_TEST_POSTGRES_URL"] == URL
        admin = create_engine(make_url(URL).set(database="postgres"), isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            assert connection.scalar(text("SELECT current_user")) == "engineering_ci"
            assert (
                connection.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": database}
                )
                is None
            )
            connection.execute(text(f'CREATE DATABASE "{database}"'))
        url = make_url(URL).set(database=database).render_as_string(False)
    monkeypatch.setenv("ENGINEERING_DATABASE_URL", url)
    root = Path(__file__).resolve().parents[1]
    # No fileConfig(): a fixture migration must not disable existing app
    # loggers or replace the embedding test runner's logging configuration.
    config = Config()
    config.set_main_option("script_location", str(root / "migrations"))
    engine = create_engine(url)
    try:
        yield config, engine
    finally:
        engine.dispose()
        if admin is not None:
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{database}"'))
            admin.dispose()


def fingerprint(engine):
    result = {}
    with engine.connect() as connection:
        for table, statement in {
            "action_candidates": text("SELECT * FROM action_candidates ORDER BY id"),
            "operation_requests": text("SELECT * FROM operation_requests ORDER BY id"),
        }.items():
            rows = [dict(row) for row in connection.execute(statement).mappings()]
            for row in rows:
                row.pop("accepted_operation_id", None)
            result[table] = hashlib.sha256(
                json.dumps(rows, sort_keys=True, default=str).encode()
            ).hexdigest()
    return result


def test_receipt_migration_preserves_rows_and_refuses_receipt_loss(migration_database):
    config, engine = migration_database
    command.upgrade(config, "0016")
    command.downgrade(config, "0015")
    assert "accepted_operation_id" not in {
        c["name"] for c in inspect(engine).get_columns("action_candidates")
    }
    # Self-contained0015 schema produced by a safe empty-receipt downgrade.
    # Exact released-predecessor upgrade is a separate release acceptance fixture.
    now = datetime.now(UTC)
    common = dict(
        action="refresh_connections",
        project=None,
        target_type="connections",
        target_id="all",
        payload_json={"action": "refresh_connections", "scope": "all"},
        binding_hash="a" * 64,
        created_at=now,
    )
    with engine.begin() as connection:
        connection.execute(
            insert(OperationRequest.__table__).values(
                **common,
                id=UUID(int=101),
                source_channel="dashboard",
                operator_class="isolated-fixture",
                idempotency_key="a" * 64,
                status="COMPLETED",
                confirmed_at=now,
                completed_at=now,
            )
        )
        connection.execute(
            insert(ActionCandidate.__table__).values(
                **common,
                id=UUID(int=102),
                capability_status="AVAILABLE",
                blockers_json=[],
                session_binding_hash="b" * 64,
                expires_at=now,
                used_at=now,
            )
        )
    original = fingerprint(engine)
    command.upgrade(config, "0016")
    assert fingerprint(engine) == original
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0016"
        assert (
            connection.scalar(text("SELECT accepted_operation_id FROM action_candidates")) is None
        )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE action_candidates SET accepted_operation_id = "
                "(SELECT id FROM operation_requests LIMIT 1)"
            )
        )
    with pytest.raises(RuntimeError, match="refuses to discard"):
        command.downgrade(config, "0015")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0016"
        assert (
            connection.scalar(text("SELECT accepted_operation_id FROM action_candidates"))
            is not None
        )
    # Clear only this test's synthetic receipt; no real history is in this DB.
    with engine.begin() as connection:
        connection.execute(text("UPDATE action_candidates SET accepted_operation_id = NULL"))
    command.downgrade(config, "0015")
    assert fingerprint(engine) == original
    command.upgrade(config, "0016")
    assert fingerprint(engine) == original


def test_fresh_model_bootstrap_and_exact_migration_chain_are_compatible(migration_database):
    config, engine = migration_database
    command.upgrade(config, "0016")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0016"
    column = next(
        c
        for c in inspect(engine).get_columns("action_candidates")
        if c["name"] == "accepted_operation_id"
    )
    assert column["nullable"] is True


def test_migration_preserves_application_logging(migration_database, caplog):
    config, _engine = migration_database
    logger = logging.getLogger("engineering_control.dashboard.app")
    caplog.set_level(logging.INFO, logger=logger.name)
    assert logger.disabled is False
    logger.info("synthetic migration logging before")
    command.upgrade(config, "0016")
    assert logger.disabled is False
    logger.info("synthetic migration logging after")
    assert "synthetic migration logging before" in caplog.text
    assert "synthetic migration logging after" in caplog.text
