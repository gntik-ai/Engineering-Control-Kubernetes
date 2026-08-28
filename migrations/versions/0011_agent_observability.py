"""Add bounded native-agent execution and transcript projections.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.String(length=240), nullable=False),
        sa.Column("temporal_run_id", sa.String(length=240)),
        sa.Column("activity_id", sa.String(length=240)),
        sa.Column("project", sa.String(length=32), nullable=False),
        sa.Column("repository_full_name", sa.String(length=200)),
        sa.Column("issue_number", sa.Integer()),
        sa.Column("campaign_id", sa.Uuid()),
        sa.Column("change_set_id", sa.Uuid()),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=80), nullable=False),
        sa.Column("host", sa.String(length=253)),
        sa.Column("worker_kind", sa.String(length=32)),
        sa.Column("task_queue", sa.String(length=120)),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("phase", sa.String(length=120)),
        sa.Column("selection_reason", sa.String(length=500)),
        sa.Column("cli_path_identifier", sa.String(length=80)),
        sa.Column("cli_version", sa.String(length=120)),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("retryable", sa.Boolean()),
        sa.Column("blocker_code", sa.String(length=120)),
        sa.Column("error_summary", sa.Text()),
        sa.Column("output_truncated", sa.Boolean(), nullable=False),
        sa.Column("transcript_incomplete", sa.Boolean(), nullable=False),
        sa.Column("total_log_bytes", sa.BigInteger(), nullable=False),
        sa.Column("redaction_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.ForeignKeyConstraint(["change_set_id"], ["change_sets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "agent_log_chunks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("stream", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redacted_text", sa.Text(), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("redaction_count", sa.Integer(), nullable=False),
        sa.Column("is_truncated", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["execution_id"], ["agent_executions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_id", "sequence", name="uq_agent_log_execution_sequence"),
    )
    for column in (
        "workflow_id",
        "project",
        "provider",
        "role",
        "worker_kind",
        "status",
        "phase",
        "started_at",
    ):
        op.create_index(f"ix_agent_executions_{column}", "agent_executions", [column])
    op.create_index("ix_agent_executions_project_status", "agent_executions", ["project", "status"])
    op.create_index(
        "ix_agent_executions_provider_started",
        "agent_executions",
        ["provider", "started_at"],
    )
    op.create_index("ix_agent_executions_last_heartbeat", "agent_executions", ["last_heartbeat_at"])
    for column in ("execution_id", "stream", "created_at"):
        op.create_index(f"ix_agent_log_chunks_{column}", "agent_log_chunks", [column])
    op.create_index(
        "ix_agent_log_chunks_execution_sequence",
        "agent_log_chunks",
        ["execution_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_table("agent_log_chunks")
    op.drop_table("agent_executions")
