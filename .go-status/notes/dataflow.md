# Engineering Control — Mission Data-Flow Trace (code archaeology)

Read-only trace of `/home/hermes/engineering/engineering-control` at branch `go-status/2026-09-17`.
Language/framework confirmed from `pyproject.toml`: Python 3.12, `temporalio>=1.16,<2` (Temporal
Python SDK), FastAPI, SQLAlchemy/psycopg, single package `src/engineering_control`.

All file:line references are exact for this checkout unless noted "runtime-dependent" (i.e. depends
on data not present in this checkout — a live Postgres row, a live Temporal history, or a file under
`/etc|/srv|/var/lib` that `hermes` cannot read here: confirmed `permission denied`, no sudo).

---

## Mission 1 — Issue-resolution loop (GitHub Projects trigger → branch/PR)

Core single-issue engine is `IssueDeliveryWorkflow`; the "loop" wrapper that walks a backlog and
calls it repeatedly is `IssueSolutionLoopWorkflow` / `ExistingGitHubBacklogLoopWorkflow` /
`AutonomousBacklogCampaignWorkflow`.

| Hop | Component | Exact file:line | What it does |
|---|---|---|---|
| 0 | Hermes skill (trigger boundary, outside this repo) | `/home/hermes/.hermes/skills/implement-issue/SKILL.md`, `/home/hermes/.hermes/skills/issue-delivery/SKILL.md` | `implement-issue` skill does its own discovery/spec/implementation work as an interactive agent session (not via this repo), then says it must "invoke `/issue-delivery`"; `issue-delivery` SKILL.md states explicitly it has "no authority to run shell, SSH, Git push, GitHub writes... or provider CLIs" and instead "submit[s] only a typed `/run` request through the fixed Engineering Control Telegram/API adapter." This is the seam between free-form Hermes-agent work and the bounded control plane below. |
| 1 | HTTP trigger endpoint | `src/engineering_control/api/main.py:550` `campaign_start`/`:620` `issue_loop_start`/`:824` `backlog_delivery_approve` | FastAPI routes behind `auth_dependency` (bearer token, see Gates). `POST /v1/loops` → `service.start_issue_loop(body)`; `POST /v1/campaigns` → `service.start_campaign`; `POST /v1/backlog-loops/{id}/approve-delivery` (would gate full-delivery) |
| 2 | Temporal client `start_workflow` | `src/engineering_control/api/service.py:394-400` (`start_issue_loop`) and `:190-197` (`start_delivery`) | `self.client.start_workflow(WorkflowName.ISSUE_SOLUTION_LOOP.value, ..., task_queue=self.task_queue)` — workflow type name literal `"IssueSolutionLoopWorkflow"` (`src/engineering_control/workflows/names.py:20`); `self.task_queue` = `CONTROL_TASK_QUEUE = "engineering-control"` (`src/engineering_control/workflows/worker_registry.py:20`) |
| 3 | Temporal workflow — loop driver | `src/engineering_control/workflows/definitions.py:1640-1762` `IssueSolutionLoopWorkflow.run` | Loads eligible items (`loop.load_selection`), then for each item calls `workflow.execute_child_workflow("IssueDeliveryWorkflow", IssueDeliveryInput(...), task_queue=workflow.info().task_queue, ...)` at `definitions.py:1675-1685` |
| 4 | Temporal workflow — single-issue engine | `src/engineering_control/workflows/definitions.py:860-918` `IssueDeliveryWorkflow.run` | Ordered step list at `:865-885`: `delivery.ensure_issue → acquire_leases → route_providers → allocate_worktrees → deterministic_prechecks → prepare_spec → invoke_spec → create_openspec → prepare_maker → invoke_maker → prepare_deployment_maker → invoke_deployment_maker → prepare_checker → invoke_checker → validate_changeset → prepare_publication → publish → persist_publication` |
| 5 | Activity routing (per-step task queue) | `src/engineering_control/workflows/definitions.py:218-345` `ControlledWorkflow._activity_queue` | Maps each step name to a role task queue, e.g. `delivery.invoke_maker` → `provider_task_queue(maker_provider, default="codex")` (`:290-293`) → `"engineering-codex"`; `delivery.invoke_checker` → `"engineering-claude"` (`:298-301`); `delivery.allocate_worktrees`/`deterministic_prechecks`/`validate_changeset` → `"engineering-scan"` (`:229-249`); `delivery.publish` → `GITHUB_TASK_QUEUE = "engineering-github"` (`:319-320`) |
| 6 | Single Temporal activity call | `src/engineering_control/workflows/definitions.py:140-150` `ControlledWorkflow._step` | Every step is the **same** activity type name `"execute_control_step"`, dispatched with `{"step": step, "payload": payload}` |
| 7 | Runner process / Unix user | `src/engineering_control/workflows/worker_registry.py:34-86` `ROLE_WORKER_REGISTRY` | e.g. `codex` role: `unix_user="codex-runner"`, `service="engineering-worker@codex.service"`, `task_queue="engineering-codex"`; `claude` role: `unix_user="claude-runner"`, service `engineering-worker@claude.service`; `github` role: `unix_user="hermes"`, service `engineering-github-worker.service`, host `"controller"` (all others host `"worker"`) |
| 8 | Activity dispatcher entry point | `src/engineering_control/activities/core.py:460-461` `ControlActivities.execute_control_step` (`@activity.defn(name="execute_control_step")`), constructed per worker kind at `:416-454` | Routes `step` to a handler instance chosen by `worker_kind` at construction: `codex`/`claude` → `ProviderActivityHandler`; `github` → `GitHubActivityHandler`/`DeliveryActivityHandler`; `scan` → `ScanActivityHandler`, etc. |
| 9 | Provider (agent) activity handler | `src/engineering_control/activities/provider.py:100-132` `ProviderActivityHandler.handle` | For `delivery.invoke_maker`/`delivery.invoke_checker` etc. builds an `AgentRequest`, checks `self.adapter.authenticated()`, then calls `self.adapter.execute(request, on_event=...)` |
| 10 | Engine CLI subprocess — Codex (maker) | `src/engineering_control/adapters/codex/client.py:61-93` `CodexCLIAdapter.execute` | See "Engine invocation" section below |
| 10' | Engine CLI subprocess — Claude (checker) | `src/engineering_control/adapters/claude/client.py:69-101` `ClaudeCLIAdapter.execute` | See "Engine invocation" section below |
| 11 | Git operations (worktree branch) | `src/engineering_control/worktrees/manager.py:245` `git worktree add -b <branch> ...` | Isolated worktree per component lease under `settings.worktree_root` (`/srv/engineering/worktrees`) |
| 12 | GitHub write — push + PR | `src/engineering_control/activities/delivery.py:838-904` `_publish`; git push at `:859-871` (`git ... push --porcelain https://github.com/<repo>.git HEAD:refs/heads/<branch>`); PR creation at `:876-884` calling `src/engineering_control/adapters/github/client.py:421-449` `create_pull_request` (`gh pr create --repo ... --head ... --base ... --draft`) | Runs under the `github` role worker (`hermes` unix user, `engineering-github-worker.service`); gated by `GhCliBroker._write` (`adapters/github/client.py:614-624`) and `GitHubWritePolicyEvaluator.evaluate` (`policy/github_write.py:116-142`) — see Gates |
| 13 | Result persisted back | `src/engineering_control/workflows/definitions.py:196-200` `_agent_step` calls `"agent.persist_result"`; DB tables `runs`, `steps`, `agent_invocations`, `agent_executions`, `change_sets` in `src/engineering_control/db/models.py:138-221` | Written via `MetadataActivityHandler`/`DeliveryActivityHandler` to Postgres (`engineering_control` DB); dashboard reads the same rows (`src/engineering_control/dashboard/repository.py`) |
| 14 | Post-merge closure (optional, mission says "produces a branch/PR" — this hop is beyond scope but exists in code) | `src/engineering_control/workflows/definitions.py:975-1201` `FinalClosureWorkflow` | Only reachable from `ExistingGitHubBacklogLoopWorkflow` when `delivery_mode == "full-delivery"` **and** the Temporal patch marker `existing-backlog-final-closure-v1` is active — separate from the plain "source PR" outcome |

Full-delivery variant note: `ExistingGitHubBacklogLoopWorkflow` (`definitions.py:1801-2400`) is the
version driven by `AutonomousBacklogCampaignWorkflow` (the "GitHub Projects"-triggered global
selector, `definitions.py:2402-2497`, one active issue globally, staging-only, `maximum_risk="R2"`
enforced at `:2408-2422`). Its `full-delivery` branch is hard-gated: `definitions.py:2119-2124`
returns `{"status": "BLOCKED_BY_DELIVERY", "blocker": "FULL_DELIVERY_REQUIRES_PROJECT_BUILD_AND_STAGING_GATES"}`
unless `workflow.patched("existing-backlog-full-delivery-orchestration-v1")` is true for that
workflow's history (new workflow executions get `True` by Temporal SDK semantics — this is a
version-branch marker, not a config flag, so its "current value" is execution-history-dependent,
not statically knowable from source). Independent of that: the API surface that would let an
operator approve a full delivery is **hardcoded to refuse**:
`src/engineering_control/api/main.py:824-836` `POST /v1/backlog-loops/{workflow_id}/approve-delivery`
unconditionally raises `HTTPException(409, "staging approval candidate is not yet available...")`
for every call, and `src/engineering_control/api/main.py:838-856`
`GET /v1/backlog-loops/{workflow_id}/delivery-candidate` unconditionally returns
`{"status": "BLOCKED_BY_DELIVERY", "blocker": "ISSUE_HAS_NOT_REACHED_AWAITING_STAGING_DELIVERY", ...}`
regardless of actual workflow state. This corroborates the prior audit's F-03 claim
(`~/engineering/docs/audit/02-findings.md:23`) that full delivery beyond a source PR cannot
currently be approved through the API at all — this is a source-verified code fact, not just a
log-count claim.

**Task queue / workflow type summary — Mission 1**: workflow types `IssueSolutionLoopWorkflow`,
`IssueDeliveryWorkflow`, `ExistingGitHubBacklogLoopWorkflow`, `AutonomousBacklogCampaignWorkflow`,
`FinalClosureWorkflow` (`workflows/names.py:8,20,21,22,9`), all started on task queue
`"engineering-control"`; activities fan out to `engineering-codex`, `engineering-claude`,
`engineering-scan`, `engineering-github`, `engineering-release` depending on step.

---

## Mission 2 — Product audit (audit a target repo → report)

| Hop | Component | Exact file:line | What it does |
|---|---|---|---|
| 0 | Hermes skill (outside this repo) | `/home/hermes/.hermes/skills/audit-product/SKILL.md` | Describes an audit run driven largely by direct agent tool use (`hmw ping all`, `hflow check-project`, `kubectl`/`oc` directly, `/audit-cluster`) — this skill's documented flow does **not** obviously route through the Temporal `AuditCampaignWorkflow` described below; it reads as a parallel/legacy execution path. Flagged as a discrepancy worth reconciling, not resolved by this repo's code alone. |
| 1 | HTTP trigger endpoint | `src/engineering_control/api/main.py:549` `POST /v1/campaigns` → `campaign_start`; `src/engineering_control/api/main.py` multi-project variant (`start_all_project_audits`) | Behind `auth_dependency` |
| 2 | Temporal client `start_workflow` | `src/engineering_control/api/service.py:145-160` `start_campaign` | `self.client.start_workflow(WorkflowName.AUDIT_CAMPAIGN.value, ..., task_queue=self.task_queue)` → workflow type literal `"AuditCampaignWorkflow"` (`workflows/names.py:5`), queue `"engineering-control"` |
| 3 | Temporal workflow — campaign | `src/engineering_control/workflows/definitions.py:360-647` `AuditCampaignWorkflow.run` | Builds a coverage-cell matrix (`:391-406`), snapshots the repo (`project.snapshot`, `:416`), then runs static/deployment/runtime audit stages (`_audit_stages`, `:649-675`), then iterates cells calling `audit.deterministic_cell` + `audit.agent_cell` |
| 4 | Multi-project wrapper | `src/engineering_control/workflows/definitions.py:678-816` `MultiProjectAuditWorkflow.run` | Sequentially runs `AuditCampaignWorkflow` as a child workflow per project in `("falcone", "musematic", "llmwiki")` (`:682`, child-workflow call `:747-761`) — workflow type `"MultiProjectAuditWorkflow"` |
| 5 | Activity routing | `src/engineering_control/workflows/definitions.py:229-249,254-256` | `audit.static_source`/`audit.static_deployment`/`audit.deterministic_cell`/etc. → `task_queue_for("scan")` = `"engineering-scan"`; `audit.agent_cell` → `provider_task_queue(payload.provider, default="codex")` → `"engineering-codex"` (or `-claude`); `audit.api_flow`/`audit.browser_flow` → `"engineering-browser"` |
| 6 | Agent audit cell execution | `src/engineering_control/activities/provider.py:100-132` `ProviderActivityHandler.handle("audit.agent_cell", ...)` | Same CLI adapter path as Mission 1 (Codex or Claude, per cell's assigned provider) |
| 7 | Enhancement/finding discovery inside the audit | `src/engineering_control/workflows/definitions.py:670-673` (`audit.discover_enhancements` in `_audit_stages`); candidate escalation to `FindingVerificationWorkflow` at `:599-609` | A `CANDIDATE_FOUND` cell result spawns `workflow.execute_child_workflow("FindingVerificationWorkflow", ...)` |
| 8 | Report/result persistence | `campaign.persist_cell_result` (`:586-594`), `campaign.finalize` (`:640-642`); DB tables `campaigns`, `coverage_cells`, `findings`, `project_snapshots` (`db/models.py:27,38,68,396`) | `MetadataActivityHandler` (controller worker, `worker_kind="controller"`) persists to Postgres; dashboard/API expose it via `GET /v1/campaigns`, `GET /v1/campaign-detail/{id}` (`api/main.py:464,584`) |

**Task queue / workflow type summary — Mission 2**: workflow types `AuditCampaignWorkflow`,
`MultiProjectAuditWorkflow`, `FindingVerificationWorkflow` (`workflows/names.py:5,6,7`), started on
`"engineering-control"`; agent-cell activities fan out to `engineering-codex`/`engineering-claude`,
deterministic/static-analysis steps to `engineering-scan`, browser/API-flow steps to
`engineering-browser`.

---

## Mission 3 — Enhancement detection (find + file enhancement opportunities)

| Hop | Component | Exact file:line | What it does |
|---|---|---|---|
| 1 | HTTP trigger endpoint | `src/engineering_control/api/main.py:878-884` `POST /v1/enhancements/discover` → `enhancement_discover` | Behind `auth_dependency` |
| 2 | Temporal client `start_workflow` | `src/engineering_control/api/service.py:482-490` `start_enhancement_discovery` | `WorkflowName.ENHANCEMENT_DISCOVERY.value` = `"EnhancementDiscoveryWorkflow"` (`workflows/names.py:24`), queue `"engineering-control"` |
| 3 | Temporal workflow — discovery | `src/engineering_control/workflows/definitions.py:2499-2533` `EnhancementDiscoveryWorkflow.run` | Steps: `enhancement.load_evidence → enhancement.prepare_discovery → [dry_run short-circuit] → enhancement.discover → enhancement.validate_evidence → enhancement.dedupe → enhancement.github_dedupe → enhancement.product_review → enhancement.persist` |
| 4 | Activity routing | `src/engineering_control/workflows/definitions.py:282-285` | `enhancement.discover`/`enhancement.product_review` → `provider_task_queue(payload.provider, default="codex")` (agent steps); `enhancement.dedupe`/`enhancement.github_dedupe`/`enhancement.publish` → `GITHUB_TASK_QUEUE = "engineering-github"` (`:270-275`) |
| 5 | Agent discovery/review execution | `src/engineering_control/activities/provider.py` `ProviderActivityHandler.handle("enhancement.discover"/"enhancement.product_review", ...)` | Same Codex/Claude CLI adapters as Missions 1–2 |
| 6 | Result: candidate persisted (not yet filed) | `EnhancementDiscoveryWorkflow` returns `{"status": "CANDIDATE_ENHANCEMENT", ...}` (`definitions.py:2533`); DB table `enhancements` (`db/models.py:419-432`) | Requires human/operator approval next: `POST /v1/enhancements/{id}/approve` (`api/main.py:910`) |
| 7 | Temporal workflow — delivery (files the issue / opens the PR) | `src/engineering_control/workflows/definitions.py:2536-2553` `EnhancementDeliveryWorkflow.run` | Requires `context["status"] == "APPROVED"` (else `{"status": "BLOCKED", "blocker": "ENHANCEMENT_APPROVAL_REQUIRED"}`, `:2541-2542`); if `request.publish` is true, calls `_github_write_step("enhancement.publish", ...)` (`:2547-2549`) — a GitHub write gated exactly like Mission 1's `delivery.publish` |
| 8 | Enhancement → full solution loop (optional) | `src/engineering_control/workflows/definitions.py:2556-2584` `EnhancementSolutionLoopWorkflow.run` | Also requires `APPROVED`; if not dry-run/plan-only, spawns `IssueSolutionLoopWorkflow` as a child (`:2566-2581`) — i.e. an approved enhancement is delivered through the **same** Mission-1 pipeline |
| 9 | GitHub write (issue/PR) | Same as Mission 1 hop 12: `activities/delivery.py:838-904`, `adapters/github/client.py:421-449` and `:391-419` (`create_issue`) | Gated by the same `GitHubWritePolicyEvaluator` |

**Task queue / workflow type summary — Mission 3**: workflow types `EnhancementDiscoveryWorkflow`,
`EnhancementDeliveryWorkflow`, `EnhancementSolutionLoopWorkflow` (`workflows/names.py:24,25,26`),
started on `"engineering-control"`; discovery/review agent steps fan out to
`engineering-codex`/`engineering-claude`, publish steps to `engineering-github`.

---

## Engine invocation (exact subprocess construction)

### Claude Code CLI — `src/engineering_control/adapters/claude/client.py:69-93`

```python
command = [
    self.executable,               # "claude" by default; settings.claude_executable = /usr/local/bin/claude
    "--print",                     # non-interactive mode
    "--output-format", "stream-json",
    "--verbose",
    "--json-schema", json.dumps(cli_schema, separators=(",", ":"), sort_keys=True),
    "--no-session-persistence",
    "--safe-mode",
    "--strict-mcp-config",
    "--mcp-config", '{"mcpServers":{}}',
    "--permission-mode", "dontAsk",
    "--tools", ",".join(tools),
    "--allowed-tools", ",".join(tools),
]
if request.model:  command.extend(("--model", request.model))
if request.effort: command.extend(("--effort", request.effort))
```
Executed by `run_jsonl_process(command, prompt=request.prompt, cwd=request.working_directory, ...)`
at `adapters/claude/client.py:94-101`, which shells out via `asyncio.create_subprocess_exec` in
`src/engineering_control/adapters/process.py:96-105` with `cwd=cwd` (the assigned git worktree) and
`stdin=PIPE` (the prompt is written to stdin, not passed as an argv string) — see `process.py:112-114`.
Output is read as newline-delimited JSON events from stdout (`process.py:120-141`); the terminal
`"result"` event's `structured_output`/`result` fields become the parsed result
(`adapters/claude/client.py:128-143`).

### Codex CLI — `src/engineering_control/adapters/codex/client.py:61-84`

```python
command = [
    self.executable,                # "codex" by default; settings.codex_executable = /usr/local/bin/codex
    "exec",                         # non-interactive mode
    "--json",
    "--color", "never",
    "--ephemeral",
    "--strict-config",
    "--cd", request.working_directory,
    "--sandbox", "workspace-write" if request.writable else "read-only",
    "--output-schema", str(schema_path),
    "--output-last-message", str(final_path),
]
if request.skip_git_repository_check: command.append("--skip-git-repo-check")
if request.model:  command.extend(("--model", request.model))
if request.effort: command.extend(("--config", f'model_reasoning_effort="{request.effort}"'))
command.append("-")                 # read prompt from stdin
```
Same `run_jsonl_process` shell-out mechanism (`adapters/codex/client.py:86-93` →
`adapters/process.py:96-105`); final structured result is read back from a temp file
`--output-last-message <final_path>` (`codex/client.py:96-104`), not just parsed from stdout.

Both adapters call `subscription_only_environment(provider, ...)` (`adapters/process.py:24-60`)
to build the subprocess environment, which **strips** `OPENAI_API_KEY`, `OPENAI_BASE_URL`,
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `OPENROUTER_API_KEY`,
`DEEPSEEK_API_KEY`, `OMNIROUTE_URL`, `OMNIROUTE_BASE_URL`, `LLM_BASE_URL`, `MODEL_GATEWAY`,
`MODEL_ROUTER`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`,
`AWS_ACCESS_KEY_ID/SECRET_ACCESS_KEY/SESSION_TOKEN` from `os.environ` before exec, and raises if an
override tries to reintroduce one (`process.py:53-59`). This is a deliberate code-level control to
force subscription-CLI-only invocation (relevant to the "OmniRoute"/paid-API-fallback history
referenced by `src/engineering_control/validation/c01.py` — see Gates).

---

## Any API usage found in mission code paths

**None found.** Searched `src/engineering_control/activities`, `src/engineering_control/adapters`,
`src/engineering_control/workflows` for `openai`, `anthropic.Anthropic`, `ChatCompletion`,
`chat.completions`, `httpx.post/get/Client`, `requests.post/get`, `api.anthropic.com`,
`api.openai.com`: the only "provider" HTTP-shaped code is `httpx.AsyncClient` in
`src/engineering_control/activities/browser.py:417,477`, which drives the **browser-journey audit
tool** (Playwright-style runtime checks against the target product's own HTTP endpoints for Mission
2, not an LLM call) — confirmed unrelated to engine invocation. Both `ClaudeCLIAdapter.execute` and
`CodexCLIAdapter.execute` exclusively shell out to the `claude`/`codex` binaries as shown above.
`adapters/process.py:24-60` actively strips API-key/gateway environment variables before every
subprocess launch, which is a code-level anti-regression control against silently falling back to a
paid HTTP API. **F-000 (HTTP-instead-of-CLI blocker) is not present in this checkout's code.**

---

## Gates (fail-closed policy/feature-flag checks)

| Gate | file:line | Current value in this checkout | What would open it |
|---|---|---|---|
| GitHub writes master switch | `src/engineering_control/config.py:83` `github_writes_enabled: bool = False` | Code default `False`; overridden to `True` only for the `github` role worker via `Environment=ENGINEERING_GITHUB_WRITES_ENABLED=true` in `systemd/engineering-github-worker.service:17` | Deploy sets env var per-service; other role workers (codex/claude/scan/delivery/release) never set it, so it stays `False` for them |
| GitHub write policy document (repo-allowlist) | `src/engineering_control/policy/github_write.py:116-142` `GitHubWritePolicyEvaluator.evaluate`; loaded from `settings.github_issue_write_validation_file` = `/etc/engineering-control/policy/github-issue-write-validation.yaml` (`config.py:86`, default `GITHUB_WRITE_POLICY_PATH`) | Checked-in template `config/policy/github-issue-write-validation.yaml:1-3` = `enabled: false`, `safe_repositories: []`. **Deployed** `/etc/engineering-control/policy/github-issue-write-validation.yaml` could not be read from this checkout (`Permission denied`; parent dir `drwxr-x--- root:engineering-control`, `hermes` not in that group) — live value unverified | Effective write-enable is `settings.github_writes_enabled AND policy.enabled AND repo in policy.safe_repositories` (`github_write.py:122,133-136`); an operator must edit that YAML to `enabled: true` and list exact repos |
| GitHub write binding requirement | `src/engineering_control/adapters/github/client.py:614-624` `GhCliBroker._write` | Requires `writes_enabled=True` **and** a `change_binding` matching `^(?:changeset\|campaign\|finding\|enhancement):[A-Za-z0-9-]{8,200}$` | Always enforced in code; not a flag, structural precondition |
| Full-delivery orchestration (backlog loop beyond source-PR) | `src/engineering_control/workflows/definitions.py:2119-2124` `if not workflow.patched("existing-backlog-full-delivery-orchestration-v1")` | Temporal "patch marker" — `True` for any freshly-started workflow execution in this build (no legacy history to preserve), so **not currently blocking new runs by itself**; runtime-dependent, not a static config value | N/A — code-version gate, not an operator switch |
| Delivery-approval API stub | `src/engineering_control/api/main.py:824-836` `POST /v1/backlog-loops/{id}/approve-delivery` | Hardcoded: **always** raises `HTTPException(409, ...)`, unconditionally, for every call | Requires code change — this endpoint has no live/dead-code path to success at all in this checkout |
| Delivery-candidate status stub | `src/engineering_control/api/main.py:838-856` `GET /v1/backlog-loops/{id}/delivery-candidate` | Hardcoded: always returns `{"status": "BLOCKED_BY_DELIVERY", "blocker": "ISSUE_HAS_NOT_REACHED_AWAITING_STAGING_DELIVERY"}` regardless of actual workflow state | Requires code change |
| Application (target-product) data mutation | `src/engineering_control/config.py:85` `application_data_mutation_enabled: bool = False` | Default `False`; used at `src/engineering_control/activities/browser.py:334` to gate mutating browser-journey steps | Deploy-time env var `ENGINEERING_APPLICATION_DATA_MUTATION_ENABLED=true` |
| LLMWiki staging bootstrap | `src/engineering_control/config.py:84` `llmwiki_staging_bootstrap_enabled: bool = False` | Default `False` | Deploy-time env var |
| Cleanup execution | `src/engineering_control/config.py:104` `cleanup_execution_enabled: bool = False`; also `config/control.yaml:44` `cleanup.execution_enabled: false` | `False` in both code default and checked-in control.yaml | Deploy-time env var / control.yaml edit |
| Acceptance-lifecycle worker | `src/engineering_control/config.py:96` `acceptance_enabled: bool = False` | Code default `False`; systemd test asserts `ENGINEERING_ACCEPTANCE_ENABLED=true` is set for the acceptance worker unit (`tests/test_systemd_units.py:60`) — i.e. deploy-time override exists for that one service | Env var per-service, as with GitHub writes |
| Preview/production deployment master switches | `config/control.yaml:34-38` `deployments.default_enabled: false`, `production_enabled: false`; `runtime_verification.default_enabled: false` | All `false` | Per-target override file under `deployment_target_root` (`/etc/engineering-control/targets`) |
| Paid-API fallback | `config/control.yaml:68-69` `paid_api_fallback.enabled: false`; enforced structurally by `validation/c01.py:37-46` `validate_provider` (requires `paid_api_fallback is False` and `authentication_mode == "native-subscription-cli"`) and by env-var stripping in `adapters/process.py:24-60` | `False` — consistent with "no HTTP API usage found" above | N/A — this is the control that should stay closed |
| API/dashboard bind scope | `src/engineering_control/config.py:143-155` `private_api_only`/`private_dashboard_only` validators | API cannot bind `0.0.0.0`/`::`; dashboard can only bind `127.0.0.1`/`::1`/`localhost` | Structural, not configurable to public bind at all |
| API bearer-token auth | `config/control.yaml:6-8` `api.authentication: bearer-token-file`, `token_file: /etc/engineering-control/env/api.token`; enforced by `auth_dependency` referenced throughout `api/main.py` (e.g. `:552,560,571`) | Token file path fixed; token itself not readable from this checkout | N/A |

---

## Evidence of actual mission execution

Could not be independently confirmed or refuted from this checkout:

- **No filesystem access**: `/srv/engineering/*` (evidence, mirrors, worktrees, runs) and
  `/var/lib/engineering-control/*` are `Permission denied` for `hermes` (no sudo, consistent with
  audit memory). Cannot inspect run directories for dated/non-empty contents.
- **No database access**: `psql` is not installed in this shell and the configured Postgres
  (`postgresql://.../engineering_control` on `127.0.0.1:55432`) was not queryable from here. Tables
  that would prove a completed run exist in the schema — `runs`, `steps`, `agent_invocations`,
  `agent_executions`, `campaigns`, `change_sets`, `enhancements`
  (`src/engineering_control/db/models.py:27,68,105,115,138,155,168,187,419`) — but their row counts
  are unknown from this vantage point.
- **No live Temporal access**: did not query `temporal_target = 127.0.0.1:7233` for workflow history
  (out of scope for a read-only file-based trace and no client tooling available here).
- The prior audit (`~/engineering/docs/audit/02-findings.md:23`, dated 2026-09-01, explicitly marked
  "claims to reverify" in `.go-status/progress.json`) asserts **F-03: "0 executions of
  IssueSolutionLoop/IssueDelivery/ExistingGitHubBacklogLoop/AutonomousBacklogCampaign in 30 d"**. This
  trace corroborates the *code-level* part of that claim (the `BLOCKED_BY_DELIVERY` patch gate at
  `definitions.py:2119-2124` and, more conclusively, the hardcoded-409/hardcoded-blocked API stubs at
  `api/main.py:824-836` and `:838-856`) but cannot independently confirm the "0 executions" execution
  count itself — that requires DB/Temporal access this session does not have.
- No results/reports directory with real (non-empty, dated) contents was found **inside the git
  repository itself** — `schemas/agent-result.schema.json` is a JSON Schema, not run output. The
  actual runtime output locations by design live outside the repo, under `/srv/engineering/*`
  (per `config.py` defaults), which is inaccessible here.
- **Net assessment**: this hop cannot itself prove or disprove that any of the three mission types
  has completed a real end-to-end run; it can only confirm that the code path exists, is
  CLI-based (not HTTP-API-based), and that specific API surfaces for one path (full backlog delivery
  approval) are currently non-functional stubs. Recommend the "reports/claims" discovery hop (per
  `.go-status/progress.json`) be the authority on actual execution history via DB/Temporal.
