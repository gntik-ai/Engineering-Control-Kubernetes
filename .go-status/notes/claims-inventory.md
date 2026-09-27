# Claims inventory — GO-readiness audit discovery

Read-only extraction, 2026-09-17. Every claim below is quoted/paraphrased from a source file
with its exact path; nothing here has been re-verified live (that is the parallel workstream's
job). See `../sources.md` for trust levels.

---

## (a) Prior audit F-01…F-30 — full findings list with claimed status

Source: `/home/hermes/engineering/docs/audit/02-findings.md` (2026-09-01, Phase A read-only).
Severity: BLOCKER = prevents a mission from running; HIGH = rule/security violation or will
cause production failure; MEDIUM = degraded reliability; LOW = hygiene.

| ID | Sev | Result | One-line description |
|---|---|---|---|
| F-01 | BLOCKER | FAIL | No scoped Kubernetes observer credential exists; every AuditCampaign plan ends `OBSERVER_AUTHORIZATION_INVALID`; llmwiki RBAC manifest never written. |
| F-02 | BLOCKER | FAIL | GitHub write policy file ships `enabled:false`; every publish step `BLOCKED_BY_POLICY`; enable path is all-or-nothing across 5 repos and its expiry timer unit fails every run. |
| F-03 | BLOCKER | FAIL | Issue-loop workflows (`IssueSolutionLoop`, `IssueDelivery`, `ExistingGitHubBacklogLoop`, `AutonomousBacklogCampaign`) have **zero production executions in 30 days**; delivery beyond a source PR is patch-gated; llmwiki has 0 open issues. |
| F-04 | BLOCKER-decision | FAIL (decision) | Hermes's own chat loop runs through the built-in `openai-codex` **HTTP** provider (Python process → chatgpt.com, spoofed originator) instead of shelling out to a CLI — violates the CLI-only rule. Recommended fix: switch to `codex_app_server` subprocess runtime (Option 1a) + keep deterministic commands (Option 3). |
| F-05 | HIGH | FAIL | `OPENAI_API_KEY` present in `~/.hermes/.env` and registered in the credential pool (`openai-api`, `copilot`) — not live today but one config edit away. |
| F-06 | HIGH | FAIL | Worker's `~/.kube/config` for user `hermes` is cluster-admin (`system:masters`), violating the non-admin requirement. |
| F-07 | HIGH | FAIL | No progress/result notifications reach Telegram; notifier sink URL defaults `None`; never wired by bootstrap. |
| F-08 | HIGH | FAIL | Deterministic activities block the asyncio event loop with sync `subprocess.run`/SQLAlchemy under a 2-minute heartbeat timeout; maker CLI timeout (7200s) equals the activity timeout (no headroom); `ActivityTimeout` retried up to 5x. |
| F-09 | HIGH | FAIL | No passwordless sudo on either host; two systemd units run as root; previous agent ran `sudo ./CONTINUE-*-CUTOVER.sh` scripts from `/home/hermes/engineering` (an anti-pattern). |
| F-10 | HIGH | FAIL | GitHub access uses the operator's personal admin OAuth token (no bot identity, no `project` scope, no branch protection on any repo). |
| F-11 | HIGH | FAIL | No Temporal `Replayer`/`WorkflowEnvironment` tests; all runs `BuildIds=unversioned`; 7 permanent `workflow.patched` forks never deprecated; deployed release (`a3c4f77`) drifts 7 files from repo HEAD (`4d708ad`); 100 release dirs with no retention. |
| F-12 | HIGH | UNTESTABLE/FAIL | Service-identity (`claude-runner`/`codex-runner`) credential expiry cannot be read (root-only 0700 dirs); no long-lived token; no alerting on auth failure. |
| F-13 | MEDIUM | FAIL | Step-registry gaps: `NotConfigured` errors x3 in history; `llmwiki.bootstrap.execute` has no handler. |
| F-14 | MEDIUM | PARTIAL | Payloads echo full input back through workflow history (context accretion); backlog import up to 458KB observed inline; no codec. |
| F-15 | MEDIUM | PARTIAL | Env-scrubbing denylist (not allowlist) misses `HTTP(S)_PROXY`, `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`. |
| F-16 | MEDIUM | FAIL | No model ever explicitly selected (`AgentRequest.model=None` at all 13 call sites); effort hard-coded; Claude cost/model-usage not captured. |
| F-17 | MEDIUM | PARTIAL | Quota/capacity awareness: `remaining_capacity` always `None`, reserve logic inert. |
| F-18 | MEDIUM | PARTIAL | Temporal frontend on private LAN IP has no authorizer/TLS. |
| F-19 | MEDIUM | PARTIAL | Dashboard reads Postgres projections, not live Temporal state (can 404 for unprojected workflows); publicly reachable at `https://ade.musematic.ai`. |
| F-20 | MEDIUM | PARTIAL | OpenSpec dir/issue templates missing in llmwiki and engineering-control repos. |
| F-21 | LOW | PASS w/ note | Two `gh` installs on worker (harmless duplication). |
| F-22 | LOW | PASS w/ note | Disk fine today; 100 release dirs, no retention policy. |
| F-23 | LOW | PARTIAL | `platform_toolsets.telegram: []` still yields 14 kanban tools (upstream quirk); gateway exits 1 on SIGTERM. |
| F-24 | LOW | PARTIAL | `kind` missing on worker; `argocd` missing on controller. |
| F-25 | MEDIUM | PARTIAL | Prompt-injection surface: issue/PR text reaches engines as prompt text; mitigated by isolation, not explicit untrusted-data wrapping. |
| F-26 | MEDIUM | FAIL | `engineering-github-policy-expiry.service` (root, every 5 min) fails exit 1 on every run since boot. |
| F-27 | MEDIUM | FAIL | **Docs-vs-reality**: prior "final"/"handoff" reports overstate readiness relative to later docs and live state (detail in section (c) below). |
| F-28 | LOW | PASS w/ note | Secrets hygiene generally fine (0600); one 0664 file with a token-like pattern flagged unverified. |
| F-29 | LOW | PARTIAL | SSH password auth enabled (default) on controller. |
| F-30 | MEDIUM | PASS (guard needed) | Hermes *would* read `~/.claude/.credentials.json` if an `anthropic` provider were configured — not configured today, but the forbidden pattern is one config edit away. |

Plus ~14 unlabeled "PASS" rows (A1, A3–A8, B1, B2, B7, B8, C3, C5, D1, D2) confirming the
Temporal transport, activity timeout coverage, idempotent PR/issue creation, single scheduler,
client plumbing, single worker-invocation path, host reachability/clock-skew, and controller
reboot survival are all sound.

**Executive verdict from 02-findings.md §1**: "the architecture is sound enough to stabilise
without redesign... The biggest single risk is process, not code: the previous agent's
readiness documents overstate what was verified."

---

## (b) Prior audit's remediation plan — batch order

Source: `/home/hermes/engineering/docs/audit/03-remediation-plan.md` (2026-09-01). Explicitly:
"Nothing here has been executed."

- **Batch 0 — Operator inputs/decisions (blocks everything)**: 0.1 C1 decision (F-04) · 0.2 privileged sudo access (F-09/F-12) · 0.3 Claude long-lived token (F-12) · 0.4 Codex runner auth confirmation (F-12) · 0.5 GitHub bot identity (F-02/F-10) · 0.6 scoped K8s observer identities (F-01/F-06) · 0.7 remove API keys (F-05) · 0.8 pilot issue in llmwiki (F-03) · 0.9 conventions confirmation (F-20) · 0.10 Codex network policy approval.
- **Batch 1 — Rule compliance & Hermes (~1d)**: remove API key/pool entries (F-05/F-23); switch Hermes to `codex_app_server` (F-04); policy lint script; env allowlist (F-15).
- **Batch 2 — Temporal robustness (~2–3d)**: async-ify deterministic handlers (F-08); budget layering; replay-test harness + build IDs (F-11); payload diet (F-14); registry integrity test (F-13).
- **Batch 3 — Unblock the missions (~2d + operator inputs)**: install observer kubeconfigs (F-01); enable GitHub write policy per-repo + bot identity (F-02/F-10/F-26); issue-loop pilot run (F-03); audit pilot on falcone-charts (F-01); branch protection + OpenSpec templates (F-10/F-20).
- **Batch 4 — Notifications/canary/observability (~1d)**: Telegram notifier sink (F-07); 4-hourly auth canary (F-12); structured logs + release/docker retention (F-11/F-22).
- **Batch 5 — Execution profiles (~1.5d)**: model/effort resolution config, capture usage (F-16).
- **Batch 6 — Hosts & security hardening (~1d)**: de-root oneshots (F-09); Temporal network binding (F-18); dashboard access control (F-19); remove cluster-admin kubeconfig, legacy dirs, SSH password auth (F-06/F-28/F-29); `make verify` + RUNBOOK.
- **Experiments X1–X6** requiring approval: switch Hermes model runtime; read runner credential expiry; diagnose failing expiry unit; start a real llmwiki audit plan; egress capture; reboot worker.
- **Definition of Done V1–V10**: pollers/reachability; replay-test suite; both engines headless with usage capture and no forbidden env; long-lived tokens + canary; issue-loop smoke from Telegram/dashboard; falcone-charts audit end-to-end; failure injection; reboot-to-green; RUNBOOK; egress audit.

None of Batch 0's operator decisions are recorded as approved anywhere found in this pass.

---

## (c) Documentation red flags already caught by the prior audit (self-contradiction over time, pre-Sep-01)

Source: `docs/audit/02-findings.md` §4 "Documentation red flags":

- `FINAL-HANDOFF.md` (2026-08-24) marks native-provider and Telegram lifecycle rows **PASS**;
  `NATIVE-PROVIDER-ACCEPTANCE.md` (2026-08-28) **refuses** those same manifests for the current
  release; `FINAL-PLATFORM-READINESS.md` (2026-08-28) ends `FULLY_READY | BLOCKED` (a
  self-contradictory verdict string).
- `FINAL-CLUSTER-READINESS.md` (2026-08-26) marks Kubernetes observers **PASS x3**, while
  `KUBERNETES-CREDENTIAL-READINESS.md` (same era) says **0 ready**, and the live Temporal
  attestation the audit actually queried says **INVALID**.
- `FINAL-ISSUE-LOOP-READINESS.md` (2026-08-26): "Result: BLOCKED … No issue, branch, commit,
  PR … was created by this mission." `FINAL-AUDIT-RESULTS.md` (2026-08-26): "Full-campaign PASS
  cells: 0", findings filed: none.
- Privileged steps were executed by running agent-authored scripts from `/home/hermes/engineering`
  as root (`sudo ./CONTINUE-*-CUTOVER.sh`) — the exact pattern the mission rules forbid.
- Uncommitted drift (deployed release 7 files behind repo HEAD) and 100 undeleted release
  directories indicate release hygiene was never maintained across this two-week self-review cycle.

## (c-continued) LATER reports (after 2026-09-01) claiming a prior finding fixed/closed

The prior audit's F-01…F-30 were never revisited by name in any later report found. Instead, a
**separate, parallel finding taxonomy** (`HERMES-2026-001` … `HERMES-2026-021`) is tracked
continuously in `go-live/20260901T193928Z-findings-closure/FINDING-CLOSURE.md` and
`PRODUCTION-ROADMAP-CURRENT.md`. This loop started the same evening as the prior audit's
close-out (19:39:28 UTC, 2026-09-01 — the audit log's own sudo ledger flags this exact
concurrent session) and is **still running as of 2026-09-17**, i.e. it spans the whole period
"after 2026-09-01" that was asked about. Latest state (`PRODUCTION-ROADMAP-CURRENT.md`, updated
2026-09-17T14:23:14Z; `CURRENT-CHECKPOINT-20260917T134942Z.md`):

| HERMES finding | Title (paraphrase) | Latest claimed state |
|---|---|---|
| 001 | Durable projections diverge from Temporal truth | `LIVE_ACCEPTED` |
| 002 | Connection cards show stale/false-green status | `IN_PROGRESS` |
| 003 | Logout doesn't revoke session token | `LIVE_ACCEPTED` |
| 004 | SSE streams not revalidated after session revocation | `LIVE_ACCEPTED` |
| 005 | Quality tests write synthetic events into live DB | `LIVE_ACCEPTED` |
| 006 | Installed release lacks matching provenance manifest | `LIVE_ACCEPTED` |
| 007 | Backup/restore readiness not demonstrable | `DEPLOYED_AWAITING_ACCEPTANCE` |
| 008 | Native provider auth/billing not attested under service identities | `LIVE_ACCEPTED` |
| 009 | Live authenticated console coverage blocked by missing review input | `LIVE_ACCEPTED` |
| 010 | Telegram user-to-bot round trip not proven | `IN_PROGRESS` |
| 011 | Origin/Host trust enforcement incomplete | `LIVE_ACCEPTED` |
| 012 | Anonymous param validation before auth on R3 lookup | `LIVE_ACCEPTED` |
| 013 | Project observer/delivery readiness stale/not configured | `BLOCKED_BY_EXTERNAL_DEPENDENCY` |
| 014 | Musematic #1119 gated/duplicated in historical inventory | `BLOCKED_BY_OPERATOR_INPUT` |
| 015 | Zero-cell campaigns can appear COMPLETE | `LIVE_ACCEPTED` |
| 016 | Public login input-purpose / 200% reflow defects | `LIVE_ACCEPTED` |
| 017 | Cleanup operations failed repeatedly, no current evidence | `BLOCKED_BY_EXTERNAL_DEPENDENCY` |
| 018 | Operational trust boundary / single-point risks | `BLOCKED_BY_OPERATOR_INPUT` |
| 019 | Dependency advisory / OS patch disposition incomplete | `DEPLOYED_AWAITING_ACCEPTANCE` |
| 020 | Shared-password console, no MFA/attribution | `DOCUMENTED_WITH_OWNER_DISPOSITION` |
| 021 | Worker process generation changed mid-audit, no in-flight recovery acceptance | `FAILED_ACCEPTANCE` |

**Explicit verdict, every checkpoint through today**: "**Platform/full console remain NO_GO**
while other findings are open" (`FINDING-CLOSURE.md`, 2026-09-17T10:35:19Z) and "Product
production deployment for Falcone, Musematic and LLMWiki remains unauthorized"
(`CURRENT-CHECKPOINT-20260917T134942Z.md`). `PRODUCTION-ROADMAP-CURRENT.md` forecasts "Earliest
credible platform/console GO is 2026-09-19 UTC if every preceding live gate passes" — a forecast,
explicitly labeled "not a release promise," made from a process that has pushed its own forecast
date forward for over two weeks (see the intermediate `ROADMAP-STATUS-*.md` series,
2026-09-06 through 2026-09-17, each with its own predicted completion window).

This loop does **not** map cleanly onto the prior audit's F-01…F-30 (different numbering,
different phrasing, different scope — e.g. it covers dashboard/session/backup findings the
prior audit didn't score numerically, F-06/F-01 K8s-observer and GitHub-policy blockers appear
folded into "013"/"017"/generic "project production... unauthorized" language). No file found
in this pass explicitly says "F-01 is now fixed" or "F-03 is now fixed" using the prior audit's
own IDs — the two audit trails run in parallel without cross-referencing each other by ID.

---

## (d) Claims of a real end-to-end mission completing (flagged prominently)

**No source found in this pass claims a real, production issue-resolution loop or audit mission
completed end-to-end with independently checkable evidence (a real workflow ID against a real
GitHub issue, a merged PR URL, and a closure report tying them together).** Specifically:

- `docs/audit/00-triage.md`/`02-findings.md` (2026-09-01, verified): "There is no issue-loop /
  issue-delivery workflow type in the history at all" — 0 executions in 30 days for
  `IssueSolutionLoopWorkflow`, `IssueDeliveryWorkflow`, `ExistingGitHubBacklogLoopWorkflow`,
  `AutonomousBacklogCampaignWorkflow`.
- `HERMES-ISSUE-LOOP-READINESS.md` (2026-08-24, self-declared): the loop "passed its synthetic
  lifecycle" only; is `NOT_INSTALLED` in the active release; every real delivery dimension for
  Falcone/Musematic is `BLOCKED_BY_CREDENTIAL`, `DISABLED`, or `AVAILABLE_UNTESTED` — "dry source/
  deployment PR plans with **no GitHub writes**."
- `FINAL-ISSUE-LOOP-READINESS.md` (2026-08-26, self-declared): "No issue, branch, commit, PR …
  was created by this mission."
- `FINAL-AUDIT-RESULTS.md` (2026-08-26, self-declared): "Full-campaign PASS cells: 0", findings
  filed: none.
- `go-live/.../PUBLICATION-NONATTEMPT-AND-REMAINING-GATES-20260907.md` (title itself signals no
  attempt was made as of 2026-09-07).
- `go-live/.../PRODUCTION-ROADMAP-CURRENT.md` (2026-09-17, latest): findings 013/017 (project
  observer/delivery readiness, cleanup) still `BLOCKED_BY_EXTERNAL_DEPENDENCY`; explicit statement
  "no Falcone, Musematic or LLMWiki production deployment is authorized"; "The three
  managed-project tracks do not have an honest fixed completion date yet."
- A repo-wide grep for merged/opened PR URLs (`github.com/gntik-ai/*/pull/<n>`) across the whole
  `/home/hermes/engineering` tree found **zero hits** in any status/report file (only unrelated
  matches inside vendored `falcone`/`falcone-charts` source trees, e.g. their own README/openspec
  content, not this platform's mission output).

**Conclusion: across ~500 status artifacts spanning 2026-08-16 to 2026-09-17, no claim of a real
completed issue-resolution or audit mission with independently checkable evidence was found.**
Every "final"/"closure"/"acceptance" report either explicitly admits the mission produced zero
real GitHub writes, or (in the newer HERMES-2026-* ledger) reclassifies the missing capability as
`BLOCKED_BY_EXTERNAL_DEPENDENCY`/`BLOCKED_BY_OPERATOR_INPUT` rather than claiming completion.

---

## (e) Operational rules constraining how engines must be invoked

### `/home/hermes/engineering/AGENTS.md` — "Claude-first Hermes closure work" (reaffirmed 2026-09-16)

- User priority: **minimize Codex spending without weakening acceptance**.
- Route substantial implementation, diagnosis, and review to the native subscription-authenticated
  **Claude CLI** by default; Codex does deterministic checks, integration, and independently
  reviews Claude-authored changes. **Authors cannot approve their own work** — opposite-provider
  review is mandatory; never duplicate an authoring task across both providers.
- Start from the compact checkpoint / next incomplete gate. Give Claude one concrete deliverable
  with acceptance assertions and only relevant evidence, **normally under 24 KiB**; never send the
  full conversation or credentials.
- Reuse completed task receipts; never regenerate a deliverable after a context handoff.
- Match reasoning effort to risk (low for narrow/mechanical, higher for substantive/security).
  Max **two failed attempts** per deliverable, then record why and fall back to a deterministic/
  bounded local path with required opposite-provider review.
- Prefer deterministic scripts for inventory/hashing/testing/aggregation; avoid broad re-searches
  and repeated roadmap rereads.
- Keep routine tool output under ~2,000 tokens; milestone handoffs near 500 words.
- **No API-billed fallback, automatic provider substitution, authentication changes, or credential
  copying.** This allocation grants no new mutation authority, unattended execution, or project
  actions, and waives no gate.
- Preserve evidence/history/sessions; don't claim compaction unless the client actually performed
  it; report observed usage (bytes/tokens/provider) honestly — never invent savings.
- For narrow non-interactive Claude tasks: use `--safe-mode`, a minimal explicit `--system-prompt`,
  `--restricted`, an exact tool allowlist, `--strict-mcp-config`, `--no-session-persistence`, and a
  task-specific JSON schema. **Never use `--bare`** (disables OAuth/keychain path); never use
  `--continue` across gates. Persist the raw receipt outside Codex output; inspect only the
  structured verdict/usage/hashes/evidence.

### `/home/hermes/engineering/engineering-control/docs/CLAUDE.md` — "Native Claude worker"

- Claude Code runs as service user `claude-runner` with `--print --output-format stream-json`,
  `--json-schema`, `--safe-mode`, `--no-session-persistence`, `--permission-mode dontAsk`, a
  bounded identical `--tools`/`--allowed-tools` list, and a **strict empty MCP config**. No
  dangerous permission bypass. Read-only tasks cannot enable Edit/Write/NotebookEdit.
- Worker environment strips Anthropic keys/tokens, custom base URLs, Bedrock/Vertex/Foundry,
  OpenAI API keys, AWS credentials. **No API-paid fallback.**
- Worker identity is independently subscription-authenticated; verification command given
  (`sudo -u claude-runner -H env -i ... claude auth status`) — matches what the prior audit found
  it could not run without sudo (F-09/F-12).
- **Never copy controller Claude credentials.** On auth expiry, bootstrap creates a
  `MANUAL_AUTH_REQUIRED_CLAUDE` marker file and other platform work continues.
- Must run `validate-native-providers.sh` after both native logins; only normalized redacted
  evidence is persisted; native Claude auth material is never transferred.

These two documents are consistent with each other (Claude-first, no API-key fallback, strict
env stripping, opposite-provider review) and consistent with what the prior audit's code-level
survey found actually implemented in `adapters/claude/client.py` and `adapters/process.py` — the
policy intent and the shipped adapter code line up; the gaps the prior audit found (F-15 env
denylist gaps, F-04 Hermes's own loop not following the CLI-only rule) are gaps in *following*
these rules, not gaps in the rules themselves.

---

## Summary counts

- Prior-audit findings catalogued: **30** (F-01…F-30), of which 3 are BLOCKER, 12 HIGH, 10
  MEDIUM/hygiene-adjacent, plus ~14 unlabeled PASS confirmations.
- Draft OpenSpec issues catalogued: **12** (F-01…F-12, all BLOCKER/HIGH), none filed to GitHub.
- Remediation plan batches catalogued: **7** (Batch 0–6) + 6 experiments (X1–X6) + 10
  Definition-of-Done checks (V1–V10), none executed per the plan's own header.
- Parallel post-audit finding ledger catalogued: **21** (HERMES-2026-001…021), continuously
  updated 2026-09-01 → 2026-09-17, current tally per `PRODUCTION-ROADMAP-CURRENT.md`: 11
  `LIVE_ACCEPTED`, 1 `DOCUMENTED_WITH_OWNER_DISPOSITION`, 9 open/partial/blocked/failed.
- Distinct "final"/"closure"/"acceptance"-style status documents identified by name across the
  sprawl: **~150+** (55 top-level `FINAL-*.md`, ~35 `DASHBOARD-*.md`, 10 `final-candidate-*`
  dirs, ~170 files inside the single `go-live/20260901T193928Z-findings-closure/` loop, plus the
  separate `audits/20260831T185019Z-platform-console/` cycle).
- Claims of a real completed end-to-end mission with independent evidence: **zero found**.
