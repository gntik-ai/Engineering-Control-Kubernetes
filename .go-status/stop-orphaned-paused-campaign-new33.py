"""One-off NEW-33 remediation: end musematic campaign 44e4bfd3, left PAUSED by a loop
workflow that had already finished (the #1026 solve_issue of 2026-09-23 08:22Z), so no
signal can resume it and it still owns the GitHub write policy.

Runs the deployed backlog.record_loop_state step once with STOPPED and the loop's own
workflow id -- the same step the loop runs at its end: marks the loop Run terminal,
sets the campaign STOPPED and queues the policy-disable request for the reconciler.
Refuses to act unless the campaign is still PAUSED.

  sudo systemd-run --quiet --pipe --wait -p User=engineering-control \
    -p EnvironmentFile=/etc/engineering-control/env/control.env \
    /opt/engineering-control/venv/bin/python - < stop-orphaned-paused-campaign-new33.py
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
    "25a0d010-320d-4774-b7b6-0cf003755bf5"
)

handler = EngineeringActivityHandler(get_settings())
with handler.sessions() as session:
    campaign = session.get(db.GitHubBacklogCampaign, UUID(CAMPAIGN_ID))
    status = campaign.status if campaign is not None else None
print("campaign before:", status)
if status != "PAUSED":
    raise SystemExit("campaign is not PAUSED; nothing done")
result = asyncio.run(
    handler.handle(
        "backlog.record_loop_state",
        {
            "project": "musematic",
            "backlog_campaign_id": CAMPAIGN_ID,
            "workflow_id": LOOP_WORKFLOW_ID,
            "loop_status": "STOPPED",
            "global_campaign_id": None,
        },
    )
)
print("record_loop_state:", result.get("status"))
with handler.sessions() as session:
    campaign = session.get(db.GitHubBacklogCampaign, UUID(CAMPAIGN_ID))
    run = session.query(db.Run).filter(db.Run.workflow_id == LOOP_WORKFLOW_ID).one_or_none()
    print("campaign after:", campaign.status if campaign else None)
    print("loop run after:", run.status if run else None)
