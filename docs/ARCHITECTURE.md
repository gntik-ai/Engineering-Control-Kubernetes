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
       -> engineering-scan / codex / claude / browser / release task queues on worker
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
- `scan-runner` owns canonical mirrors and deterministic scanners.
- `release-runner` receives only scoped release plans and release credentials. It has no model
  home or prompt-driven execution.
- Kubernetes observer access excludes Secrets and mutation. Release identities are namespaced and
  are never made available to model workers.

Every delivery is one ChangeSet spanning source and deployment repositories. Deterministic policy
code validates evidence before state transitions. Maker and checker contexts differ; R3 requires
both providers and typed human approval. Large campaigns use coverage cells and Temporal
continue-as-new rather than one permanent model session.

Current fail-closed controls are intentional: production activity steps without a configured
handler raise `NotConfigured`; missing native authentication is a manual blocker; deployment
mutation needs a deterministic adapter, current artifact binding, and scoped credentials.
