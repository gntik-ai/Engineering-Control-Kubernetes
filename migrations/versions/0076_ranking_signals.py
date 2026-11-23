"""Keep what the ranking service says of planning records: a vector a record, a score a pair."""

import sqlalchemy as sa
from alembic import op

revision = "0076"
down_revision = "0075"
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
    if "record_vectors" not in tables:
        op.create_table(
            "record_vectors",
            *_stamps(),
            sa.Column("kind", sa.String(8), nullable=False),
            sa.Column("record_id", sa.Uuid(), nullable=False),
            sa.Column("model", sa.String(120), nullable=False),
            sa.Column("digest", sa.String(64), nullable=False),
            sa.Column("vector", sa.LargeBinary(), nullable=False),
            sa.UniqueConstraint(
                "project_id", "kind", "record_id", "model", name="uq_record_vectors_record"
            ),
        )
        op.create_index("ix_record_vectors_project_id", "record_vectors", ["project_id"])
    if "pair_scores" not in tables:
        op.create_table(
            "pair_scores",
            *_stamps(),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("child_id", sa.Uuid(), nullable=False),
            sa.Column("parent_id", sa.Uuid(), nullable=False),
            sa.Column("model", sa.String(120), nullable=False),
            sa.Column("digest", sa.String(64), nullable=False),
            sa.Column("score", sa.Float(), nullable=False),
            sa.UniqueConstraint(
                "project_id", "kind", "child_id", "parent_id", "model", name="uq_pair_scores_pair"
            ),
        )
        op.create_index("ix_pair_scores_project_id", "pair_scores", ["project_id"])


def downgrade():
    tables = sa.inspect(op.get_bind()).get_table_names()
    for name in ("pair_scores", "record_vectors"):
        if name in tables:
            op.drop_table(name)
