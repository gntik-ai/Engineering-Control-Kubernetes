"""Let a run be limited in US dollars."""

import sqlalchemy as sa
from alembic import op

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def _columns():
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("choice_runs")}


def upgrade():
    # Fresh installations may already have the current ORM schema.
    if "dollar_cap" not in _columns():
        op.add_column("choice_runs", sa.Column("dollar_cap", sa.Float()))


def downgrade():
    if "dollar_cap" in _columns():
        with op.batch_alter_table("choice_runs") as batch:
            batch.drop_column("dollar_cap")
