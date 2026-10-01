#!/usr/bin/env bash
set -euo pipefail
umask 027

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo: sudo $0" >&2
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
LOG_ROOT=/var/log/engineering-control
OPERATOR_ROOT=/home/hermes/engineering

for command_name in python3 docker git tar flock mktemp mv openssl systemctl install setfacl runuser; do
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

getent group engineering-control >/dev/null || groupadd --system engineering-control
getent group engineering-api-clients >/dev/null || groupadd --system engineering-api-clients
id engineering-control >/dev/null 2>&1 || useradd \
  --system --gid engineering-control --home-dir "${STATE_ROOT}" --shell /usr/sbin/nologin \
  engineering-control
usermod -a -G engineering-api-clients engineering-control
usermod -a -G engineering-api-clients hermes

install -d -o root -g root -m 0755 \
  "${INSTALL_ROOT}" "${INSTALL_ROOT}/releases" \
  "${AGENT_PACK_INSTALL_ROOT}" "${AGENT_PACK_INSTALL_ROOT}/releases"
if [[ ! -d "${OPERATOR_ROOT}" ]]; then
  install -d -o hermes -g hermes -m 0750 "${OPERATOR_ROOT}"
fi
install -d -o hermes -g hermes -m 0755 \
  "${OPERATOR_ROOT}/bootstrap" "${OPERATOR_ROOT}/docs" \
  "${OPERATOR_ROOT}/project-config"
install -d -o root -g engineering-control -m 0751 "${CONFIG_ROOT}" \
  "${CONFIG_ROOT}/projects.d" "${CONFIG_ROOT}/personas.d" \
  "${CONFIG_ROOT}/flows.d" "${CONFIG_ROOT}/policies" \
  "${CONFIG_ROOT}/targets" \
  "${CONFIG_ROOT}/runtime" "${CONFIG_ROOT}/systemd"
install -d -o root -g engineering-control -m 0750 "${CONFIG_ROOT}/policy"
install -d -o engineering-control -g engineering-control -m 0700 \
  "${STATE_ROOT}/github-policy-requests"
install -d -o root -g root -m 0700 "${STATE_ROOT}/github-policy-backups"
install -d -o root -g root -m 0711 "${CONFIG_ROOT}/credentials"
install -d -o root -g root -m 0711 "${CONFIG_ROOT}/credentials/github"
install -d -o root -g engineering-control -m 0751 "${CONFIG_ROOT}/env"
install -d -o root -g engineering-control -m 0750 "${LOG_ROOT}"
install -d -o engineering-control -g engineering-control -m 0750 "${STATE_ROOT}" \
  "${STATE_ROOT}/leases"
setfacl -m u:hermes:--x "${STATE_ROOT}"
install -d -o root -g root -m 0711 "${STATE_ROOT}/workers"
install -d -o hermes -g hermes -m 0700 "${STATE_ROOT}/workers/github"
install -d -o root -g root -m 0700 "${STATE_ROOT}/postgresql"
chown 70:70 "${STATE_ROOT}/postgresql"
install -d -o root -g root -m 0711 "${PLATFORM_ROOT}"
install -d -o engineering-control -g engineering-control -m 0750 \
  "${PLATFORM_ROOT}/mirrors" "${PLATFORM_ROOT}/worktrees" "${PLATFORM_ROOT}/runs" \
  "${PLATFORM_ROOT}/evidence" "${PLATFORM_ROOT}/cache" "${PLATFORM_ROOT}/tmp"
setfacl -m u:hermes:rwx \
  "${PLATFORM_ROOT}/mirrors" "${PLATFORM_ROOT}/worktrees" \
  "${PLATFORM_ROOT}/runs" "${PLATFORM_ROOT}/tmp"
install -d -o engineering-control -g engineering-control -m 0750 \
  "${PLATFORM_ROOT}/tmp/acceptance"

RELEASE_ID=$(git -C "${SOURCE_ROOT}" rev-parse --verify HEAD) || {
  echo "Engineering Control bootstrap requires a version-controlled source checkout" >&2
  exit 1
}
RELEASE_ROOT=$(install_committed_release "${SOURCE_ROOT}" "${INSTALL_ROOT}" "${RELEASE_ID}")
ln -sfn "${RELEASE_ROOT}/source" "${INSTALL_ROOT}/source"
ln -sfn "${INSTALL_ROOT}/source/docker" "${INSTALL_ROOT}/infrastructure"

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

install -o root -g engineering-control -m 0640 \
  "${RELEASE_ROOT}/source/config/control.yaml" "${CONFIG_ROOT}/control.yaml"
for project_manifest in "${RELEASE_ROOT}"/source/config/projects/*.yaml; do
  project_name=$(basename "${project_manifest}")
  install -o root -g engineering-control -m 0640 \
    "${project_manifest}" "${CONFIG_ROOT}/projects.d/${project_name}"
done
for persona in "${RELEASE_ROOT}"/source/config/personas/*.yaml; do
  install -o root -g engineering-control -m 0640 "${persona}" "${CONFIG_ROOT}/personas.d/"
done
for project_flows in "${RELEASE_ROOT}"/source/config/flows/*; do
  project_name=$(basename "${project_flows}")
  install -d -o root -g engineering-control -m 0750 "${CONFIG_ROOT}/flows.d/${project_name}"
  for flow in "${project_flows}"/*.yaml; do
    install -o root -g engineering-control -m 0640 \
      "${flow}" "${CONFIG_ROOT}/flows.d/${project_name}/"
  done
done
for policy in "${RELEASE_ROOT}"/source/config/policies/*.yaml; do
  install -o root -g engineering-control -m 0640 "${policy}" "${CONFIG_ROOT}/policies/"
done
"${INSTALL_ROOT}/venv/bin/python" \
  "${RELEASE_ROOT}/source/scripts/reconcile-github-write-policy.py" \
  --template "${RELEASE_ROOT}/source/config/policy/github-issue-write-validation.yaml" \
  --policy "${CONFIG_ROOT}/policy/github-issue-write-validation.yaml" \
  --backup-root /var/backups/engineering-control/policy-migrations
for target in "${RELEASE_ROOT}"/source/config/targets/*.yaml; do
  target_name=$(basename "${target}")
  if [[ ! -f "${CONFIG_ROOT}/targets/${target_name}" ]]; then
    install -o root -g engineering-control -m 0640 \
      "${target}" "${CONFIG_ROOT}/targets/${target_name}"
  fi
done
for runtime_target in "${RELEASE_ROOT}"/source/config/runtime/*.yaml; do
  runtime_name=$(basename "${runtime_target}")
  if [[ ! -f "${CONFIG_ROOT}/runtime/${runtime_name}" ]]; then
    install -o root -g engineering-control -m 0640 \
      "${runtime_target}" "${CONFIG_ROOT}/runtime/${runtime_name}"
  fi
done

if [[ ! -f "${CONFIG_ROOT}/env/infrastructure.env" ]]; then
  POSTGRES_ADMIN_PASSWORD=$(openssl rand -hex 32)
  ENGINEERING_DB_PASSWORD=$(openssl rand -hex 32)
  {
    printf 'POSTGRES_ADMIN_USER=engineering_admin\n'
    printf 'POSTGRES_ADMIN_PASSWORD=%s\n' "${POSTGRES_ADMIN_PASSWORD}"
    printf 'ENGINEERING_DB_PASSWORD=%s\n' "${ENGINEERING_DB_PASSWORD}"
  } >"${CONFIG_ROOT}/env/infrastructure.env"
  chmod 0600 "${CONFIG_ROOT}/env/infrastructure.env"
fi

if [[ ! -f "${CONFIG_ROOT}/env/api.token" ]]; then
  openssl rand -hex 48 >"${CONFIG_ROOT}/env/api.token"
fi
chown root:engineering-api-clients "${CONFIG_ROOT}/env/api.token"
chmod 0640 "${CONFIG_ROOT}/env/api.token"
# The existing Hermes user manager may predate engineering-api-clients group
# enrollment. A named read-only ACL makes the narrow API credential available
# immediately without weakening mode bits or requiring a disruptive re-login.
setfacl -m u:hermes:r-- "${CONFIG_ROOT}/env/api.token"

install -d -o root -g engineering-control -m 0750 \
  "${CONFIG_ROOT}/env/evidence-uploaders"
for identity in codex-runner claude-runner browser-runner scan-runner delivery-runner \
  release-runner github-runner; do
  token_path="${CONFIG_ROOT}/env/evidence-uploaders/${identity}.token"
  if [[ ! -f "${token_path}" ]]; then
    openssl rand -hex 48 >"${token_path}"
  fi
  chown root:engineering-control "${token_path}"
  chmod 0640 "${token_path}"
done
{
  printf 'ENGINEERING_EVIDENCE_INGEST_HOST=10.10.0.2\n'
  printf 'ENGINEERING_EVIDENCE_INGEST_PORT=8750\n'
  printf 'ENGINEERING_EVIDENCE_INGEST_TOKEN_DIRECTORY=%s/env/evidence-uploaders\n' \
    "${CONFIG_ROOT}"
  printf 'ENGINEERING_EVIDENCE_MAX_BYTES=33554432\n'
} >"${CONFIG_ROOT}/env/evidence-ingest.env"
chown root:engineering-control "${CONFIG_ROOT}/env/evidence-ingest.env"
chmod 0640 "${CONFIG_ROOT}/env/evidence-ingest.env"

if [[ ! -f "${CONFIG_ROOT}/env/telegram-allowlist" ]]; then
  printf '# One authorized numeric Telegram user ID per line.\n' \
    >"${CONFIG_ROOT}/env/telegram-allowlist"
fi
chown root:hermes "${CONFIG_ROOT}/env/telegram-allowlist"
chmod 0640 "${CONFIG_ROOT}/env/telegram-allowlist"

HERMES_HOME=/home/hermes/.hermes
HERMES_INTEGRATION="${RELEASE_ROOT}/source/integrations/hermes-engineering-control"
install -d -o root -g hermes -m 0750 "${HERMES_HOME}/plugins"
install -d -o hermes -g hermes -m 0750 \
  "${HERMES_HOME}/plugins/engineering-control" "${HERMES_HOME}/hooks/engineering-control"
install -o hermes -g hermes -m 0640 \
  "${HERMES_INTEGRATION}/plugin/plugin.yaml" \
  "${HERMES_HOME}/plugins/engineering-control/plugin.yaml"
install -o hermes -g hermes -m 0640 \
  "${HERMES_INTEGRATION}/plugin/__init__.py" \
  "${HERMES_HOME}/plugins/engineering-control/__init__.py"
install -o hermes -g hermes -m 0640 \
  "${HERMES_INTEGRATION}/hook/HOOK.yaml" \
  "${HERMES_HOME}/hooks/engineering-control/HOOK.yaml"
install -o hermes -g hermes -m 0640 \
  "${HERMES_INTEGRATION}/hook/handler.py" \
  "${HERMES_HOME}/hooks/engineering-control/handler.py"
if [[ -f "${HERMES_HOME}/config.yaml" ]]; then
  runuser -u hermes -- "${INSTALL_ROOT}/venv/bin/python" \
    "${RELEASE_ROOT}/source/scripts/verify-hermes-policy.py" \
    --config "${HERMES_HOME}/config.yaml" \
    --hermes-home "${HERMES_HOME}" \
    --apply-telegram-lockdown \
    --backup-root /home/hermes/engineering-backups
  runuser -u hermes -- env \
    HOME=/home/hermes \
    PYTHONPATH=/home/hermes/.hermes/hermes-agent \
    /home/hermes/.hermes/hermes-agent/venv/bin/python \
    "${RELEASE_ROOT}/source/scripts/verify-hermes-runtime.py"
fi

if [[ ! -f "${CONFIG_ROOT}/env/control.env" ]]; then
  ENGINEERING_DB_PASSWORD=$(sed -n 's/^ENGINEERING_DB_PASSWORD=//p' \
    "${CONFIG_ROOT}/env/infrastructure.env")
  {
    printf 'ENGINEERING_ENVIRONMENT=production\n'
    printf 'ENGINEERING_API_HOST=127.0.0.1\n'
    printf 'ENGINEERING_API_PORT=8740\n'
    printf 'ENGINEERING_API_TOKEN_FILE=%s/env/api.token\n' "${CONFIG_ROOT}"
    printf 'ENGINEERING_TEMPORAL_TARGET=127.0.0.1:7233\n'
    printf 'ENGINEERING_TEMPORAL_NAMESPACE=engineering-control\n'
    printf 'ENGINEERING_TEMPORAL_TASK_QUEUE=engineering-control\n'
    printf 'ENGINEERING_DATABASE_URL=postgresql+psycopg://engineering_control:%s@127.0.0.1:55432/engineering_control\n' \
      "${ENGINEERING_DB_PASSWORD}"
    printf 'ENGINEERING_EVIDENCE_ROOT=%s/evidence\n' "${PLATFORM_ROOT}"
    printf 'ENGINEERING_AGENT_PACK_ROOT=%s/source\n' "${AGENT_PACK_INSTALL_ROOT}"
    printf 'ENGINEERING_SYNTHETIC_MODE=false\n'
    printf 'ENGINEERING_ACCEPTANCE_ENABLED=true\n'
    printf 'ENGINEERING_CLEANUP_EXECUTION_ENABLED=false\n'
  } >"${CONFIG_ROOT}/env/control.env"
  chown root:engineering-control "${CONFIG_ROOT}/env/control.env"
  chmod 0640 "${CONFIG_ROOT}/env/control.env"
fi
if ! grep -q '^ENGINEERING_ACCEPTANCE_ENABLED=' "${CONFIG_ROOT}/env/control.env"; then
  printf 'ENGINEERING_ACCEPTANCE_ENABLED=true\n' >>"${CONFIG_ROOT}/env/control.env"
fi
if ! grep -q '^ENGINEERING_AGENT_PACK_ROOT=' "${CONFIG_ROOT}/env/control.env"; then
  printf 'ENGINEERING_AGENT_PACK_ROOT=%s/source\n' \
    "${AGENT_PACK_INSTALL_ROOT}" >>"${CONFIG_ROOT}/env/control.env"
fi
if [[ ! -f "${CONFIG_ROOT}/env/github-worker.env" ]]; then
  {
    printf 'ENGINEERING_ENVIRONMENT=production\n'
    printf 'ENGINEERING_TEMPORAL_TARGET=127.0.0.1:7233\n'
    printf 'ENGINEERING_TEMPORAL_NAMESPACE=engineering-control\n'
    printf 'ENGINEERING_GITHUB_EXECUTABLE=/usr/bin/gh\n'
    printf 'ENGINEERING_GITHUB_AUTHENTICATED_HOME=/home/hermes\n'
    printf 'ENGINEERING_GITHUB_WRITES_ENABLED=true\n'
    printf 'ENGINEERING_GITHUB_ISSUE_WRITE_VALIDATION_FILE=%s/policy/github-issue-write-validation.yaml\n' \
      "${CONFIG_ROOT}"
    printf 'ENGINEERING_AGENT_PACK_ROOT=%s/source\n' "${AGENT_PACK_INSTALL_ROOT}"
    printf 'ENGINEERING_EVIDENCE_INGEST_URL=http://10.10.0.2:8750/v1/evidence\n'
    printf 'ENGINEERING_EVIDENCE_INGEST_TOKEN_FILE=%s/env/evidence-uploaders/github-runner.token\n' \
      "${CONFIG_ROOT}"
    printf 'ENGINEERING_SYNTHETIC_MODE=false\n'
    printf 'ENGINEERING_CLEANUP_EXECUTION_ENABLED=false\n'
  } >"${CONFIG_ROOT}/env/github-worker.env"
fi
if ! grep -q '^ENGINEERING_GITHUB_WRITES_ENABLED=' \
  "${CONFIG_ROOT}/env/github-worker.env"; then
  printf 'ENGINEERING_GITHUB_WRITES_ENABLED=true\n' \
    >>"${CONFIG_ROOT}/env/github-worker.env"
fi
github_write_capability_count=$(grep -c '^ENGINEERING_GITHUB_WRITES_ENABLED=' \
  "${CONFIG_ROOT}/env/github-worker.env")
if [[ ${github_write_capability_count} -ne 1 ]]; then
  echo "Refusing GitHub worker configuration with duplicate write capability" >&2
  exit 1
fi
sed -i -E 's/^ENGINEERING_GITHUB_WRITES_ENABLED=(false|0|no|off)$/ENGINEERING_GITHUB_WRITES_ENABLED=true/' \
  "${CONFIG_ROOT}/env/github-worker.env"
if ! grep -Eq '^ENGINEERING_GITHUB_WRITES_ENABLED=true$' \
  "${CONFIG_ROOT}/env/github-worker.env"; then
  echo "Refusing noncanonical GitHub broker capability configuration" >&2
  exit 1
fi
if ! grep -q '^ENGINEERING_GITHUB_ISSUE_WRITE_VALIDATION_FILE=' \
  "${CONFIG_ROOT}/env/github-worker.env"; then
  printf 'ENGINEERING_GITHUB_ISSUE_WRITE_VALIDATION_FILE=%s/policy/github-issue-write-validation.yaml\n' \
    "${CONFIG_ROOT}" >>"${CONFIG_ROOT}/env/github-worker.env"
fi
github_policy_path_count=$(grep -c '^ENGINEERING_GITHUB_ISSUE_WRITE_VALIDATION_FILE=' \
  "${CONFIG_ROOT}/env/github-worker.env")
github_policy_path=$(sed -n 's/^ENGINEERING_GITHUB_ISSUE_WRITE_VALIDATION_FILE=//p' \
  "${CONFIG_ROOT}/env/github-worker.env")
if [[ ${github_policy_path_count} -ne 1 || \
  ${github_policy_path} != "${CONFIG_ROOT}/policy/github-issue-write-validation.yaml" ]]; then
  echo "Refusing GitHub worker configuration with a noncanonical write-policy path" >&2
  exit 1
fi
if ! grep -q '^ENGINEERING_EVIDENCE_INGEST_URL=' \
  "${CONFIG_ROOT}/env/github-worker.env"; then
  printf 'ENGINEERING_EVIDENCE_INGEST_URL=http://10.10.0.2:8750/v1/evidence\n' \
    >>"${CONFIG_ROOT}/env/github-worker.env"
fi
if ! grep -q '^ENGINEERING_EVIDENCE_INGEST_TOKEN_FILE=' \
  "${CONFIG_ROOT}/env/github-worker.env"; then
  printf 'ENGINEERING_EVIDENCE_INGEST_TOKEN_FILE=%s/env/evidence-uploaders/github-runner.token\n' \
    "${CONFIG_ROOT}" >>"${CONFIG_ROOT}/env/github-worker.env"
fi
github_evidence_url_count=$(grep -c '^ENGINEERING_EVIDENCE_INGEST_URL=' \
  "${CONFIG_ROOT}/env/github-worker.env")
github_evidence_token_count=$(grep -c '^ENGINEERING_EVIDENCE_INGEST_TOKEN_FILE=' \
  "${CONFIG_ROOT}/env/github-worker.env")
if [[ ${github_evidence_url_count} -ne 1 || ${github_evidence_token_count} -ne 1 ]] || \
  ! grep -Eq '^ENGINEERING_EVIDENCE_INGEST_URL=http://10\.10\.0\.2:8750/v1/evidence$' \
    "${CONFIG_ROOT}/env/github-worker.env" || \
  ! grep -Fqx \
    "ENGINEERING_EVIDENCE_INGEST_TOKEN_FILE=${CONFIG_ROOT}/env/evidence-uploaders/github-runner.token" \
    "${CONFIG_ROOT}/env/github-worker.env"; then
  echo "Refusing noncanonical GitHub worker evidence-ingest configuration" >&2
  exit 1
fi
chown root:hermes "${CONFIG_ROOT}/env/github-worker.env"
chmod 0640 "${CONFIG_ROOT}/env/github-worker.env"
for unit in engineering-infrastructure.service engineering-migrate.service \
  engineering-api.service engineering-evidence-ingest.service \
  engineering-temporal-worker.service engineering-scheduler.service \
  engineering-notifier.service engineering-acceptance-worker.service \
  engineering-dashboard.service \
  engineering-github-worker.service engineering-github-policy-reconciler.service \
  engineering-github-policy-reconciler.path engineering-github-policy-expiry.service \
  engineering-github-policy-expiry.timer; do
  install -o root -g root -m 0644 \
    "${RELEASE_ROOT}/source/systemd/${unit}" "${CONFIG_ROOT}/systemd/${unit}"
  install -o root -g root -m 0644 \
    "${CONFIG_ROOT}/systemd/${unit}" "/etc/systemd/system/${unit}"
done

docker compose --env-file "${CONFIG_ROOT}/env/infrastructure.env" \
  -f "${INSTALL_ROOT}/infrastructure/compose.yaml" config --quiet
systemd-analyze verify \
  /etc/systemd/system/engineering-infrastructure.service \
  /etc/systemd/system/engineering-migrate.service \
  /etc/systemd/system/engineering-api.service \
  /etc/systemd/system/engineering-evidence-ingest.service \
  /etc/systemd/system/engineering-temporal-worker.service \
  /etc/systemd/system/engineering-scheduler.service \
  /etc/systemd/system/engineering-notifier.service \
  /etc/systemd/system/engineering-acceptance-worker.service \
  /etc/systemd/system/engineering-dashboard.service \
  /etc/systemd/system/engineering-github-worker.service \
  /etc/systemd/system/engineering-github-policy-reconciler.service \
  /etc/systemd/system/engineering-github-policy-reconciler.path \
  /etc/systemd/system/engineering-github-policy-expiry.service \
  /etc/systemd/system/engineering-github-policy-expiry.timer
"${INSTALL_ROOT}/venv/bin/python" \
  "${RELEASE_ROOT}/source/scripts/verify-worker-registry.py" --host controller \
  --write-policy "${CONFIG_ROOT}/policy/github-issue-write-validation.yaml"
"${RELEASE_ROOT}/source/scripts/verify-no-omniroute.sh" --scope controller
systemctl daemon-reload
systemctl enable engineering-infrastructure.service
systemctl restart engineering-infrastructure.service
systemctl restart engineering-migrate.service
systemctl enable engineering-api.service engineering-evidence-ingest.service \
  engineering-temporal-worker.service engineering-scheduler.service engineering-notifier.service \
  engineering-acceptance-worker.service engineering-github-worker.service \
  engineering-github-policy-reconciler.path engineering-github-policy-expiry.timer
systemctl restart engineering-api.service engineering-evidence-ingest.service \
  engineering-temporal-worker.service engineering-scheduler.service engineering-notifier.service \
  engineering-acceptance-worker.service engineering-github-worker.service
systemctl restart engineering-github-policy-reconciler.path
systemctl restart engineering-github-policy-expiry.timer

"${RELEASE_ROOT}/source/scripts/verify-no-omniroute.sh" --scope controller

echo "Controller bootstrap completed without altering Hermes vendor source, product repositories, or Kubernetes."
