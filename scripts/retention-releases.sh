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
  if [[ "$dir" == "$LIVE_HASH" ]]; then
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
       "${#TO_DELETE[@]} directories listed above as DELETE."
  exit 0
fi

echo ""
for hash in "${TO_DELETE[@]}"; do
  echo "Deleting $RELEASES_ROOT/$hash"
  sudo rm -rf "${RELEASES_ROOT:?}/${hash:?}"
done

echo "Done. Remaining release directories:"
ls -1 "$RELEASES_ROOT"
