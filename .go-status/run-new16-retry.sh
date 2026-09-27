#!/usr/bin/env bash
# NEW-16 retry: one clean, uninterrupted backlog-import -> plan -> approval-candidate ->
# approve -> start sequence against a BRAND NEW llmwiki campaign, run only after the
# currently-stuck campaign's approval naturally expires (~2026-09-21 22:32 UTC).
#
# Do not reuse 3a363acd-46fc-4421-8165-d366c584677f or c405b4d7-9cb6-4b5d-a8af-4bd77b83107c --
# both have permanently used their one solve_backlog attempt (see .go-status/findings.json,
# NEW-16). This script always creates a fresh campaign via backlog-import.
#
# Usage: ./run-new16-retry.sh [--force]
#   --force skips the ~22:32 UTC clock check (only use if you know the prior campaign's
#   approval has already expired for some other confirmed reason).
set -euo pipefail

CTL=/opt/engineering-control/venv/bin/engineering-ctl
PROJECT=llmwiki
IMPORT_FILTER=all
DELIVERY_MODE=source-pr
MAXIMUM_RISK=R1
GATE_EPOCH=$(date -u -d "2026-09-21T22:32:00Z" +%s)

if [[ "${1:-}" != "--force" ]]; then
  now_epoch=$(date -u +%s)
  if (( now_epoch < GATE_EPOCH )); then
    echo "Refusing to run: it is $(date -u), the NEW-16 TTL window opens at 2026-09-21T22:32:00Z." >&2
    echo "Pass --force only if you have independently confirmed the prior campaign's approval already expired." >&2
    exit 1
  fi
fi

echo "== 1/5 backlog-import =="
import_result=$("$CTL" backlog-import "$PROJECT" "$IMPORT_FILTER" --max-items 5)
echo "$import_result" | jq .
campaign_id=$(echo "$import_result" | jq -r .backlog_campaign_id)
if [[ -z "$campaign_id" || "$campaign_id" == "null" ]]; then
  echo "backlog-import did not return a backlog_campaign_id; aborting." >&2
  exit 1
fi
echo "campaign_id=$campaign_id"

echo "== 2/5 backlog-plan =="
"$CTL" backlog-plan "$PROJECT" "$campaign_id" | jq .

echo "== 3/5 backlog-approval-candidate =="
candidate_result=$("$CTL" backlog-approval-candidate "$PROJECT" "$campaign_id" "$DELIVERY_MODE" "$MAXIMUM_RISK" --max-items 1)
echo "$candidate_result" | jq .
approval_binding=$(echo "$candidate_result" | jq -r .approval_binding)
if [[ -z "$approval_binding" || "$approval_binding" == "null" ]]; then
  echo "backlog-approval-candidate did not return an approval_binding; aborting." >&2
  exit 1
fi
echo "approval_binding=$approval_binding"

echo "== 4/5 backlog-approve =="
"$CTL" backlog-approve "$PROJECT" "$campaign_id" "$approval_binding" APPROVE_EXISTING_GITHUB_BACKLOG_LOOP | jq .

echo "== 5/5 backlog-start =="
start_result=$("$CTL" backlog-start "$PROJECT" "$campaign_id" "$MAXIMUM_RISK" "$DELIVERY_MODE" --max-items 1)
echo "$start_result" | jq .
workflow_id=$(echo "$start_result" | jq -r .workflow_id)
echo "workflow_id=$workflow_id"

echo
echo "Started. Campaign $campaign_id / workflow $workflow_id is now running."
echo "Check status with: $CTL backlog-status $workflow_id"
echo "Checking gntik-ai/llmwiki for a real draft PR (may need a minute or two to appear)..."
gh pr list --repo gntik-ai/llmwiki --state open --json number,title,url,createdAt --limit 10 || true
