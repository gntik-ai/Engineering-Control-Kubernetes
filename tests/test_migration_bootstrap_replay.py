from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from engineering_control.db.models import ACTIVE_OPERATION_STATUSES, OperationRequest


def test_fresh_bootstrap_reaches_head_and_latest_revision_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "fresh-control-plane.sqlite3"
    database_url = f"sqlite+pysqlite:///{database}"
    monkeypatch.setenv("ENGINEERING_DATABASE_URL", database_url)
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    head = scripts.get_current_head()
    assert head is not None
    head_revision = scripts.get_revision(head)
    assert isinstance(head_revision.down_revision, str)
    parent = head_revision.down_revision
    # Merged with main: 0019 (change requests) follows the release's 0018.
    assert (head, parent) == ("0019", "0018")

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head
    active_owner_index = next(
        index
        for index in inspect(engine).get_indexes("operation_requests")
        if index["name"] == "uq_operation_requests_active_target"
    )
    predicate = str(active_owner_index["dialect_options"]["sqlite_where"])
    assert all(status in predicate for status in ACTIVE_OPERATION_STATUSES)
    assert "accepted_operation_id" in {
        column["name"] for column in inspect(engine).get_columns("action_candidates")
    }
    assert "gateway_ingestion_key" in {
        column["name"] for column in inspect(engine).get_columns("dashboard_events")
    }
    assert "temporal_first_execution_run_id" in {
        column["name"] for column in inspect(engine).get_columns("runs")
    }

    command.downgrade(config, parent)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == parent
    assert "change_requests" not in inspect(engine).get_table_names()
    assert "temporal_first_execution_run_id" in {
        column["name"] for column in inspect(engine).get_columns("runs")
    }
    command.downgrade(config, "0017")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0017"
    # Only0018 is downgraded: ownership, receipts and0017 ingestion remain intact.
    assert "uq_operation_requests_active_target" in {
        index["name"] for index in inspect(engine).get_indexes("operation_requests")
    }
    assert "accepted_operation_id" in {
        column["name"] for column in inspect(engine).get_columns("action_candidates")
    }
    assert "gateway_ingestion_key" in {
        column["name"] for column in inspect(engine).get_columns("dashboard_events")
    }
    assert "temporal_first_execution_run_id" not in {
        column["name"] for column in inspect(engine).get_columns("runs")
    }

    # Preserve the previous0017 round-trip coverage independently of the new head.
    command.downgrade(config, "0016")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0016"
    assert "gateway_ingestion_key" not in {
        column["name"] for column in inspect(engine).get_columns("dashboard_events")
    }

    command.upgrade(config, head)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head
    assert "uq_operation_requests_active_target" in {
        index["name"] for index in inspect(engine).get_indexes("operation_requests")
    }
    assert "accepted_operation_id" in {
        column["name"] for column in inspect(engine).get_columns("action_candidates")
    }
    assert "gateway_ingestion_key" in {
        column["name"] for column in inspect(engine).get_columns("dashboard_events")
    }
    assert "temporal_first_execution_run_id" in {
        column["name"] for column in inspect(engine).get_columns("runs")
    }
    engine.dispose()


def test_session_revocation_downgrade_invalidates_all_outstanding_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'session-downgrade.sqlite3'}"
    monkeypatch.setenv("ENGINEERING_DATABASE_URL", database_url)
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(config, "0014")
    engine = create_engine(database_url)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO dashboard_authentication "
                "(id, password_hash, password_version, created_at, password_changed_at) "
                "VALUES (1, :password_hash, 7, :now, :now)"
            ),
            {"password_hash": "synthetic-non-secret-hash", "now": now},
        )

    command.downgrade(config, "0013")
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT password_version FROM dashboard_authentication WHERE id = 1")
            )
            == 8
        )
    assert not inspect(engine).has_table("dashboard_session_revocations")
    command.upgrade(config, "0014")
    assert inspect(engine).has_table("dashboard_session_revocations")
    engine.dispose()


def test_active_owner_migration_refuses_ambiguous_existing_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'active-owner-duplicates.sqlite3'}"
    monkeypatch.setenv("ENGINEERING_DATABASE_URL", database_url)
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(config, "0014")
    engine = create_engine(database_url)
    # Revision 0001 bootstraps current model metadata on a fresh fixture. Remove
    # the future index to reproduce an actual predecessor (0014) installation.
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX uq_operation_requests_active_target"))
    now = datetime.now(UTC)
    with Session(engine) as session:
        with session.begin():
            for ordinal in (1, 2):
                session.add(
                    OperationRequest(
                        action="solve_issue",
                        project="falcone",
                        target_type="github_issue",
                        target_id="gntik-ai/falcone#42",
                        payload_json={"ordinal": ordinal},
                        source_channel="cli",
                        operator_class="synthetic-fixture",
                        idempotency_key=f"{ordinal:064x}",
                        binding_hash=f"{ordinal + 2:064x}",
                        workflow_id=None,
                        temporal_run_id=None,
                        status="PENDING",
                        safe_error=None,
                        created_at=now,
                        confirmed_at=now,
                        started_at=None,
                        completed_at=None,
                    )
                )
    with pytest.raises(RuntimeError, match="refuses active-owner duplicates"):
        command.upgrade(config, "0015")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0014"
    assert "uq_operation_requests_active_target" not in {
        index["name"] for index in inspect(engine).get_indexes("operation_requests")
    }
    engine.dispose()


def test_active_owner_migration_rejects_same_named_full_unique_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'active-owner-wrong-index.sqlite3'}"
    monkeypatch.setenv("ENGINEERING_DATABASE_URL", database_url)
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(config, "0014")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX uq_operation_requests_active_target"))
        connection.execute(
            text(
                "CREATE UNIQUE INDEX uq_operation_requests_active_target "
                "ON operation_requests (action, target_id)"
            )
        )
    with pytest.raises(RuntimeError, match="incompatible active-owner predicate"):
        command.upgrade(config, "0015")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0014"
    engine.dispose()


def test_active_owner_migration_rejects_negated_partial_predicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'active-owner-negated-index.sqlite3'}"
    monkeypatch.setenv("ENGINEERING_DATABASE_URL", database_url)
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(config, "0014")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX uq_operation_requests_active_target"))
        connection.execute(
            text(
                "CREATE UNIQUE INDEX uq_operation_requests_active_target "
                "ON operation_requests (action, target_id) "
                "WHERE status NOT IN ('PENDING', 'DISPATCHING', 'STARTED', 'RUNNING')"
            )
        )
    with pytest.raises(RuntimeError, match="incompatible active-owner predicate"):
        command.upgrade(config, "0015")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0014"
    engine.dispose()
