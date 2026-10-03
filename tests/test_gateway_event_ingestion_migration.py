"""Exact predecessor chain in a separate interpreter; disposable DBs only."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, insert, inspect, text
from sqlalchemy.engine import make_url

URL = "postgresql+psycopg://engineering_ci@127.0.0.1:5432/engineering_control_ci"


@pytest.fixture(params=["sqlite", "postgresql"])
def migration_database(request, tmp_path, monkeypatch):
    if not Path("/predecessor/migrations").is_dir():
        pytest.skip("requires the isolated exact predecessor mount (/predecessor)")
    database = "gateway_ingestion_" + uuid4().hex
    admin = None
    if request.param == "postgresql":
        if "ENGINEERING_TEST_POSTGRES_URL" not in os.environ:
            pytest.skip("requires explicitly supplied isolated PostgreSQL fixture")
        assert os.environ["ENGINEERING_TEST_POSTGRES_URL"] == URL
        admin = create_engine(make_url(URL).set(database="postgres"), isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            assert connection.scalar(text("SELECT current_user")) == "engineering_ci"
            connection.execute(text(f'CREATE DATABASE "{database}"'))
        url = make_url(URL).set(database=database).render_as_string(False)
    else:
        url = f"sqlite+pysqlite:///{tmp_path / 'gateway-events.db'}"
    monkeypatch.setenv("ENGINEERING_DATABASE_URL", url)
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    engine = create_engine(url)
    try:
        prepared = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "alembic",
                "-c",
                "/predecessor/alembic.ini",
                "upgrade",
                "0016",
            ],
            cwd="/predecessor",
            capture_output=True,
            timeout=45,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(tmp_path / "fixture-home"),
                "PYTHONPATH": "/predecessor/src",
                "PYTHONDONTWRITEBYTECODE": "1",
                "ENGINEERING_DATABASE_URL": url,
            },
        )
        assert prepared.returncode == 0, (
            "exact predecessor migration failed; no live state involved"
        )
        assert "gateway_ingestion_key" not in {
            row["name"] for row in inspect(engine).get_columns("dashboard_events")
        }
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0016"
        yield config, engine
    finally:
        engine.dispose()
        if admin is not None:
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{database}"'))
            admin.dispose()


def fingerprint(engine):
    with engine.connect() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                text("SELECT * FROM dashboard_events ORDER BY id")
            ).mappings()
        ]
    for row in rows:
        row.pop("gateway_ingestion_key", None)
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def test_exact0016_upgrade_preserves_historical_duplicate_events(migration_database):
    config, engine = migration_database
    table = Table("dashboard_events", MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        for _ in range(2):
            connection.execute(
                insert(table).values(
                    occurred_at=datetime.now(UTC),
                    level="INFO",
                    event_type="telegram.command_received",
                    source="hermes",
                    message="historical synthetic event",
                    metadata_json={"synthetic": True},
                )
            )
    before = fingerprint(engine)
    command.upgrade(config, "0017")
    assert fingerprint(engine) == before
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM dashboard_events")) == 2
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM dashboard_events WHERE gateway_ingestion_key IS NOT NULL"
                )
            )
            == 0
        )
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0017"
    command.upgrade(config, "0017")
    assert fingerprint(engine) == before
    command.downgrade(config, "0016")
    assert fingerprint(engine) == before


def test_downgrade_refuses_loss_of_new_receipt(migration_database):
    config, engine = migration_database
    command.upgrade(config, "0017")
    table = Table("dashboard_events", MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(
            insert(table).values(
                occurred_at=datetime.now(UTC),
                level="INFO",
                event_type="telegram.command_received",
                source="hermes",
                message="fixture receipt",
                metadata_json={"synthetic": True},
                gateway_ingestion_key="a" * 64,
            )
        )
    before = fingerprint(engine)
    with pytest.raises(RuntimeError, match="refuses to discard"):
        command.downgrade(config, "0016")
    assert fingerprint(engine) == before
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0017"
        assert (
            connection.scalar(text("SELECT gateway_ingestion_key FROM dashboard_events"))
            == "a" * 64
        )


def test_incompatible_partial_definition_fails_closed(migration_database):
    config, engine = migration_database
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE dashboard_events ADD COLUMN gateway_ingestion_key VARCHAR(12)")
        )
    before = fingerprint(engine)
    with pytest.raises(RuntimeError, match="incompatible"):
        command.upgrade(config, "0017")
    assert fingerprint(engine) == before
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0016"
