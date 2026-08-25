# Troubleshooting

`MANUAL_AUTH_REQUIRED_CODEX` or `MANUAL_AUTH_REQUIRED_CLAUDE`: authenticate on worker as the exact
runner identity. Do not copy controller files.

`NotConfigured`: the production activity has no deterministic handler. Do not switch to synthetic
mode in production; implement and test the handler or keep the workflow blocked.

`RATE_LIMITED`: inspect the redacted handoff directory and quota reset metadata. Resume the same
provider later or record an explicit cross-provider handoff.

`BLOCKED_BY_GH_AUTH`: authenticate `gh` interactively as `hermes` on the controller. Do not copy
the auth store, extract a token, create a token file, or install the GitHub Temporal worker on the
worker VPS. Run `scripts/verify-worker-registry.py --host controller` to detect a
unit/kind/queue/activity mismatch before bootstrap reaches systemd.

`GITHUB_PUBLICATION_FAILED`: verify the exact ChangeSet worktree is clean at its recorded branch
and head, the protected askpass helper is executable, the token can push the branch and create a
draft PR, and repository protection has not changed. Never solve this with force-push or bypass.

`ApprovalMismatch`/HTTP 409: retrieve the current approval candidate. Artifact drift or invalidated
candidate requires a fresh review and approval.

`DirtyWorktree`: preserve it, capture status/diff/test evidence, and use abandoned-run cleanup. Do
not run `git clean`, reset, or remove directly.

Temporal unavailable: check PostgreSQL, schema/bootstrap containers, Temporal logs, private
10.10.0.2 listener, namespace, then worker task queues. Do not recreate state volumes.

Argo mismatch: compare approved deployment SHA with observed revision and immutable image digests.
Do not patch the live deployment to hide Git drift.

Helm preflight failure: inspect lint/template, exact context/namespace, backup/parity, digest map,
and current approval. Never fake a safety value.

Hermes command unavailable: verify plugin and hook files, allowlist permissions, API token group,
loopback API health, and restart status. Do not enable arbitrary quick-command execution.
