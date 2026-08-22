"""Add identity-bound worker heartbeat metadata.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "worker_heartbeats" in inspector.get_table_names():
        return
    op.create_table(
        "worker_heartbeats",
        sa.Column("identity", sa.String(length=80), nullable=False),
        sa.Column("hostname", sa.String(length=253), nullable=False),
        sa.Column("worker_kind", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity"),
    )
    op.create_index(
        op.f("ix_worker_heartbeats_identity"),
        "worker_heartbeats",
        ["identity"],
        unique=True,
    )
    op.create_index(
        op.f("ix_worker_heartbeats_worker_kind"),
        "worker_heartbeats",
        ["worker_kind"],
        unique=False,
    )
    op.create_index(
        op.f("ix_worker_heartbeats_observed_at"),
        "worker_heartbeats",
        ["observed_at"],
        unique=False,
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "worker_heartbeats" not in inspector.get_table_names():
        return
    op.drop_index(op.f("ix_worker_heartbeats_observed_at"), table_name="worker_heartbeats")
    op.drop_index(op.f("ix_worker_heartbeats_worker_kind"), table_name="worker_heartbeats")
    op.drop_index(op.f("ix_worker_heartbeats_identity"), table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")
