"""Let a run ask a cheaper model first, and keep what each of its two models did."""

import sqlalchemy as sa
from alembic import op

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None

RUN = (
    ("first_provider", sa.String(32), None),
    ("first_model", sa.String(200), None),
    ("first_asked", sa.Integer(), "0"),
    ("first_done", sa.Integer(), "0"),
    ("first_spent", sa.BigInteger(), "0"),
    ("first_cost", sa.Float(), "0"),
    ("cost", sa.Float(), "0"),
    ("priced", sa.Integer(), "0"),
)
OTHER = (("choice_reviews", "passed"), ("model_trial_lines", "settled"))


def _columns(table):
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    # Fresh installations may already have the current ORM schema.
    have = _columns("choice_runs")
    for name, kind, default in RUN:
        if name not in have:
            op.add_column(
                "choice_runs",
                sa.Column(name, kind, nullable=default is None, server_default=default),
            )
    for table, name in OTHER:
        if name not in _columns(table):
            op.add_column(
                table, sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false())
            )


def downgrade():
    for table, name in OTHER:
        if name in _columns(table):
            with op.batch_alter_table(table) as batch:
                batch.drop_column(name)
    have = _columns("choice_runs")
    with op.batch_alter_table("choice_runs") as batch:
        for name, _kind, _default in RUN:
            if name in have:
                batch.drop_column(name)
