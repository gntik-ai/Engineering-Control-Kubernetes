#!/usr/bin/env bash
set -euo pipefail
umask 077

for command_name in ssh scp find sort head cut mktemp sha256sum python3 install \
  readlink basename dirname chmod stat mv rm id; do
  command -v "${command_name}" >/dev/null || {
    echo "Required command missing: ${command_name}" >&2
    exit 1
  }
done

if [[ ${EUID} -eq 0 || $(id -un) != hermes ]]; then
  echo "Run as the normal hermes operator; do not run with sudo" >&2
  exit 1
fi

destination_root=/home/hermes/engineering-bootstrap/provider-acceptance
install -d -m 0700 "${destination_root}"
if [[ $(readlink -f "${destination_root}") != "${destination_root}" ]] || \
  [[ $(stat -c '%U:%G:%a' "${destination_root}") != 'hermes:hermes:700' ]]; then
  echo "Local provider evidence directory is not a protected real directory" >&2
  exit 1
fi
remote_name=$(ssh worker \
  'find /home/hermes/engineering-bootstrap/provider-acceptance -maxdepth 1 -type f -name "*.json" -printf "%T@ %f\n" | sort -nr | head -n 1 | cut -d" " -f2-')
if [[ ! ${remote_name} =~ ^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\.json$ ]]; then
  echo "Worker did not return a bounded provider acceptance filename" >&2
  exit 1
fi

destination="${destination_root}/${remote_name}"
temporary=$(mktemp "${destination_root}/.provider-acceptance.XXXXXX")
trap 'rm -f -- "${temporary}"' EXIT
scp -q "worker:/home/hermes/engineering-bootstrap/provider-acceptance/${remote_name}" \
  "${temporary}"
chmod 0600 "${temporary}"
release_sha=$(basename "$(dirname "$(readlink -f /opt/engineering-control/source)")")
python3 - "${temporary}" "${release_sha}" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
release = sys.argv[2]
value = json.loads(path.read_text(encoding="utf-8"))
if not isinstance(value, dict):
    raise SystemExit("Provider acceptance export is not an object")
if value.get("status") != "PASS" or value.get("release_sha") != release:
    raise SystemExit("Provider acceptance export is not bound to the installed release")
if value.get("redaction_status") != "REDACTED":
    raise SystemExit("Provider acceptance export is not marked redacted")
if not re.fullmatch(
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
    str(value.get("run_id", "")),
):
    raise SystemExit("Provider acceptance export has an invalid run identifier")
PY

if [[ -e ${destination} ]]; then
  if [[ ! -f ${destination} || -L ${destination} ]]; then
    echo "Existing provider evidence path is not a regular file" >&2
    exit 1
  fi
  existing_sha=$(sha256sum "${destination}" | cut -d' ' -f1)
  incoming_sha=$(sha256sum "${temporary}" | cut -d' ' -f1)
  if [[ ${existing_sha} != "${incoming_sha}" ]]; then
    echo "Existing provider evidence filename has different content; refusing overwrite" >&2
    exit 1
  fi
  echo "PASS provider acceptance already fetched path=${destination} sha256=${existing_sha}"
  exit 0
fi
mv -- "${temporary}" "${destination}"
trap - EXIT
digest=$(sha256sum "${destination}" | cut -d' ' -f1)
echo "PASS fetched redacted provider acceptance path=${destination} sha256=${digest}"
