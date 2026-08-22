#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run on worker with sudo: sudo $0" >&2
  exit 1
fi

for identity in codex-runner claude-runner browser-runner scan-runner delivery-runner release-runner; do
  id "${identity}" >/dev/null
done
for service in engineering-worker@codex engineering-worker@claude engineering-worker@browser \
  engineering-worker@scan engineering-worker@delivery engineering-release-worker; do
  systemctl is-active --quiet "${service}.service"
done
sudo -u codex-runner -H codex --version >/dev/null
sudo -u claude-runner -H claude --version >/dev/null
sudo -u codex-runner -H codex login status >/dev/null 2>&1
sudo -u claude-runner -H claude auth status >/dev/null 2>&1
sudo -u browser-runner test -r /opt/engineering-browser/node_modules/@playwright/test/package.json
sudo -u scan-runner /usr/local/bin/gitleaks version >/dev/null
sudo -u scan-runner /usr/local/bin/trivy --version >/dev/null
sudo -u scan-runner /usr/local/bin/semgrep --version >/dev/null
sudo -u release-runner /usr/local/bin/helm version --short >/dev/null
sudo -u release-runner /usr/local/bin/kubectl version --client >/dev/null
sudo -u release-runner /usr/local/bin/argocd version --client >/dev/null
sudo -u delivery-runner /usr/local/bin/gh --version >/dev/null
sudo -u delivery-runner test -s /etc/engineering-control/env/delivery/github.token
[[ $(stat -c '%a:%U:%G' /etc/engineering-control/env/delivery/github.token) == \
  640:root:delivery-runner ]]
for identity in codex-runner claude-runner browser-runner scan-runner delivery-runner release-runner; do
  own_token="/etc/engineering-control/env/evidence-${identity}.token"
  sudo -u "${identity}" test -r "${own_token}"
  for other_identity in codex-runner claude-runner browser-runner scan-runner delivery-runner release-runner; do
    if [[ ${other_identity} != "${identity}" ]] && \
      sudo -u "${identity}" test -r \
        "/etc/engineering-control/env/evidence-${other_identity}.token" 2>/dev/null; then
      echo "FAIL ${identity} can impersonate ${other_identity} evidence" >&2
      exit 1
    fi
  done
done
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
for identity in codex-runner claude-runner browser-runner scan-runner release-runner; do
  if sudo -u "${identity}" test -r \
    /etc/engineering-control/env/delivery/github.token 2>/dev/null; then
    echo "FAIL ${identity} can read GitHub delivery credentials" >&2
    exit 1
  fi
done
for identity in codex-runner claude-runner browser-runner scan-runner delivery-runner; do
  if sudo -u "${identity}" test -r /etc/engineering-control/env/release 2>/dev/null; then
    echo "FAIL ${identity} can read release credentials" >&2
    exit 1
  fi
done
sudo -u delivery-runner test ! -r \
  /var/lib/engineering-control/identities/codex-runner/.codex/auth.json
sudo -u delivery-runner test ! -r \
  /var/lib/engineering-control/identities/claude-runner/.claude/.credentials.json
for identity in codex-runner claude-runner browser-runner delivery-runner release-runner; do
  if sudo -u "${identity}" test -r /etc/engineering-control/env/observer 2>/dev/null; then
    echo "FAIL ${identity} can read the observer credential directory" >&2
    exit 1
  fi
done
if sudo -u scan-runner test -r /etc/engineering-control/env/release 2>/dev/null; then
  echo "FAIL scan runner can read the release credential directory" >&2
  exit 1
fi
echo "PASS worker verification"
