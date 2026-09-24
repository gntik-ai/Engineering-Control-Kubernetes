#!/usr/bin/env bash
# F-11 (b): release retention policy for /opt/engineering-control/releases.
#
# Keeps the KEEP_COUNT most-recently-modified release directories plus
# whichever one is currently live (via /opt/engineering-control/source),
# regardless of its age. Everything else is a deletion candidate -- but a
# candidate is only ever actually deleted if its directory name is a commit
# hash that this git repo can still reach (`git cat-file -e`). Anything not
# git-reachable is left alone and reported loudly instead of deleted: that
# is exactly the class of data-loss risk F-11 flagged (a release built from
# a dangling/unreachable commit, e.g. a git-worktree lineage that was never
# merged to a tracked branch).
#
# Defaults to a dry run. Pass --execute to actually delete. Deletion uses
# sudo per-directory, one at a time, the same shape as .go-status/
# f22-release-cleanup.sh -- run this yourself; the harness's own classifier
# blocks the assistant from running rm against this path directly.
#
# Usage:
#   scripts/retention-releases.sh                 # dry run, default keep=15
#   scripts/retention-releases.sh --keep 20        # dry run, custom keep count
#   scripts/retention-releases.sh --execute        # actually delete
set -euo pipefail

RELEASES_ROOT="/opt/engineering-control/releases"
# NEW-51: each release has a matching venv (~190 MB, vs ~5 MB of source); pruning only
# releases left 48 venvs (8.1 GB) and the backup free-space precondition failing.
VENVS_ROOT="/opt/engineering-control/venvs"
REPO_ROOT="/home/hermes/engineering/engineering-control"
KEEP_COUNT=15
EXECUTE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep)
      KEEP_COUNT="$2"
      shift 2
      ;;
    --execute)
      EXECUTE=1
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ ! -d "$RELEASES_ROOT" ]]; then
  echo "No such directory: $RELEASES_ROOT" >&2
  exit 1
fi

LIVE_HASH=""
if [[ -e /opt/engineering-control/source ]]; then
  LIVE_HASH=$(readlink -f /opt/engineering-control/source | sed -E "s#$RELEASES_ROOT/([^/]+)/.*#\1#")
fi
echo "Current live release: ${LIVE_HASH:-<none found>}"

# NEW-51: a venv still used by a running process (services that the deploy script does
# not restart keep running from an older venv) is never a candidate.
mapfile -t IN_USE < <(
  { readlink -f /opt/engineering-control/venv 2>/dev/null
    ps -eo args= 2>/dev/null
  } | grep -oE "$VENVS_ROOT/[0-9a-f]{40}" | sed -E "s#$VENVS_ROOT/##" | sort -u
)
echo "Venvs in use by the live link or a running process: ${IN_USE[*]:-<none>}"
in_use() {
  local hash="$1" used
  for used in "${IN_USE[@]}"; do [[ "$used" == "$hash" ]] && return 0; done
  return 1
}

mapfile -t ALL_DIRS < <(
  find "$RELEASES_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %f\n' \
    | sort -rn \
    | awk '{print $2}'
)

echo "Found ${#ALL_DIRS[@]} release directories; keeping the $KEEP_COUNT most recent plus the live one."

KEEP_SET=()
CANDIDATES=()
count=0
for dir in "${ALL_DIRS[@]}"; do
  if [[ "$dir" == "$LIVE_HASH" ]] || in_use "$dir"; then
    KEEP_SET+=("$dir")
    continue
  fi
  if (( count < KEEP_COUNT )); then
    KEEP_SET+=("$dir")
    count=$((count + 1))
  else
    CANDIDATES+=("$dir")
  fi
done

echo ""
echo "Keeping (${#KEEP_SET[@]}):"
printf '  %s\n' "${KEEP_SET[@]}"

if [[ ${#CANDIDATES[@]} -eq 0 ]]; then
  echo ""
  echo "No deletion candidates. Nothing to do."
  exit 0
fi

echo ""
echo "Deletion candidates (${#CANDIDATES[@]}), pending git-reachability check:"

TO_DELETE=()
TO_SKIP_UNREACHABLE=()
for hash in "${CANDIDATES[@]}"; do
  if git -C "$REPO_ROOT" cat-file -e "${hash}^{commit}" 2>/dev/null; then
    TO_DELETE+=("$hash")
    echo "  DELETE  $hash (git-reachable)"
  else
    TO_SKIP_UNREACHABLE+=("$hash")
    echo "  KEEP    $hash -- NOT git-reachable, refusing to delete (see F-11)"
  fi
done

if [[ ${#TO_SKIP_UNREACHABLE[@]} -gt 0 ]]; then
  echo ""
  echo "WARNING: ${#TO_SKIP_UNREACHABLE[@]} old release dir(s) are not git-reachable and were" >&2
  echo "left in place instead of deleted. Investigate before they age further (F-11 precedent:" >&2
  echo "this is exactly how the abandoned-worktree data-loss risk was found)." >&2
fi

if [[ $EXECUTE -eq 0 ]]; then
  echo ""
  echo "Dry run only -- no directories deleted. Re-run with --execute to delete the" \
       "${#TO_DELETE[@]} releases listed above as DELETE, and their venvs under $VENVS_ROOT."
  exit 0
fi

echo ""
for hash in "${TO_DELETE[@]}"; do
  echo "Deleting $RELEASES_ROOT/$hash"
  sudo rm -rf "${RELEASES_ROOT:?}/${hash:?}"
  if [[ -d "$VENVS_ROOT/$hash" ]]; then
    echo "Deleting $VENVS_ROOT/$hash"
    sudo rm -rf "${VENVS_ROOT:?}/${hash:?}"
  fi
done

echo "Done. Remaining release directories:"
ls -1 "$RELEASES_ROOT"
