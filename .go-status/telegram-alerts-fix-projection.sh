#!/usr/bin/env bash
# Stop the watchdog's 65-minute Telegram alert for `projection-reconciliation` (run on the
# CONTROLLER as hermes, not root; asks for the sudo password once).
#
# Why: control.env has ENGINEERING_PROJECTION_RECONCILIATION_ENABLED=true, so the watchdog
# (pinned to the 2b678d9 venv) probes the projection reconciler. That reconciler only
# existed in the abandoned 2b678d9 lineage; its last observation is from 2026-09-21, so the
# probe is permanently FAILED_OR_STALE and re-alerts every watchdog_repeat_seconds (3900 s).
# No current code reads the flag (grep src/ -> nothing), so turning it off only removes
# that probe. The probe's old state entry is simply no longer evaluated (no recovery
# message is sent for a probe that is not collected).
#
#   cd ~/engineering/engineering-control && .go-status/telegram-alerts-fix-projection.sh
set -euo pipefail
trap 'echo "FAILED at line $LINENO (exit $?)" >&2' ERR

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this as hermes, not root (it calls sudo itself)." >&2
  exit 1
fi

ENV_FILE=/etc/engineering-control/env/control.env
KEY=ENGINEERING_PROJECTION_RECONCILIATION_ENABLED

echo "=== 1/3 set ${KEY}=false in ${ENV_FILE} (backup kept next to it) ==="
sudo bash -c "
  set -euo pipefail
  cp -p '${ENV_FILE}' '${ENV_FILE}.bak-projection-$(date -u +%Y%m%dT%H%M%SZ)'
  if grep -q '^${KEY}=' '${ENV_FILE}'; then
    sed -i 's|^${KEY}=.*|${KEY}=false|' '${ENV_FILE}'
  else
    printf '%s\n' '${KEY}=false' >> '${ENV_FILE}'
  fi
  grep '^${KEY}=' '${ENV_FILE}'
"

echo "=== 2/3 restart engineering-watchdog only ==="
sudo systemctl restart engineering-watchdog
systemctl is-active engineering-watchdog
systemctl show engineering-watchdog -p ExecStart --value | grep -o 'venvs/[0-9a-f]*' | head -1

echo "=== 3/3 wait for two probe cycles (~130 s) and show the result ==="
sleep 130
sudo journalctl -u engineering-watchdog --since "-3min" --no-pager -o cat \
  | grep operational_watchdog_completed | tail -2
echo "Expected: probe_count 16 (was 17), unhealthy_count 0, delivered_count 0."
