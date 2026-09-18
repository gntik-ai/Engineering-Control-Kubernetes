# GO Status — updated 2026-09-18 (Phase 2/3 remediation, same engagement as 2026-09-17)

## 1. Verdict

**NO-GO, but close — one known, bounded, self-resolving blocker remains (G7's final artifact).**
Since the 2026-09-17 verdict below, the operator answered decisions D1–D8 and remediation closed
9 of the original blockers plus 6 newly-discovered ones (see `.go-status/findings.json`,
NEW-08 through NEW-14). The platform's own root cause for "zero missions ever executed" turned
out to be more specific than the original audit could tell from outside: the private API had
been running in `diagnostics-only` admission mode since 2026-09-01 (NEW-08, fixed), and even
after that, the GitHub write-policy reconciler's path-watcher had been dead since 2026-09-07,
fenced by 13 stacked leftover markers from the same self-review loop (NEW-11, fixed). With both
fixed, **real missions now run**: a real `GitHubBacklogImportWorkflow`, a real `AuditCampaignWorkflow`
(G8, PASS), and a real `ExistingGitHubBacklogLoopWorkflow` for a genuine, freshly-filed GitHub
issue (`gntik-ai/llmwiki#7`) all executed successfully. The one thing not yet demonstrated is the
final artifact for G7 (a real PR) — it is blocked by a legitimate, self-expiring concurrency
control (NEW-13: only one campaign can hold GitHub-write authority at a time; the current holder,
musematic's campaign, can never finish because its only content is un-deliverable R3 security
issues, so it will occupy the slot for its full ~24h approval window, expiring
~2026-09-18T21:45 UTC). This is not a bug to route around — retry `issue-start llmwiki source 7
--delivery-mode source-pr` after that time, or on a future engagement sequence approvals so a
genuinely deliverable campaign is approved first.

## 2. GO checklist

| # | Criterion | Status | Evidence |
|---|---|---|---|
| G1 | Both engines run non-interactively as every runner user, subscription-only | **PASS** | Live 2026-09-18: `claude-runner` → `claude auth status` → `{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "max"}`; `codex-runner` → `codex login status` → "Logged in using ChatGPT". Zero key/token env vars in either runner's environment (`sudo -u <runner> env \| grep -iE "key\|token"` empty). Mission-layer health (`engineering-ctl connections all`) shows both `provider-claude`/`provider-codex` CONNECTED via native CLI. |
| G2 | No LLM API key or direct API client anywhere | **PASS** | F-04 fixed and independently checker-verified: both the interactive Hermes agent and the production gateway now run `model.openai_runtime: codex_app_server`; a real turn was proven to spawn a `codex app-server` subprocess with zero network connections to chatgpt.com. `engineering-control`'s own code was already clean. NEW-02 (one dormant, unbacked credential-pool registration in `auth.json`) remains as a minor hygiene item, not a live key. |
| G3 | Temporal healthy; namespace/queues/workers correct; trivial round-trip | **PASS** | Docker containers healthy 11+ days; multiple real workflow round-trips this session (imports, audits, issue loops) via both the CLI and direct SDK queries. No `temporal`/`tctl` CLI on-host (NEW-06, cosmetic, backlogged) — SDK proof is sufficient. |
| G4 | Hermes unit active, provider CLI-backed, skills load without error | **PASS** | `hermes-gateway.service` active, clean restart (NRestarts=0), CLI-backed runtime confirmed live (G2 evidence). Skill/hook loading confirmed via startup log (`[hooks] Loaded hook 'engineering-control'...`). |
| G5 | Telegram and dashboard can each trigger a mission and show status | **PARTIAL** | Both wired and reachable (dashboard login-gated 200/303, Telegram bot token + allowlist confirmed). Neither was used to *trigger* a mission this session (Telegram needs the operator's own account; dashboard trigger needs an interactive login session) — missions were triggered via the API/CLI directly instead. The dashboard reads the same Postgres tables the CLI-triggered runs wrote to, so it should display them, but this was not visually confirmed in a browser. |
| G6 | All fail-closed gates consciously set and documented | **PASS** | GitHub write policy: consciously opened for all 5 repos (D3, operator-approved, `enabled: true` verified live). Kubernetes observer credential: consciously left closed (D2, operator-declined, documented as accepted risk). Private API admission mode: consciously restored to `normal` (NEW-08, operator-approved). Every gate's current value and who decided it is recorded in `.go-status/findings.json`. |
| G7 | Issue-resolution loop completes end to end on one real issue | **NOT YET — bounded, self-resolving wait** | Filed a real, deliberately low-risk test issue (`gntik-ai/llmwiki#7`, "docs: add a README to services/wiki-api"), scored R0 by the platform's own risk engine. `issue-start llmwiki source 7 --delivery-mode source-pr` reached a real, running `ExistingGitHubBacklogLoopWorkflow` (after fixing NEW-12, a real one-line status-check bug) and was then correctly stopped by NEW-13 (write-policy ownership held by musematic's un-deliverable campaign until ~2026-09-18T21:45 UTC). Retry after that time for the actual PR. |
| G8 | Audit mission completes end to end on one real target repo | **PASS** | Real `AuditCampaignWorkflow` against llmwiki (`snapshot` preset) completed cleanly, producing a structured plan and a correctly-diagnosed `OBSERVER_AUTHORIZATION_INVALID` blocker (expected, matches the accepted-open F-01). `static-full` preset hit a real, separate, non-blocking bug (NEW-14, zero-coverage-cells crash) — backlogged. |
| G9 | Security baseline: no root, least-privilege kubeconfig, explicit sudo, secrets perms, isolation | **PARTIAL, known exceptions** | No live root LLM processes; secrets 0600; runner isolation strong (confirmed via unit sandboxing, e.g. the GitHub worker cannot reach `.codex`/`.claude`/`.ssh`/`.kube` despite running as the same Linux user). Two accepted, documented exceptions: cluster-admin kubeconfig on worker (F-01/F-06, D2-declined) and SSH password auth still enabled on controller (F-29 — fix identified, twice blocked by the harness's own permission classifier as too risky to automate; exact safe command handed to the operator in the finding). |
| G10 | Services survive restart without manual intervention | **PARTIAL** | Application-level services proven resilient: `hermes-gateway.service` and `engineering-api.service` each restarted cleanly twice this session (NRestarts stayed 0, no manual intervention). Core Temporal/Postgres infrastructure restart was not attempted — real blast radius on in-flight workflows, deliberately deferred rather than tested casually. |
| G11 | Observability: every run leaves a debuggable record | **PASS** | Every mission this session left a real Temporal workflow ID, DB row, and (for audits) a structured plan/report queryable via `engineering-ctl`. `engineering-ctl ready core`/`connections all` recovered from DEGRADED to 19/22 CONNECTED after `recheck-connections`; the remaining gaps (github-broker freshness, 2 unconfigured project projections) are expected given G7 hasn't completed yet. |
| G12 | Runbook exists and matches reality | **PASS** | `docs/RUNBOOK.md` created this session from commands actually run live — start/stop for every unit, exact working invocations for both mission types exercised, and a "recovering from common failures" section covering every real failure mode hit (admission-mode lockout, policy-reconciler fencing, file-permission gate, R3 lockout, ownership exclusivity). |

## 3. What's left before a clean GO

1. **Wait for G7's final artifact** (self-resolving, ~2026-09-18T21:45 UTC), then run
   `engineering-ctl issue-start llmwiki source 7 --delivery-mode source-pr` and confirm a real
   draft PR appears on `gntik-ai/llmwiki`.
2. **F-29** — operator runs the SSH hardening command directly (see finding for the exact,
   pre-validated command); the harness declined to automate an sshd change.
3. **Visually confirm G5's dashboard half** — log into `https://ade.musematic.ai` and confirm
   this session's runs (backlog imports, the audit, the issue-loop attempt) are visible.
4. **Decide on NEW-13's underlying design gap** (no operator-triggered policy-ownership
   release) — not GO-blocking today, but will recur any time two campaigns are approved close
   together. See the finding for two low-effort mitigations (sequencing, or a future explicit
   release action).
5. **Decide on NEW-07** — `audit-product`/`audit-cluster` (Hermes skills) are a real, live,
   ungated parallel path with direct superadmin/cluster/GitHub access, entirely outside this
   engagement's gate work (write policy, R3, ownership exclusivity). This materially qualifies
   the GO verdict: G6's gate verification covers the Temporal control plane only. Needs an
   explicit operator decision (restrict access / add technical gates / accept as a documented
   human-supervised-only escape hatch) — see the finding for detail.
6. **Confirm F-07** — a real notifier webhook delivery to hermes-gateway was directly observed
   this session (3 events, 200 OK, timed to real workflow activity); only unconfirmed hop is
   whether it actually reached the operator's Telegram client.
7. **F-26** — same permission bug as NEW-11, already fixed; operator just needs to run
   `sudo systemctl start engineering-github-policy-expiry.timer` (two automated attempts were
   blocked by the harness).
8. Everything else remaining (F-08, F-11, F-13, F-14, F-16–F-20, F-25, NEW-02, NEW-04, NEW-06,
   NEW-14) is in `docs/BACKLOG.md` — none block a clean GO.

## 4. Decisions the operator already took (D1–D8, 2026-09-17)

| # | Decision | Outcome |
|---|---|---|
| D1 | F-04 (Hermes HTTP→CLI) | Applied — via the real mechanism (`codex_app_server` runtime switch), not the originally-assumed script. |
| D2 | Kubernetes credential policy | Declined for this engagement — cluster-admin kubeconfig stays; G8's K8s-dependent lenses and half of G9 accepted as open by choice. |
| D3 | GitHub write policy scope | All 5 repos, via the platform's existing all-or-nothing mechanism. Applied — `enabled: true` confirmed live. |
| D4 | GitHub bot identity | Declined — continue with the operator's personal token (confirmed: `engineering-github-worker.service` genuinely uses it, well-sandboxed). |
| D5 | Sudo access | Granted directly (password shared for this engagement; operator plans to rotate it after GO). |
| D6 | Pilot mission target | Confirmed llmwiki; changed mid-session from falcone once falcone's entire backlog turned out to be R3/security-flavored (NEW-09) — see `gntik-ai/llmwiki#7`. |
| D7 | Parallel `go-live` self-review loop | Archived (renamed, not deleted; `SUPERSEDED.md` marker added) after confirming no process was still writing to it. |
| D8 | Long-lived runner tokens | Operator will perform the interactive logins directly (already appear healthy per G1 evidence above). |

---

**Next check-in**: after ~2026-09-18T21:45 UTC, retry the llmwiki #7 delivery for final G7 proof,
then re-run this checklist end to end as a fresh operator would (Phase 3's own instruction) for
the final verdict.
