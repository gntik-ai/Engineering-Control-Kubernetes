"""Let a run carry out a connection, and say which projects it reads."""

import sqlalchemy as sa
from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installations may already have the current ORM schema.
    inspector = sa.inspect(op.get_bind())
    runs = {c["name"] for c in inspector.get_columns("organize_runs")}
    if "connection_id" not in runs:
        op.add_column("organize_runs", sa.Column("connection_id", sa.Uuid(), nullable=True))
        op.create_index("ix_organize_runs_connection_id", "organize_runs", ["connection_id"])
    if "scope" not in runs:
        op.add_column(
            "organize_runs", sa.Column("scope", sa.JSON(), nullable=False, server_default="[]")
        )
    lines = {c["name"] for c in inspector.get_columns("organize_lines")}
    if "subject_project" not in lines:
        op.add_column("organize_lines", sa.Column("subject_project", sa.Uuid(), nullable=True))


def downgrade():
    with op.batch_alter_table("organize_lines") as batch:
        batch.drop_column("subject_project")
    with op.batch_alter_table("organize_runs") as batch:
        batch.drop_index("ix_organize_runs_connection_id")
        batch.drop_column("connection_id")
        batch.drop_column("scope")
