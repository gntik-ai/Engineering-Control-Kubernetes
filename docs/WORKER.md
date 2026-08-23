# Worker

The worker executes bounded activities from six task queues using six Unix identities:

| Identity | Purpose | Explicitly forbidden |
| --- | --- | --- |
| `codex-runner` | assigned worktree + native Codex | Claude auth, release credentials, deploy, merge |
| `claude-runner` | assigned worktree + native Claude | Codex auth, release credentials, deploy, merge |
| `browser-runner` | Playwright journeys and evidence | model auth, release credentials, production admin |
| `scan-runner` | mirrors, worktree allocation, scanners | model auth, release credentials |
| `delivery-runner` | exact-head Git push and draft PR publication | model auth, release credentials, merge, force-push |
| `release-runner` | deterministic bounded release activity | model auth, arbitrary prompts, general LLM work |

Install:

```bash
ssh worker
cd /home/hermes/engineering/engineering-control
sudo ./scripts/bootstrap-worker.sh
sudo ./scripts/bootstrap-mirrors.sh
sudo ./scripts/verify-worker.sh
```

The mirror bootstrap verifies exact origins and bare-repository structure, stages new clones
outside the canonical path, and never replaces an existing mirror. Public repositories can use
anonymous access. Private repositories use a distinct read-only token at
`/etc/engineering-control/env/scan/github-read.token`, owned by `root:scan-runner` and mode `0640`.
An absent file beneath that protected root means anonymous-only operation; an out-of-root path,
symlink escape, non-regular file, or unsafe mode fails closed.
Git receives only the token file path through the fixed askpass adapter; the value is not placed in
service configuration, command arguments, or logs. Never reuse the delivery credential.

Provision private-mirror access interactively, without printing the token:

```bash
ssh -t worker 'sudoedit /etc/engineering-control/env/scan/github-read.token && \
  sudo chown root:scan-runner /etc/engineering-control/env/scan/github-read.token && \
  sudo chmod 0640 /etc/engineering-control/env/scan/github-read.token && \
  sudo /opt/engineering-control/source/scripts/bootstrap-mirrors.sh'
```

Use a fine-grained credential limited to repository contents read access for the four configured
repositories. It must not grant pull-request write, Actions, administration, package write,
deployment, or organization-management access.

The bootstrap installs Codex 0.149.0, Claude Code 2.1.239, Playwright 1.62.1, GitHub CLI 2.98.0,
Gitleaks 8.30.0,
Trivy 0.74.0, Semgrep 1.174.0, kubeconform 0.8.0, and Syft 1.51.0. Release archives are checked
against upstream checksum manifests. It also pins the deterministic adapters to kubectl 1.36.3,
Helm 4.2.3, and Argo CD CLI 3.5.1 with upstream checksums.

`/srv/engineering/worktrees/<repo>/<change-set-id>` is allocated by `scan-runner`. ACLs grant the
maker write, the opposite-provider checker read, and `delivery-runner` read access to the exact
source/deployment pair only. `release-runner` receives read access only to an exact deployment
worktree, its shadow Git metadata and canonical object alternate after deterministic ChangeSet
validation. Shared group write access is not used.

Writable Git metadata is never shared through the canonical mirror. Each worktree is linked to an
isolated bare shadow under `/srv/engineering/cache/scan/worktree-git/<repo>/<change-set-id>.git`.
The shadow uses the scan-owned canonical mirror only as a read-only object alternate. The maker can
write one shadow repository; the checker, delivery and release identities receive bounded read
ACLs only. This prevents one ChangeSet from rewriting another ChangeSet's refs or canonical mirror.
Each role owns separate run, evidence, cache, temp, and state directories. Systemd filesystem
restrictions supplement normal Unix permissions.

Each bounded non-release worker may write only its own identity home so native subscription CLIs
can refresh their local session and initialize structured clients. The template expands
`/var/lib/engineering-control/identities/%i-runner`; the deterministic release unit intentionally
has no writable identity-home path and cannot read Codex or Claude homes.

Bootstrap does not provision GitHub or Kubernetes credentials. Provide the read-only mirror token
only to `scan-runner`, separate provider OAuth to model identities, a scoped GitHub token only to
`delivery-runner`, and namespaced kubeconfigs only to the identities documented in `SECURITY.md`.
The delivery token is root-owned, group-readable by `delivery-runner`, and mode `0640`; the service
can use it but cannot replace it.

Runtime observation uses only `/etc/engineering-control/env/observer/*.kubeconfig`, owned by
`scan-runner` and mode `0600`. Release kubeconfigs remain in the disjoint `release-runner` directory.
The observation adapter runs explicit `kubectl auth can-i` negative checks before every runtime
snapshot and returns reduced readiness/revision/digest metadata, never raw Kubernetes objects.

The controller generates one upload-only evidence token per worker identity. Provision each token
on the worker as `/etc/engineering-control/env/evidence-<identity>.token`, owned by
`root:<identity>` and mode `0640`. An identity cannot read another identity's token, and none can
access campaigns, approvals, or deployments. If any token is missing, bootstrap records
`MANUAL_EVIDENCE_INGEST_TOKEN_REQUIRED` and production evidence publication fails closed.

Use a protected temporary transfer only after both bootstraps have created the groups. The commands
do not print the token:

```bash
token_transfer_root=$(mktemp -d /home/hermes/evidence-token-transfer.XXXXXX)
for identity in codex-runner claude-runner browser-runner scan-runner delivery-runner release-runner; do
  sudo install -o hermes -g hermes -m 0600 \
    "/etc/engineering-control/env/evidence-uploaders/${identity}.token" \
    "${token_transfer_root}/${identity}.token"
done
scp -r "${token_transfer_root}" worker:/home/hermes/evidence-token-transfer
find "${token_transfer_root}" -type f -exec shred --remove {} +
rmdir "${token_transfer_root}"
ssh -t worker 'for identity in codex-runner claude-runner browser-runner scan-runner delivery-runner release-runner; do
  sudo install -o root -g "${identity}" -m 0640 \
    "/home/hermes/evidence-token-transfer/${identity}.token" \
    "/etc/engineering-control/env/evidence-${identity}.token" || exit 1
done
find /home/hermes/evidence-token-transfer -type f -exec shred --remove {} + &&
rmdir /home/hermes/evidence-token-transfer'
```
