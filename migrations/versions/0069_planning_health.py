"""Keep one measurement a day of how each project's planning hangs together."""

import sqlalchemy as sa
from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installations may already have the current ORM schema.
    if "planning_health" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "planning_health",
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("numbers", sa.JSON(), nullable=False),
    )


def downgrade():
    if "planning_health" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("planning_health")
