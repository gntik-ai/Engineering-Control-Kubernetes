"""Persist review flow assignments, pinned runs and usage attribution."""

import sqlalchemy as sa
from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None

OWNERS = (
    "workspace_imports",
    "planning_reviews",
    "project_connections",
    "epic_execution_plans",
    "workspace_model_usage",
)


def upgrade():
    schema = sa.inspect(op.get_bind())
    if not schema.has_table("review_flow_assignments"):
        op.create_table(
            "review_flow_assignments",
            sa.Column(
                "project_id",
                sa.Uuid(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("action", sa.String(24), primary_key=True),
            sa.Column("flow_key", sa.String(64), nullable=False, index=True),
            sa.Column("version", sa.Integer()),
        )
    if not schema.has_table("review_flow_runs"):
        op.create_table(
            "review_flow_runs",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "project_id",
                sa.Uuid(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("action", sa.String(24), nullable=False),
            sa.Column("flow_key", sa.String(64), nullable=False),
            sa.Column("flow_name", sa.String(100), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("settings", sa.JSON(), nullable=False),
            sa.Column("outcome", sa.String(16)),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, index=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), index=True),
        )
    if "ix_workspace_model_usage_recorded_at" not in {
        i["name"] for i in schema.get_indexes("workspace_model_usage")
    }:
        op.create_index(
            "ix_workspace_model_usage_recorded_at", "workspace_model_usage", ["recorded_at"]
        )
    for table in OWNERS:
        if "review_run_id" not in {c["name"] for c in schema.get_columns(table)}:
            op.add_column(table, sa.Column("review_run_id", sa.Uuid()))


def downgrade():
    db = op.get_bind()
    if db.scalar(sa.text("SELECT 1 FROM review_flow_assignments LIMIT 1")) or db.scalar(
        sa.text("SELECT 1 FROM review_flow_runs LIMIT 1")
    ):
        raise RuntimeError("Downgrade would discard review flow assignments or execution history")
    op.drop_index("ix_workspace_model_usage_recorded_at", table_name="workspace_model_usage")
    for table in OWNERS:
        op.drop_column(table, "review_run_id")
    op.drop_table("review_flow_runs")
    op.drop_table("review_flow_assignments")
