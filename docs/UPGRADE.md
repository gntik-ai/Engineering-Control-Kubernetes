# Upgrade

1. Inventory versions, active workflows, leases, incidents, disk, and current Git commits.
2. Back up non-secret configuration plus PostgreSQL; record SHA256 and test readability.
3. Build/test new `engineering-control` and agent-pack commits without changing active symlinks.
4. Pull new pinned image digests and CLI/scanner checksums.
5. Stop schedulers, drain role workers, and let in-flight activities heartbeat or cancel safely.
6. Run database migrations once; never edit a released migration. Migration `0005` adds nullable
   artifact purge tombstones. Migration `0006` adds nullable native-provider presence,
   authentication, and status fields to quota snapshots. Migration `0007` adds nullable immutable
   agent-pack revision, instruction-bundle hash/source, and effective-tool provenance to agent
   invocations. All are backward-compatible with readers that ignore those columns.
7. Switch `/opt/engineering-control/source`, restart services, and run verification.
8. Roll back code/image symlinks on failure. Restore PostgreSQL only when a migration is not
   backward-compatible and after stopping all writers.

Component guidance:

- Hermes: back up config, update the one canonical checkout/package, reapply no vendor patches,
  verify the user plugin/hook, run `verify-no-omniroute.sh --scope controller`, then restart the
  gateway. The validator must pass before a reconfiguration or upgrade is accepted.
- Temporal/PostgreSQL: read upstream release notes, take database backup, update exact image
  digests, run supported schema updates, test namespace/workflow resume, then retain old images.
- Codex/Claude: install exact npm versions, authenticate per worker identity, run structured smoke
  output, and keep paid API variables absent.
- Playwright/scanners: update package/release checksum locks, reinstall browsers/dependencies, and
  run synthetic evidence checks.
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
