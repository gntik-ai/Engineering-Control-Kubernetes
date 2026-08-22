# Engineering Control

`engineering-control` is the external, durable control plane for autonomous engineering work on
Falcone and Musematic. It keeps authoritative workflow state in Temporal and PostgreSQL, invokes
native subscription-authenticated Codex and Claude CLIs through isolated workers, and places all
Git, deployment, approval, and evidence transitions behind deterministic policy code.

The control plane is deliberately independent of both products and their Kubernetes workloads.
Hermes and Telegram are clients of its narrow authenticated API; they are not the workflow engine
and do not receive release credentials.

Current bootstrap status and host-specific blockers are recorded outside this repository under
`/home/hermes/engineering-bootstrap/`. No cleanup, Kubernetes mutation, product edit, remote
repository creation, or credential copy is performed by this repository's bootstrap scripts.

## Safety invariants

- one ChangeSet and one bounded issue per worktree;
- maker and checker are separate invocations, preferably separate providers;
- LLM identities cannot deploy or merge;
- release identities cannot read model credentials;
- approvals bind to immutable source, deployment, image, target, and evidence digests;
- R3 work requires typed human approval and cross-provider verification;
- unknown quota is represented as unknown, never invented;
- native CLI rate limits produce a durable `RATE_LIMITED` handoff, never paid API fallback;
- all persisted output passes through central secret redaction.

See `docs/ARCHITECTURE.md`, `docs/REQUIREMENTS-MATRIX.md`, `docs/VALIDATION.md`, and
`docs/OPERATOR-CHEATSHEET.md`. Exact PR/CI, signed candidate image, and digest-only deployment
reconciliation is specified in `docs/CI-ARTIFACT-RECONCILIATION.md`.
