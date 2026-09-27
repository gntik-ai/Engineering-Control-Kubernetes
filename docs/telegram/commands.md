# Telegram command reference (Hermes gateway → Engineering Control)

Generated 2026-09-27, read-only, from code: the repository hook `integrations/hermes-engineering-control/hook/handler.py` (`H:`), plugin `integrations/hermes-engineering-control/plugin/__init__.py`, API `src/engineering_control/api/main.py` (`A:`), command bus `src/engineering_control/operations/*`, workflows `src/engineering_control/workflows/definitions.py` (`D:`), all at `main` `33c14ed`; the installed hook copy `~/.hermes/hooks/engineering-control/handler.py`; the live gateway's Hermes source `/opt/hermes-agent/releases/fecf787…/source`; and read-only API calls. Worked examples are in `examples.md`; machine-readable list in `commands.yaml`.

## Part 1 — The real command surface

### How a Telegram slash command is processed

1. **Gateway access policy** (`gateway/run.py:18992-19001` in the live Hermes source): `platforms.telegram.allow_admin_from` is the sentinel `engineering-control-no-admin`, so nobody is admin; non-admins may run only `user_allowed_commands` — the 30 engineering commands (`~/.hermes/config.yaml:135-218`, same list for groups) — plus the always-allowed floor `/help` and `/whoami`. The live gateway reads its own copy under `/var/lib/hermes-gateway/home` (unreadable → UNVERIFIED that it matches).
2. **`command:*` hook** (`HOOK.yaml`: `events: [command:*]`; `gateway/run.py:19032-19075`): every gateway-known slash command goes to `handle()` (H:1227-1278). Commands outside the hook's `COMMANDS` set (H:18-49) get `Unknown engineering command denied.` — so **every Hermes built-in, including `/help` and `/whoami`, is denied** despite the policy floor. Non-Telegram platforms get `Engineering Control chat commands are restricted to Telegram.`; senders not in the numeric allowlist file (`ENGINEERING_TELEGRAM_ALLOWLIST_FILE`, one Telegram user id per line, root:hermes 0640) get `Engineering Control access denied.` (H:122-130).
3. **Dispatch** (`_dispatch`, H:524-1224): fixed argument grammar (`shlex.split`), then one loopback HTTP call (or two for locate-then-act commands) to the API with bearer token and `X-Engineering-Actor: telegram:<user-id>`; `/v1/commands` calls carry an `Idempotency-Key: telegram-<uuid4>` (H:152-183).
4. **Reply**: always one message — the JSON result rendered as `{"next_cursor": …, "payload": …}` (sorted keys, indent 2, secrets redacted), at most 3500 characters per page; further pages via the same command plus `--cursor <32-hex>` (valid 10 minutes, same user and command) (H:274-419). Errors: `Engineering Control: <message>`; API errors `Engineering Control: Engineering API rejected the request (<code>): <detail>`; API down `Engineering Control: Engineering API is unavailable on loopback`; errors longer than 3500 characters are replaced by a digest (H:142-149, H:1273-1278).
5. **Later messages** (not from the hook): the notifier delivers two outbox topics to the gateway webhook route `engineering-alerts`: `telegram.engineering-operation-started` when a typed command starts a workflow (`operations/repository.py:1576-1592`), and `telegram.engineering-write-policy-required` when a flow waits for GitHub write approval (`activities/engineering.py:1919-1967`). **No completion or failure notification exists**; the operator polls with `/status`, `/loop status`, `/loop tail`.

### Classification

All 59 entries below are **deterministic** (hook → API, no model call). 37 can change state (start/signal a workflow, record an approval, change GitHub write policy); the rest are reads. **Model-mediated** use is only free text (non-slash messages), which Hermes's agent handles with its own model and tools (skills are not loaded on Telegram, see `docs/inventory/skills.md`); it has no command grammar and is out of scope here.

### Sources compared

| Surface | Commands | Evidence |
|---|---|---|
| Hook `COMMANDS` (repo = installed) | 30 top-level names | H:18-49 |
| Plugin fallback registration | same 30 names with one-line descriptions; handler only replies `Engineering Control gateway hook unavailable; use engineering-ctl locally.` | plugin/__init__.py:9-57 |
| Hermes `user_allowed_commands` (~/.hermes copy) | same 30 names | ~/.hermes/config.yaml:135-218 |
| Telegram menu (`setMyCommands`/`getMyCommands`) | UNVERIFIED — no platform helper reads it; skipped as instructed. Registration code is in Hermes `gateway/slash_commands.py`, not traced | — |
| Dashboard | typed commands via `/api/dashboard/actions/candidates` + password re-authentication (`dashboard/app.py:807-830`): all 17 command types of the bus (`operations/models.py:272-290`) | — |

### Live usage

- `engineering-ctl channel-check` (2026-09-27): gateway RUNNING, hook profile `engineering-control-fixed-commands`, last recognized command 2026-09-27T19:17:46Z, `last_safe_error: Telegram response delivery failed` [live].
- Per-command history for the last 30 days: **UNVERIFIED** — the gateway's session DB, logs and acceptance log live under `/var/lib/hermes-gateway/home` (permission denied); the API stores `telegram.command_*` events but exposes no listing route; `/internal/metrics` has no Telegram counters. The only readable log is the old acceptance log `~/engineering-bootstrap/telegram-acceptance.jsonl` (40 events 2026-08-24..09-02: `/status` 20, `/usage` 11, `/validate` 9).
- Most common error replies: UNVERIFIED for the same reason.

### Discrepancies

1. **Installed hook ≠ repository hook.** `~/.hermes/hooks/engineering-control/handler.py` still has `/loop delivery-candidate` and `/loop approve-delivery` (API actions removed → 404) and `/loop r3-candidate|approve-r3` hard-wired to `musematic source 1119`; the repo copy removed/generalized them (NEW-01, `docs/GO-STATUS.md:132-145`: "written, tested, not deployed"). The hook is installed only by `scripts/bootstrap-controller.sh:226-242`, which the release deploy script does not run. Which copy the live gateway (`/var/lib/hermes-gateway/home`) uses: UNVERIFIED.
2. **`/help` and `/whoami` are denied** by the hook (`Unknown engineering command denied.`) although Hermes's policy floor allows them; there is no in-chat help. Six engineering names shadow Hermes built-ins (`/approve`, `/pause`, `/resume`, `/rollback`, `/status`, `/usage`) — on Telegram they always mean the engineering command.
3. **Usage strings disagree with the grammar**: `/loop` with no args lists `import|inventory|plan|queue|issue|approval-candidate|approve|start|status|pause|resume|cancel|skip|retry` but not `issue-locate`, `issue-start`, `global-start`, `diagnose`, `tail`, `nudge`, `retry-issue`, `r3-candidate`, `approve-r3` (H:749-753). `/loop start` accepts `plan-only|preview|staging` as mode (H:1030) but the typed command only allows `source-pr|full-delivery`, so those fail at the API.
4. **`/loop nudge` is effectively a no-op** (sends `retry` with value `operator-nudge`, which no wait condition matches).
5. **Write-policy alert gives CLI, not Telegram, commands** (`engineering-ctl backlog-approval-candidate … --max-items 1`, `engineering-ctl backlog-approve …`, activities/engineering.py:1934-1937); the Telegram equivalents are `/loop approval-candidate` and `/loop approve` with positional `max-items`.
6. **No Telegram command** for: releasing write-policy ownership early (`POST /v1/backlogs/{id}/release-ownership`, CLI `backlog-release-ownership`), `pause_after_current` / `stop_after_current` (bus actions available to the dashboard), `refresh_connections` (dashboard/CLI `recheck-connections`), delivery reconciliation, preview/staging deployment preparation, runtime verification, fetching an export body (`operation-export`), R3 via dashboard only in the installed copy for non-1119 issues.
7. **Dashboard vs Telegram**: the dashboard submits typed bus commands after password re-authentication; Telegram calls a mix of `/v1/commands` and direct REST routes (enhancement approve/reject go to REST in Telegram but are bus commands on the dashboard).
8. **`/approve` / `/reject`** target Preview/Staging/Recovery approval waits; the current delivery path (StagingGitOpsDelivery) has no approval gate, so these only matter for the legacy candidate pipeline and recovery.
9. **`/loop issue-start` fixes maximum risk at R2** (H:821-825): R3 issues (e.g. falcone #1012–#1016, falcone-charts #22) fail preflight until an R3 approval exists; there is no per-run risk, model or effort override in any command.

## Part 2 — Reference

### Group: health

#### /ready

```
Purpose:        Show the core or complete readiness projection.
Type:           deterministic · read
Syntax:         /ready <core|all>
Arguments:
  - scope · enum · required: yes · core | all · default: - · error: "usage: /ready <core|all>"
Backend:        GET /v1/readiness/{scope} (src/engineering_control/api/main.py:240)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419). Real sample (2026-09-27): `{"status":"DEGRADED","connection_count":23,"connected_count":21,"worker_count":7,"scope":"core",...}`.
Side effects:   none
Related:        /connections all, /workers all
Example:        /ready core
Evidence:       integrations/hermes-engineering-control/hook/handler.py:535-538; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /connections

```
Purpose:        Show the bounded connection-health projections.
Type:           deterministic · read
Syntax:         /connections all
Arguments:
  - scope · literal · required: yes · all · default: - · error: "usage: /connections all"
Backend:        GET /v1/connections?scope=all (src/engineering_control/api/main.py:251)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419).
Side effects:   none
Related:        /ready core
Example:        /connections all
Evidence:       integrations/hermes-engineering-control/hook/handler.py:539-542; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /temporal

```
Purpose:        Show the safe Temporal summary.
Type:           deterministic · read
Syntax:         /temporal summary
Arguments:
  - view · literal · required: yes · summary · default: - · error: "usage: /temporal summary"
Backend:        GET /v1/temporal/summary (src/engineering_control/api/main.py:285)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419). Real sample: `{"status":"CONNECTED","target":"127.0.0.1:7233","active_workflow_projections":107,"worker_heartbeat_count":7,...}`.
Side effects:   none
Related:        -
Example:        /temporal summary
Evidence:       integrations/hermes-engineering-control/hook/handler.py:543-546; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /workers

```
Purpose:        Show task-worker heartbeat projections.
Type:           deterministic · read
Syntax:         /workers all
Arguments:
  - scope · literal · required: yes · all · default: - · error: "usage: /workers all"
Backend:        GET /v1/workers (src/engineering_control/api/main.py:264)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419).
Side effects:   none
Related:        -
Example:        /workers all
Evidence:       integrations/hermes-engineering-control/hook/handler.py:551-554; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /channel-check

```
Purpose:        Show Telegram ↔ Hermes gateway ↔ API correlation.
Type:           deterministic · read
Syntax:         /channel-check
Arguments:      none
Backend:        GET /v1/channel-check (src/engineering_control/api/main.py:278)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419). Real sample: `{"gateway_state":"RUNNING","hook_profile":"engineering-control-fixed-commands","status":"CONNECTED","last_safe_error":"Telegram response delivery failed",...}`.
Side effects:   none
Related:        -
Example:        /channel-check
Evidence:       integrations/hermes-engineering-control/hook/handler.py:555-558; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /validate

```
Purpose:        Start the synthetic control-plane lifecycle canary (AcceptanceLifecycleWorkflow).
Type:           deterministic · write (synthetic)
Syntax:         /validate [hold-seconds]
Arguments:
  - hold-seconds · int · required: no · 2..30 · default: 5 · error: "hold-seconds must be between 2 and 30"
Backend:        POST /v1/validation/lifecycle (src/engineering_control/api/main.py:1125 → src/engineering_control/api/service.py:356)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: `OperationAccepted` JSON `{workflow_id, run_id, status: "ACCEPTED"}` (src/engineering_control/api/schemas.py:330-333) wrapped as `{"next_cursor": null, "payload": {...}}`. No later message; poll with `/status <workflow-id>`. Recorded in the acceptance log (H:441-446).
Side effects:   synthetic files only (acceptance worker runs pre-deploy code 2b678d9)
Related:        /status <workflow-id>
Example:        /validate 5
Evidence:       integrations/hermes-engineering-control/hook/handler.py:572-583; last observed use: 9 uses 2026-08-24..09-02 (old acceptance log)
```

### Group: engines

#### /providers

```
Purpose:        Show native CLI provider readiness (codex, claude).
Type:           deterministic · read
Syntax:         /providers
Arguments:      none
Backend:        GET /v1/providers (src/engineering_control/api/main.py:271)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419). Real sample: `{"status":"READY","providers":{"codex":{"authenticated":true,"cli_present":true,"rate_limited":false,"running_jobs":0,"status":"AVAILABLE",...},"claude":{...}}}`.
Side effects:   none
Related:        /usage
Example:        /providers
Evidence:       integrations/hermes-engineering-control/hook/handler.py:547-550; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /usage

```
Purpose:        Show provider auth/capacity snapshot and 7-day token history per role.
Type:           deterministic · read
Syntax:         /usage
Arguments:      none
Backend:        GET /v1/usage (src/engineering_control/api/main.py:1067-1073; 7-day window)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419). Real sample: `{"claude":{"authenticated":true,"source":"native_cli_auth_status;capacity_not_exposed","history":{"roles":{"code-reviewer":{"invocations":48,"successes":48,"output_tokens":1323608,...}}}},"codex":{...}}`. Recorded in the acceptance log (H:425-440).
Side effects:   none
Related:        /providers
Example:        /usage
Evidence:       integrations/hermes-engineering-control/hook/handler.py:559-562; last observed use: 11 uses 2026-08-24..09-02 in the old acceptance log (~/engineering-bootstrap/telegram-acceptance.jsonl); none readable in the last 30 days
```

### Group: runs

#### /status

```
Purpose:        Show one workflow run's projection (status, type, validation summary).
Type:           deterministic · read
Syntax:         /status <workflow-id>
Arguments:
  - workflow-id · string · required: yes · exact Temporal workflow id · default: - · error: "usage: /status <workflow-id>"
Backend:        GET /v1/runs/{workflow_id} (src/engineering_control/api/main.py:979)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419). Real sample: `{"workflow_id":"backlog-loop-musematic-66a97948-…-issue-1-staging-gitops-retry-1","run_id":"01a0e2ee-…","status":"COMPLETED","workflow_type":"StagingGitOpsDeliveryWorkflow"}`. Recorded in the acceptance log (H:447-472).
Side effects:   none
Related:        /loop status, /loop tail, /loop diagnose
Example:        /status backlog-loop-musematic-66a97948-9f9d-4ae9-b204-e89f27e2712d-27604282-30bc-4052-8dc1-12f92bcd7dea
Evidence:       integrations/hermes-engineering-control/hook/handler.py:563-566; last observed use: 20 uses 2026-08-24..09-02 (old acceptance log); none readable in the last 30 days
```

#### /pause

```
Purpose:        Pause a workflow at its next checkpoint (Temporal `pause` signal).
Type:           deterministic · write
Syntax:         /pause <workflow-id>
Arguments:
  - workflow-id · string · required: yes · [A-Za-z0-9._:-]{1,240} · default: - · error: "usage: /pause <workflow-id>"
Backend:        POST /v1/commands action=pause (src/engineering_control/api/main.py:183 → src/engineering_control/operations/service.py WorkflowControlCommand, :318-326)
Preconditions:  Preflight: WORKFLOW_NOT_FOUND, WORKFLOW_IS_TERMINAL, WORKFLOW_ALREADY_PAUSED, WORKFLOW_IS_NOT_RUNNING (operations/repository.py:896-921) → reply status BLOCKED.
Reply:          Immediate: the OperationView JSON of the typed command (`operation_id`, `action`, `target_type`, `target_id`, `source_channel: telegram`, `status` COMPLETED|BLOCKED|FAILED|PENDING, `workflow_id`, `temporal_run_id`, `binding_hash`, `safe_error`, `created_at`, `result_available`) (src/engineering_control/operations/models.py:326-339), rendered by the hook as `{"next_cursor": null, "payload": {...}}`. BLOCKED carries `safe_error` = `operation preflight changed: <BLOCKER>` or the policy error text (operations/service.py:97-110).
Side effects:   Temporal signal `pause`
Related:        /resume, /cancel
Example:        /pause audit-falcone-<uuid>
Evidence:       integrations/hermes-engineering-control/hook/handler.py:614-617; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /resume

```
Purpose:        Resume a paused workflow (`resume` signal).
Type:           deterministic · write
Syntax:         /resume <workflow-id>
Arguments:
  - workflow-id · string · required: yes · workflow id · default: - · error: "usage: /resume <workflow-id>"
Backend:        POST /v1/commands action=resume (src/engineering_control/operations/service.py)
Preconditions:  WORKFLOW_IS_NOT_PAUSED when not paused.
Reply:          Immediate: the OperationView JSON of the typed command (`operation_id`, `action`, `target_type`, `target_id`, `source_channel: telegram`, `status` COMPLETED|BLOCKED|FAILED|PENDING, `workflow_id`, `temporal_run_id`, `binding_hash`, `safe_error`, `created_at`, `result_available`) (src/engineering_control/operations/models.py:326-339), rendered by the hook as `{"next_cursor": null, "payload": {...}}`. BLOCKED carries `safe_error` = `operation preflight changed: <BLOCKER>` or the policy error text (operations/service.py:97-110).
Side effects:   Temporal signal `resume`
Related:        /pause
Example:        /resume audit-falcone-<uuid>
Evidence:       integrations/hermes-engineering-control/hook/handler.py:614-617; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /cancel

```
Purpose:        Cancel a bounded workflow (`cancel` signal).
Type:           deterministic · write
Syntax:         /cancel <workflow-id>
Arguments:
  - workflow-id · string · required: yes · workflow id · default: - · error: "usage: /cancel <workflow-id>"
Backend:        POST /v1/commands action=cancel (src/engineering_control/operations/service.py)
Preconditions:  WORKFLOW_IS_TERMINAL when already closed.
Reply:          Immediate: the OperationView JSON of the typed command (`operation_id`, `action`, `target_type`, `target_id`, `source_channel: telegram`, `status` COMPLETED|BLOCKED|FAILED|PENDING, `workflow_id`, `temporal_run_id`, `binding_hash`, `safe_error`, `created_at`, `result_available`) (src/engineering_control/operations/models.py:326-339), rendered by the hook as `{"next_cursor": null, "payload": {...}}`. BLOCKED carries `safe_error` = `operation preflight changed: <BLOCKER>` or the policy error text (operations/service.py:97-110).
Side effects:   Temporal signal `cancel`; backlog loops record CANCELLED and release leases (NEW-60)
Related:        /status
Example:        /cancel backlog-loop-llmwiki-<campaign>-<uuid>
Evidence:       integrations/hermes-engineering-control/hook/handler.py:614-617; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop status

```
Purpose:        Show a loop workflow's projection.
Type:           deterministic · read
Syntax:         /loop status <workflow-id>
Arguments:
  - workflow-id · string · required: yes · - · default: -
Backend:        GET /v1/loops/{workflow_id} (src/engineering_control/api/main.py:841)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419). Real sample: `{"workflow_id":"backlog-loop-musematic-66a97948-…-27604282-…","run_id":"01a0df61-…","status":"COMPLETED","workflow_type":"ExistingGitHubBacklogLoopWorkflow"}`.
Side effects:   none
Related:        /loop tail
Example:        /loop status backlog-loop-musematic-66a97948-9f9d-4ae9-b204-e89f27e2712d-27604282-30bc-4052-8dc1-12f92bcd7dea
Evidence:       integrations/hermes-engineering-control/hook/handler.py:1052-1056; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop tail

```
Purpose:        Show the last N dashboard events of a workflow (phases, provider start/finish).
Type:           deterministic · read
Syntax:         /loop tail <workflow-id> <1-100>
Arguments:
  - workflow-id · string · required: yes · - · default: -
  - limit · int · required: yes · 1..100 · default: - · error: "tail limit must be between 1 and 100"
Backend:        GET /v1/workflows/{id}/tail (src/engineering_control/api/main.py:479)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419). Real event types: request.accepted, workflow.queued, workflow.started, provider.execution_started/completed, provider.observability_incomplete, workflow.completed (no model or token data).
Side effects:   none
Related:        -
Example:        /loop tail backlog-loop-musematic-66a97948-…-issue-1-retry-5-attempt-1 50
Evidence:       integrations/hermes-engineering-control/hook/handler.py:827-841; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop diagnose

```
Purpose:        Alias of /status for a loop workflow.
Type:           deterministic · read
Syntax:         /loop diagnose <workflow-id>
Arguments:
  - workflow-id · string · required: yes · - · default: -
Backend:        GET /v1/runs/{id} (src/engineering_control/api/main.py:979)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419).
Side effects:   none
Related:        -
Example:        /loop diagnose <workflow-id>
Evidence:       integrations/hermes-engineering-control/hook/handler.py:827-833; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop nudge

```
Purpose:        Nudge a workflow (typed control `nudge`).
Type:           deterministic · write
Syntax:         /loop nudge <workflow-id>
Arguments:
  - workflow-id · string · required: yes · - · default: -
Backend:        POST /v1/commands action=nudge (src/engineering_control/operations/service.py:318-336)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the OperationView JSON of the typed command (`operation_id`, `action`, `target_type`, `target_id`, `source_channel: telegram`, `status` COMPLETED|BLOCKED|FAILED|PENDING, `workflow_id`, `temporal_run_id`, `binding_hash`, `safe_error`, `created_at`, `result_available`) (src/engineering_control/operations/models.py:326-339), rendered by the hook as `{"next_cursor": null, "payload": {...}}`. BLOCKED carries `safe_error` = `operation preflight changed: <BLOCKER>` or the policy error text (operations/service.py:97-110).
Side effects:   Temporal `retry` signal with value `operator-nudge` (operations/service.py:327-337). Loops only act on a retry whose value equals `gntik-ai/<repo>#<n>` (D:2644-2647, D:3342-3345) and write-policy waits only on the step name (D:216-228), so a nudge wakes the wait but changes nothing — effectively a no-op
Related:        -
Example:        /loop nudge <workflow-id>
Evidence:       integrations/hermes-engineering-control/hook/handler.py:827-842; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop pause

```
Purpose:        Pause a loop at its next checkpoint.
Type:           deterministic · write
Syntax:         /loop pause <workflow-id>
Arguments:
  - workflow-id · string · required: yes · - · default: -
Backend:        POST /v1/commands action=pause (src/engineering_control/operations/service.py)
Preconditions:  CONTROL_REQUIRES_BACKLOG_CAMPAIGN for backlog-loop ids without a campaign.
Reply:          Immediate: the OperationView JSON of the typed command (`operation_id`, `action`, `target_type`, `target_id`, `source_channel: telegram`, `status` COMPLETED|BLOCKED|FAILED|PENDING, `workflow_id`, `temporal_run_id`, `binding_hash`, `safe_error`, `created_at`, `result_available`) (src/engineering_control/operations/models.py:326-339), rendered by the hook as `{"next_cursor": null, "payload": {...}}`. BLOCKED carries `safe_error` = `operation preflight changed: <BLOCKER>` or the policy error text (operations/service.py:97-110).
Side effects:   Temporal `pause` signal
Related:        /loop resume
Example:        /loop pause <loop-workflow-id>
Evidence:       integrations/hermes-engineering-control/hook/handler.py:1057-1062; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop resume

```
Purpose:        Resume a paused loop.
Type:           deterministic · write
Syntax:         /loop resume <workflow-id>
Arguments:
  - workflow-id · string · required: yes · - · default: -
Backend:        POST /v1/commands action=resume
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the OperationView JSON of the typed command (`operation_id`, `action`, `target_type`, `target_id`, `source_channel: telegram`, `status` COMPLETED|BLOCKED|FAILED|PENDING, `workflow_id`, `temporal_run_id`, `binding_hash`, `safe_error`, `created_at`, `result_available`) (src/engineering_control/operations/models.py:326-339), rendered by the hook as `{"next_cursor": null, "payload": {...}}`. BLOCKED carries `safe_error` = `operation preflight changed: <BLOCKER>` or the policy error text (operations/service.py:97-110).
Side effects:   Temporal `resume` signal
Related:        -
Example:        /loop resume <loop-workflow-id>
Evidence:       integrations/hermes-engineering-control/hook/handler.py:1057-1062; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop cancel

```
Purpose:        Cancel a loop (releases the campaign's leases).
Type:           deterministic · write
Syntax:         /loop cancel <workflow-id>
Arguments:
  - workflow-id · string · required: yes · - · default: -
Backend:        POST /v1/commands action=cancel
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the OperationView JSON of the typed command (`operation_id`, `action`, `target_type`, `target_id`, `source_channel: telegram`, `status` COMPLETED|BLOCKED|FAILED|PENDING, `workflow_id`, `temporal_run_id`, `binding_hash`, `safe_error`, `created_at`, `result_available`) (src/engineering_control/operations/models.py:326-339), rendered by the hook as `{"next_cursor": null, "payload": {...}}`. BLOCKED carries `safe_error` = `operation preflight changed: <BLOCKER>` or the policy error text (operations/service.py:97-110).
Side effects:   Temporal `cancel`; campaign CANCELLED; leases released
Related:        -
Example:        /loop cancel <loop-workflow-id>
Evidence:       integrations/hermes-engineering-control/hook/handler.py:1057-1062; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

### Group: exports

#### /evidence

```
Purpose:        Show redacted metadata of one evidence artifact.
Type:           deterministic · read
Syntax:         /evidence <artifact-id>
Arguments:
  - artifact-id · string · required: yes · artifact id (URL-quoted) · default: - · error: "usage: /evidence <artifact-id>"
Backend:        GET /v1/evidence/{artifact_id} (src/engineering_control/api/main.py:1113)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419).
Side effects:   none
Related:        -
Example:        /evidence <artifact-id from a run>
Evidence:       integrations/hermes-engineering-control/hook/handler.py:584-587; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /export-campaign

```
Purpose:        Create a bounded redacted JSON export of one campaign.
Type:           deterministic · write
Syntax:         /export-campaign <campaign-uuid>
Arguments:
  - campaign-uuid · UUID · required: yes · UUID · default: - · error: "campaign ID must be a UUID"
Backend:        POST /v1/commands action=evidence_export_campaign format=json (src/engineering_control/operations/service.py)
Preconditions:  CAMPAIGN_NOT_FOUND.
Reply:          Immediate: the OperationView JSON of the typed command (`operation_id`, `action`, `target_type`, `target_id`, `source_channel: telegram`, `status` COMPLETED|BLOCKED|FAILED|PENDING, `workflow_id`, `temporal_run_id`, `binding_hash`, `safe_error`, `created_at`, `result_available`) (src/engineering_control/operations/models.py:326-339), rendered by the hook as `{"next_cursor": null, "payload": {...}}`. BLOCKED carries `safe_error` = `operation preflight changed: <BLOCKER>` or the policy error text (operations/service.py:97-110). The export body is fetched later with `engineering-ctl operation-export <operation-id>` (`GET /v1/operations/{id}/export`, A:219) — there is no Telegram command for it.
Side effects:   export record in PostgreSQL
Related:        /campaign
Example:        /export-campaign 4123dbd9-cdb7-412a-adc4-bab08f7ebe47
Evidence:       integrations/hermes-engineering-control/hook/handler.py:588-600; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /export-workflow

```
Purpose:        Create a bounded redacted JSON export of one workflow.
Type:           deterministic · write
Syntax:         /export-workflow <workflow-id>
Arguments:
  - workflow-id · string · required: yes · [A-Za-z0-9._:-]{1,240} · default: - · error: "usage: /export-workflow <workflow-id>"
Backend:        POST /v1/commands action=evidence_export_workflow (src/engineering_control/operations/service.py)
Preconditions:  WORKFLOW_NOT_FOUND.
Reply:          Immediate: the OperationView JSON of the typed command (`operation_id`, `action`, `target_type`, `target_id`, `source_channel: telegram`, `status` COMPLETED|BLOCKED|FAILED|PENDING, `workflow_id`, `temporal_run_id`, `binding_hash`, `safe_error`, `created_at`, `result_available`) (src/engineering_control/operations/models.py:326-339), rendered by the hook as `{"next_cursor": null, "payload": {...}}`. BLOCKED carries `safe_error` = `operation preflight changed: <BLOCKER>` or the policy error text (operations/service.py:97-110). Body only via CLI `operation-export`.
Side effects:   export record
Related:        -
Example:        /export-workflow backlog-loop-musematic-66a97948-…-27604282-…
Evidence:       integrations/hermes-engineering-control/hook/handler.py:601-613; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

### Group: audits

#### /audit-plan

```
Purpose:        Validate and show the audit plan for one product/preset/environment (AuditCampaignWorkflow, plan_only).
Type:           deterministic · write (starts a plan-only workflow)
Syntax:         /audit-plan <falcone|musematic|llmwiki> <preset> <environment>
Arguments:
  - project · enum · required: yes · falcone | musematic | llmwiki · default: - · error: "usage: /audit-plan …"
  - preset · enum · required: yes · snapshot | static-full | e2e-smoke | full-e2e | security | ux | deployment | documentation | falcone-intensive · default: - · error: "usage"
  - environment · string · required: yes · llmwiki: staging only · default: - · error: "LLMWiki supports staging only; falcone-intensive is bound to Falcone"
Backend:        POST /v1/commands action=audit_plan (src/engineering_control/operations/service.py:262-271) → ControlService.plan_campaign execute_workflow plan_only (src/engineering_control/api/service.py:125) → AuditCampaignWorkflow
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the OperationView JSON of the typed command (`operation_id`, `action`, `target_type`, `target_id`, `source_channel: telegram`, `status` COMPLETED|BLOCKED|FAILED|PENDING, `workflow_id`, `temporal_run_id`, `binding_hash`, `safe_error`, `created_at`, `result_available`) (src/engineering_control/operations/models.py:326-339), rendered by the hook as `{"next_cursor": null, "payload": {...}}`. BLOCKED carries `safe_error` = `operation preflight changed: <BLOCKER>` or the policy error text (operations/service.py:97-110).
Side effects:   Temporal: short AuditCampaignWorkflow (identity attest + campaign.plan)
Related:        /audit … --execute
Example:        /audit-plan falcone static-full staging
Evidence:       integrations/hermes-engineering-control/hook/handler.py:631-650; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /audit

```
Purpose:        Plan (default) or start a bounded audit campaign for one product; advanced 8/9-argument form pins SHAs and one cell.
Type:           deterministic · write
Syntax:         /audit <project> <preset> <environment> [--execute] [--publish-findings]  |  /audit <project> <name> <source-sha> <deployment-sha> <domain> <persona> <lens> <environment> [--execute]
Arguments:
  - project · enum · required: yes · falcone | musematic | llmwiki · default: - · error: "usage"
  - preset · enum · required: yes (short form) · snapshot | static-full | e2e-smoke | full-e2e | security | ux | deployment | documentation | falcone-intensive · default: - · error: "usage"
  - environment · string · required: yes · llmwiki: staging · default: - · error: "LLMWiki supports staging only"
  - --execute · flag · required: no · start instead of plan · default: plan · error: "audit flags are not allowlisted"
  - --publish-findings · flag · required: no · requires --execute · default: off · error: "--publish-findings requires --execute"
  - source-sha / deployment-sha (advanced) · 40-hex · required: yes · full lowercase SHA · default: - · error: "source and deployment SHA must be full lowercase Git SHAs"
Backend:        short: POST /v1/commands action=audit_plan|audit_start (src/engineering_control/operations/service.py:262-282); advanced: POST /v1/campaigns (src/engineering_control/api/main.py:550 → src/engineering_control/api/service.py:152) → AuditCampaignWorkflow (+ FindingVerificationWorkflow children)
Preconditions:  audit_start requires a COMPLETED audit_plan for the same scope first → BLOCKED `AUDIT_PLAN_REQUIRED` (operations/repository.py:839-849). E2E/security presets call identity.superadmin_attest (musematic superadmin login currently fails, see flows.md).
Reply:          Immediate: the OperationView JSON of the typed command (`operation_id`, `action`, `target_type`, `target_id`, `source_channel: telegram`, `status` COMPLETED|BLOCKED|FAILED|PENDING, `workflow_id`, `temporal_run_id`, `binding_hash`, `safe_error`, `created_at`, `result_available`) (src/engineering_control/operations/models.py:326-339), rendered by the hook as `{"next_cursor": null, "payload": {...}}`. BLOCKED carries `safe_error` = `operation preflight changed: <BLOCKER>` or the policy error text (operations/service.py:97-110). Later: when the operation starts a workflow the notifier posts one `telegram.engineering-operation-started` message (`operation_id`, `project`, `target`, `workflow_id`, `phase: QUEUED`) (src/engineering_control/operations/repository.py:1576-1592). No completion message is sent; poll with `/status` or `/loop status`.
Side effects:   Temporal AuditCampaignWorkflow; worker snapshots/worktrees; with --publish-findings: GitHub issues (write-policy gated)
Related:        /audit-plan first; then /findings, /verify
Example:        /audit falcone static-full staging --execute
Evidence:       integrations/hermes-engineering-control/hook/handler.py:674-719; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /audit-all

```
Purpose:        Plan or start fixed sequential staging audits for falcone, musematic and llmwiki.
Type:           deterministic · write
Syntax:         /audit-all <plan|start> <preset> staging [--publish-findings]
Arguments:
  - mode · enum · required: yes · plan | start · default: - · error: "usage"
  - preset · enum · required: yes · snapshot | static-full | e2e-smoke | full-e2e | security | ux | deployment | documentation · default: - · error: "usage"
  - environment · literal · required: yes · staging · default: - · error: "usage"
  - --publish-findings · flag · required: no · start only · default: off · error: "usage"
Backend:        POST /v1/commands action=audit_all_projects (src/engineering_control/operations/service.py:283-304): plan → 3× plan_campaign; start → MultiProjectAuditWorkflow (src/engineering_control/api/service.py:174)
Preconditions:  start requires a completed all-project plan (`ALL_PROJECT_AUDIT_PLAN_REQUIRED`) and none active (`ALL_PROJECT_AUDIT_ALREADY_ACTIVE`) (operations/repository.py:862-884).
Reply:          Immediate: the OperationView JSON of the typed command (`operation_id`, `action`, `target_type`, `target_id`, `source_channel: telegram`, `status` COMPLETED|BLOCKED|FAILED|PENDING, `workflow_id`, `temporal_run_id`, `binding_hash`, `safe_error`, `created_at`, `result_available`) (src/engineering_control/operations/models.py:326-339), rendered by the hook as `{"next_cursor": null, "payload": {...}}`. BLOCKED carries `safe_error` = `operation preflight changed: <BLOCKER>` or the policy error text (operations/service.py:97-110). Later: when the operation starts a workflow the notifier posts one `telegram.engineering-operation-started` message (`operation_id`, `project`, `target`, `workflow_id`, `phase: QUEUED`) (src/engineering_control/operations/repository.py:1576-1592). No completion message is sent; poll with `/status` or `/loop status`.
Side effects:   Temporal MultiProjectAuditWorkflow → 3 AuditCampaignWorkflow children
Related:        /campaigns, /campaign
Example:        /audit-all plan static-full staging
Evidence:       integrations/hermes-engineering-control/hook/handler.py:651-673; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /campaigns

```
Purpose:        List audit campaigns.
Type:           deterministic · read
Syntax:         /campaigns [limit]
Arguments:
  - limit · int · required: no · 1..100 · default: 20 · error: "limit must be between 1 and 100"
Backend:        GET /v1/campaigns?limit= (src/engineering_control/api/main.py:584)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419).
Side effects:   none
Related:        /campaign
Example:        /campaigns 10
Evidence:       integrations/hermes-engineering-control/hook/handler.py:526-530; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /campaign

```
Purpose:        Show one audit, imported-backlog or global campaign.
Type:           deterministic · read
Syntax:         /campaign <campaign-id>
Arguments:
  - campaign-id · UUID · required: yes · UUID · default: - · error: "usage: /campaign <campaign-id>"
Backend:        GET /v1/campaign-detail/{id} (src/engineering_control/api/main.py:464)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419).
Side effects:   none
Related:        /export-campaign
Example:        /campaign 4123dbd9-cdb7-412a-adc4-bab08f7ebe47
Evidence:       integrations/hermes-engineering-control/hook/handler.py:531-534; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /findings

```
Purpose:        List verified findings.
Type:           deterministic · read
Syntax:         /findings [limit]
Arguments:
  - limit · int · required: no · 1..100 · default: 20 · error: "limit must be between 1 and 100"
Backend:        GET /v1/findings?limit= (src/engineering_control/api/main.py:612)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419).
Side effects:   none
Related:        /verify, /run
Example:        /findings 20
Evidence:       integrations/hermes-engineering-control/hook/handler.py:526-530; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /verify

```
Purpose:        Start independent verification of one candidate finding.
Type:           deterministic · write
Syntax:         /verify <finding-uuid>
Arguments:
  - finding-uuid · UUID · required: yes · UUID · default: - · error: "finding ID must be a UUID"
Backend:        POST /v1/findings/verify (src/engineering_control/api/main.py:950 → src/engineering_control/api/service.py:206) → FindingVerificationWorkflow
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: `OperationAccepted` JSON `{workflow_id, run_id, status: "ACCEPTED"}` (src/engineering_control/api/schemas.py:330-333) wrapped as `{"next_cursor": null, "payload": {...}}`. No later message; poll with `/status <workflow-id>`.
Side effects:   Temporal FindingVerificationWorkflow (2 provider reproductions)
Related:        /findings, /run
Example:        /verify <finding-uuid from /findings>
Evidence:       integrations/hermes-engineering-control/hook/handler.py:567-571; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

### Group: issue loop

#### /run

```
Purpose:        Start source-PR delivery for one confirmed finding (IssueDeliveryWorkflow); dry run unless --execute.
Type:           deterministic · write
Syntax:         /run <finding-uuid> <R0|R1|R2|R3> [--execute]
Arguments:
  - finding-uuid · UUID · required: yes · UUID · default: - · error: "finding ID must be a UUID"
  - risk · enum · required: yes · R0 | R1 | R2 | R3 · default: - · error: "usage"
  - --execute · flag · required: no · - · default: dry_run=true
Backend:        POST /v1/deliveries (src/engineering_control/api/main.py:961 → src/engineering_control/api/service.py:193) → IssueDeliveryWorkflow
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: `OperationAccepted` JSON `{workflow_id, run_id, status: "ACCEPTED"}` (src/engineering_control/api/schemas.py:330-333) wrapped as `{"next_cursor": null, "payload": {...}}`. No later message; poll with `/status <workflow-id>`.
Side effects:   worktrees, provider CLIs; with --execute a GitHub source PR (write-policy gated)
Related:        /verify first; /status
Example:        /run <confirmed-finding-uuid> R1
Evidence:       integrations/hermes-engineering-control/hook/handler.py:618-630; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop import

```
Purpose:        Import open GitHub issues of one product as a backlog campaign (GitHubBacklogImportWorkflow).
Type:           deterministic · write (read-only GitHub import workflow)
Syntax:         /loop import <falcone|musematic|llmwiki> github-open <bugs|bugs-and-docs|security|enhancements|all> [max-items]
Arguments:
  - project · enum · required: yes · falcone | musematic | llmwiki · default: - · error: "usage"
  - source · literal · required: yes · github-open · default: - · error: "usage"
  - filter · enum · required: yes · bugs | bugs-and-docs | security | enhancements | all · default: - · error: "usage"
  - max-items · int · required: no · 1..500 · default: 500 · error: "backlog import max-items must be between 1 and 500"
Backend:        POST /v1/backlogs/import (src/engineering_control/api/main.py:687 → src/engineering_control/api/service.py:422) → GitHubBacklogImportWorkflow
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: `BacklogImportAccepted` `{workflow_id, run_id, status: ACCEPTED, backlog_campaign_id}` (api/schemas.py:336-337).
Side effects:   GitHub read; PostgreSQL backlog rows
Related:        /loop inventory, /loop issue-locate
Example:        /loop import falcone github-open bugs 20
Evidence:       integrations/hermes-engineering-control/hook/handler.py:906-930; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop inventory

```
Purpose:        List a backlog campaign's imported issues with risk/eligibility.
Type:           deterministic · read
Syntax:         /loop inventory <project> <backlog-campaign-id> [limit]
Arguments:
  - project · enum · required: yes · - · default: -
  - campaign · UUID · required: yes · - · default: -
  - limit · int · required: no · 1..200 · default: 20 · error: "inventory limit must be between 1 and 200"
Backend:        GET /v1/backlogs/{id}/issues?limit= (src/engineering_control/api/main.py:716)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419).
Side effects:   none
Related:        /loop issue
Example:        /loop inventory musematic 66a97948-9f9d-4ae9-b204-e89f27e2712d 5
Evidence:       integrations/hermes-engineering-control/hook/handler.py:931-941; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop queue

```
Purpose:        Same listing as /loop inventory without the project argument.
Type:           deterministic · read
Syntax:         /loop queue <backlog-campaign-id> [limit]
Arguments:
  - campaign · UUID · required: yes · - · default: -
  - limit · int · required: no · 1..200 · default: 20
Backend:        GET /v1/backlogs/{id}/issues (src/engineering_control/api/main.py:716)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419).
Side effects:   none
Related:        -
Example:        /loop queue 4123dbd9-cdb7-412a-adc4-bab08f7ebe47
Evidence:       integrations/hermes-engineering-control/hook/handler.py:952-959; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop issue

```
Purpose:        Show one imported issue record (risk, eligibility, reproduction, PRs).
Type:           deterministic · read
Syntax:         /loop issue <backlog-campaign-id> <repository-short-name> <issue-number>
Arguments:
  - campaign · UUID · required: yes · - · default: -
  - repository · enum · required: yes · falcone | falcone-charts | musematic | musematic-deploy | llmwiki · default: - · error: "usage"
  - issue-number · int ≥1 · required: yes · - · default: -
Backend:        GET /v1/backlogs/{id}/issues/{repo}/{n} (src/engineering_control/api/main.py:727)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419).
Side effects:   none
Related:        -
Example:        /loop issue 3f24fc41-0ab0-4cf7-88a5-9b2491e79eee falcone-charts 45
Evidence:       integrations/hermes-engineering-control/hook/handler.py:960-976; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop issue-locate

```
Purpose:        Find the current backlog record for a product issue by number.
Type:           deterministic · read
Syntax:         /loop issue-locate <project> <source|deployment> <issue-number>
Arguments:
  - project · enum · required: yes · - · default: -
  - surface · enum · required: yes · source | deployment · default: -
  - issue-number · int ≥1 · required: yes · - · default: -
Backend:        GET /v1/backlogs/locate (src/engineering_control/api/main.py:492)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419). Not imported → `Engineering Control: Engineering API rejected the request (404): {"detail":"issue is not in a current backlog inventory"}` (real, llmwiki #8).
Side effects:   none
Related:        /loop issue-start
Example:        /loop issue-locate falcone deployment 45
Evidence:       integrations/hermes-engineering-control/hook/handler.py:786-810; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop issue-start

```
Purpose:        Resolve one imported issue end to end (ExistingGitHubBacklogLoopWorkflow, max risk R2, correction limit 5).
Type:           deterministic · write
Syntax:         /loop issue-start <project> <source|deployment> <issue-number> [source-pr|full-delivery]
Arguments:
  - project · enum · required: yes · - · default: -
  - surface · enum · required: yes · source | deployment · default: -
  - issue-number · int ≥1 · required: yes · - · default: -
  - mode · enum · required: no · source-pr | full-delivery · default: source-pr · error: "usage"
Backend:        GET /v1/backlogs/locate, then POST /v1/commands action=solve_issue (src/engineering_control/operations/service.py:204-230: prepare approval → approve → wait ≤60 s for write-policy activation → start_existing_backlog_loop) → ExistingGitHubBacklogLoopWorkflow
Preconditions:  Preflight blockers (operations/repository.py:716-773): BACKLOG_CAMPAIGN_ALREADY_ACTIVE, ISSUE_NOT_IN_PERSISTED_PROJECT_BACKLOG, GITHUB_ISSUE_NOT_OPEN, GITHUB_REFERENCE_IS_PULL_REQUEST, ISSUE_RISK_EXCEEDS_BOUND (R3 issues — the hook fixes maximum risk at R2), ISSUE_WORKFLOW_ALREADY_ACTIVE, <PROVIDER>_AUTH_NOT_READY/_RATE_LIMITED/_HEALTH_STALE, FULL_DELIVERY_* contract blockers. Policy errors: `another backlog campaign policy activation is pending`, `GitHub write policy activation did not complete; check engineering-github-policy-reconciler before starting` (operations/service.py:187-190).
Reply:          Immediate: the OperationView JSON of the typed command (`operation_id`, `action`, `target_type`, `target_id`, `source_channel: telegram`, `status` COMPLETED|BLOCKED|FAILED|PENDING, `workflow_id`, `temporal_run_id`, `binding_hash`, `safe_error`, `created_at`, `result_available`) (src/engineering_control/operations/models.py:326-339), rendered by the hook as `{"next_cursor": null, "payload": {...}}`. BLOCKED carries `safe_error` = `operation preflight changed: <BLOCKER>` or the policy error text (operations/service.py:97-110). Later: when the operation starts a workflow the notifier posts one `telegram.engineering-operation-started` message (`operation_id`, `project`, `target`, `workflow_id`, `phase: QUEUED`) (src/engineering_control/operations/repository.py:1576-1592). No completion message is sent; poll with `/status` or `/loop status`. While a GitHub write is denied the loop sends the write-policy alert (see /loop approve).
Side effects:   GitHub write policy for the campaign (24 h, one owner), worktrees, provider CLIs, GitHub PR/merge/tag/issue close; with full-delivery Argo CD sync of staging
Related:        /loop issue-locate → /loop issue-start → /loop status → /loop tail
Example:        /loop issue-start falcone deployment 45 full-delivery
Evidence:       integrations/hermes-engineering-control/hook/handler.py:786-826; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop start

```
Purpose:        Work a backlog campaign (up to max-items issues) with ExistingGitHubBacklogLoopWorkflow.
Type:           deterministic · write
Syntax:         /loop start <project> <campaign-id> <max-risk> <source-pr|full-delivery> [max-items]
Arguments:
  - project · enum · required: yes · - · default: -
  - campaign · UUID · required: yes · - · default: -
  - max-risk · enum · required: yes · R0..R3 · default: -
  - mode · enum · required: yes · source-pr | full-delivery (hook also accepts plan-only|preview|staging, which the command model rejects) · default: - · error: "usage"
  - max-items · int · required: no · 1..50 · default: 1 · error: "max-items must be between 1 and 50"
Backend:        POST /v1/commands action=solve_backlog (src/engineering_control/operations/service.py:234-262) → ExistingGitHubBacklogLoopWorkflow
Preconditions:  As /loop issue-start (self-approves the candidate, waits ≤60 s for policy activation); max-risk R3 → R3_OPERATOR_ACTION_REQUIRED (operations/repository.py:772).
Reply:          Immediate: the OperationView JSON of the typed command (`operation_id`, `action`, `target_type`, `target_id`, `source_channel: telegram`, `status` COMPLETED|BLOCKED|FAILED|PENDING, `workflow_id`, `temporal_run_id`, `binding_hash`, `safe_error`, `created_at`, `result_available`) (src/engineering_control/operations/models.py:326-339), rendered by the hook as `{"next_cursor": null, "payload": {...}}`. BLOCKED carries `safe_error` = `operation preflight changed: <BLOCKER>` or the policy error text (operations/service.py:97-110). Later: when the operation starts a workflow the notifier posts one `telegram.engineering-operation-started` message (`operation_id`, `project`, `target`, `workflow_id`, `phase: QUEUED`) (src/engineering_control/operations/repository.py:1576-1592). No completion message is sent; poll with `/status` or `/loop status`.
Side effects:   as /loop issue-start, for several issues sequentially (one at a time)
Related:        /loop status, /loop pause|resume|cancel
Example:        /loop start llmwiki cd217170-1a35-464a-9e81-93d36c5a76fa R1 source-pr 1
Evidence:       integrations/hermes-engineering-control/hook/handler.py:1025-1051; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop plan

```
Purpose:        Legacy: plan an IssueSolutionLoop for a campaign (returns the backlog record when the id is an imported backlog).
Type:           deterministic · write (plan workflow)
Syntax:         /loop plan <project> <campaign-id>
Arguments:
  - project · enum · required: yes · - · default: -
  - campaign · UUID · required: yes · - · default: -
Backend:        POST /v1/loop-plan (src/engineering_control/api/main.py:629) → IssueSolutionLoopWorkflow
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: `OperationAccepted` JSON `{workflow_id, run_id, status: "ACCEPTED"}` (src/engineering_control/api/schemas.py:330-333) wrapped as `{"next_cursor": null, "payload": {...}}`. No later message; poll with `/status <workflow-id>`.
Side effects:   none
Related:        /loop start
Example:        /loop plan musematic <audit-campaign-uuid>
Evidence:       integrations/hermes-engineering-control/hook/handler.py:942-951; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop skip

```
Purpose:        Skip the issue a paused loop is waiting on, with an allowlisted reason.
Type:           deterministic · write
Syntax:         /loop skip <workflow-id> <repository-short-name> <issue-number> <reason-code>
Arguments:
  - workflow-id · string · required: yes · - · default: -
  - repository · enum · required: yes · falcone | falcone-charts | musematic | musematic-deploy | llmwiki · default: -
  - issue-number · int ≥1 · required: yes · - · default: -
  - reason · enum · required: yes · DUPLICATE | ALREADY_FIXED | SUPERSEDED | NEEDS_PRODUCT_DECISION | BLOCKED_BY_DEPENDENCY | BLOCKED_BY_RUNTIME_BASELINE | R3_REQUIRES_OPERATOR | NOT_REPRODUCIBLE | ENHANCEMENT_DEFERRED · default: - · error: "usage"
Backend:        POST /v1/commands action=skip → `skip` signal `gntik-ai/<repo>#<n>|<reason>` (operations/service.py:338-346; D:2626-2643, D:3326-3341)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the OperationView JSON of the typed command (`operation_id`, `action`, `target_type`, `target_id`, `source_channel: telegram`, `status` COMPLETED|BLOCKED|FAILED|PENDING, `workflow_id`, `temporal_run_id`, `binding_hash`, `safe_error`, `created_at`, `result_available`) (src/engineering_control/operations/models.py:326-339), rendered by the hook as `{"next_cursor": null, "payload": {...}}`. BLOCKED carries `safe_error` = `operation preflight changed: <BLOCKER>` or the policy error text (operations/service.py:97-110).
Side effects:   Temporal signal; backlog record SKIPPED
Related:        /loop retry
Example:        /loop skip <loop-id> musematic 1119 BLOCKED_BY_RUNTIME_BASELINE
Evidence:       integrations/hermes-engineering-control/hook/handler.py:1063-1086; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop retry

```
Purpose:        Retry the issue a paused loop is waiting on (`retry` signal with `<repo>#<n>`).
Type:           deterministic · write
Syntax:         /loop retry <workflow-id> <repository-short-name> <issue-number>
Arguments:
  - workflow-id · string · required: yes · - · default: -
  - repository · enum · required: yes · - · default: -
  - issue-number · int ≥1 · required: yes · - · default: -
Backend:        POST /v1/commands action=retry (D:2644-2647, D:3342-3345)
Preconditions:  WORKFLOW_IS_NOT_RETRYABLE when the loop is not PAUSED/awaiting operator (operations/repository.py:897-905).
Reply:          Immediate: the OperationView JSON of the typed command (`operation_id`, `action`, `target_type`, `target_id`, `source_channel: telegram`, `status` COMPLETED|BLOCKED|FAILED|PENDING, `workflow_id`, `temporal_run_id`, `binding_hash`, `safe_error`, `created_at`, `result_available`) (src/engineering_control/operations/models.py:326-339), rendered by the hook as `{"next_cursor": null, "payload": {...}}`. BLOCKED carries `safe_error` = `operation preflight changed: <BLOCKER>` or the policy error text (operations/service.py:97-110).
Side effects:   Temporal `retry` signal
Related:        /loop tail
Example:        /loop retry <loop-id> musematic 1125
Evidence:       integrations/hermes-engineering-control/hook/handler.py:1087-1108; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop retry-issue

```
Purpose:        Retry by product issue number (locates the repository first).
Type:           deterministic · write
Syntax:         /loop retry-issue <workflow-id> <project> <source|deployment> <issue-number>
Arguments:
  - workflow-id · string · required: yes · - · default: -
  - project · enum · required: yes · - · default: -
  - surface · enum · required: yes · - · default: -
  - issue-number · int ≥1 · required: yes · - · default: -
Backend:        GET /v1/backlogs/locate + POST /v1/commands action=retry
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the OperationView JSON of the typed command (`operation_id`, `action`, `target_type`, `target_id`, `source_channel: telegram`, `status` COMPLETED|BLOCKED|FAILED|PENDING, `workflow_id`, `temporal_run_id`, `binding_hash`, `safe_error`, `created_at`, `result_available`) (src/engineering_control/operations/models.py:326-339), rendered by the hook as `{"next_cursor": null, "payload": {...}}`. BLOCKED carries `safe_error` = `operation preflight changed: <BLOCKER>` or the policy error text (operations/service.py:97-110).
Side effects:   Temporal `retry` signal
Related:        -
Example:        /loop retry-issue <loop-id> musematic source 1125
Evidence:       integrations/hermes-engineering-control/hook/handler.py:843-871; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop global-start

```
Purpose:        Start the global autonomous selector over the three product backlogs (staging, R2, one issue at a time).
Type:           deterministic · write
Syntax:         /loop global-start <global-campaign-id> <policy-campaign-id> <falcone-campaign-id> <musematic-campaign-id> <llmwiki-campaign-id> START_GLOBAL_STAGING_R2_BACKLOG
Arguments:
  - 5 campaign ids · UUID · required: yes · - · default: - · error: "campaign ID must be a UUID"
  - phrase · literal · required: yes · START_GLOBAL_STAGING_R2_BACKLOG · default: - · error: "usage"
Backend:        POST /v1/commands action=start_autonomous_backlog (src/engineering_control/operations/service.py:201) → AutonomousBacklogCampaignWorkflow
Preconditions:  <PROJECT>_BACKLOG_INVENTORY_INVALID / _APPROVAL_NOT_CURRENT, GITHUB_WRITE_POLICY_NOT_ACTIVE unless the policy campaign is the approved owner (operations/repository.py:681-715), BACKLOG_CAMPAIGN_ALREADY_ACTIVE; PolicyViolation outside the staging R2 boundary (D:3393-3407).
Reply:          Immediate: the OperationView JSON of the typed command (`operation_id`, `action`, `target_type`, `target_id`, `source_channel: telegram`, `status` COMPLETED|BLOCKED|FAILED|PENDING, `workflow_id`, `temporal_run_id`, `binding_hash`, `safe_error`, `created_at`, `result_available`) (src/engineering_control/operations/models.py:326-339), rendered by the hook as `{"next_cursor": null, "payload": {...}}`. BLOCKED carries `safe_error` = `operation preflight changed: <BLOCKER>` or the policy error text (operations/service.py:97-110). Later: when the operation starts a workflow the notifier posts one `telegram.engineering-operation-started` message (`operation_id`, `project`, `target`, `workflow_id`, `phase: QUEUED`) (src/engineering_control/operations/repository.py:1576-1592). No completion message is sent; poll with `/status` or `/loop status`.
Side effects:   as /loop start across all products
Related:        -
Example:        /loop global-start <5 uuids> START_GLOBAL_STAGING_R2_BACKLOG
Evidence:       integrations/hermes-engineering-control/hook/handler.py:755-785; last observed use: never run in the retained 30-day history
```

#### /loop delivery-candidate

```
Purpose:        REMOVED in repo (NEW-01); still in the installed ~/.hermes copy, where it calls an API action that returns 404.
Type:           deterministic · read  [BROKEN]
Syntax:         /loop delivery-candidate <workflow-id> <repository-short-name> <issue-number>
Arguments:      none
Backend:        GET /v1/backlog-loops/{id}/delivery-candidate → 404 `unknown backlog loop action` (src/engineering_control/api/main.py:814-839)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          `Engineering Control: Engineering API rejected the request (404): …` [reconstructed]
Side effects:   none
Related:        -
Example:        (do not use)
Evidence:       ~/.hermes/hooks/engineering-control/handler.py:1101-1118; docs/GO-STATUS.md:132-145; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop approve-delivery

```
Purpose:        REMOVED in repo (NEW-01); installed copy calls a missing API action (404).
Type:           deterministic · write  [BROKEN]
Syntax:         /loop approve-delivery <workflow-id> <binding-sha256> APPROVE_STAGING_ISSUE_DELIVERY
Arguments:      none
Backend:        POST /v1/backlog-loops/{id}/approve-delivery → 404
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          404 as above [reconstructed]
Side effects:   none
Related:        -
Example:        (do not use)
Evidence:       ~/.hermes/hooks/engineering-control/handler.py:1119-1135; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

### Group: releases

#### /approve

```
Purpose:        Inspect (1 arg) or approve an immutable deployment candidate of a ChangeSet.
Type:           deterministic · read (1 arg) / write (2+ args)
Syntax:         /approve <change-set-uuid> [artifact-binding-sha256] [typed confirmation…]
Arguments:
  - change-set-uuid · UUID · required: yes · UUID · default: - · error: "ChangeSet ID must be a UUID"
  - binding · 64-hex · required: no · from the candidate · default: - · error: "artifact binding must be a 64-character SHA256"
  - confirmation · text · required: no · phrase shown by the candidate · default: -
Backend:        1 arg: GET /v1/approvals/{id}/candidate (src/engineering_control/api/main.py:1008); else POST /v1/approvals decision=approve (src/engineering_control/api/main.py:996) → `approval` signal to the waiting Preview/Staging/Recovery workflow
Preconditions:  A workflow must be waiting in AWAITING_PREVIEW_APPROVAL / AWAITING_STAGING_APPROVAL (D:1456, D:1522); no current run path waits (the GitOps flow has no approval gate).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419).
Side effects:   Temporal `approval` signal
Related:        /reject
Example:        /approve <change-set-uuid>
Evidence:       integrations/hermes-engineering-control/hook/handler.py:1190-1208; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /reject

```
Purpose:        Reject an immutable deployment candidate.
Type:           deterministic · write
Syntax:         /reject <change-set-uuid> <binding> [confirmation…]
Arguments:
  - change-set-uuid · UUID · required: yes · - · default: -
  - binding · 64-hex · required: yes · - · default: -
Backend:        POST /v1/approvals decision=reject (src/engineering_control/api/main.py:996)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419).
Side effects:   approval record
Related:        /approve
Example:        /reject <change-set-uuid> <binding>
Evidence:       integrations/hermes-engineering-control/hook/handler.py:1190-1208; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /rollback

```
Purpose:        Prepare (default) or start deterministic recovery of a deployment (RecoveryWorkflow).
Type:           deterministic · write
Syntax:         /rollback <deployment-uuid> <binding-sha256> [--execute]
Arguments:
  - deployment-uuid · UUID · required: yes · - · default: - · error: "deployment ID must be a UUID"
  - binding · 64-hex · required: yes · - · default: - · error: "artifact binding must be a 64-character SHA256"
  - --execute · flag · required: no · - · default: dry run
Backend:        POST /v1/deployments/rollback (src/engineering_control/api/main.py:1031 → src/engineering_control/api/service.py:300) → RecoveryWorkflow
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: `OperationAccepted` JSON `{workflow_id, run_id, status: "ACCEPTED"}` (src/engineering_control/api/schemas.py:330-333) wrapped as `{"next_cursor": null, "payload": {...}}`. No later message; poll with `/status <workflow-id>`.
Side effects:   with --execute: Kubernetes recovery after an `approval` signal (D:1663-1676)
Related:        /approve
Example:        /rollback <deployment-uuid> <binding>
Evidence:       integrations/hermes-engineering-control/hook/handler.py:1209-1223; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /bootstrap-plan

```
Purpose:        Plan the first deterministic LLMWiki staging bootstrap.
Type:           deterministic · write (plan workflow)
Syntax:         /bootstrap-plan llmwiki staging
Arguments:
  - project environment · literal · required: yes · llmwiki staging · default: - · error: "usage: /bootstrap-plan llmwiki staging"
Backend:        POST /v1/bootstrap-plan (src/engineering_control/api/main.py:569) → LLMWikiStagingBootstrapWorkflow
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: `OperationAccepted` JSON `{workflow_id, run_id, status: "ACCEPTED"}` (src/engineering_control/api/schemas.py:330-333) wrapped as `{"next_cursor": null, "payload": {...}}`. No later message; poll with `/status <workflow-id>`.
Side effects:   none
Related:        /bootstrap
Example:        /bootstrap-plan llmwiki staging
Evidence:       integrations/hermes-engineering-control/hook/handler.py:720-728; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /bootstrap

```
Purpose:        Execute an approval-bound LLMWiki staging bootstrap.
Type:           deterministic · write
Syntax:         /bootstrap llmwiki staging <binding-sha256> --execute
Arguments:
  - binding · 64-hex · required: yes · from the plan · default: - · error: "usage"
Backend:        POST /v1/bootstrap (src/engineering_control/api/main.py:577) → LLMWikiStagingBootstrapWorkflow execute
Preconditions:  Binding must equal the plan's (ApprovalMismatch, D:3589-3594); worker setting llmwiki_staging_bootstrap_enabled (core.py:536).
Reply:          Immediate: `OperationAccepted` JSON `{workflow_id, run_id, status: "ACCEPTED"}` (src/engineering_control/api/schemas.py:330-333) wrapped as `{"next_cursor": null, "payload": {...}}`. No later message; poll with `/status <workflow-id>`.
Side effects:   Kubernetes (llmwiki staging)
Related:        /bootstrap-plan
Example:        /bootstrap llmwiki staging <sha256> --execute
Evidence:       integrations/hermes-engineering-control/hook/handler.py:729-747; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

### Group: github policy

#### /loop approval-candidate

```
Purpose:        Create the 24-hour GitHub write-policy approval candidate for a backlog campaign.
Type:           deterministic · write (records a candidate)
Syntax:         /loop approval-candidate <project> <backlog-campaign-id> <source-pr|full-delivery> <max-risk> [max-items]
Arguments:
  - project · enum · required: yes · - · default: -
  - campaign · UUID · required: yes · - · default: -
  - mode · enum · required: yes · source-pr | full-delivery · default: -
  - max-risk · enum · required: yes · R0..R3 · default: -
  - max-items · int · required: no · 1..50 · default: 1 · error: "approval max-items must be between 1 and 50"
Backend:        POST /v1/backlogs/{id}/approval-candidate (src/engineering_control/api/main.py:750 → db/repository.py:599-633)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: `{status: AWAITING_APPROVAL, backlog_campaign_id, approval_binding: <sha256>, approval_candidate: {...expires_at...}, typed_confirmation: APPROVE_EXISTING_GITHUB_BACKLOG_LOOP}` (db/repository.py:627-633).
Side effects:   campaign → AWAITING_APPROVAL
Related:        /loop approve
Example:        /loop approval-candidate musematic 66a97948-9f9d-4ae9-b204-e89f27e2712d full-delivery R2 1
Evidence:       integrations/hermes-engineering-control/hook/handler.py:977-1002; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop approve

```
Purpose:        Approve the candidate: requests GitHub write-policy activation for this campaign (one owner at a time, 24 h).
Type:           deterministic · write (policy)
Syntax:         /loop approve <project> <backlog-campaign-id> <binding-sha256> APPROVE_EXISTING_GITHUB_BACKLOG_LOOP
Arguments:
  - project · enum · required: yes · - · default: -
  - campaign · UUID · required: yes · - · default: -
  - binding · 64-hex · required: yes · from approval-candidate · default: - · error: "usage"
  - phrase · literal · required: yes · APPROVE_EXISTING_GITHUB_BACKLOG_LOOP · default: - · error: "usage"
Backend:        POST /v1/backlogs/{id}/approve (src/engineering_control/api/main.py:762 → db/repository.py:642-770); root reconciler `engineering-github-policy-reconciler` activates the policy
Preconditions:  409 errors (db/repository.py): `existing-backlog approval binding is not current`, `another backlog campaign policy activation is pending`, `multiple backlog campaigns claim GitHub write policy ownership`, `existing-backlog approval candidate changed`, `existing-backlog approval candidate expired`.
Reply:          Immediate: campaign status JSON (APPROVED_POLICY_PENDING → APPROVED after the reconciler). Related alert (NEW-84, activities/engineering.py:1919-1967): `Hermes is waiting for a GitHub write approval (<REASON>) for <repo>#<n> at <step>. Approve the campaign; the flow re-checks every 5 minutes and resumes by itself.` with **CLI** commands (`engineering-ctl backlog-approval-candidate … full-delivery R2 --max-items 1`, `engineering-ctl backlog-approve …`).
Side effects:   GitHub write policy file (via root reconciler); expiry by engineering-github-policy-expiry.timer
Related:        /loop approval-candidate; release early only via CLI `engineering-ctl backlog-release-ownership`
Example:        /loop approve musematic 66a97948-9f9d-4ae9-b204-e89f27e2712d <binding> APPROVE_EXISTING_GITHUB_BACKLOG_LOOP
Evidence:       integrations/hermes-engineering-control/hook/handler.py:1003-1024; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop r3-candidate

```
Purpose:        Prepare an R3 (operator-approved) issue binding.
Type:           deterministic · write (records a candidate)
Syntax:         /loop r3-candidate <project> <source|deployment> <issue-number>   (installed ~/.hermes copy: only `musematic source 1119`)
Arguments:
  - project surface issue · enum/int · required: yes · - · default: - · error: "usage"
Backend:        POST /v1/r3-issues/candidate (src/engineering_control/api/main.py:518)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419).
Side effects:   none
Related:        /loop approve-r3
Example:        /loop r3-candidate falcone source 1016
Evidence:       integrations/hermes-engineering-control/hook/handler.py:872-891; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /loop approve-r3

```
Purpose:        Approve an R3 issue with its binding and generated confirmation phrase.
Type:           deterministic · write (policy)
Syntax:         /loop approve-r3 <project> <source|deployment> <issue-number> <binding-sha256> APPROVE_R3_<PROJECT>_<N>_<16-HEX>
Arguments:
  - binding · 64-hex · required: yes · - · default: - · error: "R3 approval binding or confirmation phrase is malformed"
  - phrase · pattern · required: yes · APPROVE_R3_[A-Z]+_[1-9][0-9]*_[A-F0-9]{16} · default: -
Backend:        POST /v1/r3-issues/approve (src/engineering_control/api/main.py:531)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419).
Side effects:   R3 approval record
Related:        /loop issue-start
Example:        /loop approve-r3 falcone source 1016 <binding> APPROVE_R3_FALCONE_1016_<hex>
Evidence:       integrations/hermes-engineering-control/hook/handler.py:892-905; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

### Group: enhancements

#### /enhancements discover

```
Purpose:        Run an enhancement scan for one product (EnhancementDiscoveryWorkflow); dry run unless --execute.
Type:           deterministic · write
Syntax:         /enhancements discover <project> <campaign-id> [--execute]
Arguments:
  - project · enum · required: yes · - · default: -
  - campaign-id · UUID · required: yes · audit campaign whose evidence is scanned · default: -
  - --execute · flag · required: no · - · default: dry_run · error: "enhancement discovery flag is not allowlisted"
Backend:        POST /v1/enhancements/discover (src/engineering_control/api/main.py:862 → src/engineering_control/api/service.py:499)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: `OperationAccepted` JSON `{workflow_id, run_id, status: "ACCEPTED"}` (src/engineering_control/api/schemas.py:330-333) wrapped as `{"next_cursor": null, "payload": {...}}`. No later message; poll with `/status <workflow-id>`.
Side effects:   codex discovery + claude product review; GitHub dedupe read
Related:        /enhancements list
Example:        /enhancements discover musematic <audit-campaign-uuid> --execute
Evidence:       integrations/hermes-engineering-control/hook/handler.py:1113-1129; last observed use: never run in the retained 30-day history
```

#### /enhancements list

```
Purpose:        List enhancements of a product, optionally by status.
Type:           deterministic · read
Syntax:         /enhancements list <project> [status]
Arguments:
  - project · enum · required: yes · - · default: -
  - status · string · required: no · e.g. CANDIDATE_ENHANCEMENT · default: -
Backend:        GET /v1/enhancements (src/engineering_control/api/main.py:869)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419). Real sample (musematic, 2026-09-27): `{"items": [], "next_cursor": null}`.
Side effects:   none
Related:        -
Example:        /enhancements list musematic
Evidence:       integrations/hermes-engineering-control/hook/handler.py:1130-1136; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /enhancements show

```
Purpose:        Show one enhancement (incl. approval binding).
Type:           deterministic · read
Syntax:         /enhancements show <enh-id>
Arguments:
  - enh-id · pattern · required: yes · enh-[a-f0-9]{16,64} · default: -
Backend:        GET /v1/enhancements/{id} (src/engineering_control/api/main.py:882)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419).
Side effects:   none
Related:        -
Example:        /enhancements show enh-<hex>
Evidence:       integrations/hermes-engineering-control/hook/handler.py:1137-1140; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /enhancements approve

```
Purpose:        Approve a candidate enhancement with its binding.
Type:           deterministic · write
Syntax:         /enhancements approve <enh-id> <binding-sha256>
Arguments:
  - enh-id · pattern · required: yes · - · default: -
  - binding · 64-hex · required: yes · - · default: -
Backend:        POST /v1/enhancements/{id}/approve (src/engineering_control/api/main.py:893)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419).
Side effects:   enhancement APPROVED
Related:        /enhancements run
Example:        /enhancements approve enh-<hex> <binding>
Evidence:       integrations/hermes-engineering-control/hook/handler.py:1141-1153; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /enhancements reject

```
Purpose:        Reject a candidate enhancement with a free-text reason.
Type:           deterministic · write
Syntax:         /enhancements reject <enh-id> <reason…>
Arguments:
  - enh-id · pattern · required: yes · - · default: -
  - reason · text · required: yes · - · default: -
Backend:        POST /v1/enhancements/{id}/reject (src/engineering_control/api/main.py:909)
Preconditions:  Telegram platform, sender in the allowlist file (H:122-130).
Reply:          Immediate: the API JSON wrapped as `{"next_cursor": <32-hex or null>, "payload": ...}`; pages of ≤3500 characters, next page with the same command plus `--cursor <token>` (10-minute, same-user token) (hook `_format`/`_split_cursor`, H:392-419).
Side effects:   enhancement REJECTED
Related:        -
Example:        /enhancements reject enh-<hex> out of scope for W1
Evidence:       integrations/hermes-engineering-control/hook/handler.py:1154-1162; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

#### /enhancements run

```
Purpose:        Build an approved enhancement via EnhancementSolutionLoop → IssueSolutionLoop; dry run unless --execute.
Type:           deterministic · write
Syntax:         /enhancements run <enh-id> <R0|R1|R2|R3> <plan-only|source-pr|preview|staging> [--execute]
Arguments:
  - enh-id · pattern · required: yes · - · default: -
  - risk · enum · required: yes · R0..R3 · default: -
  - mode · enum · required: yes · plan-only | source-pr | preview | staging · default: -
  - --execute · flag · required: no · - · default: dry_run
Backend:        GET /v1/enhancements/{id} (binding) + POST /v1/enhancements/{id}/run (src/engineering_control/api/main.py:925 → src/engineering_control/api/service.py:513)
Preconditions:  `approved enhancement binding is unavailable` when not approved (H:1174-1177).
Reply:          Immediate: `OperationAccepted` JSON `{workflow_id, run_id, status: "ACCEPTED"}` (src/engineering_control/api/schemas.py:330-333) wrapped as `{"next_cursor": null, "payload": {...}}`. No later message; poll with `/status <workflow-id>`.
Side effects:   as IssueDelivery (PR) for source-pr and beyond
Related:        -
Example:        /enhancements run enh-<hex> R1 source-pr --execute
Evidence:       integrations/hermes-engineering-control/hook/handler.py:1163-1189; last observed use: UNVERIFIED (gateway logs/session store unreadable)
```

### Hermes built-ins on Telegram

All Hermes built-in slash commands (`/help`, `/whoami`, `/new`, `/model`, `/reset`, memory/session commands, …) reach the `command:*` hook and are **denied** with `Unknown engineering command denied.`; non-admin policy would additionally block all but `/help` and `/whoami`. None are usable by Telegram operators. (Live gateway config UNVERIFIED.)

## Part 4 — Quick reference

| Command | Type | What it does | Typical next |
|---|---|---|---|
| `/ready` | read | Show the core or complete readiness projection. | /connections all, /workers all |
| `/connections` | read | Show the bounded connection-health projections. | /ready core |
| `/temporal` | read | Show the safe Temporal summary. | - |
| `/providers` | read | Show native CLI provider readiness (codex, claude). | /usage |
| `/workers` | read | Show task-worker heartbeat projections. | - |
| `/channel-check` | read | Show Telegram ↔ Hermes gateway ↔ API correlation. | - |
| `/usage` | read | Show provider auth/capacity snapshot and 7-day token history per role. | /providers |
| `/status` | read | Show one workflow run's projection (status, type, validation summary). | /loop status, /loop tail, /loop diagnose |
| `/pause` | write | Pause a workflow at its next checkpoint (Temporal `pause` signal). | /resume, /cancel |
| `/resume` | write | Resume a paused workflow (`resume` signal). | /pause |
| `/cancel` | write | Cancel a bounded workflow (`cancel` signal). | /status |
| `/evidence` | read | Show redacted metadata of one evidence artifact. | - |
| `/export-campaign` | write | Create a bounded redacted JSON export of one campaign. | /campaign |
| `/export-workflow` | write | Create a bounded redacted JSON export of one workflow. | - |
| `/audit-plan` | write | Validate and show the audit plan for one product/preset/environment (AuditCampaignWorkflow, plan_only). | /audit … --execute |
| `/audit` | write | Plan (default) or start a bounded audit campaign for one product; advanced 8/9-argument form pins SHAs and one cell. | /audit-plan first; then /findings, /verify |
| `/audit-all` | write | Plan or start fixed sequential staging audits for falcone, musematic and llmwiki. | /campaigns, /campaign |
| `/campaigns` | read | List audit campaigns. | /campaign |
| `/campaign` | read | Show one audit, imported-backlog or global campaign. | /export-campaign |
| `/findings` | read | List verified findings. | /verify, /run |
| `/verify` | write | Start independent verification of one candidate finding. | /findings, /run |
| `/run` | write | Start source-PR delivery for one confirmed finding (IssueDeliveryWorkflow); dry run unless --execute. | /verify first; /status |
| `/approve` | write | Inspect (1 arg) or approve an immutable deployment candidate of a ChangeSet. | /reject |
| `/reject` | write | Reject an immutable deployment candidate. | /approve |
| `/rollback` | write | Prepare (default) or start deterministic recovery of a deployment (RecoveryWorkflow). | /approve |
| `/validate` | write | Start the synthetic control-plane lifecycle canary (AcceptanceLifecycleWorkflow). | /status <workflow-id> |
| `/bootstrap-plan` | write | Plan the first deterministic LLMWiki staging bootstrap. | /bootstrap |
| `/bootstrap` | write | Execute an approval-bound LLMWiki staging bootstrap. | /bootstrap-plan |
| `/loop import` | write | Import open GitHub issues of one product as a backlog campaign (GitHubBacklogImportWorkflow). | /loop inventory, /loop issue-locate |
| `/loop inventory` | read | List a backlog campaign's imported issues with risk/eligibility. | /loop issue |
| `/loop queue` | read | Same listing as /loop inventory without the project argument. | - |
| `/loop issue` | read | Show one imported issue record (risk, eligibility, reproduction, PRs). | - |
| `/loop issue-locate` | read | Find the current backlog record for a product issue by number. | /loop issue-start |
| `/loop issue-start` | write | Resolve one imported issue end to end (ExistingGitHubBacklogLoopWorkflow, max risk R2, correction limit 5). | /loop issue-locate → /loop issue-start → /loop status → /loop tail |
| `/loop approval-candidate` | write | Create the 24-hour GitHub write-policy approval candidate for a backlog campaign. | /loop approve |
| `/loop approve` | write | Approve the candidate: requests GitHub write-policy activation for this campaign (one owner at a time, 24 h). | /loop approval-candidate; release early only via CLI `engineering-ctl backlog-release-ownership` |
| `/loop start` | write | Work a backlog campaign (up to max-items issues) with ExistingGitHubBacklogLoopWorkflow. | /loop status, /loop pause|resume|cancel |
| `/loop plan` | write | Legacy: plan an IssueSolutionLoop for a campaign (returns the backlog record when the id is an imported backlog). | /loop start |
| `/loop status` | read | Show a loop workflow's projection. | /loop tail |
| `/loop tail` | read | Show the last N dashboard events of a workflow (phases, provider start/finish). | - |
| `/loop diagnose` | read | Alias of /status for a loop workflow. | - |
| `/loop nudge` | write | Nudge a workflow (typed control `nudge`). | - |
| `/loop pause` | write | Pause a loop at its next checkpoint. | /loop resume |
| `/loop resume` | write | Resume a paused loop. | - |
| `/loop cancel` | write | Cancel a loop (releases the campaign's leases). | - |
| `/loop skip` | write | Skip the issue a paused loop is waiting on, with an allowlisted reason. | /loop retry |
| `/loop retry` | write | Retry the issue a paused loop is waiting on (`retry` signal with `<repo>#<n>`). | /loop tail |
| `/loop retry-issue` | write | Retry by product issue number (locates the repository first). | - |
| `/loop global-start` | write | Start the global autonomous selector over the three product backlogs (staging, R2, one issue at a time). | - |
| `/loop r3-candidate` | write | Prepare an R3 (operator-approved) issue binding. | /loop approve-r3 |
| `/loop approve-r3` | write | Approve an R3 issue with its binding and generated confirmation phrase. | /loop issue-start |
| `/loop delivery-candidate` | read (BROKEN) | REMOVED in repo (NEW-01); still in the installed ~/.hermes copy, where it calls an API action that returns 404. | - |
| `/loop approve-delivery` | write (BROKEN) | REMOVED in repo (NEW-01); installed copy calls a missing API action (404). | - |
| `/enhancements discover` | write | Run an enhancement scan for one product (EnhancementDiscoveryWorkflow); dry run unless --execute. | /enhancements list |
| `/enhancements list` | read | List enhancements of a product, optionally by status. | - |
| `/enhancements show` | read | Show one enhancement (incl. approval binding). | - |
| `/enhancements approve` | write | Approve a candidate enhancement with its binding. | /enhancements run |
| `/enhancements reject` | write | Reject a candidate enhancement with a free-text reason. | - |
| `/enhancements run` | write | Build an approved enhancement via EnhancementSolutionLoop → IssueSolutionLoop; dry run unless --execute. | - |

### Standard sequences

**Resolve one issue** — `/loop issue-locate <p> <source|deployment> <n>` → (if 404) `/loop import <p> github-open all` and wait for the import → `/loop issue-start <p> <surface> <n> full-delivery` → note `workflow_id` from the reply / the operation-started message → poll `/loop status <id>` and `/loop tail <id> 30` → if PAUSED: read the blocker, fix, `/loop retry <id> <repo> <n>` or `/loop skip <id> <repo> <n> <REASON>` → done when the loop is COMPLETED and the GitHub issue is closed by Hermes.

**Audit a product** — `/audit-plan <p> <preset> staging` → `/audit <p> <preset> staging --execute [--publish-findings]` → `/campaigns 5` → `/campaign <id>` → `/findings 20` → `/verify <finding>` → `/run <finding> <risk> --execute`.

**Unblock GitHub writes** — on the write-policy alert: `/loop approval-candidate <p> <campaign> full-delivery R2 1` → copy `approval_binding` → `/loop approve <p> <campaign> <binding> APPROVE_EXISTING_GITHUB_BACKLOG_LOOP` → the waiting flow re-checks every 5 minutes and resumes. (Approval lasts 24 h; one owner campaign at a time; early release only via CLI.)

**Recover a failed run** — `/status <id>` → `/loop tail <id> 50` → for a paused loop `/loop retry …`; for a failed child a new `/loop issue-start` (a fresh child id is used); for a hung workflow `/cancel <id>` then restart.

### Common errors

| Reply text | Cause | Fix |
|---|---|---|
| `Unknown engineering command denied.` | command not in the 30 engineering names (incl. /help) | use a listed command |
| `Engineering Control access denied.` | Telegram user id not in the allowlist file | operator adds the numeric id to /etc/engineering-control/env/telegram-allowlist |
| `Engineering Control: usage: /… ` | argument grammar not met | follow the syntax exactly (positional, case-sensitive) |
| `Engineering Control: … must be a UUID` | malformed id | copy the id from a previous reply |
| `… rejected the request (404): {"detail":"issue is not in a current backlog inventory"}` | issue not imported (real: llmwiki #8) | /loop import <p> github-open all |
| `status BLOCKED, safe_error `operation preflight changed: AUDIT_PLAN_REQUIRED`` | audit start without a completed plan | /audit-plan … first |
| `… `ISSUE_RISK_EXCEEDS_BOUND` / `R3_OPERATOR_ACTION_REQUIRED`` | issue-start is capped at R2 and the issue is R3 / `/loop start` with max-risk R3 | /loop r3-candidate + /loop approve-r3 (repo hook; installed copy only for musematic 1119) |
| `… `BACKLOG_CAMPAIGN_ALREADY_ACTIVE` / `ISSUE_WORKFLOW_ALREADY_ACTIVE`` | another loop owns the campaign/issue | /loop status the running loop; /loop cancel it or wait |
| `… `another backlog campaign policy activation is pending`` | second campaign asked for the write policy | wait for the pending one or let it expire (24 h) |
| `… `GitHub write policy activation did not complete; check engineering-github-policy-reconciler before starting`` | root reconciler did not promote within 60 s | operator checks the reconciler unit, then retry |
| `… `CODEX_RATE_LIMITED` / `CLAUDE_AUTH_NOT_READY` / `…_HEALTH_STALE`` | provider preflight | /providers, /usage; wait for quota reset or re-authenticate the runner |
| `… `WORKFLOW_IS_NOT_RETRYABLE`` | retry sent to a loop that is not paused | /loop status first |
| `Engineering Control: pagination cursor is invalid or expired` | cursor older than 10 min or other user | rerun the command |

### Permissions

- Only Telegram (platform check) and only numeric user ids listed in the allowlist file may run any command (H:122-130); ids are not listed here. Group chats use the same 30-command list (`group_user_allowed_commands`) and the same user-id allowlist; there is no per-command role — every allowlisted user can run every command, including write commands. Approvals record the actor `telegram:<user-id>`.
- API side: the bearer token authenticates the gateway as one API client; policy-changing endpoints additionally require the typed confirmation phrases shown above.
