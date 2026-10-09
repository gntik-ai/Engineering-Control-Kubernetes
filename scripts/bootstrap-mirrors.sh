#!/usr/bin/env bash
set -euo pipefail
umask 007

if [[ ${EUID} -ne 0 ]]; then
  echo "Run on worker with sudo: sudo $0" >&2
  exit 1
fi

# Worker canonical mirror layout (NEW-162; created by bootstrap-worker.sh):
#   /srv/engineering/mirrors      scan-runner:scan-runner 0700, named-user ACL --x for
#                                 hermes, release-runner, delivery-runner, claude-runner and
#                                 codex-runner (traverse only).
#   /srv/engineering/mirrors/<repository>.git
#                                 scan-runner:scan-runner, ACL user:scan-runner:rwx,
#                                 user:hermes:rwx, user:{delivery,claude,codex}-runner:r-x,
#                                 group::---, other::---; directories carry the same default
#                                 ACL plus release-runner r-x.
# An existing mirror root or mirror is never re-owned or re-moded: a previous version of this
# script re-owned the root to hermes and removed scan-runner's access, so every revalidation
# then failed "canonical mirror does not exist".
MIRROR_ROOT=/srv/engineering/mirrors
TMP_ROOT=/srv/engineering/tmp/github
GH_HOME=/home/hermes
READERS=(delivery-runner claude-runner codex-runner)
TRAVERSERS=(hermes release-runner delivery-runner claude-runner codex-runner)

for identity in scan-runner "${TRAVERSERS[@]}"; do
  id "${identity}" >/dev/null 2>&1
done

if [[ ! -e ${MIRROR_ROOT} ]]; then
  install -d -o scan-runner -g scan-runner -m 0700 "${MIRROR_ROOT}"
  root_acl=""
  for identity in "${TRAVERSERS[@]}"; do
    root_acl+="${root_acl:+,}u:${identity}:--x"
  done
  # Same as bootstrap-worker.sh: an explicit traverse-only mask, never a recalculated one
  # (it would re-enable broader default entries inherited from the parent).
  setfacl -n --modify "${root_acl},m::--x" "${MIRROR_ROOT}"
fi
[[ ! -L ${MIRROR_ROOT} && -d ${MIRROR_ROOT} ]]
# bootstrap-worker.sh creates the root with these traversal ACLs; an existing root is never
# changed here, so fail closed (with the fix) when hermes cannot traverse it.
if ! runuser -u hermes -- test -x "${MIRROR_ROOT}"; then
  echo "hermes cannot traverse ${MIRROR_ROOT}; run bootstrap-worker.sh first" >&2
  exit 1
fi
if [[ ! -e ${TMP_ROOT} ]]; then
  install -d -o hermes -g hermes -m 0700 "${TMP_ROOT}"
fi
[[ ! -L ${TMP_ROOT} && -d ${TMP_ROOT} ]]

mirror_acl="u:scan-runner:rwX,u:hermes:rwX,g::---,o::---"
default_acl="d:u::rwx,d:u:scan-runner:rwx,d:u:hermes:rwx,d:u:release-runner:r-x,d:g::---,d:o::---"
for identity in "${READERS[@]}"; do
  mirror_acl+=",u:${identity}:r-X"
  default_acl+=",d:u:${identity}:r-x"
done

# hermes runs git in a scan-runner-owned repository: trust exactly that path, per command
# (no global or system git configuration change).
mirror_git() {
  local target=$1
  shift
  runuser -u hermes -- env HOME="${GH_HOME}" GIT_TERMINAL_PROMPT=0 \
    git -c "safe.directory=${target}" -C "${target}" "$@"
}

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
    mv -T "${staging}/mirror.git" "${target}"
    rmdir "${staging}"
    chown -R scan-runner:scan-runner "${target}"
    chmod -R u+rwX,go-rwx "${target}"
    setfacl -R --modify "${mirror_acl}" "${target}"
    find "${target}" -type d -exec setfacl --modify "${default_acl}" {} +
  fi
  [[ ! -L ${target} && -d ${target} ]]
  if [[ $(stat -c %U "${target}") != scan-runner ]]; then
    echo "${target} is not owned by scan-runner; restore the worker mirror layout" >&2
    exit 1
  fi
  [[ $(mirror_git "${target}" rev-parse --is-bare-repository) == true ]]
  [[ $(mirror_git "${target}" remote get-url origin) == "${remote}" ]]
  mirror_git "${target}" fetch --prune origin \
    '+refs/heads/*:refs/remotes/origin/*' '+refs/tags/*:refs/tags/*'
done

echo "Canonical mirrors are verified and current through the gh-authenticated broker identity."
