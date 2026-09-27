#!/usr/bin/env bash
# NEW-85 one-off (run on the CONTROLLER as hermes, not root): remove the 27 pre-GO
# acceptance worktree directories on the worker. They sit under the root-owned
# /srv/engineering/worktrees/acceptance, which the scan worker cannot touch by design,
# so the automatic cleanup never will. Shows count and size first and asks before deleting.
set -euo pipefail
trap 'echo "FAILED at line $LINENO (exit $?)" >&2' ERR

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this as hermes, not root (it calls sudo itself)." >&2
  exit 1
fi

ssh -t worker "sudo bash -c '
  set -euo pipefail
  dir=/srv/engineering/worktrees/acceptance
  [[ -d \$dir && ! -L \$dir ]] || { echo \"nothing to do: \$dir is absent\"; exit 0; }
  echo \"entries: \$(find \$dir -mindepth 1 -maxdepth 1 | wc -l)  size: \$(du -sh \$dir | cut -f1)\"
  echo \"newest entry: \$(find \$dir -mindepth 1 -maxdepth 1 -printf \"%TY-%Tm-%Td %f\n\" | sort | tail -1)\"
  read -r -p \"Delete \$dir entirely? [y/N] \" answer
  [[ \$answer == y ]] || { echo \"kept\"; exit 0; }
  rm -rf --one-file-system \$dir
  echo \"REMOVED \$dir\"
  df -h / | tail -1
'"
