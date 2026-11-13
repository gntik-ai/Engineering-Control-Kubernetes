#!/usr/bin/env bash
# Run the sandbox image the way a run will and check what it holds.
#
#   - an unprivileged user, a read-only root filesystem, no capabilities, no network;
#   - every pinned tool present at the version its lock file names;
#   - no credentials, no way to raise privileges, nothing that reaches the cluster or
#     GitHub;
#   - the secret scanner catches a planted token, and git, the compiler, Node.js and
#     Python work in the workspace.
#
# Usage: smoke-test.sh <image>   (needs docker and jq)
set -euo pipefail

image="${1:?usage: smoke-test.sh <image>}"
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
uid=10002

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# A run's pod: the image is read-only and the workspace, the home directory and /tmp are
# empty volumes owned by the run's user.
sandbox() {
  docker run --rm --read-only --cap-drop ALL \
    --security-opt no-new-privileges --network none --user "${uid}:${uid}" \
    --tmpfs "/tmp:exec,uid=${uid},gid=${uid}" \
    --tmpfs "/home/agent:exec,uid=${uid},gid=${uid}" \
    --tmpfs "/workspace:exec,uid=${uid},gid=${uid}" \
    "${image}" "$@"
}

# --- versions -------------------------------------------------------------------------
expected="$(
  {
    jq --raw-output '.dependencies | to_entries[] | "\(.key) \(.value)"' \
      "${here}/node-tools/package.json" \
      | sed -e 's|^@anthropic-ai/claude-code |claude |' -e 's|^@openai/codex |codex |' \
        -e 's|^playwright-core |playwright |'
    awk '$1 !~ /^#/ && NF { print $1, $2 }' "${here}/tools.lock"
    sed -n 's/^semgrep==\([0-9.]*\).*/semgrep \1/p' "${here}/semgrep.lock"
  } | sort
)"
[[ "$(wc -l <<<"${expected}")" -eq 10 ]] || fail "expected ten pinned tools, got: ${expected}"
reported="$(sandbox sandbox-versions)"
echo "${reported}"
actual="$(grep --invert-match --extended-regexp '^(node|git|python|chromium) ' <<<"${reported}" | sort)"
[[ "${actual}" == "${expected}" ]] \
  || fail "versions differ from the lock files"$'\n'"expected:"$'\n'"${expected}"$'\n'"actual:"$'\n'"${actual}"
grep --quiet --extended-regexp '^node 22\.' <<<"${reported}" || fail "Node.js is not version 22"

# The default command is the version report, and the default user is the unprivileged one.
[[ "$(docker run --rm --network none "${image}")" == "${reported}" ]] \
  || fail "the default command is not the version report"
[[ "$(docker run --rm --network none "${image}" id -u)" == "${uid}" ]] \
  || fail "the image does not default to user ${uid}"

# --- what must not be there -----------------------------------------------------------
sandbox bash -euc '
  for name in kubectl helm argocd gh ssh scp sudo docker; do
    if command -v "${name}" >/dev/null; then echo "found ${name}"; exit 1; fi
  done
' || fail "the image holds a tool that reaches the cluster, GitHub or another host"
# Without the volumes of a run, so that what is checked is the image itself.
docker run --rm --network none "${image}" bash -euc '
  for path in "${HOME}/.claude" "${HOME}/.claude.json" "${HOME}/.codex" "${HOME}/.config" \
    "${HOME}/.netrc" "${HOME}/.git-credentials" "${HOME}/.npmrc" "${HOME}/.ssh"; do
    if [[ -e "${path}" ]]; then echo "found ${path}"; exit 1; fi
  done
  if env | grep --extended-regexp "(KEY|TOKEN|SECRET|PASSWORD)="; then exit 1; fi
  if [[ -n "$(find / -xdev -type f -perm /6000 2>/dev/null)" ]]; then
    echo "files with a set-user or set-group bit:"; find / -xdev -type f -perm /6000; exit 1
  fi
  for path in / /usr/local/bin /opt/sandbox /etc; do
    if [[ -w "${path}" ]]; then echo "${path} is writable"; exit 1; fi
  done
' || fail "the image holds credentials, a privileged file or a writable system path"

# Neither client is signed in: an account is mounted for one run, never built in.
if sandbox codex login status >/dev/null 2>&1; then fail "Codex reports a signed-in account"; fi
if sandbox claude auth status >/dev/null 2>&1; then
  fail "Claude Code reports a signed-in account"
fi

# --- the tools work, offline, as the run's user ----------------------------------------
# A scanner that passes over a planted token is worse than no scanner.
set +e
sandbox bash -euc '
  printf "token = \"ghp_%s\"\n" "$(head -c 18 /dev/urandom | od -An -tx1 | tr -d " \n")" \
    >/workspace/canary.txt
  gitleaks dir --no-banner --redact=100 --exit-code 23 /workspace >/dev/null 2>&1
'
canary=$?
set -e
[[ ${canary} -eq 23 ]] || fail "gitleaks did not catch the planted token (exit ${canary})"

sandbox bash -euc '
  cd /workspace
  git init --quiet --initial-branch main .
  printf "int main(void) { return 0; }\n" >main.c
  cc -o main main.c && ./main
  node --eval "if (1 + 1 !== 2) process.exit(1)"
  python3 -c "import json, sqlite3, ssl, venv"
  git add main.c
  git -c user.name=smoke -c user.email=smoke@example.invalid commit --quiet --message smoke
  [[ "$(git rev-list --count HEAD)" == 1 ]]
  # A second commit, described the way the platform publishes a change.
  base="$(git rev-parse HEAD)"
  printf "int main(void) { return 1; }\n" >main.c
  printf "new\n" >added.txt
  git add main.c added.txt
  git -c user.name=smoke -c user.email=smoke@example.invalid commit --quiet --message change
  sandbox-bundle /workspace "${base}" /tmp/bundle.json >/dev/null
  [[ "$(jq --compact-output "[.files[] | [.status, .path, .mode]]" /tmp/bundle.json)" \
    == "[[\"A\",\"added.txt\",\"100644\"],[\"M\",\"main.c\",\"100644\"]]" ]]
  [[ "$(jq --raw-output ".files[0].content" /tmp/bundle.json | base64 --decode)" == new ]]
  printf "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: smoke\n" >object.yaml
  rg --quiet ConfigMap object.yaml
  jq --null-input --exit-status "true" >/dev/null
' || fail "git, the compiler, Node.js or Python do not work in the workspace"

# The browser starts as the run's user, on a read-only image, without Chromium's own
# sandbox (the pod is the sandbox), and reads a page by its roles.
sandbox bash -euc '
  cd /tmp
  cat >browser.js <<"SCRIPT"
const { launch } = require("sandbox-browser");
(async () => {
  const browser = await launch();
  const page = await browser.newPage();
  await page.setContent("<title>smoke</title><main><button>Press</button></main>");
  const name = await page.getByRole("button").innerText();
  const title = await page.title();
  await browser.close();
  if (name !== "Press" || title !== "smoke") process.exit(1);
})().catch((error) => { console.error(error); process.exit(1); });
SCRIPT
  node browser.js
' || fail "the browser does not start as the run user"

# A step: the runner carries out a job and ends with a result the platform can read.
job='{"commands":[{"name":"one","argv":["bash","-c","echo step-ran"]},{"name":"two","argv":["false"]},{"name":"never","argv":["true"]}]}'
step_log="$(docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
  --network none --user "${uid}:${uid}" --tmpfs "/tmp:exec,uid=${uid},gid=${uid}" \
  --env "SANDBOX_JOB=${job}" "${image}" sandbox-step)"
grep --quiet '^step-ran$' <<<"${step_log}" || fail "the step runner did not run its command"
result="$(sed -n 's/^::sandbox-result:://p' <<<"${step_log}" | tail -n 1 | base64 --decode)"
[[ "$(jq --compact-output '[.commands[] | [.name, .exit_code]]' <<<"${result}")" \
  == '[["one",0],["two",1]]' ]] || fail "the step runner's result is not what ran: ${result}"

echo "sandbox image ok"
