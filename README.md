# Engineering Control on Kubernetes

This repository is where Engineering Control is rebuilt for Kubernetes. It was seeded from
[`gntik-ai/engineering-control`](https://github.com/gntik-ai/engineering-control) with its
full history; that repository is not changed by this work and keeps running the current
two-host deployment until each project is cut over.

- **Plan and design:** [`docs/design/kubernetes-architecture-study.md`](docs/design/kubernetes-architecture-study.md).
  Start with the summary, then section 13.6 (consolidated plan) and sections 14–15.
- **State of this repository:** the imported platform code, plus the gntik-ui console
  (`web/console`). Host-only files (systemd units, Compose and Caddy files, bootstrap and
  cut-over scripts, operational notes) have been removed from the working tree. Nothing has
  been deployed from here yet.
- **Still describing the old deployment:** everything below this section and most of
  `docs/`. They are kept as reference and will be rewritten as each part is replaced.

---

# Engineering Control

`engineering-control` is the external, durable control plane for autonomous engineering work on
Falcone and Musematic. It keeps authoritative workflow state in Temporal and PostgreSQL, invokes
native subscription-authenticated Codex and Claude CLIs through isolated workers, and places all
Git, deployment, approval, and evidence transitions behind deterministic policy code.

The control plane is deliberately independent of both products and their Kubernetes workloads.
Hermes and Telegram are clients of its narrow authenticated API; they are not the workflow engine
and do not receive release credentials.

Existing paired GitHub issue backlogs use the fixed Telegram/API/CLI contract documented in
[`docs/EXISTING-GITHUB-BACKLOG.md`](docs/EXISTING-GITHUB-BACKLOG.md). Import and planning are
read-only; every write remains bound to an exact expiring Telegram campaign approval.

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
- provider invocation outcomes and exposed usage are durably tracked by role without storing prompts;
- native CLI rate limits produce a durable `RATE_LIMITED` handoff, never paid API fallback;
- all persisted output passes through central secret redaction.
- every controller, worker, bootstrap, upgrade, and end-to-end verification runs the fail-closed
  `scripts/verify-no-omniroute.sh` negative policy before acceptance.

A lecture-style walkthrough of the whole platform (components, software choices, workflows,
state machines, control surfaces, security gates, operations) lives in
[`docs/architecture-course/README.md`](docs/architecture-course/README.md). A higher-level
companion for presenting the platform to mixed audiences (Hermes and Temporal, the GitHub
backlog, orchestration, agents, skills, and tools) is
[`docs/architecture-course/functional-overview/README.md`](docs/architecture-course/functional-overview/README.md).

See `docs/ARCHITECTURE.md`, `docs/REQUIREMENTS-MATRIX.md`, `docs/VALIDATION.md`, and
`docs/OPERATOR-CHEATSHEET.md`. Exact PR/CI, signed candidate image, and digest-only deployment
reconciliation is specified in `docs/CI-ARTIFACT-RECONCILIATION.md`.
