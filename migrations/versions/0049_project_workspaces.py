"""Project workspace metadata, default project and planning relationships.

Revision ID: 0049
Revises: 0048
"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROJECTS = sa.table(
    "projects",
    sa.column("id", sa.Uuid()),
    sa.column("name", sa.String()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("archived_at", sa.DateTime(timezone=True)),
    sa.column("description", sa.Text()),
    sa.column("architecture", sa.Text()),
)
_SETTINGS = sa.table(
    "project_settings", sa.column("id", sa.Integer()), sa.column("default_project_id", sa.Uuid())
)
_ITEM_INDEX = "uq_backlog_items_project_item"


def _bound(bind: Connection) -> None:
    if bind.dialect.name not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0049 supports PostgreSQL and SQLite only")
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        bind.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _initial_default(bind: Connection) -> UUID | None:
    return bind.scalar(
        sa.select(_PROJECTS.c.id)
        .where(_PROJECTS.c.archived_at.is_(None))
        .order_by(
            sa.case((_PROJECTS.c.name == "falcone", 0), else_=1),
            _PROJECTS.c.created_at,
            _PROJECTS.c.name,
        )
        .limit(1)
    )


def _create_planning_table(table: str, key_name: str) -> None:
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "id", name=key_name),
    )


def upgrade() -> None:
    bind = op.get_bind()
    _bound(bind)
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    # On a fresh install revision 0001 has already created the current models.
    columns = {column["name"] for column in inspector.get_columns("projects")}
    for name in ("description", "architecture"):
        if name not in columns:
            op.add_column("projects", sa.Column(name, sa.Text(), nullable=True))
    if _ITEM_INDEX not in {index["name"] for index in inspector.get_indexes("backlog_items")}:
        op.create_index(_ITEM_INDEX, "backlog_items", ["project_id", "id"], unique=True)

    if "project_settings" not in tables:
        op.create_table(
            "project_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "default_project_id",
                sa.Uuid(),
                sa.ForeignKey("projects.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.CheckConstraint("id = 1", name=op.f("ck_project_settings_singleton")),
        )
    if "project_epics" not in tables:
        _create_planning_table("project_epics", "uq_project_epics_project_epic")
    if "project_user_stories" not in tables:
        _create_planning_table("project_user_stories", "uq_project_user_stories_project_story")
    if "project_story_epics" not in tables:
        op.create_table(
            "project_story_epics",
            sa.Column("project_id", sa.Uuid(), primary_key=True),
            sa.Column("story_id", sa.Uuid(), primary_key=True),
            sa.Column("epic_id", sa.Uuid(), primary_key=True),
            sa.ForeignKeyConstraint(
                ["project_id", "story_id"],
                ["project_user_stories.project_id", "project_user_stories.id"],
                name="fk_project_story_epics_story",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["project_id", "epic_id"],
                ["project_epics.project_id", "project_epics.id"],
                name="fk_project_story_epics_epic",
                ondelete="CASCADE",
            ),
        )
    if "project_issue_stories" not in tables:
        op.create_table(
            "project_issue_stories",
            sa.Column("project_id", sa.Uuid(), primary_key=True),
            sa.Column("item_id", sa.Uuid(), primary_key=True),
            sa.Column("story_id", sa.Uuid(), primary_key=True),
            sa.ForeignKeyConstraint(
                ["project_id", "item_id"],
                ["backlog_items.project_id", "backlog_items.id"],
                name="fk_project_issue_stories_item",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["project_id", "story_id"],
                ["project_user_stories.project_id", "project_user_stories.id"],
                name="fk_project_issue_stories_story",
                ondelete="CASCADE",
            ),
        )
    if bind.scalar(sa.select(_SETTINGS.c.id).where(_SETTINGS.c.id == 1)) is None:
        bind.execute(sa.insert(_SETTINGS).values(id=1, default_project_id=_initial_default(bind)))


def downgrade() -> None:
    bind = op.get_bind()
    _bound(bind)
    if (
        bind.scalar(
            sa.select(_PROJECTS.c.id)
            .where(_PROJECTS.c.description.is_not(None) | _PROJECTS.c.architecture.is_not(None))
            .limit(1)
        )
        is not None
    ):
        raise RuntimeError("revision 0049 would discard workspace metadata")
    default = bind.scalar(sa.select(_SETTINGS.c.default_project_id).where(_SETTINGS.c.id == 1))
    if default is not None and default != _initial_default(bind):
        raise RuntimeError("revision 0049 would discard the chosen default project")
    for name in (
        "project_issue_stories",
        "project_story_epics",
        "project_user_stories",
        "project_epics",
    ):
        if bind.scalar(sa.select(1).select_from(sa.table(name)).limit(1)) is not None:
            raise RuntimeError("revision 0049 would discard project planning records")

    op.drop_table("project_issue_stories")
    op.drop_table("project_story_epics")
    op.drop_table("project_user_stories")
    op.drop_table("project_epics")
    op.drop_table("project_settings")
    op.drop_index(_ITEM_INDEX, table_name="backlog_items")
    op.drop_column("projects", "architecture")
    op.drop_column("projects", "description")
