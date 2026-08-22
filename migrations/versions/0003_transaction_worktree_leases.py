"""Allow source and deployment worktree leases in one ChangeSet.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _unique_constraints() -> dict[str, set[str]]:
    values: dict[str, set[str]] = {}
    for item in sa.inspect(op.get_bind()).get_unique_constraints("worktree_leases"):
        name = item.get("name")
        columns = item.get("column_names")
        if isinstance(name, str) and isinstance(columns, list):
            values[name] = {str(column) for column in columns}
    return values


def upgrade() -> None:
    constraints = _unique_constraints()
    if op.get_bind().dialect.name == "postgresql":
        for name, columns in constraints.items():
            if columns in ({"change_set_id"}, {"branch"}):
                op.drop_constraint(name, "worktree_leases", type_="unique")
        constraints = _unique_constraints()
        if {"change_set_id", "repository"} not in constraints.values():
            op.create_unique_constraint(
                "uq_worktree_changeset_repository",
                "worktree_leases",
                ["change_set_id", "repository"],
            )
        if {"repository", "branch"} not in constraints.values():
            op.create_unique_constraint(
                "uq_worktree_repository_branch",
                "worktree_leases",
                ["repository", "branch"],
            )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    constraints = _unique_constraints()
    for name, columns in constraints.items():
        if columns in ({"change_set_id", "repository"}, {"repository", "branch"}):
            op.drop_constraint(name, "worktree_leases", type_="unique")
    op.create_unique_constraint(
        "worktree_leases_change_set_id_key", "worktree_leases", ["change_set_id"]
    )
    op.create_unique_constraint("worktree_leases_branch_key", "worktree_leases", ["branch"])
