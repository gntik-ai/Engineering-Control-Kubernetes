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
CONTROL_ENV="/etc/engineering-control/env/control.env"

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
# /tmp is a RAM-backed tmpfs on the controller. pytest's default basetemp
# (/tmp/pytest-of-<user>) keeps the last three full runs, about 0.5 GB each, which
# starved Hermes of memory on 2026-10-02/03. Use a private basetemp and delete it.
PYTEST_BASETEMP=$(mktemp -d /tmp/engineering-control-preflight.XXXXXX)
trap 'rm -rf "$PYTEST_BASETEMP"' EXIT
.venv/bin/pytest tests/ -q -p no:cacheprovider --basetemp="$PYTEST_BASETEMP"
rm -rf "$PYTEST_BASETEMP"

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

# NEW-118: run the NEW release's alembic exactly as engineering-migrate.service would
# (same user, EnvironmentFile and working directory), without touching the symlinks.
new_release_alembic() {
  sudo systemd-run --quiet --wait --pipe --collect \
    --uid=engineering-control --gid=engineering-control \
    --property=EnvironmentFile="$CONTROL_ENV" \
    --working-directory="$RELEASE_DIR/source" \
    "$VENV_DIR/bin/alembic" -c "$RELEASE_DIR/source/alembic.ini" "$@"
}

# Revision IDs from `alembic current` / `alembic heads` output ("0019 (head)").
alembic_revisions() {
  grep -Eo '^[A-Za-z0-9_]+( \(head\)| \(mergepoint\)| \(branchpoint\))*$' | awk '{print $1}' || true
}

echo ""
echo "=== 2b/7: migration check on controller (before any symlink swap) ==="
# 2026-10-01: the DB was at 0018 from an unmerged branch; the migrate unit failed after
# the swap and stopped every unit that Requires= it. Refuse before swapping anything if
# the DB revision is not part of this release's migration tree.
if ! DB_CURRENT_OUT=$(new_release_alembic current); then
  echo "ABORT: the new release's alembic cannot resolve the database revision (it is not" >&2
  echo "in this release's migration tree, or the database is unreachable). Nothing swapped." >&2
  exit 1
fi
mapfile -t DB_REVISIONS < <(alembic_revisions <<<"$DB_CURRENT_OUT")
if [[ "${#DB_REVISIONS[@]}" -ne 1 ]]; then
  echo "ABORT: expected exactly one database revision, got ${#DB_REVISIONS[@]}:" >&2
  echo "$DB_CURRENT_OUT" >&2
  exit 1
fi
DB_REVISION="${DB_REVISIONS[0]}"
if ! RELEASE_HEADS_OUT=$(new_release_alembic heads); then
  echo "ABORT: the new release's alembic heads could not be read. Nothing swapped." >&2
  exit 1
fi
mapfile -t RELEASE_HEADS < <(alembic_revisions <<<"$RELEASE_HEADS_OUT")
if [[ "${#RELEASE_HEADS[@]}" -lt 1 ]]; then
  echo "ABORT: the new release has no alembic head. Nothing swapped." >&2
  exit 1
fi
MIGRATION_NEEDED=yes
for head in "${RELEASE_HEADS[@]}"; do
  [[ "$head" == "$DB_REVISION" ]] && MIGRATION_NEEDED=no
done
echo "Database revision: $DB_REVISION (known to this release)"
echo "Release heads: ${RELEASE_HEADS[*]}"
echo "Migration needed: $MIGRATION_NEEDED"

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

echo ""
echo "=== 5c/7: database migration on controller (only if needed) ==="
# Every unit that Requires= engineering-migrate (api, temporal-worker, evidence-ingest,
# dashboard, notifier, watchdog on the live host) is stopped when the migrate unit is
# restarted, so each one is started again explicitly afterwards.
mapfile -t MIGRATE_DEPENDENTS < <(
  systemctl list-dependencies --reverse engineering-migrate.service --plain --no-pager \
    | sed -e 's/^[[:space:]]*//' \
    | grep -E '^engineering-[A-Za-z0-9@_.-]+\.service$' \
    | grep -vx 'engineering-migrate.service' || true
)
echo "Requires=engineering-migrate dependents: ${MIGRATE_DEPENDENTS[*]:-none}"
if [[ "$MIGRATION_NEEDED" == yes ]]; then
  echo "-- controller: engineering-migrate ($DB_REVISION -> ${RELEASE_HEADS[*]}) --"
  sudo systemctl restart engineering-migrate.service || true
  MIGRATE_RESULT=$(systemctl show engineering-migrate.service --property=Result --value)
  if [[ "$MIGRATE_RESULT" != success ]]; then
    echo "ABORT: engineering-migrate finished with Result=$MIGRATE_RESULT; its dependents" >&2
    echo "are stopped. Inspect: journalctl -u engineering-migrate.service -n 100" >&2
    exit 1
  fi
  echo "engineering-migrate: Result=success"
  for unit in "${MIGRATE_DEPENDENTS[@]}"; do
    echo "-- controller: start $unit (stopped with engineering-migrate) --"
    sudo systemctl start "$unit"
  done
else
  echo "Database already at a release head; engineering-migrate not restarted."
fi

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
# NEW-118: both kept running the previous release because nothing restarted them.
echo "-- controller: engineering-notifier, engineering-dashboard --"
sudo systemctl restart engineering-notifier.service engineering-dashboard.service
# Every other Requires=engineering-migrate dependent, except the watchdog: its F-11 venv
# pin is left alone and this script never restarted it (it was only started above when
# the migration stopped it).
for unit in "${MIGRATE_DEPENDENTS[@]}"; do
  case "$unit" in
    engineering-watchdog.service | engineering-api.service | engineering-temporal-worker.service | \
      engineering-evidence-ingest.service | engineering-notifier.service | \
      engineering-dashboard.service) ;;
    *)
      echo "-- controller: $unit (Requires=engineering-migrate) --"
      sudo systemctl restart "$unit"
      ;;
  esac
done
echo "-- worker: all 5 role workers + release worker --"
ssh -t worker "sudo systemctl restart engineering-worker@codex.service engineering-worker@claude.service engineering-worker@browser.service engineering-worker@scan.service engineering-worker@delivery.service engineering-release-worker.service"

echo ""
echo "=== 7/7: verify ==="
sleep 3
echo "-- controller --"
systemctl is-active engineering-api.service engineering-temporal-worker.service engineering-scheduler.service engineering-evidence-ingest.service engineering-github-worker.service engineering-notifier.service engineering-dashboard.service
systemctl show engineering-api.service engineering-temporal-worker.service --property=NRestarts,ActiveState
echo "-- controller: every Requires=engineering-migrate dependent must be active --"
INACTIVE_DEPENDENTS=()
for unit in "${MIGRATE_DEPENDENTS[@]}"; do
  state=$(systemctl is-active "$unit" || true)
  echo "$unit: $state"
  [[ "$state" == active ]] || INACTIVE_DEPENDENTS+=("$unit")
done
echo "-- controller: database revision (this release) --"
DB_REVISION_AFTER=$(sudo systemd-run --quiet --wait --pipe --collect \
  --uid=engineering-control --gid=engineering-control \
  --property=EnvironmentFile="$CONTROL_ENV" \
  --working-directory=/opt/engineering-control/source \
  /opt/engineering-control/venv/bin/alembic -c /opt/engineering-control/source/alembic.ini current || true)
echo "${DB_REVISION_AFTER:-unknown}"
if [[ "${#INACTIVE_DEPENDENTS[@]}" -gt 0 ]]; then
  echo "ERROR: not active after the deploy: ${INACTIVE_DEPENDENTS[*]}" >&2
  exit 1
fi
echo "-- worker --"
ssh worker "systemctl is-active engineering-worker@codex.service engineering-worker@claude.service engineering-worker@browser.service engineering-worker@scan.service engineering-worker@delivery.service engineering-release-worker.service"
echo "-- temporal schedules (should now include engineering-connections-refresh) --"
temporal schedule list --address 127.0.0.1:7233 --namespace engineering-control

echo ""
echo "=== Done. Deployed $HASH to both hosts. ==="
# F-11: the watchdog has no console script on this branch; a drop-in pins it to the
# 2b678d9 venv (which retention keeps). Warn only if that pin is missing.
if systemctl show engineering-watchdog -p ExecStart 2>/dev/null | grep -q "/venvs/2b678d9"; then
  echo "engineering-watchdog: pinned to the 2b678d9 venv (F-11 drop-in present)."
else
  echo "WARNING: engineering-watchdog is not pinned and has no console script on this branch;"
  echo "it will fail on its next restart -- see F-11 (50-pin-venv.conf drop-in)."
fi
