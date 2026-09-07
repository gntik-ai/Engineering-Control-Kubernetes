# Dispatch receipts and later workflow state

`OperationsRepository.finish` finalizes an application **dispatch attempt**. It
does not report the eventual business result of a Temporal workflow. It returns
`True` only when it records the first dispatch result on an unlinked PENDING or
DISPATCHING operation. The dispatch receipt, its dashboard event and any start
notification intent now commit in one caller-owned database transaction.

The repository checks the current row after taking its database row lock. If a
workflow/run receipt, terminal timestamp, or later status already exists, it
returns `False` without changing that row. A delayed dispatch failure must not
erase a known execution or release its active ownership. A delayed start reply
must not reopen terminal history, demote RUNNING to STARTED, replace an execution
with another workflow, or roll a recorded run lineage backward. Repeated terminal
replies preserve the first timestamp and error.

The typed command bus and the approved direct-delivery writer share the same
transactional receipt function. It locks and refreshes the operation before
checking whether a first receipt can still be recorded. Suppressed stale replies
do not emit false started, completed, failed or blocked events. An insertion
failure rolls back the receipt and both projections; a lost application reply
after commit can be retried without creating another local notification intent.

A first recovered receipt for an existing workflow produces `operation.linked`
and a notification with phase `LINKED`, not a claim that new work started. It uses
the existing `telegram.engineering-operation-started` route; gateway rendering
and actual delivery require separate runtime acceptance. Other first starts use
phase `QUEUED`. A workflow ID is required for STARTED; an empty run ID remains
supported for existing signal acknowledgements, which are not completed effects.

This guard neither reconciles workflows nor verifies Continue-As-New lineage.
Those paths must use authoritative histories and may legitimately advance a run
ID. It does not repair historical rows or supply a fencing token for an execution
that has no durable receipt. It does not make an external action exactly-once.
This atomic local transaction does not span Temporal or Telegram. A failure
after Temporal starts but before this transaction commits still needs recovery
against the same canonical execution. Delivery acknowledgements can be lost
after Telegram accepts a message; one local outbox row is not proof of exactly
one external notification. Historical receipt-only rows are not backfilled or
rewritten automatically, and other operation-state writers need their own
reviewed state-transition and lineage contracts.

The generic issue loop still requires real component ownership, child approval
handoff, durable result recording and finalization. This shared receipt guard is
a prerequisite, not an alternative plan-only implementation or full lifecycle
acceptance. Installation, live queue/provider acceptance and project-specific
authorization remain separate. No database migration is needed for this delta.
