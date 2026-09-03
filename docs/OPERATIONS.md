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

Validate disabled GitHub writes through the real Temporal queue without creating an external
effect:

```bash
sudo -u hermes -H /opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/validate-github-write-denial.py
```

Healthy output is `PASS_DENIAL_ENFORCED`, exactly one activity start, zero retries, zero external
commands, and zero mutation attempts. Any queue mismatch, generic activity failure, retry,
subprocess start, or policy bypass fails validation. The operator may enable publication only in a
separately approved change to the canonical policy and service setting. After that change, signal
the durable workflow's fixed `retry` operation with the exact blocked activity name; do not restart
it as an unrelated workflow merely to bypass `AWAITING_WRITE_POLICY`.

Check worker:

```bash
ssh worker 'systemctl status engineering-worker@codex engineering-worker@claude \
  engineering-worker@browser engineering-worker@scan engineering-worker@delivery \
  engineering-release-worker'
ssh worker 'journalctl -u engineering-worker@codex -n 100 --no-pager'
```

`/audit-plan` is a real plan-only Temporal execution. Runtime presets obtain bounded
`identity.superadmin_attest` and `identity.observer_attest` results from the browser and scan
workers before returning readiness. The observer result contains booleans, contract binding,
worker identity, timestamp, TTL, and blocker code only. It contains no kubeconfig path, cluster
subject, authentication material, or Secret data. Observer files remain worker-only and must be
either private to the scan worker (`0600`) or `root`-owned and group-readable only by the effective
scan-worker group (`0640`). Missing, stale, wrong-worker, wrong-target, overprivileged, or insecure
observer credentials fail closed before an audit proceeds.

All logs are structured and redacted. Run context fields are campaign ID, ChangeSet ID, run ID,
project, role, provider, and worker. Metrics are available only on the authenticated loopback API
listener at `/internal/metrics`. They are derived from PostgreSQL and include active campaigns,
queued and active runs, successful/failed runs, provider rate-limit events, per-identity heartbeat
health, deployment duration/failures, verification failures, and indexed evidence bytes. Every
role worker publishes a signed identity-bound heartbeat through the upload-only ingest listener;
a claimed identity that differs from the token owner is rejected.
Dashboard session revocations are exported by active/expired state together with a cleanup-limit
signal. If an observed expired backlog exceeds the bounded 10,000-row cleanup batch, cleanup
finalization writes one idempotent warning to the durable notification outbox. Outbox counts and
the oldest pending notification age are exported so sink failure cannot remain a silent green
state. A delivered notification is diagnostic evidence. While the schedule remains in its default
dry-run mode, it does not delete revocations and the backlog cannot be expected to fall on a later
run. The operator must either complete the documented per-identity enablement and validation gates
or use a separately approved, bounded retention procedure; waiting is not remediation.
`engineering_notification_outbox` has the bounded labels `PENDING`, `DELIVERED`, and `UNKNOWN`;
the last label aggregates unexpected persisted states instead of creating unbounded label values.
Alert on any nonzero `UNKNOWN` value because the notifier's complete writable status domain is
`PENDING` and `DELIVERED`. Delivery failures remain pending with bounded backoff; the persisted
error distinguishes `SINK_HTTP_STATUS_ERROR` from `SINK_TRANSPORT_ERROR` without recording a URL,
token, response body, or exception text.
Interpret `engineering_notification_oldest_pending_age_seconds=0` together with
`engineering_notification_outbox{status="PENDING"}`: zero pending rows means no backlog, while a
newly-created pending row can also have an age close to zero. Unlabelled cleanup and age gauges are
explicitly reset to zero on an empty snapshot, so a prior nonzero value cannot remain stale.

The dashboard's `session-validation` persistence-failure gauge is cleared by either a successful
authenticated session lookup or a successful readiness query. Readiness touches both the
authentication and session-revocation relations, so this recovery signal remains available after
failed requests have removed all affected browser cookies.

Authentication persistence errors fail closed. Logs contain only the fixed operation name,
exception class, and bounded suppression/recovery counters; exception text, SQL parameters,
session bindings, and tracebacks are deliberately omitted. Alert on repeated
`dashboard session validation failed` or `dashboard logout session revocation failed` records and
require the corresponding `recovered` record before closing the incident. The first failure is
emitted immediately, repeated failures are rate-limited for 60 seconds, and the recovery record
reports the suppressed count.
The dashboard process also exposes fixed-boundary persistence metrics at
`http://127.0.0.1:8750/internal/metrics` when the request Host is loopback. The public host is
deliberately answered with 404. Scrape the listener locally and alert when any
`engineering_dashboard_persistence_failure_active` series is one; use the matching
`engineering_dashboard_persistence_failures_total` counter for rate and recurrence, not as a
current-health gauge.
Monitor the unauthenticated `/health/ready` endpoint and alert on any non-200 response. Its
readiness query touches both the singleton authentication row and the durable revocation relation,
so a database outage, missing migration `0014`, or partial revocation schema cannot remain a green
process-only check. The endpoint returns only `ready` or `not-ready`; diagnostic detail remains in
the sanitized, rate-limited journal records.
If `engineering-dashboard.service` fails before binding a listener, check the upgrade log for
`dashboard session revocation database is unsupported`. PostgreSQL is the only supported runtime
dialect; SQLite is fixture-only and any other dialect is a configuration error. Follow the
database support and migration recovery procedure in `UPGRADE.md` rather than changing the
dialect guard.

If logout returns HTTP 503, the server did not durably revoke the cookie and therefore does not
clear it or claim logout success. Retry logout after PostgreSQL recovers. Closing the browser or
deleting the local cookie removes only that copy and does not revoke another copy; use the approved
password reset/version rotation as the break-glass action when immediate global invalidation is
required. Authorization matches the revocation row's token-specific binding digest and
`password_version`, while the authentication row's global password version independently
invalidates all older sessions.

Cleanup alert identity is scoped to the Temporal workflow ID. Temporal schedule actions expand the
configured `cleanup-scheduled` template with the scheduled UTC instant (for example,
`cleanup-scheduled-2026-09-01T03:00:00Z`), so each daily action can emit one durable alert while a
retry/finalize replay of that same action remains idempotent. Treat a schedule action without a
run-unique expanded workflow ID as a configuration blocker. In the alert payload,
`observed_expired_count` is the discovery-time count; `deleted_count` and `remaining_count` are
execute-time values after revalidation, so concurrent expiry can make the deleted count exceed the
earlier observed count without implying over-deletion.

The reserved `/var/log/engineering-control` directory intentionally remains empty during normal
operation because systemd/journald is authoritative; unmanaged service log files are not used.

`engineering-ctl usage` and Telegram `/usage` show each provider's latest native CLI snapshot plus
a seven-day, PostgreSQL-derived history by role. Native CLI presence and authentication status are
persisted when exposed. Unknown capacity, reset time, token counts, and estimated cost remain
`null`; the control plane never converts missing provider data into an estimate. Invocation starts
and terminal results are idempotently bound to the Temporal workflow and normalized provider run
IDs. The durable provider refresh schedule runs every 240 seconds, inside the 300-second provider
freshness window, and bounds each scheduled workflow to 180 seconds. Runs do not overlap, and a
failed run remains visible without permanently pausing later refresh attempts. A missed, slow, or
failed refresh therefore becomes `STALE` instead of retaining a false-green provider card.

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
always read-only and controller-owned. A `dry_run=true` workflow finalizes immediately after
inspection and never invokes `cleanup.authorize`, any `cleanup.execute*` activity, or identity
recovery. Every metadata execution activity also rejects a payload unless `dry_run` is explicitly
`false`. The execution-enabled setting is therefore necessary but not sufficient for mutation; the
scheduled workflow input must also be `dry_run=false`. Discovery covers only exact,
retention-expired records: clean terminal worktrees,
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
