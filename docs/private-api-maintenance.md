# Private API admission during maintenance

`ENGINEERING_API_ADMISSION_MODE=synthetic-only` is a startup-only service setting.
The default is `normal` for existing installations; absent configuration is NOT
evidence of a maintenance fence. Invalid mode values prevent settings validation.

In synthetic-only mode the private API does not start its pending-operation
dispatcher. Authenticated unsafe requests receive HTTP503 before body parsing or
application dispatch, except these existing typed POST routes:

- `/v1/validation/lifecycle`
- `/v1/validation/observability`
- `/v1/validation/observability/reverse`

The three routes retain bearer authentication, bounded schemas and the independent
`ENGINEERING_ACCEPTANCE_ENABLED` requirement. They do not acquire product GitHub,
deployment, alternate-provider or approval authority. Unknown writes, real issue
starts, typed commands, control signals, approvals, imports and reconciliation
POSTs remain blocked. GET/HEAD diagnostics retain their existing authorization.
WebSocket admission is denied. No HTTP header, query, body or API action can enable
normal admission. The authenticated `GET /v1/admission` reports the running API's
startup mode and whether its pending-operation dispatcher task is running.

This is only the private API boundary. A cutover must separately fence the public
dashboard (which owns a command bus), gateway/GitHub ingress, persisted Temporal
schedules and scheduler startup. It must prove a current post-fence drain and
exclude other writers. It does not cancel or repair pending operations, workflows,
leases or history. Pending work remains durable for later authorized recovery.

The privileged cutover must bind this exact setting and source release to its
approved manifest, keep the setting reboot-persistent, verify it in the loaded
process through `/v1/admission`, and refuse acceptance if it reads `normal` or a
running dispatcher. Releasing the fence requires a separately authorized service
configuration/start transition after applicable live gates; deleting a temporary
start-condition file must not also remove this admission setting. An active API
or HTTP200 health response alone does not establish that mutation admission is
fenced. This document does not authorize deployment or declare a complete cutover.
