"""Durable automatic review when a workspace record is added."""

import sqlalchemy as sa
from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade():
    db = sa.inspect(op.get_bind())
    for table, name, typ in (
        ("projects", "planning_provider", sa.String(16)),
        ("projects", "planning_model", sa.String(200)),
        ("workspace_candidates", "review_reason", sa.Text()),
    ):
        if name not in {column["name"] for column in db.get_columns(table)}:
            op.add_column(table, sa.Column(name, typ))
    if not db.has_table("planning_reviews"):
        op.create_table(
            "planning_reviews",
            sa.Column(
                "project_id",
                sa.Uuid(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("kind", sa.String(16), primary_key=True),
            sa.Column("record_id", sa.Uuid(), primary_key=True),
            sa.Column("title", sa.String(300), nullable=False),
            sa.Column("scope", sa.String(16), nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("lease", sa.Uuid()),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("progress", sa.JSON(), nullable=False),
            sa.Column("result", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_planning_reviews_due", "planning_reviews", ["status", "due_at"])


def downgrade():
    op.drop_table("planning_reviews")
    op.drop_column("workspace_candidates", "review_reason")
    op.drop_column("projects", "planning_model")
    op.drop_column("projects", "planning_provider")
