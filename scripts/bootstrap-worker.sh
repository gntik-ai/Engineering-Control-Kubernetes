#!/usr/bin/env bash
set -euo pipefail
umask 027

if [[ ${EUID} -ne 0 ]]; then
  echo "Run on worker with sudo: sudo $0" >&2
  exit 1
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SOURCE_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
INSTALL_ROOT=/opt/engineering-control
CONFIG_ROOT=/etc/engineering-control
STATE_ROOT=/var/lib/engineering-control
PLATFORM_ROOT=/srv/engineering
CLI_ROOT=/opt/engineering-cli

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
for command_name in python3 git setfacl systemctl install; do
  command -v "${command_name}" >/dev/null || {
    echo "Required command missing: ${command_name}" >&2
    exit 1
  }
done

for identity in codex-runner claude-runner browser-runner scan-runner delivery-runner release-runner; do
  getent group "${identity}" >/dev/null || groupadd --system "${identity}"
  id "${identity}" >/dev/null 2>&1 || useradd --system --create-home \
    --home-dir "${STATE_ROOT}/identities/${identity}" --gid "${identity}" \
    --shell /usr/sbin/nologin "${identity}"
done

install -d -o root -g root -m 0755 \
  "${INSTALL_ROOT}" "${INSTALL_ROOT}/releases" "${CLI_ROOT}"
install -d -o root -g root -m 0755 "${CONFIG_ROOT}"
install -d -o root -g root -m 0711 "${CONFIG_ROOT}/env"
install -d -o root -g browser-runner -m 0750 "${CONFIG_ROOT}/journeys"
install -d -o browser-runner -g browser-runner -m 0700 "${CONFIG_ROOT}/env/browser"
install -d -o scan-runner -g scan-runner -m 0700 "${CONFIG_ROOT}/env/observer"
install -d -o root -g delivery-runner -m 0750 \
  "${CONFIG_ROOT}/env/delivery" "${CONFIG_ROOT}/env/delivery/registry"
install -d -o release-runner -g release-runner -m 0700 "${CONFIG_ROOT}/env/release"
install -d -o scan-runner -g scan-runner -m 0700 "${PLATFORM_ROOT}/mirrors"
install -d -o root -g root -m 0711 \
  "${PLATFORM_ROOT}/worktrees" "${PLATFORM_ROOT}/runs" \
  "${PLATFORM_ROOT}/evidence" "${PLATFORM_ROOT}/cache" "${PLATFORM_ROOT}/tmp"
for repository in falcone falcone-charts musematic musematic-deploy; do
  install -d -o scan-runner -g scan-runner -m 0730 \
    "${PLATFORM_ROOT}/worktrees/${repository}"
done
for kind in codex claude browser scan delivery release; do
  owner="${kind}-runner"
  install -d -o "${owner}" -g "${owner}" -m 0750 "${STATE_ROOT}/workers/${kind}"
  install -d -o "${owner}" -g "${owner}" -m 0700 \
    "${PLATFORM_ROOT}/runs/${kind}" "${PLATFORM_ROOT}/evidence/${kind}" \
    "${PLATFORM_ROOT}/cache/${kind}" "${PLATFORM_ROOT}/tmp/${kind}"
done
install -d -o scan-runner -g scan-runner -m 0710 \
  "${PLATFORM_ROOT}/cache/scan/worktree-git"
for identity in codex-runner claude-runner browser-runner scan-runner delivery-runner release-runner; do
  chmod 0700 "${STATE_ROOT}/identities/${identity}"
  chown "${identity}:${identity}" "${STATE_ROOT}/identities/${identity}"
done

if git -C "${SOURCE_ROOT}" rev-parse --verify HEAD >/dev/null 2>&1; then
  RELEASE_ID=$(git -C "${SOURCE_ROOT}" rev-parse HEAD)
else
  RELEASE_ID=uncommitted-bootstrap
fi
RELEASE_ROOT="${INSTALL_ROOT}/releases/${RELEASE_ID}"
if [[ ! -d "${RELEASE_ROOT}/source" ]]; then
  install -d -o root -g root -m 0755 "${RELEASE_ROOT}/source"
  tar --exclude=.git --exclude=.venv -C "${SOURCE_ROOT}" -cf - . | \
    tar -C "${RELEASE_ROOT}/source" -xf -
fi
ln -sfn "${RELEASE_ROOT}/source" "${INSTALL_ROOT}/source"
python3 -m venv "${INSTALL_ROOT}/venv"
"${INSTALL_ROOT}/venv/bin/pip" install --disable-pip-version-check --upgrade \
  --constraint "${INSTALL_ROOT}/source/requirements.lock" "${INSTALL_ROOT}/source"
"${INSTALL_ROOT}/source/scripts/install-scanners.sh"

NODE_SOURCE=$(readlink -f -- "${NODE_BIN}")
if [[ ${NODE_SOURCE} != /usr/local/bin/node ]]; then
  install -o root -g root -m 0755 "${NODE_SOURCE}" /usr/local/bin/.engineering-node.new
  mv -f -- /usr/local/bin/.engineering-node.new /usr/local/bin/node
fi
"/usr/local/bin/node" "${NPM_CLI}" install --prefix "${CLI_ROOT}" \
  @openai/codex@0.149.0 @anthropic-ai/claude-code@2.1.239
ln -sfn "${CLI_ROOT}/node_modules/.bin/codex" /usr/local/bin/codex
ln -sfn "${CLI_ROOT}/node_modules/.bin/claude" /usr/local/bin/claude

install -d -o browser-runner -g browser-runner -m 0750 /opt/engineering-browser
cp -a "${SOURCE_ROOT}/browser/." /opt/engineering-browser/
cd /opt/engineering-browser
"/usr/local/bin/node" "${NPM_CLI}" install --omit=dev
"${NODE_BIN}" node_modules/@playwright/test/cli.js install-deps chromium firefox
PLAYWRIGHT_BROWSERS_PATH=/opt/engineering-browser/browsers \
  "${NODE_BIN}" node_modules/@playwright/test/cli.js install chromium firefox
chown -R browser-runner:browser-runner /opt/engineering-browser

if [[ ! -f "${CONFIG_ROOT}/env/worker-common.env" ]]; then
  {
    printf 'ENGINEERING_ENVIRONMENT=production\n'
    printf 'ENGINEERING_TEMPORAL_TARGET=10.10.0.2:7233\n'
    printf 'ENGINEERING_TEMPORAL_NAMESPACE=engineering-control\n'
    printf 'ENGINEERING_TEMPORAL_TASK_QUEUE=engineering-control\n'
    printf 'ENGINEERING_SYNTHETIC_MODE=false\n'
    printf 'ENGINEERING_CLEANUP_EXECUTION_ENABLED=false\n'
    printf 'PLAYWRIGHT_BROWSERS_PATH=/opt/engineering-browser/browsers\n'
  } >"${CONFIG_ROOT}/env/worker-common.env"
  chmod 0644 "${CONFIG_ROOT}/env/worker-common.env"
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
if [[ ! -f "${CONFIG_ROOT}/env/delivery-worker.env" ]]; then
  {
    printf 'ENGINEERING_GITHUB_TOKEN_FILE=%s/env/delivery/github.token\n' "${CONFIG_ROOT}"
    printf 'ENGINEERING_GITHUB_REGISTRY_AUTH_FILE=%s/env/delivery/registry/config.json\n' \
      "${CONFIG_ROOT}"
    printf 'ENGINEERING_DELIVERY_CREDENTIAL_ROOT=%s/env/delivery\n' "${CONFIG_ROOT}"
    printf 'ENGINEERING_GITHUB_EXECUTABLE=/usr/local/bin/gh\n'
  } >"${CONFIG_ROOT}/env/delivery-worker.env"
  chown root:delivery-runner "${CONFIG_ROOT}/env/delivery-worker.env"
  chmod 0640 "${CONFIG_ROOT}/env/delivery-worker.env"
fi
if ! grep -q '^ENGINEERING_GITHUB_REGISTRY_AUTH_FILE=' \
  "${CONFIG_ROOT}/env/delivery-worker.env"; then
  printf 'ENGINEERING_GITHUB_REGISTRY_AUTH_FILE=%s/env/delivery/registry/config.json\n' \
    "${CONFIG_ROOT}" >>"${CONFIG_ROOT}/env/delivery-worker.env"
fi
if [[ ! -f "${CONFIG_ROOT}/env/delivery/github.token" ]]; then
  touch "${STATE_ROOT}/MANUAL_GITHUB_DELIVERY_CREDENTIAL_REQUIRED"
else
  chown root:delivery-runner "${CONFIG_ROOT}/env/delivery/github.token"
  chmod 0640 "${CONFIG_ROOT}/env/delivery/github.token"
  rm -f -- "${STATE_ROOT}/MANUAL_GITHUB_DELIVERY_CREDENTIAL_REQUIRED"
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

install -o root -g root -m 0644 "${SOURCE_ROOT}/systemd/engineering-worker@.service" \
  /etc/systemd/system/engineering-worker@.service
install -o root -g root -m 0644 "${SOURCE_ROOT}/systemd/engineering-release-worker.service" \
  /etc/systemd/system/engineering-release-worker.service
systemd-analyze verify /etc/systemd/system/engineering-worker@.service \
  /etc/systemd/system/engineering-release-worker.service
systemctl daemon-reload
systemctl enable --now engineering-worker@codex.service engineering-worker@claude.service \
  engineering-worker@browser.service engineering-worker@scan.service \
  engineering-worker@delivery.service \
  engineering-release-worker.service

if ! sudo -u codex-runner -H /usr/local/bin/codex login status >/dev/null 2>&1; then
  touch "${STATE_ROOT}/MANUAL_AUTH_REQUIRED_CODEX"
fi
if ! sudo -u claude-runner -H /usr/local/bin/claude auth status >/dev/null 2>&1; then
  touch "${STATE_ROOT}/MANUAL_AUTH_REQUIRED_CLAUDE"
fi
echo "Worker bootstrap completed; inspect MANUAL_AUTH_REQUIRED markers without copying credentials."
