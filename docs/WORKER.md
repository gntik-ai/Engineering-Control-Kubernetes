# Worker

The worker executes bounded activities through separated identities. The typed GitHub broker is a
controller-only service and is deliberately not installed by the worker bootstrap:

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
outside the canonical path, and never replaces an existing mirror. GitHub authentication comes
only from the installed `gh` credential store of the exact `hermes` broker identity. Engineering
Control never calls `gh auth token`, copies that store, or places a GitHub token in configuration,
environment variables, arguments, logs, evidence, or workflow state.

Worker-side `gh` remains available for secret-safe operator diagnostics and source preparation,
but no worker-VPS GitHub Temporal queue is declared. Configure Git's `gh` credential helper only
as a documented operator action when local clone/fetch preparation needs it:

```bash
ssh -t worker 'gh auth status --hostname github.com && gh auth setup-git && \
  sudo /opt/engineering-control/source/scripts/bootstrap-mirrors.sh'
```

The controller application constrains the broad operator identity to five allowlisted repositories
and typed operations. GitHub writes stay disabled until an approval-bound policy enables them.

The bootstrap installs Codex 0.159.3 (gpt-6.1-sol needs codex-cli 0.159 or later), Claude Code 2.1.239, Playwright 1.62.1, GitHub CLI 2.98.0,
Gitleaks 8.30.0,
Trivy 0.74.0, Semgrep 1.174.0, kubeconform 0.8.0, and Syft 1.51.0. Release archives are checked
against upstream checksum manifests. It also pins the deterministic adapters to kubectl 1.36.3,
Helm 4.2.3, and Argo CD CLI 3.5.1 with upstream checksums.

It also normalizes the operator-owned `/home/hermes/engineering/{bootstrap,docs,project-config}`
directories, installs inspectable unit snapshots under `/etc/engineering-control/systemd`, and
reserves `/var/log/engineering-control`. The active unit copies must match the snapshots; worker
logs remain in journald rather than unmanaged files.

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

Provider activities heartbeat Temporal every 30 seconds independently of native JSONL events.
This keeps a long, quiet test or code-mode command on its original durable activity attempt while
preserving event-driven heartbeats and the provider timeout/cancellation boundary.

Bootstrap does not provision GitHub or Kubernetes credentials. It validates the existing `gh`
broker identity in place, never copies it, keeps provider OAuth isolated, and accepts only scoped
kubeconfigs documented in `SECURITY.md`.

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
