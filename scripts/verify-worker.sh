#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run on worker with sudo: sudo $0" >&2
  exit 1
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
"${SCRIPT_DIR}/verify-no-omniroute.sh" --scope worker

cd /var/lib/engineering-control
blocked=0

for path in /home/hermes/engineering/bootstrap /home/hermes/engineering/docs \
  /home/hermes/engineering/project-config /etc/engineering-control/systemd \
  /var/log/engineering-control /opt/engineering-agent-pack/source; do
  test -d "${path}"
done
agent_pack_head=$(git -C /home/hermes/engineering/engineering-agent-pack rev-parse HEAD)
agent_pack_release=$(basename \
  "$(dirname "$(readlink -f /opt/engineering-agent-pack/source)")")
[[ ${agent_pack_release} == "${agent_pack_head}" ]]
grep -q '^ENGINEERING_AGENT_PACK_ROOT=/opt/engineering-agent-pack/source$' \
  /etc/engineering-control/env/worker-common.env
for identity in codex-runner claude-runner; do
  sudo -u "${identity}" test -r \
    /opt/engineering-agent-pack/source/domain-packs/common/POLICIES.md
done
/opt/engineering-control/venv/bin/python \
  /opt/engineering-agent-pack/source/generators/generate.py --check
/opt/engineering-control/venv/bin/python \
  /opt/engineering-agent-pack/source/generators/validate.py
for unit in engineering-workers.slice engineering-workers-agents.slice \
  engineering-worker@.service engineering-release-worker.service; do
  cmp -s "/etc/engineering-control/systemd/${unit}" "/etc/systemd/system/${unit}"
done
# NEW-167: the workers' memory budget is in force (aggregate slice, nested agents slice).
[[ $(systemctl show -p Slice --value engineering-worker@scan.service) == engineering-workers.slice ]]
for agent in codex claude; do
  [[ $(systemctl show -p Slice --value "engineering-worker@${agent}.service") == \
    engineering-workers-agents.slice ]]
done

for identity in codex-runner claude-runner browser-runner scan-runner delivery-runner release-runner; do
  id "${identity}" >/dev/null
done
for service in engineering-worker@codex engineering-worker@claude engineering-worker@browser \
  engineering-worker@scan engineering-worker@delivery engineering-release-worker; do
  systemctl is-active --quiet "${service}.service"
done
[[ $(sudo -u codex-runner -H codex --version) == "codex-cli 0.159.3" ]]
[[ $(sudo -u claude-runner -H claude --version) == "2.1.239 (Claude Code)" ]]
if ! sudo -u codex-runner -H env -i \
  HOME=/var/lib/engineering-control/identities/codex-runner \
  PATH=/usr/local/bin:/usr/bin:/bin /usr/local/bin/codex login status >/dev/null 2>&1; then
  echo "BLOCKED MANUAL_AUTH_REQUIRED_CODEX"
  blocked=1
fi
if ! sudo -u claude-runner -H env -i \
  HOME=/var/lib/engineering-control/identities/claude-runner \
  PATH=/usr/local/bin:/usr/bin:/bin /usr/local/bin/claude auth status >/dev/null 2>&1; then
  echo "BLOCKED MANUAL_AUTH_REQUIRED_CLAUDE"
  blocked=1
fi
sudo -u browser-runner test -r \
  /opt/engineering-browser/runtime/node_modules/@playwright/test/package.json
[[ $(sudo -u browser-runner -H env -i \
  HOME=/var/lib/engineering-control/identities/browser-runner \
  PATH=/usr/local/bin:/usr/bin:/bin \
  /opt/engineering-browser/runtime/node_modules/.bin/pnpm --version) == "10.34.5" ]]
sudo -u browser-runner -H env -i \
  HOME=/var/lib/engineering-control/identities/browser-runner \
  PATH=/usr/local/bin:/usr/bin:/bin \
  PLAYWRIGHT_BROWSERS_PATH=/opt/engineering-browser/browsers \
  /usr/bin/python3 /opt/engineering-control/source/scripts/browser-runtime-smoke.py
[[ $(sudo -u scan-runner /usr/local/bin/gitleaks version) == "8.30.0" ]]
sudo -u scan-runner /usr/local/bin/trivy --version | grep -qx 'Version: 0.74.0'
[[ $(sudo -u scan-runner /usr/local/bin/semgrep --version) == "1.174.0" ]]
sudo -u scan-runner /usr/local/bin/syft version | grep -qx 'Version:       1.51.0'
[[ $(sudo -u scan-runner /usr/local/bin/kubeconform -v) == "v0.8.0" ]]
sudo -u release-runner /usr/local/bin/helm version --short | grep -q '^v4\.2\.3+'
sudo -u release-runner /usr/local/bin/kubectl version --client=true -o json | \
  grep -q '"gitVersion": "v1.36.3"'
sudo -u release-runner /usr/local/bin/argocd version --client --short | \
  grep -q '^argocd: v3\.5\.1+'
sudo -u hermes /usr/local/bin/gh --version | head -n 1 | \
  grep -q '^gh version 2\.98\.0 '
sudo -u hermes -H /usr/local/bin/gh auth status --hostname github.com >/dev/null
for repository in falcone falcone-charts musematic musematic-deploy llmwiki gntik-ui falcone-serverless; do
  sudo -u hermes -H /usr/local/bin/gh repo view "gntik-ai/${repository}" \
    --json nameWithOwner,defaultBranchRef,viewerPermission,isPrivate >/dev/null
done
if sudo -u delivery-runner test -s \
  /etc/engineering-control/env/delivery/registry/config.json; then
  [[ $(stat -c '%a:%U:%G' \
    /etc/engineering-control/env/delivery/registry/config.json) == \
    640:root:delivery-runner ]]
else
  echo "BLOCKED MANUAL_GITHUB_REGISTRY_AUTH_REQUIRED"
  blocked=1
fi
for identity in codex-runner claude-runner browser-runner scan-runner delivery-runner release-runner; do
  own_token="/etc/engineering-control/env/evidence-${identity}.token"
  sudo -u "${identity}" test -r "${own_token}"
  for other_identity in codex-runner claude-runner browser-runner scan-runner delivery-runner release-runner; do
    if [[ ${other_identity} != "${identity}" ]] && \
      sudo -u "${identity}" test -r \
        "/etc/engineering-control/env/evidence-${other_identity}.token" 2>/dev/null; then
      echo "FAIL ${identity} can impersonate ${other_identity} evidence" >&2
      exit 1
    fi
  done
done
sudo -u release-runner test ! -r \
  /var/lib/engineering-control/identities/codex-runner/.codex/auth.json
sudo -u release-runner test ! -r \
  /var/lib/engineering-control/identities/claude-runner/.claude/.credentials.json
if sudo -u codex-runner test -r /etc/engineering-control/env/release-worker.env 2>/dev/null; then
  echo "FAIL codex runner can read release environment" >&2
  exit 1
fi
if sudo -u claude-runner test -r /etc/engineering-control/env/release-worker.env 2>/dev/null; then
  echo "FAIL claude runner can read release environment" >&2
  exit 1
fi
for identity in codex-runner claude-runner browser-runner scan-runner release-runner; do
  if sudo -u "${identity}" test -r \
    /etc/engineering-control/env/delivery/registry/config.json 2>/dev/null; then
    echo "FAIL ${identity} can read GitHub registry credentials" >&2
    exit 1
  fi
done
for identity in codex-runner claude-runner browser-runner scan-runner delivery-runner; do
  if sudo -u "${identity}" test -r /etc/engineering-control/env/release 2>/dev/null; then
    echo "FAIL ${identity} can read release credentials" >&2
    exit 1
  fi
done
sudo -u delivery-runner test ! -r \
  /var/lib/engineering-control/identities/codex-runner/.codex/auth.json
sudo -u delivery-runner test ! -r \
  /var/lib/engineering-control/identities/claude-runner/.claude/.credentials.json
for identity in codex-runner claude-runner browser-runner delivery-runner release-runner; do
  if sudo -u "${identity}" test -r /etc/engineering-control/env/observer 2>/dev/null; then
    echo "FAIL ${identity} can read the observer credential directory" >&2
    exit 1
  fi
done
if sudo -u scan-runner test -r /etc/engineering-control/env/release 2>/dev/null; then
  echo "FAIL scan runner can read the release credential directory" >&2
  exit 1
fi
# NEW-159: optional side-by-side project toolchains (bootstrap-worker.sh
# --node-toolchain[-only]). Verified when installed, or required with
# ENGINEERING_VERIFY_NODE_TOOLCHAIN=1 (set it once a project selects node 24).
# Keep the versions in sync with scripts/bootstrap-worker.sh and config.py.
TOOLCHAIN_ROOT=/opt/engineering-toolchain
NODE24_VERSION=24.21.0
PNPM_PINNED_VERSION=11.28.3
if [[ -e ${TOOLCHAIN_ROOT} || ${ENGINEERING_VERIFY_NODE_TOOLCHAIN:-0} == 1 ]]; then
  # The side-by-side install never replaces the workers' Node.js 22.
  [[ $(/usr/local/bin/node --version) == v22.* ]]
  [[ -d ${TOOLCHAIN_ROOT} && ! -L ${TOOLCHAIN_ROOT} ]]
  [[ $(stat -c '%a:%U:%G' "${TOOLCHAIN_ROOT}") == 755:root:root ]]
  # Each stable path is a symlink to an immutable release directory beside it.
  for link in "${TOOLCHAIN_ROOT}/node-24" "${TOOLCHAIN_ROOT}/pnpm-${PNPM_PINNED_VERSION}"; do
    [[ -L ${link} ]]
    release=$(readlink -f -- "${link}")
    [[ $(dirname -- "${release}") == "${TOOLCHAIN_ROOT}" && -d ${release} && ! -L ${release} ]]
    [[ $(stat -c '%a:%U:%G' "${release}") == 755:root:root ]]
  done
  if [[ -n $(find "${TOOLCHAIN_ROOT}" \( ! -user root -o ! -group root \
    -o \( ! -type l -perm /022 \) \) -print -quit) ]]; then
    echo "FAIL ${TOOLCHAIN_ROOT} has a file not owned by root or writable by others" >&2
    exit 1
  fi
  for identity in codex-runner claude-runner scan-runner; do
    [[ $(cd / && sudo -u "${identity}" env -i PATH=/usr/bin:/bin \
      "${TOOLCHAIN_ROOT}/node-24/bin/node" --version) == "v${NODE24_VERSION}" ]]
    [[ $(cd / && sudo -u "${identity}" env -i \
      HOME="/var/lib/engineering-control/identities/${identity}" \
      PATH="${TOOLCHAIN_ROOT}/node-24/bin:/usr/bin:/bin" pnpm_config_pm_on_fail=ignore \
      "${TOOLCHAIN_ROOT}/pnpm-${PNPM_PINNED_VERSION}/pnpm" --version) == \
      "${PNPM_PINNED_VERSION}" ]]
  done
  echo "PASS node toolchain: node v${NODE24_VERSION}, pnpm ${PNPM_PINNED_VERSION}"
else
  echo "SKIP node toolchain: ${TOOLCHAIN_ROOT} is not installed"
fi
# Shared offline toolchain caches (bootstrap-worker.sh): makers and the checker read and
# traverse them but never write; scan-runner is the only writer.
TOOLCHAIN_CACHE=/srv/engineering/cache/scan/toolchain
for cache in go-mod cargo-home trivy; do
  for identity in codex-runner claude-runner; do
    sudo -u "${identity}" test -r "${TOOLCHAIN_CACHE}/${cache}" -a -x "${TOOLCHAIN_CACHE}/${cache}"
    if sudo -u "${identity}" test -w "${TOOLCHAIN_CACHE}/${cache}"; then
      echo "FAIL ${identity} can write ${TOOLCHAIN_CACHE}/${cache}" >&2
      exit 1
    fi
  done
  [[ $(stat -c '%U' "${TOOLCHAIN_CACHE}/${cache}") == scan-runner ]]
  getfacl -p "${TOOLCHAIN_CACHE}/${cache}" | grep -qx 'default:user:codex-runner:r-x'
  getfacl -p "${TOOLCHAIN_CACHE}/${cache}" | grep -qx 'default:user:claude-runner:r-x'
done
echo "PASS shared toolchain caches: read-only for makers and the checker"
# ToolchainPolicy.rust / .go (bootstrap-worker.sh --project-toolchains[-only]). Verified
# when installed, or required with ENGINEERING_VERIFY_LANGUAGE_TOOLCHAINS=1 (set it once a
# project selects them, today falcone-serverless). Keep in sync with bootstrap-worker.sh
# and config.py (toolchain_rust_roots / toolchain_go_roots).
RUST_PINNED_VERSION=1.99.1
RUST_PINNED_RUSTC="rustc 1.99.1 (58cf8f9c3 2026-10-13)"
GO_PINNED_LINE=1.26
GO_PINNED_VERSION=1.26.10
RUST_LINK="${TOOLCHAIN_ROOT}/rust-${RUST_PINNED_VERSION}"
GO_LINK="${TOOLCHAIN_ROOT}/go-${GO_PINNED_LINE}"
if [[ -e ${RUST_LINK} || -e ${GO_LINK} || ${ENGINEERING_VERIFY_LANGUAGE_TOOLCHAINS:-0} == 1 ]]; then
  for link in "${RUST_LINK}" "${GO_LINK}"; do
    [[ -L ${link} ]]
    release=$(readlink -f -- "${link}")
    [[ $(dirname -- "${release}") == "${TOOLCHAIN_ROOT}" && -d ${release} && ! -L ${release} ]]
    [[ $(stat -c '%a:%U:%G' "${release}") == 755:root:root ]]
  done
  for tool in cargo rustc rustfmt cargo-fmt cargo-clippy clippy-driver; do
    [[ -x ${RUST_LINK}/bin/${tool} ]]
  done
  [[ -x ${GO_LINK}/bin/go && -x ${GO_LINK}/bin/gofmt ]]
  for identity in codex-runner claude-runner scan-runner; do
    [[ $(cd / && sudo -u "${identity}" env -i PATH=/usr/bin:/bin HOME=/nonexistent \
      "${RUST_LINK}/bin/rustc" --version) == "${RUST_PINNED_RUSTC}" ]]
    [[ $(cd / && sudo -u "${identity}" env -i PATH=/usr/bin:/bin HOME=/nonexistent \
      GOTOOLCHAIN=local GOTELEMETRY=off "${GO_LINK}/bin/go" version) == \
      "go version go${GO_PINNED_VERSION} linux/amd64" ]]
  done
  echo "PASS language toolchains: rust ${RUST_PINNED_VERSION}, go ${GO_PINNED_VERSION}"
else
  echo "SKIP language toolchains: rust ${RUST_PINNED_VERSION} and go ${GO_PINNED_LINE} are not installed"
fi

if (( blocked != 0 )); then
  echo "BLOCKED worker verification; all credential-independent checks passed"
  exit 2
fi
echo "PASS worker verification"
