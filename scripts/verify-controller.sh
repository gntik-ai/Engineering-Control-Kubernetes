#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run on the controller with sudo: sudo $0" >&2
  exit 1
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
"${SCRIPT_DIR}/verify-no-omniroute.sh" --scope controller

for path in /home/hermes/engineering/bootstrap /home/hermes/engineering/docs \
  /home/hermes/engineering/project-config /etc/engineering-control/systemd \
  /var/log/engineering-control /opt/engineering-agent-pack/source; do
  test -d "${path}"
done
agent_pack_head=$(git -C /home/hermes/engineering/engineering-agent-pack rev-parse HEAD)
agent_pack_release=$(basename \
  "$(dirname "$(readlink -f /opt/engineering-agent-pack/source)")")
[[ ${agent_pack_release} == "${agent_pack_head}" ]]
runuser -u engineering-control -- test -r \
  /opt/engineering-agent-pack/source/domain-packs/common/POLICIES.md
/opt/engineering-control/venv/bin/python \
  /opt/engineering-agent-pack/source/generators/generate.py --check
/opt/engineering-control/venv/bin/python \
  /opt/engineering-agent-pack/source/generators/validate.py
grep -q '^ENGINEERING_AGENT_PACK_ROOT=/opt/engineering-agent-pack/source$' \
  /etc/engineering-control/env/control.env
runuser -u hermes -- env \
  HOME=/home/hermes \
  PYTHONPATH=/home/hermes/.hermes/hermes-agent \
  /home/hermes/.hermes/hermes-agent/venv/bin/python \
  /opt/engineering-control/source/scripts/verify-hermes-runtime.py

services=(engineering-infrastructure engineering-migrate engineering-api engineering-evidence-ingest engineering-temporal-worker engineering-scheduler engineering-notifier engineering-acceptance-worker engineering-github-worker)
for service in "${services[@]}"; do
  systemctl is-active --quiet "${service}.service" || {
    echo "FAIL service ${service}" >&2
    exit 1
  }
done
/opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/verify-worker-registry.py --host controller \
  --write-policy /etc/engineering-control/policy/github-issue-write-validation.yaml
runuser -u hermes -- env -i \
  HOME=/home/hermes PATH=/usr/local/bin:/usr/bin:/bin \
  /opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/validate-gh-cli-broker.py
runuser -u hermes -- env -i \
  HOME=/home/hermes PATH=/usr/local/bin:/usr/bin:/bin \
  /opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/validate-github-write-denial.py
for service in "${services[@]}"; do
  cmp -s "/etc/engineering-control/systemd/${service}.service" \
    "/etc/systemd/system/${service}.service"
done

docker inspect --format '{{.State.Health.Status}}' engineering-postgresql | grep -qx healthy
docker inspect --format '{{.State.Health.Status}}' engineering-temporal | grep -qx healthy
python3 - <<'PY'
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8740/health/ready", timeout=5) as response:
    value = json.load(response)
if value.get("status") != "ready":
    raise SystemExit("Engineering API not ready")
with urllib.request.urlopen("http://10.10.0.2:8750/health/live", timeout=5) as response:
    value = json.load(response)
if value.get("status") != "ok":
    raise SystemExit("Evidence ingest not ready")
PY
listeners=$(ss -ltn)
grep -q '127\.0\.0\.1:55432' <<<"${listeners}"
grep -q '10\.10\.0\.2:7233' <<<"${listeners}"
grep -q '10\.10\.0\.2:8750' <<<"${listeners}"
if grep -Eq '(0\.0\.0\.0|\*|\[::\]):(55432|7233|8740|8741|8750)' <<<"${listeners}"; then
  echo "FAIL control-plane service exposed on every interface" >&2
  exit 1
fi
# The controller-to-worker key and Host alias intentionally belong to the normal
# runtime operator, not root. The verifier otherwise needs root for Docker and
# service inspection, so cross-host connectivity must explicitly use hermes.
runuser -u hermes -- ssh -o BatchMode=yes -o ConnectTimeout=5 worker true
echo "PASS controller verification"
