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

## Onboarding a project: canonical mirrors on BOTH hosts (NEW-162)

A project's repositories need a canonical bare mirror on the controller **and** on the worker.
A missing or unreadable mirror on either host fails every revalidation with "canonical mirror
does not exist" (the GitHub worker's `github.refresh_mirrors` fetches the controller mirror and
pushes it to the worker copy, which scan-runner then reads).

**Controller** (`hermes-control`): `/srv/engineering/mirrors/<slug>.git`, owner `hermes`, group
`engineering-control`, group `rwx` with setgid directories (2770 style), other none. Create it
as `hermes`, then hand the group over:

```bash
git clone --mirror https://github.com/gntik-ai/<slug>.git /srv/engineering/mirrors/<slug>.git
sudo chgrp -R engineering-control /srv/engineering/mirrors/<slug>.git
sudo chmod -R g+rwX,o-rwx /srv/engineering/mirrors/<slug>.git
sudo find /srv/engineering/mirrors/<slug>.git -type d -exec chmod g+s {} +
```

**Worker** (`hermes-worker`): `/srv/engineering/mirrors/<slug>.git`, owner
`scan-runner:scan-runner`, ACL `user:scan-runner:rwx`, `user:hermes:rwx`,
`user:delivery-runner:r-x`, `user:claude-runner:r-x`, `user:codex-runner:r-x`, `group::---`,
`other::---`; every directory carries the same default ACL plus `release-runner` `r-x`. The
root `/srv/engineering/mirrors` stays `scan-runner:scan-runner 0700` with a named-user `--x`
ACL for `hermes`, `release-runner`, `delivery-runner`, `claude-runner` and `codex-runner`;
`bootstrap-worker.sh` creates it with those ACLs (re-applied on every run), and
`bootstrap-mirrors.sh` refuses when `hermes` cannot traverse it, so run the worker bootstrap
first. Add
the slug to `repositories=(...)` in `scripts/bootstrap-mirrors.sh` (and the worktree list in
`scripts/bootstrap-worker.sh`; `tests/test_project_registry.py` pins both to the registry),
deploy, then run `sudo /opt/engineering-control/source/scripts/bootstrap-mirrors.sh` on the
worker: it creates only missing mirrors with exactly that layout (cloned and fsck'd as `hermes`
in a staging directory, then handed to `scan-runner`) and verifies existing ones with
`git -c safe.directory=<path>`. It never re-owns or re-modes an existing mirror or the mirror
root (an earlier version re-owned the root to `hermes` and cut `scan-runner` off).

## Worker Build ID + Deployments versioning (F-11 (a))

`Settings.build_id` (env `ENGINEERING_BUILD_ID`) is wired into both `Worker(...)` constructions
via `worker_deployment_kwargs()` in `workflows/worker_registry.py`. Unset (`None`, the default),
it's a total no-op -- `worker_deployment_kwargs(None) == {}` -- so this ships with zero behavior
change until an operator opts in.

When `ENGINEERING_BUILD_ID` is set, it enables Temporal's **Worker Deployments** feature
(`WorkerDeploymentConfig`, `use_worker_versioning=True`) with `default_versioning_behavior =
AUTO_UPGRADE` for all 24 workflows (no per-workflow override needed) -- matching this codebase's
existing `workflow.patched()`-based replay-safety model, where in-flight executions are expected
to pick up new worker code rather than stay pinned to the build they started on. All worker
processes share one deployment identity, `WORKER_DEPLOYMENT_NAME = "engineering-control"`
(`worker_registry.py`), since they're always built and deployed together from the same release.

**Temporal itself flags this as an experimental feature** (`temporal worker deployment --help`
prints a caution banner) -- expect flags/behavior to potentially change on a future
temporalio/temporal-cli upgrade; re-verify against a real server before relying on it after one.

**Critical: setting the env var and restarting is not enough on its own.** Temporal does not
route new workflow executions to a newly-versioned worker until its version is explicitly
promoted -- this is deliberate (safe rollout), not a bug, but it means skipping the activation
step below leaves the service up, healthy, and polling while silently starting zero new workflow
executions.

To enable and activate on a deploy:

1. Set `ENGINEERING_BUILD_ID=<git commit hash of the deployed release>` (matching this repo's
   existing convention of naming release directories by commit hash) in the `EnvironmentFile=`
   stack for `engineering-temporal-worker.service` (controller) and each
   `engineering-worker@{codex,claude,browser,scan,delivery}.service` /
   `engineering-release-worker.service` (worker host).
2. Restart those units. Give them a few seconds to connect and start polling -- Temporal needs to
   have seen at least one poll from the new build before it can be set current.
3. Run `scripts/activate-worker-deployment.sh <git-commit-hash>` (from the controller, where
   Temporal is reachable at `127.0.0.1:7233`) to promote that build to current. It prints the
   deployment's state before and after, and fails loudly (non-zero exit) if promotion didn't
   succeed -- don't consider the deploy done until it prints "Done."

None of this is done automatically by this change -- it touches live systemd config on two hosts
and a real Temporal server call, so it's a deliberate operator/deploy-script step each time.

Verified end-to-end against a real (local, offline) Temporal dev server before being adopted
here: a versioned worker started, received zero traffic until promoted, then correctly received
and completed a workflow immediately after `scripts/activate-worker-deployment.sh` ran.

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

### Undeletable maker files in a ChangeSet worktree (NEW-168, design note)

**Symptom.** The scan worker's `git clean -ffdX` (delivery toolchain cleanup,
`activities/scan.py` `_clean_ignored_toolchain_artifacts`), a worktree reset or the nightly
cleanup fails with `Permission denied` on directories the maker created: `openspec/changes/...`
(2026-10-01), `node_modules/.vitest` (2026-10-09).

**Cause.** A ChangeSet worktree is owned by `scan-runner`; allocation
(`GitWorktreeManager._grant_access`) grants the maker `rwX` and `scan-runner` `rwX` through access
and default ACLs. Inheritance covers only entries created inside with a permissive mode: a tool
that creates a directory with an explicit mode (`mkdir 0755`, archive extraction, `chmod`)
narrows the inherited ACL mask to `r-x`, and a directory renamed in from outside (a temp or cache
directory) carries no inherited ACL at all (worker `UMask=0027`, other `---`). `scan-runner` can
then no longer remove entries inside it, and only the owner or root can widen that ACL.

**Why there is no in-repo fix.** A umask cannot help (an inherited ACL ignores it; the mask comes
from the creating call's mode). Adding `scan-runner` to the runner groups does not help either
(its named-user ACL entry wins and is masked) and would expose the runners' group-readable homes.
A maker-side repair (the maker re-granting `scan-runner` on paths it owns after each run) was
rejected in the NEW-105 review: it walks a maker-controlled tree where symlink/rename races can
redirect the grant to the maker's own credentials. The correct fix needs root at runtime, and no
privileged helper is built without an operator decision.

**Operator recovery** (worker host, as root). First make sure nothing writes in that worktree any
more: the ChangeSet's loop is `PAUSED` and none of its delivery children runs (`backlog-status`;
`change-reset` checks the same in Temporal), and no `codex-runner`/`claude-runner` process is left
working there (`sudo pgrep -a -u codex-runner,claude-runner`). With the writers quiescent, nothing
can swap a path component during the walk; `find` does not follow symlinks and `chown -h` does not
dereference them. The whole remote command is quoted so the escaped parentheses reach `find`:

```bash
WT=/srv/engineering/worktrees/<repository-slug>/<change_set_id>
ssh -t 10.10.0.3 "sudo find '$WT' -xdev \( -user codex-runner -o -user claude-runner \) -exec chown -h scan-runner:scan-runner {} +"
```

Then retry the stopped step (`backlog-retry`, or let the next cleanup run). The next allocation of
that ChangeSet archives and cleans the worktree as `scan-runner`.

**Proposed fix: open design work, not implemented.** Any automatic root reclaim must not repeat
the race that sank the maker-side repair: a path-based walk (`find`, `chown -h`) protects only the
last component, so a writer still active in the tree can swap an intermediate directory for a
symlink between the check and the change. A design is acceptable only with both of:

1. verified writer quiescence, checked first and fail-closed: the ChangeSet's delivery children
   closed in Temporal, its worktree and component leases released, and no runner process inside
   the worktree; and
2. descriptor-relative traversal: open the worktree root component by component with
   `O_DIRECTORY | O_NOFOLLOW`, then walk only through directory descriptors (e.g. a small Python
   helper on `os.fwalk(dir_fd=..., follow_symlinks=False)`, `os.chown(name, uid, gid,
   dir_fd=..., follow_symlinks=False)`), never re-resolving a path string, staying on one device
   and skipping symlinks.

It would run as a root-owned oneshot unit triggered by a scan-worker request naming one ChangeSet
UUID (the `engineering-github-policy-reconciler.path` pattern). Until such a design is reviewed,
recovery stays the manual command above.

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
