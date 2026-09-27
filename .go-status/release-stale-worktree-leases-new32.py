"""One-off NEW-32 remediation: release the worktree leases left by musematic #1026's
first delivery (run 62ce2ee5, stopped at the spec step on 2026-09-23 before NEW-32).

Runs the deployed delivery.release_leases step for that single run -- the same code a
stopped delivery now calls itself. It releases nothing if the ChangeSet already has a PR.

  sudo systemd-run --quiet --pipe --wait -p User=engineering-control \
    -p EnvironmentFile=/etc/engineering-control/env/control.env \
    /opt/engineering-control/venv/bin/python - < release-stale-worktree-leases-new32.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import get_settings
from engineering_control.db.models import WorktreeLease

RUN_ID = "62ce2ee5-bcb1-4604-8215-c7c72935ced3"


def active_leases(handler: MetadataActivityHandler) -> list[str]:
    with handler.sessions() as session:
        return [
            f"{lease.repository} owner={lease.owner_run_id}"
            for lease in session.scalars(
                select(WorktreeLease).where(WorktreeLease.released_at.is_(None))
            )
            if str(lease.owner_run_id) == RUN_ID
        ]


handler = MetadataActivityHandler(get_settings())
print("before:", active_leases(handler))
print(asyncio.run(handler.handle("delivery.release_leases", {"run_id": RUN_ID}))["status"])
print("after:", active_leases(handler))
