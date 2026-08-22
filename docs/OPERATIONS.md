# Operations

Check controller:

```bash
sudo systemctl status engineering-infrastructure engineering-migrate engineering-api \
  engineering-evidence-ingest engineering-temporal-worker engineering-scheduler \
  engineering-notifier
sudo journalctl -u engineering-api -n 100 --no-pager
sudo journalctl -u engineering-evidence-ingest -n 100 --no-pager
docker compose --env-file /etc/engineering-control/env/infrastructure.env \
  -f /opt/engineering-control/infrastructure/compose.yaml ps
```

Check worker:

```bash
ssh worker 'systemctl status engineering-worker@codex engineering-worker@claude \
  engineering-worker@browser engineering-worker@scan engineering-release-worker'
ssh worker 'journalctl -u engineering-worker@codex -n 100 --no-pager'
```

All logs are structured and redacted. Run context fields are campaign ID, ChangeSet ID, run ID,
project, role, provider, and worker. Metrics are available only on the private API listener under
`/internal/metrics` and cover campaign/run counts, provider limits, workers, deployments,
verification, and evidence storage as implementation handlers publish them.

Back up PostgreSQL and `/etc/engineering-control` before upgrades. Do not back up provider OAuth or
SSH private keys into general platform archives. Temporal and PostgreSQL must be healthy before
starting API/workers. The notifier uses an outbox so an unavailable sink does not lose state.

Cleanup remains dry-run until expiry policy, active leases, dirty-tree evidence, incidents, and
retention holds are all checked. Never delete active/dirty worktrees or incident/security/release
evidence.
