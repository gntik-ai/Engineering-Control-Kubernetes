# Telegram worked examples

Generated 2026-09-27, read-only. **No write command was run to produce these.** Conventions:

- `👤` = operator message, `🤖` = bot reply. Replies are always one JSON message `{"next_cursor": …, "payload": …}` (see `commands.md` Part 1); long payloads are shortened here with `…`.
- **[real API]** = the payload was fetched read-only from the live API on 2026-09-27 (`GET` equivalents of the command). **[real run]** = facts from Temporal history / GitHub for a run that happened (the run was started through the CLI/API; which channel was used is not recorded). **[reconstructed]** = built from the code paths cited in `commands.md`; field names are exact, values illustrative.
- Real Telegram transcripts are **not available**: the gateway's session DB and logs are under `/var/lib/hermes-gateway/home` (unreadable to `hermes`).
- Syntax is from the repository hook. The installed `~/.hermes` hook differs only for `/loop r3-*` (musematic 1119 only) and the removed `/loop delivery-candidate|approve-delivery`.

What each product supports today (from `config/projects/*.yaml`):

| Product | `source-pr` | `full-delivery` | Why |
|---|---|---|---|
| musematic (+ musematic-deploy) | yes | **yes** — post-merge GitOps flow (`staging_flow.enabled: true`, Argo CD `musematic-platform`, argocd-image-updater) | `config/projects/musematic.yaml:176-184` |
| falcone (+ falcone-charts) | yes | **no** — preflight BLOCKED `FALCONE_CANDIDATE_BUILD_CONTRACT_MISSING` (also `FALCONE_SCOPED_STAGING_RELEASER_AND_REPOSITORY_VALUES_MISSING`) | `config/projects/falcone.yaml:48-81`, `operations/repository.py:1110-1135` |
| llmwiki | yes | **no** — `LLMWIKI_CANDIDATE_BUILD_DISABLED`, `LLMWIKI_STAGING_BOOTSTRAP_APPROVAL_REQUIRED` | `config/projects/llmwiki.yaml:43-87` |

In `source-pr` mode Hermes stops at an open, checked source PR: **merging it and closing the issue are manual**, and Hermes does not read PR review comments (no code path consumes them). **GitHub Project cards are never moved** — there is no Projects API code in `src/` (grep `projectV2|project_card` → nothing).

---

## 1. Issue resolution — falcone (`gntik-ai/falcone-charts#45`)

Issue: *bug: keycloak publicPath=/auth is a no-op under Keycloak 26 — iam.\* public OIDC surface 404s* (open, R1).

**Step 1 — find it in the backlog** [real API]

```
👤 /loop issue-locate falcone deployment 45
🤖 {"next_cursor": null, "payload": {"status": "FOUND", "project": "falcone", "surface": "deployment",
     "backlog_campaign_id": "3f24fc41-0ab0-4cf7-88a5-9b2491e79eee",
     "issue": {"repository": "gntik-ai/falcone-charts", "issue_number": 45, "calculated_risk": "R1",
               "candidate_status": "NEEDS_CURRENT_STATE_REVALIDATION", "eligibility_status": "ELIGIBLE_FOR_REVALIDATION",
               "reproduction_mode": "DEPLOYMENT_REQUIRED", "delivery_status": "NOT_STARTED", "planned_wave": "W0", …}}}
```

The campaign `3f24fc41…` was imported 2026-08-31 (82 issues) and its approval is `EXPIRED` [real API]. Refresh it first so the issue body and state are current:

**Step 2 — refresh the backlog** [reconstructed]

```
👤 /loop import falcone github-open all
🤖 {"next_cursor": null, "payload": {"workflow_id": "backlog-import-falcone-<new-campaign-uuid>", "run_id": "…",
     "status": "ACCEPTED", "backlog_campaign_id": "<new-campaign-uuid>"}}
```

Wait ~1–3 min, then `👤 /status backlog-import-falcone-<uuid>` until `"status": "COMPLETED"`. Check: Temporal UI → `GitHubBacklogImportWorkflow` completed; `/loop inventory falcone <new-uuid> 20` lists the issues.

**Step 3 — try full delivery (shows the blocker)** [reconstructed from `operations/service.py:97-110`]

```
👤 /loop issue-start falcone deployment 45 full-delivery
🤖 {"next_cursor": null, "payload": {"operation_id": "…", "action": "solve_issue", "source_channel": "telegram",
     "status": "BLOCKED", "workflow_id": null,
     "safe_error": "operation preflight changed: FALCONE_CANDIDATE_BUILD_CONTRACT_MISSING", …}}
```

**Step 4 — start source-PR delivery** [reconstructed]

```
👤 /loop issue-start falcone deployment 45 source-pr
🤖 {"next_cursor": null, "payload": {"operation_id": "…", "action": "solve_issue", "status": "COMPLETED",
     "workflow_id": "backlog-loop-falcone-<campaign>-<uuid>", "temporal_run_id": "…", "safe_error": null, …}}
🤖 (a few seconds later, notifier) {"topic": "telegram.engineering-operation-started", "payload": {"operation_id": "…",
     "project": "falcone", "target": "…", "workflow_id": "backlog-loop-falcone-…", "phase": "QUEUED"}}
```

The command self-approves the 24 h write policy for this campaign and waits up to 60 s for the root reconciler. If another campaign owns the policy the reply is `status: BLOCKED` with `safe_error: another backlog campaign policy activation is pending`.

**Step 5 — follow progress** (every 5–15 min)

```
👤 /loop status backlog-loop-falcone-<campaign>-<uuid>
🤖 {"payload": {"workflow_id": "…", "run_id": "…", "status": "RUNNING", "workflow_type": "ExistingGitHubBacklogLoopWorkflow"}}
👤 /loop tail backlog-loop-falcone-<campaign>-<uuid>-issue-1-attempt-1 30
🤖 {"payload": {"items": [{"event_type": "workflow.started", …}, {"event_type": "provider.execution_started", …}, …]}}
```

Expected path: mirror refresh → revalidation (FindingVerificationWorkflow, ~10–20 min, 2 provider reproductions) → IssueDeliveryWorkflow (spec Claude, maker Codex, checker Claude; ~30–90 min per attempt, up to 6 attempts) → PR opened on `gntik-ai/falcone-charts`. Check GitHub: a PR titled `changeset <8-hex>: falcone-charts:general finding` (title pattern from PRs #1121/#1126).

**Step 6 — review and merge (manual)**. Review comments on the PR are not read by Hermes. To have Hermes change the PR, the loop must be paused on the issue (e.g. after a blocker) and retried: `/loop retry <loop-id> falcone-charts 45` re-runs delivery on the same ChangeSet. Merge the PR and close #45 on GitHub yourself; Hermes will not, in `source-pr` mode.

**If revalidation cannot reproduce it** [reconstructed, D:2567-2612]: the loop pauses with `phase AWAITING_OPERATOR:gntik-ai/falcone-charts#45:CURRENT_REVALIDATION_<STATUS>`; decide with `/loop skip <loop-id> falcone-charts 45 NOT_REPRODUCIBLE` or `/loop retry …`.

**R3 variant** (e.g. `gntik-ai/falcone#1016`, R3 [real API]): `/loop issue-start falcone source 1016` → BLOCKED `operation preflight changed: ISSUE_RISK_EXCEEDS_BOUND`. Prepare an R3 binding with `/loop r3-candidate falcone source 1016`, then `/loop approve-r3 falcone source 1016 <binding> APPROVE_R3_FALCONE_1016_<hex>`. The `~/.hermes` hook copy only accepts `musematic source 1119` here.

---

## 2. Issue resolution — llmwiki (`gntik-ai/llmwiki#7`, and how a failure becomes a Falcone gap)

`#7 docs: add a README to services/wiki-api` is imported in campaign `cd217170-1a35-464a-9e81-93d36c5a76fa`, R0, `REVALIDATION_BLOCKED` [real API]. `#8 ci: run the e2e and integration tests in CI` is **not** imported:

```
👤 /loop issue-locate llmwiki source 8
🤖 Engineering Control: Engineering API rejected the request (404): {"detail":"issue is not in a current backlog inventory"}   [real API response]
👤 /loop import llmwiki github-open all
🤖 {"payload": {"workflow_id": "backlog-import-llmwiki-<uuid>", "status": "ACCEPTED", "backlog_campaign_id": "<uuid>", …}}   [reconstructed]
```

Then, as in example 1, in `source-pr` mode (`full-delivery` → BLOCKED `LLMWIKI_CANDIDATE_BUILD_DISABLED`):

```
👤 /loop issue-start llmwiki source 7 source-pr
🤖 {"payload": {"action": "solve_issue", "status": "COMPLETED", "workflow_id": "backlog-loop-llmwiki-<campaign>-<uuid>", …}}   [reconstructed]
```

**Falcone gap from an llmwiki failure.** No Telegram command or activity files an issue into `gntik-ai/falcone` from an llmwiki delivery. The code path that turns llmwiki-observed failures into Falcone findings is the `falcone-intensive` audit preset. It snapshots llmwiki and runs `audit.llmwiki_consumer_suite` as a Falcone audit stage (D:560-569, D:728-729); confirmed findings are published to Falcone:

```
👤 /audit-plan falcone falcone-intensive staging
👤 /audit falcone falcone-intensive staging --execute --publish-findings
🤖 {"payload": {"action": "audit_start", "status": "COMPLETED", "workflow_id": "audit-falcone-<uuid>", …}}   [reconstructed]
```

`--publish-findings` writes GitHub issues through the write policy (`finding.publish`), so an active write-policy owner campaign for falcone is required.

---

## 3. Issue resolution — musematic, including musematic-deploy / Argo CD (real run: #1125 and #993)

This is the path that ran end to end on 2026-09-26/27 [real run]. The Telegram form of the start command:

```
👤 /loop issue-start musematic source 1125 full-delivery
🤖 {"payload": {"action": "solve_issue", "status": "COMPLETED",
     "workflow_id": "backlog-loop-musematic-66a97948-9f9d-4ae9-b204-e89f27e2712d-27604282-30bc-4052-8dc1-12f92bcd7dea", …}}   [workflow id real; reply reconstructed]
```

Timeline [real run, Temporal + GitHub]:

| Time (UTC) | What happened | Check |
|---|---|---|
| 09-26 20:22 | loop started (campaign `66a97948…`, security filter, 5 issues) | Temporal: `ExistingGitHubBacklogLoopWorkflow` RUNNING |
| 09-26 20:22–20:26 | revalidation `FindingVerificationWorkflow` CONFIRMED | `/status …-issue-1-revalidate-1125` |
| 09-27 ~10:25–11:00 | IssueDelivery attempts (maker `codex` high, checker `claude-opus-5-5` high) → source PR | GitHub PR `gntik-ai/musematic#1126` |
| 09-27 11:21 | `StagingGitOpsDeliveryWorkflow` (`…-issue-1-staging-gitops`) | `/loop tail … 30` |
| 09-27 12:21 | PR #1126 merged → `f7b0a97` (Hermes, protected merge) | GitHub |
| 09-27 12:32 | release `v0.0.109` published; image built | GitHub releases |
| 09-27 12:54 | GitOps retry `…-staging-gitops-retry-1`: image pin (argocd-image-updater writes musematic-deploy), Argo sync, workload verification | Argo CD app `musematic-platform` Synced/Healthy |
| 09-27 12:56 | issue #1125 closed by Hermes with delivery evidence | GitHub issue |
| 09-27 13:59 | #993 (PR #1121, merge `707ba5d`, shipped in v0.0.109) closed by its own loop `backlog-loop-musematic-94a83947…-70ad7d74…` (GitOps `…-retry-4`) | GitHub issue #993 |

Status as the operator saw it [real API]:

```
👤 /loop status backlog-loop-musematic-66a97948-9f9d-4ae9-b204-e89f27e2712d-27604282-30bc-4052-8dc1-12f92bcd7dea
🤖 {"next_cursor": null, "payload": {"workflow_id": "backlog-loop-musematic-66a97948-…-27604282-30bc-4052-8dc1-12f92bcd7dea",
     "run_id": "01a0df61-dc8c-7c71-8ee4-6320f646b0c5", "status": "COMPLETED", "workflow_type": "ExistingGitHubBacklogLoopWorkflow"}}
👤 /status backlog-loop-musematic-66a97948-…-27604282-30bc-4052-8dc1-12f92bcd7dea-issue-1-staging-gitops-retry-1
🤖 {"next_cursor": null, "payload": {"workflow_id": "…-issue-1-staging-gitops-retry-1", "run_id": "01a0e2ee-14b1-74f3-b5cf-7cdf47ac0784",
     "status": "COMPLETED", "workflow_type": "StagingGitOpsDeliveryWorkflow"}}
```

**musematic-deploy variant.** When the ChangeSet also changes `gntik-ai/musematic-deploy`, the deployment maker produces a second PR. The GitOps flow merges source first, then the deployment PR (D:2143-2161). Image pinning and the Argo sync always target the deploy repo / Argo app, whether or not a deployment PR exists. While the GitOps flow waits on a GitHub write that the policy denies, the operator receives [reconstructed from `activities/engineering.py:1942-1945`]:

```
🤖 Hermes is waiting for a GitHub write approval (POLICY_DISABLED) for gntik-ai/musematic#1125 at gitops.merge.
   Approve the campaign; the flow re-checks every 5 minutes and resumes by itself.
   commands: /opt/engineering-control/venv/bin/engineering-ctl backlog-approval-candidate musematic 66a97948-… full-delivery R2 --max-items 1 …
```

The Telegram equivalent is example 8.

---

## 4. Batch: several llmwiki issues, monitor, cancel one

**Parallel execution is not supported**: loops run one issue at a time (`maximum_concurrency = 1`, `db/repository.py:623`). A second loop on the same campaign is refused (`BACKLOG_CAMPAIGN_ALREADY_ACTIVE`), and so is a second loop on the same issue (`ISSUE_WORKFLOW_ALREADY_ACTIVE`). The batch form is a sequential loop [reconstructed]:

```
👤 /loop start llmwiki <campaign-uuid> R1 source-pr 3
🤖 {"payload": {"action": "solve_backlog", "status": "COMPLETED", "workflow_id": "backlog-loop-llmwiki-<campaign>-<uuid>", …}}
👤 /loop tail backlog-loop-llmwiki-<campaign>-<uuid> 20
🤖 {"payload": {"items": [ … "workflow.started" … "provider.execution_started" … ]}}
```

"Cancel one" means skipping the current issue once the loop pauses on it. The whole loop can also be cancelled:

```
👤 /loop skip backlog-loop-llmwiki-<campaign>-<uuid> llmwiki 8 ENHANCEMENT_DEFERRED
👤 /loop cancel backlog-loop-llmwiki-<campaign>-<uuid>
🤖 {"payload": {"action": "cancel", "status": "COMPLETED", …}}   — campaign CANCELLED, leases released (NEW-60)
```

`/loop skip` only takes effect while the loop waits on exactly that issue (the signal value must match `gntik-ai/llmwiki#8`). Otherwise it is ignored (D:3326-3330).

---

## 5. Audit: static then dynamic on falcone (charts), findings, verify, publish

Audits are per **project**; `falcone-charts` is the falcone project's deployment surface and is always included. There is no repo-only scope [reconstructed]:

```
👤 /audit-plan falcone static-full staging
🤖 {"payload": {"action": "audit_plan", "status": "COMPLETED", "workflow_id": "audit-plan-falcone-<uuid>", …}}
👤 /audit falcone static-full staging --execute
🤖 {"payload": {"action": "audit_start", "status": "COMPLETED", "workflow_id": "audit-falcone-<uuid>", …}}
```

Without the plan step the start is refused with `safe_error: operation preflight changed: AUDIT_PLAN_REQUIRED`. Dynamic (runtime) audit — adds live API/browser/Kubernetes stages and needs the superadmin/observer identities:

```
👤 /audit-plan falcone e2e-smoke staging
👤 /audit falcone e2e-smoke staging --execute
👤 /campaigns 5            → campaign ids and status
👤 /campaign <campaign-uuid>
👤 /findings 20            → verified findings
👤 /verify <finding-uuid>  → {"payload": {"workflow_id": "finding-verify-…", "status": "ACCEPTED"}}
```

Publishing as a GitHub issue: re-run with `--publish-findings`, or publication happens inside the campaign for CONFIRMED findings (D:674-691). Findings become ordinary GitHub issues (`finding.publish`), not OpenSpec issues. OpenSpec documents are created for deliveries (`delivery.create_openspec`) and enhancements.

---

## 6. Multi-project audit with a scope filter

`/audit-all` runs falcone, musematic and llmwiki in sequence. The only scope controls are the preset and `--publish-findings`; project, domain or persona filters are not available [reconstructed]:

```
👤 /audit-all plan security staging
🤖 {"payload": {"action": "audit_all_projects", "status": "COMPLETED", …}}   (3 plan-only runs)
👤 /audit-all start security staging
🤖 {"payload": {"action": "audit_all_projects", "status": "COMPLETED", "workflow_id": "<multi-project-workflow-id>", …}}
👤 /status <multi-project-workflow-id>
```

The consolidated result is the parent's `project_results` per project (D:861-878), read with `/status` or `/campaign <id>` for each child. For a narrower scope use the advanced single-project form: `/audit falcone <name> <source-sha> <deployment-sha> <domain> <persona> <lens> staging --execute` (one coverage cell).

Note: musematic's security/e2e presets currently risk failing at `identity.superadmin_attest`, because every cleanup run on 2026-09-27 reports `SUPERADMIN_AUTHENTICATION_FAILED` for musematic [real run].

---

## 7. Enhancement scan on musematic → approve → deliver

```
👤 /enhancements list musematic
🤖 {"next_cursor": null, "payload": {"items": [], "next_cursor": null}}   [real API — no enhancements yet]
👤 /enhancements discover musematic <audit-campaign-uuid>             → dry run (load evidence + prepare only)
👤 /enhancements discover musematic <audit-campaign-uuid> --execute
🤖 {"payload": {"workflow_id": "…", "run_id": "…", "status": "ACCEPTED"}}   [reconstructed]
👤 /enhancements list musematic CANDIDATE_ENHANCEMENT
👤 /enhancements show enh-<hex>                                     → includes the approval binding
👤 /enhancements approve enh-<hex> <binding-sha256>
👤 /enhancements run enh-<hex> R1 source-pr --execute
```

"Publish" (turning the approved enhancement into a GitHub issue with its OpenSpec) is **not reachable**: `EnhancementDeliveryWorkflow` has no API route or command (see `docs/inventory/flows.md`). `/enhancements run` goes straight to delivery (EnhancementSolutionLoop → IssueSolutionLoop → IssueDelivery → source PR).

---

## 8. GitHub write policy: request, approve, expiry, extend

```
👤 /loop approval-candidate musematic 66a97948-9f9d-4ae9-b204-e89f27e2712d full-delivery R2 1
🤖 {"payload": {"status": "AWAITING_APPROVAL", "backlog_campaign_id": "66a97948-…",
     "approval_binding": "<64-hex>", "approval_candidate": {…, "expires_at": "…"},
     "typed_confirmation": "APPROVE_EXISTING_GITHUB_BACKLOG_LOOP"}}   [shape from db/repository.py:627-633]
👤 /loop approve musematic 66a97948-9f9d-4ae9-b204-e89f27e2712d <64-hex> APPROVE_EXISTING_GITHUB_BACKLOG_LOOP
🤖 {"payload": {… "status": "APPROVED_POLICY_PENDING" …}}   then the root reconciler activates the policy → APPROVED
```

- **Check expiry**: `/campaign 66a97948-…` (approval binding `expires_at`); the approval lasts 24 h and one campaign owns the policy at a time.
- **Extend**: there is no extend command. Run approval-candidate + approve again, which requires the previous binding to have expired or to belong to the same campaign. The other errors are `another backlog campaign policy activation is pending` and `existing-backlog approval candidate expired`.
- **Release early**: CLI only (`engineering-ctl backlog-release-ownership`).
- `/loop issue-start` and `/loop start` perform approval-candidate + approve themselves.

---

## 9. Operations

```
👤 /providers        → codex/claude authenticated, rate_limited, status   [real API sample in commands.md]
👤 /usage            → 7-day tokens per role, e.g. claude code-reviewer 48 invocations / 48 successes   [real API]
👤 /ready core       → {"status": "DEGRADED", "connected_count": 21, "connection_count": 23, …}   [real API]
👤 /status <id>      → why a run is BLOCKED_* : read "status" and, for loops, /loop tail <id> 50 (phase AWAITING_OPERATOR:<issue>:<BLOCKER>)
👤 /loop retry <loop-id> musematic 1125           → retries the paused issue
👤 /pause <workflow-id>   /resume <workflow-id>
👤 /export-workflow <workflow-id>
🤖 {"payload": {"action": "evidence_export_workflow", "status": "COMPLETED", "operation_id": "<uuid>", "result_available": true, …}}
```

The export body is fetched with the CLI `engineering-ctl operation-export <operation-id>`; there is no Telegram command for it.

---

## 10. Failure handling

| Situation | What the operator sees | Next command |
|---|---|---|
| Engine rate limit before start | `/loop issue-start …` → `status: BLOCKED`, `safe_error: operation preflight changed: CODEX_RATE_LIMITED` (or `CLAUDE_…`) [reconstructed, operations/repository.py:1144-1158] | `/providers`, `/usage`; retry after reset |
| Engine rate limit during a delivery | child IssueDelivery returns `RATE_LIMITED`; the loop pauses: `/loop tail` shows phase `AWAITING_OPERATOR:gntik-ai/<repo>#<n>:RATE_LIMITED` | `/loop retry <loop-id> <repo> <n>` |
| PR has failing CI | GitOps flow `SOURCE_PR_CI_REGRESSION` → up to 2 automatic CI-repair re-deliveries (NEW-64), then pause with `CI_REPAIR_<reason>` or `SOURCE_PR_CI_FAILED` | fix or `/loop retry …` (repeats the CI repair) / `/loop skip … NEEDS_PRODUCT_DECISION` |
| GitHub write denied | the write-policy alert (example 3) | example 8 |
| Workflow timeout | child `TIMED_OUT`, loop pauses on the issue; e.g. `SOURCE_PR_CI_TIMEOUT` after 90 min, `ARGO_SYNC_TIMEOUT` | `/loop retry …`; `/cancel <id>` for a hung non-loop workflow |

---

## 11. Model / effort override per run

**Not supported.** No command accepts a model, effort or engine argument. The engine is chosen by the quota broker; effort is fixed per role (`models/core.py:89-109`); models come from worker settings (`ENGINEERING_CODEX_MODEL`, `ENGINEERING_CLAUDE_SPEC_MODEL`, `ENGINEERING_CLAUDE_CHECKER_MODEL`) — see `docs/inventory/models.md`. Adding one would take three changes: a field on the typed commands (`operations/models.py`), a parameter passed through `ExistingGitHubBacklogLoopInput` → `IssueDeliveryInput`, and use in `_prepare_*` (`activities/metadata.py`).
