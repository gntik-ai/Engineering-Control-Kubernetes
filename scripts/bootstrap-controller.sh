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
CONFIG_ROOT=/etc/engineering-control
STATE_ROOT=/var/lib/engineering-control
PLATFORM_ROOT=/srv/engineering

for command_name in python3 docker openssl systemctl install; do
  command -v "${command_name}" >/dev/null || {
    echo "Required command missing: ${command_name}" >&2
    exit 1
  }
done

getent group engineering-control >/dev/null || groupadd --system engineering-control
getent group engineering-api-clients >/dev/null || groupadd --system engineering-api-clients
id engineering-control >/dev/null 2>&1 || useradd \
  --system --gid engineering-control --home-dir "${STATE_ROOT}" --shell /usr/sbin/nologin \
  engineering-control
usermod -a -G engineering-api-clients engineering-control
usermod -a -G engineering-api-clients hermes

install -d -o root -g root -m 0755 "${INSTALL_ROOT}/releases"
install -d -o root -g engineering-control -m 0750 "${CONFIG_ROOT}" \
  "${CONFIG_ROOT}/projects.d" "${CONFIG_ROOT}/policies" "${CONFIG_ROOT}/targets" \
  "${CONFIG_ROOT}/runtime"
install -d -o root -g engineering-control -m 0750 "${CONFIG_ROOT}/env"
install -d -o engineering-control -g engineering-control -m 0750 "${STATE_ROOT}" \
  "${STATE_ROOT}/leases"
install -d -o 70 -g 70 -m 0700 "${STATE_ROOT}/postgresql"
install -d -o engineering-control -g engineering-control -m 0750 \
  "${PLATFORM_ROOT}/mirrors" "${PLATFORM_ROOT}/worktrees" "${PLATFORM_ROOT}/runs" \
  "${PLATFORM_ROOT}/evidence" "${PLATFORM_ROOT}/cache" "${PLATFORM_ROOT}/tmp"

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
ln -sfn "${INSTALL_ROOT}/source/docker" "${INSTALL_ROOT}/infrastructure"

python3 -m venv "${INSTALL_ROOT}/venv"
"${INSTALL_ROOT}/venv/bin/pip" install --disable-pip-version-check --upgrade \
  --constraint "${INSTALL_ROOT}/source/requirements.lock" "${INSTALL_ROOT}/source"

install -o root -g engineering-control -m 0640 \
  "${SOURCE_ROOT}/config/control.yaml" "${CONFIG_ROOT}/control.yaml"
install -o root -g engineering-control -m 0640 \
  "${SOURCE_ROOT}/config/projects/falcone.yaml" "${CONFIG_ROOT}/projects.d/falcone.yaml"
install -o root -g engineering-control -m 0640 \
  "${SOURCE_ROOT}/config/projects/musematic.yaml" "${CONFIG_ROOT}/projects.d/musematic.yaml"
for policy in "${SOURCE_ROOT}"/config/policies/*.yaml; do
  install -o root -g engineering-control -m 0640 "${policy}" "${CONFIG_ROOT}/policies/"
done
for target in "${SOURCE_ROOT}"/config/targets/*.yaml; do
  target_name=$(basename "${target}")
  if [[ ! -f "${CONFIG_ROOT}/targets/${target_name}" ]]; then
    install -o root -g engineering-control -m 0640 \
      "${target}" "${CONFIG_ROOT}/targets/${target_name}"
  fi
done
for runtime_target in "${SOURCE_ROOT}"/config/runtime/*.yaml; do
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

install -d -o root -g engineering-control -m 0750 \
  "${CONFIG_ROOT}/env/evidence-uploaders"
for identity in codex-runner claude-runner browser-runner scan-runner release-runner; do
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
HERMES_INTEGRATION="${SOURCE_ROOT}/integrations/hermes-engineering-control"
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
    printf 'ENGINEERING_GITHUB_TOKEN_FILE=%s/env/github.token\n' "${CONFIG_ROOT}"
    printf 'ENGINEERING_SYNTHETIC_MODE=false\n'
    printf 'ENGINEERING_CLEANUP_EXECUTION_ENABLED=false\n'
  } >"${CONFIG_ROOT}/env/control.env"
  chown root:engineering-control "${CONFIG_ROOT}/env/control.env"
  chmod 0640 "${CONFIG_ROOT}/env/control.env"
fi

for unit in engineering-infrastructure.service engineering-migrate.service \
  engineering-api.service engineering-evidence-ingest.service \
  engineering-temporal-worker.service engineering-scheduler.service \
  engineering-notifier.service; do
  install -o root -g root -m 0644 "${SOURCE_ROOT}/systemd/${unit}" "/etc/systemd/system/${unit}"
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
  /etc/systemd/system/engineering-notifier.service
systemctl daemon-reload
systemctl enable --now engineering-infrastructure.service
systemctl restart engineering-migrate.service
systemctl enable --now engineering-api.service engineering-evidence-ingest.service \
  engineering-temporal-worker.service engineering-scheduler.service engineering-notifier.service

echo "Controller bootstrap completed without altering Hermes, product repositories, or Kubernetes."
