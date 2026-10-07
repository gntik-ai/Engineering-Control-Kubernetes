"""Add the MERGED change-request status and UI gap consolidations (NEW-154).

Revision ID: 0021
Revises: 0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "ck_change_requests_status_known"
_PREVIOUS = "status IN ('DRAFTING', 'CANDIDATE', 'APPROVED', 'PUBLISHED', 'REJECTED', 'FAILED')"
_CURRENT = (
    "status IN ('DRAFTING', 'CANDIDATE', 'APPROVED', 'PUBLISHED', 'REJECTED', 'FAILED', 'MERGED')"
)
_INDEXES = {
    "ix_ui_gap_consolidations_blueprint_id": (["blueprint_id"], False),
    "ix_ui_gap_consolidations_status": (["status"], False),
    "ix_ui_gap_consolidations_input_sha256": (["input_sha256"], False),
    "ix_ui_gap_consolidations_plan_sha256": (["plan_sha256"], False),
}
_COLUMNS = {
    "id",
    "blueprint_id",
    "project",
    "kind",
    "status",
    "workflow_id",
    "requested_by",
    "input_sha256",
    "inputs_json",
    "plan_json",
    "plan_sha256",
    "problems_json",
    "error_code",
    "applied_by",
    "applied_at",
    "result_json",
    "created_at",
    "updated_at",
}


def _status_checks() -> list[tuple[str, str]]:
    """Every status CHECK constraint of change_requests as (name, sql). Revision 0019 ran
    under the metadata naming convention, so an installation upgraded through it names the
    constraint ck_change_requests_ck_change_requests_status_known; a fresh bootstrap names
    it ck_change_requests_status_known."""
    inspector = sa.inspect(op.get_bind())
    return [
        (str(item["name"]), str(item.get("sqltext") or ""))
        for item in inspector.get_check_constraints("change_requests")
        if item.get("name") and "DRAFTING" in str(item.get("sqltext") or "")
    ]


def _state() -> tuple[bool, bool]:
    """(table present, change-request columns present).

    Revision 0001 bootstraps the current models, so a fresh installation already has the
    table and the columns; a partial schema is refused rather than guessed at."""
    inspector = sa.inspect(op.get_bind())
    table = "ui_gap_consolidations" in set(inspector.get_table_names())
    if table:
        columns = {str(item["name"]) for item in inspector.get_columns("ui_gap_consolidations")}
        indexes = {
            str(item["name"])
            for item in inspector.get_indexes("ui_gap_consolidations")
            if item.get("name")
        }
        if not _COLUMNS.issubset(columns) or not set(_INDEXES).issubset(indexes):
            raise RuntimeError("revision 0021 found a partial bootstrap schema")
    columns = {str(item["name"]) for item in inspector.get_columns("change_requests")}
    added = {"merged_into_id", "consolidation_json"}
    if added & columns and not added.issubset(columns):
        raise RuntimeError("revision 0021 found a partial change-request schema")
    return table, added.issubset(columns)


def _replace_status_check(batch: object, condition: str, checks: list[tuple[str, str]]) -> None:
    for name, _sql in checks:
        batch.drop_constraint(op.f(name), type_="check")  # type: ignore[attr-defined]
    batch.create_check_constraint(op.f(_CHECK), sa.text(condition))  # type: ignore[attr-defined]


def upgrade() -> None:
    table, columns = _state()
    checks = _status_checks()
    merged = len(checks) == 1 and checks[0][0] == _CHECK and "MERGED" in checks[0][1]
    if not columns or not merged:
        with op.batch_alter_table("change_requests") as batch:
            if not columns:
                batch.add_column(sa.Column("merged_into_id", sa.Uuid(), nullable=True))
                batch.add_column(sa.Column("consolidation_json", sa.JSON(), nullable=True))
                batch.create_index(
                    op.f("ix_change_requests_merged_into_id"), ["merged_into_id"], unique=False
                )
            if not merged:
                _replace_status_check(batch, _CURRENT, checks)
    if table:
        return
    op.create_table(
        "ui_gap_consolidations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("blueprint_id", sa.Uuid(), nullable=False),
        sa.Column("project", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("workflow_id", sa.String(240), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("inputs_json", sa.JSON(), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=True),
        sa.Column("plan_sha256", sa.String(64), nullable=True),
        sa.Column("problems_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("applied_by", sa.String(128), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PLANNING', 'PROPOSED', 'INVALID', 'FAILED', 'APPLIED', 'SUPERSEDED')",
            name=op.f("ck_ui_gap_consolidations_status_known"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ui_gap_consolidations")),
        sa.UniqueConstraint("workflow_id", name=op.f("uq_ui_gap_consolidations_workflow_id")),
    )
    for name, (index_columns, unique) in _INDEXES.items():
        op.create_index(op.f(name), "ui_gap_consolidations", index_columns, unique=unique)


def downgrade() -> None:
    bind = op.get_bind()
    merged = bind.execute(
        sa.text("SELECT COUNT(*) FROM change_requests WHERE status = 'MERGED'")
    ).scalar()
    if merged:
        raise RuntimeError(
            "revision 0021 downgrade refuses MERGED change requests (restore a backup instead)"
        )
    op.drop_table("ui_gap_consolidations")
    checks = _status_checks()
    with op.batch_alter_table("change_requests") as batch:
        batch.drop_index(op.f("ix_change_requests_merged_into_id"))
        batch.drop_column("consolidation_json")
        batch.drop_column("merged_into_id")
        _replace_status_check(batch, _PREVIOUS, checks)
