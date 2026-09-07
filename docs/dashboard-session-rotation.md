# Dashboard session rotation

The console remains a single-password operator interface. Reauthentication is
not MFA and session correlation does not identify an individual human.

Successful password login issues a fresh signed cookie/CSRF value and durably
revokes a previously presented, valid signed cookie. Independent sessions are
not revoked. A login with a previously revoked cookie can recover using the
correct current password; no password reset is needed after a lost response.

For typed action confirmation, password verification is followed by one database
transaction: check the current session/password version/expiry; lock and recheck
the exact unused, unexpired candidate and its current capability/snapshot; move
that candidate and its single-use reauthentication binding to a new session;
revoke the old session. The candidate ID, payload, target, binding hash and expiry
are not extended or replaced. Reauthentication does not create an operation.
An action token lasts at most five minutes and never beyond the candidate expiry.
The response exposes its actual UTC expiry and conservatively rounded-down
remaining seconds, rather than reporting a fixed five minutes.

PostgreSQL mutating session paths acquire the authentication singleton lock
before candidate/binding locks. Logout and password change/reset share that
ordering. Web action admission repeats session authorization under the same
lock, after HTTP authentication, and commits the operation before releasing it.
Every newly admitted candidate repeats its current capability and immutable
snapshot check at execution, not only during reauthentication. An existing
operation can be returned without authorizing another effect. The former
nonrotating reauthentication entry point has been removed; action admission
requires explicit session authorization, including repository-level callers.
An action committed before a concurrent logout remains a durable accepted action;
logout is not cancellation. A logout committed first denies stale admission.
Lock waits are bounded to five seconds and statements to ten seconds on the
transactional session paths. SQLite fixtures do not establish PostgreSQL locking.

The browser adopts the returned CSRF value before executing the confirmed action
and updates hidden form fields. An older tab may retain stale form/approval data:
reload and prepare the action again rather than automatically replaying it.
The browser clears the password input even on errors. A lost rotation response
leaves the old cookie revoked and no operation created; sign in again and prepare
a new candidate. Pending candidates from the old session do not silently migrate.

HTTP and SSE authorization use the shared revocation store. Already-open streams
retain the existing maximum five-second revocation contract and may reconnect
with the browser's new session cookie. Revoked-cookie reconnects remain denied.
Authorization storage failure fails closed; no cookie is returned on an uncertain
login rotation, and partial reauthentication writes roll back together.

Existing cleanup discovers/counts expired session revocations, deletes at most
10,000 expired rows per enabled metadata-cleanup execution, recounts the remainder,
and persists a deduplicated alert when the limit is exceeded. Active revocations
are preserved. Deletion requires enabled cleanup policy plus explicit dry_run=false;
this session change neither enables live cleanup nor authorizes retention deletion.
Recurring live cleanup/alert acceptance remains tracked under HERMES-2026-017.

This source change uses the existing schema0015 tables. Source fixture acceptance
is not installed/live acceptance, full browser action coverage or production GO.
