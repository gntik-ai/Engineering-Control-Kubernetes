# Global backlog policy ownership

## Problem

The global backlog requires one immutable approval for each project inventory and one shared,
root-installed GitHub write policy for the five allowlisted repositories. Treating every inventory
approval as a policy-owner claim made the valid three-inventory state impossible. A blocked child
issue could also pause or terminate its inventory row and request policy deactivation, preventing the
global selector from continuing with an independent issue.

## Contract

- Exactly one approved backlog campaign has `policy_role=owner` and owns activation of the shared
  five-repository policy.
- Additional exact R2 inventory approvals use `policy_role=inventory`, persist their immutable
  operator evidence, and never enqueue a second privileged policy mutation.
- A pending owner activation blocks additional inventory approval until the root reconciler has
  committed the policy and advanced the owner to `APPROVED`.
- Owner expiry invalidates every dependent inventory approval before the shared policy is disabled;
  a replacement owner and each inventory must receive new exact approvals.
- Global registration requires current, unexpired approvals for all three inventories and requires
  the selected policy campaign to be the active owner.
- A global child issue reports a blocker to the parent without changing the inventory campaign state
  or deactivating the shared policy.
- Global `COMPLETE`, `STOPPED`, and `CANCELLED` are the only paths that terminate the bound inventory
  rows and enqueue the one typed policy-deactivation request.
- The root reconciler accepts `STOPPED` as a terminal deactivation state and continues to enforce the
  exact repository allowlist, actor, binding, owner role, and secret-free request schema.

## Per-project owners (NEW-163)

- Outside an open autonomous global backlog, ownership is decided per project: at most one owner
  per project, a further approval of that project is an inventory, and an approval of a project
  without an owner becomes its owner beside the owners of other projects.
- At most `backlog_parallel_project_limit` projects (Settings, default 2, minimum 1) hold an owner
  at once; an approval beyond it is refused, naming the limit and the owning projects. The root
  reconciler applies the same limit (same control.env) and refuses an enable beside an
  `APPROVED`/`RUNNING`/`PAUSED` campaign of the same project.
- A limit of 1, or an open autonomous global backlog run (active, `PAUSED` or `BLOCKED`), applies
  the single global owner contract above unchanged.
- The global backlog is exclusive: its capability and registration also require that no campaign
  outside its own inventories owns the policy.
- The single pending activation slot, the disable rule (the shared policy stays enabled while any
  verified owner remains) and the policy file shape are unchanged. Delivery writes are also bound
  to their project's repository pair (`REPOSITORY_OUTSIDE_PROJECT`).

## Acceptance

- The first exact approval queues one root policy request.
- A second exact inventory approval under the active owner succeeds without writing a request.
- Nested API approval evidence is accepted by the global command capability and Temporal activity
  only when its binding, heads, dependency plan, delivery mode, risk, actor, and expiry remain exact.
- A blocked global child returns `BLOCKED` immediately and preserves its inventory approval.
- Global terminal handling emits one bounded disable request with no repository mutation payload.

## Upgrade and rollback

This change is schema-neutral: `policy_role` is stored inside the existing bounded approval JSON.
Pre-upgrade approvals without that key are treated as owner claims. More than one legacy/explicit
owner fails closed and requires the operator to reconcile and re-approve the inventory before global
registration so that exactly one row is the owner and every additional row is an inventory.

Rollback is allowed only after the root reconciler has disabled the shared GitHub write policy and
the global Temporal workflow is terminal. Convert any remaining `APPROVED_INVENTORY` rows back to
`AWAITING_APPROVAL`; do not delete their approval or inventory evidence. A previous release may then
activate at most one newly approved inventory. No database downgrade or operational-data deletion is
required.
