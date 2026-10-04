"""Add UI migration blueprints and the fanned-out change-request context (NEW-123).

Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = {
    "ix_ui_blueprints_project": (["project"], False),
    "ix_ui_blueprints_status": (["status"], False),
    "ix_ui_blueprints_approval_binding_sha256": (["approval_binding_sha256"], False),
    "ix_ui_blueprints_active_fanout_workflow_id": (["active_fanout_workflow_id"], True),
}
_COLUMNS = {
    "id",
    "project",
    "status",
    "workflow_id",
    "requested_by",
    "requested_source_sha",
    "source_sha",
    "inventory_sha256",
    "inventory_summary_json",
    "kit_snapshot_sha256",
    "gntik_ui_version",
    "kit_image",
    "draft_attempts",
    "review_json",
    "drafts_json",
    "blueprint_json",
    "blueprint_sha256",
    "reviewed_sha256",
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
    "created_at",
    "updated_at",
}


def _bootstrap_state() -> tuple[bool, bool]:
    """(table present, column present): revision 0001 creates the current models."""
    inspector = sa.inspect(op.get_bind())
    table = "ui_blueprints" in set(inspector.get_table_names())
    if table:
        columns = {str(column["name"]) for column in inspector.get_columns("ui_blueprints")}
        indexes = {
            str(index["name"])
            for index in inspector.get_indexes("ui_blueprints")
            if index.get("name")
        }
        if not _COLUMNS.issubset(columns) or not set(_INDEXES).issubset(indexes):
            raise RuntimeError("revision 0020 found a partial bootstrap schema")
    column = "ui_context_json" in {
        str(item["name"]) for item in inspector.get_columns("change_requests")
    }
    return table, column


def upgrade() -> None:
    table, column = _bootstrap_state()
    if not column:
        with op.batch_alter_table("change_requests") as batch:
            batch.add_column(sa.Column("ui_context_json", sa.JSON(), nullable=True))
    if table:
        return
    op.create_table(
        "ui_blueprints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("workflow_id", sa.String(240), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("requested_source_sha", sa.String(40), nullable=True),
        sa.Column("source_sha", sa.String(40), nullable=True),
        sa.Column("inventory_sha256", sa.String(64), nullable=True),
        sa.Column("inventory_summary_json", sa.JSON(), nullable=False),
        sa.Column("kit_snapshot_sha256", sa.String(64), nullable=True),
        sa.Column("gntik_ui_version", sa.String(100), nullable=True),
        sa.Column("kit_image", sa.String(300), nullable=True),
        sa.Column("draft_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("review_json", sa.JSON(), nullable=False),
        sa.Column("drafts_json", sa.JSON(), nullable=False),
        sa.Column("blueprint_json", sa.JSON(), nullable=True),
        sa.Column("blueprint_sha256", sa.String(64), nullable=True),
        sa.Column("reviewed_sha256", sa.String(64), nullable=True),
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
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('DRAFTING', 'BLOCKED', 'FAILED', 'CANDIDATE', 'APPROVED', 'SUPERSEDED')",
            name="ck_ui_blueprints_status_known",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, (columns, unique) in _INDEXES.items():
        op.create_index(name, "ui_blueprints", columns, unique=unique)


def downgrade() -> None:
    op.drop_table("ui_blueprints")
    with op.batch_alter_table("change_requests") as batch:
        batch.drop_column("ui_context_json")
