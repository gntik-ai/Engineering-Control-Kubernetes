#!/usr/bin/env python3
"""One-off, operator-approved correction for NEW-16's second sub-bug (2026-09-22).

Campaign defef39e-b0ae-4a36-8863-dd59f08e960c is stuck at status=RUNNING because
its backlog.register_run activity call crashed (a missing ControlActivities.
_allowed_steps entry, fixed in commit f402e269...) OUTSIDE the workflow's
try/except at the time, so the cleanup that resets campaign.status (backlog.
record_loop_state) never ran. That gap is now fixed in git too (register_run's
call moved inside the try block) but is not deployed, and even once it is,
that fix cannot retroactively repair this ALREADY-terminated workflow's
campaign row -- there is no live Temporal execution left to trigger it.

This script does EXACTLY what the fixed code's exception handler would have
done had it run at the time: sets campaign.status = "FAILED" (the same value
backlog.record_loop_state's failure path uses) and records who approved this
correction and why, mirroring the audit trail backlog-release-ownership
already writes for its own voluntary releases. It does NOT touch any other
row, does NOT call any GitHub/Temporal API, and does NOT use raw SQL --
it goes through the same SQLAlchemy model (GitHubBacklogCampaign) and session
pattern (SQLMetadataRepository) the application itself uses everywhere else.

Refuses to run unless the campaign's CURRENT status is exactly "RUNNING" --
if something already changed it (e.g. its TTL naturally expired, or someone
else already corrected it), this script does nothing rather than guess.

Usage (needs DB access -- run as a user/environment that can see
ENGINEERING_DATABASE_URL, e.g. the same env the engineering-control services
use):
  sudo -u engineering-control bash -c \
    'set -a; source /etc/engineering-control/env/control.env; set +a; \
     /opt/engineering-control/venv/bin/python \
     .go-status/correct-defef39e-stuck-running.py'
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from uuid import UUID

CAMPAIGN_ID = UUID("defef39e-b0ae-4a36-8863-dd59f08e960c")
APPROVED_BY = "operator, 2026-09-22, via .go-status ledger (NEW-16 second sub-bug)"


def main() -> int:
    from engineering_control.config import get_settings
    from engineering_control.db.models import GitHubBacklogCampaign
    from engineering_control.db.repository import SQLMetadataRepository

    settings = get_settings()
    repository = SQLMetadataRepository(settings.database_url.get_secret_value())

    with repository.sessions.begin() as session:
        campaign = session.get(GitHubBacklogCampaign, CAMPAIGN_ID)
        if campaign is None:
            print(f"No campaign found with id {CAMPAIGN_ID}; nothing to do.", file=sys.stderr)
            return 1

        print("BEFORE:")
        print(f"  id={campaign.id}")
        print(f"  project={campaign.project}")
        print(f"  status={campaign.status}")
        print(f"  approval_json={campaign.approval_json}")

        if campaign.status != "RUNNING":
            print(
                f"\nRefusing to act: status is {campaign.status!r}, not 'RUNNING'. "
                "Something else already changed this (TTL expiry, an earlier "
                "correction, etc.) -- doing nothing rather than guess.",
                file=sys.stderr,
            )
            return 1

        campaign.status = "FAILED"
        campaign.approval_json = {
            **campaign.approval_json,
            "manual_correction": {
                "reason": (
                    "backlog.register_run crashed outside the workflow's "
                    "try/except (missing ControlActivities._allowed_steps entry, "
                    "since fixed) -- campaign.status was set to RUNNING by "
                    "backlog.load_execution moments earlier and nothing ever "
                    "reset it, permanently blocking GitHub write-policy "
                    "ownership for every other campaign with no exposed "
                    "recovery path. This sets the same terminal status "
                    "(FAILED) the fixed code's own exception handler would "
                    "have set, had it run at the time."
                ),
                "approved_by": APPROVED_BY,
                "corrected_at": datetime.now(UTC).isoformat(),
                "finding": "NEW-16",
            },
        }

    with repository.sessions.begin() as session:
        campaign = session.get(GitHubBacklogCampaign, CAMPAIGN_ID)
        assert campaign is not None
        print("\nAFTER:")
        print(f"  status={campaign.status}")
        print(f"  approval_json={campaign.approval_json}")

    print("\nDone. This campaign no longer counts as an active GitHub write-policy owner.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
