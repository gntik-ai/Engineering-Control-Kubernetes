"""Subscription accounts the agents work with.

Revision ID: 0028
Revises: 0027

One table. ``provider_accounts`` holds one row per subscription sign-in: its name, its
provider, what the platform last learned about it and how many sandboxes may use it at
once. The credential itself is never in the database; it is a Secret of the sandbox
namespace.

Locking: a new table only, created in this revision's transaction, bounded as in 0022-0027.
The downgrade refuses while an account exists that is not removed: dropping the table would
leave its stored sign-in without a record.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "provider_accounts"
_COLUMNS = {
    "id",
    "name",
    "provider",
    "state",
    "state_reason",
    "paused",
    "max_concurrent",
    "credential_generation",
    "identity",
    "signed_in_at",
    "checked_at",
    "last_used_at",
    "limited_until",
    "archived_at",
    "created_at",
    "updated_at",
}


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0028 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _present() -> bool:
    """Revision 0001 bootstraps the current models, so a fresh install has the table."""
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return False
    columns = {str(item["name"]) for item in inspector.get_columns(_TABLE)}
    # A later revision may add columns, which a fresh install then has from the start.
    if not _COLUMNS <= columns:
        raise RuntimeError("revision 0028 found a partial bootstrap schema")
    return True


def upgrade() -> None:
    _bound()
    if _present():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("state_reason", sa.String(300), nullable=True),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("max_concurrent", sa.Integer(), nullable=False),
        sa.Column("credential_generation", sa.Integer(), nullable=False),
        sa.Column("identity", sa.String(200), nullable=True),
        sa.Column("signed_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("limited_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('signed_out', 'unchecked', 'ready', 'limited', 'sign_in_required')",
            name=op.f("ck_provider_accounts_state"),
        ),
        sa.CheckConstraint(
            "max_concurrent BETWEEN 1 AND 8", name=op.f("ck_provider_accounts_max_concurrent")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_accounts")),
    )
    op.create_index(op.f("ix_provider_accounts_name"), _TABLE, ["name"])
    op.create_index(op.f("ix_provider_accounts_provider"), _TABLE, ["provider"])


def downgrade() -> None:
    _bound()
    kept = (
        op.get_bind()
        .execute(sa.text("SELECT COUNT(*) FROM provider_accounts WHERE archived_at IS NULL"))
        .scalar()
    )
    if kept:
        raise RuntimeError("revision 0028 downgrade refuses to drop accounts that are not removed")
    op.drop_table(_TABLE)
