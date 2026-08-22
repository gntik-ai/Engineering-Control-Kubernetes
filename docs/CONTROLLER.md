# Controller

The controller runs as normal Unix identity `engineering-control`; `hermes` remains the Telegram
gateway identity. Root is used only by bootstrap to create users, protected directories, units, and
machine-local secrets.

Components:

- PostgreSQL 17.11 on loopback port 55432;
- Temporal 1.31.2 on loopback and private `10.10.0.2:7233`;
- Temporal UI 2.53.3 on loopback port 8741;
- Engineering API on loopback port 8740;
- upload-only evidence ingest on private `10.10.0.2:8750`;
- orchestration worker, scheduler, notifier, and migrations under systemd;
- Hermes user plugin and gateway hook under `/home/hermes/.hermes`.

Install only after reviewing host inventory and the cleanup checkpoint:

```bash
cd /home/hermes/engineering/engineering-control
sudo ./scripts/bootstrap-controller.sh
./scripts/verify-controller.sh
```

Configuration lives under `/etc/engineering-control`; PostgreSQL state under
`/var/lib/engineering-control/postgresql`; evidence and mirrors under `/srv/engineering`. The API
token is group-readable only by `engineering-api-clients`. The database password is not readable by
Hermes. Evidence ingest uses identity-bound tokens under
`/etc/engineering-control/env/evidence-uploaders/`; possessing one grants no control API access.
Compose ports are never published on all interfaces.

The bootstrap installs a commit-addressed release under `/opt/engineering-control/releases` and
updates a symlink. It does not touch product repositories, Kubernetes, or existing Hermes service
state. Restart Hermes separately after its plugin/hook and allowlist are verified.
