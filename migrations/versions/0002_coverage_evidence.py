"""Add coverage-cell and finding evidence payloads.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    coverage = _columns("coverage_cells")
    if "evidence_json" not in coverage:
        op.add_column(
            "coverage_cells",
            sa.Column("evidence_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        )
    if "result_json" not in coverage:
        op.add_column(
            "coverage_cells",
            sa.Column("result_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        )
    if "evidence_json" not in _columns("findings"):
        op.add_column(
            "findings",
            sa.Column("evidence_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        )


def downgrade() -> None:
    finding = _columns("findings")
    if "evidence_json" in finding:
        op.drop_column("findings", "evidence_json")
    coverage = _columns("coverage_cells")
    if "result_json" in coverage:
        op.drop_column("coverage_cells", "result_json")
    if "evidence_json" in coverage:
        op.drop_column("coverage_cells", "evidence_json")
