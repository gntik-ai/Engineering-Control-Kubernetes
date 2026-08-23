#!/usr/bin/env bash
set -euo pipefail
umask 027

if [[ ${EUID} -ne 0 ]]; then
  echo "Run on worker with sudo: sudo $0" >&2
  exit 1
fi

for command_name in python3 sudo install setfacl flock; do
  command -v "${command_name}" >/dev/null || {
    echo "Required command missing: ${command_name}" >&2
    exit 1
  }
done

SCRIPT=/opt/engineering-control/source/scripts/native-provider-smoke.py
PYTHON=/opt/engineering-control/venv/bin/python
blocked=false
if ! sudo -u codex-runner -H env -i \
  HOME=/var/lib/engineering-control/identities/codex-runner \
  PATH=/usr/local/bin:/usr/bin:/bin /usr/local/bin/codex login status >/dev/null 2>&1; then
  echo "BLOCKED MANUAL_AUTH_REQUIRED_CODEX" >&2
  blocked=true
fi
if ! sudo -u claude-runner -H env -i \
  HOME=/var/lib/engineering-control/identities/claude-runner \
  PATH=/usr/local/bin:/usr/bin:/bin /usr/local/bin/claude auth status >/dev/null 2>&1; then
  echo "BLOCKED MANUAL_AUTH_REQUIRED_CLAUDE" >&2
  blocked=true
fi
if [[ ${blocked} == true ]]; then
  exit 2
fi

RUN_ID=$(python3 -c 'import uuid; print(uuid.uuid4())')
WORK_ROOT="/srv/engineering/worktrees/acceptance/${RUN_ID}"
TEMP_ROOT="/srv/engineering/tmp/provider-acceptance/${RUN_ID}"
EVIDENCE_ROOT="/srv/engineering/evidence/acceptance/${RUN_ID}"
LOCK=/var/lib/engineering-control/leases/native-provider-acceptance.lock

exec 9>"${LOCK}"
flock -n 9 || {
  echo "Another native provider acceptance run is active" >&2
  exit 1
}

install -d -o root -g root -m 0711 "${WORK_ROOT}" "${TEMP_ROOT}"
install -d -o codex-runner -g codex-runner -m 0700 \
  "${WORK_ROOT}/codex-maker" "${TEMP_ROOT}/codex-maker" "${TEMP_ROOT}/codex-checker"
install -d -o claude-runner -g claude-runner -m 0700 \
  "${WORK_ROOT}/claude-maker" "${TEMP_ROOT}/claude-maker" "${TEMP_ROOT}/claude-checker"

sudo -u codex-runner -H "${PYTHON}" "${SCRIPT}" run \
  --provider codex --mode maker \
  --worktree "${WORK_ROOT}/codex-maker" \
  --expected-line provider-acceptance-codex \
  --result-file "${TEMP_ROOT}/codex-maker/result.json"
"${PYTHON}" - "${WORK_ROOT}/codex-maker/candidate.txt" provider-acceptance-codex <<'PY'
from pathlib import Path
import sys
path=Path(sys.argv[1]); expected=sys.argv[2] + "\n"
if path.read_text(encoding="utf-8") != expected:
    raise SystemExit("Codex maker artifact is not exact")
PY
setfacl -R -m u:claude-runner:r-X "${WORK_ROOT}/codex-maker"
sudo -u claude-runner -H "${PYTHON}" "${SCRIPT}" run \
  --provider claude --mode checker \
  --worktree "${WORK_ROOT}/codex-maker" \
  --expected-line provider-acceptance-codex \
  --result-file "${TEMP_ROOT}/claude-checker/result.json"

sudo -u claude-runner -H "${PYTHON}" "${SCRIPT}" run \
  --provider claude --mode maker \
  --worktree "${WORK_ROOT}/claude-maker" \
  --expected-line provider-acceptance-claude \
  --result-file "${TEMP_ROOT}/claude-maker/result.json"
"${PYTHON}" - "${WORK_ROOT}/claude-maker/candidate.txt" provider-acceptance-claude <<'PY'
from pathlib import Path
import sys
path=Path(sys.argv[1]); expected=sys.argv[2] + "\n"
if path.read_text(encoding="utf-8") != expected:
    raise SystemExit("Claude maker artifact is not exact")
PY
setfacl -R -m u:codex-runner:r-X "${WORK_ROOT}/claude-maker"
sudo -u codex-runner -H "${PYTHON}" "${SCRIPT}" run \
  --provider codex --mode checker \
  --worktree "${WORK_ROOT}/claude-maker" \
  --expected-line provider-acceptance-claude \
  --result-file "${TEMP_ROOT}/codex-checker/result.json"

install -d -o root -g root -m 0750 "${EVIDENCE_ROOT}"
"${PYTHON}" "${SCRIPT}" finalize \
  --run-id "${RUN_ID}" \
  --codex-worktree "${WORK_ROOT}/codex-maker" \
  --claude-worktree "${WORK_ROOT}/claude-maker" \
  --codex-maker "${TEMP_ROOT}/codex-maker/result.json" \
  --claude-checker "${TEMP_ROOT}/claude-checker/result.json" \
  --claude-maker "${TEMP_ROOT}/claude-maker/result.json" \
  --codex-checker "${TEMP_ROOT}/codex-checker/result.json" \
  --evidence-directory "${EVIDENCE_ROOT}"
