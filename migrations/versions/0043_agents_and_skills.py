"""Agents and skills: who does a task, defined once.

Revision ID: 0043
Revises: 0042

``agents`` holds what the administrator says of one who does work: standing
instructions, the subscriptions and models it works with in a sandbox, the provider and
model it is asked at over an API, how long it may work, and the skills it has. ``skills``
holds named instructions an agent can be given. A task of a flow names an agent; nothing
here says what a flow does.

Locking: two new tables; bounded as in 0022-0042.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0043 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _common() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
    ]


def upgrade() -> None:
    _bound()
    # Revision 0001 bootstraps the current models, so a fresh install has them already.
    there = _tables()
    if "skills" not in there:
        op.create_table("skills", *_common(), sa.Column("text", sa.Text(), nullable=False))
    if "agents" not in there:
        op.create_table(
            "agents",
            *_common(),
            sa.Column("instructions", sa.Text(), nullable=False),
            sa.Column("settings", sa.JSON(), nullable=False),
            sa.Column("skills", sa.JSON(), nullable=False),
        )


def downgrade() -> None:
    _bound()
    there = _tables()
    for name in ("agents", "skills"):
        if name in there:
            op.drop_table(name)
