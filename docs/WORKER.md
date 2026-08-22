# Worker

The worker executes bounded activities from five task queues using five Unix identities:

| Identity | Purpose | Explicitly forbidden |
| --- | --- | --- |
| `codex-runner` | assigned worktree + native Codex | Claude auth, release credentials, deploy, merge |
| `claude-runner` | assigned worktree + native Claude | Codex auth, release credentials, deploy, merge |
| `browser-runner` | Playwright journeys and evidence | model auth, release credentials, production admin |
| `scan-runner` | mirrors, worktree allocation, scanners | model auth, release credentials |
| `release-runner` | deterministic bounded release activity | model auth, arbitrary prompts, general LLM work |

Install:

```bash
ssh worker
cd /home/hermes/engineering/engineering-control
sudo ./scripts/bootstrap-worker.sh
./scripts/verify-worker.sh
```

The bootstrap installs Codex 0.149.0, Claude Code 2.1.239, Playwright 1.62.1, Gitleaks 8.30.0,
Trivy 0.74.0, Semgrep 1.174.0, kubeconform 0.8.0, and Syft 1.51.0. Release archives are checked
against upstream checksum manifests. It also pins the deterministic adapters to kubectl 1.36.3,
Helm 4.2.3, and Argo CD CLI 3.5.1 with upstream checksums.

`/srv/engineering/worktrees/<repo>/<change-set-id>` is allocated by `scan-runner`. ACLs grant the
maker write and the opposite-provider checker read only. Shared group write access is not used.
Each role owns separate run, evidence, cache, temp, and state directories. Systemd filesystem
restrictions supplement normal Unix permissions.

Bootstrap does not provision GitHub or Kubernetes credentials. Provide read-only mirror access to
`scan-runner`, separate provider OAuth to model identities, and namespaced kubeconfigs only to the
identities documented in `SECURITY.md`.

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
for identity in codex-runner claude-runner browser-runner scan-runner release-runner; do
  sudo install -o hermes -g hermes -m 0600 \
    "/etc/engineering-control/env/evidence-uploaders/${identity}.token" \
    "${token_transfer_root}/${identity}.token"
done
scp -r "${token_transfer_root}" worker:/home/hermes/evidence-token-transfer
find "${token_transfer_root}" -type f -exec shred --remove {} +
rmdir "${token_transfer_root}"
ssh -t worker 'for identity in codex-runner claude-runner browser-runner scan-runner release-runner; do
  sudo install -o root -g "${identity}" -m 0640 \
    "/home/hermes/evidence-token-transfer/${identity}.token" \
    "/etc/engineering-control/env/evidence-${identity}.token" || exit 1
done
find /home/hermes/evidence-token-transfer -type f -exec shred --remove {} + &&
rmdir /home/hermes/evidence-token-transfer'
```
