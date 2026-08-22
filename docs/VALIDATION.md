# Validation status

This file distinguishes source-level validation from activation validation. It does not claim that
services, provider identities, or Kubernetes release credentials are active.

Validated on 2026-08-22 at engineering-control functional commit
`89488e2` and engineering-agent-pack commit
`47a6353dafbf0c1ecea649d434db5dd555e6249e`.

## Passed before activation

| Area | Result | Evidence |
| --- | --- | --- |
| Control-plane Python | PASS | Ruff; strict mypy across 73 source files; 126 pytest tests; wheel build |
| Agent pack | PASS | deterministic generation changed 0 files; validator passed; Ruff; 2 tests |
| Database | PASS | fresh SQLite migrations `0001 -> 0002 -> 0003 -> 0004` reached head |
| Infrastructure definition | PASS | pinned Docker Compose renders with required variables |
| Scripts/browser | PASS | Bash syntax for bootstrap/docker scripts; Node syntax for Playwright runner |
| Schemas | PASS | every JSON Schema is valid Draft 2020-12; disabled target templates validate |
| Secret persistence scan | PASS | redacted Gitleaks worktree scan returned zero findings |
| Worktrees | PASS | per-ChangeSet shadow Git metadata, read-only canonical alternates, allocation/retry, dirty protection, branch-bound cleanup, leases and ACL bounds |
| Maker/checker lifecycle | PASS | bounded real-Git synthetic activity chain reaches `AWAITING_STAGING_APPROVAL` |
| Draft PR publication | PASS | exact clean heads, scoped askpass, no force/merge, idempotent draft lookup, structured missing-credential handoff |
| CI/artifact reconciliation | PASS/FAIL-CLOSED | exact source PR head and named checks, cross-provider review binding, exact-run manifest, per-image provenance, digest-only Falcone promotion and retryable draft PR; both candidate-build contracts and Musematic digest rendering remain intentionally disabled |
| Falcone chart reality check | PASS | discovered staging values pass Helm 4.2.3 lint/template; 181 documents render and all six configured digests appear |
| Approval drift | PASS | source, deployment, preflight, runtime, and recovery drift tests fail closed |
| Preview deployment | PASS | dry-run preflight and enabled-target deterministic mutation/verification path; R0/R1 policy approval and R2/R3 human gate |
| Deployment failure | PASS | terminal Run, lease release, candidate invalidation, recovery baseline, incident |
| Runtime verification | PASS | reduced read-only observation, browser redaction, opposite provider, incident gate |
| Recovery | PASS | new approval, exact Helm baseline hash, migration/GitOps forward-recovery gates |
| Retention cleanup | PASS | dry-run schedule, terminal metadata, dirty protection, double authorization, off switch |
| Observability | PASS | authenticated database-backed metrics and identity-bound heartbeat/impersonation tests for all six workers |

Systemd unit definitions parse, but local `systemd-analyze verify` correctly reports their
`/opt/engineering-control` executables absent before bootstrap installation. That is an activation
dependency rather than a unit syntax waiver.

## Current host observations

- No Engineering Control systemd services or infrastructure containers are active.
- No Engineering API, evidence ingest, Temporal, or PostgreSQL engineering listener is active.
- Controller public ports 80/443 belong to the retained pre-cleanup OmniRoute Caddy deployment.
- Worker listeners are SSH and local DNS only.
- Controller has Codex 0.149.0 and Claude Code 2.1.238; these are not worker service identities.
- Worker has kubectl 1.36.3 and Helm 4.2.3, but Codex, Claude, and Argo CD CLI are absent.
- Worker provider OAuth has not been established. Credentials were not copied from controller.

## Pending activation validation

The following must not be marked PASS until the cleanup checkpoint and ordinary bootstrap are
completed:

- systemd service health and restart behavior;
- PostgreSQL and Temporal container health and durable volume restart;
- authenticated API and identity-bound evidence ingest over the private network;
- full Temporal worker-restart acceptance run;
- worker Codex and Claude native structured smoke tasks;
- both cross-provider maker/checker directions using real subscriptions;
- scoped `delivery-runner` GitHub credential and exact-head draft PR smoke test;
- Playwright browser binaries under `browser-runner`;
- scanner binaries under `scan-runner`;
- release/provider/observer credential filesystem isolation on the installed worker;
- read-only Kubernetes observer authorization against newly scoped credentials;
- Falcone Helm and Musematic GitOps target dry-run against operator-configured targets;
- Hermes/Telegram allowlist and command-to-API integration;
- confirmation that no Engineering Control port is bound to a public wildcard interface.

Real staging mutation remains outside bootstrap validation and requires separate authorization.
