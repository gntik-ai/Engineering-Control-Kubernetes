#!/usr/bin/env bash
set -euo pipefail
umask 027

if [[ ${EUID} -ne 0 ]]; then
  echo "Run on worker with sudo: sudo $0" >&2
  exit 1
fi

MIRROR_ROOT=/srv/engineering/mirrors
TMP_ROOT=/srv/engineering/tmp/scan

id scan-runner >/dev/null 2>&1 || {
  echo "scan-runner must exist; run bootstrap-worker.sh first" >&2
  exit 1
}
install -d -o scan-runner -g scan-runner -m 0700 "${MIRROR_ROOT}" "${TMP_ROOT}"

repositories=(falcone falcone-charts musematic musematic-deploy)
for repository in "${repositories[@]}"; do
  remote="https://github.com/gntik-ai/${repository}.git"
  target="${MIRROR_ROOT}/${repository}.git"
  if [[ ! -e "${target}" ]]; then
    staging=$(runuser -u scan-runner -- mktemp -d "${TMP_ROOT}/${repository}.XXXXXX")
    runuser -u scan-runner -- env GIT_TERMINAL_PROMPT=0 git clone --mirror \
      "${remote}" "${staging}/mirror.git"
    runuser -u scan-runner -- git -C "${staging}/mirror.git" fsck --no-dangling
    runuser -u scan-runner -- mv "${staging}/mirror.git" "${target}"
    rmdir "${staging}"
  fi
  [[ ! -L "${target}" && -d "${target}" ]] || {
    echo "Mirror target is not an ordinary directory: ${target}" >&2
    exit 1
  }
  [[ $(runuser -u scan-runner -- git -C "${target}" rev-parse --is-bare-repository) == true ]]
  [[ $(runuser -u scan-runner -- git -C "${target}" remote get-url origin) == "${remote}" ]] || {
    echo "Mirror origin mismatch: ${target}" >&2
    exit 1
  }
  runuser -u scan-runner -- env GIT_TERMINAL_PROMPT=0 git -C "${target}" fetch \
    --prune origin '+refs/heads/*:refs/remotes/origin/*' '+refs/tags/*:refs/tags/*'
done

echo "Canonical mirrors are verified and current."
