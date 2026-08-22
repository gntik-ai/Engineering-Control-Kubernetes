#!/usr/bin/env bash
set -euo pipefail

for identity in codex-runner claude-runner browser-runner scan-runner release-runner; do
  id "${identity}" >/dev/null
done
for service in engineering-worker@codex engineering-worker@claude engineering-worker@browser \
  engineering-worker@scan engineering-release-worker; do
  systemctl is-active --quiet "${service}.service"
done
sudo -u codex-runner -H codex --version >/dev/null
sudo -u claude-runner -H claude --version >/dev/null
sudo -u browser-runner test -r /opt/engineering-browser/node_modules/@playwright/test/package.json
sudo -u release-runner test ! -r \
  /var/lib/engineering-control/identities/codex-runner/.codex/auth.json
sudo -u release-runner test ! -r \
  /var/lib/engineering-control/identities/claude-runner/.claude/.credentials.json
if sudo -u codex-runner test -r /etc/engineering-control/env/release-worker.env 2>/dev/null; then
  echo "FAIL codex runner can read release environment" >&2
  exit 1
fi
if sudo -u claude-runner test -r /etc/engineering-control/env/release-worker.env 2>/dev/null; then
  echo "FAIL claude runner can read release environment" >&2
  exit 1
fi
echo "PASS worker verification"
