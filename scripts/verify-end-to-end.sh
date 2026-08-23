#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if [[ ${EUID} -ne 0 ]]; then
  echo "Run on the controller with sudo: sudo $0" >&2
  exit 1
fi

# Local Docker/service inspection needs root, but the controller-to-worker SSH
# identity intentionally belongs to the normal operator. Keep root out of the
# cross-host trust path and run non-privileged lifecycle evidence as that same
# operator.
operator=${SUDO_USER:-hermes}
if [[ ${operator} == root ]]; then
  operator=hermes
fi
id "${operator}" >/dev/null

"${SCRIPT_DIR}/verify-controller.sh"
runuser -u "${operator}" -- ssh -tt worker \
  'sudo /opt/engineering-control/source/scripts/verify-worker.sh'
runuser -u "${operator}" -- "${SCRIPT_DIR}/validate-temporal-lifecycle.py"
echo "PASS infrastructure boundaries and durable synthetic lifecycle"
