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
- Global registration requires current, unexpired approvals for all three inventories and requires
  the selected policy campaign to be the active owner.
- A global child issue reports a blocker to the parent without changing the inventory campaign state
  or deactivating the shared policy.
- Global `COMPLETE`, `STOPPED`, and `CANCELLED` are the only paths that terminate the bound inventory
  rows and enqueue the one typed policy-deactivation request.
- The root reconciler accepts `STOPPED` as a terminal deactivation state and continues to enforce the
  exact repository allowlist, actor, binding, owner role, and secret-free request schema.

## Acceptance

- The first exact approval queues one root policy request.
- A second exact inventory approval under the active owner succeeds without writing a request.
- Nested API approval evidence is accepted by the global command capability and Temporal activity
  only when its binding, heads, dependency plan, delivery mode, risk, actor, and expiry remain exact.
- A blocked global child returns `BLOCKED` immediately and preserves its inventory approval.
- Global terminal handling emits one bounded disable request with no repository mutation payload.
