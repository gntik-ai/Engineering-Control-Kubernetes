# Upgrade

1. Inventory versions, active workflows, leases, incidents, disk, and current Git commits.
2. Back up non-secret configuration plus PostgreSQL; record SHA256 and test readability.
3. Build/test new `engineering-control` and agent-pack commits without changing active symlinks.
4. Pull new pinned image digests and CLI/scanner checksums.
5. Stop schedulers, drain role workers, and let in-flight activities heartbeat or cancel safely.
6. Run database migrations once; never edit a released migration. Migration `0005` adds nullable
   artifact purge tombstones and is backward-compatible with readers that ignore those columns.
7. Switch `/opt/engineering-control/source`, restart services, and run verification.
8. Roll back code/image symlinks on failure. Restore PostgreSQL only when a migration is not
   backward-compatible and after stopping all writers.

Component guidance:

- Hermes: back up config, update the one canonical checkout/package, reapply no vendor patches,
  verify the user plugin/hook, then restart the gateway.
- Temporal/PostgreSQL: read upstream release notes, take database backup, update exact image
  digests, run supported schema updates, test namespace/workflow resume, then retain old images.
- Codex/Claude: install exact npm versions, authenticate per worker identity, run structured smoke
  output, and keep paid API variables absent.
- Playwright/scanners: update package/release checksum locks, reinstall browsers/dependencies, and
  run synthetic evidence checks.
- Agent pack: edit neutral catalogs, regenerate all providers, validate, review generated diff.
- Project adapters: validate against repository and dry-run output before enabling mutation.
