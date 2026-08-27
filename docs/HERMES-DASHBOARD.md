# Hermes Engineering Operations Dashboard

## Architecture

`engineering-dashboard` is a separate FastAPI process bound only to `127.0.0.1:8750`. It uses
server-rendered Jinja2, local CSS and vanilla JavaScript. Caddy is the only public listener and
proxies the `ade.musematic.ai` virtual host to that loopback address. The existing private API at
`127.0.0.1:8740` is never proxied.

The dashboard is observational. It reads existing PostgreSQL solution loops, GitHub backlog
campaigns, ChangeSets, Runs, campaigns, coverage cells and findings. It has no workflow, GitHub,
provider, deployment, shell or Kubernetes mutation route. Migration `0010` adds only:

- the singleton `dashboard_authentication` row, containing an Argon2id hash and password version;
- `dashboard_events`, a bounded redacted activity projection with monotonically increasing IDs;
- fail-open PostgreSQL projection triggers whose insertion failures cannot change workflow writes.

The backlog preserves raw Hermes state while mapping it to `backlog`, `queued`, `preparing`,
`running`, `paused`, `awaiting_approval`, `blocked`, `verifying`, `deploying`, `completed`, `failed`,
`cancelled` or `skipped`. Active items sort first. Audit completion is terminal coverage cells divided
by total cells; a zero-cell campaign reports zero percent. Event streaming uses authenticated SSE,
bounded PostgreSQL reads, `Last-Event-ID`, heartbeats and a 1,000-row browser cap.

## Authentication and password files

There is one password and no username. Sessions are HMAC signed for 12 hours and use Secure,
HttpOnly, SameSite=Strict cookies. The signed value includes `password_version`; a password change
or reset invalidates all older sessions. Login failures are limited in memory per public client IP.
All state-changing forms use CSRF validation.

Initialize only when no authentication row exists:

```bash
sudo /opt/engineering-control/venv/bin/engineering-ctl dashboard init-password \
  --output-file /home/hermes/.config/hermes-dashboard/initial-password.txt
```

Reset explicitly:

```bash
sudo /opt/engineering-control/venv/bin/engineering-ctl dashboard reset-password \
  --output-file /home/hermes/.config/hermes-dashboard/reset-password.txt
```

The CLI reads the root-readable database environment directly, creates the output atomically,
stores only the Argon2id hash in PostgreSQL, changes the final file owner to match its private
`hermes:hermes` parent, sets mode `0600`, and prints only the path. It refuses an existing output
unless `--force-output` is explicitly used. The service cannot access this directory. Delete a
retrieved initial password as `hermes`:

```bash
rm -- /home/hermes/.config/hermes-dashboard/initial-password.txt
```

The unrelated session key is `/etc/engineering-control/dashboard/session.key`, owned
`root:engineering-control`, mode `0640`, in a root-owned mode-`0750` directory. It never contains a
dashboard password.

## DNS and HTTPS

The only DNS mutation is `ade.musematic.ai A 138.199.173.27`, TTL 300. The fixed helper reads
`/home/hermes/.config/hermes-dashboard/hetzner-cloud-api.token` directly as `hermes`, uses Bearer
authentication with the current Hetzner Cloud API, verifies exact zone authority/protection and
conflicts, and writes the pre-change RRSet to:

- `/home/hermes/engineering/HERMES-DASHBOARD-DNS-BEFORE.json`
- `/home/hermes/engineering/HERMES-DASHBOARD-DNS-ROLLBACK.md`

It accepts only `preflight`, `apply`, `verify`, or exact-backup `rollback`; no zone, record, value or
endpoint argument exists. Run DNS operations without sudo. After API, public DNS, certificate and
HTTPS acceptance all pass, remove the local token file and revoke the deployment token in the
Hetzner Console.

The Caddy site source is `caddy/ade.musematic.ai.caddy`. It explicitly rejects `/v1`, proxies only
to `127.0.0.1:8750`, disables SSE buffering, and sends HSTS, nosniff, DENY framing, no-referrer,
restrictive Permissions-Policy and self-only CSP headers. Validate before every reload.

## Service and routine operations

```bash
systemctl status engineering-dashboard.service
journalctl -u engineering-dashboard.service
curl --fail http://127.0.0.1:8750/health/ready
dig +short ade.musematic.ai A
curl -I https://ade.musematic.ai/login
```

The systemd unit runs as `engineering-control:engineering-control`, not `hermes`. It uses a strict
filesystem sandbox, loopback-only network access and explicit inaccessible operator credential
paths. It receives neither the Hetzner token nor product, GitHub, registry, Kubernetes, Codex,
Claude or Telegram credentials.

Dashboard events default to 14-day retention. The existing authorized metadata cleanup activity
counts expired events during discovery and deletes at most 10,000 per execution only after the
normal cleanup authorization and execution switch. Evidence artifacts are not dashboard events and
are unaffected.

## Backup, upgrade and rollback

Before installation, take fresh root-owned runtime and PostgreSQL backups. Install only the exact
committed tree using the existing commit-addressed controller bootstrap. Migration `0010` is
additive; upgrade readers remain compatible with the existing tables.

Normal rollback:

1. Stop and disable `engineering-dashboard.service`.
2. Remove only the independent `ade.musematic.ai` Caddy site and validate/reload Caddy.
3. Switch `/opt/engineering-control/source` to the recorded previous release and restart the
   pre-existing services.
4. Downgrade `0010` to `0009` only after stopping dashboard/event writers and backing up PostgreSQL;
   this removes dashboard password/session state and dashboard events only.
5. Remove the dashboard session-key file and directory only after the service is stopped.
6. If DNS rollback is explicitly requested, run the fixed helper's `rollback` operation as
   `hermes`; it restores the exact pre-change `ade A` RRSet backup and nothing else.

Rollback never deletes campaigns, findings, issue-loop state, audit evidence or product data, and
never changes product repositories, Kubernetes, Helm, Argo or production.
