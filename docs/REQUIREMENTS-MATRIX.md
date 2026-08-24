# Mission requirements matrix

Status is split between implementation, live host activation, and external gates. `IMPLEMENTED`
means version-controlled behavior exists and has source tests. `ACTIVE` means it is installed and
verified on the named host. `BLOCKED` names a credential, security, or authorization gate that the
platform fails closed rather than bypassing.

| Phase | Status | Evidence or remaining gate |
| --- | --- | --- |
| 0 Safety | IMPLEMENTED | `SAFETY-RULES.md`, central redaction, protected-path rules, no credential copying |
| 1 Host inventory | COMPLETE | Redacted controller/worker inventory and checksum manifests under `/home/hermes/engineering-bootstrap/inventory` |
| 2 Asset discovery | COMPLETE | Repository/worktree and reusable-agent reports; dirty worktrees remain protected |
| 3 Security scan | COMPLETE/BLOCKED | Redacted Gitleaks/history inventory; Musematic Git secrets and shared cluster-admin identity are active blockers |
| 4 Cleanup plan | CONDITIONALLY APPROVED | C-03 complete; C-01/C-02 remain behind the operator's additional acceptance gates; a deterministic release-bound C-01 evidence builder now prevents manual gate claims |
| 5 Backup before cleanup | COMPLETE | `engineering-backups/20260823T095538Z` has readable checksums and restore procedures; protected credentials were excluded |
| 6 Filesystem architecture | ACTIVE | Normalized controller/worker trees, ownership, ACLs, state and journal services are installed |
| 7 Git repository layout | IMPLEMENTED | Independent local Git repositories for `engineering-control` and `engineering-agent-pack`; no remote repository was created |
| 8 Controller role | ACTIVE | PostgreSQL, Temporal, API, workflow worker, scheduler, notifier and evidence ingest are healthy |
| 9 Worker role | ACTIVE | Six isolated Unix identities and systemd queues are active; credential-independent verifier passes |
| 10 Native CLIs | PASS | Codex 0.149.0 and Claude Code 2.1.239 are independently authenticated; both structured calls and both cross-provider maker/checker directions pass with redacted evidence |
| 11 Model adapters | IMPLEMENTED | Native structured Codex JSONL and Claude stream-JSON adapters, cancellation/timeouts/usage/events, redaction, and immutable role/skill/domain instruction-bundle provenance |
| 12 Quota broker | IMPLEMENTED | Provider-independent snapshots persist CLI presence/authentication/status, unknown-capacity semantics, logical classes, 25% reserve, no paid fallback, durable rate-limit handoff, and seven-day per-role invocation/success/token/cost history without invented values |
| 13 Temporal | ACTIVE/PASS | All required workflows installed; live worker replacement during an active lifecycle resumed durably |
| 14 PostgreSQL metadata | IMPLEMENTED | Alembic models/migrations for every required table plus approval candidates, notification outbox, worker heartbeats, and artifact-retention tombstones |
| 15 ChangeSet | IMPLEMENTED | First-class model, strict state policy, exact PR/CI/provenance reconciliation, deterministic diff-risk classification, independent release-plan review, digest promotion, artifact-bound approvals and metadata transitions |
| 16 Worktrees | IMPLEMENTED | Mirrors, exact-SHA fetch/allocation, branch naming, leases, dirty protection and ACL isolation |
| 17 Component leases | IMPLEMENTED | Project/component/environment/global safety keys and configured concurrency/recursion limits |
| 18 Agent pack | ACTIVE | Thirteen requested provider-neutral roles are installed as an independent immutable release and consumed by native activities with bundle hashes/tool boundaries; deterministic release remains code, not an agent |
| 19 Skills | IMPLEMENTED | Twenty-three canonical skills generated for Claude, Codex and generic `.agents`, with validation CI |
| 20 Domain packs | IMPLEMENTED | Common plus Falcone/Musematic domain catalogs covering all requested systems |
| 21 Audit campaigns | IMPLEMENTED | Domain/persona/lens/environment cells, complete coverage statuses and evidence/blocker/exclusion gates; snapshot failures become batched deterministic cell blockers instead of crashed workflows |
| 22 Evidence | ACTIVE | Content-addressed metadata/store, identity-bound private upload, live redaction and reject-before-persistence canaries pass |
| 23 Playwright | ACTIVE | Chromium/Firefox runtime and fixed journey harness installed under `browser-runner`; target credentials remain unprovisioned |
| 24 Deterministic security | ACTIVE | Pinned Gitleaks, Trivy, Semgrep, Syft, Helm, kubeconform and manifest checks installed; CodeQL remains repository-owned |
| 25 Finding verification | IMPLEMENTED/BLOCKED FOR EXTERNAL READS | Candidate cells launch durable verification children; bounded API/CLI/Telegram retry exists for candidate/flaky/blocked findings; controls/fingerprints/dedupe are enforced; controller issue-search token and private Musematic mirror credential are not provisioned |
| 26 Finding schema | IMPLEMENTED | Draft 2020-12 schema includes every requested identity, impact, evidence and affected-state field |
| 27 Risk classes | IMPLEMENTED | R0-R3 policy, cross-provider gates, deterministic low-risk approval, human R2/R3 approval and typed R3 confirmation |
| 28 OpenSpec | IMPLEMENTED | Existing assets preserved; bounded OpenSpec generated per delivery and passed without historical audit context |
| 29 Falcone adapter | IMPLEMENTED/BLOCKED LIVE | Exact-SHA CI/provenance, digest-only chart promotion, rendered-digest enforcement, Helm lint/template plus kubeconform, immutable plans, independent release review, real backup/migration gates, atomic deploy, verification/recovery; current release workflow lacks the signed candidate contract, target is disabled, and scoped kubeconfig is absent |
| 30 Musematic adapter | IMPLEMENTED/SECURITY BLOCKED | Exact-SHA CI gate, immutable desired-state digest validation, unsafe Image Updater/direct-main rejection, bounded YAML/symlink checks, draft deployment PR, exact Argo observation and Git recovery plan; committed secrets, missing signed candidate contract, tag-only chart rendering and newest-build policy block promotion/write enablement |
| 31 Kubernetes identity separation | IMPLEMENTED/BLOCKED CREDENTIALS | RBAC templates and negative checks exist; no scoped observer/release kubeconfigs are installed |
| 32 Hermes normalization | ACTIVE/PARTIAL | Canonical 0.20.0, explicitly enabled command plugin, fixed allowlisted hook and bounded `/validate` route are active; only the user-originated Telegram canary and gated Omni alias cutover remain |
| 33 Control API | IMPLEMENTED | Strict authenticated campaign/finding/delivery/approval/deployment/usage/evidence routes bound off public wildcards |
| 34 Telegram approvals | ACTIVE/PENDING INBOUND | Allowlist, hook-to-API, bounded `/validate`, outbound Telegram, and release/workflow-bound secret-free canary receipts are active; user-originated `/usage`/`validate`/`status` sequence is pending |
| 35 systemd | ACTIVE | Controller, Hermes and six worker identities are supervised with resource/filesystem limits and journald |
| 36 Observability | IMPLEMENTED | Structured redacted context logs, PostgreSQL-backed Prometheus metrics, authenticated worker heartbeats |
| 37 Failure/handoff | IMPLEMENTED | Required handoff bundle files and explicit `RATE_LIMITED`/cross-provider semantics |
| 38 Cleanup automation | IMPLEMENTED/DISABLED | Daily schedule defaults dry-run and switches to typed mutation only under the explicit per-identity execution setting; it covers expired clean worktrees, bounded delivery/browser/Codex-temp directories, explicitly ephemeral evidence, stale terminal component leases, and opt-in managed preview namespaces; owner inspection, one immutable cross-resource authorization, symlink/drift checks, and protected evidence/incident/rate-limit gates are tested; host mutation remains disabled |
| 39 Documentation | IMPLEMENTED | All requested manuals plus cheatsheet, validation status and this matrix |
| 40 Bootstrap/reinstall | PASS | Both bootstraps rerun successfully and installed the same immutable release on controller and worker |
| 41 Update procedure | IMPLEMENTED | `UPGRADE.md` covers backup, drain, pin, migration, validation and rollback by component |
| 42 Validation | HOST/PROVIDER PASS; EXTERNAL BLOCKED | Source, services, both native providers, browser/scanners, isolation, evidence and Temporal restart pass; GitHub/Kubernetes credentials remain |
| 43 Acceptance lifecycle | SYNTHETIC AND PROVIDERS PASS / INBOUND PENDING | Authenticated API/Temporal/worktree/evidence chain passes with restart; both real cross-provider directions pass; inbound Telegram remains |
| 44 Final report | COMPLETE | Version-controlled `FINAL-HANDOFF.md` records post-activation facts, exact blockers, cleanup state, recovery, verification, and next commands without claiming gated acceptance |

## Gates in required order

1. Rerun/fetch the release-bound provider matrix and restart/resume lifecycle.
2. Apply the backup-first native Hermes alias cutover while keeping OmniRoute fully available for
   rollback; restart Hermes and require the no-OmniRoute active-configuration policy to pass.
3. Send allowlisted Telegram `/usage`, `/validate 30`, and `/status`; validate the genuine inbound
   round trip and use the deterministic builder to emit `C01-ACCEPTANCE.json` only if every C-01
   gate passes.
4. Stop/disable OmniRoute without uninstalling it, restart Hermes/workers, and rerun the complete
   Telegram regression. Only after that pass, uninstall the reviewed npm package while retaining
   `.omniroute`.
5. Secret-safely prove Caddy has no consumers and has no legitimate runtime traffic, then execute
   C-02 and rerun the complete Telegram regression.
6. Provision the distinct read-only mirror token, controller issue-search token, draft-PR token,
   and registry-read credential without copying interactive user credentials.
7. Remediate Musematic committed credentials and replace the shared cluster-admin kubeconfig with
   scoped observer/release identities before any relevant write enablement.
8. Keep real staging mutation at `AWAITING_STAGING_APPROVAL` until separately authorized.
