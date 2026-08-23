"""Record immutable agent-pack instruction provenance.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("agent_invocations")}
    if "agent_pack_revision" not in columns:
        op.add_column("agent_invocations", sa.Column("agent_pack_revision", sa.String(length=40)))
    if "instruction_bundle_sha256" not in columns:
        op.add_column(
            "agent_invocations",
            sa.Column("instruction_bundle_sha256", sa.String(length=64)),
        )
    if "instruction_sources_json" not in columns:
        op.add_column("agent_invocations", sa.Column("instruction_sources_json", sa.JSON()))
    if "allowed_tools_json" not in columns:
        op.add_column("agent_invocations", sa.Column("allowed_tools_json", sa.JSON()))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("agent_invocations")}
    for name in (
        "allowed_tools_json",
        "instruction_sources_json",
        "instruction_bundle_sha256",
        "agent_pack_revision",
    ):
        if name in columns:
            op.drop_column("agent_invocations", name)
