# Validation status

Validated on 2026-08-24 against the immutable engineering-control release selected by
`/opt/engineering-control/source` on both hosts and engineering-agent-pack release
`a279690ade98d7c968cb646ce9f87d8e083b0e1a`. The verifier checks the installed release identity at
runtime so this document does not embed a self-referential commit identifier.

This report distinguishes a pass from a credential or security blocker. A blocked native-provider
test is not represented as a pass, and no paid API fallback was used.

## Source and generated assets

| Area | Result | Evidence |
| --- | --- | --- |
| Control-plane Python | PASS | 218 pytest tests; Ruff clean; strict mypy clean across 78 source files; Bash syntax clean |
| Agent pack | PASS | 351 generated files changed 0; validator PASS; 2 pytest tests; Ruff clean |
| Schemas/migrations | PASS | Draft 2020-12 validation and migration tests are in the control-plane suite |
| Secret persistence | PASS | Gitleaks worktree scan has zero findings; central redaction tests and live rejection canary pass |
| Git safety | PASS | exact-SHA mirrors/worktrees, leases, dirty protection, bounded cleanup and credential-isolation tests pass |

## Controller activation

| Requirement | Result | Evidence |
| --- | --- | --- |
| Installed immutable release | PASS | `/opt/engineering-control/source` resolves beneath an immutable release directory |
| Agent-pack runtime | PASS | exact committed pack installed under `/opt`; generated assets revalidated; 32 real role/skill/project/provider bindings load with content-addressed provenance |
| PostgreSQL | PASS | pinned container healthy; loopback listener `127.0.0.1:55432`; persistent volume active |
| Temporal | PASS | loopback/private listeners only; server and namespace healthy |
| Engineering API | PASS | authenticated API healthy on `127.0.0.1:8740` |
| Evidence ingest | PASS | private listener `10.10.0.2:8750`; identity-bound upload and impersonation denial pass |
| Workflow/acceptance workers, scheduler and notifier | PASS | all systemd services active; acceptance worker is write-bounded to its synthetic temporary root |
| Retention schedule | PASS/DISABLED | installed Temporal schedule payload is `dry_run=true`; controller and worker mutation settings remain false |
| Durable restart/resume | PASS | workflow worker replaced mid-run; workflow resumed and reached `AWAITING_STAGING_APPROVAL` |
| Native activity heartbeat | PASS | a recovered Codex activity remained on one attempt beyond the two-minute Temporal heartbeat limit while 30-second keepalives continued |
| Schedule registration | PASS | registrar has a 30-second RPC bound, a 45-second systemd start bound, and `on-failure` retry; installed schedules registered successfully |
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
| Native CLIs | PASS | Codex 0.149.0 and Claude Code 2.1.239 are independently authenticated; both structured calls and both cross-provider maker/checker directions pass |
| Browser runtime | PASS | Playwright 1.62.1 launches Chromium and Firefox against an offline bounded journey and produces structured results, screenshots, and traces under `browser-runner` |
| Scan tooling | PASS | Gitleaks, Trivy, Semgrep, Syft and kubeconform installed and identity-readable |
| Release tooling | PASS | kubectl 1.36.3, Helm 4.2.3 and Argo CD CLI 3.5.1 installed under `release-runner` |
| Credential separation | PASS | release cannot read model auth; model/browser/delivery cannot read release credentials; evidence impersonation denied |
| Canonical mirrors | PARTIAL/BLOCKED | Falcone mirrors pass; private Musematic mirrors require a distinct contents-read `scan-runner` credential |
| GitHub delivery/provenance | BLOCKED | draft-PR token and GHCR read credential have not been provisioned |
| Kubernetes identities | BLOCKED | no scoped observer or release kubeconfigs are provisioned; shared cluster-admin remains excluded |

`verify-worker.sh` completes every credential-independent check, then reports these exact external
GitHub blockers:

```text
MANUAL_GITHUB_MIRROR_CREDENTIAL_REQUIRED
MANUAL_GITHUB_DELIVERY_CREDENTIAL_REQUIRED
MANUAL_GITHUB_REGISTRY_AUTH_REQUIRED
```

## Hermes and Telegram

| Requirement | Result | Evidence |
| --- | --- | --- |
| Canonical Hermes | PASS | 0.20.0 vendor install retained; no vendor source patch |
| Engineering Control hook | PASS | command-discovery plugin explicitly enabled; native hook loaded for `command:*`; fixed route map only |
| Authorization | PASS | non-Telegram and unauthorized identities denied; protected numeric allowlist active |
| Hermes -> API `/usage` | PASS | allowlisted installed hook returned live Codex and Claude metadata again after the final release restart |
| Telegram outbound | PASS | Bot API identity check and silent canary delivery succeeded |
| Telegram inbound command | PENDING OPERATOR | bot cannot generate a user-originated command; operator must send `/usage`, `/validate 30`, then `/status` for the returned workflow |
| Service supervision | PASS | user systemd unit enabled, active, linger enabled, zero restarts |

Hermes still has runtime OmniRoute provider aliases. Native provider and cross-provider acceptance
now pass; C-01 remains closed until the genuine inbound Telegram canaries pass and are recorded.

## Lifecycle acceptance

The current synthetic lifecycle passed through authenticated API -> Temporal -> snapshot ->
bounded worktree -> independent synthetic maker/checker -> evidence -> ChangeSet transition. The
workflow worker was stopped and replaced while the run was active; the durable result resumed with
maker `codex-synthetic`, checker `claude-synthetic`, and state `AWAITING_STAGING_APPROVAL`.
A fresh post-release lifecycle started through the installed Hermes hook also completed with
redaction status `SCANNED_CLEAN`, persisted evidence, and status retrieval through that hook.
The standalone lifecycle verifier uses unique internal workflow and activity queues per run, so it
cannot race the persistent production acceptance worker; its latest isolated restart/resume run
also reached `AWAITING_STAGING_APPROVAL` with independent synthetic providers.

The real native provider matrix passes in both directions: Codex maker -> Claude checker and Claude
maker -> Codex checker. Each provider owns its maker repository, checker access is read-only, each
result validates against a strict schema, and exact deterministic file content is verified. The
normalized result preserves only numeric usage counters, redacts credential-shaped token values,
and records the installed immutable instruction-bundle revision and content hash. The resulting
manifest is `/srv/engineering/evidence/acceptance/62344004-b893-4673-9366-b2d8fc1f23f8/native-provider-acceptance.json`
on the worker with SHA256 `716d500d1eca057eb705b3917fb35331d8f68042088f7f7baede4c1a952338e3`.

The following remain blocked and must not be inferred from the synthetic pass:

- inbound Telegram `/usage`, `/validate 30`, and `/status` for the returned workflow;
- Musematic snapshot through the private canonical mirrors;
- scoped Kubernetes observer authorization and release dry-runs;
- real draft-PR and registry-provenance validation.

The installed `validate-native-providers.sh` runner operates only in isolated synthetic directories
and persists a normalized, redacted, SHA256-addressed acceptance manifest.

Real staging mutation is not authorized. Deployment workflows must stop at
`AWAITING_STAGING_APPROVAL`.

## Security blockers

- `SECURITY_BLOCKER_MUSEMATIC_GIT_SECRETS`: rotate, rewrite coordinated history, and rescan every
  clone before Musematic write enablement.
- `SECURITY_BLOCKER_KUBERNETES_CLUSTER_ADMIN_IDENTITY`: replace the shared kubeconfig with scoped
  observer and release identities before any Kubernetes automation is enabled.

C-01 and C-02 remain unexecuted. C-03 is complete with a validated rollback copy under
`/home/hermes/engineering-backups/20260823T095538Z/`.
