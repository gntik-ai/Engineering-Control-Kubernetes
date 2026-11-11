# Architecture study: Engineering Control on Kubernetes

Status: proposal for discussion. Nothing in this document is implemented.

Sections 1–10 are the first iteration. Section 11 is a second iteration that adds designable
flows, a supervising agent and the options those two make necessary.
Section 12 records the decisions taken on section 11 and takes precedence where it differs.
Section 13 records the final decisions, corrects section 12.4 and gives the consolidated
plan; it takes precedence over everything before it.

This study answers two questions asked ahead of the Kubernetes migration:

1. Does it still make sense to keep Hermes?
2. What should change in the architecture so that one system handles several kinds of
   work — resolving issues, auditing code, creating enhancements — at scale?

## 1. Summary

**Hermes.** The word names three different things in this repository, and the answer differs
for each:

| "Hermes" means | Verdict |
| --- | --- |
| The third-party Hermes agent framework, used here only as a Telegram gateway | **Do not carry it into the cluster.** Replace it with a small first-party chat adapter (about 2–3 engineer-weeks). |
| The `hermes` Unix account (GitHub login, SSH transport, CLI operator check) | **It disappears with the hosts.** Replace with a GitHub App, service accounts and real user identities. |
| The product name used in code, prompts, labels and branch names | **Keep the machine-read identifiers, rename the human-facing text** if a new name is wanted. Renaming identifiers changes hashes and breaks deduplication. |

The framework is not doing the job its name suggests. No model takes part in any command;
the integration is a 1,521-line fixed-grammar forwarder to the private API. What Hermes
contributes is Telegram transport, and it brings a stateful, loopback-bound agent runtime to
get it.

**Architecture.** The platform has the right foundations — Temporal for durable execution, a
typed command bus, deterministic gates around every agent, evidence with provenance — and
those should stay. What limits it is that it grew as one workflow per feature on one host:

- 33 workflow classes in one 8,596-line file, with the author → reviewer → gate loop written
  at least five times.
- No shared queue of work: each project's loop pulls its own issues, and the only
  cross-project selector is fixed at one active issue across three named projects.
- Projects, providers and agent roles are compiled in, so adding any of them is a code change
  across dozens of files.
- Two sources of truth (Temporal and PostgreSQL) held together by about 2,700 lines of
  reconciliation code that is switched off by default.

The proposal is five changes, in order of value:

1. **One work-item model.** Audits, enhancement discovery and planning *produce* work items;
   delivery *consumes* them. The three workflows the question names become two producers and
   one consumer over a shared backlog.
2. **A small mission engine.** A handful of reusable stages composed by short mission
   definitions, replacing the per-feature workflow classes.
3. **A global scheduler** that admits work against explicit budgets (per project, per
   provider seat, per risk class) instead of per-project loops.
4. **Projects, providers and roles as data** with plug-in points for the parts that are
   really project-specific.
5. **One source of truth per fact**, real identities on the API, and a build-and-release
   pipeline for the platform itself.

**Effort.** The earlier infrastructure-only estimate was 17–23 engineer-weeks. The
restructuring in this study adds about 24–31, for roughly 41–54 engineer-weeks in total:
about six months for a team of three, allowing for coordination. The plan in section 8
delivers isolation and scale first and migrates one workflow family at a time, so it can stop
after any phase.

**Second iteration (section 11).** Designable flows built from pre-built modules and a
supervising agent are both feasible without giving up the safety rules, provided those rules
are enforced by the engine rather than written into each flow, and the supervisor acts only
through a closed list of typed actions. With the supporting options recommended there, the
programme grows to about 67–90 engineer-weeks.

**Decisions taken (section 12).** Flows are designed visually with the gntik-ui kit; the
single administrator publishes them without a second approver; the supervisor acts on its
own for reversible actions and waits for the administrator before risky ones; and it runs
on its own credentials. With these the programme is about 72–97 engineer-weeks.

**Final decisions (section 13).** A first-party chat channel; subscriptions for every model,
the supervisor included; deployment on an existing cluster through its Argo CD; the name
Engineering Control; and the full restructuring with all 33 existing workflows imported as
built-in flows. The consolidated plan is about 73–96 engineer-weeks, roughly eleven months
for a team of three. Throughput will be set by the number of subscription accounts, not by
the cluster.

## 2. Scope and method

- Based on reading the repository at the head of branch `claude/determined-cray-5v3lhn`.
  Nothing was executed against production, and the live hosts were not inspected.
- Counts (lines, classes, occurrences) were measured from the code. Where the repository's
  own documents disagree with the code, the code is quoted and the drift is listed in
  appendix A.
- Statements about the live deployment (what is installed, what failed in production) come
  from documents and notes in the repository (`docs/`, `.go-status/`) and are marked as such.
- Effort figures are estimates from code structure, not from a measured velocity.

## 3. The system today

Two hosts under systemd. The controller runs the private API, the dashboard service, evidence
ingest, the notifier, the watchdog, the Temporal workflow worker and the GitHub worker, with
PostgreSQL and Temporal in Compose. One 15 GB worker host runs six role workers (codex,
claude, scan, browser, delivery, release), isolated from each other by Unix users, systemd
sandboxing and POSIX ACLs on a shared filesystem.

| Measure | Value |
| --- | --- |
| Source / tests | about 113,000 / 121,000 lines of Python |
| Workflow classes | 33, all in `workflows/definitions.py` (8,596 lines) |
| Activity types | 1 (`execute_control_step`), dispatching 297 string-named steps |
| Replay patch markers | 73 `workflow.patched(` calls, 0 retired |
| Database tables | 41, in one PostgreSQL server shared with Temporal |
| Private API routes | 99, behind one shared bearer token |
| Registered projects | 6 (`projects.py`) |
| Agent providers | 2 (Codex, Claude), two concurrent runs each at most |
| Recorded incidents | 215 distinct `NEW-xxx` identifiers referenced across the repository |

## 4. Audit findings

Findings are grouped by layer. Each states what was observed and why it matters for the
migration. Severity is this study's judgement: **High** blocks scale or safety on Kubernetes,
**Medium** costs ongoing effort, **Low** is hygiene.

### 4.1 Workflow layer

**W1 — One workflow class per feature, with the core loop duplicated. (High)**
The author → reviewer → deterministic gate → correction loop is the heart of the product and
is written separately in each family:

- issue delivery, as a step tuple with special cases inlined by step name;
- the correction retry around it, three times (backlog loop, CI repair, legacy solution
  loop);
- change requests, with their own two-attempt draft/review loop;
- UI migration planning and backlog decomposition, as near-identical parallel copies
  (`_ui_plan_part` and `_decomposition_part`, `_structural_redraft` twice, `_part_usage`
  differing only in a string);
- the two fan-out workflows, again as parallel copies.

`ExistingGitHubBacklogLoopWorkflow` alone is 2,204 lines with 39 methods, and about 700 of
those lines are nested closures inside one method. A fix or a new rule has to be applied in
each copy, and the incident history shows fixes landing in one and not another.

**W2 — Steps are strings routed by two tables that must agree. (Medium)**
There is one activity type. Workflows pick a task queue for each of the 297 step names in a
180-line routing method; workers hold a second table of which steps each role may run; a
mismatch is a runtime policy violation. Handlers dispatch through if-chains. Nothing is
type-checked across the boundary, and the largest handler (`activities/metadata.py`) is
10,754 lines with 81 steps in one class.

**W3 — Replay-safety debt grows without bound. (High)**
Workflows run for days, so every behaviour change needs a `workflow.patched` fork. There are
73, none ever retired, 33 of them in the backlog loop, and several are generated per issue
and round. The test suite keeps 4,211 lines of frozen copies of old workflow classes to prove
old histories still replay. Worker versioning exists but is off by default (`build_id` is
unset) and uses auto-upgrade, which is what makes the patches mandatory.

**W4 — Workflow history carries the payload. (Medium)**
Each step receives and returns the accumulated context. The backlog records inline payloads
up to 458 KB and one loop result that exceeded Temporal's 2 MB limit. The cleanup workflow
has already been moved to storing lists in a table and passing references; the rest has not.

**W5 — The base class has absorbed one workflow's needs. (Low)**
About 250 lines of `ControlledWorkflow` exist only for the backlog loop's pipelining. Rate
limits and continue-as-new are handled ad hoc in each class.

### 4.2 Scheduling and capacity

**S1 — There is no shared queue of work. (High)**
Each project's backlog loop iterates its own items. The only cross-project selector
(`AutonomousBacklogCampaignWorkflow`) is pinned in code to one active issue, the three
projects falcone, musematic and llmwiki, and one risk level. Priorities cannot be traded
between projects, and nothing can say "run the ten most important items wherever they are".

**S2 — Concurrency limits encode the size of one machine. (High)**
Provider slots are capped at 2 in the settings schema, implementation leases at 4 per
project, pipelined delivery at 3 lanes. The memory comments in the systemd units record the
reason: the worker host was OOM-killed three times at 3 GB and fourteen times at 5 GB. On a
cluster these numbers have no meaning; capacity becomes provider seats and sandbox quota.

**S3 — Quota awareness does not work. (Medium)**
No provider CLI exposes remaining quota, so the snapshot stores "unknown" and the broker
treats unknown as available. Rate limits are discovered by failing. On a limit the loop
pauses for the operator; there is no automatic switch or backoff queue.

### 4.3 Projects, providers and roles

**P1 — Projects are compiled in. (High)**
The project list is a Python `Literal`. A test pins 139 runtime sites, 45 regular expressions
and 35 CLI sites that must list every project. There are 55 lines that branch on a project
name, including identity bootstrap (about 25 branches), delivery mode, and UI-planning
eligibility. The Telegram hook has its own hard-coded list, and it already disagrees: it
knows three projects while the registry has six. Manifests in `config/projects/` carry
commands and delivery settings, but identity, release and bootstrap behaviour is Python.

**P2 — Exactly two providers are assumed. (Medium)**
A two-value enum, "the opposite provider" as a binary rule, per-provider queue names, step
sets and retry errors. A third provider, or a second account of the same provider, is a code
change in about ten places.

**P3 — Roles are half data, half code. (Medium)**
The agent pack supplies role and skill text with recorded provenance, which is good. But the
mapping from step to skill, the read-only versus writable role sets, and the queue routing
live in code, so a new role still needs a release.

### 4.4 State and consistency

**D1 — Two sources of truth, reconciled after the fact. (High)**
Run, step and operation status are kept in PostgreSQL in parallel with Temporal. Keeping them
aligned takes about 2,670 lines (classification, reconciliation, binding, start proofs,
dispatch receipts, publication reconciliation). The reconciler is disabled by default, and
the repository records a period where the flag was on while the deployed code lacked it,
producing a permanent alert. One finding (operations stuck at STARTED) is still open, and
`.go-status/` holds five one-off scripts that repaired stuck rows by hand.

**D2 — Projection triggers swallow their own failures. (Medium)**
Dashboard events are written by triggers on 11 tables whose bodies end in
`EXCEPTION WHEN OTHERS THEN NULL`. A failing projection is silently lost.

**D3 — Connection handling will not survive replicas. (Medium)**
There are 14 separately created engines with their own pools, no pooler, and no
`max_connections` setting. Reconciler leadership uses a session-level advisory lock, which
the documentation says is incompatible with a transaction-mode pooler. Temporal shares the
server and connects as the database administrator.

**D4 — Local-disk state in controller-side activities. (High)**
Beyond the worker's worktrees: file locks for leases, the evidence tree, handoff bundles,
GitOps scratch state, a watched file that triggers a root unit to activate GitHub write
policy, and the watchdog's state file. Each is a single-host assumption. Evidence has one
store class but 37 direct path uses outside it.

### 4.5 Security and identity

**I1 — The API has one shared token and a self-declared actor. (High)**
`api/auth.py` compares one bearer token and then trusts an `X-Engineering-Actor` header that
only has to match a character pattern. Approvals are recorded against that actor. Today the
token is readable by two local accounts, so the weakness is contained by the host. In a
cluster with a web console and several users it is not: anyone holding the token can approve
as anyone.

**I2 — GitHub access is the operator's personal admin login. (High, already recorded as open)**
All GitHub writes use the `gh` credential store in one user's home directory. It cannot be
replicated, scoped per repository, or rotated without an interactive login.

**I3 — Cluster access is by kubeconfig files, and one is cluster-admin. (High, recorded as open)**
Observer, release and GitOps kubeconfigs are files with no rotation. The notes record a
shared cluster-admin kubeconfig on the worker that policy prohibits but the operator kept.

**I4 — Temporal has no TLS or authorisation. (High on a shared network, recorded as open)**
Acceptable on a private two-host link, not inside a cluster where agent sandboxes run
project code.

**I5 — Provider logins are interactive and single-seat. (High for scale)**
Codex and Claude run on subscription logins stored in one home directory each; 28
environment variables that could enable an API key are stripped on purpose. This is the real
ceiling on parallelism and was the open decision from the first study.

### 4.6 Operating the platform itself

**O1 — No continuous integration for the code. (High)**
The only GitHub workflow runs one PostgreSQL acceptance test. Lint, type checks and the
121,000-line unit suite never run automatically. Thirty-five test files skip unless the host
has specific facilities (systemd, bubblewrap, ACLs, a Temporal binary).

**O2 — Releases are manual and have drifted. (High)**
Deployment is operator-run scripts that switch symlinks on both hosts. The repository records
the two hosts running different lineages, and fixes that existed only as in-place edits and
were lost on the next deploy — twice.

**O3 — Monitoring is home-made. (Medium)**
A 1,110-line watchdog probes systemd units and sends alerts through the Telegram gateway.
Metrics are computed from PostgreSQL at scrape time and nothing scrapes them. Logs are
already structured JSON on stdout, which suits a cluster.

**O4 — Documentation has drifted from the code. (Low)**
Counts of workflows, steps and patches, the lease default, and line references in the command
reference are all out of date (appendix A).

### 4.7 What is sound and should be kept

- Temporal as the execution engine, with signals for pause, resume, approve and retry.
- The typed command bus shared by every channel, with idempotency keys and dispatch receipts.
- The separation of author and reviewer, the deterministic gate before any publication, and
  the risk floor that a model cannot lower.
- Approvals bound to exact digests of source, plan and evidence.
- Evidence that is redacted or rejected, with deterministic identifiers.
- The agent pack with recorded provenance per invocation.
- The notification outbox (`FOR UPDATE SKIP LOCKED`, idempotent delivery).
- The replay test harness, which is the right tool even if it should have less to do.

## 5. Should Hermes stay?

### 5.1 What Hermes does today

The Hermes product is a general agent framework: gateway, model loop, skills, tools, memory,
scheduling, plug-ins. This platform uses its gateway, one hook, one plug-in and one
deliver-only webhook. The project's own history explains why: Hermes skills were the engine
until August 2026, Temporal replaced them, and September's hardening removed the gateway's
tools and privileged skills because "an agent framework's instructions are advice to a
model, not technical controls".

What remains:

| Function | Where it lives | Notes |
| --- | --- | --- |
| Telegram transport, inbound and outbound | Hermes gateway (unit and configuration are **not in this repository**) | The only Telegram client the platform has |
| Command grammar and forwarding | `integrations/hermes-engineering-control/hook/handler.py`, 1,521 lines | Deterministic: parse, validate, call `/v1/...`. No model call |
| Sender allowlist | Same file, numeric Telegram ids from a file | The API validates again |
| Duplicate suppression | Hermes "durable command delivery", turned into idempotency keys | Would need to be rebuilt |
| Notifications to the operator | `notifier.py` → HMAC webhook on `127.0.0.1:8644` → Hermes → Telegram | A plain HTTPS bearer mode already exists in the notifier |
| Gateway telemetry | Hook posts events; the API derives "channel healthy" | Consumer hard-codes the `hermes-gateway` subject |
| Free-text chat with a model | Hermes model loop, configured with no tools | Out of scope by the platform's own command reference |

### 5.2 Why it does not fit the cluster

- **It is doing one small job.** Everything of substance is in the hook, which is ordinary
  Python owned by this repository. The framework around it — model loop, skills, memory,
  delegation — is deliberately switched off.
- **It is bound to one machine.** The hook refuses any API address that is not loopback. The
  notifier's HMAC mode accepts exactly `127.0.0.1:8644`. The gateway keeps a home directory.
  In Kubernetes that forces a single stateful pod with the API beside it, which is the
  topology the migration is trying to leave.
- **Its configuration is outside version control.** The gateway unit, the webhook route and
  the pinned hook hash are not in the repository; the activation sign-off document is still
  marked unreviewed; the documents disagree on the installed version and path. A component
  that gates every operator command should be reproducible from source.
- **It has already drifted.** The hook's project list covers three of six projects, and its
  line references in the command documentation are stale.
- **It widens the attack surface for no return.** A model loop reachable from a chat channel
  sits beside the command path. It is configured with no tools, but that configuration is
  recorded as unverified because the gateway's home was unreadable during the last audit.

### 5.3 What replaces it

A first-party **chat adapter**: one small stateless service that receives Telegram updates by
webhook, authenticates the sender, parses the fixed grammar, submits a typed command to the
command bus, and sends replies and notifications.

- The grammar, validation, pagination and redaction move over from `handler.py` largely as
  they are. Project and command lists come from the registry instead of being hard-coded.
- Duplicate suppression uses Telegram's update id as the idempotency key.
- The notifier switches to its existing bearer mode and posts to the adapter.
- Channel health comes from the adapter's own metrics instead of gateway events.
- The same adapter shape serves Slack or another channel later; only the transport differs.

Estimated at 2–3 engineer-weeks including tests, since the logic exists. The free-text model
chat is lost; the platform never relied on it.

### 5.4 The other two meanings

**The `hermes` account.** It is load-bearing in four ways, all of which end with the hosts:

- GitHub credentials in its home → a **GitHub App** with per-repository installation tokens.
  This also closes finding I2.
- SSH transport between the two hosts → not needed once a run's result is handed over as a
  content-addressed bundle (first study, section "Replace SSH").
- `engineering-ctl` refusing to run unless the Unix user is `hermes`, and approvals accepting
  only `engineering-ctl:hermes` → real user identities (section 6.6).
- File ownership and ACLs in bootstrap scripts → the scripts are retired with the hosts.

**The name.** "Hermes" appears on about 350 lines of source. Most are comments, prompts and
titles, which can be renamed freely. A minority are read back by machines and must not
change without a migration: issue-title prefixes used to exclude the platform's own issues
from import, labels (`hermes:risk-r1`, `hermes:change-request`), the CI-baseline branch
pattern and marker file, HTML markers in issue bodies, and namespace strings that feed
deterministic identifiers and approval bindings. Renaming those changes hashes and makes
existing records look new. The recommendation is to rename what people read and leave what
code reads.

### 5.5 A consequence to plan for

On this branch the server-rendered dashboard was removed and the console is read-only. With
Hermes gone and the `hermes` account gone, **no channel could issue commands**. The chat
adapter, authenticated CLI access and command support in the console (section 6.6) therefore
have to land before Hermes is switched off, not after.

## 6. Target architecture

### 6.1 The organising idea: producers and one consumer

The three workflows in the question look different but share a shape:

| Workflow | What it does | What it yields |
| --- | --- | --- |
| Code audit | Agents inspect a snapshot cell by cell; a second provider reproduces each finding | Verified findings |
| Enhancement creation | An agent proposes improvements; a person approves or rejects | Approved proposals |
| Planning (UI migration, backlog decomposition) | An agent drafts a breakdown; another reviews | Child issues |
| Issue import | Reads GitHub | Eligible issues |
| **Issue resolution** | **Spec, implement, review, gate, publish, merge, deploy, verify** | **A delivered change** |

The first four **produce work items**. The last **consumes** them. Today each producer has
its own route to delivery (a legacy "solution loop" for findings, another for enhancements, a
fan-out for plans). In the target there is one backlog of work items and one delivery path:

```text
 producers                      backlog              consumer
 ─────────                      ───────              ────────
 GitHub import ──┐
 Code audit ─────┤                                   ┌─► Delivery mission ─► Release mission
 Enhancement ────┼──► work items ──► Scheduler ──────┤       (one item)        (per target)
 Planning ───────┘    (PostgreSQL)   (admission,     └─► ...
                                      budgets)
```

A work item has a project, a kind, a risk class, a priority, dependencies and an approval
state. Everything else in this section follows from that model.

### 6.2 Mission engine

Replace the per-feature workflow classes with a small set of **stages** and short **mission
definitions** that compose them.

Stages, each a child workflow with typed input and output:

| Stage | Replaces |
| --- | --- |
| Prepare workspace | Sandbox launch, checkout, toolchain preparation, baseline scans |
| Author–review loop | The five copies described in W1: draft, opposite-provider review, deterministic gate, bounded corrections |
| Publish | Bundle handoff, branch push, draft pull request |
| CI watch and repair | CI polling, regression attribution, repair rounds |
| Merge | Protected merge, merge-forward |
| Release | Tag, image pin, deploy through the project's strategy |
| Runtime verify | Observation, browser journeys, opposite-provider review |
| Fan-out | Run a stage over a list with a concurrency limit and collect results |
| Human gate | Wait for an approval bound to a digest |

Mission definitions:

| Mission | Stages |
| --- | --- |
| Deliver item | prepare → author–review (spec) → author–review (implement) → publish → CI → merge → release → verify |
| Audit | plan cells → fan-out (prepare read-only → author–review with auditor and verifier roles) → create work items |
| Discover enhancements | prepare read-only → author–review (proposal) → human gate → create work items |
| Plan (UI, decomposition) | prepare read-only → author–review (frame) → fan-out (author–review per part) → human gate → create work items |

Design rules that keep this honest:

- **Stages are typed modules in code; definitions are data.** In the first iteration the
  definitions were to be fixed Python modules. Section 11 extends this so that people can
  compose and edit definitions, on the condition that safety rules are enforced by the
  engine and not by the definition.
- **The definition version is part of the mission's input.** A running mission keeps the
  definition it started with. New behaviour is a new version, not a patch fork.
- **Missions are short; long-lived things are thin.** A delivery mission lasts hours. The
  only long-running workflow is the scheduler, which does nothing but admit and start
  children and continues-as-new frequently. With short missions, workers can be versioned by
  build and **pinned**, so old runs finish on old code. This is what stops patch markers
  accumulating (W3).
- **Typed activities with one registry.** Each activity declares the capability it needs
  (codex, claude, scan, github, release …); the queue is derived from that. The two routing
  tables (W2) become one declaration.
- **References, not payloads.** Stages pass identifiers; content lives in PostgreSQL or
  object storage (W4).

Existing workflows are **drained, not ported**: new work starts on the new engine while old
executions finish on old workers. There is no obligation to replay old histories on new code.

### 6.3 Scheduler

One scheduler admits work items against explicit budgets:

- **Per provider seat.** One running agent per credential. This is the true unit of capacity
  and replaces worker slots.
- **Per project.** A weight for fairness and a maximum, replacing leases-per-project.
- **Per component.** One writer per component, kept from today's component leases.
- **Per risk class and per environment.** One deployment per environment, one migration or
  secret change globally — kept from today's policy.
- **Global.** A cluster-wide ceiling tied to sandbox quota.

Ordering is by priority, then risk, then age, with dependencies respected. Rate limits feed
back: a provider that reports a limit has its seat paused with backoff and the item returns
to the queue, instead of pausing a whole project for the operator (S3).

The scheduler's state is the work-item table; admission is a transaction, so several API
replicas can submit safely.

### 6.4 Projects, providers and roles as data

**Projects.** A project is a record: repositories, branch rules, test commands, toolchain,
delivery settings, concurrency weight, and the names of the plug-ins it uses. The registry
`Literal`, the pinned site lists and the allowlists go away; validation happens when a
project is loaded. Three plug-in points cover what is really project-specific today:

| Plug-in point | Implementations today |
| --- | --- |
| Release strategy | Helm (falcone), GitOps promotion with Argo CD (musematic), protected merge only (gntik-ui and others) |
| Identity bootstrap for verification | Product admin login and persona creation (falcone, musematic), external identity (llmwiki) |
| Runtime verification | Observer checks and browser journeys per target |

Onboarding a project that fits an existing strategy becomes configuration. The manifest can
live in the project's own repository so that its team owns it.

**Providers.** A provider registry with one entry per credential (seat). "The opposite
provider" becomes "a reviewer from a different provider than the author", which works for
two providers, three, or several seats of each.

**Roles.** The step-to-skill mapping and the read-only/writable classification move into the
agent pack, which is shipped as a versioned image mounted into the sandbox. A new role is a
pack release.

### 6.5 State

- **Temporal owns execution state.** What is running, at which stage, waiting on what, is
  read from Temporal (with search attributes for project, mission kind and work item) rather
  than mirrored row by row. Most of the reconciliation code (D1) exists to repair that
  mirror.
- **PostgreSQL owns domain state.** Work items, change sets, findings, approvals, evidence
  metadata, leases. Workflows change it only through idempotent activities, as now.
- **Projections are explicit.** Where the console needs a denormalised view, one projector
  consumes an outbox table; triggers that swallow errors (D2) are removed.
- **Object storage** holds evidence, transcripts, handoff bundles and change bundles.
- **Database.** A managed or operator-run PostgreSQL with a pooler; Temporal on its own
  database and role; leadership by lease rows rather than session locks (D3).

### 6.6 Identity and access

- **People** sign in to the console through the organisation's identity provider. The API
  takes the actor from the verified token, never from a header (I1).
- **Roles**: viewer, operator (start, pause, retry), approver (approvals, by risk class).
  Approvals are bound to the authenticated person and the digest they approved.
  (Superseded by section 12.2: there is one administrator and no identity provider.)
- **Services** (chat adapter, CLI, workers) each have their own credential with a fixed
  actor.
- **The console regains commands** through the typed command bus, with re-authentication for
  approvals as the removed dashboard had.
- **GitHub** through a GitHub App (I2). **Clusters** through service accounts with the
  existing least-privilege roles and the negative permission checks kept (I3). **Temporal**
  with mutual TLS (I4). **Secrets** through the cluster's secret operator, with rotation.
- **Provider credentials** through a broker that leases one seat to one sandbox at a time
  (I5). Whether seats are subscriptions or API keys remains the open decision.

### 6.7 Execution plane

As proposed in the first study, unchanged by this one: one ephemeral sandbox per mission with
a private workspace, author and reviewer as separate containers, default-deny networking, a
sandboxed runtime, and a per-run evidence token. Two additions from this audit:

- **Where it runs.** The three products share one staging cluster. Agent sandboxes execute
  project code and should not share nodes or network with it. Prefer a dedicated cluster for
  the platform; at minimum a dedicated, tainted node pool with strict network policy.
- **What it removes.** File locks, ACL grants, per-run control groups, temporary-directory
  sweeps, the checker's scratch copy, SSH transport, and the memory slices are all
  replaced by the pod boundary. That is a large share of the incident themes in
  `.go-status/` (out-of-memory kills, stale leases, stalled loops, disk growth).

### 6.8 Building and operating the platform

- **Continuous integration**: lint, type checks, the unit suite, replay tests, image build
  (O1). Host-dependent tests are split out and run in a privileged job or retired with the
  host model.
- **Delivery**: a Helm chart in a separate deployment repository, promoted by GitOps — the
  same model the platform already applies to musematic (O2).
- **Monitoring**: readiness and liveness probes, Prometheus scraping, alert rules. The
  watchdog shrinks to the checks that are about the product (stalled missions, old
  notifications) and loses the ones about systemd (O3).

### 6.9 Component view

```text
            people                          chat
              │                              │
        ┌─────▼─────┐                 ┌──────▼──────┐
        │  Console  │                 │ Chat adapter│
        └─────┬─────┘                 └──────┬──────┘
              │       verified identity      │
        ┌─────▼──────────────────────────────▼─────┐
        │  API · typed command bus · approvals     │
        └─────┬───────────────────────────┬────────┘
              │                           │
      ┌───────▼────────┐          ┌───────▼────────┐
      │   PostgreSQL   │          │    Temporal    │
      │ work items,    │          │ scheduler,     │
      │ domain state   │          │ missions       │
      └───────▲────────┘          └───────┬────────┘
              │                           │ activities by capability
   ┌──────────┴───────────────────────────▼───────────────────────────┐
   │ control workers     GitHub broker     release / verify workers   │
   │ (metadata, policy)  (GitHub App)      (service accounts)         │
   └──────────────────────────────┬───────────────────────────────────┘
                                  │ launches
                     ┌────────────▼─────────────┐
                     │ sandbox per mission      │      object storage:
                     │ author · reviewer · scan │ ───► evidence, bundles,
                     │ (leased provider seat)   │      transcripts
                     └──────────────────────────┘
```

## 7. How the three workflows run in the target

**Resolving an issue.** The import producer keeps the backlog in step with GitHub. The
scheduler admits an item when its project, component and a provider seat are free and its
risk class is approved. A delivery mission launches a sandbox, writes the spec, runs the
author–review loop, passes the deterministic gate, publishes a bundle, and the GitHub broker
opens the pull request. CI, merge, release and verification follow as stages; projects
without a runtime stop at merge. A failure returns the item to the queue with its history.

**Auditing code.** An audit mission plans coverage cells for a snapshot, then fans out. Each
cell runs in a read-only sandbox: an auditor role proposes findings and a verifier from a
different provider reproduces them. Verified findings are deduplicated against open issues
and become work items. Delivery of those items is the ordinary delivery mission — the
separate "solution loop" disappears.

**Creating enhancements.** A discovery mission proposes enhancements with evidence. A person
approves or rejects each in the console or by chat. Approved proposals become work items with
a specification attached and are delivered the same way.

Planning missions (UI migration, backlog decomposition) follow the audit shape: draft, review,
human gate, create child items.

## 8. Migration plan

Phases 0–4 are the infrastructure plan from the first study. Phases A–F are the restructuring
from this one. They interleave; the order below puts isolation and scale first and keeps the
system running throughout.

| Step | Phase | Scope | Engineer-weeks |
| --- | --- | --- | --- |
| 1 | F | CI for lint, types, unit and replay tests; image builds | 2 |
| 2 | 0 | Container images; paths and hosts moved to configuration | 2–3 |
| 3 | 1 | Control plane on Kubernetes: chart, database, Temporal with TLS, secrets, object storage for evidence | 3–4 |
| 4 | D | Identity: sign-in for people, service credentials, roles, GitHub App; console commands; chat adapter; **retire Hermes** | 4–5 |
| 5 | 2 | Sandbox per mission: launcher, workspace, bundle handoff | 6–8 |
| 6 | 3 | Isolation and provider seat broker | 3–4 |
| 7 | A | Work-item model and scheduler; import as the first producer | 3–4 |
| 8 | B | Mission engine and stages; delivery mission; drain the backlog loop | 6–8 |
| 9 | C | Projects, providers and roles as data; plug-in points | 3–4 |
| 10 | B | Audit, enhancement and planning missions on the engine; retire the legacy loops | 3–4 |
| 11 | E | State: Temporal-backed status, outbox projector, references instead of payloads; remove reconciliation code | 3–4 |
| 12 | 4 | Autoscaling, monitoring, runbooks, cut-over | 3–4 |
| | | **Total** | **about 41–54** |

Steps 2, 3, 5, 6 and 12 are the 17–23 weeks of the first study; the other steps are the
24–31 weeks this study adds.

Natural stopping points:

- **After step 6** the system runs on Kubernetes with isolated runs and parallelism limited
  only by provider seats. Workflows are unchanged. This is the smallest outcome that meets
  the original goals of scale and isolation.
- **After step 8** issue resolution, the highest-volume workflow, runs on the new engine with
  a global scheduler.
- **After step 10** all three workflows share one model and the duplicated loops are gone.

## 9. Risks

| Risk | Mitigation |
| --- | --- |
| Provider logins cap parallelism regardless of architecture | Decide seats versus API keys before step 5; the seat broker works for either |
| Rewriting the delivery path regresses safety rules that live in the 2,204-line loop | Extract rules into stages with the existing tests first; run old and new side by side per project; keep the deterministic gate code untouched |
| Draining long-running loops takes weeks | Stop admitting new work to old loops per project; set a cut-off after which remaining items are re-queued |
| The mission engine becomes a second framework | Modules are typed code with engine-enforced invariants (section 11.2); review each new module against "could two flows use it" |
| Removing Hermes leaves no command channel | Step 4 lands console commands, CLI credentials and the chat adapter together, before the gateway is stopped |
| Renaming breaks deduplication and bindings | Machine-read identifiers are frozen (section 5.4) |
| Test suite depends on host facilities | Split in step 1; retire host-model tests as their code is removed |

## 10. Decisions needed

All five were decided; see section 13.

1. **Chat channel.** Keep Telegram through a first-party adapter (recommended), or rely on
   the console and CLI only.
2. **Provider credentials.** Subscription seats behind a broker, or API keys. Unchanged from
   the first study and still the gating decision for scale.
3. **Cluster.** A dedicated cluster for the platform (recommended), or a dedicated node pool
   in the existing staging cluster.
4. **Name.** Keep "Hermes" as the product name, or rename the human-facing text.
5. **Scope.** Stop after step 6 (Kubernetes with today's workflows) or continue to the full
   restructuring.

## 11. Extended options (second iteration)

This part builds on sections 1–10. It evaluates two requested capabilities — designable
flows and a supervising agent — and adds the further options that those two make necessary.
It changes one rule of the first iteration, noted in 11.1.

### 11.1 What changes from the first iteration

Section 6.2 originally ruled out user-editable workflow definitions. The reason was safety:
today every rule that keeps an agent from publishing, merging or deploying unchecked is
written inside workflow code, so a freely edited workflow could simply leave a rule out.

Designable flows are compatible with that concern if the rules move **out of the flow and
into the engine**. A flow author arranges modules; the engine refuses to publish or run an
arrangement that breaks an invariant. With that in place the first iteration's stages become
the first entries of a module catalogue, and its mission definitions become the first
built-in flows. Section 6.2 and the risk table have been amended accordingly.

### 11.2 Option A — designable flows from pre-built modules

#### What a flow is

A flow is a versioned document: a graph of module instances, their parameters and the
connections between their typed inputs and outputs. Modules are code, shipped with the
platform; flows are data, created by people.

#### Module catalogue

Almost every module already exists as code in the current system. The work is to give each a
typed interface and remove its assumptions about which workflow calls it.

| Group | Modules | Built from |
| --- | --- | --- |
| Triggers | Work item of a given kind, GitHub event, schedule, manual start, call from another flow | Backlog import, scheduler, command bus |
| Workspace | Read-only snapshot, writable workspace, toolchain preparation, dependency resolution | Worktree manager, toolchain and dependency steps |
| Agents | **Claude CLI agent**, **Codex CLI agent**, generic agent (provider chosen at run time) | `adapters/claude`, `adapters/codex`, provider activity |
| Agent presets | Spec architect, implementer, reviewer, auditor, finding verifier, triager, planner | Agent pack roles and skills |
| Review | Reviewer (must be a different provider from the author), author–review loop with a correction budget | The five loop copies in W1 |
| Deterministic checks | Secret, vulnerability and static-analysis scans; test commands; baseline comparison; change validation gate; risk classification; schema validation | `adapters/scanning`, `delivery.validate_changeset`, pre-existing-failure checks |
| GitHub | Read issue, comment, label, create or update issue, open draft pull request, watch CI, merge, tag release | Typed GitHub broker operations |
| People | Approval gate bound to a digest, question to the operator, notification | Approvals, notification outbox |
| Release and verification | Deploy through the project's strategy, runtime observation, browser journey | Release adapters, observer, journey runner |
| Control | Condition, bounded loop, fan-out with a concurrency limit, wait, sub-flow, create work item | Mission engine |
| Records | Publish evidence, summarise a run | Evidence pipeline |

Every agent module exposes the same parameters: provider and model, role and skills, prompt
template, tool permissions (read-only or write within the workspace), output schema, and
time and token budgets.

#### Editable prompts

Today prompts are assembled in Python — `activities/metadata.py` alone has about 46 prompt
references, concatenating toolchain rules, maker rules and deployment-pin text — on top of
the role and skill files in the agent pack. To make them editable:

- Prompts become **versioned templates with declared variables**, stored with the flow.
- Each template has two parts. The **contract** (output schema, tool boundaries, safety
  rules, redaction) is fixed by the module and cannot be edited. The **instructions** are
  editable.
- The exact template version and its hash are recorded with every invocation, extending the
  provenance record that exists today.
- A changed prompt is a new flow version and goes through the same validation and
  evaluation as any other change (11.4, option C).

#### Invariants the engine enforces

Checked when a flow is published and again at run time. A flow that violates one cannot be
published.

| Invariant | Enforcement |
| --- | --- |
| Nothing an agent wrote reaches GitHub, a merge or a deployment without passing the change validation gate | Graph check: every path from a write-capable agent to a publishing module passes through the gate |
| Author and reviewer are different invocations from different providers | Graph and run-time check on the review module |
| The risk floor can only rise | Risk classification is computed by the engine, not passed between modules |
| High-risk work needs a human approval bound to the exact digest | Engine inserts the gate from the item's risk class; a flow cannot remove it |
| Agents never hold GitHub, release or cluster credentials | Credentials attach to module types, not to flows; agent modules have none |
| Loops and fan-outs are bounded | Maximum iterations and width are mandatory parameters with platform ceilings |
| Mutating modules run in dry-run unless the target is enabled and approved | Kept from today's `_mutation_steps` rule |

#### Lifecycle

Draft → validate (types, invariants, permissions) → simulate (dry-run against recorded or
synthetic data) → evaluate (option C) → publish as an immutable version → run. A run is
pinned to the version it started with. Publishing a flow that contains merge or deploy
modules needs an approver, like any other high-impact change.

#### Three levels, built in order

| Level | What the user can do | Added effort (engineer-weeks) |
| --- | --- | --- |
| 1. Configurable flows | Pick a built-in flow per project; switch optional stages on or off; edit prompts, models and budgets | 2–3 for prompt templates; the rest comes with the mission engine |
| 2. Composable flows | Write a flow as a document from the module catalogue; validation, simulation, versioning; stored in Git | 6–8 |
| 3. Visual designer | Draw and edit flows in the console | 5–7 |

Level 3 is an editor for the level 2 format, not a separate system. The UI kit already
ships what it needs: `@gntik-ai/flow` (a node-graph canvas) and `@gntik-ai/editor` (a code
editor for prompts), both published at 0.11.0.

**Recommendation:** build the mission engine as a flow interpreter from the start (about
2–3 weeks more than the plain engine of step 8), deliver level 1 with it, then level 2.
Treat level 3 as optional: it makes flows approachable but adds no capability.

#### Costs and risks

- **Testing surface.** Built-in flows can be tested exhaustively; arbitrary arrangements
  cannot. The invariants bound the damage, and the evaluation lab (option C) bounds the
  quality risk.
- **Debuggability.** A failed run of a custom flow must be explainable without reading
  engine code. This needs the run trace view (option F).
- **Support.** Each flow someone designs is something the platform must keep running across
  module upgrades. Modules therefore need version and compatibility rules.

### 11.3 Option B — a supervising agent

#### What handles stalls and errors today

Handling is fixed in code: activity retries, a classifier that maps transient provider
failures to "pause and wait for the operator", a watchdog alarm for stalled backlog loops,
and eight operator commands (pause, pause after current, resume, nudge, retry, stop after
current, cancel, skip). When none of that fits, a person investigates. The repository's
`.go-status/` directory contains five scripts written to repair stuck runs by hand, and one
finding about operations stuck in STARTED is still open. A supervisor that triages these
cases has clear value.

#### Design

The supervisor is **not** a model polling every run. It has three parts:

1. **Detectors (deterministic).** They open a *case* when a run makes no progress beyond its
   expected duration, a module fails after retries, corrections repeat without converging, a
   provider is limited or logged out, a run exceeds its budget, or an item is blocked longer
   than a threshold.
2. **Known-signature rules (deterministic).** A table of failure signatures with a fixed
   response — for example "provider overloaded → retry on another seat after backoff". Cases
   that match never reach a model.
3. **The supervising agent.** Invoked once per unmatched case. It reads the run's status,
   timings, error codes, the tail of the transcript, the evidence and similar past cases,
   and returns a structured decision: a diagnosis, one action from a closed list, and its
   reasoning.

The agent's output is data. Deterministic code checks it against policy and executes it
through the same typed command bus people use. This follows the lesson the project already
drew from its Hermes period: instructions to a model are advice, not controls.

#### What it may and may not do

| May (through typed, policy-checked actions) | Never |
| --- | --- |
| Retry a module; retry on another provider or seat | Approve anything |
| Nudge or resume a waiting run | Merge or deploy |
| Return an item to the queue, or skip it with a reason | Lower a risk class or bypass a gate |
| Pause a flow or a project | Edit flows, prompts or policies |
| Extend a correction budget within a platform ceiling | Touch credentials or run commands in a workspace |
| Escalate to a person with a summary; open an incident; file a platform bug | Act on the same case more than a set number of times |

#### Autonomy levels

| Level | Behaviour |
| --- | --- |
| 0. Advise | Diagnosis and a proposed action go to the operator, who accepts with one action |
| 1. Act on reversible steps | Retries, nudges, re-queues and seat switches run automatically; everything else is advice |
| 2. Act broadly | Also skips, pauses and budget extensions, within limits per project and per hour |

The level is set per project and per risk class. Start at level 0 and record how often the
operator accepts the proposal unchanged; promote a category of action only once that rate is
high. Recurring agent decisions should be turned into signature rules, so the model handles
a shrinking share of cases.

#### Choosing the model

- The supervisor's provider and model are settings, like any agent module: Claude or an
  OpenAI model, with a fallback order.
- It needs **its own credential**, separate from the seats that run work. Otherwise the
  commonest failure — a limited or logged-out provider — also silences the supervisor.
- If the case concerns one provider, prefer the other for the diagnosis.
- If no model is reachable, the deterministic fallback applies: pause the affected run and
  notify a person.
- It runs headless and continuously, which is the natural case for API access rather than an
  interactive subscription login. This adds weight to decision 2 in section 10.

#### Safeguards

- Every case, decision, rationale and executed action is recorded and visible in the
  console.
- Action budgets per run, per project and per hour, with a circuit breaker that escalates.
- The plain liveness checks stay deterministic and independent, so a failed supervisor is
  itself detected.
- The supervisor reads; it has no workspace and no write tools.

#### Effort

| Part | Engineer-weeks |
| --- | --- |
| Detectors, case model, signature rules, action policy | 3–4 |
| Supervising agent at level 0, with the decision inbox (option D) | 2–3 |
| Levels 1 and 2, acceptance tracking, rule promotion | 2–3 |
| **Total** | **7–10** |

### 11.4 Further options

Editable flows and an agent that acts on its own judgement both create needs that the first
iteration did not have. These are the options I consider necessary, in order.

**C — Evaluation lab (necessary for A).** Once prompts, models and flows can be edited,
changes need evidence before they reach real issues. A set of past work items per project is
replayed in dry-run sandboxes against a candidate flow version and scored: gate pass rate,
correction rounds, time, cost, reviewer findings. It also supports comparing two models on
the same items, which today exists only as a hash-based rotation between providers.
Effort: 4–5 weeks.

**D — Decision inbox (necessary for B at level 0).** One place, in the console and mirrored
to chat, for everything waiting on a person: approvals, supervisor proposals, operator
questions. The console currently has no write actions, so this is also where commands
return. Effort: 2–3 weeks, partly counted in step 4.

**E — Budgets and cost accounting (necessary for both).** Per run, flow and project limits
on tokens, wall-clock time and correction rounds; cost per delivered item by flow version
and model. Token usage is already recorded per invocation. A breached budget opens a
supervisor case instead of failing silently. Effort: 2 weeks.

**F — Run trace view (necessary for A).** A timeline of one run: modules, inputs and outputs
by reference, agent transcripts, gate results, supervisor actions. It is what a flow author
debugs with and what the supervisor reads. Effort: 2–3 weeks.

**G — Model routing policy.** A catalogue of providers and models with per-module defaults,
per-project overrides, a fallback order and the "reviewer differs from author" constraint.
This is the general form of choosing a model for the supervisor and for each agent module,
and it replaces the model names currently fixed in `config.py`. Effort: counted in step 9.

**H — Event-driven triggers.** With a GitHub App the platform can receive events instead of
polling, so a flow can start when an issue is labelled or a pull request is reviewed.
Effort: 1–2 weeks on top of the GitHub App.

Options I would defer:

- **Project memory** — feeding recurring reviewer findings back into author instructions.
  Valuable, but it should wait for the evaluation lab to measure whether it helps.
- **A shared flow library across organisations.** Flows in Git already give reuse within one
  organisation.

### 11.5 Effect on the plan

The options add to the 41–54 weeks of section 8.

| Option | Engineer-weeks | Recommended |
| --- | --- | --- |
| A — engine built as a flow interpreter (added to step 8) | 2–3 | Yes |
| A — prompt templates, level 1 | 2–3 | Yes |
| A — composable flows, level 2 | 6–8 | Yes |
| A — visual designer, level 3 | 5–7 | Later |
| B — supervisor, levels 0 and 1 | 5–7 | Yes |
| B — supervisor, level 2 | 2–3 | Later |
| C — evaluation lab | 4–5 | Yes |
| D — decision inbox | 2–3 | Yes |
| E — budgets and cost | 2 | Yes |
| F — run trace view | 2–3 | Yes |
| H — event triggers | 1–2 | Yes |
| **Recommended set** | **26–36** | |
| **Everything** | **33–46** | |

With the recommended set the programme is about **67–90 engineer-weeks**: nine to ten
months for a team of three. Section 12.5 revises this after the decisions: the visual
designer and full supervisor autonomy are in scope.

Where they fit in the sequence of section 8:

- **With step 4 (identity and console):** decision inbox.
- **With step 8 (mission engine):** interpreter, prompt templates, run trace view, budgets.
- **After step 8:** supervisor at level 0, then level 1 once acceptance is measured.
- **After step 10 (all workflows on the engine):** composable flows, evaluation lab, event
  triggers.
- **Last, if wanted:** visual designer, supervisor level 2.

The stopping points of section 8 still hold. The first new one worth naming: after step 8
with the supervisor at level 0, issue resolution runs on the new engine with editable prompts
and assisted triage — the point where day-to-day operation changes most.

### 11.6 Additional decisions

These four were decided; see section 12.

6. **How far to take flow design:** configurable (level 1), composable (level 2,
   recommended) or visual (level 3).
7. **Who may publish flows,** and whether flows containing merge or deploy modules need a
   second approver (recommended).
8. **Supervisor autonomy:** the starting level (recommended: advise only) and which projects
   and risk classes may be promoted.
9. **Supervisor model and credential:** which provider is primary, and whether it uses API
   access.

## 12. Decisions taken and their consequences (third iteration)

The administrator decided the four questions of section 11.6. This section records each
decision and what it changes in the design. Where it conflicts with sections 6 or 11, this
section takes precedence.

| # | Question | Decision |
| --- | --- | --- |
| 6 | How far to take flow design | **Visual designer**, built with the gntik-ui kit |
| 7 | Who may publish flows | **The administrator.** The platform has one user; no second approver |
| 8 | Supervisor autonomy | **As autonomous as possible**, stopping to wait for the administrator before risky actions |
| 9 | Supervisor credential | **Its own credentials** (see 12.4 for how this was read) |

Decisions 1–5 of section 10 were open at this point; they are taken in section 13.

### 12.1 Visual flow designer

All three levels of section 11.2 are now in scope. The designer is the main way flows are
created; the document format of level 2 stays underneath it as the stored form.

**Screen layout**, using `SidebarLayout` and `InspectorLayout` from `@gntik-ai/ui`:

| Area | Content | Kit component |
| --- | --- | --- |
| Left | Module palette, grouped as in the catalogue, searchable | `TreeList`, `SearchField` |
| Centre | The flow graph: drag modules in, connect outputs to inputs | `@gntik-ai/flow` |
| Right | Inspector for the selected module: parameters, provider and model, budgets, permissions | Forms generated from the module's schema |
| Right, prompt tab | Prompt editor with the fixed contract shown read-only above the editable instructions, and the declared variables | `@gntik-ai/editor` |
| Bottom | Validation results, simulation output, version history | `Tabs`, `DiffViewer` |

**Behaviour:**

- **Typed connections.** An output can only be dropped on an input of a compatible type.
- **Live validation.** The invariants of section 11.2 are checked as the graph changes. A
  violation is shown on the offending connection or module with the rule it breaks, and
  blocks publishing. Gates the engine will insert (for example the approval for high-risk
  items) are drawn as locked nodes so the author sees the flow that will really run.
- **Simulation.** Runs the draft in dry-run against a recorded work item and shows the
  result per module.
- **Versions.** Every publish creates an immutable version with a diff against the previous
  one. Running flows keep the version they started with.
- **Built-in flows** (deliver item, audit, discover enhancements, plan) open in the designer
  as editable copies; the originals stay available as a baseline.
- **Run view.** The same canvas, read-only, shows a live or finished run with each module's
  state. This is the run trace view of option F, so the two are built together.

**Storage.** With one user editing in a browser, the database is the source of truth for
flows and versions. Flows can be exported and imported as documents, which also gives a
backup and a way to move a flow between environments. This replaces "stored in Git" in
section 11.2.

**To verify during the build.** The console runs under a strict Content-Security-Policy
(no inline styles or scripts). The graph canvas must be checked against it, as the select
popup had to be when the console was built.

### 12.2 One administrator

- **Identity.** No identity provider and no roles. The existing password sign-in stays, and
  the administrator is the only human actor. This replaces the "People" and "Roles" items
  of section 6.6.
- **What still has to change.** Finding I1 stands: the API must take the actor from the
  credential presented, not from a header. Each channel — console session, CLI, chat
  adapter — gets its own credential mapped to "administrator via that channel", and each
  service keeps its own.
- **Publishing flows.** The administrator publishes directly. There is no second approver.
- **A safeguard that costs one prompt.** Publishing a flow that contains merge or deploy
  modules, and giving any approval, asks for the password again. This is the control the
  removed dashboard had for commands. It protects against an unattended session, not
  against the administrator.
- **Unaffected.** The separation of author and reviewer is between agents and does not
  depend on how many people use the platform.
- **Kept open for later.** Actions are still recorded against an actor, so more users or
  roles can be added without changing stored records.

### 12.3 Supervisor: autonomous by default, waits before risky actions

The levels of section 11.3 are replaced by one mode. Every action belongs to one of three
classes. The administrator can move an action between the first two classes; the third is
fixed.

**Automatic.** The supervisor acts and reports afterwards. These are reversible and have no
effect outside the platform.

| Action | Limit |
| --- | --- |
| Retry a module that has no external effect | Bounded per run |
| Retry on another provider or account | When the first is limited, overloaded or logged out |
| Nudge or resume a waiting run | — |
| Return an item to the queue | Bounded per item |
| Extend a correction budget | Up to the platform ceiling |
| Pause one run | — |
| Skip an item, with a recorded reason | The item stays in the backlog and can be re-queued |
| Pause one flow or one project | When failures repeat across its runs |
| Open an incident; file a platform bug | — |

**Waits for the administrator.** The supervisor prepares the action with its diagnosis and
evidence, puts it in the decision inbox, notifies by chat, and holds the affected run in a
paused state. Other runs continue. A waiting action is never taken on a timeout; reminders
are sent instead.

| Action | Why it is risky |
| --- | --- |
| Cancel a run that holds unpublished work | The work is discarded |
| Retry a publish, merge, release or deploy module after a partial failure | External effects may already have happened |
| Resolve a publication whose outcome is unknown | Needs a judgement about external state |
| Any corrective action on an item at the highest risk class | Policy already requires a person there |
| Close or abandon a work item | Changes what the backlog will deliver |
| Go beyond a budget ceiling or an action limit | The limits exist to stop loops |
| Pause every project | Stops the platform |
| Any action when the diagnosis is uncertain | The agent reports its confidence; below a threshold it asks |

**Never.** Approve, merge, deploy, lower a risk class, bypass a gate, publish or edit a flow
or prompt, or touch credentials. Unchanged from section 11.3.

**Two additions that widen what the supervisor can usefully do without widening its
authority:**

- **Draft fixes.** When the cause is a prompt or a flow — for example the same reviewer
  objection across many runs — the supervisor may draft a changed version and place it in
  the inbox. Only the administrator publishes it.
- **Learned rules.** A decision the supervisor takes repeatedly with the same outcome is
  proposed as a known-signature rule, so that case stops needing a model.

**Controls that make full autonomy acceptable:**

- Limits on automatic actions per run, per project and per hour. Reaching a limit turns the
  next action into one that waits.
- Every decision is recorded with its evidence and reasoning, and summarised to the
  administrator in a daily digest.
- Any automatic action can be reversed from the inbox, and any action can be moved to the
  waiting class in settings.
- Liveness checks stay deterministic and independent of the supervisor.

### 12.4 Supervisor credentials

**Withdrawn.** The decision was corrected: every model, the supervisor included, runs on a
subscription and no API key is used. See section 13.1. The text below is kept for the
record only.

The decision was "use credentials". It is read here as: **the supervisor has its own
provider credentials** — an API key for the chosen provider, with a second provider
configured as fallback — stored as a cluster secret and separate from the accounts that run
work. Consequences:

- The supervisor keeps working when a work account is rate-limited or logged out.
- Its usage is metered and budgeted on its own (option E).
- The rule that strips API keys from agent environments stays in force for work agents; the
  supervisor is a separate service with a separate environment.

This does not settle decision 2 of section 10 (how the agents that do the work
authenticate), which still sets the achievable parallelism. If "use credentials" was meant
to cover those agents as well, that decision is closed in favour of API keys and the seat
broker of section 6.6 reduces to a per-key concurrency limit.

### 12.5 Revised scope and effort

| Item | Engineer-weeks | Change |
| --- | --- | --- |
| Sections 1–10 (migration and restructuring) | 40–53 | One week less: no identity provider or roles |
| Flow interpreter, prompt templates, composable flows | 10–14 | Unchanged |
| Visual designer, including the run view | 6–8 | Now in scope; absorbs option F |
| Supervisor, single autonomous mode with action classes | 7–10 | Now in scope in full |
| Evaluation lab | 4–5 | Unchanged |
| Decision inbox | 2–3 | Unchanged; required by 12.3 |
| Budgets and cost | 2 | Unchanged |
| Event triggers | 1–2 | Unchanged |
| **Total** | **72–97** | |

About ten to eleven months for a team of three.

Sequence, adjusting section 11.5:

- **With step 4:** single-administrator credentials per channel, decision inbox.
- **With step 8:** flow interpreter, prompt templates, budgets, run view on the canvas.
- **After step 8:** supervisor with automatic and waiting actions.
- **After step 10:** composable flows and the visual designer, evaluation lab, event
  triggers.

The designer comes after step 10 because it needs every workflow family expressed as
modules first; before that, built-in flows are configured through settings and prompt
templates.

## 13. Final decisions and consolidated plan (fourth iteration)

All open decisions are now taken. This section records them, corrects section 12.4, and
restates the plan in one place. It takes precedence over earlier sections where they differ.

| # | Question | Decision |
| --- | --- | --- |
| 1 | Chat channel | **Yes**: a first-party chat adapter replaces the Hermes gateway |
| 2 | How agents authenticate | **Subscriptions**, for every model |
| 3 | Cluster | **An existing cluster**, deployed through its existing Argo CD if suitable |
| 4 | Name | **Engineering Control** |
| 5 | Scope | **Full restructuring**, with the existing workflows imported as flows |
| 9 | Supervisor credential (corrects 12.4) | **Subscription**, not an API key |

### 13.1 Subscriptions for every model

No API keys anywhere: the working agents, the reviewer and the supervisor all run the
provider's command-line client on a subscription login, as today. The rule that strips API
keys from agent environments stays and now also covers the supervisor. Section 12.4 is
withdrawn.

**Accounts as the unit of capacity.** A subscription login is an *account*. The platform
keeps a pool of accounts per provider and the scheduler admits work against it.

| Aspect | Design |
| --- | --- |
| Storage | Each account's client home (its login tokens) is a persistent volume of its own. The client refreshes tokens in place, so the volume must be writable and must survive the pod |
| Use | An account is leased to a sandbox for one agent invocation and released afterwards. Concurrent invocations per account is a setting, starting at the value that works today (two) |
| Health | The existing status probes (`claude auth status`, `codex login status`) give each account a state: ready, limited (waiting with backoff), or login required |
| Login | Interactive, as it is today over SSH. In the cluster it becomes an item in the decision inbox that opens a login session for that account. How each client's browser login can be driven from the console has to be verified per client |
| Rate limits | Neither client exposes remaining quota. The scheduler learns from refusals: the account is set to limited until the reset time the client reports, or with backoff when it reports none |
| Supervisor | A read-only pod running the same client with structured output. It should have **an account of its own** on at least one provider, so that a limited work account does not silence it, with the other provider as fallback. If neither answers, the deterministic fallback applies: pause the affected run and notify |

**What this means for scale.** Parallelism is the number of accounts multiplied by the
concurrent invocations each tolerates, not the size of the cluster. A running mission holds
about one invocation at a time (author, then reviewer from the other provider), so N
missions in parallel need about N invocation slots spread over both providers, plus the
supervisor's. Adding capacity means adding accounts.

**Two things to confirm outside the code:**

- Whether the subscription plans permit this many automated, concurrent sessions, and what
  their limits are. This is a question of plan terms, and it bounds the whole programme.
- How many accounts per provider are to be bought. The design works with any number; the
  achievable throughput follows from it.

### 13.2 Chat channel

A first-party adapter as described in section 5.3: a stateless service that receives
messages by webhook, checks the sender against the administrator's identity, parses the
fixed command grammar, submits typed commands, and delivers replies, notifications and
supervisor requests. Telegram is the first transport because the allowlist, the command set
and the operator's habits already exist; the transport is a separate layer so another can be
added.

With the supervisor of section 12.3, the chat channel carries three kinds of message:
notifications, questions that wait for an answer (approvals and risky supervisor actions,
answerable in chat), and the daily digest.

### 13.3 An existing cluster and its Argo CD

**Assumption.** The repository shows one cluster: the staging cluster that hosts falcone,
musematic and llmwiki, with an Argo CD instance in the `argocd` namespace. This section
assumes that is the cluster meant. If it is another, the isolation measures below can be
relaxed.

**What sharing the cluster gains:**

- Observer and release identities become service accounts bound to the existing
  least-privilege roles in `config/kubernetes/`. The kubeconfig files, and the open findings
  about them, go away.
- No second cluster to run.

**What it requires**, because agent sandboxes execute project code next to the products
they deliver to:

| Measure | Purpose |
| --- | --- |
| Separate namespaces for the control plane and for sandboxes | Different privileges and quotas |
| Default-deny network policy on sandboxes: no route to product namespaces, the cluster API or the control-plane database; outbound only to the evidence service, the provider endpoints and package registries | A compromised sandbox reaches nothing |
| No service-account token mounted in sandboxes; restricted pod security | No cluster identity inside a sandbox |
| Resource quotas, limits and a lower priority class for sandboxes | Product workloads are never starved; a maker run today needs up to 6–8 GB |
| A dedicated node pool with taints, if the cluster can add one | Keeps untrusted code off the nodes that run products |
| A sandboxed container runtime, if the cluster offers one | A second boundary below the pod |

**Argo CD.** Using the existing instance is reasonable, with two conditions:

- A dedicated Argo CD project for Engineering Control, restricted to its own namespaces and
  its deployment repository.
- The platform's own Argo CD account, which it uses to sync product applications, must not
  be able to change the platform's application. Otherwise an agent-driven change could
  alter the system that supervises it.

**Facts about the cluster needed before step 3**, which the repository does not contain:

- Kubernetes distribution and version; whether the network plug-in enforces network
  policies.
- Free capacity, and whether nodes can be added or a pool dedicated.
- Storage classes for persistent volumes; what is available for object storage.
- Ingress controller and certificate management.
- Whether a sandboxed runtime class and user namespaces are available (the loopback shell
  that today protects agent credentials relies on bubblewrap, which needs them).
- The secret operator in use (the product manifests reference external-secrets).

### 13.4 The name

"Engineering Control" replaces "Hermes" wherever a person reads it; identifiers that code
reads back stay as they are, for the reasons in section 5.4.

| Renamed | Kept |
| --- | --- |
| Sign-in page, console, notifications and chat messages | Labels `hermes:risk-r1`, `hermes:change-request` |
| Titles and bodies of new issues and pull requests the platform opens | The CI-baseline branch pattern and marker file |
| Prompt text that names the platform | HTML markers in existing issue bodies |
| Documentation, comments, unit descriptions | Namespace strings behind deterministic identifiers and approval bindings |
| Environment variable names, with the old names accepted during the transition | Event types and sources already stored in the database |

One detail: the backlog import skips the platform's own operational issues by title prefix.
New titles need new prefixes added to that list, and the old prefixes must stay, or existing
operational issues would be imported as work.

The console already carries the new name; the sign-in page still shows the old one.

### 13.5 Full restructuring, with existing workflows imported

Every one of the 33 existing workflows has a destination. Those that represent work become
**built-in flows**, visible and editable in the designer. Those that are plumbing become
part of the engine, the scheduler or the platform's maintenance jobs.

| Existing workflows | Destination |
| --- | --- |
| Existing-backlog loop, issue delivery, staging GitOps delivery, delivery reconciliation, final closure | Flow **Deliver issue** |
| Preview deployment, staging deployment, runtime verification | Sub-flow **Release and verify**, using the project's release strategy |
| Recovery | Flow **Recover deployment** |
| GitHub backlog import | Producer **Import backlog** |
| Audit campaign, finding verification | Flow **Audit code** |
| Multi-project audit, autonomous backlog campaign | The scheduler (fan-out across projects is its job, not a flow's) |
| Enhancement discovery, enhancement delivery | Flow **Discover enhancements** |
| Issue solution loop, enhancement solution loop | Retired: their items go through **Deliver issue** |
| Change request | Flow **Change request** |
| UI migration plan, UI blueprint fan-out, UI gap consolidation | Flow **Plan UI migration** |
| Backlog decomposition plan and fan-out | Flow **Decompose backlog** |
| Issue amend observation, publication heads observation | Event triggers and engine internals |
| Cleanup, quota refresh, connections refresh | Platform maintenance jobs, not editable |
| LLMWiki staging bootstrap | An action of the llmwiki project plug-in |
| Acceptance lifecycle, both observability acceptance workflows | A synthetic canary flow and test fixtures |

**What "imported" has to mean:**

- **Same behaviour first.** Each built-in flow starts as a faithful translation: the same
  stages, gates, budgets and prompts. Prompts are extracted from the Python that assembles
  them into templates word for word. Improvements come afterwards, as new versions.
- **Proven on recorded work.** Old and new are run on the same past items and compared on
  gate results, corrections and outcome. This is the evaluation lab of section 11.4, which
  therefore moves earlier in the plan: it is the acceptance test for the import.
- **Existing records carried over.** Backlog campaigns, issue records, change sets,
  findings, enhancements and approvals are migrated into the work-item model so history and
  open items are not lost. Approvals bound to digests stay valid because their bindings are
  not recomputed.
- **Project settings carried over.** The six project manifests become project records and
  flow parameters.
- **Running work drained.** Executions in flight finish on the old workers; nothing is
  converted mid-run.

### 13.6 Consolidated plan

| Step | Scope | Engineer-weeks |
| --- | --- | --- |
| 1 | Continuous integration; image builds | 2 |
| 2 | Containers; paths and hosts moved to configuration | 2–3 |
| 3 | Control plane on the existing cluster through Argo CD: namespaces, policies, database, Temporal with TLS, secrets, object storage | 4–5 |
| 4 | Access: one administrator, a credential per channel, GitHub App; console commands and decision inbox; chat adapter; rename to Engineering Control; **retire Hermes** | 5–6 |
| 5 | Sandbox per mission: launcher, workspace, bundle handoff | 6–8 |
| 6 | Isolation hardening; account pool with leasing, health and login from the inbox | 4–6 |
| 7 | Work-item model and scheduler; migration of existing records | 5–7 |
| 8 | Flow interpreter, module catalogue, prompt templates, budgets; **Deliver issue** imported; evaluation lab used as its acceptance test | 12–15 |
| 9 | Projects, providers, models and roles as data; plug-in points | 3–4 |
| 10 | Remaining flows imported: audit, enhancements, change request, UI planning, decomposition, recovery; legacy loops retired | 4–5 |
| 11 | Supervisor: detectors, rules, agent on a subscription account, automatic and waiting actions | 7–10 |
| 12 | Composable flows and the visual designer with run view; event triggers | 13–17 |
| 13 | State: status from Temporal, outbox projector, references instead of payloads; remove reconciliation code | 3–4 |
| 14 | Monitoring, runbooks, cut-over, old hosts retired | 3–4 |
| | **Total** | **about 73–96** |

About eleven months for a team of three, allowing for coordination. Steps 11 and 12 can run
in parallel with 9–10 once step 8 is complete.

Milestones:

- **After step 6:** running on the cluster with isolated sandboxes and the account pool;
  Hermes gone; workflows unchanged.
- **After step 8:** issue resolution on the new engine, with editable prompts.
- **After step 10:** every existing workflow imported; legacy code removed.
- **After step 12:** flows designed visually; supervisor operating.

### 13.7 What is still unknown

No decisions remain open. Three facts do, and each can change the plan:

1. **Subscription terms and the number of accounts** (13.1). This sets the throughput the
   whole programme can reach.
2. **The cluster's properties** (13.3), especially capacity, network policy enforcement and
   whether a sandboxed runtime is available.
3. **Driving the clients' login from the console** (13.1). If it cannot be done, logging an
   account in remains a command-line task for the administrator.

## Appendix A. Documentation drift found during the audit

| Document says | Code has |
| --- | --- |
| 31 workflows, about 220 step names, about 55 patch markers (architecture course, lecture 3) | 33, 297, 73 |
| "there are 8" patch forks (`tests/test_workflow_replay.py` docstring) | 73 |
| At most two implementations per project (`docs/ISSUE-DELIVERY.md`) | Default 3, range 1–4 (`config.py`) |
| Children start with `start_child_workflow` (`docs/design/new-214-pipelined-delivery.md`) | Never called; the back slot is an asyncio task |
| About 75 API routes (architecture course, lecture 8) | 99 |
| 59 command entries (`docs/telegram/commands.md`) | 60 in `commands.yaml`; line references into the hook are stale |
| Hermes 0.20.0 at `/home/hermes/.hermes` (`docs/HERMES-TELEGRAM.md`) | Other documents say 0.21.0 under `/opt/hermes-agent/releases/` |
| Projects falcone, musematic, llmwiki (Telegram hook) | Six projects in `projects.py` |

## Appendix B. Where the evidence is

| Topic | Files |
| --- | --- |
| Workflow classes, routing, patches | `src/engineering_control/workflows/definitions.py`, `worker_registry.py`, `activities/core.py` |
| Controller activities | `activities/metadata.py`, `activities/engineering.py` |
| Scheduling limits | `leases/component.py`, `config.py`, `config/projects/*.yaml`, `quota/broker.py`, `policy/rotation.py` |
| Project coupling | `projects.py`, `tests/test_project_registry.py`, `identity/`, `adapters/{falcone,musematic,llmwiki}/` |
| Reconciliation | `operations/run_reconciliation*.py`, `recurring_reconciliation.py`, `reconciliation_state.py`, `db/run_binding.py`, `db/start_proof.py` |
| API identity | `api/auth.py`, `api/main.py`, `engineering/backlog.py`, `cli.py` |
| Hermes integration | `integrations/hermes-engineering-control/`, `notifier.py`, `docs/architecture-course/functional-overview/02-hermes-and-temporal-together.md` |
| Credentials | `config/credentials/contracts.yaml`, `config/policies/credentials.yaml`, `docs/SECURITY.md`, `scripts/bootstrap-*.sh` |
| Recorded incidents and open findings | `.go-status/findings.json`, `.go-status/notes/`, `docs/BACKLOG.md`, `docs/GO-STATUS.md` |
