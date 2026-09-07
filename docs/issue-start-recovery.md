# Canonical issue start uncertainty and recovery

The originating `solve_issue` operation is committed before dispatch. Its stored
command, approval binding and operation ID determine one canonical Temporal
workflow ID. A timeout after submission is not evidence that no execution exists.

Ambiguous canonical issue starts retain DISPATCHING active ownership, with an
explicit `ISSUE_START_UNCONFIRMED` safe error and one warning event. They do not
emit a successful-start notification. A later generic failure cannot erase this
state and admit a replacement issue operation. Pre-submission validation remains
blocking and current approval is still required to start work.

The existing dispatcher observes at most one candidate per five seconds, using
keyset traversal. Explicitly unknown starts and receipt-less DISPATCHING issues
older than sixty seconds are eligible. Observation describes the canonical
workflow with a five-second RPC timeout and verifies its ID, workflow type and
nonempty UUID run identity. It does not start, signal or cancel any workflow.
Missing/expired/unavailable or mismatched history retains unknown ownership; it
is never proof that external work did not happen. An invalid stored binding is
logged by operation ID, without preventing traversal to the next record.

Read-only observation does not need a new approval of an edited issue body. The
stored attempted scope is checked again under a row lock before atomically
recording the recovered receipt, linked event and notification intent. This does
not approve further activities or external effects; their current binding checks
remain mandatory. Existing operation state is not rewritten if another writer
has already recorded its receipt or terminal result.

Ordinary iteration failures no longer kill the dispatcher permanently. They emit
a bounded safe error-type event and wait before retrying; cancellation still
propagates. A repeated database failure can therefore remain degraded, not green.

Limits: this is not a Temporal start-intent outbox for every workflow family.
It does not automatically retry an absent execution, resolve expired history,
verify original history input bytes or Continue-As-New lineage, clear leases,
backfill historical terminal rows, or make external effects exactly-once. The
canonical ID/type observation is not full input-lineage attestation. A database
failure after a direct request obtains an acknowledgement may still return an
ordinary unavailable response before later observation links its receipt.
Real process-loss/Temporal, all-channel operator rendering, full assembled
release and live recovery acceptance remain separate. Schema0015 is unchanged.
