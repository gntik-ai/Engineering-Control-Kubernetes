# Falcone adapter

Source is `gntik-ai/falcone`; deployment state is `gntik-ai/falcone-charts`; staging mutation is a
deterministic Helm transaction. The ChangeSet binds both branches/PRs, base and head SHAs, image
digests, chart version, CI, review, preview, staging, verification, and recovery.

Preflight runs Helm lint/template, validates values stay inside the assigned chart worktree,
requires immutable `sha256:` image mappings, and records rendered manifests. A migration requires
real backup evidence and parity evidence. The adapter refuses fake backup flags or bypasses. Release
uses an exact context, namespace, release name, values set, timeout, current approval binding,
`--atomic`, and `--wait`.

Staging preflight also captures the prior Helm revision and only a SHA256 of its rendered manifest.
A non-migration rollback needs a new artifact-bound approval, restores that exact revision, and
verifies the post-rollback manifest hash. Migration-bearing releases require forward recovery.

Falcone domain coverage includes tenant lifecycle/isolation, Keycloak/IAM, APISIX/gateway,
PostgreSQL, DocumentDB/FerretDB, storage, Kafka, realtime, functions/Knative, Temporal flows, MCP
hosting, plans/billing/quotas, backup/purge, audit/observability, console, Helm, External
Secrets/OpenBao, and cluster security.

The discovered overprivileged shared kubeconfig is prohibited. Falcone release enablement remains
off until observer and staging-releaser identities are provisioned and verified.
