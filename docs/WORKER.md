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

The bootstrap installs Codex 0.149.0, Claude Code 2.1.239, Playwright 1.62.1, Gitleaks 8.30.1,
Trivy 0.74.0, Semgrep 1.174.0, kubeconform 0.8.0, and Syft 1.51.0. Release archives are checked
against upstream checksum manifests.

`/srv/engineering/worktrees/<repo>/<change-set-id>` is allocated by `scan-runner`. ACLs grant the
maker write and the opposite-provider checker read only. Shared group write access is not used.
Each role owns separate run, evidence, cache, temp, and state directories. Systemd filesystem
restrictions supplement normal Unix permissions.

Bootstrap does not provision GitHub or Kubernetes credentials. Provide read-only mirror access to
`scan-runner`, separate provider OAuth to model identities, and namespaced kubeconfigs only to the
identities documented in `SECURITY.md`.
