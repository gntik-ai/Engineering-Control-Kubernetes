# Operations

Check controller:

```bash
sudo systemctl status engineering-infrastructure engineering-migrate engineering-api \
  engineering-evidence-ingest engineering-temporal-worker engineering-scheduler \
  engineering-notifier engineering-acceptance-worker engineering-github-worker
sudo journalctl -u engineering-api -n 100 --no-pager
sudo journalctl -u engineering-evidence-ingest -n 100 --no-pager
sudo journalctl -u engineering-github-worker -n 100 --no-pager
sudo docker compose --env-file /etc/engineering-control/env/infrastructure.env \
  -f /opt/engineering-control/infrastructure/compose.yaml ps
```

Check worker:

```bash
ssh worker 'systemctl status engineering-worker@codex engineering-worker@claude \
  engineering-worker@browser engineering-worker@scan engineering-worker@delivery \
  engineering-release-worker'
ssh worker 'journalctl -u engineering-worker@codex -n 100 --no-pager'
```

All logs are structured and redacted. Run context fields are campaign ID, ChangeSet ID, run ID,
project, role, provider, and worker. Metrics are available only on the authenticated loopback API
listener at `/internal/metrics`. They are derived from PostgreSQL and include active campaigns,
queued and active runs, successful/failed runs, provider rate-limit events, per-identity heartbeat
health, deployment duration/failures, verification failures, and indexed evidence bytes. Every
role worker publishes a signed identity-bound heartbeat through the upload-only ingest listener;
a claimed identity that differs from the token owner is rejected.
The reserved `/var/log/engineering-control` directory intentionally remains empty during normal
operation because systemd/journald is authoritative; unmanaged service log files are not used.

`engineering-ctl usage` and Telegram `/usage` show each provider's latest native CLI snapshot plus
a seven-day, PostgreSQL-derived history by role. Native CLI presence and authentication status are
persisted when exposed. Unknown capacity, reset time, token counts, and estimated cost remain
`null`; the control plane never converts missing provider data into an estimate. Invocation starts
and terminal results are idempotently bound to the Temporal workflow and normalized provider run
IDs.

Back up PostgreSQL and `/etc/engineering-control` before upgrades. Do not back up provider OAuth or
SSH private keys into general platform archives. Temporal and PostgreSQL must be healthy before
starting API/workers. The notifier uses an outbox so an unavailable sink does not lose state.
The one-shot schedule registrar bounds its complete Temporal RPC transaction to 30 seconds and its
systemd start to 45 seconds; transient failures restart after 15 seconds instead of holding an
install indefinitely.

An internal audit recovery may use `BOOTSTRAP_VALIDATION_SUPERSEDED` only for a nonterminal
campaign whose authoritative metadata is `dry_run=true`. It writes per-cell recovery evidence,
completes coverage, and records the prior audit Run as `CANCELLED`; the public campaign API cannot
request this path. It exists to reconcile terminal bootstrap-validation executions without deleting
their histories or falsely recording them as successful audits.

The daily cleanup schedule defaults to dry-run mode. It becomes a mutating schedule only when the
operator explicitly sets `ENGINEERING_CLEANUP_EXECUTION_ENABLED=true` on the controller and every
owning worker, restarts them, and lets the scheduler update the Temporal schedule. Discovery is
controller-owned and covers only exact, retention-expired records: clean terminal worktrees,
terminal delivery-run
directories, old browser output directories, artifacts explicitly classified `ephemeral`, stale
component leases owned by terminal Runs, deterministic Codex per-run temporary directories, and
explicitly managed preview namespaces belonging to a terminal ChangeSet. Active Runs/leases,
rate-limit handoffs, dirty worktrees, open incidents, and
`standard`, `security`, `release`, or `incident` evidence are excluded.

Each owning identity inspects its resource without following symlinks. The scan worker proves exact
Git path/branch/cleanliness; controller, delivery, browser, and Codex workers hash exact file
manifests; the release worker reads only an explicitly configured preview namespace and requires
the fixed
`engineering.gntik.ai/managed-by`, deployment-ID, and ChangeSet-ID labels. The controller then
revalidates current PostgreSQL eligibility and binds every worktree, file, component lease, and
namespace observation into one canonical SHA256 authorization. Any identity, path, file manifest,
namespace UID/resourceVersion/labels, target configuration, or eligibility drift fails closed.

Execution has a second switch, `ENGINEERING_CLEANUP_EXECUTION_ENABLED=false`, on every involved
identity. A separately started non-dry-run workflow still uses only typed adapters: `git worktree
remove`, bounded unlink/tree removal, exact namespace deletion with a mode-`0600` preview-releaser
kubeconfig, then idempotent PostgreSQL lease release/evidence/deployment tombstones. Arbitrary paths,
generic shell/kubectl arguments, active work, and protected evidence have no override path. Partial
completion is safe to retry from fresh discovery. This retention workflow is distinct from the
bootstrap software cleanup plan gated by `APPROVE CLEANUP`.
