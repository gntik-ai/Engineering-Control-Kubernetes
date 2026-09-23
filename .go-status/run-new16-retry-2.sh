#!/usr/bin/env bash
# NEW-16 retry #2, post-deploy (2026-09-22): both blocking bugs (missing
# backlog.register_run dispatch entry, CLAUDE_HEALTH_STALE scheduling
# mismatch) are now fixed and deployed to both hosts. This script fixes the
# two remaining gaps found during retry #1's live debugging:
#   1. Waits for backlog-plan's workflow to actually reach COMPLETED before
#      calling backlog-approval-candidate (retry #1's script called it
#      immediately after the async ACCEPTED response and hit a 409).
#   2. Proactively triggers a quota refresh right before backlog-start,
#      rather than waiting to hit CLAUDE_HEALTH_STALE reactively.
#
# Always imports a brand-new campaign -- defef39e, 3a363acd, and c405b4d7
# have all permanently used their one solve_backlog attempt (see NEW-16).
#
# Usage: ./run-new16-retry-2.sh [project] [max-items]
#   project: falcone, musematic, or llmwiki (default: llmwiki)
#   max-items: how many eligible issues to let the campaign attempt in
#     sequence (default: 1). Each attempted issue that fails reproduction
#     verification is CURRENT_REVALIDATION_BLOCKED but -- since
#     continue_after_blocked_issue defaults true -- the loop moves on to the
#     next one instead of stopping, so a higher value gives a real shot at
#     landing on an issue that actually passes verification and produces a
#     PR. Only matters if the campaign has more than 1 eligible issue.
set -euo pipefail

CTL=/opt/engineering-control/venv/bin/engineering-ctl
PROJECT="${1:-llmwiki}"
MAXIMUM_ITEMS="${2:-1}"
IMPORT_FILTER=all
DELIVERY_MODE=source-pr
MAXIMUM_RISK=R1
PLAN_POLL_TIMEOUT_SECONDS=300
PLAN_POLL_INTERVAL_SECONDS=10

case "$PROJECT" in
  falcone) SOURCE_REPO="gntik-ai/falcone" ;;
  musematic) SOURCE_REPO="gntik-ai/musematic" ;;
  llmwiki) SOURCE_REPO="gntik-ai/llmwiki" ;;
  *)
    echo "Unknown project: $PROJECT (expected falcone, musematic, or llmwiki)" >&2
    exit 1
    ;;
esac

echo "== 1/5 backlog-import =="
import_result=$("$CTL" backlog-import "$PROJECT" "$IMPORT_FILTER" --max-items 5)
echo "$import_result" | jq .
campaign_id=$(echo "$import_result" | jq -r .backlog_campaign_id)
import_workflow_id=$(echo "$import_result" | jq -r .workflow_id)
if [[ -z "$campaign_id" || "$campaign_id" == "null" ]]; then
  echo "backlog-import did not return a backlog_campaign_id; aborting." >&2
  exit 1
fi
echo "campaign_id=$campaign_id"

echo "== waiting for backlog-import's workflow to complete =="
elapsed=0
while true; do
  status=$("$CTL" backlog-status "$import_workflow_id" | jq -r .status)
  echo "  import workflow status: $status (${elapsed}s elapsed)"
  if [[ "$status" == "COMPLETED" ]]; then
    break
  fi
  if [[ "$status" == "FAILED" || "$status" == "TERMINATED" || "$status" == "CANCELED" ]]; then
    echo "backlog-import's workflow ended in $status; aborting." >&2
    exit 1
  fi
  if (( elapsed >= PLAN_POLL_TIMEOUT_SECONDS )); then
    echo "Timed out waiting for backlog-import to complete." >&2
    exit 1
  fi
  sleep "$PLAN_POLL_INTERVAL_SECONDS"
  elapsed=$((elapsed + PLAN_POLL_INTERVAL_SECONDS))
done

echo "== 2/5 backlog-plan =="
plan_result=$("$CTL" backlog-plan "$PROJECT" "$campaign_id")
echo "$plan_result" | jq .
plan_workflow_id=$(echo "$plan_result" | jq -r .workflow_id)

echo "== waiting for backlog-plan's workflow to reach COMPLETED (this is the fix for retry #1's race) =="
elapsed=0
while true; do
  status=$("$CTL" backlog-status "$plan_workflow_id" | jq -r .status)
  echo "  plan workflow status: $status (${elapsed}s elapsed)"
  if [[ "$status" == "COMPLETED" ]]; then
    break
  fi
  if [[ "$status" == "FAILED" || "$status" == "TERMINATED" || "$status" == "CANCELED" ]]; then
    echo "backlog-plan's workflow ended in $status; aborting." >&2
    exit 1
  fi
  if (( elapsed >= PLAN_POLL_TIMEOUT_SECONDS )); then
    echo "Timed out waiting for backlog-plan to complete." >&2
    exit 1
  fi
  sleep "$PLAN_POLL_INTERVAL_SECONDS"
  elapsed=$((elapsed + PLAN_POLL_INTERVAL_SECONDS))
done

echo "== 3/5 backlog-approval-candidate =="
candidate_result=$("$CTL" backlog-approval-candidate "$PROJECT" "$campaign_id" "$DELIVERY_MODE" "$MAXIMUM_RISK" --max-items "$MAXIMUM_ITEMS")
echo "$candidate_result" | jq .
approval_binding=$(echo "$candidate_result" | jq -r .approval_binding)
if [[ -z "$approval_binding" || "$approval_binding" == "null" ]]; then
  echo "backlog-approval-candidate did not return an approval_binding; aborting." >&2
  exit 1
fi
echo "approval_binding=$approval_binding"

echo "== 4/5 backlog-approve =="
"$CTL" backlog-approve "$PROJECT" "$campaign_id" "$approval_binding" APPROVE_EXISTING_GITHUB_BACKLOG_LOOP | jq .

echo "== waiting for the policy reconciler to move the campaign to APPROVED =="
# backlog-start also accepts APPROVED_POLICY_PENDING and flips it to RUNNING,
# after which the reconciler rejects the request ("campaign state is invalid")
# and the GitHub write policy is never enabled -- so never start before this.
campaign_status() {
  /opt/engineering-control/venv/bin/python -c "
from engineering_control.cli import APIClient, parser
a = parser().parse_args(['campaigns'])
print(APIClient(a.api_url, a.token_file).request('GET', '/v1/backlogs/$1').get('status'))"
}
elapsed=0
while true; do
  cstatus=$(campaign_status "$campaign_id")
  echo "  campaign status: $cstatus (${elapsed}s elapsed)"
  if [[ "$cstatus" == "APPROVED" || "$cstatus" == "APPROVED_INVENTORY" ]]; then
    break
  fi
  if (( elapsed >= 60 )); then
    echo "Campaign still $cstatus after 60s: engineering-github-policy-reconciler did not" >&2
    echo "process activate.json. Check: systemctl is-failed engineering-github-policy-reconciler.service" >&2
    exit 1
  fi
  sleep 5
  elapsed=$((elapsed + 5))
done

echo "== proactively refreshing quota snapshot before backlog-start (fix for NEW-17 / CLAUDE_HEALTH_STALE) =="
temporal schedule trigger --address 127.0.0.1:7233 --namespace engineering-control --schedule-id engineering-quota-refresh
sleep 8

echo "== 5/5 backlog-start =="
start_result=$("$CTL" backlog-start "$PROJECT" "$campaign_id" "$MAXIMUM_RISK" "$DELIVERY_MODE" --max-items "$MAXIMUM_ITEMS")
echo "$start_result" | jq .
status=$(echo "$start_result" | jq -r .status)
workflow_id=$(echo "$start_result" | jq -r .workflow_id)
echo "status=$status workflow_id=$workflow_id"

if [[ "$status" != "STARTED" || -z "$workflow_id" || "$workflow_id" == "null" ]]; then
  echo "backlog-start did not report STARTED with a workflow_id -- see safe_error above. Aborting before polling." >&2
  exit 1
fi

echo "== waiting a bit, then checking the workflow and for a PR =="
sleep 15
"$CTL" backlog-status "$workflow_id" | jq .
echo "Checking $SOURCE_REPO for a real draft PR (may need more time to appear)..."
gh pr list --repo "$SOURCE_REPO" --state open --json number,title,url,createdAt --limit 10

echo
echo "Started. Campaign $campaign_id / workflow $workflow_id."
echo "Re-check status any time with: $CTL backlog-status $workflow_id"
