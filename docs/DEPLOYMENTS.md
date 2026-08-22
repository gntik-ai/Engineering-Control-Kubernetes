# Deployments

Deployment mutation is an activity on the `engineering-release` task queue and runs only as
`release-runner`. Models produce plans and reviews; they never receive release credentials.

Falcone uses a protected chart plan followed by deterministic Helm. Required inputs include exact
chart worktree, release/context/namespace, immutable values, rendered manifest, migration flag,
real backup/parity evidence, CI/reviewer hashes, approval binding, and recovery plan. A single
staging deployment lease prevents overlap.

Musematic uses source PR plus deployment PR, immutable digest change, approval, merge, Argo CD
observation, and runtime verification. Rollback is another Git change to an approved revision; an
undocumented Kubernetes patch is never the desired state.

Approval candidates display project, issue/risk, both PRs and SHAs, image digests, target context
and namespace, migration, backup requirement, recovery plan, CI, and independent review. The API
persists the actor and exact candidate digest. Artifact drift invalidates approval.

No real staging mutation is part of bootstrap validation. Dry-run/preflight may render Helm and
GitOps plans. Production credentials and bindings are intentionally absent.
