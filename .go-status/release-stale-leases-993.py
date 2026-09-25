"""One-off remediation (NEW-60 stopgap): the terminated #993 loop (cf901c78) left the
component and worktree leases of its cancelled child run unreleased, so the new loop
(70ad7d74) fails with COMPONENT_LEASE_CONFLICT / "active worktree lease conflicts".

Releases only unreleased leases whose owner Run belongs to the OLD loop's children
(workflow_id prefix below). Refuses to touch anything owned by the new loop.

  sudo systemd-run --quiet --pipe --wait -p User=engineering-control \
    -p EnvironmentFile=/etc/engineering-control/env/control.env \
    /opt/engineering-control/venv/bin/python - < release-stale-leases-993.py
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.config import get_settings
from engineering_control.db import models as db

OLD_LOOP = (
    "backlog-loop-musematic-94a83947-65f3-4f44-99fe-b1881a002983-"
    "cf901c78-5255-4bc0-94d6-94f4c0166908"
)
NEW_LOOP_MARKER = "70ad7d74-af7e-43b7-98c2-957abf7592f3"

handler = EngineeringActivityHandler(get_settings())
now = datetime.now(UTC)
with handler.sessions.begin() as session:
    old_runs = {
        run.id: run.workflow_id
        for run in session.scalars(
            select(db.Run).where(db.Run.workflow_id.like(f"{OLD_LOOP}-issue-%"))
        )
    }
    released = 0
    for model in (db.ComponentLease, db.WorktreeLease):
        for lease in session.scalars(select(model).where(model.released_at.is_(None))):
            owner = old_runs.get(lease.owner_run_id)
            if owner is None or NEW_LOOP_MARKER in owner:
                continue
            lease.released_at = now
            released += 1
            print(f"released {model.__name__} {lease.id} owned by {owner}")
print(f"released {released} lease(s)")
