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

## Operator change requests (NEW-108)

An operator can turn a free-text description of a wanted change into one reviewed, approved
GitHub issue. The flow stops at the published issue. Delivery then uses the existing
`backlog-import`, `backlog-approve` and `backlog-start` path.

```bash
engineering-ctl change-draft --project falcone --text "Add a CSV export of the tenant audit log"
engineering-ctl change-draft --project musematic --file request.md --repository musematic-deploy
engineering-ctl changes --project falcone --status CANDIDATE
engineering-ctl change-show <id>      # rendered title/body, binding, findings, approve command
engineering-ctl change-approve <id> <binding> APPROVE_CHANGE_<PROJECT>_<FIRST16HEX>
engineering-ctl change-revise <id> --note "Only administrators may export"
engineering-ctl change-reject <id> --reason "Superseded by the reporting epic"
```

The description must be 20 characters to 8 KiB of UTF-8. Credentials are redacted before the
description is stored, and only the redacted text is kept (`redacted: true`). The target
repository defaults to the project's source repository and must be one of the project's two
allowlisted repositories. `ChangeRequestWorkflow` (`change-request-<id>-r<revision>`) allocates a
read-only audit snapshot of the current main branch. The Claude spec architect drafts a proposal
(the `create-openspec` skill and the pinned spec model), and the Codex checker reviews it (the
`code-reviewer` role with the `review-change` skill). A `REVISE` verdict gets one automatic
redraft and re-review. If the re-review still says `REVISE`, the request becomes a `CANDIDATE`
anyway, and its unresolved findings appear in `change-show` and in the issue body. Nothing is
ever approved automatically. Similar open issues in the target repository are attached as
warnings only. Draft or review failures end in `FAILED` with a safe error code, and
`change-revise` retries them.

`change-show` prints the exact approve command. The typed phrase binds the project and the
first 16 hex digits of the approval binding. The binding covers the request, its revision,
the proposal fingerprint, the target repository, the issue title and the body hash, so any
revision or edit invalidates it. Approval accepts only the CLI operator and allowlisted
`telegram:<id>` actors. It starts `ChangeRequestPublishWorkflow`
(`change-request-publish-<id>-r<revision>`). Its `change_request.publish` step is a separate,
narrow capability that can only create an issue. It is not one of the backlog write-capable
steps and does not need backlog write-policy ownership. Only the global
`ENGINEERING_GITHUB_WRITES_ENABLED` switch applies.

The GitHub worker has no database credential, so its authorization does not come from the
workflow payload. Every approval change drops an id-only request in
`/var/lib/engineering-control/change-request-approvals`. The root
`engineering-change-request-attestor` (a path unit plus a 15-minute timer) reads the row from
PostgreSQL. For an `APPROVED`, unpublished, re-verified row it writes a 24-hour attestation to
`/etc/engineering-control/policy/change-requests` (`root:engineering-control 0640`); otherwise it
revokes it. The worker re-renders that attestation and checks the binding, revision, workflow type
and workflow id before it publishes the attested title and body. Before the first create, the
publish intent is recorded durably. Every later attempt first pages the repository's issues
newest-first through the REST API back past that intent. It adopts only an issue whose title and
body exactly match the approved ones, whose last line is the marker and whose author is the Hermes
identity. Any other issue that claims the request is a conflict. A lookup that cannot finish
fails as `PUBLISH_OUTCOME_UNKNOWN` without creating anything. Labels (`hermes:change-request` and
the kind) are applied only if they already exist and are never created.

A failed publication fails the workflow run and leaves the request `APPROVED` with an
`error_code`. Repeating the same `change-approve` command starts a fresh run. An `APPROVED` request
that is not yet published can still be revised or rejected, which revokes its attestation.

Backlog import reads a `Change request:` issue's `kind:` line. A `feature`, `bug` or `chore`
request is classified like the same hand-written issue without that metadata, so it is eligible
under the bug filters. An `enhancement` request follows the existing enhancement rules. Its
`Risk: R<n>` line is a floor: the imported risk is the higher of the declared risk and the
keyword-detected risk.
