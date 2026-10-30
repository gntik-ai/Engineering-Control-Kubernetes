#!/usr/bin/env bash
# NEW-243 phase 0: one-off, root-only repair of ChangeSet worktrees and their Git metadata
# written before the hand-off support, so another provider's runner can take them over.
#
# Hermes itself (no root) keeps new worktrees in shape: the runner that wrote a worktree
# restores the ACL mask on its own files after every writable step. Older worktrees may
# still hold runner-owned entries with a narrowed mask that the checker cannot read or the
# cleanup cannot remove. This wrapper runs the repair
# (engineering_control.worktrees.ownership_repair; see its documentation for exactly what it
# changes): a descriptor-relative walker that follows no symlink (intermediate ones
# included), re-checks every entry before changing it through its descriptor, never crosses
# a filesystem boundary, never widens "other" and never touches mirrors. Idempotent. Dry
# run by default. Exit 3 when any walk was incomplete (an unlistable directory, a race, an
# error, a mount inside a tree) or any path was refused; it says which.
#
# Usage (on the worker, as root):
#   scripts/normalise-worktree-ownership.sh            # dry run: what would change
#   scripts/normalise-worktree-ownership.sh --apply    # apply
set -euo pipefail

PYTHON="${ENGINEERING_PYTHON:-/opt/engineering-control/venv/bin/python}"
exec "${PYTHON}" -I -m engineering_control.worktrees.ownership_repair "$@"
