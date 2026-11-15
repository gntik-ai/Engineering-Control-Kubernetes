"""Accounts that reach a model over a provider's API.

Revision ID: 0042
Revises: 0041

``provider_accounts`` gains what an account with an API key holds: the key, sealed with
the platform's own key (``sealed_key``); which of the provider's addresses it is used at
(``endpoint``); the models the provider listed for it when it was last checked
(``models``); and what a million tokens cost on each model, as the administrator wrote
it (``prices``). All four stay empty for a subscription account.

Locking: four nullable columns without a default; bounded as in 0022-0041.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "provider_accounts"
_COLUMNS = (
    ("sealed_key", sa.Text()),
    ("endpoint", sa.String(length=20)),
    ("models", sa.JSON()),
    ("prices", sa.JSON()),
)


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0042 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _there() -> set[str]:
    return {str(item["name"]) for item in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    _bound()
    # Revision 0001 bootstraps the current models, so a fresh install has them already.
    there = _there()
    for name, kind in _COLUMNS:
        if name not in there:
            op.add_column(_TABLE, sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    _bound()
    there = _there()
    for name, _kind in reversed(_COLUMNS):
        if name in there:
            op.drop_column(_TABLE, name)
