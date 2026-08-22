"""Initial control-plane metadata schema.

Revision ID: 0001
Revises: None
"""

from alembic import op

from engineering_control.db import models  # noqa: F401
from engineering_control.db.base import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This bootstrap revision is pinned to engineering-control 0.1.0. Future revisions must use
    # explicit operations and must never modify this file after its release checksum is recorded.
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    # Downgrade is intentionally complete but destructive; operators must take a database backup
    # and invoke it explicitly. Bootstrap and update scripts never call downgrade automatically.
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=False)
