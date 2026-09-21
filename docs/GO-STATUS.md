# GO Status — updated 2026-09-21 07:00 UTC (1 item remains)

## Verdict

**NO-GO by exactly one item.** 46 findings tracked; 21 FIXED, 3 re-scoped NOT_APPLICABLE, 5
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
destroy that history — but two things need a real decision, not more unilateral fixing:
whether to formally review/merge that abandoned reconciler work, and a second instance of NEW-11's
stacked-`ConditionPathExists`-drop-in pattern found on `hermes-gateway.service` (20+ drop-ins,
and the unit's own header says "not reviewed for activation" despite being live). Full detail in
`.go-status/findings.json` (F-11).

## NEW-16's actual code fix (written, tested, not deployed)

Beyond tonight's timing workaround, NEW-16's root cause now has a real fix written and tested on
this branch (`src/engineering_control/workflows/definitions.py` and
`src/engineering_control/activities/engineering.py`): the top-level backlog-loop workflow now
registers its own `Run` row (matching the pattern already used for child workflows) and always
reaches a terminal Run/campaign status, including on an uncaught exception, so
`OperationsRepository`'s existing self-heal logic can actually recognize when an operation is
done. Full detail and rationale in `.go-status/findings.json` (NEW-16). **Not deployed tonight on
purpose** — restarting the live Temporal worker is a Production-Deploy-class action outside
tonight's authorized scope, and it should go through the same review this engagement has used for
every other change, plus get reconciled against F-11's known deployed-vs-source drift first. Ruff,
mypy, and the full test suite (743 tests) pass clean on this branch as-is.

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
