# Publication authority: incomplete source follow-up

This branch is not an approved installed release. It adds durable initial-publication
intent/effect records using schema 0015 and two authenticated private ingest routes.
The worker uses its existing delivery/GitHub identity token, not a copied controller
database credential. GitHub commands still pass through the typed broker and policy.

The implemented initial path checks the issue operation/child approval, current
ChangeSet/run and workflow-run binding, component/worktree leases, exact artifact
plan, and a bounded fresh typed issue observation. An admitted effect is persisted
before the worker command. Its receipt survives retries. STARTED/UNKNOWN records
block blind repetition and lease reassignment; expiry is not proof of no effect.

The lease lock is shared with acquisition and both release paths. Time is sampled
after waiting for the Finding/project and current-effect row locks. Malformed grants and lost replies are
reported as unverified, not successful starts or safely unperformed commands.

An expired admission detected before entering the command callback records
`NOT_ATTEMPTED`, not `UNKNOWN`. Only its durable receipt permits another claim;
the current approval, issue revision and leases are validated again. Each new claim
gets a fresh identifier, while the entire previous non-attempt record is retained
as an immutable attempt Step. Exact old receipts replay idempotently; stale or
changed completions cannot acknowledge the new claim. Admission is capped at five
per logical effect independently of workflow/activity retry numbering.

The worker's `publication_effect_not_attempted` describes only the current callback,
not earlier completed effects in a paired publication. If that receipt is lost,
`publication_effect_unknown` additionally signals unverified durable custody:
STARTED remains fenced unless the actual receipt committed. Neither elapsed time
nor worker death is automatically converted to NOT_ATTEMPTED. A complete safe
workflow/operator resumption path is still required; this bounded effect retry
contract alone does not establish it.

The following remain mandatory, not waived:

- Three legacy positive publication unit contracts currently omit the required
  authority input and fail (109 of 112 targeted tests pass). Decide and implement
  their legitimate approved input/caller migration without an unauthenticated
  fallback. A standalone old ChangeSet is not automatically an approved issue.
- Prove real worker/controller HTTP and Temporal routing, all role/context bindings,
  and installed configurations. PostgreSQL plus ASGI fixtures do not prove these.
- Cover promotion, merges, comments, issue closure, alternate/global/R3 and legacy
  authority paths, and correction/Continue-As-New run lineage. This follow-up does
  not claim to guard those paths merely because initial publication is guarded.
- Resolve uncertain effects through bounded authoritative reconciliation. There is
  no automatic resolution here; permanent blocking is not complete recovery.
- Review action ordering and approval/lease drift in the interval after admission.
  A short admission deadline is not an atomic transaction with GitHub or a claim
  of exactly-once external effects. Long-running commands remain subject to the
  required owned-process cancellation/fencing contract.
- Include all affected unit/integration/registration/replay tests, independent
  review, the complete derived artifact and its exact change binding before install.

The same branch contains a separately identifiable policy-writer correction:
`fchmod(fd, 0640)` makes output independent of systemd `UMask=0077`. Current live
failure is a 0600 policy rejected by an exact 0640 validator. The file is disabled
with an empty repository list. The faulty writer mechanism reproduces in isolation;
attribution of the particular historical inode to that invocation is not proven.
No live chmod, policy enablement, row mutation or service restart is performed by
the source correction. Privileged custody/maintenance acceptance is still required.

Hermes platform/console production is distinct from the managed projects: Falcone,
Musematic and LLMWiki remain development/staging-only. No real issue 1119, GitHub
write, build or project deployment was used for this source testing.
