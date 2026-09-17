# GO Status — 2026-09-17

## 1. Verdict

**NO-GO.** The platform is architecturally closer to ready than its own documentation trail
suggests — the Temporal mission-execution layer (workflows, task-queue fan-out, and the
Claude/Codex CLI adapters) is genuinely CLI-subprocess-based with API keys actively stripped
from every engine invocation, and most of the "residual API key" claims from the 2026-09-01
audit have been fixed. **But no mission has ever run end to end** (zero executions of any
issue-resolution, enhancement, or fresh audit workflow in the last 30 days), the two fail-closed
gates that would let one run (GitHub write policy, Kubernetes observer credential) are still
closed, the endpoint that would approve a full delivery is a hardcoded stub with no working code
path at all, and — separately from the mission layer — Hermes's own interactive/gateway chat
loop still makes a direct HTTP call to `chatgpt.com/backend-api/codex` instead of shelling out to
a CLI, which is a live Rule-1 violation today. The single most important reason the platform has
never reached GO: **the fail-closed gates were never consciously opened, and instead of executing
its own remediation plan after the 2026-09-01 audit, the platform spent the next two weeks
running a second, parallel self-review loop that produced ~170 more status files while never
running a real mission.**

## 2. GO checklist

| # | Criterion | Status | Evidence |
|---|---|---|---|
| G1 | Both engines run non-interactively as every runner user, subscription-only | **UNKNOWN** | Binaries + versions confirmed (`claude 2.1.239`, `codex-cli 0.149.0`); mission-layer health check shows both CONNECTED via native CLI (`engineering-ctl connections all`); but per-runner auth session state is UNVERIFIED (permission denied, no sudo) for all 6 runner identities (F-12) |
| G2 | No LLM API key or direct API client anywhere | **FAIL** | `engineering-control`'s own code is clean (no SDK imports, env-stripping confirmed). But `hermes-gateway.service` (uid 972, live today) makes a direct HTTP call to `chatgpt.com/backend-api/codex` with spoofed CLI headers (F-04) — a direct API client, even though OAuth-authenticated. An orphaned raw-key credential-pool entry also remains registered (NEW-02) |
| G3 | Temporal healthy; namespace/queues/workers correct; trivial round-trip | **PASS (with a caveat)** | Docker container healthy 10 days; live Python-SDK round-trip succeeded (namespaces, workflow queries); **no `temporal`/`tctl` CLI exists on the host at all** (NEW-06) — proof method had to be the SDK, not the CLI the checklist implies |
| G4 | Hermes unit active, provider CLI-backed, skills load without error | **FAIL** | `hermes-gateway.service` is active, but its provider is HTTP-backed (F-04, same as G2); whether skills loaded without error this boot is UNVERIFIED (journal inaccessible past the uid-972 migration) |
| G5 | Telegram and dashboard can each trigger a mission and show status | **UNKNOWN** | Both wired (bot token, allowlist, dashboard login-gated and reachable); neither was exercised to actually trigger a mission this pass; Telegram connection is STALE per live health check |
| G6 | All fail-closed gates consciously set and documented | **FAIL** | GitHub write policy and K8s observer credential are closed by default and were never consciously opened (F-01/F-02); no owner sign-off recorded anywhere in ~500 status files |
| G7 | Issue-resolution loop completes end to end on one real issue | **FAIL** | Zero executions in 30 days (F-03); even if triggered, full-delivery approval is a hardcoded 409 stub with no success path (NEW-01) — only the plain source-PR outcome is structurally reachable today |
| G8 | Audit mission completes end to end on one real target repo | **FAIL** | `AuditCampaignWorkflow` last ran 2026-08-30/31, not since; no fresh run or report this pass |
| G9 | Security baseline: no root, least-privilege kubeconfig, explicit sudo, secrets perms, isolation | **FAIL** | Secrets perms and runner isolation are actually good (0600 files, per-user 700 homes, no live root LLM processes); but a cluster-admin kubeconfig sits unremediated on the worker (F-01/F-06), and several root-configured units/oneshots remain undocumented as intentional (F-09) |
| G10 | Services survive restart without manual intervention | **UNKNOWN** | Not tested this pass |
| G11 | Observability: every run leaves a debuggable record | **PARTIAL** | DB/dashboard/Temporal all exist and are healthy; but the platform's own health check currently reports **DEGRADED** (NEW-05, github-broker stale since 09-01); dashboard-vs-live-Temporal staleness (F-19) unconfirmed |
| G12 | Runbook exists and matches reality | **UNKNOWN** | Not checked this pass — `docs/RUNBOOK.md` existence/accuracy needs a Phase-2 check |

## 3. Blockers ranked by dependency order

1. **F-04** — Hermes gateway HTTP→CLI cutover (a remediation script already exists in-repo; needs
   an operator decision on approach + a restart). Fixing this closes G2/G4 and removes the
   Rule-1 violation that is currently *live*, not hypothetical.
2. **F-01 / F-06** — Kubernetes: provision scoped observer credential(s), remove the cluster-admin
   kubeconfig. Blocks G8 (audit mission) and G9 (security baseline). Requires operator-provisioned
   cluster credentials — cannot be self-issued from here without using the very credential being
   removed.
3. **F-02 / F-26** — GitHub write policy: decide which repo(s) to open, fix the crash-looping
   expiry-reconciler unit, confirm the live policy file actually flips. Blocks G6/G7/NEW-05.
4. **NEW-01** — Implement the full-delivery approval endpoint (currently a hardcoded stub). This
   is a real code change, not a config flip; scope it as its own finding with its own definition
   of done before Phase 2 starts on it. Blocks the "full delivery" half of G7 (the plain
   source-PR path is otherwise reachable once #1–3 are fixed).
5. **F-09 / F-12 / NEW-04** — Sudo/verification access: without *some* narrow, explicit,
   documented sudo grant (or the operator running specific read-only commands and pasting output
   back), F-10, F-11, F-29, the live GitHub-policy value, and the production gateway's own config
   cannot be verified at all, only inferred. This blocks closing several other findings cleanly,
   not just G9.
6. **F-10** — GitHub bot identity (read `adapters/github/client.py` first; may turn out to be a
   pure code-read task, not blocked on #5).
7. **F-07** — Prove one real Telegram notification round-trips end to end. Blocks G5/G11.
8. **NEW-07** — Resolve whether the `audit-product`/`implement-issue` Hermes skills are a legacy
   doc or a live parallel execution path, before trusting Phase 3's Temporal-only proof as
   complete for G7/G8.
9. Everything else (F-08, F-11, F-13–F-20 except F-18/F-26 above, F-25, F-29, F-30) — not
   independently reverified this pass, does not block a first pilot mission, tracked in
   `docs/BACKLOG.md` or left OPEN/UNVERIFIED in the ledger for a later pass.

## 4. Decisions the operator must take

| # | Decision | Options | Recommendation | Unblocks |
|---|---|---|---|---|
| D1 | How to close F-04 (Hermes gateway HTTP→CLI) | (a) run the existing `scripts/prepare-hermes-c01-cutover.py` cutover to the `codex_app_server` subprocess runtime; (b) leave as-is and accept the OAuth-based HTTP call as a documented exception | (a) — the fix is already built and tested per its own test file; this is exactly what the mission's Rule 1 forbids and the tooling to fix it exists | G2, G4, F-04 |
| D2 | Kubernetes credential policy | (a) provision scoped least-privilege observer ServiceAccounts/kubeconfigs per project and remove the cluster-admin one; (b) keep cluster-admin but restrict its use in code (already partially done via `KubernetesObserver.verify_permissions` self-checks, but the credential itself is still admin) | (a) — the repo's own README already calls the current credential "prohibited"; (b) is a band-aid on top of an already-known-bad credential | F-01, F-06, G8, G9 |
| D3 | GitHub write policy scope | (a) open write access for one low-risk repo first (e.g. `llmwiki`) as a pilot, with a dedicated bot identity; (b) open all 5 repos at once; (c) keep closed and only prove the source-PR path without ever writing to GitHub | (a) — matches the mission's own default pilot suggestion (smallest open issue in llmwiki or falcone) and limits blast radius while G7 is still unproven | F-02, F-26, G6, G7 |
| D4 | GitHub bot identity | (a) provision a dedicated GitHub App/machine account with branch protection; (b) continue using the operator's personal token, documented as a known exception | (a) if D3 opens real writes; can defer if D3 chooses (c) | F-10 |
| D5 | Sudo/verification access for this audit | (a) grant `hermes` a narrow, logged, time-boxed sudo rule for specific read-only commands (e.g. `cat /etc/engineering-control/policy/*.yaml`, `journalctl -u hermes-gateway`, `sudo -u <runner> claude/codex --version`); (b) operator runs a fixed list of read-only commands themselves and pastes output back into `.go-status/notes/` | Either works; (a) is faster for the remaining Phase 2/3 work, (b) has zero new privilege surface | F-09, F-10, F-11, F-12, F-29, NEW-04 |
| D6 | Pilot mission target | Confirm: smallest open issue in `llmwiki` (per mission default) vs `falcone`, for the Phase 3 G7 proof | Follow the mission's own default (llmwiki) unless the operator has a specific repo in mind | G7 Phase 3 proof |
| D7 | The parallel `go-live` self-review loop (NEW-03) | (a) archive/freeze it now and adopt `.go-status/` as the sole ledger; (b) leave it running alongside this audit | (a) — it has run for two weeks without reaching GO and duplicates this effort under different IDs; freezing it (not deleting — move/label as superseded) stops the drift F-27 describes | F-27, NEW-03 |
| D8 | Long-lived runner auth tokens (F-12, Batch 0.3/0.4 in the prior plan) | Needs an interactive OAuth login as each runner user (`claude-runner`, `codex-runner`) — this cannot be done non-interactively by an agent | Operator performs the login sessions directly, or explicitly delegates a narrow one-time sudo session for it | F-12, G1 |

## 5. Non-blockers

Moved to `docs/BACKLOG.md`: F-08, F-11, F-13, F-14, F-16, F-17, F-18, F-20, F-25, F-28 (already
fixed, kept for record), F-29, F-30 (not applicable today), NEW-06.

---

**GATE 1 — waiting for GO.**
Decisions needed before Phase 2 remediation starts: **D1–D8 above.** Two of them (D2, D3) are
irreversible-adjacent (cluster credentials, GitHub write policy) per the mission's own rule 3 and
must not be actioned without explicit operator sign-off even after a general "GO."
