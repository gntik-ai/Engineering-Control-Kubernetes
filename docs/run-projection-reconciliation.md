# Durable Run Execution Binding & Recurring Reconciliation

## Overview

This document describes the durable binding between engineering application runs and their execution backend (Temporal-first), and the recurring projection-reconciliation subsystem that detects and repairs projection drift without executing product-level side effects.

Introduced by the H001/H002 follow-up derived from commit `c8965d8`. Exact release, approval and deployment evidence is tracked in the protected findings-closure directory outside this repository. No historical bindings are assumed or guessed; only the exact migration path `0017 -> 0018` is documented.

## Schema: Migration 0018

Migration `0018` follows `0017` and:

- Adds nullable `runs.temporal_first_execution_run_id`.
- Replaces a known PostgreSQL projection trigger (guarded) to suppress false-progress signals caused by binding-only or no-op writes.
- Preserves existing owner/ACL grants.
- Denies migration if unknown trigger drift is detected (fails closed rather than silently overwriting).

**Operational constraints:**

- No "stamp" or "upgrade to latest" shortcuts are supported; the migration chain must be applied exactly `0017 -> 0018`.
- Once bindings are populated, downgrading is prevented. Do not automatically downgrade after binding writes. Use a reviewed forward repair or a separately validated recovery procedure; this migration does not authorize a destructive restore.
- Password/session history tables are unchanged by this migration.
- **Before any live application:** take a fresh backup and perform a restore rehearsal against a restored copy. Fixture/unit tests do **not** prove migration safety in production topology.

## API Binding Semantics

Eight API start paths participate in write/ACK enrollment for run execution.

- Each run is identified by a UUID `engineering_application_run_id` (memo), with `engineering_binding_version=1`.
- The first history event is immutable and validates: canonical UUID format, event type, and current+first lineage consistency.
- An unconfirmed start returns `503 START_UNCONFIRMED`. Clients **must not** treat this as license to start a replacement run.
- Recovery of missing projections is permitted **only** when a first event can be proven to exist; unknown retention or conflict states remain unresolved (no inference).
- Campaign-to-run transitions use row locks and SERIALIZABLE compare-and-swap. Projection repairs are limited to status/error fields only — the system must never guess `SUCCESS` or `finished_at`, clear leases, or re-execute external effects.

## Reconciliation Settings

All settings share the prefix `ENGINEERING_PROJECTION_RECONCILIATION_`.

| Setting | Default | Bounds |
|---|---|---|
| `ENABLED` | `False` | — |
| `INTERVAL_SECONDS` | 30 | 5–300 |
| `BATCH_SIZE` | 25 | 1–100 |
| `CYCLE_SECONDS` | 30 | 10–120 |
| `MAX_SWEEP_AGE_SECONDS` | 300 | 30–3600 |

The API-owned reconciliation loop is enabled only for normal/diagnostics-only operation — not for maintenance mode or synthetic-only environments.

## Connection & Leadership Requirements

- Session-advisory leadership requires a **direct, session-persistent** PostgreSQL physical connection.
- Transaction-pooling connections (e.g., pgbouncer transaction mode) cannot be assumed compatible and must not be used for leadership.
- The same physical connection must be used for all reconciliation SQL and enrollment operations within a cycle.
- On reconnect failure, the loop fails closed (does not proceed with stale/assumed leadership).
- Cancellation must join outstanding SQL before releasing the advisory lock.

## Cursor & Status Persistence

A fixed `ConnectionSnapshot` persists the reconciler's cursor, counters, and last-full-sweep timestamp.

- Only a **healthy, complete** full sweep updates `last_success`.
- Partial or failed sweeps, or unresolved-item sweeps, never refresh this timestamp.

### Freshness Check (shared by API, Web, watchdog)

```
healthy if:
  age(last_cycle) <= interval + cycle + 15s
  AND age(last_healthy_full_sweep) <= max_sweep_age
```

- A timestamp more than 5 seconds in the future, or in a malformed format, is treated as degraded/stale.
- The CLI is a thin client over the API; it does not compute freshness independently.

### Status Presentation Rules

- Missing `ENABLED` flag renders as `DEGRADED`.
- Explicitly disabled renders as `NOT_CONFIGURED`.
- Raw status and blocker details are always retained and surfaced, not summarized away.
- Unknown recorded start/finish/duration values remain `unknown` — never defaulted to `0` or computed as "growing from now."
- An actual zero-duration interval legitimately displays `0s` (distinct from unknown).
- Closed executions whose business verification remains required are terminal and displayed as blocked; they are not presented as active or successful.

## Operator Guidance

- Operators use this subsystem to **read** current status and evidence, and to inspect immutable lineage — not to arbitrarily restart or "nudge" runs.
- Any activation of reconciliation in a given environment must be bound to an exact release, configuration, and backup/rollback plan.
- Reconciliation is not a cleanup tool and does not execute issues/work items; it carries no product write authority beyond status/error projection repair described above.

## Explicit Non-Claims

- No CLI commands or slash commands beyond what is documented here are implied to exist.
- No ASVS conformance or "full GO" release status is claimed by this document.
- Test evidence referenced during development (1,806 backend tests passing, 2 Temporal-dependent tests skipped in the broad run, 4 PostgreSQL-specific tests; 18 reader-fixture assertions per browser in Chromium and Firefox across desktop and 320-pixel viewports) is **source-level only** and does not by itself constitute release acceptance. Actual Temporal replay evidence is separate and not included here.
- The existing backup worker-mirror is not a high-availability solution.
- No new approvals are claimed by this document.
