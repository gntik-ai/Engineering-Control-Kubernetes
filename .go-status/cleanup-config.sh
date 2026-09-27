#!/usr/bin/env bash
# NEW-85 worktree cleanup configuration (run on the CONTROLLER as hermes, not root).
#
#   .go-status/cleanup-config.sh retention   # phase 1: 3-day retention, still dry-run only
#   .go-status/cleanup-config.sh enable      # phase 2: deletion on (after reviewing the dry run)
#   .go-status/cleanup-config.sh disable     # back to dry-run only
#
# Settings are written idempotently to /etc/engineering-control/env/control.env (controller:
# scheduler decides dry-run vs execute, temporal worker runs the lease rules) and
# /etc/engineering-control/env/scan-worker.env (worker: the scan step that deletes), then
# only the services that read them are restarted. No workflow is interrupted by design:
# check that no delivery loop is running before phase 2.
set -euo pipefail
trap 'echo "FAILED at line $LINENO (exit $?)" >&2' ERR

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this as hermes, not root (it calls sudo itself)." >&2
  exit 1
fi

case "${1:-}" in
  retention) SETTINGS=("ENGINEERING_CLEANUP_RETENTION_DAYS=3") ;;
  enable) SETTINGS=("ENGINEERING_CLEANUP_RETENTION_DAYS=3" "ENGINEERING_CLEANUP_EXECUTION_ENABLED=true") ;;
  disable) SETTINGS=("ENGINEERING_CLEANUP_EXECUTION_ENABLED=false") ;;
  *) echo "usage: $0 retention|enable|disable" >&2; exit 2 ;;
esac

# Replace KEY=... if present, otherwise append; keeps the file's owner and mode.
SET_KV='f="$1"; shift; for kv in "$@"; do k="${kv%%=*}"; if grep -q "^${k}=" "$f"; then sed -i "s|^${k}=.*|${kv}|" "$f"; else printf "%s\n" "$kv" >> "$f"; fi; done; grep -E "^ENGINEERING_CLEANUP_" "$f"'

echo "=== controller: /etc/engineering-control/env/control.env ==="
sudo bash -c "$SET_KV" _ /etc/engineering-control/env/control.env "${SETTINGS[@]}"
sudo systemctl restart engineering-scheduler engineering-temporal-worker
systemctl is-active engineering-scheduler engineering-temporal-worker

echo "=== worker: /etc/engineering-control/env/scan-worker.env ==="
QUOTED=$(printf '%q ' "${SETTINGS[@]}")
ssh -t worker "sudo touch /etc/engineering-control/env/scan-worker.env && sudo bash -c '$SET_KV' _ /etc/engineering-control/env/scan-worker.env $QUOTED && sudo systemctl restart engineering-worker@scan && systemctl is-active engineering-worker@scan"

echo "Done: ${SETTINGS[*]}"
