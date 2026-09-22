#!/usr/bin/env bash
# One-off resume (2026-09-22): llmwiki campaign cd217170-1a35-464a-9e81-93d36c5a76fa
# is already approved (status=APPROVED_POLICY_PENDING) from an earlier retry tonight,
# but its backlog-start attempt failed at the operations-layer preflight check with
# CLAUDE_AUTH_NOT_READY before any workflow started -- so campaign.status was
# correctly left untouched (nothing destructive happened; this is a safe state to
# retry from, not a bug).
#
# Root-caused why this blocks OTHER projects too: TypedCommandBus enforces a
# GLOBAL (not per-project) maximum_concurrency=1 on existing-backlog campaigns via
# _approve_backlog_campaign's active_campaigns/pending_owner queries in
# db/repository.py, which intentionally have no `project ==` filter -- "root
# reconciler" is a single shared process, by design. So this one stuck
# APPROVED_POLICY_PENDING llmwiki campaign is correctly, intentionally blocking
# musematic's approval too, until it resolves one way or another.
#
# claude-runner's auth is now fixed, so simply retrying backlog-start on this
# SAME already-approved campaign (not a fresh import/approve cycle) should let it
# proceed normally and free up the global slot.
set -euo pipefail

CTL=/opt/engineering-control/venv/bin/engineering-ctl
PROJECT=llmwiki
CAMPAIGN_ID=cd217170-1a35-464a-9e81-93d36c5a76fa
MAXIMUM_RISK=R1
DELIVERY_MODE=source-pr
MAXIMUM_ITEMS=4

echo "== retrying backlog-start on already-approved campaign $CAMPAIGN_ID =="
start_result=$("$CTL" backlog-start "$PROJECT" "$CAMPAIGN_ID" "$MAXIMUM_RISK" "$DELIVERY_MODE" --max-items "$MAXIMUM_ITEMS")
echo "$start_result" | jq .
status=$(echo "$start_result" | jq -r .status)
workflow_id=$(echo "$start_result" | jq -r .workflow_id)
safe_error=$(echo "$start_result" | jq -r .safe_error)
echo "status=$status workflow_id=$workflow_id safe_error=$safe_error"

if [[ "$status" != "STARTED" || -z "$workflow_id" || "$workflow_id" == "null" ]]; then
  echo "backlog-start did not report STARTED -- see safe_error above. Aborting." >&2
  exit 1
fi

echo "Started. workflow_id=$workflow_id"
echo "Re-check status any time with: $CTL backlog-status $workflow_id"
