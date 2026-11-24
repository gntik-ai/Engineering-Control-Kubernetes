"""Let a queued issue closure say why, and carry the comment that is left on the issue."""

import sqlalchemy as sa
from alembic import op

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installations may already have the current ORM schema.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("workspace_actions")}
    with op.batch_alter_table("workspace_actions") as batch:
        if "state_reason" not in columns:
            batch.add_column(sa.Column("state_reason", sa.String(16)))
        if "note" not in columns:
            batch.add_column(sa.Column("note", sa.Text()))


def downgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("workspace_actions")}
    with op.batch_alter_table("workspace_actions") as batch:
        for name in ("note", "state_reason"):
            if name in columns:
                batch.drop_column(name)
