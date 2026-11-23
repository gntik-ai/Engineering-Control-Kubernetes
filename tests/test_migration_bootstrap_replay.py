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
    # 0044 (evaluations of another flow's runs) follows
    # 0043 (agents and skills), which follows 0042 (accounts with an API key), which follows
    # 0041 (a project's own commands), which follows
    # 0040 (tasks managed from the console), 0039 (how hard the supervisor's
    # model reasons), 0038 (a stored test identity), 0037 (triggers, where an
    # application runs),
    # 0036 (flows as documents, usage, evaluations), 0035 (paused projects),
    # 0034 (items with their own text, incidents, cut-over), 0033 (the chat
    # channel's memory), 0032 (queues and budgets), 0031 (the
    # supervisor), 0030 (what a step was
    # given), 0029 (runs in sandboxes),
    # 0028 (subscription accounts), 0027 (the backlog),
    # 0026 (sections of a project) and 0025 (projects as records), which follow
    # NEW-245's 0024 (stored cleanup lists), NEW-234
    # PR6's 0023 (one active request per decomposition lineage), 0022 (backlog
    # decompositions), 0021 (MERGED and gap consolidations) and 0020 (UI blueprints).
    # 0046 (batches of questions to a model) follows 0045 (summaries of a project's code).
    # 0047 (the day a backlog item was done) follows it, and 0048 (deleted flows of the
    # platform's own) follows that. 0049 adds the workspace records and default.
    # 0070 keeps a daily measurement of how each project's planning hangs together.
    # 0071 records who made each planning link and why, and the primary parent.
    # 0072 keeps the runs that ask about issues without a User Story, and the answers.
    # 0073 keeps one kind of run and answer for every choice of a parent.
    # 0074 keeps an inventory of each project's documentation and its changes.
    # 0075 keeps the pairs of planning records the administrator judges once.
    assert (head, parent) == ("0075", "0074")

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head
    assert {"project_id", "day", "measured_at", "numbers"} == {
        column["name"] for column in inspect(engine).get_columns("planning_health")
    }
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
    # 0049: a fresh database uses the same default as an upgraded installation.
    planning_tables = {
        "project_settings",
        "project_epics",
        "project_user_stories",
        "project_story_epics",
        "project_issue_stories",
    }
    assert planning_tables <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT name FROM projects "
                    "JOIN project_settings ON default_project_id = projects.id"
                )
            )
            == "falcone"
        )
    assert "issues_synced_at" in {
        c["name"] for c in inspect(engine).get_columns("workspace_imports")
    }
    for table in ("project_epics", "project_user_stories"):
        assert "closed_at" in {c["name"] for c in inspect(engine).get_columns(table)}
    assert "story_issue_coverage" in inspect(engine).get_table_names()
    assert {"story_revision", "issue_versions"} <= {
        c["name"] for c in inspect(engine).get_columns("story_issue_coverage")
    }
    assert "project_flows" in inspect(engine).get_table_names()
    assert "project_review_budgets" in inspect(engine).get_table_names()
    assert {"review_wiki_pages", "review_evidence_jobs"} <= set(inspect(engine).get_table_names())
    assert {"model_catalog", "catalog_updated_at"} <= {
        c["name"] for c in inspect(engine).get_columns("provider_accounts")
    }
    assert "execution_type" in {
        c["name"] for c in inspect(engine).get_columns("workspace_candidates")
    }
    assert "supervisor_advice" in inspect(engine).get_table_names()
    assert "planning_lifecycle_receipts" in inspect(engine).get_table_names()
    assert "confirmation_token" in {
        c["name"] for c in inspect(engine).get_columns("workspace_actions")
    }
    links = {index["name"] for index in inspect(engine).get_indexes("planning_links")}
    assert {"ix_planning_links_child", "uq_planning_links_active"} <= links
    documents = {"doc_folders", "doc_scans", "doc_files", "doc_changes"}
    assert "gold_pairs" in inspect(engine).get_table_names()
    command.downgrade(config, parent)
    assert "gold_pairs" not in inspect(engine).get_table_names()
    assert documents <= set(inspect(engine).get_table_names())
    command.downgrade(config, "0073")
    assert not documents & set(inspect(engine).get_table_names())
    assert {"choice_runs", "choice_reviews"} <= set(inspect(engine).get_table_names())
    assert not {"orphan_runs", "orphan_reviews"} & set(inspect(engine).get_table_names())
    command.downgrade(config, "0072")
    assert {"orphan_runs", "orphan_reviews"} <= set(inspect(engine).get_table_names())
    assert not {"choice_runs", "choice_reviews"} & set(inspect(engine).get_table_names())
    command.downgrade(config, "0071")
    assert not {"orphan_runs", "orphan_reviews"} & set(inspect(engine).get_table_names())
    assert "planning_links" in inspect(engine).get_table_names()
    command.downgrade(config, "0070")
    assert "planning_links" not in inspect(engine).get_table_names()
    assert "planning_health" in inspect(engine).get_table_names()
    command.downgrade(config, "0069")
    assert "planning_health" not in inspect(engine).get_table_names()
    assert "planning_lifecycle_receipts" in inspect(engine).get_table_names()
    command.downgrade(config, "0068")
    assert "planning_lifecycle_receipts" not in inspect(engine).get_table_names()
    assert "confirmation_token" not in {
        c["name"] for c in inspect(engine).get_columns("workspace_actions")
    }
    command.downgrade(config, "0067")
    assert "supervisor_advice" not in inspect(engine).get_table_names()
    command.downgrade(config, "0066")
    assert "execution_type" not in {
        c["name"] for c in inspect(engine).get_columns("workspace_candidates")
    }
    command.downgrade(config, "0065")
    assert "model_catalog" not in {
        c["name"] for c in inspect(engine).get_columns("provider_accounts")
    }
    command.downgrade(config, "0064")
    assert "review_evidence_jobs" not in inspect(engine).get_table_names()
    assert "review_wiki_pages" not in inspect(engine).get_table_names()
    assert "evidence_trace" not in {
        c["name"] for c in inspect(engine).get_columns("review_flow_runs")
    }
    command.downgrade(config, "0063")
    assert "review_flow_runs" not in inspect(engine).get_table_names()
    command.downgrade(config, "0062")
    assert "project_review_budgets" not in inspect(engine).get_table_names()
    command.downgrade(config, "0061")
    assert "project_flows" not in inspect(engine).get_table_names()
    command.downgrade(config, "0060")
    assert "issue_versions" not in {
        c["name"] for c in inspect(engine).get_columns("story_issue_coverage")
    }
    command.downgrade(config, "0059")
    assert "story_issue_coverage" not in inspect(engine).get_table_names()
    assert "epic_execution_plans" in inspect(engine).get_table_names()
    command.downgrade(config, "0058")
    assert "epic_execution_plans" not in inspect(engine).get_table_names()
    assert "planning_reviews" in inspect(engine).get_table_names()
    command.downgrade(config, "0057")
    assert "planning_reviews" not in inspect(engine).get_table_names()
    assert "planning_model" not in {c["name"] for c in inspect(engine).get_columns("projects")}
    command.downgrade(config, "0056")
    for table in ("project_epics", "project_user_stories"):
        assert "closed_at" not in {c["name"] for c in inspect(engine).get_columns(table)}
    command.downgrade(config, "0055")
    assert "issues_synced_at" not in {
        c["name"] for c in inspect(engine).get_columns("workspace_imports")
    }
    assert "project_connections" in inspect(engine).get_table_names()
    command.downgrade(config, "0054")
    assert "project_connections" not in inspect(engine).get_table_names()
    parent = "0053"
    assert "workspace_model_usage" in inspect(engine).get_table_names()
    command.downgrade(config, parent)
    assert "workspace_model_usage" not in inspect(engine).get_table_names()
    parent = "0052"
    assert "workspace_billed_days" in inspect(engine).get_table_names()
    command.downgrade(config, parent)
    assert "workspace_billed_days" not in inspect(engine).get_table_names()
    parent = "0051"
    approval_tables = {"workspace_candidates", "workspace_actions"}
    assert approval_tables <= set(inspect(engine).get_table_names())
    command.downgrade(config, parent)
    assert not approval_tables & set(inspect(engine).get_table_names())
    parent = "0050"
    import_tables = {"workspace_imports", "workspace_sources"}
    assert import_tables <= set(inspect(engine).get_table_names())
    command.downgrade(config, parent)
    assert not import_tables & set(inspect(engine).get_table_names())
    parent = "0049"
    assert "workspace_review" in {c["name"] for c in inspect(engine).get_columns("backlog_items")}
    command.downgrade(config, parent)
    assert "workspace_review" not in {
        c["name"] for c in inspect(engine).get_columns("backlog_items")
    }
    command.downgrade(config, "0048")
    assert not planning_tables & set(inspect(engine).get_table_names())
    assert "description" not in {c["name"] for c in inspect(engine).get_columns("projects")}
    parent = "0047"
    # 0048: the flows of the platform's own that were deleted.
    assert "removed_flows" in inspect(engine).get_table_names()
    command.downgrade(config, parent)
    assert "removed_flows" not in inspect(engine).get_table_names()
    parent = "0046"
    # 0047: the day a backlog item was done.
    assert "done_at" in {c["name"] for c in inspect(engine).get_columns("backlog_items")}
    command.downgrade(config, parent)
    assert "done_at" not in {c["name"] for c in inspect(engine).get_columns("backlog_items")}
    parent = "0045"
    # 0046: batches of questions handed to a provider, and their requests.
    assert {"model_batches", "model_batch_requests"} <= set(inspect(engine).get_table_names())
    command.downgrade(config, parent)
    assert not {"model_batches", "model_batch_requests"} & set(inspect(engine).get_table_names())
    parent = "0044"
    # 0045: summaries of a project's code.
    assert "code_summaries" in inspect(engine).get_table_names()
    command.downgrade(config, parent)
    assert "code_summaries" not in inspect(engine).get_table_names()
    parent = "0043"
    # 0044: the flow whose past runs an evaluation goes through.
    assert "of_flow" in {c["name"] for c in inspect(engine).get_columns("evaluations")}
    command.downgrade(config, parent)
    assert "of_flow" not in {c["name"] for c in inspect(engine).get_columns("evaluations")}
    parent = "0042"
    # 0043: agents and skills.
    assert {"agents", "skills"} <= set(inspect(engine).get_table_names())
    command.downgrade(config, parent)
    assert not {"agents", "skills"} & set(inspect(engine).get_table_names())
    parent = "0041"
    # 0042: what an account with an API key holds.
    of_api = {"sealed_key", "endpoint", "models", "prices"}
    assert of_api <= {c["name"] for c in inspect(engine).get_columns("provider_accounts")}
    command.downgrade(config, parent)
    assert not of_api & {c["name"] for c in inspect(engine).get_columns("provider_accounts")}
    parent = "0040"
    # 0041: a project's own commands.
    assert "toolchain" in {c["name"] for c in inspect(engine).get_columns("projects")}
    command.downgrade(config, parent)
    assert "toolchain" not in {c["name"] for c in inspect(engine).get_columns("projects")}
    parent = "0039"
    # 0040: what a saved task is for, and the kinds of task that are not offered.
    assert "description" in {c["name"] for c in inspect(engine).get_columns("saved_tasks")}
    assert "hidden_tasks" in inspect(engine).get_table_names()
    command.downgrade(config, parent)
    assert "description" not in {c["name"] for c in inspect(engine).get_columns("saved_tasks")}
    assert "hidden_tasks" not in inspect(engine).get_table_names()
    parent = "0038"
    # 0039: how hard the supervisor's model reasons, by provider.
    assert "efforts" in {c["name"] for c in inspect(engine).get_columns("supervisor_definitions")}
    command.downgrade(config, parent)
    assert "efforts" not in {
        c["name"] for c in inspect(engine).get_columns("supervisor_definitions")
    }
    parent = "0037"
    # 0038: the other hosts of a project's application, and whether a test identity is
    # stored for it.
    assert {"application_identity_at", "application_hosts"} <= {
        c["name"] for c in inspect(engine).get_columns("projects")
    }
    command.downgrade(config, parent)
    assert not {"application_identity_at", "application_hosts"} & {
        c["name"] for c in inspect(engine).get_columns("projects")
    }
    parent = "0036"
    # 0037: triggers and the address of a project's application. A trigger that is
    # switched on is work the administrator arranged: a downgrade must not drop it.
    assert {"flow_triggers", "saved_tasks"} <= set(inspect(engine).get_table_names())
    assert "application_url" in {c["name"] for c in inspect(engine).get_columns("projects")}
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects (id, name, display_name, source_repository, "
                "deployment_repository, default_branch, origin, created_at, updated_at) VALUES "
                "('00000000000000000000000000000037', 'triggered', 'Triggered', 'o/r', 'o/r', "
                "'main', 'console', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO flow_triggers (id, project_id, section_key, kind, spec, enabled, "
                "fired, created_at, updated_at) VALUES ('00000000000000000000000000000137', "
                "'00000000000000000000000000000037', 'code-audit', 'schedule', '{}', "
                ":enabled, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"enabled": True},
        )
    with pytest.raises(RuntimeError, match="refuses to drop triggers"):
        command.downgrade(config, parent)
    with engine.begin() as connection:
        connection.execute(text("UPDATE flow_triggers SET enabled = :enabled"), {"enabled": False})
        connection.execute(text("DELETE FROM projects WHERE name = 'triggered'"))
    command.downgrade(config, parent)
    assert "flow_triggers" not in set(inspect(engine).get_table_names())
    parent = "0035"
    # 0036: flows as versioned documents. A published version may be what sections and
    # runs follow, so a downgrade must not drop it; drafts and evaluations may go.
    assert {"flow_versions", "evaluations", "evaluation_cases"} <= set(
        inspect(engine).get_table_names()
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO flow_versions (id, flow_key, version, state, document, sha256, "
                "created_at, updated_at) VALUES ('00000000000000000000000000000036', "
                "'deliver-issue', 1, 'published', '{}', 'x', CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP)"
            )
        )
    with pytest.raises(RuntimeError, match="refuses to drop published"):
        command.downgrade(config, parent)
    with engine.begin() as connection:
        connection.execute(text("UPDATE flow_versions SET state = 'draft'"))
    command.downgrade(config, parent)
    assert "flow_versions" not in set(inspect(engine).get_table_names())
    assert "usage" not in {
        column["name"] for column in inspect(engine).get_columns("sandbox_steps")
    }
    # 0035: a project can be paused. A paused project must not start working again
    # because of a downgrade.
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE projects SET paused_at = CURRENT_TIMESTAMP WHERE name = 'falcone'")
        )
    with pytest.raises(RuntimeError, match="refuses to resume"):
        command.downgrade(config, "0034")
    with engine.begin() as connection:
        connection.execute(text("UPDATE projects SET paused_at = NULL"))
    command.downgrade(config, "0034")
    assert "paused_at" not in {column["name"] for column in inspect(engine).get_columns("projects")}
    # 0034: items with their own text, incidents and a project's cut-over. An item's
    # description is what it asks for, and an open incident is trouble not yet looked at,
    # so a downgrade must not drop either.
    assert "supervisor_incidents" in set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE backlog_items SET description = 'Keep me' "
                "WHERE id = (SELECT id FROM backlog_items LIMIT 1)"
            )
        )
        described = connection.scalar(
            text("SELECT COUNT(*) FROM backlog_items WHERE description IS NOT NULL")
        )
    if described:
        with pytest.raises(RuntimeError, match="refuses to drop"):
            command.downgrade(config, "0033")
        with engine.begin() as connection:
            connection.execute(text("UPDATE backlog_items SET description = NULL"))
    command.downgrade(config, "0033")
    assert "supervisor_incidents" not in set(inspect(engine).get_table_names())
    assert "description" not in {
        column["name"] for column in inspect(engine).get_columns("backlog_items")
    }
    assert "cut_over_at" not in {
        column["name"] for column in inspect(engine).get_columns("projects")
    }
    # 0033: what the chat channel remembers. Without it the channel starts again from now.
    assert {"chat_state", "chat_deliveries"} <= set(inspect(engine).get_table_names())
    command.downgrade(config, "0032")
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
