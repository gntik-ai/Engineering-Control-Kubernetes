# Workflow execution and application projections

`GET /v1/runs/{workflow_id}` and `engineering-ctl status`, `diagnose`, and `run`
retain the existing top-level Temporal `status`, `workflow_id`, and `run_id`.
Temporal `COMPLETED` means orchestration ended, not that the requested business
operation succeeded. For example, cleanup can finish with an application blocker.

The additive `application` object exposes bounded persisted metadata:

- `persisted_status` and `error_code`: application status and bounded code,
  not arbitrary summaries, paths, or workflow results.
- `updated_at`, `started_at`, `finished_at`: original UTC projection timestamps.
  `observed_at` is when this read occurred, not fresh evidence of progress.
- `run_projection`: existing terminal, durable-wait, orphaned-start-intent,
  or requires-Temporal-correlation classification.
- `source=APPLICATION_RUN_PROJECTION`, `scope=WORKFLOW_ID_ONLY` identify the source.
- `run_association=UNVERIFIED`: one application row exists, but Run does not bind
  it to a Temporal run ID. `business_status` therefore remains null, including
  when `persisted_status=SUCCESS`. Application UUIDs and timestamps do not prove
  retry or Continue-As-New lineage.
- `NONE`: no row; `AMBIGUOUS`: multiple rows, without choosing the latest;
  `UNAVAILABLE`: bounded lookup or projection failed. None implies success.

Older terminal projections remain historical facts, not current-run acceptance.
Unknown statuses become null; malformed error codes become `REDACTED_ERROR_CODE`,
never rewritten into plausible codes. PostgreSQL reads use a read-only transaction
and a three-second statement timeout. The application lookup has a five-second
response deadline; this does not terminate an already running database thread,
which remains subject to connection/pool/SQL bounds.

No repair, retry, or deletion occurs through these diagnostics. Current-run
reconciliation still requires separately bound Temporal/application evidence;
this display change neither repairs historical rows nor proves successful cleanup.
