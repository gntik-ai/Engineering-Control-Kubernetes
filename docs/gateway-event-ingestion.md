# Gateway telemetry retry identity

Revision0017 adds one nullable `dashboard_events.gateway_ingestion_key` column
with a unique index. Existing events keep NULL, unchanged IDs, timestamps,
metadata and content. No history is deleted, deduplicated, backfilled or declared
to be an authentic delivery acknowledgement by this migration.

New correlated events use a key derived from the authenticated gateway subject,
bot/profile/update correlation and event type. The canonical bounded input is
hash-bound to that identity. Repeating the same event returns its existing event
ID without changing either its observation or first-received timestamp.
Conflicting content under the same identity returns HTTP409 without revealing
the stored content. Distinct event stages retain distinct event IDs. The unique
index, not an application check-then-insert, serializes concurrent ingestion;
only that specific unique violation is handled as a possible duplicate.

An optional timezone-aware `event_observed_at` retains actual occurrence time
across delayed delivery. Times more than five seconds ahead of the API clock
are denied; UTC-equivalent representations have one canonical binding. Historical
and newly uncorrelated legacy inputs are not assigned an invented identity.
They remain explicitly `legacy-unattested`. A delivery-success event still
requires complete update/correlation and actual transport acknowledgement IDs.

This consumer component does not persist a producer outbox or guarantee delivery.
The gateway producer must record observation time once and retry the same
immutable payload; generating a new observation time on retry is a conflict.
Missing legacy observation time falls back to ingestion time and is not proof
of when a delayed external effect actually occurred. Producer recovery,
correlated connection projections and exclusion of the proven historical test
events remain mandatory before live telemetry acceptance.

Receipt identity currently lives with its event row. Event retention must not
delete these receipts while replay is possible; a future compaction policy must
preserve appropriate tombstones or enforce an evidence-bound replay boundary.
No retention deletion is introduced by this change. Restoring an older database
also requires ingress fencing and reconciliation, not blind replay.

The migration applies only0016→0017, with PostgreSQL lock/statement timeouts of
5/30seconds. It refuses an incompatible existing column/index. Downgrade refuses
to discard any non-NULL ingestion key. With no new receipts, a tested downgrade
preserves historical rows and the0015 ownership/0016 action-receipt contracts.
After new writes use forward repair; rollback must not reopen an older gateway
or reinstate an unaccepted acknowledgement producer. This is prepared source,
not approval to run a migration against a live database.

Regression acceptance uses both SQLite and PostgreSQL17.11, plus the exact
predecessor's migration chain in a separate interpreter. These are disposable
network-isolated fixtures, not a live database server upgrade or production
acceptance. The full assembled runtime, backup and cutover gates still apply.
