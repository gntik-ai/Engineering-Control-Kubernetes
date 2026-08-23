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
python_bin=${ENGINEERING_PYTHON_BIN:-/opt/engineering-control/venv/bin/python}
if [[ ! -x ${python_bin} ]]; then
  echo "FAIL Engineering Control Python environment is unavailable" >&2
  exit 1
fi

"${SCRIPT_DIR}/verify-controller.sh"
worker_rc=0
runuser -u "${operator}" -- ssh -tt worker \
  'sudo /opt/engineering-control/source/scripts/verify-worker.sh' || worker_rc=$?
if [[ ${worker_rc} -ne 0 && ${worker_rc} -ne 2 ]]; then
  echo "FAIL worker verification transport or invariant check" >&2
  exit "${worker_rc}"
fi
runuser -u "${operator}" -- "${python_bin}" \
  "${SCRIPT_DIR}/validate-temporal-lifecycle.py"
if [[ ${worker_rc} -eq 2 ]]; then
  echo "BLOCKED end-to-end verification; independent lifecycle checks passed" >&2
  exit 2
fi
echo "PASS infrastructure boundaries and durable synthetic lifecycle"
