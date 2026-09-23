#!/usr/bin/env bash
# Deploy current git HEAD to BOTH hosts (controller + worker).
#
# WHY BOTH HOSTS: the controller was last deployed 2026-09-21 (commit
# 917b394), but the WORKER host (hermes-worker, runs the codex/claude/
# browser/scan/delivery role workers) was never touched -- it is still
# running commit 2b678d9, which is NOT an ancestor of this branch at all.
# It's the same abandoned "findings closure" lineage F-11 already
# investigated and reviewed on the controller's old deploy: 33 files exist
# only in that lineage (all reconciliation/dashboard-streaming/admission
# machinery -- NOT role-worker activity-handling code), and F-11 already
# concluded that subsystem should stay archived, not be adopted. So this is
# expected to be safe, but it genuinely is the first time hermes-worker
# will run any go-status branch code.
#
# This script does NOT run itself with sudo. It calls `sudo` (locally) and
# `ssh -t worker sudo ...` (remotely) only for the specific commands that
# need root, so you'll be prompted for your password on both the local and
# remote machine at those points -- that's expected, not a bug.
#
# Run from the repo root as the hermes user:
#   .go-status/deploy-2026-09-22.sh
set -euo pipefail

if [[ "$(id -un)" != hermes ]]; then
  echo "Run this as the hermes user (no sudo); it calls sudo itself where needed." >&2
  exit 1
fi

REPO_ROOT="/home/hermes/engineering/engineering-control"
RELEASES_ROOT="/opt/engineering-control/releases"
VENVS_ROOT="/opt/engineering-control/venvs"

cd "$REPO_ROOT"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree is not clean; refusing to deploy. Commit or stash first." >&2
  exit 1
fi

HASH=$(git rev-parse HEAD)
echo "=== Deploying commit $HASH ==="

echo ""
echo "=== Pre-flight: full local verification (refuses to deploy if any of this fails) ==="
.venv/bin/ruff check src/ tests/ scripts/
.venv/bin/mypy src/engineering_control
.venv/bin/pytest tests/ -q

RELEASE_DIR="$RELEASES_ROOT/$HASH"
VENV_DIR="$VENVS_ROOT/$HASH"
ARCHIVE="/tmp/engineering-control-deploy-$HASH.tar"

echo ""
echo "=== 1/7: build release archive ==="
git archive "$HASH" -o "$ARCHIVE"
echo "Archive: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"

build_release() {
  # $1 = "controller" or "worker" (just for logging)
  local label="$1"
  echo "-- [$label] extracting release + building venv --"
  sudo mkdir -p "$RELEASE_DIR/source"
  sudo tar -x -f "$ARCHIVE" -C "$RELEASE_DIR/source"
  sudo python3 -m venv "$VENV_DIR"
  sudo "$VENV_DIR/bin/pip" install --no-cache-dir -q -r "$RELEASE_DIR/source/requirements.lock"
  sudo "$VENV_DIR/bin/pip" install --no-cache-dir -q --no-deps "$RELEASE_DIR/source"
  echo "deployed $(date -u -Iseconds)" | sudo tee "$RELEASE_DIR/.release-complete" >/dev/null
  echo "-- [$label] dry-testing imports --"
  sudo "$VENV_DIR/bin/python" -c "
import engineering_control.api.main
import engineering_control.workflows.worker
import engineering_control.workflows.role_worker
import engineering_control.scheduler
import engineering_control.cli
import engineering_control.activities.core
import engineering_control.activities.scan
import engineering_control.activities.browser
import engineering_control.activities.delivery
print('[$label] imports OK')
"
}

echo ""
echo "=== 2/7: build release on controller (this host) ==="
build_release controller

echo ""
echo "=== 3/7: copy archive + build script to worker host and build release there ==="
# Written to a local file and scp'd over (rather than piped into `ssh -t` via a
# heredoc) because piping a heredoc as stdin into `ssh -t` leaves sudo with no
# real controlling terminal to prompt on -- ssh even warns "Pseudo-terminal
# will not be allocated because stdin is not a terminal" and sudo then fails
# with "a terminal is required to authenticate". Running the script as a real
# file over an allocated pty avoids that entirely.
REMOTE_SCRIPT="/tmp/engineering-control-remote-build-$HASH.sh"
cat > "$REMOTE_SCRIPT" <<REMOTE_BUILD
#!/usr/bin/env bash
set -euo pipefail
RELEASE_DIR="$RELEASE_DIR"
VENV_DIR="$VENV_DIR"
ARCHIVE="$ARCHIVE"
echo "-- [worker] extracting release + building venv --"
sudo mkdir -p "\$RELEASE_DIR/source"
sudo tar -x -f "\$ARCHIVE" -C "\$RELEASE_DIR/source"
sudo python3 -m venv "\$VENV_DIR"
sudo "\$VENV_DIR/bin/pip" install --no-cache-dir -q -r "\$RELEASE_DIR/source/requirements.lock"
sudo "\$VENV_DIR/bin/pip" install --no-cache-dir -q --no-deps "\$RELEASE_DIR/source"
echo "deployed \$(date -u -Iseconds)" | sudo tee "\$RELEASE_DIR/.release-complete" >/dev/null
echo "-- [worker] dry-testing imports --"
sudo "\$VENV_DIR/bin/python" -c "
import engineering_control.workflows.role_worker
import engineering_control.activities.core
import engineering_control.activities.scan
import engineering_control.activities.browser
import engineering_control.activities.delivery
print('[worker] imports OK')
"
REMOTE_BUILD
scp "$ARCHIVE" "$REMOTE_SCRIPT" worker:/tmp/
ssh -t worker "bash /tmp/$(basename "$REMOTE_SCRIPT")"
rm -f "$REMOTE_SCRIPT"

echo ""
echo "=== 4/7: swap symlinks on controller ==="
sudo ln -sfn "$RELEASE_DIR/source" /opt/engineering-control/source
sudo ln -sfn "$VENV_DIR" /opt/engineering-control/venv

echo ""
echo "=== 5/7: swap symlinks on worker ==="
ssh -t worker "sudo ln -sfn '$RELEASE_DIR/source' /opt/engineering-control/source && sudo ln -sfn '$VENV_DIR' /opt/engineering-control/venv"

echo ""
echo "=== 5b/7: project contracts (projects.d) on both hosts ==="
# Both hosts read their OWN /etc/engineering-control/projects.d; a contract updated
# on only one host silently disables features on the other (e.g. the toolchain
# step on the worker). Show each host's diff against this release and ask before
# installing, so live-only operator edits are never overwritten silently.
for host in controller worker; do
  for contract in config/projects/*.yaml; do
    name=$(basename "$contract")
    live="/etc/engineering-control/projects.d/$name"
    if [[ "$host" == controller ]]; then
      diff_out=$(sudo diff "$live" "$contract" 2>&1 || true)
    else
      scp -q "$contract" "worker:/tmp/engineering-contract-$name"
      # sudo must prompt on the real terminal, so compare on the worker with a
      # visible prompt and capture only the diff text (via a file, never $(ssh -t ...)).
      ssh -t worker "sudo diff '$live' '/tmp/engineering-contract-$name' > '/tmp/engineering-contract-$name.diff' 2>&1" || true
      diff_out=$(ssh worker "cat '/tmp/engineering-contract-$name.diff'; rm -f '/tmp/engineering-contract-$name.diff'")
    fi
    if [[ -z "$diff_out" ]]; then
      [[ "$host" == worker ]] && ssh worker "rm -f '/tmp/engineering-contract-$name'"
      continue
    fi
    echo "--- $host: $live differs from this release ---"
    echo "$diff_out"
    read -r -p "Install the release copy of $name on $host? [y/N] " answer
    if [[ "$answer" == y || "$answer" == Y ]]; then
      if [[ "$host" == controller ]]; then
        sudo cp "$contract" "$live"
      else
        ssh -t worker "sudo cp '/tmp/engineering-contract-$name' '$live'"
      fi
      echo "installed on $host"
    else
      echo "left unchanged on $host"
    fi
    [[ "$host" == worker ]] && ssh worker "rm -f '/tmp/engineering-contract-$name'"
  done
done

echo "=== 6/7: restart services ==="
echo "-- controller: engineering-api, engineering-temporal-worker --"
sudo systemctl restart engineering-api.service engineering-temporal-worker.service
sleep 3
echo "-- controller: engineering-scheduler (re-registers schedules, e.g. NEW-05's connections-refresh) --"
sudo systemctl restart engineering-scheduler.service
# NEW-27: these two sit on the delivery path but were never restarted by this script,
# so they kept running 2026-09-16/17 code from memory (the ingest service's old
# non-idempotent redactor kept 422-ing precheck evidence after NEW-23 was deployed).
echo "-- controller: engineering-evidence-ingest, engineering-github-worker --"
sudo systemctl restart engineering-evidence-ingest.service engineering-github-worker.service
echo "-- worker: all 5 role workers + release worker --"
ssh -t worker "sudo systemctl restart engineering-worker@codex.service engineering-worker@claude.service engineering-worker@browser.service engineering-worker@scan.service engineering-worker@delivery.service engineering-release-worker.service"

echo ""
echo "=== 7/7: verify ==="
sleep 3
echo "-- controller --"
systemctl is-active engineering-api.service engineering-temporal-worker.service engineering-scheduler.service engineering-evidence-ingest.service engineering-github-worker.service
systemctl show engineering-api.service engineering-temporal-worker.service --property=NRestarts,ActiveState
echo "-- worker --"
ssh worker "systemctl is-active engineering-worker@codex.service engineering-worker@claude.service engineering-worker@browser.service engineering-worker@scan.service engineering-worker@delivery.service engineering-release-worker.service"
echo "-- temporal schedules (should now include engineering-connections-refresh) --"
temporal schedule list --address 127.0.0.1:7233 --namespace engineering-control

echo ""
echo "=== Done. Deployed $HASH to both hosts. ==="
echo "Known, accepted, unaffected-by-this-deploy issue: engineering-watchdog.service has no"
echo "matching console-script entry on this branch and will fail on its next restart -- see F-11."
