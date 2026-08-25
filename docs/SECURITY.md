# Security

Central redaction runs before logs, evidence, agent events, scanner reports, handoffs, API output,
and Telegram responses are persisted. Never place OAuth, kubeconfig tokens, Secret values, PATs,
bot tokens, SSH keys, passwords, or API keys in prompts or reports.

Worker evidence crosses the private host network through a dedicated service on
`10.10.0.2:8750`. Each worker has a distinct upload-only bearer token; the authenticated identity
must equal the evidence producer and is not an Engineering API identity. Uploads are bound to an
existing Temporal workflow/run, project, campaign or ChangeSet, content hash, producer, and
deterministic artifact ID. Text/JSON that still needs central redaction is rejected before any
filesystem write. Binary artifacts are withheld until their producer marks them redacted.

Host identities are separated by Unix ownership, systemd sandboxing, distinct homes, role-specific
directories, and per-worktree ACLs. Model processes inherit neither paid API keys nor third-party
provider routing variables. Release workers cannot read provider homes; model workers cannot read
release environment files. Telegram offers fixed API commands only.

Canonical mirrors are scan-owned and read-only to model identities. A maker's commit objects,
branch refs and linked-worktree metadata live in a per-ChangeSet shadow bare repository under the
scan cache. ACLs grant write only to that ChangeSet's maker and read only to its checker; canonical
objects are exposed only through a read-only alternate. No model receives write access to shared
repository refs.

Every GitHub operation uses the installed `gh` binary through `GhCliBroker`. The broker verifies
`gh auth status`, the active `gh api user` identity, and exact repository access before each batch.
It exposes typed operations only, rejects model-supplied API paths or CLI fragments, and never
extracts or transports a token. Git writes use the operator-configured `gh auth setup-git` helper;
issue and PR writes remain policy-disabled, ChangeSet-bound, draft-only, and never merge.

Kubernetes definitions under `config/kubernetes` create no binding by themselves. Observer access
can get/list/watch selected resources and logs but has no Secret or mutation permission. Preview
and Falcone staging roles must be bound per approved namespace. Musematic normally promotes by Git,
not direct cluster mutation. No production binding is bootstrapped and cluster-admin is forbidden.

Deployment targets are separate non-secret records under `/etc/engineering-control/targets` and
install disabled. A target name is bound to project and environment, with a project-fixed strategy.
Falcone kubeconfig paths must remain beneath `/etc/engineering-control/env/release`, be readable
only by the release identity, and are checked immediately before preflight and deployment. The
release worker has no model provider homes; model workers have no release target credentials.

The deterministic runtime observer is a separate `scan-runner` identity. Its kubeconfig lives under
`/etc/engineering-control/env/observer`, never the release directory. Before reading workload
metadata, the adapter verifies that Secret reads and representative create/patch/delete/impersonate
operations return `no`. Only reduced names, readiness, revisions, and image digests leave the scan
worker. Raw Kubernetes objects and browser credentials are never placed in provider prompts.

Retention cleanup is disabled for mutation by default and cannot accept arbitrary paths or generic
Kubernetes commands. PostgreSQL eligibility, owner-worker inspection, and a single controller
authorization hash over all exact resources must agree. File cleanup rejects root/interior
symlinks, special files, size/hash drift, and post-inspection manifest changes. Preview cleanup is
restricted to terminal ChangeSets, explicit preview-target opt-in, a scoped mode-`0600` release
kubeconfig, a project namespace prefix, fixed management/deployment/ChangeSet labels, and unchanged
Namespace UID/resourceVersion/label hash. Dirty worktrees, rate-limit handoffs, active work,
open-incident ChangeSets, and standard/security/release/incident evidence remain protected without
an override path. Purged ephemeral evidence is retained as a PostgreSQL tombstone and excluded from
active evidence-byte metrics.

Codex CLI scratch data is created beneath a mode-`0700`, UUID-named per-run parent. The attempt
directory is removed normally by the adapter; a crash residue is cleanup-eligible only after the
associated database Run and provider invocation are terminal, outside the retention window, and
not rate-limited. The Codex identity can remove only that exact controller-authorized UUID path.

Active security blockers:

- `SECURITY_BLOCKER_MUSEMATIC_GIT_SECRETS`: rotate affected credentials, invalidate sessions,
  rewrite history with coordinated force-update, and rescan every clone before write enablement.
- `SECURITY_BLOCKER_KUBERNETES_CLUSTER_ADMIN_IDENTITY`: create distinct observer and release
  kubeconfigs, verify `kubectl auth can-i`, then remove the shared kubeconfig from automation scope.

Do not print findings. Record only type, file/location, history presence, and remediation. Security
evidence and incidents are excluded from automatic cleanup.

There are no GitHub token-file contracts. Authentication expiry pauses the bounded operation as
`BLOCKED_BY_GH_AUTH`. Application policy restricts the operator identity to Falcone, Falcone
Charts, Musematic, Musematic Deploy, and LLMWiki, with writes disabled by default.

All four write-capable activities (`delivery.publish`, `finding.publish`,
`enhancement.publish`, and `reconcile.publish_promotion`) share the same pre-spawn evaluator and
canonical policy path. A denial is a secret-free `BLOCKED_BY_POLICY` business result marked
terminal and non-retryable, with explicit evidence that no command started and no mutation was
attempted. It is evaluated before issue/PR body creation, mutating argument construction, branch
push, `gh` spawn, or HTTP mutation. Missing or unsafe policy fails closed. The disabled default has
an empty repository list; the installation acceptance harness must consume a real write-capable
activity on `engineering-github` and return `PASS_DENIAL_ENFORCED` without an external effect.

Candidate provenance verification additionally requires registry authentication at
`/etc/engineering-control/env/delivery/registry/config.json`, owned by
`root:delivery-runner` and mode `0640`. It must be scoped to read only the configured GHCR images.
The adapter requires the file to remain beneath the delivery credential root, passes only its
parent as `DOCKER_CONFIG`, and uses `gh attestation verify` with exact repository, signer-workflow,
and source-digest constraints. Missing registry auth produces
`MANUAL_GITHUB_REGISTRY_AUTH_REQUIRED`; it never falls back to an unsigned image.
