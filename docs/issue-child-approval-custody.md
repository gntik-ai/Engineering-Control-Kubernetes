# Issue child approval custody — source follow-up, not release acceptance

This follow-up to bbaddff carries the current originating issue/campaign authority
through backlog child registration, IssueDeliveryInput, the child Run and its
ChangeSet. A deterministic Step stores the approved request, parent and child
workflow IDs, exact imported issue snapshot and campaign approval digest before
child startup. Child loading revalidates those durable records before creating
the ChangeSet. Missing or changed authority is BLOCKED, not successful delivery.

The first Step remains the ChangeSet's originating authority. Bounded correction
children retain separate immutable Step records under the same parent/request/
snapshot scope, with a bounded reference list. They cannot replace the origin,
change parent, exceed the correction count or enter from a non-correction state.
The Finding row is locked before reading the Run/approval identity map, then the
ChangeSet is locked for binding updates. This ordering prevents a waiting load
from validating a newer correction list against its cached, older Run object.
The Finding exists before the first ChangeSet, so it also serializes initial
creation. This records custody; Temporal sequential execution and component/
active-owner leases still provide execution ordering and fencing.

Controller metadata preparation and publication-plan preparation recheck the
stored authority, even if the caller omits the reference. Post-effect persistence
is deliberately not subjected to a new pre-effect approval test: completed effects
must remain visible when their acknowledgement arrives after approval changes.

Eligibility is an internal execution projection, not an issue revision. Successful
revalidation must not invalidate unchanged issue approval when it sets READY.
Body/title/state/risk, the command, campaign/plan and repository heads remain
bound; deferred child execution is denied. Previous source-checkpoint approval
hashes containing eligibility do not silently gain compatibility: any resulting
approval mismatch fails closed. No live approval/data conversion is included.

IssueDeliveryInput.payload() omits the optional approval field when absent so
legacy command payload serialization is preserved. Converter tests cover bytes
and typed decoding; they do not establish complete historical replay compatibility.
Legacy backlog-derived children without the durable reference fail closed and
require explicitly reviewed recovery/adoption, not an inferred approval.

Seven real isolated PostgreSQL 17.11 scenarios now cover simultaneous initial
loads, late registration retry, correction-load lock waits, the final correction
slot race, revision drift during a wait, pre-publication denial and origin
preservation. They supplement SQLite tests, not replace external-effect fencing.
A real isolated Temporal parent/child fixture carries the durable approval through
the actual control service and metadata handlers. Both histories replay; a fixed
fixture boundary stops before leases/providers/GitHub. Business PAUSED/BLOCKED is
not delivery success even though those Temporal executions complete.

Mandatory remaining release work: representative legacy and recovery histories,
alternative global/R3 child paths, final worker/GitHub broker enforcement with
fresh typed GitHub issue observations, check-to-effect/fencing windows, complete
background crash recovery and matching assembled/live acceptance. The scoped
concurrency/dispatch fixtures are not universal concurrency or replay acceptance.
This source follow-up does not close HERMES-2026-014, authorize #1119 or a project
pilot, or establish any production GO.

Hermes platform/console production authority is separate. Falcone, Musematic and
LLMWiki targets remain the verified existing development/staging Kubernetes
environment only; their production deployments remain prohibited.
