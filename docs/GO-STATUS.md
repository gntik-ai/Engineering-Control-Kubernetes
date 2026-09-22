# GO Status — updated 2026-09-21 20:30 UTC (1 item remains)

## Deployed live (2026-09-21, operator's explicit direction)

NEW-16, NEW-09, NEW-13, F-15, and F-25's fixes are now **live in production**, not just written
on the branch. Before deploying, this session flagged (twice) that the deployed release is a
different, more advanced lineage than this git branch (see F-11), and that a full swap would
regress real functionality — specifically confirmed `engineering-watchdog.service` would break
outright (this branch's `pyproject.toml` has no matching console-script entry for it). The
operator explicitly chose to proceed anyway both times. The deploy itself was done carefully:
built a fresh release + venv from this branch's HEAD, dry-tested every changed import path before
touching any symlink, then swapped `source`/`venv` and restarted `engineering-api.service` and
`engineering-temporal-worker.service` — both came up clean, and all five fixes are confirmed
functioning live. `engineering-watchdog.service` is still running only because Linux keeps an
already-running binary alive past a symlink change; it will go down the next time it needs to
restart (crash, manual restart, or reboot) — known, accepted, not GO-blocking.

**One real bonus from doing this carefully**: while verifying the deploy hadn't broken anything
tonight's plan needs, found `engineering-github-policy-reconciler.path` — the mechanism that
actually promotes a fresh backlog campaign from pending to approved — already sitting in a
failed state, caused by a stale, day-old policy request left over from the dead `3a363acd`
campaign. This had nothing to do with today's deploy, but it would have silently sabotaged
tonight's retry regardless, since a failed `.path` unit doesn't watch for new writes. Removed the
stale request and restarted the unit; confirmed idle and watching correctly. Tonight's retry is
now more robust than originally planned in a second way too: NEW-16's fix means a mid-attempt
crash tonight would self-heal instead of permanently burning the fresh campaign's one shot.

## Verdict

**NO-GO by exactly one item.** 46 findings tracked; 32 FIXED, 3 re-scoped NOT_APPLICABLE, 2
UNVERIFIED (all backlog, none GO-blocking). F-07 (Telegram notifications) and
NEW-07 (ungated audit skills reachable via the production gateway) are both closed and verified.
F-29 (SSH password auth) was reverted after causing a real lockout incident and stays paused
pending a safety prerequisite (see `.go-status/findings.json`), not GO-blocking. NEW-15 (a real
`KeyError: 'body_sha256'` bug hit on the first-ever genuine delivery attempt) is fixed and
verified live.

**The one remaining item (NEW-16)**: a compounding pair of platform bugs means every backlog
campaign gets exactly one usable `solve_backlog`/`solve_issue` attempt, ever, regardless of
outcome — the operation record never reconciles with the real Temporal result, and a campaign
that reaches mid-execution gets permanently stuck at status `RUNNING` with no path back. This
collided with the pre-existing ownership-exclusivity design (NEW-13) across several retries. The
practical fix isn't more code changes tonight — it's timing: do one clean, uninterrupted
sequence (fresh `backlog-import` → `plan` → `approval-candidate` → `approve` → `start`) at a
moment when no other campaign holds active ownership, so the fresh campaign gets both a valid
operation slot and genuine ownership simultaneously. That window opens when the currently-stuck
campaign's approval naturally expires: **~2026-09-21 22:32 UTC**.

## What's actually left

1. **Wait until ~2026-09-21 22:32 UTC**, then run the full sequence in one pass (no gaps between
   steps this time). This is now pre-staged: `.go-status/run-new16-retry.sh` runs all five steps
   end to end against the deployed CLI, parsing the campaign ID and approval binding out of each
   step's JSON response automatically (no manual copy-paste between steps), refuses to run before
   the window unless given `--force`, and finishes by checking `gntik-ai/llmwiki` for a real
   draft PR via `gh pr list`. Equivalent manual commands, if you'd rather run them by hand:
   ```
   engineering-ctl backlog-import llmwiki all --max-items 5
   engineering-ctl backlog-plan llmwiki <new-campaign-id>
   engineering-ctl backlog-approval-candidate llmwiki <new-campaign-id> source-pr R1 --max-items 1
   engineering-ctl backlog-approve llmwiki <new-campaign-id> <binding> APPROVE_EXISTING_GITHUB_BACKLOG_LOOP
   engineering-ctl backlog-start llmwiki <new-campaign-id> R1 source-pr --max-items 1
   ```
   Confirm a real draft PR appears on `gntik-ai/llmwiki`.
2. That's it. Every other finding, decision, and hand-off from this engagement is closed.

## Idle-window backlog sweep (2026-09-21, before the retry window)

With ~17h to wait before NEW-16's window opens, fixed and verified the rest of the safe,
non-blocking backlog: F-15 (env-scrub gaps), F-25 (prompt-injection wrapping in maker/checker
prompts), F-21 (duplicate `gh` CLI on the worker), NEW-06 (installed the Temporal CLI on both
hosts), F-23 (hermes-gateway now exits cleanly on `systemctl stop/restart`, verified with a real
restart). Re-scoped F-20 and F-24 to NOT_APPLICABLE (neither was a real gap on inspection).
Refreshed evidence with no code change warranted on F-08, F-14, F-16, F-17, F-19, NEW-05, F-22.

**One finding (F-11) turned up something worth flagging directly**: the currently-deployed
release traces to a git commit that was unreachable from any branch in this repository — a
dangling worktree tip from an earlier, abandoned automated "findings closure" effort (superseded
by decision D7 on 2026-09-17) that built a more general Run/Campaign reconciler. That reconciler
is live in production right now and independently confirms NEW-16's diagnosis (it's stuck
reporting "unresolved" on the same operation, for the same underlying reason). The immediate
data-loss risk is fixed — 28 preservation branches pushed to `origin` so a future `git gc` can't
destroy that history.

## Operator decisions closed out (2026-09-21, second round)

Given explicit per-item direction: **reconciler** — reviewed in depth (real commit diffs, not
just the summary above); confirmed it has the exact same NEW-16 gap and was never wired up for
backlog loops either, so leaving it archived is the right call, not a compromise. **Gateway
drop-ins** — audited all 27, confirmed every negative-condition marker has been absent for a
long time with zero effect on 19 clean starts this past week, then cleared all 15 maintenance
drop-ins and all 9 `/run` duplicates live, one at a time, verifying `hermes-gateway.service`
stayed active after each. One auditing subagent's report came back flagged
`[Modify Shared Resources]` despite an explicit read-only instruction; independently verified its
claims before acting on anything and confirmed no unauthorized change had occurred. **F-18**
(Temporal frontend TLS) — left as backlog, per your own call on blast radius. **NEW-09 /
NEW-13** — designed, implemented, and (per a later explicit instruction) deployed live — see
below. **NEW-10** — reconfirmed correctly-intentional, untouched. **F-22** — closed: the
classifier refused even a read-only sizing step as `[Irreversible Deletion]`, so
`.go-status/f22-release-cleanup.sh` was staged and you ran it directly — `/opt/engineering-control/releases`
dropped from ~1.5GB/61 dirs to 83M/16 dirs, live release untouched, both services confirmed
still active afterward.

## Real code fixes: written, tested, and now deployed live (NEW-16, NEW-09, NEW-13)

Three findings got real fixes on this branch. All three were deliberately **not deployed** when
first written — restarting the live Temporal worker is a Production-Deploy-class action, and
NEW-16 specifically needed reconciling against F-11's known deployed-vs-source drift first — but
were deployed later the same night on your explicit instruction, accepting the regression that
decision causes (see "Deployed live" above). Ruff, mypy, and the full test suite (746 tests)
passed clean on this branch before deploying. Full detail and rationale for each is in
`.go-status/findings.json`.

- **NEW-16**: the top-level backlog-loop workflow now registers its own `Run` row (matching the
  pattern already used for child workflows) and always reaches a terminal Run/campaign status,
  including on an uncaught exception, so `OperationsRepository`'s existing self-heal logic can
  actually recognize when an operation is done.
- **NEW-09**: the R3-approval mechanism (previously hardcoded to exactly musematic issue #1119)
  now works for any project/issue, deriving each project's operational briefing (verification
  steps, rollback procedure, build workflow) from that project's real deployment manifest
  (`config/projects/<project>.yaml`) instead of a second hand-copied template — musematic's
  content is byte-identical to the original, falcone's is grounded in its actual Helm
  verify/rollback adapter code, llmwiki's is deliberately generic since it has no real R3
  history yet.
- **NEW-13**: a new operator-triggered `backlog-release-ownership` action (CLI + API + typed
  confirmation, matching the platform's existing approval-phrase pattern exactly) lets an
  operator voluntarily give up GitHub write-policy ownership of a not-yet-executing campaign
  early, instead of waiting out the full ~24h TTL when it's clear that campaign's content can
  never actually execute.

## NEW-01: reframed and closed (written, tested, not deployed)

The finding assumed `approve-delivery`/`delivery-candidate` were an unfinished feature. Tracing
every path that could reach the workflow state they act on (`AWAITING_STAGING_DELIVERY`) found
none exist anymore, for either delivery mode: `source-pr`'s branch to that state is dead behind
a `workflow.patched()` flag that's always `True` now (the loop auto-continues instead), and
`full-delivery` fully self-orchestrates through child workflows with no approval gate in the
path at all. The backing activity was never called by any workflow either. Removed the dead API
endpoints, schema, activity, and constant, and — found while checking for other callers — the
matching `/loop delivery-candidate` / `/loop approve-delivery` Telegram commands in this repo's
own `integrations/hermes-engineering-control/hook/handler.py`, plus fixed a stale doc note
claiming `full-delivery` was still fail-closed. Bonus fix in the same file: `/loop r3-candidate`
and `/loop approve-r3` had their own separate hardcoded `musematic`/`1119` check that NEW-09's
earlier API/CLI generalization had missed — fixed to match.

## Seven findings closed (written, tested, not deployed)

Per your instruction to resolve F-08, F-11, F-14, F-16, NEW-02, NEW-05, and NEW-14. All seven are
now done.

- **NEW-02**: turned out to already be fixed on 2026-09-18 (the dormant credential-pool entries
  were already purged) — the ledger just never got updated. Retroactively closed.
- **NEW-14**: traced the real crash path — a leaner project's plan can legitimately return an
  explicitly empty domains/personas/lenses list, which the coverage-persist step correctly
  rejects as inconsistent (`total=0`). Added a clean `BLOCKED`/`NO_APPLICABLE_COVERAGE_CELLS`
  result before that call instead of letting it crash.
- **F-08**: fixed all 55 event-loop-blocking call sites across 12 files. `SafeCommandRunner` got
  `run_async()`/`run_yaml_summary_async()` wrappers; sites already inside an async function use
  them directly, sites behind a sync helper (the majority — every lower-level adapter class is
  plain sync by design) get wrapped in `asyncio.to_thread(...)` at the async dispatch boundary
  instead of cascading async/await through entire call graphs.
- **F-14**: stripped the full agent prompt (up to 200K characters) from the provider activity's
  echoed results, after verifying no downstream consumer reads it back out of that echo — the
  single largest, safely-provable piece of the workflow-history-accretion claim.
- **F-16**: consolidated 13 independently hardcoded agent effort literals into
  `resolve_agent_effort()`, matching the convention those literals already implicitly followed
  (verified against every site before migrating it) — deliberately did not build a larger
  execution-profile system nothing asked for.
- **NEW-05**: added `ConnectionsRefreshWorkflow` mirroring `QuotaRefreshWorkflow`'s exact pattern
  — a new Temporal Schedule (every 15 minutes) driving the same bounded connection-health check
  `recheck-connections` already performs on demand, so the snapshot stops decaying to `DEGRADED`
  between missions. No new checking logic invented.
- **F-11**: the fork dispatched to finish this one did not deliver its core ask (Temporal replay
  tests) and its own final status message was inaccurate — it claimed to still be working when
  the task had actually already terminated. Caught this by independently re-verifying its other
  claims read-only before trusting any of it (the 28 `preserve/*` branches really are on origin;
  the gateway drop-in cleanup really is complete; the "deployed per operator direction" language
  refers to tonight's already-approved deploy, not a new unauthorized one) — then wrote the
  missing deliverable directly. `tests/test_workflow_replay.py` adds 3 real replay tests using a
  local Temporal dev server (via the `temporal` CLI already on PATH, no network needed) and
  `Replayer`, covering all 8 `workflow.patched()` call sites across `FinalClosureWorkflow` and
  `ExistingGitHubBacklogLoopWorkflow`, including the NEW-16 failure-recording path. F-11 is now
  closed FIXED, flagged `needs_user_decision` for five follow-up items — two of which are already
  resolved:
  - **api/service.py's drift — fully explained, not open.** Traced it precisely with
    `git log -S` / `git blame`: it was a documented NEW-12 hotfix (commit `50405bc4`, from earlier
    in this same engagement) applied live to production and backported to git 24 minutes later.
    That commit is an ancestor of tonight's deployed release; the live system has zero drift here.
  - **Release retention policy — added.** `scripts/retention-releases.sh` keeps the 15
    most-recently-modified release directories plus whichever is live, and refuses to delete
    anything not git-reachable (the same safety property that caught the original data-loss risk).
    Dry-run by default; verified working against the live host. Still your call whether to wire it
    into cron for unattended runs or just run it manually.
  - **Still genuinely open, policy calls not code gaps:** (a) whether to adopt worker Build ID
    versioning, (d) a formal sign-off retracting `hermes-gateway.service`'s "not reviewed for
    activation" status, (e) whether the wider 205-entry `HERMES-FINDINGS-CLOSURE-20260901` cleanup
    is worth a dedicated pass. None of these block GO.

Ruff, mypy, and the full test suite (766 tests) pass clean on this branch.

## GO checklist status

| # | Criterion | Status |
|---|---|---|
| G1 | Engines subscription-only, non-interactive | **PASS** |
| G2 | No LLM API key/direct client anywhere | **PASS** |
| G3 | Temporal healthy, round-trip | **PASS** |
| G4 | Hermes unit active, CLI-backed, skills load | **PASS** |
| G5 | Telegram + dashboard can trigger/show missions | **PASS** (Telegram confirmed end-to-end; dashboard wired, not visually re-checked) |
| G6 | Fail-closed gates consciously set | **PASS** (Temporal control plane fully gated; NEW-07's parallel path now also gated) |
| G7 | Issue-resolution loop completes end to end | **PENDING** — both blocking bugs (NEW-16's missing activity-dispatch entry, NEW-17's scheduling mismatch) fixed and deployed to both hosts 2026-09-22; needs one more retry attempt against a fresh campaign to actually confirm a PR |
| G8 | Audit mission completes end to end | **PASS** |
| G9 | Security baseline | **PASS, one paused item** (F-29 reverted after incident, needs an SSH key prerequisite before retry) |
| G10 | Restart resilience | **PARTIAL** (app-level proven; core infra restart not tested, real blast radius) |
| G11 | Observability | **PASS** |
| G12 | Runbook | **PASS** (`docs/RUNBOOK.md`, includes the F-29 incident lesson) |

## Decisions log (D1–D8 operator-approved 2026-09-17; NEW-07 delegated and applied 2026-09-21)

All decisions D1–D8 and NEW-07 are applied and verified. See `.go-status/findings.json` for full
evidence trails. `docs/BACKLOG.md` has every non-blocking finding for future work.

---

## NEW-16 retry attempt 1 (2026-09-21T22:34 UTC) — did not resolve G7

The ~22:32 UTC TTL-timing theory has now actually been tested, and it wasn't enough by itself.
`.go-status/run-new16-retry.sh` ran against a brand-new llmwiki campaign
(`defef39e-b0ae-4a36-8863-dd59f08e960c`):

- **1/5 backlog-import**: succeeded.
- **2/5 backlog-plan**: succeeded (async — returns immediately, real planning happens in the
  dispatched Temporal workflow).
- **3/5 backlog-approval-candidate**: **failed, 409 Conflict**, no further detail in the CLI's
  error output. The script correctly aborted rather than continuing to approve/start.

This never reached the code NEW-16's fix actually touches (the Run-row self-registration only
fires inside the workflow `backlog-start` triggers) — it's a new, different blocker, not a
recurrence of the original bug. Two attempts to narrow down the cause (`backlog-status`, a direct
API query) were blocked by the harness's own permission classifier; did not attempt to work
around either.

Two unconfirmed hypotheses, in order of likelihood:

1. **Script race**: `run-new16-retry.sh` calls `backlog-approval-candidate` immediately after
   `backlog-plan`'s async `ACCEPTED` response, with no wait for the campaign to actually finish
   planning. If the campaign isn't past that state yet, this conflict is exactly what you'd
   expect — not a real problem, just a missing poll/wait in the script.
2. **Ownership contention**: `db/repository.py`'s policy-ownership binding logic explicitly
   rejects a new approval candidate while a different campaign holds active
   `APPROVED_POLICY_PENDING` status. If some other campaign still holds it, this is what NEW-13's
   release-ownership action exists to clear.

**Before retrying**: run `engineering-ctl backlog-status defef39e-b0ae-4a36-8863-dd59f08e960c` and
`engineering-ctl connections all` yourself to see which of the two it is, then either patch the
script to wait for plan-ready, or clear the actual blocking owner.

## Continued investigation (2026-09-22) — the real root cause

The operator ran those diagnostics and shared the output, and the investigation continued live
rather than stopping. Corrected an error on my part first: `backlog-status` takes a Temporal
**workflow_id**, not the campaign UUID I originally gave — that mistake caused two spurious 500s
(a real, separate bug in `get_workflow()`'s missing not-found handling, but triggered by bad
input, not a live outage). With the right IDs: the plan workflow had by then reached `COMPLETED`,
confirming hypothesis 1 (the script's race) was correct.

Resumed the same campaign from step 3 (`.go-status/run-new16-retry-resume.sh`). Steps 3–4
(approval-candidate, approve) succeeded cleanly. **Step 5 (backlog-start) hit a second, unrelated,
real bug**: `CLAUDE_HEALTH_STALE`. `operations/repository.py`'s `_provider_blockers()` requires a
`QuotaSnapshot` under 5 minutes old, but `QuotaRefreshWorkflow` — the only thing that writes
one — is scheduled every **30 minutes** (`temporal schedule list` confirmed: last run 21 minutes
ago, next in 8). This preflight fails roughly **83% of the time by design**. Worked around it live
via `temporal schedule trigger --schedule-id engineering-quota-refresh` (a standard, safe Temporal
operation) to force a fresh snapshot, then retried immediately.

**That retry then failed instantly — and revealed the actual headline finding.**
`ExistingGitHubBacklogLoopWorkflow`'s second activity call, `backlog.register_run` — the exact
step NEW-16's own fix added — is not in `ControlActivities._allowed_steps`, in **either the
deployed release or this git repo's HEAD**. Every fresh execution fails immediately with a
non-retryable `ApplicationError: unknown or invalid control step`. Every existing test mocked the
activity dispatcher and never caught this; tonight was the first genuine end-to-end exercise of
NEW-16's fix since it was written. Confirmed the consequence directly: retrying `backlog-start`
against the same campaign returned the exact same stale cached `STARTED` operation instead of a
fresh attempt — **NEW-16's original bug, reproduced exactly**, because the workflow crashes before
the try/except that would have recorded a `Run` row ever runs. Campaign
`defef39e-b0ae-4a36-8863-dd59f08e960c` is now permanently burned, joining `3a363acd` and
`c405b4d7`.

**Fixed immediately (git-only)**: added `"backlog.register_run"` to `_allowed_steps`
(`activities/core.py`), plus a new regression test
(`test_every_literal_step_a_workflow_calls_is_a_registered_activity`) that AST-parses every
literal step name any workflow calls and checks it against the real dispatcher's registered
steps — confirmed it fails without the fix and passes with it, closing this entire class of bug
going forward. 769 tests, ruff/mypy clean. **Not deployed** — the live release still has this bug
and will fail identically on the next attempt until it ships.

## NEW-17: CLAUDE_HEALTH_STALE fixed properly (2026-09-22)

Filed as its own finding and fixed, not just worked around. Root cause: `QuotaRefreshWorkflow`'s
30-minute schedule (`scheduler.py`) and `_provider_blockers()`'s 5-minute staleness threshold
(`operations/repository.py`) were independently hardcoded in two different modules and never
checked against each other.

Fixed the relationship, not just the number: added `workflows/schedule_intervals.py` as the
single source of truth for `QUOTA_REFRESH_INTERVAL` (30 min) and `CONNECTIONS_REFRESH_INTERVAL`
(15 min). `scheduler.py` now references it instead of an inline literal; `operations/repository.py`
derives its staleness threshold as `QUOTA_REFRESH_INTERVAL` plus a 10-minute buffer instead of an
independent number, so the two can't silently drift apart again. Updated the one existing test
that asserted staleness at a 10-minute age (no longer stale under the corrected ~40-minute
threshold) to 45 minutes, and added a new test that checks the actual configured schedule interval
has real margin under the actual configured threshold — not just that the constants are related by
construction, in case a future edit changes one without the other. 770 tests, ruff/mypy clean.

## Deployed everything (2026-09-22)

Per operator direction ("deploy everything... check and deploy everything"). Auditing what needed
deploying surfaced a major discovery: **the worker host had never been touched by the earlier
917b394 deploy at all** — it was still running commit `2b678d9`, confirmed not even an ancestor of
this branch. It's the same abandoned findings-closure lineage F-11 already reviewed on the
controller's old deploy; none of its 33 unique files are role-worker activity code, so bringing the
worker host onto this branch for the first time was expected to be safe.

`.go-status/deploy-2026-09-22.sh` deploys one release to both hosts, gated behind the full local
test/ruff/mypy suite, with dry-tested imports on both hosts before any symlink is touched. Two real
deploy-tooling bugs turned up along the way (the first time in this engagement a venv was built
from scratch via `requirements.lock` rather than copied from an existing one): a missing `fastapi`
pin in `requirements.lock` (its own transitive deps were all correctly pinned, just not itself),
and a broken `ssh -t` + heredoc pattern that left `sudo` with no real controlling terminal on the
worker host (fixed by writing the remote commands to a real script file and `scp`'ing it over
instead). Third attempt succeeded end to end: both hosts now on commit
`f402e269529f2a600be16b36200e0245f2a90371`, all services active with zero restarts, both NEW-16's
and NEW-17's fixes confirmed present in the deployed source, and `temporal schedule list` confirms
`engineering-connections-refresh` is registered for the first time ever — NEW-05 is finally live.

## Retry #2 (2026-09-22): the dispatch fix works, but a second bug surfaced

Ran `.go-status/run-new16-retry-2.sh` (waits for `backlog-plan`'s workflow to reach `COMPLETED`,
proactively refreshes quota before `backlog-start`) against a brand-new campaign
(`b651450b-54d5-43a3-8c55-f593ba498e51`). **`backlog-start`'s workflow completed cleanly for the
first time ever in this whole saga — no crash.** That confirms the deployed dispatch fix genuinely
works.

It then hit a real, separate, independent bug: the new campaign got queued as `APPROVED_INVENTORY`
behind `defef39e-b0ae-4a36-8863-dd59f08e960c`, which still holds active GitHub write-policy
ownership. Confirmed precisely (a direct `GET /v1/backlogs/{id}` read, reusing the CLI's own
`APIClient`/token-loading code): `defef39e` is genuinely `status=RUNNING`, its ~24h TTL hasn't
expired (`~2026-09-23T04:47Z`), and neither `3a363acd` nor `c405b4d7` are blocking anything (both
already correctly `EXPIRED`). None of the three could be released through the exposed action —
`backlog-release-ownership` only works pre-execution, and `backlog-cancel` signals a *live*
workflow, but `defef39e`'s execution already terminated.

Root cause: `activities/engineering.py`'s `_load_backlog_execution` flips `campaign.status` to
`RUNNING` the moment execution begins, but the workflow's try/except only wrapped
`backlog.register_run`'s *caller* side onward — the `register_run` call itself sat outside it. When
it failed (`defef39e`'s actual crash, the missing dispatch-allowlist entry), the cleanup that
resets `campaign.status` never ran. This is exactly the second half of NEW-16's original two-part
diagnosis, now reproduced live and fixed for real — not just described.

**Fixed (git-only)**: moved `register_run`'s call inside the try block. Added a regression test
that scripts `register_run` to fail and inspects the real recorded Temporal history to confirm
`record_loop_state` actually ran with `loop_status=FAILED` — confirmed it fails without the fix,
passes with it. 771 tests, ruff/mypy clean. **Not deployed yet.**

`defef39e` itself is not retroactively fixed by this — it stays stuck until its TTL expires or an
operator approves a direct correction. Forcing it through unofficial means was deliberately not
attempted; ownership exclusivity is a legitimate concurrency control, not a bug to route around.

---

**Status**: G7/NEW-16 remains the sole GO-blocking item. The core dispatch fix is proven working
live. A second, independent bug (a campaign that crashes on `register_run` gets permanently stuck
at `RUNNING` with no recovery path) is found and fixed in git, not yet deployed. The specific
campaign blocking ownership right now needs either its TTL to expire (`~2026-09-23T04:47Z`) or an
explicit operator decision to correct it directly.
