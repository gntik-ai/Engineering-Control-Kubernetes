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
    # 0033 (the chat channel's memory) follows 0032 (queues and budgets), 0031 (the
    # supervisor), 0030 (what a step was
    # given), 0029 (runs in sandboxes),
    # 0028 (subscription accounts), 0027 (the backlog),
    # 0026 (sections of a project) and 0025 (projects as records), which follow
    # NEW-245's 0024 (stored cleanup lists), NEW-234
    # PR6's 0023 (one active request per decomposition lineage), 0022 (backlog
    # decompositions), 0021 (MERGED and gap consolidations) and 0020 (UI blueprints).
    assert (head, parent) == ("0033", "0032")

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

    # 0026: the table of sections, with the routing guide. The console fills it when it
    # starts (tests/test_dashboard_sections.py), so a migrated database has none yet.
    columns = {column["name"] for column in inspect(engine).get_columns("project_sections")}
    assert {"description", "use_when", "avoid_when", "labels", "examples", "flow_key"} <= columns
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM project_sections")) == 0
    # 0033: what the chat channel remembers. Without it the channel starts again from now.
    assert {"chat_state", "chat_deliveries"} <= set(inspect(engine).get_table_names())
    command.downgrade(config, parent)
    assert "chat_state" not in set(inspect(engine).get_table_names())
    # 0032: queues and budgets. A queue that is under way is an order still to be worked
    # on, so a downgrade must not drop it; drafts and finished queues may go.
    assert {"work_queues", "work_queue_entries", "budgets"} <= set(
        inspect(engine).get_table_names()
    )
    with engine.begin() as connection:
        project_id = connection.scalar(text("SELECT id FROM projects WHERE name = 'falcone'"))
        for queue_id, state in (
            ("00000000000000000000000000000009", "draft"),
            ("0000000000000000000000000000000a", "paused"),
        ):
            connection.execute(
                text(
                    "INSERT INTO work_queues (id, project_id, name, state, at_once, created_at, "
                    "updated_at) VALUES (:id, :project, 'Next', :state, 1, CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP)"
                ),
                {"id": queue_id, "project": project_id, "state": state},
            )
    with pytest.raises(RuntimeError, match="queues that are under way"):
        command.downgrade(config, "0031")
    with engine.begin() as connection:
        connection.execute(text("UPDATE work_queues SET state = 'finished'"))
    command.downgrade(config, "0031")
    assert "work_queues" not in set(inspect(engine).get_table_names())
    # 0031: the supervisor's definition, conversation and record of actions, and who
    # proposed an item. An action that waits for the administrator is a decision not taken
    # yet, so a downgrade must not drop it.
    assert {"supervisor_definitions", "supervisor_messages", "supervisor_actions"} <= set(
        inspect(engine).get_table_names()
    )
    assert "proposed_by" in {c["name"] for c in inspect(engine).get_columns("backlog_items")}
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO supervisor_actions (id, tool, kind, arguments, outcome, "
                "created_at, updated_at) VALUES ('00000000000000000000000000000008', "
                "'route_item', 'plan', '{}', 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    with pytest.raises(RuntimeError, match="wait for the administrator"):
        command.downgrade(config, "0030")
    with engine.begin() as connection:
        connection.execute(text("UPDATE supervisor_actions SET outcome = 'rejected'"))
    command.downgrade(config, "0030")
    assert "supervisor_actions" not in set(inspect(engine).get_table_names())
    assert "proposed_by" not in {c["name"] for c in inspect(engine).get_columns("backlog_items")}
    # 0030: one nullable column, added and dropped without touching a row.
    assert "input" in {c["name"] for c in inspect(engine).get_columns("sandbox_steps")}
    command.downgrade(config, "0029")
    assert "input" not in {c["name"] for c in inspect(engine).get_columns("sandbox_steps")}
    # 0029: runs in sandboxes. A run that is not finished has a sandbox in the cluster, so
    # a downgrade must not drop its record; finished runs are history and may go.
    assert {"sandbox_runs", "sandbox_steps"} <= set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        for run_id, state in (
            ("00000000000000000000000000000006", "succeeded"),
            ("00000000000000000000000000000007", "running"),
        ):
            connection.execute(
                text(
                    "INSERT INTO sandbox_runs (id, kind, state, title, cancel_requested, data, "
                    "created_at, updated_at) VALUES (:id, 'account_check', :state, 'Try it', "
                    "FALSE, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"id": run_id, "state": state},
            )
    with pytest.raises(RuntimeError, match="refuses to drop runs that are not finished"):
        command.downgrade(config, "0028")
    with engine.begin() as connection:
        connection.execute(text("UPDATE sandbox_runs SET state = 'cancelled'"))
    command.downgrade(config, "0028")
    assert "sandbox_runs" not in inspect(engine).get_table_names()
    assert "sandbox_steps" not in inspect(engine).get_table_names()
    # 0028: subscription accounts. An account that is not removed still has its sign-in
    # stored in the cluster, so a downgrade must not drop its record.
    assert "provider_accounts" in inspect(engine).get_table_names()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO provider_accounts (id, name, provider, state, paused, "
                "max_concurrent, credential_generation, created_at, updated_at) VALUES "
                "('00000000000000000000000000000005', 'Claude one', 'claude', 'signed_out', "
                "FALSE, 2, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    with pytest.raises(RuntimeError, match="refuses to drop accounts that are not removed"):
        command.downgrade(config, "0027")
    with engine.begin() as connection:
        connection.execute(text("UPDATE provider_accounts SET archived_at = CURRENT_TIMESTAMP"))
    command.downgrade(config, "0027")
    assert "provider_accounts" not in inspect(engine).get_table_names()
    # 0027: the backlog. An item the administrator wrote must not be lost by a downgrade;
    # one that only mirrors a GitHub issue can be imported again.
    assert {"backlog_items", "backlog_syncs"} <= set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        for item_id, source, routed_by in (
            ("00000000000000000000000000000003", "github", None),
            ("00000000000000000000000000000004", "manual", "administrator"),
        ):
            connection.execute(
                text(
                    "INSERT INTO backlog_items (id, project_id, title, source, state, routed_by, "
                    "issue_labels, created_at, updated_at) SELECT :id, id, 'An item', :source, "
                    "'proposed', :routed_by, '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
                    "FROM projects WHERE name = 'made-here'"
                ),
                {"id": item_id, "source": source, "routed_by": routed_by},
            )
    with pytest.raises(RuntimeError, match="refuses to drop backlog items the administrator"):
        command.downgrade(config, "0026")
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM backlog_items WHERE source = 'manual'"))
    command.downgrade(config, "0026")
    assert "backlog_items" not in inspect(engine).get_table_names()
    assert "backlog_syncs" not in inspect(engine).get_table_names()
    # A section the administrator added must not be lost by a downgrade.
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO project_sections (id, project_id, key, name, description, "
                "use_when, avoid_when, labels, examples, kind, position, created_at, "
                "updated_at) SELECT '00000000000000000000000000000002', id, 'release-notes', "
                "'Release notes', 'What changed.', '', '', '[]', '[]', 'custom', 6, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP FROM projects WHERE name = 'made-here'"
            )
        )
    with pytest.raises(RuntimeError, match="refuses to drop sections the administrator added"):
        command.downgrade(config, "0025")
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM project_sections WHERE kind = 'custom'"))
    command.downgrade(config, "0025")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0025"
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
