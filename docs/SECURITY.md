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
