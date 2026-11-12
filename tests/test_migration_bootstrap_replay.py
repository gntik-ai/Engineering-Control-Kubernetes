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
from engineering_control.projects import PROJECTS


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
    # 0026 (sections of a project) follows 0025 (projects as records), which follows
    # NEW-245's 0024 (stored cleanup lists), NEW-234
    # PR6's 0023 (one active request per decomposition lineage), 0022 (backlog
    # decompositions), 0021 (MERGED and gap consolidations) and 0020 (UI blueprints).
    assert (head, parent) == ("0026", "0025")

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

    assert "backlog_decompositions" in inspect(engine).get_table_names()
    assert "uq_change_requests_active_decomposition_lineage" in {
        index["name"] for index in inspect(engine).get_indexes("change_requests")
    }
    assert "cleanup_run_items" in inspect(engine).get_table_names()
    # 0025: the registry's projects are copied in, once, as records.
    with engine.connect() as connection:
        copied = connection.execute(
            text("SELECT name, source_repository, origin FROM projects ORDER BY created_at, name")
        ).all()
    assert {row[0] for row in copied} == set(PROJECTS)
    assert all(row[2] == "registry" for row in copied)
    assert ("falcone", "gntik-ai/falcone", "registry") in [tuple(row) for row in copied]
    # A project created in the console must not be lost by a downgrade.
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects (id, name, display_name, source_repository, "
                "deployment_repository, default_branch, origin, created_at, updated_at) "
                "VALUES ('00000000000000000000000000000001', 'made-here', 'Made here', "
                "'gntik-ai/made-here', 'gntik-ai/made-here', 'main', 'console', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    # 0026: every project has the six core sections; the registry's keep their three flows.
    def sections_by_project() -> dict[str, list[tuple[str, str, str | None]]]:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT p.name, s.key, s.kind, s.flow_key FROM project_sections s "
                    "JOIN projects p ON p.id = s.project_id ORDER BY p.name, s.position"
                )
            ).all()
        found: dict[str, list[tuple[str, str, str | None]]] = {}
        for name, key, kind, flow in rows:
            found.setdefault(name, []).append((key, kind, flow))
        return found

    by_project = sections_by_project()
    assert set(by_project) == set(PROJECTS)  # the console's project was added after 0026 ran
    assert by_project["falcone"] == [
        ("code-audit", "core", "audit-code"),
        ("ui-audit", "core", None),
        ("ux-audit", "core", None),
        ("code-clean", "core", None),
        ("enhancement", "core", "discover-enhancements"),
        ("issues", "core", "deliver-issue"),
    ]
    # Run 0026 again with the console's project present: it gets the same six, without a flow.
    command.downgrade(config, parent)
    assert "project_sections" not in inspect(engine).get_table_names()
    command.upgrade(config, head)
    by_project = sections_by_project()
    assert set(by_project) == {*PROJECTS, "made-here"}
    assert [key for key, _kind, _flow in by_project["made-here"]] == [
        key for key, _kind, _flow in by_project["falcone"]
    ]
    assert [flow for _key, _kind, flow in by_project["made-here"]] == [None] * 6
    # A section the administrator added must not be lost by a downgrade.
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO project_sections (id, project_id, key, name, description, kind, "
                "position, created_at, updated_at) SELECT '00000000000000000000000000000002', "
                "id, 'release-notes', 'Release notes', '', 'custom', 6, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP FROM projects WHERE name = 'made-here'"
            )
        )
    with pytest.raises(RuntimeError, match="refuses to drop sections the administrator added"):
        command.downgrade(config, parent)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM project_sections WHERE kind = 'custom'"))
    command.downgrade(config, parent)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == parent
    assert "project_sections" not in inspect(engine).get_table_names()
    with pytest.raises(RuntimeError, match="refuses to drop projects created in the console"):
        command.downgrade(config, "0024")
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM projects WHERE origin = 'console'"))
    command.downgrade(config, "0024")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0024"
    assert "projects" not in inspect(engine).get_table_names()
    assert "cleanup_run_items" in inspect(engine).get_table_names()
    command.downgrade(config, "0023")
    assert "cleanup_run_items" not in inspect(engine).get_table_names()
    command.downgrade(config, "0022")
    assert "uq_change_requests_active_decomposition_lineage" not in {
        index["name"] for index in inspect(engine).get_indexes("change_requests")
    }
    command.downgrade(config, "0021")
    assert "backlog_decompositions" not in inspect(engine).get_table_names()
    assert "decomposition_lineage" not in {
        column["name"] for column in inspect(engine).get_columns("change_requests")
    }
    assert "ui_gap_consolidations" in inspect(engine).get_table_names()
    command.downgrade(config, "0020")
    assert "ui_gap_consolidations" not in inspect(engine).get_table_names()
    assert "merged_into_id" not in {
        column["name"] for column in inspect(engine).get_columns("change_requests")
    }
    assert "ui_blueprints" in inspect(engine).get_table_names()
    command.downgrade(config, "0019")
    assert "ui_blueprints" not in inspect(engine).get_table_names()
    assert "ui_context_json" not in {
        column["name"] for column in inspect(engine).get_columns("change_requests")
    }
    command.downgrade(config, "0018")
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
