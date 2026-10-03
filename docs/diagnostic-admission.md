# Operational acceptance without product execution

`ENGINEERING_API_ADMISSION_MODE=diagnostics-only` is a startup-bound transition
mode, not production acceptance. It leaves existing product operations pending
and does not run issue-start recovery. Default `normal` behavior remains unchanged;
`synthetic-only` retains its original stricter boundary.

Diagnostics mode admits existing authenticated reads and synthetic validation,
the strict `hermes-gateway` event-ingestion endpoint, and only the typed
`refresh_connections` command. No request can change the mode. Diagnostic bodies
are authenticated before reading, bounded to 16 KiB/two seconds, and validated
again by the existing routes. Product, approval and workflow-control writes remain
denied. The dispatcher filters diagnostic records and repeats the action check
before execution, including mismatched stored action/payload denial.

The same existing connection projector refreshes bounded fixed probes every two
minutes and processes queued web diagnostics. It cannot start a project workflow.
Native authentication snapshots remain refreshed by the separate existing Temporal
schedule. Provider connection views read the latest durable quota observation,
preserve last successful and failed authentication independently, and never infer
known subscription capacity from a successful login check.

The usage API retains `raw_status` as historical evidence and marks observations
older than 300 seconds, missing timestamps, or more than five seconds in the
future as stale. Authentication and unknown subscription capacity are separate.
Probe failures preserve the last successful observation; delayed results do not
replace a newer connection state.

Diagnostic refresh has a 35-second cancellation deadline, including lock waiting.
HTTP probes close after headers without consuming response bodies; TLS is an
owned asynchronous connection. Diagnostic SQL uses transaction-local statement
and lock limits of at most five seconds, shortened by the remaining deadline.
Cancellation joins in-flight synchronous SQL before releasing refresh ownership,
so the deadline is not a promise of instantaneous thread termination. Transport/
host-failure drain and the overall service-stop bound require separate recovery
acceptance; this source test does not establish those live guarantees. No database
role, server configuration or persistent connection setting is changed.

The console shows **DIAGNOSTICS ONLY**, not a full operational or project-ready
verdict. Shared-password reauthentication, CSRF, confirmation bindings and durable
operation receipts remain required for web actions. Existing product records are
not deleted or relabelled as completed. Gateway event ingestion is not proof of
Telegram receipt unless a genuine correlated user command and delivered reply
have actually been observed.

Before changing the live mode, bind the exact release/configuration and eligible
pending diagnostic rows to reviewed maintenance, preserve recovery protection, and
verify product operations remain fenced. Restore `normal` only after its mandatory
execution, approval, recovery and per-project gates pass. Do not use this mode as a
permanent display-only substitute for the required full console.
