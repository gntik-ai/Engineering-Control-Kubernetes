"""Add the password-protected operations dashboard projections.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _current_bootstrap_schema_present() -> bool:
    inspector = sa.inspect(op.get_bind())
    expected = {"dashboard_authentication", "dashboard_events"}
    present = expected.intersection(inspector.get_table_names())
    if present == expected:
        return True
    if present:
        raise RuntimeError("revision 0010 found a partial bootstrap schema")
    return False


def upgrade() -> None:
    if _current_bootstrap_schema_present():
        if op.get_bind().dialect.name == "postgresql":
            _create_projection_triggers()
        return
    op.create_table(
        "dashboard_authentication",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("password_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_dashboard_authentication_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "dashboard_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("host", sa.String(length=253)),
        sa.Column("worker_kind", sa.String(length=32)),
        sa.Column("project", sa.String(length=32)),
        sa.Column("workflow_id", sa.String(length=240)),
        sa.Column("temporal_run_id", sa.String(length=240)),
        sa.Column("issue_number", sa.Integer()),
        sa.Column("campaign_id", sa.String(length=240)),
        sa.Column("entity_type", sa.String(length=64)),
        sa.Column("entity_id", sa.String(length=240)),
        sa.Column("current_phase", sa.String(length=120)),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "occurred_at",
        "level",
        "event_type",
        "source",
        "worker_kind",
        "project",
        "workflow_id",
        "campaign_id",
    ):
        op.create_index(f"ix_dashboard_events_{column}", "dashboard_events", [column])
    op.create_index("ix_dashboard_events_stream", "dashboard_events", ["id", "occurred_at"])
    if op.get_bind().dialect.name == "postgresql":
        _create_projection_triggers()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in (
            "runs",
            "coverage_cells",
            "findings",
            "deployments",
            "agent_invocations",
            "change_sets",
            "solution_loops",
            "steps",
            "approvals",
            "github_backlog_campaigns",
            "verification_results",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS dashboard_project_{table} ON {table}")
            op.execute(f"DROP FUNCTION IF EXISTS dashboard_project_{table}()")
    op.drop_table("dashboard_events")
    op.drop_table("dashboard_authentication")


def _create_projection_triggers() -> None:
    definitions = {
        "runs": """
            IF TG_OP = 'INSERT' THEN
                INSERT INTO dashboard_events (
                    occurred_at, level, event_type, source, worker_kind, project,
                    workflow_id, campaign_id, entity_type, entity_id, current_phase,
                    message, metadata_json
                ) VALUES (
                    CURRENT_TIMESTAMP, 'INFO', 'request.accepted', 'controller', NEW.worker,
                    NEW.project, NEW.workflow_id, NEW.campaign_id::text, 'run', NEW.id::text,
                    NEW.status, 'Engineering request accepted', '{}'::json
                );
            END IF;
            INSERT INTO dashboard_events (
                occurred_at, level, event_type, source, host, worker_kind, project,
                workflow_id, temporal_run_id, campaign_id, entity_type, entity_id,
                current_phase, message, metadata_json
            ) VALUES (
                CURRENT_TIMESTAMP,
                CASE WHEN NEW.status IN ('FAILED', 'TIMED_OUT') THEN 'ERROR'
                     WHEN NEW.status IN ('BLOCKED', 'RATE_LIMITED') THEN 'WARNING' ELSE 'INFO' END,
                CASE WHEN NEW.status = 'QUEUED' THEN 'workflow.queued'
                     WHEN NEW.status = 'RUNNING' THEN 'workflow.started'
                     WHEN NEW.status IN ('SUCCESS', 'COMPLETE', 'COMPLETED')
                         THEN 'workflow.completed'
                     WHEN NEW.status = 'CANCELLED' THEN 'workflow.cancelled'
                     WHEN NEW.status = 'RATE_LIMITED' THEN 'provider.rate_limited'
                     WHEN NEW.status IN ('FAILED', 'TIMED_OUT') THEN 'workflow.failed'
                     ELSE 'workflow.phase_changed' END,
                CASE WHEN NEW.worker IS NULL THEN 'controller' ELSE 'worker' END,
                NULL, NEW.worker, NEW.project, NEW.workflow_id, NULL,
                NEW.campaign_id::text, 'run', NEW.id::text, NEW.status,
                'Workflow state changed to ' || left(NEW.status, 64), '{}'::json
            );
        """,
        "coverage_cells": """
            INSERT INTO dashboard_events (
                occurred_at, level, event_type, source, project, campaign_id, entity_type,
                entity_id, current_phase, message, metadata_json
            )
            SELECT CURRENT_TIMESTAMP,
                CASE WHEN NEW.status IN ('FAILED', 'ERROR') THEN 'ERROR'
                     WHEN NEW.status = 'BLOCKED' THEN 'WARNING' ELSE 'INFO' END,
                CASE WHEN NEW.status IN ('NOT_STARTED', 'QUEUED') THEN 'audit_cell.queued'
                     WHEN NEW.status IN ('IN_PROGRESS', 'RUNNING') THEN 'audit_cell.started'
                     WHEN NEW.status = 'BLOCKED' THEN 'audit_cell.blocked'
                     WHEN NEW.status IN ('FAILED', 'ERROR') THEN 'audit_cell.failed'
                     ELSE 'audit_cell.completed' END,
                'worker', campaigns.project, NEW.campaign_id::text, 'coverage_cell',
                NEW.id::text, NEW.status, 'Audit coverage cell state changed',
                json_build_object('cell_status', left(NEW.status, 64))
            FROM campaigns WHERE campaigns.id = NEW.campaign_id;
        """,
        "findings": """
            INSERT INTO dashboard_events (
                occurred_at, level, event_type, source, project, campaign_id, entity_type,
                entity_id, current_phase, message, metadata_json
            ) VALUES (
                CURRENT_TIMESTAMP, 'INFO',
                CASE WHEN NEW.disposition = 'CANDIDATE' THEN 'finding.created'
                     WHEN NEW.disposition IN ('PUBLISHED', 'VERIFIED_PUBLISHED')
                         THEN 'finding.published'
                     ELSE 'finding.verified' END,
                'controller', NEW.project, NEW.campaign_id::text, 'finding', NEW.id::text,
                NEW.disposition, 'Finding disposition changed',
                json_build_object('finding_disposition', left(NEW.disposition, 32))
            );
        """,
        "deployments": """
            INSERT INTO dashboard_events (
                occurred_at, level, event_type, source, project, entity_type, entity_id,
                current_phase, message, metadata_json
            ) VALUES (
                CURRENT_TIMESTAMP,
                CASE WHEN NEW.status LIKE '%FAIL%' THEN 'ERROR' ELSE 'INFO' END,
                CASE WHEN NEW.status IN ('QUEUED', 'RUNNING', 'DEPLOYING') THEN 'deployment.started'
                     WHEN NEW.status LIKE '%FAIL%' THEN 'deployment.failed'
                     ELSE 'deployment.completed' END,
                'worker', NEW.project, 'deployment', NEW.id::text, NEW.status,
                'Deployment state changed', json_build_object('status', left(NEW.status, 64))
            );
        """,
        "agent_invocations": """
            INSERT INTO dashboard_events (
                occurred_at, level, event_type, source, worker_kind, entity_type, entity_id,
                current_phase, message, metadata_json
            ) VALUES (
                CURRENT_TIMESTAMP,
                CASE WHEN NEW.status IN ('FAILED', 'ERROR') THEN 'ERROR'
                     WHEN NEW.status = 'RATE_LIMITED' THEN 'WARNING' ELSE 'INFO' END,
                CASE WHEN NEW.status = 'RUNNING' THEN 'provider.selected'
                     WHEN NEW.status = 'RATE_LIMITED' THEN 'provider.rate_limited'
                     WHEN NEW.status IN ('FAILED', 'ERROR') THEN 'provider.failed'
                     ELSE 'provider.completed' END,
                'worker', NEW.provider, 'agent_invocation', NEW.id::text, NEW.status,
                'Provider activity state changed',
                json_build_object(
                    'provider', left(NEW.provider, 32), 'status', left(NEW.status, 64)
                )
            );
        """,
        "change_sets": """
            INSERT INTO dashboard_events (
                occurred_at, level, event_type, source, project, entity_type, entity_id,
                current_phase, message, metadata_json
            ) VALUES (
                CURRENT_TIMESTAMP,
                CASE WHEN NEW.state IN ('BLOCKED', 'DEPLOYMENT_FAILED')
                     THEN 'WARNING' ELSE 'INFO' END,
                CASE WHEN NEW.state IN ('IMPLEMENTING', 'CI_VALIDATED') THEN 'build.started'
                     WHEN NEW.state IN ('CANDIDATE_BUILT', 'FINAL_ARTIFACT_BUILT')
                         THEN 'build.completed'
                     WHEN NEW.state = 'DEPLOYMENT_FAILED' THEN 'build.failed'
                     WHEN NEW.state LIKE 'AWAITING%APPROVAL' THEN 'approval.required'
                     ELSE 'issue_status.changed' END,
                'controller', NEW.project, 'change_set', NEW.id::text, NEW.state,
                'ChangeSet state changed', json_build_object('status', left(NEW.state, 64))
            );
        """,
        "solution_loops": """
            INSERT INTO dashboard_events (
                occurred_at, level, event_type, source, project, workflow_id, campaign_id,
                entity_type, entity_id, current_phase, message, metadata_json
            ) VALUES (
                CURRENT_TIMESTAMP,
                CASE WHEN NEW.status IN ('FAILED', 'BLOCKED') THEN 'WARNING' ELSE 'INFO' END,
                CASE WHEN NEW.status = 'QUEUED' THEN 'workflow.queued'
                     WHEN NEW.status = 'PAUSED' THEN 'workflow.paused'
                     WHEN TG_OP = 'UPDATE' AND OLD.status = 'PAUSED'
                          AND NEW.status = 'RUNNING' THEN 'workflow.resumed'
                     WHEN NEW.status = 'CANCELLED' THEN 'workflow.cancelled'
                     WHEN NEW.status IN ('COMPLETE', 'COMPLETED', 'SUCCESS')
                         THEN 'workflow.completed'
                     WHEN NEW.status = 'FAILED' THEN 'workflow.failed'
                     ELSE 'issue_status.changed' END,
                'controller', NEW.project, NEW.workflow_id, NEW.campaign_id, 'solution_loop',
                NEW.id::text, NEW.status, 'Solution-loop state changed',
                json_build_object('status', left(NEW.status, 64))
            );
        """,
        "steps": """
            INSERT INTO dashboard_events (
                occurred_at, level, event_type, source, worker_kind, project, workflow_id,
                campaign_id, entity_type, entity_id, current_phase, message, metadata_json
            )
            SELECT CURRENT_TIMESTAMP,
                CASE WHEN NEW.status IN ('FAILED', 'ERROR') THEN 'ERROR' ELSE 'INFO' END,
                CASE WHEN NEW.status IN ('RUNNING', 'IN_PROGRESS') THEN 'test.started'
                     WHEN NEW.status IN ('FAILED', 'ERROR') THEN 'test.failed'
                     ELSE 'test.completed' END,
                CASE WHEN runs.worker IS NULL THEN 'controller' ELSE 'worker' END,
                runs.worker, runs.project, runs.workflow_id, runs.campaign_id::text, 'step',
                NEW.id::text, NEW.name, 'Workflow step state changed',
                json_build_object('step', left(NEW.name, 120), 'status', left(NEW.status, 64))
            FROM runs WHERE runs.id = NEW.run_id;
        """,
        "approvals": """
            INSERT INTO dashboard_events (
                occurred_at, level, event_type, source, project, entity_type, entity_id,
                current_phase, message, metadata_json
            )
            SELECT CURRENT_TIMESTAMP,
                CASE WHEN NEW.decision = 'REJECT' THEN 'WARNING' ELSE 'INFO' END,
                CASE WHEN NEW.decision = 'REJECT' THEN 'approval.rejected'
                     ELSE 'approval.received' END,
                'controller', change_sets.project, 'approval', NEW.id::text, NEW.decision,
                'Deployment approval decision recorded',
                json_build_object('status', left(NEW.decision, 16))
            FROM change_sets WHERE change_sets.id = NEW.change_set_id;
        """,
        "github_backlog_campaigns": """
            INSERT INTO dashboard_events (
                occurred_at, level, event_type, source, project, workflow_id, campaign_id,
                entity_type, entity_id, current_phase, message, metadata_json
            ) VALUES (
                CURRENT_TIMESTAMP,
                CASE WHEN NEW.status LIKE '%BLOCK%' THEN 'WARNING' ELSE 'INFO' END,
                CASE WHEN NEW.status = 'AWAITING_APPROVAL' THEN 'approval.required'
                     WHEN NEW.status IN ('APPROVED', 'APPROVED_POLICY_PENDING')
                         THEN 'approval.received'
                     WHEN NEW.status LIKE '%BLOCK%' THEN 'github.operation_blocked'
                     WHEN NEW.status IN ('COMPLETE', 'COMPLETED', 'SUCCESS')
                         THEN 'github.operation_completed'
                     ELSE 'github.operation_attempted' END,
                'controller', NEW.project, NEW.workflow_id, NEW.id::text,
                'github_backlog_campaign', NEW.id::text, NEW.status,
                'GitHub backlog campaign state changed',
                json_build_object('status', left(NEW.status, 64))
            );
        """,
        "verification_results": """
            INSERT INTO dashboard_events (
                occurred_at, level, event_type, source, worker_kind, project, workflow_id,
                entity_type, entity_id, current_phase, message, metadata_json
            )
            SELECT CURRENT_TIMESTAMP,
                CASE WHEN NEW.status IN ('FAILED', 'ERROR') THEN 'ERROR' ELSE 'INFO' END,
                CASE WHEN NEW.status IN ('RUNNING', 'IN_PROGRESS')
                         THEN 'runtime_verification.started'
                     WHEN NEW.status IN ('FAILED', 'ERROR')
                         THEN 'runtime_verification.failed'
                     ELSE 'runtime_verification.completed' END,
                CASE WHEN runs.worker IS NULL THEN 'controller' ELSE 'worker' END,
                runs.worker, change_sets.project, runs.workflow_id,
                'verification_result', NEW.id::text, NEW.status,
                'Runtime verification state changed',
                json_build_object(
                    'verification_type', left(NEW.verification_type, 80),
                    'status', left(NEW.status, 64)
                )
            FROM change_sets
            LEFT JOIN runs ON runs.id = NEW.run_id
            WHERE change_sets.id = NEW.change_set_id;
        """,
    }
    for table, body in definitions.items():
        op.execute(
            f"""
            CREATE FUNCTION dashboard_project_{table}() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                BEGIN
                    {body}
                EXCEPTION WHEN OTHERS THEN
                    NULL;
                END;
                RETURN NEW;
            END;
            $$
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER dashboard_project_{table}
            AFTER INSERT OR UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION dashboard_project_{table}()
            """
        )
