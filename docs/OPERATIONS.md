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
freshness window, and bounds each scheduled workflow to 45 seconds. Runs do not overlap, and a
failed run remains visible without permanently pausing later refresh attempts. A missed, slow, or
failed refresh therefore becomes `STALE` instead of retaining a false-green provider card.

Back up PostgreSQL and `/etc/engineering-control` before upgrades. Do not back up provider OAuth or
SSH private keys into general platform archives. Temporal and PostgreSQL must be healthy before
starting API/workers. The notifier uses an outbox so an unavailable sink does not lose state.
The notifier unit is confined to loopback (the default HMAC sink is the gateway webhook on
127.0.0.1:8644). A remote bearer HTTPS sink is an explicit opt-in: install
`systemd/engineering-notifier.service.d/remote-bearer-sink.conf.example` as a drop-in with the
sink's exact addresses; see the instructions in that file.
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

## Extra correction attempt for validation findings (NEW-133)

A backlog delivery may use `correction_limit + 1` attempts per ChangeSet. When the last of them
was PASSed by the independent checker and stopped only by Hermes's own deterministic validation
(`CHANGESET_VALIDATION_FINDINGS`, e.g. a semgrep finding the change introduced), the loop pauses
with `CORRECTION_LIMIT_EXHAUSTED` and a plain `backlog-retry` fails with `POLICY_VIOLATION`
("ChangeSet exceeds the approved correction limit"). The operator may grant that ChangeSet exactly
one more attempt, once ever:

```bash
engineering-ctl change-extend-correction <loop_workflow_id> <project> <issue_number> \
  <change_set_id> EXTEND_CORRECTION_ONCE_<first 8 hex of the ChangeSet ID, upper case>

# falcone #1053
engineering-ctl change-extend-correction \
  backlog-loop-falcone-16979310-af9c-4f78-8d0e-2a6830e12421-operation-83ba57bb-5bd9-4942-80db-06d54ce35ec9 \
  falcone 1053 98345e77-680f-56ce-bbb1-390a72484290 EXTEND_CORRECTION_ONCE_98345E77
engineering-ctl backlog-retry <loop_workflow_id> <repository> 1053
```

The API route is `POST /v1/change-sets/extend-correction` (operator channel only, like
`r3-approve`). The controller re-reads every precondition from durable records and refuses with
a `CORRECTION_EXTENSION_*` code unless all hold:

- the ChangeSet exists in that project, belongs to that loop and issue, and its originating
  delivery approval and correction lineage are intact (`APPROVAL_INVALID`, `TARGET_MISMATCH`);
- the ChangeSet is `REQUEST_CHANGES` (`STATE`) and its lineage used exactly
  `correction_limit + 1` attempts (`LINEAGE_NOT_EXHAUSTED`: use `backlog-retry` instead);
- the latest attempt's recorded stop (`recovery_json.correction_requested`) is
  `CHANGESET_VALIDATION_FINDINGS` (`NOT_VALIDATION_BLOCKED`), with non-empty
  `recovery_json.validation_findings` recorded during that attempt (`FINDINGS_MISSING`), and
  that attempt's checker invocation (`code-reviewer`) decided PASS (`CHECKER_NOT_PASS`);
- the loop Run is `PAUSED` on this issue, as recorded by the loop itself in its
  `backlog.loop_pause_target` Step at every loop-state change (`LOOP_NOT_PAUSED`; a loop
  paused before this record existed needs one `backlog-retry` to re-pause and record it);
- no extension was ever granted to this ChangeSet (`ALREADY_GRANTED`).

The grant is stored in `ChangeSet.recovery_json.correction_extension` (operator, time, the
SHA-256 of the validation findings, the attempt it extends) and audited as a
`correction_extension_granted` dashboard event. The bounded lineage then admits
`correction_limit + 2` attempts for that ChangeSet only; CI-repair bounds are unchanged.

`backlog-retry` resumes the loop. The retry round's first attempt resumes the existing ChangeSet
worktree and each maker is told its own repository's recorded findings (`deployment: ...`
findings go to the deployment maker). If that attempt is again sent back, the retry round does
not start another child: it pauses with `CORRECTION_EXTENSION_USED`, and only `backlog-skip` or a
fresh delivery remains. ChangeSet worktrees in `REQUEST_CHANGES` are kept by the nightly cleanup
until neither the ChangeSet nor any of its runs changed for `cleanup_retention_days` (14 days by
default); preview namespaces are cleaned only for terminal ChangeSets.

## Retry after a provider outage (NEW-137)

When a delivery attempt's spec, maker or checker call fails for a transient provider reason,
the attempt's lease release records `REQUEST_CHANGES` on its `PLANNED`/`IMPLEMENTING` ChangeSet
(`recovery_json.correction_requested.blocker = PROVIDER_UNAVAILABLE`, with `provider`,
`error_code`, `provider_error_kind`, `provider_step` and `role`), once per attempt and only for
the ChangeSet's latest attempt. The loop pauses with `DELIVERY_FAILED:PROVIDER_UNAVAILABLE` (a `RATE_LIMITED` child
status pauses as `RATE_LIMITED`), and once the provider is back a plain
`engineering-ctl backlog-retry <loop_workflow_id> <repository> <issue>` binds the next attempt
without any database fix. Each retry is one child of the bounded correction lineage; no extra
allowance is granted.

Converted (explicit allowlist): a provider result `RATE_LIMITED`/`PROVIDER_RATE_LIMIT`,
`FAILED`/`PROVIDER_MODEL_AT_CAPACITY` or `FAILED`/`PROVIDER_EXIT_NONZERO` only when the adapter's
`provider_error_kind` is `OVERLOADED`, `RATE_LIMITED`, `SERVER_ERROR` or `CONNECTION_ERROR`; a
typed `RATE_LIMITED` activity error; and `PROVIDER_ACTIVITY_FAILED` only after a heartbeat or
schedule-to-start timeout (the provider worker stopped answering). The kind is derived from
provider-owned diagnostics only (Claude's terminal result subtype, `api_error_status`, `errors`,
`api_retry` events and its own `API Error: ...` envelope; Codex `error`/`turn.failed` events;
captured stderr), never from the summary or any agent output. The terminal result wins: an
`error_max_turns`, structured-output-retries or other excluded terminal subtype, or any
authentication, permission, policy, sandbox, schema or budget cause in those diagnostics, is never
converted, whatever transient errors came before. No provider-owned signal means no conversion.
Policy, schema, auth, missing CLI, `PROVIDER_TIMEOUT` and any other activity error still need an
operator decision.

NEW-168: the provider worker's own pre-invocation stop `BLOCKED`/`AUTH_STATUS_TIMED_OUT_CODEX`
(or `_CLAUDE`), written when its bounded `<cli> login status` probe exceeded its deadline (a
CPU-starved worker), is converted too (`provider_error_kind` `AUTH_STATUS_TIMED_OUT`), bound to
the request's provider and invocation. `MANUAL_AUTH_REQUIRED_*`, `PROVIDER_CLI_MISSING_*` and the
other `AUTH_STATUS_*` probe failures are not. Such a pre-invocation `BLOCKED` result no longer
carries the previous provider step's `agent_result`, so `agent.persist_result` records the
invocation as `BLOCKED` instead of failing the child with a `PolicyViolation` (falcone #975). The
loop workflow is unchanged: the child ends `BLOCKED` and an issue-start loop pauses with
`BLOCKED`; `backlog-retry` then binds.

## Voided issue approval of an issue-start loop (NEW-143)

> **Warning:** do not use `backlog-pause` on a loop started with `issue-start` (for example
> during a GitHub outage). Pausing the campaign voids its per-issue approval; the general
> `backlog-approval-candidate` / `backlog-approve` then re-approves the campaign **without** the
> issue reference, and the loop's next delivery child is refused ("direct child campaign approval
> differs from its operation", pause blocker `LOOP_STEP_FAILED_BACKLOG_REGISTER_CHILD_RUN`). Let
> the loop stop on its own blocked pause instead and resume it with `backlog-retry`. A new
> `issue-start` cannot replace the loop: its ChangeSet belongs to the existing operation.

Recovery re-creates the per-issue approval for the loop's **existing** issue operation:

```bash
engineering-ctl issue-reapprove <loop_workflow_id> <project> <repository> <issue_number> \
  REAPPROVE_ISSUE_<first 8 hex of the loop's operation ID, upper case>

# falcone #972
engineering-ctl issue-reapprove \
  backlog-loop-falcone-b8a7e731-4304-43de-8c33-26e139bbc9f7-operation-e2535599-198d-46e3-b9f6-e8e2ad8da088 \
  falcone falcone 972 REAPPROVE_ISSUE_E2535599
engineering-ctl backlog-retry <loop_workflow_id> falcone 972
```

The API route is `POST /v1/issues/reapprove` (operator channel only, like `r3-approve`). The
loop's request is rebuilt from the operation's stored command and binding and must equal the
request the loop recorded in its delivery child approvals; nothing comes from the client. It is
refused with an `ISSUE_REAPPROVE_*` code unless:

- the loop is an issue-start loop of that project and its operation is that issue's
  `solve_issue` operation, still `DISPATCHING`/`STARTED` (`LOOP_INVALID`, `OPERATION_MISMATCH`,
  `OPERATION_FINISHED`, `LOOP_REQUEST_MISMATCH`);
- the operation's binding still matches the issue snapshot (`STALE_BINDING`: run `issue-amend`
  first) and its full issue approval still verifies (`OPERATION_APPROVAL_INVALID`);
- the loop Run is `PAUSED` on that issue, proven by its durable pause record written by the open
  Temporal run or, for a loop paused before that record existed, by its `AWAITING_OPERATOR`
  status query (`LOOP_NOT_PAUSED`);
- the operation's risk and delivery mode are within the campaign's recorded approval policy and
  the issue's risk within the operation's (`POLICY_EXCEEDED`, `POLICY_UNAVAILABLE`);
- no other loop of the campaign is open and the current approval is not bound to another open
  issue operation (`CAMPAIGN_IN_USE`, `CAMPAIGN_BOUND_TO_OTHER_ISSUE`).

It then runs issue-start's own approval steps (prepare with the issue request, approve) in one
transaction that holds the campaign approval and row locks, re-checks every precondition first
(a campaign approval or status changed since the first check refuses with `CAMPAIGN_CHANGED`) and
writes the `issue_approval_reissued` dashboard event (operator, new approval binding) with the
approval. The approval has the same one-item, issue-bound shape, goes `APPROVED_POLICY_PENDING`
and waits for the policy reconciler's promotion. A rerun reports `ALREADY_APPROVED` without
changing anything; like every activation wait (issue-start too), it first re-enqueues the policy
activation request if none is pending, so a failed request write is never left stuck.

The loop's ChangeSet keeps its original issue-start approval as custody. Its next delivery or
CI-repair child, registered under the re-issued campaign approval, continues that custody
(NEW-144) only if everything but the campaign approval is identical (operation, operation
binding, request, loop, issue snapshot) and the `issue_approval_reissued` event names that
campaign approval for the operation and loop. The move is recorded once in the ChangeSet's
`recovery_json.issue_execution_approval_reissued` (from/to binding, audit event); correction and
CI-repair bounds keep counting the whole lineage.

### One open issue loop per campaign (NEW-162)

A campaign approval names exactly one issue operation, and every loop of the campaign reports
its state into the one campaign status. On falcone campaign `31c772b3` (2026-10-08) an
`issue-start` of another issue while #978's loop was paused replaced that approval, so #978's
next delivery child was refused ("direct child campaign approval differs from its operation");
the loops' pause and end states overwrote each other's campaign status (one end disabled the
GitHub write policy); and the final `backlog-skip` of #978 re-raised the step error and failed
the campaign.

Operator decision (2026-10-08): a campaign holds at most one open issue loop.

- `issue-start` (and `issue-reapprove`, for anything but the paused loop's own operation) is
  refused with `ISSUE_START_CAMPAIGN_IN_USE` while another loop of the campaign is open
  (`QUEUED`, `RUNNING`, `PAUSED`, `BLOCKED`, `RATE_LIMITED`), or while the current approval
  names another issue operation that is still dispatching or has not registered its loop yet.
  The refusal names the open loop or operation and changes nothing. Finish, `backlog-skip` or
  `backlog-cancel` that loop first; a skipped or cancelled issue is started again later with
  `issue-start` (its revalidation runs again).
- An `issue-start` is also refused while a campaign-wide loop of the campaign is open or its
  `backlog-start` is still being handed off. A campaign-wide `backlog-approve`/`backlog-start` is
  refused while an issue loop of the campaign is open (it used to replace the paused loop's issue
  approval, #972); a campaign-wide loop can still renew its own approval.
- Every approval preparation takes the campaign approval lock and the campaign row lock, so two
  simultaneous starts never both see the campaign free.
- A loop Run still recorded open whose workflow was closed outside Hermes (e.g. terminated with
  `temporal workflow terminate`) would fence its campaign. Before preparing, `issue-start` and
  `issue-reapprove` ask Temporal about every other open loop of the campaign and close
  (`CANCELLED`, dashboard event `backlog.loop_run_closed`) only those Temporal reports closed
  and whose Run did not change since it was read; an unknown state fails the start.
- `backlog-skip` of an issue paused on a failed loop step records the skip and completes its loop
  (`COMPLETE_NOTHING_DELIVERED` when nothing was delivered); it no longer fails the loop or the
  campaign. Workflow patch `existing-backlog-loop-step-skip-closes-v1`, evaluated only when the
  skip arrives, so a loop paused before the deploy also closes cleanly.

Campaigns of different projects running in parallel is NEW-163 (next section).

### Campaigns of different projects in parallel (NEW-163)

Operator decision (2026-10-09): campaigns of different projects may run at the same time. The
shared GitHub write policy (`github-issue-write-validation.yaml`, one switch listing every
project's repositories; its shape is unchanged) is now owned per project:

- At most one campaign per project owns the policy (`policy_role=owner`). A further approval of
  the same project becomes `APPROVED_INVENTORY`, as before; an approval of a project with no
  owner becomes that project's owner (`APPROVED_POLICY_PENDING`, then `APPROVED` by the root
  reconciler) even while another project's owner is running.
- At most `ENGINEERING_BACKLOG_PARALLEL_PROJECT_LIMIT` projects (control.env, default 2,
  minimum 1) hold an owner at once. An approval beyond it is refused with
  `backlog parallel project limit N reached: GitHub write policy is owned by <projects>`; finish,
  cancel or `backlog-release-ownership` one of them first. The root reconciler reads the same setting
  from the same control.env and refuses an enable beside a same-project owner or beyond the
  limit. Changing the limit needs an API restart (the reconciler reads it per request).
- `ENGINEERING_BACKLOG_PARALLEL_PROJECT_LIMIT=1` is the previous rule: one owner globally, every
  other approval (any project) an inventory under it. While an autonomous global backlog run is
  open (`RUNNING`, `PAUSED`, `BLOCKED`, ...) the same single global owner rule applies whatever
  the limit.
- The global backlog stays exclusive: `global-backlog-start` (`GITHUB_WRITE_POLICY_NOT_ACTIVE`)
  and its registration refuse while any campaign outside its own three inventories owns the
  policy. Its inventories are approved one after the other; with a limit of 2 or more the second
  project's approval would become an owner, so set the global backlog up with the limit at 1
  (restart the API), then restore it after the global backlog ends.
- Only one activation request is processed at a time (one `activate.json`): an approval while
  another campaign's activation is pending is still refused, except that `issue-start` now waits
  for that slot (up to the activation timeout, polling) and retries the same approval. If its
  approval commits while the root reconciler has not yet removed the previous `activate.json`, it
  is not approved again: the activation request is re-queued once the file is gone.
- An owner that expires voids only its own project's inventory approvals.
- Disabling and expiry are unchanged: a campaign's terminal disable keeps the policy while any
  other verified owner (of any project) remains, and the policy turns off only when none does.
- Write-time defence: issue delivery publication, final merges and issue comments/closure,
  promotion (when its payload names a project) and staging GitOps writes must target their own
  project's repository pair; otherwise the step is denied with
  `BLOCKED_BY_POLICY`/`REPOSITORY_OUTSIDE_PROJECT` before any command. Finding and enhancement
  publication keep the shared allowlist only (a contract gap may target another project).

## Reset a stuck, never-published ChangeSet (NEW-168)

A delivery attempt that stopped without recording a correction state (a crashed or failed
child, an older code path) leaves its ChangeSet `PLANNED`/`IMPLEMENTING`, and every
`backlog-retry` is refused ("another delivery child requires an explicit correction state").
Instead of a hand-written SQL update, for an **issue-start** loop paused on that issue:

```bash
engineering-ctl change-reset <loop_workflow_id> <project> <repository> <issue_number> \
  <change_set_id> RESET_CHANGESET_<first 8 hex of the ChangeSet ID, upper case> [--mode rebase]

# falcone #975
engineering-ctl change-reset <loop_workflow_id> falcone falcone 975 \
  7221c489-b899-511e-a0fc-c179d759a2e4 RESET_CHANGESET_7221C489
engineering-ctl backlog-retry <loop_workflow_id> falcone 975
```

- `--mode retry` (default): the ChangeSet moves to `REQUEST_CHANGES` (version + 1) with
  `recovery_json.correction_requested = {blocker: OPERATOR_RESET, actor, at}` (the stopped runs
  stay listed), exactly the state NEW-137 records after a provider outage. The next
  `backlog-retry` binds one more child of the **bounded** correction lineage; the reset grants
  no attempt (`change-extend-correction` stays the only, separate, allowance).
- `--mode rebase`: the ChangeSet is reset to its unstarted state (`CONFIRMED`, no branch or head
  SHAs) on the heads of its finding's current-source revalidation
  (`impact_json.revalidated_heads`, NEW-117), its unreleased worktree leases are released and its
  `recovery_json.validation_findings` (about the old base's diff) dropped. The next child binds
  as an unstarted retry, allocates the worktree on the new base (the old branch head does not
  descend from it, so it is archived, not resumed) and runs the maker from scratch. The issue
  approval custody, correction lineage, OpenSpec and any correction extension are kept. Only a
  move the revalidation proves is a fast-forward is made: the ChangeSet must still sit on the
  finding's import heads and the revalidated heads must differ. A ChangeSet already on its
  revalidated heads (gntik-ui #25) is refused with `REVALIDATION_REQUIRED`: a CONFIRMED finding
  is never revalidated again, so a newer base needs a fresh revalidation (a new `backlog-import`
  + `issue-start`, which creates a new finding and ChangeSet). Allocation still requires the
  base branch to be at (or resumable from) that base; if `main` moved on since the revalidation
  the attempt stops with `base SHA mismatch` and the ChangeSet stays retryable.

The API route is `POST /v1/change-sets/reset` (operator channel only). The loop's pause is
proven like `issue-reapprove`'s; everything else is re-read in one transaction under the
Finding and ChangeSet row locks (the order delivery children bind in), and the reset is audited
as a `change_set_reset` dashboard event (also kept as `recovery_json.operator_reset`). Refusals
(`CHANGE_RESET_*`):

- `CONFIRMATION_MISMATCH`, `MODE_INVALID`, `OPERATOR_REQUIRED`;
- `LOOP_INVALID` (not an issue-start loop of that project), `LOOP_NOT_PAUSED` (not `PAUSED` on
  that issue by its open run);
- `CHANGESET_NOT_FOUND`, `APPROVAL_INVALID`, `TARGET_MISMATCH` (not that loop's issue
  operation, campaign issue and finding);
- `PUBLISHED`: a PR URL, release artifacts, publication/CI/merge/release records, a publication
  intent/effect or a deployment exists: a published ChangeSet is never reset;
- `STATE` (terminal/delivered or otherwise not resettable), `ALREADY_RETRYABLE` (already
  `REQUEST_CHANGES` for retry, or unstarted), `LINEAGE_EXHAUSTED` (no correction attempt left);
- `REVALIDATION_REQUIRED` (rebase only, see above).

## Reconcile an uncertain publication effect (NEW-145)

A publication whose outcome Hermes could not observe leaves its
`delivery.publication_effect` Step `UNKNOWN` (or `STARTED`). Until it is resolved, every
implementation lease of that project is refused ("unresolved publication effect requires
reconciliation") and the run keeps its leases. This command resolves only the provably safe
case: an `UNKNOWN` effect whose publisher returned (its committed receipt is `UNKNOWN` for the
current claim) and whose action is a `git_push` of the recorded branch of an already-bound PR
or (NEW-145b) a `pr_create` for it. A `STARTED` effect (a publisher may still be running) still
needs a fenced, manual resolution.

1. Find the effect Step id (`delivery.publication_effect`, status `UNKNOWN`) and its ChangeSet.
2. Make each bound PR branch match the head recorded at the ChangeSet's last confirmed
   publication (`recovery_json.change_evidence.{source,deployment}.head_sha`), e.g. reset a
   branch the uncertain push moved. Never edit the database.
3. Run:

```bash
engineering-ctl publication-reconcile <effect_step_id> RECONCILE_PUBLICATION_<first 8 hex, upper>

# falcone #972
engineering-ctl publication-reconcile ba4a87ad-e4ef-573c-a71b-a902641f49bf \
  RECONCILE_PUBLICATION_BA4A87AD
```

The API route is `POST /v1/publication-effects/reconcile` (operator channel only, like
`r3-approve`). The GitHub worker reads every bound PR live (`gitops.pr_heads`, as NEW-102's
restore does). It refuses with a `PUBLICATION_RECONCILE_*` code unless the effect's run is
terminal (`RUN_ACTIVE`), the effect is `UNKNOWN` (`EFFECT_STARTED`, `EFFECT_NOT_UNRESOLVED`)
with a committed `UNKNOWN` receipt for its current claim (`EFFECT_RECEIPT_INVALID`), its action
is `git_push` or `pr_create` (`EFFECT_ACTION_UNSUPPORTED`), its target is exactly the
ChangeSet's recorded PRs and branch (`EFFECT_TARGET_UNBOUND`, `PUBLICATION_BINDING_MISSING`),
and every bound PR is OPEN or MERGED, still on its recorded head branch and base
(`PR_RETARGETED`), at exactly its recorded head (`HEAD_MISMATCH`, `PR_NOT_OPEN`, `PR_MISSING`).

PR creation (NEW-145b). A created PR need not be a bound URL (for example a replacement at
the configured base after the bound PR was retargeted), so every `pr_create` effect of the
effect's publication intent (whether the effect being reconciled or another one, in any status)
needs listing evidence. The GitHub worker lists every PR whose head is the plan's branch in that
repository (`gh api repos/<repo>/pulls?head=gntik-ai:<branch>&state=all`, any base, paginated to
the end; a listing of 1000 or more is refused, never truncated). The listing must be exactly the
ChangeSet's bound PR for that repository (`PR_CREATED_UNRECORDED`, `PR_LISTING_MISSING`):

- an `UNKNOWN` or `NOT_ATTEMPTED` creation: the bound PR was created before that effect's
  admission, so the uncertain claim created nothing (`BOUND_PR_CREATED_AFTER_ADMISSION`; GitHub reports `created_at` in whole seconds, so a creation within the admission's own second is refused as `PR_CREATION_ORDER_AMBIGUOUS`);
- a `COMPLETE` creation: the PR it created must be the bound one, so the bound PR was created
  at or after its admission (an older bound PR means the created PR is unrecorded:
  `PR_CREATED_UNRECORDED`);
- a `STARTED` creation is refused (`EFFECT_ACTION_UNSUPPORTED`): its publisher may still run.

The publisher settlement below covers the latest `UNKNOWN` receipt of the effect and of these
creations. For falcone #972 the intent holds a `COMPLETE` falcone push and the `UNKNOWN`
falcone `pr_create` ba4a87ad; the head branch lists only PR #1068, created before the
admission, so the effect reconciles once both PRs are at their recorded heads.

An `UNKNOWN` receipt does not prove the push is over: a timed-out command kills only its parent
process, and a git transport child can outlive it. Reconciliation therefore also requires the
publisher's systemd service (`engineering-github-worker.service` for `github-runner`, read
on the controller, never supplied by the client) to run with `KillMode=control-group` and to
have started after the receipt was committed (`PUBLISHER_NOT_SETTLED`,
`PUBLISHER_SETTLEMENT_UNVERIFIABLE`). If it has not restarted since, restart it (or let the
next deploy do so) and run the command again. The restart is read before the PR heads, and
the heads may only be read 30 minutes (`REMOTE_SETTLE`) after it: GitHub can still apply a
push it had already received when the client was killed, and no remote completion evidence
exists, so this bounded wait is the fence for that (an explicit assumption: GitHub finishes
processing a received push well within 30 minutes). A `delivery-runner` effect (worker host) is
refused as unverifiable here.

NEW-147: publication now binds the existing PR by head branch and base and waits (up to 120 s)
for GitHub to show the pushed commit, so the #972 race (an UNKNOWN `pr_create` right after a
`COMPLETE` `git_push`) no longer occurs; if the head never converges the publish blocks with
`PR_HEAD_NOT_CONVERGED` (retry) and creates nothing. For such an existing effect, a bound PR may
also be at exactly the head the intent's `COMPLETE` push delivered; the reconciliation records
the matched basis (`CHANGE_EVIDENCE` or `COMPLETED_PUSH_OF_INTENT`) and leaves `change_evidence`
unchanged (it is diff-bound to its head), so NEW-102's restore refuses until a new delivery
republishes or the branch is reset to the recorded head.

In one transaction under the publication locks it then sets the effect to the terminal status
`RECONCILED` (the receipt is kept; a `reconciliation` record holds the operator, time, method,
recorded and observed heads and the PR-creation listing evidence), writes a `publication_effect_reconciled` dashboard event, and
releases the terminal run's component leases. A rerun reports `ALREADY_RECONCILED`. The loop's
`backlog-retry` then proceeds: the CI-repair restore (NEW-102) and the next delivery child
acquire their leases normally, and that child publishes under its own new effect.

## Issue touched without a content change (NEW-150)

> **Any GitHub edit of an issue under delivery, even a no-op or an edit restored byte for
> byte, moves its GitHub `updated_at`.** Publication custody (NEW-115) compares that time with
> the approved revision, so every later publication claim is refused with "fresh GitHub issue
> observation differs from approval" until the operator runs `issue-touch-refresh`. Avoid
> editing issues under delivery; if one was touched, run this command.

```bash
engineering-ctl issue-touch-refresh <project> <repository> <issue_number>            # preview
engineering-ctl issue-touch-refresh <project> <repository> <issue_number> --confirm <token12>

# falcone #972
engineering-ctl issue-touch-refresh falcone falcone 972
engineering-ctl issue-touch-refresh falcone falcone 972 --confirm <confirm_with from the preview>
```

The API route is `POST /v1/issues/touch-refresh` (operator channel only, like `issue-amend`).
The GitHub worker reads the issue fresh (the same read path as `issue-amend`, now also
returning its labels). The preview shows the approved, consumed and current revision times
and `confirm_with`, the first 12 hex of a token bound to the target, body hash, title hash,
normalized label set, open state and current `updated_at`: a further touch between preview
and confirm refuses (`CONFIRMATION_MISMATCH`). Unlike `issue-amend`,
it is allowed while the delivery is active. It refuses with an `ISSUE_TOUCH_REFRESH_*` code
unless the issue is an approved, non-R3 backlog delivery (`TARGET_INVALID`,
`NOT_IN_APPROVED_BACKLOG`, `R3_REQUIRES_REAPPROVAL`), open in Hermes and on GitHub
(`ISSUE_CLOSED`), and its fresh body and title hashes and label set equal the approved revision
(`BODY_CHANGED`: content changes go through `issue-amend`; `TITLE_CHANGED`;
`LABELS_CHANGED`: labels feed the risk class, so an added `security` or `severity:high`
label needs a re-import and a new approval; `LABELS_UNAVAILABLE` until the GitHub worker runs
this release). Labels are compared as the import records them (lower case, trimmed, sorted).
A GitHub time before the approved one is refused (`REVISION_STALE`).

Confirmed, it runs under the `issue-amend` lock order (Finding, issue record, ChangeSet) and
records, in one transaction with an `issue_revision_touch_refreshed` dashboard event (operator,
body/title hashes, previous approved and consumed times, new time):

- the new time as the issue's effective approved revision (the NEW-115 amendment record with
  `change: TOUCH_REFRESHED`; body unchanged), so later delivery loads consume it;
- for each delivery run that consumed exactly that body (the run named in the ChangeSet's
  `recovery_json.issue_body_revision`, or for a ChangeSet loaded before NEW-115 its `RUNNING`
  runs), an insert-only `delivery.issue_touch_refresh` Step of that run (deterministic id;
  body, title and label hashes, previous and new time, operator; a binding over the trusted
  ChangeSet, run and Step ids). That run publishes against the newer time; a record copied to
  another run or ChangeSet, or tampered, fails closed. The run's own `issue_body_revision`,
  the immutable import and every approval snapshot are unchanged. The records live outside
  `recovery_json` so that its read-modify-write writers cannot drop them; no migration.

A rerun reports `NO_CHANGE`. The next publication claim of the current loop then passes; no
retry is sent (resume a paused loop with `backlog-retry` as usual).

## Operator change requests (NEW-108)

An operator can turn a free-text description of a wanted change into one reviewed, approved
GitHub issue. Hermes drafts and reviews the request but never publishes it: the operator
publishes it on the controller with their own `gh` login. Delivery then uses the normal
`backlog-import`, `backlog-approve` and `backlog-start` path.

```bash
engineering-ctl change-draft --project falcone --text "Add a CSV export of the tenant audit log"
engineering-ctl change-draft --project musematic --file request.md --repository musematic-deploy
engineering-ctl changes --project falcone --status CANDIDATE
engineering-ctl change-show <id>      # rendered title/body, binding, findings, approve command
engineering-ctl change-approve <id> <binding> APPROVE_CHANGE_<PROJECT>_<FIRST16HEX>
engineering-ctl change-publish <id> --dry-run   # exact title, body and gh command; no writes
engineering-ctl change-publish <id>             # publish with the operator's own gh login
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
`change-revise` retries them. All model-written and operator-written text is escaped, so it can't
hide or imitate the issue's `kind:`/`Risk:` lines, headings, checkboxes, review or hidden marker.

`change-show` prints the exact approve command. The typed phrase binds the project and the
first 16 hex digits of the approval binding. The binding covers the request, its revision,
the proposal fingerprint, the target repository, the issue title and the body hash, so any
revision or edit invalidates it. Approve, revise, reject and publish accept only the CLI operator
and allowlisted `telegram:<id>` actors. Approval records a decision record and bumps the
request's decision sequence.

`change-publish` runs on the controller as the operator. It fetches the `APPROVED` request,
re-renders the title and body locally from the stored proposal, and recomputes the binding. It
refuses on any mismatch or on a repository outside the allowlist. With the operator's `gh` (an
argument list, never a shell), it searches the repository for an issue that already carries this
request's hidden marker, from any revision. It adopts an issue only if the title, body and final
marker line match exactly; any other claimant stops the command. Otherwise it runs `gh issue
create`. The `hermes:change-request` and kind labels are added only if they already exist and are
never created. Finally it calls `POST /v1/change-requests/<id>/published` with the URL, binding and
decision sequence. That moves the request to `PUBLISHED` (final) only if all of them still match.
It is idempotent for the same URL, so rerunning `change-publish` adopts the issue instead of
creating another. An `APPROVED` request that is not yet published can still be revised or
rejected.

Backlog import reads a `Change request:` issue's `kind:` line. A `feature`, `bug` or `chore`
request is classified like the same hand-written issue without that metadata, so it is eligible
under the bug filters. An `enhancement` request follows the existing enhancement rules. Its
`Risk: R<n>` line is a floor: the imported risk is the higher of the declared risk and the
keyword-detected risk.

## UI migration blueprint (NEW-123, flow A)

`UiMigrationPlanWorkflow` (`ui-plan-falcone-<id>`) produces the Falcone console migration
blueprint. Models get no MCP: Hermes collects the kit data itself.

```bash
engineering-ctl ui-plan falcone [--source-sha <expected main head>]
engineering-ctl ui-plan-status <id>     # status, hashes, findings, blockers, approve command
engineering-ctl ui-blueprint-show <id>  # Markdown summary of the candidate
engineering-ctl ui-blueprint-approve <id> <binding> APPROVE_UI_BLUEPRINT [--max-items N]
```

The steps are:

1. `ui_plan.register` checks that the released agent pack (`agent_pack_root`) has profile 1.4.0
   or later and the `plan-ui-migration` (with its frame and batch part schemas, pack 1.5.0) and
   `triage-ui-gap` skills with Hermes's synced schemas.
   Otherwise the run is `BLOCKED AGENT_PACK_SKILL_MISSING` and lists what is missing.
2. A read-only snapshot of main (`project.snapshot`), then `ui.inventory` and `ui.kit_snapshot`
   on the scan worker. A kit snapshot that is `BLOCKED UI_MCP_UNAVAILABLE` stops the run with
   that blocker; nothing is drafted without kit data.
3. `ui_plan.stage_inputs` recomputes the inventory and kit snapshot hashes from their content
   and copies them into `/srv/engineering/worktrees/ui-plan-inputs/<id>/`, which only the
   model workers can read. `bootstrap-worker.sh` provisions that root (scan-runner 0700 with a
   traverse ACL for codex-runner and claude-runner); without it the run is `BLOCKED
   UI_PLAN_INPUT_DIRECTORY_MISSING`.
4. NEW-131: the blueprint is drafted in parts. `ui_plan.stage_parts` (scan worker) fills every
   mechanical field from the inventory: page ids, routes, legacy files, reachability, the
   endpoint lists copied verbatim (method, path, cross-check class, `inventory_ref`), the i18n
   namespace and locales, LOC and tiers, legacy tests, orphan pages (`DELETE`), dead modules
   (`DELETE`), the cutover's fixed fields and page list, and the inputs. It splits the routed
   pages into batches of `ui_plan_batch_pages` (default 8; Falcone: 50 routed pages, 7 batches)
   and writes each part a small input file (its inventory slice; the batches get the kit's
   `list_kit` outputs, the frame the whole kit snapshot).
5. Claude drafts the frame first (foundation epics, cross-page gaps with their cutover
   decision, the cutover steps and criteria, global decisions), then each page batch (only the
   judgement fields of its pages), with `plan-ui-migration` (high effort, Read/Glob/Grep only,
   1800 s per call). Codex reviews every part with the same skill in review mode against the
   same slice. A `REVISE` redrafts only that part, once; a second `REVISE` ends `BLOCKED
   BLUEPRINT_PART_REVIEW_UNRESOLVED`, naming the part, its pages and the findings. NEW-132: a
   first draft Hermes rejects (`DRAFT_SCHEMA_INVALID` or `DRAFT_STRUCTURE_INVALID`) uses that
   single redraft too, with Hermes's findings and no review of the rejected draft; a second
   rejection ends `BLOCKED` with the details. A gap's `blocks` holds only epic IDs, page keys
   or `u5-cutover` (schema pattern; pack 1.5.1). The complete
   reviewed frame is staged as a file every batch drafter and reviewer reads. Part ids are
   deterministic (inventory, kit, pages, the reviewed frame, and the provenance: the skill and
   role instruction sources, profile and schemas): a later plan with the same inputs reuses
   every part already approved, after revalidating it against the current schema, instead of
   drafting it again; a skill or schema change forces a redraft.
6. Every part version is immutable; Hermes hashes the exact part file the reviewer read. Hermes
   then assembles the reviewed parts and the mechanical fields into the full blueprint,
   validates it against the full schema and a cross-batch dependency graph check (no cycles,
   no dependency on a later wave or a deleted page, consistent cutover gaps; otherwise `BLOCKED
   BLUEPRINT_GRAPH_INVALID` listing the offending edges), and publishes and stores it as a `CANDIDATE`. Its
   approval binding is the sha256 of the blueprint id, the canonical blueprint hash, the
   reviewed hash (the assembled blueprint), the inventory and kit snapshot hashes and the
   source SHA. A newer candidate for the project supersedes an older, unapproved one.
   Telegram gets "UI blueprint ready for approval". `ui-blueprint-show` ends with the token
   usage of every drafting and review call (failed calls included), per provider and in total.

Histories started before NEW-131 (patch `ui-plan-batched-blueprint-v1` absent) keep the
whole-blueprint draft and review: one Claude draft, a Codex review, at most one redraft.

Approval accepts only the CLI operator and allowlisted `telegram:<id>` actors. It then starts
`UiBlueprintFanOutWorkflow` (`ui-blueprint-fanout-<id>-<n>`). A blueprint has at most one
active fan-out (a compare-and-set marker in its row): a replay while it runs joins it, and only
a replay after it ended consumes the next `<n>`. The fan-out creates one change-request
draft for each foundation epic, page (u1 to u4), the u5 cutover and each gap. The id of each
draft is derived from the blueprint hash and the item key.

- Gaps are drafted by Claude with `triage-ui-gap`. Epics, pages and the cutover are rendered
  deterministically.
- Codex reviews every draft, as in NEW-108.
- Epic, page and cutover lists are rendered in full: a list beyond the proposal bounds goes to
  a `UI migration: <field> (continued)` section of the bound body. An item whose body cannot
  fit an issue fails with `RENDERED_BODY_TOO_LARGE`; no entry is ever dropped.
- The issue body gains a `## UI migration` section with the labels (`ui-migration`, `wave:uN`
  and the item kind) and the `blocked by` change requests: a page is blocked by its backend gaps.
- Kit gaps target `gntik-ai/gntik-ui`. The Hermes GitHub worker never searches or writes that
  repository; `change-publish` with the operator's own `gh` publishes them like any other change
  request. Labels are applied only if they already exist in the repository.

Each fan-out run drafts at most `--max-items` items, capped by `ui_blueprint_fanout_max_items`
(default 80), in batches of 10 that continue as new. Repeating the approval command resumes with
the remaining items; finished items are never drafted twice. Telegram gets "UI fan-out done: N
drafts". Revision 0020 adds the `ui_blueprints` table and `change_requests.ui_context_json`: run
the migration before deploying this release.

### Retrying failed fan-out items (NEW-146)

A fan-out item that fails is recorded `FAILED` and is never drafted again by a later fan-out or
an approval replay. To see why, use `change-show <id>`: its `failure` names the phase, the
step's own blocker (for example `AGENT_PACK_INVALID`) and a `detail` of at most 300
characters, redacted (for example `ValueError: <message>`; a pydantic validation error is
summarised by field and message, never its input). The fan-out result and `ui-plan-status
<blueprint>` (`fanout.last_run.failed_items`, up to 10, and `failed_total`) list the failed items
too, and both `ui-plan-status` and the "UI fan-out done" notification then offer the retry
command. Every provider step that blocks reports such a `blocker_detail`; it is also kept in the
agent execution's error summary.

Fix the cause first (for an agent pack error, the pack or the worker), then:

```bash
engineering-ctl ui-plan-status <blueprint>   # fanout_retry_command, fanout.last_run.failed_items
engineering-ctl ui-blueprint-fanout-retry <blueprint> <binding> RETRY_UI_FANOUT \
  [--kind gap|backend-gap|kit-gap|epic|page|cutover] [--max-items N]
```

- `<binding>` is the blueprint's approval binding. Only the CLI operator and allowlisted
  `telegram:<id>` actors may retry, and only an `APPROVED` blueprint.
- It is refused (409) while a fan-out for the blueprint runs or is being started. A marker left
  by a fan-out that has ended is released first, as an approval replay does.
- In one transaction it resets at most `--max-items` (default and cap
  `ui_blueprint_fanout_max_items`) of the blueprint's `FAILED` fan-out requests, in blueprint
  order, to `DRAFTING` revision 1 (proposal, review, failure and error code cleared). It never
  touches a `CANDIDATE`, approved, published or rejected request, or one that failed after an
  operator `change-revise` (revision 2 or later). Ids stay the same (blueprint hash plus item
  key). The reset is recorded in `fanout.retries` and as a `ui_fanout.retry_requested`
  dashboard event.
- Every write to a fan-out item at revision 1 (item creation, failure, candidate) is fenced
  in its own transaction on the blueprint's active fan-out marker: a leftover activity of an
  earlier fan-out cannot change a reset item; it is a no-op that reports
  `SUPERSEDED UI_FANOUT_SUPERSEDED`.
- It then starts the next fan-out (`ui-blueprint-fanout-<id>-<n+1>`) with `--max-items` as
  its cap, which drafts and reviews the reset items like any other pending item. A repeat finds
  nothing left to reset; if the start fails (503), repeat the retry or the approval to resume.

### Consolidating duplicate gap requests (NEW-154)

The fan-out drafts one gap request per gap per page batch, so one need (cursor pagination, a
create wizard, a reveal-once secret dialog) can appear several times. Consolidate them per
blueprint, one kind at a time if the blueprint is large:

```bash
engineering-ctl ui-blueprint-gap-consolidate <blueprint> [--kind backend-gap|kit-gap]
# PLANNING: one bounded Claude step runs (UiGapConsolidationPlanWorkflow). Repeat the same
# command to see the plan: status PROPOSED, plan_sha12, preview (one line per cluster) and
# apply_command. A repeat never runs the model again for the same inputs; --replan does.
engineering-ctl ui-blueprint-gap-consolidate <blueprint> [--kind ...] --apply <plan_sha12> CONSOLIDATE_GAPS
engineering-ctl ui-plan-status <blueprint>   # change_requests.by_status (MERGED), gap_consolidations
engineering-ctl change-show <id>             # MERGED: merged_into; pages: blocked_by_resolved
```

- The plan reads only the blueprint's mergeable gap requests: `CANDIDATE`, `FAILED` or a
  never-drafted `DRAFTING` revision 1. `PUBLISHED`, `APPROVED`, `REJECTED`, `MERGED` and
  requests under an operator revision are never included. Hermes checks the plan before it is
  stored: an unknown or ineligible key, a key in two clusters, or a cluster that mixes target
  repositories or gap kinds makes it `INVALID` (`problems` lists why; plan again with
  `--replan`). Gaps without an equivalent stay unclustered. The plan is stored with its sha256.
- `--apply` is operator-only (CLI or allowlisted `telegram:<id>`), bound to the plan sha and
  the typed `CONSOLIDATE_GAPS`, refused (409) while a fan-out for the blueprint runs or is being
  started, and refused whole if any member was published, approved or rejected since the plan.
  The plan binds each member's revision and content hash (proposal and statement): a member
  revised or changed since the plan refuses the apply with `UI_GAP_PLAN_STALE` (plan again
  with `--replan`). Every canonical issue body is sized before anything is written, at plan
  time and again at apply: all members' acceptance criteria are kept verbatim (inherited ones
  of a canonical request merged again too, labelled `[<original key> via <canonical key>]`),
  what_changes only when they fit, and 20000 characters stay free for the drafted proposal.
  A cluster that cannot fit the 60000-character body is refused with
  `UI_GAP_CLUSTER_TOO_LARGE` (the plan is INVALID; split the cluster with `--replan`).
  In one audited transaction (`ui_gap_consolidation.applied` dashboard event) it creates one
  canonical request per cluster (id from the blueprint hash and the sorted member keys, item
  key `gap:merged-<hash>`), marks every member `MERGED` with `merged_into` (never deleted, its
  draft kept), and re-points every page, epic and cutover `blocked_by` entry to the canonical
  request. A `CANDIDATE` request is rendered again (new approval binding); an `APPROVED`,
  unpublished one goes back to `CANDIDATE` and must be approved again (`result.reopened`). A
  `PUBLISHED` request is not edited and nothing is changed on GitHub: its
  `consolidation.merged_references` records the merged id its issue body names, and
  `change-show` resolves it (`blocked_by_resolved`, `merged_into`). Repeating the apply is a
  no-op that returns the recorded result.
- The apply then starts (or joins) a fan-out that drafts the canonical requests like any gap:
  Claude drafts with triage-ui-gap from every member's proposal, Codex reviews, and the
  candidate is published with `change-publish` as usual. The verbatim criteria are rendered in
  the canonical issue body, labelled with their item keys; the body also lists the merged
  requests and the items that need it. A failed canonical request is retried with `ui-blueprint-fanout-retry`.
- Revision 0021 adds `MERGED`, `change_requests.merged_into_id` and `consolidation_json`, and
  the `ui_gap_consolidations` table: run the migration before deploying this release. Its
  downgrade refuses while any request is `MERGED`.

### Schema keys and step redaction (NEW-148)

Every step payload is redacted before it is dispatched, and the redactor replaces the value of
any key whose name contains `token`, `secret`, `session`, `key` (as in `api_key`), `password`
and similar. An output schema with such a property name is therefore changed in the request and
refused by the agent pack (`AGENT_PACK_INVALID`, "drifted from the released skill output
schema"); this failed every gap draft until pack 1.5.3 renamed `kit_proposal.tokens` and
`tokens_only` to `style_variables` and `style_variables_only`. Tests in both repositories keep
every Hermes and pack output schema clear of such keys.

The gap draft schema is pinned on both sides, so Hermes and the pack must change together.
While they disagree, `ui_fanout.plan` (and a new `ui-plan`) stops with `BLOCKED
AGENT_PACK_SKILL_MISSING` ("skills/triage-ui-gap output schema differs") before any item is
created or failed. Deploy Hermes first, then release pack 1.5.3 (the quick, reversible step),
then retry the failed gaps with `ui-blueprint-fanout-retry <blueprint> <binding>
RETRY_UI_FANOUT --kind gap`. Start or retry no fan-out between the two steps.
