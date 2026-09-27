#!/usr/bin/env bash
# NEW-16 retry, resumed: campaign defef39e-b0ae-4a36-8863-dd59f08e960c already has a
# COMPLETED backlog-import and a COMPLETED backlog-plan (confirmed via
# `engineering-ctl backlog-status <workflow_id>` on 2026-09-22). The original
# .go-status/run-new16-retry.sh failed at step 3/5 (backlog-approval-candidate, 409
# Conflict) because it calls that step immediately after backlog-plan's async
# "ACCEPTED" response, with no wait for the plan workflow to actually finish writing
# campaign.plan_hash back to the DB -- a race in the script, not a recurrence of
# NEW-16's own bug (which lives in backlog-start's operations-dispatch path, never
# reached by the failed attempt) and not ownership contention.
#
# This script resumes from step 3 against that SAME campaign instead of importing a
# new one -- safe to do because approval-candidate/approve are plain, uncached DB
# calls (see db/repository.py's _prepare_backlog_approval/_approve_backlog_campaign),
# unlike backlog-start, which is the one step NEW-16 says can only ever be attempted
# once per campaign.
#
# Usage: ./run-new16-retry-resume.sh
set -euo pipefail

CTL=/opt/engineering-control/venv/bin/engineering-ctl
PROJECT=llmwiki
CAMPAIGN=defef39e-b0ae-4a36-8863-dd59f08e960c
DELIVERY_MODE=source-pr
MAXIMUM_RISK=R1

echo "== 3/5 backlog-approval-candidate =="
candidate_result=$("$CTL" backlog-approval-candidate "$PROJECT" "$CAMPAIGN" "$DELIVERY_MODE" "$MAXIMUM_RISK" --max-items 1)
echo "$candidate_result" | jq .
approval_binding=$(echo "$candidate_result" | jq -r .approval_binding)
if [[ -z "$approval_binding" || "$approval_binding" == "null" ]]; then
  echo "backlog-approval-candidate did not return an approval_binding; aborting." >&2
  exit 1
fi
echo "approval_binding=$approval_binding"

echo "== 4/5 backlog-approve =="
"$CTL" backlog-approve "$PROJECT" "$CAMPAIGN" "$approval_binding" APPROVE_EXISTING_GITHUB_BACKLOG_LOOP | jq .

echo "== 5/5 backlog-start =="
start_result=$("$CTL" backlog-start "$PROJECT" "$CAMPAIGN" "$MAXIMUM_RISK" "$DELIVERY_MODE" --max-items 1)
echo "$start_result" | jq .
workflow_id=$(echo "$start_result" | jq -r .workflow_id)
echo "workflow_id=$workflow_id"

echo
echo "Started. Campaign $CAMPAIGN / workflow $workflow_id is now running."
echo "Check status with: $CTL backlog-status $workflow_id"
echo "Checking gntik-ai/llmwiki for a real draft PR (may need a minute or two to appear)..."
gh pr list --repo gntik-ai/llmwiki --state open --json number,title,url,createdAt --limit 10 || true
