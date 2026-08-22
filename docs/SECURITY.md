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

GitHub write publication uses a separate `delivery-runner` identity and token. It receives read
access only to an exact validated ChangeSet repository pair, may push only the bound branch head,
and may create or reuse draft PRs. The adapter forbids force-push and merge operations, validates
the clean branch/head immediately before push, and supplies credentials through a protected
askpass helper rather than command arguments. `delivery-runner` cannot read model or release
credentials. The controller's issue-search token remains a separate read-only credential.

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

Retention cleanup is disabled for mutation by default and cannot accept arbitrary paths.
PostgreSQL eligibility, worker Git cleanliness and branch identity, a second controller
authorization hash, and the scan worker policy switch must all agree. Dirty worktrees and
open-incident ChangeSets remain protected without an override path.

Active security blockers:

- `SECURITY_BLOCKER_MUSEMATIC_GIT_SECRETS`: rotate affected credentials, invalidate sessions,
  rewrite history with coordinated force-update, and rescan every clone before write enablement.
- `SECURITY_BLOCKER_KUBERNETES_CLUSTER_ADMIN_IDENTITY`: create distinct observer and release
  kubeconfigs, verify `kubectl auth can-i`, then remove the shared kubeconfig from automation scope.

Do not print findings. Record only type, file/location, history presence, and remediation. Security
evidence and incidents are excluded from automatic cleanup.

GitHub duplicate search uses a controller-only fine-grained token file at
`/etc/engineering-control/env/github.token`. Provision it as `root:engineering-control` mode
`0640`, with read-only issue metadata permission for the in-scope repositories. Until that file
exists and works, independent finding verification terminates `BLOCKED`; it does not confirm or
file an issue. Never reuse or copy the interactive `hermes` GitHub credential file.

Draft PR publication requires a second fine-grained token at
`/etc/engineering-control/env/delivery/github.token` on the worker, owned by
`root:delivery-runner` and mode `0640`. Scope it only to contents/PR write access for the four
configured repositories, with
no administration, Actions-secret, environment, or merge-bypass permission. Do not reuse the
controller token or an interactive user credential.

Candidate provenance verification additionally requires registry authentication at
`/etc/engineering-control/env/delivery/registry/config.json`, owned by
`root:delivery-runner` and mode `0640`. It must be scoped to read only the configured GHCR images.
The adapter requires the file to remain beneath the delivery credential root, passes only its
parent as `DOCKER_CONFIG`, and uses `gh attestation verify` with exact repository, signer-workflow,
and source-digest constraints. Missing registry auth produces
`MANUAL_GITHUB_REGISTRY_AUTH_REQUIRED`; it never falls back to an unsigned image.
