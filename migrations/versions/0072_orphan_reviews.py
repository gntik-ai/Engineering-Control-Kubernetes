"""Record the runs that ask about issues without a User Story, and what they answered."""

import sqlalchemy as sa
from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installations may already have the current ORM schema.
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "orphan_runs" not in tables:
        op.create_table(
            "orphan_runs",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "project_id",
                sa.Uuid(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("model", sa.String(200), nullable=False),
            sa.Column("token_cap", sa.BigInteger(), nullable=False),
            sa.Column("spent", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("done", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("uncounted", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("claimed_at", sa.DateTime(timezone=True)),
            sa.Column("current_item", sa.Uuid()),
            sa.Column("reserved", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("ended_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_orphan_runs_project_id", "orphan_runs", ["project_id"])
        op.create_index("ix_orphan_runs_state", "orphan_runs", ["state"])
    if "orphan_reviews" not in tables:
        op.create_table(
            "orphan_reviews",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "project_id",
                sa.Uuid(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "item_id",
                sa.Uuid(),
                sa.ForeignKey("backlog_items.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("run_id", sa.Uuid(), sa.ForeignKey("orphan_runs.id", ondelete="SET NULL")),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("revision", sa.String(64), nullable=False),
            sa.Column("shortlist", sa.JSON(), nullable=False),
            sa.Column("answer", sa.String(16)),
            sa.Column("story_id", sa.Uuid()),
            sa.Column("confidence", sa.String(16)),
            sa.Column("reason", sa.Text()),
            sa.Column("evidence", sa.JSON()),
            sa.Column("checked", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("missing", sa.JSON()),
            sa.Column("provider", sa.String(32)),
            sa.Column("model", sa.String(200)),
            sa.Column("tokens", sa.Integer()),
            sa.Column("detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("decision", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("decided_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("project_id", "item_id", name="uq_orphan_reviews_item"),
        )
        op.create_index("ix_orphan_reviews_project_id", "orphan_reviews", ["project_id"])


def downgrade():
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "orphan_reviews" in tables:
        op.drop_table("orphan_reviews")
    if "orphan_runs" in tables:
        op.drop_table("orphan_runs")
