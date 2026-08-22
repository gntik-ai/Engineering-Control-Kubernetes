#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
"${SCRIPT_DIR}/verify-controller.sh"
ssh worker sudo /opt/engineering-control/source/scripts/verify-worker.sh
echo "Infrastructure boundaries pass; run the synthetic lifecycle command in OPERATOR-CHEATSHEET.md."
