# Approved publication fixture contract

The positive publication tests exercise the existing application authority, not
an injected successful authorization result. They create an inert issue revision
before approval, use the real operation creation/claim and backlog approval
methods, register the child run/approval, acquire component and worktree leases,
and ask the metadata handler to persist a publication intent. The worker uses
the actual typed publication client, authenticated ASGI claim/receipt endpoints,
repository transactions and typed GitHub broker. Only Git/GitHub commands are
recorded; no network GitHub write or real project identity is involved.

The paired case asserts two exact-SHA pushes, two draft PRs, four current issue
observations and four durable completed effect receipts. Repeating it must not
repeat recorded writes. The source-only case publishes only the source repository,
records two effects and binds the unchanged deployment repository as
AWAITING_IMMUTABLE_ARTIFACT. The metadata test repeats preparation and verifies the
same durable intent, without executing an effect.

Seven worker denial cases retain a valid approved fixture and independently vary
the intent presence, durable/remote issue revision, workflow run, component lease,
operation state or prepared plan. They require denial before any recorded effect.
Temporary worker tokens are fixture-only and removed; database engines close.
Settings are reconstructed with normal validation, not unvalidated model_copy
updates that can replace typed secrets with plain strings.

Run these tests only inside the established unprivileged, no-live-egress harness.
No host HOME, auth/config directories or sockets may be mounted. SQLite/ASGI tests
are not PostgreSQL concurrency or HTTP transport acceptance; the matching isolated
PostgreSQL scenarios provide separate evidence for paired and source-only paths.
An in-process ASGI client does not prove real worker routing or authentication.

This follow-up changes tests and documentation, not application code, schema,
Agent Pack or installed services. The parent source's direct adapter still needs
assembled/live acceptance. Unlinked-Finding import, IssueSolutionLoop approval
propagation, truthful durable loop controls, terminal/Continue-As-New handling,
unknown-effect reconciliation and final staging/closure behavior remain open.
In particular, a generic loop handler returning a PASS envelope is not proof of
lease acquisition, persistence or completion. Do not use these tests to approve
product writes, a pilot, or production readiness.
