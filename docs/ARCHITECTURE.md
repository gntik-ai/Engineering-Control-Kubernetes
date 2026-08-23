# Architecture

The controller is the authority. PostgreSQL stores searchable engineering metadata and Temporal
stores durable workflow histories. FastAPI exposes a small authenticated loopback API. Hermes is a
Telegram client of that API; it is neither a workflow store nor a release principal.

```text
Telegram user (allowlist)
  -> Hermes plugin + command hook (controller, no shell)
  -> Engineering API 127.0.0.1:8740
  -> Temporal frontend 127.0.0.1/10.10.0.2:7233
  -> control workflow worker
       -> engineering-scan / codex / claude / browser / delivery / release task queues on worker
       -> evidence ingest 10.10.0.2:8750 (identity-bound upload tokens, 32 MiB/artifact)
  -> PostgreSQL 127.0.0.1:55432
  -> evidence metadata + /srv/engineering/evidence
```

PostgreSQL, Temporal server, and Temporal UI run as a pinned infrastructure-only Compose project.
API, scheduler, notifier, workflow worker, and role workers are supervised by systemd. Neither
Falcone nor Musematic hosts a control-plane component.

Trust boundaries:

- `codex-runner` and `claude-runner` use separately authenticated native CLIs and write only an
  assigned worktree through POSIX ACLs.
- `browser-runner` receives journey credentials by protected file handle, never prompt text.
- `scan-runner` owns canonical mirrors, deterministic scanners, a contents-read-only Git token for
  private mirrors, and a separate read-only observer kubeconfig that is programmatically rejected
  if it can read Secrets or mutate workloads.
- `delivery-runner` may push only exact validated ChangeSet heads and create draft PRs for the
  configured repository pair. It cannot merge, deploy, or read either model provider home.
- `release-runner` receives only scoped release plans and release credentials. It has no model
  home or prompt-driven execution.
- Each worker identity receives its own upload-scoped evidence token; producer impersonation is
  rejected. These tokens cannot call the Engineering API, signal workflows, approve, or deploy.
  The ingest listener has no workflow-control routes.
- Kubernetes observer access excludes Secrets and mutation. Release identities are namespaced and
  are never made available to model workers.

Every delivery is one ChangeSet spanning source and deployment repositories. An unchanged
deployment repository is recorded as deferred until a real immutable artifact exists; the control
plane never creates a no-diff PR or invents a digest. `DeliveryReconciliationWorkflow` later binds
the exact source PR head and CI, verifies the configured candidate manifest and every OCI
attestation, and may ask the non-LLM promotion activity for one allowlisted digest-only deployment
commit and draft PR. Deterministic policy code validates evidence before state transitions. Maker
and checker contexts differ; R3 requires both providers and typed human approval. Large campaigns
use coverage cells and Temporal continue-as-new rather than one permanent model session.

Current fail-closed controls are intentional: production activity steps without a configured
handler raise `NotConfigured`; missing native authentication is a manual blocker; deployment
mutation needs a deterministic adapter, current artifact binding, and scoped credentials.
Text and JSON evidence is rejected in memory if central redaction would make any replacement;
unredacted bytes are never written. Binary evidence requires a producer `REDACTED` assertion.

Post-staging verification is another durable transaction: controller metadata snapshot, reduced
read-only Kubernetes observation, isolated Playwright journey, opposite-provider review (both
providers for R3), deterministic artifact comparison, evidence indexing, and either
`RUNTIME_VERIFIED` or `INCIDENT_OPEN`.
