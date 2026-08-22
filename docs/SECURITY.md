# Security

Central redaction runs before logs, evidence, agent events, scanner reports, handoffs, API output,
and Telegram responses are persisted. Never place OAuth, kubeconfig tokens, Secret values, PATs,
bot tokens, SSH keys, passwords, or API keys in prompts or reports.

Host identities are separated by Unix ownership, systemd sandboxing, distinct homes, role-specific
directories, and per-worktree ACLs. Model processes inherit neither paid API keys nor third-party
provider routing variables. Release workers cannot read provider homes; model workers cannot read
release environment files. Telegram offers fixed API commands only.

Kubernetes definitions under `config/kubernetes` create no binding by themselves. Observer access
can get/list/watch selected resources and logs but has no Secret or mutation permission. Preview
and Falcone staging roles must be bound per approved namespace. Musematic normally promotes by Git,
not direct cluster mutation. No production binding is bootstrapped and cluster-admin is forbidden.

Active security blockers:

- `SECURITY_BLOCKER_MUSEMATIC_GIT_SECRETS`: rotate affected credentials, invalidate sessions,
  rewrite history with coordinated force-update, and rescan every clone before write enablement.
- `SECURITY_BLOCKER_KUBERNETES_CLUSTER_ADMIN_IDENTITY`: create distinct observer and release
  kubeconfigs, verify `kubectl auth can-i`, then remove the shared kubeconfig from automation scope.

Do not print findings. Record only type, file/location, history presence, and remediation. Security
evidence and incidents are excluded from automatic cleanup.
