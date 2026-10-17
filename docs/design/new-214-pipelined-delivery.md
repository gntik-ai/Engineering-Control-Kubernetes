# NEW-214: pipelined, parallel and batched delivery in one campaign

Approved 2026-10-17: three stages, three reviewed PRs. Merges and deploys are always serialized. With concurrency 1 and no batching, behaviour is unchanged: each stage sits behind its own `workflow.patched` flag and its own approval or config field.

## Facts the design relies on
- **Loop state.** `_run_backlog_loop` is sequential; helpers share loop-wide `_phase`, `_retry_step`, `_skip_reason` and `_paused`. Retry/skip carry `repo#issue` (service.py:598).
- **Merge binding.** The merge is bound to the published head: gitops.py:2637 and 2972, with `prepare` rebuilding the plan from `ChangeSet.source_head_sha` (:864). Heads are verified under publication authority (delivery.py:1069, publication_authority.py:82). GitHub's merge precondition covers only the head, so freshness at merge time needs branch protection.
- **Repairs.** Only CI-regression blockers are repairable (issue_delivery_approval.py:521); repairs resume the original base (metadata.py:2776).
- **Write policy.** A denied write is retried every 5 min (definitions.py:372). A PAUSED campaign invalidates its approval (backlog_authority.py:150).
- **Leases.** Default 2 implementation leases per project (leases/component.py:87).
- **gntik-ui** is `merge_only`, with no staging deploy: issues close at merge.

## Prerequisite for concurrency of 2 or more (operator)
Each merge target repository needs strict "require up-to-date" branch protection, or a GitHub merge queue, so a merge on a moved base is refused atomically. Hermes checks this at `backlog-start` and refuses concurrency above 1 without it.

## Stage 1a (PR 1): merge-forward
- **When.** In BACK, when an open PR (source or deployment) is BEHIND. A MERGED PR is never advanced (delivery.py:1009).
- **Effect.** GitOps orchestrates a new publication kind, `merge-forward`, under Run, component and worktree ownership that BACK acquires itself. The delivery worker builds a deterministic M = merge(V, base_sha) in an isolated worktree and pushes it as a fast-forward.
- **Provenance.** An append-only `ChangeSet.merge_forwards` list records (V, base_sha, M, tree_sha), plus `head_origin`. The head becomes M in the same transaction. A lost receipt is recovered by recomputing M against the remote head. The worker branch is synced, and repair and restore accept M records. The merge requires green CI on M, provenance and an up-to-date base. V's checker evidence is never relabelled as validating M.
- **Base movement.** If the base moves again, merge-forward runs again. After 2 rounds the item waits with `PR_BASE_MOVING`.
- **Conflict.** A conflict becomes the blocker `*_PR_MERGE_FORWARD_CONFLICT`. Its evidence is the conflicting paths. The blocker joins both repair allowlists and is charged to the deployment correction limit. The repair worktree is V plus `git merge origin/<base>`, with the conflicts left for the maker, then the checker. The result is a new V′.

## Stage 1b (PR 1): one FRONT plus one BACK
- **Phases.** FRONT is the gate, revalidation and IssueDelivery, ending at a published PR. BACK is merge-forward, CI or repair, merge, staging and closure.
- **Coordinator.** One coordinator owns per-slot state: item, phase, child handle and blocker. Children start with `start_child_workflow` and are awaited with `workflow.wait`. Helpers write only slot state. Only the coordinator writes the loop and Run status, notices, terminal states, and lease and write-policy releases.
- **Admission.** B is the next `planned_order` item whose dependencies are all delivered (SKIPPED doesn't count). Its components must not overlap anything in flight. One lease of project capacity always stays reserved for BACK. The coordinator owns reservations and refill (`_refill_free_slot`).
- **Slot-level pauses.** The campaign never becomes PAUSED while another slot is active, so approval stays valid. A blocker pauses only its slot: the loop stays RUNNING, and the Run records `paused_slot` (`repo#issue`, blocker). The retry and skip preflight accepts a paused slot with an exact `repo#issue` match. Campaign PAUSED is recorded only once no slot has an active child.
- **Controls.** stop and pause-after-current carry every started FRONT through BACK, so no PR is left without custody. `cancel` cancels both handles, awaits their cleanup, then releases. A NEW-162 skip closes only its slot.

## Stage 2 (PR 2): N = 1..3
- **Binding.** `maximum_concurrency` is part of the approval binding (`backlog-start --concurrency N`, shown by approval-candidate). FRONT slots = min(N−1, project lease capacity − 1, free worker slots for the maker role); a FRONT also needs provider quota above its floor. Waiting does no provider work.
- **Selection.** The Stage 1b admission rule, applied to every slot.
- **Merge queue.** FIFO by checker-pass time, then `planned_order`.
- **Conflicts.** A conflicting head gives up BACK, which is released before the next ready PR takes it, and repairs in a FRONT slot, or waits for one.

## Stage 3 (PR 3): batched staging
- **Scope.** Projects with a real staging deploy (`gitops.flow` deploy, i.e. falcone first), with concurrency 2 or more and `delivery.batched_staging: {enabled, max_batch, window_minutes}`. gntik-ui first needs a staging contract (decision 6), so at most it batches merges.
- **Batch.** Up to `max_batch` CI-green, up-to-date PRs that become ready within the window are merged in queue order. Each merge is followed by merge-forward and CI of the next PR, and a failing PR leaves the batch for repair. Then one deploy runs.
- **Identity.** A durable `BatchDeployment` record holds batch_id, its member ChangeSets (each with its own approval and provider lineage), the merge shas and the deployed artifact (commit and image digests). `RuntimeVerificationInput` gains `change_set_id`, and its binding requires that the artifact contains the ChangeSet's merge. Results are keyed by (deployment_id, change_set_id). Any redeploy invalidates earlier results, and an issue closes only when it is verified on the final deployment.
- **Failure.** A failed check marks issue k as affected, not as the culprit. The oracle is each member's own RuntimeVerification plus a deploy health check. Hermes falls back to serial attribution:
  1. One revert PR restores the batch's known-good base B0, which is redeployed and must verify.
  2. Members are re-applied one at a time, in queue order, as reapply PRs, each deployed and verified.
  3. When member j fails, Hermes counts it as reproduced only if it fails twice.
  4. Hermes then checks B0 plus j alone, without the earlier members: if j fails reproducibly there, j is the culprit and goes to correction with fresh custody and a new PR from main. If B0 plus j alone passes, the cause is an interaction, so Hermes pauses without blame.
  5. The remaining members continue serially.

  This takes at most 2·`max_batch` deploys. It fails closed: an unreproducible failure, an infrastructure error, a revert conflict, a non-revertible change (for example a schema migration, for which batching is off), a failed B0 or exhausted budgets pause the batch for the operator. No member is reported verified, and no correction budget is charged without reproduced evidence.
- **Authority.** The new publication kinds `batch-revert` and `batch-reapply` need `revert-pr-create` in the approved operations, and are fenced and reconciled like merge-forward. One budget per batch; no RecoveryWorkflow. Merged source branches are never advanced.
- **Replay.** `workflow.patched("existing-backlog-batched-staging-v1")`; global children stay at 1.

## Operator decisions
1. Enable strict up-to-date protection or a merge queue on each repository: required for N of 2 or more.
2. The N cap per project (proposal: 2 for falcone, 3 for gntik-ui).
3. Provider maker concurrency and quota floors.
4. Implementation lease capacity per project and worker slots per role.
5. The batch size and window (proposal ≤3, ≤10 min).
6. A gntik-ui docs-site staging contract (Pages deploy of a commit plus per-issue checks), or merge-only.

## Sizes
1a ~700 lines plus a migration; 1b ~600; Stage 2 ~300; Stage 3 ~900.
