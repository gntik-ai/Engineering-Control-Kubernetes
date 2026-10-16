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
# NEW-212: with pytest-xdist installed (pip install -e '.[dev]'), the suite runs on
# PREFLIGHT_WORKERS processes (default 4 of the controller's 8 CPUs), one file per worker
# at a time; each worker gets its own directory under the private basetemp. Without it the
# run stays serial.
PYTEST_PARALLEL=()
if .venv/bin/python -c 'import xdist' 2>/dev/null; then
  PYTEST_PARALLEL=(-n "${PREFLIGHT_WORKERS:-4}" --dist loadfile)
fi
.venv/bin/pytest tests/ -q -p no:cacheprovider --basetemp="$PYTEST_BASETEMP" "${PYTEST_PARALLEL[@]}"
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
# $1 = release source dir, $2 = its venv, then the alembic arguments.
release_alembic() {
  local source="$1" venv="$2"
  shift 2
  sudo systemd-run --quiet --wait --pipe --collect \
    --uid=engineering-control --gid=engineering-control \
    --property=EnvironmentFile="$CONTROL_ENV" \
    --working-directory="$source" \
    "$venv/bin/alembic" -c "$source/alembic.ini" "$@"
}

new_release_alembic() {
  release_alembic "$RELEASE_DIR/source" "$VENV_DIR" "$@"
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
# NEW-118: recorded first, so a failed migration can roll both hosts back.
PREVIOUS_SOURCE=$(readlink /opt/engineering-control/source)
PREVIOUS_VENV=$(readlink /opt/engineering-control/venv)
PREVIOUS_WORKER_SOURCE=$(ssh worker "readlink /opt/engineering-control/source")
PREVIOUS_WORKER_VENV=$(ssh worker "readlink /opt/engineering-control/venv")
if [[ -z "$PREVIOUS_SOURCE" || -z "$PREVIOUS_VENV" || -z "$PREVIOUS_WORKER_SOURCE" || -z "$PREVIOUS_WORKER_VENV" ]]; then
  echo "ABORT: the current release symlinks could not be read; nothing swapped." >&2
  exit 1
fi
echo "Previous release: controller $PREVIOUS_SOURCE ($PREVIOUS_VENV), worker $PREVIOUS_WORKER_SOURCE ($PREVIOUS_WORKER_VENV)"
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
# Start every dependent (a stopped one pulls engineering-migrate in again first).
start_dependents() {
  local failed=0 unit
  sudo systemctl reset-failed engineering-migrate.service || true
  for unit in "${MIGRATE_DEPENDENTS[@]}"; do
    echo "-- controller: start $unit --"
    sudo systemctl start "$unit" || failed=1
  done
  return "$failed"
}

recovery_instructions() {
  echo "RECOVERY (controller):" >&2
  echo "  journalctl -u engineering-migrate.service -n 100 --no-pager" >&2
  echo "  # previous release: $PREVIOUS_SOURCE ($PREVIOUS_VENV)" >&2
  echo "  sudo ln -sfn '$PREVIOUS_SOURCE' /opt/engineering-control/source && sudo ln -sfn '$PREVIOUS_VENV' /opt/engineering-control/venv" >&2
  echo "  ssh -t worker \"sudo ln -sfn '$PREVIOUS_WORKER_SOURCE' /opt/engineering-control/source && sudo ln -sfn '$PREVIOUS_WORKER_VENV' /opt/engineering-control/venv\"" >&2
  echo "  sudo systemctl reset-failed engineering-migrate.service && sudo systemctl start ${MIGRATE_DEPENDENTS[*]}" >&2
  echo "  # (only roll back if the database is still at $DB_REVISION; otherwise fix forward" >&2
  echo "  #  and run: sudo systemctl restart engineering-migrate.service, then start the units)" >&2
}

# NEW-118: a failed migration never leaves the controller stopped. With the database
# still at its old revision (alembic runs each migration in a transaction), both hosts
# go back to the previous release and every dependent is started on it. errexit is off
# for the whole recovery: every step is attempted, each failure is collected, and the
# exact commands for what failed are printed before the non-zero exit.
recover_failed_migration() {
  set +e
  local reason="$1" after_out after="" rc unit
  local -a failures=()
  echo "MIGRATION FAILED: $reason" >&2
  after_out=$(new_release_alembic current)
  rc=$?
  if [[ "$rc" -eq 0 ]]; then
    after=$(alembic_revisions <<<"$after_out" | head -n 1)
  fi
  echo "Database revision after the failure: ${after:-unknown} (before: $DB_REVISION)" >&2
  if [[ -n "$after" && "$after" == "$DB_REVISION" ]]; then
    echo "-- rollback: database unchanged; restoring the previous release on both hosts --" >&2
    sudo ln -sfn "$PREVIOUS_SOURCE" /opt/engineering-control/source ||
      failures+=("sudo ln -sfn '$PREVIOUS_SOURCE' /opt/engineering-control/source")
    sudo ln -sfn "$PREVIOUS_VENV" /opt/engineering-control/venv ||
      failures+=("sudo ln -sfn '$PREVIOUS_VENV' /opt/engineering-control/venv")
    ssh -t worker "sudo ln -sfn '$PREVIOUS_WORKER_SOURCE' /opt/engineering-control/source" ||
      failures+=("ssh -t worker \"sudo ln -sfn '$PREVIOUS_WORKER_SOURCE' /opt/engineering-control/source\"")
    ssh -t worker "sudo ln -sfn '$PREVIOUS_WORKER_VENV' /opt/engineering-control/venv" ||
      failures+=("ssh -t worker \"sudo ln -sfn '$PREVIOUS_WORKER_VENV' /opt/engineering-control/venv\"")
  else
    echo "The database is not at its previous revision; the new release stays linked." >&2
    failures+=("# fix forward: journalctl -u engineering-migrate.service -n 100 --no-pager, then sudo systemctl restart engineering-migrate.service")
  fi
  sudo systemctl reset-failed engineering-migrate.service ||
    failures+=("sudo systemctl reset-failed engineering-migrate.service")
  for unit in "${MIGRATE_DEPENDENTS[@]}"; do
    echo "-- controller: start $unit --" >&2
    sudo systemctl start "$unit" || failures+=("sudo systemctl start $unit")
  done
  if [[ "${#failures[@]}" -eq 0 && "$after" == "$DB_REVISION" ]]; then
    echo "ROLLED BACK: every dependent runs the previous release again; $HASH is NOT deployed." >&2
    echo "(Project contracts installed in step 5b were not reverted.)" >&2
  else
    echo "RECOVERY: run these on the controller (previous release $PREVIOUS_SOURCE):" >&2
    for unit in "${failures[@]}"; do
      echo "  $unit" >&2
    done
    recovery_instructions
  fi
  exit 1
}

if [[ "$MIGRATION_NEEDED" == yes ]]; then
  echo "-- controller: engineering-migrate ($DB_REVISION -> ${RELEASE_HEADS[*]}) --"
  MIGRATE_INVOCATION_BEFORE=$(systemctl show engineering-migrate.service --property=InvocationID --value)
  if ! sudo systemctl restart engineering-migrate.service; then
    recover_failed_migration "systemctl restart engineering-migrate.service failed"
  fi
  MIGRATE_INVOCATION_AFTER=$(systemctl show engineering-migrate.service --property=InvocationID --value)
  if [[ -z "$MIGRATE_INVOCATION_AFTER" || "$MIGRATE_INVOCATION_AFTER" == "$MIGRATE_INVOCATION_BEFORE" ]]; then
    # An old Result=success from the previous run must never pass for this one.
    recover_failed_migration "engineering-migrate did not run again (InvocationID unchanged)"
  fi
  MIGRATE_RESULT=$(systemctl show engineering-migrate.service --property=Result --value)
  if [[ "$MIGRATE_RESULT" != success ]]; then
    recover_failed_migration "engineering-migrate finished with Result=$MIGRATE_RESULT"
  fi
  MIGRATED_OUT=$(new_release_alembic current) || recover_failed_migration "alembic current failed after the migration"
  MIGRATED_REVISION=$(alembic_revisions <<<"$MIGRATED_OUT" | head -n 1)
  if ! printf '%s\n' "${RELEASE_HEADS[@]}" | grep -qx -- "$MIGRATED_REVISION"; then
    recover_failed_migration "database at ${MIGRATED_REVISION:-unknown}, not at a release head (${RELEASE_HEADS[*]})"
  fi
  echo "engineering-migrate: Result=success, database at $MIGRATED_REVISION"
  if ! start_dependents; then
    echo "ERROR: some Requires=engineering-migrate dependents did not start." >&2
    recovery_instructions
    exit 1
  fi
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
# 2026-10-04: the watchdog is unpinned (F-11 drop-in removed) and runs the release venv,
# so it is restarted like every other service; before, it kept the previous release.
echo "-- controller: engineering-watchdog --"
sudo systemctl restart engineering-watchdog.service
# 2026-10-03: the Telegram gateway hook was never installed by this script and stayed at
# 2026-08-30 for weeks. Install the release copy when it differs (previous copy backed up,
# then synced: files written right before a reboot once came back empty), and restart
# the gateway only then.
HOOK_SOURCE="$RELEASE_DIR/source/integrations/hermes-engineering-control/hook"
HOOK_TARGET="$HOME/.hermes/hooks/engineering-control"
hook_changed=0
for hook_file in HOOK.yaml handler.py; do
  if ! cmp -s "$HOOK_SOURCE/$hook_file" "$HOOK_TARGET/$hook_file"; then
    hook_changed=1
  fi
done
if [[ $hook_changed -eq 1 ]]; then
  echo "-- controller: gateway hook differs from this release; installing it --"
  HOOK_BACKUP="$HOME/engineering-backups/hook-$(date -u +%Y%m%dT%H%M%SZ)"
  if [[ -d $HOOK_TARGET ]]; then
    install -d -m 0750 "$HOOK_BACKUP"
    cp -a "$HOOK_TARGET/." "$HOOK_BACKUP/"
  else
    HOOK_BACKUP="(none: no previous hook)"
  fi
  install -d -m 0750 "$HOOK_TARGET"
  for hook_file in HOOK.yaml handler.py; do
    install -m 0640 "$HOOK_SOURCE/$hook_file" "$HOOK_TARGET/$hook_file"
    cmp -s "$HOOK_SOURCE/$hook_file" "$HOOK_TARGET/$hook_file" \
      || { echo "ERROR: installing $hook_file failed" >&2; exit 1; }
  done
  sync
  echo "   previous hook backed up to $HOOK_BACKUP; restarting hermes-gateway"
  sudo systemctl restart hermes-gateway.service
else
  echo "-- controller: gateway hook already matches this release --"
fi
# Every other Requires=engineering-migrate dependent (the named ones are restarted above).
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
DB_FINAL_OUT=$(release_alembic /opt/engineering-control/source /opt/engineering-control/venv current || true)
DB_FINAL_REVISION=$(alembic_revisions <<<"$DB_FINAL_OUT" | head -n 1)
echo "Database revision: ${DB_FINAL_REVISION:-unknown} (release heads: ${RELEASE_HEADS[*]})"
if [[ "${#INACTIVE_DEPENDENTS[@]}" -gt 0 ]]; then
  echo "ERROR: not active after the deploy: ${INACTIVE_DEPENDENTS[*]}" >&2
  exit 1
fi
if ! printf '%s\n' "${RELEASE_HEADS[@]}" | grep -qx -- "${DB_FINAL_REVISION:-unknown}"; then
  echo "ERROR: the database revision ${DB_FINAL_REVISION:-unknown} is not this release's head." >&2
  exit 1
fi
echo "-- worker --"
ssh worker "systemctl is-active engineering-worker@codex.service engineering-worker@claude.service engineering-worker@browser.service engineering-worker@scan.service engineering-worker@delivery.service engineering-release-worker.service"
echo "-- temporal schedules (should now include engineering-connections-refresh) --"
temporal schedule list --address 127.0.0.1:7233 --namespace engineering-control

echo ""
echo "=== Done. Deployed $HASH to both hosts. ==="
# The watchdog must run this release's venv (the F-11 pin was retired 2026-10-04).
WATCHDOG_PID=$(systemctl show engineering-watchdog -p MainPID --value 2>/dev/null || echo 0)
if [[ ${WATCHDOG_PID:-0} != 0 ]] && tr '\0' ' ' <"/proc/$WATCHDOG_PID/cmdline" 2>/dev/null \
  | grep -q -- "$VENV_DIR/"; then
  echo "engineering-watchdog: running this release ($VENV_DIR)."
else
  echo "WARNING: engineering-watchdog is not running this release's venv ($VENV_DIR)." >&2
fi
