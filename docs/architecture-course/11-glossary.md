# Lecture 11 — Glossary and quick reference

> Every term, code, and name a student will meet in this codebase, in one place. Codes are grouped by where they appear.

---

## A. Core vocabulary

| Term | Definition |
|---|---|
| **Agent** | One bounded invocation of a native provider CLI (Codex or Claude Code) with one agent-pack role, one skill, a tool allowlist, a sandbox mode, and a JSON output schema. Not a long-lived process. |
| **Agent pack** | The separate, commit-addressed repository of roles, skills, common policy, domain packs, and generated provider-specific role files, installed read-only at `/opt/engineering-agent-pack/source`. |
| **Approval binding** | SHA-256 of the canonical JSON of an immutable candidate payload; an approval is valid only while the live content still hashes to it. |
| **Backlog campaign** | An imported inventory of open GitHub issues for one project, with classification, plan, and a 24-hour approval binding. |
| **Candidate build contract** | A CI run at the exact PR head that publishes `engineering-artifacts.json` with one `sha256:` digest per first-party image, each verifiable with `gh attestation verify`. |
| **ChangeSet** | The first-class record of one bounded change across the source and deployment repositories: branches, SHAs, PRs, digests, approvals, verification, recovery. 26 states. |
| **Checker** | The read-only reviewing agent (`code-reviewer`), always a different invocation and by default a different provider from the maker. |
| **Controller** | The host running the API, Temporal, PostgreSQL, scheduler, notifier, dashboard, evidence ingest, and the GitHub broker worker. |
| **Control step** | A named unit of work (`<domain>.<verb>`) executed by the single Temporal activity `execute_control_step` and dispatched to a handler. About 220 exist. |
| **Coverage cell** | One `(surface, domain, persona, flow, lens, environment)` element of an audit matrix. |
| **Deployment target** | A non-secret record under `/etc/engineering-control/targets` enabling a preview or staging environment; ships disabled with a blocker. |
| **Evidence** | Content-addressed, redacted artefacts uploaded by worker identities through identity-bound tokens. |
| **Finding** | A candidate or confirmed defect with a fingerprint, severity, confidence, risk class, and evidence. |
| **GitHub broker** | `GhCliBroker`: typed `gh` operations executed on the controller as `hermes`, gated by the write policy and a change binding. |
| **Handoff bundle** | The seven files written when a provider is rate-limited: `TASK.md`, `CONTEXT.json`, `DECISIONS.md`, `SOURCE-REFS.json`, `DIFF.patch`, `TEST-RESULTS.json`, `OPEN-QUESTIONS.md`. |
| **Harness** | Any layer that constrains an untrusted executor: Unix identity + systemd sandbox; leased worktree with ACLs; provider CLI flags; browser origin allowlist; observer `can-i` checks; evidence ingest. |
| **Hermes** | The third-party Telegram gateway agent into which the platform installs a plugin and a hook. A client, not an engine. |
| **Issue loop** | `ExistingGitHubBacklogLoopWorkflow`: the main per-issue delivery loop over an imported backlog. |
| **Lease** | Exclusive ownership: file leases for worktree allocation; PostgreSQL component leases (`implementation` ≤2 per project, `deployment` 1 per environment, `migration-secret` 1 global). |
| **Maker** | The writable implementing agent (`implementer`), the only role with `Edit`/`Write` tools and `--sandbox workspace-write`. |
| **Mirror** | A canonical bare Git repository under `/srv/engineering/mirrors`, refreshed only by the GitHub worker. |
| **Mission** | One of the three product functions: issue resolution, product audit, enhancement discovery. |
| **Operation** | One row of `operation_requests`: a typed command from any surface with one active owner per target and an atomic dispatch receipt. |
| **Policy owner** | The approved backlog campaign whose approval enabled the shared GitHub write policy for its project. Since NEW-163 there is at most one per project and at most two projects at once (one globally while the global backlog runs). |
| **Preset** | A named audit configuration: `snapshot`, `static-full`, `e2e-smoke`, `full-e2e`, `security`, `ux`, `deployment`, `falcone-intensive`. |
| **Provider** | `codex` or `claude`. |
| **Publication intent / effect** | Durable `steps` records binding a publication plan hash and each admitted `git_push` / `pr_create` effect with a `COMPLETE` / `UNKNOWN` / `NOT_ATTEMPTED` receipt. |
| **Risk class** | `R0`–`R3`; a deterministic floor that can only rise. |
| **Root blocker** | One deterministic cause (`rb-<hash>`) referenced by many blocked cells, classified as exactly one `RootBlockerClassification`. |
| **Run** | One application run bound to one Temporal workflow id and, after its first activity, to the Temporal first-execution run id. |
| **Shadow repo** | A per-ChangeSet bare Git repository (`clone --bare --shared`) under the scan cache that holds the maker's refs and reads objects from the canonical mirror. |
| **Staging flow** | The post-merge GitOps delivery (`StagingGitOpsDeliveryWorkflow`) driven by Argo CD Image Updater; the path actually enabled for falcone and musematic. |
| **Typed phrase** | A server-generated confirmation string, usually containing a hash prefix, that an operator must type to approve. |
| **Worker** | The host running the six isolated role identities. Also, generically, a Temporal worker process. |
| **Worktree** | The maker's working copy at `/srv/engineering/worktrees/<repo>/<change-set-id>`, allocated by `scan-runner` with POSIX ACLs. |
| **Write policy** | `/etc/engineering-control/policy/github-issue-write-validation.yaml` with exactly `enabled` and `safe_repositories`, root-owned, rewritten only by the root reconciler. |

## B. Identities, hosts, queues

| Identity | Host | Queue | Unit |
|---|---|---|---|
| `engineering-control` | controller | `engineering-control` (workflows + controller steps) | `engineering-temporal-worker`, `engineering-api`, `engineering-dashboard`, `engineering-evidence-ingest`, `engineering-scheduler`, `engineering-notifier`, `engineering-watchdog`, `engineering-acceptance-worker`, `engineering-migrate` |
| `hermes` | controller | `engineering-github` | `engineering-github-worker` |
| `root` | controller | — | `engineering-infrastructure`, `engineering-github-policy-reconciler`, `engineering-github-policy-expiry` |
| `hermes-gateway` | controller | — | `hermes-gateway.service` (outside this repo) |
| `codex-runner` | worker | `engineering-codex` | `engineering-worker@codex` |
| `claude-runner` | worker | `engineering-claude` | `engineering-worker@claude` |
| `scan-runner` | worker | `engineering-scan` | `engineering-worker@scan` |
| `browser-runner` | worker | `engineering-browser` | `engineering-worker@browser` |
| `delivery-runner` | worker | `engineering-delivery` | `engineering-worker@delivery` |
| `release-runner` | worker | `engineering-release` | `engineering-release-worker` |
| (acceptance) | controller | `engineering-acceptance` | `engineering-acceptance-worker` |

Ports: PostgreSQL `127.0.0.1:55432`; Temporal `127.0.0.1:7233` and `10.10.0.2:7233`; Temporal UI `127.0.0.1:8741`; API `127.0.0.1:8740`; evidence ingest `10.10.0.2:8750`; dashboard `127.0.0.1:8750`; Hermes webhook `127.0.0.1:8644`; Caddy `:443`.

## C. Workflow names

`AuditCampaignWorkflow`, `MultiProjectAuditWorkflow`, `FindingVerificationWorkflow`, `IssueDeliveryWorkflow`, `DeliveryReconciliationWorkflow`, `FinalClosureWorkflow`, `PreviewDeploymentWorkflow`, `StagingDeploymentWorkflow`, `RuntimeVerificationWorkflow`, `RecoveryWorkflow`, `QuotaRefreshWorkflow`, `ConnectionsRefreshWorkflow`, `CleanupWorkflow`, `AcceptanceLifecycleWorkflow`, `ObservabilityAcceptanceWorkflow`, `ReverseObservabilityAcceptanceWorkflow`, `IssueSolutionLoopWorkflow`, `GitHubBacklogImportWorkflow`, `IssueAmendObservationWorkflow`, `PublicationHeadsObservationWorkflow`, `StagingGitOpsDeliveryWorkflow`, `ExistingGitHubBacklogLoopWorkflow`, `AutonomousBacklogCampaignWorkflow`, `EnhancementDiscoveryWorkflow`, `EnhancementDeliveryWorkflow`, `EnhancementSolutionLoopWorkflow`, `LLMWikiStagingBootstrapWorkflow`, `ChangeRequestWorkflow`, `UiMigrationPlanWorkflow`, `UiBlueprintFanOutWorkflow`, `UiGapConsolidationPlanWorkflow`, plus the script-only `GitHubWriteDenialAcceptanceWorkflow`.

## D. Agent-pack roles and skills

| Role | Class | Writable | Used by step |
|---|---|---|---|
| `implementer` | maker / worker | **yes** | `delivery.invoke_maker`, `delivery.invoke_deployment_maker` |
| `product-spec-architect` | planner / judge | no | `delivery.invoke_spec`, `change_request.draft`, `enhancement.product_review` |
| `code-reviewer` | checker / judge | no | `delivery.invoke_checker`, `change_request.review` |
| `domain-auditor` | finder / routine | no | `audit.agent_cell`, `enhancement.discover` |
| `security-auditor` | checker / judge | no | `audit.agent_cell` for security, authorization, tenant-isolation lenses |
| `finding-verifier` | checker / judge | no | `finding.reproduce` |
| `release-plan-reviewer` | checker / judge | no | `reconcile.invoke_release_reviewer` |
| `runtime-verifier` | checker / judge | no | `runtime.independent_review` |
| `ui-migration-architect`, `ui-gap-triager` | — | no, tools `Read, Glob, Grep` | `ui_plan.draft/review`, `ui_fanout.gap_draft`, `ui_consolidate.plan` |
| `persona-auditor`, `campaign-planner`, `incident-analyst`, `journey-tester`, `repository-cartographer` | — | no | catalogue roles not bound to a current step |

Skills by step: `audit-domain`, `audit-security`, `verify-finding`, `create-openspec`, `implement-issue`, `review-change`, `prepare-staging`, `verify-runtime`, `discover-enhancements`, `review-enhancement`, `plan-ui-migration`, `triage-ui-gap`.

## E. Status and blocker codes (selected)

**Run / agent:** `QUEUED`, `RUNNING`, `SUCCESS`, `FAILED`, `RATE_LIMITED`, `CANCELLED`, `TIMED_OUT`, `BLOCKED`; `PROVIDER_TIMEOUT`, `PROVIDER_MODEL_AT_CAPACITY`, `PROVIDER_RATE_LIMIT`, `PROVIDER_EXIT_NONZERO`, `PROVIDER_ACTIVITY_FAILED`, `AGENT_PACK_INVALID`, `MANUAL_AUTH_REQUIRED_CODEX|CLAUDE`, `PROVIDER_CLI_MISSING_CODEX|CLAUDE`, `AUTH_STATUS_*`, `SchemaViolation`, `PolicyViolation`, `NotConfigured`, `SecurityBlocker`, `DirtyWorktree`, `ApprovalMismatch`, `ReconciliationPending`, `ProtectedRecoveryPRRequired`.

**Finding:** `CANDIDATE`, `CONFIRMED`, `REJECTED`, `FLAKY`, `BLOCKED`, `DUPLICATE`; cells `NOT_STARTED`, `IN_PROGRESS`, `CANDIDATE_FOUND`, `BLOCKED`, `NO_FINDING`, `CONFIRMED_FINDING`, `RETEST_REQUIRED`, `VERIFIED_FIXED`; root blockers `PRODUCT_DEFECT`, `DEPLOYMENT_DEFECT`, `ENGINEERING_CONTROL_DEFECT`, `TEST_HARNESS_DEFECT`, `MISSING_OPERATOR_INPUT`, `EXTERNAL_DEPENDENCY_FAILURE`, `RUNTIME_PROVENANCE_GAP`; `PROJECT_SNAPSHOT_UNAVAILABLE`, `LIVE_REVISION_DRIFT`, `COVERAGE_MATRIX_LIMIT`, `NO_APPLICABLE_COVERAGE_CELLS`, `INCOMPLETE_IDENTITY_CLEANUP`, `SUPERADMIN_AUTHENTICATION_FAILED`.

**Delivery:** `WORKTREES_ALLOCATED`, `PRECHECKS_PASS`, `TOOLCHAIN_READY|SKIPPED`, `SPEC_REUSED`, `DEPENDENCIES_REQUIRED|RESOLVED`, `DEPLOYMENT_PIN_CONVERGED|UNCHANGED`, `LOCAL_VALIDATION_PASS`, `REQUEST_CHANGES`, `INDEPENDENT_CHECKER_DID_NOT_PASS`, `MAKER_TEST_FAILURE`, `AGENT_CONFIG_CHANGE_REFUSED`, `CHANGESET_VALIDATION_FINDINGS`, `WITHHELD_SECRET_FINDINGS`, `SOURCE_PRS_CREATED`, `PR_HEAD_NOT_CONVERGED`, `AWAITING_IMMUTABLE_ARTIFACT`, `COMPONENT_LEASE_CONFLICT`, `ISSUE_BODY_CHANGED_SINCE_APPROVAL`, `CORRECTION_LIMIT_EXHAUSTED`, `CORRECTION_EXTENSION_USED`, `BLOCKED_BY_POLICY`, `BLOCKED_BY_GH_AUTH`, `AWAITING_WRITE_POLICY`, `PUBLICATION_AUTHORITY_DENIED_OR_UNVERIFIED`.

**GitOps / release:** `SOURCE_PR_CI_TIMEOUT|FAILED|REGRESSION`, `DEPLOYMENT_PR_CI_REGRESSION`, `*_PR_MERGE_BLOCKED|CONFLICT`, `PROTECTED_MERGE_FAILED`, `RELEASE_TAG_FAILED`, `MAIN_CI_*`, `IMAGE_BUILD_*`, `ENVIRONMENT_UNHEALTHY`, `IMAGE_NOT_PINNED`, `ARGO_OPERATION_BUSY`, `ARGO_SYNC_FAILED|TIMEOUT`, `ARGO_PRUNE_REQUIRED`, `STAGING_VERIFICATION_FAILED`, `STAGING_VERIFIED`, `MERGE_DELIVERED`, `R3_MERGE_REQUIRES_APPROVAL`, `CHANGE_SET_STATE_NOT_DELIVERABLE`, `CANDIDATE_VERIFIED`, `PROMOTION_READY|COMMITTED`, `IMMUTABLE_PROMOTION_VALIDATION_FAILED`, `DEPLOYMENT_PR_CREATED`, `RELEASE_REVIEW_REQUIRED`, `RELEASE_EVIDENCE_INCOMPLETE`, `PREFLIGHT_PASS`, `AWAITING_PREVIEW|STAGING|RECOVERY_APPROVAL`, `APPROVAL_ARTIFACT_DRIFT`, `APPROVAL_INVALIDATED`, `STAGING_DEPLOYED`, `PREVIEW_VERIFIED`, `RUNTIME_VERIFIED`, `DEPLOYMENT_FAILED`, `INCIDENT_OPEN`, `ROLLED_BACK`, `ROLLBACK_APPLIED`, `RUNTIME_RETEST_REQUIRED`, `FORWARD_RECOVERY_REQUIRED`, `PROTECTED_RECOVERY_PR_REQUIRED`, `RECOVERY_BASELINE_MISSING`, `FINAL_BUILD_WAIT_EXHAUSTED`, `FINAL_ARTIFACT_VERIFIED`, `FIXED`.

**Backlog / operations:** `IMPORTED`, `PLANNED`, `AWAITING_APPROVAL`, `APPROVED_POLICY_PENDING`, `APPROVED_INVENTORY`, `APPROVED`, `RUNNING`, `PAUSED`, `EXPIRED`, `COMPLETE`, `COMPLETE_NOTHING_DELIVERED`, `STOPPED`; `R3_REQUIRES_OPERATOR`, `HERMES_OPERATIONAL_ISSUE`, `EXISTING_PR_ADOPTION_RECONCILIATION_REQUIRED`, `CURRENT_SOURCE_REVALIDATION_REQUIRED`, `CANONICAL_MIRROR_REFRESH_FAILED`, `LOOP_STEP_FAILED_<STEP>`, `DELIVERY_FAILED:PROVIDER_UNAVAILABLE`, `ISSUE_START_CAMPAIGN_IN_USE`, `ISSUE_START_UNCONFIRMED`, `START_UNCONFIRMED`, `ORPHANED_START_INTENT`; capability blockers `BACKLOG_CAMPAIGN_ALREADY_ACTIVE`, `ISSUE_RISK_EXCEEDS_BOUND`, `ISSUE_WORKFLOW_ALREADY_ACTIVE`, `GITHUB_WRITE_POLICY_NOT_ACTIVE`, `R3_APPROVAL_REQUIRED`, `AUDIT_PLAN_REQUIRED`, `WORKFLOW_IS_TERMINAL`, `UNKNOWN_STATUS_MAPPING`.

**Policy / bootstrap:** `POLICY_DISABLED`, `EMPTY_SAFE_REPOSITORY_LIST`, `REPOSITORY_NOT_ALLOWLISTED`, `POLICY_MISSING`, `POLICY_UNREADABLE`, `POLICY_INVALID`, `PASS_DENIAL_ENFORCED`, `GITHUB_BACKLOG_POLICY_KEPT_FOR_OWNER`, `MANUAL_GITHUB_MIRROR_CREDENTIAL_REQUIRED`, `MANUAL_GITHUB_DELIVERY_CREDENTIAL_REQUIRED`, `MANUAL_GITHUB_REGISTRY_AUTH_REQUIRED`, `MANUAL_EVIDENCE_INGEST_TOKEN_REQUIRED`, `SECURITY_BLOCKER_MUSEMATIC_GIT_SECRETS`, `SECURITY_BLOCKER_KUBERNETES_CLUSTER_ADMIN_IDENTITY`, `FALCONE_CANDIDATE_BUILD_CONTRACT_MISSING`, `MUSEMATIC_CANDIDATE_BUILD_CONTRACT_MISSING`, `MUSEMATIC_DIGEST_RENDERING_UNSUPPORTED`, `LLMWIKI_PREVIEW_PROHIBITED`.

## F. Paths

`/opt/engineering-control/{releases,source,venv}`, `/opt/engineering-agent-pack`, `/opt/engineering-cli`, `/opt/engineering-browser{,-runtimes}`, `/opt/engineering-scanners`, `/opt/engineering-toolchain`, `/etc/engineering-control/{control.yaml,projects.d,personas.d,flows.d,policies,policy,targets,runtime,journeys,env,credentials,dashboard,systemd}`, `/var/lib/engineering-control/{identities,workers,leases,github-policy-requests,github-policy-backups,postgresql}`, `/var/lib/engineering-watchdog`, `/var/lib/engineering-backup`, `/srv/engineering/{mirrors,worktrees,runs,evidence,cache,tmp}`, `/run/gntik-ui-mcp/mcp.sock`, `/home/hermes/.hermes/{plugins,hooks}/engineering-control`, `/home/hermes/engineering-bootstrap`, `/home/hermes/engineering-backups`.

## G. Where to read next

| Question | Document |
|---|---|
| What is installed and verified right now? | `docs/REQUIREMENTS-MATRIX.md`, `docs/VALIDATION.md`, `docs/ARCHITECTURE-VERIFIED.md` |
| Every workflow and step with line numbers | `docs/inventory/flows.md` |
| Every Telegram command and its grammar | `docs/telegram/commands.md`, `docs/telegram/commands.yaml` |
| Which model and effort each role dispatches with | `docs/inventory/models.md` |
| How to operate day to day | `docs/OPERATOR-CHEATSHEET.md`, `docs/RUNBOOK.md`, `docs/OPERATIONS.md` |
| How to upgrade or roll back | `docs/UPGRADE.md`, the dashboard document's backup section |
| Why a flow is blocked | the `blocker` code in the workflow status, then `docs/DEPLOYMENTS.md`, `docs/CI-ARTIFACT-RECONCILIATION.md`, `docs/SECURITY.md` |
| Project history and open findings | `docs/GO-STATUS.md`, `docs/BACKLOG.md`, `.go-status/findings.json` |
