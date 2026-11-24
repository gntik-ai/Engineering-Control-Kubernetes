"""Keep what the scorer reports for a project and what it would do with each record."""

import sqlalchemy as sa
from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def _stamps(**project):
    return (
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            **project,
        ),
    )


def upgrade():
    # Fresh installations may already have the current ORM schema.
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "scorer_reports" not in tables:
        op.create_table(
            "scorer_reports",
            *_stamps(unique=True),
            sa.Column("state", sa.String(64), nullable=False),
            sa.Column("report", sa.JSON(), nullable=False),
            sa.Column("computed_at", sa.DateTime(timezone=True)),
        )
    if "scorer_decisions" not in tables:
        op.create_table(
            "scorer_decisions",
            *_stamps(),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("child_id", sa.Uuid(), nullable=False),
            sa.Column("parent_id", sa.Uuid()),
            sa.Column("outcome", sa.String(16), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("model", sa.String(8), nullable=False),
            sa.Column("detail", sa.JSON()),
        )
        op.create_index("ix_scorer_decisions_project_id", "scorer_decisions", ["project_id"])
        op.create_index("ix_scorer_decisions_outcome", "scorer_decisions", ["outcome"])


def downgrade():
    tables = sa.inspect(op.get_bind()).get_table_names()
    for name in ("scorer_decisions", "scorer_reports"):
        if name in tables:
            op.drop_table(name)
