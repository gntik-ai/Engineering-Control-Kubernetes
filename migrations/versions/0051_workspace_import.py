"""Persist repository evidence and review checkpoints.

Revision ID: 0051
Revises: 0050
"""

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "workspace_imports" not in tables:
        op.create_table(
            "workspace_imports",
            sa.Column(
                "project_id",
                sa.Uuid(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("stage", sa.String(24), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("repository", sa.String(140), nullable=False),
            sa.Column("branch", sa.String(100), nullable=False),
            sa.Column("commit_sha", sa.String(40)),
            sa.Column("progress", sa.JSON(), nullable=False),
            sa.Column("draft", sa.Text()),
            sa.Column("provider", sa.String(16)),
            sa.Column("model", sa.String(200)),
            sa.Column("lease", sa.Uuid()),
            sa.Column("lease_until", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "workspace_sources" not in tables:
        op.create_table(
            "workspace_sources",
            sa.Column(
                "project_id",
                sa.Uuid(),
                sa.ForeignKey("workspace_imports.project_id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("key", sa.String(96), primary_key=True),
            sa.Column("kind", sa.String(24), nullable=False),
            sa.Column("evidence", sa.JSON(), nullable=False),
        )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM workspace_imports LIMIT 1")):
        raise RuntimeError("revision 0051 would discard a saved repository analysis")
    op.drop_table("workspace_sources")
    op.drop_table("workspace_imports")
