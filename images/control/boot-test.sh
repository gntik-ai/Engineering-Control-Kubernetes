#!/usr/bin/env bash
# Boot the control-plane image the way a cluster would and check that every process
# starts from configuration alone: environment variables and a mounted Secret volume.
#
#   - a non-root user, a read-only root filesystem, no capabilities;
#   - secrets in the layout the kubelet gives a mounted Secret (links into ..data,
#     root-owned, group-readable through the pod's fsGroup);
#   - a real PostgreSQL and a real Temporal server.
#
# Usage: boot-test.sh <image>   (needs docker, sudo, curl and `temporal` on PATH)
set -euo pipefail

image="${1:?usage: boot-test.sh <image>}"
postgres_image="postgres@sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73"
uid=10001
work="$(mktemp -d)"
services=(api dashboard evidence-ingest notifier worker)
temporal_pid=""

dump() {
  for name in "${services[@]}" migrate password scheduler; do
    if docker inspect "boot-${name}" >/dev/null 2>&1; then
      echo "::group::logs boot-${name}"
      docker logs "boot-${name}" 2>&1 | tail -n 60 || true
      echo "::endgroup::"
    fi
  done
  echo "::group::temporal"; tail -n 30 "${work}/temporal.log" 2>/dev/null || true; echo "::endgroup::"
}
cleanup() {
  status=$?
  if [[ ${status} -ne 0 ]]; then dump; fi
  docker rm --force $(docker ps --all --quiet --filter name=boot-) >/dev/null 2>&1 || true
  if [[ -n "${temporal_pid}" ]]; then kill "${temporal_pid}" 2>/dev/null || true; fi
  sudo rm -rf "${work}"
  exit "${status}"
}
trap cleanup EXIT

wait_for() { # <description> <attempts> <command...>
  local description="$1" attempts="$2"
  shift 2
  for _ in $(seq "${attempts}"); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "timed out waiting for ${description}" >&2
  return 1
}

# --- dependencies ---------------------------------------------------------------------
docker run --detach --name boot-postgres --publish 127.0.0.1:55432:5432 \
  --env POSTGRES_PASSWORD=boot_admin_only "${postgres_image}" >/dev/null
temporal server start-dev --ip 127.0.0.1 --port 7233 --namespace engineering-control \
  --headless --log-level warn >"${work}/temporal.log" 2>&1 &
temporal_pid=$!
# Over TCP, not the socket: while the image initialises a new database it runs a temporary
# server that answers on the socket only and is then shut down. A check on the socket can
# find that one, and the next command then meets a server that is going away.
wait_for "PostgreSQL" 90 docker exec boot-postgres \
  pg_isready --host 127.0.0.1 --port 5432 --username postgres
docker exec boot-postgres psql --username postgres --quiet \
  --command "CREATE ROLE engineering_control LOGIN PASSWORD 'boot_only'" \
  --command "CREATE DATABASE engineering_control OWNER engineering_control ENCODING 'UTF8' TEMPLATE template0"
wait_for "Temporal" 60 temporal operator namespace describe \
  --address 127.0.0.1:7233 --namespace engineering-control

# --- a mounted Secret volume, as the kubelet lays it out ------------------------------
secrets="${work}/secrets"
version="..2026_01_01_00_00_00.1"
mkdir -p "${secrets}/${version}"
token() { head --bytes 48 /dev/urandom | base64 | tr --delete '\n=+/' | head --bytes 56; }
for name in api.token evidence-ingest.token codex-runner.token claude-runner.token \
  browser-runner.token scan-runner.token delivery-runner.token release-runner.token \
  github-runner.token; do
  token >"${secrets}/${version}/${name}"
done
head --bytes 64 /dev/urandom >"${secrets}/${version}/session.key"
ln --symbolic "${version}" "${secrets}/..data"
for path in "${secrets}/${version}"/*; do
  ln --symbolic "..data/$(basename "${path}")" "${secrets}/$(basename "${path}")"
done
sudo chown --recursive "0:${uid}" "${secrets}"
sudo chmod 0440 "${secrets}/${version}"/*
sudo chmod 0755 "${secrets}/${version}"
sudo chmod 3777 "${secrets}"

# Writable volumes (an emptyDir or a claim in the cluster), owned by the pod's user.
mkdir -p "${work}/state" "${work}/evidence" "${work}/out"
chmod 0700 "${work}/out"
sudo chown --recursive "${uid}:${uid}" "${work}/state" "${work}/evidence" "${work}/out"

mount="/var/run/secrets/engineering-control"
common=(
  --network host
  --user "${uid}:${uid}"
  --read-only --tmpfs /tmp
  --cap-drop ALL --security-opt no-new-privileges
  --volume "${secrets}:${mount}:ro"
  --volume "${work}/state:/var/lib/engineering-control"
  --volume "${work}/evidence:/srv/engineering/evidence"
  --env ENGINEERING_ENVIRONMENT=boot-test
  --env ENGINEERING_NETWORK_BOUNDARY=cluster
  --env "ENGINEERING_DATABASE_URL=postgresql+psycopg://engineering_control:boot_only@127.0.0.1:55432/engineering_control"
  --env ENGINEERING_TEMPORAL_TARGET=127.0.0.1:7233
  --env ENGINEERING_API_HOST=0.0.0.0
  --env "ENGINEERING_API_TOKEN_FILE=${mount}/api.token"
  --env ENGINEERING_DASHBOARD_HOST=0.0.0.0
  --env "ENGINEERING_DASHBOARD_SESSION_KEY_FILE=${mount}/session.key"
  --env ENGINEERING_EVIDENCE_INGEST_HOST=0.0.0.0
  --env ENGINEERING_EVIDENCE_INGEST_PORT=8751
  --env "ENGINEERING_EVIDENCE_INGEST_TOKEN_FILE=${mount}/evidence-ingest.token"
  --env "ENGINEERING_EVIDENCE_INGEST_TOKEN_DIRECTORY=${mount}"
)

# --- one-off jobs ---------------------------------------------------------------------
docker run --name boot-migrate "${common[@]}" "${image}" alembic upgrade head
docker run --name boot-password "${common[@]}" --volume "${work}/out:/out" "${image}" \
  engineering-ctl dashboard init-password --output-file /out/initial-password.txt
docker run --name boot-scheduler "${common[@]}" "${image}" engineering-scheduler

# --- long-running processes -----------------------------------------------------------
for name in "${services[@]}"; do
  docker run --detach --name "boot-${name}" "${common[@]}" "${image}" "engineering-${name}" >/dev/null
done

status() { curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 "$@"; }
expect() { # <description> <expected status> <curl arguments...>
  local description="$1" expected="$2" actual
  shift 2
  for _ in $(seq 40); do
    actual="$(status "$@" || true)"
    if [[ "${actual}" == "${expected}" ]]; then
      echo "ok   ${description} (${actual})"
      return 0
    fi
    sleep 1
  done
  echo "FAIL ${description}: expected ${expected}, got ${actual}" >&2
  return 1
}

api_token="$(sudo cat "${secrets}/api.token")"
expect "API is live" 200 http://127.0.0.1:8740/health/live
expect "API is ready (Temporal and database reached)" 200 http://127.0.0.1:8740/health/ready
expect "API refuses a call without the token" 401 http://127.0.0.1:8740/v1/readiness/core
expect "API answers an authenticated call" 200 \
  --header "Authorization: Bearer ${api_token}" http://127.0.0.1:8740/v1/readiness/core
expect "dashboard is ready" 200 http://127.0.0.1:8750/health/ready
expect "sign-in page is served" 200 http://127.0.0.1:8750/login
expect "sign-in token is issued" 200 http://127.0.0.1:8750/login/challenge
expect "console requires a session" 303 http://127.0.0.1:8750/console
expect "evidence ingest is live" 200 http://127.0.0.1:8751/health/live

# Nothing may have exited or logged an unhandled error after a settling period.
sleep 15
for name in "${services[@]}"; do
  if [[ "$(docker inspect --format '{{.State.Running}}' "boot-${name}")" != "true" ]]; then
    echo "FAIL boot-${name} is not running" >&2
    exit 1
  fi
  if docker logs "boot-${name}" 2>&1 | grep --quiet 'Traceback (most recent call last)'; then
    echo "FAIL boot-${name} logged an unhandled error" >&2
    exit 1
  fi
  echo "ok   boot-${name} is running"
done
echo "boot test passed"
