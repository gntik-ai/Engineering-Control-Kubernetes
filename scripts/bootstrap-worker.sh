#!/usr/bin/env bash
set -euo pipefail
umask 027

if [[ ${EUID} -ne 0 ]]; then
  echo "Run on worker with sudo: sudo $0" >&2
  exit 1
fi

# NEW-159: optional side-by-side project toolchains (ToolchainPolicy.node / .pnpm) under
# /opt/engineering-toolchain. They never replace /usr/local/bin/node (Node 22) or
# /usr/local/bin/pnpm. Without an argument this script behaves exactly as before.
#   --node-toolchain           full bootstrap, then also install the pinned toolchains
#   --node-toolchain-only      install only the pinned toolchains and exit
# ToolchainPolicy.rust / .go (falcone-serverless) add a pinned Rust release and a Go line
# beside them; they never replace /usr/local/go (the worker's Go) or any rustup install.
#   --project-toolchains       full bootstrap, then Node.js/pnpm, Rust and Go
#   --project-toolchains-only  install only Node.js/pnpm, Rust and Go and exit
NODE_TOOLCHAIN_MODE=none
LANGUAGE_TOOLCHAINS=0
USAGE="Usage: sudo $0 [--node-toolchain | --node-toolchain-only | --project-toolchains | --project-toolchains-only]"
case "${1-}" in
  "") ;;
  --node-toolchain) NODE_TOOLCHAIN_MODE=also ;;
  --node-toolchain-only) NODE_TOOLCHAIN_MODE=only ;;
  --project-toolchains) NODE_TOOLCHAIN_MODE=also LANGUAGE_TOOLCHAINS=1 ;;
  --project-toolchains-only) NODE_TOOLCHAIN_MODE=only LANGUAGE_TOOLCHAINS=1 ;;
  *)
    echo "${USAGE}" >&2
    exit 64
    ;;
esac
if (( $# > 1 )); then
  echo "${USAGE}" >&2
  exit 64
fi

# Keep in sync with config.py (toolchain_node_roots / toolchain_pnpm_executables) and
# scripts/verify-worker.sh. Hashes: nodejs.org/dist/v24.21.0/SHASUMS256.txt and the
# pnpm v11.28.3 GitHub release asset digest (both re-verified by download 2026-10-08).
TOOLCHAIN_ROOT=/opt/engineering-toolchain
NODE24_VERSION=24.21.0
NODE24_SHA256=fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6
NODE24_URL=https://nodejs.org/dist/v${NODE24_VERSION}/node-v${NODE24_VERSION}-linux-x64.tar.xz
PNPM_PINNED_VERSION=11.28.3
PNPM_PINNED_SHA256=b0c9673be13d4b914b8045a56986418c55c446bb8d113b65ec65bf7cb7bec35f
PNPM_PINNED_URL=https://github.com/pnpm/pnpm/releases/download/v${PNPM_PINNED_VERSION}/pnpm-linux-x64.tar.gz
# ToolchainPolicy.rust / .go: keep in sync with config.py (toolchain_rust_roots /
# toolchain_go_roots) and scripts/verify-worker.sh. Hashes: the .sha256 file published
# beside the Rust standalone installer on static.rust-lang.org/dist and go.dev/dl's
# published SHA256 (both re-verified by download 2026-10-22). The standalone installer is
# installed with only rustc, cargo, rust-std, rustfmt and clippy (no rustup: a repo's
# rust-toolchain.toml can never trigger a download).
RUST_PINNED_VERSION=1.99.1
RUST_PINNED_SHA256=231fcae47ee950621374725b5fc3b5441d2fbda2955ac45dd7dc816d8876cfe7
RUST_PINNED_URL=https://static.rust-lang.org/dist/rust-${RUST_PINNED_VERSION}-x86_64-unknown-linux-gnu.tar.xz
RUST_PINNED_RUSTC="rustc 1.99.1 (58cf8f9c3 2026-10-13)"
RUST_COMPONENTS=rustc,cargo,rust-std-x86_64-unknown-linux-gnu,rustfmt-preview,clippy-preview
GO_PINNED_LINE=1.26
GO_PINNED_VERSION=1.26.10
GO_PINNED_SHA256=ccfe7ae822d2c2f41f4fe7c956199967a4dac22d0ee0d2b2bfdb440aab3b20c5
GO_PINNED_URL=https://go.dev/dl/go${GO_PINNED_VERSION}.linux-amd64.tar.gz

# toolchain_release_ok RELEASE SHA256 CHECK_RELATIVE EXPECTED_VERSION [VERSION_ARGUMENT]
toolchain_release_ok() {
  local release=$1 sha256=$2 check=$3 expected=$4 version_argument=${5:---version}
  [[ -d ${release} && ! -L ${release} && -f ${release}/.engineering-toolchain ]] &&
    [[ $(<"${release}/.engineering-toolchain") == "${sha256}" ]] &&
    [[ $(cd / && env -i PATH=/usr/bin:/bin HOME=/nonexistent \
      "${release}/${check}" "${version_argument}" 2>/dev/null) == "${expected}" ]]
}

# install_pinned_toolchain LINK URL SHA256 COMPRESSION STRIP CHECK_RELATIVE EXPECTED_VERSION
#   [VERSION_ARGUMENT] [RUST_COMPONENTS]
# Each verified download is an immutable release directory LINK.<sha256 prefix>; LINK is
# a symlink (the path Settings uses) switched by an atomic rename. A previous release is
# never removed, so an interrupted run leaves the published toolchain untouched, and the
# EXIT trap only ever removes this run's staging directory. Idempotent. With
# RUST_COMPONENTS the verified archive is a Rust standalone installer: its install.sh
# installs exactly those components into the release (no ldconfig, nothing outside it).
install_pinned_toolchain() {
  local name=$1 url=$2 sha256=$3 compression=$4 strip=$5 check=$6 expected=$7
  local version_argument=${8:---version} components=${9-}
  local link="${TOOLCHAIN_ROOT}/${name}"
  local release_name="${name}.${sha256:0:16}"
  local release="${TOOLCHAIN_ROOT}/${release_name}"
  local staging
  if [[ -e ${link} && ! -L ${link} ]]; then
    echo "${link} exists and is not a symlink; refusing to replace it" >&2
    exit 1
  fi
  if ! toolchain_release_ok "${release}" "${sha256}" "${check}" "${expected}" \
    "${version_argument}"; then
    if [[ -e ${release} || -L ${release} ]]; then
      if [[ $(readlink -- "${link}" 2>/dev/null) == "${release_name}" ]]; then
        echo "${link} points at an unverifiable release ${release}" >&2
        exit 1
      fi
      rm -rf -- "${release}"  # an unpublished, incomplete release from an earlier run
    fi
    staging=$(mktemp -d "${TOOLCHAIN_ROOT}/.${name}.staging.XXXXXX")
    # shellcheck disable=SC2064 # expand now: the trap removes only this staging directory
    trap "rm -rf -- '${staging}'" EXIT
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
      --max-time 900 --output "${staging}/download" "${url}"
    printf '%s  %s\n' "${sha256}" "${staging}/download" | sha256sum --check --quiet -
    install -d -m 0755 "${staging}/root"
    if [[ -n ${components} ]]; then
      install -d -m 0700 "${staging}/installer"
      tar "-x${compression}f" "${staging}/download" -C "${staging}/installer" \
        --strip-components="${strip}" --no-same-owner --no-same-permissions
      rm -f -- "${staging}/download"
      (cd / && env -i PATH=/usr/bin:/bin HOME=/nonexistent \
        /bin/bash "${staging}/installer/install.sh" --prefix="${staging}/root" \
        --components="${components}" --disable-ldconfig >/dev/null)
      rm -rf -- "${staging}/installer"
    else
      tar "-x${compression}f" "${staging}/download" -C "${staging}/root" \
        --strip-components="${strip}" --no-same-owner --no-same-permissions
      rm -f -- "${staging}/download"
    fi
    printf '%s\n' "${sha256}" >"${staging}/root/.engineering-toolchain"
    chown -R root:root "${staging}/root"
    chmod -R u+rwX,go+rX,go-w "${staging}/root"
    chmod 0755 "${staging}/root"
    toolchain_release_ok "${staging}/root" "${sha256}" "${check}" "${expected}" \
      "${version_argument}" || {
      echo "${name}: downloaded toolchain does not report ${expected}" >&2
      exit 1
    }
    # Same filesystem: the complete release appears under its final name atomically.
    mv -T -- "${staging}/root" "${release}"
    rm -rf -- "${staging}"
    trap - EXIT
  fi
  if [[ $(readlink -- "${link}" 2>/dev/null) == "${release_name}" ]]; then
    echo "${name}: ${expected} already installed"
    return 0
  fi
  ln -sfn -- "${release_name}" "${TOOLCHAIN_ROOT}/.${name}.link.new"
  mv -Tf -- "${TOOLCHAIN_ROOT}/.${name}.link.new" "${link}"
  [[ $(cd / && env -i PATH=/usr/bin:/bin HOME=/nonexistent \
    "${link}/${check}" "${version_argument}") == "${expected}" ]]
  echo "${name}: installed ${expected} (${release_name}; earlier releases are kept)"
}

install_node_toolchains() {
  [[ $(uname -m) == x86_64 ]] || {
    echo "The pinned Node.js 24 and pnpm toolchains are linux-x64 builds" >&2
    exit 1
  }
  for command_name in curl sha256sum tar xz mktemp install; do
    command -v "${command_name}" >/dev/null || {
      echo "Required command missing: ${command_name}" >&2
      exit 1
    }
  done
  install -d -o root -g root -m 0755 "${TOOLCHAIN_ROOT}"
  install_pinned_toolchain node-24 "${NODE24_URL}" "${NODE24_SHA256}" \
    J 1 bin/node "v${NODE24_VERSION}"
  install_pinned_toolchain "pnpm-${PNPM_PINNED_VERSION}" "${PNPM_PINNED_URL}" \
    "${PNPM_PINNED_SHA256}" z 0 pnpm "${PNPM_PINNED_VERSION}"
}

install_language_toolchains() {
  local tool
  install_pinned_toolchain "rust-${RUST_PINNED_VERSION}" "${RUST_PINNED_URL}" \
    "${RUST_PINNED_SHA256}" J 1 bin/rustc "${RUST_PINNED_RUSTC}" --version "${RUST_COMPONENTS}"
  install_pinned_toolchain "go-${GO_PINNED_LINE}" "${GO_PINNED_URL}" "${GO_PINNED_SHA256}" \
    z 1 bin/go "go version go${GO_PINNED_VERSION} linux/amd64" version
  # Every executable resolve_node_toolchain requires (engineering/toolchain.py).
  for tool in cargo rustc rustfmt cargo-fmt cargo-clippy clippy-driver; do
    [[ -x ${TOOLCHAIN_ROOT}/rust-${RUST_PINNED_VERSION}/bin/${tool} ]] || {
      echo "rust-${RUST_PINNED_VERSION}: ${tool} is missing" >&2
      exit 1
    }
  done
  for tool in go gofmt; do
    [[ -x ${TOOLCHAIN_ROOT}/go-${GO_PINNED_LINE}/bin/${tool} ]] || {
      echo "go-${GO_PINNED_LINE}: ${tool} is missing" >&2
      exit 1
    }
  done
}

# Shared offline toolchain caches (Settings.toolchain_cache_root): the scan worker fills
# go-mod, cargo-home and trivy with network (delivery.prepare_toolchain); makers and the
# checker (codex-runner, claude-runner) only read them. Traverse-only on the ancestors,
# read on the caches and, through default ACLs, on every future entry; scan-runner stays
# the only writer. The other toolchain cache directories (uv, pnpm, home, baselines) stay
# private to scan-runner. Called after every chmod of the ancestors (setfacl recomputes
# the mask). Idempotent.
SCAN_CACHE_ROOT=/srv/engineering/cache/scan
TOOLCHAIN_CACHE="${SCAN_CACHE_ROOT}/toolchain"
SHARED_TOOLCHAIN_CACHES=(go-mod cargo-home trivy)
grant_shared_toolchain_caches() {
  local cache
  command -v setfacl >/dev/null || {
    echo "Required command missing: setfacl" >&2
    exit 1
  }
  install -d -o scan-runner -g scan-runner -m 0700 "${SCAN_CACHE_ROOT}"
  install -d -o scan-runner -g scan-runner -m 0750 "${TOOLCHAIN_CACHE}"
  for cache in "${SHARED_TOOLCHAIN_CACHES[@]}"; do
    install -d -o scan-runner -g scan-runner -m 0750 "${TOOLCHAIN_CACHE}/${cache}"
  done
  setfacl --modify u:codex-runner:--x,u:claude-runner:--x \
    "${SCAN_CACHE_ROOT}" "${TOOLCHAIN_CACHE}"
  for cache in "${SHARED_TOOLCHAIN_CACHES[@]}"; do
    setfacl -R --modify u:codex-runner:rX,u:claude-runner:rX "${TOOLCHAIN_CACHE}/${cache}"
    find "${TOOLCHAIN_CACHE}/${cache}" -type d -exec setfacl --modify \
      d:u::rwx,d:u:codex-runner:rx,d:u:claude-runner:rx,d:g::---,d:m::rwx,d:o::--- {} +
  done
}

install_project_toolchains() {
  install_node_toolchains
  if (( LANGUAGE_TOOLCHAINS == 1 )); then
    install_language_toolchains
  fi
}

if [[ ${NODE_TOOLCHAIN_MODE} == only ]]; then
  exec 9>/run/engineering-control-bootstrap.lock
  flock -n 9 || {
    echo "Another Engineering Control bootstrap is active" >&2
    exit 1
  }
  install_project_toolchains
  if (( LANGUAGE_TOOLCHAINS == 1 )); then
    # The worker already exists (users, /srv/engineering): share the caches now too.
    grant_shared_toolchain_caches
  fi
  exit 0
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SOURCE_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
INSTALL_ROOT=/opt/engineering-control
AGENT_PACK_INSTALL_ROOT=/opt/engineering-agent-pack
AGENT_PACK_SOURCE_ROOT=${ENGINEERING_AGENT_PACK_SOURCE_ROOT:-/home/hermes/engineering/engineering-agent-pack}
CONFIG_ROOT=/etc/engineering-control
STATE_ROOT=/var/lib/engineering-control
PLATFORM_ROOT=/srv/engineering
CLI_ROOT=/opt/engineering-cli
LOG_ROOT=/var/log/engineering-control
OPERATOR_ROOT=/home/hermes/engineering

NODE_BIN=$(command -v node || true)
NPM_BIN=$(command -v npm || true)
if [[ -z "${NODE_BIN}" || -z "${NPM_BIN}" ]]; then
  NODE_BIN=/home/hermes/.local/node-v22/bin/node
  NPM_BIN=/home/hermes/.local/node-v22/bin/npm
fi
[[ -x "${NODE_BIN}" && -x "${NPM_BIN}" ]] || {
  echo "Node.js 22 and npm are required before worker bootstrap" >&2
  exit 1
}
[[ $("${NODE_BIN}" --version) == v22.* ]] || {
  echo "Worker bootstrap requires Node.js 22" >&2
  exit 1
}
NPM_CLI=$(readlink -f -- "${NPM_BIN}")
[[ -f "${NPM_CLI}" ]] || {
  echo "Unable to resolve the npm CLI" >&2
  exit 1
}
for command_name in python3 git tar flock mktemp mv setfacl systemctl install; do
  command -v "${command_name}" >/dev/null || {
    echo "Required command missing: ${command_name}" >&2
    exit 1
  }
done

exec 9>/run/engineering-control-bootstrap.lock
flock -n 9 || {
  echo "Another Engineering Control bootstrap is active" >&2
  exit 1
}

install_committed_release() {
  local source_checkout=$1
  local install_root=$2
  local release_id=$3
  local release_root="${install_root}/releases/${release_id}"
  local marker="${release_root}/.release-complete"
  if [[ -f "${marker}" ]]; then
    [[ $(<"${marker}") == "${release_id}" && -d "${release_root}/source" ]] || {
      echo "Release completion marker is inconsistent: ${release_root}" >&2
      return 1
    }
    printf '%s\n' "${release_root}"
    return
  fi
  if [[ -e "${release_root}" ]]; then
    echo "Refusing incomplete or unmarked release: ${release_root}" >&2
    return 1
  fi
  local staging
  staging=$(mktemp -d "${install_root}/releases/.${release_id}.XXXXXX")
  chmod 0755 "${staging}"
  install -d -o root -g root -m 0755 "${staging}/source"
  git -C "${source_checkout}" archive "${release_id}" | tar -C "${staging}/source" -xf -
  printf '%s\n' "${release_id}" >"${staging}/.release-complete"
  chmod 0644 "${staging}/.release-complete"
  chown -R root:root "${staging}"
  mv -T -- "${staging}" "${release_root}"
  printf '%s\n' "${release_root}"
}

for identity in codex-runner claude-runner browser-runner scan-runner delivery-runner release-runner; do
  getent group "${identity}" >/dev/null || groupadd --system "${identity}"
  id "${identity}" >/dev/null 2>&1 || useradd --system --create-home \
    --home-dir "${STATE_ROOT}/identities/${identity}" --gid "${identity}" \
    --shell /usr/sbin/nologin "${identity}"
done

install -d -o root -g root -m 0755 \
  "${INSTALL_ROOT}" "${INSTALL_ROOT}/releases" "${CLI_ROOT}" \
  "${AGENT_PACK_INSTALL_ROOT}" "${AGENT_PACK_INSTALL_ROOT}/releases"
if [[ ! -d "${OPERATOR_ROOT}" ]]; then
  install -d -o hermes -g hermes -m 0750 "${OPERATOR_ROOT}"
fi
install -d -o hermes -g hermes -m 0755 \
  "${OPERATOR_ROOT}/bootstrap" "${OPERATOR_ROOT}/docs" \
  "${OPERATOR_ROOT}/project-config"
install -d -o root -g root -m 0755 "${STATE_ROOT}"
install -d -o root -g root -m 0711 "${STATE_ROOT}/workers" "${PLATFORM_ROOT}"
install -d -o scan-runner -g scan-runner -m 0700 "${STATE_ROOT}/leases"
install -d -o root -g root -m 0755 "${CONFIG_ROOT}"
install -d -o root -g root -m 0755 "${CONFIG_ROOT}/systemd"
install -d -o root -g root -m 0751 \
  "${CONFIG_ROOT}/projects.d" "${CONFIG_ROOT}/personas.d" "${CONFIG_ROOT}/flows.d"
install -d -o root -g delivery-runner -m 0750 "${CONFIG_ROOT}/policy"
install -d -o root -g root -m 0711 "${CONFIG_ROOT}/credentials"
install -d -o root -g root -m 0750 "${LOG_ROOT}"
install -d -o root -g root -m 0711 "${CONFIG_ROOT}/env"
install -d -o root -g browser-runner -m 0750 "${CONFIG_ROOT}/journeys"
install -d -o browser-runner -g browser-runner -m 0700 "${CONFIG_ROOT}/env/browser"
install -d -o scan-runner -g scan-runner -m 0700 "${CONFIG_ROOT}/env/observer"
install -d -o root -g delivery-runner -m 0750 \
  "${CONFIG_ROOT}/env/delivery" "${CONFIG_ROOT}/env/delivery/registry"
install -d -o root -g scan-runner -m 0750 "${CONFIG_ROOT}/env/scan"
install -d -o release-runner -g release-runner -m 0700 "${CONFIG_ROOT}/env/release"
install -d -o scan-runner -g scan-runner -m 0700 "${PLATFORM_ROOT}/mirrors"
# NEW-162: the identities that read canonical mirrors (bootstrap-mirrors.sh, worktree
# allocation, delivery, release) may only traverse the root. Re-applied on every run (the
# mode above resets the ACL mask). -n with an explicit --x mask: a recalculated mask would
# re-enable any older group or named-user write entry left on the root.
setfacl -n --modify \
  u:hermes:--x,u:release-runner:--x,u:delivery-runner:--x,u:claude-runner:--x,u:codex-runner:--x,m::--x \
  "${PLATFORM_ROOT}/mirrors"
install -d -o root -g root -m 0711 \
  "${PLATFORM_ROOT}/worktrees" "${PLATFORM_ROOT}/runs" \
  "${PLATFORM_ROOT}/evidence" "${PLATFORM_ROOT}/cache" "${PLATFORM_ROOT}/tmp"
for repository in falcone falcone-charts musematic musematic-deploy llmwiki gntik-ui \
  falcone-serverless falcone-workflow \
  gntik-ai-falcone gntik-ai-falcone-charts gntik-ai-musematic gntik-ai-musematic-deploy \
  gntik-ai-llmwiki gntik-ai-gntik-ui gntik-ai-falcone-serverless gntik-ai-falcone-workflow; do
  install -d -o scan-runner -g scan-runner -m 0730 \
    "${PLATFORM_ROOT}/worktrees/${repository}"
done
install -d -o scan-runner -g scan-runner -m 0755 \
  "${PLATFORM_ROOT}/worktrees/observability-canary"
# NEW-123: flow A model inputs. scan-runner writes one directory per blueprint and grants
# codex-runner and claude-runner read on it by ACL; here they may only traverse.
install -d -o scan-runner -g scan-runner -m 0700 "${PLATFORM_ROOT}/worktrees/ui-plan-inputs"
setfacl --modify u:codex-runner:--x,u:claude-runner:--x \
  "${PLATFORM_ROOT}/worktrees/ui-plan-inputs"
for kind in codex claude browser scan delivery release; do
  owner="${kind}-runner"
  install -d -o "${owner}" -g "${owner}" -m 0750 "${STATE_ROOT}/workers/${kind}"
  install -d -o "${owner}" -g "${owner}" -m 0700 \
    "${PLATFORM_ROOT}/runs/${kind}" "${PLATFORM_ROOT}/evidence/${kind}" \
    "${PLATFORM_ROOT}/cache/${kind}" "${PLATFORM_ROOT}/tmp/${kind}"
done
install -d -o hermes -g hermes -m 0700 \
  "${STATE_ROOT}/workers/github" "${PLATFORM_ROOT}/runs/github" \
  "${PLATFORM_ROOT}/tmp/github"
install -d -o scan-runner -g scan-runner -m 0710 \
  "${PLATFORM_ROOT}/cache/scan/worktree-git"
# Shared offline toolchain caches: read-only for makers and the checker (see below).
grant_shared_toolchain_caches
for identity in codex-runner claude-runner browser-runner scan-runner delivery-runner release-runner; do
  chmod 0700 "${STATE_ROOT}/identities/${identity}"
  chown "${identity}:${identity}" "${STATE_ROOT}/identities/${identity}"
done

RELEASE_ID=$(git -C "${SOURCE_ROOT}" rev-parse --verify HEAD) || {
  echo "Engineering Control bootstrap requires a version-controlled source checkout" >&2
  exit 1
}
RELEASE_ROOT=$(install_committed_release "${SOURCE_ROOT}" "${INSTALL_ROOT}" "${RELEASE_ID}")
ln -sfn "${RELEASE_ROOT}/source" "${INSTALL_ROOT}/source"
for project in falcone musematic llmwiki gntik-ui falcone-serverless falcone-workflow; do
  install -o root -g root -m 0644 \
    "${RELEASE_ROOT}/source/config/projects/${project}.yaml" \
    "${CONFIG_ROOT}/projects.d/${project}.yaml"
  # NEW-160: credentials, personas and flows exist only for projects with a runtime;
  # gntik-ui (a UI kit), falcone-serverless and falcone-workflow (no runtime adapter
  # yet), all delivered by a protected merge, have none.
  if [[ ${project} == gntik-ui || ${project} == falcone-serverless || ${project} == falcone-workflow ]]; then
    continue
  fi
  if [[ ${project} == falcone || ${project} == musematic ]]; then
    install -d -o root -g browser-runner -m 0750 "${CONFIG_ROOT}/credentials/${project}"
  else
    install -d -o root -g root -m 0711 "${CONFIG_ROOT}/credentials/${project}"
  fi
  install -o root -g root -m 0644 \
    "${RELEASE_ROOT}/source/config/personas/${project}.yaml" \
    "${CONFIG_ROOT}/personas.d/${project}.yaml"
  install -d -o root -g root -m 0755 "${CONFIG_ROOT}/flows.d/${project}"
  for flow in "${RELEASE_ROOT}/source/config/flows/${project}"/*.yaml; do
    install -o root -g root -m 0644 "${flow}" "${CONFIG_ROOT}/flows.d/${project}/"
  done
done
AGENT_PACK_RELEASE_ID=$(git -C "${AGENT_PACK_SOURCE_ROOT}" rev-parse --verify HEAD) || {
  echo "Agent-pack bootstrap requires its independent version-controlled checkout" >&2
  exit 1
}
AGENT_PACK_RELEASE_ROOT=$(install_committed_release \
  "${AGENT_PACK_SOURCE_ROOT}" "${AGENT_PACK_INSTALL_ROOT}" "${AGENT_PACK_RELEASE_ID}")
ln -sfn "${AGENT_PACK_RELEASE_ROOT}/source" "${AGENT_PACK_INSTALL_ROOT}/source"

python3 -m venv "${INSTALL_ROOT}/venv"
"${INSTALL_ROOT}/venv/bin/pip" install --disable-pip-version-check --upgrade \
  --constraint "${INSTALL_ROOT}/source/requirements.lock" "${INSTALL_ROOT}/source"
chmod -R a+rX "${INSTALL_ROOT}/venv"
"${INSTALL_ROOT}/venv/bin/python" \
  "${AGENT_PACK_INSTALL_ROOT}/source/generators/generate.py" --check
"${INSTALL_ROOT}/venv/bin/python" \
  "${AGENT_PACK_INSTALL_ROOT}/source/generators/validate.py"
"${INSTALL_ROOT}/source/scripts/install-scanners.sh"

NODE_SOURCE=$(readlink -f -- "${NODE_BIN}")
if [[ ${NODE_SOURCE} != /usr/local/bin/node ]]; then
  install -o root -g root -m 0755 "${NODE_SOURCE}" /usr/local/bin/.engineering-node.new
  mv -f -- /usr/local/bin/.engineering-node.new /usr/local/bin/node
fi
"/usr/local/bin/node" "${NPM_CLI}" install --prefix "${CLI_ROOT}" \
  @openai/codex@0.159.3 @anthropic-ai/claude-code@2.1.239
chmod -R a+rX "${CLI_ROOT}"
ln -sfn "${CLI_ROOT}/node_modules/.bin/codex" /usr/local/bin/codex
ln -sfn "${CLI_ROOT}/node_modules/.bin/claude" /usr/local/bin/claude
if [[ ${NODE_TOOLCHAIN_MODE} == also ]]; then
  install_project_toolchains
fi

install -d -o root -g root -m 0755 \
  /opt/engineering-browser /opt/engineering-browser-runtimes
BROWSER_RELEASE_ROOT=/opt/engineering-browser-runtimes/${RELEASE_ID}
if [[ ! -f ${BROWSER_RELEASE_ROOT}/.release-complete ]]; then
  [[ ! -e ${BROWSER_RELEASE_ROOT} ]] || {
    echo "Refusing incomplete browser runtime: ${BROWSER_RELEASE_ROOT}" >&2
    exit 1
  }
  (
    BROWSER_STAGING=$(mktemp -d "/opt/engineering-browser-runtimes/.${RELEASE_ID}.XXXXXX")
    trap 'rm -rf -- "${BROWSER_STAGING}"' EXIT
    cp -a "${RELEASE_ROOT}/source/browser/." "${BROWSER_STAGING}/"
    cd "${BROWSER_STAGING}"
    "/usr/local/bin/node" "${NPM_CLI}" ci --omit=dev --ignore-scripts --no-audit --no-fund
    [[ $("/usr/local/bin/node" node_modules/pnpm/bin/pnpm.cjs --version) == 10.34.5 ]]
    [[ $(env -i PATH=/usr/local/bin:/usr/bin:/bin \
      node_modules/.bin/pnpm --version) == 10.34.5 ]]
    printf '%s\n' "${RELEASE_ID}" >.release-complete
    chmod -R a+rX,go-w "${BROWSER_STAGING}"
    chown -R root:root "${BROWSER_STAGING}"
    mv -T -- "${BROWSER_STAGING}" "${BROWSER_RELEASE_ROOT}"
    trap - EXIT
  )
fi
[[ $(<"${BROWSER_RELEASE_ROOT}/.release-complete") == "${RELEASE_ID}" ]]
"${NODE_BIN}" "${BROWSER_RELEASE_ROOT}/node_modules/@playwright/test/cli.js" \
  install-deps chromium firefox
PLAYWRIGHT_BROWSERS_PATH=/opt/engineering-browser/browsers \
  "${NODE_BIN}" "${BROWSER_RELEASE_ROOT}/node_modules/@playwright/test/cli.js" \
  install chromium firefox
ln -sfn "${BROWSER_RELEASE_ROOT}" /opt/engineering-browser/.runtime.new
mv -Tf -- /opt/engineering-browser/.runtime.new /opt/engineering-browser/runtime
chmod -R a+rX,go-w /opt/engineering-browser
chown -R root:root /opt/engineering-browser

if [[ ! -f "${CONFIG_ROOT}/env/worker-common.env" ]]; then
  {
    printf 'ENGINEERING_ENVIRONMENT=production\n'
    printf 'ENGINEERING_TEMPORAL_TARGET=10.10.0.2:7233\n'
    printf 'ENGINEERING_TEMPORAL_NAMESPACE=engineering-control\n'
    printf 'ENGINEERING_TEMPORAL_TASK_QUEUE=engineering-control\n'
    printf 'ENGINEERING_SYNTHETIC_MODE=false\n'
    printf 'ENGINEERING_AGENT_PACK_ROOT=%s/source\n' "${AGENT_PACK_INSTALL_ROOT}"
    printf 'ENGINEERING_CLEANUP_EXECUTION_ENABLED=false\n'
    printf 'PLAYWRIGHT_BROWSERS_PATH=/opt/engineering-browser/browsers\n'
  } >"${CONFIG_ROOT}/env/worker-common.env"
  chmod 0644 "${CONFIG_ROOT}/env/worker-common.env"
fi
if ! grep -q '^ENGINEERING_AGENT_PACK_ROOT=' "${CONFIG_ROOT}/env/worker-common.env"; then
  printf 'ENGINEERING_AGENT_PACK_ROOT=%s/source\n' \
    "${AGENT_PACK_INSTALL_ROOT}" >>"${CONFIG_ROOT}/env/worker-common.env"
fi
{
  printf 'ENGINEERING_EVIDENCE_INGEST_URL=http://10.10.0.2:8750/v1/evidence\n'
  printf 'ENGINEERING_EVIDENCE_MAX_BYTES=33554432\n'
} >"${CONFIG_ROOT}/env/evidence-upload.env"
chmod 0644 "${CONFIG_ROOT}/env/evidence-upload.env"
evidence_tokens_ready=true
for identity in codex-runner claude-runner browser-runner scan-runner delivery-runner release-runner; do
  token_path="${CONFIG_ROOT}/env/evidence-${identity}.token"
  if [[ -f "${token_path}" ]]; then
    chown root:"${identity}" "${token_path}"
    chmod 0640 "${token_path}"
  else
    evidence_tokens_ready=false
  fi
done
{
  printf 'ENGINEERING_GITHUB_REGISTRY_AUTH_FILE=%s/env/delivery/registry/config.json\n' \
    "${CONFIG_ROOT}"
  printf 'ENGINEERING_DELIVERY_CREDENTIAL_ROOT=%s/env/delivery\n' "${CONFIG_ROOT}"
} >"${CONFIG_ROOT}/env/delivery-worker.env"
chown root:delivery-runner "${CONFIG_ROOT}/env/delivery-worker.env"
chmod 0640 "${CONFIG_ROOT}/env/delivery-worker.env"
printf 'ENGINEERING_GITHUB_WRITES_ENABLED=false\n' >"${CONFIG_ROOT}/env/scan-worker.env"
chown root:scan-runner "${CONFIG_ROOT}/env/scan-worker.env"
chmod 0640 "${CONFIG_ROOT}/env/scan-worker.env"
if ! grep -q '^ENGINEERING_GITHUB_REGISTRY_AUTH_FILE=' \
  "${CONFIG_ROOT}/env/delivery-worker.env"; then
  printf 'ENGINEERING_GITHUB_REGISTRY_AUTH_FILE=%s/env/delivery/registry/config.json\n' \
    "${CONFIG_ROOT}" >>"${CONFIG_ROOT}/env/delivery-worker.env"
fi
if [[ ! -f "${CONFIG_ROOT}/env/delivery/registry/config.json" ]]; then
  touch "${STATE_ROOT}/MANUAL_GITHUB_REGISTRY_AUTH_REQUIRED"
else
  chown root:delivery-runner "${CONFIG_ROOT}/env/delivery/registry/config.json"
  chmod 0640 "${CONFIG_ROOT}/env/delivery/registry/config.json"
  rm -f -- "${STATE_ROOT}/MANUAL_GITHUB_REGISTRY_AUTH_REQUIRED"
fi
if [[ ${evidence_tokens_ready} == true ]]; then
  if [[ -f "${STATE_ROOT}/MANUAL_EVIDENCE_INGEST_TOKEN_REQUIRED" ]]; then
    rm -- "${STATE_ROOT}/MANUAL_EVIDENCE_INGEST_TOKEN_REQUIRED"
  fi
else
  touch "${STATE_ROOT}/MANUAL_EVIDENCE_INGEST_TOKEN_REQUIRED"
fi

# NEW-167: the workers' aggregate memory slice and the agents' nested slice come first.
for unit in engineering-workers.slice engineering-workers-agents.slice \
  engineering-worker@.service engineering-release-worker.service; do
  install -o root -g root -m 0644 \
    "${RELEASE_ROOT}/source/systemd/${unit}" "${CONFIG_ROOT}/systemd/${unit}"
  install -o root -g root -m 0644 \
    "${CONFIG_ROOT}/systemd/${unit}" "/etc/systemd/system/${unit}"
done
for agent in codex claude; do
  install -d -o root -g root -m 0755 "/etc/systemd/system/engineering-worker@${agent}.service.d"
  install -o root -g root -m 0644 \
    "${RELEASE_ROOT}/source/systemd/engineering-worker@${agent}.service.d/50-memory.conf" \
    "/etc/systemd/system/engineering-worker@${agent}.service.d/50-memory.conf"
  # NEW-188: per-run agent cgroups.
  install -o root -g root -m 0644 \
    "${RELEASE_ROOT}/source/systemd/engineering-worker@${agent}.service.d/60-cgroup-delegation.conf" \
    "/etc/systemd/system/engineering-worker@${agent}.service.d/60-cgroup-delegation.conf"
done
# NEW-228: no mounts over the claude worker's /proc (the loopback shell mounts a procfs of
# its own and re-protects it); codex keeps ProtectKernelTunables=true.
install -o root -g root -m 0644 \
  "${RELEASE_ROOT}/source/systemd/engineering-worker@claude.service.d/70-loopback-proc.conf" \
  "/etc/systemd/system/engineering-worker@claude.service.d/70-loopback-proc.conf"
systemd-analyze verify /etc/systemd/system/engineering-workers.slice \
  /etc/systemd/system/engineering-workers-agents.slice \
  /etc/systemd/system/engineering-worker@.service \
  /etc/systemd/system/engineering-release-worker.service
"${INSTALL_ROOT}/venv/bin/python" \
  "${RELEASE_ROOT}/source/scripts/verify-worker-registry.py" --host worker
systemctl daemon-reload
systemctl enable engineering-worker@codex.service engineering-worker@claude.service \
  engineering-worker@browser.service engineering-worker@scan.service \
  engineering-worker@delivery.service \
  engineering-release-worker.service
"${RELEASE_ROOT}/source/scripts/verify-no-omniroute.sh" --scope worker
systemctl restart engineering-worker@codex.service engineering-worker@claude.service \
  engineering-worker@browser.service engineering-worker@scan.service \
  engineering-worker@delivery.service \
  engineering-release-worker.service

# NEW-172: vitest 5 keeps an API token at $HOME/.local/share/vitest/.vitest-secret-token.
# The agent sandbox cannot write HOME, so vitest fell back to <worktree>/node_modules/
# .vitest (created 0700/0600), which the scan worker's `git clean` cannot remove
# (gntik-ui #25/#26). An existing token file is only read, so pre-create it once per
# agent identity; an existing one is kept.
for runner in codex-runner claude-runner; do
  sudo -u "${runner}" -H env -i HOME="${STATE_ROOT}/identities/${runner}" \
    PATH=/usr/local/bin:/usr/bin:/bin sh -c '
      umask 077
      dir="$HOME/.local/share/vitest"
      mkdir -p "$dir"
      [ -e "$dir/.vitest-secret-token" ] || cat /proc/sys/kernel/random/uuid >"$dir/.vitest-secret-token"
    '
done

if sudo -u codex-runner -H env -i \
  HOME="${STATE_ROOT}/identities/codex-runner" PATH=/usr/local/bin:/usr/bin:/bin \
  /usr/local/bin/codex login status >/dev/null 2>&1; then
  rm -f -- "${STATE_ROOT}/MANUAL_AUTH_REQUIRED_CODEX"
else
  touch "${STATE_ROOT}/MANUAL_AUTH_REQUIRED_CODEX"
fi
if sudo -u claude-runner -H env -i \
  HOME="${STATE_ROOT}/identities/claude-runner" PATH=/usr/local/bin:/usr/bin:/bin \
  /usr/local/bin/claude auth status >/dev/null 2>&1; then
  rm -f -- "${STATE_ROOT}/MANUAL_AUTH_REQUIRED_CLAUDE"
else
  touch "${STATE_ROOT}/MANUAL_AUTH_REQUIRED_CLAUDE"
fi
"${RELEASE_ROOT}/source/scripts/verify-no-omniroute.sh" --scope worker
echo "Worker bootstrap completed; inspect MANUAL_AUTH_REQUIRED markers without copying credentials."
