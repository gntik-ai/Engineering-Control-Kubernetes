"""Finalized project billing totals from an attributed EUR feed.

Revision ID: 0053
Revises: 0052
"""

import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("workspace_billed_days"):
        op.create_table(
            "workspace_billed_days",
            sa.Column(
                "project_id",
                sa.Uuid(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("day", sa.Date(), primary_key=True),
            sa.Column("source", sa.String(100), nullable=False),
            sa.Column("reference", sa.String(200), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("euro_micros", sa.BigInteger(), nullable=False),
            sa.UniqueConstraint("source", "reference", name="uq_workspace_billed_reference"),
        )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM workspace_billed_days LIMIT 1")):
        raise RuntimeError("revision 0053 would discard finalized billing records")
    op.drop_table("workspace_billed_days")
