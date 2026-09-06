# Issue approval and parent-owned execution

These contracts describe the source implementation. They do not establish an
installed release or live delivery acceptance.

Direct Solve-one operations submitted through the typed dashboard, CLI or Telegram
path retain an originating operation ID and an approval hash over the imported
issue revision, repository heads, risk and execution bounds. The active-owner key
does not change when the issue body changes. Old pending payload-only approvals
must be reapproved through the normal path; they are not silently upgraded.

Metadata preparation/approval, workflow handoff and backlog activity guards check
that reference. The issue reference is included in the campaign approval hash and
checked again by the policy-request consumer. A direct operation uses a stable
`backlog-loop-<project>-<campaign UUID>-operation-<operation UUID>` workflow ID
with duplicate reuse rejected. An
existing matching workflow is returned as `EXISTING`, not reported as newly started.
This is not an exactly-once external-effects guarantee or retention/replay proof.

`global_campaign_id` and `r3_approval_binding` are lookup references, not grants.
A global reference must resolve to a current staging campaign with matching
inventory approval and the same currently selected issue. An R3 reference must
resolve to a used, unexpired candidate whose hash, actor, target, imported issue
revision, repository heads and staging scope remain valid.

The durable global parent owns its child execution. A direct private-API request
cannot spawn another child by presenting that parent's approval—even a valid one.
Use the installed typed global campaign controls and the exact R3 approval path;
the parent selects and starts its authorized child. This does not disable the
normal Solve-one or Solve-backlog command paths. It removes a direct API shortcut
that could compete with the existing parent.

Still required before release acceptance: propagate the immutable approval through
child delivery, ChangeSets, publication and the worker's external-effect boundary;
verify fresh typed GitHub observations and check-to-effect windows; exercise actual
Temporal dispatch/replay, background recovery and live interfaces. Unit or fixture
acceptance alone must not unlock issue or deployment admission.

Hermes platform/console production authorization is separate from these technical
gates. Falcone, Musematic and LLMWiki deployment remains development/staging-only.
