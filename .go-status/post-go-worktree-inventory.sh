#!/usr/bin/env bash
# Post-GO cleanup, step 2 (run on the CONTROLLER as hermes, not root):
#   1. removes the last stale NEW-49 baseline checkout on the worker (707ba5d; main is
#      now f7b0a97 and Hermes recreates a baseline when it needs one);
#   2. lists every ChangeSet worktree on the worker and classifies it read-only
#      (ChangeSet state, issue, active leases, latest run) for the NEW-85 cleanup design.
# You will be asked for the sudo password on the worker and on the controller.
#
#   cd ~/engineering/engineering-control && .go-status/post-go-worktree-inventory.sh
set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this as hermes, not root (it calls sudo itself where needed)." >&2
  exit 1
fi
cd "$(dirname "$0")/.."

BASELINE=/srv/engineering/cache/scan/toolchain/baselines/musematic/707ba5dddfc45bad71841d3a85cecc3ae96334db
LISTING=/tmp/worktrees.txt
REPORT="$HOME/engineering/archives/worktree-inventory-$(date -u +%Y%m%dT%H%M%SZ).txt"

echo "=== 1/3 worker: remove the stale baseline checkout and list worktrees ==="
ssh -t worker "sudo bash -c 'rm -rf $BASELINE; ls -d /srv/engineering/worktrees/*/* ; df -h / | tail -1'" \
  | tr -d '\r' > "$LISTING.raw"
grep '^/srv/' "$LISTING.raw" > "$LISTING" || true
grep -v '^/srv/' "$LISTING.raw" | grep -v '^\[sudo' || true
chmod 644 "$LISTING"
echo "worktree directories found: $(wc -l < "$LISTING")"

echo "=== 2/3 controller: classify each worktree (read-only) ==="
mkdir -p "$(dirname "$REPORT")"
sudo systemd-run --quiet --pipe --wait -p User=engineering-control \
  -p EnvironmentFile=/etc/engineering-control/env/control.env \
  /opt/engineering-control/venv/bin/python - "$LISTING" \
  < .go-status/worktree-inventory.py | tee "$REPORT"

echo "=== 3/3 summary ==="
awk '{print $2}' "$REPORT" | sort | uniq -c | sort -rn
echo "Report saved to $REPORT"
rm -f "$LISTING.raw"
