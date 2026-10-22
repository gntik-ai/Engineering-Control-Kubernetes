"""Add backlog decompositions and the fanned-out decomposition context (NEW-234).

Revision ID: 0022
Revises: 0021

Locking: on PostgreSQL the change_requests columns and their lineage index are added in
this revision's transaction, so the table is held ACCESS EXCLUSIVE while the index builds.
That is acceptable here and bounded: the deploy runs migrations by restarting
engineering-migrate, which stops every unit that Requires= it (api, temporal-worker,
evidence-ingest, notifier, dashboard, watchdog; .go-status/deploy-2026-09-22.sh step 5c),
so there are no live readers or writers; change_requests is a small control-plane table;
and CREATE INDEX CONCURRENTLY cannot run inside the transactional DDL this alembic chain
uses. As in 0017/0018, lock acquisition is bounded (lock_timeout) and so is every
statement (statement_timeout): a held lock fails the migration closed instead of queueing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "backlog_decompositions"
_INDEXES = {
    "ix_backlog_decompositions_project": (["project"], False),
    "ix_backlog_decompositions_status": (["status"], False),
    "ix_backlog_decompositions_plan_sha256": (["plan_sha256"], False),
    "ix_backlog_decompositions_approval_binding_sha256": (["approval_binding_sha256"], False),
    "ix_backlog_decompositions_active_fanout_workflow_id": (["active_fanout_workflow_id"], True),
}
_COLUMNS = {
    "id",
    "project",
    "status",
    "workflow_id",
    "requested_by",
    "requested_source_sha",
    "source_sha",
    "design_sha256",
    "backlog_sha256",
    "issues_sha256",
    "parts_json",
    "provider_calls",
    "review_json",
    "plan_json",
    "plan_sha256",
    "markdown_sha256",
    "evidence_json",
    "approval_binding_sha256",
    "error_code",
    "blockers_json",
    "decided_by",
    "decided_at",
    "decision_sequence",
    "fanout_json",
    "fanout_sequence",
    "active_fanout_workflow_id",
    "fanout_claimed_at",
    "created_at",
    "updated_at",
}
_CHANGE_REQUEST_COLUMNS = {"decomposition_context_json", "decomposition_lineage"}
_LINEAGE_INDEX = "ix_change_requests_decomposition_lineage"
_CHANGE_REQUEST_INDEXES = {_LINEAGE_INDEX: (["decomposition_lineage"], False)}


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0022 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _indexes_match(
    inspector: sa.Inspector, table: str, expected: dict[str, tuple[list[str], bool]]
) -> bool:
    """Every expected index exists under its exact name, columns and uniqueness."""
    found = {
        str(item["name"]): (list(item["column_names"]), bool(item["unique"]))
        for item in inspector.get_indexes(table)
        if item.get("name")
    }
    return all(found.get(name) == (columns, unique) for name, (columns, unique) in expected.items())


def _state() -> tuple[bool, bool]:
    """(table present, change-request columns present).

    Revision 0001 bootstraps the current models, so a fresh installation already has the
    table and the columns; a partial schema is refused rather than guessed at."""
    inspector = sa.inspect(op.get_bind())
    table = _TABLE in set(inspector.get_table_names())
    if table:
        columns = {str(item["name"]) for item in inspector.get_columns(_TABLE)}
        if not _COLUMNS.issubset(columns) or not _indexes_match(inspector, _TABLE, _INDEXES):
            raise RuntimeError("revision 0022 found a partial bootstrap schema")
    columns = {str(item["name"]) for item in inspector.get_columns("change_requests")}
    present = _CHANGE_REQUEST_COLUMNS & columns
    if present and (
        present != _CHANGE_REQUEST_COLUMNS
        or not _indexes_match(inspector, "change_requests", _CHANGE_REQUEST_INDEXES)
    ):
        raise RuntimeError("revision 0022 found a partial change-request schema")
    return table, bool(present)


def upgrade() -> None:
    _bound()
    table, columns = _state()
    if not columns:
        with op.batch_alter_table("change_requests") as batch:
            batch.add_column(sa.Column("decomposition_context_json", sa.JSON(), nullable=True))
            batch.add_column(sa.Column("decomposition_lineage", sa.String(160), nullable=True))
            batch.create_index(op.f(_LINEAGE_INDEX), ["decomposition_lineage"], unique=False)
    if table:
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("workflow_id", sa.String(240), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("requested_source_sha", sa.String(40), nullable=True),
        sa.Column("source_sha", sa.String(40), nullable=True),
        sa.Column("design_sha256", sa.String(64), nullable=True),
        sa.Column("backlog_sha256", sa.String(64), nullable=True),
        sa.Column("issues_sha256", sa.String(64), nullable=True),
        sa.Column("parts_json", sa.JSON(), nullable=False),
        sa.Column("provider_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("review_json", sa.JSON(), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=True),
        sa.Column("plan_sha256", sa.String(64), nullable=True),
        sa.Column("markdown_sha256", sa.String(64), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("approval_binding_sha256", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("blockers_json", sa.JSON(), nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fanout_json", sa.JSON(), nullable=False),
        sa.Column("fanout_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_fanout_workflow_id", sa.String(240), nullable=True),
        sa.Column("fanout_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('DRAFTING', 'BLOCKED', 'FAILED', 'CANDIDATE', 'APPROVED', 'SUPERSEDED')",
            name=op.f("ck_backlog_decompositions_status_known"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backlog_decompositions")),
    )
    for name, (index_columns, unique) in _INDEXES.items():
        op.create_index(op.f(name), _TABLE, index_columns, unique=unique)


def downgrade() -> None:
    _bound()
    bind = op.get_bind()
    decompositions = bind.execute(sa.text("SELECT COUNT(*) FROM backlog_decompositions")).scalar()
    fanned_out = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM change_requests "
            "WHERE decomposition_lineage IS NOT NULL "
            # A JSON column may hold a JSON null rather than SQL NULL.
            "OR CAST(decomposition_context_json AS TEXT) <> 'null'"
        )
    ).scalar()
    if decompositions or fanned_out:
        raise RuntimeError(
            "revision 0022 downgrade refuses existing backlog decompositions or decomposition "
            "change requests (restore a backup instead)"
        )
    op.drop_table(_TABLE)
    with op.batch_alter_table("change_requests") as batch:
        batch.drop_index(op.f(_LINEAGE_INDEX))
        batch.drop_column("decomposition_lineage")
        batch.drop_column("decomposition_context_json")
