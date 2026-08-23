# Validation status

Validated on 2026-08-23 against the immutable engineering-control release selected by
`/opt/engineering-control/source` on both hosts and engineering-agent-pack release
`47a6353dafbf0c1ecea649d434db5dd555e6249e`. The verifier checks the installed release identity at
runtime so this document does not embed a self-referential commit identifier.

This report distinguishes a pass from a credential or security blocker. A blocked native-provider
test is not represented as a pass, and no paid API fallback was used.

## Source and generated assets

| Area | Result | Evidence |
| --- | --- | --- |
| Control-plane Python | PASS | 191 pytest tests; Ruff clean; strict mypy clean across 76 source files; Bash syntax clean |
| Agent pack | PASS | 351 generated files changed 0; validator PASS; 2 pytest tests; Ruff clean |
| Schemas/migrations | PASS | Draft 2020-12 validation and migration tests are in the control-plane suite |
| Secret persistence | PASS | Gitleaks worktree scan has zero findings; central redaction tests and live rejection canary pass |
| Git safety | PASS | exact-SHA mirrors/worktrees, leases, dirty protection, bounded cleanup and credential-isolation tests pass |

## Controller activation

| Requirement | Result | Evidence |
| --- | --- | --- |
| Installed immutable release | PASS | `/opt/engineering-control/source` resolves beneath an immutable release directory |
| PostgreSQL | PASS | pinned container healthy; loopback listener `127.0.0.1:55432`; persistent volume active |
| Temporal | PASS | loopback/private listeners only; server and namespace healthy |
| Engineering API | PASS | authenticated API healthy on `127.0.0.1:8740` |
| Evidence ingest | PASS | private listener `10.10.0.2:8750`; identity-bound upload and impersonation denial pass |
| Workflow/acceptance workers, scheduler and notifier | PASS | all systemd services active; acceptance worker is write-bounded to its synthetic temporary root |
| Retention schedule | PASS/DISABLED | installed Temporal schedule payload is `dry_run=true`; controller and worker mutation settings remain false |
| Durable restart/resume | PASS | workflow worker replaced mid-run; workflow resumed and reached `AWAITING_STAGING_APPROVAL` |
| Audit snapshot failure | PASS | missing exact-SHA mirror state becomes batched per-cell blocker evidence and a terminal campaign run rather than a failed workflow |
| Quota broker | PASS/PARTIAL DATA | persists native CLI presence/authentication/status information; capacity/reset remain null when providers expose none |
| Provider invocation history | PASS | controller records every provider start/result or bounded blocker plus seven-day role success/token/cost aggregates; prompts and invented values are excluded |
| Paid fallback | PASS | provider environment strips API keys/base URLs; no implicit paid route exists |
| Network boundary | PASS | API/metrics/database bind loopback; Temporal/evidence bind loopback/private only |

The only public HTTP listeners are ports 80/443 on the retained, pre-cutover OmniRoute Caddy
container. They are not Engineering Control listeners and remain under cleanup gate C-02.

## Worker activation

| Requirement | Result | Evidence |
| --- | --- | --- |
| Installed immutable release | PASS | worker and controller release identities are verified during deployment and host checks |
| Six Unix/service identities | PASS | Codex, Claude, browser, scan, delivery and release services active |
| Native CLIs | CODEX PASS / CLAUDE BLOCKED AUTH | Codex 0.149.0 and Claude Code 2.1.239 installed; Codex identity login and a real structured maker call pass; Claude identity login remains absent |
| Browser runtime | PASS | Playwright 1.62.1 plus Chromium/Firefox dependencies installed under `browser-runner` |
| Scan tooling | PASS | Gitleaks, Trivy, Semgrep, Syft and kubeconform installed and identity-readable |
| Release tooling | PASS | kubectl 1.36.3, Helm 4.2.3 and Argo CD CLI 3.5.1 installed under `release-runner` |
| Credential separation | PASS | release cannot read model auth; model/browser/delivery cannot read release credentials; evidence impersonation denied |
| Canonical mirrors | PARTIAL/BLOCKED | Falcone mirrors pass; private Musematic mirrors require a distinct contents-read `scan-runner` credential |
| GitHub delivery/provenance | BLOCKED | draft-PR token and GHCR read credential have not been provisioned |
| Kubernetes identities | BLOCKED | no scoped observer or release kubeconfigs are provisioned; shared cluster-admin remains excluded |

`verify-worker.sh` completes every credential-independent check, then reports these exact blockers:

```text
MANUAL_AUTH_REQUIRED_CLAUDE
MANUAL_GITHUB_MIRROR_CREDENTIAL_REQUIRED
MANUAL_GITHUB_DELIVERY_CREDENTIAL_REQUIRED
MANUAL_GITHUB_REGISTRY_AUTH_REQUIRED
```

## Hermes and Telegram

| Requirement | Result | Evidence |
| --- | --- | --- |
| Canonical Hermes | PASS | 0.20.0 vendor install retained; no vendor source patch |
| Engineering Control hook | PASS | native hook loaded for `command:*`; fixed route map only |
| Authorization | PASS | non-Telegram and unauthorized identities denied; protected numeric allowlist active |
| Hermes -> API `/usage` | PASS | allowlisted installed hook returned live Codex and Claude metadata again after the final release restart |
| Telegram outbound | PASS | Bot API identity check and silent canary delivery succeeded |
| Telegram inbound command | PENDING OPERATOR | bot cannot generate a user-originated command; operator must send `/usage`, `/validate 30`, then `/status` for the returned workflow |
| Service supervision | PASS | user systemd unit enabled, active, linger enabled, zero restarts |

Hermes still has runtime OmniRoute provider aliases. C-01 explicitly requires those references to
be removed only after real native provider and cross-provider acceptance passes.

## Lifecycle acceptance

The current synthetic lifecycle passed through authenticated API -> Temporal -> snapshot ->
bounded worktree -> independent synthetic maker/checker -> evidence -> ChangeSet transition. The
workflow worker was stopped and replaced while the run was active; the durable result resumed with
maker `codex-synthetic`, checker `claude-synthetic`, and state `AWAITING_STAGING_APPROVAL`.
A fresh post-release lifecycle started through the installed Hermes hook also completed with
redaction status `SCANNED_CLEAN`, persisted evidence, and status retrieval through that hook.

The real native Codex structured maker call passes with the subscription-authenticated
`codex-runner`. Its synthetic acceptance directory is a provider-owned Git repository, its output
validates against the strict schema, and the expected bounded file content matches exactly.

The following remain blocked and must not be inferred from the synthetic pass:

- real Claude structured native call;
- Codex maker -> Claude checker;
- Claude maker -> Codex checker;
- inbound Telegram `/usage`, `/status`, and safe synthetic run command;
- Musematic snapshot through the private canonical mirrors;
- scoped Kubernetes observer authorization and release dry-runs;
- real draft-PR and registry-provenance validation.

The installed `validate-native-providers.sh` runner is ready to evaluate the remaining Claude and
cross-provider items immediately after the Claude identity login. It operates only in isolated
synthetic directories and persists a normalized, redacted, SHA256-addressed acceptance manifest.

Real staging mutation is not authorized. Deployment workflows must stop at
`AWAITING_STAGING_APPROVAL`.

## Security blockers

- `SECURITY_BLOCKER_MUSEMATIC_GIT_SECRETS`: rotate, rewrite coordinated history, and rescan every
  clone before Musematic write enablement.
- `SECURITY_BLOCKER_KUBERNETES_CLUSTER_ADMIN_IDENTITY`: replace the shared kubeconfig with scoped
  observer and release identities before any Kubernetes automation is enabled.

C-01 and C-02 remain unexecuted. C-03 is complete with a validated rollback copy under
`/home/hermes/engineering-backups/20260823T095538Z/`.
