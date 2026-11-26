"""Keep which checks a project runs by itself, and which runs started that way."""

import sqlalchemy as sa
from alembic import op

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None


def _columns(table):
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    # Fresh installations may already have the current ORM schema.
    if "auto" not in _columns("choice_runs"):
        op.add_column(
            "choice_runs",
            sa.Column("auto", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "runs" not in _columns("review_settings"):
        op.add_column("review_settings", sa.Column("runs", sa.JSON(), nullable=True))


def downgrade():
    for table, column in (("review_settings", "runs"), ("choice_runs", "auto")):
        with op.batch_alter_table(table) as batch:
            batch.drop_column(column)
