"""Projects become records the administrator manages.

Revision ID: 0025
Revises: 0024

Until now the projects were a fixed list in code. This table holds one row per project:
the ones the workflows know by name are copied in with origin 'registry', and the console
adds its own with origin 'console'. Nothing else reads the table yet.

Locking: a new table only, created in this revision's transaction. Lock acquisition and
statements are bounded as in 0022-0024. The downgrade refuses while a project created in
the console exists, because dropping the table would lose it.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "projects"
# The registry as it is when this revision is written: name, source repository, deployment
# repository, default branch. Frozen here on purpose; a project added to the registry later
# gets its row when the console starts (ProjectRecords.sync_registry).
_REGISTRY = (
    ("falcone", "gntik-ai/falcone", "gntik-ai/falcone-charts", "main"),
    ("musematic", "gntik-ai/musematic", "gntik-ai/musematic-deploy", "main"),
    ("llmwiki", "gntik-ai/llmwiki", "gntik-ai/llmwiki", "master"),
    ("gntik-ui", "gntik-ai/gntik-ui", "gntik-ai/gntik-ui", "main"),
    ("falcone-serverless", "gntik-ai/falcone-serverless", "gntik-ai/falcone-serverless", "main"),
    ("falcone-workflow", "gntik-ai/falcone-workflow", "gntik-ai/falcone-workflow", "main"),
)
_COLUMNS = {
    "id",
    "name",
    "display_name",
    "source_repository",
    "deployment_repository",
    "default_branch",
    "origin",
    "archived_at",
    "created_at",
    "updated_at",
}


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0025 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _present() -> bool:
    """Revision 0001 bootstraps the current models, so a fresh install has the table."""
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return False
    columns = {str(item["name"]) for item in inspector.get_columns(_TABLE)}
    if columns != _COLUMNS:
        raise RuntimeError("revision 0025 found a partial bootstrap schema")
    return True


def _table() -> sa.Table:
    return sa.table(
        _TABLE,
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("display_name", sa.String()),
        sa.column("source_repository", sa.String()),
        sa.column("deployment_repository", sa.String()),
        sa.column("default_branch", sa.String()),
        sa.column("origin", sa.String()),
        sa.column("archived_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )


def _copy_registry() -> None:
    """One row per project of the registry that has none yet. Safe to repeat."""
    table = _table()
    bind = op.get_bind()
    existing = {str(row[0]) for row in bind.execute(sa.select(table.c.name))}
    now = datetime.now(UTC)
    rows = [
        {
            "id": uuid4(),
            "name": name,
            "display_name": name,
            "source_repository": source,
            "deployment_repository": deployment,
            "default_branch": branch,
            "origin": "registry",
            "archived_at": None,
            "created_at": now,
            "updated_at": now,
        }
        for name, source, deployment, branch in _REGISTRY
        if name not in existing
    ]
    if rows:
        op.bulk_insert(table, rows)


def upgrade() -> None:
    _bound()
    if not _present():
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.String(32), nullable=False),
            sa.Column("display_name", sa.String(80), nullable=False),
            sa.Column("source_repository", sa.String(140), nullable=False),
            sa.Column("deployment_repository", sa.String(140), nullable=False),
            sa.Column("default_branch", sa.String(100), nullable=False),
            sa.Column("origin", sa.String(16), nullable=False),
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "origin IN ('registry', 'console')", name=op.f("ck_projects_origin")
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
            sa.UniqueConstraint("name", name=op.f("uq_projects_name")),
        )
    _copy_registry()


def downgrade() -> None:
    _bound()
    created = (
        op.get_bind()
        .execute(sa.text("SELECT COUNT(*) FROM projects WHERE origin = 'console'"))
        .scalar()
    )
    if created:
        raise RuntimeError(
            "revision 0025 downgrade refuses to drop projects created in the console"
        )
    op.drop_table(_TABLE)
