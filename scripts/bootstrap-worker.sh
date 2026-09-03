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
install -d -o root -g root -m 0711 \
  "${PLATFORM_ROOT}/worktrees" "${PLATFORM_ROOT}/runs" \
  "${PLATFORM_ROOT}/evidence" "${PLATFORM_ROOT}/cache" "${PLATFORM_ROOT}/tmp"
for repository in falcone falcone-charts musematic musematic-deploy llmwiki \
  gntik-ai-falcone gntik-ai-falcone-charts gntik-ai-musematic gntik-ai-musematic-deploy \
  gntik-ai-llmwiki; do
  install -d -o scan-runner -g scan-runner -m 0730 \
    "${PLATFORM_ROOT}/worktrees/${repository}"
done
install -d -o scan-runner -g scan-runner -m 0755 \
  "${PLATFORM_ROOT}/worktrees/observability-canary"
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
for project in falcone musematic llmwiki; do
  if [[ ${project} == falcone || ${project} == musematic ]]; then
    install -d -o root -g browser-runner -m 0750 "${CONFIG_ROOT}/credentials/${project}"
  else
    install -d -o root -g root -m 0711 "${CONFIG_ROOT}/credentials/${project}"
  fi
  install -o root -g root -m 0644 \
    "${RELEASE_ROOT}/source/config/projects/${project}.yaml" \
    "${CONFIG_ROOT}/projects.d/${project}.yaml"
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
  @openai/codex@0.149.0 @anthropic-ai/claude-code@2.1.239
chmod -R a+rX "${CLI_ROOT}"
ln -sfn "${CLI_ROOT}/node_modules/.bin/codex" /usr/local/bin/codex
ln -sfn "${CLI_ROOT}/node_modules/.bin/claude" /usr/local/bin/claude

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

for unit in engineering-worker@.service engineering-release-worker.service; do
  install -o root -g root -m 0644 \
    "${RELEASE_ROOT}/source/systemd/${unit}" "${CONFIG_ROOT}/systemd/${unit}"
  install -o root -g root -m 0644 \
    "${CONFIG_ROOT}/systemd/${unit}" "/etc/systemd/system/${unit}"
done
systemd-analyze verify /etc/systemd/system/engineering-worker@.service \
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
