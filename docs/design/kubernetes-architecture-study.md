# Architecture study: Engineering Control on Kubernetes

Status: proposal for discussion. Nothing in this document is implemented.

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

- **Typed stages in Python, not a general workflow language.** A mission definition is a
  short module, validated at registration. The aim is to remove duplication, not to build a
  configurable engine.
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
| The mission engine becomes a second framework | Typed stages only; no user-editable definitions; review each new stage against "could two missions use it" |
| Removing Hermes leaves no command channel | Step 4 lands console commands, CLI credentials and the chat adapter together, before the gateway is stopped |
| Renaming breaks deduplication and bindings | Machine-read identifiers are frozen (section 5.4) |
| Test suite depends on host facilities | Split in step 1; retire host-model tests as their code is removed |

## 10. Decisions needed

1. **Chat channel.** Keep Telegram through a first-party adapter (recommended), or rely on
   the console and CLI only.
2. **Provider credentials.** Subscription seats behind a broker, or API keys. Unchanged from
   the first study and still the gating decision for scale.
3. **Cluster.** A dedicated cluster for the platform (recommended), or a dedicated node pool
   in the existing staging cluster.
4. **Name.** Keep "Hermes" as the product name, or rename the human-facing text.
5. **Scope.** Stop after step 6 (Kubernetes with today's workflows) or continue to the full
   restructuring.

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
