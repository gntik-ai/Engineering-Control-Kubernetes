"""Read-only: classify every ChangeSet worktree on the worker for NEW-85 cleanup design.

Prints one line per <repo>/<change_set_id> worktree directory: ChangeSet state, issue,
active lease holders, and the latest run status/finish time. Changes nothing.

Run on the CONTROLLER (it needs the database); the directory list comes from the worker:
  ssh -t worker "sudo bash -c 'ls -d /srv/engineering/worktrees/*/*'" \
    | tr -d '\r' > /tmp/worktrees.txt
  chmod 644 /tmp/worktrees.txt
  sudo systemd-run --quiet --pipe --wait -p User=engineering-control \
    -p EnvironmentFile=/etc/engineering-control/env/control.env \
    /opt/engineering-control/venv/bin/python - /tmp/worktrees.txt < .go-status/worktree-inventory.py
"""

from __future__ import annotations

import sys
from pathlib import PurePosixPath
from uuid import UUID

from sqlalchemy import select

from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.config import get_settings
from engineering_control.db import models as db

with open(sys.argv[1], encoding="utf-8") as listing:
    paths = [line.strip() for line in listing if line.strip().startswith("/srv/")]
handler = EngineeringActivityHandler(get_settings())
with handler.sessions() as session:
    for raw in sorted(paths):
        path = PurePosixPath(raw)
        try:
            change_set_id = UUID(path.name)
        except ValueError:
            print(f"{path.parent.name}/{path.name} NOT_A_CHANGESET_DIR")
            continue
        change_set = session.get(db.ChangeSet, change_set_id)
        if change_set is None:
            print(f"{path.parent.name}/{change_set_id} NO_CHANGESET_ROW")
            continue
        active = [
            lease.repository
            for lease in session.scalars(
                select(db.WorktreeLease).where(
                    db.WorktreeLease.change_set_id == change_set_id,
                    db.WorktreeLease.released_at.is_(None),
                )
            )
        ]
        run = session.scalar(
            select(db.Run)
            .where(db.Run.change_set_id == change_set_id)
            .order_by(db.Run.created_at.desc())
            .limit(1)
        )
        issue = session.get(db.Issue, change_set.issue_id) if change_set.issue_id else None
        print(
            f"{path.parent.name}/{change_set_id} state={change_set.state} "
            f"issue={issue.repository if issue else '-'}#{issue.number if issue else '-'} "
            f"active_leases={','.join(active) or '-'} "
            f"last_run={run.status if run else '-'}@"
            f"{run.finished_at.date() if run and run.finished_at else '-'}"
        )
