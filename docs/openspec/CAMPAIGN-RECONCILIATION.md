# Campaign Reconciliation Contract

## Scope

`POST /v1/campaigns/reconcile` is an authenticated, bounded control-plane operation. It compares
persisted campaign projections with Temporal visibility and may repair only a terminal projection
whose existing immutable workflow binding still matches. It never deletes campaigns, workflows,
runs, ChangeSets, or evidence.

## Safety contract

- The request accepts `limit=1..200` and `offset=0..10000`.
- The service loads at most `limit + 1` persisted candidates and describes every distinct bound
  workflow before the first projection write.
- A Temporal `NOT_FOUND` response classifies only the affected record as
  `TEMPORAL_WORKFLOW_MISSING`.
- Any other Temporal visibility failure returns a safe HTTP 503 before any projection write.
- A workflow ID bound to more than one candidate is `INCONSISTENT_STATE`; neither projection is
  repaired, including when Temporal reports the workflow terminal.
- A concurrent workflow rebind rejected by the repository is `INCONSISTENT_STATE` and is not
  repaired.
- Terminal repair is idempotent and retains all operational data.

## Duplicate-canary identity

A `DUPLICATE_CANARY` requires at least two distinct Temporal workflows that are currently
`RUNNING` or `CONTINUED_AS_NEW`, have non-terminal/non-waiting projections, and share the same
normalized `(project, campaign name, run type)` identity. Campaign names are the persisted
operator-defined identity; workflow IDs hold per-run uniqueness. Shared projection rows for one
workflow are inconsistent state and do not contribute to duplicate counts.

Duplicate classification is explicitly page-bounded. The response includes
`classification_scope.kind=BOUNDED_PAGE`, the page offset and candidate count, and the identity
fields. Operators must follow `next_offset` when `has_more=true`; reconciliation never deletes a
record automatically.

## Response classifications

The operation may emit `QUEUED_VALID`, `RUNNING_HEALTHY`, `EXTERNAL_WAIT`, `AWAITING_HUMAN`,
`STALE_QUEUE`, `STALE_ACTIVITY`, `WORKER_POLLER_MISSING`, `ORPHANED_CAMPAIGN`,
`TEMPORAL_WORKFLOW_MISSING`, `TERMINAL_NOT_RECONCILED`, `DUPLICATE_CANARY`, or
`INCONSISTENT_STATE`. A repaired item sets `projection_repaired=true`; all other classified items
with an observed workflow set it to false.

## Acceptance and rollback

Tests must prove terminal repair precedence, same-project/name/type duplicate scoping, project and
name isolation, missing-versus-unavailable Temporal behavior, shared terminal binding denial,
shared-row count exclusion, concurrent rebind denial, bounded paging, and zero automatic deletion.
Rollback is code-only: reinstall the prior exact commit. Reconciliation schema and migration state
are unchanged, and already-correct terminal projections remain valid.
