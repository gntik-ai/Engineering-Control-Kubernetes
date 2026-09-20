# Runbook

Verified live 2026-09-17/18 during the GO-status remediation (branch `go-status/2026-09-17`).
Every command below was actually run during that engagement; this is not aspirational
documentation. Cross-reference `docs/ARCHITECTURE-VERIFIED.md` and `.go-status/findings.json`
for the evidence behind each claim.

## Hosts

- **Controller**: `hermes-control` (10.10.0.2). Runs Hermes gateway, Temporal server (Docker),
  Postgres (Docker), the private API, the dashboard, Caddy, and the `github`-role worker.
- **Worker**: `hermes-worker` (10.10.0.3), reached via `ssh worker`. Runs the other 5 role
  workers (`codex`, `claude`, `browser`, `scan`, `delivery`, `release`), each as its own
  dedicated, `nologin` Linux user.
- Neither host gives `hermes` passwordless sudo. Interactive sudo is required for anything
  touching `/etc/engineering-control/`, `/etc/systemd/system/`, or another user's home.

## Start / stop

| Component | Unit | Start | Stop | Notes |
|---|---|---|---|---|
| Temporal + Postgres | `engineering-infrastructure.service` (oneshot, wraps `docker compose`) | `sudo systemctl start engineering-infrastructure.service` | `sudo systemctl stop ...` | Restarting this bounces both containers -- has a real blast radius on in-flight workflows. Treat as a deliberate action, not routine. |
| Private API | `engineering-api.service` | `sudo systemctl restart engineering-api.service` | | Verified restart-safe twice this session (NRestarts stayed 0 both times). |
| Dashboard | `engineering-dashboard.service` | `sudo systemctl restart engineering-dashboard.service` | | |
| Temporal worker (controller-hosted queues) | `engineering-temporal-worker.service` | | | |
| GitHub role worker | `engineering-github-worker.service` (controller, runs as `hermes`) | | | Sandboxed: cannot read `.codex`/`.claude`/`.ssh`/`.kube`/credential dirs even though it's the same Linux user. |
| Role workers (worker host) | `engineering-worker@{codex,claude,browser,scan,delivery}.service`, `engineering-release-worker.service` | `sudo systemctl restart engineering-worker@claude.service` (etc.) | | Run via `ssh worker`. |
| Hermes interactive agent | n/a, ad hoc `hermes` CLI invocations under `~/.hermes` | | | |
| Hermes production gateway | `hermes-gateway.service` (isolated `hermes-gateway` uid) | `sudo systemctl restart hermes-gateway.service` | | Verified restart-safe twice this session. Known cosmetic issue: exits 1 (not 0) on SIGTERM even on a clean stop (F-23) -- don't alarm on that alone, check `NRestarts`/`ActiveState` after. |
| GitHub write-policy reconciler | `engineering-github-policy-reconciler.path` + `.service` | `sudo systemctl start engineering-github-policy-reconciler.path` | | `PathExists=`-triggered: starting it fires immediately if an approval is already queued. See "Common failures" below -- this was found dead for 10+ days this session. |

## Launching each mission type

All commands use `/opt/engineering-control/venv/bin/engineering-ctl` (not on `$PATH` by
default -- use the full path, or `export CTL=/opt/engineering-control/venv/bin/engineering-ctl`).

### Product audit (Mission 2 / G8)

```
engineering-ctl audit-plan  <falcone|musematic|llmwiki> <preset> staging
engineering-ctl audit-start <falcone|musematic|llmwiki> <preset> staging
engineering-ctl run <workflow_id>     # poll until COMPLETED/FAILED
```

Presets: `snapshot`, `static-full`, `e2e-smoke`, `full-e2e`, `security`, `ux`, `deployment`,
`documentation`, `falcone-intensive`. `snapshot` is cheapest and doesn't require the K8s
observer credential to *complete* (it will report `OBSERVER_AUTHORIZATION_INVALID` as a
diagnosed blocker rather than crash, if that credential isn't configured -- see F-01).
`static-full` on a lean project (few personas/lenses, e.g. llmwiki) can hit a real bug
(NEW-14, zero-coverage-cells crash) -- not GO-blocking, just don't rely on it for llmwiki yet.

To see the actual result payload (the CLI's own `status` output can lag): query Temporal
directly --

```python
from temporalio.client import Client
client = await Client.connect("127.0.0.1:7233", namespace="engineering-control")
handle = client.get_workflow_handle(workflow_id, run_id=run_id)
result = await handle.result()
```

### Issue-resolution loop on an *already-imported and approved* backlog issue (Mission 1 / G7)

This is the path that actually works end to end (traced live this session):

```
engineering-ctl backlog-import <project> <bugs|bugs-and-docs|security|enhancements|all> --max-items N
engineering-ctl backlog-plan <project> <campaign_id>
engineering-ctl backlog-approval-candidate <project> <campaign_id> source-pr <R0|R1|R2|R3> --max-items N
engineering-ctl backlog-approve <project> <campaign_id> <approval_binding> APPROVE_EXISTING_GITHUB_BACKLOG_LOOP
# ^ requires a human to type this exact phrase; the harness's own permission classifier
#   will (correctly) refuse to run this on an operator's behalf
engineering-ctl issue-start <project> source <issue_number> --delivery-mode source-pr
```

`issue-start` auto-detects a matching imported campaign and routes through
`ExistingGitHubBacklogLoopWorkflow` (NOT `IssueSolutionLoopWorkflow` / `POST /v1/loops` --
that workflow is for audit-derived findings, not backlog-imported GitHub issues; it will
fail with `CAMPAIGN_NOT_FOUND` if you try it against a backlog campaign id).

**R3 issues cannot be delivered automatically, ever, by design** (NEW-10): even after
`r3-candidate`/`r3-approve`, `SolveIssueCommand`/`SolveBacklogCommand`'s preflight
unconditionally blocks `maximum_risk == "R3"` with no override anywhere in the code, and the
fully-autonomous path is hardcoded to cap at R2. If every open issue in your target project
is R3 (this was true for falcone and musematic when checked 2026-09-17/18), you cannot prove
this end to end there -- pick a project with an R0-R2 issue, or file one.

**Only one campaign can hold GitHub-write-policy ownership at a time** (NEW-13). A second
campaign approved while another is still active gets `status: APPROVED_INVENTORY` and its
delivery will legitimately BLOCK with `CURRENT_BACKLOG_APPROVAL_REQUIRED`, even though its own
approval succeeded. There is no operator action to release ownership early -- only the
owner's approval `expires_at` (~24h from approval) lapsing. If the current owner's only
content is undeliverable (e.g. all-R3), it will squat on ownership for the full 24h. Sequence
approvals accordingly: approve your intended *deliverable* campaign first.

### Enhancement discovery (Mission 3)

Not exercised this session. Traced in `docs/ARCHITECTURE-VERIFIED.md` section 3 --
`POST /v1/enhancements/discover` -> `EnhancementDiscoveryWorkflow` -> operator approval ->
`EnhancementDeliveryWorkflow`/`EnhancementSolutionLoopWorkflow` (which reuses the Mission-1
pipeline once approved, so the same R3/ownership constraints above apply).

## Adding a runner

Not exercised this session (no new runner was added). Based on the existing pattern
(`worker_registry.py` + `/var/lib/engineering-control/identities/<name>` + a dedicated
`nologin` Linux user + an `engineering-worker@<role>.service` instance): would need a new
role entry in `worker_registry.py`, a new systemd unit or template instance, a new identity
home, and a fresh `claude`/`codex` OAuth login for that identity (see below). Treat as a real
code + ops change, not a config-only one.

## Rotating an engine login

```
sudo -u claude-runner -H claude auth status     # check current state (read-only)
sudo -u claude-runner -H claude login           # interactive -- needs a human at a terminal
sudo -u codex-runner  -H codex login status
sudo -u codex-runner  -H codex login            # interactive
```

These are interactive OAuth device-code flows; they cannot be scripted or run by an agent
non-interactively. This is intentional (docs/CLAUDE.md: "on auth expiry, bootstrap creates a
MANUAL_AUTH_REQUIRED_CLAUDE marker file and other platform work continues" -- other runners
keep working while one identity waits for a human).

## Recovering from common failures found this session

- **Private API returns 503 "restricted to synthetic acceptance during maintenance"**: the
  admission mode is stuck non-`normal`. Check `systemctl cat engineering-api.service` for the
  winning `EnvironmentFile=` drop-in (last one wins, sorted by filename -- the platform's own
  convention is escalating `z` prefixes) and confirm `ENGINEERING_API_ADMISSION_MODE=normal`
  in whichever file it points to. Fix: add one more drop-in with a longer `z`-prefix name
  pointing at a file with `ENGINEERING_API_ADMISSION_MODE=normal`; don't delete the history.
- **`backlog-approve` succeeds but the write policy never actually enables**: check
  `systemctl is-active engineering-github-policy-reconciler.path`. If inactive, check for
  `ConditionPathExists=!<marker>` fences under
  `/etc/systemd/system/engineering-github-policy-reconciler.path.d/*.conf` -- there can be
  *many* stacked (13, found this session), ANDed together; every referenced marker file must
  be absent for the unit to start. `systemctl start` after clearing them fires immediately
  (it uses `PathExists=`, not an edge-triggered watch) against whatever activation is already
  queued.
- **Reconciler runs but crash-loops on `ValueError: canonical GitHub policy metadata is
  unsafe`**: `/etc/engineering-control/policy/github-issue-write-validation.yaml` must be
  *exactly* mode `640`, owner `root`, group `engineering-control` (gid varies by host --
  check with `getent group engineering-control`), parent dir exactly `750`. Any deviation
  (including a stricter `600`) fails the check on purpose.
- **`backlog-start`/`issue-start` returns `BLOCKED: existing backlog campaign requires its
  exact Telegram approval`** even though you approved it: if the campaign's status is
  `APPROVED_INVENTORY` (not `APPROVED_POLICY_PENDING`/`APPROVED`), a different campaign
  currently owns write-policy authority -- see NEW-13 above.
- **`engineering-ctl <mutating-command>` gets an unhelpful generic error** but the same call
  succeeds seconds later with no change on your end: retry once. Several transient failures
  this session were the platform's own internal retry/consistency windows, not real errors.
- **Backlog-approve/r3-approve/policy-activation windows are ~24h TTLs, not durable.** If two
  related approvals (e.g. a campaign approval and a per-issue R3 approval, or two campaigns'
  approvals generated around the same time) aren't both consumed within that window, they can
  expire before you get back to them -- check `backlog-plan <project> <campaign>`'s `status`
  field (`EXPIRED` means regenerate the candidate and re-approve) before assuming a stale error
  is something else.

### Incident, 2026-09-20: an SSH hardening fix locked the operator out

An agent session disabled `PasswordAuthentication` on the controller (closing an audit finding)
and verified the fix by checking that *its own* SSH session still worked. That verification was
worthless: the agent session runs as a local process on the controller itself (inside a
long-lived tmux session), never over an actual external SSH connection, so it could not detect
that the operator's *own* access depended entirely on password auth (`hermes` had no
`authorized_keys` file at all). The operator was locked out until the change was reverted.

**Lesson for any future security-hardening change on a host you have session access to via a
means other than the exact path being hardened**: verify the *specific credential/path the
human actually uses* remains valid, not just "my own session still has access" -- those can be,
and here were, two completely different things. Before disabling any auth method, confirm a
working alternative is already in place and tested from the human's actual client, not inferred
from the agent's own unrelated access.
