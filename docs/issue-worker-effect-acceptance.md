# Issue-delivery worker effect boundary

This source checkpoint is part of HERMES-2026-014. It is not installed or accepted
for external writes. Platform production authorization does not authorize a
Falcone, Musematic, or LLMWiki production target or a project pilot.

## Confirmed final-worker gap

An isolated PostgreSQL 17.11/schema 0015 fixture registered a real durable issue
approval, loaded its child ChangeSet, and used the actual metadata handler to
prepare a paired publication plan. After the fixture changed the issue revision,
metadata correctly denied renewed preparation. Passing the already-prepared plan
to the actual DeliveryActivityHandler and typed GhCliBroker nevertheless reached
the recording command runner's push and PR-creation boundaries. The worker did
not obtain a fresh issue observation or revalidate the durable approval there.

Only the command runner was a recording sink: it executed no subprocesses or
GitHub requests. This is an isolated reproduction, not a live unauthorized write.
The test distinguishes successful diagnostic execution from FAILED security
acceptance. Repository write allowlisting is necessary but is not approval
freshness, active-owner fencing, or issue-specific authority.

The final-worker security gate remains OPEN. The required correction must cover
the actual worker/broker path, not merely move another check earlier in metadata.
It must bind the exact action, issue repository/number, approved revision, current
durable approval, ChangeSet, workflow/run lineage, and source/deployment artifact.
Fresh typed GitHub observations must be checked at the effect boundary. Missing,
expired, revoked, changed, wrong-target, unavailable-authority, and stale-owner
states must stop the effect. A copied payload boolean or digest is not enough.

Worker identities currently use scoped evidence-ingest credentials rather than a
new general API credential. Any implementation must preserve that separation and
must not copy operator or database credentials into workers. Reusing a transport
does not by itself authorize expanding the roles allowed on it. Existing lease,
operation, and activity ownership must be inspected before choosing the smallest
durable authorization mechanism. Unknown in-flight effects must be reconciled,
not automatically replayed after expiry. GitHub and the application database do
not share a transaction; freshness checks alone are not exactly-once semantics.

Required regression paths include drift after preparation, drift between paired
effects, revocation, wrong workflow/ChangeSet/artifact, duplicate activity delivery,
lost acknowledgement, stopped/stale owner, authority service failure, and bounded
recovery. Test positive accepted work as well as denials. This document does not
declare those tests complete or approve new credentials, schema, or endpoints.

## Paired-title correction

The same fixture exposed a second concrete defect: metadata produces the fixed
` (deployment)` suffix, but the broker's general value guard rejected parentheses.
This could block the deployment PR after both pushes and the source PR had already
occurred. The follow-up adds a PR-title-only validator that accepts parentheses
within the existing bounded ASCII display-field policy. Branches, queries, routes,
issue titles, write enablement, repositories, and authority bindings are unchanged.
This is not general Unicode title support and does not relax shell/control-input
denial. Titles remain a single argument in a non-shell command invocation.

The isolated paired pipeline now reaches two recorded pushes and two PR creates.
The stale-approval scenario still does so, and remains FAILED security acceptance;
the title correction is not a remedy for the final-worker authority defect.

Expanded regression exposed six old SQLite tests with missing schema or dangling
Finding references. Their fixtures now create the required non-backlog metadata,
without mocking the authority validator or changing the original assertions.
The production guards are unchanged. Real PostgreSQL coverage remains separate
from these SQLite unit tests.
