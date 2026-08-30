"""Add controlled dashboard operation and connection projections.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _current_bootstrap_schema_present() -> bool:
    inspector = sa.inspect(op.get_bind())
    expected = {
        "action_candidates",
        "reauthentication_bindings",
        "operation_requests",
        "connection_snapshots",
    }
    present = expected.intersection(inspector.get_table_names())
    if present == expected:
        return True
    if present:
        raise RuntimeError("revision 0012 found a partial bootstrap schema")
    return False


def upgrade() -> None:
    if _current_bootstrap_schema_present():
        return
    op.create_table(
        "action_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("project", sa.String(32)),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(240), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("capability_status", sa.String(32), nullable=False),
        sa.Column("blockers_json", sa.JSON(), nullable=False),
        sa.Column("binding_hash", sa.String(64), nullable=False),
        sa.Column("confirmation_phrase", sa.String(120)),
        sa.Column("session_binding_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "binding_hash",
            "session_binding_hash",
            name="uq_action_candidate_binding_session",
        ),
    )
    op.create_table(
        "reauthentication_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("session_binding_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["candidate_id"], ["action_candidates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_id"),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_table(
        "operation_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("project", sa.String(32)),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(240), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("source_channel", sa.String(16), nullable=False),
        sa.Column("operator_class", sa.String(80), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("binding_hash", sa.String(64), nullable=False),
        sa.Column("workflow_id", sa.String(240)),
        sa.Column("temporal_run_id", sa.String(240)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("safe_error", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_table(
        "connection_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("component", sa.String(80), nullable=False),
        sa.Column("logical_target", sa.String(240), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("safe_error", sa.String(500)),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("next_action", sa.String(500)),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("component"),
    )
    indexes = {
        "action_candidates": (
            "action",
            "project",
            "target_id",
            "capability_status",
            "binding_hash",
            "session_binding_hash",
            "expires_at",
        ),
        "reauthentication_bindings": (
            "candidate_id",
            "token_digest",
            "session_binding_hash",
            "expires_at",
        ),
        "operation_requests": (
            "action",
            "project",
            "target_id",
            "source_channel",
            "idempotency_key",
            "binding_hash",
            "workflow_id",
            "status",
            "created_at",
        ),
        "connection_snapshots": ("component", "status", "checked_at"),
    }
    for table, columns in indexes.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])
    op.create_index(
        "ix_operation_requests_target_status",
        "operation_requests",
        ["action", "target_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("connection_snapshots")
    op.drop_table("operation_requests")
    op.drop_table("reauthentication_bindings")
    op.drop_table("action_candidates")
