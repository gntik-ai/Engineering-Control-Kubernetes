# Upgrade

1. Inventory versions, active workflows, leases, incidents, disk, and current Git commits.
2. Back up non-secret configuration plus PostgreSQL; record SHA256 and test readability.
3. Build/test new `engineering-control` and agent-pack commits without changing active symlinks.
4. Pull new pinned image digests and CLI/scanner checksums.
5. Stop schedulers, drain role workers, and let in-flight activities heartbeat or cancel safely.
   Worker entry points translate systemd's `SIGTERM` into a bounded 30-second
   `Worker.shutdown()` before exiting. `engineering-temporal-worker.service`,
   `engineering-acceptance-worker.service`, `engineering-github-worker.service`, and
   `engineering-worker@.service` specify `TimeoutStopSec=60`; the release worker specifies 90.
   Verify those installed values and the default systemd control-group kill behavior before each
   maintenance window. This leaves at least 30 seconds after the application shutdown deadline, so
   native CLI activity cancellation has time to terminate its owned process group before systemd
   can escalate. Temporal retries the activity on another worker; heartbeat details from the lost
   attempt are available if the implementation supports resumption. A worker that exits while an
   owned child remains alive fails the maintenance gate even if a later idempotent retry succeeds.
   Verify one audit-owned in-flight retry against an idempotent controlled sink before restarting
   shared workers. Repeated termination signals, including interactive `SIGINT`, do not shorten the
   30-second application bound; use the unit's normal timeout rather than manually sending
   `SIGKILL` during a drain. The deadline bounds the shutdown request, not non-preemptible thread
   work; an exceeded deadline is a failed maintenance gate and must retain its journal evidence.
6. Run database migrations once; never edit a released migration. Migration `0005` adds nullable
   artifact purge tombstones. Migration `0006` adds nullable native-provider presence,
   authentication, and status fields to quota snapshots. Migration `0007` adds nullable immutable
   agent-pack revision, instruction-bundle hash/source, and effective-tool provenance to agent
   invocations. Migration `0014` adds durable dashboard session revocations. All are
   backward-compatible with readers that ignore those columns, but code that enforces durable
   logout requires `0014` to be present before its dashboard/API processes start.
7. Switch `/opt/engineering-control/source`, restart services, and run verification.
8. Roll back code/image symlinks on failure. Restore PostgreSQL only when a migration is not
   backward-compatible and after stopping all writers.

## Migration 0014 order and rollback

The deployment order is mandatory: take and verify a restorable PostgreSQL backup, drain the
affected dashboard/API processes, apply migration `0014`, verify the table contract and both
indexes, then start the candidate processes. Starting candidate code before `0014` fails closed and
can make every authenticated page and metric probe unavailable; it is not a supported rolling
compatibility window.

If migration `0014` finds only part of the expected table, it stops with `revision 0014 found a
partial bootstrap schema`. Keep application processes stopped, retain the backup and schema
inspection evidence, and obtain an approved database repair or restore plan. Do not drop, rename,
or recreate the partial table ad hoc, because that can resurrect logged-out sessions or discard
forensic data.

Code-only rollback to a release that does not consult `dashboard_session_revocations` is unsafe
while `0014` and its rows remain: previously logged-out cookies could become valid again. Before
starting old code, either run the reviewed `0014` downgrade (which increments the global
`password_version` before dropping the table) or rotate/reset the dashboard password to increment
that version and invalidate every outstanding cookie. Verify old-cookie denial after rollback.
The supported runtime database is PostgreSQL. SQLite is used only by isolated tests; any other
dialect is rejected at repository startup rather than silently weakening revocation semantics.
The versioned `.github/workflows/postgresql-acceptance.yml` job uses a digest-pinned PostgreSQL 17
service and runs the migration-reader concurrency, rollback-invalidation, and re-upgrade gate on
pull requests and `main` pushes. The gate asserts the PostgreSQL 17 server version and exercises
the PostgreSQL conflict-upsert session revocation path, timezone-aware expiry metrics, and
concurrent idempotent cleanup finalization. Local execution must set
`ENGINEERING_TEST_POSTGRES_URL` to an audit-owned loopback PostgreSQL database; the test refuses
non-PostgreSQL URLs. The target must be a new disposable database because the gate deliberately
seeds fixed singleton and count-bound fixtures; remove the audit-owned database or container after
each run instead of reusing it. A skipped local test is not equivalent to the required
real-PostgreSQL acceptance result.

Component guidance:

- Hermes: back up config, update the one canonical checkout/package, reapply no vendor patches,
  verify the user plugin/hook, run `verify-no-omniroute.sh --scope controller`, then restart the
  gateway. The validator must pass before a reconfiguration or upgrade is accepted.
- Temporal/PostgreSQL: read upstream release notes, take database backup, update exact image
  digests, run supported schema updates, test namespace/workflow resume, then retain old images.
- Codex/Claude: install exact npm versions, authenticate per worker identity, run structured smoke
  output, and keep paid API variables absent.
- Playwright/scanners: update package/release checksum locks, build the root-owned versioned
  browser runtime, atomically switch `/opt/engineering-browser/runtime`, reinstall browser
  binaries/dependencies where required, and run synthetic evidence checks under `browser-runner`.
- Agent pack: edit neutral catalogs, regenerate all providers, validate, review generated diff,
  deploy its commit-addressed release on both hosts, and verify invocation provenance.
- Project adapters: validate against repository and dry-run output before enabling mutation.

Required controller/worker negative gate after every upgrade:

```bash
sudo /opt/engineering-control/source/scripts/verify-no-omniroute.sh --scope controller
ssh -t worker 'sudo /opt/engineering-control/source/scripts/verify-no-omniroute.sh --scope worker'
```

The gate excludes backups, evidence, reports, quarantine, Git history, and historical cleanup
scripts. It fails on active configuration, units, processes, containers, packages, executables,
schedules, listeners, worker environments, or active residual installation paths.
