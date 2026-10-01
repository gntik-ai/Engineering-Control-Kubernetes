"""Add durable per-session dashboard logout revocations.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _current_bootstrap_schema_present() -> bool:
    inspector = sa.inspect(op.get_bind())
    if "dashboard_session_revocations" not in set(inspector.get_table_names()):
        return False
    required_columns = {
        "session_binding_hash",
        "password_version",
        "revoked_at",
        "expires_at",
    }
    columns = {
        str(column["name"]): column
        for column in inspector.get_columns("dashboard_session_revocations")
    }
    required_indexes = {
        "ix_dashboard_session_revocations_revoked_at",
        "ix_dashboard_session_revocations_expires_at",
    }
    actual_indexes = {
        str(index["name"])
        for index in inspector.get_indexes("dashboard_session_revocations")
        if index.get("name")
    }
    primary_key = inspector.get_pk_constraint("dashboard_session_revocations")
    column_contract = (
        required_columns.issubset(columns)
        and isinstance(columns["session_binding_hash"]["type"], sa.String)
        and columns["session_binding_hash"]["type"].length == 64
        and isinstance(columns["password_version"]["type"], sa.Integer)
        and isinstance(columns["revoked_at"]["type"], sa.DateTime)
        and isinstance(columns["expires_at"]["type"], sa.DateTime)
        and all(not bool(columns[name]["nullable"]) for name in required_columns)
        and list(primary_key.get("constrained_columns") or []) == ["session_binding_hash"]
    )
    if op.get_bind().dialect.name == "postgresql":
        column_contract = column_contract and all(
            bool(getattr(columns[name]["type"], "timezone", False))
            for name in ("revoked_at", "expires_at")
        )
    if not column_contract or not required_indexes.issubset(actual_indexes):
        raise RuntimeError("revision 0014 found a partial bootstrap schema")
    return True


def upgrade() -> None:
    if _current_bootstrap_schema_present():
        return
    op.create_table(
        "dashboard_session_revocations",
        sa.Column("session_binding_hash", sa.String(64), nullable=False),
        sa.Column("password_version", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("session_binding_hash"),
    )
    op.create_index(
        "ix_dashboard_session_revocations_revoked_at",
        "dashboard_session_revocations",
        ["revoked_at"],
    )
    op.create_index(
        "ix_dashboard_session_revocations_expires_at",
        "dashboard_session_revocations",
        ["expires_at"],
    )


def downgrade() -> None:
    # Dropping revocations would otherwise resurrect logged-out cookies. Rotate
    # the global password version first so every outstanding session fails closed.
    op.execute(
        sa.text(
            "UPDATE dashboard_authentication "
            "SET password_version = password_version + 1 WHERE id = 1"
        )
    )
    op.drop_table("dashboard_session_revocations")
