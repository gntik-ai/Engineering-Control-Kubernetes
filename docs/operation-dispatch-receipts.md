# Dispatch receipts and later workflow state

`OperationsRepository.finish` finalizes an application **dispatch attempt**. It
does not report the eventual business result of a Temporal workflow. It returns
`True` only when it records the first dispatch result on an unlinked PENDING or
DISPATCHING operation. Existing callers may still ignore this return value, but
event-producing callers must check it.

The repository checks the current row after taking its database row lock. If a
workflow/run receipt, terminal timestamp, or later status already exists, it
returns `False` without changing that row. A delayed dispatch failure must not
erase a known execution or release its active ownership. A delayed start reply
must not reopen terminal history, demote RUNNING to STARTED, replace an execution
with another workflow, or roll a recorded run lineage backward. Repeated terminal
replies preserve the first timestamp and error.

The typed command bus emits dispatch events and start notifications only for an
applied result. Suppressed stale replies do not emit false started, completed,
failed or blocked events. Ordinary first outcomes still produce their existing
events. A workflow ID is required for STARTED; an empty run ID remains supported
for existing signal acknowledgements, which are not claims of completed effect.

This guard neither reconciles workflows nor verifies Continue-As-New lineage.
Those paths must use authoritative histories and may legitimately advance a run
ID. It does not repair historical rows or supply a fencing token for an execution
that has no durable receipt. It does not make an external action exactly-once.
The interval between dispatch-result persistence and event/outbox persistence
still requires separate crash-recovery acceptance.

The generic issue loop still requires real component ownership, child approval
handoff, durable result recording and finalization. This shared receipt guard is
a prerequisite, not an alternative plan-only implementation or full lifecycle
acceptance. Installation, live queue/provider acceptance and project-specific
authorization remain separate. No database migration is needed for this delta.
