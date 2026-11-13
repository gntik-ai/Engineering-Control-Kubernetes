"""Flows as versioned documents, what a step used, and evaluations.

Revision ID: 0036
Revises: 0035

``flow_versions`` holds every version of a flow that is not the platform's own: drafts
and what was published. ``sandbox_steps.usage`` is what a step's agent reported using.
``evaluations`` and ``evaluation_cases`` record a version of a flow tried on past work.

Locking: new tables and one nullable column without a default; bounded as in 0022-0035.
The downgrade refuses while a published version of a flow exists: runs and sections may
follow it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSIONS = "flow_versions"
_EVALUATIONS = "evaluations"
_CASES = "evaluation_cases"
_STEPS = "sandbox_steps"


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0036 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _tables() -> set[str]:
    """Revision 0001 bootstraps the current models, so a fresh install has the tables."""
    return set(sa.inspect(op.get_bind()).get_table_names())


def _has_usage() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(_STEPS)
    return "usage" in {str(item["name"]) for item in columns}


def _stamps() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    _bound()
    tables = _tables()
    if _VERSIONS not in tables:
        op.create_table(
            _VERSIONS,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("flow_key", sa.String(64), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("document", sa.JSON(), nullable=False),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column("note", sa.String(300), nullable=True),
            sa.Column("problems", sa.JSON(), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            *_stamps(),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_flow_versions")),
            sa.UniqueConstraint("flow_key", "version", name=op.f("uq_flow_versions_key_version")),
            sa.CheckConstraint(
                "state IN ('draft', 'published')", name=op.f("ck_flow_versions_state")
            ),
        )
        for column in ("flow_key", "state"):
            op.create_index(op.f(f"ix_flow_versions_{column}"), _VERSIONS, [column])
    if _EVALUATIONS not in tables:
        op.create_table(
            _EVALUATIONS,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("flow_key", sa.String(64), nullable=False),
            sa.Column("candidate_version", sa.Integer(), nullable=False),
            sa.Column("baseline_version", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Uuid(), nullable=True),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("note", sa.String(300), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            *_stamps(),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluations")),
            sa.ForeignKeyConstraint(
                ["project_id"],
                ["projects.id"],
                name=op.f("fk_evaluations_project_id_projects"),
                ondelete="SET NULL",
            ),
            sa.CheckConstraint(
                "state IN ('running', 'finished', 'stopped')", name=op.f("ck_evaluations_state")
            ),
        )
        for column in ("flow_key", "project_id", "state"):
            op.create_index(op.f(f"ix_evaluations_{column}"), _EVALUATIONS, [column])
    if _CASES not in tables:
        op.create_table(
            _CASES,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("evaluation_id", sa.Uuid(), nullable=False),
            sa.Column("arm", sa.String(16), nullable=False),
            sa.Column("subject", sa.JSON(), nullable=False),
            sa.Column("run_id", sa.Uuid(), nullable=True),
            *_stamps(),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation_cases")),
            sa.ForeignKeyConstraint(
                ["evaluation_id"],
                ["evaluations.id"],
                name=op.f("fk_evaluation_cases_evaluation_id_evaluations"),
                ondelete="CASCADE",
            ),
            sa.CheckConstraint(
                "arm IN ('candidate', 'baseline')", name=op.f("ck_evaluation_cases_arm")
            ),
        )
        for column in ("evaluation_id", "run_id"):
            op.create_index(op.f(f"ix_evaluation_cases_{column}"), _CASES, [column])
    if not _has_usage():
        op.add_column(_STEPS, sa.Column("usage", sa.JSON(), nullable=True))


def downgrade() -> None:
    _bound()
    published = (
        op.get_bind()
        .execute(sa.text("SELECT COUNT(*) FROM flow_versions WHERE state = 'published'"))
        .scalar()
    )
    if published:
        raise RuntimeError("revision 0036 downgrade refuses to drop published flow versions")
    if _has_usage():
        op.drop_column(_STEPS, "usage")
    op.drop_table(_CASES)
    op.drop_table(_EVALUATIONS)
    op.drop_table(_VERSIONS)
