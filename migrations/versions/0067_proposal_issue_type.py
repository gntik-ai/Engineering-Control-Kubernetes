"""Keep an administrator's issue classification until a proposal is approved."""

import sqlalchemy as sa
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade():
    columns = sa.inspect(op.get_bind()).get_columns("workspace_candidates")
    if not any(c["name"] == "execution_type" for c in columns):
        op.add_column("workspace_candidates", sa.Column("execution_type", sa.String(16)))


def downgrade():
    op.drop_column("workspace_candidates", "execution_type")
