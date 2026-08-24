"""Add paired snapshots, durable solution loops, and enhancements.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("campaigns", "source_sha", existing_type=sa.String(length=40), nullable=True)
    op.alter_column(
        "campaigns", "deployment_sha", existing_type=sa.String(length=40), nullable=True
    )
    op.add_column(
        "coverage_cells",
        sa.Column(
            "repository_surface",
            sa.String(length=32),
            nullable=False,
            server_default="source",
        ),
    )
    op.add_column(
        "coverage_cells",
        sa.Column(
            "flow",
            sa.String(length=120),
            nullable=False,
            server_default="repository-static",
        ),
    )
    op.drop_constraint("uq_coverage_cell", "coverage_cells", type_="unique")
    op.create_unique_constraint(
        "uq_coverage_cell",
        "coverage_cells",
        [
            "campaign_id",
            "repository_surface",
            "domain",
            "persona",
            "flow",
            "lens",
            "environment",
        ],
    )
    op.create_table(
        "project_snapshots",
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("project", sa.String(length=32), nullable=False),
        sa.Column("source_sha", sa.String(length=40), nullable=False),
        sa.Column("deployment_sha", sa.String(length=40), nullable=False),
        sa.Column("live_revision", sa.String(length=160)),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_project_snapshots_campaign_id", "project_snapshots", ["campaign_id"])
    op.create_index("ix_project_snapshots_project", "project_snapshots", ["project"])
    op.create_index("ix_project_snapshots_source_sha", "project_snapshots", ["source_sha"])
    op.create_index("ix_project_snapshots_deployment_sha", "project_snapshots", ["deployment_sha"])
    op.create_index("ix_project_snapshots_status", "project_snapshots", ["status"])
    op.create_table(
        "solution_loops",
        sa.Column("workflow_id", sa.String(length=240), nullable=False),
        sa.Column("project", sa.String(length=32), nullable=False),
        sa.Column("campaign_id", sa.String(length=240)),
        sa.Column("execution_mode", sa.String(length=32), nullable=False),
        sa.Column("maximum_risk", sa.String(length=8), nullable=False),
        sa.Column("maximum_items", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id"),
    )
    op.create_index("ix_solution_loops_workflow_id", "solution_loops", ["workflow_id"])
    op.create_index("ix_solution_loops_project", "solution_loops", ["project"])
    op.create_index("ix_solution_loops_campaign_id", "solution_loops", ["campaign_id"])
    op.create_index("ix_solution_loops_status", "solution_loops", ["status"])
    op.create_table(
        "enhancements",
        sa.Column("enhancement_key", sa.String(length=80), nullable=False),
        sa.Column("project", sa.String(length=32), nullable=False),
        sa.Column("campaign_id", sa.String(length=240), nullable=False),
        sa.Column("persona", sa.String(length=120), nullable=False),
        sa.Column("affected_repository", sa.String(length=200), nullable=False),
        sa.Column("risk_class", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("approval_binding_sha256", sa.String(length=64)),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("enhancement_key"),
        sa.UniqueConstraint("fingerprint"),
    )
    op.create_index("ix_enhancements_project", "enhancements", ["project"])
    op.create_index("ix_enhancements_campaign_id", "enhancements", ["campaign_id"])
    op.create_index("ix_enhancements_risk_class", "enhancements", ["risk_class"])
    op.create_index("ix_enhancements_status", "enhancements", ["status"])
    op.create_index("ix_enhancements_fingerprint", "enhancements", ["fingerprint"])
    op.create_index(
        "ix_enhancements_approval_binding_sha256",
        "enhancements",
        ["approval_binding_sha256"],
    )
    op.create_index("ix_enhancements_project_status", "enhancements", ["project", "status"])


def downgrade() -> None:
    op.drop_table("enhancements")
    op.drop_table("solution_loops")
    op.drop_table("project_snapshots")
    op.drop_constraint("uq_coverage_cell", "coverage_cells", type_="unique")
    op.create_unique_constraint(
        "uq_coverage_cell",
        "coverage_cells",
        ["campaign_id", "domain", "persona", "lens", "environment"],
    )
    op.drop_column("coverage_cells", "flow")
    op.drop_column("coverage_cells", "repository_surface")
    op.alter_column(
        "campaigns", "deployment_sha", existing_type=sa.String(length=40), nullable=False
    )
    op.alter_column("campaigns", "source_sha", existing_type=sa.String(length=40), nullable=False)
