# Musematic adapter

Source is `gntik-ai/musematic`; desired deployment state is `gntik-ai/musematic-deploy`; promotion
uses a protected deployment PR followed by Argo CD reconciliation. Direct live patches are not
desired state and the model worker cannot call Argo sync.

The adapter requires a clean deployment worktree, protected non-main branch, immutable image
digests, approved Git SHA, and observed Argo revision. It rejects `latest`, `newest`, direct-main
writeback, and `newest-build` promotion.

Recovery targets the immutable deployment base SHA through a protected `musematic-deploy` revert
PR. Until that PR exists and is approved, the workflow reports `PROTECTED_RECOVERY_PR_REQUIRED`;
it never substitutes a live Kubernetes patch or direct Argo sync.

The current repository was discovered with Image Updater `newest-build` and direct main writeback;
automatic promotion is disabled until replaced by immutable digest PRs. A separate security blocker
also covers committed credential material in Musematic Git state/history. Source and deployment
writes remain disabled until those credentials are rotated and history is remediated as an R3
operation.

Domain coverage includes identity/tenancy, agents, workflows, runtime controller, sandbox,
governance, evaluation, cost/billing, integrations, observability, UI, PostgreSQL, Kafka, Redis,
Neo4j, Qdrant, OpenSearch, ClickHouse, Argo CD/GitOps, and cluster operations.
