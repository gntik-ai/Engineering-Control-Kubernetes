# Final closure workflow

## Problem

An existing-backlog issue must not be closed from the delivery workflow's pre-merge runtime
evidence. Protected source and deployment merges change the immutable revisions, so closure needs a
separate durable workflow that rebuilds and verifies the exact merged source, promotes that artifact,
deploys the exact merged deployment revision to staging, and only then comments on and closes the
issue.

## Contract

- `FinalClosureWorkflow` is registered before an existing-backlog parent may start it.
- The workflow accepts only the immutable backlog campaign, ChangeSet, finding, project, repository,
  and issue identities already persisted by the parent.
- `final.prepare` revalidates the current unexpired full-delivery campaign approval, repository pair,
  protected-merge and issue-close permissions, provider separation, documentation result, open issue,
  resumable ChangeSet state, and the exact current R3 approval when the issue is R3.
- The exact source PR head is protected-merged first. The merge SHA and tree are persisted before any
  subsequent action.
- The workflow waits at most 120 one-minute attempts for a signed candidate manifest produced from
  that exact source merge SHA. Exhaustion returns `FINAL_BUILD_WAIT_EXHAUSTED` and performs no
  deployment merge or issue close.
- Immutable promotion and opposite-provider release review bind the deployment PR head. Only that
  reviewed head may be protected-merged.
- Both local worktrees must be regular in-root directories, clean before synchronization, and clean
  at the exact merge SHAs and trees afterward.
- A staging deployment and post-staging runtime verification must persist for the exact final
  deployment SHA. No active agent execution may remain and both worktree leases must be accounted for.
- A bounded redacted evidence comment is persisted before the broker may close the issue. The issue
  close is idempotent and the terminal database transition occurs only after GitHub confirms closure.
- All remote mutations use typed broker methods and the existing repository allowlist. No shell or
  arbitrary GitHub command surface is added.
- Stored plans and evidence are immutable. Any changed binding, revision, approval, provider,
  repository, artifact, deployment, verification, or issue state fails closed.

## Acceptance

- An expired or changed campaign approval, missing current R3 approval, non-resumable ChangeSet,
  changed PR head, unsigned or wrong-source artifact, mismatched deployment revision, active agent,
  incomplete worktree lease set, dirty or escaped worktree, or absent runtime verification prevents
  issue closure.
- Source merge evidence is persisted before build polling; artifact evidence before promotion;
  reviewed deployment head before deployment merge; deployment merge before staging; final runtime
  evidence before comment; and persisted comment evidence before issue close.
- Re-entry after each irreversible boundary validates the stored plan and resumes from the first
  incomplete idempotent operation. Re-entry from a stored final issue plan performs only the bounded
  comment/persist/close/persist sequence.
- A completed issue returns `FIXED` without replaying remote operations.
- Tests exercise negative gates directly as well as Temporal ordering, bounded polling, cancellation,
  and resume behavior.

## Upgrade and rollback

The change is additive and uses existing JSON/state columns; no database downgrade is required.
Upgrade registers `FinalClosureWorkflow` and the final activity steps before any parent release can
start the child. Existing workflows continue on their recorded Temporal patch branch.

Rollback must first stop creation of new final-closure children and wait for or explicitly cancel
every in-flight final-closure workflow. Do not unregister the workflow while an execution can still
replay. A partially completed closure is recovered by state:

- Before source merge: the previous release can continue the legacy blocked handoff.
- Source merged, deployment unmerged: leave the issue open and preserve the final-closure evidence;
  reinstall the closure-capable release to resume exact-SHA build and promotion.
- Deployment merged, issue open: leave both merges and the issue state unchanged; reinstall the
  closure-capable release to repeat staging/runtime verification and close only after the gates pass.
- Evidence comment present, issue open: the idempotency marker permits safe comment adoption and
  close resumption.
- Issue closed: preserve the terminal ChangeSet, issue, run, evidence, and worktree-cleanup metadata.

Never attempt to undo protected merges automatically. Product rollback is a new reviewed change
through the normal source/deployment workflow. Runtime rollback must use the previously verified
immutable deployment revision and must not delete campaign, ChangeSet, workflow, or evidence data.
