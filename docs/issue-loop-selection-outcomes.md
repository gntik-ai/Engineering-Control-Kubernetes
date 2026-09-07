# Issue-loop selection outcomes

The generic IssueSolutionLoopWorkflow must not turn an unavailable or invalid
selection into successful work. New executions record the Temporal patch marker
`issue-loop-selection-outcomes-v1` after loading the selection. BLOCKED and FAILED
selections return that business outcome, without finalization or a child start.
Only bounded uppercase blocker codes are forwarded; invalid values use a fixed
fallback. Unknown status or malformed selected-item structure returns FAILED.
A valid selection with no eligible findings returns EMPTY/NO_ELIGIBLE_FINDINGS.

These are terminal business outcomes, not a claim that a paused live workflow can
be resumed. Temporal orchestration completion is distinct from behavior success.
Valid SELECTED results and existing synthetic PASS selections continue through
the existing bounded child path. No provider, lease or delivery success is inferred
from selection acceptance.

The versioned branch preserves old histories without the patch marker, including
their historical false-success result. Replaying such a history does not correct
or approve it. Operators still need authoritative history/projection reconciliation;
this source change does not rewrite old rows or histories.

This is a partial fix, not acceptance of the generic loop lifecycle. Durable loop
leases and record handlers, approved issue/child custody, final external effects,
full assembled/live and cross-channel acceptance remain required. Product writes
and deployment are not authorized by these fixtures.
