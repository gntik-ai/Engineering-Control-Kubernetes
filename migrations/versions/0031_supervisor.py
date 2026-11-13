"""The supervisor: its definition, its conversation and the record of what it did.

Revision ID: 0031
Revises: 0030

Three tables and one column. ``supervisor_definitions`` holds every version of the
supervisor's prompt, skills, tools and subscription; ``supervisor_messages`` the one
conversation between it and the administrator; ``supervisor_actions`` every call it made to
a tool and what became of it. ``backlog_items.proposed_by`` says when an item was proposed
by the supervisor.

Locking: new tables, and one nullable column without a default, which is a catalogue change
only; bounded as in 0022-0030. The downgrade refuses while an action still waits for the
administrator: the decision would be lost.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFINITIONS = "supervisor_definitions"
_MESSAGES = "supervisor_messages"
_ACTIONS = "supervisor_actions"
_ITEMS = "backlog_items"
_COLUMNS = {
    _DEFINITIONS: {"id", "version", "prompt", "skills", "tools", "providers", "account_id"},
    _MESSAGES: {"id", "role", "channel", "text", "run_id"},
    _ACTIONS: {"id", "run_id", "tool", "kind", "arguments", "outcome", "summary"},
}


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0031 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _present(table: str) -> bool:
    """Revision 0001 bootstraps the current models, so a fresh install has the tables."""
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return False
    columns = {str(item["name"]) for item in inspector.get_columns(table)}
    if not _COLUMNS[table] <= columns:
        raise RuntimeError("revision 0031 found a partial bootstrap schema")
    return True


def _has_proposed_by() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(_ITEMS)
    return "proposed_by" in {str(item["name"]) for item in columns}


def _stamps() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    _bound()
    if not _present(_DEFINITIONS):
        op.create_table(
            _DEFINITIONS,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("prompt", sa.Text(), nullable=False),
            sa.Column("skills", sa.JSON(), nullable=False),
            sa.Column("tools", sa.JSON(), nullable=False),
            sa.Column("providers", sa.JSON(), nullable=False),
            sa.Column("account_id", sa.Uuid(), nullable=True),
            sa.Column("exclusive", sa.Boolean(), nullable=False),
            sa.Column("models", sa.JSON(), nullable=False),
            sa.Column("note", sa.String(300), nullable=True),
            *_stamps(),
            sa.ForeignKeyConstraint(
                ["account_id"],
                ["provider_accounts.id"],
                name=op.f("fk_supervisor_definitions_account_id_provider_accounts"),
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_supervisor_definitions")),
            sa.UniqueConstraint("version", name=op.f("uq_supervisor_definitions_version")),
        )
    if not _present(_MESSAGES):
        op.create_table(
            _MESSAGES,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("role", sa.String(16), nullable=False),
            sa.Column("channel", sa.String(16), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("run_id", sa.Uuid(), nullable=True),
            *_stamps(),
            sa.CheckConstraint(
                "role IN ('administrator', 'supervisor', 'platform')",
                name=op.f("ck_supervisor_messages_role"),
            ),
            sa.CheckConstraint(
                "channel IN ('console', 'chat', 'platform')",
                name=op.f("ck_supervisor_messages_channel"),
            ),
            sa.ForeignKeyConstraint(
                ["run_id"],
                ["sandbox_runs.id"],
                name=op.f("fk_supervisor_messages_run_id_sandbox_runs"),
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_supervisor_messages")),
        )
        for column in ("role", "run_id"):
            op.create_index(op.f(f"ix_supervisor_messages_{column}"), _MESSAGES, [column])
    if not _present(_ACTIONS):
        op.create_table(
            _ACTIONS,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("run_id", sa.Uuid(), nullable=True),
            sa.Column("tool", sa.String(40), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("arguments", sa.JSON(), nullable=False),
            sa.Column("outcome", sa.String(16), nullable=False),
            sa.Column("summary", sa.String(600), nullable=True),
            sa.Column("reason", sa.String(600), nullable=True),
            sa.Column("detail", sa.String(600), nullable=True),
            sa.Column("project_id", sa.Uuid(), nullable=True),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("decided_via", sa.String(16), nullable=True),
            *_stamps(),
            sa.CheckConstraint(
                "outcome IN ('read', 'applied', 'pending', 'accepted', 'rejected', 'refused', "
                "'failed')",
                name=op.f("ck_supervisor_actions_outcome"),
            ),
            sa.CheckConstraint(
                "kind IN ('read', 'plan', 'operate', 'ask')",
                name=op.f("ck_supervisor_actions_kind"),
            ),
            sa.ForeignKeyConstraint(
                ["run_id"],
                ["sandbox_runs.id"],
                name=op.f("fk_supervisor_actions_run_id_sandbox_runs"),
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["project_id"],
                ["projects.id"],
                name=op.f("fk_supervisor_actions_project_id_projects"),
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_supervisor_actions")),
        )
        for column in ("run_id", "tool", "outcome", "project_id"):
            op.create_index(op.f(f"ix_supervisor_actions_{column}"), _ACTIONS, [column])
    if not _has_proposed_by():
        op.add_column(_ITEMS, sa.Column("proposed_by", sa.String(16), nullable=True))


def downgrade() -> None:
    _bound()
    waiting = (
        op.get_bind()
        .execute(sa.text("SELECT COUNT(*) FROM supervisor_actions WHERE outcome = 'pending'"))
        .scalar()
    )
    if waiting:
        raise RuntimeError(
            "revision 0031 downgrade refuses to drop actions that wait for the administrator"
        )
    if _has_proposed_by():
        op.drop_column(_ITEMS, "proposed_by")
    op.drop_table(_ACTIONS)
    op.drop_table(_MESSAGES)
    op.drop_table(_DEFINITIONS)
