# Hermes Engineering Operations Dashboard

## Architecture

`engineering-dashboard` is a separate FastAPI process bound only to `127.0.0.1:8750`. It uses
server-rendered Jinja2, local CSS and vanilla JavaScript. Caddy is the only public listener and
proxies the `ade.musematic.ai` virtual host to that loopback address. The existing private API at
`127.0.0.1:8740` is never proxied.

The dashboard reads existing PostgreSQL solution loops, GitHub backlog campaigns, ChangeSets,
Runs, campaigns, coverage cells and findings. It also exposes a small controlled-operation surface:
immutable typed candidates, current-password reauthentication, one-use five-minute action
bindings, and state-dependent workflow signals. The dashboard never accepts a command line,
provider invocation, shell string, repository argument outside the catalog, production target, or
arbitrary Engineering Control argument. All accepted operations use the same application command
bus as Telegram and `engineering-ctl`. The authenticated navigation is:

- **Overview** — current work, queue/blocker summaries and persisted service/provider health;
- **Backlog** — issues and their current workflow/provider state;
- **Workflows** — cursor-paginated workflow summaries and durable detail timelines;
- **Audits** — campaigns, current cells and grouped root blockers;
- **Logs** — redacted operational events and native-agent execution transcripts;
- **Commands** — allowlisted typed forms with explicit disabled/runtime-blocked capabilities;
- **Connections** — fixed read-only probes and redacted persisted health snapshots;
- **Settings** — dashboard password change only.

Legacy `/activity` bookmarks redirect to `/logs`; the authenticated root redirects to `/overview`.
Migration `0010` adds only:

- the singleton `dashboard_authentication` row, containing an Argon2id hash and password version;
- `dashboard_events`, a bounded redacted activity projection with monotonically increasing IDs;
- fail-open PostgreSQL projection triggers whose insertion failures cannot change workflow writes.

Additive migration `0011` adds `agent_executions` and `agent_log_chunks`. It preserves the
dashboard password hash/version, session key, all prior dashboard events, workflow state, backlog,
campaign, finding and evidence data. Each execution summary correlates workflow/run/activity,
project, issue/campaign/ChangeSet, provider, canonical role, host, worker, task queue, phase,
attempts, heartbeat and blocker. Transcript chunks contain only bounded, already CLI-visible,
redacted text; they never contain hidden model reasoning.

Additive migration `0012` adds `action_candidates`, `reauthentication_bindings`,
`operation_requests`, and `connection_snapshots`. Candidate and reauthentication records contain
only hashes, typed non-secret payloads, expiry and session-binding digests; no password, cookie,
API token, or provider credential is stored. Operation requests durably correlate Dashboard,
Telegram and CLI submissions to one workflow/run identity. Connection snapshots contain safe
status names and bounded error summaries published by the component that already owns any needed
credential; the dashboard receives no Telegram, GitHub, Codex, Claude, Kubernetes or product
credential.

Additive migration `0013` adds `operation_exports`. Each row belongs to exactly one completed typed
operation and contains a canonical JSON projection, its SHA-256 integrity digest, its encoded size,
and an expiry. It does not copy raw artifact bodies, coverage-cell result/evidence JSON, agent
transcripts, prompts, model responses, journals, credentials, or internal filesystem paths. The
canonical redactor runs before the bounded projection is inserted. The default maximum encoded
export is 1 MiB and the default retention is seven days; both settings are validated within fixed
safety limits.

Every dashboard mutation requires CSRF plus the current dashboard password. A successful
reauthentication token is HMAC/session bound, action-candidate bound, expires after five minutes,
and is consumed exactly once. High-impact backlog and full/security audit candidates require a
server-generated typed phrase bound to the candidate digest. Capability preflight is repeated in
the API dispatcher immediately before starting or signaling a workflow. Production is a literal
`false` field in the typed schemas. R3 actions fail closed for operator handling.

Full delivery now includes a separate typed `FinalClosureWorkflow`. Candidate staging evidence is
still not final evidence: the closure workflow merges only the exact protected source PR head,
discovers and verifies signed artifacts built at the resulting merge SHA, regenerates immutable
deployment content, obtains an opposite-provider release review, merges the exact deployment PR
head, synchronizes the bounded worktrees to both merge commits, and repeats staging and runtime
verification. Only then may it publish a bounded evidence comment and close the issue. Each GitHub
mutation passes through the same repository write policy as Telegram and `engineering-ctl`, and
uses an idempotent ChangeSet binding. Source-PR mode retains the existing gated path. Project build,
promotion, staging, observer, credential, and production blockers remain authoritative, so the
presence of the closure service does not make an incompletely configured project actionable.

Enhancement candidates use their existing `approval_binding_sha256` as the authoritative immutable
binding. Commands exposes typed approve, reject, and run forms: the server resolves and injects the
binding into the durable operation, repeats project/status/risk/provider preflight immediately
before dispatch, and the metadata repository compares the binding again under its row lock.
Rejection accepts only a fixed reason code. Non-plan execution requires `dry_run=false`, native
Codex/Claude readiness, a bounded risk, and a server-generated confirmation phrase. These controls
call the existing enhancement repository and solution workflow; they do not add a GitHub or shell
bypass.

The application validates the HTTP `Host` before routing. It also requires an allowlisted
`Origin`, or an allowlisted same-origin `Referer` when `Origin` is absent, for every unsafe method.
Anonymous `/api/dashboard/*` requests are rejected before request-body or object validation so the
public boundary does not expose an authenticated route's schema or object-existence differences.
The bounded settings are `dashboard_allowed_hosts` and `dashboard_allowed_origins`; only the
installed public name and loopback origins are defaults. A proxy hostname change must update these
settings through normal release configuration and repeat the Host/Origin regression tests. A
non-default `dashboard_port` must likewise be reflected in the loopback origin entries.

Raw state remains visible. The canonical normalized states are `backlog`, `eligible`, `queued`,
`preparing`, `implementing`, `reviewing`, `reworking`, `testing`, `awaiting_ci`,
`awaiting_approval`, `building`, `deploying`, `verifying`, `blocked`, `paused`, `completed`,
`failed`, `cancelled` and `skipped`. Unknown raw states fail closed as `blocked` with
`UNKNOWN_STATUS_MAPPING`. Roles normalize to maker, checker, auditor, reviewer or verifier while
their raw role remains durable metadata. Audit completion uses terminal coverage cells divided by
total cells; zero-cell campaigns report zero percent and indeterminate work never receives a false
percentage.

Workflow projections distinguish terminal rows, durable external/human waits, active rows that
still require Temporal correlation, and queued/planned start intents older than 15 minutes. The
last class is shown as `ORPHANED_START_INTENT` with a reconciliation next action; it is never
reported as active merely because its database row is nonterminal. Campaigns expose both their
workflow execution status and a derived coverage outcome. A terminal campaign with no executed
cells is `NOT_RUN`; all-blocked cells are `BLOCKED`; findings and partial blockers remain distinct
from a clean pass. If the campaign execution is terminal while any coverage cell remains
nonterminal, its execution and coverage fields remain visible but its derived operator status is
`BLOCKED` until reconciled.

Status domains are explicit per surface. The private control API's campaign `execution_status`
preserves the raw uppercase persisted workflow value, while the dashboard audit projection uses a
lowercase normalized `execution_status` for presentation. Both expose the independently derived
`coverage_outcome`; consumers must not compare raw and normalized fields without conversion.

Operational-event and transcript streams use authenticated SSE, ordered event IDs,
`Last-Event-ID`, bounded PostgreSQL reads, approximately 15-second heartbeats and clean disconnect
handling. Event pages retain at most 1,000 rows; transcript pages retain at most 1,500 rendered
lines. Browser filters survive refresh and use URL query parameters where appropriate.

## Native-agent transcript and redaction contract

Only the native Codex CLI and native Claude Code CLI are provider executions. The provider worker
creates an execution before spawn, reports a roughly ten-second heartbeat, incrementally normalizes
visible output, and closes the execution in a `finally` boundary. Persistence failures do not alter
provider results; instead the summary is marked `transcript_incomplete`. Transcript text is capped
at 5 MiB per execution, normalized lines at 16 KiB and persisted chunks at approximately 64 KiB.
Excess output sets `output_truncated` once while execution continues.

The canonical redactor runs before database insertion, SSE and HTML. It removes credential fields,
authorization headers, cookies, JWTs, private-key blocks, provider token forms, password/secret
assignments, kubeconfig credential material, credential-bearing URLs and ANSI-obscured variants.
Git commit SHAs and ordinary long operational identifiers remain visible. Original unredacted text
is never retained next to a redacted value. Raw prompts, unrestricted tool results, environment
values and raw journals remain SSH/operator-only.

Provider roles mean:

- **maker** — bounded implementation activity (raw roles such as `implementer`);
- **checker** — independent code or outcome review;
- **auditor** — a coverage-cell/domain assessment;
- **reviewer** — release or plan review;
- **verifier** — finding or runtime verification.

## Authentication and password files

There is one password and no username. Sessions are HMAC signed for 12 hours and use Secure,
HttpOnly, SameSite=Strict cookies. The signed value includes `password_version`; a password change
or reset invalidates all older sessions. Successful logout also writes a token-binding digest to
PostgreSQL, so that exact cookie remains revoked across process restarts while other concurrent
operator sessions remain valid. Authorization matches the revocation row's binding digest and
password version and independently requires the current global password version. If PostgreSQL is unavailable,
session validation fails closed and logout returns 503 without clearing the still-active cookie.
Login failures are limited in memory per public client IP.
All state-changing forms use CSRF validation. Controlled operations additionally require the same
password again; the password value is verified in memory and never enters an operation record,
event, log, report, or URL.

Initialize only when no authentication row exists:

```bash
sudo /opt/engineering-control/venv/bin/engineering-ctl dashboard init-password \
  --output-file /home/hermes/.config/hermes-dashboard/initial-password.txt
```

Reset explicitly:

```bash
sudo /opt/engineering-control/venv/bin/engineering-ctl dashboard reset-password \
  --output-file /home/hermes/.config/hermes-dashboard/reset-password.txt
```

The CLI reads the root-readable database environment directly, creates the output atomically,
stores only the Argon2id hash in PostgreSQL, changes the final file owner to match its private
`hermes:hermes` parent, sets mode `0600`, and prints only the path. It refuses an existing output
unless `--force-output` is explicitly used. The service cannot access this directory. Delete a
retrieved initial password as `hermes`:

```bash
rm -- /home/hermes/.config/hermes-dashboard/initial-password.txt
```

The unrelated session key is `/etc/engineering-control/dashboard/session.key`, owned
`root:engineering-control`, mode `0640`, in a root-owned mode-`0750` directory. It never contains a
dashboard password.

## DNS and HTTPS

The only DNS mutation is `ade.musematic.ai A 138.199.173.27`, TTL 300. The fixed helper reads
`/home/hermes/.config/hermes-dashboard/hetzner-cloud-api.token` directly as `hermes`, uses Bearer
authentication with the current Hetzner Cloud API, verifies exact zone authority/protection and
conflicts, and writes the pre-change RRSet to:

- `/home/hermes/engineering/HERMES-DASHBOARD-DNS-BEFORE.json`
- `/home/hermes/engineering/HERMES-DASHBOARD-DNS-ROLLBACK.md`

It accepts only `preflight`, `apply`, `verify`, or exact-backup `rollback`; no zone, record, value or
endpoint argument exists. Run DNS operations without sudo. After API, public DNS, certificate and
HTTPS acceptance all pass, remove the local token file and revoke the deployment token in the
Hetzner Console.

The Caddy site source is `caddy/ade.musematic.ai.caddy`. It explicitly rejects `/v1`, proxies only
to `127.0.0.1:8750`, disables SSE buffering, and sends HSTS, nosniff, DENY framing, no-referrer,
restrictive Permissions-Policy and self-only CSP headers. Validate before every reload.

## Service and routine operations

```bash
systemctl status engineering-dashboard.service
journalctl -u engineering-dashboard.service
curl --fail http://127.0.0.1:8750/health/ready
curl --fail -H 'Host: 127.0.0.1' http://127.0.0.1:8750/internal/metrics
dig +short ade.musematic.ai A
curl -I https://ade.musematic.ai/login
```

The dashboard metrics endpoint is accepted only with a loopback Host and is not a public Caddy
surface. It exports fixed-label counters and active-state gauges for readiness, logout-revocation,
and session-validation persistence failures. A public-host request receives 404. Alert on an
active gauge of one and close the incident only after it returns to zero following the matching
recovery event.

The systemd unit runs as `engineering-control:engineering-control`, not `hermes`. It uses a strict
filesystem sandbox, loopback-only network access and explicit inaccessible operator credential
paths. It receives neither the Hetzner token nor product, GitHub, registry, Kubernetes, Codex,
Claude or Telegram credentials.

Dashboard operational events default to 30-day retention, transcript chunks to 7 days and agent
execution summaries to 90 days. These non-secret settings are validated configuration. The existing
authorized metadata cleanup activity counts expired rows during discovery and deletes at most
10,000 per execution only after normal cleanup authorization and the execution switch. Summary rows
are removed only when their expired chunks are already gone. Audit, finding, deployment and other
evidence artifacts are not transcript rows and are unaffected.

Overview health is a persisted, bounded snapshot, never a probe of provider credential stores. Each
entry names its source, observation time, freshness and stale state. `unknown` means no safe probe
has been persisted; it is not treated as healthy. The dashboard service remains unable to read
Codex, Claude, GitHub, Kubernetes or operator authentication directories.

## Controlled operations and safe acceptance

Backlog Solve, backlog campaign, audit and workflow-control buttons first create an immutable
preflight candidate. Solve is enabled only for an open persisted issue whose eligibility and active
workflow state permit a new candidate. Duplicate issue/campaign submissions return the existing
operation/workflow identity. Backlog concurrency and deployment concurrency are fixed to one.
Audit Start requires a matching completed audit-plan operation from the preceding hour, and
`publish_findings` defaults to false. Pause, resume, nudge, retry and cancel are enabled only for
compatible persisted workflow states.

`POST /v1/commands` accepts only strict discriminated request models and only the authenticated
`engineering-ctl` or `telegram:*` actor classes. Dashboard requests reach the same bus through the
durable `operation_requests` queue; the dashboard process never receives the private API token or a
Temporal client. A started dashboard operation creates a bounded Telegram notification outbox
record containing only project, target, workflow and phase.

The fixed `/api/dashboard/r3-issues/candidate` GET and POST routes authenticate the dashboard
session before examining the query or POST body. Their only accepted query contains exactly the
order-insensitive keys and values `project=musematic`, `surface=source`, and `issue_number=1119`,
with no duplicate or extra keys. Anonymous
missing, malformed and oversized inputs receive the generic authentication response. Authenticated
invalid targets receive the fixed `{"detail":"invalid request"}` 422 response; authenticated valid
targets retain the typed 404/409 operation outcomes. A valid target with an invalid typed body gets
the same generic 422, while bounded JSON media-type, size and syntax errors keep their fixed
415/413/400 responses; an incorrect CSRF token gets the fixed generic 403. The fixed query is validated in application
code after authentication and is therefore intentionally not inferred from FastAPI query-parameter
declarations in the generated OpenAPI schema. Requests larger than the proxy or ASGI request-line
limit can be rejected at that edge before application authentication; the application contract
applies to requests admitted to the ASGI app with an accepted Host/Origin. Host and unsafe-method
Origin enforcement intentionally runs before the route-level session check.

Connections refresh executes only fixed loopback/public HTTP, DNS, TLS and Temporal TCP probes.
Privileged components publish safe heartbeat/quota/event/project snapshots into PostgreSQL. A
missing or old snapshot becomes `NOT_CONFIGURED` or `STALE`; source code presence alone never
produces `CONNECTED`. Status is recomputed at read time: workers expire after 180 seconds,
providers and ordinary service probes after 300 seconds, Hermes/Telegram after 600 seconds,
GitHub broker observations after 900 seconds, and project readiness observations after 24 hours.
The CLI is reported as `PRESENT`, not as a continuously connected service. Telegram requires a
causally ordered delivered response after the latest inbound command; a later delivery failure is
`DEGRADED`, and an old exchange is `STALE`.

## Bounded evidence exports

Commands provides two typed JSON-only operations: campaign summary export and workflow summary
export. The forms accept only a campaign UUID or a bounded workflow ID; there is no output path,
format plugin, command string, query language, or artifact-body selector. Dashboard submissions use
the normal CSRF, current-password reauthentication, immutable candidate binding, and one-use token
flow. Telegram and `engineering-ctl` submit the same discriminated command models and create the
same durable `operation_requests` audit record.

An authenticated dashboard session downloads a completed, unexpired result from the fixed
`/api/dashboard/operations/{operation_id}/export` route. The private authenticated API uses
`/v1/operations/{operation_id}/export`. The CLI equivalents are:

```bash
engineering-ctl export-campaign CAMPAIGN_UUID
engineering-ctl export-workflow WORKFLOW_ID
engineering-ctl operation-export OPERATION_UUID
```

Campaign exports contain campaign identity, aggregate coverage counts, bounded root-blocker
summaries, finding summaries, workflow-run summaries, artifact metadata, truncation flags, source,
generation time, and the explicit exclusion list. Workflow exports contain the existing redacted
dashboard workflow projection plus its exclusion list. Export expiry is enforced on retrieval and
expired rows are removed by the existing authorized metadata cleanup in batches of at most 10,000.
The durable operation audit record remains after its downloadable result expires.

## Read-only observability acceptance

When the existing acceptance feature flag is enabled, the authenticated private API exposes one
strict synthetic canary at `POST /v1/validation/observability` with the literal
`{"confirm_read_only": true}` contract. It creates an empty bounded workspace, records a queued
workflow, runs native Codex as a read-only maker, runs native Claude as an independent read-only
checker, persists their bounded visible transcripts, finalizes the workflow and removes the empty
workspace. It cannot accept a repository, command, prompt, provider or tool argument and performs
no product, GitHub, Kubernetes, deployment or repository mutation.

Troubleshooting checks:

```bash
curl --fail http://127.0.0.1:8750/health/ready
systemctl status engineering-dashboard.service
journalctl -u engineering-dashboard.service --since '30 minutes ago'
```

For a missing transcript, inspect the execution's `transcript_incomplete`, worker heartbeat and
persisted service-health source. Do not grant the dashboard access to provider auth stores and do
not expose `journalctl` through a web route.

## Backup, upgrade and rollback

Before installation, take fresh root-owned runtime, configuration and PostgreSQL backups. Install
only the exact committed tree using the existing commit-addressed controller bootstrap. Because
provider adapters and worker instrumentation are part of this release, controller and worker must
run the exact same committed tree. Apply the reviewed linear migration chain through `0014` only
after the database backup and while the affected dashboard/API writers are stopped. Verify the
`dashboard_session_revocations` table and both indexes before starting the candidate dashboard/API;
candidate authentication fails closed when the table is unavailable. Preserve the current password
hash/version, session key, DNS, Caddy and TLS state. Migration `0013` remains the additive bounded
export projection; migration `0014` adds durable per-session revocations without changing product,
workflow, campaign, finding, ChangeSet, evidence, transcript or session-key data.

Normal rollback:

1. Stop the affected new-release dashboard/controller/provider writers.
2. Switch controller and worker `/opt/engineering-control/source` links to their recorded previous
   commit-addressed releases.
3. Restart the previous controller, worker and dashboard services in controlled order.
4. Do not start pre-`0014` dashboard/API code while revocation rows are merely being ignored. Before
   that code rollback, use the reviewed `0014` downgrade while all writers are stopped; it increments
   the global password version before dropping the revocation table so old cookies cannot revive.
   Verify denial of an acceptance-owned old cookie before reopening the console. If that forward-safe
   rollback cannot be completed, keep the public console in maintenance and repair forward.
5. Leave `agent_executions`, `agent_log_chunks`, and `operation_exports` in place by default; the
   previous release ignores those additive tables. A separate downgrade from `0013` to `0012` is not
   part of normal rollback and requires its own reviewed database change because it removes bounded
   export projections. Do not remove operation audit records. Leave `0012` command candidates,
   operation correlation, connection snapshots, and `0011` transcript data in place.
6. Preserve the dashboard password row/version and `/etc/engineering-control/dashboard/session.key`.
7. Do not change DNS, Caddy or TLS for a normal observability rollback.

Rollback never deletes campaigns, findings, issue-loop state, audit evidence or product data, and
never changes product repositories, Kubernetes, Helm, Argo or production.
