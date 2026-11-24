"""Keep the batches of changes the scorer would make, and each of their lines."""

import sqlalchemy as sa
from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def _stamps():
    return (
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
        ),
    )


def upgrade():
    # Fresh installations may already have the current ORM schema.
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "reorg_batches" not in tables:
        op.create_table(
            "reorg_batches",
            *_stamps(),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("measured", sa.JSON(), nullable=False),
            sa.Column("allowed_wrong", sa.Integer(), nullable=False),
            sa.Column("decided_at", sa.DateTime(timezone=True)),
            sa.Column("undone_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_reorg_batches_project_id", "reorg_batches", ["project_id"])
    if "reorg_lines" not in tables:
        op.create_table(
            "reorg_lines",
            *_stamps(),
            sa.Column(
                "batch_id",
                sa.Uuid(),
                sa.ForeignKey("reorg_batches.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("link_kind", sa.String(16), nullable=False),
            sa.Column("child_id", sa.Uuid(), nullable=False),
            sa.Column("parent_id", sa.Uuid(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("signals", sa.JSON()),
            sa.Column("sampled", sa.Boolean(), nullable=False),
            sa.Column("verdict", sa.String(8)),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("before", sa.JSON()),
            sa.Column("detail", sa.Text()),
        )
        op.create_index("ix_reorg_lines_project_id", "reorg_lines", ["project_id"])
        op.create_index("ix_reorg_lines_batch_id", "reorg_lines", ["batch_id"])


def downgrade():
    tables = sa.inspect(op.get_bind()).get_table_names()
    for name in ("reorg_lines", "reorg_batches"):
        if name in tables:
            op.drop_table(name)
