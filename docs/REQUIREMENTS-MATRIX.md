# Mission requirements matrix

Status is deliberately split between implementation and host activation. `IMPLEMENTED` means the
version-controlled control-plane behavior exists and has source-level tests. `PREPARED` means
idempotent configuration/automation exists but has not been installed. `BLOCKED` names an external
gate that this bootstrap must not bypass. No row claims live deployment or provider validation.

| Phase | Status | Evidence or remaining gate |
| --- | --- | --- |
| 0 Safety | IMPLEMENTED | `SAFETY-RULES.md`, central redaction, protected-path rules, no credential copying |
| 1 Host inventory | COMPLETE | Redacted controller/worker inventory and checksum manifests under `/home/hermes/engineering-bootstrap/inventory` |
| 2 Asset discovery | COMPLETE | Repository/worktree and reusable-agent reports; dirty worktrees remain protected |
| 3 Security scan | COMPLETE/BLOCKED | Redacted Gitleaks/history inventory; Musematic Git secrets and shared cluster-admin identity are active blockers |
| 4 Cleanup plan | COMPLETE/AWAITING APPROVAL | `/home/hermes/engineering-bootstrap/CLEANUP-PLAN.md`; no removal has run; exact `APPROVE CLEANUP` is required |
| 5 Backup before cleanup | BLOCKED | Runs only after cleanup approval and before the reviewed removals; must produce manifest/checksums/restore guide |
| 6 Filesystem architecture | PREPARED | Controller/worker bootstrap creates `/home/hermes/engineering`, `/srv/engineering`, `/etc/engineering-control`, `/var/lib`, and journald services |
| 7 Git repository layout | IMPLEMENTED | Independent local Git repositories for `engineering-control` and `engineering-agent-pack`; no remote repository was created |
| 8 Controller role | PREPARED | Pinned PostgreSQL/Temporal Compose plus API, workers, scheduler, notifier, evidence ingest, policy and quota services |
| 9 Worker role | PREPARED | Six isolated users/queues: Codex, Claude, browser, scan, delivery, release; filesystem denial tests included |
| 10 Native CLIs | PREPARED/BLOCKED | Pinned installers; independent worker OAuth remains `MANUAL_AUTH_REQUIRED_CODEX` and `MANUAL_AUTH_REQUIRED_CLAUDE` |
| 11 Model adapters | IMPLEMENTED | Native structured Codex JSONL and Claude stream-JSON adapters, cancellation/timeouts/usage/events, redaction |
| 12 Quota broker | IMPLEMENTED | Provider-independent snapshots, unknown-capacity semantics, logical classes, 25% reserve, no paid fallback, durable rate-limit handoff |
| 13 Temporal | IMPLEMENTED/PENDING LIVE TEST | All nine required workflows plus durable delivery reconciliation, signals/retries/compensation/activity boundaries; reboot/restart proof awaits activation |
| 14 PostgreSQL metadata | IMPLEMENTED | Alembic models/migrations for every required table plus approval candidates, notification outbox and worker heartbeats |
| 15 ChangeSet | IMPLEMENTED | First-class model, strict state policy, exact PR/CI/provenance reconciliation, digest promotion, artifact-bound approvals and deterministic metadata transitions |
| 16 Worktrees | IMPLEMENTED | Mirrors, exact-SHA fetch/allocation, branch naming, leases, dirty protection and ACL isolation |
| 17 Component leases | IMPLEMENTED | Project/component/environment/global safety keys and configured concurrency/recursion limits |
| 18 Agent pack | IMPLEMENTED | Fourteen provider-neutral roles, including every requested role; deterministic release remains code, not an agent |
| 19 Skills | IMPLEMENTED | Twenty-three canonical skills generated for Claude, Codex and generic `.agents`, with validation CI |
| 20 Domain packs | IMPLEMENTED | Common plus Falcone/Musematic domain catalogs covering all requested systems |
| 21 Audit campaigns | IMPLEMENTED | Domain/persona/lens/environment cells, complete coverage statuses and evidence/blocker/exclusion gates |
| 22 Evidence | IMPLEMENTED/PREPARED | Content-addressed SHA256 metadata/store, identity-bound upload, central redaction and local hierarchy; external S3 is a documented future option |
| 23 Playwright | IMPLEMENTED/PENDING INSTALL | Chromium/Firefox-capable fixed journeys, isolated context, trace/screenshot/console/network capture, protected credential files |
| 24 Deterministic security | IMPLEMENTED/PENDING INSTALL | Pinned Gitleaks, Trivy, Semgrep, Syft, Helm, kubeconform and manifest/RBAC/secret-reference checks; existing GitHub CodeQL remains repository-owned |
| 25 Finding verification | IMPLEMENTED/BLOCKED FOR WRITE | Two attempts where practical, controls, fresh snapshot, fingerprint and GitHub dedupe; controller read token is not provisioned |
| 26 Finding schema | IMPLEMENTED | Draft 2020-12 schema includes every requested identity, impact, evidence and affected-state field |
| 27 Risk classes | IMPLEMENTED | R0-R3 policy, cross-provider gates, deterministic low-risk approval, human R2/R3 approval and typed R3 confirmation |
| 28 OpenSpec | IMPLEMENTED | Existing assets preserved; bounded OpenSpec generated per delivery and passed without historical audit context |
| 29 Falcone adapter | IMPLEMENTED/BLOCKED LIVE | Exact-SHA CI/provenance and digest-only chart promotion plus Helm lint/template/preflight, immutable plans, real backup/migration gates, atomic deploy, verification/recovery; current release workflow lacks the signed candidate contract, target is disabled, and scoped kubeconfig is absent |
| 30 Musematic adapter | IMPLEMENTED/SECURITY BLOCKED | Exact-SHA CI gate, immutable GitOps validation, draft deployment PR, exact Argo revision observation and Git recovery plan; committed secrets, missing signed candidate contract, tag-only chart rendering and newest-build policy block promotion/write enablement |
| 31 Kubernetes identity separation | PREPARED/BLOCKED | RBAC templates and negative authorization checks exist; operator must create scoped credentials before activation |
| 32 Hermes normalization | PREPARED | Canonical 0.20.0 vendor install retained; extension/hook implements fixed allowlisted Telegram commands with no shell; restart awaits checkpoint/backup |
| 33 Control API | IMPLEMENTED | Strict authenticated campaign/finding/delivery/approval/deployment/usage/evidence routes bound off public wildcards |
| 34 Telegram approvals | IMPLEMENTED/PENDING LIVE TEST | Exact artifact display/digest binding, drift invalidation and typed R3 confirmation; allowlist/token service not activated |
| 35 systemd | PREPARED | Controller and six worker identities use restart/resource/filesystem limits and journald; installed-unit verification awaits sudo bootstrap |
| 36 Observability | IMPLEMENTED | Structured redacted context logs, PostgreSQL-backed Prometheus metrics, authenticated worker heartbeats |
| 37 Failure/handoff | IMPLEMENTED | Required handoff bundle files and explicit `RATE_LIMITED`/cross-provider semantics |
| 38 Cleanup automation | IMPLEMENTED/DISABLED | Dry-run retention workflow, terminal-state and incident/evidence protections, worker/controller double authorization |
| 39 Documentation | IMPLEMENTED | All requested manuals plus cheatsheet, validation status and this matrix |
| 40 Bootstrap/reinstall | IMPLEMENTED/PENDING RUN | Idempotent controller/worker/bootstrap and verification scripts; destructive cleanup remains separate |
| 41 Update procedure | IMPLEMENTED | `UPGRADE.md` covers backup, drain, pin, migration, validation and rollback by component |
| 42 Validation | SOURCE PASS/LIVE BLOCKED | Static/unit/synthetic validation passes; actual services, OAuth, browser/scanners, scoped Kubernetes and restart tests await activation |
| 43 Acceptance lifecycle | SYNTHETIC PASS/LIVE BLOCKED | Real-Git bounded synthetic maker/checker reaches approval; full Telegram/API/Temporal/native-provider chain awaits activation |
| 44 Final report | PENDING | `FINAL-HANDOFF.md` must be generated from post-activation facts; it must not claim uninstalled services or unresolved security gates |

## Gates in required order

1. Operator sends exact `APPROVE CLEANUP` after reviewing the cleanup plan.
2. Create the approved timestamped backup and execute only reviewed cleanup items.
3. Operator supplies sudo interactively for controller/worker bootstrap.
4. Provision controller read-only GitHub issue metadata and worker draft-PR credentials.
5. Authenticate Codex and Claude independently as their worker identities.
6. Remediate Musematic committed credentials and replace the shared cluster-admin kubeconfig with
   scoped observer/release identities before any relevant write enablement.
7. Run live controller/worker, Temporal restart, native cross-provider, Hermes and read-only
   Kubernetes acceptance validation.
8. Keep real staging mutation at `AWAITING_STAGING_APPROVAL` until separately authorized.
