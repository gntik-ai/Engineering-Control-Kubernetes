"""Canonicalize authenticated backlog operator actors.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from copy import deepcopy

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None

_LEGACY_AUTHENTICATED_ACTORS = {
    "cli:engineering-ctl": "cli:engineering-ctl:hermes",
    "dashboard:reauthenticated": "dashboard:reauthenticated:hermes",
}


def upgrade() -> None:
    """Backfill only exact fixed actors accepted by the preceding release."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "github_backlog_campaigns" not in set(inspector.get_table_names()):
        raise RuntimeError("revision 0014 requires github_backlog_campaigns")
    campaigns = sa.table(
        "github_backlog_campaigns",
        sa.column("id", sa.Uuid()),
        sa.column("approval_json", sa.JSON()),
    )
    rows = bind.execute(sa.select(campaigns.c.id, campaigns.c.approval_json)).mappings()
    for row in rows:
        evidence = row["approval_json"]
        if not isinstance(evidence, dict):
            continue
        approval = evidence.get("approval")
        actor_record = approval if isinstance(approval, dict) else evidence
        actor = actor_record.get("approved_by")
        replacement = _LEGACY_AUTHENTICATED_ACTORS.get(actor) if isinstance(actor, str) else None
        if replacement is None:
            continue
        updated = deepcopy(evidence)
        updated_approval = updated.get("approval")
        if isinstance(updated_approval, dict):
            updated_approval["approved_by"] = replacement
        else:
            updated["approved_by"] = replacement
        bind.execute(
            campaigns.update().where(campaigns.c.id == row["id"]).values(approval_json=updated)
        )


def downgrade() -> None:
    """Keep canonical identities; reverting them would corrupt post-upgrade approvals."""
