#!/usr/bin/env bash
set -euo pipefail

services=(engineering-infrastructure engineering-migrate engineering-api engineering-temporal-worker engineering-scheduler engineering-notifier)
for service in "${services[@]}"; do
  systemctl is-active --quiet "${service}.service" || {
    echo "FAIL service ${service}" >&2
    exit 1
  }
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
PY
ss -ltn | grep -Eq '127\.0\.0\.1:55432|10\.10\.0\.2:7233'
ssh -o BatchMode=yes -o ConnectTimeout=5 worker true
echo "PASS controller verification"
