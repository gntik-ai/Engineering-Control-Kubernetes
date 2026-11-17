"""Questions handed to a provider as a batch.

Revision ID: 0046
Revises: 0045

``model_batches`` is what the platform knows of a batch it handed to a provider: which
step and run it is for, which account pays, the batch's name at the provider, where it
is, and until when it is waited for. ``model_batch_requests`` holds its questions, each
with what came back. A batch lasts hours, so it is followed from these records and not
from memory: a launcher that starts again finds it here and does not hand it in again.

Locking: two new tables; bounded as in 0022-0045.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCHES = "model_batches"
_REQUESTS = "model_batch_requests"


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0046 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def upgrade() -> None:
    _bound()
    # Revision 0001 bootstraps the current models, so a fresh install has them already.
    present = set(sa.inspect(op.get_bind()).get_table_names())
    if _BATCHES not in present:
        op.create_table(
            _BATCHES,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "step_id",
                sa.Uuid(),
                sa.ForeignKey("sandbox_steps.id", ondelete="CASCADE"),
                nullable=True,
                index=True,
            ),
            sa.Column("run_id", sa.Uuid(), nullable=True, index=True),
            sa.Column(
                "account_id",
                sa.Uuid(),
                sa.ForeignKey("provider_accounts.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("provider", sa.String(length=40), nullable=False),
            sa.Column("model", sa.String(length=120), nullable=False),
            sa.Column("remote_id", sa.String(length=200), nullable=True),
            sa.Column("state", sa.String(length=20), nullable=False, index=True),
            sa.Column("stopping", sa.String(length=20), nullable=True),
            sa.Column("requests", sa.Integer(), nullable=False),
            sa.Column("answered", sa.Integer(), nullable=False),
            sa.Column("detail", sa.String(length=600), nullable=True),
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "state IN ('submitting', 'waiting', 'stopping', 'ended', 'failed')",
                name=op.f("ck_model_batches_state"),
            ),
        )
    if _REQUESTS not in present:
        op.create_table(
            _REQUESTS,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "batch_id",
                sa.Uuid(),
                sa.ForeignKey("model_batches.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("question", sa.JSON(), nullable=False),
            sa.Column("state", sa.String(length=20), nullable=False),
            sa.Column("answer", sa.Text(), nullable=True),
            sa.Column("model", sa.String(length=120), nullable=True),
            sa.Column("usage", sa.JSON(), nullable=True),
            sa.Column("kind", sa.String(length=20), nullable=True),
            sa.Column("error", sa.String(length=600), nullable=True),
            sa.UniqueConstraint("batch_id", "name", name="uq_model_batch_requests_batch_name"),
            sa.CheckConstraint(
                "state IN ('waiting', 'answered', 'failed', 'late')",
                name=op.f("ck_model_batch_requests_state"),
            ),
        )


def downgrade() -> None:
    _bound()
    present = set(sa.inspect(op.get_bind()).get_table_names())
    for table in (_REQUESTS, _BATCHES):
        if table in present:
            op.drop_table(table)
