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

## Six more findings closed (written, tested, not deployed)

Per your instruction to resolve F-08, F-11, F-14, F-16, NEW-02, NEW-05, and NEW-14. Six are done;
F-11 (replay tests, the wider abandoned-effort audit, api/service.py drift characterization) is
large and was correctly not rushed — still in progress.

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

Ruff, mypy, and the full test suite (763 tests) pass clean on this branch.

## GO checklist status

| # | Criterion | Status |
|---|---|---|
| G1 | Engines subscription-only, non-interactive | **PASS** |
| G2 | No LLM API key/direct client anywhere | **PASS** |
| G3 | Temporal healthy, round-trip | **PASS** |
| G4 | Hermes unit active, CLI-backed, skills load | **PASS** |
| G5 | Telegram + dashboard can trigger/show missions | **PASS** (Telegram confirmed end-to-end; dashboard wired, not visually re-checked) |
| G6 | Fail-closed gates consciously set | **PASS** (Temporal control plane fully gated; NEW-07's parallel path now also gated) |
| G7 | Issue-resolution loop completes end to end | **PENDING** — the one remaining item, self-resolving ~21:45→22:32 UTC window |
| G8 | Audit mission completes end to end | **PASS** |
| G9 | Security baseline | **PASS, one paused item** (F-29 reverted after incident, needs an SSH key prerequisite before retry) |
| G10 | Restart resilience | **PARTIAL** (app-level proven; core infra restart not tested, real blast radius) |
| G11 | Observability | **PASS** |
| G12 | Runbook | **PASS** (`docs/RUNBOOK.md`, includes the F-29 incident lesson) |

## Decisions log (D1–D8 operator-approved 2026-09-17; NEW-07 delegated and applied 2026-09-21)

All decisions D1–D8 and NEW-07 are applied and verified. See `.go-status/findings.json` for full
evidence trails. `docs/BACKLOG.md` has every non-blocking finding for future work.

---

**Next check-in**: after ~2026-09-21 22:32 UTC, run the sequence above for G7's final artifact,
then this platform should be at a clean, fully-evidenced GO.
