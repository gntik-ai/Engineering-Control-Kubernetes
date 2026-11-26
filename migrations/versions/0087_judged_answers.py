"""Keep what a judge said to each answer, and whether the two agreed."""

import sqlalchemy as sa
from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None

RUN = (
    ("judge_provider", sa.String(32), None),
    ("judge_model", sa.String(200), None),
    ("judge_asked", sa.Integer(), "0"),
    ("agreed", sa.Integer(), "0"),
    ("judge_cost", sa.Float(), "0"),
)
REVIEW = (
    ("phase", sa.String(8), ""),
    ("judge", sa.JSON(), None),
    ("agreed", sa.Boolean(), None),
    ("rounds", sa.Integer(), "0"),
)


def _columns(table):
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    # Fresh installations may already have the current ORM schema.
    for table, columns in (("choice_runs", RUN), ("choice_reviews", REVIEW)):
        have = _columns(table)
        for name, kind, default in columns:
            if name not in have:
                op.add_column(
                    table, sa.Column(name, kind, nullable=default is None, server_default=default)
                )


def downgrade():
    for table, columns in (("choice_reviews", REVIEW), ("choice_runs", RUN)):
        have = _columns(table)
        with op.batch_alter_table(table) as batch:
            for name, _kind, _default in columns:
                if name in have:
                    batch.drop_column(name)
