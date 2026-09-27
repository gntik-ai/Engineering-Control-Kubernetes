"""One-off NEW-33b remediation: the #1026 loop (ae4214cc) is alive and paused at
AWAITING_OPERATOR, but it recorded its pause before NEW-33b, so its loop Run still
reads RUNNING and engineering-ctl backlog-retry is refused (WORKFLOW_IS_NOT_RETRYABLE).

Runs the deployed backlog.record_loop_state step once with PAUSED and the loop's
workflow id -- exactly what the fixed loop now records itself when it pauses.
Refuses to act unless the campaign is PAUSED and the loop Run is RUNNING.

  sudo systemd-run --quiet --pipe --wait -p User=engineering-control \
    -p EnvironmentFile=/etc/engineering-control/env/control.env \
    /opt/engineering-control/venv/bin/python - < mark-paused-loop-run-new33b.py
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.config import get_settings
from engineering_control.db import models as db

CAMPAIGN_ID = "44e4bfd3-133c-490e-84e9-b00e28390c5c"
LOOP_WORKFLOW_ID = (
    "backlog-loop-musematic-44e4bfd3-133c-490e-84e9-b00e28390c5c-"
    "ae4214cc-2026-4896-a330-b1daf7d8d252"
)


def state(handler: EngineeringActivityHandler) -> tuple[str | None, str | None]:
    with handler.sessions() as session:
        campaign = session.get(db.GitHubBacklogCampaign, UUID(CAMPAIGN_ID))
        run = session.query(db.Run).filter(db.Run.workflow_id == LOOP_WORKFLOW_ID).one_or_none()
        return (campaign.status if campaign else None, run.status if run else None)


handler = EngineeringActivityHandler(get_settings())
before = state(handler)
print("campaign, loop run before:", before)
if before != ("PAUSED", "RUNNING"):
    raise SystemExit("unexpected state; nothing done")
asyncio.run(
    handler.handle(
        "backlog.record_loop_state",
        {
            "project": "musematic",
            "backlog_campaign_id": CAMPAIGN_ID,
            "workflow_id": LOOP_WORKFLOW_ID,
            "loop_status": "PAUSED",
            "blocker": "FAILED",
            "global_campaign_id": None,
        },
    )
)
print("campaign, loop run after:", state(handler))
