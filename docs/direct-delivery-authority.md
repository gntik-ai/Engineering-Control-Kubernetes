# Direct delivery admission and durable acknowledgement

This source-only follow-up addresses part of HERMES-2026-014. It does not authorize
installation, project writes, a staging pilot, or production GO.

`POST /v1/deliveries` keeps the existing finding/risk/dry-run interface. A real
(`dry_run=false`) request additionally requires `approved_issue`, containing the
existing typed `ExistingBacklogLoopStart` request for an already-approved,
claimed single-issue operation. Do not manufacture an operation or approval row
to satisfy this input. Use the platform's existing typed approval path.

The controller verifies the current operation/issue binding and the exact
confirmed Finding-to-issue relationship before dispatch. The requested mode is
`source-pr`, preserving the original endpoint's PR-stage scope; full staging
delivery uses the existing full-delivery solve-issue path. A dry run cannot carry
the mutating request, and legacy dry-run workflow payloads remain unchanged.

Real direct requests reuse the canonical operation-derived backlog workflow ID.
They do not start a second standalone delivery Run. The canonical parent registers
the delivery child and its durable approval using the existing typed handler.
An approved RUNNING campaign is not permission to start again: only the exact
existing workflow and expected type can be linked. A missing or unrelated
execution fails closed. REJECT_DUPLICATE remains the Temporal start policy.

Before returning acceptance, the direct adapter records the workflow ID and real
Temporal run UUID on the original operation, under a row lock and with approval
revalidation. It preserves its existing start timestamp. A receipt failure is not
reported as successful acceptance. A retry describes the same canonical execution
and retries the durable acknowledgement; it does not invent another operation.
STARTED describes dispatch acknowledgement, not terminal workflow success.

## Evidence boundaries and remaining work

- Unit/API and PostgreSQL fixtures cover valid admission, denial, duplicate
  linkage and start-receipt loss before and after commit. No fixture has product
  credentials or a route to live infrastructure.
- The isolated Temporal scenario uses the real service, parent/child workflows,
  six actual metadata-handler steps and history replay. It stops before lease
  acquisition, providers or GitHub. It is not a full delivery acceptance.
- This adapter does not yet provide an approved importer for an unlinked Finding.
  It requires an existing approved issue operation. It does not repair
  IssueSolutionLoopWorkflow's missing child-approval propagation.
- Post-terminal acknowledgement/reconciliation, Continue-As-New lineage, the
  full background command dispatcher, lease/cancellation recovery, final external
  effects, console/Telegram consistency and matching assembled/live acceptance
  retain their existing required gates. No unknown effect may be manually cleared
  on the strength of these tests.
- The previous three positive publication-fixture failures must be retained and
  resolved with legitimate originating authority, not deleted or bypassed.

Schema0015 and Agent Pack are unchanged. This is not a replacement for the
separately prepared memory-release artifact or its exact change-control binding.
