# Engineering Control Plane final handoff

Generated and validated on 2026-08-24 UTC. This document is the version-controlled source for
`/home/hermes/engineering/FINAL-HANDOFF.md`. Runtime release identity is the commit-addressed
directory selected by `/opt/engineering-control/source` on both hosts.

## 1. Executive summary

An external, durable Engineering Control Plane now exists for Falcone and Musematic. The controller
hosts the private API, Temporal, PostgreSQL, policy, scheduling, notification, quota metadata,
Hermes integration, and evidence index. The worker hosts six separated execution identities for
Codex, Claude, browser tests, scans, GitHub delivery, and deterministic release operations.

The platform implements bounded audit coverage cells, independent finding verification,
ChangeSets spanning source and deployment repositories, isolated Git worktrees, component leases,
maker/checker separation, deterministic deployment gates, central redaction, durable handoffs,
cleanup policy, metrics, bootstraps, tests, and recovery workflows. No product source was modified,
no GitHub repository was created, and no Kubernetes staging or production mutation was performed.

Codex and Claude are independently authenticated; both structured native calls and both
cross-provider maker/checker directions pass. Private Musematic mirror access, GitHub
delivery/registry credentials, scoped Kubernetes identities, and user-originated Telegram canaries
remain manual blockers. OmniRoute and its Caddy endpoint remain running because cleanup gates C-01
and C-02 are not satisfied.

## 2. Final architecture

Controller:

- Hermes 0.20.0 and the allowlisted Telegram Engineering Control hook;
- authenticated FastAPI Engineering Control API;
- Temporal 1.31.2 and Temporal UI 2.53.3;
- PostgreSQL 17.11 metadata database at migration `0007 (head)`;
- Temporal workflow worker, scheduler, notifier, quota broker, evidence ingest, and synthetic
  acceptance worker;
- separately commit-addressed Engineering Control and validated provider-neutral agent-pack
  releases; each invocation records the exact instruction bundle provenance.

Worker:

- `codex-runner`, `claude-runner`, `browser-runner`, `scan-runner`, `delivery-runner`, and
  `release-runner` Unix identities and systemd services;
- native subscription CLIs, Playwright, deterministic scanners, Git/GitHub tooling, and
  deterministic Helm/Argo/Kubernetes adapters;
- scan-owned canonical mirrors, per-ChangeSet shadow Git repositories, leased worktrees, bounded
  run roots, and identity-specific upload credentials.

Neither Falcone nor Musematic hosts any control-plane component.

## 3. Network diagram

```text
Allowlisted Telegram user
        |
        | Telegram API over outbound TLS
        v
Hermes (controller, no shell/deploy principal)
        |
        | loopback HTTP + bearer token
        v
Engineering API 127.0.0.1:8740 ----> PostgreSQL 127.0.0.1:55432
        |
        +----> Temporal 127.0.0.1 / 10.10.0.2:7233
                         |
                         | private network
                         v
             worker role queues at 10.10.0.3
                         |
                         +----> evidence ingest 10.10.0.2:8750

Temporal UI: 127.0.0.1:8741 only
SSH controller -> worker: private port 22
Provider/GitHub/browser access: worker outbound TLS only
Retained pre-cutover OmniRoute Caddy: public 80/443
Retained OmniRoute service: controller loopback 20128/20131/20132
```

PostgreSQL, the API, metrics, and Temporal UI are not public. Temporal and evidence ingest bind
only loopback/private interfaces. Worker evidence tokens cannot call the control API.

## 4. Removed software

Only approved cleanup item C-03 was executed:

- controller `omniroute-migration-test.service` was disabled, confirmed inactive, backed up, and
  removed from `/etc/systemd/system`;
- rollback material and checksums are under
  `/home/hermes/engineering-backups/20260823T095538Z/`.

C-01 and C-02 were not executed. No repository, worktree, credential, historical evidence,
`.hmw`, legacy workflow asset, update bundle, old backup, `/opt/omniroute`, OpenBao data, or TLS
state was removed.

## 5. Retained software

- canonical Hermes: required Telegram interface;
- OmniRoute service/package/wrapper and `~/.omniroute`: required rollback state until C-01 passes;
- OmniRoute Caddy and TLS state: retained until C-02 consumer/traffic and regression gates pass;
- OpenBao: retained for Falcone secret lifecycle and because its runtime dependencies are not
  safely removable;
- all product repositories, worktrees, OpenSpec assets, `.hmw`, legacy Hermes workflows, update
  bundles, backups, incident evidence, and historical delivery evidence: protected or useful;
- existing kubeconfigs: retained but excluded from automation because the discovered identity is
  overprivileged;
- native controller Codex/Claude installations and auth stores: retained as operator tools and
  never copied to worker identities.

The authoritative reviewed cleanup record is
`/home/hermes/engineering-bootstrap/CLEANUP-PLAN.md`.

## 6. Installed software

Controller infrastructure uses pinned PostgreSQL 17.11, Temporal 1.31.2, and Temporal UI 2.53.3.
Worker versions are Codex 0.149.0, Claude Code 2.1.239, Playwright 1.62.1, Gitleaks 8.30.0,
Trivy 0.74.0, Semgrep 1.174.0, Syft 1.51.0, kubeconform 0.8.0, GitHub CLI 2.98.0,
kubectl 1.36.3, Helm 4.2.3, and Argo CD CLI 3.5.1. Downloaded release tooling is checksum
verified. The Python package is `engineering-control` 0.1.0.

Check exact installed release identities:

```bash
readlink -f /opt/engineering-control/source
ssh worker 'readlink -f /opt/engineering-control/source'
git -C /home/hermes/engineering/engineering-agent-pack rev-parse HEAD
```

## 7. Filesystem structure

```text
/home/hermes/engineering/
  engineering-control/          version-controlled control plane
  engineering-agent-pack/       version-controlled roles, skills, and domain packs
  project-config/               machine-independent project configuration when extended
  docs/                         operator material
  bootstrap/                    bootstrap material

/srv/engineering/
  mirrors/                      scan-owned canonical bare mirrors
  worktrees/<repo>/<id>/        one bounded ChangeSet per worktree
  runs/                         identity-specific run state and handoffs
  evidence/                     content-addressed redacted evidence
  cache/                        shadow Git metadata and bounded caches
  tmp/                          identity-specific temporary roots

/etc/engineering-control/
  control.yaml
  projects.d/
  policies/
  targets/
  runtime/
  systemd/                     installed unit snapshots, checked against active units
  env/                          machine-local protected credentials; never committed

/var/lib/engineering-control/   persistent service state and worker identity homes
/var/log/engineering-control/   reserved; journald is the authoritative log store
/opt/engineering-control/       commit-addressed installed releases and virtual environment
/opt/engineering-agent-pack/    independently commit-addressed validated role/skill assets
```

## 8. Systemd services

Controller services:

```text
engineering-infrastructure
engineering-migrate
engineering-api
engineering-evidence-ingest
engineering-temporal-worker
engineering-scheduler
engineering-notifier
engineering-acceptance-worker
```

Worker services:

```text
engineering-worker@codex
engineering-worker@claude
engineering-worker@browser
engineering-worker@scan
engineering-worker@delivery
engineering-release-worker
```

Operate them with:

```bash
sudo systemctl status engineering-api engineering-temporal-worker engineering-scheduler
sudo systemctl start|stop|restart <controller-service>
sudo journalctl -u <controller-service> -n 100 --no-pager
ssh worker 'systemctl status engineering-worker@codex engineering-release-worker'
ssh -t worker 'sudo systemctl start|stop|restart <worker-service>'
ssh -t worker 'sudo journalctl -u <worker-service> -n 100 --no-pager'
```

Hermes is the lingering user unit `hermes-gateway.service`; production services do not use tmux.

## 9. Hermes

Supported Telegram commands are `/audit`, `/campaigns`, `/findings`, `/verify`, `/run`, `/status`,
`/evidence`, `/approve`, `/reject`, `/pause`, `/resume`, `/cancel`, `/usage`, `/validate`, and
`/rollback`. The hook accepts only Telegram events from numeric IDs in
`/etc/engineering-control/env/telegram-allowlist` and maps them to fixed API routes. It has no
generic shell, SSH, Helm, or kubectl tool.

Final inbound canary from the allowlisted account:

```text
/usage
/validate 30
/status <workflow-id-returned-by-validate>
```

Hermes still contains rollback OmniRoute aliases. Do not remove them until every C-01 gate passes.

## 10. Codex

`codex-runner` is independently subscription-authenticated. Structured native execution,
read-only/workspace-write sandbox selection, schema validation, event normalization, cancellation,
redaction, and usage capture where exposed pass. Native activities emit 30-second keepalives while
the CLI is quiet. Paid API variables and custom gateways are stripped; no paid fallback exists.

Verify without exposing credentials:

```bash
ssh worker 'sudo -u codex-runner -H env -i \
  HOME=/var/lib/engineering-control/identities/codex-runner \
  PATH=/usr/local/bin:/usr/bin:/bin /usr/local/bin/codex login status >/dev/null'
```

## 11. Claude

Claude Code is independently subscription-authenticated under `claude-runner`; no controller
credential was copied. Structured execution, bounded noninteractive tool authorization, schema and
usage normalization, redaction, and both cross-provider directions pass without a dangerous
permission bypass or paid API fallback.

Verify without displaying authentication material:

```bash
ssh worker 'sudo -u claude-runner -H env -i \
  HOME=/var/lib/engineering-control/identities/claude-runner \
  PATH=/usr/local/bin:/usr/bin:/bin /usr/local/bin/claude auth status >/dev/null'
ssh -t worker 'sudo /opt/engineering-control/source/scripts/validate-native-providers.sh'
```

## 12. Project configuration

Falcone maps `gntik-ai/falcone` to `gntik-ai/falcone-charts` and uses deterministic Helm release
plans. The validated reference snapshot used source SHA
`3ecc86496e6fa121910471f893a44017aa75fb78` and chart SHA
`7ad79c5e98af46bb9f25c8e49e042519d705aa96`.

Musematic maps `gntik-ai/musematic` to `gntik-ai/musematic-deploy` and uses protected deployment
PRs followed by Argo CD reconciliation. The inspected reference snapshot used source SHA
`3e609bd80c10a7645cea3a1bb62e0f59392806c8` and deployment SHA
`419aebf8d1269a178dff3ba0a2974c311c67d0c9`. Private canonical mirrors remain blocked pending a
contents-read-only credential.

## 13. Kubernetes

The design separates observer, preview releaser, Falcone staging releaser, optional Musematic
staging observer/releaser, and production releaser identities. Model workers receive no release
credentials. `release-runner` receives no provider credentials.

The existing shared kubeconfig was identified as cluster-admin and is excluded from automation.
Provision scoped identities under `/etc/engineering-control/env/observer` and
`/etc/engineering-control/env/release`, then run negative `kubectl auth can-i` tests before
enabling any adapter. No real cluster mutation was used for bootstrap validation.

## 14. Audit architecture

Audits are durable coverage cells over domain × persona × lens × environment. Every required cell
must end with evidence, an explicit blocker, or an approved exclusion. Deterministic scans run
before model work; candidates require independent fresh-state verification and deduplication.

Example read-only Falcone campaign:

```bash
engineering-ctl audit falcone \
  --name bounded-gateway-audit \
  --source-sha <40-char-source-sha> \
  --deployment-sha <40-char-chart-sha> \
  --domains gateway --personas tenant-admin \
  --lenses correctness --environments snapshot --execute
```

Omit `--execute` for a dry-run request. Audit validation never auto-files GitHub issues.

## 15. Issue delivery

Only `CONFIRMED` findings can enter delivery. A ChangeSet binds the finding, issue, OpenSpec,
source/deployment bases and branches, PRs, immutable image digests, CI, approvals, deployment,
verification, and recovery evidence. One ChangeSet receives one source/deployment worktree pair and
component leases. Codex/Claude maker and checker roles differ; R3 requires both plus human approval.

```bash
engineering-ctl verify-finding <finding-uuid>
engineering-ctl delivery <finding-uuid> --risk R1
engineering-ctl status <returned-workflow-id>
```

GitHub publication remains blocked until a scoped `delivery-runner` credential and registry-read
configuration are installed. The worker cannot merge PRs.

## 16. Deployment

Falcone flow:

```text
source PR -> candidate digest evidence -> falcone-charts PR -> immutable approval
-> deterministic Helm lint/template/diff/preflight -> Helm transaction -> verification/recovery
```

Migration, OpenBao/External Secrets, backup, parity, and recovery gates remain fail-closed. The
adapter never fakes backup verification.

Musematic flow:

```text
source PR -> immutable candidate digest -> musematic-deploy protected PR
-> approved Git state -> Argo CD reconciliation -> post-sync verification -> Git rollback
```

No uncontrolled latest/newest tag promotion or undocumented live patch is accepted. Real staging
mutation requires separate authorization and credentials and otherwise stops at
`AWAITING_STAGING_APPROVAL`.

## 17. Recovery

Recovery is a durable workflow with a fresh immutable binding, environment lease, deterministic
inspection, typed approval, execution by `release-runner`, and post-recovery evidence. Migration
releases prefer forward recovery. Open incidents and release/security evidence are protected from
cleanup.

```bash
engineering-ctl rollback <deployment-uuid> <current-binding-sha256>
engineering-ctl approval-candidate <change-set-uuid>
engineering-ctl approve <change-set-uuid> <new-recovery-binding-sha256>
```

Do not reuse the original staging approval for recovery.

## 18. Upgrade procedure

1. Inventory running workflows, leases, incidents, disk, versions, and release commits.
2. Back up safe `/etc/engineering-control` configuration and PostgreSQL; do not duplicate provider,
   SSH, Telegram, Kubernetes, or Vault credentials.
3. Test and secret-scan new control-plane and agent-pack commits.
4. Review pinned image/tool checksums and upstream migration notes.
5. Drain workers/schedulers, install the commit-addressed release, and run Alembic once.
6. Restart services and verify controller, worker, Temporal restart/resume, Hermes, evidence, and
   redaction.
7. Roll back the code symlink on failure; restore PostgreSQL only for an incompatible migration
   after all writers are stopped.

Detailed component instructions are in `docs/UPGRADE.md`.

## 19. Security findings

- `SECURITY_BLOCKER_MUSEMATIC_GIT_SECRETS`: real credentials were found in Musematic Git history.
  Rotate them, coordinate history rewriting, and rescan every clone before Musematic write or
  deployment enablement. Values are intentionally absent from reports.
- `SECURITY_BLOCKER_KUBERNETES_CLUSTER_ADMIN_IDENTITY`: replace the shared cluster-admin kubeconfig
  with scoped observer/release identities before Kubernetes automation.
- Mirror, delivery, registry, and Kubernetes credentials are absent from their isolated worker
  domains; this is a fail-closed blocker, not a failed installation.
- OmniRoute remains a rollback dependency and public Caddy endpoint until gated cutover. C-02
  requires a fresh secret-safe consumer search and traffic review after C-01.

Central redaction runs before persistence. Secret values are absent from PostgreSQL, reports,
evidence, Telegram responses, prompts, and commits.

## 20. Remaining manual actions

1. Send inbound Telegram `/usage`, `/validate 30`, and `/status` canaries.
2. Provision a contents-read-only private-mirror credential for `scan-runner`.
3. Provision a separate draft-PR credential and GHCR read configuration for `delivery-runner`.
4. Rotate/rewrite/rescan the Musematic credential history before write enablement.
5. Provision scoped Kubernetes observer/release identities and negative permission tests.
6. Authorize any real staging deployment separately.

Remote GitHub repository creation was not authorized or performed.

## 21. Verification results

| Requirement | Result |
| --- | --- |
| Controller API, PostgreSQL, Temporal, scheduler, notifier, evidence ingest | PASS |
| Controller restart/resume lifecycle | PASS |
| Worker service users, services, tools, filesystem/credential isolation | PASS |
| Control-plane tests (216), Ruff, formatting, strict mypy, shell/systemd verification | PASS |
| Agent pack generation, validation, and tests | PASS |
| Codex install/auth/structured native call | PASS |
| Claude install | PASS |
| Claude authentication/structured native call | PASS |
| Codex maker -> Claude checker | PASS |
| Claude maker -> Codex checker | PASS |
| Quota broker without invented values or paid fallback | PASS |
| Hermes fixed command policy, allowlist, API `/usage`, outbound Telegram | PASS |
| User-originated Telegram `/usage`/`validate`/`status` | PENDING OPERATOR |
| Synthetic API -> Temporal -> worktree -> maker/checker -> evidence lifecycle | PASS |
| Independent queue and workflow-worker restart/resume | PASS |
| Falcone public mirrors and exact-SHA worktrees | PASS |
| Musematic private mirrors | BLOCKED: read credential |
| Git dirty-tree/lease conflict/cleanup protection | PASS |
| Evidence hashing, permissions, persistence, and redaction | PASS |
| Kubernetes read-only health and release dry-run | BLOCKED: scoped identities |
| Real staging/production mutation | NOT AUTHORIZED / NOT RUN |
| Cleanup C-03 | PASS |
| Cleanup C-01/C-02 | GATED / NOT RUN |

The detailed live evidence is in `docs/VALIDATION.md` and the inventory under
`/home/hermes/engineering-bootstrap/inventory/`.

## 22. Exact next commands

The provider portion of C-01 passes. To close the remaining Telegram gate, send from the
allowlisted Telegram account:

```text
/usage
/validate 30
/status <workflow-id-returned-by-validate>
```

After those pass, rerun the documented C-01 gate preparation. Do not stop OmniRoute or Caddy
manually and do not run a deployment merely to test the platform.
