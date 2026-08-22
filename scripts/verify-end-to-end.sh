#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
"${SCRIPT_DIR}/verify-controller.sh"
ssh -t worker 'sudo /opt/engineering-control/source/scripts/verify-worker.sh'
"${SCRIPT_DIR}/validate-temporal-lifecycle.py"
echo "PASS infrastructure boundaries and durable synthetic lifecycle"
