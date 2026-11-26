"""Keep the third model a run asks where two differ, and the one a project names for it."""

import sqlalchemy as sa
from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None

RUN = (
    ("third_provider", sa.String(32), None),
    ("third_model", sa.String(200), None),
    ("third_asked", sa.Integer(), "0"),
    ("third_cost", sa.Float(), "0"),
)
SETTINGS = (
    ("doubt_provider", sa.String(32), None),
    ("doubt_model", sa.String(200), None),
)


def upgrade():
    # Fresh installations may already have the current ORM schema.
    for table, columns in (("choice_runs", RUN), ("review_settings", SETTINGS)):
        have = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}
        for name, kind, default in columns:
            if name not in have:
                op.add_column(
                    table, sa.Column(name, kind, nullable=default is None, server_default=default)
                )


def downgrade():
    for table, columns in (("review_settings", SETTINGS), ("choice_runs", RUN)):
        with op.batch_alter_table(table) as batch:
            for name, _kind, _default in reversed(columns):
                batch.drop_column(name)
