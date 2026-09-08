"""Bind consumed dashboard candidates to their exact accepted operation.

Revision ID: 0016
Revises: 0015
No historical receipt is guessed or backfilled. Used legacy candidates fail closed.
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

TABLE = "action_candidates"
COLUMN = "accepted_operation_id"
INDEX = "ix_action_candidates_accepted_operation_id"
FOREIGN_KEY = "fk_action_candidates_accepted_operation"


def _bounded_connection() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0016 supports only PostgreSQL and SQLite")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _existing_is_compatible() -> bool:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"]: column for column in inspector.get_columns(TABLE)}
    if COLUMN not in columns:
        return False
    column = columns[COLUMN]
    dialect = op.get_bind().dialect.name
    uuid_type = (
        isinstance(column["type"], sa.Uuid)
        if dialect == "postgresql"
        else (isinstance(column["type"], sa.CHAR) and column["type"].length == 32)
    )
    matches = [
        key for key in inspector.get_foreign_keys(TABLE) if key["constrained_columns"] == [COLUMN]
    ]
    indexes = [index for index in inspector.get_indexes(TABLE) if index["name"] == INDEX]
    if (
        not uuid_type
        or column["nullable"] is not True
        or len(matches) != 1
        or matches[0]["name"] != FOREIGN_KEY
        or matches[0]["referred_table"] != "operation_requests"
        or matches[0]["referred_columns"] != ["id"]
        or matches[0].get("options", {}).get("ondelete") != "SET NULL"
        or len(indexes) != 1
        or indexes[0]["column_names"] != [COLUMN]
        or bool(indexes[0]["unique"])
    ):
        raise RuntimeError("revision 0016 found an incompatible action receipt definition")
    return True


def upgrade() -> None:
    _bounded_connection()
    if _existing_is_compatible():
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            FOREIGN_KEY, "operation_requests", [COLUMN], ["id"], ondelete="SET NULL"
        )
        batch.create_index(INDEX, [COLUMN], unique=False)
    if not _existing_is_compatible():
        raise RuntimeError("revision 0016 action receipt definition was not installed")


def downgrade() -> None:
    _bounded_connection()
    if not _existing_is_compatible():
        raise RuntimeError("revision 0016 receipt definition is missing")
    if (
        op.get_bind().scalar(
            sa.text(
                "SELECT 1 FROM action_candidates WHERE accepted_operation_id IS NOT NULL LIMIT 1"
            )
        )
        is not None
    ):
        raise RuntimeError("revision 0016 refuses to discard accepted-operation receipts")
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_index(INDEX)
        batch.drop_constraint(FOREIGN_KEY, type_="foreignkey")
        batch.drop_column(COLUMN)
