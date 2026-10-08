#!/usr/bin/env bash
set -euo pipefail
umask 007

if [[ ${EUID} -ne 0 ]]; then
  echo "Run on worker with sudo: sudo $0" >&2
  exit 1
fi

MIRROR_ROOT=/srv/engineering/mirrors
TMP_ROOT=/srv/engineering/tmp/github
GH_HOME=/home/hermes

id hermes >/dev/null 2>&1
id scan-runner >/dev/null 2>&1
install -d -o hermes -g scan-runner -m 0770 "${MIRROR_ROOT}" "${TMP_ROOT}"

# This uses the authenticated gh credential store in place. It never extracts,
# copies, or places a token in an environment variable. The operator must have
# run `gh auth setup-git` once as hermes.
runuser -u hermes -- env HOME="${GH_HOME}" GH_PROMPT_DISABLED=1 \
  gh auth status --hostname github.com >/dev/null

repositories=(falcone falcone-charts musematic musematic-deploy llmwiki gntik-ui)
for repository in "${repositories[@]}"; do
  remote="https://github.com/gntik-ai/${repository}.git"
  target="${MIRROR_ROOT}/${repository}.git"
  if [[ ! -e ${target} ]]; then
    staging=$(runuser -u hermes -- mktemp -d "${TMP_ROOT}/${repository}.XXXXXX")
    runuser -u hermes -- env HOME="${GH_HOME}" GH_PROMPT_DISABLED=1 \
      gh repo clone "gntik-ai/${repository}" "${staging}/mirror.git" -- --mirror
    runuser -u hermes -- git -C "${staging}/mirror.git" fsck --no-dangling
    runuser -u hermes -- mv "${staging}/mirror.git" "${target}"
    rmdir "${staging}"
  fi
  [[ ! -L ${target} && -d ${target} ]]
  [[ $(runuser -u hermes -- git -C "${target}" rev-parse --is-bare-repository) == true ]]
  [[ $(runuser -u hermes -- git -C "${target}" remote get-url origin) == "${remote}" ]]
  runuser -u hermes -- env HOME="${GH_HOME}" GIT_TERMINAL_PROMPT=0 \
    git -C "${target}" fetch --prune origin \
      '+refs/heads/*:refs/remotes/origin/*' '+refs/tags/*:refs/tags/*'
  chown -R hermes:scan-runner "${target}"
  find "${target}" -type d -exec chmod 0770 {} +
  find "${target}" -type f -exec chmod 0660 {} +
done

echo "Canonical mirrors are verified and current through the gh-authenticated broker identity."
